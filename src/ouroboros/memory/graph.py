"""The record/state memory both stores share: governor's model, and the loop logic on it.

A memory here is governor's discipline (docs/CONVENTIONS.md in governor): an
append-only log of **records**, each declaring what it changes in **state**, and a
small set of state **claims** that cite the records they rest on. One writer, the
fold, turns the unfolded tail into state.

Two stores hold that model:

- `GovernorStore` (governor.py): a Flywheel graph, through the `governor` CLI.
- `FilesStore` (files.py): markdown files with frontmatter under `.ouroboros/graph/`,
  in git, shaped so that a later import can load them into a Flywheel graph.

The agent never talks to a store. It runs `ouroboros mem <verb>`, and the verb does
whatever the store needs. So the prompts here are written once, for both stores,
and a difference between the stores shows up as a failing test, not as a prompt
the agent has to pick between.
"""

from __future__ import annotations

import hashlib
import re
from importlib import resources
from pathlib import Path
from typing import Protocol

from .base import BaseMemory
from .. import goal as charter

FRONTIER_STATUSES = ("broken", "blocked", "open")
STATUSES = ("open", "working", "broken", "blocked", "superseded")
STATE_MD_LIMIT = 16000

_GAP_LINE = re.compile(r"^- \[gap\] (\S+): (.*)$", re.MULTILINE)
_NEW_LINE = re.compile(r"^- NEW ([a-z0-9-]+) — (.*)$", re.MULTILINE)
KEBAB = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


# -- small text helpers ------------------------------------------------------

def section(content: str, heading: str) -> str:
    """The text under `## heading`, up to the next `## `."""
    for part in re.split(r"^## ", content or "", flags=re.MULTILINE):
        if part.startswith(heading + "\n") or part.strip() == heading:
            return part[len(heading):].strip()
    return ""


def first_line(text: str, limit: int = 200) -> str:
    for line in (text or "").splitlines():
        line = re.sub(r"^[-*]\s*", "", line).strip()
        if line and not line.startswith("_("):
            return line if len(line) <= limit else line[: limit - 1] + "…"
    return ""


