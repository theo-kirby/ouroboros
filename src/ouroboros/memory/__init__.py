"""Memory adapters: how one iteration talks to the next."""

from __future__ import annotations

import shutil
from pathlib import Path

from .base import MemoryAdapter
from .handoff import HandoffMemory
from .hypergraph import HypergraphMemory


def hypergraph_available(repo: Path) -> bool:
    return (repo / ".hypergraph" / "config.yml").exists() and shutil.which("hypergraph") is not None


def make_memory(kind: str, repo: Path, *, recent: int = 3, plan: bool = True, plan_view: str = "plan",
                plan_md: str = "PLAN.md", max_new_directions: int = 3, **hg_opts) -> MemoryAdapter:
    if kind == "auto":
        kind = "hypergraph" if hypergraph_available(repo) else "handoff"
    if kind == "handoff":
        return HandoffMemory(repo / ".ouroboros", recent=recent, plan=plan, max_new_directions=max_new_directions)
    hg_opts.update(plan=plan, plan_view=plan_view, plan_md=plan_md, max_new_directions=max_new_directions)
    if kind == "hypergraph":
        if not (repo / ".hypergraph" / "config.yml").exists():
            raise ValueError("memory: hypergraph requested but .hypergraph/config.yml is missing")
        if shutil.which("hypergraph") is None:
            raise ValueError("memory: hypergraph requested but the `hypergraph` CLI is not on PATH (uv tool install hypergraph-protocol)")
        return HypergraphMemory(repo, recent=recent, **hg_opts)
    raise ValueError(f"unknown memory adapter {kind!r}")


__all__ = ["MemoryAdapter", "HandoffMemory", "HypergraphMemory", "make_memory", "hypergraph_available"]
