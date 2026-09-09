"""Hypergraph-protocol memory: one iteration = one dispatch; reconcile is a maintainer pass."""

from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
from importlib import resources
from pathlib import Path

import yaml

from .base import BaseMemory
from .. import goal as charter

_FM_TITLE = re.compile(r"^title:\s*(.+)$", re.MULTILINE)
_FM_ROOT = re.compile(r"^parents:\s*\[\s*\]\s*$", re.MULTILINE)
_UNREC = re.compile(r"(\d+) unreconciled record node")
_GAP_LINE = re.compile(r"^- \[gap\] (\S+): (.*)$", re.MULTILINE)
STATE_MD_LIMIT = 16000
PLAN_MD_LIMIT = 4000
_FRONTIER = re.compile(r"^## Frontier\s*$(.*?)(?=^## |\Z)", re.MULTILINE | re.DOTALL)
# `- [working] **Title** (`slug`) — ...` anywhere in STATE.md. Status plus slug is
# the whole of what "the frontier moved" means; the prose around it is not.
_NODE_STATUS = re.compile(r"\[([a-z_]+)\]\s+\*\*.+?\*\*\s+\(`([a-z0-9-]+)`\)")


def frontier_of(state_md: str) -> str:
    m = _FRONTIER.search(state_md)
    return m.group(1).strip() if m else ""


def _strip_front_matter(text: str) -> str:
    if text.startswith("---"):
        _, _, rest = text.partition("\n---\n")
        return rest.lstrip("\n")
    return text


def _title_of(node_file: Path) -> str:
    m = _FM_TITLE.search(node_file.read_text()[:2000])
    return m.group(1).strip().strip("'\"") if m else node_file.stem


