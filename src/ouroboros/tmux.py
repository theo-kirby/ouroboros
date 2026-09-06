"""Run the loop inside a named tmux session so it survives the terminal."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess

ENV_FLAG = "OUROBOROS_IN_TMUX"


def available() -> bool:
    return shutil.which("tmux") is not None


def inside_ouroboros_tmux() -> bool:
    return os.environ.get(ENV_FLAG) == "1"


def session_name(run: str) -> str:
    return f"ouroboros-{run}".replace(".", "_").replace(":", "_")


def session_exists(name: str) -> bool:
    return subprocess.run(["tmux", "has-session", "-t", name], capture_output=True).returncode == 0


def launch(name: str, argv: list[str], cwd: str) -> None:
    """Start `argv` in a detached tmux session with a second pane tailing status."""
    inner = f"{ENV_FLAG}=1 OUROBOROS_TMUX_SESSION={shlex.quote(name)} " + " ".join(shlex.quote(a) for a in argv)
    cmd = f"{inner}; echo; echo \"[ouroboros exited with status $?] press enter to close\"; read _"
    subprocess.run(["tmux", "new-session", "-d", "-s", name, "-c", cwd, cmd], check=True)
    subprocess.run(["tmux", "split-window", "-v", "-t", name, "-c", cwd, "-l", "8",
                    f"{ENV_FLAG}=1 " + shlex.quote(argv[0]) + " status --watch"], check=False)
    subprocess.run(["tmux", "select-pane", "-t", f"{name}:0.0"], check=False)


def kill(name: str) -> None:
    subprocess.run(["tmux", "kill-session", "-t", name], capture_output=True)