def one_line(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def strip_front_matter(text: str) -> str:
    if text.startswith("---"):
        _, _, rest = text.partition("\n---\n")
        return rest.lstrip("\n")
    return text


def skill(name: str) -> str:
    return strip_front_matter(resources.files("ouroboros.skills").joinpath(f"{name}/SKILL.md").read_text())


# -- the snapshot ------------------------------------------------------------

class Graph:
    """One snapshot of a memory, in `governor snapshot`'s shape.

    `nodes` maps an id to {id, slug, kind, title, status, parents, children,
    content, folded, seq, live, at}; `edges` are {source, target, content}: one
    `impact: <delta>` edge per (record, claim) impact and one `cites` edge per
    (claim, record) citation. Both stores produce exactly this, so everything
    below reads one shape.
    """

    def __init__(self, data: dict | None) -> None:
        data = data or {}
        self.ok = bool(data)
        self.root: str | None = data.get("root")
        self.head: str | None = data.get("head")
        self.nodes: dict[str, dict] = {k: v for k, v in (data.get("nodes") or {}).items() if v.get("live", True)}
        self.edges: list[dict] = list(data.get("edges") or [])

    def label(self, node_id: str) -> str:
        n = self.nodes.get(node_id) or {}
        return n.get("slug") or node_id[:8]

    def records(self) -> list[dict]:
        return sorted((n for n in self.nodes.values() if n.get("kind") == "record"), key=lambda n: n.get("seq", 0))

    def tail(self) -> list[dict]:
        return [n for n in self.records() if not n.get("folded")]

    def is_component(self, n: dict) -> bool:
        return not n.get("status") and self.root in (n.get("parents") or [])

    def claims(self, kind: str = "state") -> list[dict]:
        """Every claim of `kind`, components (status-less children of the root) excluded."""
        out = [n for n in self.nodes.values() if n.get("kind") == kind and not self.is_component(n)]
        return sorted(out, key=lambda n: n.get("seq", 0))

    def by_slug(self, slug: str) -> dict | None:
        if slug in self.nodes:
            return self.nodes[slug]
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

    def cites(self, claim: dict) -> list[dict]:
        """The records a claim cites, newest first."""
        out = [self.nodes[e["target"]] for e in self.edges
               if e.get("source") == claim["id"] and (self.nodes.get(e.get("target", "")) or {}).get("kind") == "record"]
        return sorted(out, key=lambda n: -n.get("seq", 0))


# -- rendering (governor's lib/render.ts, in Python) ---------------------------

def render_state(g: Graph, *, title: str, generator: str) -> str:
    """STATE.md: the frontier, the unfolded tail, the architecture."""
    claims = [n for n in g.nodes.values() if n.get("kind") == "state"]
    lines = [f"# {title}", "", f"Generated by `{generator}` — do not edit by hand.", f"Head: `{g.head or 'none'}`.",
             "", "## Frontier", ""]
    frontier = [n for n in claims if n.get("status") in FRONTIER_STATUSES]
    if not frontier:
        lines.append("_Nothing open, broken or blocked._")
    for st in FRONTIER_STATUSES:
        for n in sorted((n for n in frontier if n.get("status") == st), key=lambda n: n.get("seq", 0)):
            lines.append(f"- [{st}] **{n.get('title', '')}** (`{g.label(n['id'])}`) — {first_line(section(n.get('content', ''), 'Current'))}")
    t = g.tail()
    lines += ["", f"## Unfolded records ({len(t)})", ""]
    if not t:
        lines.append("_None: state is current._")
    for r in t[-20:]:
        lines.append(f"- `{g.label(r['id'])}` {r.get('title', '')}")
    if len(t) > 20:
        lines.append(f"- … and {len(t) - 20} more")
    lines += ["", "## Architecture", ""]

    def walk(node_id: str, depth: int) -> None:
        kids = [g.nodes[c] for c in (g.nodes.get(node_id) or {}).get("children", []) if c in g.nodes]
        for n in sorted((k for k in kids if k.get("kind") == "state"), key=lambda n: n.get("title", "")):
            if n.get("status") == "superseded":
                continue
            c = g.cites(n)
            status = f"[{n['status']}] " if n.get("status") else ""
            lines.append(f"{'  ' * depth}- {status}**{n.get('title', '')}** (`{g.label(n['id'])}`) — "
                         f"{first_line(section(n.get('content', ''), 'Current'), 160)}"
                         f"{f' [{len(c)} record(s)]' if c else ''}")
            walk(n["id"], depth + 1)

    if g.root:
        walk(g.root, 0)
    superseded = [n for n in claims if n.get("status") == "superseded"]
    if superseded:
        lines += ["", f"_{len(superseded)} superseded claim(s) omitted._"]
    return "\n".join(lines) + "\n"


def tail_listing(g: Graph) -> tuple[int, str]:
    """The unfolded tail: (count, every record with its declared impacts)."""
    t = g.tail()
    lines = [f"{len(t)} unfolded record(s)"]
    for r in t:
        lines.append(f"- `{g.label(r['id'])}` {r.get('title', '')}")
        for target, label, delta in g.impacts_of(r):
            mark = " (plan: legacy, leave it)" if target is not None and target.get("kind") == "plan" else ""
            lines.append(f"  - → `{label}`{mark}: {delta}")
        reason = g.none_reason(r)
        if reason:
            lines.append(f"  - none: {reason}")
    return len(t), "\n".join(lines)


def orient_text(g: Graph, *, title: str, generator: str, recent: int = 8) -> str:
    """governor's `orient`: the state, the tail's declared impacts, the most recent records."""
    out = [render_state(g, title=title, generator=generator)]
    count, listing = tail_listing(g)
    if count:
        out += ["## The tail's declared impacts", "", *listing.splitlines()[1:], ""]
    newest = sorted(g.records(), key=lambda n: -n.get("seq", 0))[:recent]
    out += ["## Most recent records", "",
            *[f"- `{g.label(r['id'])}` {r.get('title', '')}" + (f" ({r['at'][:10]})" if r.get("at") else "") for r in newest], ""]
    return "\n".join(out)


# -- the brief (`ouroboros mem brief`) -------------------------------------------

# What a newcomer needs to start a turn, at a size that does not grow with the run:
# about 1.5-3k tokens however many claims and records there are. Built from the
# orientation lab's prototype (research-projects/orientation-lab/scripts/brief.py,
# 2026-11-01) on the stores' own snapshot instead of the files on disk.
BRIEF_MAX_CHARS = 10000
BRIEF_FRONTIER = 5
BRIEF_BEST = 5
BRIEF_RULED = 8
BRIEF_FIX = 5
BRIEF_ENV = 5
BRIEF_FILES = 6
ENVIRONMENT_CLAIM = "environment"   # the claim the writer keeps host facts in
CONSTRAINTS_CLAIM = "constraints"   # the charter's constraints, short, as the writer seeds them
BRIEF_CONSTRAINTS = 5

# Negative knowledge bullets the writer tags, so "do not retry" and "fix and resubmit"
# never mean the same thing; an untagged bullet counts as ruled out only when it says so.
_RULED_TAG = re.compile(r"^(?:ruled out|do not retry|dead end)\s*:\s*", re.IGNORECASE)
_FIX_TAG = re.compile(r"^(?:fix and resubmit|fix(?: forward)?|resubmit)\s*:\s*", re.IGNORECASE)
_CLOSES = re.compile(r"\b(avoid|do not|don't|rejected|inconclusive|failed|fails|timed? ?out|crash(?:ed|es)?|"
                     r"cannot|can't|exhausted|ruled out|no gain|slower|denied|refused|abandon|dead end|"
                     r"does not work|did not work|no speedup)\b", re.IGNORECASE)
_HOST = re.compile(r"\b(host|network|offline|download|permission|denied|sandbox|CPU|cores|RAM|\d+ ?GB|"
                   r"Python 3\.\d+|Linux|macOS|GPU|browser|chromium|internet|proxy|disk)\b", re.IGNORECASE)
_NEXT = re.compile(r"\b(next (?:bounded |single |one )?(?:experiment|step|unit|test|diagnostic)|Next:)", re.IGNORECASE)
_REC_TAG = re.compile(r"\s*\[rec:[^\]]*\]")
_PATH = re.compile(r"`([A-Za-z0-9_.\-]+(?:/[A-Za-z0-9_.\-]+)*\.[A-Za-z0-9]{1,8}|[A-Za-z0-9_.\-]+(?:/[A-Za-z0-9_.\-]+)+/?)`")


def _clip(text: str, n: int) -> str:
    text = one_line(text)
    return text if len(text) <= n else text[: n - 1].rsplit(" ", 1)[0] + "…"


def _bullets(sec: str) -> list[str]:
    """A section's bullets (or paragraphs), without comments and `[rec: …]` tags."""
    sec = re.sub(r"<!--.*?-->", "", sec or "", flags=re.S)
    out = []
    for part in re.split(r"\n\s*\n|\n(?=\s*[-*] )", sec):
        part = _REC_TAG.sub("", re.sub(r"^\s*[-*]\s*", "", part.strip())).strip()
        part = one_line(part)
        if part and not part.startswith("_("):
            out.append(part)
    return out


def _sentences(text: str) -> list[str]:
    return [x.strip() for x in re.split(r"(?<=[.;])\s+(?=[A-Z0-9`'\"(])", text) if x.strip()]


def mission_paragraph(goal_text: str, limit: int = 900) -> str:
    """The charter's title and the first paragraph of its mission."""
    title = re.search(r"^#\s+(.*)$", goal_text or "", re.MULTILINE)
    mission = charter.section(goal_text or "", "mission")
    para = one_line(mission.split("\n\n")[0]) if mission.strip() else ""
    head = title.group(1).strip() if title else "Goal"
    return head + ("\n\n" + _clip(para, limit) if para else "")


def brief_text(g: Graph, *, goal_text: str, goal_path: str, max_chars: int = BRIEF_MAX_CHARS) -> str:
    """`ouroboros mem brief`: the goal, the best results, the frontier, what was ruled out
    (apart from what failed and is to be fixed and resubmitted), the host, and the files
    the last unit touched. Every section has a fixed number of clipped items, and the
    whole is cut at `max_chars`, so the brief is the same size on turn 3 and turn 300."""
    out = ["# Brief: what you need to start this turn", "", "## Goal", "",
           mission_paragraph(goal_text) if (goal_text or "").strip() else "(no charter text)", "",
           f"The charter is `{goal_path}`: done criteria, horizon ladder, constraints, policies. "
           "Read it when you need a criterion's exact wording.", ""]
    if not g.ok:
        out += ["## Memory", "", "(the memory could not be read; `ouroboros mem orient` says why)"]
        return "\n".join(out) + "\n"
    claims = g.claims("state")
    env_claim = next((c for c in claims if c.get("slug") == ENVIRONMENT_CLAIM), None)
    rules = next((c for c in claims if c.get("slug") == CONSTRAINTS_CLAIM), None)
    work = [c for c in claims if c is not env_claim and c is not rules]
    if rules is not None:
        kept = [f"- {_clip(x, 200)}" for x in _bullets(section(rules.get("content", ""), "Current"))[:BRIEF_CONSTRAINTS]]
        out += ["## Constraints (short; the charter has them whole)", "", *(kept or ["- (none recorded)"]), ""]

    # the best results: criteria first, then the newest claims, each by its newest number
    working = [c for c in work if c.get("status") == "working"]
    working.sort(key=lambda c: (not str(c.get("slug") or "").startswith("gap-"), -c.get("seq", 0)))
    out += ["## Best results so far", ""]
    for c in working[:BRIEF_BEST]:
        cur = _bullets(section(c.get("content", ""), "Current"))
        best = next((i for i in cur if re.search(r"\d", i)), cur[0] if cur else "")
        out.append(f"- **{_clip(c.get('title', ''), 100)}** (`{c.get('slug')}`): {_clip(best, 300)}")
    if not working:
        out.append("- (nothing established yet)")
    if len(working) > BRIEF_BEST:
        out.append(f"- …and {len(working) - BRIEF_BEST} more working claims (`ouroboros mem orient`).")
    out.append("")

    # the frontier, in the order governor ranks it: broken, blocked, open
    frontier = [c for st in FRONTIER_STATUSES for c in work if c.get("status") == st]
    out += ["## Frontier (open, broken, blocked)", ""]
    for c in frontier[:BRIEF_FRONTIER]:
        body = c.get("content", "")
        cur = _bullets(section(body, "Current"))
        line = (f"- [{c.get('status')}] **{_clip(c.get('title', ''), 100)}** (`{c.get('slug')}`): "
                f"{_clip(cur[0] if cur else '', 320)}")
        nxt = [x for i in cur + _bullets(section(body, "Negative knowledge")) for x in _sentences(i) if _NEXT.search(x)]
        if nxt and nxt[0] not in line:
            line += f" Next: {_clip(re.sub(r'^Next:\s*', '', nxt[0]), 200)}"
        out.append(line)
    if not frontier:
        out.append("- (nothing open: take the next rung of the charter's horizon ladder)")
    if len(frontier) > BRIEF_FRONTIER:
        out.append(f"- …and {len(frontier) - BRIEF_FRONTIER} more (`ouroboros mem orient`).")
    out.append("")

    # negative knowledge, split by what it asks of the next turn
    ruled: list[str] = []
    fix: list[str] = []
    seen: set[str] = set()
    for c in sorted(work, key=lambda c: -c.get("seq", 0)):
        for item in _bullets(section(c.get("content", ""), "Negative knowledge")):
            key = item.lower()[:80]
            if key in seen:
                continue
            seen.add(key)
            if _FIX_TAG.match(item):
                fix.append(f"{_clip(_FIX_TAG.sub('', item), 240)} (`{c.get('slug')}`)")
            elif _RULED_TAG.match(item) or _CLOSES.search(item):
                ruled.append(f"{_clip(_RULED_TAG.sub('', item), 240)} (`{c.get('slug')}`)")
    out += ["## Ruled out (do not retry without a new reason)", ""]
    out += [f"- {x}" for x in ruled[:BRIEF_RULED]] or ["- (nothing recorded)"]
    if len(ruled) > BRIEF_RULED:
        out.append(f"- …and {len(ruled) - BRIEF_RULED} more in the claims' negative knowledge.")
    out.append("")
    records = g.records()
    newest = records[-1] if records else None
    failed = newest is not None and str(newest.get("title") or "").lower().startswith(("failed", "dead end"))
    if fix or failed:
        out += ["## Failed, to fix and resubmit (not ruled out)", ""]
        if failed:
            out.append(f"- The last turn: {_clip(newest.get('title', ''), 200)} (`{newest.get('slug')}`)")
        out += [f"- {x}" for x in fix[:BRIEF_FIX]]
        out.append("")

    # the host: the writer's environment claim, else sentences that speak about it
    env: list[str] = []
    if env_claim is not None:
        body = env_claim.get("content", "")
        env = [_clip(i, 240) for i in _bullets(section(body, "Current")) + _bullets(section(body, "Negative knowledge"))]
    else:
        seen = set()
        for c in work:
            body = c.get("content", "")
            for item in _bullets(section(body, "Current")) + _bullets(section(body, "Negative knowledge")):
                for x in _sentences(item):
                    if _HOST.search(x) and x.lower()[:80] not in seen:
                        seen.add(x.lower()[:80])
                        env.append(_clip(x, 240))
    out += ["## Environment (this host)", ""]
    out += [f"- {x}" for x in env[:BRIEF_ENV]] or ["- (nothing recorded)"]
    out.append("")

    # where the last unit worked: the paths its records name
    files: list[str] = []
    for r in reversed(records[-2:]):
        for m in _PATH.finditer(r.get("content") or ""):
            if m.group(1) not in files and not m.group(1).startswith((".ouroboros/", ".flywheel/")):
                files.append(m.group(1))
    if files:
        out += ["## Files the last unit touched", "", *[f"- `{f}`" for f in files[:BRIEF_FILES]], ""]

    last = f"`{newest.get('slug')}`: {_clip(newest.get('title', ''), 140)}" if newest else "(none)"
    out.append(f"Memory: {len(claims)} claims, {len(records)} records; newest record {last}. "
               "`ouroboros mem show <id>` reads one whole.")
    text = "\n".join(out) + "\n"
    if len(text) > max_chars:
        text = text[: max_chars - 60].rsplit("\n", 1)[0] + "\n- …(brief trimmed; `ouroboros mem orient` has the rest)\n"
    return text


# -- the store interface -------------------------------------------------------

class Store(Protocol):
    """Where a memory lives. Every verb of `ouroboros mem` is one of these."""

    name: str
    where: str          # one sentence for the agent: where this memory lives
    state_md: Path      # the rendered state

    def prepare(self) -> None: ...
    def snapshot(self) -> Graph: ...
    def record(self, *, title: str, body: str, parent: str | None, impacts: list[tuple[str, str]],
               news: list[tuple[str, str]], none: str | None) -> tuple[str | None, str]: ...
    def fold(self, plan: dict) -> tuple[bool, str]: ...
    def check(self) -> tuple[bool, str]: ...
    def orient(self) -> str: ...
    def brief(self, *, goal_text: str, goal_path: str) -> str: ...
    def render(self) -> bool: ...


def parse_pair(text: str, flag: str) -> tuple[str, str]:
    """`"<slug>: <delta>"` -> (slug, delta)."""
    name, sep, delta = text.partition(":")
    if not sep or not name.strip() or not delta.strip():
        raise ValueError(f"{flag} wants \"<name>: <delta>\", got {text!r}")
    return name.strip(), delta.strip()


# -- the run directive -----------------------------------------------------------

def charter_gaps(goal_text: str) -> list[tuple[str, str]]:
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


def directive_body(*, goal_text: str, branch: str, previous: str | None, gaps: list[tuple[str, str]],
                   dropped: list[tuple[str, str]]) -> str:
    """The run directive's body: the charter verbatim and its done criteria as gaps."""
    gap_lines = "\n".join(f"- [gap] {n}: {c}" for n, c in gaps) or "- (none declared)"
    supersedes = (
        f"\n\nThis directive supersedes `{previous}`: the operator edited the charter; this version applies at a run start or iteration boundary."
        if previous else ""
    )
    dropped_text = (
        "\n\nCriteria this version drops (the next fold marks their gaps superseded):\n"
        + "\n".join(f"- {n}: {c}" for n, c in dropped)
    ) if dropped else ""
    # fenced, so the charter's own headings do not read as the record's sections
    text = f"~~~~markdown\n{goal_text.strip()}\n~~~~"
    return (
        "## What\n\nOperator directive: an Ouroboros loop starts on this repo. Every record of the run "
        f"descends from this record.{supersedes}\n\n## Why\n\nThe charter (the operator's goal document), verbatim:\n\n"
        f"{text}\n\n**Done criteria as gaps** (one open state claim each; work closes them through "
        f"declared impacts):\n\n{gap_lines}{dropped_text}\n\n"
        f"## Method\n\nOuroboros iterations on branch `{branch}`: orient, one dispatched unit, record, commit; "
        "the scribe records each unit and folds the tail on pressure.\n\n"
        "## Result\n\nDirective recorded. Work follows as child records.\n"
    )


# -- the memory --------------------------------------------------------------------

class GraphMemory(BaseMemory):
    """The loop's side of a record/state memory, on any store."""

    def __init__(self, store: Store, *, reconcile_every: int = 5, pressure: int = 3, budget_units: int = 1,
                 recent: int = 3) -> None:
        self.store = store
        self.name = store.name
        self.reconcile_every = reconcile_every
        self.pressure = pressure
        self.budget_units = budget_units
        self.recent = recent
        self.branch = ""
        self.goal_text = ""
        self.run_dir: Path | None = None
        self.directive_slug: str | None = None
        self.last_record_slug: str | None = None
        self.since_reconcile = 0
        self.reconcile_due = False
        self.last_error: str | None = None

    @property
    def state_md(self) -> Path:
        return self.store.state_md

    def graph(self) -> Graph:
        return self.store.snapshot()

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

    def new_record(self, *, title: str, body: str, parent: str | None, none_reason: str | None = None,
                   impacts: list[tuple[str, str]] | None = None, news: list[tuple[str, str]] | None = None) -> str | None:
        none = None if (impacts or news) else (none_reason or "no state change")
        slug, message = self.store.record(title=title, body=body, parent=parent or "root", impacts=impacts or [],
                                          news=news or [], none=none)
        if slug is None:
            self.last_error = message
        return slug

    def charter_gaps(self, goal_text: str) -> list[tuple[str, str]]:
        return charter_gaps(goal_text)

    # -- lifecycle -------------------------------------------------------
    def start(self, *, run: str, goal_text: str, run_dir: Path, branch: str) -> None:
        self.branch, self.goal_text, self.run_dir = branch, goal_text, run_dir
        self.store.prepare()
        g = self.graph()
        if not g.ok or not g.root:
            raise RuntimeError(f"no {self.name} memory here ({getattr(self.store, 'last_error', None) or 'no root'})")
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
            body = directive_body(goal_text=goal_text, branch=branch, previous=previous, gaps=gaps, dropped=dropped)
            news = [
                (n, f"Charter gap, status open: {c[:300].rstrip('.')}. Flip to working only when the criterion is verifiably met.")
                for n, c in added
            ]
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
            self.store.render()

    def mark_iteration(self) -> None:
        self.since_reconcile += 1

    def mark_reconciled(self) -> None:
        self.since_reconcile = 0
        self.reconcile_due = False

    # -- the frontier ----------------------------------------------------
    def frontier_text(self, g: Graph | None = None) -> str:
        g = g or self.graph()
        lines = []
        for status in FRONTIER_STATUSES:
            for n in g.claims("state"):
                if n.get("status") == status:
                    lines.append(f"- [{status}] **{n.get('title', '')}** (`{n.get('slug')}`) — "
                                 f"{first_line(section(n.get('content', ''), 'Current'))}")
        return "\n".join(lines)

    def frontier_fingerprint(self) -> str | None:
        """Stable criterion statuses; auxiliary state is tracked only without a charter.

        These are the only two things that make a run's night worth having: a
        claim changed status, or a criterion got ticked. Records, audits and docs
        are how those happen, not whether they did. nt3 wrote 22,437 lines and
        this digest changed once in 201 iterations.
        """
        g = self.graph()
        if not g.ok:
            return None
        from ..criteria import rows
        claims = g.claims("state")
        if not claims:
            return None
        if charter.done_criteria(self.goal_text or "", include_checked=True):
            # Reuse display/accounting identities, duplicate handling and evidence rules.
            pairs = sorted((row["key"], row["status"]) for row in rows(g, self.goal_text))
        else:
            pairs = sorted({(n.get("slug") or n["id"], n.get("status") or "?") for n in claims})
        body = repr(pairs)
        return hashlib.sha256(body.encode()).hexdigest()[:16]

    # -- the tail --------------------------------------------------------
    def unreconciled(self, g: Graph | None = None) -> tuple[int, str]:
        g = g or self.graph()
        if not g.ok:
            return 0, f"(the {self.name} snapshot failed: {getattr(self.store, 'last_error', None)})"
        return tail_listing(g)

    def needs_reconcile(self) -> bool:
        if self.reconcile_due:
            return True
        if self.reconcile_every and self.since_reconcile >= self.reconcile_every:
            return True
        count, _ = self.unreconciled()
        return count >= self.pressure

    def fold_prompt(self) -> str:
        """The fold procedure, for the scribe's prompt when a fold is due."""
        _, tail = self.unreconciled()
        return (
            skill("ouroboros-fold")
            .replace("{branch}", self.branch or "(run branch)")
            .replace("{tail}", f"```\n{tail.strip()[-4000:]}\n```")
        )

    # -- the actor's view ----------------------------------------------------
    def orient_prompt(self) -> str:
        count, _ = self.unreconciled()
        return (
            skill("ouroboros-orient")
            .replace("{where}", self.store.where)
            .replace("{budget}", str(self.budget_units))
            .replace("{orient}", self._fit_orient(self.store.orient().strip()))
            .replace("{tail_count}", str(count))
            + self._legacy_handoff()
        )

    def brief(self, goal_path: str) -> str:
        """`ouroboros mem brief` for the run's goal: what the worker's prompt carries."""
        return self.store.brief(goal_text=self.goal_text, goal_path=goal_path)

    def _legacy_handoff(self) -> str:
        """The last file of the old handoff memory, while this memory is still young."""
        return ""

    @staticmethod
    def _fit_orient(text: str, limit: int = STATE_MD_LIMIT) -> str:
        """Keep the frontier, the tail and the recent records whole; trim the architecture first."""
        if len(text) <= limit:
            return text
        head, sep, rest = text.partition("\n## Architecture")
        if not sep:
            return text[:limit] + "\n\n(… truncated; run `ouroboros mem orient` for the rest)"
        arch, sep2, after = rest.partition("\n## The tail's declared impacts")
        if not sep2:
            arch, sep2, after = rest.partition("\n## Most recent records")
        room = max(limit - len(head) - len(after) - 200, 1000)
        if len(arch) > room:
            arch = arch[:room] + "\n\n(… architecture truncated; `ouroboros mem show <slug>` reads one claim whole)"
        out = head + sep + arch + sep2 + after
        return out if len(out) <= limit * 2 else out[: limit * 2] + "\n\n(… truncated; run `ouroboros mem orient`)"

    def fallback_record(self, *, iteration: int, report: str, verdict: str) -> str | None:
        """The record the engine writes when the scribe wrote none: an iteration is never lost."""
        text = (report or "").strip() or "(the actor wrote no report)"
        body = (f"## What\n\nIteration {iteration}, recorded by the loop: the scribe did not record it.\n\n"
                f"## Why\n\n(see the actor's report below)\n\n## Method\n\n(not recorded)\n\n"
                f"## Result\n\nCritic: {verdict}\n\nThe actor's report, verbatim:\n\n~~~~\n{text[:6000]}\n~~~~\n")
        return self.new_record(title=f"Iteration {iteration} (unrecorded by the scribe)", body=body,
                               parent=self.last_record_slug or self.directive_slug,
                               none_reason="the scribe did not record this iteration; its impacts are unknown")

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
            return f"### Frontier\n\n(the {self.name} snapshot failed: {getattr(self.store, 'last_error', None)})"
        parts = ["### Frontier (open, broken, blocked state claims)\n\n" + (self.frontier_text(g) or "(empty: no open gaps)")]
        count, _ = self.unreconciled(g)
        parts.append(f"Unfolded records in the tail: {count} (their declared impacts are not on the frontier yet).")
        return "\n\n".join(parts)

    def check_report(self) -> str | None:
        ok, text = self.store.check()
        if ok:
            return None
        return f"`ouroboros mem check` failed:\n{text.strip()[-2000:]}"

    def last_summary(self) -> str:
        records = self.graph().records()
        if not records:
            return "no record"
        return (records[-1].get("title") or records[-1].get("slug") or "record")[:72]