class HypergraphMemory(BaseMemory):
    name = "hypergraph"

    def __init__(
        self,
        repo: Path,
        *,
        reconcile_every: int = 5,
        pressure: int = 3,
        budget_units: int = 1,
        binary: str = "hypergraph",
        recent: int = 3,
        plan: bool = True,
        plan_view: str = "plan",
        plan_md: str = "PLAN.md",
        max_new_directions: int = 3,
    ) -> None:
        self.repo = repo
        self.config_path = repo / ".hypergraph" / "config.yml"
        cfg = yaml.safe_load(self.config_path.read_text()) if self.config_path.exists() else {}
        cfg = cfg or {}
        self.graph_dir = repo / cfg.get("graph_dir", ".hypergraph/graph")
        self.cache_dir = repo / cfg.get("cache_dir", ".hypergraph/cache")
        self.state_md = repo / cfg.get("state_md", "STATE.md")
        self.record_root_slug = (cfg.get("record_root") or {}).get("slug")
        self.state_root_slug = (cfg.get("state_root") or {}).get("slug")
        self.plan = plan
        self.plan_view = plan_view
        self.plan_md = repo / plan_md
        self.max_new_directions = max_new_directions
        self.plan_root_slug = self._read_plan_root()
        self.reconcile_every = reconcile_every
        self.pressure = pressure
        self.budget_units = budget_units
        self.binary = shutil.which(binary) or binary
        self.recent = recent
        # run state
        self.branch = ""
        self.goal_text = ""
        self.directive_slug: str | None = None
        self.last_record_slug: str | None = None
        self.since_reconcile = 0
        self.run_dir: Path | None = None
        self.last_error: str | None = None
        self.reconcile_due = False

    def _read_plan_root(self) -> str | None:
        try:
            cfg = yaml.safe_load(self.config_path.read_text()) or {}
        except (OSError, yaml.YAMLError):
            return None
        return (((cfg.get("views") or {}).get(self.plan_view) or {}).get("root") or {}).get("slug")

    # -- CLI plumbing ----------------------------------------------------
    def hg(self, *args: str, timeout: float = 120) -> tuple[int, str]:
        try:
            proc = subprocess.run(
                [self.binary, *args], cwd=str(self.repo), capture_output=True, text=True, timeout=timeout
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return 2, f"hypergraph {' '.join(args)}: {exc}"
        return proc.returncode, (proc.stdout + proc.stderr).strip()

    def _cfg(self) -> list[str]:
        return ["--config", str(self.config_path.relative_to(self.repo))]

    def _exports(self) -> list[str]:
        rel = self.cache_dir.relative_to(self.repo)
        return ["--record", str(rel / "record.json"), "--state", str(rel / "state.json")]

    def export(self) -> tuple[int, str]:
        return self.hg("export", *self._cfg())

    def hwm(self) -> tuple[int, str]:
        code, out = self.export()
        if code != 0:
            return code, out
        return self.hg("hwm", *self._exports(), *self._cfg())

    def unreconciled(self) -> tuple[int, str]:
        code, out = self.hwm()
        if code != 0:
            return 0, out
        m = _UNREC.search(out)
        return (int(m.group(1)) if m else 0), out

    def check(self, since: str | None = None) -> tuple[int, str]:
        code, out = self.export()
        if code != 0:
            return code, out
        args = ["check", *self._exports(), *self._cfg()]
        if since:
            args += ["--since", since]
        return self.hg(*args)

    # -- node files ------------------------------------------------------
    def record_dir(self) -> Path:
        return self.graph_dir / "record"

    def record_files(self) -> list[Path]:
        d = self.record_dir()
        if not d.exists():
            return []
        return sorted(d.glob("*.md"), key=lambda p: p.stat().st_mtime)

    def find_record_root(self) -> str | None:
        if self.record_root_slug:
            return self.record_root_slug
        for f in self.record_files():
            head = f.read_text()[:2000]
            fm = head.partition("\n---\n")[0] if head.startswith("---") else ""
            if _FM_ROOT.search(fm):
                self.record_root_slug = f.stem
                return f.stem
        return None

    def find_record_by_title(self, prefix: str) -> str | None:
        """Newest record node whose title starts with `prefix`."""
        for f in reversed(self.record_files()):
            if _title_of(f).startswith(prefix):
                return f.stem
        return None

    def new_record(
        self, *, title: str, body: str, parent: str | None, none_reason: str | None = None,
        impacts: list[str] | None = None,
    ) -> str | None:
        args = ["new", "record", *self._cfg(), "--title", title, "--body", "-", "--repo-auto"]
        for imp in impacts or []:
            args += ["--impact", imp]
        if not impacts:
            args += ["--none", none_reason or "no state change"]
        if parent:
            args += ["--parent", parent]
        try:
            proc = subprocess.run([self.binary, *args], cwd=str(self.repo), input=body, capture_output=True, text=True, timeout=120)
        except (OSError, subprocess.TimeoutExpired) as exc:
            self.last_error = str(exc)
            return None
        if proc.returncode != 0:
            self.last_error = (proc.stderr or proc.stdout).strip()
            return None
        return proc.stdout.split()[0] if proc.stdout.split() else None

    # -- lifecycle -------------------------------------------------------
    def read_record(self, slug: str) -> str:
        f = self.record_dir() / f"{slug}.md"
        return _strip_front_matter(f.read_text()) if f.exists() else ""

    def charter_gaps(self, goal_text: str) -> list[tuple[str, str]]:
        """(gap-name, criterion) for every done criterion, names made unique."""
        seen: dict[str, int] = {}
        out: list[tuple[str, str]] = []
        for crit in charter.done_criteria(goal_text):
            name = charter.gap_name(crit)
            seen[name] = seen.get(name, 0) + 1
            if seen[name] > 1:
                name = f"{name}-{seen[name]}"
            out.append((name, crit))
        return out

    def start(self, *, run: str, goal_text: str, run_dir: Path, branch: str) -> None:
        self.branch, self.goal_text, self.run_dir = branch, goal_text, run_dir
        digest = hashlib.sha256(goal_text.encode()).hexdigest()[:8]
        title = f"Ouroboros run: {run}"
        versioned = f"{title} [{digest}]"
        self.directive_slug = self.find_record_by_title(versioned)
        if self.directive_slug is None:
            previous = self.find_record_by_title(title)  # an earlier goal version of this run, if any
            known = dict(_GAP_LINE.findall(self.read_record(previous))) if previous else {}
            gaps = self.charter_gaps(goal_text)
            added = [(n, c) for n, c in gaps if n not in known]
            dropped = [(n, c) for n, c in known.items() if n not in {g for g, _ in gaps}]
            gap_lines = "\n".join(f"- [gap] {n}: {c}" for n, c in gaps) or "- (none declared)"
            supersedes = (
                f"\n\nThis directive supersedes `{previous}`: the operator edited the charter and restarted the run."
                if previous else ""
            )
            dropped_text = (
                "\n\nCriteria this version drops (the maintainer marks their gaps superseded):\n"
                + "\n".join(f"- {n}: {c}" for n, c in dropped)
            ) if dropped else ""
            body = (
                "## What\n\nOperator directive: an Ouroboros loop starts on this repo. Every work node of the run "
                f"descends from this node.{supersedes}\n\n## Why\n\nThe charter (the operator's goal document), verbatim:\n\n"
                f"{goal_text.strip()}\n\n**Done criteria as gaps** (one open state node each; work closes them through "
                f"declared impacts):\n\n{gap_lines}{dropped_text}\n\n"
                f"## Method\n\nOuroboros iterations on branch `{branch}`: orient, one dispatched unit, record, commit; "
                "a reconcile pass folds the tail on pressure; with the planner on, a bet follows each reconcile.\n\n"
                "## Result\n\nDirective recorded. Work follows as child nodes.\n"
            )
            impacts = [
                f"NEW {n} — Charter gap, status open: {c[:300]}. Flip to working only when the criterion is verifiably met."
                for n, c in added
            ]
            impacts += self.plan_seed_impacts(goal_text)
            self.directive_slug = self.new_record(
                title=f"{versioned} — operator directive", body=body, parent=previous or self.find_record_root(),
                impacts=impacts,
                none_reason="operator directive with no new gaps; impacts are declared by the work nodes that follow",
            )
            if self.directive_slug is not None and impacts:
                self.reconcile_due = True   # fold the gaps onto the frontier at the first chance
        if self.directive_slug is None:
            raise RuntimeError(f"could not record the run directive: {self.last_error}")
        files = self.record_files()
        newest = files[-1].stem if files else None
        self.last_record_slug = newest or self.directive_slug

    # -- the plan view ---------------------------------------------------
    def ensure_plan_view(self) -> bool:
        """Declare the plan view once (a reconcile-gated write Ouroboros owns). True when it exists."""
        if not self.plan:
            return False
        if self.plan_root_slug:
            return True
        code, out = self.hg("views", "add", self.plan_view, "--md", str(self.plan_md.relative_to(self.repo)), "--reconcile", *self._cfg())
        if code != 0:
            self.last_error = out
            return False
        self.plan_root_slug = self._read_plan_root()
        return self.plan_root_slug is not None

    def plan_dir(self) -> Path:
        return self.graph_dir / self.plan_view

    def plan_nodes(self) -> list[dict]:
        """Every plan node but the root: {slug, title, status, current}."""
        out = []
        d = self.plan_dir()
        if not d.exists():
            return out
        for f in sorted(d.glob("*.md")):
            if f.stem == self.plan_root_slug:
                continue
            text = f.read_text()
            body = _strip_front_matter(text)
            status = body.splitlines()[0].replace("Status:", "").strip() if body.startswith("Status:") else "?"
            current = body.split("## Current", 1)[1].split("## Negative knowledge", 1)[0].strip() if "## Current" in body else body
            out.append({"slug": f.stem, "title": _title_of(f), "status": status, "current": current})
        order = {"short": 0, "medium": 1, "long": 2}
        out.sort(key=lambda n: (order.get(n["title"].lower(), 9), n["title"]))
        return out

    def plan_text(self, limit: int = PLAN_MD_LIMIT) -> str:
        nodes = self.plan_nodes()
        if not nodes:
            return self.plan_md_text()[:limit]
        text = "\n\n".join(f"### {n['title']} (`{n['slug']}`, {n['status']})\n\n{n['current']}" for n in nodes)
        return text[:limit]

    def plan_seed_impacts(self, goal_text: str) -> list[str]:
        """Impacts that seed the plan view from the charter's horizon ladder, once."""
        if not self.ensure_plan_view() or self.plan_nodes():
            return []
        ladder = charter.horizon_ladder(goal_text)
        if not ladder:
            return []

        def rung(*names: str) -> str:
            return " ".join(ladder.get(n, "") for n in names).strip()[:400] or "(the charter gives no rung here)"

        seed = "Seeded from the charter's horizon ladder; the planner re-plans after each maintainer pass"
        return [
            f"{self.plan_view}/NEW short — {seed}. Next units, one iteration each: {rung('short')}",
            f"{self.plan_view}/NEW medium — {seed}. Open gaps in order, several units each: {rung('medium')}",
            f"{self.plan_view}/NEW long — {seed}. Directions and standing work: {rung('long')}",
        ]

    def plan_pending(self) -> str:
        """Lines of `check` that name pending impacts on the plan view."""
        code, out = self.check()
        lines = [l for l in out.splitlines() if f"[{self.plan_view}/" in l or "pending impact" in l]
        return "\n".join(lines) or "(none)"

    def planner_prompt(self, signals: str) -> str | None:
        if not self.plan or not self.plan_root_slug:
            return None
        tpl = _strip_front_matter(resources.files("ouroboros.skills").joinpath("ouroboros-planner/SKILL.md").read_text())
        frontier = frontier_of(self.state_md.read_text()) if self.state_md.exists() else "(STATE.md missing)"
        recent = self.record_files()[-8:]
        recent_text = "\n".join(f"- `{f.stem}` — {_title_of(f)}" for f in recent) or "(none)"
        parent = self.last_record_slug or self.directive_slug or "<causal-parent-slug>"
        fold = (
            f"1. Write ONE decision record, title starting `Bet:` (never more than one per pass). Body headings exactly "
            f"`## What`, `## Why`, `## Method`, `## Result`. `## Why` carries the reasoning and cites the evidence. Mint it:\n"
            f"   `hypergraph new record --config .hypergraph/config.yml --title \"Bet: <summary>\" --body bet.md --parent {parent} "
            f"--impact \"{self.plan_view}/<slug> — <delta>\" --repo-auto`, one `--impact` per plan node you change "
            f"(`{self.plan_view}/NEW <name>` for a new one). Use `--none \"plan holds: <reason>\"` when nothing changes; then stop after step 5.\n"
            f"2. You are the single writer of the `{self.plan_view}` view. Fold your own bet and every pending plan impact listed above:\n"
            f"   - new node: `hypergraph new {self.plan_view} --config .hypergraph/config.yml --title <short|medium|long|name> --status open "
            f"--parent {self.plan_root_slug} --prov \"<bet-slug> — why\" --reconcile --body current.md` where current.md holds ONLY the "
            f"`## Current` content (ranked bullets, each with `[rec: <slug>]`).\n"
            f"   - existing node: `hypergraph update <slug> --config .hypergraph/config.yml --print-sha`, then rewrite the FULL body "
            f"(`Status: open`, `## Current`, `## Negative knowledge`, `## Provenance` with the bet slug added) and "
            f"`hypergraph update <slug> --config .hypergraph/config.yml --body full.md --expect <sha> --reconcile`.\n"
            f"   Never touch the state graph (`hypergraph new state`, `update` on a state slug) and never edit STATE.md.\n"
            f"3. Advance the view's mark: `hypergraph export --config .hypergraph/config.yml`, then "
            f"`hypergraph hwm --record .hypergraph/cache/record.json --state .hypergraph/cache/state.json --config .hypergraph/config.yml "
            f"--view {self.plan_view} --tips` prints `high_water_mark: <slugs>`. Rewrite the view root `{self.plan_root_slug}` body "
            f"(keep its prose, set `## Reconciliation` to that mark and `reconciled_at` now) through `hypergraph update --expect --reconcile`.\n"
            f"4. `hypergraph sync --config .hypergraph/config.yml` renders `{self.plan_md.name}` and runs check. Fix any violation and sync again.\n"
            f"5. `git add .hypergraph {self.plan_md.name}` and commit with a message starting `plan:`. Delete your scratch files."
        )
        return (
            tpl.replace("{max_new}", str(self.max_new_directions))
            .replace("{charter}", (self.goal_text or "").strip()[:12000] or "(none)")
            .replace("{frontier}", frontier or "(empty: no open gaps — propose directions)")
            .replace("{plan}", self.plan_text(6000) or "(no plan nodes yet: create short, medium, long from the pending impacts)")
            .replace("{pending}", self.plan_pending())
            .replace("{recent}", recent_text)
            .replace("{signals}", signals.strip() or "(none)")
            .replace("{fold}", fold)
        )

    def verify_bet(self, before: set[str]) -> str | None:
        for f in self.record_files():
            if f.name not in before and _title_of(f).startswith("Bet:"):
                self.last_record_slug = f.stem
                return f"{f.stem} — {_title_of(f)}"
        return None

    def frontier_fingerprint(self) -> str | None:
        """Every state node's status, plus how many charter criteria are still open.

        These are the only two things that make a run's night worth having: a
        node changed status, or a criterion got ticked. Records, plans, audits
        and docs are how those happen, not whether they did. nt3 wrote 22,437
        lines and this digest changed once in 201 iterations.
        """
        if not self.state_md.exists():
            return None
        try:
            text = self.state_md.read_text()
        except OSError:
            return None
        pairs = sorted({(slug, status) for status, slug in _NODE_STATUS.findall(text)})
        if not pairs:
            return None
        open_criteria = len(charter.done_criteria(self.goal_text or ""))
        body = f"{open_criteria}|" + ";".join(f"{slug}={status}" for slug, status in pairs)
        return hashlib.sha256(body.encode()).hexdigest()[:16]

    def mark_iteration(self) -> None:
        self.since_reconcile += 1

    def mark_reconciled(self) -> None:
        self.since_reconcile = 0
        self.reconcile_due = False

    # -- adapter protocol ------------------------------------------------
    def orient_prompt(self) -> str:
        parts = [
            "## Hypergraph\n\nThis repo keeps its memory as a hypergraph-protocol graph. STATE.md below is what is "
            "true now. The unreconciled tail after it is the replayable log on top of that checkpoint: read each "
            "listed node's `## State Impact` and apply it in your head before choosing work.\n\n"
            f"This iteration is **one dispatch with a budget of {self.budget_units} unit(s)**. Target: the goal, "
            "or a frontier node (open / broken / blocked) in STATE.md that serves it. Say which in `## Why`.\n\n"
            "**Forbidden in a work iteration, no exceptions:** the hypergraph-reconcile skill, `hypergraph update`, "
            "`hypergraph new state`, `hypergraph views add`, editing anything under `.hypergraph/graph/state/`, "
            "editing STATE.md. You are a contributor: you record; a separate reconcile pass folds the tail. If the "
            "tail looks fat, say so in `## Result` and keep working. Even when the done criteria are met, the "
            "next unit is a rung of the horizon ladder, never a reconcile.",
        ]
        if self.state_md.exists():
            text = self.state_md.read_text()
            if len(text) > STATE_MD_LIMIT:
                text = text[:STATE_MD_LIMIT] + "\n\n(… STATE.md truncated; open the file for the rest)"
            parts.append(f"## STATE.md\n\n{text.strip()}")
        else:
            parts.append("## STATE.md\n\n(missing — run `hypergraph sync --config .hypergraph/config.yml`)")
        plan = self.plan_text()
        if plan:
            parts.append("## Plan (agent-owned bets: short / medium / long)\n\nPick this iteration's unit from `short` unless the "
                         "critic's message or a broken frontier node says otherwise.\n\n" + plan)
        count, out = self.unreconciled()
        parts.append(f"## Unreconciled tail ({count} node(s))\n\n```\n{out.strip()[-4000:]}\n```")
        recent = self.record_files()[-self.recent :]
        if recent:
            parts.append("## Most recent record nodes\n\n" + "\n".join(f"- `{f.stem}` — {_title_of(f)}" for f in recent))
        return "\n\n".join(parts)

    def record_prompt(self) -> str:
        parent = self.last_record_slug or self.directive_slug or "<causal-parent-slug>"
        return (
            "When the unit is finished, and before you stop, you MUST record it as ONE record node "
            "(the hypergraph-record skill; never a state node, never a reconcile):\n"
            "1. Write a body file with exactly these headings: `## What`, `## Why`, `## Method`, `## Result`. "
            "A dead end is recorded the same way as a success. `## Why` says why this unit, and, if you did "
            "not do what the critic's message asked, what you did instead and why. `## Result` carries what "
            "is true now, then any concern, assumption, or new dependency the next iteration must know about, "
            "and ends with the line `Dispatch closed: 1 unit — <summary>`.\n"
            f"2. Mint it (the causal parent is the previous node of this run unless the work truly follows from "
            f"another node — then use that slug):\n"
            f"   `hypergraph new record --config .hypergraph/config.yml --title \"<title>\" --body body.md "
            f"--parent {parent} --impact \"<state-slug> — <delta>\" --repo-auto`\n"
            "   Use `--impact \"NEW <kebab-name> — <delta>\"` for a new state node, or `--none \"<reason>\"` when "
            "state truly does not change. Look up real state slugs in STATE.md; a wrong slug fails the checker "
            "(exit 2 = nothing written; fix and retry).\n"
            "3. `hypergraph export --config .hypergraph/config.yml` then `git add .hypergraph/graph`.\n"
            "4. Delete the body file, or keep it out of the repo.\n"
            "Never hand-edit STATE.md. Never run `hypergraph update` or `new state`. An iteration with no new "
            "record node is counted as lost work."
        )

    def snapshot(self) -> set[str]:
        return {f.name for f in self.record_files()}

    def verify_recorded(self, before: set[str]) -> bool:
        new = [f for f in self.record_files() if f.name not in before]
        if not new:
            return False
        self.last_record_slug = new[-1].stem
        return True

    def needs_reconcile(self) -> bool:
        if self.reconcile_due:
            return True
        if self.reconcile_every and self.since_reconcile >= self.reconcile_every:
            return True
        count, _ = self.unreconciled()
        return count >= self.pressure

    def reconcile_prompt(self) -> str | None:
        tpl = _strip_front_matter(
            resources.files("ouroboros.skills").joinpath("ouroboros-maintainer/SKILL.md").read_text()
        )
        _, tail = self.unreconciled()
        return (
            tpl.replace("{branch}", self.branch or "(run branch)")
            .replace("{tail}", f"```\n{tail.strip()[-4000:]}\n```")
            .replace("{goal}", (self.goal_text or "").strip()[:4000] or "(none)")
        )

    def plan_md_text(self) -> str:
        return self.plan_md.read_text().strip() if self.plan_md.exists() else ""

    def critic_context(self) -> str:
        parts = []
        if self.state_md.exists():
            frontier = frontier_of(self.state_md.read_text())
            parts.append("### Frontier (open, broken, blocked state nodes)\n\n" + (frontier or "(empty: no open gaps)"))
        else:
            parts.append("### Frontier\n\n(STATE.md missing)")
        count, _ = self.unreconciled()
        parts.append(f"Unreconciled record nodes past the high-water mark: {count} (their declared impacts are not on the frontier yet).")
        plan = self.plan_text()
        if plan:
            parts.append("### Plan (agent-owned bets: short / medium / long)\n\n" + plan)
        return "\n\n".join(parts)

    def check_report(self) -> str | None:
        code, out = self.check()
        if code == 0:
            return None
        tail = "\n".join(l for l in out.splitlines() if not l.startswith("info")).strip()[-2000:]
        return f"`hypergraph check` exit {code}:\n{tail}"

    def last_summary(self) -> str:
        files = self.record_files()
        if not files:
            return "no record node"
        return _title_of(files[-1])[:72]
