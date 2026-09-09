"""Handoff-document memory: goal.md, plan.md, journal.md, handoff/NNNN.md."""

from __future__ import annotations

import re
from pathlib import Path

from .base import BaseMemory

_NUM = re.compile(r"^(\d{4})\.md$")


class HandoffMemory(BaseMemory):
    name = "handoff"

    def __init__(self, root: Path, *, recent: int = 3, hypergraph_hint: bool = False, plan: bool = True,
                 max_new_directions: int = 3) -> None:
        self.root = root
        self.recent = recent
        self.hypergraph_hint = hypergraph_hint
        self.goal = root / "goal.md"
        self.plan = root / "plan.md"
        self.bets = root / "bets.md"
        self.journal = root / "journal.md"
        self.handoffs = root / "handoff"
        self.plan_enabled = plan
        self.max_new_directions = max_new_directions
        self.goal_text = ""

    def start(self, *, run: str, goal_text: str, run_dir: Path, branch: str) -> None:
        self.goal_text = goal_text
        self.ensure()

    # -- the plan layer (file-backed parity with the hypergraph plan view) --
    def planner_prompt(self, signals: str) -> str | None:
        if not self.plan_enabled:
            return None
        from importlib import resources
        tpl = resources.files("ouroboros.skills").joinpath("ouroboros-planner/SKILL.md").read_text()
        tpl = tpl.split("\n---\n", 1)[1] if tpl.startswith("---") else tpl
        recent = self.handoff_files()[-3:]
        recent_text = "\n\n".join(f"### {self._rel(f)}\n\n{f.read_text().strip()[:2500]}" for f in recent) or "(none)"
        last_next = ""
        if recent:
            t = recent[-1].read_text()
            last_next = t.split("## Next", 1)[1].strip()[:1500] if "## Next" in t else ""
        fold = (
            f"1. Rewrite `{self._rel(self.plan)}` in full with exactly three sections: `## short` (the next two or three "
            f"units, one iteration each, ranked), `## medium` (the open gaps in the order they should fall, with why), "
            f"`## long` (directions, bets, things to prove, and the standing work that never ends). "
            f"Cite the handoff file that motivates each line, like `[handoff/0007.md]`.\n"
            f"2. Append ONE entry to `{self._rel(self.bets)}` (create it with a `# Bets` heading if missing): "
            f"`## Bet <ISO date>: <summary>` followed by `### Why` (the reasoning and the evidence) and `### Changed` "
            f"(what moved between horizons; or `plan holds: <reason>` when nothing changes).\n"
            f"3. Write nothing else. Do not commit; the loop commits for you."
        )
        goal = self.goal_text or (self.goal.read_text() if self.goal.exists() else "")
        return (
            tpl.replace("{max_new}", str(self.max_new_directions))
            .replace("{charter}", goal.strip()[:12000] or "(none)")
            .replace("{frontier}", (f"Last handoff's `## Next`:\n\n{last_next}" if last_next else "(no handoffs yet)"))
            .replace("{plan}", self.plan.read_text().strip()[:6000] if self.plan.exists() else "(no plan yet)")
            .replace("{pending}", "(not tracked without hypergraph)")
            .replace("{recent}", recent_text)
            .replace("{signals}", signals.strip() or "(none)")
            .replace("{fold}", fold)
        )

    def verify_bet(self, before: int) -> str | None:
        if not self.bets.exists():
            return None
        text = self.bets.read_text()
        if len(text) <= before:
            return None
        heads = [l for l in text[before:].splitlines() if l.startswith("## Bet")]
        return (heads[-1][3:].strip() if heads else "bet appended")[:120]

    def bets_size(self) -> int:
        return len(self.bets.read_text()) if self.bets.exists() else 0

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

    def critic_context(self) -> str:
        parts = []
        if self.plan.exists() and self.plan.read_text().strip():
            parts.append(f"### Plan (`{self._rel(self.plan)}`)\n\n{self.plan.read_text().strip()[:4000]}")
        recent = self.handoff_files()[-1:]
        if recent:
            text = recent[0].read_text()
            nxt = text.split("## Next", 1)[1].strip() if "## Next" in text else text.strip()
            parts.append(f"### Last handoff, `## Next`\n\n{nxt[:1500]}")
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
