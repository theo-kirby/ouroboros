"""Fallback chains: switch harness on a usage limit, come back when it resets."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path

from ouroboros.config import Config, RoleConfig, FallbackConfig
from ouroboros.harness.base import Result
from ouroboros.harness.pool import LimitBoard, PooledHarness

from fake_harness import FakeHarness, works, rate_limited, crashes
from test_engine import make_engine


def limited(msg="You've hit your session limit · resets 2:50am (Europe/Madrid)"):
    return lambda cwd, prompt: Result(text="", exit_code=1, error=msg)


def limited_epoch(epoch: float):
    return lambda cwd, prompt: Result(text="", exit_code=1, error="usage_limit_reached", extra={"kind": "limit", "resets_at": epoch})


class Clock:
    def __init__(self, t=None):
        self.t = t if t is not None else time.time()

    def __call__(self):
        return self.t


def named(name: str, script):
    h = FakeHarness(script)
    h.name = name
    return h


def test_kind_classification():
    assert Result(text="fine").kind == "ok"
    assert Result(timed_out=True).kind == "timeout"
    assert Result(exit_code=1, error="You've hit your session limit · resets 2:50am (Europe/Madrid)").kind == "limit"
    assert Result(exit_code=1, error="Claude AI usage limit reached|1757100000").kind == "limit"
    assert Result(exit_code=1, error="429 rate limit exceeded").kind == "transient"   # per-minute throttle; strikes escalate it
    assert Result(exit_code=1, error='{"type":"usage_limit_reached","resets_in_seconds":1200}').kind == "limit"
    assert Result(exit_code=1, error="API Error: 529 overloaded_error").kind == "transient"
    assert Result(exit_code=1, error="ECONNRESET").kind == "transient"
    assert Result(exit_code=1, error="Not logged in. Please run /login").kind == "auth"
    assert Result(exit_code=1, error="segfault in tool").kind == "error"
    assert Result(exit_code=1, error="whatever", extra={"kind": "limit"}).kind == "limit"


def test_board_blocks_until_reset_or_cooldown():
    clock = Clock(1_000_000.0)   # a whole number: datetime round-trips lose sub-microsecond float precision
    board = LimitBoard(cooldown=100, max_cooldown=350, clock=clock)
    until = datetime.fromtimestamp(clock.t + 500, tz=timezone.utc)
    at = board.block("claude", until=until)
    assert at == clock.t + 560 and board.is_blocked("claude")
    clock.t += 561
    assert not board.is_blocked("claude")
    # unknown reset: 100, 200, 350 (capped)
    assert board.block("codex") == clock.t + 100
    assert board.block("codex") == clock.t + 200
    assert board.block("codex") == clock.t + 350
    board.clear("codex")
    assert board.block("codex") == clock.t + 100
    assert board.earliest() == clock.t + 100
    assert "codex" in board.snapshot()


def test_transient_strikes_turn_into_a_block():
    board = LimitBoard(transient_strikes=2, clock=Clock())
    assert not board.strike("pi")
    assert board.strike("pi")
    assert board.is_blocked("pi")


def test_pool_switches_on_limit_and_returns_after_reset(tmp_path: Path):
    clock = Clock()
    board = LimitBoard(clock=clock)
    reset = clock.t + 3600
    claude = named("claude", [limited_epoch(reset), works("claude again")])
    codex = named("codex", [works("codex work")])
    pool = PooledHarness([(claude, "opus"), (codex, "gpt-5-codex")], board)
    r = pool.run("p", cwd=tmp_path, timeout=1)
    assert r.ok and "codex" in r.text and pool.name == "codex"
    assert codex.calls[0]["model"] == "gpt-5-codex"
    assert board.is_blocked("claude")
    # still blocked: codex keeps working
    codex.script = [works("codex more")]
    assert "codex more" in pool.run("p", cwd=tmp_path, timeout=1).text
    # past the reset: back to claude
    clock.t = reset + 61
    r = pool.run("p", cwd=tmp_path, timeout=1)
    assert "claude again" in r.text and pool.name == "claude"


def test_pool_all_blocked_reports_earliest_reset(tmp_path: Path):
    clock = Clock()
    board = LimitBoard(cooldown=600, clock=clock)
    claude = named("claude", [limited_epoch(clock.t + 7200)])
    codex = named("codex", [crashes("usage_limit_reached")])
    pool = PooledHarness([(claude, None), (codex, None)], board)
    r = pool.run("p", cwd=tmp_path, timeout=1)
    assert r.kind == "limit" and "every harness is blocked" in r.error
    assert r.extra["resets_at"] == clock.t + 600   # codex's cooldown is the earliest
    now = datetime.fromtimestamp(clock.t, tz=timezone.utc)
    assert abs(r.reset_wait_seconds(now=now) - 600) < 1


def test_pool_resume_only_on_the_minting_harness(tmp_path: Path):
    board = LimitBoard(clock=Clock())
    claude = named("claude", [works("a"), limited_epoch(Clock().t + 9999)])
    codex = named("codex", [works("b")])
    pool = PooledHarness([(claude, None), (codex, None)], board)
    r1 = pool.run("p", cwd=tmp_path, timeout=1)
    assert r1.session_id == "s1"
    pool.run("p", cwd=tmp_path, timeout=1, resume="s1")   # claude limited -> codex, no resume
    assert claude.calls[1]["resume"] == "s1" and codex.calls[0]["resume"] is None


def test_pool_transient_returns_to_caller_then_blocks(tmp_path: Path):
    board = LimitBoard(transient_strikes=2, clock=Clock())
    claude = named("claude", [crashes("529 overloaded"), crashes("529 overloaded")])
    codex = named("codex", [works("codex")])
    pool = PooledHarness([(claude, None), (codex, None)], board)
    r = pool.run("p", cwd=tmp_path, timeout=1)
    assert r.kind == "transient" and len(codex.calls) == 0
    r = pool.run("p", cwd=tmp_path, timeout=1)        # second strike -> block -> codex
    assert r.ok and pool.name == "codex"


def test_engine_falls_back_without_sleeping(repo: Path):
    board = LimitBoard(clock=Clock())
    claude = named("claude", [works("one"), limited_epoch(time.time() + 3600), works("never")])
    codex = named("codex", [works("two"), works("three")])
    pool = PooledHarness([(claude, None), (codex, None)], board)
    eng = make_engine(repo, pool, max_iterations=3)
    sleeps = []
    eng.sleeper = sleeps.append
    eng.run()
    assert eng.iteration == 3
    assert [c for c in sleeps if c > 10] == []          # no limit wait: codex took over
    assert (repo / "work.txt").read_text().splitlines() == ["one", "two", "three"]
    assert len(claude.calls) == 2 and len(codex.calls) == 2


def test_engine_waits_when_every_harness_is_limited(repo: Path):
    clock = Clock()
    board = LimitBoard(cooldown=900, clock=clock)
    claude = named("claude", [limited_epoch(clock.t + 3000), works("later")])
    codex = named("codex", [crashes("usage_limit_reached")])
    pool = PooledHarness([(claude, None), (codex, None)], board)
    sleeps: list[float] = []
    eng = make_engine(repo, pool, max_iterations=1, sleeps=sleeps)

    def sleep(sec):
        sleeps.append(sec)
        clock.t += sec
    eng.sleeper = sleep
    eng.run()
    assert any(800 < s < 1100 for s in sleeps), sleeps   # slept until codex's cooldown, not claude's reset
    assert eng.iteration == 1


def test_config_chain():
    cfg = Config.model_validate({"roles": {"actor": {"harness": "claude", "model": "opus",
                                                     "fallback": [{"harness": "codex", "model": "gpt-5-codex"}, {"harness": "pi"}]}}})
    assert cfg.role("actor").chain == [("claude", "opus"), ("codex", "gpt-5-codex"), ("pi", None)]
    assert Config().role("critic").chain == [("claude", None)]
    assert cfg.limits.cooldown_seconds == 1800


def test_usage_from_limited_attempt_survives_fallback(tmp_path):
    from ouroboros.usage import UsageSnapshot, Window
    snapshot = UsageSnapshot('claude', {'five_hour': Window('five_hour', .96)})
    claude = named('claude', [lambda cwd, prompt: Result(
        exit_code=1, error='session limit', usage=snapshot)])
    codex = named('codex', [lambda cwd, prompt: Result()])
    board = LimitBoard()
    pool = PooledHarness([(claude, None), (codex, None)], board)
    assert pool.run('p', cwd=tmp_path, timeout=1).ok
    assert board.usage.latest['claude'] == snapshot


# -- reserves: block the harness, not the run -------------------------------

def _usage(util: float, minutes=10080, resets_at=None):
    from ouroboros.usage import UsageSnapshot, Window
    return UsageSnapshot(harness="a", windows={
        "seven_day": Window(name="seven_day", utilization=util, resets_at=resets_at, minutes=minutes)})


def spends(util: float, resets_at=None):
    def b(cwd, prompt):
        return Result(text="did a unit", usage=_usage(util, resets_at=resets_at))
    return b


def test_a_harness_that_spends_its_reserve_is_blocked_and_the_pool_falls_back(tmp_path: Path):
    a, b = FakeHarness([], default=spends(0.90)), FakeHarness([], default=works("b"))
    a.name, b.name = "a", "b"
    board = LimitBoard(reserve={"a": 0.85})
    pool = PooledHarness([(a, None), (b, None)], board)
    first = pool.run("go", cwd=tmp_path, timeout=1)
    assert first.ok, "the call that crosses the reserve still returns its work"
    assert board.is_blocked("a")
    pool.run("go again", cwd=tmp_path, timeout=1)
    assert b.calls, "the next call falls back"


def test_a_reserve_blocks_until_the_window_resets(tmp_path: Path):
    reset = time.time() + 3600
    a = FakeHarness([], default=spends(0.9, resets_at=reset))
    a.name = "a"
    board = LimitBoard(reserve={"a": 0.85})
    PooledHarness([(a, None)], board).run("go", cwd=tmp_path, timeout=1)
    assert board.blocked["a"] > reset, "blocked past the reset, not on a blind cooldown"


def test_below_the_reserve_nothing_is_blocked(tmp_path: Path):
    a = FakeHarness([], default=spends(0.80))
    a.name = "a"
    board = LimitBoard(reserve={"a": 0.85})
    PooledHarness([(a, None)], board).run("go", cwd=tmp_path, timeout=1)
    assert not board.is_blocked("a")


def test_a_harness_with_no_reserve_configured_is_never_blocked_by_one(tmp_path: Path):
    a = FakeHarness([], default=spends(0.99))
    a.name = "a"
    board = LimitBoard()
    PooledHarness([(a, None)], board).run("go", cwd=tmp_path, timeout=1)
    assert not board.is_blocked("a")


def test_a_short_window_does_not_trip_a_reserve(tmp_path: Path):
    """Five-hour windows heal on their own; a reserve is about the long one."""
    from ouroboros.usage import UsageSnapshot, Window
    a = FakeHarness([], default=lambda cwd, p: Result(
        text="x", usage=UsageSnapshot(harness="a", windows={
            "five_hour": Window(name="five_hour", utilization=0.99, minutes=300)})))
    a.name = "a"
    board = LimitBoard(reserve={"a": 0.85})
    PooledHarness([(a, None)], board).run("go", cwd=tmp_path, timeout=1)
    assert not board.is_blocked("a")
