"""ouroboros: init | run | status | report | skills install."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from datetime import datetime
from importlib import resources
from pathlib import Path

from . import __version__, tmux
from .budget import BudgetClock
from .config import DEFAULT_CONFIG_PATH, Config
from .engine import Engine
from .gitguard import GitError, GitGuard
from .harness import make_harness
from .memory import make_memory
from .memory.handoff import HandoffMemory
from .recorder import Recorder
from .roles.overseer import AgentOverseer, RulesOverseer

GOAL_TEMPLATE = """# Goal: {name}

## Mission

(one paragraph. what and why.)

## Done criteria

- [ ] ...

## Horizon ladder

What to do if this runs for:

- **the next hour:** ...
- **the next day:** ...
- **the next week:** ...
- **the next month:** ...
- **the next year:** ...

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

(creative | maintain | report_done)

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
    if getattr(args, "mode", None):
        cfg.mode = args.mode
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
    if getattr(args, "allow_dirty", False):
        cfg.git.allow_dirty = True
    if getattr(args, "overseer", None):
        cfg.overseer = args.overseer
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
    try:
        make_harness(cfg.role("actor").harness)
    except ValueError as exc:
        return str(exc)
    return None


def cmd_run(args: argparse.Namespace) -> int:
    repo = repo_root()
    cfg = load_config(args, repo)

    problem = preflight(cfg, repo)
    if problem:
        print(f"ouroboros: {problem}", file=sys.stderr)
        return 2

    if not args.foreground and not tmux.inside_ouroboros_tmux():
        if not tmux.available():
            print("tmux not found; running in the foreground", file=sys.stderr)
        else:
            name = tmux.session_name(cfg.run)
            if tmux.session_exists(name):
                print(f"tmux session {name} already exists. attach: tmux attach -t {name}")
                return 1
            tmux.launch(name, [sys.argv[0], *sys.argv[1:]], str(repo))
            print(f"started in tmux session {name}\n  attach:  tmux attach -t {name}\n  status:  ouroboros status\n  stop:    tmux kill-session -t {name}")
            return 0

    git = GitGuard(repo, cfg.branch)
    try:
        git.start(allow_dirty=cfg.git.allow_dirty)
    except GitError as exc:
        print(f"git: {exc}", file=sys.stderr)
        return 2

    memory = make_memory(cfg.memory, repo, recent=cfg.handoff.recent)
    if isinstance(memory, HandoffMemory):
        memory.ensure()
    goal_path = repo / cfg.goal
    goal_text = goal_path.read_text() if goal_path.exists() else ""
    if not goal_text.strip():
        print(f"no goal at {goal_path}; run `ouroboros init` and fill it in", file=sys.stderr)
        return 2

    recorder = Recorder(run_dir_for(repo, cfg.run))
    (recorder.run_dir / "run.yml").write_text(cfg.dump() + f"\nstarted: {datetime.now().isoformat(timespec='seconds')}\nversion: {__version__}\n")
    (recorder.run_dir / "pid").write_text(str(os.getpid()))

    if cfg.overseer == "agent":
        orole = cfg.role("overseer")
        overseer = AgentOverseer(
            make_harness(orole.harness), goal_text=goal_text, cwd=repo, timeout=orole.timeout_seconds,
            model=orole.model, transcript_path=lambda n, a: recorder.transcript_path(n, "overseer", a),
            log=recorder.log,
        )
    else:
        overseer = RulesOverseer()

    engine = Engine(
        config=cfg, repo=repo, harness=make_harness(cfg.role("actor").harness), memory=memory,
        git=git, recorder=recorder, budget=BudgetClock(cfg.stop), goal_text=goal_text, overseer=overseer,
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
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    repo = repo_root()
    cfg = load_config(args, repo)
    rec = Recorder(run_dir_for(repo, cfg.run), echo=lambda *_: None)
    while True:
        st = rec.read_status()
        if args.watch:
            print("\033[2J\033[H", end="")
        if st is None:
            print(f"no status for run {cfg.run!r}")
        else:
            age = int(time.time() - st.get("epoch", time.time()))
            print(f"run {st['run']}  state={st['state']}  iteration={st['iteration']}  branch={st['branch']}")
            print(f"harness={st['harness']}  cost=${st.get('cost_usd', 0):.2f}  elapsed={st.get('elapsed_s', 0) // 60}m  updated {age}s ago")
            for k in ("last_verdict", "last_ok_tag", "why", "seconds", "stop_reason"):
                if st.get(k) is not None:
                    print(f"{k}={st[k]}")
            if rec.needs_human_path.exists():
                print("\n!! NEEDS_HUMAN.md exists:\n" + rec.needs_human_path.read_text())
        if not args.watch:
            return 0
        time.sleep(5)


def cmd_report(args: argparse.Namespace) -> int:
    repo = repo_root()
    cfg = load_config(args, repo)
    rec = Recorder(run_dir_for(repo, cfg.run), echo=lambda *_: None)
    steps = rec.read_jsonl(rec.iterations)
    decisions = rec.read_jsonl(rec.overseer)
    commits = [s for s in steps if s.get("step") == "commit"]
    reverts = [s for s in steps if s.get("step") == "revert"]
    cost = sum(s.get("cost") or 0 for s in commits)
    counts: dict[str, int] = {}
    for d in decisions:
        counts[d["verdict"]] = counts.get(d["verdict"], 0) + 1
    st = rec.read_status() or {}
    lines = [f"# Ouroboros report: {cfg.run}", ""]
    lines += [f"- state: {st.get('state', '?')}  (stop reason: {st.get('stop_reason', '-')})",
              f"- iterations: {len(commits)}   changed: {sum(1 for c in commits if c.get('changed'))}   recorded: {sum(1 for c in commits if c.get('recorded'))}",
              f"- reverts: {len(reverts)}", f"- cost: ~${cost:.2f}", f"- branch: {cfg.branch}", ""]
    lines += ["## Overseer verdicts", ""] + [f"- {k}: {v}" for k, v in sorted(counts.items())] + [""]
    answered = [d for d in decisions if d["verdict"] in ("answer", "done_rejected", "stuck", "revert")]
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


def cmd_skills(args: argparse.Namespace) -> int:
    src = resources.files("ouroboros.skills")
    targets = [Path(args.target)] if args.target else [Path(".claude/skills"), Path(".agents/skills")]
    if args.user:
        targets = [Path.home() / ".claude" / "skills"]
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
    r.add_argument("--max-cost", type=float)
    r.add_argument("--mode")
    r.add_argument("--harness", choices=["claude", "codex", "pi"])
    r.add_argument("--model")
    r.add_argument("--allow-dirty", action="store_true")
    r.add_argument("--overseer", choices=["agent", "rules"])
    r.add_argument("--foreground", action="store_true", help="do not wrap in tmux")
    r.set_defaults(fn=cmd_run)

    s = sub.add_parser("status", parents=[common], help="show the current state")
    s.add_argument("--watch", action="store_true")
    s.set_defaults(fn=cmd_status)

    rep = sub.add_parser("report", parents=[common], help="write REPORT.md for the morning")
    rep.set_defaults(fn=cmd_report)

    sk = sub.add_parser("skills", help="install the ouroboros-* skills")
    sk.add_argument("action", choices=["install"])
    sk.add_argument("--user", action="store_true", help="install to ~/.claude/skills")
    sk.add_argument("--target")
    sk.set_defaults(fn=cmd_skills)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
