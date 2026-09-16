"""The reporter: triggers off the run directory, digests since the last one, never raises."""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from ouroboros.config import Config
from ouroboros.harness.base import Result
from ouroboros.notify import Console
from ouroboros.watch import (AgentReporter, DigestContext, Watcher, build_reporter_prompt, fmt_duration,
                             read_snapshot, stats_text)


class Clock:
    def __init__(self, t: float = 1_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def tick(self, s: float) -> None:
        self.t += s


class FakeRun:
    """Writes the files the loop writes, one call per thing that happens."""

    def __init__(self, repo: Path, clock: Clock, run: str = "r") -> None:
        self.repo = repo
        self.run = run
        self.dir = repo / ".ouroboros" / "runs" / run
        self.dir.mkdir(parents=True)
        self.clock = clock
        self.n = 0
        self.status(state="work", iteration=0)
        (self.dir / "pid").write_text(str(os.getpid()))   # alive: this process

    def status(self, **fields) -> None:
        st = {"ts": "t", "epoch": self.clock(), "run": self.run, "state": "work", "iteration": self.n,
              "branch": f"ouroboros/{self.run}", "harness": "claude", "memory": "handoff", "elapsed_s": 100,
              "usage_compact": "claude 7d 41% +6"}
        st.update(fields)
        (self.dir / "status.json").write_text(json.dumps(st))

    def _append(self, name: str, rec: dict) -> None:
        with (self.dir / name).open("a") as f:
            f.write(json.dumps({"ts": "t", **rec}) + "\n")

    def iterate(self, verdict: str = "continue", *, changed: bool = True, reason: str = "ok", did: str = "", doing: str = "",
                revert: bool = False, revert_error: str | None = None) -> int:
        self.n += 1
        self._append("iterations.jsonl", {"iteration": self.n, "step": "commit", "sha": "abc", "changed": changed, "recorded": changed})
        self._append("iterations.jsonl", {"iteration": self.n, "step": "critique", "verdict": verdict, "reason": reason})
        self._append("critic.jsonl", {"iteration": self.n, "verdict": verdict, "reason": reason, "reply": "", "did": did, "doing": doing})
        if revert:
            rec = {"iteration": self.n, "step": "revert", "to": "ok-1", "sha": "def"}
            if revert_error:
                rec["error"] = revert_error
            self._append("iterations.jsonl", rec)
        self.status(state="idle", last_verdict=verdict)
        return self.n

    def step(self, **rec) -> None:
        self._append("iterations.jsonl", {"iteration": self.n, **rec})

    def log(self, line: str) -> None:
        with (self.dir / "loop.log").open("a") as f:
            f.write(f"t {line}\n")

    def needs_human(self, text: str) -> None:
        (self.dir / "NEEDS_HUMAN.md").write_text(f"# Needs a human\n\nt\n\n{text}\n")

    def dead(self) -> None:
        (self.dir / "pid").write_text("999999")


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def fake(repo: Path, clock: Clock) -> FakeRun:
    return FakeRun(repo, clock)


def make_watcher(fake: FakeRun, clock: Clock, *, reporter=None, **report) -> tuple[Watcher, Console]:
    cfg = Config(run=fake.run)
    for k, v in report.items():
        setattr(cfg.report, k, v)
    out = Console(out=lambda *_: None)
    w = Watcher(fake.dir, config=cfg, notifier=out, repo=fake.repo, reporter=reporter, now=clock, log=lambda *_: None)
    return w, out


def kinds(events) -> list[str]:
    return [e.kind for e in events]


# ----------------------------------------------------------------------------- triggers
def test_first_pass_says_watching_and_skips_history(fake, clock):
    for _ in range(4):
        fake.iterate("stuck")            # history the reporter was not there for
    fake.log("harness claude is out of usage until 12:10: 'limit'")
    w, out = make_watcher(fake, clock)
    events = w.check()
    assert kinds(events) == ["started"]
    assert out.sent[0][0] == "r: watching from here"
    assert w.cursor["steps"] == 8 and w.cursor["decisions"] == 4 and w.cursor["log"] == 1
    assert (fake.dir / "reporter.json").exists()


def test_nothing_new_sends_nothing(fake, clock):
    w, out = make_watcher(fake, clock)
    w.check()
    assert w.check() == [] and len(out.sent) == 1


def test_no_status_yet_is_not_an_error(repo, clock):
    cfg = Config(run="never")
    w = Watcher(repo / ".ouroboros" / "runs" / "never", config=cfg, notifier=Console(out=lambda *_: None), repo=repo, now=clock)
    assert w.check() == [] and not w.done


def test_needs_human_is_high_priority_and_fires_once(fake, clock):
    w, out = make_watcher(fake, clock)
    w.check()
    fake.needs_human("Iteration 3 was rejected, but reverting failed.")
    events = w.check()
    assert kinds(events) == ["needs_human"]
    title, body, prio = out.sent[-1]
    assert prio == 1 and "reverting failed" in body and "# Needs" not in body
    assert w.check() == []
    fake.needs_human("something else")
    assert kinds(w.check()) == ["needs_human"]


def test_stuck_fires_at_three_in_a_row_then_resets(fake, clock):
    w, out = make_watcher(fake, clock)
    w.check()
    fake.iterate("stuck"); fake.iterate("stuck")
    assert w.check() == []
    fake.iterate("stuck", reason="nothing changed", doing="waiting on a clock")
    events = w.check()
    assert kinds(events) == ["stuck"] and "x3" in events[0].title and "waiting on a clock" in events[0].body
    fake.iterate("continue"); fake.iterate("stuck"); fake.iterate("stuck"); fake.iterate("stuck")
    assert kinds(w.check()) == ["stuck"]     # the streak started over after the continue
    for _ in range(6):
        fake.iterate("stuck")
    assert w.check() == []                   # 9 in a row: the next push is at 10
    fake.iterate("stuck")
    assert kinds(w.check()) == ["stuck"]


def test_loop_escalation_fires_once_per_step(fake, clock):
    w, out = make_watcher(fake, clock)
    w.check()
    fake.step(step="loop", signal="no_product", streak=9, escalation=1, acted=True, evidence=["x"])
    assert w.check() == []                   # step 1 only names it to the actor
    fake.step(step="loop", signal="no_product", streak=14, escalation=2, acted=True, evidence=["x"])
    fake.step(step="loop", signal="no_product", streak=15, escalation=2, acted=False, evidence=["x"])
    events = w.check()
    assert kinds(events) == ["looping"] and "re-plan" in events[0].body
    fake.step(step="loop", signal="no_product", streak=19, escalation=3, acted=True, evidence=["x"])
    events = w.check()
    assert kinds(events) == ["looping"] and "rotating" in events[0].body


def test_limit_lines_fire_once_per_harness_per_cooldown(fake, clock):
    w, out = make_watcher(fake, clock)
    w.check()
    fake.log("harness claude is out of usage until 12:10 CEST: 'You have hit your limit'")
    fake.log("harness: claude -> codex")
    events = w.check()
    assert kinds(events) == ["limited"] and events[0].title == "r: harness claude limited" and events[0].priority == 0
    fake.log("harness claude is out of usage until 12:10 CEST: 'again'")
    assert w.check() == []
    clock.tick(3 * 3600)
    fake.status()                                       # the loop is still writing
    fake.log("harness claude is out of usage until 15:10 CEST: 'again'")
    assert kinds(w.check()) == ["limited"]
    fake.log("[7] actor: every harness is blocked (claude, codex): limit")
    events = w.check()
    assert kinds(events) == ["limited"] and events[0].priority == 1 and "every harness" in events[0].title


def test_engine_error_has_a_cooldown(fake, clock):
    w, out = make_watcher(fake, clock)
    w.check()
    fake.step(step="engine_error", error="KeyError('x')")
    assert kinds(w.check()) == ["engine_error"]
    fake.step(step="engine_error", error="KeyError('x')")
    assert w.check() == []
    clock.tick(3601)
    fake.step(step="engine_error", error="KeyError('x')")
    assert kinds(w.check()) == ["engine_error"]


def test_dead_process_is_urgent_and_fires_once(fake, clock):
    w, out = make_watcher(fake, clock)
    w.check()
    fake.dead()
    events = w.check()
    assert kinds(events) == ["silent"] and events[0].priority == 1 and "gone" in events[0].title
    assert w.check() == []
    assert not w.done


def test_quiet_status_fires_once_per_stale_epoch(fake, clock):
    w, out = make_watcher(fake, clock, silent_after="1h")
    w.check()
    clock.tick(3600)
    events = w.check()
    assert kinds(events) == ["silent"] and events[0].priority == 0 and "quiet for 1h00m" in events[0].title
    clock.tick(600)
    assert w.check() == []
    fake.status(state="critique")     # the loop spoke again
    assert w.check() == []
    clock.tick(3600)
    assert kinds(w.check()) == ["silent"]


def test_stopped_sends_the_last_word_with_a_digest_and_finishes(fake, clock):
    w, out = make_watcher(fake, clock)
    w.check()
    fake.iterate(); fake.iterate("reject", revert=True)
    fake.status(state="stopped", stop_reason="wall clock: 8h elapsed")
    events = w.check()
    assert kinds(events) == ["reject", "stopped"]
    assert [t for t, _, _ in out.sent] == ["r: watching", "r: stopped — wall clock: 8h elapsed"]   # reject is off by default
    title, body, prio = out.sent[-1]
    assert title == "r: stopped — wall clock: 8h elapsed"
    assert "2 iterations, 2 changed, 2 recorded, 1 reverted" in body and "reject 1" in body
    assert w.done
    assert w.check() == [] and w.done


def test_a_stale_killed_status_is_not_announced_as_news(fake, clock):
    """`run` starts the reporter and the loop together; the loop has not written yet.

    The directory still holds the last run's ending. Announcing it and exiting is
    the one failure that makes the reporter useless exactly when it is started.
    """
    fake.status(state="killed", signal=15)
    fake.dead()
    w, out = make_watcher(fake, clock)
    assert w.check() == [] and out.sent == [] and not w.done
    clock.tick(60)
    assert w.check() == [] and not w.done
    fake.status(state="work", iteration=1)        # the loop is up
    (fake.dir / "pid").write_text(str(os.getpid()))
    events = w.check()
    assert kinds(events) == ["started"] and not w.done
    fake.status(state="stopped", stop_reason="wall clock")
    assert kinds(w.check()) == ["stopped"] and w.done


def test_a_run_that_really_is_over_is_given_up_on_quietly(fake, clock):
    fake.status(state="killed", signal=15)
    fake.dead()
    logged = []
    w, out = make_watcher(fake, clock)
    w.log = logged.append
    assert w.check() == []
    clock.tick(181)
    assert w.check() == [] and w.done and out.sent == []
    assert "already over" in logged[-1] and "--once" in logged[-1]


def test_once_reports_on_a_finished_run(fake, clock):
    rep = ScriptedReporter(["the post-mortem"])
    fake.iterate()
    fake.status(state="stopped", stop_reason="max_iterations 1 reached")
    fake.dead()
    w, out = make_watcher(fake, clock, reporter=rep)
    assert w.once() == "the post-mortem"
    assert "stop reason: max_iterations 1 reached" in rep.contexts[0].stats
    assert json.loads((fake.dir / "reporter.json").read_text())["terminal_sent"] is True


def test_killed_reads_the_signal(fake, clock):
    w, out = make_watcher(fake, clock)
    w.check()
    fake.status(state="killed", signal=15)
    events = w.check()
    assert kinds(events) == ["stopped"] and "signal 15" in events[0].title


def test_reject_alerts_only_when_asked_for(fake, clock):
    w, out = make_watcher(fake, clock, on=["reject"])
    w.check()
    fake.iterate("reject", reason="no test", revert=True)
    events = w.check()
    assert kinds(events) == ["reject"] and events[0].priority == -1 and "no test" in events[0].body
    fake.iterate("reject", reason="no test", revert=True, revert_error="tree still differs")
    events = w.check()
    assert events[0].priority == 1 and "revert FAILED" in events[0].body


def test_triggers_not_in_on_are_detected_but_not_sent(fake, clock):
    w, out = make_watcher(fake, clock, on=["stopped"])
    w.check()
    fake.dead()
    events = w.check()
    assert kinds(events) == ["silent"] and len(out.sent) == 1


def test_done_accepted_carries_a_digest(fake, clock):
    w, out = make_watcher(fake, clock)
    w.check()
    fake.iterate("done_accepted", reason="all criteria hold")
    events = w.check()
    assert kinds(events) == ["done_accepted"] and events[0].digest
    assert "all criteria hold" in out.sent[-1][1] and "since last report" in out.sent[-1][1]


# ----------------------------------------------------------------------------- the interval digest
def test_interval_digest_uses_the_numbers_when_no_model(fake, clock):
    w, out = make_watcher(fake, clock, every="4h")
    w.check()
    fake.iterate(did="added the horn", doing="measuring the boss")
    clock.tick(3 * 3600)
    fake.status()
    assert w.check() == []
    clock.tick(3600)
    fake.status()
    events = w.check()
    assert kinds(events) == ["interval"] and events[0].priority == -1
    title, body, _ = out.sent[-1]
    assert title == "r #1: 4h report"
    assert "no model answered" in body and "last: added the horn" in body and "now: measuring the boss" in body
    assert "usage: claude 7d 41% +6" in body
    reports = list((fake.dir / "reports").glob("*-interval.md"))
    assert len(reports) == 1 and "added the horn" in reports[0].read_text()
    # the next one starts where this one stopped
    assert w.cursor["digest_decisions"] == 1
    clock.tick(4 * 3600)
    fake.status()
    assert kinds(w.check()) == ["interval"]
    assert "since last report: 0 iterations" in out.sent[-1][1]


def test_interval_off_when_every_is_null(fake, clock):
    w, out = make_watcher(fake, clock, every=None)
    w.check()
    clock.tick(48 * 3600)
    fake.status()
    assert w.check() == []


class ScriptedReporter:
    def __init__(self, texts: list[str | None]) -> None:
        self.texts, self.contexts = list(texts), []

    def write(self, ctx: DigestContext) -> str | None:
        self.contexts.append(ctx)
        return self.texts.pop(0) if self.texts else None


def test_model_digest_gets_only_what_is_new_and_falls_back(fake, clock):
    rep = ScriptedReporter(["STATUS moving\nDONE the horn", None])
    w, out = make_watcher(fake, clock, every="1h", reporter=rep)
    w.check()
    fake.iterate(reason="fine", did="old unit")
    clock.tick(3600); fake.status()
    w.check()
    assert out.sent[-1][1] == "STATUS moving\nDONE the horn"
    ctx = rep.contexts[0]
    assert "#1 continue: fine (did: old unit)" in ctx.verdicts and ctx.trigger == "interval"
    assert "keep going" in ctx.goal          # the charter travels with the prompt
    fake.iterate(reason="newer", did="new unit")
    clock.tick(3600); fake.status()
    w.check()
    ctx = rep.contexts[1]
    assert "new unit" in ctx.verdicts and "old unit" not in ctx.verdicts
    assert "no model answered" in out.sent[-1][1]        # the second call returned nothing


def test_digest_is_clipped_to_max_chars(fake, clock):
    rep = ScriptedReporter(["x" * 5000])
    w, out = make_watcher(fake, clock, every="1h", max_chars=300, reporter=rep)
    w.check()
    clock.tick(3600); fake.status()
    w.check()
    assert len(out.sent[-1][1]) == 300 and out.sent[-1][1].endswith("…")
    full = next((fake.dir / "reports").glob("*.md")).read_text()
    assert "x" * 5000 in full                             # the whole thing stays on disk


def test_a_reporter_that_raises_still_sends_the_numbers(fake, clock):
    class Boom:
        def write(self, ctx):
            raise RuntimeError("driver exploded")

    w, out = make_watcher(fake, clock, every="1h", reporter=Boom())
    w.check()
    clock.tick(3600); fake.status()
    assert kinds(w.check()) == ["interval"] and "no model answered" in out.sent[-1][1]


def test_commits_since_the_last_digest_come_from_git(fake, clock, repo):
    rep = ScriptedReporter(["one", "two"])
    w, out = make_watcher(fake, clock, every="1h", reporter=rep)
    subprocess.run(["git", "checkout", "-q", "-b", "ouroboros/r"], cwd=repo, check=True)
    w.check()
    (repo / "a.txt").write_text("a")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "ouroboros #1: added a"], cwd=repo, check=True)
    clock.tick(3600); fake.status()
    w.check()
    assert "added a" in rep.contexts[0].commits
    (repo / "b.txt").write_text("b")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "ouroboros #2: added b"], cwd=repo, check=True)
    clock.tick(3600); fake.status()
    w.check()
    assert "added b" in rep.contexts[1].commits and "added a" not in rep.contexts[1].commits


