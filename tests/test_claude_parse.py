import json

from ouroboros.harness.claude import ClaudeHarness


def test_parse_json_result():
    out = json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "hello",
                      "session_id": "abc", "total_cost_usd": 0.12, "num_turns": 4})
    r = ClaudeHarness.parse(out, "", 0, False, None)
    assert r.ok and r.text == "hello" and r.session_id == "abc" and r.cost_usd == 0.12 and r.turns == 4


def test_parse_error_flag():
    out = json.dumps({"type": "result", "is_error": True, "result": "Rate limit exceeded (429)"})
    r = ClaudeHarness.parse(out, "", 0, False, None)
    assert not r.ok and r.retriable


def test_parse_garbage():
    r = ClaudeHarness.parse("not json", "stderr text", 1, False, None)
    assert not r.ok and r.error == "stderr text"


def test_build_cmd():
    h = ClaudeHarness()
    cmd = h.build_cmd(resume="s", model="opus", system_append="x")
    assert cmd[1:] == ["-p", "--output-format", "json", "--dangerously-skip-permissions", "--model", "opus", "--resume", "s", "--append-system-prompt", "x"]
