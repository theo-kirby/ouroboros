"""Memory adapters: how one iteration talks to the next."""

from __future__ import annotations

import shutil
from pathlib import Path

from .base import MemoryAdapter
from .governor import GovernorMemory, governor_repo
from .handoff import HandoffMemory

KINDS = ("auto", "governor", "handoff")


def governor_available(repo: Path) -> bool:
    """A Flywheel repository at the checkout (or its main checkout) and the `governor` CLI."""
    return governor_repo(repo) is not None and shutil.which("governor") is not None


def make_memory(kind: str, repo: Path, *, recent: int = 3, **graph_opts) -> MemoryAdapter:
    if kind == "auto":
        kind = "governor" if governor_available(repo) else "handoff"
    if kind == "handoff":
        return HandoffMemory(repo / ".ouroboros", recent=recent)
    if kind == "governor":
        if governor_repo(repo) is None:
            raise ValueError("memory: governor requested but there is no .flywheel/db.sqlite here or at the main checkout "
                             "(`flywheel repo:init`, then `governor init --title ...`)")
        if shutil.which("governor") is None:
            raise ValueError("memory: governor requested but the `governor` CLI is not on PATH (see the governor README)")
        return GovernorMemory(repo, recent=recent, **graph_opts)
    raise ValueError(f"unknown memory adapter {kind!r}")


__all__ = ["MemoryAdapter", "GovernorMemory", "HandoffMemory", "KINDS", "make_memory", "governor_available"]
