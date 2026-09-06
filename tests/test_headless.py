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


def test_run_subprocess_streams_the_transcript_while_running(tmp_path):
    import threading
    import time
    log = tmp_path / "t.json"
    seen_early = {}

    def peek():
        for _ in range(40):
            time.sleep(0.05)
            if log.exists() and "first" in log.read_text():
                seen_early["ok"] = True
                return

    t = threading.Thread(target=peek)
    t.start()
    r = run_subprocess(["sh", "-c", "echo first; sleep 0.6; echo second; echo oops >&2"], cwd=tmp_path, timeout=10, log_path=log)
    t.join()
    assert seen_early.get("ok"), "the log file must carry the first line before the process ends"
    assert r.stdout == "first\nsecond\n" and r.exit_code == 0 and not r.timed_out
    assert log.read_text() == "first\nsecond\n"
    assert log.with_suffix(".json.stderr").read_text() == "oops\n"


def test_run_subprocess_feeds_stdin(tmp_path):
    r = run_subprocess(["cat"], cwd=tmp_path, timeout=10, stdin_text="hello\n")
    assert r.stdout == "hello\n"
