"""The critic: the one read-only call after every actor turn. Rules stand behind it.

It is the reviewer and the sleeping user in one. They used to be two roles, and
each was half blind: the critic graded the diff without seeing the plan, the
overseer steered from the plan without seeing the diff. Both are judgment jobs
over the same evidence, so they are one call now, and the rule that keeps that
judgment honest is that the critic never writes to the repo. The actor does the
housekeeping the critic asks for.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Protocol

from ..harness.base import Harness, Result

VERDICTS = ("continue", "answer", "done_rejected", "done_accepted", "stuck", "looping", "reject")
# Verdicts the older prompts produced, read as what they meant.
_ALIASES = {"revert": "reject", "accept": "continue"}

# How many past decisions the critic sees. Three was not enough to notice a
# pattern: nt3 re-derived "stuck" from scratch 113 times because each call could
# only see the two before it. A loop is only visible over a window.
HISTORY_WINDOW = 20
# Read-only tools, so the critic can open the files the diff touches. Twelve turns
# is room to look, not room to wander.
CRITIC_MAX_TURNS = 12
DIFF_LIMIT = 24000

# `did` and `doing` are for the person watching, not for the loop: the monitor shows
# one sentence for the unit that just finished and one for the unit now running, so a
# glance answers "where is it" without reading a transcript. `fix_first` is the
# housekeeping the actor must do before its next unit: the critic names it, the
# actor does it, because the one that judges never writes.
VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": list(VERDICTS)},
        "reply": {"type": "string"},
        "reason": {"type": "string"},
        "did": {"type": "string"},
        "doing": {"type": "string"},
        "fix_first": {"type": "string"},
    },
    "required": ["verdict", "reply", "reason", "did", "doing", "fix_first"],
}

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
LOOPING_REPLY = (
    "The last iterations changed files but moved nothing. Writing about the work is "
    "not the work. Name one open criterion from the goal's done criteria, and make the "
    "smallest change to a real file that moves it. Do not write another record about "
    "what you plan to do."
)
REJECT_REPLY = (
    "The same error happened three times. That path is dead. Record it as a dead end "
    "in the handoff and take a different approach."
)
NO_RECORD_REPLY = (
    "The previous iteration did not write its handoff file. Write it FIRST, "
    "from the git log and the diff, then continue with one unit."
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
    iteration: int = 0
    diff_stat: str = ""
    diff: str = ""       # the change itself, truncated to DIFF_LIMIT in the prompt
    history: list[dict] = field(default_factory=list)
    memory: str = ""     # what the memory adapter says is true now: frontier, plan
    loop: str = ""       # the loop detector's three counters (loops.py)
    loop_fired: str = ""  # the signal that has crossed its threshold, if any
    housekeeping: bool = False  # this iteration was the actor folding memory, not a unit of work

    def describe(self) -> str:
        lines = [
            f"- files changed this iteration: {'yes' if self.changed else 'no'}",
            f"- handoff recorded: {'yes' if self.recorded else 'NO'}",
            f"- iterations in a row with no changes: {self.no_change_streak}",
            f"- identical errors in a row: {self.error_streak}",
        ]
        if self.housekeeping:
            lines.append("- this was a housekeeping iteration: the actor folded the memory instead of "
                         "doing a unit; no record node is expected from it")
        if self.loop:
            lines.append(self.loop)
        if self.loop_fired:
            lines.append(f"- LOOP DETECTED: {self.loop_fired}")
        if self.error:
            lines.append(f"- harness error: {self.error[:300]}")
        if _QUESTION.search(self.text.strip()[-1500:]):
            lines.append("- the message looks like it ends with a question")
        if _DONE.search(self.text.strip()[-1500:]):
            lines.append("- the message looks like a claim that the goal is done")
        return "\n".join(lines)


@dataclass
class Verdict:
    verdict: str
    reply: str
    reason: str
    source: str = "rules"
    did: str = ""        # one sentence: the unit that just finished
    doing: str = ""      # one sentence: the unit the next iteration is starting
    fix_first: str = ""  # housekeeping the actor must do before its next unit

    @property
    def rejected(self) -> bool:
        return self.verdict == "reject"


class Critic(Protocol):
    name: str

    def judge(self, s: Signals) -> Verdict: ...


class RulesCritic:
    """Deterministic. Never blocks. Never accepts done. Runs when no model can."""

    name = "rules"

    def judge(self, s: Signals) -> Verdict:
        tail = s.text.strip()[-1500:]
        if s.error_streak >= 3:
            return Verdict("reject", REJECT_REPLY, f"same error {s.error_streak}x")
        if s.no_change_streak >= 3:
            return Verdict("stuck", STUCK_REPLY, f"no changes for {s.no_change_streak} iterations")
        if s.loop_fired:
            return Verdict("looping", LOOPING_REPLY, f"loop detector: {s.loop_fired}")
        if _DONE.search(tail):
            return Verdict("done_rejected", DONE_REPLY, "actor claimed done; rules never accept")
        if _QUESTION.search(tail):
            return Verdict("answer", RULES_REPLY, "actor ended with a question")
        if not s.recorded and not s.housekeeping:
            return Verdict("continue", NO_RECORD_REPLY, "no record written")
        return Verdict("continue", "", "housekeeping done" if s.housekeeping else "ok")


def load_template() -> str:
    text = resources.files("ouroboros.skills").joinpath("ouroboros-critic/SKILL.md").read_text()
    if text.startswith("---"):
        _, _, rest = text.partition("\n---\n")
        text = rest.lstrip("\n")
    return text


def build_critic_prompt(*, goal_text: str, s: Signals, template: str | None = None) -> str:
    tpl = template or load_template()
    hist = "\n".join(
        f"- #{h.get('iteration')}: {h.get('verdict')} — {h.get('reason')}" for h in s.history[-HISTORY_WINDOW:]
    ) or "(none yet)"
    return (
        tpl.replace("{iteration}", str(s.iteration))
        .replace("{goal}", goal_text.strip() or "(no goal)")
        .replace("{signals}", s.describe())
        .replace("{actor_output}", (s.text.strip() or "(empty)")[-6000:])
        .replace("{diff_stat}", s.diff_stat.strip() or "(no changes)")
        .replace("{diff}", s.diff.strip()[:DIFF_LIMIT] or "(no diff)")
        .replace("{history}", hist)
        .replace("{memory}", s.memory.strip() or "(no memory context)")
    )


def _one_line(value, limit: int = 160) -> str:
    """One sentence on one line. The monitor gives each of these a single row."""
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def parse_verdict(text: str, source: str = "agent") -> Verdict | None:
    """Lenient: whole text, else the outermost {...} block. Validates the verdict."""
    candidates = [text.strip()]
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])
    for cand in candidates:
        try:
            data = json.loads(cand)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        verdict = str(data.get("verdict", "")).strip().lower()
        verdict = _ALIASES.get(verdict, verdict)
        if verdict not in VERDICTS:
            continue
        fix = data.get("fix_first") or data.get("must_fix") or ""
        if isinstance(fix, list):
            fix = "\n".join(str(f) for f in fix)
        return Verdict(verdict, str(data.get("reply") or "").strip(), str(data.get("reason") or ""), source=source,
                       did=_one_line(data.get("did")), doing=_one_line(data.get("doing")), fix_first=str(fix).strip())
    return None


class AgentCritic:
    """One headless call with read-only tools. Falls back to rules, never blocks."""

    name = "agent"

    def __init__(
        self,
        harness: Harness,
        *,
        goal_text: str,
        cwd: Path,
        timeout: float = 600.0,
        model: str | None = None,
        transcript_path=None,
        log=None,
        rules: RulesCritic | None = None,
        retries: int = 1,
        name: str | None = None,
    ) -> None:
        self.harness = harness
        self.goal_text = goal_text
        self.cwd = cwd
        self.timeout = timeout
        self.model = model
        self.transcript_path = transcript_path or (lambda n, attempt: None)
        self.log = log or (lambda line: None)
        self.rules = rules or RulesCritic()
        self.retries = retries
        self.name = name or f"critic:{harness.name}"
        self.last_cost: float = 0.0
        self.last_usage = None

    def judge(self, s: Signals) -> Verdict:
        prompt = build_critic_prompt(goal_text=self.goal_text, s=s)
        self.last_cost = 0.0
        self.last_usage = None
        for attempt in range(self.retries + 1):
            try:
                result: Result = self.harness.run(
                    prompt if attempt == 0 else prompt + "\n\nYour previous reply was not valid JSON. Return ONLY the JSON object.",
                    cwd=self.cwd, timeout=self.timeout, model=self.model,
                    log_path=self.transcript_path(s.iteration, attempt),
                    tools="readonly", json_schema=VERDICT_SCHEMA, max_turns=CRITIC_MAX_TURNS,
                )
            except Exception as exc:
                self.log(f"critic attempt {attempt}: harness raised {exc!r}")
                continue
            self.last_cost += result.cost_usd or 0.0
            self.last_usage = result.usage or self.last_usage
            if not result.ok and not result.text.strip():
                self.log(f"critic attempt {attempt}: {result.error or 'no output'}")
                continue
            verdict = parse_verdict(result.text, source=self.name)
            if verdict is None:
                self.log(f"critic attempt {attempt}: unparseable reply: {result.text[:200]!r}")
                continue
            return verdict
        fallback = self.rules.judge(s)
        fallback.reason = f"rules fallback (critic agent failed): {fallback.reason}"
        return fallback
