"""What a run leaves behind after its logs are gone.

A run directory is tens of megabytes of transcripts and is gitignored, which is
right: nobody should carry a night of JSON in a project's history. The
consequence, until now, was that nothing survived a run at all. Four cadex runs
produced three `REPORT.md` files on one laptop and none at all on the box that
ran the fourth, so the only durable record of some 450 iterations was whatever a
human happened to paste into a chat. An operator agent working inside the target
repo cannot learn from that, because it cannot read it.

So this module writes the part that must last, into the project's own git:

- `.ouroboros/history/<run>.md` -- one digest per run, front matter with the
  measured facts and then the read.
- `.ouroboros/RUNS.md` -- the index, generated from that front matter. Never
  hand-edited, the way `STATE.md` is never hand-edited.

Two rules make the digest trustworthy. Every number in the generated part comes
from the logs or from git, never from a summary somebody wrote. And everything
below `NOTES_MARKER` is the human's -- what the run taught, what to change next
time -- so a rewrite preserves it rather than flattening it.
"""

from __future__ import annotations

import json
import socket
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import goal as charter
from .config import Config
from .gitguard import GitError, GitGuard
from .loops import is_product_change
from .recorder import Recorder
from .usage import short_name

HISTORY_DIR = Path(".ouroboros") / "history"
INDEX_PATH = Path(".ouroboros") / "RUNS.md"
NOTES_MARKER = "<!-- notes: everything below this line is yours; a rewrite keeps it -->"
# One commit, as `git log --name-only` prints it after a record separator.
_COMMIT = "\x1e"


def base_branch(git: GitGuard) -> str:
    """The branch a run is measured against. `main`, or `master` on an older repo."""
    for name in ("main", "master"):
        if git.branch_exists(name):
            return name
    return "main"


# ----------------------------------------------------------------------
@dataclass
class RunFacts:
    """One run, as its logs and git can account for it.

    Everything here is measured. A field that could not be measured is None or
    empty rather than guessed, because the index is read later by someone
    deciding whether to trust a run, and a plausible number is worse than none.
    """

    run: str
    machine: str = ""
    started: str = ""
    ended: str = ""
    elapsed_s: int = 0
    state: str = "?"
    stop_reason: str = "-"
    branch: str = ""
    memory: str = ""
    models: dict[str, str] = field(default_factory=dict)
    iterations: int = 0
    changed: int = 0
    recorded: int = 0
    reverts: int = 0
    failed_reverts: list[dict] = field(default_factory=list)
    verdicts: dict[str, int] = field(default_factory=dict)
    loop_signals: dict[str, int] = field(default_factory=dict)
    longest_loop: int = 0
    bets: list[dict] = field(default_factory=list)
    decisions: list[dict] = field(default_factory=list)
    landed: list[str] = field(default_factory=list)
    commits: int = 0
    diffstat: str = ""
    criteria_total: int = 0
    criteria_closed: int = 0        # ticked at the branch tip, including boxes the charter opened with
    criteria_ticked: int | None = None   # ticked *by this run*; None when there is no merge base to compare
    merged: str = ""          # the merge commit's sha, or "" when the branch is unmerged
    usage_lines: list[str] = field(default_factory=list)
    money: dict[str, float] = field(default_factory=dict)

    @property
    def hours(self) -> float:
        return round(self.elapsed_s / 3600, 1)

    @property
    def ticked(self) -> str:
        """What the run closed, as the index prints it. `?` is not `0`."""
        return "?" if self.criteria_ticked is None else str(self.criteria_ticked)


