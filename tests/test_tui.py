import json

from ouroboros.tui.layout import Rect, col, compute_layout, leaf, row
from ouroboros.tui.state import derive_iterations, parse_events, load_snapshot
from ouroboros.tui.widgets import braille_chart, fmt_duration, hbar, wrap


def jl(*objs):
    return [json.dumps(o) for o in objs]


def test_parse_claude_stream_json():
    ev = parse_events(jl(
        {"type": "system", "subtype": "init", "session_id": "abcdef123", "model": "claude-fable-5-1"},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "Looking at the build."},
                                                        {"type": "tool_use", "name": "Bash", "input": {"command": "pixi run gate", "description": "x"}}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "content": "ok 12 passed", "is_error": False}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "content": [{"type": "text", "text": "boom"}], "is_error": True}]}},
        {"type": "result", "subtype": "success", "num_turns": 6, "total_cost_usd": 1.25, "is_error": False},
    ))
    kinds = [(e.kind, e.text) for e in ev]
    assert kinds[0] == ("init", "session abcdef12  model claude-fable-5-1")
    assert kinds[1] == ("text", "Looking at the build.") and kinds[2] == ("tool", "Bash  pixi run gate")
    assert kinds[3] == ("tool_out", "ok 12 passed") and kinds[4] == ("error", "boom")
    assert kinds[5] == ("result", "success  turns 6  $1.25 api-eq")


def test_parse_codex_and_pi_events():
    ev = parse_events(jl(
        {"type": "thread.started", "thread_id": "01a07822-x"},
        {"type": "item.completed", "item": {"type": "reasoning", "text": "hmm"}},
        {"type": "item.completed", "item": {"type": "command_execution", "command": "ls", "exit_code": 2}},
        {"type": "item.completed", "item": {"type": "file_change", "changes": [{"path": "a.py"}, {"path": "b.py"}]}},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "Done."}},
        {"type": "error", "message": "You've hit your usage limit"},
        {"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 2}},
    ))
    assert [e.kind for e in ev] == ["init", "think", "tool", "tool", "text", "error", "result"]
    assert ev[2].text == "$ ls  → exit 2" and ev[3].text == "edit  a.py, b.py"
    ev = parse_events(jl(
        {"type": "session", "id": "pi-session-1"},
        {"type": "tool_execution_start", "toolName": "read", "args": {"path": "x.py"}},
        {"type": "message_end", "message": {"role": "assistant", "content": [{"type": "text", "text": "hi"}], "stopReason": "error",
                                             "errorMessage": "rate limited", "usage": {"cost": {"total": 0.01}}}},
        {"not": "json-with-type"},
        "garbage",
    ))
    assert [e.kind for e in ev] == ["init", "tool", "text", "error", "result"]
    assert ev[1].text == "read  x.py" and ev[3].text == "rate limited"


def test_derive_iterations_and_stage_durations():
    steps = [
        {"ts": "2026-09-06T19:00:00+00:00", "iteration": 1, "step": "actor"},
        {"ts": "2026-09-06T19:00:01+00:00", "iteration": 1, "step": "commit", "changed": True, "recorded": True, "cost": 2.0},
        {"ts": "2026-09-06T19:00:31+00:00", "iteration": 1, "step": "critique", "verdict": "accept"},
        {"ts": "2026-09-06T19:01:31+00:00", "iteration": 1, "step": "oversee", "verdict": "continue"},
        {"ts": "2026-09-06T19:11:31+00:00", "iteration": 2, "step": "actor", "error": "timeout"},
        {"ts": "2026-09-06T19:11:32+00:00", "iteration": 2, "step": "commit", "changed": False, "recorded": False, "cost": 0},
        {"ts": "2026-09-06T19:11:40+00:00", "iteration": 2, "step": "oversee", "verdict": "revert"},
        {"ts": "2026-09-06T19:11:41+00:00", "iteration": 2, "step": "revert"},
        {"ts": "2026-09-06T19:21:41+00:00", "iteration": 2, "step": "reconcile"},
        {"ts": "2026-09-06T19:26:41+00:00", "iteration": 2, "step": "plan", "bet": "Bet: x"},
    ]
    its, stages, reconciles, plans = derive_iterations(steps)
    assert [i.n for i in its] == [1, 2]
    assert its[0].changed and its[0].recorded and its[0].verdict == "continue" and its[0].critique == "accept" and its[0].cost == 2.0
    assert its[1].reverted and its[1].error == "timeout" and its[1].bet == "Bet: x" and its[1].actor_seconds == 600.0
    assert stages["critic"].last == 30.0 and stages["overseer"].count == 2 and stages["overseer"].avg == 34.0
    assert stages["maintainer"].last == 600.0 and stages["planner"].last == 300.0 and stages["commit"].count == 2
    assert (reconciles, plans) == (1, 1)


