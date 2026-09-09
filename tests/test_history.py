"""The durable record: what survives a run after its logs are gone.

The cases here are the ones that lost cadex's first four runs: a run killed
mid-iteration, a machine that is not the one reading the digest later, and a
branch that gets merged long after the run ended.
"""

import json
from pathlib import Path

import pytest

from ouroboros import history
from ouroboros.cli import main
from ouroboros.config import Config
from ouroboros.gitguard import GitGuard

from conftest import git


def run_dir(repo: Path, name: str, *, steps: list[dict], decisions: list[dict] = (), status: dict = None) -> Path:
    d = repo / ".ouroboros" / "runs" / name
    (d / "transcripts").mkdir(parents=True, exist_ok=True)
    (d / "iterations.jsonl").write_text("".join(json.dumps(s) + "\n" for s in steps))
    (d / "overseer.jsonl").write_text("".join(json.dumps(s) + "\n" for s in decisions))
    (d / "run.yml").write_text("run: x\nstarted: 2026-09-08T17:30:00\n")
    (d / "status.json").write_text(json.dumps({
        "ts": "2026-09-09T06:47:49+00:00", "state": "killed", "elapsed_s": 67138,
        "branch": f"ouroboros/{name}", "memory": "hypergraph",
        "usage_lines": ["claude seven_day 58% -> 1%"], **(status or {}),
    }))
    return d


def branch_with(repo: Path, name: str, commits: list[tuple[str, str]]) -> None:
    """A run branch carrying one commit per (subject, path) pair.

    The path is what decides whether a commit landed anything: a commit under
    `.hypergraph/` is the loop talking to itself, whatever its subject says.
    """
    g = GitGuard(repo, f"ouroboros/{name}")
    g.start()
    for i, (subject, path) in enumerate(commits):
        f = repo / path
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(f"{i}\n")
        g.commit(subject)


# -- what the digest measures -----------------------------------------------

def test_a_killed_run_still_accounts_for_itself(repo):
    """ot4 was killed mid-iteration and produced no report at all. It must produce one."""
    d = run_dir(repo, "ot4", steps=[
        {"iteration": 1, "step": "commit", "changed": True, "recorded": True},
        {"iteration": 2, "step": "commit", "changed": True, "recorded": False},
        {"iteration": 2, "step": "plan", "bet": "close the walk criterion"},
    ], decisions=[{"iteration": 1, "verdict": "continue"}, {"iteration": 2, "verdict": "looping", "reason": "circling"}])
    cfg = Config(run="ot4")
    facts = history.gather(repo, cfg, d, machine="sb1x")

    assert facts.iterations == 2 and facts.changed == 2 and facts.recorded == 1
    assert facts.state == "killed" and facts.machine == "sb1x"
    assert facts.hours == 18.6
    assert facts.verdicts == {"continue": 1, "looping": 1}
    assert facts.started == "2026-09-08T17:30:00"


def test_the_run_is_measured_against_the_branch_not_the_working_tree(repo):
    branch_with(repo, "ot4", [
        ("A real change", "cli/main.py"),
        # nt3's record commits carry no prefix to match on, so the files decide.
        ("Record the blocked transition", ".hypergraph/graph/record/witty-brook-9419.md"),
        ("ouroboros #201: reconcile", "STATE.md"),
    ])
    d = run_dir(repo, "ot4", steps=[])
    facts = history.gather(repo, Config(run="ot4"), d)
    assert facts.commits == 3
    assert facts.landed == ["A real change"]
    assert "3 files changed" in facts.diffstat


def criteria(repo: Path, *boxes: str) -> None:
    (repo / ".ouroboros" / "goal.md").write_text(
        "# Goal\n\n## Mission\n\ngo.\n\n## Done criteria\n\n"
        + "".join(f"- {b}\n" for b in boxes))


