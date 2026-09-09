"""ouroboros: init | run | status | report | skills install."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime
from importlib import resources
from pathlib import Path

from . import __version__, tmux
from .banner import render as render_banner
from .budget import BudgetClock
from .config import DEFAULT_CONFIG_PATH, Config
from .engine import Engine
from .gitguard import GitError, GitGuard
from . import history
from .history import archive, unverified_reverts
from . import goal as charter
from .harness import login, make_harness
from .harness import backend_headless
from .harness.backend_headless import kill_active
from .harness.pool import LimitBoard, PooledHarness
from .memory import make_memory
from .memory.handoff import HandoffMemory
from . import operator
from .recorder import Recorder
from .roles.critic import AgentCritic, RulesCritic

GOAL_TEMPLATE = """# Goal: {name}

<!-- This is the charter: the one document the human owns. No agent role edits it.
     The agents write the plan (short / medium / long) and their bets; you overrule them
     by editing this file and restarting. /ouroboros-design writes it by interview. -->

## Mission

(one paragraph. what and why. priorities in order.)

## Done criteria

(claims about the world, not tasks; each becomes an open gap on the frontier)

- [ ] ...

## Horizon ladder

What to do when the rung above is exhausted. Granularity, not time: agents have
no clock, so never write hours, days, or weeks here.

- **short-term:** (units, one iteration each) ...
- **medium-term:** (gaps, several units each) ...
- **long-term:** (directions, and the standing work that never ends) ...

## Constraints

- never ...
- always keep ... green

## Question policy

How to decide for me when I am not here:

- prefer the reversible option
- when unsure, match the existing style
- never add a dependency without writing why in the handoff

## Exhaustion policy

creative

(creative | maintain | report_done; under creative the critic names new directions when the frontier runs dry)

## Quality bar

