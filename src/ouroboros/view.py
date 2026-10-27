"""The run view: one live HTML page about a run, at `<run_dir>/view/index.html`.

The page is a template (owned by Ouroboros, or `<repo>/.ouroboros/view/template.html`
when a repo gives its own) filled with two kinds of content:

- facts that need no model: status.json, critic.jsonl, iterations.jsonl, the
  charter and the memory graph. `render` writes these, every time, the same way.
- the story: `view/story.html`, a fragment the scribe writes with its own images
  under `view/assets/`. It goes into the page raw (the run's own agent wrote it).

Everything else that goes into the page is HTML-escaped.

The engine calls `render` at every stage change. It is cheap, it writes with
tmp + os.replace, and it never raises: a broken view must not stop the loop.
`render` also writes `view/live.json`, which the page's small script polls to
swap in new content without a reload. `serve` is a read-only static server
for `view/` and nothing else.
"""

from __future__ import annotations

import hashlib
import html
import json
import mimetypes
import os
import re
import socket
import subprocess
import time
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from urllib.parse import unquote, urlsplit

from . import goal as charter
from . import criteria as progress
from .memory.graph import FRONTIER_STATUSES, Graph, first_line, one_line, section

VIEW = "view"
TEMPLATE = "view_template.html"
REPO_TEMPLATE = Path(".ouroboros") / VIEW / "template.html"

DECISIONS_SHOWN = 8
TIMELINE_OPEN = 30
TIMELINE_MAX = 400
JSONL_TAIL_BYTES = 4_000_000
MISSION_LIMIT = 600
LIVE_STATES = ("starting", "work", "critique", "scribe", "idle", "backoff", "refreshed")
STAGES = (("work", "actor"), ("critique", "critic"), ("scribe", "scribe"))

# Verdict -> chip class. The word is always shown too: color is never the only signal.
VERDICT_CLASS = {
    "continue": "ok", "accept": "ok", "answer": "ok", "done_accepted": "ok",
    "done_rejected": "warn", "stuck": "warn", "looping": "warn",
    "reject": "bad", "revert": "bad",
}
STATUS_CLASS = {"working": "ok", "open": "warn", "broken": "bad", "blocked": "bad", "superseded": "mute"}
STATE_CLASS = {"work": "on", "critique": "on", "scribe": "on", "starting": "on", "idle": "mute",
               "refreshed": "mute", "backoff": "warn", "stopped": "mute", "killed": "bad"}
STATE_WORD = {"work": "actor", "critique": "critic", "scribe": "scribe"}

_AGO = "\x00ago\x00"          # stands in for now-dependent text while the version hash is made
_PLACEHOLDER = re.compile(r"\{\{(\w+)\}\}")
_GOAL_TITLE = re.compile(r"^#\s+Goal:\s*(.+?)\s*$", re.MULTILINE)
_SAFE_ID = re.compile(r"[^A-Za-z0-9_-]+")


def _logo() -> str:
    try:
        return resources.files("ouroboros").joinpath("logo.svg").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


LOGO_SVG = _logo()
LOGO = LOGO_SVG.replace("<svg ", '<svg class="logo" aria-hidden="true" ', 1)
FAVICON = "data:image/svg+xml," + LOGO_SVG.replace('fill="currentColor"', 'fill="#1d1d1b"').replace("#", "%23").replace('"', "'")


def view_dir(run_dir: Path) -> Path:
    return Path(run_dir) / VIEW


def story_path(run_dir: Path) -> Path:
    return view_dir(run_dir) / "story.html"


def summary_path(run_dir: Path) -> Path:
    return view_dir(run_dir) / "summary.txt"


def assets_dir(run_dir: Path) -> Path:
    return view_dir(run_dir) / "assets"


# -- reading, defensively ------------------------------------------------------

def _e(value) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _read_text(path: Path, limit: int | None = None) -> str:
    try:
        if limit is not None and path.stat().st_size > limit:
            with path.open("rb") as f:
                f.seek(-limit, os.SEEK_END)
                data = f.read()
            text = data.decode("utf-8", "replace")
            return text.split("\n", 1)[1] if "\n" in text else ""   # drop the cut first line
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(_read_text(path) or "{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _read_jsonl(path: Path) -> list[dict]:
    out = []
    for line in _read_text(path, JSONL_TAIL_BYTES).splitlines():
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict):
            out.append(item)
    return out


def _int(value, default: int | None = None) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


_MD_CODE = re.compile(r"`([^`\n]+)`")
_MD_STRONG = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*")
_MD_EM = re.compile(r"(?<![\w*])\*(?=[^\s*])(.+?)(?<=[^\s*])\*(?![\w*])|(?<![\w_])_(?=[^\s_])(.+?)(?<=[^\s_])_(?![\w_])")
_MD_LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)")


def _md(value) -> str:
    """Escaped text with inline markdown made HTML: `code`, **bold**, *em* or _em_, [text](https://...).

    Escaping comes first, so a model's text can format but never inject. Code spans and
    link targets are set aside before the rest, so `snake_case`, `a*b` and URLs stay as
    they are. Only `**` is bold: `__init__` is a name, not emphasis.
    """
    text = _e(value).replace("\x00", "")   # the placeholders below use NUL; text must not fake one
    kept: list[str] = []

    def keep(html_: str) -> str:
        kept.append(html_)
        return f"\x00{len(kept) - 1}\x00"
    text = _MD_CODE.sub(lambda m: keep(f"<code>{m.group(1)}</code>"), text)
    text = _MD_LINK.sub(lambda m: keep(f'<a href="{m.group(2)}" rel="noopener noreferrer">') + m.group(1) + keep("</a>"), text)
    text = _MD_STRONG.sub(lambda m: f"<strong>{m.group(1)}</strong>", text)
    text = _MD_EM.sub(lambda m: f"<em>{m.group(1) or m.group(2)}</em>", text)
    return re.sub("\x00(\\d+)\x00", lambda m: kept[int(m.group(1))], text)


def _plain(value) -> str:
    """Text with the inline markdown markers taken out, for tooltips and attributes."""
    text = _MD_LINK.sub(lambda m: m.group(1), str(value or ""))
    text = _MD_CODE.sub(lambda m: m.group(1), text)
    text = _MD_STRONG.sub(lambda m: m.group(1), text)
    return _MD_EM.sub(lambda m: m.group(1) or m.group(2), text)


def _clip(text: str, limit: int) -> str:
    text = one_line(text)
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _duration(seconds) -> str:
    s = _int(seconds)
    if s is None or s < 0:
        return "–"
    h, rem = divmod(s, 3600)
    m = rem // 60
    return f"{h} h {m:02d} min" if h else (f"{m} min" if m else f"{s} s")


def _when(at) -> str:
    """'2026-10-17T03:12:44Z' -> '10-17 03:12' (UTC); anything else as it is."""
    text = str(at or "")
    m = re.match(r"^\d{4}-(\d\d-\d\d)T(\d\d:\d\d)", text)
    return f"{m.group(1)} {m.group(2)}" if m else text


def _id(prefix: str, value) -> str:
    return f"{prefix}-{_SAFE_ID.sub('-', str(value))[:60]}"


def _chip(word: str, cls: str = "") -> str:
    return f'<span class="chip {cls}">{_e(word)}</span>'


# -- the parts ---------------------------------------------------------------

def _goal_name(goal_text: str) -> str:
    m = _GOAL_TITLE.search(goal_text or "")
    return m.group(1) if m else ""


