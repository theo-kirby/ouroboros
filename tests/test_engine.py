from pathlib import Path

import pytest

from ouroboros.budget import BudgetClock
from ouroboros.config import Config, StopConfig
from ouroboros.engine import Engine
from ouroboros.gitguard import GitGuard
from ouroboros.memory.handoff import HandoffMemory
from ouroboros.recorder import Recorder
from ouroboros.harness.base import Result

from conftest import git
from fake_harness import (FakeHarness, asks_question, claims_done, crashes, no_record, nothing,
                          raises, rate_limited, records_only, times_out, works)


def make_engine(repo: Path, harness: FakeHarness, *, max_iterations=5, resume_for=1, sleeps=None,
                overseer=None, goal_text=None, stop=None, planner=None):
    cfg = Config(run="t", stop=stop or StopConfig(max_iterations=max_iterations))
    cfg.roles["actor"].resume_for = resume_for
    git_ = GitGuard(repo, cfg.branch)
    git_.start()
    mem = HandoffMemory(repo / ".ouroboros")
    rec = Recorder(repo / ".ouroboros" / "runs" / "t", echo=lambda *_: None)
    sleeps = sleeps if sleeps is not None else []

    def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) > 50:  # a real run would keep going; a test must fail loudly
            raise KeyboardInterrupt("too many backoffs in a test")

    kw = {"overseer": overseer} if overseer is not None else {}
    # a planner of its own, so actor scripts are not consumed by planner passes
    kw["planner"] = planner if planner is not None else FakeHarness([], default=nothing())
    eng = Engine(config=cfg, repo=repo, harness=harness, memory=mem, git=git_, recorder=rec,
                 budget=BudgetClock(cfg.stop), sleeper=fake_sleep,
                 goal_text=goal_text if goal_text is not None else (repo / ".ouroboros" / "goal.md").read_text(), **kw)
    return eng


def test_happy_path_commits_and_tags(repo):
    h = FakeHarness([works("a"), works("b")])
    eng = make_engine(repo, h, max_iterations=2)
    reason = eng.run()
    assert "max_iterations" in reason
    assert eng.iteration == 2
    log = git(repo, "log", "--oneline")
    assert "ouroboros #1: a" in log and "ouroboros #2: b" in log
    assert git(repo, "tag") .splitlines() == ["ouroboros/t/ok-0001", "ouroboros/t/ok-0002"]
    assert git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "ouroboros/t"
    assert git(repo, "status", "--porcelain") == ""
    assert eng.budget.cost_usd == pytest.approx(1.0)


def test_question_never_blocks_and_is_answered(repo):
    h = FakeHarness([asks_question(), works()])
    eng = make_engine(repo, h, max_iterations=2)
    eng.run()
    assert eng.outcomes[0].verdict.verdict == "answer"
    assert "Decide for yourself" in h.prompts[1]
    assert "(none)" in h.prompts[0]


def test_done_claim_is_rejected(repo):
    h = FakeHarness([claims_done(), works()])
    eng = make_engine(repo, h, max_iterations=2)
    eng.run()
    assert eng.outcomes[0].verdict.verdict == "done_rejected"
    assert "Do not stop" in h.prompts[1]
    assert eng.budget.done_streak == 0


def test_crash_retries_once_then_continues(repo):
    h = FakeHarness([crashes("boom"), crashes("boom"), works()])
    sleeps = []
    eng = make_engine(repo, h, max_iterations=2, sleeps=sleeps)
    eng.run()
    assert len(h.prompts) == 3  # 2 attempts for iteration 1, then iteration 2
    assert eng.outcomes[0].result.ok is False
    assert "ended with an error: boom" in h.prompts[2]
    assert eng.iteration == 2


