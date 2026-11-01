"""The writer of the worker-writer loop: its prompt, its call, and its JSON reply.

Fresh every turn and the only memory writer. Turn 0 seeds the memory from the
charter and writes the first prompt; every later turn reviews the worker's turn,
records and folds it, and writes the next prompt. Its reply is strict JSON: the
turn's outcome, a done flag and the next prompt. The prompt text is the
`ouroboros-writer` skill's.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from ..harness.base import Result
from ..memory.graph import section, skill
from .critic import _one_line
from .scribe import Scribe

SKILL = "ouroboros-writer"
FINDINGS_SKILL = "ouroboros-findings"
FINDINGS_LIMIT = 16000   # the findings file as the writer sees it; the page has it whole
TURNS = ("ok", "failed", "stuck")
# What a turn means to the rest of the loop (stop rules, backoff, the view): the
# verdict vocabulary the standard loop's critic uses. A failed turn reads as a
# reject with nothing reverted, the standard loop's fix-forward case.
VERDICT_OF_TURN = {"ok": "continue", "failed": "reject", "stuck": "stuck"}
DIFF_LIMIT = 24000
TOUCHED_MAX = 6
TOUCHED_CHARS = 2500

WRITER_SCHEMA = {
    "type": "object",
    "properties": {
        "turn": {"type": "string", "enum": list(TURNS)},
        "done": {"type": "boolean"},
        "reason": {"type": "string"},
        "did": {"type": "string"},
        "doing": {"type": "string"},
        "target": {"type": "string"},
        "prompt": {"type": "string"},
    },
    "required": ["turn", "done", "reason", "did", "doing", "target", "prompt"],
}


@dataclass
class WriterReply:
    turn: str
    done: bool
    reason: str
    did: str = ""
    doing: str = ""
    prompt: str = ""
    target: str = ""   # the claim id the next unit works on; "" lets the worker pick from the frontier

    @property
    def verdict(self) -> str:
        return "done_accepted" if self.done else VERDICT_OF_TURN[self.turn]


def parse_reply(text: str) -> WriterReply | None:
    """Lenient about where the JSON sits, strict about what it says. None when it is not a reply."""
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
        if not isinstance(data, dict):
            continue
        turn = str(data.get("turn") or "").strip().lower()
        done = data.get("done")
        if isinstance(done, str):
            done = {"true": True, "false": False}.get(done.strip().lower())
        if turn not in TURNS or not isinstance(done, bool) or not isinstance(data.get("prompt", ""), str):
            continue
        return WriterReply(turn=turn, done=done, reason=_one_line(data.get("reason"), 400),
                           did=_one_line(data.get("did")), doing=_one_line(data.get("doing")),
                           prompt=str(data.get("prompt") or "").strip(),
                           target=str(data.get("target") or "").strip().strip("`").removeprefix("NEW ").strip())
    return None


def build_writer_prompt(*, iteration: int, handoff: str, where: str, goal_text: str, charter_path: str,
                        fold: str, zero: bool = False, orient: str = "", fix_first: str = "",
                        signals: str = "", previous_prompt: str = "", report: str = "", commit: str = "",
                        diff_stat: str = "", diff: str = "", touched: str = "", frontier: str = "",
                        history: str = "", previous_target: str = "", findings_path: str = "",
                        assets_dir: str = "", findings: str = "", findings_problems: str = "") -> str:
    """Turn 0 (`zero`) seeds the memory; every other turn reviews `iteration`'s worker turn."""
    s = skill(SKILL)
    if zero:
        task = section(s, "turn zero")
        context = section(s, "context zero").replace("{orient}", orient.strip() or "(empty)")
    else:
        block = ""
        if findings_path:
            block = (section(skill(FINDINGS_SKILL), "writer")
                     .replace("{findings_path}", findings_path).replace("{assets_dir}", assets_dir or "assets")
                     .replace("{summary_path}", str(__import__("pathlib").Path(findings_path).parent / "summary.txt"))
                     .replace("{problems}", findings_problems.strip() or "none.")
                     .replace("{findings}", f"```json\n{findings.strip()[:FINDINGS_LIMIT]}\n```" if findings.strip()
                              else "(no findings yet: the file does not exist; write it when the run first knows something)"))
        task = (section(s, "review")
                .replace("{findings_section}", block)
                .replace("{fix_first}", f"**Fix first** (the memory's own check failed after the last turn):\n\n{fix_first.strip()}"
                         if fix_first.strip() else ""))
        context = (section(s, "context review")
                   .replace("{signals}", signals.strip() or "(none)")
                   .replace("{previous_prompt}", previous_prompt.strip() or "(no prose step: the claim below was the step, "
                            "or the worker chose from the memory's frontier)")
                   .replace("{previous_target}", f"`{previous_target}`" if previous_target else
                            "(none: the worker chose from the frontier; its report says which)")
                   .replace("{report}", report.strip() or "(the worker wrote no report)")
                   .replace("{commit}", commit or "none")
                   .replace("{diff_stat}", diff_stat.strip()[:4000] or "(no change)")
                   .replace("{diff}", diff.strip()[:DIFF_LIMIT] or "(no diff)")
                   .replace("{touched}", touched.strip() or "(the report names no claim the memory holds)")
                   .replace("{frontier}", frontier.strip() or "(no claims yet)")
                   .replace("{history}", history.strip() or "(none yet)"))
    task = task.replace("{fold}", fold.strip())
    return (section(s, "role")
            .replace("{task}", task)
            .replace("{handoff}", section(s, f"handoff {handoff}").replace("{check_the_step}", section(s, "check the step")))
            .replace("{context}", context)
            .replace("{where}", where)
            .replace("{worker_turn}", str(iteration))
            .replace("{iteration}", str(iteration))
            .replace("{charter_path}", charter_path)
            .replace("{goal}", (goal_text or "").strip()[:12000] or "(none)"))


