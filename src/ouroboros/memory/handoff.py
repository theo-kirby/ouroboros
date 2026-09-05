"""Handoff-document memory: goal.md, plan.md, journal.md, handoff/NNNN.md."""

from __future__ import annotations

import re
from pathlib import Path

_NUM = re.compile(r"^(\d{4})\.md$")


class HandoffMemory:
    name = "handoff"

    def __init__(self, root: Path, *, recent: int = 3, hypergraph_hint: bool = False) -> None:
        self.root = root
        self.recent = recent
        self.hypergraph_hint = hypergraph_hint
        self.goal = root / "goal.md"
        self.plan = root / "plan.md"
        self.journal = root / "journal.md"
        self.handoffs = root / "handoff"

    # -- files ---------------------------------------------------------
    def ensure(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.handoffs.mkdir(exist_ok=True)
        if not self.plan.exists():
            self.plan.write_text("# Plan\n\n(agent-owned. rewrite freely.)\n")
        if not self.journal.exists():
            self.journal.write_text("# Journal\n\n(append-only. one block per iteration.)\n")

    def handoff_files(self) -> list[Path]:
        if not self.handoffs.exists():
            return []
        files = [p for p in self.handoffs.iterdir() if _NUM.match(p.name)]
        return sorted(files, key=lambda p: int(_NUM.match(p.name).group(1)))

    def next_number(self) -> int:
        files = self.handoff_files()
        return (int(_NUM.match(files[-1].name).group(1)) + 1) if files else 1

    def next_path(self) -> Path:
        return self.handoffs / f"{self.next_number():04d}.md"

    # -- adapter protocol ----------------------------------------------
    def orient_prompt(self) -> str:
        parts = []
        if self.plan.exists():
            parts.append(f"## Current plan (`{self._rel(self.plan)}`)\n\n{self.plan.read_text().strip()}")
        recent = self.handoff_files()[-self.recent :]
        if recent:
            blocks = [f"### {self._rel(p)}\n\n{p.read_text().strip()}" for p in recent]
            parts.append("## Recent handoffs (oldest first)\n\n" + "\n\n".join(blocks))
        else:
            parts.append("## Recent handoffs\n\nNone. This is the first iteration.")
        if self.hypergraph_hint:
            parts.append(
                "## Hypergraph\n\nThis repo uses hypergraph-protocol. Read STATE.md first. "
                "Record each unit with the hypergraph-record skill in addition to the handoff. "
                "Never reconcile. Never write state nodes."
            )
        return "\n\n".join(parts)

    def record_prompt(self) -> str:
        path = self._rel(self.next_path())
        return (
            f"When the unit is finished, and before you stop, you MUST:\n"
            f"1. Write `{path}` with these headings: `## Did`, `## Learned`, `## Assumed`, `## Next`.\n"
            f"   Keep it under 40 lines. `## Next` names one concrete unit for the next iteration.\n"
            f"2. Append one block to `{self._rel(self.journal)}`: a line `## Iteration -> {path}` "
            f"followed by a 1-3 line summary.\n"
            f"3. Update `{self._rel(self.plan)}` if the plan changed.\n"
            f"Do not skip step 1. An iteration with no handoff file is counted as lost work."
        )

    def snapshot(self) -> set[str]:
        return {p.name for p in self.handoff_files()}

    def verify_recorded(self, before: set[str]) -> bool:
        return bool(self.snapshot() - before)

    def needs_reconcile(self) -> bool:
        return False

    def reconcile_prompt(self) -> str | None:
        return None

    def last_summary(self) -> str:
        files = self.handoff_files()
        if not files:
            return "no handoff"
        text = files[-1].read_text()
        m = re.search(r"## Did\s*\n+(.+)", text)
        line = (m.group(1) if m else text.strip().splitlines()[0] if text.strip() else "").strip("-* ").strip()
        return line[:72] or files[-1].name

    def _rel(self, p: Path) -> str:
        try:
            return str(p.relative_to(self.root.parent))
        except ValueError:
            return str(p)
