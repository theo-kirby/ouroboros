"""Fallback chains: run the preferred harness, switch while it is out of usage, come back when it resets.

A LimitBoard is shared by every role in a run, because usage limits belong to an
account, not to a role. A PooledHarness is one role's ordered list of
(harness, model) entries; it looks like a Harness to the engine.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .base import Harness, Result
from ..usage import UsageLedger, Window

Log = Callable[[str], None]


@dataclass
class LimitBoard:
    usage: UsageLedger = field(default_factory=UsageLedger)
    # Stop *using* a harness at this much of one of its subscription windows,
    # rather than stopping the run. A reserve is what keeps a two-day run from
    # eating the whole 7-day window the operator also works out of: the harness
    # is blocked until the window resets, the pool falls back, and the run
    # continues. `stop.max_usage` is the other, blunter tool -- it ends the run.
    reserve: dict[str, float] = field(default_factory=dict)   # harness -> 0..1
    cooldown: float = 1800.0        # block length when the reset time is unknown
    max_cooldown: float = 10800.0
    transient_strikes: int = 3      # transient errors in a row that count as a limit
    clock: Callable[[], float] = time.time
    blocked: dict[str, float] = field(default_factory=dict)   # harness -> epoch when it is usable again
    repeats: dict[str, int] = field(default_factory=dict)     # harness -> unknown-reset blocks in a row
    strikes: dict[str, int] = field(default_factory=dict)     # harness -> transient errors in a row
    why: dict[str, str] = field(default_factory=dict)

    def check_reserve(self, name: str) -> Window | None:
        """Block a harness that has spent its reserve. Returns the window that did it."""
        ceiling = self.reserve.get(name)
        if ceiling is None or self.is_blocked(name):
            return None
        w = self.usage.reserve_hit(name, ceiling)
        if w is None:
            return None
        until = datetime.fromtimestamp(w.resets_at, tz=timezone.utc) if w.resets_at else None
        self.block(name, until=until, why=f"reserve {ceiling:.0%} reached ({w.name} at {w.percent:.0f}%)")
        return w

    def is_blocked(self, name: str) -> bool:
        until = self.blocked.get(name)
        if until is None:
            return False
        if until <= self.clock():
            self.blocked.pop(name, None)
            return False
        return True

    def block(self, name: str, *, until: datetime | None = None, why: str = "") -> float:
        """Block a harness until `until`, or for a growing cooldown when nobody knows. Returns the epoch."""
        now = self.clock()
        if until is not None:
            at = max(now + 60.0, until.timestamp() + 60.0)   # a minute past the reset, never in the past
            self.repeats[name] = 0
        else:
            n = self.repeats.get(name, 0)
            at = now + min(self.cooldown * (2 ** n), self.max_cooldown)
            self.repeats[name] = n + 1
        self.blocked[name] = at
        self.why[name] = why[:120]
        self.strikes[name] = 0
        return at

    def strike(self, name: str, why: str = "") -> bool:
        """Count a transient error. Returns True when the strikes turned into a block."""
        self.strikes[name] = self.strikes.get(name, 0) + 1
        if self.strikes[name] >= self.transient_strikes:
            self.block(name, why=f"{self.strikes[name]} transient errors in a row: {why}")
            return True
        return False

    def clear(self, name: str) -> None:
        self.blocked.pop(name, None)
        self.repeats.pop(name, None)
        self.strikes.pop(name, None)
        self.why.pop(name, None)

    def earliest(self) -> float | None:
        """Epoch when the first blocked harness frees up, or None when nothing is blocked."""
        live = [t for t in self.blocked.values() if t > self.clock()]
        return min(live) if live else None

    def snapshot(self) -> dict[str, str]:
        now = self.clock()
        return {
            name: f"{int((t - now) // 60)}m ({self.why.get(name, '')})"
            for name, t in self.blocked.items() if t > now
        }


class PooledHarness:
    """A role's harness chain behind the plain Harness interface.

    `run` tries the first entry that is not blocked. A limit or auth failure blocks that
    entry on the board and moves on to the next. Session ids are remembered per harness,
    so a resume only happens on the harness that minted the session.
    """

    def __init__(self, entries: list[tuple[Harness, str | None]], board: LimitBoard, *, log: Log | None = None) -> None:
        if not entries:
            raise ValueError("a pool needs at least one harness")
        self.entries = entries
        self.board = board
        self.log = log or (lambda _m: None)
        self.active: Harness = entries[0][0]
        self._session_owner: dict[str, str] = {}

    @property
    def name(self) -> str:
        return self.active.name

    @property
    def preferred(self) -> Harness:
        return self.entries[0][0]

    @property
    def names(self) -> list[str]:
        return [h.name for h, _ in self.entries]

    def _candidates(self) -> list[tuple[Harness, str | None]]:
        return [(h, m) for h, m in self.entries if not self.board.is_blocked(h.name)]

    def run(
        self,
        prompt: str,
        *,
        cwd: Path,
        timeout: float,
        resume: str | None = None,
        model: str | None = None,   # ignored: each entry carries its own model
        **kw,
    ) -> Result:
        last: Result | None = None
        for harness, entry_model in self._candidates():
            own_session = resume if (resume and self._session_owner.get(resume) == harness.name) else None
            if harness is not self.active:
                self.log(f"harness: {self.active.name} -> {harness.name}" + (f" ({entry_model})" if entry_model else ""))
                self.active = harness
            try:
                result = harness.run(prompt, cwd=cwd, timeout=timeout, resume=own_session, model=entry_model, **kw)
            except Exception as exc:  # a driver bug must not stop the loop
                result = Result(exit_code=-1, error=f"harness raised {exc!r}")
            result.extra.setdefault("harness", harness.name)
            self.board.usage.record(result.usage)
            if result.session_id:
                self._session_owner[result.session_id] = harness.name
            kind = result.kind
            if kind in ("ok", "timeout"):
                # `clear` first: the call succeeded, so any earlier strike or block
                # is stale. The reserve is then applied on top, or clearing would
                # undo the block the reading it just took asks for.
                self.board.clear(harness.name)
                spent = self.board.check_reserve(harness.name)
                if spent is not None:
                    self.log(f"harness {harness.name} hit its reserve "
                             f"({spent.name} at {spent.percent:.0f}%); blocked until it resets")
                return result
            if kind == "limit":
                at = self.board.block(harness.name, until=result.reset_at(), why=(result.error or result.text)[-120:])
                self.log(f"harness {harness.name} is out of usage until {_fmt(at)}: {(result.error or result.text)[-100:]!r}")
                last = result
                continue
            if kind == "auth":
                self.board.block(harness.name, why=f"auth failure: {(result.error or '')[:80]}")
                self.log(f"harness {harness.name} auth failure; blocked for a while: {(result.error or '')[:100]!r}")
                last = result
                continue
            if kind == "transient":
                if self.board.strike(harness.name, (result.error or "")[:80]):
                    self.log(f"harness {harness.name}: repeated transient errors, treating as a limit")
                    last = result
                    continue
                return result   # the caller backs off and calls again; same harness next time
            return result       # a plain error: the caller decides
        if last is None:
            last = Result(exit_code=-1, error="every harness in the chain is blocked", extra={"kind": "limit"})
        earliest = self.board.earliest()
        if earliest is not None:
            last.extra["resets_at"] = earliest
            last.extra["kind"] = "limit"
        if len(self.entries) > 1:
            last.error = f"every harness is blocked ({', '.join(self.names)}): {last.error or ''}"[:400]
        return last


def _fmt(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).astimezone().strftime("%H:%M %Z")
