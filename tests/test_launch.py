"""Where the loop lives: the launchers (launch.py) and how run/stop/watch use them."""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from ouroboros import cli, launch, tmux
from ouroboros.config import Config


class _Proc:
    def __init__(self, stdout: str = "", returncode: int = 0):
        self.stdout, self.returncode, self.stderr = stdout, returncode, ""


@pytest.fixture
def commands(monkeypatch):
    """Every subprocess the launchers run, recorded instead of run."""
    seen: list[list[str]] = []

    def run(cmd, **kwargs):
        seen.append(list(cmd))
        if cmd[:2] == ["tern", "new"]:
            return _Proc(json.dumps({"session": 1, "tab": 2, "block": 3}))
        if cmd[:3] == ["tern", "ls", "--json"]:
            return _Proc(json.dumps({"sessions": [{"name": "cdx"}]}))
        if cmd[:3] == ["systemctl", "--user", "is-active"]:
            return _Proc("inactive\n", 3)
        return _Proc()

    monkeypatch.setattr(launch.subprocess, "run", run)
    return seen


# -- choosing ---------------------------------------------------------------

def test_auto_follows_the_terminal_it_is_started_from(monkeypatch):
    monkeypatch.setattr(tmux, "available", lambda: True)
    monkeypatch.setattr(launch.shutil, "which", lambda name: f"/usr/bin/{name}")
    assert launch.resolve("auto", {"TMUX": "/tmp/tmux-1/default,1,0"}) == "tmux"
    assert launch.resolve("auto", {"TERN_PANE": "42"}) == "tern"
    assert launch.resolve("auto", {"TMUX": "x", "TERN_PANE": "42"}) == "tmux"   # tmux inside a tern pane
    assert launch.resolve("auto", {}) == "tmux"
    monkeypatch.setattr(tmux, "available", lambda: False)
    assert launch.resolve("auto", {}) == "foreground"
    assert launch.resolve("systemd", {"TERN_PANE": "42"}) == "systemd"   # an explicit choice is kept


def test_config_and_flags_choose_the_launcher():
    import argparse
    assert Config().launcher == "auto"
    cfg = Config.model_validate({"launcher": "systemd", "memory_max": "40G"})
    assert (cfg.launcher, cfg.memory_max) == ("systemd", "40G")
    with pytest.raises(ValueError):
        Config.model_validate({"launcher": "screen"})
    args = argparse.Namespace(launcher="tern", foreground=False)
    assert cli.load_config(args, __import__("pathlib").Path("/nonexistent")).launcher == "tern"
    args = argparse.Namespace(launcher=None, foreground=True)
    assert cli.load_config(args, __import__("pathlib").Path("/nonexistent")).launcher == "foreground"


def test_a_launched_loop_never_launches_again(monkeypatch):
    monkeypatch.delenv(tmux.ENV_FLAG, raising=False)
    monkeypatch.delenv(launch.LAUNCHED_ENV, raising=False)
    assert not launch.launched()
    monkeypatch.setenv(launch.LAUNCHED_ENV, "1")
    assert launch.launched()


# -- the systemd unit -------------------------------------------------------

def test_the_unit_inherits_the_shell_minus_its_terminal_and_agent_session():
    env = {"PATH": "/usr/bin", "HOME": "/home/t", "ANTHROPIC_API_KEY": "k", "TMUX": "x", "TERN_PANE": "1",
           "SSH_CONNECTION": "a b", "TERM": "xterm", "CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": "cli",
           "OUROBOROS_IN_TMUX": "1", "MULTI": "a\nb"}
    assert launch.unit_environment(env) == {"PATH": "/usr/bin", "HOME": "/home/t", "ANTHROPIC_API_KEY": "k"}


def test_systemd_unit_caps_memory_and_marks_the_loop_launched(commands, monkeypatch):
    monkeypatch.setattr(launch.os, "environ", {"PATH": "/usr/bin"})
    launch.systemd_unit("ouroboros-r.service", ["/bin/ouroboros", "run"], "/repo", memory_max="40G")
    cmd = commands[-1]
    assert cmd[:6] == ["systemd-run", "--user", "--unit", "ouroboros-r.service", "--collect", "--quiet"]
    assert "WorkingDirectory=/repo" in cmd and "MemoryMax=40G" in cmd and "MemorySwapMax=0" in cmd
    assert "--setenv=PATH=/usr/bin" in cmd and f"--setenv={launch.LAUNCHED_ENV}=1" in cmd
    assert cmd[cmd.index("--") + 1:] == ["/bin/ouroboros", "run"]
    launch.systemd_unit("ouroboros-r-watch.service", ["/bin/ouroboros", "watch"], "/repo")
    assert not any(a.startswith("MemoryMax") for a in commands[-1])