def test_rate_limit_backs_off_forever_then_recovers(repo):
    h = FakeHarness([rate_limited()] * 6 + [works()])
    sleeps = []
    eng = make_engine(repo, h, max_iterations=1, sleeps=sleeps)
    eng.run()
    assert sleeps == [60, 120, 300, 600, 600, 600]
    assert eng.outcomes[0].result.ok


def test_timeout_is_not_fatal(repo):
    h = FakeHarness([times_out(), works()])
    eng = make_engine(repo, h, max_iterations=2)
    eng.run()
    assert eng.outcomes[0].result.timed_out
    assert eng.iteration == 2


def test_missing_record_is_noticed(repo):
    h = FakeHarness([no_record(), works()])
    eng = make_engine(repo, h, max_iterations=2)
    eng.run()
    assert eng.outcomes[0].recorded is False
    assert eng.outcomes[0].changed is True
    assert "did not write its handoff" in h.prompts[1]


def test_driver_exception_is_survived(repo):
    h = FakeHarness([raises(), raises(), works()])
    eng = make_engine(repo, h, max_iterations=2)
    eng.run()
    assert eng.iteration == 2
    assert "harness raised" in (eng.outcomes[0].result.error or "")


def test_stuck_after_three_no_change_iterations(repo):
    h = FakeHarness([nothing()] * 4)
    eng = make_engine(repo, h, max_iterations=4)
    eng.run()
    verdicts = [o.verdict.verdict for o in eng.outcomes]
    assert verdicts[2] == "stuck"
    assert "exhaustion policy" in h.prompts[3]


def test_same_error_three_times_reverts_to_last_ok(repo):
    h = FakeHarness([works("good"), crashes("x"), crashes("x"), crashes("x"), crashes("x"), crashes("x"), crashes("x")])
    eng = make_engine(repo, h, max_iterations=4)
    eng.run()
    verdicts = [o.verdict.verdict for o in eng.outcomes]
    assert verdicts[3] == "revert"
    # failed iterations make no commits, so there was nothing to revert and no patch to save
    assert not (repo / ".ouroboros" / "runs" / "t" / "reverted" / "0004.patch").exists()
    # the tree matches the last accepted tag
    assert git(repo, "diff", "ouroboros/t/ok-0001", "HEAD", "--stat") == ""


def test_resume_for_reuses_session(repo):
    h = FakeHarness([works(), works(), works(), works()])
    eng = make_engine(repo, h, max_iterations=4, resume_for=3)
    eng.run()
    assert [c["resume"] for c in h.calls] == [None, "s1", "s1", None]


def test_status_and_logs_written(repo):
    h = FakeHarness([works()])
    eng = make_engine(repo, h, max_iterations=1)
    eng.run()
    rd = repo / ".ouroboros" / "runs" / "t"
    st = eng.recorder.read_status()
    assert st["state"] == "stopped" and st["iteration"] == 1
    assert (rd / "iterations.jsonl").exists() and (rd / "overseer.jsonl").exists()
    assert (rd / "transcripts").is_dir()


