"""The worker-writer loop (DESIGN.md section 24). One iteration = worker -> commit -> writer -> commit.

Two roles, both fresh every call. The worker gets the writer's next step and the
memory's brief (as `handoff` says) under a footer the engine appends, does one unit,
commits, and reports in JSON. The writer reviews that turn against the charter,
records and folds it (it is the only memory writer), and returns the next prompt and
a done flag in strict JSON. The writer also goes first: turn 0 seeds the memory from
the charter and writes prompt 1. Nothing is reverted: a bad turn is recorded as
failed and the next prompt fixes it forward.

Everything else is the standard engine's: stop rules, the no-early-exit rule, the
loop detector, limits, backoff, refresh, budget accounting, the view and the final
report pass (run by the writer). Every path here survives an exception.
"""

from __future__ import annotations

from dataclasses import dataclass

from .budget import backoff_seconds
from .config import parse_duration
from .engine import (CREATIVE_REPLY, MAINTAIN_REPLY, NO_EARLY_EXIT_REPLY, REPORT_DONE_REPLY, Engine,
                     IterationOutcome, _harness_of, exhaustion_policy, role_env)
from .harness.base import Result
from .roles.critic import RulesCritic, Signals, Verdict, _one_line
from .roles.worker import (agents_path, build_worker_prompt, claimed_ids, parse_report, report_for_writer,
                           report_summary)
from .roles.writer import Writer, WriterReply, build_writer_prompt, parse_reply, touched_claims

NEXT_PROMPT = "next-prompt.md"   # the writer's last prompt, in the run directory, so a restart picks it up
HISTORY = 5                       # past decisions the writer sees; the memory holds the rest


