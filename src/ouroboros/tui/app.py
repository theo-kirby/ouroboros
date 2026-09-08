"""The curses application: a poller thread reads the run directory; the main thread draws."""

from __future__ import annotations

import curses
import threading
import time
from pathlib import Path
from typing import Optional, Set

from .layout import MIN_COLS, MIN_LINES, Rect, col, compute_layout, leaf, row
from .panels import (
    LoadHistory, Painter, draw_feed, draw_frontier, draw_iterations, draw_load, draw_log, draw_loop, draw_overseer,
    draw_plan, draw_run, draw_stages, draw_time,
)
from .state import Snapshot, load_snapshot
from .theme import PAIR_DIM, PAIR_TITLE, PAIR_YELLOW, Theme

RENDER_TICK = 0.25
# The run strip is the one panel a person reads at a glance and the one that was
# cramped. Six rows packed four lines against each other and hung the usage on the
# end of the harness line. Eight gives each its own row -- iteration, harness chain,
# usage, charter -- with a blank line before the clock.
RUN_STRIP_H = 8

# hotkey number → panel id; the superscript on each box names it. The four on by
# default answer the four questions a run raises, in the order they get asked:
# is it moving, will the subscription last, what is it saying, what was decided.
#
# The panels are sized by how often what they hold changes. `messages` changes every
# few seconds and gets the most room; the charter and the stage split change a few
# times a run and are now two lines in the run strip rather than two panels.
PANELS = ["iterations", "activity", "messages", "overseer", "time", "frontier", "plan", "log"]
DEFAULT_ON = {"iterations", "activity", "messages", "overseer"}
VIEW = col(
    row(leaf("iterations", 1), leaf("activity", 1), weight=10),
    row(leaf("time", 1), leaf("frontier", 1), weight=6),
    leaf("overseer", weight=4, min_h=4),   # 2 borders + the strip + one line of reason
    # The feed used to be last, which under the old split handed it every leftover row
    # and ran it the height of a screen. Ten or so lines is all anyone reads of a live
    # transcript; the rows are worth more above it.
    row(leaf("messages", 3), leaf("plan", 1), leaf("log", 2), weight=8),
)
DRAW = {
    "iterations": draw_iterations, "activity": draw_load, "time": draw_time,
    "frontier": draw_frontier, "overseer": draw_overseer, "plan": draw_plan,
    "loop": draw_loop, "stages": draw_stages, "log": draw_log,
}


class Poller(threading.Thread):
    def __init__(self, run_dir: Path, **kw) -> None:
        super().__init__(daemon=True)
        self.run_dir = run_dir
        self.kw = kw
        self.interval = 2.0
        self._lock = threading.Lock()
        self._latest: Optional[Snapshot] = None
        self._stop = threading.Event()
        self.error: str | None = None

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                snap = load_snapshot(self.run_dir, **self.kw)
                with self._lock:
                    self._latest = snap
                self.error = None
            except Exception as exc:  # the monitor must never die on a half-written file
                self.error = f"{exc.__class__.__name__}: {exc}"
            slept = 0.0
            while slept < self.interval and not self._stop.is_set():
                time.sleep(0.1)
                slept += 0.1

    def take(self) -> Optional[Snapshot]:
        with self._lock:
            snap, self._latest = self._latest, None
            return snap

    def stop(self) -> None:
        self._stop.set()