def gather(repo: Path, cfg: Config, run_dir: Path, *, machine: str | None = None) -> RunFacts:
    """Read one run directory and the repo's git, and account for the run.

    Safe to call on a run that is still going, on one that was killed
    mid-iteration, and on one whose branch has already been merged and deleted.
    """
    rec = Recorder(run_dir, echo=lambda *_: None)
    steps = rec.read_jsonl(rec.iterations)
    decisions = rec.read_decisions()
    st = rec.read_status() or {}
    commits = [s for s in steps if s.get("step") == "commit"]
    reverts = [s for s in steps if s.get("step") == "revert"]

    facts = RunFacts(
        run=cfg.run,
        machine=machine or socket.gethostname(),
        started=_started(run_dir),
        ended=str(st.get("ts") or ""),
        elapsed_s=int(st.get("elapsed_s") or 0),
        state=str(st.get("state") or "?"),
        stop_reason=str(st.get("stop_reason") or st.get("why") or "-"),
        branch=str(st.get("branch") or cfg.branch),
        memory=str(st.get("memory") or cfg.memory),
        models={n: f"{cfg.role(n).harness}:{cfg.role(n).model or 'default'}" for n in cfg.active_roles},
        iterations=len(commits),
        changed=sum(1 for c in commits if c.get("changed")),
        recorded=sum(1 for c in commits if c.get("recorded")),
        reverts=len(reverts),
        bets=[s for s in steps if s.get("step") == "plan"],
        decisions=[d for d in decisions if d.get("verdict") in ("answer", "done_rejected", "stuck", "looping", "reject", "revert")],
        usage_lines=[str(u) for u in (st.get("usage_lines") or [])],
        money={h: float(c) for h, c in (st.get("money") or {}).items()},
    )
    for d in decisions:
        v = d.get("verdict")
        if v:
            facts.verdicts[v] = facts.verdicts.get(v, 0) + 1
    for s in steps:
        if s.get("step") != "loop":
            continue
        sig = str(s.get("signal") or "?")
        facts.loop_signals[sig] = facts.loop_signals.get(sig, 0) + 1
        facts.longest_loop = max(facts.longest_loop, int(s.get("streak") or 0))

    _add_git(repo, facts)
    _add_criteria(repo, cfg, facts)
    facts.failed_reverts = unverified_reverts(repo, facts.branch, reverts)
    return facts


def _started(run_dir: Path) -> str:
    """The `started:` line `cmd_run` appends to run.yml, or nothing."""
    path = run_dir / "run.yml"
    if not path.exists():
        return ""
    for line in path.read_text().splitlines():
        if line.startswith("started:"):
            return line.split(":", 1)[1].strip()
    return ""


def _commits(git: GitGuard, span: str) -> list[tuple[str, list[str]]]:
    """Every commit in `span` as (subject, files), newest first."""
    raw = git.git("log", "--no-merges", f"--format={_COMMIT}%s", "--name-only", span)
    out = []
    for block in raw.split(_COMMIT):
        lines = [l for l in block.splitlines() if l.strip()]
        if lines:
            out.append((lines[0], lines[1:]))
    return out


def _add_git(repo: Path, facts: RunFacts) -> None:
    """Commits, the diffstat, and whether the branch reached the base branch.

    A branch that was merged and then deleted still answers: the merge is found
    by walking the base branch's own merges, not by asking whether the branch
    name survives.

    `landed` is the subjects of commits that touched something outside the
    memory graph, decided by the same `is_product_change` the loop detector
    uses. Filtering on the subject line instead would be a guess -- nt3's
    record commits are called "Record the blocked transition", with no prefix
    to match -- and the two numbers would then disagree about the same run.
    """
    git = GitGuard(repo, facts.branch)
    if not git.is_repo():
        return
    base = base_branch(git)
    tip = facts.branch
    try:
        if not git.branch_exists(tip):
            return
        if git.git_code("merge-base", "--is-ancestor", tip, base) != 0:
            span, diff = f"{base}..{tip}", [f"{base}...{tip}"]
        else:
            # Already merged: `base..tip` is empty, so measure what the merge brought in.
            merges = git.git("rev-list", "--ancestry-path", "--merges", f"{tip}..{base}").split()
            facts.merged = merges[-1] if merges else "yes"
            if not merges:
                return
            merge = merges[-1]
            span, diff = f"{merge}^1..{merge}", [f"{merge}^1", merge]
        commits = _commits(git, span)
        facts.commits = len(commits)
        facts.landed = [subject for subject, files in commits if is_product_change(files)]
        facts.diffstat = (git.git("diff", "--shortstat", *diff) or "").strip()
    except GitError:
        return


