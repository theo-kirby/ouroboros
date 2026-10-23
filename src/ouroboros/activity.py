"""What a running harness call is doing now, read from the end of its live transcript.

Claude's stream-json pairs each `tool_use` with a `tool_result` by id, and lists its
background shells in `background_tasks_changed`. Codex's JSONL pairs `item.started`
with `item.completed` by item id. A tool still open is what the agent waits on. Pi
has no recorded sample with call ids yet, so a Pi call reads as thinking.

Kinds, for the run page's ring:
  thinking  the model is writing; no tool is open
  tool      a short tool runs (read, edit, search, fetch, a subagent)
  command   a shell command runs
  wait      the agent waits on purpose: a monitor, a wake-up, a sleep or until loop
"""

from __future__ import annotations

import json
import re
from pathlib import Path

TAIL_BYTES = 600_000
WAIT_TOOLS = {"Monitor", "ScheduleWakeup"}
SHELL_TOOLS = {"Bash"}
_WAIT_CMD = re.compile(r"^\s*(sleep|until|while|wait)\b")
_SHELL_WRAP = re.compile(r"^/bin/(?:zsh|bash|sh) -lc ['\"]?")
ROLE_OF_STATE = {"work": "actor", "critique": "critic", "scribe": "scribe"}


def _one(text, limit: int = 160) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _claude_tool(name: str, inp: dict) -> tuple[str, str]:
    if name in SHELL_TOOLS:
        cmd = str(inp.get("command") or "")
        kind = "wait" if _WAIT_CMD.match(cmd) else "command"
        return kind, _one(inp.get("description") or cmd)
    if name in WAIT_TOOLS:
        return "wait", _one(inp.get("description") or inp.get("reason") or inp.get("command") or name)
    if name in ("TaskOutput", "BashOutput") and inp.get("block", True):
        return "wait", "a background shell"
    for key in ("description", "file_path", "path", "pattern", "query", "url", "skill"):
        if isinstance(inp.get(key), str):
            return "tool", _one(f"{name} {inp[key]}")
    return "tool", name


def current(lines) -> dict | None:
    """The call's activity from its transcript lines, or None once the call has ended."""
    open_: dict[str, dict] = {}
    background = 0
    done = False
    for line in lines:
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(d, dict):
            continue
        t = d.get("type")
        if t == "assistant":
            for b in (d.get("message") or {}).get("content") or []:
                if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("id"):
                    inp = b.get("input") if isinstance(b.get("input"), dict) else {}
                    kind, what = _claude_tool(str(b.get("name") or "?"), inp)
                    open_[b["id"]] = {"kind": kind, "what": what, "tool": b.get("name"), "id": b["id"]}
        elif t == "user":
            for b in (d.get("message") or {}).get("content") or []:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    open_.pop(b.get("tool_use_id"), None)
        elif t == "system" and d.get("subtype") == "background_tasks_changed":
            background = sum(1 for x in d.get("tasks") or [] if isinstance(x, dict) and x.get("task_type") == "local_bash")
        elif t == "item.started":
            item = d.get("item") or {}
            it, iid = item.get("type"), item.get("id")
            if it == "command_execution" and iid:
                cmd = _SHELL_WRAP.sub("", str(item.get("command") or "")).rstrip("'\"")
                open_[iid] = {"kind": "wait" if _WAIT_CMD.match(cmd) else "command", "what": _one(cmd), "tool": "shell", "id": iid}
            elif it in ("mcp_tool_call", "web_search") and iid:
                open_[iid] = {"kind": "tool", "what": _one(item.get("tool") or item.get("query") or it), "tool": it, "id": iid}
        elif t == "item.completed":
            open_.pop((d.get("item") or {}).get("id"), None)
        elif t in ("result", "turn.completed", "turn.failed", "agent_end"):
            done = True
        if t in ("assistant", "item.started", "turn.started", "agent_start"):
            done = False
    if done:
        return None
    if open_:
        act = dict(list(open_.values())[-1])
    else:
        act = {"kind": "thinking", "what": "", "tool": None, "id": None}
    act["background"] = background
    return act


def tail_lines(path: Path, size: int = TAIL_BYTES) -> list[str]:
    with open(path, "rb") as f:
        f.seek(0, 2)
        end = f.tell()
        f.seek(max(0, end - size))
        data = f.read().decode("utf-8", errors="replace")
    lines = data.splitlines()
    return lines[1:] if end > size else lines   # the first line of a tail is cut


def live(run_dir: Path, iteration: int, state: str) -> dict | None:
    """The activity of the call that runs now: the newest transcript of this iteration's role. Never raises."""
    try:
        role = ROLE_OF_STATE.get(state)
        if not role:
            return None
        paths = list((Path(run_dir) / "transcripts").glob(f"{int(iteration):04d}-{role}*.json"))
        if not paths:
            return {"kind": "thinking", "what": "", "tool": None, "id": None, "background": 0}
        return current(tail_lines(max(paths, key=lambda p: p.stat().st_mtime)))
    except Exception:
        return None