# ----------------------------------------------------------------------------- once, run, robustness
def test_once_on_a_fresh_reporter_covers_the_whole_run(fake, clock):
    """Someone asking by hand wants the run, not the nothing since they typed it."""
    rep = ScriptedReporter(["the read", "the second read"])
    w, out = make_watcher(fake, clock, reporter=rep)
    fake.iterate(did="the first unit")
    fake.iterate(did="the second unit")
    assert w.once() == "the read"
    assert out.sent[-1][0] == "r #2: report" and rep.contexts[0].trigger == "manual"
    assert "the first unit" in rep.contexts[0].verdicts and "the second unit" in rep.contexts[0].verdicts
    assert json.loads((fake.dir / "reporter.json").read_text())["digest_decisions"] == 2
    fake.iterate(did="the third unit")
    again, _ = make_watcher(fake, clock, reporter=rep)      # a second `--once` is a delta
    assert again.once() == "the second read"
    assert "the third unit" in rep.contexts[1].verdicts and "the first unit" not in rep.contexts[1].verdicts


def test_watching_primes_from_now_not_from_history(fake, clock):
    rep = ScriptedReporter(["the read"])
    fake.iterate(did="before the reporter existed")
    w, out = make_watcher(fake, clock, every="1h", reporter=rep)
    w.check()
    fake.iterate(did="after it started")
    clock.tick(3600); fake.status()
    w.check()
    assert "after it started" in rep.contexts[0].verdicts
    assert "before the reporter existed" not in rep.contexts[0].verdicts


