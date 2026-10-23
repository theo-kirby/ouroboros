"""`ouroboros view --debug`: the run page on a copy of the run, with a panel that sets its state.

The panel sets the stage, the iteration, the time in the stage and the usage, so each
animation of the page can be seen without a live run. Every change goes to the copy's
`status.json` and the page is written again; the run's own directory is never written.
"""
from __future__ import annotations

import copy
import json
import shutil
import tempfile
import threading
import time
from http import HTTPStatus
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from . import view

STATES = ("starting", "work", "critique", "scribe", "idle", "backoff", "stopped")
DEBUG_PATH = "/__debug"
OTHER_MODEL = {"claude": "claude-opus-5-5", "codex": "gpt-5.5-codex"}


def copy_run(run_dir: Path) -> Path:
    """A temp copy of the run directory without its transcripts (the page does not read them)."""
    dest = Path(tempfile.mkdtemp(prefix="ouroboros-debug-")) / Path(run_dir).name
    shutil.copytree(run_dir, dest, ignore=shutil.ignore_patterns("transcripts", "reverted"))
    return dest


def _float(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def apply(base: dict, knobs: dict, now: float | None = None) -> dict:
    """The base status with the panel's knobs on it. Unknown or broken knobs are ignored."""
    now = time.time() if now is None else now
    st = copy.deepcopy(base)
    state = knobs.get("state")
    if state in STATES:
        st["state"] = state
    if "iteration" in knobs:
        st["iteration"] = max(0, int(_float(knobs["iteration"], 0)))
    st["epoch"] = now
    st["stage_since"] = now - max(0.0, _float(knobs.get("stage_s"), 0))
    elapsed = max(0.0, _float(knobs.get("elapsed_min"), (st.get("elapsed_s") or 0) / 60) * 60)
    st["elapsed_s"] = elapsed
    st["started_at"] = now - elapsed
    if "budget_h" in knobs:
        st["time_budget_s"] = max(0.0, _float(knobs["budget_h"], 0) * 3600) or None
    if st.get("state") == "backoff":
        st.setdefault("seconds", 300)
        st.setdefault("why", "the 5-hour window is full")
    if st.get("state") == "stopped":
        st.setdefault("stop_reason", "Stopped by hand.")

    actor = knobs.get("actor")
    models = st.get("models") if isinstance(st.get("models"), dict) else {}
    if actor in OTHER_MODEL:
        models = {h: {m: [r for r in rs if r != "actor"] for m, rs in ms.items()} for h, ms in models.items()}
        models = {h: {m: rs for m, rs in ms.items() if rs} for h, ms in models.items()}
        models = {h: ms for h, ms in models.items() if ms}
        mine = models.setdefault(actor, {})
        name = next(iter(mine), OTHER_MODEL[actor])
        mine.setdefault(name, []).insert(0, "actor")
        st["models"] = models

    usage = st.get("usage") if isinstance(st.get("usage"), dict) else {}
    windows = {"five_hour": (300, knobs.get("five_hour")), "seven_day": (10080, knobs.get("seven_day"))}
    for harness in set(usage) | set(models):
        u = usage.setdefault(harness, {"harness": harness})
        u.setdefault("windows", {})
        u.setdefault("run_spent", {})
        for name, (minutes, pct) in windows.items():
            if pct is None:
                continue
            took = max(0.0, _float(pct, 0) / 100)
            w = u["windows"].setdefault(name, {"minutes": minutes})
            w["utilization"] = min(1.0, took)
            w["resets_at"] = now + minutes * 60 * max(0.05, 1 - min(took, 1.0))
            u["run_spent"][name] = took
    st["usage"] = usage
    return st


PANEL = """
<div id="dbg" class="dbg">
<style>
.dbg{position:fixed;right:12px;bottom:12px;z-index:50;width:min(320px,calc(100vw - 24px));max-height:calc(100vh - 24px);overflow:auto;
background:var(--paper);color:var(--ink);border:1px solid var(--ink);box-shadow:0 6px 24px rgba(0,0,0,.18);font:13px/1.4 var(--sans);padding:10px 12px}
.dbg h4{margin:0 0 6px;font:600 .7rem var(--sans);letter-spacing:.12em;text-transform:uppercase;display:flex;justify-content:space-between}
.dbg.min>*:not(h4){display:none}.dbg.min{width:auto}
.dbg button{font:inherit;border:1px solid var(--rule);background:none;color:inherit;border-radius:2px;padding:2px 7px;cursor:pointer}
.dbg button.on{background:var(--acc);border-color:var(--acc);color:var(--paper)}
.dbg .st{display:flex;flex-wrap:wrap;gap:4px;margin:0 0 8px}
.dbg label{display:grid;grid-template-columns:7.5em 1fr 3.4em;align-items:center;gap:6px;margin:3px 0}
.dbg label output{text-align:right;font-variant-numeric:tabular-nums}
.dbg input[type=range]{width:100%}
.dbg .row{display:flex;gap:6px;align-items:center;margin:8px 0 0;flex-wrap:wrap}
.dbg .msg{color:var(--mute);font-size:.75rem;margin-top:6px}
</style>
<h4><span>Debug · not the live run</span><button type="button" data-min>–</button></h4>
<div class="st" data-group="state"></div>
<label>Iteration<input type="number" min="0" data-k="iteration" style="width:100%"><output></output></label>
<label>In stage<input type="range" min="0" max="3600" step="15" data-k="stage_s"><output></output></label>
<label>Elapsed<input type="range" min="0" max="720" step="5" data-k="elapsed_min"><output></output></label>
<label>Budget<input type="range" min="0" max="24" step="1" data-k="budget_h"><output></output></label>
<label>5-hour used<input type="range" min="0" max="150" step="1" data-k="five_hour"><output></output></label>
<label>Weekly used<input type="range" min="0" max="150" step="1" data-k="seven_day"><output></output></label>
<div class="row">Actor on <span class="st" data-group="actor" style="margin:0"></span></div>
<div class="row"><button type="button" data-play>▶ Auto-play</button>
<label style="display:flex;margin:0">every <input type="number" min="1" max="60" value="4" data-every style="width:3.5em"> s</label>
<button type="button" data-reset>Reset</button></div>
<p class="msg" data-msg></p>
</div>
<script>
(function () {
  var box = document.getElementById("dbg"), K = __KNOBS__, STATES = __STATES__, base = JSON.parse(JSON.stringify(K));
  var fmt = {stage_s: function (v) { return v < 60 ? v + " s" : Math.round(v / 60) + " min"; },
             elapsed_min: function (v) { return v < 60 ? v + " min" : (v / 60).toFixed(1) + " h"; },
             budget_h: function (v) { return +v ? v + " h" : "none"; },
             five_hour: function (v) { return v + "%"; }, seven_day: function (v) { return v + "%"; },
             iteration: function () { return ""; }};
  function buttons(group, values) {
    var g = box.querySelector('[data-group="' + group + '"]');
    g.innerHTML = "";
    values.forEach(function (v) {
      var b = document.createElement("button");
      b.type = "button"; b.textContent = v; b.className = K[group] === v ? "on" : "";
      b.onclick = function () { K[group] = v; send(); };
      g.appendChild(b);
    });
  }
  function draw() {
    buttons("state", STATES);
    buttons("actor", ["claude", "codex"]);
    box.querySelectorAll("[data-k]").forEach(function (i) {
      var k = i.getAttribute("data-k");
      if (document.activeElement !== i) i.value = K[k];
      i.parentNode.querySelector("output").textContent = fmt[k](K[k]);
    });
  }
  var pending = null, inflight = false;
  function send() {
    draw();
    if (inflight) { pending = true; return; }
    inflight = true;
    fetch("__DEBUG__", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(K)})
      .then(function (r) { return r.json(); })
      .then(function (j) { box.querySelector("[data-msg]").textContent = j.ok ? "page written " + new Date().toLocaleTimeString() : (j.error || "failed"); return window.ouroborosSwap && window.ouroborosSwap(); })
      .catch(function (e) { box.querySelector("[data-msg]").textContent = String(e); })
      .then(function () { inflight = false; if (pending) { pending = null; send(); } });
  }
  box.querySelectorAll("[data-k]").forEach(function (i) {
    i.addEventListener("input", function () { K[i.getAttribute("data-k")] = +i.value; draw(); });
    i.addEventListener("change", function () { K[i.getAttribute("data-k")] = +i.value; send(); });
  });
  var timer = null, CYCLE = ["work", "critique", "scribe", "idle"];
  function step() {
    var at = CYCLE.indexOf(K.state);
    var next = CYCLE[(at + 1) % CYCLE.length];
    if (next === "work") K.iteration = (+K.iteration || 0) + 1;
    K.state = next; K.stage_s = 0; send();
  }
  box.querySelector("[data-play]").onclick = function () {
    var b = this;
    if (timer) { clearInterval(timer); timer = null; b.textContent = "▶ Auto-play"; b.className = ""; return; }
    var s = Math.max(1, +box.querySelector("[data-every]").value || 4);
    step(); timer = setInterval(step, s * 1000); b.textContent = "■ Stop"; b.className = "on";
  };
  box.querySelector("[data-reset]").onclick = function () { K = JSON.parse(JSON.stringify(base)); send(); };
  box.querySelector("[data-min]").onclick = function () { box.classList.toggle("min"); this.textContent = box.classList.contains("min") ? "+" : "–"; };
  draw();
})();
</script>
"""


def knobs_of(status: dict) -> dict:
    """The panel's starting values, read from the real status."""
    usage = status.get("usage") if isinstance(status.get("usage"), dict) else {}
    spent: dict[str, float] = {}
    for u in usage.values():
        for name, took in ((u or {}).get("run_spent") or {}).items():
            spent[name] = max(spent.get(name, 0.0), _float(took, 0))
    actor = "claude"
    for harness, ms in (status.get("models") or {}).items():
        if any("actor" in (rs or []) for rs in (ms or {}).values()):
            actor = harness
    state = status.get("state") if status.get("state") in STATES else "work"
    return {"state": state, "iteration": int(_float(status.get("iteration"), 1)), "stage_s": 60,
            "elapsed_min": round(_float(status.get("elapsed_s"), 0) / 60),
            "budget_h": round(_float(status.get("time_budget_s"), 0) / 3600),
            "five_hour": round(100 * spent.get("five_hour", 0)), "seven_day": round(100 * spent.get("seven_day", 0)),
            "actor": actor}


def panel(knobs: dict) -> str:
    return (PANEL.replace("__KNOBS__", json.dumps(knobs)).replace("__STATES__", json.dumps(list(STATES)))
            .replace("__DEBUG__", DEBUG_PATH))


def make_server(run_dir: Path, repo: Path, *, goal_text: str, graph=None,
                host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    """A view server on a copy of `run_dir`, with the debug panel on its page and a POST to set the state."""
    work = copy_run(Path(run_dir))
    base = view._read_json(work / "status.json")
    lock = threading.Lock()
    html = panel(knobs_of(base))

    def render(knobs: dict) -> bool:
        with lock:
            (work / "status.json").write_text(json.dumps(apply(base, knobs)))
            return view.render(work, repo, goal_text=goal_text, graph=graph) is not None

    render(knobs_of(base))
    Base = view._handler(view.view_dir(work))

    class DebugHandler(Base):
        def do_GET(self) -> None:
            if urlsplit(self.path).path not in ("/", "/index.html"):
                return super().do_GET()
            try:
                page = (view.view_dir(work) / "index.html").read_text()
            except OSError:
                return super().do_GET()
            body = page.replace("</body>", html + "</body>", 1).encode()
            self._send(HTTPStatus.OK, body, "text/html; charset=utf-8", "index.html")

        def do_POST(self) -> None:
            if urlsplit(self.path).path != DEBUG_PATH:
                return self._refuse()
            try:
                size = min(int(self.headers.get("Content-Length") or 0), 65536)
                knobs = json.loads(self.rfile.read(size) or b"{}")
                ok = render(knobs if isinstance(knobs, dict) else {})
                body = json.dumps({"ok": ok}).encode()
            except Exception as exc:   # a bad knob shows in the panel; the server stays up
                body = json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}).encode()
            self._send(HTTPStatus.OK, body, "application/json", "")

    server = ThreadingHTTPServer((host, port), DebugHandler)
    server.daemon_threads = True
    server.debug_dir = work
    return server


def serve(run_dir: Path, repo: Path, *, goal_text: str, graph=None, host: str = "127.0.0.1", port: int = 8765) -> None:
    server = make_server(run_dir, repo, goal_text=goal_text, graph=graph, host=host, port=port)
    print(f"  http://{host}:{server.server_address[1]}/   (debug: a copy at {server.debug_dir})")
    print("Ctrl-C stops the server.", flush=True)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print()
    finally:
        server.server_close()
        shutil.rmtree(server.debug_dir.parent, ignore_errors=True)
