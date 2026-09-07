"""The critic: one read-only call that grades an iteration's diff. Fails open (accept)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

from ..harness.base import Harness, Result
from .. import goal as charter

CRITIC_MAX_TURNS = 12
DIFF_LIMIT = 24000

CRITIQUE_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["accept", "reject"]},
        "reasons": {"type": "array", "items": {"type": "string"}},
        "must_fix": {"type": "string"},
    },
    "required": ["verdict", "reasons", "must_fix"],
}


@dataclass
class Critique:
    verdict: str            # accept | reject
    reasons: list[str] = field(default_factory=list)
    must_fix: str = ""
    source: str = "critic"

    @property
    def rejected(self) -> bool:
        return self.verdict == "reject"

    def describe(self) -> str:
        why = "; ".join(self.reasons)[:400]
        return f"{self.source}: {self.verdict}" + (f" ({why})" if why else "") + (f" must_fix: {self.must_fix[:300]}" if self.must_fix else "")


def load_template() -> str:
    text = resources.files("ouroboros.skills").joinpath("ouroboros-critic/SKILL.md").read_text()
    return text.split("\n---\n", 1)[1] if text.startswith("---") else text


def build_critic_prompt(*, goal_text: str, iteration: int, actor_output: str, diff: str, template: str | None = None) -> str:
    bar = "\n\n".join(
        f"### {name}\n\n{charter.section(goal_text, name)}"
        for name in ("Quality bar", "Constraints") if charter.section(goal_text, name)
    ) or goal_text.strip()[:6000] or "(no quality bar; grade against ordinary engineering judgement)"
    return (
        (template or load_template())
        .replace("{iteration}", str(iteration))
        .replace("{quality_bar}", bar)
        .replace("{actor_output}", (actor_output.strip() or "(empty)")[-4000:])
        .replace("{diff}", diff.strip()[:DIFF_LIMIT] or "(no diff)")
    )


def parse_critique(text: str, source: str = "critic") -> Critique | None:
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
        if verdict not in ("accept", "reject"):
            continue
        reasons = data.get("reasons") or []
        if isinstance(reasons, str):
            reasons = [reasons]
        return Critique(verdict, [str(r) for r in reasons][:5], str(data.get("must_fix") or "").strip(), source=source)
    return None


class Critic:
    def __init__(self, harness: Harness, *, goal_text: str, cwd: Path, timeout: float = 900.0, model: str | None = None,
                 transcript_path=None, log=None, name: str | None = None) -> None:
        self.harness = harness
        self.goal_text = goal_text
        self.cwd = cwd
        self.timeout = timeout
        self.model = model
        self.transcript_path = transcript_path or (lambda n, attempt: None)
        self.log = log or (lambda line: None)
        self.name = name or f"critic:{harness.name}"
        self.last_cost: float = 0.0
        self.last_usage = None

    def grade(self, *, iteration: int, actor_output: str, diff: str) -> Critique:
        prompt = build_critic_prompt(goal_text=self.goal_text, iteration=iteration, actor_output=actor_output, diff=diff)
        self.last_cost = 0.0
        self.last_usage = None
        for attempt in range(2):
            try:
                result: Result = self.harness.run(
                    prompt if attempt == 0 else prompt + "\n\nYour previous reply was not valid JSON. Return ONLY the JSON object.",
                    cwd=self.cwd, timeout=self.timeout, model=self.model, log_path=self.transcript_path(iteration, attempt),
                    tools="readonly", json_schema=CRITIQUE_SCHEMA, max_turns=CRITIC_MAX_TURNS,
                )
            except Exception as exc:
                self.log(f"{self.name} attempt {attempt}: harness raised {exc!r}")
                continue
            self.last_cost += result.cost_usd or 0.0
            self.last_usage = result.usage or self.last_usage
            if not result.text.strip():
                self.log(f"{self.name} attempt {attempt}: {result.error or 'no output'}")
                continue
            critique = parse_critique(result.text, source=self.name)
            if critique is None:
                self.log(f"{self.name} attempt {attempt}: unparseable reply: {result.text[:200]!r}")
                continue
            return critique
        return Critique("accept", ["critic failed; accepting by default"], "", source=self.name)


class Council:
    """Several critics; a majority of rejects rejects. Ties accept."""

    def __init__(self, critics: list[Critic]) -> None:
        self.critics = critics
        self.last_cost: float = 0.0
        self.last: list[Critique] = []

    @property
    def name(self) -> str:
        return "council(" + ", ".join(c.name for c in self.critics) + ")"

    def grade(self, *, iteration: int, actor_output: str, diff: str) -> Critique:
        self.last = [c.grade(iteration=iteration, actor_output=actor_output, diff=diff) for c in self.critics]
        self.last_cost = sum(c.last_cost for c in self.critics)
        self.last_usage = next((c.last_usage for c in reversed(self.critics) if c.last_usage), None)
        rejects = [c for c in self.last if c.rejected]
        if len(rejects) * 2 > len(self.last):
            reasons = [r for c in rejects for r in c.reasons][:5]
            must_fix = "\n".join(dict.fromkeys(c.must_fix for c in rejects if c.must_fix))
            return Critique("reject", reasons, must_fix, source=f"{len(rejects)}/{len(self.last)} critics")
        return Critique("accept", [f"{len(self.last) - len(rejects)}/{len(self.last)} accepted"], "", source=self.name)
