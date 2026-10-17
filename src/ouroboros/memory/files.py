"""The files store: governor's record/state model as markdown files in git.

    .ouroboros/graph/
      records/r-0007.md     one per record, append-only
      claims/<name>.md      one per state claim (or component)
      folds/f-0003.json     one per fold: which records it applied, which claims it wrote
      STATE.md              rendered by every fold; never edited by hand

Every node file is YAML frontmatter, in a fixed key order, then the markdown body
governor would hold in the node's `content`: a record's `## What / ## Why /
## Method / ## Result`, then the generated `## Recorded` and `## State Impact`; a
claim's `## Current / ## Negative knowledge / ## Evidence`. So the bodies need no
translation to go into a Flywheel graph later, and `node_id` (a uuid minted here)
lets that import run twice without making two of anything.

Where governor keeps a rule with Flywheel's commit log, this store keeps it with a
`digest` of the body in the frontmatter: a record whose body no longer matches was
edited after it was written (G2), and a claim whose body no longer matches was
written outside a fold (G7). `ouroboros mem check` reports both.

The model is borrowed from flywheel-local (one file per node, canonical
frontmatter, a doctor) without its snapshot store: git already versions these
files, and the run branch already carries them next to the code they describe.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path

import yaml

from .graph import KEBAB, STATUSES, Graph, GraphMemory, orient_text, render_state

RECORD_KEYS = ("id", "kind", "title", "parents", "at", "node_id", "digest")
CLAIM_KEYS = ("id", "kind", "title", "status", "parents", "seq", "node_id", "digest")
_RECORD_FILE = re.compile(r"^r-(\d{4,})\.md$")
_REC_ID = r"r-\d{4,}"
_FOLD_FILE = re.compile(r"^f-(\d{4,})\.json$")
_IMPACT = re.compile(r"^- (?!NEW )(\S+) — (.*)$", re.MULTILINE)
TAIL_WARNING = 3

README = """# Ouroboros memory (files)

Written by `ouroboros mem`. Do not edit these files by hand: `ouroboros mem check`
notices (each file carries a digest of its body).

- `records/r-NNNN.md`: what happened, one per iteration, append-only.
- `claims/<name>.md`: what is true now; only a fold writes them.
- `folds/f-NNNN.json`: which records each fold applied.
- `STATE.md`: the rendered state.

This is the governor model (records, claims, folds) on plain files, so that the
records can later seed a Flywheel graph.
"""


def _digest(body: str) -> str:
    return hashlib.sha256(body.encode()).hexdigest()[:16]


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _dump(meta: dict, keys: tuple[str, ...], body: str) -> str:
    ordered = {k: meta[k] for k in keys if meta.get(k) is not None}
    fm = yaml.safe_dump(ordered, sort_keys=False, default_flow_style=None, allow_unicode=True, width=1_000_000)
    return f"---\n{fm}---\n{body}"


def _parse(text: str) -> tuple[dict, str]:
    if not text.startswith("---\n"):
        raise ValueError("no `---` frontmatter fence")
    end = text.find("\n---\n", 3)
    if end < 0:
        raise ValueError("frontmatter has no closing `---` fence")
    meta = yaml.safe_load(text[4:end + 1])
    if not isinstance(meta, dict):
        raise ValueError("frontmatter is not a mapping")
    return meta, text[end + 5:]


def claim_content(current: str, negative: str, evidence: list[tuple[str, str]]) -> str:
    """governor's claimContent: the two prose sections and the evidence list."""
    ev = [f"- {slug}{f' — {why}' if why else ''}" for slug, why in evidence]
    return "\n".join([
        "## Current", "", current.strip() or "_(nothing yet)_", "",
        "## Negative knowledge", "", (negative or "").strip() or "_(none recorded)_", "",
        "## Evidence", "", *(ev or ["_(none yet)_"]), "",
    ])


def citations(content: str) -> list[tuple[str, str]]:
    """A claim's `## Evidence` lines, then its `[rec: id]` mentions; first reason wins."""
    out: dict[str, str] = {}
    ev = content.split("\n## Evidence", 1)
    for raw in (ev[1] if len(ev) > 1 else "").splitlines():
        m = re.match(rf"^-\s*`?({_REC_ID})`?\s*(?:—\s*(.*))?$", raw.strip())
        if m and m.group(1) not in out:
            out[m.group(1)] = (m.group(2) or "").strip()
    for m in re.finditer(rf"\[rec:\s*({_REC_ID})\s*\]", content):
        out.setdefault(m.group(1), "")
    return list(out.items())


