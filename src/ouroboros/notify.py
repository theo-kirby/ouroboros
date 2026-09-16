"""Push a message to a phone. Pushover today; the interface is one method.

Credentials come from the environment, or from a `.env` beside the target repo
or under `~/.ouroboros/`, because a run happens in the target repo and the
operator's own dotfiles live somewhere else. `PO_USER`/`PO_TOKEN` are read as
well as `PUSHOVER_USER`/`PUSHOVER_TOKEN`. A notifier never raises: a phone that
cannot be reached is a log line, not a reason for the watcher to die.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Protocol

PUSHOVER_URL = "https://api.pushover.net/1/messages.json"
TITLE_LIMIT = 250
MESSAGE_LIMIT = 1024

USER_KEYS = ("PUSHOVER_USER", "PO_USER")
TOKEN_KEYS = ("PUSHOVER_TOKEN", "PO_TOKEN", "PO_APP_TOKEN")


def env_files(repo: Path, home: Path | None = None) -> list[Path]:
    """Where credentials may live, most specific first."""
    home = home or Path.home()
    return [repo / ".env", repo / ".ouroboros" / ".env", home / ".ouroboros" / ".env"]


def parse_env(text: str) -> dict[str, str]:
    """KEY=VALUE lines; `export` prefixes, quotes, blank lines and comments tolerated."""
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            out[key] = value
    return out


def load_env(paths: list[Path], environ: dict[str, str] | None = None) -> dict[str, str]:
    """The process environment with each file's values filled in underneath it.

    A variable already exported wins over every file; the first file that sets a
    name wins over the files after it.
    """
    env = dict(os.environ if environ is None else environ)
    for path in paths:
        try:
            values = parse_env(path.read_text())
        except OSError:
            continue
        for key, value in values.items():
            env.setdefault(key, value)
    return env


def _first(env: dict[str, str], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = (env.get(key) or "").strip()
        if value:
            return value
    return None


def pushover_credentials(env: dict[str, str]) -> tuple[str, str] | None:
    """(user key, application token), or None when either is missing."""
    user, token = _first(env, USER_KEYS), _first(env, TOKEN_KEYS)
    return (user, token) if user and token else None


def missing_credentials(env: dict[str, str]) -> str:
    """Which of the two Pushover secrets is not set, for the operator's eyes."""
    missing = []
    if not _first(env, USER_KEYS):
        missing.append("PO_USER (your user key)")
    if not _first(env, TOKEN_KEYS):
        missing.append("PO_TOKEN (an application token from pushover.net/apps/build)")
    return " and ".join(missing)


def clip(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


class Notifier(Protocol):
    name: str

    def send(self, title: str, message: str, *, priority: int = 0) -> bool: ...


class Pushover:
    """One HTTP POST per message. Priority: -1 quiet, 0 normal, 1 breaks through quiet hours."""

    name = "pushover"

    def __init__(self, user: str, token: str, *, log=None, opener=None, timeout: float = 20.0) -> None:
        self.user, self.token = user, token
        self.log = log or (lambda line: None)
        self.opener = opener or urllib.request.urlopen
        self.timeout = timeout

    def send(self, title: str, message: str, *, priority: int = 0) -> bool:
        payload = {
            "token": self.token, "user": self.user,
            "title": clip(title, TITLE_LIMIT), "message": clip(message, MESSAGE_LIMIT) or "(empty)",
            "priority": max(-2, min(1, int(priority))),
        }
        req = urllib.request.Request(PUSHOVER_URL, data=urllib.parse.urlencode(payload).encode(),
                                     headers={"Content-Type": "application/x-www-form-urlencoded"})
        try:
            with self.opener(req, timeout=self.timeout) as resp:
                body = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", errors="replace")[:300]
            except Exception:
                pass
            self.log(f"pushover: HTTP {exc.code} {detail}".strip())
            return False
        except Exception as exc:   # DNS, timeout, no network: all the same to the watcher
            self.log(f"pushover: {exc!r}")
            return False
        try:
            ok = int((json.loads(body) or {}).get("status", 0)) == 1
        except (ValueError, TypeError):
            ok = False
        if not ok:
            self.log(f"pushover: rejected: {body[:300]}")
        return ok


class Console:
    """No phone: print what would have been pushed. `notify: none`, and tests."""

    name = "console"

    def __init__(self, out=print) -> None:
        self.out = out
        self.sent: list[tuple[str, str, int]] = []

    def send(self, title: str, message: str, *, priority: int = 0) -> bool:
        self.sent.append((title, message, priority))
        try:
            self.out(f"--- {title} (priority {priority})\n{message}\n---")
        except (OSError, ValueError):
            pass
        return True


def make_notifier(kind: str, env: dict[str, str], *, log=None) -> tuple[Notifier | None, str]:
    """(notifier, why) for a `report.notify` setting. None means the reporter has no channel."""
    kind = (kind or "auto").lower()
    creds = pushover_credentials(env)
    if kind == "none":
        return None, "notify: none"
    if kind in ("auto", "pushover"):
        if creds:
            return Pushover(*creds, log=log), "pushover"
        why = f"no Pushover credentials: set {missing_credentials(env)}"
        if kind == "pushover":
            return None, why
        return None, f"{why} (report.notify is auto)"
    if kind == "console":
        return Console(), "console"
    return None, f"unknown report.notify {kind!r}; known: auto, pushover, console, none"
