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


def view_dir(run_dir: Path) -> Path:
    return Path(run_dir) / VIEW


def story_path(run_dir: Path) -> Path:
    return view_dir(run_dir) / "story.html"


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


def _facts(status: dict, steps: list[dict], run_name: str, goal_text: str) -> str:
    state = str(status.get("state") or "unknown")
    word = STATE_WORD.get(state, state)
    if state == "work" and status.get("housekeeping"):
        word = "actor (reconcile)"
    if state == "backoff" and status.get("seconds"):
        word = f"backoff {_duration(status.get('seconds'))}"
    commits = sum(1 for s in steps if s.get("step") == "commit" and s.get("sha"))
    reverts = sum(1 for s in steps if s.get("step") == "revert")
    verdict = status.get("last_verdict")
    epoch = _float(status.get("epoch"))
    live = "1" if state in LIVE_STATES else "0"
    mission = _clip(charter.mission(goal_text or ""), MISSION_LIMIT)
    goal = _goal_name(goal_text)

    def cell(label: str, value: str, cls: str = "") -> str:
        return f'<div class="fact{(" " + cls) if cls else ""}"><dt>{_e(label)}</dt><dd>{value}</dd></div>'

    cells = [
        cell("State", _chip(word, STATE_CLASS.get(state, ""))),
        cell("Iteration", f'<span class="num">{_e(status.get("iteration", "–"))}</span>'),
        cell("Elapsed", f'<span class="num">{_e(_duration(status.get("elapsed_s")))}</span>'),
        cell("Last verdict", _chip(verdict, VERDICT_CLASS.get(str(verdict), "")) if verdict else "–"),
        cell("Commits", f'<span class="num">{commits}</span>'
             + (f' <span class="sub-inline">· {reverts} reverted</span>' if reverts else "")),
        cell("Branch", f"<code>{_e(status.get('branch') or '–')}</code>"),
        cell("Harness", _e(status.get("harness") or "–")),
        cell("Memory", _e(status.get("memory") or "–")),
    ]
    if status.get("usage_compact"):
        cells.append(cell("Usage", _e(status["usage_compact"]), "wide"))
    if status.get("stop_reason"):
        cells.append(cell("Stop reason", _e(status["stop_reason"]), "wide"))
    if state == "backoff" and status.get("why"):
        cells.append(cell("Backoff because", _e(_clip(str(status["why"]), 300)), "wide"))
    updated = (f'<span id="ago" class="ago" data-ts="{epoch:.0f}" data-live="{live}">{_AGO}</span>'
               if epoch else '<span class="ago">no status yet</span>')
    head = [
        '<header class="top">',
        f'<p class="kicker">Ouroboros run <strong>{_e(run_name)}</strong> · {updated}</p>',
        f"<h1>{_e(goal or run_name)}</h1>",
    ]
    if mission:
        head.append(f'<p class="mission" title="{_e(mission)}">{_e(mission)}</p>')
    head += ["</header>", f'<dl class="facts">{"".join(cells)}</dl>']
    return "\n".join(head)


def _stages(status: dict) -> str:
    state = str(status.get("state") or "")
    items = []
    for key, label in STAGES:
        on = state == key
        items.append(f'<li class="stage{" now" if on else ""}"{" aria-current=\"step\"" if on else ""}>'
                     f'{_e(label)}{"<span class=\"sr\"> (now)</span>" if on else ""}</li>')
    rest = "" if state in dict(STAGES) else f'<li class="stage rest">{_e(STATE_WORD.get(state, state) or "–")}</li>'
    arrow = '<li class="arrow" aria-hidden="true">→</li>'
    return (f'<ol class="stages" aria-label="Loop stage">{arrow.join(items)}'
            f'<li class="arrow" aria-hidden="true" title="then the loop starts again">↺</li>{rest}</ol>')


