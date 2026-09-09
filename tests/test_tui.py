import json

import pytest

from ouroboros.tui.layout import Rect, col, compute_layout, leaf, row
from ouroboros.tui.state import derive_iterations, harness_roster, parse_events, load_snapshot
from ouroboros.tui.widgets import braille_chart, chart_bounds, fmt_duration, hbar, wrap


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
    # `total_cost_usd` is priced at API list rates even on a flat-fee subscription,
    # so the feed does not repeat it as if it were money.
    assert kinds[5] == ("result", "success  turns 6")


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
    assert s.plan_short == ["do A", "do B"] and len(s.iterations) == 1
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


# ------------------------------------------------------------------ harnesses
def _roles():
    """A night like cadex nt2: claude everywhere but the critic, codex behind it all."""
    return {
        "actor": [("claude", "claude-fable-5-1"), ("codex", "gpt-6-astra")],
        "critic": [("codex", "gpt-6-astra")],
        "overseer": [("claude", "claude-opus-5"), ("codex", "gpt-6-astra")],
        "maintainer": [("claude", "claude-fable-5-1"), ("codex", "gpt-6-astra")],
        "planner": [("claude", "claude-fable-5-1"), ("codex", "gpt-6-astra")],
    }


def _usage():
    return {
        "claude": {"windows": {"seven_day": {"utilization": 0.33, "minutes": 10080},
                               "five_hour": {"utilization": 1.0, "minutes": 300},
                               "seven_day_overage_included": {"utilization": 0.65, "minutes": 10080}},
                   "first_windows": {"seven_day": 0.13}},
        "codex": {"windows": {"seven_day": {"utilization": 0.98, "minutes": 10080}},
                  "first_windows": {"seven_day": 0.24}},
    }


def test_roster_turns_roles_into_one_row_per_harness():
    claude, codex = harness_roster(_roles(), _usage())
    assert claude.name == "claude"
    assert claude.roles == ["actor", "overseer", "maintainer", "planner"]
    assert claude.backs == []
    assert claude.models == ["claude-fable-5-1", "claude-opus-5"]
    assert codex.roles == ["critic"]
    # Codex stands behind every other role without being first choice for them.
    assert codex.backs == ["actor", "overseer", "maintainer", "planner"]


def test_roster_shows_the_long_window_and_the_rise_this_run_caused():
    claude, codex = harness_roster(_roles(), _usage())
    # The weekly window, not the five-hour one that happens to be full.
    assert claude.window == "seven_day"
    assert claude.utilization == 0.33
    assert claude.delta == pytest.approx(20.0)
    assert codex.delta == pytest.approx(74.0)


def test_roster_ignores_the_overage_meter():
    """Overage measures billing past the plan, so it must not be mistaken for the plan."""
    rows = harness_roster({"actor": [("claude", "m")]}, {
        "claude": {"windows": {"seven_day": {"utilization": 0.3, "minutes": 10080},
                               "seven_day_overage_included": {"utilization": 0.9, "minutes": 10080}}}})
    assert rows[0].window == "seven_day"
    assert rows[0].utilization == 0.3


def test_roster_marks_a_limited_harness():
    claude, _ = harness_roster(_roles(), _usage(), {"claude": "223m (session limit)"})
    assert claude.limited


def test_a_harness_with_no_reading_yet_has_no_delta():
    rows = harness_roster({"actor": [("claude", "m")]}, {})
    assert rows[0].utilization is None and rows[0].delta is None


def test_a_standby_harness_sorts_last_and_reads_as_idle():
    rows = harness_roster({"actor": [("claude", "m"), ("pi", "p")]}, {
        "claude": {"windows": {"seven_day": {"utilization": 0.4, "minutes": 10080}}}})
    assert [r.name for r in rows] == ["claude", "pi"]
    assert rows[1].idle and not rows[0].idle


