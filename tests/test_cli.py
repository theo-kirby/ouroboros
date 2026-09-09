import pytest
import sys

from ouroboros import cli
from ouroboros.cli import main
from ouroboros.banner import BANNER


def test_no_arguments_show_art_and_menu_without_reading_pipe(capsys, monkeypatch):
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr("builtins.input", lambda _: pytest.fail("must not read piped input"))
    assert main([]) == 0
    out, err = capsys.readouterr()
    assert out == BANNER + "\nan infinite loop\n\n" + cli.MENU_TEXT + "\n"
    assert "usage:" not in out
    assert not err


@pytest.mark.parametrize("choice, handler, expected", [
    ("1", "cmd_init", {"cmd": "init", "name": None, "force": False}),
    ("2", "cmd_design", {"cmd": "design", "harness": None}),
    ("3", "cmd_run", {"cmd": "run"}),
    ("4", "cmd_status", {"cmd": "status", "watch": True}),
])
def test_menu_dispatches_selection(monkeypatch, capsys, choice, handler, expected):
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: True)
    answers = iter(["invalid", choice])
    prompts = []

    def read_choice(prompt):
        prompts.append(prompt)
        return next(answers)

    monkeypatch.setattr("builtins.input", read_choice)
    calls = []

    def selected(args):
        calls.append(vars(args))
        return 7

    monkeypatch.setattr(cli, handler, selected)
    assert main([]) == 7
    assert len(calls) == 1
    assert expected.items() <= calls[0].items()
    assert prompts == ["", ""]
    assert "Choose" not in capsys.readouterr().out


@pytest.mark.parametrize("error", [EOFError, KeyboardInterrupt])
def test_menu_can_be_cancelled(monkeypatch, error):
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: True)

    def cancel(_):
        raise error

    monkeypatch.setattr("builtins.input", cancel)
    assert main([]) == 0


def test_monitor_can_be_interrupted(monkeypatch):
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: "4")

    def monitor(_):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "cmd_status", monitor)
    assert main([]) == 0


@pytest.mark.parametrize("option", ["--help", "--version"])
def test_explicit_information_has_no_banner(option, capsys):
    with pytest.raises(SystemExit) as exc:
        main([option])
    assert exc.value.code == 0
    assert BANNER.strip() not in capsys.readouterr().out


def test_unknown_command_still_fails(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["unknown"])
    assert exc.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


# ---------- design command, skill targets, preflight ----------
from pathlib import Path

from ouroboros.config import Config


def test_design_command_shapes(tmp_path):
    skill = tmp_path / "ouroboros-design"
    skill.mkdir()
    (skill / "SKILL.md").write_text("---\nname: ouroboros-design\n---\n# Design\nAsk things.\n")
    assert cli.design_command("claude", skill, True) == ["claude", "/ouroboros-design"]
    assert cli.design_command("codex", skill, True) == ["codex", "$ouroboros-design"]
    inline = cli.design_command("codex", skill, False)[1]
    assert inline.startswith("Follow this skill now") and "Ask things." in inline and "name: ouroboros-design" not in inline
    pi = cli.design_command("pi", skill, False)
    assert pi[:3] == ["pi", "--skill", str(skill)]
    with pytest.raises(ValueError):
        cli.design_command("gemini", skill, False)


def test_pick_harness_prefers_the_actor_chain(monkeypatch):
    cfg = Config(run="t")
    cfg.roles["actor"].harness = "codex"
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/bin/x" if name in ("codex", "pi") else None)
    assert cli.pick_harness(cfg, None) == "codex"
    assert cli.pick_harness(cfg, "pi") == "pi"
    assert cli.pick_harness(cfg, "claude") is None
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    assert cli.pick_harness(cfg, None) is None


def test_skill_targets_follow_installed_harnesses(tmp_path):
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".pi" / "agent").mkdir(parents=True)
    got = cli.skill_targets(user=True, home=tmp_path)
    assert got == [tmp_path / ".claude/skills", tmp_path / ".pi/agent/skills", tmp_path / ".agents/skills"]
    assert cli.skill_targets(user=True, home=tmp_path / "empty") == [tmp_path / "empty/.agents/skills"]
    assert cli.skill_targets(user=False) == cli.PROJECT_SKILL_DIRS


def _git_repo(path: Path) -> None:
    import subprocess
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    (path / "README.md").write_text("x")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"], check=True)


