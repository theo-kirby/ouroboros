from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol


class MemoryAdapter(Protocol):
    name: str

    def orient_prompt(self) -> str:
        """Text placed before the work instructions: what is true now."""

    def record_prompt(self) -> str:
        """Text that tells the agent exactly how to record this unit."""

    def snapshot(self) -> Any:
        """Opaque marker taken before the call."""

    def verify_recorded(self, before: Any) -> bool:
        """True if the agent recorded since `before`."""

    def needs_reconcile(self) -> bool: ...

    def reconcile_prompt(self) -> str | None: ...

    def last_summary(self) -> str:
        """One line for the commit message."""

    # lifecycle hooks (no-ops for adapters that do not need them)
    def start(self, *, run: str, goal_text: str, run_dir: Path, branch: str) -> None: ...

    def mark_iteration(self) -> None: ...

    def mark_reconciled(self) -> None: ...

    def check_report(self) -> str | None:
        """Problems the memory's own checker sees after a commit, or None."""

    def overseer_context(self) -> str:
        """Short text for the overseer: the frontier and the current plan."""

    def planner_prompt(self, signals: str) -> str | None:
        """The planner role prompt, or None when this memory has no plan layer."""

    def verify_bet(self, before: Any) -> str | None:
        """The bet the planner wrote since `before` (a slug or title), or None."""


class BaseMemory:
    """No-op lifecycle defaults."""

    name = "base"

    def start(self, *, run: str, goal_text: str, run_dir: Path, branch: str) -> None:
        return None

    def mark_iteration(self) -> None:
        return None

    def mark_reconciled(self) -> None:
        return None

    def check_report(self) -> str | None:
        return None

    def needs_reconcile(self) -> bool:
        return False

    def reconcile_prompt(self) -> str | None:
        return None

    def overseer_context(self) -> str:
        return ""

    def planner_prompt(self, signals: str) -> str | None:
        return None

    def verify_bet(self, before) -> str | None:
        return None