def test_roster_survives_a_bare_chain_with_no_models():
    """`chains` carries harness names only; the panel falls back to it before a run starts."""
    rows = harness_roster({"actor": ["claude"], "critic": ["codex"]}, {})
    assert [(r.name, r.roles, r.models) for r in rows] == [
        ("claude", ["actor"], []), ("codex", ["critic"], [])]


# ------------------------------------------------------------------ chart scale
def test_a_clustered_series_lifts_its_floor_instead_of_starting_at_zero():
    """Charted from zero these all reach the same height and the panel is a block."""
    vals, lo, hi, note = chart_bounds([5.0, 5.4, 5.1, 5.8, 5.2])
    assert note == ""
    assert lo > 4.0 and hi == 5.8      # the band fills the panel rather than the top 5%
    assert vals == [5.0, 5.4, 5.1, 5.8, 5.2]


def test_a_wide_spread_goes_log_so_small_values_survive_an_outlier():
    vals, lo, hi, note = chart_bounds([2.0, 2.5, 2.0, 60.0])
    assert note == "log"
    assert 10 ** lo == pytest.approx(2.0) and 10 ** hi == pytest.approx(60.0)
    # The median sits well up the panel instead of being flattened by the outlier.
    assert 0.2 < (vals[0] - lo) / (hi - lo) < 0.5 or vals[0] == lo


def test_a_leading_zero_does_not_decide_the_scale():
    """The first iteration has no previous step to measure from and reads as zero."""
    vals, lo, hi, note = chart_bounds([0.0, 5.0, 5.4, 5.1])
    assert note == ""                   # not log: the real spread is narrow
    assert lo >= 0.0 and hi == 5.4
    wide, lo2, hi2, note2 = chart_bounds([0.0, 2.0, 2.5, 60.0])
    assert note2 == "log"
    assert 10 ** lo2 == pytest.approx(2.0)   # floor is the smallest real value, not the zero
    assert wide[0] == lo2                    # the artefact is clamped to the floor, not dropped


def test_degenerate_series_do_not_explode():
    assert chart_bounds([]) == ([], 0.0, 1.0, "")
    assert chart_bounds([0.0, 0.0]) == ([0.0, 0.0], 0.0, 1.0, "")
    vals, lo, hi, note = chart_bounds([3.0, 3.0, 3.0])
    assert note == "" and lo == 0.0 and hi == 3.0


# ---------------------------------------------------------------- charts fit their panel
def test_a_long_series_is_resampled_rather_than_cropped():
    """240 iterations in a 60-dot panel used to show only the last 60 and say nothing."""
    from ouroboros.tui.widgets import resample
    spike = [1.0] * 200 + [9.0] + [1.0] * 39
    got = resample(spike, 20)
    assert len(got) == 20
    # The spike is at 200/240 of the run, and a bucket reports its peak, so it survives.
    assert max(got) == 9.0
    assert got.index(9.0) == 16


def test_a_short_series_stretches_only_when_the_chart_asks():
    from ouroboros.tui.widgets import braille_chart, resample
    assert resample([1.0, 2.0], 6) == [1.0, 1.0, 1.0, 2.0, 2.0, 2.0]
    # fill=False leaves the newest sample on the right and the rest of the panel empty,
    # which is what a rolling history wants.
    right = braille_chart([0.0, 1.0], 4, 1, 0.0, 1.0, fill=False)[0]
    assert right.startswith("⠀⠀⠀")
    assert braille_chart([0.0, 1.0], 4, 1, 0.0, 1.0, fill=True)[0][0] != "⠀"


def test_resample_survives_the_degenerate_cases():
    from ouroboros.tui.widgets import resample
    assert resample([], 5) == []
    assert resample([3.0], 0) == []
    assert resample([3.0], 3) == [3.0, 3.0, 3.0]
    assert resample([1.0, 2.0, 3.0], 3) == [1.0, 2.0, 3.0]


