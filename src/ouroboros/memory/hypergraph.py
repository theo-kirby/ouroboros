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

_FM_TITLE = re.compile(r"^title:\s*(.+)$", re.MULTILINE)
_FM_ROOT = re.compile(r"^parents:\s*\[\s*\]\s*$", re.MULTILINE)
_UNREC = re.compile(r"(\d+) unreconciled record node")
STATE_MD_LIMIT = 16000


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

    def new_record(self, *, title: str, body: str, parent: str | None, none_reason: str) -> str | None:
        args = ["new", "record", *self._cfg(), "--title", title, "--body", "-", "--none", none_reason, "--repo-auto"]
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
    def start(self, *, run: str, goal_text: str, run_dir: Path, branch: str) -> None:
        self.branch, self.goal_text, self.run_dir = branch, goal_text, run_dir
        digest = hashlib.sha256(goal_text.encode()).hexdigest()[:8]
        title = f"Ouroboros run: {run}"
        versioned = f"{title} [{digest}]"
        self.directive_slug = self.find_record_by_title(versioned)
        if self.directive_slug is None:
            previous = self.find_record_by_title(title)  # an earlier goal version of this run, if any
            supersedes = (
                f"\n\nThis directive supersedes `{previous}`: the operator edited the goal and restarted the run."
                if previous else ""
            )
            body = (
                "## What\n\nOperator directive: an Ouroboros loop starts on this repo. Every work node of the run "
                f"descends from this node.{supersedes}\n\n## Why\n\nThe goal document, verbatim:\n\n{goal_text.strip()}\n\n"
                f"## Method\n\nOuroboros iterations on branch `{branch}`: orient, one dispatched unit, record, commit; "
                "a maintainer pass reconciles on pressure.\n\n## Result\n\nDirective recorded. Work follows as child nodes.\n"
            )
            self.directive_slug = self.new_record(
                title=f"{versioned} — operator directive", body=body, parent=previous or self.find_record_root(),
                none_reason="operator directive; impacts are declared by the work nodes that follow",
            )
        if self.directive_slug is None:
            raise RuntimeError(f"could not record the run directive: {self.last_error}")
        files = self.record_files()
        newest = files[-1].stem if files else None
        self.last_record_slug = newest or self.directive_slug

    def mark_iteration(self) -> None:
        self.since_reconcile += 1

    def mark_reconciled(self) -> None:
        self.since_reconcile = 0

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
            "editing STATE.md. You are a contributor: you record; a separate maintainer pass reconciles. If the "
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
            "A dead end is recorded the same way as a success. In `## Result`, end with the line "
            "`Dispatch closed: 1 unit — <summary>`.\n"
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
