"""Memory adapters: how one iteration talks to the next."""

from __future__ import annotations

import shutil
from pathlib import Path

from .base import MemoryAdapter
from .governor import GovernorMemory, governor_repo
from .handoff import HandoffMemory
from .hypergraph import HypergraphMemory

KINDS = ("auto", "governor", "hypergraph", "handoff")


def hypergraph_available(repo: Path) -> bool:
    return (repo / ".hypergraph" / "config.yml").exists() and shutil.which("hypergraph") is not None


def governor_available(repo: Path) -> bool:
    """A Flywheel repository at the checkout (or its main checkout), the CLI, and no hypergraph config."""
    return (
        governor_repo(repo) is not None
        and shutil.which("governor") is not None
        and not (repo / ".hypergraph" / "config.yml").exists()
    )


def make_memory(kind: str, repo: Path, *, recent: int = 3, plan: bool = True, plan_view: str = "plan",
                plan_md: str = "PLAN.md", max_new_directions: int = 3, **hg_opts) -> MemoryAdapter:
    if kind == "auto":
        kind = "governor" if governor_available(repo) else "hypergraph" if hypergraph_available(repo) else "handoff"
    if kind == "handoff":
        return HandoffMemory(repo / ".ouroboros", recent=recent, plan=plan, max_new_directions=max_new_directions)
    hg_opts.update(plan=plan, plan_view=plan_view, plan_md=plan_md, max_new_directions=max_new_directions)
    if kind == "governor":
        if governor_repo(repo) is None:
            raise ValueError("memory: governor requested but there is no .flywheel/db.sqlite here or at the main checkout "
                             "(`flywheel repo:init`, then `governor init --title ...`)")
        if shutil.which("governor") is None:
            raise ValueError("memory: governor requested but the `governor` CLI is not on PATH (see the governor README)")
        return GovernorMemory(repo, recent=recent, **hg_opts)
    if kind == "hypergraph":
        if not (repo / ".hypergraph" / "config.yml").exists():
            raise ValueError("memory: hypergraph requested but .hypergraph/config.yml is missing")
        if shutil.which("hypergraph") is None:
            raise ValueError("memory: hypergraph requested but the `hypergraph` CLI is not on PATH (uv tool install hypergraph-protocol)")
        return HypergraphMemory(repo, recent=recent, **hg_opts)
    raise ValueError(f"unknown memory adapter {kind!r}")


__all__ = ["MemoryAdapter", "GovernorMemory", "HandoffMemory", "HypergraphMemory", "KINDS", "make_memory",
           "governor_available", "hypergraph_available"]