def _epoch(ts) -> float | None:
    """An ISO time ('2026-10-17T11:32:22+00:00' or '...Z') as epoch seconds."""
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def _span(seconds) -> str:
    """A short duration: '45 s', '4 min', '1 h 05'."""
    s = _int(seconds)
    if s is None or s < 0:
        return "–"
    if s < 60:
        return f"{s} s"
    if s < 3600:
        return f"{s // 60} min"
    return f"{s // 3600} h {s % 3600 // 60:02d}"


def _median(values: list[float]) -> float | None:
    v = sorted(x for x in values if x is not None)
    if not v:
        return None
    mid = len(v) // 2
    return v[mid] if len(v) % 2 else (v[mid - 1] + v[mid]) / 2


def _iterations(steps: list[dict], decisions: list[dict], status: dict, graph: Graph | None) -> list[dict]:
    """One row per finished or running iteration, from iterations.jsonl, critic.jsonl and the records.

    A stage lasts from the end of the stage before it: the actor's time includes any
    wait before it began. A record belongs to the iteration whose scribe step came
    first at or after the record's time; the run's directive belongs to iteration 0.
    """
    epoch, elapsed = _float(status.get("epoch")), _float(status.get("elapsed_s"))
    started = epoch - elapsed if epoch is not None and elapsed is not None else None
    its: dict[int, dict] = {}
    for st in steps:
        n = _int(st.get("iteration"))
        if n is None or n < 1:
            continue
        it = its.setdefault(n, {"n": n, "ts": {}, "reverted": False, "fold": False, "changed": None,
                                "signal": None, "error": None})
        ts = _epoch(st.get("ts"))
        step = st.get("step")
        if step in ("commit", "critique", "scribe") and ts is not None:
            it["ts"][step] = ts
        if step == "commit":
            it["changed"] = bool(st.get("changed"))
        elif step == "critique":
            it["verdict"] = st.get("verdict")
        elif step == "revert":
            it["reverted"] = True
        elif step == "scribe":
            it["fold"] = bool(st.get("fold_due")) and bool(st.get("recorded"))
        elif step == "loop":
            it["signal"] = st.get("signal")
        elif step in ("actor", "engine_error") and st.get("error"):
            it["error"] = st.get("error")
        if ts is not None:
            it["end"] = max(it.get("end", ts), ts)
    for d in decisions:
        n = _int(d.get("iteration"))
        if n in its:
            its[n]["decision"] = d
            its[n]["verdict"] = d.get("verdict") or its[n].get("verdict")
    rows = [its[k] for k in sorted(its)]
    prev = started
    for it in rows:
        t = it["ts"]
        a, c, w = t.get("commit"), t.get("critique"), t.get("scribe")
        it["start"] = prev
        it["actor_s"] = a - prev if a is not None and prev is not None else None
        it["critic_s"] = c - a if c is not None and a is not None else None
        it["scribe_s"] = w - c if w is not None and c is not None else None
        prev = it.get("end", prev)
    if graph is not None and graph.ok:
        cut = [(it["ts"].get("scribe") or it.get("end"), it) for it in rows]
        for r in graph.records():
            at = _epoch(r.get("at"))
            if at is None or graph.root in (r.get("parents") or []):   # the run's directive: before iteration 1
                continue
            for end, it in cut:
                if end is not None and at <= end:
                    it.setdefault("records", []).append(r)
                    break
    return rows


# -- the parts ---------------------------------------------------------------

def _header(status: dict, run_name: str, goal_text: str) -> str:
    state = str(status.get("state") or "unknown")
    epoch = _float(status.get("epoch"))
    live = "1" if state in LIVE_STATES else "0"
    goal = _goal_name(goal_text)
    updated = (f'<span id="ago" class="ago" data-ts="{epoch:.0f}" data-live="{live}">{_AGO}</span>'
               if epoch else '<span class="ago">no status yet</span>')
    meta = " · ".join(x for x in (
        f"branch <code>{_e(status.get('branch'))}</code>" if status.get("branch") else "",
        f"harness {_e(status.get('harness'))}" if status.get("harness") else "",
        f"memory {_e(status.get('memory'))}" if status.get("memory") else "") if x)
    notes = []
    if status.get("stop_reason"):
        notes.append(f'<p class="notice">Stopped: {_md(_clip(str(status["stop_reason"]), 300))}</p>')
    if state == "backoff" and status.get("why"):
        notes.append(f'<p class="notice warn">Waiting {_e(_duration(status.get("seconds")))}: '
                     f'{_e(_clip(str(status["why"]), 300))}</p>')
    return (f'<header class="mast"><div class="brand">{LOGO}<span class="kick">Ouroboros · run report</span></div>'
            f'<div class="stamp"><span class="run">{_e(run_name)}</span> · {updated}'
            '<button class="theme" type="button" aria-label="Switch light or dark">◐</button></div></header>'
            f'<h1>{_e(goal or run_name)}</h1>'
            + (f'<p class="meta">{meta}</p>' if meta else "") + "".join(notes))


def _summary(run_dir: Path, goal_text: str) -> str:
    """The scribe's plain-language summary (view/summary.txt), else the charter's mission."""
    text = _read_text(summary_path(run_dir), 8000).strip()
    source = ""
    if not text:
        text = charter.mission(goal_text or "").strip()
        source = '<p class="src">From the charter\'s mission. The scribe writes a summary after its first iteration.</p>'
    if not text:
        return ""
    paras = [one_line(p) for p in re.split(r"\n\s*\n", text) if p.strip()][:4]
    return '<div class="lede">' + "".join(f"<p>{_md(_clip(p, 900))}</p>" for p in paras) + source + "</div>"


def _figures(status: dict, steps: list[dict], graph: Graph | None, goal_text: str) -> str:
    commits = sum(1 for s in steps if s.get("step") == "commit" and s.get("sha"))
    reverts = sum(1 for s in steps if s.get("step") == "revert")
    verdict = status.get("last_verdict")
    met, total = _criteria_count(graph, goal_text)

    def fig(label: str, value: str, sub: str = "") -> str:
        return (f'<div class="fig"><dt>{_e(label)}</dt><dd>{value}'
                f'{f"<small>{sub}</small>" if sub else ""}</dd></div>')

    cells = [
        fig("Iteration", f'<span class="num">{_e(status.get("iteration", "–"))}</span>'),
        fig("Elapsed", f'<span class="num">{_e(_span(status.get("elapsed_s")))}</span>'),
        fig("Criteria met", f'<span class="num">{met}<span class="of">/{total}</span></span>' if total else "–"),
        fig("Commits kept", f'<span class="num">{max(0, commits - reverts)}</span>',
            f"{reverts} reverted" if reverts else ""),
        fig("Last verdict", _chip(VERDICT_WORD.get(str(verdict), verdict), VERDICT_CLASS.get(str(verdict), "")) if verdict else "–"),
    ]
    return f'<dl class="figs">{"".join(cells)}</dl>'


def _facts(status: dict, steps: list[dict], run_name: str, goal_text: str) -> str:
    """The header and the key figures, for a template that has one `{{facts}}` slot."""
    return _header(status, run_name, goal_text) + _figures(status, steps, None, goal_text)


# The gauges' icons: a clock for the time budget, a dial for usage.
CLOCK_ICON = ('<svg class="ico" viewBox="0 0 16 16" aria-hidden="true"><circle cx="8" cy="8" r="6.5"/>'
              '<path d="M8 4.2V8l2.6 1.6"/></svg>')
USAGE_ICON = ('<svg class="ico" viewBox="0 0 16 16" aria-hidden="true"><path d="M2 11.5a6 6 0 1 1 12 0"/>'
              '<path d="M8 11.5l3-4"/><circle class="pin" cx="8" cy="11.5" r=".9"/></svg>')

