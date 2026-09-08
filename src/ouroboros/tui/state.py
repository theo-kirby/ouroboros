"""What the TUI shows, read from the run directory and the repo. No process talks to the loop.

`load_snapshot` is called by the poller thread every couple of seconds. It reads
status.json, the two JSONL logs, the tail of loop.log, the newest transcript
(stream-json for Claude, JSONL events for Codex and Pi), the plan file, and the
process tree under the loop's pid.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

FEED_BYTES = 400_000
FEED_EVENTS = 120
LOG_LINES = 40

STAGES = ("actor", "commit", "critic", "overseer", "maintainer", "planner")
STATE_TO_STAGE = {
    "work": "actor", "critique": "critic", "oversee": "overseer", "reconcile": "maintainer", "plan": "planner",
}
STEP_TO_STAGE = {"actor": "actor", "critique": "critic", "oversee": "overseer", "reconcile": "maintainer", "plan": "planner"}


# ---------------------------------------------------------------- transcript events
@dataclass
class Event:
    kind: str    # text | think | tool | tool_out | result | error | init
    text: str


def _short_input(name: str, inp) -> str:
    if not isinstance(inp, dict):
        return str(inp)[:200]
    for key in ("command", "cmd", "file_path", "path", "pattern", "query", "url", "prompt", "description"):
        if key in inp and isinstance(inp[key], str):
            return inp[key]
    if "notebook_path" in inp:
        return str(inp["notebook_path"])
    return ", ".join(f"{k}={str(v)[:40]}" for k, v in list(inp.items())[:3])


def _claude_event(d: dict) -> list[Event]:
    t = d.get("type")
    out: list[Event] = []
    if t == "assistant":
        for b in (d.get("message") or {}).get("content") or []:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "text" and b.get("text", "").strip():
                out.append(Event("text", b["text"]))
            elif b.get("type") == "tool_use":
                out.append(Event("tool", f"{b.get('name', '?')}  {_short_input(b.get('name', ''), b.get('input'))}"))
            elif b.get("type") == "thinking" and b.get("thinking", "").strip():
                out.append(Event("think", b["thinking"]))
    elif t == "user":
        for b in (d.get("message") or {}).get("content") or []:
            if isinstance(b, dict) and b.get("type") == "tool_result":
                c = b.get("content")
                if isinstance(c, list):
                    c = " ".join(x.get("text", "") for x in c if isinstance(x, dict))
                text = str(c or "").strip()
                if b.get("is_error"):
                    out.append(Event("error", text or "tool error"))
                elif text:
                    out.append(Event("tool_out", text))
    elif t == "system" and d.get("subtype") == "init":
        out.append(Event("init", f"session {str(d.get('session_id', ''))[:8]}  model {d.get('model', '?')}"))
    elif t == "result":
        # `total_cost_usd` is priced from token counts at API list prices even on a
        # subscription that bills a flat fee, so it is not money. The usage row on the
        # run strip carries what this really spends. Pi, billed per call, keeps its own.
        note = f"{d.get('subtype', 'result')}  turns {d.get('num_turns', '?')}"
        out.append(Event("error" if d.get("is_error") else "result", note))
    return out


def _codex_event(d: dict) -> list[Event]:
    t = d.get("type")
    if t == "item.completed":
        item = d.get("item") or {}
        it = item.get("type")
        if it == "agent_message" and item.get("text"):
            return [Event("text", item["text"])]
        if it == "reasoning" and item.get("text"):
            return [Event("think", item["text"])]
        if it == "command_execution":
            code = item.get("exit_code")
            tail = f"  → exit {code}" if code not in (None, 0) else ""
            cmd = re.sub(r"^/bin/(?:zsh|bash|sh) -lc ['\"]?", "", str(item.get("command", "?"))).rstrip("'\"")
            return [Event("tool", f"$ {cmd}{tail}")]
        if it == "file_change":
            paths = [c.get("path", "?") for c in item.get("changes") or [] if isinstance(c, dict)]
            return [Event("tool", "edit  " + ", ".join(paths[:4]))]
        if it in ("mcp_tool_call", "web_search"):
            return [Event("tool", f"{it}  {item.get('server', '')}{item.get('tool', '')}{item.get('query', '')}")]
        return []
    if t == "error":
        return [Event("error", str(d.get("message", "error")))]
    if t == "turn.failed":
        return [Event("error", str((d.get("error") or {}).get("message", "turn failed")))]
    if t == "turn.completed":
        u = d.get("usage") or {}
        return [Event("result", f"turn completed  in {u.get('input_tokens', '?')} / out {u.get('output_tokens', '?')} tokens")]
    if t == "thread.started":
        return [Event("init", f"thread {str(d.get('thread_id', ''))[:8]}")]
    return []


def _pi_event(d: dict) -> list[Event]:
    t = d.get("type")
    out: list[Event] = []
    if t == "session":
        out.append(Event("init", f"session {str(d.get('id', ''))[:8]}"))
    elif t == "message_end":
        msg = d.get("message") or {}
        if msg.get("role") == "assistant":
            for c in msg.get("content") or []:
                if not isinstance(c, dict):
                    continue
                if c.get("type") == "text" and c.get("text", "").strip():
                    out.append(Event("text", c["text"]))
                elif c.get("type") == "toolCall":
                    out.append(Event("tool", f"{c.get('name', '?')}  {_short_input(c.get('name', ''), c.get('arguments'))}"))
            if msg.get("stopReason") == "error":
                out.append(Event("error", str(msg.get("errorMessage") or "provider error")))
            cost = ((msg.get("usage") or {}).get("cost") or {}).get("total")
            if isinstance(cost, (int, float)):
                out.append(Event("result", f"message end  ${cost:.4f}"))
    elif t == "tool_execution_start":
        out.append(Event("tool", f"{d.get('toolName', '?')}  {_short_input(d.get('toolName', ''), d.get('args'))}"))
    elif t == "auto_retry_start":
        out.append(Event("error", f"retry {d.get('attempt', '?')}: {d.get('error', '')}"))
    return out


def parse_events(lines) -> list[Event]:
    """Events from any harness's transcript lines; the shape decides the parser."""
    out: list[Event] = []
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
        t = d.get("type", "")
        if t in ("assistant", "user", "system", "result") and ("message" in d or t in ("system", "result")):
            out.extend(_claude_event(d))
        elif t.startswith(("item.", "turn.", "thread.")) or (t == "error" and "message" in d and "item" not in d):
            out.extend(_codex_event(d))
        else:
            out.extend(_pi_event(d))
    return out