def test_preflight_refuses_the_template_charter(tmp_path, monkeypatch, capsys):
    _git_repo(tmp_path)
    (tmp_path / ".ouroboros").mkdir()
    goal = tmp_path / ".ouroboros" / "goal.md"
    goal.write_text(cli.GOAL_TEMPLATE.format(name="t"))
    import subprocess
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "goal"], check=True)
    cfg = Config(run="t")
    problem = cli.preflight(cfg, tmp_path)
    assert problem and "not filled in" in problem and "mission" in problem and "ouroboros design" in problem


def test_preflight_reports_logins(tmp_path, monkeypatch, capsys):
    _git_repo(tmp_path)
    (tmp_path / ".ouroboros").mkdir()
    (tmp_path / ".ouroboros" / "goal.md").write_text(
        "# Goal: t\n\n## Mission\n\nShip it.\n\n## Done criteria\n\n- [ ] tests pass\n\n"
        "## Horizon ladder\n\n- **short-term:** fix the build.\n- **long-term:** keep it green.\n"
    )
    import subprocess
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "goal"], check=True)
    cfg = Config(run="t")
    monkeypatch.setattr(cli.login, "check_roles", lambda chains: (["actor: no usable harness — claude: not logged in"], ["codex: Logged in"]))
    problem = cli.preflight(cfg, tmp_path)
    assert problem.startswith("not logged in: actor")
    assert "codex: Logged in" in capsys.readouterr().err
    monkeypatch.setattr(cli.login, "check_roles", lambda chains: ([], ["claude: logged in as t"]))
    assert cli.preflight(cfg, tmp_path) is None


# -- where the loop lives in tmux -------------------------------------------

def test_run_takes_a_window_of_the_session_it_is_started_from(tmp_path, monkeypatch, capsys):
    from ouroboros import cli, tmux
    from ouroboros.config import Config
    calls = []
    monkeypatch.setattr(tmux, "current_session", lambda: "cadxot5")
    monkeypatch.setattr(tmux, "launch_window", lambda s, w, argv, cwd: (calls.append(("window", s, w)), "cadxot5:@7")[1])
    monkeypatch.setattr(tmux, "launch", lambda n, argv, cwd: calls.append(("session", n)))
    monkeypatch.setattr(sys, "argv", ["ouroboros", "run", "--for", "24h"])
    cfg = Config(run="ot5")
    assert cli._launch_in_tmux(cfg, tmp_path, own_session=False) == 0
    assert calls == [("window", "cadxot5", "ouroboros-ot5")]
    assert cli._tmux_target(tmp_path / ".ouroboros" / "runs" / "ot5") == ("window", "cadxot5:@7")
    assert "window ouroboros-ot5 of this tmux session (cadxot5)" in capsys.readouterr().out


def test_run_gets_its_own_session_outside_tmux_or_on_request(tmp_path, monkeypatch, capsys):
    from ouroboros import cli, tmux
    from ouroboros.config import Config
    calls = []
    monkeypatch.setattr(tmux, "current_session", lambda: "cadxot5")
    monkeypatch.setattr(tmux, "session_exists", lambda n: False)
    monkeypatch.setattr(tmux, "launch", lambda n, argv, cwd: calls.append(n))
    monkeypatch.setattr(sys, "argv", ["ouroboros", "run"])
    assert cli._launch_in_tmux(Config(run="ot5"), tmp_path, own_session=True) == 0
    monkeypatch.setattr(tmux, "current_session", lambda: None)
    assert cli._launch_in_tmux(Config(run="ot6"), tmp_path, own_session=False) == 0
    assert calls == ["ouroboros-ot5", "ouroboros-ot6"]
    assert cli._tmux_target(tmp_path / ".ouroboros" / "runs" / "ot6") == ("session", "ouroboros-ot6")


def test_stop_kills_the_window_it_took_and_never_the_operators_session(tmp_path, monkeypatch):
    from ouroboros import cli, tmux
    from ouroboros.config import Config
    killed = []
    monkeypatch.setattr(tmux, "kill_window", lambda t: killed.append(("window", t)))
    monkeypatch.setattr(tmux, "kill", lambda n: killed.append(("session", n)))
    rd = tmp_path / "r"
    cli._tmux_note(rd, "window", "cadxot5:@7")
    cli._tmux_kill(Config(run="ot5"), rd)
    cli._tmux_kill(Config(run="old"), tmp_path / "nothing-here")   # a run from before the note existed
    assert killed == [("window", "cadxot5:@7"), ("session", "ouroboros-old")]