ACT_WORDS = {"thinking": "thinking", "tool": "using a tool", "command": "running a command", "wait": "waiting"}


def _activity(status: dict) -> dict | None:
    """What the running call does now (`activity.live`, written by the loop's heartbeat), while a stage runs."""
    act = status.get("activity")
    if str(status.get("state") or "") not in dict(STAGES) or not isinstance(act, dict):
        return None
    return act if act.get("kind") in ACT_WORDS else None


def _loop(status: dict, its: list[dict]) -> str:
    """The loop as a dial: one turn is a round span of time, and the band fills clockwise from
    twelve o'clock as the iteration runs, coloured by stage, with the hand at now.

    The page's script draws it, every second, from what this puts on the svg (data-dial): the
    iteration's start, each stage's start and end so far (the running one has no end), the
    three iterations before, the critic's verdict and what the call is doing. Here only the
    centre is drawn, so the page still reads without the script.
    """
    state = str(status.get("state") or "")
    n = _int(status.get("iteration"))
    epoch, elapsed = _float(status.get("epoch")), _float(status.get("elapsed_s"))
    row = next((i for i in its if i["n"] == n), None)
    before = [i for i in its if n is None or i["n"] < n]
    started = epoch - elapsed if epoch is not None and elapsed is not None else None
    start = row["start"] if row and row.get("start") is not None else (before[-1].get("end") if before else started)
    ts = row["ts"] if row else {}
    keys = [k for k, _ in STAGES]
    begin = {"work": start, "critique": ts.get("commit"), "scribe": ts.get("critique")}
    end = {"work": ts.get("commit"), "critique": ts.get("critique"), "scribe": ts.get("scribe")}
    since = _float(status.get("stage_since"))
    if state in begin:   # the running stage: from when the status says it began, to now
        begin[state] = begin[state] or since
        end[state] = None
        at = keys.index(state)
        if at and begin[state] is not None:
            end[keys[at - 1]] = end[keys[at - 1]] or begin[state]
        for k in keys[at + 1:]:
            begin[k] = end[k] = None
        if start is None:
            start = begin["work"] or begin[state]
    stages = [[k, round(begin[k]), round(end[k]) if end[k] is not None else None] for k in keys if begin[k] is not None]
    past = [[i.get("actor_s"), i.get("critic_s"), i.get("scribe_s"), VERDICT_CLASS.get(str(i.get("verdict") or ""), "")]
            for i in reversed(before) if None not in (i.get("actor_s"), i.get("critic_s"), i.get("scribe_s"))][:3]
    act = _activity(status)
    kind = act["kind"] if act else ""
    spec = {"n": n, "state": state, "live": state in LIVE_STATES, "at": round(epoch) if epoch else None,
            "start": round(start) if start is not None else None, "stages": stages, "past": past,
            "verdict": VERDICT_CLASS.get(str((row or {}).get("verdict") or ""), "") if ts.get("critique") else "",
            "kind": kind, "shells": _int((act or {}).get("background"), 0) or 0}
    word = STATE_WORD.get(state, state) or "–"
    label = "The loop as a dial: actor, then critic, then scribe" + (f"; now the {STATE_WORD[state]}" if state in STATE_WORD else "")
    return (f'<svg class="ring dial" data-state="{_e(state)}" data-activity="{_e(kind)}" data-dial="{_e(json.dumps(spec))}"'
            f' viewBox="-72 -24 444 348" role="img" aria-label="{_e(label)}">'
            f'<text x="150" y="120" text-anchor="middle" class="cl">iteration</text>'
            f'<text x="150" y="160" text-anchor="middle" class="cn">{_e(n if n is not None else "–")}</text>'
            f'<text x="150" y="184" text-anchor="middle" class="cs">{_e(word)}</text></svg>')


def _stages(status: dict) -> str:
    """The loop dial without the iterations, for a template that has the old `{{stages}}` slot."""
    return _loop(status, [])


USAGE_CELLS = 20
WINDOW_WORDS = {"five_hour": "5-hour window", "seven_day": "weekly window", "seven_day_opus": "weekly, Opus",
                "seven_day_sonnet": "weekly, Sonnet", "daily": "daily window", "hourly": "hourly window"}
TOKEN_WORDS = (("input", "in"), ("cache_read", "cache read"), ("cache_write", "cache write"), ("output", "out"))


def _cells(fraction: float, label: str) -> str:
    """A row of USAGE_CELLS cells, lit from the left (the stylesheet colours them green to red); past 1.0 every cell is lit."""
    lit = round(max(0.0, min(fraction, 1.0)) * USAGE_CELLS)
    cells = "".join(
        '<i class="on"></i>' if k < lit else "<i></i>"
        for k in range(USAGE_CELLS))
    return (f'<span class="cells" role="meter" aria-valuemin="0" aria-valuemax="100" aria-valuenow="{round(100 * fraction)}"'
            f' aria-label="{_e(label)}">{cells}</span>')


def _count(n: int) -> str:
    """12345678 -> '12.3 M'."""
    for size, unit in ((1e9, "B"), (1e6, "M"), (1e3, "k")):
        if n >= size:
            return f"{n / size:.1f}".rstrip("0").rstrip(".") + f" {unit}"
    return str(n)


def _tokens(tokens: dict) -> str:
    """The run's tokens on one harness: the total, and a thin bar of what kind they were."""
    counts = [(k, w, _int(tokens.get(k), 0) or 0) for k, w in TOKEN_WORDS]
    total = sum(n for _, _, n in counts)
    if not total:
        return ""
    bar = "".join(f'<span class="t-{k}" style="width:{100 * n / total:.2f}%" title="{_e(w)}: {n:,}"></span>'
                  for k, w, n in counts if n)
    words = "".join(f'<span><i class="t-{k}"></i>{_e(_count(n))} {_e(w)}</span>' for k, w, n in counts if n)
    return (f'<div class="gauge tok"><span class="gl">Tokens</span>'
            f'<span class="tbar" role="img" aria-label="{total:,} tokens">{bar}</span>'
            f'<span class="gv num">{_e(_count(total))}</span><span class="gs legend">{words}</span></div>')


def _models(models: dict) -> str:
    """'claude-opus-5-5 (actor, critic) · claude-sonnet-5 (scribe)'."""
    out = []
    for name, roles in models.items():
        roles = [str(r) for r in roles] if isinstance(roles, list) else []
        out.append(f'<code>{_e(name)}</code>' + (f' <span class="quiet">{_e(", ".join(roles))}</span>' if roles else ""))
    return " · ".join(out)


