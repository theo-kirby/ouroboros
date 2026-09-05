"""Memory adapters: how one iteration talks to the next."""

from __future__ import annotations

import shutil
from pathlib import Path

from .base import MemoryAdapter
from .handoff import HandoffMemory


def make_memory(kind: str, repo: Path, *, recent: int = 3) -> MemoryAdapter:
    if kind == "auto":
        has_hg = (repo / ".hypergraph" / "config.yml").exists() and shutil.which("hypergraph")
        kind = "hypergraph" if has_hg else "handoff"
    if kind == "handoff":
        return HandoffMemory(repo / ".ouroboros", recent=recent)
    if kind == "hypergraph":
        # Phase 3 lands the real adapter. Until then, hypergraph repos get the
        # handoff adapter plus a note in the prompt.
        return HandoffMemory(repo / ".ouroboros", recent=recent, hypergraph_hint=True)
    raise ValueError(f"unknown memory adapter {kind!r}")


__all__ = ["MemoryAdapter", "HandoffMemory", "make_memory"]