- tests pass
- ...
"""


def repo_root(start: Path | None = None) -> Path:
    p = (start or Path.cwd()).resolve()
    for cand in [p, *p.parents]:
        if (cand / ".git").exists():
            return cand
    return p


def run_dir_for(repo: Path, run: str) -> Path:
    return repo / ".ouroboros" / "runs" / run


# ----------------------------------------------------------------------
def cmd_init(args: argparse.Namespace) -> int:
    repo = repo_root()
    root = repo / ".ouroboros"
    root.mkdir(exist_ok=True)
    cfg_path = repo / DEFAULT_CONFIG_PATH
    name = args.name or repo.name
    if cfg_path.exists() and not args.force:
        print(f"{cfg_path} exists (use --force to overwrite)")
    else:
        cfg = Config(run=name)
        cfg_path.write_text(cfg.dump())
        print(f"wrote {cfg_path}")
    goal = root / "goal.md"
    if goal.exists() and not args.force:
        print(f"{goal} exists")
    else:
        goal.write_text(GOAL_TEMPLATE.format(name=name))
        print(f"wrote {goal}  <- fill this in (or run the ouroboros-design skill)")
    HandoffMemory(root).ensure()
    guide = operator.write_guide(repo, name)
    print(f"wrote {guide.relative_to(repo)}  <- how a run is operated here" if guide
          else f"{repo / operator.GUIDE_PATH} exists")
    for path in operator.add_pointer(repo):
        print(f"pointed {path.relative_to(repo)} at it")
    gi = repo / ".gitignore"
    line = ".ouroboros/runs/"
    if not gi.exists() or line not in gi.read_text():
        with gi.open("a") as f:
            f.write(f"\n# ouroboros run state\n{line}\n")
        print(f"added {line} to .gitignore")
    return 0


def load_config(args: argparse.Namespace, repo: Path) -> Config:
    path = Path(args.config) if getattr(args, "config", None) else repo / DEFAULT_CONFIG_PATH
    cfg = Config.load(path) if path.exists() else Config(run=repo.name)
    if getattr(args, "run_name", None):
        cfg.run = args.run_name
    if getattr(args, "harness", None):
        cfg.roles.setdefault("actor", cfg.role("actor")).harness = args.harness
    if getattr(args, "model", None):
        cfg.roles.setdefault("actor", cfg.role("actor")).model = args.model
    if getattr(args, "for_", None):
        cfg.stop.after = args.for_
    if getattr(args, "max_iterations", None) is not None:
        cfg.stop.max_iterations = args.max_iterations
    if getattr(args, "max_cost", None) is not None:
        cfg.stop.max_cost_usd = args.max_cost
    if getattr(args, "max_usage", None) is not None:
        cfg.stop.max_usage = args.max_usage
    if getattr(args, "allow_dirty", False):
        cfg.git.allow_dirty = True
    if getattr(args, "critic", None):
        cfg.critic = args.critic
    if getattr(args, "maintainer", None) is not None:
        cfg.maintainer = args.maintainer
    if getattr(args, "planner", None) is not None:
        cfg.planner = args.planner
    if getattr(args, "memory", None):
        cfg.memory = args.memory
    return cfg


def preflight(cfg: Config, repo: Path) -> str | None:
    """Everything that can refuse a run, checked before we detach into tmux."""
    git = GitGuard(repo, cfg.branch)
    if not git.is_repo():
        return f"{repo} is not a git repository"
    try:
        git.head()
    except GitError:
        return "repository has no commits yet; make an initial commit first"
    if git.is_dirty() and not cfg.git.allow_dirty:
        return "working tree is dirty; commit, stash, or pass --allow-dirty"
    goal_path = repo / cfg.goal
    if not goal_path.exists() or not goal_path.read_text().strip():
        return f"no goal at {goal_path}; run `ouroboros init` and fill it in"
    reasons = charter.unfilled(goal_path.read_text())
    if reasons:
        return (f"the charter at {goal_path} is not filled in: " + "; ".join(reasons)
                + ". Run `ouroboros design` (or /ouroboros-design in Claude Code), or edit it by hand")
    chains = {}
    for role_name in cfg.active_roles:
        for harness_name, _model in cfg.role(role_name).chain:
            try:
                make_harness(harness_name)
            except ValueError as exc:
                return f"{role_name}: {exc}"
        chains[role_name] = cfg.role(role_name).chain
    errors, notes = login.check_roles(chains)
    for note in notes:
        print(f"  {note}", file=sys.stderr)
    if errors:
        return "not logged in: " + " | ".join(errors)
    return None


def cmd_preflight(args: argparse.Namespace) -> int:
    """Everything `run` checks, without starting anything.

    Same checks, said out loud. A launch that is going to be refused should be
    refused while a human is still looking at the terminal, and the warnings --
    which never block -- are the ones that cost ot4 its first hours.
    """
    repo = repo_root()
    cfg = load_config(args, repo)
    problem = preflight(cfg, repo)
    for line in history.spent_window_lines(repo):
        print(f"warning: {line}")
    if problem:
        print(f"not ready: {problem}", file=sys.stderr)
        return 2
    print(f"ready: run {cfg.run!r} on branch {cfg.branch}, memory {cfg.memory}")
    print("  roles: " + ", ".join(f"{n}={cfg.role(n).harness}:{cfg.role(n).model or 'default'}" for n in cfg.active_roles)
          + ("" if cfg.critic == "agent" else "  (critic: rules only)"))
    stop = [f"{k}={v}" for k, v in cfg.stop.model_dump().items() if v is not None]
    print("  stops at: " + (", ".join(stop) or "nothing — it will run until you stop it"))
    return 0


def _pools(cfg: Config, log) -> dict[str, PooledHarness]:
    """One harness chain per role, all sharing one limit board (limits belong to accounts, not roles)."""
    board = LimitBoard(
        cooldown=cfg.limits.cooldown_seconds, max_cooldown=cfg.limits.max_cooldown_seconds,
        transient_strikes=cfg.limits.transient_strikes, reserve=dict(cfg.limits.reserve),
    )
    return {
        name: PooledHarness([(make_harness(h), m) for h, m in cfg.role(name).chain], board, log=log)
        for name in cfg.active_roles
    }


def cmd_run(args: argparse.Namespace) -> int:
    repo = repo_root()
    cfg = load_config(args, repo)

    problem = preflight(cfg, repo)
    if problem:
        print(f"ouroboros: {problem}", file=sys.stderr)
        return 2
    for line in history.spent_window_lines(repo):
        print(f"ouroboros: warning: {line}", file=sys.stderr)

    if not args.foreground and not tmux.inside_ouroboros_tmux():
        if not tmux.available():
            print("tmux not found; running in the foreground", file=sys.stderr)
        else:
            return _launch_in_tmux(cfg, repo, own_session=bool(getattr(args, "own_session", False)))

    git = GitGuard(repo, cfg.branch)
    try:
        git.start(allow_dirty=cfg.git.allow_dirty)
    except GitError as exc:
        print(f"git: {exc}", file=sys.stderr)
        return 2

    try:
        memory = make_memory(
            cfg.memory, repo, recent=cfg.handoff.recent, plan=cfg.planner,
            plan_view=cfg.plan.view, plan_md=cfg.plan.md, max_new_directions=cfg.plan.max_new_directions,
            **cfg.hypergraph.model_dump(),
        )
    except ValueError as exc:
        print(f"ouroboros: {exc}", file=sys.stderr)
        return 2
    if isinstance(memory, HandoffMemory):
        memory.ensure()
    goal_path = repo / cfg.goal
    goal_text = goal_path.read_text() if goal_path.exists() else ""
    if not goal_text.strip():
        print(f"no goal at {goal_path}; run `ouroboros init` and fill it in", file=sys.stderr)
        return 2

    recorder = Recorder(run_dir_for(repo, cfg.run))

    def _die(signum, frame):  # SIGTERM (kill, `ouroboros stop`): take the children down and exit
        try:
            killed = kill_active()
            recorder.log(f"signal {signum}: {killed} child process(es) killed, exiting")
            st = recorder.read_status() or {}
            st.update(state="killed", signal=signum)
            recorder.status(**{k: v for k, v in st.items() if k not in ("ts", "epoch")})
        finally:
            os._exit(128 + signum)

    def _hup(signum, frame):  # the tmux pane or terminal died: keep looping, log to file only
        try:
            recorder.echo = None
            recorder.log("SIGHUP: terminal went away; continuing headless (stop with `ouroboros stop`)")
        except Exception:
            pass

    signal.signal(signal.SIGTERM, _die)
    signal.signal(signal.SIGHUP, _hup)

    (recorder.run_dir / "run.yml").write_text(cfg.dump() + f"\nstarted: {datetime.now().isoformat(timespec='seconds')}\nversion: {__version__}\n")
    (recorder.run_dir / "pid").write_text(str(os.getpid()))

    backend_headless.BACKEND = "tmux" if cfg.backend == "tmux" else "headless"
    if cfg.backend == "tmux":
        from .harness.backend_tmux import usable as tmux_usable
        if not tmux_usable():
            recorder.log("backend tmux requested but no run session is reachable; calls run headless")
    pools = _pools(cfg, recorder.log)
    if cfg.critic == "agent":
        crole = cfg.role("critic")
        critic = AgentCritic(
            pools["critic"], goal_text=goal_text, cwd=repo, timeout=crole.timeout_seconds,
            model=crole.model, transcript_path=lambda n, a: recorder.transcript_path(n, "critic", a),
            log=recorder.log,
        )
    else:
        critic = RulesCritic()

    engine = Engine(
        config=cfg, repo=repo, harness=pools["actor"], memory=memory, critic=critic,
        git=git, recorder=recorder, budget=BudgetClock(cfg.stop), goal_text=goal_text,
        maintainer=pools.get("maintainer"), planner=pools.get("planner"),
    )
    # a re-run of the same run name continues where the last one stopped
    prior = recorder.read_jsonl(recorder.iterations)
    last_iter = max((int(r.get("iteration") or 0) for r in prior), default=0)
    if last_iter:
        engine.iteration = last_iter
        prev = recorder.read_status() or {}
        engine.last_ok_tag = prev.get("last_ok_tag")
        recorder.log(f"continuing run {cfg.run} after iteration {last_iter}")

    reason = engine.run()
    print(f"ouroboros stopped: {reason}")
    _archive(repo, cfg, recorder.run_dir)
    return 0


def _launch_in_tmux(cfg: Config, repo: Path, *, own_session: bool) -> int:
    """Put the loop in tmux: a window of the session we are in, or a session of its own.

    The window is the default when there is a session to put it in, because that
    session is usually the operator's -- an agent and a human already talking about
    this run -- and the run belongs beside them, not in a session they have to go
    and find. `ouroboros stop` reads the note left here to know which one to kill.
    """
    rd = run_dir_for(repo, cfg.run)
    pid, alive = _pid_alive(rd)
    if alive:
        print(f"run {cfg.run!r} is already running (pid {pid}). watch: ouroboros top   stop: ouroboros stop")
        return 1
    argv = [sys.argv[0], *sys.argv[1:]]
    here = None if own_session else tmux.current_session()
    if here:
        target = tmux.launch_window(here, tmux.window_name(cfg.run), argv, str(repo))
        _tmux_note(rd, "window", target)
        print(f"started in window {tmux.window_name(cfg.run)} of this tmux session ({here})\n"
              f"  watch:   ouroboros top      (open another window for it)\n"
              f"  status:  ouroboros status\n  stop:    ouroboros stop")
        return 0
    name = tmux.session_name(cfg.run)
    if tmux.session_exists(name):
        print(f"tmux session {name} already exists. attach: tmux attach -t {name}")
        return 1
    tmux.launch(name, argv, str(repo))
    _tmux_note(rd, "session", name)
    print(f"started in tmux session {name}\n  attach:  tmux attach -t {name}\n  status:  ouroboros status\n  stop:    ouroboros stop")
    return 0


def _tmux_note(run_dir: Path, kind: str, target: str) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "tmux").write_text(f"{kind} {target}\n")


def _tmux_target(run_dir: Path) -> tuple[str, str] | None:
    """('window', 'sess:@3') or ('session', 'ouroboros-x'), as `run` left it; None if unknown."""
    path = run_dir / "tmux"
    if not path.exists():
        return None
    kind, _, target = path.read_text().strip().partition(" ")
    return (kind, target) if kind in ("window", "session") and target else None


def _tmux_kill(cfg: Config, run_dir: Path) -> None:
    """Take down whatever `run` put in tmux. Never the operator's own session."""
    note = _tmux_target(run_dir)
    if note and note[0] == "window":
        tmux.kill_window(note[1])
    else:
        tmux.kill(tmux.session_name(cfg.run))


