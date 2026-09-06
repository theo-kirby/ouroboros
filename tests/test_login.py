import json
from types import SimpleNamespace

from ouroboros.harness import login


def fake_run(table):
    def run(cmd):
        out, code = table[cmd[0]]
        return SimpleNamespace(stdout=out, stderr="", returncode=code)
    return run


def test_check_each_harness(monkeypatch, tmp_path):
    monkeypatch.setattr(login.shutil, "which", lambda name: "/bin/" + name)
    (tmp_path / ".pi" / "agent").mkdir(parents=True)
    (tmp_path / ".pi" / "agent" / "settings.json").write_text(json.dumps({"defaultProvider": "openrouter"}))
    run = fake_run({
        "claude": (json.dumps({"loggedIn": True, "email": "t@t", "subscriptionType": "max"}), 0),
        "codex": ("Logged in using ChatGPT\n", 0),
        "pi": ('{"status":"ready","provider":"openrouter","authType":"api_key"}\n', 0),
    })
    assert login.check("claude", run=run) == (login.OK, "claude: logged in as t@t (max)")
    assert login.check("codex", run=run) == (login.OK, "codex: Logged in using ChatGPT")
    assert login.check("pi", run=run, home=tmp_path) == (login.OK, "pi: provider openrouter ready (api_key)")

    run = fake_run({
        "claude": (json.dumps({"loggedIn": False}), 0),
        "codex": ("Not logged in\n", 1),
        "pi": ('{"status":"not_ready","provider":"openrouter","reason":"credentials_not_configured"}', 1),
    })
    assert login.check("claude", run=run)[0] == login.NOT_LOGGED_IN
    assert login.check("codex", run=run) == (login.NOT_LOGGED_IN, "codex: Not logged in; run `codex login`")
    status, detail = login.check("pi", run=run, home=tmp_path)
    assert status == login.NOT_LOGGED_IN and "credentials_not_configured" in detail
    assert login.check("pi", run=run, home=tmp_path / "nowhere")[0] == login.UNKNOWN


def test_check_missing_and_failures(monkeypatch):
    monkeypatch.setattr(login.shutil, "which", lambda name: None)
    assert login.check("codex") == (login.MISSING, "codex: not on PATH")
    monkeypatch.setattr(login.shutil, "which", lambda name: "/bin/x")

    def boom(cmd):
        raise OSError("nope")
    assert login.check("claude", run=boom)[0] == login.UNKNOWN
    assert login.check("gemini", run=boom)[0] == login.UNKNOWN


def test_check_roles_errors_only_when_a_role_has_nothing():
    table = {"claude": (login.NOT_LOGGED_IN, "claude: not logged in"), "codex": (login.OK, "codex: Logged in"), "pi": (login.MISSING, "pi: not on PATH")}
    checker = lambda h, m: table[h]
    chains = {"actor": [("claude", None), ("codex", None)], "critic": [("codex", None)], "planner": [("claude", None), ("pi", None)]}
    errors, notes = login.check_roles(chains, checker=checker)
    assert errors == ["planner: no usable harness — claude: not logged in; pi: not on PATH"]
    assert "actor: claude: not logged in; starting on codex" in notes and "codex: Logged in" in notes
    ok = {"claude": (login.OK, "claude: logged in as t")}
    assert login.check_roles({"actor": [("claude", None)]}, checker=lambda h, m: ok[h]) == ([], ["claude: logged in as t"])
    unknown = {"pi": (login.UNKNOWN, "pi: cannot check")}
    errors, notes = login.check_roles({"actor": [("pi", None)]}, checker=lambda h, m: unknown[h])
    assert errors == [] and notes == ["actor: pi: cannot check; could not verify"]
