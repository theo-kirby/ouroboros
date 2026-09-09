"""Motion is not progress.

The `stuck` signal counts iterations that produced no diff. That catches a loop
which stops working. It cannot catch the more expensive one: a loop that keeps
working and ships nothing — records about records, audits of what was already
audited, plans that re-state the same bet, handoffs from an agent to itself.
Every one of those writes files, so a diff-based signal reads them as progress.

nt3 is the worked example. 201 iterations, 22,437 lines, 128 record nodes, and
**one** node moved on the frontier. `no_change_streak` stayed at 0 for the whole
tail while nothing shipped, and the overseer said `continue` sixteen times in a
row.

This module measures the other thing. Three signals, deliberately mechanical:

    no_product_streak    iterations whose diff touched only bookkeeping
    no_frontier_streak   iterations after which the frontier looked the same
    repeat_bet_streak    planner bets that restate a recent bet

They are mechanical because an agent asked "are you looping?" will always find a
reason why this time is different. Judgment goes on top of the measurement,
never instead of it: the numbers reach the critic, which is free to disagree
with them, and the escalation ladder runs on the numbers regardless.

Nothing here stops a run. A loop is a thing to break out of, not a thing to die
of, and a false positive must never end a run nobody is awake for.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

# Paths that are the loop talking about itself. A diff confined to these is
# bookkeeping: real, often necessary, and not the work. Everything else --
# source, tests, docs, data, build files -- counts as product, because a
# narrower rule would call a docs fix "not work", and it is.
BOOKKEEPING = (".hypergraph/", ".ouroboros/")
GENERATED = ("STATE.md", "PLAN.md", "ROADMAP.md")

# Number words collapse to one token before bets are compared. This is not
# decoration: nt3's planner wrote "retain the conditional rehearsal after
# fifteen unchanged iterations", then twenty, then twenty-four, then thirty-four
# -- twenty bets whose only moving part was the count. Without this the shingle
# overlap reads 0.25 and the loop is invisible; with it, 1.0.
_NUMBER_WORDS = {
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight",
    "nine", "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen",
    "sixteen", "seventeen", "eighteen", "nineteen", "twenty", "thirty",
    "forty", "fifty", "sixty", "seventy", "eighty", "ninety", "hundred",
    "first", "second", "third", "fourth", "fifth", "sixth", "seventh",
    "eighth", "ninth", "tenth", "another", "again", "further", "more",
}
# `#` is a token, not punctuation: it is what every number collapses to.
_WORD = re.compile(r"[a-z]+|#")
# A bet arrives as "slug-name-1234 — Bet: ...". Neither half of that prefix says
# anything about what the bet is, and the slug is new every time, so both go.
_BET_PREFIX = re.compile(r"^\s*(?:[a-z]+-[a-z]+-\d+\s*[\u2014-]\s*)?(?:bet\s*:\s*)?", re.IGNORECASE)
# Words that carry no subject. Dropping them is what lets a paraphrase be
# recognised as the same bet: "preserve the held rehearsal after fifteen
# iterations" and "hold the rehearsal pending new evidence" share their content
# and share almost none of their word order.
_STOPWORDS = {
    "the", "a", "an", "of", "to", "and", "or", "for", "in", "on", "at", "by",
    "with", "as", "is", "are", "was", "be", "been", "being", "that", "this",
    "it", "its", "after", "before", "then", "than", "from", "into", "over",
    "under", "new", "without", "through", "while",
}


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


def normalise(text: str) -> list[str]:
    """Words only, lowercased, every number collapsed to `#`, the slug prefix gone."""
    body = re.sub(r"\d+", " # ", _BET_PREFIX.sub("", text or "").lower())
    out = ["#" if w in _NUMBER_WORDS else w for w in _WORD.findall(body)]
    # "twenty four" and "24" must not differ by a token count.
    return [w for i, w in enumerate(out) if not (w == "#" and i and out[i - 1] == "#")]


def content_words(text: str) -> set[str]:
    """What a bet is about: no stopwords, no numbers, no order."""
    return {w for w in normalise(text) if w != "#" and w not in _STOPWORDS}


def similarity(a: str, b: str) -> float:
    """Jaccard overlap of content words. 1.0 is the same bet, 0.0 shares no subject.

    Word order is deliberately thrown away. Measured against the whole nt3 run,
    order-sensitive shingles score a genuine paraphrase at 0.00 -- the same as
    two unrelated bets -- because the planner rewrites the sentence every time.
    Content words separate the two cases: across nt3's 35 healthy bets the
    highest score against a recent bet was 0.33, and across the stall it climbed
    from 0.43 to a flat 1.00. The default threshold sits in that gap.
    """
    ca, cb = content_words(a), content_words(b)
    if not ca or not cb:
        return 0.0
    return len(ca & cb) / len(ca | cb)


@dataclass
class LoopReport:
    """One fired signal, with the evidence that fired it."""

    signal: str          # no_product | no_frontier | repeat_bet
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
            "repeat_bet": f"{self.streak} planner bets in a row restated an earlier bet",
        }[self.signal]
        if not self.evidence:
            return head
        return head + ":\n" + "\n".join(f"  - {e}" for e in self.evidence)


class LoopDetector:
    """Counts the three signals and names the strongest one that has fired.

    Every counter resets the moment the thing it measures happens: one product
    commit clears `no_product`, one node moving clears `no_frontier`, one fresh
    bet clears `repeat_bet`. A loop has to be sustained to be reported.
    """

    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self.no_product = 0
        self.no_frontier = 0
        self.repeat_bet = 0
        self.recent_bets: list[str] = []
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

    def observe_bet(self, bet: str | None) -> float:
        """One planner pass. Returns how close the bet was to the nearest recent one."""
        if not bet or not bet.strip():
            return 0.0
        window = self.cfg.bet_window
        best = max((similarity(bet, old) for old in self.recent_bets[-window:]), default=0.0)
        if best >= self.cfg.bet_similarity:
            self.repeat_bet += 1
        else:
            self.repeat_bet = 0
            self._acted.pop("repeat_bet", None)
        self.recent_bets.append(bet.strip())
        del self.recent_bets[: -max(window, 1)]
        return best

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
            self._check("repeat_bet", self.repeat_bet, c.repeat_bet_after, self.recent_bets[-3:]),
        ]

    def _check(self, name: str, streak: int, after: int | None, evidence: list[str]) -> LoopReport | None:
        if after is None or streak < after:
            return None
        return LoopReport(name, streak, after, list(evidence))

    def should_act(self, report: LoopReport) -> bool:
        """True on the first firing, then once per `escalate_every` iterations.

        Naming the loop to the actor is free and happens every iteration. Forcing
        a planner pass and rotating the harness are not, and a long streak would
        otherwise repeat them forever: nt3's frontier did not move after
        iteration 44, so an ungated ladder would have re-planned and rotated 157
        times. Acting periodically keeps the escalation meaningful and its cost
        proportional to the run, not to the streak.
        """
        last = self._acted.get(report.signal)
        if last is not None and report.streak < last + max(self.cfg.escalate_every, 1):
            return False
        self._acted[report.signal] = report.streak
        return True

    def escalation(self, report: LoopReport) -> int:
        """1 name it, 2 re-plan, 3 change the model. Never 4: nothing here stops a run."""
        step = 1 + report.over // max(self.cfg.escalate_every, 1)
        return min(step, 3)

    def counters(self) -> dict[str, int]:
        return {
            "no_product": self.no_product,
            "no_frontier": self.no_frontier,
            "repeat_bet": self.repeat_bet,
        }

    def describe(self) -> str:
        """The three numbers, for the critic's signal block."""
        return (
            f"- iterations in a row changing only bookkeeping: {self.no_product}\n"
            f"- iterations in a row with the frontier unmoved: {self.no_frontier}\n"
            f"- planner bets in a row restating an earlier bet: {self.repeat_bet}"
        )


# -- what the actor is told -------------------------------------------------

_NAMED = {
    "no_product": (
        "You are writing about the work instead of doing it. Your last {streak} units "
        "changed nothing outside the memory graph: no source, no test, no document. "
        "A record describes a change; it is not one. Your next unit must change a file "
        "that is not a record and not a plan, or say plainly in one line why no such "
        "change is possible."
    ),
    "no_frontier": (
        "The frontier has not moved in {streak} iterations. Nodes are not changing "
        "status and no charter criterion is getting closer to ticked. Whatever you have "
        "been doing, it is not advancing the goal. Pick one criterion from the charter's "
        "done criteria, name it, and do the smallest thing that moves it."
    ),
    "repeat_bet": (
        "The plan is going in a circle. The last {streak} bets restate the same bet with "
        "different numbers. Re-planning is not progress. Do not write another bet on this "
        "subject."
    ),
}

_REPLAN = (
    "\n\nThe current bet is now banned. Choose a different charter criterion, and write "
    "one bet about that instead. If every criterion looks blocked, say which one and why, "
    "in one line each."
)

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
        body += _REPLAN
    if step >= 3:
        body += _ROTATE.format(streak=report.streak)
    return body