def touched_claims(graph, ids: list[str], text: str = "") -> str:
    """The claims the worker touched, whole: the ids its report names, then any claim id
    its message mentions in backticks. At most TOUCHED_MAX, each clipped."""
    if graph is None or not getattr(graph, "ok", False):
        return ""
    known = {c.get("slug"): c for c in graph.claims("state")}
    wanted = [i for i in ids if i in known]
    for m in re.finditer(r"`([a-z0-9]+(?:-[a-z0-9]+)*)`", text or ""):
        if m.group(1) in known and m.group(1) not in wanted:
            wanted.append(m.group(1))
    out = []
    for cid in wanted[:TOUCHED_MAX]:
        c = known[cid]
        body = (c.get("content") or "").strip()
        if len(body) > TOUCHED_CHARS:
            body = body[:TOUCHED_CHARS] + "\n… (`ouroboros mem show " + cid + "` for the rest)"
        out.append(f"#### {c.get('title', '')} (`{cid}`, {c.get('status') or 'no status'})\n\n{body}")
    return "\n\n".join(out)


WRITER_MAX_TURNS = 80   # the scribe's book-keeping plus a review: room to read the diff and run a check


class Writer(Scribe):
    """One headless call with full tools in the repo, like the scribe's. Its failures are survived by the engine.

    A turn's call asks the harness for WRITER_SCHEMA, so the reply is strict JSON; the
    report pass at the end ("final") writes files and replies in prose."""

    name = "writer"

    def run(self, prompt: str, iteration: int, attempt: int | str = 0, *, timeout: float | None = None):
        try:
            return self.harness.run(prompt, cwd=self.cwd, timeout=timeout or self.timeout, model=self.model,
                                    log_path=self.transcript_path(iteration, attempt), max_turns=WRITER_MAX_TURNS,
                                    json_schema=None if attempt == "final" else WRITER_SCHEMA)
        except Exception as exc:   # the engine falls back to rules and a loop-written record; the loop goes on
            self.log(f"writer harness raised {exc!r}")
            return Result(exit_code=-1, error=f"writer raised {exc!r}")