@dataclass
class WorkerWriterEngine(Engine):
    writer: Writer | None = None
    next_prompt: str | None = None   # the writer's prompt for the next worker; None: the worker picks from the brief
    writer_failures: int = 0         # writer calls in a row that gave no reply

    # -- hooks into the standard engine ------------------------------------
    def _role_pools(self) -> list:
        h = getattr(self.writer, "harness", None)
        return [h] if h is not None else []

    def _final_role(self):
        return self.writer, "writer"

    def _status(self, state: str, **extra) -> None:
        super()._status(state, loop="worker-writer", handoff=self.config.handoff, **extra)

    def _before_loop(self) -> None:
        """Turn 0 on a fresh run; on a continued run, the prompt the last writer left."""
        saved = self.recorder.run_dir / NEXT_PROMPT
        if saved.exists():
            self.next_prompt = saved.read_text(encoding="utf-8").strip() or None
            self.recorder.log("continuing with the writer's saved prompt")
            return
        if self.iteration > 0:
            self.recorder.log("no saved prompt from the writer: the next worker picks from the brief")
            return
        self._turn_zero()

    def _reload_goal(self) -> None:
        """The standard reload; the worker reads the charter by path, so the notice points there."""
        super()._reload_goal()
        if self.injected and "Follow the current charter above." in self.injected:
            self.injected = self.injected.replace(
                "Follow the current charter above.",
                f"Follow the current charter, `{self._charter_path()}`; the writer has the new version too.")

    # -- small helpers -------------------------------------------------------
    def _charter_path(self) -> str:
        return self._rel(self.goal_path) if self.goal_path is not None else self.config.goal

    def _where(self) -> str:
        return getattr(getattr(self.memory, "store", None), "where", self.memory.name)

    def _save_prompt(self) -> None:
        try:
            (self.recorder.run_dir / NEXT_PROMPT).write_text(self.next_prompt or "", encoding="utf-8")
        except OSError as exc:
            self.recorder.log(f"could not save the writer's prompt: {exc!r}")

    def _graph(self):
        try:
            return self.memory.graph() if hasattr(self.memory, "graph") else None
        except Exception as exc:
            self.recorder.log(f"memory unreadable: {exc!r}")
            return None

    def _memory_before(self, n: int):
        """(records before, folded count before, the fold procedure) for the writer's call."""
        try:
            return self.memory.snapshot(), self._folded_count(), self.memory.fold_prompt()
        except Exception as exc:
            self.recorder.log(f"[{n}] memory unreadable before the writer: {exc!r}")
            return set(), 0, ""

    def _call_writer(self, prompt: str, n: int) -> tuple[Result | None, WriterReply | None]:
        """One writer call. No retry: it may already have written the memory. Never raises."""
        if self.writer is None:
            return None, None
        try:
            self._refresh_limits("writer")
            with role_env("writer"), self._heartbeat():
                res = self.writer.run(prompt, n)
        except Exception as exc:
            res = Result(exit_code=-1, error=f"writer raised {exc!r}")
        self.budget.add(cost=res.cost_usd, usage=res.usage, harness=res.extra.get("harness"))
        self.budget.count(res, "writer", _harness_of(self.writer))
        reply = parse_reply(res.text) if (res.text or "").strip() else None
        if reply is None:
            self.recorder.log(f"[{n}] the writer gave no reply: {(res.error or res.text or 'no output')[:200]!r}")
        return res, reply

    def _commit_memory(self, message: str) -> None:
        try:
            self.git.commit(message, allow_empty=False)
        except Exception as exc:
            self.recorder.log(f"committing the writer's memory failed: {exc!r}")

    def _check(self, n: int) -> None:
        try:
            self.check_problem = self.memory.check_report()
        except Exception as exc:
            self.recorder.log(f"memory check raised {exc!r}")
            self.check_problem = None
        if self.check_problem:
            self.recorder.step(iteration=n, step="check", problem=self.check_problem[:300])

    def _folded(self, due_before: int) -> bool:
        try:
            if self._folded_count() > due_before:
                self.memory.mark_reconciled()
                return True
        except Exception as exc:
            self.recorder.log(f"fold bookkeeping raised {exc!r}")
        return False

    # -- turn 0 ----------------------------------------------------------------
    def _turn_zero(self) -> None:
        """The writer seeds the memory from the charter and writes prompt 1. Survives anything."""
        self._status("write", iteration=0)
        _, folded_before, fold = self._memory_before(0)
        try:
            orient = self.memory.store.orient() if hasattr(self.memory, "store") else ""
        except Exception as exc:
            orient = f"(`ouroboros mem orient` failed: {exc!r})"
        prompt = build_writer_prompt(iteration=0, zero=True, handoff=self.config.handoff, where=self._where(),
                                     goal_text=self.goal_text, charter_path=self._charter_path(), fold=fold,
                                     orient=orient)
        res, reply = self._call_writer(prompt, 0)
        folded = self._folded(folded_before)
        self._commit_memory("ouroboros #0: seed the memory from the charter")
        self._check(0)
        self.next_prompt = (reply.prompt or None) if reply else None
        self._save_prompt()
        self.recorder.step(iteration=0, step="writer", turn=reply.turn if reply else None,
                           exit=res.exit_code if res else None, error=((res.error or None) and res.error[:200]) if res else None,
                           folded=folded or None, prompt=bool(self.next_prompt), cost=res.cost_usd if res else 0.0)
        if reply is None:
            self.writer_failures += 1

    # -- one iteration ---------------------------------------------------------
    def step(self) -> IterationOutcome:
        self._consume_refresh()
        self._reload_goal()
        self.iteration += 1
        n = self.iteration
        role = self.config.role("worker")
        handoff = self.config.handoff
        asked = self.next_prompt if handoff != "graph" else None
        try:
            brief = self.memory.brief(self._charter_path())
        except Exception as exc:   # a memory bug costs the brief, not the turn
            self.recorder.log(f"[{n}] brief failed: {exc!r}")
            brief = f"# Brief\n\n(the memory could not be read: {exc!r}; try `ouroboros mem orient`)"
        prompt = build_worker_prompt(iteration=n, handoff=handoff, prompt=asked, brief=brief,
                                     notes=self.injected or "", charter_path=self._charter_path(),
                                     agents=agents_path(self.repo))
        head_before = self.git.head()
        self._status("work", iteration=n)
        self._refresh_limits("worker")
        result = self._call_actor(prompt, role.timeout_seconds, role.model, name="worker")

        worked = result.ok or result.timed_out
        changed = self.git.has_changes() or self.git.head() != head_before   # the worker commits on its own
        summary = report_summary(result.text) if worked else ""
        commit = self.git.commit(f"ouroboros #{n}: {summary or (result.error or 'no report')[:60]}", allow_empty=False)
        self.recorder.step(iteration=n, step="commit", role="worker", sha=commit[:10], changed=changed,
                           cost=result.cost_usd)

        self.no_change_streak = 0 if changed else self.no_change_streak + 1
        if result.error and result.error == self.last_error:
            self.error_streak += 1
        elif result.error:
            self.error_streak = 1
        else:
            self.error_streak = 0
        self.last_error = result.error

        files = self.git.changed_files(head_before, commit or "HEAD") if changed else []
        try:
            frontier = self.memory.frontier_fingerprint()
        except Exception as exc:
            self.recorder.log(f"frontier_fingerprint raised {exc!r}")
            frontier = None
        self.loops.observe_iteration(files=files, frontier=frontier, subject=summary)
        loop = self.loops.report()

        # -- the writer: review, record and fold, the next prompt ----------------
        self._status("write", iteration=n)
        diff_stat = self.git.diff_stat(head_before, commit)
        report = parse_report(result.text)
        signals = Signals(
            result.text, changed, self.no_change_streak, self.error_streak, result.error,
            iteration=n, diff_stat=diff_stat, diff=self._diff(head_before, commit) if changed else "",
            history=self.recorder.read_decisions()[-HISTORY:], loop=self.loops.describe(),
            loop_fired=loop.describe() if loop else "", check=self.check_problem or "",
            report_missing=report is None, who="worker",
        )
        before, folded_before, fold = self._memory_before(n)
        graph = self._graph()
        try:
            frontier_text = self.memory.frontier_text(graph) if graph is not None else ""
        except Exception as exc:
            self.recorder.log(f"[{n}] frontier unreadable: {exc!r}")
            frontier_text = ""
        wprompt = build_writer_prompt(
            iteration=n, handoff=handoff, where=self._where(), goal_text=self.goal_text,
            charter_path=self._charter_path(), fold=fold, fix_first=self.check_problem or "",
            signals=signals.describe(), previous_prompt=asked or "", report=report_for_writer(result.text),
            commit=(commit or "")[:10] if changed else "", diff_stat=diff_stat, diff=signals.diff,
            touched=touched_claims(graph, claimed_ids(result.text), result.text), frontier=frontier_text,
            history="\n".join(f"- #{h.get('iteration')}: {h.get('verdict')} — {_one_line(h.get('reason'), 200)}; "
                              f"next: {_one_line(h.get('doing'), 160)}" for h in signals.history),
        )
        res, reply = self._call_writer(wprompt, n)
        if reply is not None:
            verdict = Verdict(reply.verdict, "", reply.reason, source=f"writer:{_harness_of(self.writer) or 'agent'}",
                              did=reply.did, doing=reply.doing)
            next_prompt = reply.prompt or None
            self.writer_failures = 0
        else:
            # No reply: the rules judge the turn, and the next worker picks from the brief.
            verdict = RulesCritic().judge(signals)
            verdict.reason = f"rules fallback (the writer gave no reply): {verdict.reason}"
            next_prompt = None
            self.writer_failures += 1

        if loop is not None:
            self._escalate(n, loop, verdict)
        idle = False
        if verdict.verdict == "done_accepted":
            self.done_accepted_seen = True
            policy = exhaustion_policy(self.goal_text)
            nudge = {"creative": CREATIVE_REPLY, "maintain": MAINTAIN_REPLY}.get(policy, REPORT_DONE_REPLY)
            verdict.reply = f"{nudge}\n\n{verdict.reply}".strip()
            idle = policy == "report_done"
        if result.error and verdict.verdict == "continue":
            note = f"The previous turn ended with an error: {result.error[:500]}. Recover and continue."
            verdict.reply = f"{note}\n\n{verdict.reply}".strip()
        if verdict.verdict == "stuck" and not self.done_accepted_seen and self.stuck_iterations >= 1:
            verdict.reply = f"{NO_EARLY_EXIT_REPLY.format(n=self.stuck_iterations + 1)}\n\n{verdict.reply}".strip()
        self.injected = verdict.reply or None          # the loop's own words, above the writer's prompt
        self.next_prompt = next_prompt
        self._save_prompt()

        # -- the books: one record per iteration, the fold, the check, the commit --
        recorded = False
        verdict_text = f"{verdict.verdict} — {verdict.reason}"
        try:
            recorded = self.memory.verify_recorded(before)
            if not recorded:
                slug = self.memory.fallback_record(iteration=n, report=result.text, verdict=f"Writer: {verdict_text}")
                recorded = slug is not None
                self.recorder.log(f"[{n}] the writer recorded nothing; the loop wrote {slug or 'no'} record")
        except Exception as exc:
            self.recorder.log(f"[{n}] writer bookkeeping raised {exc!r}")
        folded = self._folded(folded_before)
        # `prompt` (not the critic's `reply`) is the writer's next step; `notes` is what the loop adds above it.
        self.recorder.decision(iteration=n, verdict=verdict.verdict, reason=verdict.reason, did=verdict.did,
                               doing=verdict.doing, source=verdict.source, prompt=next_prompt or "",
                               notes=verdict.reply or None)
        self.recorder.step(iteration=n, step="writer", verdict=verdict.verdict, source=verdict.source,
                           turn=reply.turn if reply else None, done=reply.done if reply else None,
                           reason=verdict.reason[:300], recorded=recorded, fold_due=True, folded=folded or None,
                           prompt=bool(next_prompt), exit=res.exit_code if res else None,
                           error=((res.error or None) and res.error[:200]) if res else None,
                           cost=res.cost_usd if res else 0.0)
        self._commit_memory(f"ouroboros #{n}: record — {self._last_summary()}")
        self._check(n)
        if verdict.verdict in ("continue", "answer", "done_rejected", "done_accepted") and self.config.git.tag_on_accept:
            tag = f"ouroboros/{self.config.run}/ok-{n:04d}"
            try:
                self.git.tag(tag)
                self.last_ok_tag = tag
            except Exception as exc:
                self.recorder.log(f"[{n}] tag failed: {exc!r}")
        if worked and changed:
            self.memory.mark_iteration()

        self.budget.add(cost=result.cost_usd, iteration=True, harness=result.extra.get("harness"),
                        done_accepted=(verdict.verdict == "done_accepted") if verdict.verdict.startswith("done") else None,
                        stuck=(verdict.verdict == "stuck"))
        self._status("idle", iteration=n, last_verdict=verdict.verdict)
        out = IterationOutcome(n, result, verdict, commit, changed, recorded)
        self.outcomes.append(out)
        if idle and self.budget.should_stop() is None:
            self._sleep(parse_duration(self.config.idle_interval) or 1800.0, "done accepted; report_done policy")
        self.stuck_iterations = self.stuck_iterations + 1 if verdict.verdict == "stuck" else 0
        if self.stuck_iterations and self.budget.should_stop() is None:
            self._sleep(backoff_seconds(self.stuck_iterations - 1), f"{self.stuck_iterations} stuck verdict(s) in a row")
        self.failed_iterations = 0 if result.ok or result.timed_out else self.failed_iterations + 1
        if self.failed_iterations and self.budget.should_stop() is None:
            self._sleep(backoff_seconds(self.failed_iterations - 1), f"{self.failed_iterations} failed iteration(s) in a row")
        elif self.writer_failures and self.budget.should_stop() is None:
            # A writer that keeps failing (a limit on every harness of its chain, a broken
            # driver) slows the loop the same way a failing worker does; the work goes on.
            self._sleep(backoff_seconds(self.writer_failures - 1), f"{self.writer_failures} writer call(s) in a row gave no reply")
        return out

    def _last_summary(self) -> str:
        try:
            return self.memory.last_summary()
        except Exception:
            return "no record"
