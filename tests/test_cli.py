import pytest

from ouroboros import cli
from ouroboros.cli import main
from ouroboros.banner import BANNER


def test_no_arguments_show_art_and_menu_without_reading_pipe(capsys, monkeypatch):
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr("builtins.input", lambda _: pytest.fail("must not read piped input"))
    assert main([]) == 0
    out, err = capsys.readouterr()
    assert out == BANNER + "\nan infinite loop\n\n1. init\n2. monitor\n\n"
    assert "usage:" not in out
    assert not err


@pytest.mark.parametrize("choice, handler, expected", [
    ("1", "cmd_init", {"cmd": "init", "name": None, "force": False}),
    ("2", "cmd_status", {"cmd": "status", "watch": True}),
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
    monkeypatch.setattr("builtins.input", lambda _: "2")

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
