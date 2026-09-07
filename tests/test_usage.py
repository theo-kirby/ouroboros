"""Metering a subscription. Payload shapes are the ones the cadex nt2 run recorded."""

import json

from ouroboros.budget import BudgetClock
from ouroboros.config import StopConfig
from ouroboros.usage import (
    UsageLedger,
    UsageSnapshot,
    Window,
    find_codex_rollout,
    from_claude_stream,
    from_codex_rollout,
    window_name,
)

CLAUDE_EVENT = {
    "type": "rate_limit_event",
    "session_id": "s1",
    "rate_limit_info": {
        "status": "allowed_warning",
        "resetsAt": 1788775800,
        "rateLimitType": "five_hour",
        "utilization": 0.96,
        "unifiedWindows": {
            "five_hour": {"utilization": 0.96, "resetsAt": 1788775800},
            "seven_day": {"utilization": 0.33, "resetsAt": 1788933600},
            "seven_day_overage_included": {"utilization": 0.65, "resetsAt": 1788933600},
        },
    },
}

CODEX_ROLLOUT_EVENT = {
    "timestamp": "2026-09-06T23:12:34.212Z",
    "type": "event_msg",
    "payload": {
        "type": "token_count",
        "info": {"total_token_usage": {"total_tokens": 157616}},
        "rate_limits": {
            "limit_id": "codex",
            "primary": {"used_percent": 55.0, "window_minutes": 10080, "resets_at": 1789205350},
            "secondary": None,
            "plan_type": "prolite",
        },
    },
}


def _claude_stream(*infos):
    out = []
    for info in infos:
        ev = dict(CLAUDE_EVENT)
        ev["rate_limit_info"] = info
        out.append(json.dumps(ev))
    out.append(json.dumps({"type": "result", "subtype": "success", "total_cost_usd": 1.0}))
    return "\n".join(out)


def test_claude_windows_come_off_the_stream():
    snap = from_claude_stream(_claude_stream(CLAUDE_EVENT["rate_limit_info"]))
    assert snap.harness == "claude"
    assert snap.windows["five_hour"].utilization == 0.96
    assert snap.windows["five_hour"].minutes == 300
    assert snap.windows["seven_day"].utilization == 0.33
    assert snap.windows["seven_day"].resets_at == 1788933600


def test_the_last_reading_wins_because_a_window_only_fills():
    early = {"unifiedWindows": {"five_hour": {"utilization": 0.11, "resetsAt": 1}}}
    late = {"unifiedWindows": {"five_hour": {"utilization": 0.98, "resetsAt": 1}}}
    snap = from_claude_stream(_claude_stream(early, late))
    assert snap.windows["five_hour"].utilization == 0.98


def test_an_older_claude_reporting_one_flat_window_is_still_metered():
    snap = from_claude_stream(_claude_stream(
        {"rateLimitType": "five_hour", "utilization": 0.5, "resetsAt": 1788775800}))
    assert snap.windows["five_hour"].utilization == 0.5
    assert snap.windows["five_hour"].minutes == 300


def test_a_stream_with_no_rate_limit_event_meters_nothing():
    assert from_claude_stream(json.dumps({"type": "result", "total_cost_usd": 1.0})) is None
    assert from_claude_stream("") is None


def test_codex_windows_come_off_the_rollout_log(tmp_path):
    f = tmp_path / "rollout-2026-09-07T01-12-11-abc.jsonl"
    f.write_text(json.dumps({"type": "event_msg", "payload": {"type": "turn"}}) + "\n"
                 + json.dumps(CODEX_ROLLOUT_EVENT) + "\n")
    snap = from_codex_rollout(f)
    assert snap.harness == "codex"
    assert snap.plan == "prolite"
    # used_percent is out of 100; a window is a fraction.
    assert snap.windows["seven_day"].utilization == 0.55
    assert snap.windows["seven_day"].minutes == 10080
    assert "secondary" not in snap.windows  # null slot is not a window


