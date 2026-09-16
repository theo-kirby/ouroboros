"""The reporter: an observer outside the loop that pushes to a phone.

`ouroboros watch` is its own process. It reads the run directory the loop writes
-- status.json, iterations.jsonl, critic.jsonl, loop.log, NEEDS_HUMAN.md -- and
never the loop itself, so the loop cannot tell whether a reporter exists and a
reporter that crashes, hangs, or runs out of usage costs the run nothing.

Two tiers, because the urgent messages must not depend on a model. When usage
runs out, the reporter's model is usually out too:

- **triggers** are facts read straight off the files -- the run stopped, a
  harness is limited, NEEDS_HUMAN appeared, the critic called it stuck, the
  process died -- and go out as fixed text the moment they are seen;
- **digests** are a model's read of what landed, what is next and what is broken,
  written on a timer (`report.every`) and after the milestones (stopped,
  done_accepted). They fall back to the measured numbers when no model answers.

The reporter keeps its place in `reporter.json`, so each digest covers only what
happened since the last one, and a restart does not replay the run's history.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path
from typing import Callable

from .config import Config
from .harness import make_harness
from .harness.base import Result
from .notify import Notifier, clip

TERMINAL = ("stopped", "killed")
ALWAYS = ("started", "interval", "manual")     # never filtered by `report.on`
REPORTER_MAX_TURNS = 10
STREAK_FIRES = (3, 10, 20)                     # stuck/looping verdicts in a row worth a push
ENGINE_ERROR_COOLDOWN = 3600.0
LIMIT_COOLDOWN = 2 * 3600.0                    # the pool re-logs a block on every recheck
LOG_TAIL = 40
VERDICT_LINES = 40
# `run` starts the reporter and the loop at the same moment, and the loop takes a
# second or two to overwrite the last run's status.json. Until it does, the
# directory still says `killed`. So a reporter that finds a finished run on its
# very first pass waits this long for one to come alive before believing it.
STARTUP_GRACE = 180.0

_LIMIT_LINE = re.compile(
    r"harness (?P<h>\S+?):? (?P<why>is out of usage until .*|auth failure.*|hit its reserve.*|"
    r"repeated transient errors, treating as a limit.*)"
)
_ALL_BLOCKED = re.compile(r"every harness (in the chain )?is blocked")


@dataclass
class Event:
    kind: str
    title: str
    body: str
    priority: int = 0        # pushover: -1 quiet, 0 normal, 1 breaks through quiet hours
    digest: bool = False     # also worth a model-written read


@dataclass
class Snapshot:
    """What the run directory says at one moment."""
    status: dict | None
    steps: list[dict]
    decisions: list[dict]
    log_lines: list[str]
    needs_human: str | None
    pid_alive: bool
    now: float

    @property
    def state(self) -> str:
        return str((self.status or {}).get("state") or "?")

    @property
    def iteration(self) -> int:
        return int((self.status or {}).get("iteration") or 0)

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL


@dataclass
class DigestContext:
    trigger: str
    stats: str
    verdicts: str
    commits: str
    log: str
    needs_human: str
    goal: str
    plan: str
    run: str
    run_dir: Path
    max_chars: int


def fmt_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    h, m = divmod(seconds // 60, 60)
    return f"{h}h{m:02d}m" if h else f"{m}m"


def pid_alive(run_dir: Path) -> bool:
    pf = run_dir / "pid"
    try:
        os.kill(int(pf.read_text().strip()), 0)
        return True
    except (OSError, ValueError):
        return False


def read_snapshot(run_dir: Path, *, now: float | None = None) -> Snapshot:
    """The same files `Recorder` writes, read without it: a reader must not create directories."""
    now = now or time.time()
    if not run_dir.exists():
        return Snapshot(None, [], [], [], None, False, now)
    status = None
    try:
        status = json.loads((run_dir / "status.json").read_text())
    except (OSError, ValueError):
        pass
    try:
        log_lines = (run_dir / "loop.log").read_text(errors="replace").splitlines()
    except OSError:
        log_lines = []
    try:
        needs = (run_dir / "NEEDS_HUMAN.md").read_text()
    except OSError:
        needs = None
    decisions = run_dir / "critic.jsonl"
    if not decisions.exists():
        decisions = run_dir / "overseer.jsonl"   # runs from before the critic and the overseer were one role
    return Snapshot(status if isinstance(status, dict) else None, _jsonl(run_dir / "iterations.jsonl"),
                    _jsonl(decisions), log_lines, needs, pid_alive(run_dir), now)


def _jsonl(path: Path) -> list[dict]:
    """Like Recorder.read_jsonl, but a half-written last line (the loop is mid-write) is skipped, not fatal."""
    if not path.exists():
        return []
    out = []
    for line in path.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


# ----------------------------------------------------------------------------- the numbers
def _counts(decisions: list[dict]) -> str:
    counts: dict[str, int] = {}
    for d in decisions:
        v = str(d.get("verdict") or "?")
        counts[v] = counts.get(v, 0) + 1
    return ", ".join(f"{k} {v}" for k, v in sorted(counts.items(), key=lambda kv: -kv[1])) or "none"


def _iteration_counts(steps: list[dict]) -> str:
    commits = [s for s in steps if s.get("step") == "commit"]
    reverts = sum(1 for s in steps if s.get("step") == "revert")
    errors = sum(1 for s in steps if s.get("step") == "engine_error")
    changed = sum(1 for c in commits if c.get("changed"))
    recorded = sum(1 for c in commits if c.get("recorded"))
    text = f"{len(commits)} iterations, {changed} changed, {recorded} recorded, {reverts} reverted"
    return text + (f", {errors} engine errors" if errors else "")


def stats_text(snap: Snapshot, *, since_steps: int, since_decisions: int, run: str) -> str:
    """The measured read of the run, for a phone and for the model: never a guess."""
    st = snap.status or {}
    lines = [f"run {run} · iteration {snap.iteration} · state {snap.state} · {fmt_duration(float(st.get('elapsed_s') or 0))} elapsed"
             f" · harness {st.get('harness', '?')} · process {'alive' if snap.pid_alive else 'GONE'}"]
    if st.get("stop_reason"):
        lines.append(f"stop reason: {st['stop_reason']}")
    new_steps, new_decisions = snap.steps[since_steps:], snap.decisions[since_decisions:]
    lines.append(f"since last report: {_iteration_counts(new_steps)}; verdicts: {_counts(new_decisions)}")
    lines.append(f"whole run: {_iteration_counts(snap.steps)}; verdicts: {_counts(snap.decisions)}")
    if st.get("usage_compact"):
        lines.append(f"usage: {st['usage_compact']}")
    elif st.get("money"):
        lines.append("billed: " + ", ".join(f"{h} ${float(c):.2f}" for h, c in sorted(st["money"].items())))
    if st.get("limited"):
        lines.append("limited: " + "; ".join(f"{h} for {v}" for h, v in st["limited"].items()))
    loops = [s for s in new_steps if s.get("step") == "loop"]
    if loops:
        last = loops[-1]
        lines.append(f"loop detector: {last.get('signal')} x{last.get('streak')} (escalation {last.get('escalation')}/3)")
    if snap.decisions:
        last = snap.decisions[-1]
        if last.get("did"):
            lines.append(f"last: {last['did']}")
        if last.get("doing"):
            lines.append(f"now: {last['doing']}")
        if last.get("verdict") in ("stuck", "looping", "reject", "answer", "done_rejected", "done_accepted"):
            lines.append(f"last verdict: {last['verdict']} — {str(last.get('reason') or '')[:200]}")
    if st.get("state") == "backoff" and st.get("why"):
        lines.append(f"backing off {fmt_duration(float(st.get('seconds') or 0))}: {st['why']}")
    if snap.needs_human:
        lines.append("NEEDS_HUMAN.md exists")
    return "\n".join(lines)


def _age(snap: Snapshot) -> float:
    return snap.now - float((snap.status or {}).get("epoch") or snap.now)


# ----------------------------------------------------------------------------- the digest
def load_template() -> str:
    text = resources.files("ouroboros.skills").joinpath("ouroboros-reporter/SKILL.md").read_text()
    if text.startswith("---"):
        _, _, rest = text.partition("\n---\n")
        text = rest.lstrip("\n")
    return text


def build_reporter_prompt(ctx: DigestContext, template: str | None = None) -> str:
    tpl = template or load_template()
    return (
        tpl.replace("{run}", ctx.run)
        .replace("{run_dir}", str(ctx.run_dir))
        .replace("{trigger}", ctx.trigger)
        .replace("{stats}", ctx.stats.strip() or "(nothing measured)")
        .replace("{verdicts}", ctx.verdicts.strip() or "(none since the last report)")
        .replace("{commits}", ctx.commits.strip() or "(none, or git could not be read)")
        .replace("{log}", ctx.log.strip() or "(empty)")
        .replace("{needs_human}", ctx.needs_human.strip())
        .replace("{goal}", ctx.goal.strip() or "(no charter found)")
        .replace("{plan}", ctx.plan.strip() or "(no plan file)")
        .replace("{max_chars}", str(ctx.max_chars))
    )


class AgentReporter:
    """One read-only headless call per digest, down a chain of harnesses. Returns None when none answers."""

    def __init__(self, chain: list[tuple[str, str | None]], *, cwd: Path, timeout: float,
                 transcript_dir: Path | None = None, log=None, harnesses: dict | None = None) -> None:
        self.chain = list(chain)
        self.cwd = cwd
        self.timeout = timeout
        self.transcript_dir = transcript_dir
        self.log = log or (lambda line: None)
        self._harnesses = harnesses or {}

    def _harness(self, name: str):
        if name not in self._harnesses:
            self._harnesses[name] = make_harness(name)
        return self._harnesses[name]

    def write(self, ctx: DigestContext) -> str | None:
        prompt = build_reporter_prompt(ctx)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        for harness_name, model in self.chain:
            try:
                harness = self._harness(harness_name)
            except ValueError as exc:
                self.log(f"reporter: {exc}")
                continue
            log_path = (self.transcript_dir / f"{stamp}-{harness_name}.json") if self.transcript_dir else None
            try:
                result: Result = harness.run(prompt, cwd=self.cwd, timeout=self.timeout, model=model,
                                             log_path=log_path, tools="readonly", max_turns=REPORTER_MAX_TURNS)
            except Exception as exc:   # a driver bug is a missed digest, not a dead reporter
                self.log(f"reporter: {harness_name} raised {exc!r}")
                continue
            text = (result.text or "").strip()
            if result.ok and text:
                return text
            self.log(f"reporter: {harness_name} {result.kind}: {(result.error or text or 'no output')[:200]}")
        return None


# ----------------------------------------------------------------------------- the watcher
class Watcher:
    """Poll the run directory, turn what changed into pushes, remember where it left off."""

    def __init__(self, run_dir: Path, *, config: Config, notifier: Notifier | None, repo: Path | None = None,
                 reporter: AgentReporter | None = None, now: Callable[[], float] = time.time, log=print) -> None:
        self.run_dir = run_dir
        self.config = config
        self.notifier = notifier
        self.repo = repo or run_dir.parents[2]
        self.reporter = reporter
        self.now = now
        self.log = log
        self.state_path = run_dir / "reporter.json"
        self.reports_dir = run_dir / "reports"
        self.cursor: dict = self._load()
        self.stop_requested = False
        self.done = False
        self.waiting_since: float | None = None   # a finished run seen before this reporter ever primed

    # --- the place it keeps -------------------------------------------------
    def _load(self) -> dict:
        try:
            return json.loads(self.state_path.read_text())
        except (OSError, ValueError):
            return {}

    def _save(self) -> None:
        try:
            self.run_dir.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.cursor, indent=2, default=str))
            os.replace(tmp, self.state_path)
        except OSError as exc:
            self.log(f"reporter: could not save its place: {exc}")

    def _prime(self, snap: Snapshot) -> None:
        """Where a reporter starts: alerts from here, but the first digest covers the run.

        Alerts are about things happening, so a reporter that attaches to a run
        already under way must not replay the alerts it was not there for. A digest
        is an orientation, and until one has actually gone out there is no "since
        the last report" to speak of -- so the first one covers the whole run and
        every one after it is a delta. For the usual case, a reporter started with
        the loop, the two are the same thing.
        """
        self.cursor.update(
            steps=len(snap.steps), decisions=len(snap.decisions), log=len(snap.log_lines),
            digest_steps=0, digest_decisions=0, digest_sha="", digest_epoch=self.now(), primed=True,
            streak={"verdict": "", "n": 0}, limited={}, escalation={},
        )

    # --- one pass -----------------------------------------------------------
    def check(self, *, quick: bool = False) -> list[Event]:
        """Read, detect, push, save. Never raises: the reporter has nobody to catch it.

        `quick` sends the measured numbers in place of a model digest: the last pass
        before `ouroboros stop` takes the window down, where a model call would be cut off.
        """
        try:
            snap = read_snapshot(self.run_dir, now=self.now())
        except Exception as exc:
            self.log(f"reporter: could not read the run directory: {exc!r}")
            return []
        if snap.status is None:
            return []
        if not self.cursor.get("primed") and snap.terminal and not snap.pid_alive and self._starting_up(snap):
            return []
        events: list[Event] = []
        try:
            fresh = not self.cursor.get("primed")
            if fresh:
                self._prime(snap)
            events = self.detect(snap, fresh=fresh)
            self._advance(snap)
            self.deliver(events, snap, quick=quick)
        except Exception as exc:
            self.log(f"reporter: check failed: {exc!r}")
        self._save()   # whatever went wrong above, the place it reached is not re-read
        if snap.terminal and self.cursor.get("terminal_sent"):
            self.done = True
        return events

    def _starting_up(self, snap: Snapshot) -> bool:
        """True while a `killed` directory might still be a run that is only just booting.

        The alternative is announcing the *previous* run's ending as though it were
        news and exiting before the loop it was started to watch has drawn breath,
        which is what a reporter launched beside a run does every single time.
        """
        self.waiting_since = self.waiting_since if self.waiting_since is not None else snap.now
        if snap.now - self.waiting_since < STARTUP_GRACE:
            return True
        self.log(f"reporter: run {self.config.run} is already over "
                 f"({(snap.status or {}).get('stop_reason') or snap.state}); nothing to watch. "
                 f"`ouroboros watch --once` reports on it.")
        self.done = True
        return True

    def _advance(self, snap: Snapshot) -> None:
        self.cursor.update(steps=len(snap.steps), decisions=len(snap.decisions), log=len(snap.log_lines))

    def _new(self, snap: Snapshot, key: str, items: list) -> list:
        seen = int(self.cursor.get(key) or 0)
        return items[min(seen, len(items)):]   # a rewritten (shorter) file starts the count over

    def detect(self, snap: Snapshot, *, fresh: bool = False) -> list[Event]:
        run = self.config.run
        cfg = self.config.report
        events: list[Event] = []
        st = snap.status or {}

        if fresh:
            watching = "watching from here" if snap.iteration else "watching"
            stop = ", ".join(f"{k}={v}" for k, v in self.config.stop.model_dump().items() if v is not None) or "none"
            events.append(Event("started", f"{run}: {watching}",
                                f"branch {st.get('branch', '?')} · harness {st.get('harness', '?')} · memory {st.get('memory', '?')}\n"
                                f"iteration {snap.iteration} · state {snap.state}\nstops at: {stop}\n"
                                + (f"reports every {cfg.every}" if cfg.every else "reports on triggers only"), priority=-1))

        # NEEDS_HUMAN: the one file that means the loop could not decide for you.
        if snap.needs_human and snap.needs_human != self.cursor.get("needs_human"):
            self.cursor["needs_human"] = snap.needs_human
            body = "\n".join(l for l in snap.needs_human.splitlines()[1:] if l.strip())
            events.append(Event("needs_human", f"{run}: needs a human", body, priority=1))
        elif not snap.needs_human:
            self.cursor.pop("needs_human", None)

        new_steps = self._new(snap, "steps", snap.steps)
        new_decisions = self._new(snap, "decisions", snap.decisions)
        new_log = self._new(snap, "log", snap.log_lines)

        for s in new_steps:
            kind = s.get("step")
            if kind == "engine_error":
                last = float(self.cursor.get("engine_error_epoch") or 0)
                if snap.now - last >= ENGINE_ERROR_COOLDOWN:
                    self.cursor["engine_error_epoch"] = snap.now
                    events.append(Event("engine_error", f"{run}: engine error at #{s.get('iteration')}",
                                        str(s.get("error") or "")[:400] + "\nThe loop backs off and continues.", priority=0))
            elif kind == "loop" and s.get("acted") and int(s.get("escalation") or 0) >= 2:
                sig = str(s.get("signal") or "?")
                sent = self.cursor.setdefault("escalation", {})
                if int(sent.get(sig) or 0) < int(s.get("escalation") or 0):
                    sent[sig] = int(s.get("escalation") or 0)
                    events.append(Event("looping", f"{run}: loop detected, {sig} x{s.get('streak')}",
                                        f"escalation {s.get('escalation')}/3 at #{s.get('iteration')}: "
                                        + {2: "forcing a re-plan", 3: "rotating the actor to its fallback"}.get(int(s.get("escalation") or 0), "named to the actor")
                                        + "\n" + "; ".join(str(e) for e in (s.get("evidence") or [])[:3]), priority=0))
            elif kind == "revert":
                # A revert that failed leaves rejected work on the branch. It reaches
                # the phone whatever `report.on` says, because the engine writes
                # NEEDS_HUMAN.md for it too, and that trigger is on by default.
                events.append(Event("reject", f"{run}: #{s.get('iteration')} rejected and reverted",
                                    self._reason_for(snap, s.get("iteration")) + (f"\nrevert FAILED: {s['error']}" if s.get("error") else ""),
                                    priority=1 if s.get("error") else -1))

        streak = self.cursor.setdefault("streak", {"verdict": "", "n": 0})
        for d in new_decisions:
            v = str(d.get("verdict") or "")
            if v in ("stuck", "looping"):
                streak["n"] = streak["n"] + 1 if streak["verdict"] == v else 1
                streak["verdict"] = v
                if streak["n"] in STREAK_FIRES:
                    events.append(Event(v, f"{run}: {v} x{streak['n']}",
                                        f"#{d.get('iteration')}: {str(d.get('reason') or '')[:300]}\n"
                                        f"{'next: ' + str(d.get('doing'))[:160] if d.get('doing') else ''}".strip(), priority=0))
            else:
                streak.update(verdict="", n=0)
            if v == "done_accepted":
                events.append(Event("done_accepted", f"{run}: the critic accepted done at #{d.get('iteration')}",
                                    str(d.get("reason") or "")[:400], priority=0, digest=True))

        limited = self.cursor.setdefault("limited", {})
        for line in new_log:
            if _ALL_BLOCKED.search(line):
                last = float(limited.get("*") or 0)
                if snap.now - last >= LIMIT_COOLDOWN:
                    limited["*"] = snap.now
                    events.append(Event("limited", f"{run}: every harness is out of usage",
                                        line.split(" ", 1)[-1][:400] + "\nThe loop waits for the earliest reset.", priority=1))
                continue
            m = _LIMIT_LINE.search(line)
            if m:
                h = m.group("h")
                last = float(limited.get(h) or 0)
                if snap.now - last >= LIMIT_COOLDOWN:
                    limited[h] = snap.now
                    events.append(Event("limited", f"{run}: harness {h} limited", m.group("why")[:400], priority=0))

        # The process is gone, or has said nothing for too long, while the run claims to be working.
        if not snap.terminal:
            if not snap.pid_alive:
                if not self.cursor.get("dead_sent"):
                    self.cursor["dead_sent"] = True
                    events.append(Event("silent", f"{run}: the loop process is gone",
                                        f"state {snap.state} at #{snap.iteration}, status last written {fmt_duration(_age(snap))} ago, "
                                        f"and no stop was recorded. Check `ouroboros status`.", priority=1))
            else:
                self.cursor.pop("dead_sent", None)
                age = _age(snap)
                if age >= cfg.silent_after_seconds and self.cursor.get("silent_epoch") != st.get("epoch"):
                    self.cursor["silent_epoch"] = st.get("epoch")
                    events.append(Event("silent", f"{run}: quiet for {fmt_duration(age)}",
                                        f"status.json was last written {fmt_duration(age)} ago (state {snap.state}, #{snap.iteration}) "
                                        f"and the process is alive. A harness call may be hung past its timeout.", priority=0))

        if snap.terminal and not self.cursor.get("terminal_sent"):
            self.cursor["terminal_sent"] = True
            why = st.get("stop_reason") or (f"signal {st['signal']}" if st.get("signal") else snap.state)
            events.append(Event("stopped", f"{run}: {snap.state} — {why}"[:120],
                                f"{why}\n{_iteration_counts(snap.steps)}\nverdicts: {_counts(snap.decisions)}", priority=0, digest=True))
        elif not snap.terminal:
            self.cursor.pop("terminal_sent", None)   # the same run name continued after a stop

        every = cfg.every_seconds
        if every and not snap.terminal and snap.now - float(self.cursor.get("digest_epoch") or 0) >= every:
            events.append(Event("interval", f"{run} #{snap.iteration}: {cfg.every} report", "", priority=-1, digest=True))
        return events

    def _reason_for(self, snap: Snapshot, iteration) -> str:
        for d in reversed(snap.decisions):
            if d.get("iteration") == iteration:
                return str(d.get("reason") or "")[:300]
        return ""

    # --- sending --------------------------------------------------------------
    def deliver(self, events: list[Event], snap: Snapshot, *, quick: bool = False) -> None:
        wanted = set(self.config.report.on)
        events = [e for e in events if e.kind in ALWAYS or e.kind in wanted]
        # One digest per pass, however many milestones fired together: it rides on
        # the first of them, and the others go out as their fixed text.
        digest = None
        for e in events:
            body = e.body
            if e.digest and digest is None:
                digest = self.digest(snap, trigger=", ".join(x.kind for x in events if x.digest), model=not quick)
                body = (e.body + "\n\n" if e.body else "") + digest
            self._send(e.title, body, e.priority)

    def _send(self, title: str, body: str, priority: int) -> None:
        """One push. A channel that is down costs this message, never the next one."""
        text = clip(body, self.config.report.max_chars)
        if self.notifier is None:
            self.log(f"reporter: (no channel) {title}\n{text}")
            return
        try:
            ok = self.notifier.send(title, text, priority=priority)
        except Exception as exc:
            self.log(f"reporter: [{self.notifier.name}] raised {exc!r}; {title} was not sent")
            return
        self.log(f"reporter: {'sent' if ok else 'FAILED'} [{self.notifier.name}] {title}")

    # --- the digest -----------------------------------------------------------
    def once(self, *, trigger: str = "manual") -> str | None:
        """One digest now, pushed and kept; None when the run has not started."""
        snap = read_snapshot(self.run_dir, now=self.now())
        if snap.status is None:
            return None
        if not self.cursor.get("primed"):
            self._prime(snap)
            self.cursor["terminal_sent"] = snap.terminal   # a run already over is not news to announce later
        text = self.digest(snap, trigger=trigger)
        self._send(f"{self.config.run} #{snap.iteration}: report", text, 0)
        self._save()
        return text

    def digest(self, snap: Snapshot, *, trigger: str, model: bool = True) -> str:
        """The model's read since the last digest, or the measured numbers when no model answers."""
        since_steps = int(self.cursor.get("digest_steps") or 0)
        since_dec = int(self.cursor.get("digest_decisions") or 0)
        stats = stats_text(snap, since_steps=since_steps, since_decisions=since_dec, run=self.config.run)
        text = None
        if self.reporter is not None and model:
            ctx = DigestContext(
                trigger=trigger, stats=stats,
                verdicts="\n".join(f"- #{d.get('iteration')} {d.get('verdict')}: {str(d.get('reason') or '')[:200]}"
                                   + (f" (did: {d['did']})" if d.get("did") else "")
                                   for d in snap.decisions[since_dec:][-VERDICT_LINES:]),
                commits=self._commits_since(snap), log="\n".join(snap.log_lines[-LOG_TAIL:]),
                needs_human=f"NEEDS_HUMAN.md:\n\n{snap.needs_human}" if snap.needs_human else "",
                goal=self._read(self.repo / self.config.goal)[:6000], plan=self._plan()[:3000],
                run=self.config.run, run_dir=self.run_dir, max_chars=self.config.report.max_chars,
            )
            try:
                text = self.reporter.write(ctx)
            except Exception as exc:
                self.log(f"reporter: digest failed: {exc!r}")
        body = text or (stats if not model else f"(no model answered; the numbers)\n{stats}")
        self._keep(trigger, body, stats)
        self.cursor.update(digest_epoch=snap.now, digest_steps=len(snap.steps), digest_decisions=len(snap.decisions),
                           digest_sha=self._head(snap), digest_iteration=snap.iteration)
        return body

    def _keep(self, trigger: str, body: str, stats: str) -> None:
        """The full digest, on disk beside the run: the phone gets the first `max_chars` of it."""
        try:
            self.reports_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
            (self.reports_dir / f"{stamp}-{trigger}.md").write_text(
                f"# {self.config.run}: {trigger}\n\n{body}\n\n---\n\n```\n{stats}\n```\n")
        except OSError:
            pass

    def _read(self, path: Path) -> str:
        try:
            return path.read_text()
        except OSError:
            return ""

    def _plan(self) -> str:
        for cand in (self.repo / self.config.plan.md, self.repo / ".ouroboros" / "plan.md"):
            text = self._read(cand)
            if text.strip():
                return text
        return ""

    def _git(self, *args: str) -> str:
        try:
            proc = subprocess.run(["git", *args], cwd=self.repo, capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            return ""
        return proc.stdout.strip() if proc.returncode == 0 else ""

    def _head(self, snap: Snapshot) -> str:
        branch = str((snap.status or {}).get("branch") or self.config.branch)
        return self._git("rev-parse", branch)

    def _commits_since(self, snap: Snapshot) -> str:
        branch = str((snap.status or {}).get("branch") or self.config.branch)
        last = self.cursor.get("digest_sha")
        span = f"{last}..{branch}" if last else branch
        out = self._git("log", "--oneline", "--no-decorate", "-n", "40", span)
        if not out and last:
            out = self._git("log", "--oneline", "--no-decorate", "-n", "15", branch)
        return out

    # --- the loop -------------------------------------------------------------
    def run(self, *, sleeper: Callable[[float], None] = time.sleep) -> None:
        """Until the run ends and its last word is sent, or the reporter is told to stop."""
        poll = self.config.report.poll_seconds
        while True:
            self.check()
            if self.done:
                self.log("reporter: the run is over and reported; exiting")
                return
            deadline = self.now() + poll
            while self.now() < deadline:
                if self.stop_requested:
                    self.check(quick=True)   # `ouroboros stop` killed the loop first; say so before going
                    return
                sleeper(min(1.0, max(0.0, deadline - self.now())))
            if self.stop_requested:
                self.check(quick=True)
                return
