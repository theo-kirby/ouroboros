"""Stop conditions and backoff. Stop conditions end the run. Backoff never does."""

from __future__ import annotations

import time
from datetime import datetime

from .config import StopConfig
from .usage import UsageLedger, UsageSnapshot

BACKOFF_STEPS = [60.0, 120.0, 300.0, 600.0]


class BudgetClock:
    def __init__(self, stop: StopConfig, *, now=time.time) -> None:
        self.stop = stop
        self.now = now
        self.started_at = now()
        self.cost_usd = 0.0
        self.usage = UsageLedger()
        self.iterations = 0
        self.done_streak = 0
        self.stuck_streak = 0

    def add(self, *, cost: float | None = None, usage: UsageSnapshot | None = None,
            iteration: bool = False, done_accepted: bool | None = None,
            stuck: bool | None = None) -> None:
        if cost:
            self.cost_usd += cost
        if usage:
            self.usage.record(usage)
        if iteration:
            self.iterations += 1
        if done_accepted is True:
            self.done_streak += 1
        elif done_accepted is False:
            self.done_streak = 0
        if stuck is True:
            self.stuck_streak += 1
        elif stuck is False:
            self.stuck_streak = 0

    @property
    def elapsed(self) -> float:
        return self.now() - self.started_at

    def should_stop(self) -> str | None:
        s = self.stop
        if s.after_seconds is not None and self.elapsed >= s.after_seconds:
            return f"wall clock: {s.after} elapsed"
        if s.max_iterations is not None and self.iterations >= s.max_iterations:
            return f"max_iterations {s.max_iterations} reached"
        if s.max_cost_usd is not None and self.cost_usd >= s.max_cost_usd:
            return f"max_cost_usd {s.max_cost_usd} reached (~${self.cost_usd:.2f})"
        if s.max_usage is not None:
            hit = self.usage.exceeded(s.max_usage)
            if hit:
                harness, w = hit
                return f"max_usage {s.max_usage:.0%} reached ({harness} {w.name} at {w.percent:.0f}%)"
        if s.until_dt is not None and datetime.fromtimestamp(self.now()) >= s.until_dt:
            return f"until {s.until} reached"
        if s.on_done_accepted is not None and self.done_streak >= s.on_done_accepted:
            return f"overseer accepted done {self.done_streak}x in a row"
        if s.max_stuck is not None and self.stuck_streak >= s.max_stuck:
            return f"overseer called the loop stuck {self.stuck_streak}x in a row"
        return None


def backoff_seconds(attempt: int) -> float:
    """attempt 0 -> 60s, 1 -> 120s, 2 -> 300s, then 600s forever."""
    return BACKOFF_STEPS[min(attempt, len(BACKOFF_STEPS) - 1)]
