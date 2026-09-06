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
# The subset of retriable errors that mean "this account is out of usage for a while":
# worth switching to another harness, not worth a 60-second retry.
_LIMIT = re.compile(
    r"usage.?limit|session limit|weekly limit|(hit|reached) your \w+ ?limit|limit reached|out of (extra )?usage|"
    r"resets? (at )?\d|insufficient_quota|quota|plan limit|usage cap|"
    r"usage_limit_reached|usage_not_included|upgrade to plus|out of credits|spend cap|"
    r"exceeded retry limit, last status: 429|try again in ~?\d+ ?min",
    re.IGNORECASE,
)
_AUTH = re.compile(
    r"unauthorized|\b401\b|not logged in|invalid api key|authentication|login required|"
    r"token (has )?expired|please (run|sign)|no credentials|refresh token|sign in again|login is required|"
    r"no api key",
    re.IGNORECASE,
)


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
    def _blob(self) -> str:
        return f"{self.error or ''}\n{self.text[-2000:]}"

    @property
    def retriable(self) -> bool:
        return self.kind in ("limit", "transient")

    @property
    def limit(self) -> bool:
        """A usage limit: the account is out of usage until some reset time."""
        return self.kind == "limit"

    @property
    def auth_failure(self) -> bool:
        return self.kind == "auth"

    @property
    def kind(self) -> str:
        """ok | timeout | limit | auth | transient | error.

        A driver that knows better sets extra["kind"]; otherwise the error text decides.
        """
        if self.timed_out:
            return "timeout"
        if self.ok:
            return "ok"
        known = self.extra.get("kind")
        if known in ("limit", "auth", "transient", "error"):
            return known
        blob = self._blob
        if self.exit_code != 0 and _AUTH.search(blob) and not _LIMIT.search(blob):
            return "auth"
        if _LIMIT.search(blob) or self.reset_at() is not None:
            return "limit"
        if _RETRIABLE.search(blob):
            return "transient"
        return "error"

    def reset_at(self, *, now: datetime | None = None) -> datetime | None:
        """When the limit resets, if the driver or the message says."""
        epoch = self.extra.get("resets_at")
        if epoch:
            return datetime.fromtimestamp(float(epoch), tz=timezone.utc)
        return parse_reset_time(self._blob, now=now)

    def reset_wait_seconds(self, *, now: datetime | None = None) -> float | None:
        """Seconds until the limit resets, if known."""
        at = self.reset_at(now=now)
        if at is None:
            return None
        now = now or datetime.now(timezone.utc)
        return max(0.0, (at - now).total_seconds())


_MONTHS = "jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec"
_RESET_CLOCK = re.compile(
    r"(?:resets?|try again)\s*(?:at\s*)?"
    rf"(?:(?P<mon>{_MONTHS})[a-z]*\.?\s+(?P<day>\d{{1,2}})(?:st|nd|rd|th)?,?\s+(?P<year>\d{{4}})\s+)?"
    r"(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<ampm>am|pm)?\s*(?:\((?P<tz>[A-Za-z_]+/[A-Za-z_+\-]+(?:/[A-Za-z_]+)?)\))?",
    re.IGNORECASE,
)
_RESET_EPOCH = re.compile(r"limit reached\|(\d{9,11})|resets?_at[\"']?\s*[:=]\s*(\d{9,11})")
_RESET_IN = re.compile(r"(?:try again|resets?) in ~?(\d+(?:\.\d+)?)\s*(s|sec|seconds?|m|min|minutes?|h|hours?)\b", re.IGNORECASE)
_UNIT = {"s": 1, "sec": 1, "second": 1, "seconds": 1, "m": 60, "min": 60, "minute": 60, "minutes": 60, "h": 3600, "hour": 3600, "hours": 3600}


def parse_reset_time(text: str, *, now: datetime | None = None) -> datetime | None:
    """'resets 2:50am (Europe/Madrid)' / 'resets at 3pm' / 'usage limit reached|1757100000' -> aware datetime."""
    now = now or datetime.now(timezone.utc)
    m = _RESET_EPOCH.search(text)
    if m:
        return datetime.fromtimestamp(int(m.group(1) or m.group(2)), tz=timezone.utc)
    m = _RESET_IN.search(text)
    if m:
        return now + timedelta(seconds=float(m.group(1)) * _UNIT[m.group(2).lower()])
    m = _RESET_CLOCK.search(text)
    if not m:
        return None
    hour, minute, ampm, tzname = int(m.group("hour")), int(m.group("minute") or 0), (m.group("ampm") or "").lower(), m.group("tz")
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
    if m.group("mon"):
        try:
            month = _MONTHS.split("|").index(m.group("mon").lower()[:3]) + 1
            return candidate.replace(year=int(m.group("year")), month=month, day=int(m.group("day")))
        except ValueError:
            pass
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