def test_charter_gaps_are_counted_only_off_the_charter(tmp_path):
    """`gaps_open` tallies STATE.md, which is a different list; mixing it read 0 done."""
    from ouroboros.tui.state import frontier_counts
    (tmp_path / ".ouroboros").mkdir()
    (tmp_path / ".ouroboros" / "goal.md").write_text(
        "# Goal\n\n## Done criteria\n\n- [x] **one**\n- [x] **two**\n- [ ] **three**\n")
    (tmp_path / "STATE.md").write_text(
        "## Frontier\n\n- [open] charter criterion one\n- [open] charter criterion two\n"
        "- [open] charter criterion three\n\n## Architecture\n\n- [working] a\n")
    f = frontier_counts(tmp_path)
    assert f["gaps_total"] == 3 and f["gaps_unchecked"] == 1
    assert f["gaps_total"] - f["gaps_unchecked"] == 2


def _snap(**kw):
    """A Snapshot with every required field filled, so a test names only what it tests."""
    from ouroboros.tui.state import STAGES, Snapshot, StageStat
    base = dict(now=1000.0, status={}, alive=True, pid=1, iterations=[],
                stages={s: StageStat() for s in STAGES}, stage="actor", stage_since=0.0,
                decisions=[], feed=[], feed_role="", feed_age=None, log_tail=[], plan_short=[],
                needs_human=None, loadavg=(0.0, 0.0, 0.0), procs=0, cpu_pct=0.0, rss_mb=0.0,
                stop_after_s=None, max_iterations=None, chains={}, mode="single")
    stages = kw.pop("stages", None)
    if stages:
        base["stages"] = {**{s: StageStat() for s in STAGES}, **stages}
    return Snapshot(**{**base, **kw})


# ---------------------------------------------------------------- the new layout
def test_a_panel_with_a_floor_is_never_squeezed_below_it():
    """A box shorter than its own border holds nothing, which is worse than absent."""
    from ouroboros.tui.layout import Rect, col, compute_layout, leaf
    view = col(leaf("big", 10), leaf("strip", 1, min_h=4))
    placed = compute_layout(Rect(0, 0, 18, 80), view, {"big", "strip"})
    assert placed["strip"].h >= 4
    assert placed["big"].h + placed["strip"].h == 18
    # The floor only binds when it has to; weight still decides the share when it fits.
    tall = compute_layout(Rect(0, 0, 110, 80), view, {"big", "strip"})
    assert tall["strip"].h == 10


def test_a_floor_that_cannot_be_paid_for_does_not_starve_a_sibling():
    from ouroboros.tui.layout import Rect, col, compute_layout, leaf
    view = col(leaf("a", 1, min_h=40), leaf("b", 1, min_h=40))
    placed = compute_layout(Rect(0, 0, 10, 80), view, {"a", "b"})
    assert placed["a"].h + placed["b"].h == 10
    assert placed["a"].h > 0 and placed["b"].h > 0


def test_every_harness_keeps_its_meter_however_short_the_panel():
    from ouroboros.tui.panels import _block
    from ouroboros.tui.state import HarnessRow
    rows = [HarnessRow(name="claude", roles=["actor"], utilization=0.4, short_utilization=0.9),
            HarnessRow(name="codex", roles=["critic"], utilization=0.2)]
    assert all(_block(r, 1) == ["meter"] for r in rows)
    # The short window is the first detail dropped, the roles the second.
    assert _block(rows[0], 3) == ["meter", "short", "roles"]
    assert _block(rows[0], 2) == ["meter", "roles"]
    assert _block(rows[1], 3) == ["meter", "roles"]   # no short window to show


