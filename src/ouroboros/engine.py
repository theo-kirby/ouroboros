"""The loop. One iteration = actor call -> verify record -> commit -> critic -> budget.

Two roles run every iteration: the actor, which writes, and the critic, which reads
the change and the memory and writes the next prompt. Housekeeping is the actor's
iteration when the memory says it is due; a separate maintainer and planner exist
but are off unless the config turns them on.
"""

from __future__ import annotations

import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .budget import BudgetClock, backoff_seconds
import re

from .config import Config, parse_duration
from .gitguard import GitError, GitGuard
from .harness.backend_headless import kill_active
from .harness.base import Harness, Result
from .memory.base import MemoryAdapter
from .recorder import Recorder
from .roles.actor import build_actor_prompt
from .loops import LoopDetector, loop_reply
from .roles.critic import HISTORY_WINDOW, Critic, RulesCritic, Signals, Verdict

Sleeper = Callable[[float], None]

MAX_RESET_WAIT = 6 * 3600.0  # never trust a parsed reset time further out than this

CREATIVE_REPLY = (
    "The done criteria are met. Exhaustion policy: creative. Propose three new directions "
    "that serve the mission, write them in the plan, pick the most valuable one, and do one unit of it."
)
MAINTAIN_REPLY = (
    "The done criteria are met. Exhaustion policy: maintain. Run the tests, fix flakes, "
    "improve docs and structure, refactor carefully. Do not expand scope."
)
REPORT_DONE_REPLY = (
    "The done criteria are met. Exhaustion policy: report_done. Do one light maintenance pass "
    "(tests green, docs accurate), record it, and stop."
)
_EXHAUST = re.compile(r"##\s*Exhaustion policy\s*\n+\s*\**(creative|maintain|report_done)\**", re.IGNORECASE)


def exhaustion_policy(goal_text: str) -> str:
    m = _EXHAUST.search(goal_text or "")
    return m.group(1).lower() if m else "creative"


@dataclass
class IterationOutcome:
    iteration: int
    result: Result
    verdict: Verdict
    commit: str | None
    changed: bool
    recorded: bool
    housekeeping: bool = False


def _harness_of(role) -> str | None:
    """The harness a role just used. Roles hold a pool; a pool knows which one is active."""
    return getattr(getattr(role, "harness", None), "name", None)