# ---------------------------------------------------------------- per-run derived data
@dataclass
class StageStat:
    count: int = 0
    total: float = 0.0
    last: float = 0.0

    @property
    def avg(self) -> float:
        return self.total / self.count if self.count else 0.0


@dataclass
class Iteration:
    n: int
    changed: bool = False
    recorded: bool = False
    verdict: str = ""
    critique: str = ""
    reverted: bool = False
    actor_seconds: float = 0.0
    cost: float = 0.0
    bet: str | None = None
    error: str | None = None


ROLE_ORDER = ("actor", "critic", "overseer", "maintainer", "planner")


@dataclass
class HarnessRow:
    """One harness: the jobs it holds, what it drives them with, what it has used."""

    name: str
    roles: list[str] = field(default_factory=list)        # roles it is first choice for
    backs: list[str] = field(default_factory=list)        # roles it only stands in for
    models: list[str] = field(default_factory=list)       # distinct, in role order
    window: str = ""                                      # the window the numbers below describe
    utilization: float | None = None                      # 0..1 now
    delta: float | None = None                            # points added since the run began
    resets_at: float | None = None                        # epoch the long window empties
    short_window: str = ""                                # the sub-day window, when the harness reports one
    short_utilization: float | None = None
    short_resets_at: float | None = None
    short_expired: bool = False
    expired: bool = False
    limited: bool = False
    limit_note: str = ""

    @property
    def idle(self) -> bool:
        """Configured only as a fallback and never called on."""
        return not self.roles and self.utilization is None


