"""Codex and Pi drivers: parse recorded event streams, no CLI needed."""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone

from ouroboros.harness.base import parse_reset_time
from ouroboros.harness.codex import CodexHarness, rate_limits_reset
from ouroboros.harness.pi import PiHarness


def jl(*events) -> str:
    return "\n".join(json.dumps(e) for e in events) + "\n"


# ---- codex

def test_codex_success():
    out = jl({"type": "thread.started", "thread_id": "t-1"}, {"type": "turn.started"},
             {"type": "item.completed", "item": {"id": "i1", "type": "reasoning", "text": "hmm"}},
             {"type": "item.completed", "item": {"id": "i2", "type": "agent_message", "text": "Done. Next: tests."}},
             {"type": "turn.completed", "usage": {"input_tokens": 100, "output_tokens": 20}})
    r = CodexHarness.parse(out, "", 0, False, None)
    assert r.ok and r.text == "Done. Next: tests." and r.session_id == "t-1" and r.turns == 1
    assert r.extra["usage"]["input_tokens"] == 100 and r.cost_usd is None


def test_codex_usage_limit_with_time():
    out = jl({"type": "thread.started", "thread_id": "t-2"},
             {"type": "error", "message": "Reconnecting... 1/5 (stream disconnected)"},
             {"type": "error", "message": "You've hit your usage limit. Upgrade to Pro, or try again at 3:45 PM."},
             {"type": "turn.failed", "error": {"message": "You've hit your usage limit. Upgrade to Pro, or try again at 3:45 PM."}})
    r = CodexHarness.parse(out, "", 1, False, None)
    assert r.kind == "limit" and "Reconnecting" not in r.error
    at = r.reset_at()
    assert at is not None and (at.hour, at.minute) == (15, 45)


def test_codex_usage_limit_with_date():
    now = datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)
    at = parse_reset_time("You've hit your usage limit. Try again at Sep 7th, 2026 3:45 PM.", now=now)
    assert (at.month, at.day, at.hour, at.minute) == (9, 7, 15, 45)


def test_codex_auth_and_transient():
    out = jl({"type": "error", "message": "Your access token could not be refreshed because your refresh token has expired. Please log out and sign in again."},
             {"type": "turn.failed", "error": {"message": "unauthorized"}})
    assert CodexHarness.parse(out, "", 1, False, None).kind == "auth"
    out = jl({"type": "turn.failed", "error": {"message": "We're currently experiencing high demand, which may cause temporary errors."}})
    assert CodexHarness.parse(out, "", 1, False, None).kind == "transient"
    out = jl({"type": "turn.failed", "error": {"message": "exceeded retry limit, last status: 429 Too Many Requests"}})
    assert CodexHarness.parse(out, "", 1, False, None).kind == "limit"


def test_codex_rollout_rate_limits():
    now = 1_788_100_000.0
    limits = {"primary": {"used_percent": 100.0, "window_minutes": 300, "resets_at": now + 2905},
              "secondary": {"used_percent": 40.0, "window_minutes": 10080, "resets_at": now + 589_705}}
    assert rate_limits_reset(limits, now=now) == now + 2905
    limits["secondary"]["used_percent"] = 99.5
    assert rate_limits_reset(limits, now=now) == now + 589_705
    assert rate_limits_reset({"primary": {"used_percent": 3, "resets_at": now - 10}}, now=now) is None
    assert rate_limits_reset({"primary": {"used_percent": 3, "resets_at": now - 10},
                              "secondary": {"used_percent": 3, "resets_at": now + 500_000}}, now=now) is None


def test_codex_cmd_shapes(tmp_path):
    h = CodexHarness(binary="codex")
    new = h.build_cmd(cwd=tmp_path, resume=None, model="gpt-5-codex", tools=None)
    assert new[1:3] == ["exec", "--json"] and "--dangerously-bypass-approvals-and-sandbox" in new and new[-1] == "-"
    assert "-C" in new and "-m" in new
    res = h.build_cmd(cwd=tmp_path, resume="abc", model=None, tools="readonly")
    assert res[1:4] == ["exec", "resume", "abc"] and "-C" not in res and "-s" not in res
    ro = h.build_cmd(cwd=tmp_path, resume=None, model=None, tools="readonly")
    assert "-s" in ro and "--dangerously-bypass-approvals-and-sandbox" not in ro


# ---- pi

def assistant(text, *, stop="stop", err=None, cost=0.001):
    msg = {"role": "assistant", "content": [{"type": "text", "text": text}] if text else [],
           "usage": {"input": 10, "output": 5, "cost": {"total": cost}}, "stopReason": stop}
    if err:
        msg["errorMessage"] = err
    return {"type": "message_end", "message": msg}


def test_pi_success_sums_cost():
    out = jl({"type": "session", "version": 3, "id": "u-1"}, {"type": "agent_start"},
             assistant("looking", cost=0.002), {"type": "tool_execution_end"},
             assistant("Done. Next: more.", cost=0.003), {"type": "agent_end"})
    r = PiHarness.parse(out, "", 0, False, None)
    assert r.ok and r.text == "Done. Next: more." and r.session_id == "u-1" and r.turns == 2
    assert abs(r.cost_usd - 0.005) < 1e-9


def test_pi_error_exit_zero_is_still_an_error():
    out = jl({"type": "session", "version": 3, "id": "u-2"},
             assistant("", stop="error", err="You have hit your ChatGPT usage limit (plus plan). Try again in ~12 min."),
             {"type": "agent_end"})
    r = PiHarness.parse(out, "", 0, False, None)
    assert not r.ok and r.kind == "limit"
    wait = r.reset_wait_seconds()
    assert 11 * 60 < wait <= 12 * 60


def test_pi_anthropic_limit_passes_through():
    err = '429 {"type":"error","error":{"type":"rate_limit_error","message":"You\'ve hit your session limit · resets 2:50am (Europe/Madrid)"}}'
    out = jl({"type": "session", "id": "u-3"}, assistant("", stop="error", err=err))
    r = PiHarness.parse(out, "", 0, False, None)
    assert r.kind == "limit" and r.reset_at() is not None


def test_pi_retry_then_success_is_ok():
    out = jl({"type": "session", "id": "u-4"},
             assistant("", stop="error", err="529 overloaded"),
             {"type": "auto_retry_start", "attempt": 1, "maxAttempts": 3, "delayMs": 2000},
             assistant("fine now"), {"type": "agent_end"})
    r = PiHarness.parse(out, "", 0, False, None)
    assert r.ok and r.text == "fine now"


def test_pi_auth():
    out = jl({"type": "session", "id": "u-5"}, assistant("", stop="error", err="No API key for provider: anthropic"))
    assert PiHarness.parse(out, "", 0, False, None).kind == "auth"


def test_pi_cmd_shape():
    cmd = PiHarness(binary="pi").build_cmd(session_id="abc", model="anthropic/claude-sonnet-4-5", system_append="be brief", tools="none")
    assert cmd[1:5] == ["-p", "--mode", "json", "--session-id"] and "--no-tools" in cmd and "--append-system-prompt" in cmd


def test_reset_in_and_epoch_forms():
    now = datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)
    assert parse_reset_time("Rate limit reached. Please try again in 11.054s.", now=now) == now.replace(second=11, microsecond=54000)
    assert parse_reset_time('{"type":"usage_limit_reached","plan_type":"plus","resets_at":1788102905}', now=now).timestamp() == 1788102905
