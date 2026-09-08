"""What a run spends when there is no invoice.

Dollars are the wrong meter for a subscription. Claude Code and Codex bill a flat
fee and then ration by window -- five hours, seven days -- so the scarce thing a
run consumes is a fraction of a window, not money. Both report it, in different
places and different units:

- Claude Code emits a `rate_limit_event` line in its stream whose `unifiedWindows`
  carry `utilization` as a 0..1 fraction and `resetsAt` as an epoch.
- Codex writes it to its own rollout log, not the `exec --json` stream: a
  `token_count` event whose `rate_limits.primary`/`.secondary` carry
  `used_percent` out of 100, a `window_minutes`, and `resets_at`.

Pi against an API key is genuinely billed per call, so it keeps reporting dollars
and has no windows. A run can meter both at once and neither number is the other.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

# Window lengths we can name. Anything else is reported by its own length.
_NAMED = {300: "five_hour", 10080: "seven_day", 1440: "daily", 60: "hourly"}
# The same windows in the width a status line can afford.
_SHORT = {"five_hour": "5h", "seven_day": "7d", "daily": "24h", "hourly": "1h"}
# A window this long or longer is worth stopping a run over: it will not come back
# this run. Shorter windows heal on their own, and the harness fallback covers them.
STOP_WINDOW_MINUTES = 1440


def window_name(minutes: int | None) -> str:
    if minutes is None:
        return "window"
    return _NAMED.get(minutes, f"{minutes}min")


def short_name(name: str) -> str:
    """`seven_day` -> `7d`. Unknown names keep their own, trimmed of the overage suffix."""
    if name in _SHORT:
        return _SHORT[name]
    base = name.replace("_overage_included", "+ov")
    return _SHORT.get(base, base)


@dataclass(frozen=True)
class Window:
    """One rationing window, as a fraction used rather than an amount spent."""

    name: str
    utilization: float           # 0..1
    resets_at: float | None = None   # epoch seconds
    minutes: int | None = None

    @property
    def percent(self) -> float:
        return self.utilization * 100

    @property
    def governs_stop(self) -> bool:
        """Whether crossing this window should end a run.

        Overage windows measure a different thing -- what you would be billed past
        the plan -- so they never end a run on their own.
        """
        if self.name.endswith("overage_included"):
            return False
        return self.minutes is not None and self.minutes >= STOP_WINDOW_MINUTES


@dataclass(frozen=True)
class UsageSnapshot:
    """Every window one harness reported at one moment."""

    harness: str
    windows: dict[str, Window] = field(default_factory=dict)
    plan: str | None = None

    def __bool__(self) -> bool:
        return bool(self.windows)

    def to_dict(self) -> dict:
        return {
            "harness": self.harness,
            "plan": self.plan,
            "windows": {
                n: {"utilization": round(w.utilization, 4), "resets_at": w.resets_at, "minutes": w.minutes}
                for n, w in self.windows.items()
            },
        }


def _window(name: str, utilization: float | None, resets_at, minutes: int | None) -> Window | None:
    if utilization is None:
        return None
    try:
        util = float(utilization)
    except (TypeError, ValueError):
        return None
    try:
        reset = float(resets_at) if resets_at is not None else None
    except (TypeError, ValueError):
        reset = None
    return Window(name=name, utilization=max(0.0, min(util, 1.0)), resets_at=reset, minutes=minutes)


def from_claude_stream(stdout: str) -> UsageSnapshot | None:
    """The last `rate_limit_event` in a Claude Code stream, or None if it emitted none.

    Older Claude Code builds report a single flat window rather than `unifiedWindows`;
    both shapes are read so a run does not lose its meter on an older CLI.
    """
    info = None
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if '"rate_limit_info"' not in line or not line.startswith("{"):
            continue
        try:
            info = (json.loads(line) or {}).get("rate_limit_info")
        except json.JSONDecodeError:
            continue
        if isinstance(info, dict):
            break
        info = None
    if not isinstance(info, dict):
        return None

    windows: dict[str, Window] = {}
    unified = info.get("unifiedWindows")
    if isinstance(unified, dict):
        for name, w in unified.items():
            if not isinstance(w, dict):
                continue
            minutes = 10080 if name.startswith("seven_day") else 300 if name == "five_hour" else None
            got = _window(name, w.get("utilization"), w.get("resetsAt"), minutes)
            if got:
                windows[name] = got
    if not windows:  # older shape: one flat window named by rateLimitType
        name = info.get("rateLimitType") or "window"
        minutes = 300 if name == "five_hour" else 10080 if name.startswith("seven_day") else None
        got = _window(name, info.get("utilization"), info.get("resetsAt"), minutes)
        if got:
            windows[name] = got
    if not windows:
        return None
    return UsageSnapshot(harness="claude", windows=windows)


def from_codex_rollout(path: Path) -> UsageSnapshot | None:
    """The last `token_count` rate limits in a Codex rollout log.

    Codex keeps this out of `exec --json`, so it is read from the session file
    rather than the stream the loop already captured.
    """
    try:
        text = path.read_text(errors="ignore")
    except OSError:
        return None
    limits = None
    for line in reversed(text.splitlines()):
        if '"rate_limits"' not in line:
            continue
        try:
            payload = (json.loads(line) or {}).get("payload") or {}
        except json.JSONDecodeError:
            continue
        got = payload.get("rate_limits")
        if isinstance(got, dict):
            limits = got
            break
    if not isinstance(limits, dict):
        return None

    windows: dict[str, Window] = {}
    for slot in ("primary", "secondary"):
        w = limits.get(slot)
        if not isinstance(w, dict):
            continue
        minutes = w.get("window_minutes")
        minutes = int(minutes) if isinstance(minutes, (int, float)) else None
        pct = w.get("used_percent")
        got = _window(window_name(minutes), (pct / 100) if isinstance(pct, (int, float)) else None,
                      w.get("resets_at"), minutes)
        if got:
            windows[got.name] = got
    if not windows:
        return None
    return UsageSnapshot(harness="codex", windows=windows, plan=limits.get("plan_type"))


def find_codex_rollout(home: Path, session_id: str) -> Path | None:
    """The rollout log for one Codex thread. Its filename ends with the thread id."""
    if not session_id:
        return None
    hits = sorted(home.glob(f"sessions/**/rollout-*{session_id}.jsonl"))
    return hits[-1] if hits else None


class UsageLedger:
    """What each harness's windows read when the run started, and where they are now.

    A window is a gauge, not a total: it says how full the bucket is, and the bucket
    drains on its own. So the run's own consumption is the rise since it started,
    which is what the morning read wants, while the level itself is what a stop rule
    has to watch.
    """

    def __init__(self) -> None:
        self.first: dict[str, UsageSnapshot] = {}
        self.latest: dict[str, UsageSnapshot] = {}

    def record(self, snapshot: UsageSnapshot | None) -> None:
        if not snapshot:
            return
        self.first.setdefault(snapshot.harness, snapshot)
        self.latest[snapshot.harness] = snapshot

    def __bool__(self) -> bool:
        return bool(self.latest)

    def exceeded(self, ceiling: float) -> tuple[str, Window] | None:
        """The first harness window at or past `ceiling`, if any is."""
        for harness, snap in sorted(self.latest.items()):
            for w in snap.windows.values():
                if w.governs_stop and w.utilization >= ceiling:
                    return harness, w
        return None

    def reserve_hit(self, harness: str, ceiling: float) -> Window | None:
        """The fullest window of one harness at or past `ceiling`, if any is.

        The same measurement as `exceeded`, asked per harness rather than across
        all of them, because a reserve blocks one harness and leaves the run
        alone. A window with no reset time still counts: the board falls back to
        its own cooldown when it is asked to block until nowhere.
        """
        snap = self.latest.get(harness)
        if not snap:
            return None
        hit = [w for w in snap.windows.values() if w.governs_stop and w.utilization >= ceiling]
        return max(hit, key=lambda w: w.utilization) if hit else None

    def lines(self) -> list[str]:
        """One line per window: where it is, and how far this run moved it."""
        out = []
        for harness, snap in sorted(self.latest.items()):
            started = self.first.get(harness)
            for name, w in sorted(snap.windows.items(), key=lambda kv: -(kv[1].minutes or 0)):
                was = (started.windows.get(name) if started else None)
                line = f"{harness} {name} {w.percent:.0f}%"
                if was is not None and abs(w.utilization - was.utilization) >= 0.005:
                    line = f"{harness} {name} {was.percent:.0f}% -> {w.percent:.0f}% ({w.percent - was.percent:+.0f} this run)"
                out.append(line)
        return out

    def has_windows(self, harness: str) -> bool:
        """Whether this harness rations by window. If it does not, it bills in money."""
        snap = self.latest.get(harness)
        return bool(snap and snap.windows)

    def compact(self, billed: dict[str, float] | None = None) -> str:
        """One line: what the run has spent, in the unit each harness actually charges.

        A subscription does not bill per call, so a dollar figure derived from token
        counts at API list prices measures nothing anyone pays. What a run really
        spends there is a slice of a rationing window, so that is what this reports.
        Money appears only for a harness with no windows -- an API key, where the
        dollars are a real invoice.
        """
        parts = []
        for harness, snap in sorted(self.latest.items()):
            started = self.first.get(harness)
            for name, w in sorted(snap.windows.items(), key=lambda kv: -(kv[1].minutes or 0)):
                was = started.windows.get(name) if started else None
                rose = f" {w.percent - was.percent:+.0f}" if was is not None and abs(w.utilization - was.utilization) >= 0.005 else ""
                parts.append(f"{harness} {short_name(name)} {w.percent:.0f}%{rose}")
        for harness, amount in sorted((billed or {}).items()):
            if amount and not self.has_windows(harness):
                parts.append(f"{harness} ${amount:.2f}")
        return "  ·  ".join(parts)

    def to_dict(self) -> dict:
        out = {}
        for harness, snap in sorted(self.latest.items()):
            d = snap.to_dict()
            first = self.first.get(harness)
            if first is not None:
                d["first_windows"] = {n: round(w.utilization, 4) for n, w in first.windows.items()}
            out[harness] = d
        return out
