from __future__ import annotations

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
