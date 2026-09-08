"""Configuration models and duration parsing."""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

_DUR = re.compile(r"(\d+(?:\.\d+)?)\s*([smhd]?)")
_UNIT = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_duration(value: str | int | float | None) -> float | None:
    """'10h', '45m', '1h30m', '90s', 90 -> seconds."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = value.strip().lower()
    if not text:
        return None
    pos = 0
    total = 0.0
    for m in _DUR.finditer(text):
        if m.start() != pos:
            raise ValueError(f"bad duration: {value!r}")
        total += float(m.group(1)) * _UNIT[m.group(2)]
        pos = m.end()
    if pos != len(text):
        raise ValueError(f"bad duration: {value!r}")
    return total


class FallbackConfig(BaseModel):
    """One alternative harness for a role, used while the ones before it are limited."""
    harness: str
    model: str | None = None


class RoleConfig(BaseModel):
    harness: str = "claude"
    model: str | None = None
    timeout: str = "45m"
    resume_for: int = 1
    fallback: list[FallbackConfig] = Field(default_factory=list)

    @property
    def timeout_seconds(self) -> float:
        return parse_duration(self.timeout) or 2700.0

    @property
    def chain(self) -> list[tuple[str, str | None]]:
        """(harness, model) in preference order: the role's own harness first, then its fallbacks."""
        return [(self.harness, self.model)] + [(f.harness, f.model) for f in self.fallback]


class LimitConfig(BaseModel):
    """What to do when a harness says its usage limit is reached and does not say when it resets."""
    cooldown: str = "30m"        # first block; doubles on every repeat
    max_cooldown: str = "3h"     # cap for the doubling
    transient_strikes: int = 3   # this many transient errors in a row count as a limit

    @property
    def cooldown_seconds(self) -> float:
        return parse_duration(self.cooldown) or 1800.0

    @property
    def max_cooldown_seconds(self) -> float:
        return parse_duration(self.max_cooldown) or 10800.0


class GitConfig(BaseModel):
    branch: str | None = None
    tag_on_accept: bool = True
    revert_on_reject: bool = True
    allow_dirty: bool = False


class StopConfig(BaseModel):
    after: str | None = None
    max_iterations: int | None = None
    max_cost_usd: float | None = None
    max_usage: float | None = None   # 0..1 of a subscription window; 0.8 = stop at 80%
    # A loop the overseer calls stuck every iteration is spending on nothing. Backoff
    # slows it first (60s, 120s, 300s, then 600s), so this many verdicts is hours of
    # mostly sleeping, not minutes: a run that will not recover, not a passing stall.
    max_stuck: int | None = 25       # consecutive `stuck` verdicts; null to never stop
    until: str | None = None
    on_done_accepted: int | None = None

    @property
    def after_seconds(self) -> float | None:
        return parse_duration(self.after)

    @property
    def until_dt(self) -> datetime | None:
        return datetime.fromisoformat(self.until) if self.until else None


class HandoffConfig(BaseModel):
    recent: int = 3


class HypergraphConfig(BaseModel):
    reconcile_every: int = 5   # maintainer pass after this many work iterations
    pressure: int = 3          # ...or as soon as this many record nodes are unreconciled
    budget_units: int = 1      # dispatch budget per iteration


class LoopConfig(BaseModel):
    """Motion-without-progress detection (see loops.py).

    Thresholds are counted in iterations and are deliberately generous: a real
    loop is sustained, and a signal that fires on a slow afternoon is a signal
    the operator learns to ignore. Any of them may be null to switch that
    signal off. None of them can stop a run -- a loop is a thing to break out
    of, not a thing to die of.
    """

    product_after: int | None = 8     # iterations changing only bookkeeping
    frontier_after: int | None = 30   # iterations with the frontier unmoved
    repeat_bet_after: int | None = 3  # planner bets restating an earlier bet
    bet_similarity: float = 0.5       # Jaccard over content words; nt3 healthy peaked at 0.33
    bet_window: int = 6               # how many recent bets a new one is compared against
    escalate_every: int = 5           # iterations past the threshold per escalation step
    rotate: bool = True               # step 3 may switch the actor to its fallback


class PlanConfig(BaseModel):
    """The self-evolving plan layer (DESIGN.md section 20)."""
    enabled: bool | None = None   # None = on for every memory adapter that supports it
    every: int = 5                # handoff repos: planner pass every N work iterations (hypergraph: after each reconcile)
    view: str = "plan"
    md: str = "PLAN.md"
    max_new_directions: int = 1   # per planner pass, not per run


class Config(BaseModel):
    run: str = "run"
    goal: str = ".ouroboros/goal.md"
    memory: str = "auto"  # auto | hypergraph | handoff
    backend: str = "headless"
    mode: str = "single"   # single | actor-critic | council
    council: list[FallbackConfig] = Field(default_factory=list)   # council mode: extra critics beside roles.critic
    overseer: str = "agent"  # agent | rules
    idle_interval: str = "30m"  # sleep between iterations after done_accepted under report_done
    roles: dict[str, RoleConfig] = Field(
        default_factory=lambda: {
            "actor": RoleConfig(),
            "overseer": RoleConfig(model="haiku", timeout="3m"),
            "maintainer": RoleConfig(timeout="20m"),
            "planner": RoleConfig(timeout="20m"),
            "critic": RoleConfig(timeout="15m"),
        }
    )
    git: GitConfig = Field(default_factory=GitConfig)
    stop: StopConfig = Field(default_factory=StopConfig)
    handoff: HandoffConfig = Field(default_factory=HandoffConfig)
    hypergraph: HypergraphConfig = Field(default_factory=HypergraphConfig)
    limits: LimitConfig = Field(default_factory=LimitConfig)
    plan: PlanConfig = Field(default_factory=PlanConfig)
    loop: LoopConfig = Field(default_factory=LoopConfig)

    @property
    def branch(self) -> str:
        return self.git.branch or f"ouroboros/{self.run}"

    def role(self, name: str) -> RoleConfig:
        return self.roles.get(name) or RoleConfig()

    @classmethod
    def load(cls, path: Path) -> "Config":
        data = yaml.safe_load(path.read_text()) or {}
        return cls.model_validate(data)

    def dump(self) -> str:
        return yaml.safe_dump(self.model_dump(mode="json"), sort_keys=False)


DEFAULT_CONFIG_PATH = Path(".ouroboros/config.yml")
