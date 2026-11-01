"""`ouroboros plot` and `ouroboros shot`: figures and screenshots for any run.

`plot` runs a Python script with this package's interpreter (matplotlib, seaborn,
pandas, numpy) after `figstyle.apply()`, with `OUROBOROS_ASSETS` pointing at the run's
assets folder, so `figstyle.save(fig, "name")` lands where the run page finds it.

`shot` screenshots a URL or a local HTML or SVG file with a headless Chromium: one on
PATH, else the one Playwright keeps in its cache. A host without either says so.
"""

from __future__ import annotations

import glob
import os
import runpy
import shutil
import subprocess
import sys
from pathlib import Path

from . import figstyle


def run_plot(script: str, argv: list[str], assets: Path) -> int:
    """Run `script` ("-" for stdin) like `python script argv...`, in the house style."""
    assets.mkdir(parents=True, exist_ok=True)
    os.environ[figstyle.ASSETS_ENV] = str(assets)
    figstyle.apply()
    old_argv = sys.argv
    try:
        if script == "-":
            code = sys.stdin.read()
            sys.argv = ["<stdin>", *argv]
            exec(compile(code, "<stdin>", "exec"), {"__name__": "__main__"})
        else:
            sys.argv = [script, *argv]
            runpy.run_path(script, run_name="__main__")
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
    finally:
        sys.argv = old_argv
    return 0


def chromium() -> str | None:
    """A headless-capable Chromium: on PATH, else Playwright's (newest first)."""
    for name in ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable", "chrome"):
        found = shutil.which(name)
        if found:
            return found
    home = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or Path.home() / ".cache" / "ms-playwright")
    for pattern in ("chromium_headless_shell-*/chrome-*/chrome-headless-shell", "chromium_headless_shell-*/chrome-*/headless_shell",
                    "chromium-*/chrome-linux*/chrome", "chromium-*/chrome-mac*/Chromium.app/Contents/MacOS/Chromium"):
        hits = sorted(glob.glob(str(home / pattern)), reverse=True)
        if hits:
            return hits[0]
    return None


def screenshot(target: str, out: Path, *, size: str = "1280x800", wait_ms: int = 1500) -> tuple[bool, str]:
    """(ok, message). `target` is a URL or a path to a local file."""
    exe = chromium()
    if exe is None:
        return False, ("no headless Chromium on this host (none on PATH, none in ~/.cache/ms-playwright); "
                       "take the picture another way, or draw the result with `ouroboros plot`")
    url = target if "://" in target else Path(target).resolve().as_uri()
    try:
        w, h = (int(x) for x in size.lower().split("x"))
    except ValueError:
        return False, f"--size {size!r}: give WIDTHxHEIGHT, e.g. 1280x800"
    out.parent.mkdir(parents=True, exist_ok=True)
    argv = [exe, "--headless", "--disable-gpu", "--no-sandbox", "--hide-scrollbars", f"--window-size={w},{h}",
            f"--virtual-time-budget={wait_ms}", f"--screenshot={out.resolve()}", url]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=90)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"the browser failed: {exc}"
    if not out.exists() or out.stat().st_size == 0:
        return False, f"no picture was written (exit {proc.returncode}): {(proc.stderr or '').strip()[-300:]}"
    return True, f"saved {out}; look at it before you use it"
