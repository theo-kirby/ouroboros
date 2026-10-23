import http.client
import json
import threading

from ouroboros import viewdebug

from test_view import GOAL, run_dir  # noqa: F401  (fixture; `repo` is in conftest)


def test_apply_sets_the_stage_and_moves_the_actor():
    base = {"state": "idle", "iteration": 3, "elapsed_s": 600,
            "models": {"claude": {"claude-opus-5-5": ["actor", "critic"]}},
            "usage": {"claude": {"windows": {}, "run_spent": {}}}}
    st = viewdebug.apply(base, {"state": "work", "iteration": 9, "stage_s": 120, "actor": "codex",
                                "five_hour": 40, "seven_day": 5}, now=10_000.0)
    assert st["state"] == "work" and st["iteration"] == 9
    assert st["stage_since"] == 10_000.0 - 120
    assert st["models"] == {"claude": {"claude-opus-5-5": ["critic"]}, "codex": {"gpt-5.5-codex": ["actor"]}}
    assert st["usage"]["codex"]["run_spent"]["five_hour"] == 0.4
    assert base["state"] == "idle"   # the base is not changed


def test_apply_ignores_broken_knobs():
    st = viewdebug.apply({"state": "idle"}, {"state": "rm -rf", "stage_s": "x", "five_hour": None})
    assert st["state"] == "idle"


def test_the_debug_server_sets_state_on_a_copy(run_dir, repo):  # noqa: F811
    before = (run_dir / "status.json").read_text()
    srv = viewdebug.make_server(run_dir, repo, goal_text=GOAL, host="127.0.0.1", port=0)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    try:
        port = srv.server_address[1]
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        c.request("GET", "/")
        page = c.getresponse().read().decode()
        assert 'id="dbg"' in page and "ouroborosSwap" in page
        c.request("POST", viewdebug.DEBUG_PATH, body=json.dumps({"state": "scribe", "iteration": 42}),
                  headers={"Content-Type": "application/json"})
        assert json.loads(c.getresponse().read())["ok"] is True
        copy = json.loads((srv.debug_dir / "status.json").read_text())
        assert copy["state"] == "scribe" and copy["iteration"] == 42
        c.request("POST", "/anything-else", body="{}")
        assert c.getresponse().status == 405
    finally:
        srv.shutdown()
        srv.server_close()
    assert (run_dir / "status.json").read_text() == before   # the real run is never written
