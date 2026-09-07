#!/usr/bin/env python3
"""Synthetic runs for working on the monitor, so the TUI can be driven without a night.

Each scenario is a whole self-contained fake repo under `.ouroboros/synthetic/`, which
is gitignored. It gets its own `.git` (so `repo_root()` stops there), its own config,
STATE.md and PLAN.md, and a run directory with the same files a real run writes:
status.json, iterations.jsonl, overseer.jsonl, loop.log and transcripts.

    python tools/fakerun.py                 # build every scenario
    python tools/fakerun.py night           # build one
    python tools/fakerun.py night --live    # build it, then keep it moving

Then, in another terminal:

    cd .ouroboros/synthetic/night && ouroboros top

Scenarios exist to push the panels somewhere a real run rarely goes:

  night    ten hours of ordinary work in ordinary sentences: the reading the monitor
           will really be given, and the one to judge a layout against
  nominal  a healthy mid-run, six hours in: two harnesses, nothing on fire
  stress   three harnesses, one limited and one at 97% of its week, 240 iterations,
           reverts, timeouts, NEEDS_HUMAN, and text that is too long, wide or foreign
  fresh    forty seconds old: no iterations, no transcripts, no usage yet
  done     a finished run, process gone, so the dead-run rendering can be seen
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOME = ROOT / ".ouroboros" / "synthetic"

MODELS = {"claude": "claude-fable-5-1", "codex": "gpt-6-astra", "pi": "pi-1-preview-20260812"}


# --------------------------------------------------------------------------- voice
# `stress` exists to prove the panels survive text that is long, wide and not ASCII.
# Every other scenario wants the opposite: the ordinary sentences a night actually
# produces, so a layout can be judged on the reading it will really be given.
LONG = ("The packaged lifecycle gate ran against the staged payload and the manifest "
        "comparison came back clean, but the inherited CTest suite still carries its 162 "
        "baseline failures, which predate this run and are not attributable to the change "
        "under review, so they are reported rather than chased. ") * 3
WIDE = "pixi run python -m pytest " + " ".join(f"src/Mod/cadex/cadex_tests/test_{n}.py" for n in
                                               ("library", "terminals", "tessellation", "wiring_scope", "catalog"))
FOREIGN = "记录节点已导出并通过校验 · зафиксировано · 記録ノードを検証しました · ✅ 🔁 ⚠️"

SAID = [
    "The tessellation cache was keyed on the shape handle, which is reused after a document "
    "reload, so two different solids could share a mesh. Keyed it on the placement hash instead.",
    "Reading CadexCatalog.py first — the part registry is the thing the robot prompt walks, "
    "so I want to know its shape before I add to it.",
    "Added the 25T horn as a library value rather than a script. It came from the manufacturer "
    "STEP, so the tooth count and the boss diameter are measured, not guessed.",
    "The clearance call needs a scriptable entry point. Right now it only exists behind the GUI "
    "command, which the headless walk cannot reach.",
    "Test run is green: 2062 passed, 14 skipped. The skips are the GUI ones, same as before.",
    "Three of the four failures were the same missing fixture. Added it to conftest and re-ran; "
    "the fourth is a real ordering bug in the wiring scope.",
    "Stopping here rather than widening this. The change is bounded and recorded, and the next "
    "unit should be the variant study, which needs its own iteration.",
    "The fork delta is down to 41 files from 46. The two I removed were both dead compatibility "
    "shims for the 0.21 branch.",
    "Regenerating STATE.md from the graph now, so the frontier reflects the node I just closed.",
    "The headless renderer writes a PNG but the named angle is still hard-coded to iso. Threading "
    "the angle through takes a signature change in three call sites.",
    "Found it: the motor mount inherited its origin from the parent body, so moving the leg moved "
    "the mount twice. Reset the placement to the local frame.",
    "No change needed. The behaviour the critic flagged is the documented one, and the test that "
    "looked wrong is asserting the documented case.",
]
DID = [
    ("Bash", "pixi run pytest src/Mod/cadex/cadex_tests/test_catalog.py -q"),
    ("Bash", "pixi run pytest -q -x"),
    ("Read", "src/Mod/cadex/CadexCatalog.py"),
    ("Read", "src/Mod/cadex/mechanisms/four_bar.py"),
    ("Edit", "src/Mod/cadex/CadexCatalog.py"),
    ("Edit", "src/Mod/cadex/tessellate.py"),
    ("Grep", "def register_part"),
    ("Bash", "git diff --stat"),
    ("Bash", "pixi run python -m cadex.walk --headless --angle front"),
    ("Write", "src/Mod/cadex/cadex_tests/test_wiring_scope.py"),
    ("Bash", "ls src/Mod/cadex/library/horns"),
]
SAW = [
    "2062 passed, 14 skipped in 71.40s",
    "1 failed, 2061 passed in 74.12s\nFAILED test_wiring_scope.py::test_scope_is_ordered",
    " src/Mod/cadex/CadexCatalog.py | 34 +++++++++++++-----\n 1 file changed, 26 insertions(+), 8 deletions(-)",
    "wrote build/walk/front.png (1600x1200)",
    "horn_25t_futaba.step  horn_25t_savox.step  horn_15t_generic.step",
]
CRITIC_OK = [
    "The unit is bounded, the test covers the new branch, and STATE.md moved in the same commit.",
    "Small and recorded. The fixture is in the right place and nothing else moved.",
    "Accepts. The measurement comes from the STEP file rather than a magic number, which is what "
    "the criterion asked for.",
]
CRITIC_NO = [
    "The cache key change is right but there is no test that two reloaded solids get different "
    "meshes, which is the whole bug.",
    "This widened the scope: the angle threading is fine, but the unrelated rename in "
    "tessellate.py belongs in its own unit.",
    "STATE.md was not updated, so the frontier still says this node is open. Record it or the "
    "next iteration will redo the work.",
]
MUST_FIX = [
    "Add a test that reloads the document and asserts the two solids do not share a mesh.",
    "Revert the rename in tessellate.py and land it separately.",
    "Update the frontier entry for the node you just closed.",
]
OVERSEER_WHY = [
    "Work is landing and the frontier is moving. Continue.",
    "Third iteration on this node, but each one closed a distinct sub-problem. Continue.",
    "The critic rejected and the actor has the must-fix. Revert and let it retry.",
    "The actor asked whether to keep the 0.21 shims. Answering: drop them, the charter targets 1.0.",
    "Two consecutive empty commits on this node. Not stuck yet, but the next one decides it.",
]
REPLIES = [
    "Drop the 0.21 shims. The charter targets 1.0 only, and the fork-delta criterion counts them "
    "against you.",
    "Keep going on the catalog. The variant study can wait until the walk runs on a fresh machine.",
    "Yes, measure it from the STEP. A magic number will not pass the criterion.",
]
BETS = [
    "making the clearance call scriptable unblocks both the headless walk and the variant study",
    "the catalog is the bottleneck for the robot prompt; ten more parts should move two criteria",
    "the fork delta will not fall further by deleting shims — the rest is real divergence",
]
PLAN_ITEMS = [
    "Make the clearance call reachable without the GUI, then use it from the headless walk.",
    "Widen the catalog to cover the parts the robot prompt actually asks for.",
    "Thread a named angle through the headless renderer instead of hard-coding iso.",
    "Qualify a second mechanism from a paper, so the library has more than the four-bar.",
    "Upstream the three shims that are still real divergence in the fork.",
    "Regenerate STATE.md from the graph on every reconcile, not by hand.",
    "Add the L3 motors and the mechanisms that depend on them.",
    "Get mg-legs tipping at the declared shove band.",
]
CRITERIA = [
    "the walk runs on a fresh machine", "compound mechanisms are library values",
    "the catalog is broad enough for a robot prompt", "a research report renders headlessly",
    "the fork's delta is smaller than at the start", "a variant study exists",
    "one mechanism came from a paper", "the robot prompt works unattended",
    "mg-legs tips at the declared shove band", "L3 motors and mechanisms exist",
    "25T horns come from manufacturer STEP", "the agent can see without a screen",
    "inherited-tree reduction continues", "the RL training loop and mg-legs",
    "headless review renders a named angle", "assembly inventory lands in the project",
    "clearance calls are scriptable", "a second mechanism is qualified",
    "the packaged payload is portable", "the licensing gate passes clean",
    "STATE.md regenerates from the graph",
]


class Voice:
    """The text a scenario speaks: the hostile strings for `stress`, real ones for the rest."""

    def __init__(self, rng, hostile: bool) -> None:
        self.rng, self.hostile = rng, hostile

    def _pick(self, pool, cap):
        return LONG[:cap] if self.hostile else self.rng.choice(pool)

    def said(self):       return self._pick(SAID, 300)
    def critic_ok(self):  return self._pick(CRITIC_OK, 240)
    def critic_no(self):  return self._pick(CRITIC_NO, 240)
    def must_fix(self):   return self._pick(MUST_FIX, 200)
    def overseer(self):   return self._pick(OVERSEER_WHY, 160)
    def bet(self):        return self._pick(BETS, 90)
    def plan_item(self):  return self._pick(PLAN_ITEMS, 120)
    def blurb(self, cap): return (LONG if self.hostile else self.rng.choice(SAID))[:cap]

    def reply(self):
        return (FOREIGN + " " + LONG)[:600] if self.hostile else self.rng.choice(REPLIES)

    def did(self):
        return ("Bash", WIDE) if self.hostile else self.rng.choice(DID)

    def saw(self):
        return (WIDE + "\n") * 6 if self.hostile else self.rng.choice(SAW)


def ts(offset_s: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=offset_s)).isoformat(timespec="seconds")


def sha(rng) -> str:
    return "".join(rng.choice("0123456789abcdef") for _ in range(10))


def slug(rng) -> str:
    a = rng.choice(["quiet", "amber", "still", "brave", "hollow", "solar", "tidy", "vast", "damp", "keen"])
    b = rng.choice(["reef", "glade", "tower", "delta", "sage", "wolf", "pine", "cove", "quill", "anchor"])
    return f"{a}-{b}-{rng.randint(1000, 9999)}"


# --------------------------------------------------------------------------- scenarios
def scenario(name: str) -> dict:
    """Everything that differs between scenarios, in one place."""
    common = dict(run=name, branch=f"ouroboros/{name}", mode="actor-critic", state="work",
                  stop_after_s=15 * 3600, iterations=40, limited={}, needs_human=None,
                  reverts=(), timeouts=(), gaps=(6, 14), plan_items=4, alive=True, hostile=False,
                  actor_median_s=360,
                  roles={"actor": [("claude", MODELS["claude"]), ("codex", MODELS["codex"])],
                         "critic": [("codex", MODELS["codex"])],
                         "overseer": [("claude", "claude-opus-5"), ("codex", MODELS["codex"])],
                         "maintainer": [("claude", MODELS["claude"]), ("codex", MODELS["codex"])],
                         "planner": [("claude", MODELS["claude"]), ("codex", MODELS["codex"])]},
                  usage={"claude": (0.18, 0.41), "codex": (0.09, 0.22)})
    if name == "night":
        # No `elapsed_s`: the night is as long as its own iterations took, which is the
        # only way the per-iteration chart and the wall clock tell the same story.
        return {**common, "iterations": 52, "state": "critique", "hypergraph": True,
                "actor_median_s": 260, "reverts": (11, 34, 47), "timeouts": (39,),
                "gaps": (9, 16), "plan_items": 5,
                "usage": {"claude": (0.16, 0.44), "codex": (0.05, 0.19)}}
    if name == "nominal":
        return common
    if name == "stress":
        return {**common, "iterations": 240, "state": "critique", "elapsed_s": 13 * 3600, "hostile": True,
                "limited": {"claude": "223m (You've hit your session limit · resets 12:10pm (Europe/Madrid))"},
                "reverts": (17, 63, 110, 158, 201, 233), "timeouts": (44, 129),
                "needs_human": ("Iteration 233 was rejected, but reverting to `ouroboros/stress/ok-0232` failed:\n\n"
                                "    revert to ouroboros/stress/ok-0232 left HEAD at 7dfc2ff9a9, whose tree still differs\n\n"
                                "The rejected commit is still on `stress`. Undo it by hand before merging."),
                "gaps": (19, 21), "plan_items": 8,
                "hypergraph": True,
                "roles": {**common["roles"], "planner": [("pi", MODELS["pi"]), ("claude", MODELS["claude"])]},
                "usage": {"claude": (0.13, 0.33), "codex": (0.24, 0.97), "pi": (0.0, 0.03)}}
    if name == "fresh":
        return {**common, "iterations": 0, "state": "work", "elapsed_s": 40, "usage": {},
                "gaps": (6, 14), "plan_items": 0}
    if name == "done":
        return {**common, "iterations": 68, "state": "stopped", "elapsed_s": 11 * 3600, "alive": False,
                "stop_reason": "max_usage 80% reached (codex seven_day at 81%)",
                "reverts": (28, 64), "usage": {"claude": (0.13, 0.33), "codex": (0.24, 0.81)}}
    raise SystemExit(f"unknown scenario {name!r}; try: {', '.join(SCENARIOS)}")


SCENARIOS = ("night", "nominal", "stress", "fresh", "done")


# --------------------------------------------------------------------------- the repo
def write_repo(root: Path, spec: dict, rng, v: Voice) -> None:
    root.mkdir(parents=True, exist_ok=True)
    if not (root / ".git").exists():
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)

    open_gaps, total = spec["gaps"]
    frontier = [f"- [{'open' if i < open_gaps else 'working'}] **Charter criterion {i + 1}: {t}** "
                f"(`{slug(rng)}`) — {v.blurb(180)}"
                for i, t in enumerate(CRITERIA[:total])]
    arch = [f"  - [{'open' if i % 3 else 'working'}] **{slug(rng)}** (`{slug(rng)}`) — {v.blurb(90)}"
            for i in range(total + 12)]
    (root / "STATE.md").write_text("# synthetic — State\n\n## Frontier\n\n" + "\n".join(frontier) +
                                   "\n\n## Architecture\n\n- **synthetic** (`fond-ember-4937`)\n"
                                   + "\n".join(arch) + "\n")

    items = [f"{i + 1}. **{slug(rng)}.** {v.plan_item()}" for i in range(spec["plan_items"])]
    if items:
        (root / "PLAN.md").write_text("# synthetic — plan view\n\n## Frontier\n\n- [open] **short** "
                                      "(`young-crane-9546`) — the short plan\n\n## Current\n\n"
                                      + "\n".join(items) + "\n")
    elif (root / "PLAN.md").exists():
        (root / "PLAN.md").unlink()

    # A hypergraph repo serves the plan from graph nodes, not PLAN.md, and that is the
    # branch `_plan_short` tries first. `night` and `stress` use it so both paths run.
    if spec.get("hypergraph") and items:
        nodes = root / ".hypergraph" / "graph" / "plan"
        nodes.mkdir(parents=True, exist_ok=True)
        (nodes / "young-crane-9546.md").write_text(
            "---\nnode_id: fake\nslug: young-crane-9546\ntitle: short\n---\nStatus: open\n\n"
            "## Current\n\n" + "\n".join(items) + "\n")

    roles_yaml = "\n".join(
        f"  {r}:\n    harness: {c[0][0]}\n    model: {c[0][1]}\n    timeout: 30m"
        + ("\n    fallback:\n" + "\n".join(f"    - harness: {h}\n      model: {m}" for h, m in c[1:]) if len(c) > 1 else "")
        for r, c in spec["roles"].items())
    (root / ".ouroboros").mkdir(exist_ok=True)
    (root / ".ouroboros" / "config.yml").write_text(
        f"run: {spec['run']}\nmode: {spec['mode']}\nmemory: auto\nbackend: headless\n"
        f"roles:\n{roles_yaml}\nstop:\n  after: 15h\n  max_usage: 0.8\n"
        f"plan:\n  enabled: true\n  every: 5\n  md: PLAN.md\n")
    # `gaps_total` and `gaps_unchecked` are counted off the charter's done criteria,
    # so the goal file needs real ones or the frontier panel reads zero.
    closed = total - open_gaps
    done = "\n".join(f"- [{'x' if i < closed else ' '}] **{t}**" for i, t in enumerate(CRITERIA[:total]))
    (root / ".ouroboros" / "goal.md").write_text(
        f"# Goal: synthetic\n\n## Mission\n\nDrive the monitor.\n\n## Done criteria\n\n{done}\n")


# --------------------------------------------------------------------------- the run
def usage_block(spec: dict) -> tuple[dict, list[str]]:
    out, lines = {}, []
    for harness, (first, now) in spec["usage"].items():
        windows = {"seven_day": {"utilization": round(now, 4), "resets_at": time.time() + 3 * 86400, "minutes": 10080}}
        first_windows = {"seven_day": round(first, 4)}
        if harness == "claude":  # the only one that reports a short window too
            windows["five_hour"] = {"utilization": min(1.0, now * 2.4), "resets_at": time.time() + 5400, "minutes": 300}
            windows["seven_day_overage_included"] = {"utilization": min(1.0, now * 1.9), "resets_at": time.time() + 3 * 86400,
                                                     "minutes": 10080}
            first_windows["five_hour"] = round(first * 2.0, 4)
        out[harness] = {"harness": harness, "plan": "prolite" if harness == "codex" else None,
                        "windows": windows, "first_windows": first_windows}
        lines.append(f"{harness} seven_day {first * 100:.0f}% -> {now * 100:.0f}% ({(now - first) * 100:+.0f} this run)")
    return out, lines


def write_run(run_dir: Path, spec: dict, rng, v: Voice) -> None:
    if run_dir.exists():
        shutil.rmtree(run_dir)
    (run_dir / "transcripts").mkdir(parents=True)
    (run_dir / "reverted").mkdir()
    n_iter = spec["iterations"]
    decisions: list[tuple[float, dict]] = []
    cost_total = 0.0
    # Only the last LOG_LINES of loop.log reach the TUI, so put some switches in reach.
    switch_at = {max(1, n_iter - k) for k in (2, 9, 21)} | {7, 61} if n_iter > 25 else set()

    # A step's duration is the gap since the *previous* step of any kind, so the
    # timeline is built as a clock: advance by a stage's duration, then log the step
    # that ends it. Timestamps are placed on the wall clock at the end.
    events: list[tuple[float, dict]] = []      # (seconds from run start, record)
    logged: list[tuple[float, str]] = []
    clock = 0.0
    for n in range(1, n_iter + 1):
        reverted, slow = n in spec["reverts"], n in spec["timeouts"]
        turns = rng.randint(3, 46)
        # Real iterations vary a lot and occasionally blow out: a flat series makes the
        # chart a solid block and says nothing about how the night actually went.
        actor_s = rng.lognormvariate(math.log(spec["actor_median_s"]), 0.6)
        if rng.random() < 0.07:
            actor_s *= rng.uniform(3.0, 8.0)          # the occasional long grind
        if slow:
            actor_s = 90 * 60                          # a timeout is the whole budget
        harness = "codex" if (spec["limited"] and n % 5) else ("claude" if n % 4 else "codex")
        cost = 0.0 if harness == "codex" else round(rng.uniform(0.4, 5.5), 4)
        cost_total += cost

        clock += actor_s
        events.append((clock, {"iteration": n, "step": "actor", "attempt": 1 if slow else 0,
                               "exit": 1 if slow else 0, "timed_out": slow, "turns": turns,
                               "session": f"{rng.randint(10**7, 10**8)}-fake",
                               **({"error": "timed out after 90m"} if slow else {})}))
        logged.append((clock, f"[{n}] actor: attempt=0, exit={1 if slow else 0}, turns={turns}"))
        if n in switch_at:
            logged.append((clock, "harness claude is out of usage until 12:10 CEST: "
                                  "\"You've hit your session limit\""))
            logged.append((clock, f"harness: claude -> codex ({MODELS['codex']})"))

        clock += rng.uniform(1.0, 4.0)
        events.append((clock, {"iteration": n, "step": "commit", "sha": sha(rng),
                               "changed": not slow, "recorded": not slow and not reverted, "cost": cost}))
        clock += rng.uniform(18.0, 75.0)
        events.append((clock, {"iteration": n, "step": "critique", "verdict": "reject" if reverted else "accept",
                               "source": "critic:codex",
                               "reasons": v.critic_no() if reverted else v.critic_ok(),
                               "must_fix": v.must_fix() if reverted else None, "cost": 0.0}))
        if reverted:
            logged.append((clock, f"[{n}] critic rejected: {v.must_fix()}"))
        clock += rng.uniform(4.0, 22.0)
        ov = "revert" if reverted else ("stuck" if n % 97 == 0 else ("answer" if n % 41 == 0 else "continue"))
        events.append((clock, {"iteration": n, "step": "oversee", "verdict": ov, "source": "agent",
                               "reason": v.overseer()}))
        decisions.append((clock, {"iteration": n, "verdict": ov, "reason": v.overseer(),
                                  "reply": v.reply() if ov == "answer" else "", "overseer": "agent",
                                  "cost": round(rng.uniform(0.0, 0.4), 4)}))
        if reverted:
            clock += rng.uniform(2.0, 12.0)
            events.append((clock, {"iteration": n, "step": "revert",
                                   "to": f"ouroboros/{spec['run']}/ok-{n - 1:04d}", "sha": sha(rng)}))
            (run_dir / "reverted" / f"{n:04d}.patch").write_text("diff --git a/x b/x\n" + v.blurb(400))
        if n % 5 == 0:
            clock += rng.uniform(45.0, 150.0)
            events.append((clock, {"iteration": n, "step": "reconcile", "exit": 0, "timed_out": False,
                                   "sha": sha(rng), "cost": round(rng.uniform(0.3, 2.0), 4)}))
            clock += rng.uniform(120.0, 330.0)
            events.append((clock, {"iteration": n, "step": "plan", "why": "after reconcile",
                                   "bet": f"{slug(rng)} — Bet: {v.bet()}", "exit": 0, "timed_out": False,
                                   "sha": sha(rng), "cost": round(rng.uniform(0.5, 3.0), 4)}))
        logged.append((clock, f"[{n}] commit {sha(rng)}  changed={not slow}  recorded={not slow and not reverted}"))

    # A scenario that names its wall clock has the timeline scaled onto it, shape kept.
    # One that does not is simply as long as its own iterations took.
    elapsed = spec.get("elapsed_s") or max(60.0, clock)
    scale = (elapsed / clock) if clock > 0 else 1.0
    steps = [{"ts": ts((c * scale) - elapsed), **rec} for c, rec in events]
    decisions = [{"ts": ts((c * scale) - elapsed), **rec} for c, rec in decisions]
    log = [f"{ts((c * scale) - elapsed)} {line}" for c, line in logged]

    (run_dir / "iterations.jsonl").write_text("".join(json.dumps(s) + "\n" for s in steps))
    (run_dir / "overseer.jsonl").write_text("".join(json.dumps(d) + "\n" for d in decisions))
    (run_dir / "loop.log").write_text("\n".join(log[-400:]) + "\n")
    if spec["needs_human"]:
        (run_dir / "NEEDS_HUMAN.md").write_text(f"# Needs a human\n\n{ts(0)}\n\n{spec['needs_human']}\n")

    usage, usage_lines = usage_block(spec)
    status = {"ts": ts(0), "epoch": time.time() - rng.uniform(2, 40), "run": spec["run"], "state": spec["state"],
              "iteration": n_iter, "branch": spec["branch"], "harness": "codex" if spec["limited"] else "claude",
              "memory": "hypergraph", "cost_usd": round(cost_total, 4), "elapsed_s": int(elapsed),
              "last_ok_tag": f"ouroboros/{spec['run']}/ok-{max(0, n_iter - 1):04d}"}
    if spec["limited"]:
        status["limited"] = spec["limited"]
    if usage:
        status["usage"], status["usage_lines"] = usage, usage_lines
    if spec.get("stop_reason"):
        status["stop_reason"] = spec["stop_reason"]
    (run_dir / "status.json").write_text(json.dumps(status, indent=2))
    write_transcript(run_dir, spec, rng, v)


def write_transcript(run_dir: Path, spec: dict, rng, v: Voice) -> None:
    """The newest transcript feeds the messages panel; shape it like the real harness."""
    n = spec["iterations"]
    if not n:
        return
    on_codex = bool(spec["limited"])
    path = run_dir / "transcripts" / f"{n:04d}-actor.json"
    lines: list[dict] = []
    turns = 3 if spec["hostile"] else rng.randint(7, 11)
    if on_codex:
        lines.append({"type": "thread.started", "thread_id": "01a07a87-fake"})
        lines.append({"type": "turn.started"})
        for _ in range(turns):
            _, arg = v.did()
            lines.append({"type": "item.completed", "item": {"type": "reasoning", "text": v.said()}})
            lines.append({"type": "item.completed", "item": {"type": "command_execution",
                                                             "command": f"/bin/zsh -lc '{arg}'", "exit_code": 0}})
            lines.append({"type": "item.completed", "item": {"type": "agent_message", "text": v.said()}})
        lines.append({"type": "turn.completed", "usage": {"input_tokens": 935561, "output_tokens": 4055}})
    else:
        lines.append({"type": "system", "subtype": "init", "session_id": "fake", "model": MODELS["claude"]})
        for _ in range(turns):
            tool, arg = v.did()
            key = "file_path" if tool in ("Read", "Edit", "Write") else "pattern" if tool == "Grep" else "command"
            lines.append({"type": "assistant", "message": {"content": [
                {"type": "thinking", "thinking": v.said()},
                {"type": "text", "text": v.said()},
                {"type": "tool_use", "name": tool, "input": {key: arg, "description": "the gate"}}]}})
            lines.append({"type": "user", "message": {"content": [
                {"type": "tool_result", "content": v.saw()}]}})
        for util in (0.31, 0.62, spec["usage"].get("claude", (0, 0.4))[1]):
            lines.append({"type": "rate_limit_event", "session_id": "fake", "rate_limit_info": {
                "status": "allowed_warning" if util > 0.9 else "allowed", "rateLimitType": "five_hour",
                "unifiedWindows": {"five_hour": {"utilization": min(1.0, util * 2.4), "resetsAt": time.time() + 5400},
                                   "seven_day": {"utilization": util, "resetsAt": time.time() + 3 * 86400}}}})
        lines.append({"type": "result", "subtype": "success", "is_error": False, "num_turns": 33,
                      "total_cost_usd": 3.46, "result": v.said(), "session_id": "fake"})
    path.write_text("".join(json.dumps(l) + "\n" for l in lines))


# --------------------------------------------------------------------------- liveness
def set_alive(run_dir: Path, want: bool) -> None:
    """A run looks alive only if `pid` names a live process, so borrow a real sleeper."""
    pid_file = run_dir / "pid"
    old = run_dir / ".fake_sleeper"
    if old.exists():
        try:
            os.kill(int(old.read_text().strip()), signal.SIGTERM)
        except (ValueError, ProcessLookupError, PermissionError, OSError):
            pass
        old.unlink()
    if not want:
        pid_file.write_text("999999\n")   # a pid that is not there: the dead-run rendering
        return
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(86400)"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    pid_file.write_text(f"{proc.pid}\n")
    old.write_text(f"{proc.pid}\n")


def build(name: str, *, seed: int = 0) -> Path:
    rng = random.Random(seed or hash(name) & 0xFFFF)
    spec = scenario(name)
    v = Voice(rng, spec["hostile"])
    root = HOME / name
    write_repo(root, spec, rng, v)
    run_dir = root / ".ouroboros" / "runs" / spec["run"]
    write_run(run_dir, spec, rng, v)
    set_alive(run_dir, spec["alive"])
    return root


def tick(name: str) -> None:
    """Advance a built scenario by one iteration, so the monitor can be watched moving."""
    rng = random.Random()
    spec = scenario(name)
    v = Voice(rng, spec["hostile"])
    run_dir = HOME / name / ".ouroboros" / "runs" / spec["run"]
    status = json.loads((run_dir / "status.json").read_text())
    n = int(status.get("iteration", 0)) + 1
    stage = rng.choice(["work", "critique", "oversee", "reconcile", "plan"])
    cost = round(rng.uniform(0.2, 4.0), 4)
    with (run_dir / "iterations.jsonl").open("a") as f:
        f.write(json.dumps({"ts": ts(0), "iteration": n, "step": "actor", "attempt": 0, "exit": 0,
                            "timed_out": False, "turns": rng.randint(3, 40)}) + "\n")
        f.write(json.dumps({"ts": ts(0), "iteration": n, "step": "commit", "sha": sha(rng),
                            "changed": True, "recorded": True, "cost": cost}) + "\n")
        f.write(json.dumps({"ts": ts(0), "iteration": n, "step": "oversee", "verdict": "continue",
                            "source": "agent", "reason": v.overseer()}) + "\n")
    with (run_dir / "overseer.jsonl").open("a") as f:
        f.write(json.dumps({"ts": ts(0), "iteration": n, "verdict": "continue", "reason": v.overseer(),
                            "reply": "", "overseer": "agent", "cost": 0.1}) + "\n")
    with (run_dir / "loop.log").open("a") as f:
        f.write(f"{ts(0)} [{n}] actor: exit=0, turns={rng.randint(3, 40)}\n")
    # Usage only ever climbs, which is the point of the meter.
    for block in (status.get("usage") or {}).values():
        for w in block["windows"].values():
            w["utilization"] = min(1.0, w["utilization"] + rng.uniform(0.001, 0.006))
    status["usage_lines"] = [
        f"{h} seven_day {b['first_windows']['seven_day'] * 100:.0f}% -> {b['windows']['seven_day']['utilization'] * 100:.0f}%"
        f" ({(b['windows']['seven_day']['utilization'] - b['first_windows']['seven_day']) * 100:+.0f} this run)"
        for h, b in (status.get("usage") or {}).items()]
    status.update(ts=ts(0), epoch=time.time(), iteration=n, state=stage,
                  cost_usd=round(status.get("cost_usd", 0) + cost, 4),
                  elapsed_s=int(status.get("elapsed_s", 0)) + 6)
    (run_dir / "status.json").write_text(json.dumps(status, indent=2))
    write_transcript(run_dir, {**spec, "iterations": n}, rng, v)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("scenarios", nargs="*", default=None, help=f"one or more of: {', '.join(SCENARIOS)}")
    ap.add_argument("--live", action="store_true", help="after building, append an iteration every few seconds")
    ap.add_argument("--interval", type=float, default=4.0, help="seconds between live ticks")
    ap.add_argument("--clean", action="store_true", help="remove every synthetic scenario and exit")
    args = ap.parse_args()

    if args.clean:
        for d in sorted(HOME.glob("*")):
            set_alive(d / ".ouroboros" / "runs" / d.name, False)
        shutil.rmtree(HOME, ignore_errors=True)
        print(f"removed {HOME}")
        return 0

    names = args.scenarios or list(SCENARIOS)
    for name in names:
        root = build(name)
        print(f"{name:8} {root.relative_to(ROOT)}   cd {root.relative_to(ROOT)} && ouroboros top")

    if args.live:
        name = names[0]
        print(f"\nticking {name} every {args.interval}s — ctrl-c to stop")
        try:
            while True:
                time.sleep(args.interval)
                tick(name)
        except KeyboardInterrupt:
            print("\nstopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