def test_codex_rollout_is_found_by_thread_id(tmp_path):
    d = tmp_path / "sessions" / "2026" / "09" / "07"
    d.mkdir(parents=True)
    want = d / "rollout-2026-09-07T01-12-11-01a078fe-5d12.jsonl"
    want.write_text("{}\n")
    (d / "rollout-2026-09-07T02-00-00-otherthread.jsonl").write_text("{}\n")
    assert find_codex_rollout(tmp_path, "01a078fe-5d12") == want
    assert find_codex_rollout(tmp_path, "nosuchthread") is None
    assert find_codex_rollout(tmp_path, "") is None


def test_window_naming_falls_back_to_the_length():
    assert window_name(300) == "five_hour"
    assert window_name(10080) == "seven_day"
    assert window_name(4321) == "4321min"
    assert window_name(None) == "window"


def test_only_long_windows_can_end_a_run():
    """A five-hour window refills tonight and the fallback covers it; a week does not."""
    assert Window("seven_day", 0.9, minutes=10080).governs_stop
    assert not Window("five_hour", 0.9, minutes=300).governs_stop
    # Overage measures what you would be billed past the plan, not the plan itself.
    assert not Window("seven_day_overage_included", 0.9, minutes=10080).governs_stop
    assert not Window("window", 0.9, minutes=None).governs_stop


def test_the_ledger_reports_the_rise_this_run_caused():
    led = UsageLedger()
    led.record(UsageSnapshot("claude", {"seven_day": Window("seven_day", 0.33, minutes=10080)}))
    led.record(UsageSnapshot("claude", {"seven_day": Window("seven_day", 0.65, minutes=10080)}))
    assert led.lines() == ["claude seven_day 33% -> 65% (+32 this run)"]


def test_a_window_that_has_not_moved_is_reported_plainly():
    led = UsageLedger()
    led.record(UsageSnapshot("codex", {"seven_day": Window("seven_day", 0.55, minutes=10080)}))
    assert led.lines() == ["codex seven_day 55%"]


def test_the_ledger_keeps_each_harness_apart():
    led = UsageLedger()
    led.record(UsageSnapshot("claude", {"five_hour": Window("five_hour", 0.96, minutes=300)}))
    led.record(UsageSnapshot("codex", {"seven_day": Window("seven_day", 0.55, minutes=10080)}))
    assert led.lines() == ["claude five_hour 96%", "codex seven_day 55%"]


def test_longer_windows_are_listed_first():
    led = UsageLedger()
    led.record(UsageSnapshot("claude", {
        "five_hour": Window("five_hour", 0.96, minutes=300),
        "seven_day": Window("seven_day", 0.33, minutes=10080),
    }))
    assert led.lines() == ["claude seven_day 33%", "claude five_hour 96%"]


def test_a_run_stops_when_a_subscription_window_fills():
    clock = BudgetClock(StopConfig(max_usage=0.8))
    clock.add(usage=UsageSnapshot("claude", {"seven_day": Window("seven_day", 0.79, minutes=10080)}))
    assert clock.should_stop() is None
    clock.add(usage=UsageSnapshot("claude", {"seven_day": Window("seven_day", 0.81, minutes=10080)}))
    stop = clock.should_stop()
    assert "max_usage" in stop and "seven_day" in stop and "81%" in stop


def test_a_full_five_hour_window_does_not_stop_the_run():
    """It refills on its own, and the harness fallback carries the run meanwhile."""
    clock = BudgetClock(StopConfig(max_usage=0.8))
    clock.add(usage=UsageSnapshot("claude", {"five_hour": Window("five_hour", 1.0, minutes=300)}))
    assert clock.should_stop() is None


def test_no_ceiling_means_usage_never_stops_a_run():
    clock = BudgetClock(StopConfig())
    clock.add(usage=UsageSnapshot("claude", {"seven_day": Window("seven_day", 1.0, minutes=10080)}))
    assert clock.should_stop() is None


def test_dollars_and_windows_are_metered_side_by_side():
    """Pi on an API key is really billed; a subscription role is not. Both count."""
    clock = BudgetClock(StopConfig(max_cost_usd=10.0, max_usage=0.9))
    clock.add(cost=4.0, usage=UsageSnapshot("codex", {"seven_day": Window("seven_day", 0.5, minutes=10080)}))
    assert clock.should_stop() is None
    assert clock.cost_usd == 4.0
    assert clock.usage.lines() == ["codex seven_day 50%"]