def test_once_on_a_run_that_never_started(repo, clock):
    w = Watcher(repo / ".ouroboros" / "runs" / "nope", config=Config(run="nope"), notifier=Console(out=lambda *_: None),
                repo=repo, now=clock)
    assert w.once() is None


def test_run_polls_until_the_run_ends(fake, clock):
    w, out = make_watcher(fake, clock, poll="30s")
    w.check()
    fake.iterate()
    ticks = []

    def sleeper(s):
        ticks.append(s)
        clock.tick(s)
        if len(ticks) == 35:
            fake.status(state="stopped", stop_reason="max_iterations 1 reached")

    w.run(sleeper=sleeper)
    assert w.done and out.sent[-1][0].startswith("r: stopped")
    assert 30 <= len(ticks) <= 70


def test_stop_request_sends_the_numbers_not_a_model_call(fake, clock):
    class Slow:
        def write(self, ctx):
            raise AssertionError("no model call on the way out")

    w, out = make_watcher(fake, clock, reporter=Slow())
    w.check()

    def sleeper(s):
        clock.tick(s)
        fake.status(state="killed", signal=15)
        w.stop_requested = True

    w.run(sleeper=sleeper)
    assert out.sent[-1][0].startswith("r: killed") and "since last report" in out.sent[-1][1]