def test_run_with_systemd_starts_a_unit_and_stop_stops_it(tmp_path, commands, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["/home/t/.local/bin/ouroboros", "run", "--for", "24h"])
    cfg = Config(run="orun6", launcher="systemd", memory_max="40G")
    assert cli._launch(cfg, tmp_path, "systemd") == 0
    rd = tmp_path / ".ouroboros" / "runs" / "orun6"
    assert launch.read_note(rd) == ("systemd", "ouroboros-orun6.service")
    unit = next(c for c in commands if c[0] == "systemd-run")
    assert unit[unit.index("--") + 1:] == ["/home/t/.local/bin/ouroboros", "run", "--for", "24h"]
    out = capsys.readouterr().out
    assert "systemd user unit ouroboros-orun6.service, MemoryMax=40G" in out and "reporter: off" in out

    monkeypatch.setattr(tmux, "kill", lambda n: pytest.fail("a systemd run has no tmux session"))
    cli._kill_launched(cfg, rd)
    assert commands[-1] == ["systemctl", "--user", "stop", "ouroboros-orun6.service"]


def test_systemd_puts_the_reporter_in_a_unit_of_its_own(tmp_path, commands, monkeypatch, capsys):
    monkeypatch.setattr(cli.notify, "load_env", lambda *a, **k: {"PO_USER": "u", "PO_TOKEN": "t"})
    monkeypatch.setattr(sys, "argv", ["/bin/ouroboros", "run"])
    assert cli._launch(Config(run="r"), tmp_path, "systemd") == 0
    units = [c[c.index("--unit") + 1] for c in commands if c[0] == "systemd-run"]
    assert units == ["ouroboros-r.service", "ouroboros-r-watch.service"]
    rd = tmp_path / ".ouroboros" / "runs" / "r"
    assert launch.read_note(rd, watch=True) == ("systemd", "ouroboros-r-watch.service")
    cli._stop_reporter(rd)
    assert ["systemctl", "--user", "stop", "ouroboros-r-watch.service"] in commands


def test_an_active_unit_of_the_same_name_refuses_a_second_run(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(launch, "unit_active", lambda unit: True)
    monkeypatch.setattr(launch, "systemd_unit", lambda *a, **k: pytest.fail("must not start a second unit"))
    assert cli._launch(Config(run="r"), tmp_path, "systemd") == 1
    assert "already active" in capsys.readouterr().out


# -- tern -------------------------------------------------------------------

def test_run_with_tern_takes_a_session_of_its_own(tmp_path, commands, monkeypatch, capsys):
    monkeypatch.setattr(cli.notify, "load_env", lambda *a, **k: {"PO_USER": "u", "PO_TOKEN": "t"})
    monkeypatch.setattr(sys, "argv", ["/bin/ouroboros", "run"])
    assert cli._launch(Config(run="r"), tmp_path, "tern") == 0
    new = [c for c in commands if c[:2] == ["tern", "new"]]
    assert new[0][:8] == ["tern", "new", "--json", "session", "ouroboros-r", "--cwd", str(tmp_path), "--keep-open"]
    # everything after `--` is the program: nothing of tern's may follow it
    assert new[0][new[0].index("--") + 1:] == ["env", f"{launch.LAUNCHED_ENV}=1", "/bin/ouroboros", "run"]
    assert new[1][:5] == ["tern", "new", "--json", "tab", "ouroboros-r"]   # the reporter, a tab beside it
    assert new[1][new[1].index("--") + 1:] == ["env", f"{launch.LAUNCHED_ENV}=1", "/bin/ouroboros",
                                                "watch", "--run-name", "r"]
    rd = tmp_path / ".ouroboros" / "runs" / "r"
    assert launch.read_note(rd) == ("tern", "ouroboros-r")
    assert launch.read_note(rd, watch=True) == ("tern-tab", "3")
    cli._kill_launched(Config(run="r"), rd)
    assert ["tern", "close", "3"] in commands and commands[-1] == ["tern", "kill", "session", "ouroboros-r"]


def test_tern_refuses_a_session_name_already_taken(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(launch, "tern_sessions", lambda: ["cdx", "ouroboros-r"])
    monkeypatch.setattr(launch, "tern_session", lambda *a: pytest.fail("must not start a second session"))
    assert cli._launch(Config(run="r"), tmp_path, "tern") == 1
    assert "already exists" in capsys.readouterr().out


def test_a_failed_launch_says_why_and_leaves_no_note(tmp_path, monkeypatch, capsys):
    def boom(*a, **k):
        raise subprocess.CalledProcessError(1, ["systemd-run"], stderr="Failed to connect to bus")
    monkeypatch.setattr(launch, "unit_active", lambda unit: False)
    monkeypatch.setattr(launch, "systemd_unit", boom)
    assert cli._launch(Config(run="r"), tmp_path, "systemd") == 2
    assert "Failed to connect to bus" in capsys.readouterr().err
    assert launch.read_note(tmp_path / ".ouroboros" / "runs" / "r") is None