def _archive(repo: Path, cfg: Config, run_dir: Path) -> None:
    """Write the run's durable record. Never let a failure here lose the run.

    The digest is the only part of a run that outlives its own machine, so it is
    written by the code that ends a run rather than by a command a human has to
    remember: ot4 has no `REPORT.md` for exactly that reason.
    """
    try:
        digest, index = archive(repo, cfg, run_dir)
        print(f"archived {digest.relative_to(repo)} and {index.relative_to(repo)}")
        print("commit them: the run directory itself is gitignored and does not travel")
    except Exception as exc:                        # a digest is never worth a traceback
        print(f"could not archive run {cfg.run!r}: {exc!r}", file=sys.stderr)


def _pid_alive(run_dir: Path) -> tuple[int | None, bool]:
    pf = run_dir / "pid"
    if not pf.exists():
        return None, False
    try:
        pid = int(pf.read_text().strip())
        os.kill(pid, 0)
        return pid, True
    except (ValueError, ProcessLookupError, PermissionError):
        return None, False


def cmd_stop(args: argparse.Namespace) -> int:
    repo = repo_root()
    cfg = load_config(args, repo)
    rd = run_dir_for(repo, cfg.run)
    pid, alive = _pid_alive(rd)
    if not alive:
        print(f"run {cfg.run!r} is not running")
        _tmux_kill(cfg, rd)
        _archive(repo, cfg, rd)
        return 0
    os.kill(pid, signal.SIGTERM)
    for _ in range(40):
        time.sleep(0.25)
        if not _pid_alive(rd)[1]:
            break
    else:
        os.kill(pid, signal.SIGKILL)
    _tmux_kill(cfg, rd)
    print(f"stopped run {cfg.run!r} (pid {pid})")
    _archive(repo, cfg, rd)
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    repo = repo_root()
    cfg = load_config(args, repo)
    rec = Recorder(run_dir_for(repo, cfg.run), echo=lambda *_: None)
    if args.watch and not getattr(args, "plain", False) and sys.stdout.isatty() and sys.stdin.isatty():
        from .tui import run_tui
        return run_tui(
            rec.run_dir, repo=repo, plan_md=cfg.plan.md, stop_after_s=cfg.stop.after_seconds,
            max_iterations=cfg.stop.max_iterations,
            chains={name: [h for h, _ in cfg.role(name).chain] for name in cfg.active_roles},
            roles={name: cfg.role(name).chain for name in cfg.active_roles},
        )
    while True:
        st = rec.read_status()
        if args.watch:
            print("\033[2J\033[H", end="")
        if st is None:
            print(f"no status for run {cfg.run!r}")
        else:
            age = int(time.time() - st.get("epoch", time.time()))
            pid, alive = _pid_alive(rec.run_dir)
            proc = f"pid {pid} alive" if alive else "process NOT running"
            print(f"run {st['run']}  state={st['state']}  iteration={st['iteration']}  branch={st['branch']}  memory={st.get('memory', '?')}  [{proc}]")
            print(f"harness={st['harness']}  elapsed={st.get('elapsed_s', 0) // 60}m  updated {age}s ago")
            if st.get("usage_compact"):
                print(f"usage: {st['usage_compact']}")
            for u in (st.get("usage_lines") or []):
                print(f"  {u}")
            if st.get("limited"):
                print("limited: " + "  ".join(f"{k} for {v}" for k, v in st["limited"].items()))
            for k in ("last_verdict", "last_ok_tag", "why", "seconds", "stop_reason"):
                if st.get(k) is not None:
                    print(f"{k}={st[k]}")
            if rec.needs_human_path.exists():
                print("\n!! NEEDS_HUMAN.md exists:\n" + rec.needs_human_path.read_text())
        if not args.watch:
            return 0
        time.sleep(5)