def _longest_window(snap: dict) -> tuple[str, dict] | None:
    """The window worth showing: the longest real one, ignoring overage meters."""
    best = None
    for name, w in (snap.get("windows") or {}).items():
        if name.endswith("overage_included"):
            continue
        if best is None or (w.get("minutes") or 0) > (best[1].get("minutes") or 0):
            best = (name, w)
    return best


def _shortest_window(snap: dict) -> tuple[str, dict] | None:
    """The sub-day window, if the harness reports one.

    It is the one that decides whether the run stalls in the next hour, while the long
    window decides whether it finishes the week. Neither substitutes for the other.
    """
    best = None
    for name, w in (snap.get("windows") or {}).items():
        if name.endswith("overage_included"):
            continue
        minutes = w.get("minutes") or 0
        if not 0 < minutes < 1440:
            continue
        if best is None or minutes < (best[1].get("minutes") or 0):
            best = (name, w)
    return best


def harness_roster(roles: dict, usage: dict, limited: dict | None = None, *, now: float | None = None) -> list[HarnessRow]:
    """Invert the role config into one row per harness, joined to what it has used.

    Roles map harness -> job; the panel wants job -> harness, so that a night spent
    on the fallback reads as one line rather than five.
    """
    now = time.time() if now is None else now
    limited = limited or {}
    rows: dict[str, HarnessRow] = {}

    def row(name: str) -> HarnessRow:
        return rows.setdefault(name, HarnessRow(name=name, limited=name in limited,
                                                limit_note=str(limited.get(name, "")).split(" (")[0]))

    for role in ROLE_ORDER:
        chain = roles.get(role) or []
        for i, entry in enumerate(chain):
            harness, model = (entry if isinstance(entry, (list, tuple)) else (entry, None))[:2]
            if not harness:
                continue
            r = row(harness)
            (r.roles if i == 0 else r.backs).append(role)
            if i == 0 and model and model not in r.models:
                r.models.append(model)

    for harness, snap in (usage or {}).items():
        r = row(harness)
        best = _longest_window(snap or {})
        if not best:
            continue
        r.window, w = best
        r.utilization = w.get("utilization")
        r.resets_at = w.get("resets_at")
        r.expired = r.resets_at is not None and r.resets_at <= now
        if r.expired:
            r.utilization = None
        first = ((snap or {}).get("first_windows") or {}).get(r.window)
        if first is not None and r.utilization is not None:
            r.delta = (r.utilization - first) * 100
        short = _shortest_window(snap or {})
        if short:
            r.short_window, sw = short
            r.short_utilization = sw.get("utilization")
            r.short_resets_at = sw.get("resets_at")
            r.short_expired = r.short_resets_at is not None and r.short_resets_at <= now
            if r.short_expired:
                r.short_utilization = None

    # Working harnesses first, then ones that only ever stood by.
    return sorted(rows.values(), key=lambda r: (r.idle, not r.roles, r.name))

@dataclass
class Snapshot:
    now: float
    status: dict | None
    alive: bool
    pid: int | None
    iterations: list[Iteration]
    stages: dict[str, StageStat]
    stage: str
    stage_since: float
    decisions: list[dict]
    feed: list[Event]
    feed_role: str
    feed_age: float | None
    log_tail: list[str]
    plan_short: list[str]
    needs_human: str | None
    loadavg: tuple[float, float, float]
    procs: int
    cpu_pct: float
    rss_mb: float
    stop_after_s: float | None
    max_iterations: int | None
    chains: dict[str, list[str]]
    mode: str
    roles: dict = field(default_factory=dict)   # role -> [(harness, model), ...]
    switches: int = 0
    reconciles: int = 0
    plans: int = 0
    frontier: dict[str, int] = field(default_factory=dict)

    @property
    def elapsed(self) -> float:
        return float((self.status or {}).get("elapsed_s") or 0)

    @property
    def cost(self) -> float:
        return float((self.status or {}).get("cost_usd") or 0)

    @property
    def usage(self) -> dict:
        """Per-harness subscription windows, as the engine last wrote them."""
        return (self.status or {}).get("usage") or {}

    @property
    def money(self) -> dict:
        """Harnesses billed in money rather than rationed by a window."""
        return (self.status or {}).get("money") or {}

    @property
    def usage_parts(self) -> list[tuple[str, float | None]]:
        return usage_parts(self.usage, self.money, self.now)