def test_half_written_jsonl_line_is_skipped(fake, clock):
    w, out = make_watcher(fake, clock)
    w.check()
    with (fake.dir / "iterations.jsonl").open("a") as f:
        f.write('{"iteration": 9, "step": "eng')
    assert w.check() == []
    with (fake.dir / "iterations.jsonl").open("a") as f:
        f.write('ine_error", "error": "late"}\n')
    assert kinds(w.check()) == ["engine_error"]


def test_a_notifier_that_fails_does_not_stop_the_watcher(fake, clock):
    class Down:
        name = "down"

        def send(self, *a, **k):
            raise ConnectionError("no network")

    logged = []
    cfg = Config(run="r")
    w = Watcher(fake.dir, config=cfg, notifier=Down(), repo=fake.repo, now=clock, log=logged.append)
    assert kinds(w.check()) == ["started"]         # detected, and the failure did not stop the pass
    assert "raised" in logged[-1]
    fake.dead()
    assert kinds(w.check()) == ["silent"]
    assert json.loads((fake.dir / "reporter.json").read_text())["dead_sent"] is True   # its place was still saved


def test_restart_does_not_replay(fake, clock):
    w, out = make_watcher(fake, clock)
    w.check()
    fake.dead()
    w.check()
    again, out2 = make_watcher(fake, clock)   # a new process, same reporter.json
    assert again.check() == [] and out2.sent == []