# The report is the only thing the user reads in the morning, so it must not
# flatter the run: cost covers every role, and a revert counts only if git agrees.
# `oversee` is the step older runs wrote for the overseer, before it and the critic
# were one role; their decision lines carried that role's cost.
ROLE_OF_STEP = {"commit": "actor", "critique": "critic", "oversee": "overseer",
                "reconcile": "maintainer", "plan": "planner"}


def _cost_by_role(steps: list[dict], decisions: list[dict]) -> tuple[dict[str, float], int]:
    """Cost per role, and how many completed role calls reported no cost at all."""
    by_role: dict[str, float] = {}
    uncosted = 0
    for s in steps:
        role = ROLE_OF_STEP.get(s.get("step", ""))
        if role is None:
            continue
        if s.get("cost"):
            by_role[role] = by_role.get(role, 0.0) + s["cost"]
        elif not s.get("error"):
            uncosted += 1
    for d in decisions:
        if d.get("cost"):
            by_role["overseer"] = by_role.get("overseer", 0.0) + d["cost"]
    return by_role, uncosted


def _billed_split(by_role: dict[str, float], money: dict) -> str:
    """Roles that really cost money, largest first. Empty when nothing was billed."""
    if not by_role or not money:
        return ""
    return ", ".join(f"{r} ${c:.2f}" for r, c in sorted(by_role.items(), key=lambda kv: -kv[1]))