def _usage(status: dict) -> str:
    """Per harness: its models, what this run took of each rationing window, and its tokens.

    A bar is the run's own share of one whole window (the account's level is not shown:
    other use of the account fills it too). The share sums the rises between readings and
    starts again from zero when a window resets, so it can pass 100% on a long run.
    """
    usage = status.get("usage") if isinstance(status.get("usage"), dict) else {}
    tokens = status.get("tokens") if isinstance(status.get("tokens"), dict) else {}
    models = status.get("models") if isinstance(status.get("models"), dict) else {}
    money = status.get("money") if isinstance(status.get("money"), dict) else {}
    blocks = []
    for harness in sorted(set(usage) | set(tokens) | set(models) | {h for h, a in money.items() if _float(a)}):
        d = usage.get(harness) if isinstance(usage.get(harness), dict) else {}
        rows = []
        first = d.get("first_windows") or {}
        spent = d.get("run_spent") or {}
        wins = [(n, w) for n, w in (d.get("windows") or {}).items() if isinstance(w, dict)]
        for name, w in sorted(wins, key=lambda kv: -(_int(kv[1].get("minutes"), 0) or 0)):
            u, was = _float(w.get("utilization")), _float(first.get(name))
            took = _float(spent.get(name))
            if took is None:   # a status from before run_spent: the rise since the first reading
                took = max(0.0, u - was) if u is not None and was is not None else None
            if took is None:
                continue
            reset = _float(w.get("resets_at"))
            word = WINDOW_WORDS.get(name, name.replace("_", " "))
            rows.append(f'<div class="gauge"><span class="gl">{_e(word)}</span>'
                        + _cells(took, f"{harness} {word}: used this run")
                        + f'<span class="gv num">{round(100 * took)}<small>%</small></span>'
                        + (f'<span class="gs">resets <span class="reset" data-at="{reset:.0f}"></span></span>' if reset else "")
                        + "</div>")
        if harness not in usage and _float(money.get(harness)):
            rows.append(f'<div class="gauge"><span class="gl">API <small>billed per call</small></span>'
                        f'<span class="gv num wide">${_float(money[harness]):.2f}</span></div>')
        if isinstance(tokens.get(harness), dict):
            rows.append(_tokens(tokens[harness]))
        if not rows:
            continue
        m = models.get(harness) if isinstance(models.get(harness), dict) else {}
        blocks.append(f'<div class="agent"><p class="who">{USAGE_ICON}<strong>{_e(harness)}</strong>'
                      + (f" {_models(m)}" if m else "") + ' <span class="quiet">· used this run</span></p>' + "".join(rows) + "</div>")
    if not blocks:
        text = status.get("usage_compact")
        return f'<p class="quiet">{_e(text)}</p>' if text else '<p class="quiet">No usage reading yet.</p>'
    return "".join(blocks)


def _time_budget(status: dict) -> str:
    """How much of the run's time budget has gone, as the same row of cells. The page's script keeps it ticking."""
    elapsed = _float(status.get("elapsed_s"))
    budget = _float(status.get("time_budget_s"))
    if elapsed is None:
        return ""
    state = str(status.get("state") or "")
    start = _float(status.get("started_at"))
    live = state in LIVE_STATES and start is not None
    tick = f' data-start="{start:.0f}"' if live else ""
    if not budget:
        return (f'<div class="gauge clock"{tick}><span class="gl">{CLOCK_ICON}Time <small>no time limit set</small></span>'
                f'<span class="gv num wide"><span class="el">{_e(_span(elapsed))}</span> elapsed</span></div>')
    f = elapsed / budget
    left = max(0.0, budget - elapsed)
    return (f'<div class="gauge clock"{tick} data-budget="{budget:.0f}"><span class="gl">{CLOCK_ICON}Time</span>'
            + _cells(f, "time budget used")
            + f'<span class="gv num"><span class="pc">{round(100 * min(f, 1.0))}</span><small>%</small></span>'
            f'<span class="gs"><span class="el">{_e(_span(elapsed))}</span> elapsed · <span class="left">{_e(_span(left))}</span>'
            f' left of {_e(_span(budget))}</span></div>')


NOW_WORDS = {"work": "The actor is doing the work", "critique": "The critic is checking the work",
             "scribe": "The scribe is writing it down", "idle": "Between iterations", "refreshed": "Between iterations",
             "starting": "Starting up", "backoff": "Waiting", "stopped": "Stopped", "killed": "Stopped"}


def _now(status: dict, decisions: list[dict], goal_text: str) -> str:
    """What the run is doing now, in one plain sentence: the critic's `doing` for this iteration, framed by the stage.

    The critic writes `doing` (the unit the next iteration starts) and `did` (what one just
    finished) for a person watching, so they are the sentence. While the actor or the
    critic works on iteration n, the last verdict is n-1's and its `doing` is the work;
    once the critic has judged n, its `did` is what the scribe writes down.
    """
    state = str(status.get("state") or "")
    n = _int(status.get("iteration"), 0) or 0
    last = decisions[-1] if decisions else {}
    judged = _int(last.get("iteration")) == n
    doing, did = str(last.get("doing") or ""), str(last.get("did") or "")
    main, sub = "", ""
    if state in ("work", "critique") and not judged:
        main = doing or ("Reading the charter and the memory, to make a first change." if n <= 1 else "")
    elif state == "backoff":
        main = f"For {_duration(status.get('seconds'))}: {status.get('why') or 'a harness asked the run to wait'}."
    elif state in ("stopped", "killed"):
        main = str(status.get("stop_reason") or "The run was stopped.")
        sub = f"Last: {did}" if did else ""
    elif state == "starting":
        main = "Reading the charter and the memory, to make a first change."
    else:   # the scribe, or between iterations, after the verdict on n
        main = did or doing
        sub = f"Next: {doing}" if did and doing else ""
    if not main:
        main = charter.mission(goal_text or "").strip() or "–"
    live = state in LIVE_STATES and state not in ("idle", "refreshed")
    kicker = NOW_WORDS.get(state, state or "Unknown")
    if state in dict(STAGES) and n:
        kicker += f" · iteration {n}"
    act = _activity(status)
    doing_now = ""
    if act and act["kind"] != "thinking":   # what the call waits on: a command, a tool, a monitor
        since = _float(act.get("since"))
        what = str(act.get("what") or "")
        doing_now = (f'<p class="now-a act-{_e(act["kind"])}"><b>{_e(ACT_WORDS[act["kind"]].capitalize())}</b>'
                     + (f' <span class="act-for" data-since="{since:.0f}"></span>' if since else "")
                     + (f': <code>{_e(_clip(what, 200))}</code>' if what else "") + "</p>")
    return (f'<p class="now-k">{"<i class=\"live\"></i>" if live else ""}Right now · {_e(kicker)}</p>'
            f'<p class="now-t">{_md(_clip(main, 400))}</p>' + doing_now
            + (f'<p class="now-s">{_md(_clip(sub, 300))}</p>' if sub else ""))


def _criteria_count(graph: Graph | None, goal_text: str) -> tuple[int, int]:
    rows = progress.rows(graph, goal_text)
    return sum(r['met'] for r in rows), len(rows)


ROWS_SHOWN = 3


def _rows(rows: list[str], key: str) -> str:
    """The first ROWS_SHOWN rows, and the rest behind a "show N more" toggle (kept open across swaps by its id)."""
    head = f'<ul class="rows">{"".join(rows[:ROWS_SHOWN])}</ul>'
    rest = rows[ROWS_SHOWN:]
    if not rest:
        return head
    return (head + f'<details class="more-rows" id="{_id("more", key)}"><summary><span class="closed">Show {len(rest)} more</span>'
            f'<span class="opened">Show less</span></summary><ul class="rows">{"".join(rest)}</ul></details>')


def _criteria(graph: Graph | None, goal_text: str) -> str:
    values = progress.rows(graph, goal_text)
    rows = [f'<li class="row">{_chip("ticked" if r["status"] == "checked" else "met" if r["met"] else r["status"], "ok" if r["met"] else "warn")}'
            f'<div class="what"><strong>{_md(r["title"])}</strong>'
            f'<span class="sub">{_md(first_line(progress.MARKER.sub("", section(r["current"], "Current")), 240))}</span></div></li>' for r in values]
    total, met = len(values), sum(r['met'] for r in values)
    source = "run criteria matched to memory identity; unknown means no reliable claim mapping"
    if not rows:
        return '<p class="quiet">The charter declares no done criteria.</p>'
    pct = round(100 * met / total) if total else 0
    return (f'<p class="meter"><span class="num">{met} of {total}</span> met <span class="quiet">· {source}</span></p>'
            f'<div class="bar" role="img" aria-label="{met} of {total} criteria met"><span style="width:{pct}%"></span></div>'
            + _rows(rows, "criteria"))


