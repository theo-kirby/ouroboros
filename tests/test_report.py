"""The morning report must not flatter the run."""

import json

from ouroboros.cli import main
from ouroboros.cli import _billed_split, _cost_by_role
from ouroboros.history import unverified_reverts
from ouroboros.gitguard import GitGuard


def test_cost_covers_every_role_not_just_the_actor():
    steps = [
        {"step": "commit", "cost": 2.0},
        {"step": "commit", "cost": 1.0},
        {"step": "reconcile", "cost": 0.5},
        {"step": "plan", "cost": 0.25},
        {"step": "revert"},  # not a role call; must not count as uncosted
    ]
    decisions = [{"cost": 0.1}, {"cost": 0.4}]
    by_role, uncosted = _cost_by_role(steps, decisions)
    assert by_role == {"actor": 3.0, "maintainer": 0.5, "planner": 0.25, "overseer": 0.5}
    assert sum(by_role.values()) == 4.25
    assert uncosted == 0


def test_role_calls_that_report_no_cost_are_counted_not_hidden():
    """A subscription harness reports nothing; the report must say so, not imply $0."""
    steps = [{"step": "commit", "cost": 1.0}, {"step": "commit"}, {"step": "critique"},
             {"step": "plan", "error": "harness died"}]
    by_role, uncosted = _cost_by_role(steps, [])
    assert by_role == {"actor": 1.0}
    assert uncosted == 2  # the silent commit and critique; the errored plan is not a spend


def test_billed_split_reads_largest_first_and_only_when_money_was_billed():
    billed = {"pi": 10.0}
    assert _billed_split({"planner": 1.0, "actor": 9.0}, billed) == "actor $9.00, planner $1.00"
    # A subscription's api-equivalent dollars are not money; with nothing really
    # billed the split is empty however much the harness claimed per call.
    assert _billed_split({"planner": 1.0, "actor": 9.0}, {}) == ""
    assert _billed_split({}, billed) == ""


def test_a_revert_that_never_moved_the_tree_is_reported_as_failed(repo):
    g = GitGuard(repo, "ouroboros/x")
    g.start()
    (repo / "a.txt").write_text("1\n")
    g.commit("one")
    g.tag("ok")
    (repo / "a.txt").write_text("2\n")
    bad = g.commit("two")

    # What a silent no-op logs: the head it returned is the commit it should have undone.
    noop = {"iteration": 7, "to": "ok", "sha": bad[:10]}
    assert unverified_reverts(repo, "ouroboros/x", [noop]) == [noop]

    real = g.revert_to("ok")
    good = {"iteration": 8, "to": "ok", "sha": real[:10]}
    assert unverified_reverts(repo, "ouroboros/x", [good]) == []


def test_a_revert_recorded_with_an_error_is_always_failed(repo):
    r = {"iteration": 9, "to": "ok", "sha": "deadbeef", "error": "revert refused"}
    assert unverified_reverts(repo, "ouroboros/x", [r]) == [r]


def test_unverifiable_reverts_are_not_guessed_at(repo):
    """A tag that no longer exists is not evidence of failure."""
    assert unverified_reverts(repo, "ouroboros/x", [{"iteration": 1, "to": "gone", "sha": "gone"}]) == []
    assert unverified_reverts(repo, "ouroboros/x", [{"iteration": 2}]) == []


def _run_dir(repo, run="r1", **status):
    d = repo / ".ouroboros" / "runs" / run
    d.mkdir(parents=True, exist_ok=True)
    (d / "status.json").write_text(json.dumps({"state": "killed", "run": run, **status}))
    (d / "iterations.jsonl").write_text("\n".join(json.dumps(s) for s in [
        {"iteration": 1, "step": "commit", "changed": True, "recorded": True},
        {"iteration": 1, "step": "critique"},
        {"iteration": 2, "step": "commit", "changed": True, "recorded": True, "cost": 1.5},
    ]) + "\n")
    return d


def test_report_shows_the_windows_a_subscription_run_consumed(repo, monkeypatch, capsys):
    _run_dir(repo, usage_lines=["claude seven_day 13% -> 33% (+20 this run)",
                                "codex seven_day 24% -> 98% (+74 this run)"])
    monkeypatch.chdir(repo)
    assert main(["report", "--run", "r1"]) == 0
    out = capsys.readouterr().out
    assert "- usage: claude seven_day 13% -> 33% (+20 this run)" in out
    assert "         codex seven_day 24% -> 98% (+74 this run)" in out
    # The window is the cost. A subscription bills a flat fee, so no dollar figure
    # derived from token counts belongs in the report at all.
    assert "$" not in out
    assert "api-equivalent" not in out and "no meter" not in out


def test_report_still_warns_when_nothing_meters_the_run_at_all(repo, monkeypatch, capsys):
    _run_dir(repo)  # no usage_lines: no harness reported windows either
    monkeypatch.chdir(repo)
    assert main(["report", "--run", "r1"]) == 0
    out = capsys.readouterr().out
    assert "- usage:" not in out
    assert "this run has no meter" in out
    assert "$" not in out
