"""The governor store: the record/state model in the repo's Flywheel repository (`.flywheel/`).

Every read is `governor snapshot` JSON (cached by head inside governor), and every
write is a governor command, which commits to the Flywheel repository itself. The
graph never goes into git; the rendered STATE.md does, through the loop's commit.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from .graph import Graph, GraphMemory


def _main_checkout(repo: Path) -> Path:
    """The main working tree for `repo` (itself unless `repo` is a git worktree)."""
    try:
        out = subprocess.run(["git", "-C", str(repo), "rev-parse", "--path-format=absolute", "--git-common-dir"],
                             capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return repo
    common = Path(out.stdout.strip()) if out.returncode == 0 and out.stdout.strip() else None
    return common.parent if common is not None and common.name == ".git" else repo


def governor_repo(repo: Path) -> Path | None:
    """The checkout holding the Flywheel repository for `repo` (itself or its main checkout)."""
    for cand in (repo, _main_checkout(repo)):
        if (cand / ".flywheel" / "db.sqlite").exists():
            return cand
    return None


def _json_line(out: str) -> dict | None:
    """The one JSON object a `--json` governor command prints (stderr may trail it)."""
    for line in (out or "").splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    return None


class GovernorStore:
    name = "governor"

    def __init__(self, repo: Path, *, binary: str = "governor", state_md: str = "STATE.md") -> None:
        self.repo = repo
        self.state_md = repo / state_md
        self.binary = shutil.which(binary) or binary
        self.where = ("a governor graph in this repo's Flywheel repository (`.flywheel/`, outside git). "
                      "Every `ouroboros mem` write commits to it")
        self.last_error: str | None = None
        self._home: Path | None = None
        self._cache: tuple | None = None

    # -- CLI plumbing ----------------------------------------------------
    def gov(self, *args: str, stdin: str | None = None, timeout: float = 120) -> tuple[int, str]:
        try:
            proc = subprocess.run(
                [self.binary, *args], cwd=str(self.repo), input=stdin, capture_output=True, text=True, timeout=timeout
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return 2, f"governor {' '.join(args)}: {exc}"
        return proc.returncode, (proc.stdout + proc.stderr).strip()

    def prepare(self) -> None:
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

    def head(self) -> str | None:
        code, out = self.gov("head")
        return out.strip() or None if code == 0 else None

    def snapshot(self) -> Graph:
        """The current snapshot, keyed by the repository head.

        The database's stat is a free pre-check (reads leave it alone, every commit
        moves it); only when it moved does `governor head` decide whether to re-read.
        """
        stamp = self._db_stamp()
        if self._cache is not None:
            cached_stamp, cached_head, cached = self._cache
            if stamp is not None and stamp == cached_stamp:
                return cached
            head = self.head()
            if head is not None and head == cached_head:
                self._cache = (stamp, head, cached)
                return cached
        g = self._read_graph()
        if g.ok and g.head:
            self._cache = (stamp, g.head, g)
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

    # -- the verbs -------------------------------------------------------
    def record(self, *, title: str, body: str, parent: str | None, impacts: list[tuple[str, str]],
               news: list[tuple[str, str]], none: str | None) -> tuple[str | None, str]:
        args = ["record", "--json", "--title", title, "--body", "-", "--parent", parent or "root"]
        for slug, delta in impacts:
            args += ["--impact", f"{slug}: {delta}"]
        for name, delta in news:
            args += ["--new", f"{name}: {delta}"]
        if none:
            args += ["--none", none]
        code, out = self.gov(*args, stdin=body)
        result = _json_line(out) if code == 0 else None
        if not result or not result.get("slug"):
            self.last_error = out
            return None, out
        return result["slug"], f"{result['slug']}  (record)  committed to the Flywheel repository"

    def fold(self, plan: dict) -> tuple[bool, str]:
        if (plan.get("view") or "state") != "state":
            return False, "- a fold writes state; there is no other view"
        fd, path = tempfile.mkstemp(prefix="fold-", suffix=".json")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(plan, f)
            code, out = self.gov("fold", "--apply", path, timeout=600)
        finally:
            os.unlink(path)
        if code != 0:
            self.last_error = out
            return False, out
        self.render()
        return True, f"{out.strip()}; wrote {self.state_md.name}"

    def check(self) -> tuple[bool, str]:
        code, out = self.gov("check", "--json")
        result = _json_line(out)
        if code == 0 and (result is None or result.get("ok", True)):
            return True, out
        if result and isinstance(result.get("findings"), list):
            lines = [f"VIOLATION {f.get('rule', '?')} {(f.get('node') + ': ') if f.get('node') else ''}{f.get('message', '')}"
                     for f in result["findings"] if f.get("level") == "violation"]
            text = "\n".join(lines) or f"{result.get('violations', '?')} violation(s)"
        else:
            text = out   # not JSON: governor itself failed (exit 2/3), so its message is the report
        return False, f"(governor check exit {code})\n{text}"

    def orient(self) -> str:
        code, text = self.gov("orient")
        return text if code == 0 else f"(governor orient failed: {text.strip()[-1000:]})"

    def render(self) -> bool:
        code, out = self.gov("render", "-o", str(self.state_md))
        if code != 0:
            self.last_error = out
        return code == 0


class GovernorMemory(GraphMemory):
    """The loop's memory on a governor graph."""

    def __init__(self, repo: Path, *, reconcile_every: int = 5, pressure: int = 3, budget_units: int = 1,
                 binary: str = "governor", recent: int = 3, state_md: str = "STATE.md") -> None:
        super().__init__(GovernorStore(repo, binary=binary, state_md=state_md), reconcile_every=reconcile_every,
                         pressure=pressure, budget_units=budget_units, recent=recent)
        self.repo = repo

    def gov(self, *args: str, stdin: str | None = None, timeout: float = 120) -> tuple[int, str]:
        return self.store.gov(*args, stdin=stdin, timeout=timeout)