def test_engine_bug_does_not_kill_loop(repo, monkeypatch):
    h = FakeHarness([works(), works()])
    sleeps = []
    eng = make_engine(repo, h, max_iterations=2, sleeps=sleeps)
    calls = {"n": 0}
    orig = eng.git.commit

    def flaky(msg, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("disk hiccup")
        return orig(msg, **kw)

    monkeypatch.setattr(eng.git, "commit", flaky)
    reason = eng.run()
    assert "max_iterations" in reason
    assert sleeps and sleeps[0] == 60


def test_failed_iterations_slow_down(repo):
    h = FakeHarness([crashes("a"), crashes("a"), crashes("b"), crashes("b"), works()])
    sleeps = []
    eng = make_engine(repo, h, max_iterations=3, sleeps=sleeps)
    eng.run()
    # per iteration: 5s retry gap; then 60s after the 1st failed iteration, 120s after the 2nd
    assert [s for s in sleeps if s >= 60] == [60, 120]


def test_session_limit_waits_until_reset(repo):
    from ouroboros.harness.base import Result

    def limited(cwd, prompt):
        return Result(text="You've hit your session limit · resets 2:50am (Europe/Madrid)", exit_code=0,
                      error="You've hit your session limit · resets 2:50am (Europe/Madrid)")

    h = FakeHarness([limited, works()])
    sleeps = []
    eng = make_engine(repo, h, max_iterations=1, sleeps=sleeps)
    eng.run()
    assert len(sleeps) == 1 and 60 < sleeps[0] <= 24 * 3600 + 60
    assert eng.outcomes[0].result.ok


def test_actor_own_commit_counts_as_change(repo):
    def commits_itself(cwd, prompt):
        (cwd / "self.txt").write_text("x\n")
        git(cwd, "add", "-A")
        git(cwd, "commit", "-q", "-m", "actor commit")
        from fake_harness import _handoff
        _handoff(cwd, "committed myself")
        from ouroboros.harness.base import Result
        return Result(text="done a unit", session_id="s")

    eng = make_engine(repo, FakeHarness([commits_itself]), max_iterations=1)
    eng.run()
    assert eng.outcomes[0].changed is True and eng.no_change_streak == 0


def test_failed_iteration_makes_no_empty_commit(repo):
    before = len(git(repo, "log", "--oneline").splitlines())
    eng = make_engine(repo, FakeHarness([crashes("x"), crashes("x")]), max_iterations=1)
    eng.run()
    after = len(git(repo, "log", "--oneline").splitlines())
    assert after == before


def test_plan_signals_carry_the_run_budget(repo: Path):
    from ouroboros.config import StopConfig
    eng = make_engine(repo, FakeHarness([works("a")]), stop=StopConfig(max_iterations=3, after="2h"))
    eng.run()
    text = eng._plan_signals()
    assert "iterations so far: 3 of 3" in text and "run budget:" in text and "h left" in text
    eng2 = make_engine(repo, FakeHarness([works("b")]), max_iterations=1)
    eng2.run()
    assert "(no iteration cap)" not in eng2._plan_signals() and "no wall-clock cap" in eng2._plan_signals()


def test_handoff_planner_runs_every_n_iterations(repo: Path):
    def plans(cwd: Path, prompt: str) -> Result:
        root = cwd / ".ouroboros"
        (root / "plan.md").write_text("# Plan\n\n## short\n\n- unit A [handoff/0001.md]\n\n## medium\n\n- B\n\n## long\n\n- C\n")
        with (root / "bets.md").open("a") as f:
            f.write("# Bets\n\n## Bet 2026-09-06: A before B\n\n### Why\n\nevidence\n\n### Changed\n\nA moved up\n")
        return Result(text="planned", cost_usd=0.05)

    planner = FakeHarness([], default=plans)
    h = FakeHarness([works()] * 4)
    eng = make_engine(repo, h, max_iterations=4, planner=planner)
    eng.config.plan.every = 2
    eng.run()
    assert len(planner.prompts) == 2 and "You are the planner" in planner.prompts[0]
    assert "handoff/0001.md" in planner.prompts[1]           # recent handoffs are in the prompt
    steps = eng.recorder.read_jsonl(eng.recorder.iterations)
    bets = [s for s in steps if s.get("step") == "plan"]
    assert [b["iteration"] for b in bets] == [2, 4] and "A before B" in bets[0]["bet"]
    assert "unit A" in h.prompts[2]                          # the actor reads the plan next iteration


def test_plan_disabled_skips_planner(repo: Path):
    planner = FakeHarness([], default=nothing())
    eng = make_engine(repo, FakeHarness([works()] * 2), max_iterations=2, planner=planner)
    eng.config.plan.enabled = False
    eng.config.plan.every = 1
    eng.run()
    assert planner.prompts == []


@pytest.mark.parametrize("own_commit", [False, True])
def test_noop_reconcile_does_not_create_marker_commit(repo, monkeypatch, own_commit):
    eng = make_engine(repo, FakeHarness([]))
    def maintain(cwd, prompt):
        if own_commit:
            (cwd / "maintained.txt").write_text("reconciled")
            git(cwd, "add", "-A")
            git(cwd, "commit", "-qm", "maintainer work")
        return Result()

    eng.maintainer = FakeHarness([], default=maintain)
    monkeypatch.setattr(eng.memory, 'needs_reconcile', lambda: True)
    monkeypatch.setattr(eng.memory, 'reconcile_prompt', lambda: 'reconcile')
    monkeypatch.setattr(eng, '_maybe_plan', lambda *args: None)
    before = eng.git.head()
    eng._maybe_reconcile(1)
    if own_commit:
        assert git(repo, "rev-parse", "HEAD^") == before
        assert git(repo, "log", "-1", "--format=%s") == "maintainer work"
    else:
        assert eng.git.head() == before


def test_newer_planner_usage_is_not_overwritten_by_actor(repo):
    from ouroboros.usage import UsageSnapshot, Window

    def actor(cwd, prompt):
        result = works()(cwd, prompt)
        result.usage = UsageSnapshot('claude', {'five_hour': Window('five_hour', .75)})
        return result

    def planner(cwd, prompt):
        return Result(usage=UsageSnapshot('claude', {'five_hour': Window('five_hour', .96)}))

    eng = make_engine(repo, FakeHarness([actor]), max_iterations=1,
                      planner=FakeHarness([planner]))
    eng.config.plan.every = 1
    eng.run()
    assert eng.budget.usage.latest['claude'].windows['five_hour'].utilization == .96


def test_unchanged_iterations_do_not_schedule_bookkeeping(repo, monkeypatch):
    eng = make_engine(repo, FakeHarness([], default=nothing()), max_iterations=6)
    marked = []
    reconciled = []
    monkeypatch.setattr(eng.memory, 'mark_iteration', lambda: marked.append(True))
    monkeypatch.setattr(eng, '_maybe_reconcile', lambda n: reconciled.append(n))
    eng.run()
    assert not marked and not reconciled and eng.since_plan == 0


def test_stuck_verdicts_slow_down(repo):
    # An actor that changes nothing: the rules overseer calls it stuck from the third
    # iteration on. Repeating that at full speed still pays for a critic, maintainer,
    # planner and overseer call every time, so the streak has to back off.
    h = FakeHarness([], default=nothing())
    sleeps = []
    eng = make_engine(repo, h, max_iterations=7, sleeps=sleeps)
    eng.run()
    # stuck from the 3rd iteration; the 7th hits max_iterations and stops without sleeping
    assert [s for s in sleeps if s >= 60] == [60, 120, 300, 600]


def test_a_change_clears_the_stuck_streak(repo):
    h = FakeHarness([nothing(), nothing(), nothing(), nothing(), works("out")])
    sleeps = []
    eng = make_engine(repo, h, max_iterations=5, sleeps=sleeps)
    eng.run()
    assert [s for s in sleeps if s >= 60] == [60, 120]
    assert eng.stuck_iterations == 0 and eng.budget.stuck_streak == 0


def test_max_stuck_stops_the_run(repo):
    # The night that is never coming back: stop instead of spending until the wall clock.
    h = FakeHarness([], default=nothing())
    sleeps = []
    eng = make_engine(repo, h, sleeps=sleeps,
                      stop=StopConfig(max_iterations=50, max_stuck=3))
    reason = eng.run()
    assert "stuck 3x in a row" in reason
    assert eng.iteration < 50
    # the stop wins over the backoff: no sleeping on the iteration that ends the run
    assert [s for s in sleeps if s >= 60] == [60, 120]


def test_max_stuck_none_never_stops(repo):
    h = FakeHarness([], default=nothing())
    eng = make_engine(repo, h, sleeps=[], stop=StopConfig(max_iterations=6, max_stuck=None))
    assert "max_iterations" in eng.run()


# -- loop detection: motion that is not progress ----------------------------

def test_records_without_product_changes_become_a_looping_verdict(repo):
    from ouroboros.config import LoopConfig
    h = FakeHarness([], default=records_only())
    eng = make_engine(repo, h, max_iterations=4)
    eng.config.loop = LoopConfig(product_after=2, frontier_after=None)
    eng.loops.cfg = eng.config.loop
    eng.run()
    verdicts = [o.verdict.verdict for o in eng.outcomes]
    # The first two iterations only build the streak; the threshold is crossed on the second.
    assert verdicts[0] == "continue"
    assert verdicts[1:] == ["looping", "looping", "looping"]
    assert "not the work" in eng.outcomes[1].verdict.reply or "instead of doing it" in eng.outcomes[1].verdict.reply


def test_one_real_change_clears_the_streak(repo):
    from ouroboros.config import LoopConfig
    h = FakeHarness([records_only(), records_only(), works("a real unit"), records_only()])
    eng = make_engine(repo, h, max_iterations=4)
    eng.config.loop = LoopConfig(product_after=2, frontier_after=None)
    eng.loops.cfg = eng.config.loop
    eng.run()
    assert [o.verdict.verdict for o in eng.outcomes] == ["continue", "looping", "continue", "continue"]


def test_the_loop_is_named_to_the_actor_in_the_next_prompt(repo):
    from ouroboros.config import LoopConfig
    h = FakeHarness([], default=records_only("Record the role handoff"))
    eng = make_engine(repo, h, max_iterations=3)
    eng.config.loop = LoopConfig(product_after=1, frontier_after=None)
    eng.loops.cfg = eng.config.loop
    eng.run()
    assert "Record the role handoff" in h.prompts[-1]
    assert "not a record and not a plan" in h.prompts[-1]


def test_escalation_rotates_the_actor_to_its_fallback(repo):
    """Step 3 blocks the active harness on the shared board, so the pool falls back."""
    from ouroboros.config import LoopConfig
    from ouroboros.harness.pool import LimitBoard, PooledHarness
    first, second = FakeHarness([], default=records_only()), FakeHarness([], default=works("b"))
    first.name, second.name = "first", "second"
    board = LimitBoard()
    pool = PooledHarness([(first, None), (second, None)], board)
    eng = make_engine(repo, pool, max_iterations=12)
    eng.config.loop = LoopConfig(product_after=2, frontier_after=None, escalate_every=2)
    eng.loops.cfg = eng.config.loop
    eng.run()
    assert board.is_blocked("first"), "the looping harness should have been rotated out"
    assert second.calls, "the fallback should have taken a turn"


def test_a_loop_forces_a_planner_pass_that_is_told_not_to_repeat_itself(repo):
    from ouroboros.config import LoopConfig
    planner = FakeHarness([], default=nothing())
    h = FakeHarness([], default=records_only())
    eng = make_engine(repo, h, max_iterations=6, planner=planner)
    eng.config.loop = LoopConfig(product_after=1, frontier_after=None, escalate_every=1)
    eng.loops.cfg = eng.config.loop
    eng.run()
    assert planner.prompts, "escalation step 2 should have forced a planner pass"
    assert any("LOOP DETECTED" in p for p in planner.prompts)


def test_a_repeated_bet_is_measured_even_when_the_actor_is_productive(repo):
    """The planner restating itself is a loop of its own, on the plan rather than the work."""
    eng = make_engine(repo, FakeHarness([], default=works()), max_iterations=1)
    for n in ("fifteen", "twenty", "twenty-four", "thirty-four"):
        eng.loops.observe_bet(f"Bet: retain the conditional rehearsal after {n} unchanged iterations")
    report = eng.loops.report()
    assert report is not None and report.signal == "repeat_bet"