def test_what_the_run_ticked_is_not_what_the_charter_opened_with(repo):
    """nt3's tip says 9 of 13 closed. nt3 closed none of them: they were shipped before it."""
    criteria(repo, "[x] the walk runs end to end", "[ ] the box is proven", "[ ] a biped trains")
    git(repo, "commit", "-qam", "charter")
    branch_with(repo, "ot4", [("work", "cli/main.py")])
    d = run_dir(repo, "ot4", steps=[])

    facts = history.gather(repo, Config(run="ot4"), d)
    assert (facts.criteria_closed, facts.criteria_total) == (1, 3)
    assert facts.criteria_ticked == 0, "the run closed nothing, however full the charter looks"
    assert "this run ticked 0" in history.digest(facts)

    criteria(repo, "[x] the walk runs end to end", "[x] the box is proven", "[ ] a biped trains")
    GitGuard(repo, "ouroboros/ot4").commit("prove the box")
    assert history.gather(repo, Config(run="ot4"), d).criteria_ticked == 1


def test_an_unmeasurable_tick_count_reads_as_unknown_rather_than_zero(repo):
    """No merge base means no before. `?` and `0` are different claims."""
    criteria(repo, "[ ] the box is proven")
    git(repo, "commit", "-qam", "charter")
    d = run_dir(repo, "orphan", steps=[])
    facts = history.gather(repo, Config(run="orphan"), d)
    assert facts.criteria_ticked is None and facts.ticked == "?"
    assert "this run ticked ?" in history.digest(facts)


def test_a_merged_branch_reports_the_merge_commit_and_what_it_brought(repo):
    """`merged:` is a fact about the base branch, so it changes without the run changing."""
    branch_with(repo, "ot4", [("A real change", "cli/main.py"),
                              ("Record it", ".hypergraph/graph/record/a.md")])
    d = run_dir(repo, "ot4", steps=[])
    assert history.gather(repo, Config(run="ot4"), d).merged == ""

    git(repo, "checkout", "-q", "main")
    git(repo, "merge", "--no-ff", "-q", "-m", "Merge run ot4", "ouroboros/ot4")
    facts = history.gather(repo, Config(run="ot4"), d)
    assert facts.merged == git(repo, "rev-parse", "HEAD")
    assert facts.commits == 2 and facts.landed == ["A real change"]


def test_loop_detector_firings_are_summarised_not_listed(repo):
    d = run_dir(repo, "ot4", steps=[
        {"iteration": 50, "step": "loop", "signal": "no_product", "streak": 8},
        {"iteration": 55, "step": "loop", "signal": "no_product", "streak": 13},
        {"iteration": 74, "step": "loop", "signal": "no_frontier", "streak": 30},
    ])
    facts = history.gather(repo, Config(run="ot4"), d)
    assert facts.loop_signals == {"no_product": 2, "no_frontier": 1}
    assert facts.longest_loop == 30
    assert "no_product ×2" in history.digest(facts) and "longest streak 30" in history.digest(facts)


def test_a_run_with_no_git_branch_left_is_still_digestible(repo):
    """Digesting must never fail on a branch someone deleted after merging."""
    d = run_dir(repo, "gone", steps=[{"iteration": 1, "step": "commit", "changed": True}])
    facts = history.gather(repo, Config(run="gone"), d)
    assert facts.commits == 0 and facts.iterations == 1
    assert "# Run gone" in history.digest(facts)


# -- the notes a human adds survive -----------------------------------------

def test_a_rewrite_keeps_everything_below_the_notes_marker(repo):
    d = run_dir(repo, "ot4", steps=[{"iteration": 1, "step": "commit", "changed": True}])
    facts = history.gather(repo, Config(run="ot4"), d)
    path = history.write_digest(repo, facts)
    assert "(unwritten)" in path.read_text()

    path.write_text(path.read_text().replace(
        "## What this taught\n\n(unwritten)",
        "## What this taught\n\nThe charter criteria are too big to ever tick."))

    facts.iterations = 57                       # the run went on; the digest is rewritten
    history.write_digest(repo, facts)
    text = path.read_text()
    assert "The charter criteria are too big to ever tick." in text
    assert "| iterations | 57" in text
    assert text.count(history.NOTES_MARKER) == 1


