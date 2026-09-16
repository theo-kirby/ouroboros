"""`ouroboros watch`, and the reporter `run` and `stop` put beside the loop."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from ouroboros import cli
from ouroboros.cli import main
from ouroboros.config import Config


@pytest.fixture
def run_dir(repo: Path, monkeypatch) -> Path:
    monkeypatch.chdir(repo)
    (repo / ".ouroboros" / "config.yml").write_text(Config(run="r").dump())
    rd = repo / ".ouroboros" / "runs" / "r"
    rd.mkdir(parents=True)
    (rd / "status.json").write_text(json.dumps({"ts": "t", "epoch": 1.0, "run": "r", "state": "work",
                                                "iteration": 3, "branch": "ouroboros/r", "harness": "claude"}))
    return rd


def test_watch_refuses_without_credentials(run_dir, monkeypatch, capsys):
    monkeypatch.setattr(cli.notify, "load_env", lambda *a, **k: {})
    assert main(["watch", "--test"]) == 2
    err = capsys.readouterr().err
    assert "PO_USER" in err and "PO_TOKEN" in err and "--console" in err


def test_watch_test_sends_one_message(run_dir, monkeypatch, capsys):
    monkeypatch.setattr(cli.notify, "load_env", lambda *a, **k: {"PO_USER": "u", "PO_TOKEN": "t"})
    sent = []
    monkeypatch.setattr(cli.notify.Pushover, "send",
                        lambda self, title, message, priority=0: sent.append((title, message)) or True)
    assert main(["watch", "--test"]) == 0
    assert sent == [("r: test", "Ouroboros can reach this phone.")]
    assert "pushover: sent" in capsys.readouterr().out


def test_watch_test_reports_a_failure(run_dir, monkeypatch, capsys):
    monkeypatch.setattr(cli.notify, "load_env", lambda *a, **k: {"PO_USER": "u", "PO_TOKEN": "t"})
    monkeypatch.setattr(cli.notify.Pushover, "send", lambda *a, **k: False)
    assert main(["watch", "--test"]) == 1
    assert "FAILED" in capsys.readouterr().out


def test_watch_once_prints_and_does_not_need_credentials(run_dir, monkeypatch, capsys):
    monkeypatch.setattr(cli.notify, "load_env", lambda *a, **k: {})
    monkeypatch.setattr(cli.watcher.AgentReporter, "write", lambda self, ctx: "STATUS moving")
    assert main(["watch", "--once", "--console"]) == 0
    assert "STATUS moving" in capsys.readouterr().out
    assert (run_dir / "reporter.json").exists()


def test_watch_once_on_a_run_that_never_started(repo, monkeypatch, capsys):
    monkeypatch.chdir(repo)
    (repo / ".ouroboros" / "config.yml").write_text(Config(run="ghost").dump())
    assert main(["watch", "--once", "--console"]) == 2
    assert "nothing to report on" in capsys.readouterr().err


def test_watch_refuses_a_second_reporter(run_dir, monkeypatch, capsys):
    (run_dir / "watch.pid").write_text(str(os.getpid()))
    monkeypatch.setattr(cli.notify, "load_env", lambda *a, **k: {"PO_USER": "u", "PO_TOKEN": "t"})
    assert main(["watch"]) == 1
    assert "already watching" in capsys.readouterr().err
    monkeypatch.setattr(cli.watcher.AgentReporter, "write", lambda self, ctx: "still fine")
    assert main(["watch", "--once", "--console"]) == 0      # a one-shot report is always allowed


def test_a_stale_pid_is_not_a_live_reporter(run_dir):
    (run_dir / "watch.pid").write_text("999999")
    assert cli.reporter_alive(run_dir) is None
    (run_dir / "watch.pid").write_text("not a pid")
    assert cli.reporter_alive(run_dir) is None


def test_run_launches_the_reporter_beside_the_loop(run_dir, monkeypatch, capsys):
    monkeypatch.setattr(cli.notify, "load_env", lambda *a, **k: {"PO_USER": "u", "PO_TOKEN": "t"})
    launched = []
    monkeypatch.setattr(cli.tmux, "launch_window",
                        lambda session, window, argv, cwd: launched.append((session, window, argv)) or "s:@9")
    cfg = Config(run="r")
    cli._launch_reporter(cfg, run_dir.parents[2], "s", "/bin/ouroboros")
    assert launched == [("s", "ouroboros-r-watch", ["/bin/ouroboros", "watch", "--run-name", "r"])]
    assert (run_dir / "tmux-watch").read_text().strip() == "window s:@9"
    out = capsys.readouterr().out
    assert "reporter: pushover, a digest every 4h" in out and "needs_human" in out


def test_run_says_why_the_reporter_is_off(run_dir, monkeypatch, capsys):
    monkeypatch.setattr(cli.notify, "load_env", lambda *a, **k: {})
    monkeypatch.setattr(cli.tmux, "launch_window", lambda *a, **k: pytest.fail("must not start a reporter"))
    cli._launch_reporter(Config(run="r"), run_dir.parents[2], "s", "/bin/ouroboros")
    assert "reporter: off (no Pushover credentials" in capsys.readouterr().out


def test_run_does_not_start_a_second_reporter(run_dir, monkeypatch, capsys):
    (run_dir / "watch.pid").write_text(str(os.getpid()))
    monkeypatch.setattr(cli.notify, "load_env", lambda *a, **k: {"PO_USER": "u", "PO_TOKEN": "t"})
    monkeypatch.setattr(cli.tmux, "launch_window", lambda *a, **k: pytest.fail("must not start a second reporter"))
    cli._launch_reporter(Config(run="r"), run_dir.parents[2], "s", "/bin/ouroboros")
    assert "already watching" in capsys.readouterr().out


def test_launch_failure_is_a_warning_not_a_refused_run(run_dir, monkeypatch, capsys):
    monkeypatch.setattr(cli.notify, "load_env", lambda *a, **k: {"PO_USER": "u", "PO_TOKEN": "t"})

    def boom(*a, **k):
        raise OSError("tmux said no")

    monkeypatch.setattr(cli.tmux, "launch_window", boom)
    cli._launch_reporter(Config(run="r"), run_dir.parents[2], "s", "/bin/ouroboros")
    assert "could not start" in capsys.readouterr().err


def test_stop_takes_the_reporter_down_after_the_loop(run_dir, monkeypatch):
    """The reporter gets the SIGTERM, a moment to push the last word, then its window."""
    (run_dir / "watch.pid").write_text("4242")
    (run_dir / "tmux-watch").write_text("window s:@9\n")
    signalled, killed = [], []
    monkeypatch.setattr(cli.os, "kill", lambda pid, sig: signalled.append((pid, sig)) if sig else (_ for _ in ()).throw(OSError))
    monkeypatch.setattr(cli.tmux, "kill_window", lambda target: killed.append(target))
    monkeypatch.setattr(cli.tmux, "kill", lambda name: killed.append(name))
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    cli._stop_reporter(run_dir)
    assert signalled[0] == (4242, cli.signal.SIGTERM) and killed == ["s:@9"]


def test_stop_without_a_reporter_is_quiet(run_dir, monkeypatch):
    monkeypatch.setattr(cli.tmux, "kill_window", lambda target: pytest.fail("no window to kill"))
    cli._stop_reporter(run_dir)