class App:
    def __init__(self, run_dir: Path, **kw) -> None:
        self.poller = Poller(run_dir, **kw)
        self.theme = Theme()
        self.last: Optional[Snapshot] = None
        self.enabled: Set[str] = set(DEFAULT_ON)
        self.show_help = False
        self.show_output = False
        self.hist = LoadHistory()
        self._force_clear = False

    def run(self) -> int:
        self.poller.start()
        try:
            return curses.wrapper(self._loop)
        finally:
            self.poller.stop()

    def _loop(self, stdscr) -> int:
        curses.curs_set(0)
        stdscr.nodelay(True)
        stdscr.timeout(int(RENDER_TICK * 1000))
        self.theme.init()
        while True:
            snap = self.poller.take()
            if snap is not None:
                self.last = snap
                self.hist.push(snap.cpu_pct)
            self._draw(stdscr)
            ch = stdscr.getch()
            if ch == -1:
                continue
            if self.show_help:
                self.show_help = False
                continue
            if ch in (ord("q"), 27):
                return 0
            if ch in (ord("h"), ord("?")):
                self.show_help = True
            elif ch == ord("o"):
                self.show_output = not self.show_output
            elif ch in (ord("+"), ord("=")):
                self.poller.interval = max(0.5, self.poller.interval / 2)
            elif ch == ord("-"):
                self.poller.interval = min(30.0, self.poller.interval * 2)
            elif ord("1") <= ch <= ord("9"):
                idx = ch - ord("1")
                if idx < len(PANELS):
                    pid = PANELS[idx]
                    self.enabled ^= {pid}
                    self._force_clear = True
            elif ch == curses.KEY_RESIZE:
                self._force_clear = True

    def _draw(self, stdscr) -> None:
        if self._force_clear:
            stdscr.clear()
            self._force_clear = False
        else:
            stdscr.erase()
        lines, cols = stdscr.getmaxyx()
        p = Painter(stdscr, self.theme)
        if cols < MIN_COLS or lines < MIN_LINES:
            msg = f"terminal too small — resize to at least {MIN_COLS}x{MIN_LINES}"
            p.text(lines // 2, max(0, (cols - len(msg)) // 2), msg, self.theme.attr(PAIR_YELLOW, bold=True))
            stdscr.noutrefresh(); curses.doupdate()
            return
        s = self.last
        if s is None:
            msg = self.poller.error or "reading the run directory…"
            p.text(lines // 2, max(0, (cols - len(msg)) // 2), msg, self.theme.attr(PAIR_DIM))
            stdscr.noutrefresh(); curses.doupdate()
            return
        draw_run(p, Rect(0, 0, RUN_STRIP_H, cols), s)
        placed = compute_layout(Rect(RUN_STRIP_H, 0, lines - RUN_STRIP_H, cols), VIEW, self.enabled)
        for pid, rect in placed.items():
            num = PANELS.index(pid) + 1
            if pid == "activity":
                draw_load(p, rect, s, num, self.hist)
            elif pid == "messages":
                draw_feed(p, rect, s, num, self.show_output)
            else:
                DRAW[pid](p, rect, s, num)
        if self.poller.error:
            p.text(lines - 1, 1, clip_err(self.poller.error, cols - 2), self.theme.attr(PAIR_YELLOW))
        if self.show_help:
            self._draw_help(p, lines, cols)
        stdscr.noutrefresh()
        curses.doupdate()

    def _draw_help(self, p: Painter, lines: int, cols: int) -> None:
        body = [
            "ouroboros status — keys",
            "",
            "  q / Esc    quit (the run keeps going)",
            "  1 - 9      toggle a panel: " + " ".join(f"{i + 1} {n}" for i, n in enumerate(PANELS)),
            "             (5 - 8 are off by default)",
            "  o          show tool output lines in messages",
            "  + / -      faster / slower refresh",
            "  h / ?      this help",
            "",
            "  overseer history: ● ran clean (continue, answer, done accepted)",
            "                    ● blocked (stuck, revert, done rejected)",
            "",
            "press any key to close",
        ]
        bw = min(cols - 2, max(len(s) for s in body) + 4)
        bh = len(body) + 2
        y0, x0 = max(0, (lines - bh) // 2), max(0, (cols - bw) // 2)
        for r in range(bh):
            p.text(y0 + r, x0, " " * bw, self.theme.attr(0))
        inner = p.box(Rect(y0, x0, bh, bw), "help")
        for i, s in enumerate(body):
            if i < inner.h:
                p.text(inner.y + i, inner.x + 1, s, self.theme.attr(PAIR_TITLE if i == 0 else PAIR_DIM), width=inner.w - 2)


def clip_err(s: str, w: int) -> str:
    return s if len(s) <= w else s[: w - 1] + "…"


def run_tui(run_dir: Path, **kw) -> int:
    return App(run_dir, **kw).run()
