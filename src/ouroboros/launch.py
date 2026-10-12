"""Where the loop lives: a launcher puts `ouroboros run` somewhere that outlives the terminal.

Four places, chosen by `launcher:` in the config (or `--launcher`):

- **tmux**: a window of the operator's tmux session, or a session of its own
  (:mod:`ouroboros.tmux`). The original, and still the default outside tern.
- **tern**: a session of its own in the tern daemon, named ``ouroboros-<run>``,
  beside the operator's sessions; the reporter gets a tab in it.
- **systemd**: a transient user service, ``ouroboros-<run>.service``. It belongs
  to no terminal or multiplexer, so a restart of either cannot take it down;
  with lingering on it survives logout; and ``memory_max`` caps the run and every
  process it starts, so a runaway child is killed alone instead of taking the
  operator's session with it. Output goes to the journal as well as the run
  directory.
- **foreground**: right here, as ``--foreground`` always did.

``auto`` picks tmux when this process runs inside tmux, tern when it runs inside
a tern pane, tmux when tmux is installed, and the foreground otherwise. Wherever
it goes, the launched loop sees ``OUROBOROS_LAUNCHED=1`` and runs in place.
`run` writes ``runs/<run>/launcher`` (tern and systemd; tmux keeps its own
``runs/<run>/tmux`` note) so `stop` takes down exactly what `run` started.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

from . import tmux

LAUNCHERS = ("auto", "tmux", "tern", "systemd", "foreground")
LAUNCHED_ENV = "OUROBOROS_LAUNCHED"

# What a systemd unit must not inherit from the shell that started it: the
# terminal and multiplexer it is leaving, and the markers of an agent session
# (a harness started under them can think it is nested inside one).
_DROP_PREFIXES = ("TMUX", "TERN_", "SSH_", "CLAUDE_CODE_", "OUROBOROS_")
_DROP_NAMES = {"TERM", "TERM_PROGRAM", "TERM_PROGRAM_VERSION", "COLORTERM", "PWD", "OLDPWD",
               "SHLVL", "_", "LS_COLORS", "CLAUDECODE", "STY", "WINDOW"}


def launched() -> bool:
    """True inside a loop some launcher already started: run in place, never launch again."""
    return os.environ.get(LAUNCHED_ENV) == "1" or tmux.inside_ouroboros_tmux()


def resolve(name: str, env: dict[str, str] | None = None) -> str:
    """The launcher `auto` stands for here; any other name is returned as it is."""
    if name != "auto":
        return name
    env = os.environ if env is None else env
    if env.get("TMUX") and tmux.available():
        return "tmux"
    if env.get("TERN_PANE") and shutil.which("tern"):
        return "tern"
    if tmux.available():
        return "tmux"
    return "foreground"


def problem(kind: str) -> str | None:
    """Why `kind` cannot launch on this machine, or None."""
    if kind == "tmux" and not tmux.available():
        return "launcher tmux: tmux is not installed"
    if kind == "tern" and not shutil.which("tern"):
        return "launcher tern: tern is not on PATH"
    if kind == "systemd":
        if not shutil.which("systemd-run"):
            return "launcher systemd: systemd-run is not on PATH"
        proc = subprocess.run(["systemctl", "--user", "is-system-running"], capture_output=True, text=True)
        if proc.returncode != 0 and proc.stdout.strip() in ("", "offline", "unknown"):
            return "launcher systemd: no user service manager (systemctl --user) is reachable"
    return None


def name_for(run: str) -> str:
    """``ouroboros-<run>``: the tern session and the systemd unit's stem, as tmux names its session."""
    return tmux.session_name(run)


def program(argv0: str) -> str:
    """An absolute path to the ouroboros entry point, for a manager that does not share our PATH."""
    if os.sep in argv0:
        return os.path.abspath(argv0)
    return shutil.which(argv0) or argv0


# -- notes: what `run` started, for `stop` --------------------------------


def write_note(run_dir: Path, kind: str, target: str, *, watch: bool = False) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / ("launcher-watch" if watch else "launcher")).write_text(f"{kind} {target}\n")


