"""Memory: how one iteration talks to the next.

Both memories are the same record/state model (graph.py) on two stores: a governor
graph, or markdown files in git. `files` was called `handoff`; that name still reads.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from .base import MemoryAdapter
from .files import FilesMemory, FilesStore
from .governor import GovernorMemory, GovernorStore, governor_repo
from .graph import GraphMemory, Store

KINDS = ("auto", "governor", "files")
ALIASES = {"handoff": "files"}
ENV = "OUROBOROS_MEMORY"   # set by `run` for the harnesses it starts, so `ouroboros mem` uses the run's store


def governor_available(repo: Path) -> bool:
    """A Flywheel repository at the checkout (or its main checkout) and the `governor` CLI."""
    return governor_repo(repo) is not None and shutil.which("governor") is not None


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
           "KINDS", "ALIASES", "ENV", "make_memory", "make_store", "resolve_kind", "store_for_cli", "governor_available"]