class FilesStore:
    name = "files"

    def __init__(self, repo: Path, *, dir: str = ".ouroboros/graph") -> None:
        self.repo = repo
        self.root = repo / dir
        self.records_dir = self.root / "records"
        self.claims_dir = self.root / "claims"
        self.folds_dir = self.root / "folds"
        self.state_md = self.root / "STATE.md"
        self.where = (f"markdown files under `{dir}/` (records, claims, folds), committed with the code. "
                      "Never edit them by hand: `ouroboros mem` writes them, and `ouroboros mem check` notices")
        self.last_error: str | None = None
        self.problems: list[tuple[str, str, str]] = []   # (rule, node, message) found while reading

    # -- files -----------------------------------------------------------
    def prepare(self) -> None:
        for d in (self.records_dir, self.claims_dir, self.folds_dir):
            d.mkdir(parents=True, exist_ok=True)
        readme = self.root / "README.md"
        if not readme.exists():
            readme.write_text(README)

    def _rel(self, p: Path) -> str:
        try:
            return str(p.relative_to(self.repo))
        except ValueError:
            return str(p)

    def _folds(self) -> list[tuple[int, dict]]:
        out = []
        for p in sorted(self.folds_dir.glob("f-*.json")) if self.folds_dir.exists() else []:
            m = _FOLD_FILE.match(p.name)
            if not m:
                continue
            try:
                out.append((int(m.group(1)), json.loads(p.read_text())))
            except (OSError, json.JSONDecodeError) as exc:
                self.problems.append(("G1", p.name, f"unreadable fold log: {exc}"))
        return sorted(out)

    # -- the snapshot ------------------------------------------------------
    def snapshot(self) -> Graph:
        self.problems = []
        nodes: dict[str, dict] = {"root": {"id": "root", "slug": "root", "kind": "root", "title": self._title(),
                                           "parents": [], "children": [], "content": "", "seq": 0}}
        digests: list[str] = []
        for path in sorted(self.records_dir.glob("*.md")) if self.records_dir.exists() else []:
            m = _RECORD_FILE.match(path.name)
            node = self._read(path, "record", path.stem if m else None)
            if node is None:
                continue
            node["seq"] = int(m.group(1)) if m else 0
            nodes[node["id"]] = node
            digests.append(node["digest"] or "")
        for path in sorted(self.claims_dir.glob("*.md")) if self.claims_dir.exists() else []:
            node = self._read(path, "state", path.stem)
            if node is None:
                continue
            node["seq"] = 1_000_000 + int(node.get("seq") or 0)
            nodes[node["id"]] = node
            digests.append(node["digest"] or "")
        folded: set[str] = set()
        for n, log in self._folds():
            for r in log.get("folded") or []:
                folded.add(r)
            digests.append(f"f{n}")
        for nid, node in nodes.items():
            if node["kind"] == "record":
                node["folded"] = nid in folded
            for p in node.get("parents") or []:
                if p in nodes:
                    nodes[p]["children"].append(nid)
        edges: list[dict] = []
        for node in nodes.values():
            content = node.get("content") or ""
            if node["kind"] == "record":
                sec = content.split("\n## State Impact", 1)
                for target, delta in _IMPACT.findall(sec[1] if len(sec) > 1 else ""):
                    if target in nodes and nodes[target]["kind"] == "state":
                        edges.append({"source": node["id"], "target": target, "content": f"impact: {delta.strip()}"})
            elif node["kind"] == "state":
                for rec, why in citations(content):
                    if rec in nodes and nodes[rec]["kind"] == "record":
                        edges.append({"source": node["id"], "target": rec, "content": f"cites: {why}" if why else "cites"})
        head = hashlib.sha256("|".join(digests).encode()).hexdigest()[:12]
        return Graph({"root": "root", "head": head, "nodes": nodes, "edges": edges})

    def _title(self) -> str:
        return f"{self.repo.name} — Ouroboros memory"

    def _read(self, path: Path, kind: str, expect_id: str | None) -> dict | None:
        try:
            meta, body = _parse(path.read_text())
        except (OSError, ValueError, yaml.YAMLError) as exc:
            self.problems.append(("G1", self._rel(path), str(exc)))
            return None
        nid = str(meta.get("id") or "")
        if expect_id is None or nid != expect_id:
            self.problems.append(("G1", self._rel(path), f"id `{nid}` does not match the file name"))
            return None
        if meta.get("kind") != kind:
            self.problems.append(("G1", nid, f"kind `{meta.get('kind')}` in {self._rel(path)}; expected `{kind}`"))
            return None
        return {
            "id": nid, "slug": nid, "kind": kind, "title": str(meta.get("title") or ""),
            "status": meta.get("status"), "parents": [str(p) for p in meta.get("parents") or []], "children": [],
            "content": body, "at": meta.get("at"), "node_id": meta.get("node_id"), "digest": meta.get("digest"),
            "seq": meta.get("seq"), "live": True, "path": path,
        }

    # -- writes ------------------------------------------------------------
    def _git(self, *args: str) -> str:
        try:
            out = subprocess.run(["git", *args], cwd=str(self.repo), capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            return ""
        return out.stdout.strip() if out.returncode == 0 else ""

    def record(self, *, title: str, body: str, parent: str | None, impacts: list[tuple[str, str]],
               news: list[tuple[str, str]], none: str | None) -> tuple[str | None, str]:
        g = self.snapshot()
        problems = []
        title = " ".join((title or "").split())
        if not title:
            problems.append("a record needs a title")
        if not (body or "").strip():
            problems.append("the body is empty: write `## What`, `## Why`, `## Method`, `## Result`")
        if re.search(r"^## (Recorded|State Impact)\s*$", body or "", re.MULTILINE):
            problems.append("the body has a `## Recorded` or `## State Impact` section; those are generated")
        if none and (impacts or news):
            problems.append("give impacts or `none`, not both")
        if not none and not impacts and not news:
            problems.append("declare the impact: `--impact \"<claim>: <delta>\"`, `--new \"<name>: <delta>\"`, or `--none \"<why>\"`")
        for slug, _ in impacts:
            n = g.nodes.get(slug)
            if n is None:
                problems.append(f"impact target `{slug}` is not a claim (for a claim that does not exist yet, use --new)")
            elif n["kind"] != "state":
                problems.append(f"impact target `{slug}` is a {n['kind']} node, not a claim")
        for name, _ in news:
            if not KEBAB.match(name):
                problems.append(f"new claim name `{name}` is not kebab-case")
            elif name in g.nodes:
                problems.append(f"`{name}` already exists; declare `--impact \"{name}: …\"` instead")
        parent = parent or "root"
        if parent != "root" and (g.nodes.get(parent) or {}).get("kind") != "record":
            problems.append(f"parent `{parent}` is not a record (give a record id, or `root`)")
        if problems:
            return None, "\n".join(f"- {p}" for p in problems)
        numbers = [n.get("seq", 0) for n in g.records()]
        nid = f"r-{(max(numbers) + 1) if numbers else 1:04d}"
        at = _now()
        lines = [body.rstrip(), "", "## Recorded", "", f"- at: {at}"]
        branch, commit = self._git("rev-parse", "--abbrev-ref", "HEAD"), self._git("rev-parse", "--short=12", "HEAD")
        if branch:
            lines.append(f"- branch: {branch}")
        if commit:
            lines.append(f"- commit: {commit}")
        lines += ["", "## State Impact", ""]
        if none:
            lines.append(f"none: {' '.join(none.split())}")
        lines += [f"- {slug} — {' '.join(d.split())}" for slug, d in impacts]
        lines += [f"- NEW {name} — {' '.join(d.split())}" for name, d in news]
        content = "\n".join(lines) + "\n"
        meta = {"id": nid, "kind": "record", "title": title, "parents": [parent], "at": at,
                "node_id": str(uuid.uuid4()), "digest": _digest(content)}
        self.prepare()
        (self.records_dir / f"{nid}.md").write_text(_dump(meta, RECORD_KEYS, content))
        return nid, f"{nid}  (record)  written to {self._rel(self.records_dir / f'{nid}.md')}"

    def validate_fold(self, g: Graph, plan: dict) -> list[str]:
        """governor's validateFold, for this store."""
        problems: list[str] = []
        if (plan.get("view") or "state") != "state":
            problems.append("a fold writes state; there is no other view")
        claims = plan.get("claims")
        folded = plan.get("folded")
        if not isinstance(claims, list) or not isinstance(folded, list):
            return problems + ["a fold is {\"claims\": [...], \"folded\": [...]}"]
        tail = {n["id"] for n in g.tail()}
        for r in folded:
            n = g.nodes.get(r)
            if n is None:
                problems.append(f"folded record `{r}` is not in the graph")
            elif n["kind"] != "record":
                problems.append(f"`{r}` is not a record")
            elif r not in tail:
                problems.append(f"`{r}` is already folded")
        new_names = {c.get("new") for c in claims if isinstance(c, dict) and c.get("new")}
        for c in claims:
            if not isinstance(c, dict):
                problems.append(f"claim {c!r} is not an object")
                continue
            label = c.get("slug") or c.get("new") or "?"
            if bool(c.get("slug")) == bool(c.get("new")):
                problems.append(f"claim {label}: give exactly one of slug (existing) or new (name)")
            existing = g.nodes.get(c["slug"]) if c.get("slug") else None
            if c.get("slug"):
                if existing is None:
                    problems.append(f"claim `{c['slug']}` is not in the graph")
                elif existing["kind"] != "state":
                    problems.append(f"`{c['slug']}` is a {existing['kind']} node, not a claim")
            if c.get("new"):
                if not KEBAB.match(c["new"]):
                    problems.append(f"new claim `{c['new']}`: the name is not kebab-case")
                elif c["new"] in g.nodes:
                    problems.append(f"new claim `{c['new']}` already exists; give it as `slug`")
                if not c.get("title"):
                    problems.append(f"new claim {c['new']} needs a title")
                parent = c.get("parent") or "root"
                p = g.nodes.get(parent)
                if p is None and parent not in new_names:
                    problems.append(f"new claim {c['new']}: parent `{parent}` is not in the graph")
                elif p is not None and p["kind"] not in ("root", "state"):
                    problems.append(f"new claim {c['new']}: parent `{parent}` is a {p['kind']} node")
            cites = c.get("cites") or []
            status = c.get("status")
            if c.get("component"):
                if status:
                    problems.append(f"claim {label}: a component carries no status")
                if c.get("new") and (c.get("parent") or "root") != "root":
                    problems.append(f"component {label}: its parent is the root")
                if existing and (existing.get("status") or "root" not in existing.get("parents", [])):
                    problems.append(f"`{c['slug']}` is not a component")
            else:
                if not status and existing is not None and existing.get("status"):
                    problems.append(f"claim {label}: give its status")
                elif not status and existing is None:
                    problems.append(f"claim {label}: give its status, or mark it `component: true`")
                elif status and status not in STATUSES:
                    problems.append(f"claim {label}: status `{status}` is not one of {', '.join(STATUSES)}")
                if status and not cites and status != "open" and not (existing and existing.get("status") == status):
                    problems.append(f"claim {label}: a {status} claim cites at least one record (G4)")
            if not isinstance(c.get("current", ""), str):
                problems.append(f"claim {label}: `current` is text")
            for r in cites:
                if (g.nodes.get(r) or {}).get("kind") != "record":
                    problems.append(f"claim {label}: cited `{r}` is not a record")
        return problems

    def fold(self, plan: dict) -> tuple[bool, str]:
        g = self.snapshot()
        problems = self.validate_fold(g, plan)
        if problems:
            return False, "\n".join(f"- {p}" for p in problems)
        self.prepare()
        seq = max((int(n.get("seq") or 0) - 1_000_000 for n in g.nodes.values() if n["kind"] == "state"), default=0)
        written, created = [], []
        for c in plan["claims"]:
            node = g.nodes.get(c["slug"]) if c.get("slug") else None
            added = [(r, "") for r in c.get("cites") or []]
            evidence = dict(citations(node["content"]) if node else [])
            for r, why in added:
                evidence.setdefault(r, why)
            content = claim_content(c.get("current", ""), c.get("negative", ""), list(evidence.items()))
            if node is None:
                seq += 1
                nid = c["new"]
                meta = {"id": nid, "kind": "state", "title": " ".join(c["title"].split()),
                        "status": None if c.get("component") else c.get("status"), "parents": [c.get("parent") or "root"],
                        "seq": seq, "node_id": str(uuid.uuid4())}
                created.append(nid)
            else:
                nid = node["id"]
                meta = {"id": nid, "kind": "state", "title": " ".join((c.get("title") or node["title"]).split()),
                        "status": None if c.get("component") else (c.get("status") or node.get("status")),
                        "parents": node["parents"], "seq": int(node.get("seq") or 0) - 1_000_000,
                        "node_id": node.get("node_id") or str(uuid.uuid4())}
            meta["digest"] = _digest(content)
            (self.claims_dir / f"{nid}.md").write_text(_dump(meta, CLAIM_KEYS, content))
            written.append(nid)
        numbers = [n for n, _ in self._folds()]
        number = (max(numbers) + 1) if numbers else 1
        log = {"at": _now(), "folded": list(plan["folded"]), "claims": written, "created": created}
        (self.folds_dir / f"f-{number:04d}.json").write_text(json.dumps(log, indent=2) + "\n")
        self.render()
        made = f"; new: {', '.join(created)}" if created else ""
        return True, f"folded {len(plan['folded'])} record(s) into {len(written)} claim(s){made}; wrote {self._rel(self.state_md)}"

    # -- reads -------------------------------------------------------------
    def check(self) -> tuple[bool, str]:
        g = self.snapshot()
        findings: list[tuple[str, str, str, str]] = [("violation", *p) for p in self.problems]

        def bad(rule: str, node: str, message: str) -> None:
            findings.append(("violation", rule, node, message))

        claims = {k: n for k, n in g.nodes.items() if n["kind"] == "state"}
        for n in g.records():
            if n.get("digest") != _digest(n["content"]):
                bad("G2", n["id"], "edited after it was written; a correction is a new child record")
            sec = n["content"].split("\n## State Impact", 1)
            impact = sec[1] if len(sec) > 1 else ""
            if not re.search(r"^(- \S.* — \S|none:\s*\S)", impact, re.MULTILINE):
                bad("G3", n["id"], "declares no impact (no impact line, no NEW line, no `none:`)")
            for target, _ in _IMPACT.findall(impact):
                if target not in claims:
                    bad("G5", n["id"], f"impact names `{target}`, which is not a claim")
            for p in n["parents"]:
                if p != "root" and (g.nodes.get(p) or {}).get("kind") != "record":
                    bad("G6", n["id"], f"parent `{p}` is not a record or the root")
        for n in claims.values():
            if n.get("digest") != _digest(n["content"]):
                bad("G7", n["id"], "written outside a fold; only `ouroboros mem fold --apply` writes claims")
            if not g.is_component(n):
                if n.get("status") not in STATUSES:
                    bad("G4", n["id"], f"status `{n.get('status')}` is not one of {', '.join(STATUSES)}")
                if not g.cites(n):
                    bad("G4", n["id"], "cites no record")
            for rec, _ in citations(n["content"]):
                if rec not in g.nodes:
                    bad("G5", n["id"], f"cites `{rec}`, which is not a record")
            for p in n["parents"]:
                if p != "root" and p not in claims:
                    bad("G6", n["id"], f"parent `{p}` is not a claim or the root")
        for nid in g.nodes:   # lineage cycles
            seen, todo = set(), list(g.nodes[nid].get("parents") or [])
            while todo:
                p = todo.pop()
                if p == nid:
                    bad("G6", nid, "is its own ancestor")
                    break
                if p in seen or p not in g.nodes:
                    continue
                seen.add(p)
                todo += g.nodes[p].get("parents") or []
        tail = len(g.tail())
        if tail > TAIL_WARNING:
            findings.append(("warning", "tail", "", f"{tail} unfolded records (fold threshold {TAIL_WARNING})"))
        violations = [f for f in findings if f[0] == "violation"]
        lines = [f"{'VIOLATION' if lvl == 'violation' else 'warning'} {rule} {node + ': ' if node else ''}{msg}"
                 for lvl, rule, node, msg in findings]
        summary = f"{len(violations)} violation(s), {len(findings) - len(violations)} warning(s)"
        return not violations, "\n".join(lines + [summary])

    def orient(self) -> str:
        return orient_text(self.snapshot(), title=self._title(), generator="ouroboros mem render")

    def render(self) -> bool:
        self.prepare()
        self.state_md.write_text(render_state(self.snapshot(), title=self._title(), generator="ouroboros mem render"))
        return True


class FilesMemory(GraphMemory):
    """The loop's memory on the files store."""

    def __init__(self, repo: Path, *, recent: int = 3, reconcile_every: int = 5, pressure: int = 3,
                 budget_units: int = 1) -> None:
        super().__init__(FilesStore(repo), reconcile_every=reconcile_every, pressure=pressure,
                         budget_units=budget_units, recent=recent)
        self.repo = repo

    def _legacy_handoff(self) -> str:
        """While this memory is young, the last file of the handoff memory it replaced."""
        old = self.repo / ".ouroboros" / "handoff"
        if not old.is_dir() or len(self.graph().records()) > 3:
            return ""
        files = sorted(p for p in old.glob("[0-9][0-9][0-9][0-9].md"))
        if not files:
            return ""
        return (f"\n\n## The last handoff from before this memory (`{self._rel(files[-1])}`, read-only)\n\n"
                f"{files[-1].read_text().strip()[:3000]}")

    def _rel(self, p: Path) -> str:
        try:
            return str(p.relative_to(self.repo))
        except ValueError:
            return str(p)