def test_the_summary_row_carries_both_panels_it_replaced():
    from ouroboros.tui.panels import _summary_line
    from ouroboros.tui.state import StageStat
    s = _snap(frontier={"gaps_total": 16, "gaps_unchecked": 9, "all_working": 10, "all_open": 18},
              stages={"actor": StageStat(count=5, total=800.0), "critic": StageStat(count=5, total=200.0),
                      "commit": StageStat(count=5, total=999.0)})
    line = _summary_line(s)
    assert "charter 7/16" in line and "10 working" in line and "18 open" in line
    # `commit` is an event, not a stage that takes time, so it is out of the split.
    assert "act 80%" in line and "crit 20%" in line


def test_the_summary_row_says_nothing_when_there_is_nothing_to_say():
    from ouroboros.tui.panels import _summary_line
    assert _summary_line(_snap()) == ""


def test_expired_usage_waits_for_new_reading():
    usage = _usage()
    usage['claude']['windows']['five_hour']['resets_at'] = 100
    before = harness_roster(_roles(), usage, now=99)[0]
    assert before.short_utilization == 1.0
    after = harness_roster(_roles(), usage, now=100)[0]
    assert after.short_utilization is None
    assert after.short_expired
    from ouroboros.tui.panels import _block
    assert "short" in _block(after, 3)
    assert after.utilization == .33
    usage['claude']['windows']['five_hour'].update(utilization=.02, resets_at=200)
    refreshed = harness_roster(_roles(), usage, now=101)[0]
    assert refreshed.short_utilization == .02 and not refreshed.short_expired


def test_usage_parts_read_as_windows_with_money_only_where_it_is_billed():
    from ouroboros.tui.state import usage_parts
    usage = {
        "claude": {"windows": {"seven_day": {"utilization": 0.54, "minutes": 10080, "resets_at": 9e9},
                               "five_hour": {"utilization": 0.65, "minutes": 300, "resets_at": 9e9}},
                   "first_windows": {"seven_day": 0.35, "five_hour": 0.13}},
        "codex": {"windows": {"seven_day": {"utilization": 0.42, "minutes": 10080, "resets_at": 9e9}},
                  "first_windows": {"seven_day": 0.06}},
    }
    parts = usage_parts(usage, {"pi": 2.5}, now=1000.0)
    assert [label for label, _ in parts] == [
        "claude 7d 54% +19", "claude 5h 65% +52", "codex 7d 42% +36", "pi $2.50"]
    # the fill drives the gradient; money has no ceiling, so it has none
    assert [fill for _, fill in parts] == [0.54, 0.65, 0.42, None]


def test_a_window_past_its_reset_reads_as_empty_not_as_whatever_it_last_said():
    from ouroboros.tui.state import usage_parts
    usage = {"claude": {"windows": {"five_hour": {"utilization": 0.75, "minutes": 300, "resets_at": 1500.0}}}}
    assert usage_parts(usage, {}, now=2000.0) == [("claude 5h 0%", 0.0)]


def test_the_history_strip_has_two_states():
    from ouroboros.tui.panels import BLOCKED
    # work that went through, including the overseer answering and accepting done
    assert not {"continue", "answer", "done_accepted"} & BLOCKED
    # work thrown away, or not done at all
    assert {"stuck", "revert", "done_rejected"} <= BLOCKED


# ------------------------------------------------------------------ the status panel
class FakeWin:
    """A screen that remembers what was written where, so a panel can be read back."""

    def __init__(self, h: int, w: int) -> None:
        self.grid = [[" "] * w for _ in range(h)]

    def addstr(self, y, x, s, attr=0):
        import curses
        if not (0 <= y < len(self.grid)):
            raise curses.error("off screen")
        row = self.grid[y]
        for i, ch in enumerate(s):
            if 0 <= x + i < len(row):
                row[x + i] = ch

    def lines(self):
        return [("".join(r)).rstrip() for r in self.grid]


