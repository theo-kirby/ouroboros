"""The charter: the human-owned goal document, parsed into the parts the loop needs."""

from __future__ import annotations

import re

_H2 = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)
_CHECK = re.compile(r"^\s*-\s*\[([ xX])\]\s*(.*)$")
_RUNG = re.compile(r"^\s*-\s*\*\*(?:the\s+)?next\s+(hour|day|week|month|year)[:*]*\*\*:?\s*(.*)$", re.IGNORECASE)
_WORD = re.compile(r"[a-z0-9]+")
_STOP = {"a", "an", "the", "and", "or", "of", "to", "in", "on", "is", "are", "with", "for", "by", "at", "its", "it"}

RUNGS = ("hour", "day", "week", "month", "year")


def sections(goal_text: str) -> dict[str, str]:
    """{lowercased heading: body} for every `## ` section of the charter."""
    out: dict[str, str] = {}
    matches = list(_H2.finditer(goal_text))
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(goal_text)
        out[m.group(1).strip().lower()] = goal_text[m.end():end].strip("\n")
    return out


def section(goal_text: str, name: str) -> str:
    for key, body in sections(goal_text).items():
        if key.startswith(name.lower()):
            return body
    return ""


def done_criteria(goal_text: str, *, include_checked: bool = False) -> list[str]:
    """Unchecked checkbox lines under `## Done criteria`, continuation lines joined, in order.

    A ticked box is the human saying "this is met": it declares no gap.
    """
    body = section(goal_text, "done criteria")
    items: list[tuple[bool, str]] = []
    for line in body.splitlines():
        m = _CHECK.match(line)
        if m:
            items.append((m.group(1) != " ", m.group(2).strip()))
        elif items and line.startswith((" ", "\t")) and line.strip():
            done, text = items[-1]
            items[-1] = (done, f"{text} {line.strip()}")
    return [re.sub(r"\s+", " ", t) for done, t in items if t and (include_checked or not done)]


def gap_name(criterion: str, *, prefix: str = "gap") -> str:
    """A stable kebab name for a criterion: `gap-opening-blend-hydrates-model`."""
    words = [w for w in _WORD.findall(criterion.lower().replace("`", " ")) if w not in _STOP]
    return "-".join([prefix, *words[:5]]) if words else prefix


def horizon_ladder(goal_text: str) -> dict[str, str]:
    """{rung: text} from `## Horizon ladder`; continuation lines joined."""
    body = section(goal_text, "horizon ladder")
    ladder: dict[str, str] = {}
    current: str | None = None
    for line in body.splitlines():
        m = _RUNG.match(line)
        if m:
            current = m.group(1).lower()
            ladder[current] = m.group(2).strip()
        elif current and line.strip() and line.startswith((" ", "\t")):
            ladder[current] = f"{ladder[current]} {line.strip()}".strip()
    return {k: re.sub(r"\s+", " ", v) for k, v in ladder.items()}


def exhaustion_policy(goal_text: str) -> str:
    body = section(goal_text, "exhaustion").lower()
    for word in ("report_done", "creative", "maintain"):
        if word in body:
            return word
    return "creative"


def mission(goal_text: str) -> str:
    return section(goal_text, "mission")