def _frontier(graph: Graph | None) -> str:
    if graph is None or not graph.ok:
        return '<p class="quiet">The memory could not be read.</p>'
    rows = []
    for status in FRONTIER_STATUSES:
        for n in graph.claims("state"):
            if n.get("status") != status:
                continue
            now = first_line(section(n.get("content", ""), "Current"), 240)
            rows.append(f'<li class="row">{_chip(status, STATUS_CLASS.get(status, ""))}'
                        f'<div class="what"><strong>{_md(n.get("title") or "")}</strong> <code>{_e(graph.label(n["id"]))}</code>'
                        f'{f"<span class=\"sub\">{_md(now)}</span>" if now else ""}</div></li>')
    tail = len(graph.tail())
    note = f'<p class="quiet"><span class="num">{tail}</span> record(s) not folded into state yet.</p>' if tail else ""
    if not rows:
        return '<p class="quiet">Nothing open, broken or blocked.</p>' + note
    return _rows(rows, "frontier") + note


def _decisions(decisions: list[dict]) -> str:
    if not decisions:
        return '<p class="quiet">No critic verdict yet.</p>'
    last = decisions[-DECISIONS_SHOWN:]
    strip = "".join(
        f'<a class="chip {VERDICT_CLASS.get(str(d.get("verdict")), "")}" href="#{_id("d", d.get("iteration"))}">'
        f'<span class="num">#{_e(d.get("iteration", "?"))}</span> {_e(d.get("verdict") or "?")}</a>'
        for d in last)
    items = []
    for d in reversed(last):
        v = str(d.get("verdict") or "?")
        body = []
        for key, label in (("doing", "Doing"), ("did", "Did"), ("fix_first", "Fix first"), ("reply", "Reply to the actor")):
            text = d.get(key)
            if text:
                body.append(f"<dt>{label}</dt><dd>{_e(text)}</dd>")
        if d.get("source"):
            body.append(f"<dt>Source</dt><dd>{_e(d['source'])}</dd>")
        doing = f'<span class="sub">{_md(_clip(str(d["doing"]), 200))}</span>' if d.get("doing") else ""
        items.append(
            f'<li><details id="{_id("d", d.get("iteration"))}"><summary>'
            f'<span class="num it">#{_e(d.get("iteration", "?"))}</span>{_chip(v, VERDICT_CLASS.get(v, ""))}'
            f'<span class="what">{_md(_clip(str(d.get("reason") or ""), 260))}{doing}</span></summary>'
            f'<dl class="more">{"".join(body)}</dl></details></li>')
    return f'<div class="strip" aria-label="Last verdicts, oldest first">{strip}</div><ul class="log">{"".join(items)}</ul>'


def _record_item(graph: Graph, r: dict) -> str:
    content = r.get("content", "")
    what = first_line(section(content, "What"), 400)
    result = first_line(section(content, "Result"), 400)
    folded = _chip("folded", "mute") if r.get("folded") else _chip("unfolded", "warn")
    body = "".join(f"<dt>{k}</dt><dd>{_md(v)}</dd>" for k, v in (("What", what), ("Result", result)) if v)
    return (f'<li><details id="{_id("r", r.get("id"))}"><summary>'
            f'<code class="it">{_e(graph.label(r["id"]))}</code><span class="what">{_md(r.get("title") or "")}'
            f'<span class="sub">{_e(_when(r.get("at")))}</span></span>{folded}</summary>'
            f'<dl class="more">{body or "<dd>(no What or Result section)</dd>"}</dl></details></li>')


def _timeline(graph: Graph | None) -> str:
    if graph is None or not graph.ok:
        return '<p class="quiet">The memory could not be read.</p>'
    records = sorted(graph.records(), key=lambda n: -(n.get("seq") or 0))
    if not records:
        return '<p class="quiet">No records yet.</p>'
    shown = "".join(_record_item(graph, r) for r in records[:TIMELINE_OPEN])
    older = records[TIMELINE_OPEN:TIMELINE_MAX]
    out = f'<ul class="log">{shown}</ul>'
    if older:
        rest = "".join(_record_item(graph, r) for r in older)
        cut = len(records) - TIMELINE_OPEN - len(older)
        more = f'<p class="quiet">… and {cut} older record(s).</p>' if cut > 0 else ""
        out += (f'<details id="t-older" class="older"><summary>{len(older)} older record(s)</summary>'
                f'<ul class="log">{rest}</ul>{more}</details>')
    return out


PROGRESS_ROWS = 25
VERDICT_WORD = {"continue": "kept", "accept": "kept", "answer": "answered", "done_accepted": "done",
                "done_rejected": "not done yet", "stuck": "stuck", "looping": "looping", "reject": "rejected",
                "revert": "reverted"}


