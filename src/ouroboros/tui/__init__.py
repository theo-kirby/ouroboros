"""The status TUI: a btop-style curses monitor for one Ouroboros run.

Drawing primitives, theme, and layout tree are ported from the author's own
vllmtop (MIT). Everything it shows is read from the run directory and the repo;
it never talks to the loop process.
"""

from .app import run_tui

__all__ = ["run_tui"]
