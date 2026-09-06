"""Login checks, one per harness, run once before the first iteration.

A harness that is not logged in does not crash the loop: the engine marks
NEEDS_HUMAN and retries every ten minutes, forever. That is right at 3am and
wrong at the start, when the human is still at the keyboard. So `ouroboros run`
asks each harness in the config whether it is logged in and prints what is
missing before it detaches into tmux.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

TIMEOUT = 20.0

OK, MISSING, NOT_LOGGED_IN, UNKNOWN = "ok", "missing", "not_logged_in", "unknown"


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT)


def pi_provider(home: Path | None = None) -> str | None:
    """Pi's default provider from its settings file; the auth check is per provider."""
    path = (home or Path.home()) / ".pi" / "agent" / "settings.json"
    try:
        return json.loads(path.read_text()).get("defaultProvider") or None
    except (OSError, ValueError, AttributeError):
        return None


def check(harness: str, model: str | None = None, *, run=_run, home: Path | None = None) -> tuple[str, str]:
    """(status, detail). Status is ok | missing | not_logged_in | unknown. Never raises."""
    if shutil.which(harness) is None:
        return MISSING, f"{harness}: not on PATH"
    try:
        if harness == "claude":
            p = run(["claude", "auth", "status", "--json"])
            data = json.loads(p.stdout or "{}")
            if data.get("loggedIn"):
                who = data.get("email") or data.get("authMethod") or "?"
                plan = data.get("subscriptionType") or data.get("apiProvider") or ""
                return OK, f"claude: logged in as {who}" + (f" ({plan})" if plan else "")
            return NOT_LOGGED_IN, "claude: not logged in; run `claude auth login`"
        if harness == "codex":
            p = run(["codex", "login", "status"])
            out = (p.stdout + p.stderr).strip()
            if p.returncode == 0 and "logged in" in out.lower() and "not logged in" not in out.lower():
                return OK, f"codex: {out.splitlines()[0]}"
            return NOT_LOGGED_IN, f"codex: {out.splitlines()[0] if out else 'not logged in'}; run `codex login`"
        if harness == "pi":
            provider = pi_provider(home)
            if not provider:
                return UNKNOWN, "pi: no default provider in ~/.pi/agent/settings.json; cannot check login"
            p = run(["pi", "auth", "check", "--provider", provider, "--json"])
            try:
                data = json.loads(p.stdout.strip().splitlines()[-1])
            except (ValueError, IndexError):
                data = {}
            if data.get("status") == "ready":
                return OK, f"pi: provider {provider} ready ({data.get('authType', '?')})"
            return NOT_LOGGED_IN, f"pi: provider {provider} {data.get('reason') or data.get('status') or 'not ready'}; run `pi` and log in"
        return UNKNOWN, f"{harness}: no login check"
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        return UNKNOWN, f"{harness}: login check failed ({exc.__class__.__name__})"


def check_roles(chains: dict[str, list[tuple[str, str | None]]], *, checker=check) -> tuple[list[str], list[str]]:
    """(errors, notes) for {role: [(harness, model), ...]}.

    An error is a role with no harness that is logged in. A note is a role whose
    first harness is not usable (the run starts on the fallback) or a check that
    could not decide.
    """
    seen: dict[str, tuple[str, str]] = {}
    for chain in chains.values():
        for harness, model in chain:
            if harness not in seen:
                seen[harness] = checker(harness, model)
    errors, notes = [], []
    for role, chain in chains.items():
        statuses = [seen[h][0] for h, _ in chain]
        details = [seen[h][1] for h, _ in chain]
        if not chain:
            continue
        if not any(s in (OK, UNKNOWN) for s in statuses):
            errors.append(f"{role}: no usable harness — " + "; ".join(details))
        elif statuses[0] != OK:
            first_ok = next((chain[i][0] for i, s in enumerate(statuses) if s == OK), None)
            tail = f"; starting on {first_ok}" if first_ok else "; could not verify"
            notes.append(f"{role}: {details[0]}{tail}")
    for harness, (status, detail) in seen.items():
        if status == OK:
            notes.append(detail)
    return errors, notes