def _progress(its: list[dict], status: dict) -> str:
    """Every iteration as a column: how long each stage took, the verdict under it, the folds above.

    Then the same iterations as a list, newest first: what was done, each opening on
    what the critic said, what comes next and the record it left.
    """
    if not its:
        return '<p class="quiet">No iteration has finished yet.</p>'
    current = _int(status.get("iteration"), 0) or 0
    live = str(status.get("state")) in LIVE_STATES
    W, H, top, base = 800, 230, 22, 176
    n = len(its)
    step = (W - 60) / max(n, 1)
    bw = max(2.0, min(28.0, step * 0.62))
    span = max([sum(i.get(k) or 0 for k in ("actor_s", "critic_s", "scribe_s")) for i in its] + [60])
    scale = (base - top) / span
    tick = next(t for t in (60, 120, 300, 600, 900, 1800, 3600, 7200, 10 ** 9) if span / t <= 4)
    g = [f'<svg class="chart" viewBox="0 0 {W} {H}" role="img" aria-label="Time per iteration, by stage, with the verdict">']
    t = tick
    while t <= span:
        y = base - t * scale
        g.append(f'<line class="grid" x1="48" x2="{W}" y1="{y:.1f}" y2="{y:.1f}"/>'
                 f'<text class="ax" x="42" y="{y + 4:.1f}" text-anchor="end">{_e(_span(t))}</text>')
        t += tick
    g.append(f'<line class="base" x1="48" x2="{W}" y1="{base}" y2="{base}"/>')
    every = max(1, round(n / 16))
    for k, it in enumerate(its):
        x = 54 + k * step + (step - bw) / 2
        y = base
        running = live and it["n"] == current and "scribe" not in it["ts"]
        v = str(it.get("verdict") or "")
        tip = f'#{it["n"]} · {VERDICT_WORD.get(v, v) or "running"}'
        d = it.get("decision") or {}
        if d.get("did") or d.get("reason"):
            tip += " — " + _clip(str(d.get("did") or d.get("reason")), 160)
        g.append(f'<a href="#it-{it["n"]}"><g class="col"><title>{_e(_plain(tip))}</title>')
        for key, cls in (("actor_s", "s-actor"), ("critic_s", "s-critic"), ("scribe_s", "s-scribe")):
            h = (it.get(key) or 0) * scale
            if h > 0:
                y -= h
                g.append(f'<rect class="{cls}" x="{x:.1f}" y="{y:.1f}" width="{bw:.1f}" height="{h:.1f}"/>')
        if running:
            g.append(f'<rect class="s-run" x="{x:.1f}" y="{y - 16:.1f}" width="{bw:.1f}" height="16"/>')
            y -= 16
        vc = VERDICT_CLASS.get(v, "mute") if v else "mute"
        mark = min(bw, 12.0)
        mx = x + (bw - mark) / 2
        g.append(f'<rect class="v {vc}" x="{mx:.1f}" y="{base + 6}" width="{mark:.1f}" height="{mark:.1f}"/>')
        if it.get("reverted"):
            g.append(f'<path class="x" d="M{mx:.1f},{base + 6} l{mark:.1f},{mark:.1f} M{mx + mark:.1f},{base + 6} l{-mark:.1f},{mark:.1f}"/>')
        if it.get("fold"):
            fx = x + bw / 2
            g.append(f'<path class="fold" d="M{fx:.1f},{y - 12:.1f} l5,5 l-5,5 l-5,-5z"/>')
        if k % every == 0 or k == n - 1:
            g.append(f'<text class="ax" x="{x + bw / 2:.1f}" y="{base + 36}" text-anchor="middle">{it["n"]}</text>')
        g.append("</g></a>")
    g.append("</svg>")
    legend = ('<p class="legend"><span><i class="s-actor"></i>actor</span><span><i class="s-critic"></i>critic</span>'
              '<span><i class="s-scribe"></i>scribe</span><span><i class="v ok"></i>kept</span>'
              '<span><i class="v warn"></i>held back</span><span><i class="v bad"></i>reverted</span>'
              '<span><i class="fold"></i>memory folded</span></p>')
    rows = []
    for it in reversed(its):
        d = it.get("decision") or {}
        v = str(it.get("verdict") or "")
        recs = it.get("records") or []
        did = d.get("did") or (recs[0].get("title") if recs else "") or ("(no change)" if it.get("changed") is False else "")
        why = d.get("reason") or ""
        running = live and it["n"] == current and "scribe" not in it["ts"]
        took = sum(it.get(k) or 0 for k in ("actor_s", "critic_s", "scribe_s"))
        rec = "".join(f'<code>{_e(r.get("slug") or r.get("id", "")[:8])}</code> ' for r in recs[:2])
        flags = "".join(x for x in (
            _chip("folded", "mute") if it.get("fold") else "",
            _chip(f"loop: {it['signal']}", "warn") if it.get("signal") else "",
            _chip("error", "bad") if it.get("error") and not v else "") if x)
        body = "".join(f"<dt>{k}</dt><dd>{x}</dd>" for k, x in (
            ("Critic", _md(_clip(str(why), 600))), ("Next", _md(_clip(str(d.get("doing") or ""), 400))),
            ("Record", rec.strip())) if x)
        rows.append(
            f'<li><details class="it" id="it-{it["n"]}"><summary><span class="num n">{it["n"]}</span>'
            f'{_chip("running", "on") if running else _chip(VERDICT_WORD.get(v, v), VERDICT_CLASS.get(v, "")) if v else _chip("–", "mute")}'
            f'<span class="what">{_md(_clip(str(did), 300)) or "–"} {flags}</span>'
            f'<span class="num took">{_e(_span(took)) if took else "–"}</span></summary>'
            f'<dl class="more">{body or "<dd>(nothing more)</dd>"}</dl></details></li>')
    shown, older = rows[:PROGRESS_ROWS], rows[PROGRESS_ROWS:]
    table = f'<ul class="its">{"".join(shown)}</ul>'
    if older:
        table += (f'<details id="it-older" class="older"><summary>{len(older)} older iteration(s)</summary>'
                  f'<ul class="its">{"".join(older)}</ul></details>')
    return f'<figure class="wide">{"".join(g)}<figcaption>{legend}</figcaption></figure>{table}'


# -- the memory graph: Governor's hub renderer, fed from either store ------------

GRAPH_PAGE = "graph.html"
# Fig. 2's colours, as borris.dev's theme build makes them for its "blues" graph palette (seaborn's
# Blues): met claims darkest, open ones pale, records between and paler with age. The embed reads them
# from window.GRAPH_THEME before it draws.
GRAPH_COLOURS = {"light": {"citation": "#1f77b4", "status": {"open": "#9ecae1", "broken": "#d62728", "blocked": "#9467bd", "working": "#08519c", "done": "#7f7f7f"}, "tint": {"open": "#e8f0f2", "broken": "#f4d0cd", "blocked": "#e6ddeb", "working": "#cad8e4", "done": "#ece8dd", "record": "#d6e5ec"}, "records": ["#4292c6", "#559ccc", "#68a7d2", "#7bb1d8", "#8dbcdd", "#a0c6e3", "#b3d1e9", "#c6dbef"]}, "dark": {"citation": "#679fce", "status": {"open": "#2171b5", "broken": "#ff9896", "blocked": "#c5b0d5", "working": "#c6dbef", "done": "#c7c7c7"}, "tint": {"open": "#1a2e41", "broken": "#513839", "blocked": "#433e49", "working": "#43494f", "done": "#26272a", "record": "#2c3e49"}, "records": ["#6baed6", "#609dc4", "#548db3", "#497ca1", "#3e6c8f", "#335b7d", "#274b6c", "#1c3a5a"]}}
GRAPH_MAX_NODES, GRAPH_MAX_EDGES, GRAPH_MAX_BYTES = 5000, 20000, 1_000_000
_HEAD = re.compile(r"^sha256:[a-f0-9]{64}$")
_GRAPH_KINDS = ("root", "state", "record")
_GRAPH_STATUSES = ("open", "working", "broken", "blocked", "superseded")


def report_graph(graph: Graph, keep: set[str] | None = None) -> dict | None:
    """The memory as Governor's ReportGraph (shared/report.ts): topology, titles, kinds and statuses.

    The same mapping as Governor's `toViewer`, done here so a files memory draws the
    same way a governor one does. `keep` limits it to those node ids. None when the
    result is too big for the embed (see `fit_report_graph`).
    """
    ids = set(graph.nodes) if keep is None else set(graph.nodes) & keep
    nodes, edges = [], []
    children: dict[str, int] = {}
    for nid, n in graph.nodes.items():
        if nid not in ids:
            continue
        for p in n.get("parents") or []:
            if p in ids:
                children[p] = children.get(p, 0) + 1
    stamps = []
    for nid, n in graph.nodes.items():
        if nid not in ids:
            continue
        # Governor's embed draws its hub (the layout with a light palette) only for a graph of
        # root, state and record nodes. A legacy plan claim, or a kind it does not know, is
        # drawn as a state claim, so one old node cannot turn the whole figure dark.
        kind = "root" if nid == graph.root else n.get("kind") if n.get("kind") in _GRAPH_KINDS else "state"
        parents = [p for p in (n.get("parents") or []) if p in ids]
        tags = [{"id": f"graph:{kind}", "name": kind}]
        if n.get("status") in _GRAPH_STATUSES:
            tags.append({"id": f"status:{n['status']}", "name": n["status"]})
        if n.get("folded"):
            tags.append({"id": "governor:folded", "name": "folded"})
        node = {"id": str(nid), "title": str(n.get("title") or n.get("slug") or nid)[:2000],
                "parentId": parents[0] if parents else None, "childCount": children.get(nid, 0), "edgeCount": 0,
                "lifecycle": "live", "tags": tags}
        if _epoch(n.get("at")) is not None:
            node["createdAt"] = str(n["at"])
            stamps.append(str(n["at"]))
        nodes.append(node)
        edges += [{"id": f"parent:{p}:{nid}", "source": p, "target": str(nid), "kind": "semantic", "relation": "parent"}
                  for p in parents]
    for i, e in enumerate(graph.edges):
        if e.get("source") in ids and e.get("target") in ids:
            edges.append({"id": f"edge:{i}", "source": str(e["source"]), "target": str(e["target"]),
                          "kind": "semantic", "relation": "citation"})
    out_degree: dict[str, int] = {}
    for e in edges:
        out_degree[e["source"]] = out_degree.get(e["source"], 0) + 1
    for node in nodes:
        node["edgeCount"] = out_degree.get(node["id"], 0)
    if len(nodes) > GRAPH_MAX_NODES or len(edges) > GRAPH_MAX_EDGES or any(len(x["id"]) > 256 for x in nodes):
        return None
    if len(_graph_json({"nodes": nodes, "edges": edges}).encode()) > GRAPH_MAX_BYTES:
        return None
    head = graph.head if isinstance(graph.head, str) and _HEAD.match(graph.head) else None
    # loadedAt is the newest record, not now: the page then changes only when the memory does
    return {"schemaVersion": 1, "repositoryId": str(graph.root or "ouroboros")[:256], "head": head,
            "loadedAt": max(stamps, key=lambda t: _epoch(t) or 0) if stamps else "1970-01-01T00:00:00Z",
            "nodes": nodes, "edges": edges}


