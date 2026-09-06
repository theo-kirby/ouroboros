"""The one interface the loop engine knows about."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from pathlib import Path
from typing import Protocol

_RETRIABLE = re.compile(
    r"rate.?limit|429|overloaded|too many requests|529|503|temporarily unavailable|"
    r"ECONNRESET|ETIMEDOUT|connection (reset|refused)|"
    r"usage limit|session limit|limit reached|hit your \w+ ?limit|out of (extra )?usage|resets? (at )?\d|quota|"
    r"try again (later|in)|server error|internal error|5\d\d",
    re.IGNORECASE,
)
_AUTH = re.compile(r"unauthorized|401|not logged in|invalid api key|authentication", re.IGNORECASE)


@dataclass
class Result:
    text: str = ""
    session_id: str | None = None
    cost_usd: float | None = None
    turns: int | None = None
    exit_code: int = 0
    raw_path: Path | None = None
    timed_out: bool = False
    error: str | None = None
    extra: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out and self.error is None

    @property
    def retriable(self) -> bool:
        blob = f"{self.error or ''}\n{self.text[-2000:]}"
        return bool(_RETRIABLE.search(blob))

    def reset_wait_seconds(self, *, now: datetime | None = None) -> float | None:
        """Seconds until the limit resets, if the message says when."""
        at = parse_reset_time(f"{self.error or ''}\n{self.text[-2000:]}", now=now)
        if at is None:
            return None
        now = now or datetime.now(timezone.utc)
        return max(0.0, (at - now).total_seconds())

    @property
    def auth_failure(self) -> bool:
        blob = f"{self.error or ''}\n{self.text[-2000:]}"
        return self.exit_code != 0 and bool(_AUTH.search(blob))


_RESET_CLOCK = re.compile(
    r"resets?\s*(?:at\s*)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\s*(?:\(([A-Za-z_]+/[A-Za-z_+\-]+(?:/[A-Za-z_]+)?)\))?",
    re.IGNORECASE,
)
_RESET_EPOCH = re.compile(r"limit reached\|(\d{9,11})")


def parse_reset_time(text: str, *, now: datetime | None = None) -> datetime | None:
    """'resets 2:50am (Europe/Madrid)' / 'resets at 3pm' / 'usage limit reached|1757100000' -> aware datetime."""
    now = now or datetime.now(timezone.utc)
    m = _RESET_EPOCH.search(text)
    if m:
        return datetime.fromtimestamp(int(m.group(1)), tz=timezone.utc)
    m = _RESET_CLOCK.search(text)
    if not m:
        return None
    hour, minute, ampm, tzname = int(m.group(1)), int(m.group(2) or 0), (m.group(3) or "").lower(), m.group(4)
    if ampm == "pm" and hour < 12:
        hour += 12
    if ampm == "am" and hour == 12:
        hour = 0
    if hour > 23 or minute > 59:
        return None
    try:
        tz = ZoneInfo(tzname) if tzname else now.astimezone().tzinfo
    except Exception:
        tz = now.astimezone().tzinfo
    local_now = now.astimezone(tz)
    candidate = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= local_now:
        candidate += timedelta(days=1)
    return candidate


class Harness(Protocol):
    name: str

    def run(
        self,
        prompt: str,
        *,
        cwd: Path,
        timeout: float,
        resume: str | None = None,
        model: str | None = None,
        system_append: str | None = None,
        log_path: Path | None = None,
        tools: str | None = None,  # None = all, "none" = no tools, "readonly" = read-only set
        json_schema: dict | None = None,
        max_turns: int | None = None,
    ) -> Result: ...