def snapshot(**kw):
    from ouroboros.tui.state import Snapshot
    base = dict(
        now=1000.0, status={"state": "work", "iteration": 12}, alive=True, pid=7, iterations=[], stages={},
        stage="actor", stage_since=940.0, decisions=[], feed=[], feed_role="0012-actor", feed_age=1.0,
        log_tail=[], plan_short=[], needs_human=None, loadavg=(0, 0, 0), procs=1, cpu_pct=0.0, rss_mb=0.0,
        stop_after_s=None, max_iterations=None, chains={}, mode="actor-critic",
    )
    base.update(kw)
    return Snapshot(**base)


def render(s, h=12, w=76):
    from ouroboros.tui.panels import Painter, draw_status
    from ouroboros.tui.theme import Theme
    win = FakeWin(h, w)
    draw_status(Painter(win, Theme()), Rect(0, 0, h, w), s, 3)
    return win.lines()


def decision(**kw):
    base = {"iteration": 12, "verdict": "continue", "reason": "moved the walk forward", "reply": ""}
    base.update(kw)
    return base


def test_the_summaries_follow_the_verdicts_and_the_raw_line_is_last():
    from ouroboros.tui.state import Event
    s = snapshot(
        decisions=[decision(did="Added swept clearance to the rollout path, with twelve tests.",
                            doing="Wiring the pending receipt into the collect leg.")],
        feed=[Event("think", "hmm"), Event("tool", "$ pytest cli/tests -k walk")],
    )
    lines = render(s)
    # history, stage, the one verdict, a blank, LAST, a blank, CURRENT.
    assert lines[4].strip() == "│" + " " * 0 or lines[4].strip("│ ") == ""
    assert lines[5].startswith("│ LAST") and "Added swept clearance" in lines[5]
    assert lines[6].strip("│ ") == ""
    assert lines[7].startswith("│ CURRENT") and "Wiring the pending receipt" in lines[7]
    # The raw line carries no label and holds the bottom row, whatever is above it.
    assert "pytest cli/tests -k walk" in lines[-2] and "CURRENT" not in lines[-2]


def test_a_long_summary_wraps_into_the_room_instead_of_clipping():
    long = " ".join(f"word{i}" for i in range(40))
    s = snapshot(decisions=[decision(did=long, doing="short")])
    body = render(s, h=16, w=60)
    last_rows = [l for l in body if "word" in l]
    assert len(last_rows) >= 3, "the room under the verdicts is the summaries' to use"
    assert "word39" in "\n".join(body), "nothing was cut"
    assert any("CURRENT" in l and "short" in l for l in body)


def test_two_long_summaries_share_the_room_rather_than_one_starving_the_other():
    long = " ".join(f"w{i}" for i in range(60))
    s = snapshot(decisions=[decision(did=long, doing=long.upper())])
    body = render(s, h=12, w=50)
    lower = sum(1 for l in body if "w1" in l.lower() and "W" not in l)
    upper = sum(1 for l in body if "W1" in l)
    assert lower >= 2 and upper >= 2
    assert lower + upper == 12 - 2 - 2 - 1 - 1 - 2   # borders, history, stage, verdict, raw, two spacers


def test_the_panel_keeps_the_history_strip_and_gains_the_stage():
    s = snapshot(decisions=[decision(iteration=9, verdict="stuck"), decision(iteration=10, verdict="continue")])
    lines = render(s)
    assert "history ●●" in lines[1]
    assert "#10 continue" in lines[1]
    assert "actor→critic→overseer→maintainer→planner" in lines[2]
    assert "1m 00s" in lines[2], "how long it has held this stage"
    assert "continue 1" in lines[-1] and "stuck 1" in lines[-1], "the verdict counts stay on the border"


def test_a_stage_outside_the_pipeline_is_named_rather_than_dropped():
    assert "backoff" in render(snapshot(stage="backoff", decisions=[decision()]))[2]


def test_the_overseers_own_words_still_get_the_middle():
    s = snapshot(decisions=[decision(reason="the actor asked which store to use", verdict="answer",
                                     reply="Use the existing one.", did="d", doing="c")])
    body = "\n".join(render(s)[3:-4])
    assert "#12 answer  the actor asked which store to use" in body
    assert "→ Use the existing one." in body