def test_a_continued_run_reports_again_after_its_stop(fake, clock):
    w, out = make_watcher(fake, clock)
    w.check()
    fake.status(state="stopped", stop_reason="done")
    w.check()
    assert w.done
    fake.status(state="work")       # `ouroboros run` with the same name continues it
    again, out2 = make_watcher(fake, clock)
    assert again.check() == [] and not again.done
    fake.status(state="stopped", stop_reason="done again")
    assert kinds(again.check()) == ["stopped"]


# ----------------------------------------------------------------------------- pieces
def test_stats_text_reads_the_status(fake, clock):
    fake.iterate("answer", reason="decided A", did="did A", doing="doing B")
    fake.status(state="backoff", why="2 stuck", seconds=120, limited={"codex": "23m"}, money={"pi": 1.5}, usage_compact="")
    snap = read_snapshot(fake.dir, now=clock())
    text = stats_text(snap, since_steps=0, since_decisions=0, run="r")
    assert "iteration 1 · state backoff" in text and "process alive" in text
    assert "limited: codex for 23m" in text and "billed: pi $1.50" in text
    assert "last: did A" in text and "now: doing B" in text and "last verdict: answer — decided A" in text
    assert "backing off 2m: 2 stuck" in text


def test_prompt_fills_every_placeholder(tmp_path):
    ctx = DigestContext(trigger="interval", stats="s", verdicts="", commits="", log="", needs_human="",
                        goal="", plan="", run="r", run_dir=tmp_path, max_chars=900)
    text = build_reporter_prompt(ctx)
    assert "{" not in text.replace("{stats}", "") or "{" not in text
    assert "900 characters" in text and "run `r`" in text and "(none since the last report)" in text


