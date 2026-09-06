"""Run a harness call inside a tmux window so a human can watch it stream.

The call still ends up as files (stdout, stderr, exit code) under the transcript
path, so the drivers parse exactly what they parse under the headless backend.
Falls back to the headless backend when tmux or the session is missing.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import time
from pathlib import Path

from .backend_headless import ProcResult, run_subprocess

SESSION_ENV = "OUROBOROS_TMUX_SESSION"
_ACTIVE_WINDOWS: set[str] = set()


def session() -> str | None:
    return os.environ.get(SESSION_ENV) or None


def usable() -> bool:
    name = session()
    if not name or shutil.which("tmux") is None:
        return False
    return subprocess.run(["tmux", "has-session", "-t", name], capture_output=True).returncode == 0


def kill_active_windows() -> int:
    n = 0
    for target in list(_ACTIVE_WINDOWS):
        subprocess.run(["tmux", "kill-window", "-t", target], capture_output=True)
        _ACTIVE_WINDOWS.discard(target)
        n += 1
    return n


def run_in_tmux(
    cmd: list[str],
    *,
    cwd: Path,
    timeout: float,
    stdin_text: str | None = None,
    log_path: Path | None = None,
    env: dict[str, str] | None = None,
    window: str | None = None,
    poll: float = 1.0,
) -> ProcResult:
    if not usable():
        return run_subprocess(cmd, cwd=cwd, timeout=timeout, stdin_text=stdin_text, log_path=log_path, env=env)
    name = session()
    log_path = log_path or (cwd / ".ouroboros" / "runs" / "tmux" / f"call-{int(time.time())}.log")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    prompt_file = log_path.with_suffix(log_path.suffix + ".prompt")
    err_file = log_path.with_suffix(log_path.suffix + ".stderr")
    exit_file = log_path.with_suffix(log_path.suffix + ".exit")
    prompt_file.write_text(stdin_text or "")
    for f in (log_path, err_file, exit_file):
        if f.exists():
            f.unlink()
    exports = " ".join(f"export {k}={shlex.quote(v)};" for k, v in (env or {}).items())
    inner = " ".join(shlex.quote(a) for a in cmd)
    # bash: stream to the pane and to the log at once; keep the command's own exit status
    script = (
        f"{exports} cd {shlex.quote(str(cwd))} && {inner} < {shlex.quote(str(prompt_file))} 2> {shlex.quote(str(err_file))} "
        f"| tee {shlex.quote(str(log_path))}; echo ${{PIPESTATUS[0]}} > {shlex.quote(str(exit_file))}"
    )
    window = (window or log_path.stem)[:40]
    proc = subprocess.run(
        ["tmux", "new-window", "-d", "-t", name, "-n", window, "-P", "-F", "#{session_name}:#{window_id}",
         "bash", "-c", script],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        return run_subprocess(cmd, cwd=cwd, timeout=timeout, stdin_text=stdin_text, log_path=log_path, env=env)
    target = proc.stdout.strip()
    _ACTIVE_WINDOWS.add(target)
    deadline = time.monotonic() + timeout
    timed_out = False
    try:
        while not exit_file.exists():
            if time.monotonic() > deadline:
                timed_out = True
                subprocess.run(["tmux", "kill-window", "-t", target], capture_output=True)
                break
            time.sleep(poll)
    finally:
        _ACTIVE_WINDOWS.discard(target)
    out = log_path.read_text() if log_path.exists() else ""
    err = err_file.read_text() if err_file.exists() else ""
    if timed_out:
        err = (err + "\nkilled after timeout").strip()
        code = -9
    else:
        try:
            code = int(exit_file.read_text().strip() or 1)
        except ValueError:
            code = 1
    try:
        prompt_file.unlink()
    except OSError:
        pass
    return ProcResult(code, out, err, timed_out)