# -- the index --------------------------------------------------------------

def test_the_index_is_generated_from_every_digests_front_matter(repo):
    for name, started, iters in (("nt1", "2026-09-05T22:00:00", 90), ("ot4", "2026-09-08T17:30:00", 57)):
        d = run_dir(repo, name, steps=[], status={"branch": f"ouroboros/{name}"})
        (d / "run.yml").write_text(f"run: {name}\nstarted: {started}\n")
        facts = history.gather(repo, Config(run=name), d, machine="sb1x")
        facts.iterations = iters
        history.write_digest(repo, facts)
    path = history.write_index(repo)
    text = path.read_text()

    assert path == repo / ".ouroboros" / "RUNS.md"
    # Oldest first: the index is read as a history, not as a feed.
    assert text.index("nt1") < text.index("ot4")
    assert "| 90 |" in text and "| 57 |" in text
    assert "[ot4](history/ot4.md)" in text
    assert "do not hand-edit" in text


def test_the_index_survives_a_digest_without_front_matter(repo):
    (repo / ".ouroboros" / "history").mkdir(parents=True)
    (repo / ".ouroboros" / "history" / "notes.md").write_text("just some prose\n")
    assert "(no runs yet)" in history.write_index(repo).read_text()


def test_the_record_is_not_gitignored(repo):
    """The run directory must stay ignored and the digest must not."""
    d = run_dir(repo, "ot4", steps=[])
    digest, index = history.archive(repo, Config(run="ot4"), d)
    ignored = git(repo, "status", "--porcelain", "--untracked-files=all")
    assert "history/ot4.md" in ignored and "RUNS.md" in ignored
    assert ".ouroboros/runs" not in ignored


# -- the command ------------------------------------------------------------

def test_archive_all_backfills_every_run_directory(repo, monkeypatch, capsys):
    monkeypatch.chdir(repo)
    for name in ("nt1", "nt2"):
        run_dir(repo, name, steps=[{"iteration": 1, "step": "commit", "changed": True}])
    (repo / ".ouroboros" / "config.yml").write_text(Config(run="nt2").dump())

    assert main(["archive", "--all", "--machine", "theos-mac"]) == 0
    out = capsys.readouterr().out
    assert "nt1: 1 iterations" in out and "nt2: 1 iterations" in out
    assert (repo / ".ouroboros" / "history" / "nt1.md").exists()
    assert "theos-mac" in (repo / ".ouroboros" / "history" / "nt1.md").read_text()


def test_archive_refuses_a_run_that_never_ran(repo, monkeypatch, capsys):
    monkeypatch.chdir(repo)
    (repo / ".ouroboros" / "config.yml").write_text(Config(run="never").dump())
    assert main(["archive"]) == 1
    assert "no run directory" in capsys.readouterr().err


def test_stopping_a_run_that_is_already_dead_still_writes_its_record(repo, monkeypatch, capsys):
    """The gap that lost ot4: it was stopped, and nothing durable was written."""
    monkeypatch.chdir(repo)
    run_dir(repo, "ot4", steps=[{"iteration": 1, "step": "commit", "changed": True}])
    (repo / ".ouroboros" / "config.yml").write_text(Config(run="ot4").dump())
    assert main(["stop"]) == 0
    assert (repo / ".ouroboros" / "history" / "ot4.md").exists()
    assert "commit them" in capsys.readouterr().out


def test_an_archive_failure_never_takes_the_run_down_with_it(repo, monkeypatch, capsys):
    """A digest is a convenience. Losing one must not turn a finished run into an error."""
    monkeypatch.chdir(repo)
    from ouroboros import cli
    monkeypatch.setattr(cli, "archive", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    run_dir(repo, "ot4", steps=[])
    (repo / ".ouroboros" / "config.yml").write_text(Config(run="ot4").dump())
    assert main(["stop"]) == 0
    assert "could not archive" in capsys.readouterr().err
