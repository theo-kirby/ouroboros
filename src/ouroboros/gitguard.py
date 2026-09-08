"""Git guard: branch per run, commit per iteration, tag per accepted, revert on reject."""

from __future__ import annotations

import subprocess
from pathlib import Path


class GitError(RuntimeError):
    pass


class GitGuard:
    def __init__(self, repo: Path, branch: str) -> None:
        self.repo = repo
        self.branch = branch

    # -- plumbing --------------------------------------------------------
    def git(self, *args: str, check: bool = True) -> str:
        proc = subprocess.run(
            ["git", *args], cwd=str(self.repo), capture_output=True, text=True
        )
        if check and proc.returncode != 0:
            raise GitError(f"git {' '.join(args)}: {proc.stderr.strip() or proc.stdout.strip()}")
        return proc.stdout.strip()

    def git_code(self, *args: str) -> int:
        """Run git for its exit status. Use where failure is a branch, not an error."""
        return subprocess.run(["git", *args], cwd=str(self.repo), capture_output=True).returncode

    def is_repo(self) -> bool:
        return subprocess.run(["git", "rev-parse", "--git-dir"], cwd=str(self.repo), capture_output=True).returncode == 0

    def head(self) -> str:
        return self.git("rev-parse", "HEAD")

    def current_branch(self) -> str:
        return self.git("rev-parse", "--abbrev-ref", "HEAD")

    def is_dirty(self) -> bool:
        return bool(self.git("status", "--porcelain"))

    def has_changes(self) -> bool:
        return self.is_dirty()

    def _git_path(self, name: str) -> Path:
        """Resolve a path inside the git dir, which is not always `<repo>/.git`."""
        return self.repo / self.git("rev-parse", "--git-path", name)

    def sequencer_in_progress(self) -> bool:
        """True while git holds revert/cherry-pick state, finished or abandoned."""
        return any(self._git_path(n).exists() for n in ("sequencer", "REVERT_HEAD", "CHERRY_PICK_HEAD"))

    def clear_sequencer(self) -> bool:
        """Drop leftover revert/cherry-pick state so a fresh revert can start.

        A `.git/sequencer` left by an abandoned revert -- including one from an
        earlier run -- makes git refuse every later revert outright.
        """
        if not self.sequencer_in_progress():
            return False
        self.git_code("revert", "--quit")
        if self.sequencer_in_progress():
            self.git_code("cherry-pick", "--quit")
        return True

    def diff_stat(self, a: str, b: str = "HEAD") -> str:
        return self.git("diff", "--stat", a, b, check=False)

    def changed_files(self, a: str, b: str = "HEAD") -> list[str]:
        """Repo-relative paths touched between two commits. Empty when either ref is unknown."""
        out = self.git("diff", "--name-only", a, b, check=False)
        return [line.strip() for line in out.splitlines() if line.strip()]

    def branch_exists(self, name: str) -> bool:
        return subprocess.run(["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{name}"], cwd=str(self.repo), capture_output=True).returncode == 0

    # -- lifecycle -------------------------------------------------------
    def start(self, *, allow_dirty: bool = False) -> None:
        if not self.is_repo():
            raise GitError(f"{self.repo} is not a git repository")
        try:
            self.head()
        except GitError:
            raise GitError("repository has no commits yet; make an initial commit first") from None
        if self.is_dirty() and not allow_dirty:
            raise GitError("working tree is dirty; commit, stash, or pass --allow-dirty")
        if self.current_branch() == self.branch:
            return
        if self.branch_exists(self.branch):
            self.git("checkout", self.branch)
        else:
            self.git("checkout", "-b", self.branch)

    def commit(self, message: str, *, allow_empty: bool = False) -> str:
        if not allow_empty and not self.is_dirty():
            return self.head()
        self.git("add", "-A")
        args = ["commit", "-q", "-m", message, "--no-verify"]
        if allow_empty:
            args.append("--allow-empty")
        self.git(*args)
        return self.head()

    def diff(self, a: str, b: str, *, limit: int = 60000) -> str:
        """The full diff between two refs, truncated."""
        out = self.git("diff", "--no-color", f"{a}..{b}")
        return out if len(out) <= limit else out[:limit] + "\n... (diff truncated)"

    def tag(self, name: str, *, force: bool = True) -> None:
        args = ["tag", name]
        if force:
            args.insert(1, "-f")
        self.git(*args)

    def revert_to(self, ref: str, *, patch_out: Path | None = None) -> str:
        """Add revert commits that bring the tree back to `ref`. Never rewrites history."""
        before = self.head()
        target = self.git("rev-parse", f"{ref}^{{commit}}")
        if target == before:
            return before
        if patch_out is not None:
            patch_out.parent.mkdir(parents=True, exist_ok=True)
            patch_out.write_text(self.git("diff", ref, before, check=False))
        if self.is_dirty():
            self.commit("ouroboros: snapshot before revert")
            before = self.head()
        self.clear_sequencer()
        code = self.git_code("revert", "--no-edit", f"{ref}..{before}")
        # A refused or conflicted revert leaves the tree short of `ref`. Never infer
        # success from a clean tree: git refuses outright -- writing no REVERT_HEAD
        # and touching nothing -- when sequencer state is already present.
        if code != 0 or self.sequencer_in_progress() or self.is_dirty():
            self.git_code("revert", "--abort")
            self.clear_sequencer()
            # Take `ref` wholesale. read-tree sets index and work tree to exactly
            # that commit's tree, dropping files added since, and commits without
            # `add -A` so untracked run state is neither swept up nor deleted.
            self.git("read-tree", "-u", "--reset", ref)
            self.git("commit", "-q", "-m", f"ouroboros: revert to {ref}", "--no-verify", "--allow-empty")
        head = self.head()
        if self.git("rev-parse", f"{head}^{{tree}}") != self.git("rev-parse", f"{target}^{{tree}}"):
            raise GitError(f"revert to {ref} left HEAD at {head[:10]}, whose tree still differs from {ref}")
        return head
