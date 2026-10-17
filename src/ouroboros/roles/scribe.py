"""The scribe: after the critic, it records the unit, folds when due, and keeps the view.

It is the run's book-keeper, not a judge: the critic has already decided, and the
scribe writes down what happened, including a rejected unit as a dead end. Because
it runs after any revert, its record is never undone by one.
"""

from __future__ import annotations

import re
from pathlib import Path

from ..harness.base import Harness, Result
from ..memory.graph import skill, section

SCRIBE_MAX_TURNS = 60


def report_of(text: str) -> str:
    """The actor's report: its final message from the first `## What` on, else all of it."""
    text = (text or "").strip()
    m = re.search(r"^## What\s*$", text, re.MULTILINE)
    return text[m.start():] if m else text


def summary_of(text: str, limit: int = 72) -> str:
    """One line for a commit message: the report's `## What`, else its first line."""
    what = section(report_of(text), "What")
    for line in (what or text or "").splitlines():
        line = re.sub(r"^[-*#\s]+", "", line).strip()
        if line:
            return line if len(line) <= limit else line[: limit - 1] + "…"
    return ""


def build_scribe_prompt(*, iteration: int, goal_text: str, where: str, report: str, verdict: str,
                        fix_first: str, diff_stat: str, commit: str, fold: str | None,
                        view_dir: str, story_path: str, assets_dir: str) -> str:
    return (
        skill("ouroboros-scribe")
        .replace("{iteration}", str(iteration))
        .replace("{where}", where)
        .replace("{fix_first}", fix_first.strip() or "The critic found nothing to fix.")
        .replace("{fold}", fold or "No fold is due this iteration. Do not fold.")
        .replace("{view_dir}", view_dir)
        .replace("{story_path}", story_path)
        .replace("{assets_dir}", assets_dir)
        .replace("{verdict}", verdict)
        .replace("{commit}", commit or "none")
        .replace("{diff_stat}", (diff_stat or "").strip()[:4000] or "(no change)")
        .replace("{report}", report_of(report)[:12000] or "(the actor wrote no report)")
        .replace("{goal}", (goal_text or "").strip()[:4000] or "(none)")
    )


class Scribe:
    """One headless call with full tools in the repo. Its failures are survived by the engine."""

    name = "scribe"

    def __init__(self, harness: Harness, *, cwd: Path, timeout: float = 1200.0, model: str | None = None,
                 transcript_path=None, log=None) -> None:
        self.harness = harness
        self.cwd = cwd
        self.timeout = timeout
        self.model = model
        self.transcript_path = transcript_path or (lambda n, attempt: None)
        self.log = log or (lambda line: None)

    def run(self, prompt: str, iteration: int) -> Result:
        try:
            return self.harness.run(prompt, cwd=self.cwd, timeout=self.timeout, model=self.model,
                                    log_path=self.transcript_path(iteration, 0), max_turns=SCRIBE_MAX_TURNS)
        except Exception as exc:   # the engine writes a fallback record; the loop goes on
            self.log(f"scribe harness raised {exc!r}")
            return Result(exit_code=-1, error=f"scribe raised {exc!r}")
