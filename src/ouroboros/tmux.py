"""Run the loop inside tmux so it survives the terminal.

Two shapes. Launched from inside a tmux session -- the operator's, where an agent
and a human are already talking about the run -- the loop takes a new window of
that session, beside them, and they open another window for `ouroboros top`.
Launched from a bare terminal, or with `--own-session`, it gets a detached
session of its own with the loop log on top and the status view under it.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess

ENV_FLAG = "OUROBOROS_IN_TMUX"
SESSION_ENV = "OUROBOROS_TMUX_SESSION"


def available() -> bool:
    return shutil.which("tmux") is not None


def inside_ouroboros_tmux() -> bool:
    return os.environ.get(ENV_FLAG) == "1"


def current_session() -> str | None:
    """The tmux session this process is running in, or None outside tmux."""
    if not os.environ.get("TMUX") or not available():
        return None
    proc = subprocess.run(["tmux", "display-message", "-p", "#S"], capture_output=True, text=True)
    name = proc.stdout.strip()
    return name if proc.returncode == 0 and name else None


def session_name(run: str) -> str:
    return f"ouroboros-{run}".replace(".", "_").replace(":", "_")


def window_name(run: str) -> str:
    return session_name(run)


def session_exists(name: str) -> bool:
    return subprocess.run(["tmux", "has-session", "-t", name], capture_output=True).returncode == 0


def _inner(session: str, argv: list[str]) -> str:
    return (f"{ENV_FLAG}=1 {SESSION_ENV}={shlex.quote(session)} "
            + " ".join(shlex.quote(a) for a in argv)
            + "; echo; echo \"[ouroboros exited with status $?] press enter to close\"; read _")


def launch(name: str, argv: list[str], cwd: str) -> None:
    """Start `argv` in a detached tmux session with a second pane tailing status."""
    subprocess.run(["tmux", "new-session", "-d", "-s", name, "-c", cwd, _inner(name, argv)], check=True)
    subprocess.run(["tmux", "split-window", "-v", "-t", name, "-c", cwd, "-l", "8",
                    f"{ENV_FLAG}=1 " + shlex.quote(argv[0]) + " status --watch"], check=False)
    subprocess.run(["tmux", "select-pane", "-t", f"{name}:0.0"], check=False)


def launch_window(session: str, window: str, argv: list[str], cwd: str) -> str:
    """Start `argv` in a new window of an existing session. Returns the window's target."""
    proc = subprocess.run(
        ["tmux", "new-window", "-d", "-t", session, "-n", window, "-c", cwd,
         "-P", "-F", "#{session_name}:#{window_id}", _inner(session, argv)],
        capture_output=True, text=True, check=True,
    )
    return proc.stdout.strip()


def kill(name: str) -> None:
    subprocess.run(["tmux", "kill-session", "-t", name], capture_output=True)


def kill_window(target: str) -> None:
    subprocess.run(["tmux", "kill-window", "-t", target], capture_output=True)
