import pytest

from ouroboros.gitguard import GitError, GitGuard

from conftest import git


def test_start_creates_branch_and_refuses_dirty(repo):
    g = GitGuard(repo, "ouroboros/x")
    (repo / "README.md").write_text("dirty\n")
    with pytest.raises(GitError):
        g.start()
    g.start(allow_dirty=True)
    assert g.current_branch() == "ouroboros/x"


def test_commit_tag_revert(repo):
    g = GitGuard(repo, "ouroboros/x")
    g.start()
    (repo / "a.txt").write_text("1\n")
    g.commit("one")
    g.tag("ok")
    (repo / "a.txt").write_text("2\n")
    (repo / "b.txt").write_text("new\n")
    g.commit("two")
    patch = repo / ".ouroboros" / "runs" / "x" / "reverted" / "0001.patch"
    g.revert_to("ok", patch_out=patch)
    assert (repo / "a.txt").read_text() == "1\n"
    assert not (repo / "b.txt").exists()
    assert "b.txt" in patch.read_text()
    assert len(git(repo, "log", "--oneline").splitlines()) == 4  # init, one, two, revert
    assert git(repo, "diff", "ok", "HEAD", "--stat") == ""


def test_empty_commit_skipped_by_default(repo):
    g = GitGuard(repo, "ouroboros/x")
    g.start()
    before = g.head()
    assert g.commit("nothing") == before


def test_revert_survives_a_stale_sequencer(repo):
    """A leftover .git/sequencer must not turn the revert into a silent no-op."""
    g = GitGuard(repo, "ouroboros/x")
    g.start()
    (repo / "a.txt").write_text("1\n")
    g.commit("one")
    g.tag("ok")
    (repo / "a.txt").write_text("2\n")
    g.commit("two")

    # Exactly what an abandoned revert in an earlier run leaves behind.
    seq = repo / ".git" / "sequencer"
    seq.mkdir()
    (seq / "todo").write_text("revert deadbeef stale\n")
    (seq / "head").write_text(g.head() + "\n")

    g.revert_to("ok")
    assert (repo / "a.txt").read_text() == "1\n"
    assert git(repo, "diff", "ok", "HEAD", "--stat") == ""
    assert not g.sequencer_in_progress()


def test_revert_keeps_untracked_run_state_when_it_falls_back(repo):
    """The fallback must not sweep run artifacts into the commit or delete them."""
    g = GitGuard(repo, "ouroboros/x")
    g.start()
    (repo / "a.txt").write_text("1\n")
    g.commit("one")
    g.tag("ok")
    (repo / "a.txt").write_text("2\n")
    g.commit("two")

    (repo / ".git" / "sequencer").mkdir()  # force the fallback path
    runs = repo / ".ouroboros" / "runs" / "x"
    runs.mkdir(parents=True)
    (runs / "loop.log").write_text("night\n")

    g.revert_to("ok")
    assert (runs / "loop.log").read_text() == "night\n"
    assert git(repo, "diff", "ok", "HEAD", "--stat") == ""
    assert "loop.log" not in git(repo, "show", "--name-only", "HEAD")


def test_revert_raises_rather_than_reporting_a_false_success(repo):
    """If every recovery fails, the guard must say so instead of returning HEAD."""

    class Deaf(GitGuard):
        def git(self, *args, check=True):
            if args[:1] == ("read-tree",):
                return ""  # the wholesale reset silently does nothing
            return super().git(*args, check=check)

        def git_code(self, *args):
            if args[:1] == ("revert",):
                return 1  # git refuses every revert
            return super().git_code(*args)

    g = Deaf(repo, "ouroboros/x")
    g.start()
    (repo / "a.txt").write_text("1\n")
    g.commit("one")
    g.tag("ok")
    (repo / "a.txt").write_text("2\n")
    g.commit("two")

    with pytest.raises(GitError):
        g.revert_to("ok")
