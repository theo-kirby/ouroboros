"""Harness drivers: one per agent CLI, all behind the same protocol."""

from .base import Harness, Result
from .claude import ClaudeHarness
from .codex import CodexHarness
from .pi import PiHarness

REGISTRY: dict[str, type] = {"claude": ClaudeHarness, "codex": CodexHarness, "pi": PiHarness}


def make_harness(name: str) -> Harness:
    try:
        return REGISTRY[name]()
    except KeyError:
        raise ValueError(f"unknown harness {name!r}; known: {sorted(REGISTRY)}") from None


__all__ = ["Harness", "Result", "ClaudeHarness", "CodexHarness", "PiHarness", "make_harness", "REGISTRY"]