def _graph_json(data: dict) -> str:
    return json.dumps(data, separators=(",", ":"), ensure_ascii=False).replace("<", "\\u003c")


def fit_report_graph(graph: Graph) -> tuple[dict | None, int]:
    """The whole graph when it fits; else the root, every live claim and the newest records that fit.

    Returns the graph and how many records were left out. A long run's oldest records
    are the first to go: their effect lives on in the claims they were folded into.
    """
    data = report_graph(graph)
    if data is not None:
        return data, 0
    records = sorted(graph.records(), key=lambda n: -(n.get("seq") or 0))
    base = {k for k, n in graph.nodes.items() if n.get("kind") != "record" and n.get("status") != "superseded"}
    lo, hi, best = 0, len(records), None
    while lo <= hi:   # the most records that still fit
        mid = (lo + hi) // 2
        got = report_graph(graph, base | {r["id"] for r in records[:mid]})
        if got is not None:
            best, lo = (got, len(records) - mid), mid + 1
        else:
            hi = mid - 1
    return best if best is not None else (None, len(records))


def _vendor(name: str) -> str:
    try:
        return resources.files("ouroboros").joinpath("vendor", name).read_text(encoding="utf-8")
    except OSError:
        return ""


def _graph_page(vdir: Path, graph: Graph | None) -> tuple[str | None, int]:
    """Write view/graph.html (the embed, its CSS and the snapshot) when it changed.

    Its version (None when there is nothing to draw) and the records left out to fit.
    """
    if graph is None or not graph.ok:
        return None, 0
    data, omitted = fit_report_graph(graph)
    script, css = _vendor("governor-report.js"), _vendor("governor-report.css")
    if data is None or not script:
        return None, omitted
    payload = _graph_json(data)
    if re.search(r"</script", script, re.I) or re.search(r"</style", css, re.I):
        return None
    page = ('<!doctype html><html lang="en" data-theme="light"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1"><title>Memory graph</title>'
            '<script>if(/[?&]theme=dark(&|$)/.test(location.search))document.documentElement.dataset.theme="dark"</script>'
            f"<style>{css}</style></head><body><div id=\"root\"></div>"
            f'<script id="flywheel-snapshot" type="application/json">{payload}</script>'
            f'<script>window.GRAPH_THEME={json.dumps(GRAPH_COLOURS)}</script><script>{script}</script></body></html>\n')
    version = hashlib.sha256(page.encode()).hexdigest()[:16]
    path = vdir / GRAPH_PAGE
    if _read_text(path) != page:
        _write(path, page)
    return version, omitted


def _graph(graph: Graph | None, version: str | None, omitted: int = 0) -> str:
    if graph is None or not graph.ok:
        return '<p class="quiet">The memory could not be read.</p>'
    if not version:
        return ('<p class="quiet">The memory graph could not be drawn here. '
                '<code>ouroboros mem orient</code> prints what it holds.</p>')
    records = graph.records()
    swatch = "".join(f'<span><i class="g-{k}"></i>{w}</span>' for k, w in (
        ("working", "true now (met)"), ("open", "open"), ("broken", "broken"), ("blocked", "blocked"),
        ("record", "record of an iteration")))
    return (f'<figure class="graphfig"><iframe class="graph" data-src="{GRAPH_PAGE}?v={version}" title="The run\'s memory graph"'
            ' loading="lazy"></iframe>'
            f'<figcaption><span class="legend">{swatch}</span>'
            + (f'<b>Only the newest {len(records) - omitted} of {len(records)} records are drawn</b>, so the graph fits;'
               ' the older ones live on in the claims they were folded into.' if omitted else "")
            + '</figcaption></figure>')


_SECTION_TAG = re.compile(r"<(/?)section\b[^>]*>", re.I)
_LEAD_HEADING = re.compile(r"\s*<h([23])\b[^>]*>(.*?)</h\1\s*>", re.I | re.S)


def _findings(text: str) -> str:
    """Each top-level <section> that opens with an <h2> or <h3> becomes a finding that folds:
    the heading is the summary, the rest the body. Anything else passes through as written."""
    out, depth, at, start, seen = [], 0, 0, 0, set()
    for m in _SECTION_TAG.finditer(text):
        if not m.group(1):
            depth += 1
            if depth == 1:
                start = m.end()
            continue
        if depth == 0:   # a stray close: leave it be
            continue
        depth -= 1
        if depth:
            continue
        head = _LEAD_HEADING.match(text, start, m.start())
        if not head:
            continue
        key = _id("f", hashlib.sha256(head.group(2).strip().encode()).hexdigest()[:10])
        if key in seen:
            continue
        seen.add(key)
        out += [text[at:start], f'<details class="finding" id="{key}" open><summary>{head.group(0).strip()}</summary>',
                text[head.end():m.start()], "</details>"]
        at = m.start()
    return "".join(out) + text[at:]


def _story(run_dir: Path) -> str:
    text = _read_text(story_path(run_dir))
    if not text.strip():
        return '<p class="quiet">The scribe has not written the story yet.</p>'
    return _findings(text)


def _template(repo: Path | None) -> str:
    if repo is not None:
        custom = _read_text(Path(repo) / REPO_TEMPLATE)
        if custom.strip():
            return custom
    return resources.files("ouroboros").joinpath(TEMPLATE).read_text(encoding="utf-8")


def _fill(template: str, parts: dict[str, str]) -> str:
    """One pass, so text inside a part (the story, say) is never read as a placeholder."""
    return _PLACEHOLDER.sub(lambda m: parts.get(m.group(1), m.group(0)), template)


def _ago_text(epoch: float | None, now: float, live: bool) -> str:
    if not epoch:
        return "no status yet"
    s = max(0, int(now - epoch))
    if s < 60:
        text = f"{s} s"
    elif s < 3600:
        text = f"{s // 60} min"
    else:
        text = f"{s // 3600} h {s % 3600 // 60:02d} min"
    return f"updated {text} ago" + (" (stale)" if live and s > 120 else "")


