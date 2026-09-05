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


def test_empty_commit_allowed(repo):
    g = GitGuard(repo, "ouroboros/x")
    g.start()
    before = g.head()
    assert g.commit("nothing") != before
