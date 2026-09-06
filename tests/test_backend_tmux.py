"""The tmux backend: a call runs in a window of the run session and lands as files."""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from ouroboros.harness import backend_tmux

pytestmark = pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux not installed")


@pytest.fixture
def tmux_session(monkeypatch):
    name = f"ouroboros-test-{os.getpid()}-{int(time.time())}"
    subprocess.run(["tmux", "new-session", "-d", "-s", name, "sleep 300"], check=True)
    monkeypatch.setenv(backend_tmux.SESSION_ENV, name)
    yield name
    subprocess.run(["tmux", "kill-session", "-t", name], capture_output=True)


def test_runs_in_a_window_and_captures_output(tmux_session, tmp_path: Path):
    assert backend_tmux.usable()
    log = tmp_path / "runs" / "0001-actor-0.log"
    r = backend_tmux.run_in_tmux(["bash", "-c", "cat; echo out; echo err >&2; exit 3"], cwd=tmp_path, timeout=20,
                                 stdin_text="prompt-text\n", log_path=log, poll=0.2)
    assert r.exit_code == 3 and r.stdout == "prompt-text\nout\n" and r.stderr.strip() == "err" and not r.timed_out
    assert log.read_text() == r.stdout and not log.with_suffix(".log.prompt").exists()
    assert backend_tmux._ACTIVE_WINDOWS == set()


def test_timeout_kills_the_window(tmux_session, tmp_path: Path):
    log = tmp_path / "slow.log"
    t0 = time.monotonic()
    r = backend_tmux.run_in_tmux(["bash", "-c", "echo start; sleep 60"], cwd=tmp_path, timeout=2, log_path=log, poll=0.2)
    assert r.timed_out and r.exit_code == -9 and "start" in r.stdout and time.monotonic() - t0 < 15
    windows = subprocess.run(["tmux", "list-windows", "-t", tmux_session], capture_output=True, text=True).stdout
    assert "slow" not in windows


def test_falls_back_to_headless_without_a_session(monkeypatch, tmp_path: Path):
    monkeypatch.delenv(backend_tmux.SESSION_ENV, raising=False)
    r = backend_tmux.run_in_tmux(["bash", "-c", "echo plain"], cwd=tmp_path, timeout=10)
    assert r.exit_code == 0 and r.stdout == "plain\n"
