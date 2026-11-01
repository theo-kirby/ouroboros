"""The worker of the worker-writer loop: its prompt, and the report it ends with.

The worker's prompt is the writer's next step and the memory's brief (as `handoff`
says), then a footer the engine appends and the writer cannot leave out: the
charter's path, where AGENTS.md is, the memory's read verbs, commit, and the report
shape. Everything fixed comes from the `ouroboros-worker` skill.
"""

from __future__ import annotations

import json
from pathlib import Path

from ..memory.graph import section, skill
from .scribe import summary_of

SKILL = "ouroboros-worker"


def agents_path(repo: Path) -> str | None:
    """The target repo's agent instructions, relative to its root, or None when it has none."""
    for name in ("AGENTS.md", "agents.md"):
        if (repo / name).is_file():
            return name
    return None


def footer(*, iteration: int, charter_path: str, agents: str | None, orient: str) -> str:
    """The part of every worker prompt the loop owns."""
    s = skill(SKILL)
    agents_line = (section(s, "agents").replace("{agents_path}", agents) if agents else section(s, "no agents"))
    return (section(s, "footer")
            .replace("{orient}", orient)
            .replace("{agents}", agents_line)
            .replace("{charter_path}", charter_path)
            .replace("{iteration}", str(iteration)))


def build_worker_prompt(*, iteration: int, handoff: str, prompt: str | None, brief: str, notes: str = "",
                        charter_path: str, agents: str | None, target: str = "") -> str:
    """notes, the target claim, the next step (unless `graph`), the brief (unless `prompt`), the footer.

    `target` is the claim the writer named for this unit, whole (`graph.target_text`):
    under `graph` it is the step, its `Next:` line saying what to do. With neither a
    target nor a written step (the writer failed, or named none), the brief is always
    there and the worker is told to pick from its frontier."""
    s = skill(SKILL)
    has_prompt = handoff != "graph" and bool((prompt or "").strip())
    has_target = bool((target or "").strip())
    parts = []
    if notes.strip():
        parts.append(section(s, "from the loop").replace("{notes}", notes.strip()))
    if has_target:
        parts.append(section(s, "target").replace("{target}", target.strip()))
    if has_prompt:
        parts.append(section(s, "next step").replace("{prompt}", (prompt or "").strip()))
    if handoff != "prompt" or not has_prompt:
        parts.append(brief.strip())
    if has_prompt:
        orient = "orient both" if handoff == "both" else "orient prompt"
    elif has_target:
        orient = "orient target"
    else:
        orient = "orient graph" if handoff == "graph" else "orient none"
    parts.append(footer(iteration=iteration, charter_path=charter_path, agents=agents, orient=section(s, orient)))
    return "\n\n".join(p for p in parts if p) + "\n"


REPORT_KEYS = ("did", "target", "files", "result", "evidence", "claims", "failed", "blocked", "assumed", "next")


def parse_report(text: str) -> dict | None:
    """The worker's JSON report: the whole text, else the outermost {...}. None when there is none."""
    text = (text or "").strip()
    candidates = [text]
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start:end + 1])
    for cand in candidates:
        try:
            data = json.loads(cand)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(data, dict) and any(k in data for k in REPORT_KEYS):
            return data
    return None


def report_summary(text: str, limit: int = 72) -> str:
    """One line for the commit message: the report's `did`, else the text's first line."""
    report = parse_report(text)
    did = " ".join(str((report or {}).get("did") or "").split())
    if did:
        return did if len(did) <= limit else did[: limit - 1] + "…"
    return summary_of(text, limit)


def report_for_writer(text: str, limit: int = 12000) -> str:
    """The report as the writer reads it: the JSON pretty-printed, else the raw end of the message."""
    report = parse_report(text)
    if report is not None:
        return json.dumps(report, indent=2, ensure_ascii=False)[:limit]
    raw = (text or "").strip()
    if not raw:
        return "(the worker wrote no report)"
    return "(the worker's report is not the JSON asked for; its last message, verbatim:)\n\n" + raw[-limit:]


def claimed_ids(text: str) -> list[str]:
    """The claim ids the report names in `claims`, `NEW` stripped, in order."""
    report = parse_report(text) or {}
    out: list[str] = []
    first = str(report.get("target") or "").strip().strip("`")
    if first:
        out.append(first)
    for item in report.get("claims") or []:
        cid = item.get("claim") if isinstance(item, dict) else item
        cid = str(cid or "").strip().removeprefix("NEW ").strip().strip("`")
        if cid and cid not in out:
            out.append(cid)
    return out
