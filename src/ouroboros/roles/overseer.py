"""The sleeping user. An agent overseer in front, deterministic rules behind."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Protocol

from ..harness.base import Harness, Result

VERDICTS = ("continue", "answer", "done_rejected", "done_accepted", "stuck", "revert")

# Claude needs a second turn to emit structured output even with tools off; 3 leaves slack.
OVERSEER_MAX_TURNS = 3

VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": list(VERDICTS)},
        "reply": {"type": "string"},
        "reason": {"type": "string"},
    },
    "required": ["verdict", "reply", "reason"],
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
REVERT_REPLY = (
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
    history: list[dict] = field(default_factory=list)
    memory: str = ""   # what the memory adapter says is true now: frontier, plan
    critique: str = ""  # the critic's verdict this iteration, if a critic ran

    def describe(self) -> str:
        lines = [
            f"- files changed this iteration: {'yes' if self.changed else 'no'}",
            f"- handoff recorded: {'yes' if self.recorded else 'NO'}",
            f"- iterations in a row with no changes: {self.no_change_streak}",
            f"- identical errors in a row: {self.error_streak}",
        ]
        if self.error:
            lines.append(f"- harness error: {self.error[:300]}")
        if self.critique:
            lines.append(f"- critic: {self.critique[:400]}")
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


class Overseer(Protocol):
    name: str

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
            return Verdict("continue", NO_RECORD_REPLY, "no record written")
        return Verdict("continue", "", "ok")


def load_template() -> str:
    text = resources.files("ouroboros.skills").joinpath("ouroboros-overseer/SKILL.md").read_text()
    if text.startswith("---"):
        _, _, rest = text.partition("\n---\n")
        text = rest.lstrip("\n")
    return text


def build_overseer_prompt(*, goal_text: str, s: Signals, template: str | None = None) -> str:
    tpl = template or load_template()
    hist = "\n".join(
        f"- #{h.get('iteration')}: {h.get('verdict')} — {h.get('reason')}" for h in s.history[-3:]
    ) or "(none yet)"
    return (
        tpl.replace("{iteration}", str(s.iteration))
        .replace("{goal}", goal_text.strip() or "(no goal)")
        .replace("{signals}", s.describe())
        .replace("{actor_output}", (s.text.strip() or "(empty)")[-6000:])
        .replace("{diff_stat}", s.diff_stat.strip() or "(no changes)")
        .replace("{history}", hist)
        .replace("{memory}", s.memory.strip() or "(no memory context)")
    )


def parse_verdict(text: str) -> Verdict | None:
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
        if verdict not in VERDICTS:
            continue
        return Verdict(verdict, str(data.get("reply") or ""), str(data.get("reason") or ""), source="agent")
    return None


class AgentOverseer:
    """A cheap headless call that answers as the user. Falls back to rules, never blocks."""

    name = "agent"

    def __init__(
        self,
        harness: Harness,
        *,
        goal_text: str,
        cwd: Path,
        timeout: float = 180.0,
        model: str | None = None,
        transcript_path=None,
        log=None,
        rules: RulesOverseer | None = None,
        retries: int = 1,
    ) -> None:
        self.harness = harness
        self.goal_text = goal_text
        self.cwd = cwd
        self.timeout = timeout
        self.model = model
        self.transcript_path = transcript_path or (lambda n, attempt: None)
        self.log = log or (lambda line: None)
        self.rules = rules or RulesOverseer()
        self.retries = retries
        self.last_cost: float = 0.0
        self.last_usage = None

    def judge(self, s: Signals) -> Verdict:
        prompt = build_overseer_prompt(goal_text=self.goal_text, s=s)
        self.last_cost = 0.0
        self.last_usage = None
        for attempt in range(self.retries + 1):
            try:
                result: Result = self.harness.run(
                    prompt if attempt == 0 else prompt + "\n\nYour previous reply was not valid JSON. Return ONLY the JSON object.",
                    cwd=self.cwd, timeout=self.timeout, model=self.model,
                    log_path=self.transcript_path(s.iteration, attempt),
                    tools="none", json_schema=VERDICT_SCHEMA, max_turns=OVERSEER_MAX_TURNS,
                )
            except Exception as exc:
                self.log(f"overseer attempt {attempt}: harness raised {exc!r}")
                continue
            self.last_cost += result.cost_usd or 0.0
            self.last_usage = result.usage or self.last_usage
            if not result.ok and not result.text.strip():
                self.log(f"overseer attempt {attempt}: {result.error or 'no output'}")
                continue
            verdict = parse_verdict(result.text)
            if verdict is None:
                self.log(f"overseer attempt {attempt}: unparseable reply: {result.text[:200]!r}")
                continue
            verdict.reply = verdict.reply.strip()
            return verdict
        fallback = self.rules.judge(s)
        fallback.reason = f"rules fallback (overseer agent failed): {fallback.reason}"
        return fallback