def cmd_archive(args: argparse.Namespace) -> int:
    """Write the durable record by hand: one run, or every run in `.ouroboros/runs/`.

    `run` and `stop` already do this. The command exists for the two cases they
    cannot cover -- backfilling runs that finished before there was a history,
    and refreshing a digest after its branch was merged, since `merged:` is a
    fact about the base branch and changes without the run changing.
    """
    repo = repo_root()
    cfg = load_config(args, repo)
    runs = [cfg.run]
    if args.all:
        root = repo / ".ouroboros" / "runs"
        runs = sorted(d.name for d in root.iterdir() if d.is_dir()) if root.exists() else []
        if not runs:
            print(f"no run directories under {root}", file=sys.stderr)
            return 1
    written = []
    for name in runs:
        cfg.run = name
        rd = run_dir_for(repo, name)
        if not rd.exists():
            print(f"no run directory for {name!r}", file=sys.stderr)
            return 1
        facts = history.gather(repo, cfg, rd, machine=args.machine)
        written.append(history.write_digest(repo, facts))
        print(f"{name}: {facts.iterations} iterations, {facts.commits} commits, "
              f"{facts.ticked} of {facts.criteria_total} criteria ticked, "
              + (f"merged {facts.merged[:8]}" if facts.merged else "not merged"))
    for path in written:
        print(f"wrote {path.relative_to(repo)}")
    print(f"wrote {history.write_index(repo).relative_to(repo)}")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    repo = repo_root()
    cfg = load_config(args, repo)
    rec = Recorder(run_dir_for(repo, cfg.run), echo=lambda *_: None)
    steps = rec.read_jsonl(rec.iterations)
    decisions = rec.read_decisions()
    commits = [s for s in steps if s.get("step") == "commit"]
    reverts = [s for s in steps if s.get("step") == "revert"]
    by_role, uncosted = _cost_by_role(steps, decisions)
    cost = sum(by_role.values())
    failed_reverts = unverified_reverts(repo, cfg.branch, reverts)
    counts: dict[str, int] = {}
    for d in decisions:
        counts[d["verdict"]] = counts.get(d["verdict"], 0) + 1
    st = rec.read_status() or {}
    lines = [f"# Ouroboros report: {cfg.run}", ""]
    lines += [f"- state: {st.get('state', '?')}  (stop reason: {st.get('stop_reason', '-')})",
              f"- iterations: {len(commits)}   changed: {sum(1 for c in commits if c.get('changed'))}   recorded: {sum(1 for c in commits if c.get('recorded'))}",
              f"- reverts: {len(reverts)}" + (f"   **{len(failed_reverts)} did not take**" if failed_reverts else "")]
    usage_lines = [str(u) for u in (st.get("usage_lines") or [])]
    if usage_lines:
        lines += [f"- usage: {usage_lines[0]}"] + [f"         {u}" for u in usage_lines[1:]]
    # What a run costs is the slice of a rationing window it burns. Dollars appear
    # only for a harness billed per call; a subscription's `total_cost_usd` is priced
    # from token counts at API list prices and is not money anyone pays.
    money = st.get("money") or {}
    if money:
        split = _billed_split(by_role, money)
        lines += [f"- billed: " + ", ".join(f"{h} ${float(c):.2f}" for h, c in sorted(money.items()))
                  + (f" ({split})" if split else "")]
    lines += [f"- branch: {cfg.branch}", ""]
    if not usage_lines:
        lines += ["> No harness reported a usage window, so this run has no meter."
                  " Cost is not one: a subscription bills a flat fee, not per call.", ""]
    if failed_reverts:
        lines += ["## Reverts that did not take (rejected work is still on the branch)", ""]
        lines += [f"- #{r.get('iteration')}: `{r.get('sha')}` never reached `{r.get('to')}`"
                  + (f" — {r['error']}" if r.get("error") else "") for r in failed_reverts]
        lines += ["", "Undo each by hand before merging, or the critic's rejections ship with the run.", ""]
    bets = [s for s in steps if s.get("step") == "plan"]
    lines += ["## Bets the planner changed this run (overrule by editing the charter)", ""]
    lines += [f"- #{b.get('iteration')} ({b.get('why')}): {b.get('bet') or 'no bet landed' + (' — ' + b['error'] if b.get('error') else '')}" for b in bets] or ["- (no planner pass ran)"]
    plan_md = repo / cfg.plan.md
    plan_file = plan_md if plan_md.exists() else repo / ".ouroboros" / "plan.md"
    if plan_file.exists() and plan_file.read_text().strip():
        lines += ["", f"## The plan now (`{plan_file.relative_to(repo)}`)", "", plan_file.read_text().strip()[:6000]]
    lines += ["", "## Critic verdicts", ""] + [f"- {k}: {v}" for k, v in sorted(counts.items())] + [""]
    answered = [d for d in decisions if d["verdict"] in ("answer", "done_rejected", "stuck", "looping", "reject", "revert")]
    if answered:
        lines += ["## Decisions made for you (overrule in the morning)", ""]
        lines += [f"- #{d['iteration']} {d['verdict']}: {d['reason']}" for d in answered] + [""]
    if rec.needs_human_path.exists():
        lines += ["## NEEDS HUMAN", "", rec.needs_human_path.read_text(), ""]
    lines += ["## Merge", "", f"    git log --oneline main..{cfg.branch}", f"    git diff main...{cfg.branch} --stat", ""]
    text = "\n".join(lines)
    out = rec.run_dir / "REPORT.md"
    out.write_text(text)
    print(text)
    print(f"(written to {out})")
    return 0