def test_agent_reporter_walks_the_chain_read_only(tmp_path):
    calls = []

    class H:
        def __init__(self, name, result):
            self.name, self.result = name, result

        def run(self, prompt, **kw):
            calls.append((self.name, kw))
            return self.result

    limited = H("claude", Result(text="", exit_code=1, error="usage limit reached"))
    fine = H("codex", Result(text="  the read  "))
    rep = AgentReporter([("claude", None), ("codex", "gpt")], cwd=tmp_path, timeout=5, harnesses={"claude": limited, "codex": fine},
                        transcript_dir=tmp_path / "reports")
    ctx = DigestContext(trigger="t", stats="", verdicts="", commits="", log="", needs_human="", goal="", plan="",
                        run="r", run_dir=tmp_path, max_chars=100)
    assert rep.write(ctx) == "the read"
    assert [c[0] for c in calls] == ["claude", "codex"]
    assert all(c[1]["tools"] == "readonly" for c in calls) and calls[1][1]["model"] == "gpt"
    assert calls[1][1]["log_path"].parent == tmp_path / "reports"


def test_agent_reporter_returns_none_when_no_one_answers(tmp_path):
    class Raises:
        name = "x"

        def run(self, prompt, **kw):
            raise RuntimeError("no")

    rep = AgentReporter([("x", None), ("nope", None)], cwd=tmp_path, timeout=5, harnesses={"x": Raises()})
    ctx = DigestContext(trigger="t", stats="", verdicts="", commits="", log="", needs_human="", goal="", plan="",
                        run="r", run_dir=tmp_path, max_chars=100)
    assert rep.write(ctx) is None


@pytest.mark.parametrize("s,out", [(0, "0m"), (59, "0m"), (60, "1m"), (3600, "1h00m"), (7380, "2h03m")])
def test_fmt_duration(s, out):
    assert fmt_duration(s) == out
