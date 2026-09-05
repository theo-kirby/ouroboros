import threading
import time
from pathlib import Path

from ouroboros.harness.backend_headless import kill_active, run_subprocess


def test_kill_active_stops_a_running_child(tmp_path: Path):
    out = {}

    def run():
        out["r"] = run_subprocess(["sleep", "30"], cwd=tmp_path, timeout=60)

    t = threading.Thread(target=run)
    t.start()
    time.sleep(0.3)
    assert kill_active() == 1
    t.join(timeout=5)
    assert not t.is_alive()
    assert out["r"].exit_code != 0 and not out["r"].timed_out


def test_timeout_kills_and_reports(tmp_path: Path):
    r = run_subprocess(["sleep", "5"], cwd=tmp_path, timeout=0.3)
    assert r.timed_out and r.exit_code != 0
