"""The operator guide: what an agent opened in the target repo can find out.

The failure this prevents is quiet. A guide that no harness loads, or one that
`init` overwrites on its second run, reads as fine and teaches nobody.
"""

from pathlib import Path

from ouroboros import operator
from ouroboros.cli import main


def test_the_guide_names_the_project_and_the_commands_it_will_need(repo):
    path = operator.write_guide(repo, "cadex")
    text = path.read_text()
    assert path == repo / ".ouroboros" / "AGENTS.md"
    assert "# Operating Ouroboros in cadex" in text
    for command in ("ouroboros run", "ouroboros status", "ouroboros stop", "ouroboros archive"):
        assert command in text
    for skill in ("ouroboros-design", "ouroboros-checkup"):
        assert skill in text
    assert "never squash" in text


def test_an_existing_guide_is_never_overwritten(repo):
    operator.write_guide(repo, "cadex")
    (repo / ".ouroboros" / "AGENTS.md").write_text("# mine\n\nhand-written.\n")
    assert operator.write_guide(repo, "cadex") is None
    assert (repo / ".ouroboros" / "AGENTS.md").read_text() == "# mine\n\nhand-written.\n"


def test_every_root_contract_that_exists_gets_the_pointer(repo):
    (repo / "AGENTS.md").write_text("# proj\n\nrules.\n")
    (repo / "CLAUDE.md").write_text("# proj\n\nsame rules.\n")
    touched = operator.add_pointer(repo)

    assert sorted(p.name for p in touched) == ["AGENTS.md", "CLAUDE.md"]
    for name in ("AGENTS.md", "CLAUDE.md"):
        text = (repo / name).read_text()
        assert "rules." in text, "the repo's own contract must survive"
        assert ".ouroboros/AGENTS.md" in text and ".ouroboros/RUNS.md" in text


def test_a_repo_with_no_contract_gets_one(repo):
    for name in ("AGENTS.md", "CLAUDE.md"):
        assert not (repo / name).exists()
    assert operator.add_pointer(repo) == [repo / "AGENTS.md"]
    assert ".ouroboros/AGENTS.md" in (repo / "AGENTS.md").read_text()


def test_pointing_twice_does_not_append_twice(repo):
    (repo / "AGENTS.md").write_text("# proj\n")
    operator.add_pointer(repo)
    once = (repo / "AGENTS.md").read_text()
    assert operator.add_pointer(repo) == []
    assert (repo / "AGENTS.md").read_text() == once


def test_init_writes_the_guide_and_points_the_contract_at_it(repo, monkeypatch, capsys):
    monkeypatch.chdir(repo)
    (repo / "CLAUDE.md").write_text("# proj\n")
    assert main(["init", "--name", "cadex"]) == 0
    out = capsys.readouterr().out

    assert (repo / ".ouroboros" / "AGENTS.md").exists()
    assert "pointed CLAUDE.md at it" in out
    assert ".ouroboros/AGENTS.md" in (repo / "CLAUDE.md").read_text()


def test_init_is_safe_to_run_again(repo, monkeypatch, capsys):
    """A second `init` must not flatten a filled-in guide or a filled-in charter."""
    monkeypatch.chdir(repo)
    main(["init", "--name", "cadex"])
    guide = repo / ".ouroboros" / "AGENTS.md"
    guide.write_text(guide.read_text() + "\n- **Where runs happen:** the 5090 box.\n")

    assert main(["init", "--name", "cadex"]) == 0
    assert "the 5090 box." in guide.read_text()
    assert (repo / "AGENTS.md").read_text().count(operator.POINTER_MARK) == 1