# Where each harness discovers skills. All three read the same `SKILL.md` folder
# format: Claude Code as /name, Codex as $name, Pi by `--skill <dir>` or discovery.
USER_SKILL_DIRS = {
    "claude": Path(".claude") / "skills",
    "codex": Path(".codex") / "skills",
    "pi": Path(".pi") / "agent" / "skills",
    "agents": Path(".agents") / "skills",     # the cross-tool location Codex and others scan
}
PROJECT_SKILL_DIRS = [Path(".claude/skills"), Path(".agents/skills"), Path(".pi/skills")]


def skill_targets(*, user: bool, home: Path | None = None) -> list[Path]:
    """Project dirs, or for --user the dirs of every harness that has a home folder (plus ~/.agents)."""
    if not user:
        return list(PROJECT_SKILL_DIRS)
    home = home or Path.home()
    out = [home / rel for name, rel in USER_SKILL_DIRS.items() if name == "agents" or (home / rel.parts[0]).exists()]
    return out


def cmd_skills(args: argparse.Namespace) -> int:
    src = resources.files("ouroboros.skills")
    targets = [Path(args.target)] if args.target else skill_targets(user=args.user)
    for t in targets:
        t.mkdir(parents=True, exist_ok=True)
        for skill in src.iterdir():
            if not skill.is_dir():
                continue
            dest = t / skill.name
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(str(skill), dest)
            print(f"installed {dest}")
    return 0