_SHORT_WINDOW = {"five_hour": "5h", "seven_day": "7d", "daily": "24h", "hourly": "1h"}


def usage_parts(usage: dict, money: dict, now: float) -> list[tuple[str, float | None]]:
    """`[("claude 7d 54% +19", 0.54), ("pi $2.50", None)]`, longest window first.

    What a night costs is the slice of a rationing window it burns, so that is what the
    run strip shows. A window is a gauge that drains on its own, so one whose reset has
    passed reads as empty rather than as whatever it said before it emptied. The float
    is the fill, for the gradient; money has no ceiling to be a fraction of, so it is
    None, and it appears only for a harness that reports no windows at all -- an API
    key, where the dollars are an invoice rather than a list price.
    """
    out: list[tuple[str, float | None]] = []
    for harness, snap in sorted((usage or {}).items()):
        windows = (snap or {}).get("windows") or {}
        first = (snap or {}).get("first_windows") or {}
        for name, w in sorted(windows.items(), key=lambda kv: -((kv[1] or {}).get("minutes") or 0)):
            util = (w or {}).get("utilization")
            if util is None:
                continue
            resets = (w or {}).get("resets_at")
            if resets is not None and resets <= now:
                util = 0.0
            was = first.get(name)
            rose = f" {(util - was) * 100:+.0f}" if was is not None and abs(util - was) >= 0.005 else ""
            out.append((f"{harness} {_short_window(name)} {util * 100:.0f}%{rose}", util))
    for harness, amount in sorted((money or {}).items()):
        if amount:
            out.append((f"{harness} ${float(amount):.2f}", None))
    return out


def _short_window(name: str) -> str:
    return _SHORT_WINDOW.get(name, name.replace("_overage_included", "+ov"))


def _ts(s: str) -> float:
    try:
        return datetime.fromisoformat(s).timestamp()
    except (ValueError, TypeError):
        return 0.0


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        if line.strip():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def _tail_lines(path: Path, n: int, max_bytes: int = 200_000) -> list[str]:
    if not path.exists():
        return []
    try:
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            data = f.read().decode("utf-8", "replace")
    except OSError:
        return []
    return data.splitlines()[-n:]


def derive_iterations(steps: list[dict]) -> tuple[list[Iteration], dict[str, StageStat], int, int]:
    """Per-iteration outcomes and per-stage durations from iterations.jsonl.

    A step's duration is the gap since the previous step of any kind: the actor's
    is measured from the end of the last stage before it (waits included).
    """
    iters: dict[int, Iteration] = {}
    stages = {s: StageStat() for s in STAGES}
    prev_ts = None
    reconciles = plans = 0
    for s in sorted(steps, key=lambda r: _ts(r.get("ts", ""))):
        ts = _ts(s.get("ts", ""))
        n = s.get("iteration")
        step = s.get("step")
        it = iters.setdefault(n, Iteration(n)) if isinstance(n, int) else None
        gap = (ts - prev_ts) if prev_ts and ts else 0.0
        prev_ts = ts or prev_ts
        stage = STEP_TO_STAGE.get(step or "")
        if stage:
            st = stages[stage]
            st.count += 1
            st.total += gap
            st.last = gap
        if it is None:
            continue
        if step == "actor":
            it.actor_seconds = gap
            it.error = s.get("error") or None
        elif step == "commit":
            it.changed = bool(s.get("changed"))
            it.recorded = bool(s.get("recorded"))
            it.cost = float(s.get("cost") or 0)
            stages["commit"].count += 1
        elif step == "oversee":
            it.verdict = s.get("verdict") or ""
        elif step == "critique":
            it.critique = s.get("verdict") or ""
        elif step == "revert":
            it.reverted = True
        elif step == "plan":
            plans += 1
            it.bet = s.get("bet") or s.get("error") or "no bet"
        elif step == "reconcile":
            reconciles += 1
    return [iters[k] for k in sorted(iters)], stages, reconciles, plans