def _closed(text: str) -> tuple[int, int]:
    """(ticked, total) done criteria in one version of a charter."""
    total = len(charter.done_criteria(text, include_checked=True))
    return total - len(charter.done_criteria(text)), total


def _add_criteria(repo: Path, cfg: Config, facts: RunFacts) -> None:
    """How many done criteria the charter had, and how many *this run* ticked.

    The total ticked at the branch tip is the wrong number and reads like the
    right one: a charter usually opens with several boxes already checked, the
    work that was shipped before the run started. cadex's nt3 tip says 9 of 13,
    and nt3 closed none of them. So the delta over the run is measured too --
    against the charter as it stood at the merge base, not as it stands now --
    and it is the delta the index leads with.
    """
    git = GitGuard(repo, facts.branch)
    if not (git.is_repo() and facts.branch):
        path = repo / cfg.goal
        if path.exists():
            facts.criteria_closed, facts.criteria_total = _closed(path.read_text())
        return
    tip = ""
    try:
        tip = git.git("show", f"{facts.branch}:{cfg.goal}")
    except GitError:
        path = repo / cfg.goal
        tip = path.read_text() if path.exists() else ""
    if not tip:
        return
    facts.criteria_closed, facts.criteria_total = _closed(tip)
    try:
        start = git.git("merge-base", base_branch(git), facts.branch)
        before, _ = _closed(git.git("show", f"{start}:{cfg.goal}"))
        facts.criteria_ticked = facts.criteria_closed - before
    except GitError:
        facts.criteria_ticked = None   # no merge base to compare against: unknown, not zero


def unverified_reverts(repo: Path, branch: str, reverts: list[dict]) -> list[dict]:
    """Reverts git cannot confirm: the recorded head's tree still differs from the tag.

    A revert that silently no-ops still writes its log line, so counting log
    lines overstates what was undone. Ask git instead.
    """
    git = GitGuard(repo, branch)
    if not git.is_repo():
        return []
    failed = []
    for r in reverts:
        if r.get("error"):
            failed.append(r)
            continue
        tag, sha = r.get("to"), r.get("sha")
        if not tag or not sha:
            continue
        try:
            if git.git("rev-parse", f"{sha}^{{tree}}") != git.git("rev-parse", f"{tag}^{{tree}}"):
                failed.append(r)
        except GitError:
            continue  # tag or commit is gone; not something a digest can judge
    return failed


# ----------------------------------------------------------------------
def front_matter(facts: RunFacts) -> list[str]:
    """The index's source. Flat scalars only, so the index needs no YAML parser."""
    return [
        "---",
        f"run: {facts.run}",
        f"machine: {facts.machine}",
        f"started: {facts.started or '?'}",
        f"ended: {facts.ended or '?'}",
        f"hours: {facts.hours}",
        f"state: {facts.state}",
        f"iterations: {facts.iterations}",
        f"commits: {facts.commits}",
        f"criteria_ticked: {facts.ticked}",
        f"criteria_closed: {facts.criteria_closed}",
        f"criteria_total: {facts.criteria_total}",
        f"merged: {facts.merged or 'no'}",
        f"branch: {facts.branch}",
        f"memory: {facts.memory}",
        f"actor: {facts.models.get('actor', '?')}",
        "---",
    ]