@dataclass
class Engine:
    config: Config
    repo: Path
    harness: Harness
    memory: MemoryAdapter
    git: GitGuard
    recorder: Recorder
    budget: BudgetClock
    critic: Critic = field(default_factory=RulesCritic)
    maintainer: Harness | None = None   # only called when config.maintainer is on
    planner: Harness | None = None      # only called when config.planner is on
    sleeper: Sleeper = time.sleep
    goal_text: str = ""
    # loop state
    iteration: int = 0
    injected: str | None = None
    no_change_streak: int = 0
    error_streak: int = 0
    last_error: str | None = None
    last_ok_tag: str | None = None
    session_id: str | None = None
    since_plan: int = 0
    session_uses: int = 0
    failed_iterations: int = 0
    stuck_iterations: int = 0
    loops: LoopDetector = None   # type: ignore[assignment]  # built in __post_init__
    forced_plan: str | None = None   # a loop escalation demanding the next planner pass
    last_housekeeping: int = -1      # the iteration the actor last ran the maintainer's pass
    outcomes: list[IterationOutcome] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.loops is None:
            self.loops = LoopDetector(self.config.loop)
        # All role pools share the board; retain readings from failed fallback attempts too.
        board = getattr(self.harness, "board", None)
        if board is not None:
            board.usage = self.budget.usage

    # ------------------------------------------------------------------
    def run(self) -> str:
        """Run until a stop condition. Returns the stop reason. Never raises for harness trouble."""
        chain = " -> ".join(getattr(self.harness, "names", [self.harness.name]))
        self.recorder.log(f"run {self.config.run}: branch={self.git.branch} harness={chain} memory={self.memory.name} "
                          f"roles={'+'.join(self.config.active_roles)}")
        self._status("starting")
        try:
            self.memory.start(run=self.config.run, goal_text=self.goal_text, run_dir=self.recorder.run_dir, branch=self.git.branch)
            if self.git.has_changes():
                self.git.commit(f"ouroboros: start run {self.config.run}")
        except Exception as exc:
            self.recorder.log(f"memory start failed: {exc!r} (continuing)")
        reason: str | None = None
        while reason is None:
            try:
                self.step()
            except KeyboardInterrupt:
                killed = kill_active()
                reason = f"interrupted by user ({killed} child process(es) killed)"
                break
            except Exception as exc:  # the loop must survive its own bugs
                self.recorder.log(f"engine error: {exc!r}\n{traceback.format_exc()}")
                self.recorder.step(iteration=self.iteration, step="engine_error", error=repr(exc))
                self._sleep(backoff_seconds(0), "engine error")
            reason = self.budget.should_stop()
        self.recorder.log(f"stop: {reason}")
        self._status("stopped", stop_reason=reason)
        return reason

    # ------------------------------------------------------------------
    def step(self) -> IterationOutcome:
        self.iteration += 1
        n = self.iteration
        role = self.config.role("actor")
        # Housekeeping is an iteration, not a side call: the actor runs the maintainer's
        # pass, the critic judges it like any other change, and it shows in the history.
        housekeeping = self._housekeeping_prompt()
        if housekeeping:
            prompt = housekeeping
        else:
            prompt = build_actor_prompt(goal_text=self.goal_text, memory=self.memory, iteration=n, injected=self.injected)
        before = self.memory.snapshot()
        head_before = self.git.head()
        self._status("work", iteration=n, housekeeping=bool(housekeeping))

        result = self._call_actor(prompt, role.timeout_seconds, role.model, housekeeping=bool(housekeeping))

        recorded = self.memory.verify_recorded(before)
        worked = result.ok or result.timed_out  # the actor ran; a timeout still may have landed work
        changed = self.git.has_changes() or self.git.head() != head_before  # the actor may commit on its own
        if recorded:
            summary = self.memory.last_summary()
        elif housekeeping:
            summary = "housekeeping"
        else:
            summary = (result.error or "no record")[:60]
        commit = self.git.commit(f"ouroboros #{n}: {summary}", allow_empty=False)  # no marker commits; the ok tag marks the iteration
        self.recorder.step(iteration=n, step="commit", sha=commit[:10], changed=changed, recorded=recorded, cost=result.cost_usd,
                           housekeeping=True if housekeeping else None)
        progressed = worked and (changed or recorded)
        if progressed:
            self.memory.mark_iteration()
        if housekeeping and result.ok:
            self.memory.mark_reconciled()

        check_problem = None
        try:
            check_problem = self.memory.check_report()
        except Exception as exc:
            self.recorder.log(f"memory check raised {exc!r}")
        if check_problem:
            self.recorder.step(iteration=n, step="check", problem=check_problem[:300])

        self.no_change_streak = 0 if changed else self.no_change_streak + 1
        if result.error and result.error == self.last_error:
            self.error_streak += 1
        elif result.error:
            self.error_streak = 1
        else:
            self.error_streak = 0
        self.last_error = result.error

        # What this iteration moved, as opposed to what it touched. The frontier
        # is sampled here rather than at the end of the iteration, so it reflects
        # the previous maintainer pass -- a constant lag, and the signal only ever
        # compares one iteration against the next.
        files = self.git.changed_files(head_before, commit or "HEAD") if changed else []
        try:
            frontier = self.memory.frontier_fingerprint()
        except Exception as exc:   # a memory bug must not stop the loop
            self.recorder.log(f"frontier_fingerprint raised {exc!r}")
            frontier = None
        self.loops.observe_iteration(files=files, frontier=frontier, subject=summary)
        loop = self.loops.report()

        # The critic runs every iteration, changed or not: an iteration that changed
        # nothing is exactly the one that needs a verdict (stuck) and a redirect.
        self._status("critique", iteration=n)
        signals = Signals(
            result.text, changed, recorded, self.no_change_streak, self.error_streak, result.error,
            iteration=n, diff_stat=self.git.diff_stat(head_before, commit),
            diff=self._diff(head_before, commit) if changed else "",
            history=self.recorder.read_decisions()[-HISTORY_WINDOW:],
            memory=self._memory_context(),
            loop=self.loops.describe(),
            loop_fired=loop.describe() if loop else "",
            housekeeping=bool(housekeeping),
        )
        try:
            verdict = self.critic.judge(signals)
        except Exception as exc:  # a critic bug must not stop the loop
            self.recorder.log(f"critic raised {exc!r}; using rules")
            verdict = RulesCritic().judge(signals)
            verdict.reason = f"rules fallback (critic raised): {verdict.reason}"
        critic_cost = float(getattr(self.critic, "last_cost", 0.0) or 0.0)
        self.budget.add(cost=critic_cost, usage=getattr(self.critic, "last_usage", None),
                        harness=_harness_of(self.critic))
        self.recorder.decision(iteration=n, verdict=verdict.verdict, reason=verdict.reason, reply=verdict.reply,
                               did=verdict.did, doing=verdict.doing, fix_first=verdict.fix_first, source=verdict.source)
        self.recorder.step(iteration=n, step="critique", verdict=verdict.verdict, source=verdict.source,
                           reason=verdict.reason, cost=critic_cost)
        if loop is not None:
            self._escalate(n, loop, verdict)

        idle = False
        if verdict.verdict == "done_accepted":
            policy = exhaustion_policy(self.goal_text)
            nudge = {"creative": CREATIVE_REPLY, "maintain": MAINTAIN_REPLY}.get(policy, REPORT_DONE_REPLY)
            verdict.reply = f"{nudge}\n\n{verdict.reply}".strip()
            idle = policy == "report_done"

        if verdict.rejected and not (self.config.git.revert_on_reject and self.last_ok_tag):
            # Fix forward: the rejected commit stays, gets no ok tag, and the reply says what to fix.
            self.recorder.log(f"[{n}] rejected, kept on the branch ({'revert_on_reject is off' if not self.config.git.revert_on_reject else 'nothing accepted yet to revert to'}): {verdict.reason[:200]}")
            verdict.reply = f"The critic rejected the last iteration. Fix this first:\n{verdict.reply}".strip()
            self.error_streak = 0
        elif verdict.rejected:
            try:
                sha = self.git.revert_to(self.last_ok_tag, patch_out=self.recorder.run_dir / "reverted" / f"{n:04d}.patch")
            except GitError as exc:
                # A revert that cannot undo the reject is the one failure the loop
                # must not sleep through: rejected work would ride on to the merge.
                self.recorder.log(f"[{n}] revert FAILED, rejected work is still on the branch: {exc}")
                self.recorder.needs_human(
                    f"Iteration {n} was rejected, but reverting to `{self.last_ok_tag}` failed:\n\n    {exc}\n\n"
                    f"The rejected commit is still on `{self.config.run}`. Undo it by hand before merging."
                )
                self.recorder.step(iteration=n, step="revert", to=self.last_ok_tag, sha=self.git.head()[:10], error=str(exc)[:200])
            else:
                self.recorder.step(iteration=n, step="revert", to=self.last_ok_tag, sha=sha[:10])
            self.error_streak = 0
        elif verdict.verdict in ("continue", "answer", "done_rejected", "done_accepted") and self.config.git.tag_on_accept:
            tag = f"ouroboros/{self.config.run}/ok-{n:04d}"
            self.git.tag(tag)
            self.last_ok_tag = tag

        if verdict.fix_first:
            verdict.reply = f"Fix these first, before the unit:\n{verdict.fix_first}\n\n{verdict.reply}".strip()
        if result.error and verdict.verdict == "continue":
            note = f"The previous iteration ended with an error: {result.error[:500]}. Recover and continue."
            verdict.reply = f"{note}\n\n{verdict.reply}".strip()
        if check_problem:
            verdict.reply = f"{verdict.reply}\n\nThe memory checker reports a problem. Fix it first:\n{check_problem}".strip()
        self.injected = verdict.reply or None

        if progressed and not verdict.rejected:
            self.since_plan += 1
            if housekeeping and result.ok:
                self._maybe_plan(n, "after housekeeping")
            elif self.config.maintainer:
                self._maybe_reconcile(n)
            if self.forced_plan:
                self._maybe_plan(n, f"loop escalation: {self.forced_plan}")
                self.forced_plan = None
            elif verdict.verdict == "done_accepted":
                self._maybe_plan(n, "done accepted: plan the next directions")
            elif not self.memory.reconcile_prompt() and self.config.plan.every and self.since_plan >= self.config.plan.every:
                self._maybe_plan(n, f"every {self.config.plan.every} iterations")

        self.budget.add(cost=result.cost_usd, iteration=True, harness=result.extra.get("harness"),
                        done_accepted=(verdict.verdict == "done_accepted") if verdict.verdict.startswith("done") else None,
                        stuck=(verdict.verdict == "stuck"))
        self._status("idle", iteration=n, last_verdict=verdict.verdict)
        out = IterationOutcome(n, result, verdict, commit, changed, recorded, bool(housekeeping))
        self.outcomes.append(out)
        if idle and self.budget.should_stop() is None:
            self._sleep(parse_duration(self.config.idle_interval) or 1800.0, "done accepted; report_done policy")
        # A stuck verdict means the actor ran fine and changed nothing -- waiting on a
        # clock, a quota, or an instruction it cannot act on. Repeating it at full speed
        # buys nothing and still pays for a critic call every time, so slow down the same
        # way a run of failed iterations does. The stop
        # condition (stop.max_stuck) ends a run that is never going to recover.
        self.stuck_iterations = self.stuck_iterations + 1 if verdict.verdict == "stuck" else 0
        if self.stuck_iterations and self.budget.should_stop() is None:
            self._sleep(backoff_seconds(self.stuck_iterations - 1),
                        f"{self.stuck_iterations} stuck verdict(s) in a row")

        # a second net under the retry loop: iterations that keep failing for any reason slow down
        self.failed_iterations = 0 if result.ok or result.timed_out else self.failed_iterations + 1
        if self.failed_iterations and self.budget.should_stop() is None:
            self._sleep(backoff_seconds(self.failed_iterations - 1), f"{self.failed_iterations} failed iteration(s) in a row")
        return out

    # ------------------------------------------------------------------
    def _escalate(self, n: int, loop, verdict: Verdict) -> None:
        """Name the loop, then force a re-plan, then change the model.

        There is no fourth step. A loop is a thing to break out of, not a thing
        to die of: a false positive must never end a run nobody is awake for.
        The ladder ends at "try a different model" and the loop continues either
        way, so a wrong signal costs one wasted prompt, not a run.
        """
        step = self.loops.escalation(loop)
        act = self.loops.should_act(loop)
        self.recorder.step(iteration=n, step="loop", signal=loop.signal, streak=loop.streak,
                           escalation=step, acted=act, evidence=loop.evidence[:4])
        # Only a `continue` is overruled. If the critic already saw something
        # worse -- a reject, an unanswered question -- that judgement wins.
        if verdict.verdict == "continue":
            verdict.verdict = "looping"
            verdict.reason = f"{loop.signal} x{loop.streak}; critic said continue"
        # Naming the loop costs nothing, so the actor hears about it every time.
        verdict.reply = f"{loop_reply(loop, step)}\n\n{verdict.reply}".strip()
        if not act:
            return
        self.recorder.log(f"[{n}] loop: {loop.signal} x{loop.streak} (escalation {step}/3)")
        if step >= 2 and self.plan_enabled:
            self.forced_plan = f"{loop.signal} x{loop.streak}"
        if step >= 3 and self.config.loop.rotate:
            self._rotate_actor(f"{loop.signal} x{loop.streak}")

    def _rotate_actor(self, why: str) -> None:
        """Block the actor's current harness for a while, so its pool falls back.

        A different model often breaks a rut that another prompt cannot. This
        reuses the usage-limit machinery, so the harness un-blocks itself on the
        board's cooldown and nothing has to remember to bring it back.
        """
        board = getattr(self.harness, "board", None)
        if board is None or len(getattr(self.harness, "entries", []) or []) < 2:
            return
        name = self.harness.name
        if board.is_blocked(name):
            return
        board.block(name, why=f"loop escalation: {why}"[:120])
        self.recorder.log(f"loop: actor harness {name} rotated out ({why})")

    def _housekeeping_prompt(self) -> str | None:
        """The maintainer's pass, for the actor to run as this iteration.

        Only when no separate maintainer holds the job and the memory says a
        reconcile is due. Never two iterations in a row: a pass that fails to
        clear the tail must not eat the whole night trying.
        """
        if self.config.maintainer or self.last_housekeeping == self.iteration - 1:
            return None
        try:
            if not self.memory.needs_reconcile():
                return None
            prompt = self.memory.reconcile_prompt()
        except Exception as exc:
            self.recorder.log(f"housekeeping check raised {exc!r}")
            return None
        if not prompt:
            return None
        self.last_housekeeping = self.iteration
        self.recorder.log(f"[{self.iteration}] housekeeping: the actor runs the reconcile pass")
        return prompt

    def _maybe_reconcile(self, n: int) -> None:
        try:
            due = self.memory.needs_reconcile()
        except Exception as exc:
            self.recorder.log(f"needs_reconcile raised {exc!r}")
            return
        if not due:
            return
        prompt = self.memory.reconcile_prompt()
        if not prompt:
            return
        role = self.config.role("maintainer")
        harness = self.maintainer or self.harness
        self._status("reconcile", iteration=n)
        self.recorder.log(f"[{n}] reconcile: maintainer pass ({harness.name})")
        try:
            result = harness.run(
                prompt, cwd=self.repo, timeout=role.timeout_seconds, model=role.model,
                log_path=self.recorder.transcript_path(n, "maintainer"),
            )
        except Exception as exc:
            result = Result(exit_code=-1, error=f"harness raised {exc!r}")
        sha = self.git.commit(f"ouroboros #{n}: reconcile", allow_empty=False)
        self.budget.add(cost=result.cost_usd, usage=result.usage, harness=result.extra.get("harness"))
        self.recorder.step(iteration=n, step="reconcile", exit=result.exit_code, timed_out=result.timed_out,
                           error=(result.error or None) and result.error[:200], sha=sha[:10], cost=result.cost_usd)
        if result.ok:
            self.memory.mark_reconciled()
            self._maybe_plan(n, "after reconcile")
        else:
            self.injected = f"{self.injected or ''}\n\nThe last reconcile pass failed ({result.error or 'timeout'}). Record only; a maintainer will retry.".strip()

    # ------------------------------------------------------------------
    @property
    def plan_enabled(self) -> bool:
        return bool(self.config.planner)

    def _plan_signals(self) -> str:
        outs = self.outcomes[-self.config.plan.every :]
        stop = self.config.stop
        cap = f"of {stop.max_iterations}" if stop.max_iterations else "(no iteration cap)"
        elapsed = self.budget.elapsed / 3600
        left = f"{max(stop.after_seconds / 3600 - elapsed, 0):.1f}h left" if stop.after_seconds else "no wall-clock cap"
        lines = [
            f"- iterations so far: {self.iteration} {cap}"
            + (f"; subscription usage: {'; '.join(self.budget.usage.lines())}" if self.budget.usage else ""),
            f"- run budget: {elapsed:.1f}h elapsed, {left}. Size the short horizon to what fits; the charter has no clock.",
        ]
        for o in outs:
            lines.append(f"- #{o.iteration}: {o.verdict.verdict} ({o.verdict.reason[:100]}); changed={o.changed} recorded={o.recorded}")
        if self.no_change_streak:
            lines.append(f"- iterations in a row with no change: {self.no_change_streak}")
        lines.append(self.loops.describe())
        loop = self.loops.report()
        if loop is not None:
            # The planner is the role that can end a loop cheapest, because a
            # loop is usually the plan repeating itself, not the actor failing.
            lines.append("")
            lines.append("LOOP DETECTED — " + loop.describe())
            lines.append(
                "Do not restate the current bet. Pick a different charter criterion and bet on "
                "that. If every criterion looks blocked, say which and why, one line each."
            )
        return "\n".join(lines)

    def _maybe_plan(self, n: int, why: str) -> None:
        """One planner pass: a Bet record folded into the plan. Never raises."""
        if not self.plan_enabled:
            return
        try:
            prompt = self.memory.planner_prompt(self._plan_signals())
        except Exception as exc:
            self.recorder.log(f"planner_prompt raised {exc!r}")
            return
        if not prompt:
            return
        role = self.config.role("planner")
        harness = self.planner or self.harness
        before = self._bet_marker()
        self._status("plan", iteration=n)
        self.recorder.log(f"[{n}] plan: planner pass ({harness.name}, {why})")
        try:
            result = harness.run(
                prompt, cwd=self.repo, timeout=role.timeout_seconds, model=role.model,
                log_path=self.recorder.transcript_path(n, "planner"),
            )
        except Exception as exc:
            result = Result(exit_code=-1, error=f"harness raised {exc!r}")
        bet = None
        try:
            bet = self.memory.verify_bet(before)
        except Exception as exc:
            self.recorder.log(f"verify_bet raised {exc!r}")
        near = self.loops.observe_bet(bet)
        sha = self.git.commit(f"ouroboros #{n}: plan — {(bet or 'no bet')[:60]}", allow_empty=False)
        self.budget.add(cost=result.cost_usd, usage=result.usage, harness=result.extra.get("harness"))
        self.recorder.step(iteration=n, step="plan", why=why, bet=bet, exit=result.exit_code, timed_out=result.timed_out,
                           error=(result.error or None) and result.error[:200], sha=sha[:10], cost=result.cost_usd,
                           repeats=round(near, 2))
        self.since_plan = 0
        if not result.ok or not bet:
            self.recorder.log(f"[{n}] plan: no bet landed ({result.error or 'planner wrote nothing'})")

    def _bet_marker(self):
        if hasattr(self.memory, "bets_size"):
            return self.memory.bets_size()
        return self.memory.snapshot()

    def _call_actor(self, prompt: str, timeout: float, model: str | None, *, housekeeping: bool = False) -> Result:
        """Call the harness. Retry on retriable errors with backoff, forever. One retry on other errors."""
        role = self.config.role("actor")
        n = self.iteration
        transcript = "housekeeping" if housekeeping else "actor"
        resume = self.session_id if (role.resume_for > 1 and 0 < self.session_uses < role.resume_for) else None
        retriable_attempts = 0
        plain_attempts = 0
        while True:
            attempt = retriable_attempts + plain_attempts
            try:
                result = self.harness.run(
                    prompt, cwd=self.repo, timeout=timeout, resume=resume, model=model,
                    log_path=self.recorder.transcript_path(n, transcript, attempt),
                )
            except Exception as exc:
                result = Result(exit_code=-1, error=f"harness raised {exc!r}")
            self.budget.usage.record(result.usage)
            self.recorder.step(iteration=n, step="actor", attempt=attempt, exit=result.exit_code, timed_out=result.timed_out, error=(result.error or None) and result.error[:200], session=result.session_id, turns=result.turns,
                               housekeeping=True if housekeeping else None)
            if result.ok or result.timed_out:
                self.recorder.clear_needs_human()
                break
            kind = result.kind
            if kind == "auth":
                self.recorder.needs_human(f"Harness {self.harness.name} reports an auth failure:\n\n{result.error}\n\nThe loop keeps retrying every 10 minutes.")
                self._sleep(backoff_seconds(99), "auth failure")
                retriable_attempts += 1
                continue
            if kind == "limit":
                wait = result.reset_wait_seconds()
                if wait is not None:
                    self._sleep(min(wait + 60.0, MAX_RESET_WAIT), f"limit resets in {int(wait) // 60} min: {(result.error or result.text)[-80:]}")
                else:
                    self._sleep(backoff_seconds(retriable_attempts + 2), "usage limit, reset time unknown")
                retriable_attempts += 1
                continue
            if kind == "transient":
                self._sleep(backoff_seconds(retriable_attempts), "rate limit / transient")
                retriable_attempts += 1
                continue
            if plain_attempts < 1:
                plain_attempts += 1
                self._sleep(5, "retry after error")
                continue
            break  # give up this iteration; the error rides into the next prompt
        if result.session_id:
            if resume:
                self.session_uses += 1
            else:
                self.session_id, self.session_uses = result.session_id, 1
        return result

    def _diff(self, head_before: str, commit: str | None) -> str:
        try:
            return self.git.diff(head_before, commit or "HEAD")
        except Exception as exc:
            self.recorder.log(f"diff for the critic failed: {exc!r}")
            return ""

    def _memory_context(self) -> str:
        try:
            return self.memory.critic_context() or ""
        except Exception as exc:
            self.recorder.log(f"critic_context raised {exc!r}")
            return ""

    def _sleep(self, seconds: float, why: str) -> None:
        self.recorder.log(f"backoff {seconds:.0f}s ({why})")
        self._status("backoff", seconds=seconds, why=why)
        self.sleeper(seconds)

    def _status(self, state: str, **extra) -> None:
        fields = dict(
            run=self.config.run, state=state, iteration=self.iteration, branch=self.git.branch,
            harness=self.harness.name, memory=self.memory.name, cost_usd=round(self.budget.cost_usd, 4),
            elapsed_s=int(self.budget.elapsed), last_ok_tag=self.last_ok_tag,
        )
        if self.budget.usage:
            fields["usage"] = self.budget.usage.to_dict()
            fields["usage_lines"] = self.budget.usage.lines()
        # What the run has spent, in the unit each harness charges: windows for a
        # subscription, money only where money is really billed.
        fields["usage_compact"] = self.budget.usage.compact(self.budget.billed)
        if self.budget.money:
            fields["money"] = {h: round(c, 4) for h, c in self.budget.money.items()}
        board = getattr(self.harness, "board", None)
        if board is not None and board.snapshot():
            fields["limited"] = board.snapshot()
        fields.update(extra)
        self.recorder.status(**fields)
