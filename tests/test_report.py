"""The morning report must not flatter the run."""

from ouroboros.cli import _cost_by_role, _cost_split, _unverified_reverts
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


def test_cost_split_reads_largest_first():
    assert _cost_split({"planner": 1.0, "actor": 9.0}) == "actor $9.00, planner $1.00"
    assert _cost_split({}) == "no harness reported a cost"


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
    assert _unverified_reverts(repo, "ouroboros/x", [noop]) == [noop]

    real = g.revert_to("ok")
    good = {"iteration": 8, "to": "ok", "sha": real[:10]}
    assert _unverified_reverts(repo, "ouroboros/x", [good]) == []


def test_a_revert_recorded_with_an_error_is_always_failed(repo):
    r = {"iteration": 9, "to": "ok", "sha": "deadbeef", "error": "revert refused"}
    assert _unverified_reverts(repo, "ouroboros/x", [r]) == [r]


def test_unverifiable_reverts_are_not_guessed_at(repo):
    """A tag that no longer exists is not evidence of failure."""
    assert _unverified_reverts(repo, "ouroboros/x", [{"iteration": 1, "to": "gone", "sha": "gone"}]) == []
    assert _unverified_reverts(repo, "ouroboros/x", [{"iteration": 2}]) == []