def _process_tree(pid: int | None) -> tuple[int, float, float]:
    """(process count, summed %cpu, summed RSS MB) of the loop pid and everything under it."""
    if not pid:
        return 0, 0.0, 0.0
    try:
        out = subprocess.run(["ps", "-axo", "pid=,ppid=,pcpu=,rss="], capture_output=True, text=True, timeout=3).stdout
    except (OSError, subprocess.SubprocessError):
        return 0, 0.0, 0.0
    rows = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 4:
            try:
                rows.append((int(parts[0]), int(parts[1]), float(parts[2]), float(parts[3])))
            except ValueError:
                continue
    children: dict[int, list[int]] = {}
    info = {}
    for p, pp, cpu, rss in rows:
        children.setdefault(pp, []).append(p)
        info[p] = (cpu, rss)
    if pid not in info:
        return 0, 0.0, 0.0
    seen, stack = set(), [pid]
    while stack:
        p = stack.pop()
        if p in seen:
            continue
        seen.add(p)
        stack.extend(children.get(p, []))
    cpu = sum(info[p][0] for p in seen)
    rss = sum(info[p][1] for p in seen) / 1024.0
    return len(seen), cpu, rss


def _pid(run_dir: Path) -> tuple[int | None, bool]:
    p = run_dir / "pid"
    if not p.exists():
        return None, False
    try:
        pid = int(p.read_text().strip())
        os.kill(pid, 0)
        return pid, True
    except (ValueError, ProcessLookupError, PermissionError):
        return None, False
    except OSError:
        return None, False


def _items(body: str) -> list[str]:
    """Numbered or bulleted items of a markdown body, citations stripped, bold kept as plain."""
    out: list[str] = []
    for line in body.splitlines():
        s = line.strip()
        m = re.match(r"^(?:[-*]|\d+[.)])\s+(.*)$", s)
        if m:
            out.append(re.sub(r"\s*\[rec: [^\]]+\]", "", m.group(1)).replace("**", ""))
        elif out and s and line.startswith((" ", "\t")):
            out[-1] += " " + s
    return out


def _plan_short(repo: Path, plan_md: str) -> list[str]:
    nodes = repo / ".hypergraph" / "graph" / "plan"
    if nodes.exists():
        for f in nodes.glob("*.md"):
            try:
                text = f.read_text()
            except OSError:
                continue
            if re.search(r"^title:\s*'?short'?\s*$", text, re.M) and "## Current" in text:
                body = text.split("## Current", 1)[1].split("\n## ", 1)[0]
                items = _items(body)
                if items:
                    return items[:8]
    for cand in (repo / plan_md, repo / ".ouroboros" / "plan.md"):
        if cand.exists():
            text = cand.read_text()
            m = re.search(r"^#{2,3}\s+short\b.*?$([\s\S]*?)(?=^#{2,3}\s|\Z)", text, re.M | re.I)
            body = m.group(1) if m else text
            bullets = [l.strip() for l in body.splitlines() if l.strip().startswith(("-", "*", "1", "2", "3"))]
            lines = [re.sub(r"\s*\[rec: [^\]]+\]", "", b.lstrip("-* ")) for b in bullets]
            if lines:
                return lines[:8]
            plain = [l.strip() for l in body.strip().splitlines() if l.strip() and not l.startswith(("#", "("))]
            if plain:
                return plain[:8]
    return []


