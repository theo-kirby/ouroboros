"""Configuration models and duration parsing."""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

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


class ConfigError(ValueError):
    """A config the loop cannot run, said in one line for the terminal."""


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
    # Stop *using* a harness at this much of one of its subscription windows,
    # instead of stopping the run: {claude: 0.85} keeps a two-day run from eating
    # the whole 7-day window the operator also works out of. The harness is
    # blocked until that window resets, the pool falls back, and the loop goes on.
    # `stop.max_usage` is the blunt version of the same idea -- it ends the run.
    reserve: dict[str, float] = Field(default_factory=dict)   # harness -> 0..1

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
    # A loop the critic calls stuck every iteration is spending on nothing. Backoff
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


class GraphConfig(BaseModel):
    """When the scribe folds. Both memories are the same model, so this applies to both."""
    reconcile_every: int = 5   # a fold after this many worked iterations
    pressure: int = 3          # ...or as soon as this many record nodes are unreconciled
    budget_units: int = 1      # dispatch budget per iteration


LOOPS = ("standard", "worker-writer")
HANDOFFS = ("both", "prompt", "graph")


class LoopConfig(BaseModel):
    """Which loop runs, and its motion-without-progress detection (see loops.py).

    `kind` picks the loop: `standard` (actor, critic, scribe; the default) or
    `worker-writer` (DESIGN.md section 24). `loop: worker-writer` in a config is
    short for `loop: {kind: worker-writer}`; the detector's keys sit beside `kind`.

    Thresholds are counted in iterations and are deliberately generous: a real
    loop is sustained, and a signal that fires on a slow afternoon is a signal
    the operator learns to ignore. Any of them may be null to switch that
    signal off. None of them can stop a run -- a loop is a thing to break out
    of, not a thing to die of.
    """

    kind: Literal["standard", "worker-writer"] = "standard"
    product_after: int | None = 8     # iterations changing only bookkeeping
    frontier_after: int | None = 30   # iterations with the frontier unmoved
    escalate_every: int = 5           # iterations past the threshold per escalation step
    rotate: bool = True               # step 2 may switch the actor to its fallback


# What the reporter pushes without a model: each is a fact read straight off the
# run directory, so it still fires when every model is out of usage.
TRIGGERS = ("needs_human", "stopped", "limited", "stuck", "looping", "engine_error", "silent",
            "done_accepted", "reject")
DEFAULT_TRIGGERS = tuple(t for t in TRIGGERS if t != "reject")   # a reject is routine; the digest counts them


class ReportConfig(BaseModel):
    """The reporter: an observer outside the loop that pushes to a phone (DESIGN.md section 15).

    `ouroboros watch` reads the run directory and never the loop, so the loop cannot
    tell whether it exists. `roles.reporter` names the harness that writes the digests.
    """
    notify: str = "auto"              # auto | pushover | none; auto: pushover when its credentials are set
    every: str | None = "4h"          # a model-written digest this often; null: only on triggers
    on: list[str] = Field(default_factory=lambda: list(DEFAULT_TRIGGERS))
    silent_after: str = "2h"          # status.json this stale while the run says it is working: the run is stuck or dead
    poll: str = "30s"                 # how often the reporter reads the run directory
    max_chars: int = 1000             # a push notification's size; Pushover takes 1024

    @property
    def every_seconds(self) -> float | None:
        return parse_duration(self.every)

    @property
    def silent_after_seconds(self) -> float:
        return parse_duration(self.silent_after) or 7200.0

    @property
    def poll_seconds(self) -> float:
        return parse_duration(self.poll) or 30.0


class FiguresConfig(BaseModel):
    """The figure contract of a paper or product publication (DESIGN.md section 23)."""
    enabled: bool = True       # false: no numbered figures; the evidence is tables and inline charts
    main_max: int = 5          # numbered main figures at most; everything else is supplementary
    panels: bool = True        # each main figure gathers lettered panels (A, B, C...) behind one claim
    supplementary: bool = True # plots that did not earn a main figure stay listed below the paper


# What each profile publishes when `publication.template` is auto.
PROFILE_TEMPLATE = {"research": "paper", "development": "product", "freeform": "notebook"}


class PublicationConfig(BaseModel):
    """The run's report. The scribe keeps the notebook (story.html, the run book) every
    iteration whatever the template. `paper` and `product` add a written report
    (paper.html) shown behind the run page's Paper toggle: one pass when the run stops
    (`final`), and a mid-run draft every `consolidate_every` worked iterations when set.
    `notebook` writes no report.
    """
    template: Literal["auto", "notebook", "paper", "product"] = "auto"
    figures: FiguresConfig = Field(default_factory=FiguresConfig)
    consolidate_every: int | None = None  # worked iterations between mid-run drafts of the report; null: none
    final: bool = True                     # write the report when the run stops (not on an interrupt or a spend cap)
    final_timeout: str = "45m"             # the final pass's own time limit: it draws, looks at and revises the figures

    @property
    def final_timeout_seconds(self) -> float:
        return parse_duration(self.final_timeout) or 2700.0