def digest(facts: RunFacts, notes: str = "") -> str:
    """The committed read of one run. Measured above the marker, human below it."""
    out = front_matter(facts)
    out += [
        "",
        f"# Run {facts.run}",
        "",
        f"{facts.iterations} iterations in {facts.hours}h on `{facts.machine}`, "
        f"{facts.state} ({facts.stop_reason}). Branch `{facts.branch}`, "
        + (f"merged as `{facts.merged[:8]}`." if facts.merged else "not merged."),
        "",
        "## The numbers",
        "",
        "| | |",
        "|---|---|",
        f"| iterations | {facts.iterations} (changed {facts.changed}, recorded {facts.recorded}) |",
        f"| commits | {facts.commits}{(' — ' + facts.diffstat) if facts.diffstat else ''} |",
        f"| criteria | **this run ticked {facts.ticked}**; {facts.criteria_closed} of {facts.criteria_total} checked at the tip |",
        f"| reverts | {facts.reverts}"
        + (f", **{len(facts.failed_reverts)} did not take**" if facts.failed_reverts else "")
        + " |",
        f"| verdicts | {', '.join(f'{k} {v}' for k, v in sorted(facts.verdicts.items())) or '-'} |",
        f"| loop detector | {_loops(facts)} |",
        f"| roles | {', '.join(f'{k} {v}' for k, v in facts.models.items())} |",
    ]
    if facts.usage_lines:
        out += [f"| usage | {'; '.join(facts.usage_lines)} |"]
    if facts.money:
        out += [f"| billed | {', '.join(f'{h} ${c:.2f}' for h, c in sorted(facts.money.items()))} |"]

    out += ["", "## What landed", ""]
    out += [f"- {s}" for s in facts.landed[:25]] or ["- (nothing outside the loop's own bookkeeping)"]
    if len(facts.landed) > 25:
        out += [f"- ... and {len(facts.landed) - 25} more"]

    if facts.failed_reverts:
        out += ["", "## Reverts that did not take", ""]
        out += [f"- #{r.get('iteration')}: `{r.get('sha')}` never reached `{r.get('to')}`"
                + (f" — {r['error']}" if r.get("error") else "") for r in facts.failed_reverts]

    if facts.bets:
        out += ["", "## Bets the planner changed", ""]
        out += [f"- #{b.get('iteration')}: {b.get('bet') or 'no bet landed'}" for b in facts.bets[:15]]

    if facts.decisions:
        out += ["", "## Decisions the critic made", ""]
        out += [f"- #{d.get('iteration')} {d.get('verdict')}: {d.get('reason')}" for d in facts.decisions[:15]]

    out += ["", NOTES_MARKER, ""]
    out += [notes.strip() or "## What this taught\n\n(unwritten)"]
    return "\n".join(out).rstrip() + "\n"


def _loops(facts: RunFacts) -> str:
    if not facts.loop_signals:
        return "no firing"
    named = ", ".join(f"{k} ×{v}" for k, v in sorted(facts.loop_signals.items()))
    return f"{named} (longest streak {facts.longest_loop})"


def notes_of(path: Path) -> str:
    """Whatever a human put below the marker in an existing digest."""
    if not path.exists():
        return ""
    text = path.read_text()
    _, _, tail = text.partition(NOTES_MARKER)
    return tail.strip()


# ----------------------------------------------------------------------
def write_digest(repo: Path, facts: RunFacts) -> Path:
    """Write `.ouroboros/history/<run>.md`, keeping any notes already there."""
    path = repo / HISTORY_DIR / f"{facts.run}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(digest(facts, notes_of(path)))
    return path