def read_note(run_dir: Path, *, watch: bool = False) -> tuple[str, str] | None:
    """('tern', 'ouroboros-x'), ('tern-tab', '1234'), ('systemd', 'ouroboros-x.service'), or None."""
    path = run_dir / ("launcher-watch" if watch else "launcher")
    if not path.exists():
        return None
    kind, _, target = path.read_text().strip().partition(" ")
    return (kind, target) if kind in ("tern", "tern-tab", "systemd") and target else None


def kill_note(note: tuple[str, str] | None) -> None:
    """Take down what a note names. A target already gone is not an error."""
    if note is None:
        return
    kind, target = note
    if kind == "tern":
        subprocess.run(["tern", "kill", "session", target], capture_output=True)
    elif kind == "tern-tab":
        subprocess.run(["tern", "close", target], capture_output=True)
    elif kind == "systemd":
        subprocess.run(["systemctl", "--user", "stop", target], capture_output=True)


# -- tern -----------------------------------------------------------------


def tern_sessions() -> list[str]:
    proc = subprocess.run(["tern", "ls", "--json"], capture_output=True, text=True)
    if proc.returncode != 0:
        return []
    try:
        return [s.get("name") for s in json.loads(proc.stdout).get("sessions", [])]
    except (ValueError, AttributeError):
        return []


def _tern_launch(args: list[str]) -> dict:
    """`tern new ...` with --json ahead of the `--`: after it, the flag would reach the program."""
    proc = subprocess.run(["tern", "new", "--json", *args], capture_output=True, text=True, check=True)
    return json.loads(proc.stdout)


def tern_session(name: str, argv: list[str], cwd: str) -> str:
    """Start `argv` as the one block of a new tern session. Returns the session's name.

    `--keep-open` leaves the block on screen after the loop exits, so its last
    lines -- why it stopped -- are still there to read.
    """
    _tern_launch(["session", name, "--cwd", cwd, "--keep-open", "--",
                  "env", f"{LAUNCHED_ENV}=1", *argv])
    return name


def tern_tab(session: str, argv: list[str], cwd: str) -> str:
    """Start `argv` in a new tab of an existing tern session. Returns the block's id."""
    found = _tern_launch(["tab", session, "--cwd", cwd, "--keep-open", "--",
                          "env", f"{LAUNCHED_ENV}=1", *argv])
    return str(found["block"])


# -- systemd --------------------------------------------------------------


def unit_active(unit: str) -> bool:
    proc = subprocess.run(["systemctl", "--user", "is-active", unit], capture_output=True, text=True)
    return proc.stdout.strip() in ("active", "activating", "reloading")


def unit_environment(env: dict[str, str] | None = None) -> dict[str, str]:
    """The shell's environment minus its terminal, so the unit runs as `run` would have here."""
    env = os.environ if env is None else env
    return {k: v for k, v in env.items()
            if k not in _DROP_NAMES and not k.startswith(_DROP_PREFIXES) and "\n" not in v and k.isidentifier()}


def systemd_unit(unit: str, argv: list[str], cwd: str, *, memory_max: str | None = None,
                 env: dict[str, str] | None = None) -> str:
    """Start `argv` as a transient user service. Returns the unit's name.

    `--collect` lets systemd forget the unit once it exits, even on failure, so
    the next `run` of the same name can start it again; the journal keeps its
    output (`journalctl --user -u <unit>`).
    """
    cmd = ["systemd-run", "--user", "--unit", unit, "--collect", "--quiet",
           "-p", f"WorkingDirectory={cwd}"]
    if memory_max:
        cmd += ["-p", f"MemoryMax={memory_max}", "-p", "MemorySwapMax=0"]
    for key, value in sorted(unit_environment(env).items()):
        cmd.append(f"--setenv={key}={value}")
    cmd += [f"--setenv={LAUNCHED_ENV}=1", "--", *argv]
    subprocess.run(cmd, capture_output=True, text=True, check=True)
    return unit