# ----------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ouroboros", description="A loop runner for agent harnesses.")
    p.add_argument("--version", action="version", version=f"ouroboros {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    i = sub.add_parser("init", help="write .ouroboros/config.yml and goal.md")
    i.add_argument("--name")
    i.add_argument("--force", action="store_true")
    i.set_defaults(fn=cmd_init)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", help="path to config.yml")
    common.add_argument("--run-name", dest="run_name")

    r = sub.add_parser("run", parents=[common], help="start the loop (inside tmux by default)")
    r.add_argument("--for", dest="for_", help="wall clock budget, e.g. 8h, 90m")
    r.add_argument("--max-iterations", type=int)
    r.add_argument("--max-cost", type=float, help="stop at this API-equivalent cost in USD (informational on subscriptions)")
    r.add_argument("--max-usage", type=float, metavar="FRACTION",
                   help="stop when a subscription's weekly window passes this fraction, e.g. 0.8 for 80%%")
    r.add_argument("--harness", choices=["claude", "codex", "pi"])
    r.add_argument("--model")
    r.add_argument("--allow-dirty", action="store_true")
    r.add_argument("--critic", choices=["agent", "rules"], help="rules: the deterministic critic, no model call")
    r.add_argument("--maintainer", action="store_true", default=None, help="a separate maintainer role (default: the actor does housekeeping)")
    r.add_argument("--planner", action="store_true", default=None, help="a separate planner role (default: the critic's reply names the next unit)")
    r.add_argument("--memory", choices=["auto", "hypergraph", "handoff"])
    r.add_argument("--foreground", action="store_true", help="do not wrap in tmux")
    r.add_argument("--own-session", action="store_true",
                   help="a detached tmux session of its own, even when started from inside a tmux session")
    r.set_defaults(fn=cmd_run)

    s = sub.add_parser("status", parents=[common], help="show the current state (--watch: the live TUI)")
    s.add_argument("--watch", action="store_true", help="keep watching; on a terminal this is the btop-style TUI")
    s.add_argument("--plain", action="store_true", help="with --watch: the plain text loop instead of the TUI")
    s.set_defaults(fn=cmd_status)

    top = sub.add_parser("top", parents=[common], help="the live TUI (same as status --watch)")
    top.set_defaults(fn=cmd_status, watch=True, plain=False)

    stp = sub.add_parser("stop", parents=[common], help="stop the running loop (SIGTERM, then the tmux session)")
    stp.set_defaults(fn=cmd_stop)

    rep = sub.add_parser("report", parents=[common], help="write REPORT.md for the morning")
    rep.set_defaults(fn=cmd_report)

    pf = sub.add_parser("preflight", parents=[common], help="check everything `run` checks, without starting")
    pf.set_defaults(fn=cmd_preflight)

    ar = sub.add_parser("archive", parents=[common],
                        help="write .ouroboros/history/<run>.md and RUNS.md (run and stop do this too)")
    ar.add_argument("--all", action="store_true", help="every run directory, not just this config's run")
    ar.add_argument("--machine", help="record this machine name instead of the hostname (for backfill)")
    ar.set_defaults(fn=cmd_archive)

    d = sub.add_parser("design", parents=[common], help="write the charter by interview, in claude, codex, or pi")
    d.add_argument("--harness", choices=["claude", "codex", "pi"])
    d.set_defaults(fn=cmd_design)

    sk = sub.add_parser("skills", help="install the ouroboros-* skills for claude, codex, and pi")
    sk.add_argument("action", choices=["install"])
    sk.add_argument("--user", action="store_true", help="install to ~/.claude, ~/.codex, ~/.pi/agent, ~/.agents skill dirs")
    sk.add_argument("--target")
    sk.set_defaults(fn=cmd_skills)
    return p


def design_command(harness: str, skill_dir: Path, installed: bool) -> list[str]:
    """The interactive harness call that runs the design interview.

    Claude Code and Codex get the installed skill by name; when it is not installed,
    or for Pi, the skill body travels inline (Pi also gets `--skill <dir>`).
    """
    body = (skill_dir / "SKILL.md").read_text()
    body = body.split("\n---\n", 1)[1] if body.startswith("---") else body
    inline = "Follow this skill now, in this repository:\n\n" + body
    if harness == "claude":
        return ["claude", "/ouroboros-design" if installed else inline]
    if harness == "codex":
        return ["codex", "$ouroboros-design" if installed else inline]
    if harness == "pi":
        return ["pi", "--skill", str(skill_dir), "Run the ouroboros-design skill now, in this repository."]
    raise ValueError(f"unknown harness {harness!r}")


def pick_harness(cfg: Config, wanted: str | None) -> str | None:
    """The harness for an interactive session: asked for, else the actor chain, else any on PATH."""
    order = [wanted] if wanted else [h for h, _ in cfg.role("actor").chain] + ["claude", "codex", "pi"]
    for name in order:
        if name and shutil.which(name):
            return name
    return None


def cmd_design(args: argparse.Namespace) -> int:
    """Run the charter interview in an interactive harness session."""
    repo = repo_root()
    cfg = load_config(args, repo)
    if not (repo / DEFAULT_CONFIG_PATH).exists():
        cmd_init(argparse.Namespace(name=None, force=False))
    harness = pick_harness(cfg, args.harness)
    if harness is None:
        print("no harness on PATH (claude, codex, or pi); install and log in to one first", file=sys.stderr)
        return 2
    skill_dir = Path(str(resources.files("ouroboros.skills").joinpath("ouroboros-design")))
    installed = (Path.home() / USER_SKILL_DIRS[harness] / "ouroboros-design").exists() if harness in ("claude", "codex") else False
    cmd = design_command(harness, skill_dir, installed)
    print(f"design interview in {harness}; it writes .ouroboros/goal.md and config.yml, then run `ouroboros run`\n")
    return subprocess.call(cmd, cwd=repo)


MENU = {"1": ["init"], "2": ["design"], "3": ["run"], "4": ["status", "--watch"]}
MENU_TEXT = "1. init\n2. design\n3. run\n4. monitor\n"


def interactive_menu(parser: argparse.ArgumentParser) -> int:
    print(MENU_TEXT)
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return 0

    choices = MENU
    try:
        while True:
            choice = input("").strip()
            if choice in choices:
                args = parser.parse_args(choices[choice])
                print()
                return args.fn(args)
    except (EOFError, KeyboardInterrupt):
        print()
        return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    parser = build_parser()
    if not argv:
        render_banner()
        return interactive_menu(parser)
    args = parser.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
