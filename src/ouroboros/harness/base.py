"""The one interface the loop engine knows about."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

_RETRIABLE = re.compile(
    r"rate.?limit|429|overloaded|too many requests|529|503|temporarily unavailable|"
    r"ECONNRESET|ETIMEDOUT|connection (reset|refused)",
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

    @property
    def auth_failure(self) -> bool:
        blob = f"{self.error or ''}\n{self.text[-2000:]}"
        return self.exit_code != 0 and bool(_AUTH.search(blob))


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