def _write(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def render(run_dir: Path, repo: Path, *, goal_text: str, graph=None, now: float | None = None,
           alive: bool = False) -> Path | None:
    """Write view/index.html and view/live.json. The index path, or None when anything failed.

    `alive`: the caller is the running loop, so this render is also its heartbeat and the
    page's "updated" time is now. Anyone else (`ouroboros view`) shows status.json's time.
    """
    try:
        run_dir = Path(run_dir)
        now = time.time() if now is None else now
        vdir = view_dir(run_dir)
        assets_dir(run_dir).mkdir(parents=True, exist_ok=True)
        progress.save(run_dir, goal_text or "", graph)
        status = _read_json(run_dir / "status.json")
        decisions_path = run_dir / "critic.jsonl"
        if not decisions_path.exists():
            decisions_path = run_dir / "overseer.jsonl"   # runs from before the critic/overseer split
        decisions = _read_jsonl(decisions_path)
        steps = _read_jsonl(run_dir / "iterations.jsonl")
        run_name = str(status.get("run") or run_dir.name)
        if graph is not None and not isinstance(graph, Graph):
            graph = None
        goal = _goal_name(goal_text or "")

        def part(fn, *args) -> str:
            try:
                return fn(*args)
            except Exception as exc:   # one broken part must not lose the others
                return f'<p class="quiet">This part could not be made ({_e(type(exc).__name__)}).</p>'

        try:
            its = _iterations(steps, decisions, status, graph)
        except Exception:   # the ring and the timeline then show no history; the rest stands
            its = []
        try:
            graph_version, graph_omitted = _graph_page(vdir, graph)
        except Exception:   # no graph section; the rest of the page stands
            graph_version, graph_omitted = None, 0
        parts = {
            "title": _e(f"{run_name} · {goal}" if goal else f"{run_name} · Ouroboros"),
            "run": _e(run_name),
            "goal": _e(goal),
            "mission": _e(_clip(charter.mission(goal_text or ""), MISSION_LIMIT)),
            "state": _e(status.get("state") or ""),
            "iteration": _e(status.get("iteration", "")),
            "logo": LOGO,
            "favicon": FAVICON,
            "header": part(_header, status, run_name, goal_text or ""),
            "summary": part(_summary, run_dir, goal_text or ""),
            "figures": part(_figures, status, steps, graph, goal_text or ""),
            "loop": part(_loop, status, its),
            "usage": part(_usage, status),
            "time_budget": part(_time_budget, status),
            "now": part(_now, status, decisions, goal_text or ""),
            "progress": part(_progress, its, status),
            "graph": part(_graph, graph, graph_version, graph_omitted),
            "graph_version": _e(graph_version or ""),
            "facts": part(_facts, status, steps, run_name, goal_text or ""),
            "stages": part(_stages, status),
            "criteria": part(_criteria, graph, goal_text or ""),
            "frontier": part(_frontier, graph),
            "story": part(_story, run_dir),
            "decisions": part(_decisions, decisions),
            "timeline": part(_timeline, graph),
            "generated": "",
            "version": "",
        }
        template = _template(repo)
        version = hashlib.sha256(_fill(template, parts).encode()).hexdigest()[:16]
        epoch = now if alive else _float(status.get("epoch"))
        live = str(status.get("state")) in LIVE_STATES
        stamp = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        parts["generated"] = f"Page written {_e(stamp)} by Ouroboros."
        parts["version"] = version
        page = _fill(template, parts).replace(_AGO, _e(_ago_text(epoch, now, live)))
        index = vdir / "index.html"
        _write(index, page)
        _write(vdir / "live.json", json.dumps({
            "version": version, "ts": epoch or now, "generated": now,
            "state": status.get("state"), "iteration": status.get("iteration"),
        }))
        return index
    except Exception:   # the view must never stop the loop
        return None


# -- the server ----------------------------------------------------------------

NO_STORE = {"index.html", "live.json", "story.html", "summary.txt", GRAPH_PAGE}
_TYPES = {".svg": "image/svg+xml", ".json": "application/json", ".html": "text/html; charset=utf-8",
          ".css": "text/css; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".webp": "image/webp",
          ".txt": "text/plain; charset=utf-8", ".csv": "text/plain; charset=utf-8", ".md": "text/plain; charset=utf-8"}
_EMPTY = (b'<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
          b'<title>No view yet</title><p style="font:16px system-ui;margin:2rem">This run has no view page yet. '
          b'It appears at the next stage change.</p>')


def _handler(root: Path) -> type[BaseHTTPRequestHandler]:
    root = root.resolve()

    class ViewHandler(BaseHTTPRequestHandler):
        server_version = "ouroboros-view"
        sys_version = ""

        def log_message(self, format, *args) -> None:   # quiet: a phone polls every 10 s
            pass

        def _send(self, code: int, body: bytes, ctype: str, name: str = "") -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store" if (name in NO_STORE or code != 200) else "no-cache")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _deny(self, code: HTTPStatus) -> None:
            self._send(code, f"{code.value} {code.phrase}\n".encode(), "text/plain; charset=utf-8")

        def do_GET(self) -> None:
            raw = urlsplit(self.path).path
            rel = unquote(raw)
            if "\x00" in rel or "\\" in rel:
                return self._deny(HTTPStatus.FORBIDDEN)
            rel = rel.lstrip("/") or "index.html"
            parts = rel.split("/")
            if any(p in ("..", ".") for p in parts):
                return self._deny(HTTPStatus.FORBIDDEN)
            try:
                path = (root / rel).resolve()
            except (OSError, RuntimeError):
                return self._deny(HTTPStatus.NOT_FOUND)
            if path != root and not path.is_relative_to(root):
                return self._deny(HTTPStatus.FORBIDDEN)
            if any(p.startswith(".") for p in path.relative_to(root).parts):   # tmp files, dotfiles
                return self._deny(HTTPStatus.NOT_FOUND)
            if not path.is_file():
                if rel == "index.html":
                    return self._send(HTTPStatus.NOT_FOUND, _EMPTY, "text/html; charset=utf-8", "index.html")
                return self._deny(HTTPStatus.NOT_FOUND)
            try:
                body = path.read_bytes()
            except OSError:
                return self._deny(HTTPStatus.NOT_FOUND)
            ctype = _TYPES.get(path.suffix.lower()) or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            self._send(HTTPStatus.OK, body, ctype, path.name if path.parent == root else "")

        do_HEAD = do_GET

        def _refuse(self) -> None:
            self._deny(HTTPStatus.METHOD_NOT_ALLOWED)

        do_POST = do_PUT = do_DELETE = do_PATCH = _refuse

    return ViewHandler


def make_server(run_dir: Path, host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    """A server for `<run_dir>/view/` only. Port 0 picks a free port (see `server.server_address`)."""
    vdir = view_dir(Path(run_dir))
    vdir.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer((host, port), _handler(vdir))
    server.daemon_threads = True
    return server


def _addresses(host: str) -> list[str]:
    """Names a browser can use for this host. For 0.0.0.0: localhost, the host name, the tailnet IP."""
    if host not in ("0.0.0.0", "::", ""):
        return [host]
    out = ["127.0.0.1", socket.gethostname()]
    try:
        ip = subprocess.run(["tailscale", "ip", "-4"], capture_output=True, text=True, timeout=2)
        out += [line.strip() for line in ip.stdout.splitlines() if line.strip()] if ip.returncode == 0 else []
    except (OSError, subprocess.SubprocessError):
        pass
    return list(dict.fromkeys(out))


def serve(run_dir: Path, *, host: str = "127.0.0.1", port: int = 8765) -> None:
    """Serve the run view until Ctrl-C."""
    server = make_server(run_dir, host, port)
    real = server.server_address[1]
    for name in _addresses(host):
        print(f"  http://{name}:{real}/")
    print("Ctrl-C stops the server.", flush=True)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print()
    finally:
        server.server_close()
