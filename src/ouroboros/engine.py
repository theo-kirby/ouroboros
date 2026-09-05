"""The loop. One iteration = actor call -> verify record -> commit -> overseer -> budget."""

from __future__ import annotations

import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .budget import BudgetClock, backoff_seconds
import re

from .config import Config, parse_duration
from .gitguard import GitGuard
from .harness.base import Harness, Result
from .memory.base import MemoryAdapter
from .recorder import Recorder
from .roles.actor import build_actor_prompt
from .roles.overseer import Overseer, RulesOverseer, Signals, Verdict

Sleeper = Callable[[float], None]

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


@dataclass
class Engine:
    config: Config
    repo: Path
    harness: Harness
    memory: MemoryAdapter
    git: GitGuard
    recorder: Recorder
    budget: BudgetClock
    overseer: Overseer = field(default_factory=RulesOverseer)
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
    session_uses: int = 0
    outcomes: list[IterationOutcome] = field(default_factory=list)

    # ------------------------------------------------------------------
    def run(self) -> str:
        """Run until a stop condition. Returns the stop reason. Never raises for harness trouble."""
        self.recorder.log(f"run {self.config.run}: branch={self.git.branch} harness={self.harness.name} memory={self.memory.name} mode={self.config.mode}")
        self._status("starting")
        reason: str | None = None
        while reason is None:
            try:
                self.step()
            except KeyboardInterrupt:
                reason = "interrupted by user"
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
        prompt = build_actor_prompt(goal_text=self.goal_text, memory=self.memory, iteration=n, injected=self.injected)
        before = self.memory.snapshot()
        head_before = self.git.head()
        self._status("work", iteration=n)

        result = self._call_actor(prompt, role.timeout_seconds, role.model)

        recorded = self.memory.verify_recorded(before)
        changed = self.git.has_changes()
        summary = self.memory.last_summary() if recorded else (result.error or "no record")[:60]
        commit = self.git.commit(f"ouroboros #{n}: {summary}")
        self.recorder.step(iteration=n, step="commit", sha=commit[:10], changed=changed, recorded=recorded, cost=result.cost_usd)

        self.no_change_streak = 0 if changed else self.no_change_streak + 1
        if result.error and result.error == self.last_error:
            self.error_streak += 1
        elif result.error:
            self.error_streak = 1
        else:
            self.error_streak = 0
        self.last_error = result.error

        self._status("oversee", iteration=n)
        signals = Signals(
            result.text, changed, recorded, self.no_change_streak, self.error_streak, result.error,
            iteration=n, diff_stat=self.git.diff_stat(head_before, commit),
            history=self.recorder.read_jsonl(self.recorder.overseer)[-3:],
        )
        try:
            verdict = self.overseer.judge(signals)
        except Exception as exc:  # an overseer bug must not stop the loop
            self.recorder.log(f"overseer raised {exc!r}; using rules")
            verdict = RulesOverseer().judge(signals)
            verdict.reason = f"rules fallback (overseer raised): {verdict.reason}"
        overseer_cost = float(getattr(self.overseer, "last_cost", 0.0) or 0.0)
        self.budget.add(cost=overseer_cost)
        self.recorder.decision(iteration=n, verdict=verdict.verdict, reason=verdict.reason, reply=verdict.reply, overseer=verdict.source, cost=overseer_cost)
        self.recorder.step(iteration=n, step="oversee", verdict=verdict.verdict, source=verdict.source, reason=verdict.reason)

        idle = False
        if verdict.verdict == "done_accepted":
            policy = exhaustion_policy(self.goal_text)
            nudge = {"creative": CREATIVE_REPLY, "maintain": MAINTAIN_REPLY}.get(policy, REPORT_DONE_REPLY)
            verdict.reply = f"{nudge}\n\n{verdict.reply}".strip()
            idle = policy == "report_done"

        if verdict.verdict == "revert" and self.config.git.revert_on_reject and self.last_ok_tag:
            sha = self.git.revert_to(self.last_ok_tag, patch_out=self.recorder.run_dir / "reverted" / f"{n:04d}.patch")
            self.recorder.step(iteration=n, step="revert", to=self.last_ok_tag, sha=sha[:10])
            self.error_streak = 0
        elif verdict.verdict in ("continue", "answer", "done_rejected", "done_accepted") and self.config.git.tag_on_accept:
            tag = f"ouroboros/{self.config.run}/ok-{n:04d}"
            self.git.tag(tag)
            self.last_ok_tag = tag

        if result.error and verdict.verdict == "continue":
            note = f"The previous iteration ended with an error: {result.error[:500]}. Recover and continue."
            verdict.reply = f"{note}\n\n{verdict.reply}".strip()
        self.injected = verdict.reply or None

        self.budget.add(cost=result.cost_usd, iteration=True, done_accepted=(verdict.verdict == "done_accepted") if verdict.verdict.startswith("done") else None)
        self._status("idle", iteration=n, last_verdict=verdict.verdict)
        out = IterationOutcome(n, result, verdict, commit, changed, recorded)
        self.outcomes.append(out)
        if idle and self.budget.should_stop() is None:
            self._sleep(parse_duration(self.config.idle_interval) or 1800.0, "done accepted; report_done policy")
        return out

    # ------------------------------------------------------------------
    def _call_actor(self, prompt: str, timeout: float, model: str | None) -> Result:
        """Call the harness. Retry on retriable errors with backoff, forever. One retry on other errors."""
        role = self.config.role("actor")
        n = self.iteration
        resume = self.session_id if (role.resume_for > 1 and 0 < self.session_uses < role.resume_for) else None
        retriable_attempts = 0
        plain_attempts = 0
        while True:
            attempt = retriable_attempts + plain_attempts
            try:
                result = self.harness.run(
                    prompt, cwd=self.repo, timeout=timeout, resume=resume, model=model,
                    log_path=self.recorder.transcript_path(n, "actor", attempt),
                )
            except Exception as exc:
                result = Result(exit_code=-1, error=f"harness raised {exc!r}")
            self.recorder.step(iteration=n, step="actor", attempt=attempt, exit=result.exit_code, timed_out=result.timed_out, error=(result.error or None) and result.error[:200], session=result.session_id, turns=result.turns)
            if result.ok or result.timed_out:
                self.recorder.clear_needs_human()
                break
            if result.auth_failure:
                self.recorder.needs_human(f"Harness {self.harness.name} reports an auth failure:\n\n{result.error}\n\nThe loop keeps retrying every 10 minutes.")
                self._sleep(backoff_seconds(99), "auth failure")
                retriable_attempts += 1
                continue
            if result.retriable:
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

    def _sleep(self, seconds: float, why: str) -> None:
        self.recorder.log(f"backoff {seconds:.0f}s ({why})")
        self._status("backoff", seconds=seconds, why=why)
        self.sleeper(seconds)

    def _status(self, state: str, **extra) -> None:
        fields = dict(
            run=self.config.run, state=state, iteration=self.iteration, branch=self.git.branch,
            harness=self.harness.name, cost_usd=round(self.budget.cost_usd, 4), elapsed_s=int(self.budget.elapsed),
            last_ok_tag=self.last_ok_tag,
        )
        fields.update(extra)
        self.recorder.status(**fields)
