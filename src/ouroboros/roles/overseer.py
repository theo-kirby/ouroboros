"""The sleeping user. Phase 1: rules only. Phase 2 adds the agent overseer in front."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

VERDICTS = ("continue", "answer", "done_rejected", "done_accepted", "stuck", "revert")

RULES_REPLY = (
    "Decide for yourself. Pick the option that is most reversible. "
    "Write your assumption in the handoff. Continue."
)
DONE_REPLY = (
    "Do not stop. The whole goal is never done from your side. Check the done criteria "
    "in the goal one by one and list what is still open. Then take the next rung of the "
    "horizon ladder and do one unit of it."
)
STUCK_REPLY = (
    "The last iterations changed nothing. Apply the exhaustion policy from the goal: "
    "pick a rung of the horizon ladder that still has items, or propose three new "
    "directions in the plan, pick one, and do one unit of it now."
)
REVERT_REPLY = (
    "The same error happened three times. That path is dead. Record it as a dead end "
    "in the handoff and take a different approach."
)

_QUESTION = re.compile(
    r"(\?\s*$)|\b(should i|do you want|would you like|let me know|which (one|option|approach)|"
    r"please (confirm|advise|clarify)|waiting for (your|user)|need(s)? your (input|decision|approval))\b",
    re.IGNORECASE,
)
_DONE = re.compile(
    r"\b(all (tasks|work|goals?) (are |is )?(done|complete|finished)|goal (is )?(complete|achieved|done)|"
    r"nothing (left|more|else) to do|task (is )?complete|i('m| am) done|work is complete)\b",
    re.IGNORECASE,
)


@dataclass
class Signals:
    text: str
    changed: bool
    recorded: bool
    no_change_streak: int
    error_streak: int
    error: str | None = None


@dataclass
class Verdict:
    verdict: str
    reply: str
    reason: str


class Overseer(Protocol):
    def judge(self, s: Signals) -> Verdict: ...


class RulesOverseer:
    """Deterministic. Never blocks. Never accepts done."""

    name = "rules"

    def judge(self, s: Signals) -> Verdict:
        tail = s.text.strip()[-1500:]
        if s.error_streak >= 3:
            return Verdict("revert", REVERT_REPLY, f"same error {s.error_streak}x")
        if s.no_change_streak >= 3:
            return Verdict("stuck", STUCK_REPLY, f"no changes for {s.no_change_streak} iterations")
        if _DONE.search(tail):
            return Verdict("done_rejected", DONE_REPLY, "actor claimed done; rules never accept")
        if _QUESTION.search(tail):
            return Verdict("answer", RULES_REPLY, "actor ended with a question")
        if not s.recorded:
            return Verdict(
                "continue",
                "The previous iteration did not write its handoff file. Write it FIRST, "
                "from the git log and the diff, then continue with one unit.",
                "no record written",
            )
        return Verdict("continue", "", "ok")