def frontier_counts(repo: Path) -> dict[str, int]:
    """Counts from STATE.md: frontier nodes by status, charter gaps still on it, charter gaps total."""
    out: dict[str, int] = {}
    state = repo / "STATE.md"
    if not state.exists():
        return out
    try:
        text = state.read_text()
    except OSError:
        return out
    m = re.search(r"^## Frontier\s*$([\s\S]*?)(?=^## |\Z)", text, re.M)
    body = m.group(1) if m else ""
    for line in body.splitlines():
        mm = re.match(r"^\s*-\s*\[(\w+)\]", line)
        if mm:
            out[mm.group(1)] = out.get(mm.group(1), 0) + 1
            if "charter criterion" in line.lower() or "charter gap" in line.lower():
                out["gaps_open"] = out.get("gaps_open", 0) + 1
    arch = re.search(r"^## Architecture\s*$([\s\S]*?)(?=^## |\Z)", text, re.M)
    for line in (arch.group(1) if arch else "").splitlines():
        mm = re.match(r"^\s*-\s*\[(\w+)\]", line)
        if mm:
            out["all_" + mm.group(1)] = out.get("all_" + mm.group(1), 0) + 1
    out["nodes"] = sum(v for k, v in out.items() if k.startswith("all_"))
    goal = repo / ".ouroboros" / "goal.md"
    if goal.exists():
        try:
            from .. import goal as charter
            out["gaps_total"] = len(charter.done_criteria(goal.read_text(), include_checked=True))
            out["gaps_unchecked"] = len(charter.done_criteria(goal.read_text()))
        except Exception:
            pass
    return out


def _newest_transcript(run_dir: Path) -> Path | None:
    """The newest transcript with content; an empty file just opened by the next call does not win."""
    d = run_dir / "transcripts"
    if not d.exists():
        return None
    files = sorted(d.glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True)
    for f in files[:4]:
        if f.stat().st_size > 200:
            return f
    return files[0] if files else None


def load_snapshot(run_dir: Path, *, repo: Path, plan_md: str = "PLAN.md", stop_after_s: float | None = None,
                  max_iterations: int | None = None, chains: dict[str, list[str]] | None = None, mode: str = "single",
                  roles: dict | None = None) -> Snapshot:
    now = time.time()
    status = None
    sp = run_dir / "status.json"
    if sp.exists():
        try:
            status = json.loads(sp.read_text())
        except (OSError, ValueError):
            status = None
    pid, alive = _pid(run_dir)
    steps = _read_jsonl(run_dir / "iterations.jsonl")
    decisions = _read_jsonl(run_dir / "overseer.jsonl")
    iterations, stages, reconciles, plans = derive_iterations(steps)
    state = (status or {}).get("state", "?")
    stage = STATE_TO_STAGE.get(state, state)
    stage_since = float((status or {}).get("epoch") or now)

    feed: list[Event] = []
    feed_role, feed_age = "", None
    t = _newest_transcript(run_dir)
    if t is not None:
        feed = parse_events(_tail_lines(t, 4000, FEED_BYTES))[-FEED_EVENTS:]
        feed_role = t.stem
        feed_age = now - t.stat().st_mtime

    log_tail = _tail_lines(run_dir / "loop.log", LOG_LINES)
    switches = sum(1 for l in log_tail if "harness:" in l and "->" in l)
    nh = run_dir / "NEEDS_HUMAN.md"
    needs_human = nh.read_text() if nh.exists() else None
    try:
        loadavg = os.getloadavg()
    except (OSError, AttributeError):
        loadavg = (0.0, 0.0, 0.0)
    procs, cpu, rss = _process_tree(pid if alive else None)
    return Snapshot(
        now=now, status=status, alive=alive, pid=pid, iterations=iterations, stages=stages, stage=stage,
        stage_since=stage_since, decisions=decisions, feed=feed, feed_role=feed_role, feed_age=feed_age,
        log_tail=log_tail, plan_short=_plan_short(repo, plan_md), needs_human=needs_human, loadavg=loadavg,
        procs=procs, cpu_pct=cpu, rss_mb=rss, stop_after_s=stop_after_s, max_iterations=max_iterations,
        chains=chains or {}, roles=roles or {}, mode=mode, switches=switches, reconciles=reconciles, plans=plans,
        frontier=frontier_counts(repo),
    )
