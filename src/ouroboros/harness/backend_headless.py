"""Run a CLI as a subprocess with a hard timeout. Kill the whole process group."""

from __future__ import annotations

import os
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path


_ACTIVE: set[subprocess.Popen] = set()
BACKEND = "headless"   # "headless" | "tmux"; set by the CLI from config.backend


def kill_active() -> int:
    """Kill every harness subprocess this process started. Used on SIGTERM/SIGHUP/Ctrl-C."""
    n = 0
    for proc in list(_ACTIVE):
        _kill_group(proc)
        n += 1
    if BACKEND == "tmux":
        from .backend_tmux import kill_active_windows
        n += kill_active_windows()
    return n


def run(cmd: list[str], *, cwd: Path, timeout: float, stdin_text: str | None = None,
        log_path: Path | None = None, env: dict[str, str] | None = None) -> "ProcResult":
    """Run through the configured backend."""
    if BACKEND == "tmux":
        from .backend_tmux import run_in_tmux
        return run_in_tmux(cmd, cwd=cwd, timeout=timeout, stdin_text=stdin_text, log_path=log_path, env=env)
    return run_subprocess(cmd, cwd=cwd, timeout=timeout, stdin_text=stdin_text, log_path=log_path, env=env)


@dataclass
class ProcResult:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool


def run_subprocess(
    cmd: list[str],
    *,
    cwd: Path,
    timeout: float,
    stdin_text: str | None = None,
    log_path: Path | None = None,
    env: dict[str, str] | None = None,
) -> ProcResult:
    proc = subprocess.Popen(
        cmd,
        cwd=str(cwd),
        stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
        env={**os.environ, **(env or {})},
    )
    timed_out = False
    _ACTIVE.add(proc)
    try:
        try:
            out, err = proc.communicate(input=stdin_text, timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_group(proc)
            try:
                out, err = proc.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                out, err = "", "killed after timeout"
        except BaseException:
            _kill_group(proc)
            raise
    finally:
        _ACTIVE.discard(proc)
    if log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(out or "")
        if err:
            log_path.with_suffix(log_path.suffix + ".stderr").write_text(err)
    return ProcResult(proc.returncode if proc.returncode is not None else -9, out or "", err or "", timed_out)


def _kill_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