def test_load_snapshot_from_a_run_dir(tmp_path):
    run = tmp_path / ".ouroboros" / "runs" / "t"
    (run / "transcripts").mkdir(parents=True)
    (run / "status.json").write_text(json.dumps({"run": "t", "state": "work", "iteration": 3, "epoch": 1.0, "elapsed_s": 60, "cost_usd": 1.5, "harness": "claude"}))
    (run / "iterations.jsonl").write_text("\n".join(jl(
        {"ts": "2026-09-06T19:00:00+00:00", "iteration": 1, "step": "actor"},
        {"ts": "2026-09-06T19:00:01+00:00", "iteration": 1, "step": "commit", "changed": True, "recorded": True, "cost": 1.5},
    )) + "\n")
    (run / "overseer.jsonl").write_text(json.dumps({"iteration": 1, "verdict": "continue", "reason": "ok", "reply": ""}) + "\n")
    (run / "loop.log").write_text("2026-09-06T19:00:00+00:00 harness: claude -> codex\n2026-09-06T19:00:01+00:00 [1] actor: ok\n")
    (run / "transcripts" / "0001-actor.json").write_text("\n".join(jl({"type": "assistant", "message": {"content": [{"type": "text", "text": "hello"}]}})) + "\n")
    (tmp_path / "PLAN.md").write_text("# plan\n\n## short\n\n- do A [rec: x-1]\n- do B\n\n## medium\n\n- C\n")
    s = load_snapshot(run, repo=tmp_path, stop_after_s=3600.0, chains={"actor": ["claude", "codex"]})
    assert s.stage == "actor" and not s.alive and s.pid is None and s.switches == 1
    assert [e.text for e in s.feed] == ["hello"] and s.feed_role == "0001-actor"
    assert s.plan_short == ["do A", "do B"] and s.cost_series == [1.5] and len(s.iterations) == 1
    (tmp_path / "PLAN.md").unlink()
    (tmp_path / ".ouroboros" / "plan.md").write_text("# Plan\n\n(agent-owned. rewrite freely.)\n")
    assert load_snapshot(run, repo=tmp_path).plan_short == []


def test_layout_and_widgets():
    view = col(row(leaf("a", 1), leaf("b", 1)), leaf("c", 1))
    placed = compute_layout(Rect(5, 0, 20, 80), view, {"a", "b", "c"})
    assert placed["a"] == Rect(5, 0, 10, 40) and placed["b"] == Rect(5, 40, 10, 40) and placed["c"] == Rect(15, 0, 10, 80)
    assert compute_layout(Rect(0, 0, 20, 80), view, {"c"}) == {"c": Rect(0, 0, 20, 80)}
    assert compute_layout(Rect(0, 0, 20, 80), view, set()) == {}
    rows = braille_chart([0, 5, 10], 2, 1, 0, 10)
    assert len(rows) == 1 and len(rows[0]) == 2 and rows[0] != "  "
    assert hbar(5, 10, 4) == "██░░" and fmt_duration(3725) == "1h 02m" and fmt_duration(None) == "—"
    assert wrap("one two three four five", 9, 2) == ["one two", "three f …"] or wrap("one two three four five", 9, 2)[0] == "one two"
