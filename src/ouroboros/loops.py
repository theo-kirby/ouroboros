"""Motion is not progress.

The `stuck` signal counts iterations that produced no diff. That catches a loop
which stops working. It cannot catch the more expensive one: a loop that keeps
working and ships nothing — records about records, audits of what was already
audited, handoffs from an agent to itself.
Every one of those writes files, so a diff-based signal reads them as progress.

nt3 is the worked example. 201 iterations, 22,437 lines, 128 record nodes, and
**one** node moved on the frontier. `no_change_streak` stayed at 0 for the whole
tail while nothing shipped, and the overseer said `continue` sixteen times in a
row.

This module measures the other thing. Two signals, deliberately mechanical:

    no_product_streak    iterations whose diff touched only bookkeeping
    no_frontier_streak   iterations after which the frontier looked the same

They are mechanical because an agent asked "are you looping?" will always find a
reason why this time is different. Judgment goes on top of the measurement,
never instead of it: the numbers reach the critic, which is free to disagree
with them, and the escalation ladder runs on the numbers regardless.

Nothing here stops a run. A loop is a thing to break out of, not a thing to die
of, and a false positive must never end a run nobody is awake for.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

# Paths that are the loop talking about itself. A diff confined to these is
# bookkeeping: real, often necessary, and not the work. Everything else --
# source, tests, docs, data, build files -- counts as product, because a
# narrower rule would call a docs fix "not work", and it is.
BOOKKEEPING = (".hypergraph/", ".ouroboros/", ".governor/", ".flywheel/")
GENERATED = ("STATE.md", "PLAN.md", "ROADMAP.md")

def is_product_change(paths: Iterable[str]) -> bool:
    """True when the diff touched anything that is not the loop's own bookkeeping."""
    for raw in paths:
        p = (raw or "").strip()
        if p.startswith("./"):   # lstrip("./") would also eat the dot of ".hypergraph"
            p = p[2:]
        if not p or p in GENERATED or p.startswith(BOOKKEEPING):
            continue
        return True
    return False


@dataclass
class LoopReport:
    """One fired signal, with the evidence that fired it."""

    signal: str          # no_product | no_frontier
    streak: int
    threshold: int
    evidence: list[str] = field(default_factory=list)

    @property
    def over(self) -> int:
        """How far past the threshold, in iterations."""
        return self.streak - self.threshold

    def describe(self) -> str:
        head = {
            "no_product": f"{self.streak} iterations in a row changed only bookkeeping",
            "no_frontier": f"{self.streak} iterations in a row left the frontier unchanged",
        }[self.signal]
        if not self.evidence:
            return head
        return head + ":\n" + "\n".join(f"  - {e}" for e in self.evidence)


class LoopDetector:
    """Counts the two signals and names the strongest one that has fired.

    Every counter resets the moment the thing it measures happens: one product
    commit clears `no_product`, one node moving clears `no_frontier`. A loop has to be sustained to be reported.
    """

    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self.no_product = 0
        self.no_frontier = 0
        self.recent_subjects: list[str] = []
        self._frontier: str | None = None
        self._seen_frontier = False
        self._acted: dict[str, int] = {}   # signal -> the streak the ladder last acted on

    # -- observation -----------------------------------------------------
    def observe_iteration(self, *, files: Iterable[str], frontier: str | None, subject: str = "") -> None:
        """One finished iteration: what it touched, and what the frontier looked like after."""
        paths = list(files)
        if is_product_change(paths):
            self.no_product = 0
            self.recent_subjects.clear()
            self._acted.pop("no_product", None)
        else:
            self.no_product += 1
            if subject:
                self.recent_subjects.append(subject.strip()[:90])
                del self.recent_subjects[:-6]

        # A memory adapter that cannot describe its frontier disables this
        # signal rather than firing it: unknown is not the same as unmoved.
        if frontier is None:
            self.no_frontier = 0
            self._seen_frontier = False
            self._acted.pop("no_frontier", None)
            return
        if self._seen_frontier and frontier == self._frontier:
            self.no_frontier += 1
        else:
            self.no_frontier = 0
            self._acted.pop("no_frontier", None)
        self._frontier, self._seen_frontier = frontier, True

    # -- reporting -------------------------------------------------------
    def report(self) -> LoopReport | None:
        """The signal that is furthest past its threshold, or None."""
        fired = [r for r in self._all() if r is not None]
        return max(fired, key=lambda r: (r.over, r.streak)) if fired else None

    def _all(self) -> list[LoopReport | None]:
        c = self.cfg
        return [
            self._check("no_product", self.no_product, c.product_after, self.recent_subjects),
            self._check("no_frontier", self.no_frontier, c.frontier_after, []),
        ]

    def _check(self, name: str, streak: int, after: int | None, evidence: list[str]) -> LoopReport | None:
        if after is None or streak < after:
            return None
        return LoopReport(name, streak, after, list(evidence))

    def should_act(self, report: LoopReport) -> bool:
        """True on the first firing, then once per `escalate_every` iterations.

        Naming the loop to the actor is free and happens every iteration.
        Rotating the harness is not, and a long streak would otherwise repeat it
        forever: nt3's frontier did not move after iteration 44, so an ungated
        ladder would have rotated 157 times. Acting periodically keeps the escalation meaningful and its cost
        proportional to the run, not to the streak.
        """
        last = self._acted.get(report.signal)
        if last is not None and report.streak < last + max(self.cfg.escalate_every, 1):
            return False
        self._acted[report.signal] = report.streak
        return True

    def escalation(self, report: LoopReport) -> int:
        """1 name it, 2 change the model. Never 3: nothing here stops a run."""
        step = 1 + report.over // max(self.cfg.escalate_every, 1)
        return min(step, 2)

    def counters(self) -> dict[str, int]:
        return {
            "no_product": self.no_product,
            "no_frontier": self.no_frontier,
        }

    def describe(self) -> str:
        """The two numbers, for the critic's signal block."""
        return (
            f"- iterations in a row changing only bookkeeping: {self.no_product}\n"
            f"- iterations in a row with the frontier unmoved: {self.no_frontier}"
        )


# -- what the actor is told -------------------------------------------------

_NAMED = {
    "no_product": (
        "You are writing about the work instead of doing it. Your last {streak} units "
        "changed nothing outside the memory graph: no source, no test, no document. "
        "A record describes a change; it is not one. Your next unit must change a file "
        "that is not a record, or say plainly in one line why no such "
        "change is possible."
    ),
    "no_frontier": (
        "The frontier has not moved in {streak} iterations. Nodes are not changing "
        "status and no charter criterion is getting closer to ticked. Whatever you have "
        "been doing, it is not advancing the goal. Pick one criterion from the charter's "
        "done criteria, name it, and do the smallest thing that moves it."
    ),
}

_ROTATE = (
    "\n\nA different model is taking the next turn, because this approach has not worked "
    "for {streak} iterations. Leave a handoff that says what you tried and what you would "
    "try next."
)


def loop_reply(report: LoopReport, step: int) -> str:
    """The message injected into the actor's next prompt. Evidence first, then the ask."""
    body = _NAMED[report.signal].format(streak=report.streak)
    if report.evidence:
        body += "\n\nWhat you have been doing:\n" + "\n".join(f"- {e}" for e in report.evidence)
    if step >= 2:
        body += _ROTATE.format(streak=report.streak)
    return body
