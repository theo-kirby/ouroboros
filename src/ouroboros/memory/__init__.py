"""Memory: how one iteration talks to the next.

Both memories are the same record/state model (graph.py) on two stores: a governor
graph, or markdown files in git. `files` was called `handoff`; that name still reads.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from .base import MemoryAdapter
from .files import FilesMemory, FilesStore
from .governor import GovernorMemory, GovernorStore, _main_checkout, governor_repo
from .graph import GraphMemory, Store

KINDS = ("auto", "governor", "files")
ALIASES = {"handoff": "files"}
ENV = "OUROBOROS_MEMORY"   # set by `run` for the harnesses it starts, so `ouroboros mem` uses the run's store


def governor_available(repo: Path) -> bool:
    """A Flywheel repository at the checkout (or its main checkout) and the `governor` CLI."""
    return governor_repo(repo) is not None and shutil.which("governor") is not None


def can_bootstrap() -> bool:
    """Both CLIs a new governor graph needs: `flywheel` (the local repository) and `governor`."""
    return shutil.which("flywheel") is not None and shutil.which("governor") is not None


def files_memory_exists(repo: Path) -> bool:
    """A files memory with records in it: a run that must keep its store."""
    d = repo / ".ouroboros" / "graph" / "records"
    return d.is_dir() and any(d.glob("r-*.md"))


def bootstrap(kind: str, repo: Path, *, title: str) -> str | None:
    """Make the governor graph a run will use, when there is none yet. Returns a line for
    the log, or None when nothing was done.

    Governor on a local Flywheel repository is the standard store (2026-11-01, Theo); the
    files store is the fallback. So `auto` creates `.flywheel/` and the governor root at
    the main checkout when both CLIs are installed and the repo holds no files memory
    (a files run keeps its store). `governor` asked for by name is created the same way.
    Never raises: a failed init leaves `auto` on files."""
    kind = ALIASES.get(kind, kind)
    if kind not in ("auto", "governor") or governor_repo(repo) is not None or not can_bootstrap():
        return None
    if kind == "auto" and files_memory_exists(repo):
        return None
    home = _main_checkout(repo)
    steps = (["flywheel", "repo:init"], ["governor", "init", "--title", (title or "Ouroboros run").strip()[:200]])
    for argv in steps:
        try:
            proc = subprocess.run(argv, cwd=str(home), capture_output=True, text=True, timeout=120)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return f"memory: `{' '.join(argv[:2])}` failed ({exc}); using the files store"
        if proc.returncode != 0:
            return (f"memory: `{' '.join(argv[:2])}` exited {proc.returncode} "
                    f"({(proc.stderr or proc.stdout).strip()[:200]}); using the files store")
    return f"memory: created a governor graph on a local Flywheel repository at {home / '.flywheel'}"


def resolve_kind(kind: str, repo: Path) -> str:
    kind = ALIASES.get(kind, kind)
    if kind == "auto":
        return "governor" if governor_available(repo) else "files"
    if kind not in KINDS:
        raise ValueError(f"unknown memory {kind!r} (one of {', '.join(KINDS)})")
    return kind


def make_store(kind: str, repo: Path) -> Store:
    kind = resolve_kind(kind, repo)
    if kind == "files":
        return FilesStore(repo)
    if governor_repo(repo) is None:
        raise ValueError("memory: governor requested but there is no .flywheel/db.sqlite here or at the main checkout "
                         "(`flywheel repo:init`, then `governor init --title ...`)")
    if shutil.which("governor") is None:
        raise ValueError("memory: governor requested but the `governor` CLI is not on PATH (see the governor README)")
    return GovernorStore(repo)


def make_memory(kind: str, repo: Path, *, recent: int = 3, **graph_opts) -> GraphMemory:
    kind = resolve_kind(kind, repo)
    if kind == "files":
        return FilesMemory(repo, recent=recent, **graph_opts)
    make_store(kind, repo)   # the same checks
    return GovernorMemory(repo, recent=recent, **graph_opts)


def store_for_cli(repo: Path, configured: str) -> Store:
    """The store `ouroboros mem` acts on: the running loop's choice, else the config's."""
    return make_store(os.environ.get(ENV) or configured, repo)


__all__ = ["MemoryAdapter", "GraphMemory", "GovernorMemory", "FilesMemory", "GovernorStore", "FilesStore", "Store",
           "KINDS", "ALIASES", "ENV", "make_memory", "make_store", "resolve_kind", "store_for_cli", "governor_available",
           "bootstrap", "can_bootstrap", "files_memory_exists"]