def _criteria(graph: Graph | None, goal_text: str) -> str:
    gaps = []
    if graph is not None and graph.ok:
        gaps = [n for n in graph.claims("state")
                if str(n.get("slug") or n.get("id") or "").startswith("gap-")]
    rows = []
    if gaps:
        live = [n for n in gaps if n.get("status") != "superseded"]
        for n in gaps:
            st = str(n.get("status") or "–")
            now = first_line(section(n.get("content", ""), "Current"), 240)
            cites = len(graph.cites(n))
            rows.append(
                f'<li class="row{" gone" if st == "superseded" else ""}">{_chip("met" if st == "working" else st, STATUS_CLASS.get(st, ""))}'
                f'<div class="what"><strong>{_e(n.get("title") or graph.label(n["id"]))}</strong>'
                f'{f"<span class=\"sub\">{_e(now)}</span>" if now else ""}</div>'
                f'<span class="num count" title="records cited">{cites} rec</span></li>')
        total = len(live)
        met = sum(1 for n in live if n.get("status") == "working")
        source = "from the memory's gap claims"
    else:
        every = charter.done_criteria(goal_text or "", include_checked=True)
        open_ = set(charter.done_criteria(goal_text or ""))
        for c in every:
            done = c not in open_
            rows.append(f'<li class="row">{_chip("ticked" if done else "open", "ok" if done else "warn")}'
                        f'<div class="what">{_e(c)}</div></li>')
        total, met = len(every), sum(1 for c in every if c not in open_)
        source = "from the charter (no gap claims in memory yet)"
    if not rows:
        return '<p class="quiet">The charter declares no done criteria.</p>'
    pct = round(100 * met / total) if total else 0
    return (f'<p class="meter"><span class="num">{met} of {total}</span> met <span class="quiet">· {source}</span></p>'
            f'<div class="bar" role="img" aria-label="{met} of {total} criteria met"><span style="width:{pct}%"></span></div>'
            f'<ul class="rows">{"".join(rows)}</ul>')


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
                        f'<div class="what"><strong>{_e(n.get("title") or "")}</strong> <code>{_e(graph.label(n["id"]))}</code>'
                        f'{f"<span class=\"sub\">{_e(now)}</span>" if now else ""}</div></li>')
    tail = len(graph.tail())
    note = f'<p class="quiet"><span class="num">{tail}</span> record(s) not folded into state yet.</p>' if tail else ""
    if not rows:
        return '<p class="quiet">Nothing open, broken or blocked.</p>' + note
    return f'<ul class="rows">{"".join(rows)}</ul>{note}'


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
        doing = f'<span class="sub">{_e(_clip(str(d["doing"]), 200))}</span>' if d.get("doing") else ""
        items.append(
            f'<li><details id="{_id("d", d.get("iteration"))}"><summary>'
            f'<span class="num it">#{_e(d.get("iteration", "?"))}</span>{_chip(v, VERDICT_CLASS.get(v, ""))}'
            f'<span class="what">{_e(_clip(str(d.get("reason") or ""), 260))}{doing}</span></summary>'
            f'<dl class="more">{"".join(body)}</dl></details></li>')
    return f'<div class="strip" aria-label="Last verdicts, oldest first">{strip}</div><ul class="log">{"".join(items)}</ul>'


def _record_item(graph: Graph, r: dict) -> str:
    content = r.get("content", "")
    what = first_line(section(content, "What"), 400)
    result = first_line(section(content, "Result"), 400)
    folded = _chip("folded", "mute") if r.get("folded") else _chip("unfolded", "warn")
    body = "".join(f"<dt>{k}</dt><dd>{_e(v)}</dd>" for k, v in (("What", what), ("Result", result)) if v)
    return (f'<li><details id="{_id("r", r.get("id"))}"><summary>'
            f'<code class="it">{_e(graph.label(r["id"]))}</code><span class="what">{_e(r.get("title") or "")}'
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


def _story(run_dir: Path) -> str:
    text = _read_text(story_path(run_dir))
    if not text.strip():
        return '<p class="quiet">The scribe has not written the story yet.</p>'
    return text


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

        parts = {
            "title": _e(f"{run_name} · {goal}" if goal else f"{run_name} · Ouroboros"),
            "run": _e(run_name),
            "goal": _e(goal),
            "mission": _e(_clip(charter.mission(goal_text or ""), MISSION_LIMIT)),
            "state": _e(status.get("state") or ""),
            "iteration": _e(status.get("iteration", "")),
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

NO_STORE = {"index.html", "live.json", "story.html"}
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
