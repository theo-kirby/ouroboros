"""activity.current against recorded transcript lines (tests/samples, long strings cut)."""
from pathlib import Path

from ouroboros import activity

SAMPLES = Path(__file__).parent / "samples"
CLAUDE = (SAMPLES / "activity_claude.jsonl").read_text().splitlines()
CODEX = (SAMPLES / "activity_codex.jsonl").read_text().splitlines()


def test_claude_a_bash_command_waiting_for_its_result_is_a_command():
    act = activity.current(CLAUDE[:1])
    assert act["kind"] == "command" and act["tool"] == "Bash" and act["background"] == 0


def test_claude_a_monitor_is_a_wait_and_its_result_closes_it():
    assert activity.current(CLAUDE[:3])["kind"] == "wait"
    assert activity.current(CLAUDE[:4])["kind"] == "thinking"


def test_claude_an_until_loop_is_a_wait():
    act = activity.current(CLAUDE[:5])
    assert act["kind"] == "wait" and act["what"].startswith("until grep")


def test_claude_background_shells_are_counted_and_a_result_ends_the_call():
    assert activity.current(CLAUDE[:6])["background"] == 1
    assert activity.current(CLAUDE) is None


def test_codex_items_pair_by_id():
    started = activity.current(CODEX[:3])
    assert started["kind"] == "command" and started["what"].startswith("cat ")   # the /bin/zsh -lc wrapper is gone
    assert activity.current(CODEX[:4])["kind"] == "thinking"
    assert activity.current(CODEX[:5])["what"].startswith("git status --short")
    assert activity.current(CODEX) is None


def test_live_reads_the_newest_transcript_of_the_role(tmp_path):
    t = tmp_path / "transcripts"
    t.mkdir()
    (t / "0007-actor.json").write_text("\n".join(CLAUDE[:3]) + "\n")
    assert activity.live(tmp_path, 7, "work")["kind"] == "wait"
    assert activity.live(tmp_path, 7, "critique")["kind"] == "thinking"   # no transcript yet
    assert activity.live(tmp_path, 7, "idle") is None
    assert activity.live(tmp_path / "missing", 7, "work")["kind"] == "thinking"
