"""Credentials from .env files, and Pushover as one POST that never raises."""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.parse

import pytest

from ouroboros import notify
from ouroboros.notify import Console, Pushover, load_env, make_notifier, parse_env, pushover_credentials


def test_parse_env_tolerates_the_usual_shapes():
    text = "# comment\nexport PO_USER=u1\nPO_TOKEN='t1'\nPO_EMAIL=\"x@pomail.net\"\n\nbroken line\n=novalue\n"
    assert parse_env(text) == {"PO_USER": "u1", "PO_TOKEN": "t1", "PO_EMAIL": "x@pomail.net"}


def test_load_env_prefers_the_process_then_the_first_file(tmp_path):
    a, b = tmp_path / "a.env", tmp_path / "b.env"
    a.write_text("PO_USER=from_a\nPO_TOKEN=tok_a\n")
    b.write_text("PO_USER=from_b\nPO_TOKEN=tok_b\nEXTRA=1\n")
    env = load_env([tmp_path / "missing.env", a, b], environ={"PO_USER": "from_env"})
    assert env["PO_USER"] == "from_env" and env["PO_TOKEN"] == "tok_a" and env["EXTRA"] == "1"


def test_env_files_are_repo_then_home(tmp_path):
    paths = notify.env_files(tmp_path / "repo", home=tmp_path / "home")
    assert [str(p.relative_to(tmp_path)) for p in paths] == ["repo/.env", "repo/.ouroboros/.env", "home/.ouroboros/.env"]


@pytest.mark.parametrize("env,expected", [
    ({"PO_USER": "u", "PO_TOKEN": "t"}, ("u", "t")),
    ({"PUSHOVER_USER": "u", "PUSHOVER_TOKEN": "t"}, ("u", "t")),
    ({"PO_USER": "u", "PO_EMAIL": "x@pomail.net"}, None),      # the email gateway is not the API
    ({"PO_USER": " ", "PO_TOKEN": "t"}, None),
    ({}, None),
])
def test_pushover_credentials(env, expected):
    assert pushover_credentials(env) == expected


def test_make_notifier_modes():
    creds = {"PO_USER": "u", "PO_TOKEN": "t"}
    assert make_notifier("auto", creds)[0].name == "pushover"
    assert make_notifier("pushover", creds)[0].name == "pushover"
    n, why = make_notifier("auto", {"PO_USER": "u"})
    assert n is None and "PO_TOKEN" in why and "auto" in why
    n, why = make_notifier("pushover", {})
    assert n is None and "PO_USER" in why and "PO_TOKEN" in why
    assert make_notifier("none", creds) == (None, "notify: none")
    assert make_notifier("console", {})[0].name == "console"
    assert make_notifier("carrier-pigeon", creds)[0] is None


class FakeResponse:
    def __init__(self, body: str) -> None:
        self.body = body.encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self.body


def test_pushover_posts_the_form_and_clips(monkeypatch):
    seen = {}

    def opener(req, timeout):
        seen["url"] = req.full_url
        seen["form"] = urllib.parse.parse_qs(req.data.decode())
        return FakeResponse(json.dumps({"status": 1, "request": "abc"}))

    p = Pushover("user1", "tok1", opener=opener)
    assert p.send("t" * 300, "m" * 2000, priority=5)
    form = seen["form"]
    assert seen["url"] == notify.PUSHOVER_URL
    assert form["user"] == ["user1"] and form["token"] == ["tok1"]
    assert len(form["title"][0]) == 250 and form["title"][0].endswith("…")
    assert len(form["message"][0]) == 1024
    assert form["priority"] == ["1"]           # never the emergency tier, which needs retry/expire


def test_pushover_failures_are_logged_not_raised():
    logged = []

    def http_error(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 400, "Bad Request", {}, io.BytesIO(b'{"errors":["application token is invalid"]}'))

    assert not Pushover("u", "t", opener=http_error, log=logged.append).send("a", "b")
    assert "HTTP 400" in logged[-1] and "token is invalid" in logged[-1]

    def no_network(req, timeout):
        raise OSError("Name or service not known")

    assert not Pushover("u", "t", opener=no_network, log=logged.append).send("a", "b")
    assert "Name or service" in logged[-1]

    def rejected(req, timeout):
        return FakeResponse('{"status": 0, "errors": ["user identifier is invalid"]}')

    assert not Pushover("u", "t", opener=rejected, log=logged.append).send("a", "b")
    assert "rejected" in logged[-1]

    def garbage(req, timeout):
        return FakeResponse("<html>")

    assert not Pushover("u", "t", opener=garbage, log=logged.append).send("a", "b")


def test_console_records_and_survives_a_dead_stdout():
    def bad(_):
        raise OSError(5, "EIO")

    c = Console(out=bad)
    assert c.send("t", "m", priority=-1) and c.sent == [("t", "m", -1)]