class Config(BaseModel):
    run: str = "run"
    # What kind of run this is. It sets the guidance the actor and the critic get
    # and, through publication.template: auto, the shape of the report.
    profile: Literal["research", "development", "freeform"] = "freeform"
    goal: str = ".ouroboros/goal.md"
    # governor: a Flywheel graph; files: the same model as markdown in `.ouroboros/graph/`.
    # auto: governor when the repo has one, else files. `handoff` is the old name of files.
    memory: Literal["auto", "governor", "files"] = "auto"
    backend: str = "headless"
    # Where `run` puts the loop so it outlives the terminal (launch.py): tmux,
    # tern, a systemd user service, or the foreground. auto: tmux inside tmux,
    # tern inside tern, else tmux if installed, else the foreground.
    launcher: Literal["auto", "tmux", "tern", "systemd", "foreground"] = "auto"
    # systemd only: MemoryMax for the run's unit, which covers every process it
    # starts (e.g. "40G"). Unset: no cap beyond the user slice's.
    memory_max: str | None = None
    # Three roles: the actor works and reports; the critic judges it and writes the
    # next prompt; the scribe records the unit, folds the memory, and keeps the view.
    # `rules` is a role with no model behind it -- the same deterministic fallback
    # that runs when the agent call fails (for the scribe: the loop writes the record).
    critic: str = "agent"  # agent | rules
    scribe: str = "agent"  # agent | rules
    idle_interval: str = "30m"  # sleep between iterations after done_accepted under report_done
    # worker-writer only: what the worker starts from. both: the writer's prompt and the
    # memory's brief; prompt: the prompt alone; graph: the brief alone, the worker picks.
    handoff: Literal["both", "prompt", "graph"] = "both"
    roles: dict[str, RoleConfig] = Field(
        default_factory=lambda: {
            # Codex carries the actor while Claude is limited (cadex nt2: 54 of 68 turns).
            "actor": RoleConfig(fallback=[FallbackConfig(harness="codex")]),
            "critic": RoleConfig(timeout="10m"),
            # Book-keeping, not judgment: a lighter model does it well, and Codex covers a limit.
            "scribe": RoleConfig(timeout="20m", fallback=[FallbackConfig(harness="codex")]),
            "reporter": RoleConfig(timeout="10m"),   # the digest writer; not in the pipeline
        }
    )
    git: GitConfig = Field(default_factory=GitConfig)
    stop: StopConfig = Field(default_factory=StopConfig)
    graph: GraphConfig = Field(default_factory=GraphConfig)
    limits: LimitConfig = Field(default_factory=LimitConfig)
    loop: LoopConfig = Field(default_factory=LoopConfig)
    report: ReportConfig = Field(default_factory=ReportConfig)
    publication: PublicationConfig = Field(default_factory=PublicationConfig)

    @field_validator("memory", mode="before")
    @classmethod
    def _old_memory_names(cls, value):
        return "files" if value == "handoff" else value

    @field_validator("loop", mode="before")
    @classmethod
    def _loop_by_name(cls, value):
        """`loop: worker-writer` is short for `loop: {kind: worker-writer}`."""
        if isinstance(value, str):
            if value not in LOOPS:
                raise ValueError(f"loop: {value!r} is not a loop; one of {', '.join(LOOPS)}")
            return {"kind": value}
        return value

    @model_validator(mode="after")
    def _coherent(self) -> "Config":
        problem = self.problem()
        if problem:
            raise ValueError(problem)
        return self

    def problem(self) -> str | None:
        """A combination the loop cannot run, in one sentence; None when the config holds together.

        Also called by preflight, after command-line flags have changed the config."""
        if self.loop.kind == "standard":
            if self.handoff != "both":
                return (f"handoff: {self.handoff} applies only to loop: worker-writer "
                        "(the standard loop's actor always gets the critic's reply and the memory)")
            return None
        for key in ("critic", "scribe"):
            if getattr(self, key) != "agent":
                return (f"{key}: {getattr(self, key)} does not apply to loop: worker-writer, which has no {key} "
                        "(its writer reviews and keeps the memory); remove the key")
        for name in ("worker", "writer"):
            if self.role(name).resume_for != 1:
                return (f"roles.{name}.resume_for: {self.role(name).resume_for}: the worker and the writer start "
                        "fresh every turn in loop: worker-writer; remove resume_for")
        return None

    @property
    def branch(self) -> str:
        return self.git.branch or f"ouroboros/{self.run}"

    @property
    def template(self) -> str:
        """The publication template in force: the configured one, or the profile's."""
        t = self.publication.template
        return PROFILE_TEMPLATE[self.profile] if t == "auto" else t

    def role(self, name: str) -> RoleConfig:
        return self.roles.get(name) or (ROLE_DEFAULTS[name]() if name in ROLE_DEFAULTS else RoleConfig())

    @property
    def worker_writer(self) -> bool:
        return self.loop.kind == "worker-writer"

    @property
    def primary_role(self) -> str:
        """The role that does the work: `--harness` and `--model` set its chain."""
        return "worker" if self.worker_writer else "actor"

    @property
    def active_roles(self) -> list[str]:
        """The roles the loop calls a model for, in pipeline order (a `rules` role calls none)."""
        if self.worker_writer:
            return ["worker", "writer"]
        return ["actor"] + [r for r in ("critic", "scribe") if getattr(self, r) == "agent"]

    @classmethod
    def load(cls, path: Path) -> "Config":
        data = yaml.safe_load(path.read_text()) or {}
        try:
            return cls.model_validate(data)
        except ValidationError as exc:
            raise ConfigError(f"{path}: " + "; ".join(
                f"{'.'.join(str(x) for x in e['loc']) + ': ' if e['loc'] else ''}{e['msg'].removeprefix('Value error, ')}"
                for e in exc.errors())) from None

    def dump(self) -> str:
        return yaml.safe_dump(self.model_dump(mode="json"), sort_keys=False)


# The worker-writer loop's roles, when the config names none: the worker as the
# actor (codex covers a limit), the writer as the scribe's time limit.
ROLE_DEFAULTS = {
    "worker": lambda: RoleConfig(fallback=[FallbackConfig(harness="codex")]),
    "writer": lambda: RoleConfig(timeout="20m", fallback=[FallbackConfig(harness="codex")]),
}

DEFAULT_CONFIG_PATH = Path(".ouroboros/config.yml")