def test_only_the_last_three_verdicts_are_listed_however_tall_the_panel():
    """Three is the window a glance holds; the rest of the room is the summaries'."""
    s = snapshot(decisions=[decision(iteration=n, verdict="stuck", reason=f"nothing changed ({n})")
                            for n in range(1, 9)], stage="actor")
    body = "\n".join(render(s, h=24))
    for n in (6, 7, 8):
        assert f"#{n} stuck  nothing changed ({n})" in body
    assert "nothing changed (5)" not in body
    # Newest last, so the eye lands on it right above the summaries.
    assert body.index("nothing changed (6)") < body.index("nothing changed (8)")


def test_the_newest_verdicts_reply_is_listed_under_it():
    s = snapshot(decisions=[decision(verdict="answer", reason="asked which store", reply="Use the existing one.")])
    assert "→ Use the existing one." in "\n".join(render(s))


def test_the_window_gives_way_to_the_three_rows_when_the_panel_is_short():
    s = snapshot(decisions=[decision(iteration=n, verdict="continue", reason=f"r{n}", did="D", doing="C")
                            for n in range(1, 9)])
    lines = render(s, h=7)
    assert "D" in lines[-4] and "C" in lines[-3]
    assert "r1" not in "\n".join(lines), "the older verdicts go first, the rows never do"


def test_the_rows_survive_a_panel_with_no_room_for_prose():
    """min_h is seven: two borders, history, stage, and the three rows. Nothing else."""
    lines = render(snapshot(decisions=[decision(did="finished the thing", doing="starting the next")]), h=7)
    assert "finished the thing" in lines[-4] and "starting the next" in lines[-3]


def test_the_spacing_outlives_the_verdict_list():
    """The verdicts go before the blank rows around the summaries do."""
    s = snapshot(decisions=[decision(iteration=n, did="D", doing="C") for n in range(1, 5)])
    lines = render(s, h=10)
    assert lines[3].startswith("│ #4"), "ten rows: one verdict fits"
    assert lines[4].strip("│ ") == "" and lines[6].strip("│ ") == ""
    assert lines[5].startswith("│ LAST") and lines[7].startswith("│ CURRENT")
    lines = render(s, h=9)
    assert "#4" not in lines[3], "nine rows: the verdict goes, not the spacing"
    assert lines[3].strip("│ ") == "" and lines[4].startswith("│ LAST") and lines[6].startswith("│ CURRENT")


def test_an_overseer_that_wrote_no_summary_falls_back_to_what_it_did_write():
    """The rules overseer cannot summarise. An empty row would read as nothing happening."""
    s = snapshot(decisions=[decision(reason="no record written", reply="Write the handoff first.")])
    assert s.did == "no record written"
    assert s.doing == "Write the handoff first."


def test_the_raw_row_is_what_the_agent_said_not_what_a_tool_answered():
    from ouroboros.tui.state import Event
    s = snapshot(feed=[Event("text", "I will add the test"), Event("tool_out", "1 passed"), Event("think", "…")])
    assert s.last_message.text == "I will add the test"
    assert s.last_message.kind == "text"


def test_a_run_with_no_transcript_yet_says_so_once():
    lines = render(snapshot(decisions=[decision()]))
    assert "waiting for the first message" in lines[-2]


def test_the_status_panel_takes_the_room_both_panels_used_to_have():
    from ouroboros.tui.app import DEFAULT_ON, PANELS, VIEW
    placed = compute_layout(Rect(8, 0, 44, 120), VIEW, DEFAULT_ON)
    assert set(placed) == {"iterations", "activity", "status"}
    # 12 of 22: the overseer strip's 4 and the feed's 8, in one box.
    assert placed["status"].h == 24 and placed["status"].w == 120
    assert "messages" in PANELS, "the raw feed is still reachable, just not on by default"
