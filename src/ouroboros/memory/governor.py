"""Governor memory: a record/state graph in the repo's Flywheel repository (`.flywheel/`).

The same loop shape as the hypergraph adapter, through the `governor` CLI: one
iteration = one record; the reconcile pass is a fold (the single writer of state);
the planner writes one `Bet:` record and folds it into `plan`-kind claims. Nothing is
read from node files: every read is `governor snapshot` JSON (cached by head inside
governor), and every write is a governor command, which commits to the Flywheel
repository itself. There is nothing to `git add` but the rendered STATE.md / PLAN.md.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import tempfile
from importlib import resources
from pathlib import Path
from .base import BaseMemory
from .hypergraph import (
    PLAN_MD_LIMIT,
    STATE_MD_LIMIT,
    _main_checkout,
    _strip_front_matter,
    charter_gaps,
    directive_body,
    maintainer_prompt,
)
from .. import goal as charter

_GAP_LINE = re.compile(r"^- \[gap\] (\S+): (.*)$", re.MULTILINE)
_NEW_LINE = re.compile(r"^- NEW ([a-z0-9-]+) — (.*)$", re.MULTILINE)
FRONTIER_STATUSES = ("broken", "blocked", "open")
HORIZONS = ("short", "medium", "long")


def governor_repo(repo: Path) -> Path | None:
    """The checkout holding the Flywheel repository for `repo` (itself or its main checkout)."""
    for cand in (repo, _main_checkout(repo)):
        if (cand / ".flywheel" / "db.sqlite").exists():
            return cand
    return None


def _section(content: str, heading: str) -> str:
    """The text under `## heading`, up to the next `## `."""
    for part in re.split(r"^## ", content or "", flags=re.MULTILINE):
        if part.startswith(heading + "\n") or part.strip() == heading:
            return part[len(heading):].strip()
    return ""


def _first_line(text: str, limit: int = 200) -> str:
    for line in (text or "").splitlines():
        line = re.sub(r"^[-*]\s*", "", line).strip()
        if line and not line.startswith("_("):
            return line if len(line) <= limit else line[: limit - 1] + "…"
    return ""


class Graph:
    """One parsed `governor snapshot`."""

    def __init__(self, data: dict | None) -> None:
        data = data or {}
        self.ok = bool(data)
        self.root: str | None = data.get("root")
        self.head: str | None = data.get("head")
        self.nodes: dict[str, dict] = {k: v for k, v in (data.get("nodes") or {}).items() if v.get("live", True)}
        self.edges: list[dict] = list(data.get("edges") or [])

    def slug(self, node_id: str) -> str:
        n = self.nodes.get(node_id) or {}
        return n.get("slug") or node_id[:8]

    def records(self) -> list[dict]:
        return sorted((n for n in self.nodes.values() if n.get("kind") == "record"), key=lambda n: n.get("seq", 0))

    def tail(self) -> list[dict]:
        return [n for n in self.records() if not n.get("folded")]

    def claims(self, kind: str = "state") -> list[dict]:
        """Every claim of `kind`, components (status-less children of the root) excluded."""
        out = []
        for n in self.nodes.values():
            if n.get("kind") != kind:
                continue
            if not n.get("status") and self.root in (n.get("parents") or []):
                continue
            out.append(n)
        return sorted(out, key=lambda n: n.get("seq", 0))

    def by_slug(self, slug: str) -> dict | None:
        for n in self.nodes.values():
            if n.get("slug") == slug:
                return n
        return None

    def impacts_of(self, record: dict) -> list[tuple[dict | None, str, str]]:
        """(target node or None for NEW, label, delta) for every impact a record declares."""
        out: list[tuple[dict | None, str, str]] = []
        for e in self.edges:
            if e.get("source") != record["id"]:
                continue
            target = self.nodes.get(e.get("target", ""))
            if not target or target.get("kind") not in ("state", "plan"):
                continue
            for line in (e.get("content") or "").splitlines():
                line = line.strip()
                if line.startswith("impact:"):
                    out.append((target, target.get("slug") or target["id"][:8], line[len("impact:"):].strip()))
        sec = (record.get("content") or "").split("\n## State Impact", 1)
        for name, delta in _NEW_LINE.findall(sec[1] if len(sec) > 1 else ""):
            out.append((None, f"NEW {name}", delta.strip()))
        return out

    def none_reason(self, record: dict) -> str | None:
        sec = (record.get("content") or "").split("\n## State Impact", 1)
        m = re.search(r"^none:\s*(.+)$", sec[1] if len(sec) > 1 else "", re.MULTILINE)
        return m.group(1).strip() if m else None

    def cited_by(self, kind: str) -> set[str]:
        """Record ids cited by any claim of `kind`."""
        ids = {n["id"] for n in self.nodes.values() if n.get("kind") == kind}
        return {e["target"] for e in self.edges if e.get("source") in ids}


class GovernorMemory(BaseMemory):
    name = "governor"

    def __init__(
        self,
        repo: Path,
        *,
        reconcile_every: int = 5,
        pressure: int = 3,
        budget_units: int = 1,
        binary: str = "governor",
        recent: int = 3,
        plan: bool = True,
        plan_view: str = "plan",
        plan_md: str = "PLAN.md",
        max_new_directions: int = 3,
        state_md: str = "STATE.md",
    ) -> None:
        self.repo = repo
        self.state_md = repo / state_md
        self.plan = plan
        self.plan_view = plan_view          # the plan component's title, and the `plan-` prefix of NEW horizons
        self.plan_md = repo / plan_md
        self.max_new_directions = max_new_directions
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
        self._home: Path | None = None
        self._cache: tuple | None = None
        self.plan_root_slug: str | None = self._find_plan_root(self.graph()) if plan else None

    # -- CLI plumbing ----------------------------------------------------
    def gov(self, *args: str, stdin: str | None = None, timeout: float = 120) -> tuple[int, str]:
        try:
            proc = subprocess.run(
                [self.binary, *args], cwd=str(self.repo), input=stdin, capture_output=True, text=True, timeout=timeout
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return 2, f"governor {' '.join(args)}: {exc}"
        return proc.returncode, (proc.stdout + proc.stderr).strip()

    def _db_stamp(self) -> tuple | None:
        """The Flywheel database's write stamp: reads never change it, every commit does."""
        home = governor_repo(self.repo) if self._home is None else self._home
        self._home = home
        if home is None:
            return None
        stamp = []
        for name in ("db.sqlite", "db.sqlite-wal"):
            try:
                st = (home / ".flywheel" / name).stat()
                stamp.append((st.st_mtime_ns, st.st_size))
            except OSError:
                stamp.append(None)
        return tuple(stamp)

    def graph(self) -> Graph:
        """The current snapshot; re-read only when the database changed since the last read."""
        stamp = self._db_stamp()
        if stamp is not None and self._cache is not None and self._cache[0] == stamp:
            return self._cache[1]
        g = self._read_graph()
        if g.ok and stamp is not None:
            self._cache = (stamp, g)
        return g

    def _read_graph(self) -> Graph:
        try:
            proc = subprocess.run([self.binary, "snapshot"], cwd=str(self.repo), capture_output=True, text=True, timeout=120)
        except (OSError, subprocess.TimeoutExpired) as exc:
            self.last_error = str(exc)
            return Graph(None)
        if proc.returncode != 0:
            self.last_error = (proc.stderr or proc.stdout).strip()
            return Graph(None)
        try:
            return Graph(json.loads(proc.stdout))
        except json.JSONDecodeError as exc:
            self.last_error = f"governor snapshot: {exc}"
            return Graph(None)

    def fold_apply(self, plan: dict) -> tuple[int, str]:
        with tempfile.NamedTemporaryFile("w", suffix=".json", prefix="ouroboros-fold-", delete=False) as fh:
            json.dump(plan, fh)
            path = fh.name
        try:
            return self.gov("fold", "--apply", path)
        finally:
            Path(path).unlink(missing_ok=True)

    def render_state(self) -> bool:
        code, out = self.gov("render", "-o", str(self.state_md))
        if code != 0:
            self.last_error = out
        return code == 0

    def check(self) -> tuple[int, str]:
        return self.gov("check")

    # -- records ---------------------------------------------------------
    def record_ids(self, g: Graph | None = None) -> list[str]:
        return [n["id"] for n in (g or self.graph()).records()]

    def find_record_by_title(self, prefix: str, g: Graph | None = None) -> str | None:
        """Newest record whose title starts with `prefix`."""
        for n in reversed((g or self.graph()).records()):
            if (n.get("title") or "").startswith(prefix):
                return n.get("slug")
        return None

    def read_record(self, slug: str | None) -> str:
        if not slug:
            return ""
        n = self.graph().by_slug(slug)
        return (n or {}).get("content", "")

    def new_record(
        self, *, title: str, body: str, parent: str | None, none_reason: str | None = None,
        impacts: list[tuple[str, str]] | None = None, news: list[tuple[str, str]] | None = None,
    ) -> str | None:
        """Append one record. `impacts` are (claim-slug, delta), `news` are (kebab-name, delta)."""
        args = ["record", "--title", title, "--body", "-", "--parent", parent or "root"]
        for slug, delta in impacts or []:
            args += ["--impact", f"{slug}: {delta}"]
        for name, delta in news or []:
            args += ["--new", f"{name}: {delta}"]
        if not impacts and not news:
            args += ["--none", none_reason or "no state change"]
        code, out = self.gov(*args, stdin=body)
        if code != 0:
            self.last_error = out
            return None
        first = out.split()
        return first[0] if first else None

    def charter_gaps(self, goal_text: str) -> list[tuple[str, str]]:
        return charter_gaps(goal_text)

    # -- lifecycle -------------------------------------------------------
    def start(self, *, run: str, goal_text: str, run_dir: Path, branch: str) -> None:
        self.branch, self.goal_text, self.run_dir = branch, goal_text, run_dir
        self._ensure_ignored()
        g = self.graph()
        if not g.ok or not g.root:
            raise RuntimeError(f"no governor graph here ({self.last_error or 'no root'}): `flywheel repo:init` then `governor init`")
        digest = hashlib.sha256(goal_text.encode()).hexdigest()[:8]
        title = f"Ouroboros run: {run}"
        versioned = f"{title} [{digest}]"
        self.directive_slug = self.find_record_by_title(versioned, g)
        if self.directive_slug is None:
            previous = self.find_record_by_title(f"{title} [", g)  # an earlier goal version of this run, if any
            known = dict(_GAP_LINE.findall(self.read_record(previous))) if previous else {}
            gaps = self.charter_gaps(goal_text)
            added = [(n, c) for n, c in gaps if n not in known]
            dropped = [(n, c) for n, c in known.items() if n not in {g_ for g_, _ in gaps}]
            body = directive_body(goal_text=goal_text, branch=branch, previous=previous, gaps=gaps,
                                  dropped=dropped, fence_charter=True)
            news = [
                (n, f"Charter gap, status open: {c[:300].rstrip('.')}. Flip to working only when the criterion is verifiably met.")
                for n, c in added
            ]
            news += self.plan_seed_news(goal_text)
            self.directive_slug = self.new_record(
                title=f"{versioned} — operator directive", body=body, parent=previous or "root", news=news,
                none_reason="operator directive with no new gaps; impacts are declared by the work nodes that follow",
            )
            if self.directive_slug is not None and news:
                self.reconcile_due = True   # fold the gaps onto the frontier at the first chance
            g = self.graph()
        if self.directive_slug is None:
            raise RuntimeError(f"could not record the run directive: {self.last_error}")
        records = g.records()
        self.last_record_slug = (records[-1].get("slug") if records else None) or self.directive_slug
        if not self.state_md.exists():
            self.render_state()

    def _ensure_ignored(self) -> None:
        """`.flywheel/` and `.governor/` hold a live database and locks: never let a commit sweep them up."""
        if governor_repo(self.repo) != self.repo:
            return   # a worktree: the repository lives at the main checkout, outside this tree
        missing = []
        for d in (".flywheel/", ".governor/"):
            proc = subprocess.run(["git", "check-ignore", "-q", d], cwd=str(self.repo), capture_output=True)
            if proc.returncode != 0:
                missing.append(d)
        if missing:
            gi = self.repo / ".gitignore"
            text = gi.read_text() if gi.exists() else ""
            gi.write_text(text + ("" if not text or text.endswith("\n") else "\n") + "".join(f"{d}\n" for d in missing))

    # -- the plan --------------------------------------------------------
    def _find_plan_root(self, g: Graph) -> str | None:
        """The plan component: a plan-kind child of the root, preferring the one titled like the view."""
        tops = [n for n in g.nodes.values() if n.get("kind") == "plan" and g.root in (n.get("parents") or [])]
        tops.sort(key=lambda n: (n.get("title", "").lower() != self.plan_view.lower(), n.get("seq", 0)))
        return tops[0].get("slug") if tops else None

    def ensure_plan_view(self) -> bool:
        """Create the plan component once, through a plan fold that folds no record. True when it exists."""
        if not self.plan:
            return False
        if self.plan_root_slug:
            return True
        code, out = self.fold_apply({
            "view": "plan",
            "claims": [{
                "new": self.plan_view, "parent": "root", "title": self.plan_view, "status": "open",
                "current": "- The planner's bets by horizon: `short`, `medium`, `long`. The planner is this "
                           "component's single writer; Ouroboros created it.",
                "cites": [],
            }],
            "folded": [],
        })
        if code != 0:
            self.last_error = out
            return False
        self.plan_root_slug = self._find_plan_root(self.graph())
        return self.plan_root_slug is not None

    def plan_nodes(self, g: Graph | None = None) -> list[dict]:
        """Every live plan claim but the component: {slug, title, status, current}."""
        g = g or self.graph()
        out = []
        for n in g.claims("plan"):
            if n.get("slug") == self.plan_root_slug or n.get("status") == "superseded":
                continue
            out.append({"slug": n.get("slug"), "title": n.get("title", ""), "status": n.get("status") or "?",
                        "current": _section(n.get("content", ""), "Current") or n.get("content", "")})
        order = {h: i for i, h in enumerate(HORIZONS)}
        out.sort(key=lambda n: (order.get(n["title"].lower(), 9), n["title"]))
        return out

    def plan_text(self, limit: int = PLAN_MD_LIMIT, g: Graph | None = None) -> str:
        nodes = self.plan_nodes(g)
        if not nodes:
            return self.plan_md.read_text().strip()[:limit] if self.plan_md.exists() else ""
        text = "\n\n".join(f"### {n['title']} (`{n['slug']}`, {n['status']})\n\n{n['current']}" for n in nodes)
        return text[:limit]

    def render_plan_md(self, g: Graph | None = None) -> None:
        """PLAN.md from the plan claims, so the commit after a planner pass carries a readable plan."""
        nodes = self.plan_nodes(g)
        if not nodes:
            return
        body = "\n\n".join(f"## {n['title']} (`{n['slug']}`, {n['status']})\n\n{n['current']}" for n in nodes)
        self.plan_md.write_text(
            "# Plan\n\nGenerated by Ouroboros from the governor graph's plan claims — do not edit by hand.\n\n" + body + "\n"
        )

    def plan_seed_news(self, goal_text: str) -> list[tuple[str, str]]:
        """NEW impacts that seed the plan horizons from the charter's horizon ladder, once."""
        if not self.ensure_plan_view() or self.plan_nodes():
            return []
        ladder = charter.horizon_ladder(goal_text)
        if not ladder:
            return []

        def rung(*names: str) -> str:
            return " ".join(ladder.get(n, "") for n in names).strip()[:400] or "(the charter gives no rung here)"

        seed = "Seeded from the charter's horizon ladder; the planner re-plans after each maintainer pass"
        p = self.plan_view
        return [
            (f"{p}-short", f"{seed}. Next units, one iteration each: {rung('short')}"),
            (f"{p}-medium", f"{seed}. Open gaps in order, several units each: {rung('medium')}"),
            (f"{p}-long", f"{seed}. Directions and standing work: {rung('long')}"),
        ]

    def _is_plan_impact(self, target: dict | None, label: str) -> bool:
        if target is not None:
            return target.get("kind") == "plan"
        return label.startswith(f"NEW {self.plan_view}-")

    def plan_pending(self, g: Graph | None = None) -> str:
        """Plan impacts not yet folded: declared by a record that no plan claim cites yet."""
        g = g or self.graph()
        cited = g.cited_by("plan")
        lines = []
        for r in g.records():
            if r["id"] in cited:
                continue
            mine = [(label, delta) for target, label, delta in g.impacts_of(r) if self._is_plan_impact(target, label)]
            if mine:
                lines.append(f"- `{r.get('slug')}` {r.get('title', '')}")
                lines += [f"  - → `{label}`: {delta}" for label, delta in mine]
        return "\n".join(lines) or "(none)"

    def planner_prompt(self, signals: str) -> str | None:
        if not self.plan or not self.plan_root_slug:
            return None
        g = self.graph()
        tpl = _strip_front_matter(resources.files("ouroboros.skills").joinpath("ouroboros-planner/SKILL.md").read_text())
        recent = g.records()[-8:]
        recent_text = "\n".join(f"- `{n.get('slug')}` — {n.get('title', '')}" for n in recent) or "(none)"
        parent = self.last_record_slug or self.directive_slug or "<causal-parent-slug>"
        p = self.plan_view
        fold = (
            f"1. Write ONE decision record, title starting `Bet:` (never more than one per pass). Body headings exactly "
            f"`## What`, `## Why`, `## Method`, `## Result` (governor writes `## Recorded` and `## State Impact`). "
            f"`## Why` carries the reasoning and cites the evidence. Record it:\n"
            f"   `governor record --title \"Bet: <summary>\" --body bet.md --parent {parent} --impact \"<plan-slug>: <delta>\"`, "
            f"one `--impact` per plan claim you change, `--new \"{p}-<horizon>: <delta>\"` for a horizon that does not exist yet. "
            f"Use `--none \"plan holds: <reason>\"` when nothing changes; then stop after step 3. It prints the bet's slug.\n"
            f"2. You are the single writer of the plan claims. Fold your own bet and every pending plan impact listed above "
            f"in ONE plan fold. Write `plan.json`:\n"
            f"   ```json\n"
            f"   {{\"view\": \"plan\", \"folded\": [],\n"
            f"    \"claims\": [\n"
            f"     {{\"slug\": \"<existing-plan-slug>\", \"status\": \"open\", \"current\": \"1. … [rec: <bet-slug>]\", "
            f"\"negative\": \"- …\", \"cites\": [\"<bet-slug>\"]}},\n"
            f"     {{\"new\": \"short\", \"parent\": \"{self.plan_root_slug}\", \"title\": \"short\", \"status\": \"open\", "
            f"\"current\": \"1. … [rec: <bet-slug>]\", \"cites\": [\"<bet-slug>\"]}}]}}\n"
            f"   ```\n"
            f"   `current` is the claim's WHOLE new text (ranked bullets, each ending `[rec: <slug>]`), `negative` what the "
            f"plan tried and dropped. New horizons go under the plan component `{self.plan_root_slug}`, titled `short`, "
            f"`medium` or `long`. `cites` lists every record whose plan impact you applied (the bet, and the records "
            f"under \"Pending plan impacts\"): a citation is what marks a plan impact folded. Keep `\"folded\": []` and "
            f"`\"view\": \"plan\"`: tagging records folded is the state fold's job, not yours. Then "
            f"`governor fold --apply plan.json` (it validates the whole plan first; exit 2 = nothing written, fix and retry).\n"
            f"   Never put a state claim in the plan fold, never run a fold without `\"view\": \"plan\"`, and never edit STATE.md.\n"
            f"3. `governor check` must exit 0. Delete your scratch files. There is nothing to `git add`: the bet and the plan "
            f"are committed to the Flywheel repository, and Ouroboros renders `{self.plan_md.name}` and commits it after your pass."
        )
        frontier = self.frontier_text(g)
        return (
            tpl.replace("{max_new}", str(self.max_new_directions))
            .replace("{charter}", (self.goal_text or "").strip()[:12000] or "(none)")
            .replace("{frontier}", frontier or "(empty: no open gaps — propose directions)")
            .replace("{plan}", self.plan_text(6000, g) or "(no plan claims yet: create short, medium, long from the pending impacts)")
            .replace("{pending}", self.plan_pending(g))
            .replace("{recent}", recent_text)
            .replace("{signals}", signals.strip() or "(none)")
            .replace("{fold}", fold)
        )

    def verify_bet(self, before: set[str]) -> str | None:
        g = self.graph()
        bet = None
        for n in g.records():
            if n["id"] not in before and (n.get("title") or "").startswith("Bet:"):
                bet = n
        if bet is None:
            return None
        self.last_record_slug = bet.get("slug")
        self.render_plan_md(g)
        return f"{bet.get('slug')} — {bet.get('title', '')}"

    # -- the frontier ----------------------------------------------------
    def frontier_text(self, g: Graph | None = None) -> str:
        g = g or self.graph()
        lines = []
        for status in FRONTIER_STATUSES:
            for n in g.claims("state"):
                if n.get("status") == status:
                    lines.append(f"- [{status}] **{n.get('title', '')}** (`{n.get('slug')}`) — "
                                 f"{_first_line(_section(n.get('content', ''), 'Current'))}")
        return "\n".join(lines)

    def frontier_fingerprint(self) -> str | None:
        """Every state claim's status, plus how many charter criteria are still open (see HypergraphMemory)."""
        g = self.graph()
        if not g.ok:
            return None
        pairs = sorted({(n.get("slug") or n["id"], n.get("status") or "?") for n in g.claims("state")})
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

    # -- the tail --------------------------------------------------------
    def unreconciled(self, g: Graph | None = None) -> tuple[int, str]:
        """The unfolded tail: (count, a listing of each record with its declared impacts)."""
        g = g or self.graph()
        if not g.ok:
            return 0, f"(governor snapshot failed: {self.last_error})"
        t = g.tail()
        lines = [f"{len(t)} unfolded record(s)"]
        for r in t:
            lines.append(f"- `{r.get('slug')}` {r.get('title', '')}")
            for target, label, delta in g.impacts_of(r):
                mark = " (plan: the planner's)" if self._is_plan_impact(target, label) else ""
                lines.append(f"  - → `{label}`{mark}: {delta}")
            reason = g.none_reason(r)
            if reason:
                lines.append(f"  - none: {reason}")
        return len(t), "\n".join(lines)

    def needs_reconcile(self) -> bool:
        if self.reconcile_due:
            return True
        if self.reconcile_every and self.since_reconcile >= self.reconcile_every:
            return True
        count, _ = self.unreconciled()
        return count >= self.pressure

    def reconcile_prompt(self) -> str | None:
        _, tail = self.unreconciled()
        p = self.plan_view
        procedure = (
            "Follow the `governor-fold` skill. If it is installed in this repo, read it "
            "(`.claude/skills/governor-fold/SKILL.md`). The short form:\n\n"
            "1. `governor fold` prints the tail: every unfolded record with its declared impacts "
            "(`→ <claim>: <delta>` and `→ NEW <name>: <delta>`). Read a record whole from "
            "`governor snapshot` (JSON; filter it with `jq` or a short script, never print it all).\n"
            "2. For each state claim the tail touches, write its new **whole** text once, with every record in mind: "
            "`current` (short bullets, each ending `[rec: <slug>]`; merge, drop what is superseded), `negative` "
            "(what was tried and does not work, and why), `status` (`working` true and evidenced, `open`, `broken`, "
            "`blocked`, `superseded`), and `cites` (the records it now rests on; list the new ones at least). "
            "A `NEW <name>` impact becomes a new claim: `new`, `title`, `parent` (its component's slug, or `root`). "
            "A criterion dropped by a newer directive: its gap claim becomes `superseded`.\n"
            "3. Write `fold.json` and apply it:\n"
            "   ```json\n"
            "   {\"claims\": [\n"
            "     {\"slug\": \"<claim-slug>\", \"status\": \"working\", \"current\": \"- … [rec: <rec>]\", "
            "\"negative\": \"- …\", \"cites\": [\"<rec>\"]},\n"
            "     {\"new\": \"<name>\", \"parent\": \"root\", \"title\": \"<Title>\", \"status\": \"open\", "
            "\"current\": \"- … [rec: <rec>]\", \"cites\": [\"<rec>\"]}],\n"
            "    \"folded\": [\"<every tail record slug>\"]}\n"
            "   ```\n"
            "   `governor fold --apply fold.json` validates the whole plan first (exit 2 = nothing written; fix and "
            "retry), then writes it in one commit. `folded` lists **every** tail record you applied, including "
            "the `none` ones and the `Bet:` records.\n"
            "4. `governor check` must exit 0. Fix a violation with another fold or a new record, never by hand.\n"
            "5. `governor render -o STATE.md`, then `git add STATE.md` and commit with a message starting `reconcile:`. "
            "The graph is already committed to the Flywheel repository (`.flywheel/` is gitignored): nothing else to add."
        )
        plan_rule = (
            f"- **Never touch the plan claims** (kind `plan`: impacts marked `(plan: the planner's)` below, and "
            f"`NEW {p}-…` lines). When a planner is on, its pass is their single writer and folds them itself. Leave "
            f"them out of your fold's `claims`, but still list their records in `folded`: the planner tracks its own "
            f"pending impacts by citation. Never write a fold with `\"view\": \"plan\"`, and never edit PLAN.md."
        )
        return maintainer_prompt(branch=self.branch, tail=tail, goal_text=self.goal_text,
                                 procedure=procedure, plan_rule=plan_rule)

    # -- adapter protocol ------------------------------------------------
    def orient_prompt(self) -> str:
        parts = [
            "## Governor\n\nThis repo keeps its memory as a governor graph (one Flywheel graph: append-only records, "
            "and state claims that cite them). `governor orient` below is what is true now: the frontier, the "
            "unfolded tail with each record's declared impacts, the architecture, and the most recent records. "
            "State lags the tail by design: apply the tail's impacts in your head before choosing work. Read a "
            "claim or record whole from `governor snapshot` (JSON; filter it, never print it all).\n\n"
            f"This iteration is **one dispatch with a budget of {self.budget_units} unit(s)**. Target: the goal, "
            "or a frontier claim (open / broken / blocked) that serves it. Say which in `## Why`.\n\n"
            "**Forbidden in a work iteration, no exceptions:** the governor-fold skill, `governor fold --apply`, "
            "`governor init`, `governor migrate-hypergraph`, any `flywheel repo:*` write, editing STATE.md or PLAN.md. "
            "You are a contributor: you record; a separate fold pass applies the tail. If the tail looks fat, say so "
            "in `## Result` and keep working. Even when the done criteria are met, the next unit is a rung of the "
            "horizon ladder, never a fold.",
        ]
        code, text = self.gov("orient")
        if code != 0:
            parts.append(f"## governor orient\n\n(failed: {text.strip()[-1000:]})")
        else:
            parts.append("## governor orient\n\n" + self._fit_orient(text.strip()))
        g = self.graph()
        plan = self.plan_text(g=g)
        if plan:
            parts.append("## Plan (agent-owned bets: short / medium / long)\n\nPick this iteration's unit from `short` unless the "
                         "critic's message or a broken frontier claim says otherwise.\n\n" + plan)
        count, _ = self.unreconciled(g)
        parts.append(f"Unfolded tail: {count} record(s) (listed with their impacts above).")
        return "\n\n".join(parts)

    @staticmethod
    def _fit_orient(text: str, limit: int = STATE_MD_LIMIT) -> str:
        """Keep the frontier, the tail and the recent records whole; trim the architecture first."""
        if len(text) <= limit:
            return text
        head, sep, rest = text.partition("\n## Architecture")
        if not sep:
            return text[:limit] + "\n\n(… truncated; run `governor orient` for the rest)"
        arch, sep2, after = rest.partition("\n## The tail's declared impacts")
        if not sep2:
            arch, sep2, after = rest.partition("\n## Most recent records")
        room = max(limit - len(head) - len(after) - 200, 1000)
        if len(arch) > room:
            arch = arch[:room] + "\n\n(… architecture truncated; run `governor render` for the rest)"
        out = head + sep + arch + sep2 + after
        return out if len(out) <= limit * 2 else out[: limit * 2] + "\n\n(… truncated; run `governor orient`)"

    def record_prompt(self) -> str:
        parent = self.last_record_slug or self.directive_slug or "<causal-parent-slug>"
        return (
            "When the unit is finished, and before you stop, you MUST record it as ONE governor record "
            "(the governor-record skill; never a fold, never a claim):\n"
            "1. Write a body file with exactly these headings: `## What`, `## Why`, `## Method`, `## Result` "
            "(never `## Recorded` or `## State Impact`: governor writes both). A dead end is recorded the same way "
            "as a success. `## Why` says why this unit, and, if you did not do what the critic's message asked, what "
            "you did instead and why. `## Result` carries what is true now, then any concern, assumption, or new "
            "dependency the next iteration must know about, and ends with the line `Dispatch closed: 1 unit — <summary>`.\n"
            "2. Record it (the causal parent is the previous record of this run unless the work truly follows from "
            "another record — then use that slug):\n"
            f"   `governor record --title \"<title>\" --body body.md --parent {parent} --impact \"<claim-slug>: <delta>\"`\n"
            "   Use `--new \"<kebab-name>: <delta>\"` for a claim that does not exist yet, or `--none \"<reason>\"` when "
            "state truly does not change; several `--impact`/`--new` flags are fine. Real claim slugs are in the "
            "frontier and architecture above. The delta says what changed, not what you did. Exit 2 = nothing written; "
            "the message says why: fix and retry.\n"
            "3. There is nothing to `git add`: the record is committed to the Flywheel repository (it prints "
            "`<slug>  (record)  committed …`). Delete the body file.\n"
            "Never hand-edit STATE.md. Never run `governor fold --apply`. An iteration with no new record is counted "
            "as lost work."
        )

    def snapshot(self) -> set[str]:
        return set(self.record_ids())

    def verify_recorded(self, before: set[str]) -> bool:
        g = self.graph()
        new = [n for n in g.records() if n["id"] not in before]
        if not g.ok or not new:
            return False
        self.last_record_slug = new[-1].get("slug")
        return True

    def critic_context(self) -> str:
        g = self.graph()
        if not g.ok:
            return f"### Frontier\n\n(governor snapshot failed: {self.last_error})"
        parts = ["### Frontier (open, broken, blocked state claims)\n\n" + (self.frontier_text(g) or "(empty: no open gaps)")]
        count, _ = self.unreconciled(g)
        parts.append(f"Unfolded records in the tail: {count} (their declared impacts are not on the frontier yet).")
        plan = self.plan_text(g=g)
        if plan:
            parts.append("### Plan (agent-owned bets: short / medium / long)\n\n" + plan)
        return "\n\n".join(parts)

    def check_report(self) -> str | None:
        code, out = self.check()
        if code == 0:
            return None
        tail = "\n".join(l for l in out.splitlines() if not l.startswith("warning")).strip()[-2000:]
        return f"`governor check` exit {code}:\n{tail}"

    def last_summary(self) -> str:
        records = self.graph().records()
        if not records:
            return "no record"
        return (records[-1].get("title") or records[-1].get("slug") or "record")[:72]