def _read_front_matter(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    lines = path.read_text().splitlines()
    if not lines or lines[0].strip() != "---":
        return out
    for line in lines[1:]:
        if line.strip() == "---":
            break
        key, sep, value = line.partition(":")
        if sep:
            out[key.strip()] = value.strip()
    return out


def write_index(repo: Path) -> Path:
    """Regenerate `.ouroboros/RUNS.md` from every digest's front matter.

    Fully generated, like `STATE.md`: put what a run taught in its own digest,
    below the notes marker, and this table stays a table.
    """
    rows = []
    for path in sorted((repo / HISTORY_DIR).glob("*.md")) if (repo / HISTORY_DIR).exists() else []:
        fm = _read_front_matter(path)
        if not fm.get("run"):
            continue
        rows.append(fm)
    rows.sort(key=lambda fm: fm.get("started") or "")
    lines = [
        "# Runs",
        "",
        "Every Ouroboros run against this repo. Generated from"
        " `.ouroboros/history/*.md` by `ouroboros archive`; do not hand-edit.",
        "",
        "| run | started | machine | iters | commits | ticked | hours | merged |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for fm in rows:
        started = (fm.get("started") or "?")[:10]
        merged = fm.get("merged", "no")
        merged = "no" if merged == "no" else f"`{merged[:8]}`"
        lines.append(
            f"| [{fm['run']}](history/{fm['run']}.md) | {started} | {fm.get('machine', '?')} |"
            f" {fm.get('iterations', '?')} | {fm.get('commits', '?')} |"
            f" {fm.get('criteria_ticked', '?')} of {fm.get('criteria_total', '?')} |"
            f" {fm.get('hours', '?')} | {merged} |"
        )
    if not rows:
        lines.append("| (no runs yet) | | | | | | | |")
    lines += ["", f"Written {datetime.now().isoformat(timespec='minutes')}.", ""]
    path = repo / INDEX_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines))
    return path


def archive(repo: Path, cfg: Config, run_dir: Path, *, machine: str | None = None) -> tuple[Path, Path]:
    """Digest one run and rebuild the index. The whole durable record, in one call."""
    facts = gather(repo, cfg, run_dir, machine=machine)
    return write_digest(repo, facts), write_index(repo)


# ----------------------------------------------------------------------
def spent_windows(repo: Path, *, floor: float = 0.5) -> list[tuple[str, str, float, float]]:
    """Windows the last run left full and that have not reset yet.

    A launch does not fail on a spent window, it stalls on one: the loop starts,
    the first call is refused, and the run spends its wall clock asleep. ot4 left
    Codex's weekly window at 99% with five days to run, so the next launch needed
    to know before it started, not after.

    Nothing here calls a harness. The reading is the one the last run recorded on
    its way out, which is the freshest number available for free, and a window
    only ever falls with time -- so a window this says is spent may have quietly
    recovered, and one it says is clear is never worse than it claims.
    """
    runs = repo / ".ouroboros" / "runs"
    if not runs.is_dir():
        return []
    latest, when = None, 0.0
    for path in runs.glob("*/status.json"):
        stamp = path.stat().st_mtime
        if stamp > when:
            latest, when = path, stamp
    if latest is None:
        return []
    try:
        usage = (json.loads(latest.read_text()) or {}).get("usage") or {}
    except (OSError, json.JSONDecodeError):
        return []
    now = time.time()
    out = []
    for harness, snap in sorted(usage.items()):
        for name, w in sorted((snap or {}).get("windows", {}).items()):
            util, resets = w.get("utilization"), w.get("resets_at")
            if not isinstance(util, (int, float)) or util < floor:
                continue
            if isinstance(resets, (int, float)) and resets > now:
                out.append((harness, name, float(util), float(resets)))
    return out


def spent_window_lines(repo: Path, *, floor: float = 0.5) -> list[str]:
    """`spent_windows` as one warning line each, with how long until it resets."""
    now = time.time()
    lines = []
    for harness, name, util, resets in spent_windows(repo, floor=floor):
        hours = (resets - now) / 3600
        when = f"{hours:.0f}h" if hours < 48 else f"{hours / 24:.0f}d"
        lines.append(f"{harness} {short_name(name)} was {util * 100:.0f}% full at the last run's end; resets in {when}")
    return lines
