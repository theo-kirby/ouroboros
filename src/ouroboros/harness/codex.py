"""OpenAI Codex CLI driver: `codex exec --json`.

Facts this driver relies on (codex-rs, checked against 0.153):
- one JSON object per line: thread.started {thread_id} -> item.completed {item:{type:"agent_message",text}}
  -> turn.completed {usage} ; fatal: {"type":"error","message"} then {"type":"turn.failed","error":{"message"}}
- retries also go through "error" events as "Reconnecting... N/M (...)" and are not fatal
- a plan limit prints "You've hit your usage limit. ... Try again at 3:45 PM." (local time, no zone)
- exit code 1 on a fatal error, 0 otherwise
- ~/.codex/sessions/**/rollout-*.jsonl logs token_count events with rate_limits.{primary,secondary}.resets_at
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import time
from pathlib import Path

from .backend_headless import run_subprocess
from .base import Result

_LIMIT = re.compile(
    r"You've hit your usage limit|out of credits|spend cap|Quota exceeded|usage_not_included|upgrade to Plus|"
    r"exceeded retry limit, last status: 429|usage_limit_reached",
    re.IGNORECASE,
)
_AUTH = re.compile(r"refresh token|sign in again|login is required|Unauthorized|not logged in", re.IGNORECASE)
_TRANSIENT = re.compile(r"high demand|at capacity|rate limit exceeded|stream disconnected|Reconnecting", re.IGNORECASE)
_RECONNECT = re.compile(r"^Reconnecting", re.IGNORECASE)


class CodexHarness:
    name = "codex"

    def __init__(self, binary: str = "codex", home: Path | None = None) -> None:
        self.binary = shutil.which(binary) or binary
        self.home = home or Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")

    def build_cmd(
        self, *, cwd: Path, resume: str | None, model: str | None,
        tools: str | None = None, schema_path: Path | None = None,
    ) -> list[str]:
        cmd = [self.binary, "exec"]
        if resume:
            cmd += ["resume", resume]
        cmd += ["--json", "--skip-git-repo-check"]
        if tools in ("none", "readonly"):
            if not resume:
                cmd += ["-s", "read-only"]     # exec never asks for approval; read-only sandbox stops writes
        else:
            cmd += ["--dangerously-bypass-approvals-and-sandbox"]
        if not resume:
            cmd += ["-C", str(cwd)]
        if model:
            cmd += ["-m", model]
        if schema_path:
            cmd += ["--output-schema", str(schema_path)]
        cmd.append("-")   # prompt on stdin
        return cmd

    def run(
        self,
        prompt: str,
        *,
        cwd: Path,
        timeout: float,
        resume: str | None = None,
        model: str | None = None,
        system_append: str | None = None,
        log_path: Path | None = None,
        tools: str | None = None,
        json_schema: dict | None = None,
        max_turns: int | None = None,
    ) -> Result:
        if system_append:
            prompt = f"<system>\n{system_append}\n</system>\n\n{prompt}"
        schema_path: Path | None = None
        if json_schema:
            fd, name = tempfile.mkstemp(prefix="ouroboros-schema-", suffix=".json")
            with os.fdopen(fd, "w") as f:
                json.dump(json_schema, f)
            schema_path = Path(name)
        try:
            cmd = self.build_cmd(cwd=cwd, resume=resume, model=model, tools=tools, schema_path=schema_path)
            proc = run_subprocess(cmd, cwd=cwd, timeout=timeout, stdin_text=prompt, log_path=log_path)
        finally:
            if schema_path:
                schema_path.unlink(missing_ok=True)
        result = self.parse(proc.stdout, proc.stderr, proc.exit_code, proc.timed_out, log_path)
        if result.kind == "limit" and result.reset_at() is None:
            at = self.limit_reset_from_rollouts()
            if at:
                result.extra["resets_at"] = at
        return result

    @staticmethod
    def parse(stdout: str, stderr: str, exit_code: int, timed_out: bool, raw_path: Path | None) -> Result:
        session_id: str | None = None
        texts: list[str] = []
        errors: list[str] = []
        usage: dict = {}
        turns = 0
        for line in stdout.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = ev.get("type")
            if t == "thread.started":
                session_id = ev.get("thread_id") or session_id
            elif t == "item.completed":
                item = ev.get("item") or {}
                if item.get("type") == "agent_message" and item.get("text"):
                    texts.append(item["text"])
                    turns += 1
            elif t == "error":
                msg = str(ev.get("message") or "")
                if msg and not _RECONNECT.match(msg):
                    errors.append(msg)
            elif t == "turn.failed":
                msg = str((ev.get("error") or {}).get("message") or "turn failed")
                errors.append(msg)
            elif t == "turn.completed":
                for k, v in (ev.get("usage") or {}).items():
                    if isinstance(v, (int, float)):
                        usage[k] = usage.get(k, 0) + v
        text = texts[-1] if texts else ""
        error: str | None = None
        if exit_code != 0 or errors or timed_out:
            error = "; ".join(dict.fromkeys(errors)) or stderr.strip() or (None if timed_out else f"codex exit {exit_code}")
        elif not stdout.strip():
            error = stderr.strip() or "no JSON output"
        extra: dict = {"usage": usage}
        blob = f"{error or ''}\n{stderr[-1000:]}"
        if error:
            if _LIMIT.search(blob):
                extra["kind"] = "limit"
            elif _AUTH.search(blob):
                extra["kind"] = "auth"
            elif _TRANSIENT.search(blob):
                extra["kind"] = "transient"
        return Result(
            text=text, session_id=session_id, cost_usd=None, turns=turns or None,
            exit_code=exit_code if (exit_code != 0 or not error) else 1,
            raw_path=raw_path, timed_out=timed_out, error=error, extra=extra,
        )

    def limit_reset_from_rollouts(self, *, now: float | None = None) -> float | None:
        """The reset epoch of the exhausted window, from the newest rollout's last token_count event."""
        now = now or time.time()
        root = self.home / "sessions"
        try:
            files = sorted(root.rglob("rollout-*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)[:3]
        except OSError:
            return None
        for path in files:
            try:
                lines = path.read_text(errors="replace").splitlines()
            except OSError:
                continue
            for line in reversed(lines[-400:]):
                if '"token_count"' not in line or '"rate_limits"' not in line:
                    continue
                try:
                    limits = json.loads(line)["payload"]["rate_limits"]
                except (json.JSONDecodeError, KeyError, TypeError):
                    continue
                return rate_limits_reset(limits, now=now)
        return None


def rate_limits_reset(limits: dict, *, now: float) -> float | None:
    """From codex rate_limits {primary:{used_percent,resets_at}, secondary:{...}}: when usage frees up."""
    windows = [w for w in (limits.get("primary"), limits.get("secondary")) if isinstance(w, dict)]
    future = [w for w in windows if (w.get("resets_at") or 0) > now]
    exhausted = [w for w in future if (w.get("used_percent") or 0) >= 99]
    if exhausted:
        return float(max(w["resets_at"] for w in exhausted))
    primary = limits.get("primary") or {}
    if (primary.get("resets_at") or 0) > now:   # the 5-hour window is the best guess when nobody is at 100%
        return float(primary["resets_at"])
    return None
