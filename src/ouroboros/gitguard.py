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

    def diff_stat(self, a: str, b: str = "HEAD") -> str:
        return self.git("diff", "--stat", a, b, check=False)

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

    def commit(self, message: str, *, allow_empty: bool = True) -> str:
        if not allow_empty and not self.is_dirty():
            return self.head()
        self.git("add", "-A")
        args = ["commit", "-q", "-m", message, "--no-verify"]
        if allow_empty:
            args.append("--allow-empty")
        self.git(*args)
        return self.head()

    def tag(self, name: str, *, force: bool = True) -> None:
        args = ["tag", name]
        if force:
            args.insert(1, "-f")
        self.git(*args)

    def revert_to(self, ref: str, *, patch_out: Path | None = None) -> str:
        """Add revert commits that bring the tree back to `ref`. Never rewrites history."""
        before = self.head()
        if self.git("rev-parse", ref) == before:
            return before
        if patch_out is not None:
            patch_out.parent.mkdir(parents=True, exist_ok=True)
            patch_out.write_text(self.git("diff", ref, before, check=False))
        if self.is_dirty():
            self.commit("ouroboros: snapshot before revert")
            before = self.head()
        self.git("revert", "--no-edit", f"{ref}..{before}", check=False)
        # if revert stopped on a conflict, resolve by taking `ref` wholesale
        if (self.repo / ".git" / "REVERT_HEAD").exists() or self.git("status", "--porcelain"):
            self.git("revert", "--abort", check=False)
            self.git("checkout", ref, "--", ".")
            self.git("clean", "-fd", "-e", ".ouroboros/runs", check=False)
            self.commit(f"ouroboros: revert to {ref}")
        return self.head()
