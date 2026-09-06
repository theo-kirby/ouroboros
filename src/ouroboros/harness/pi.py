"""Pi coding agent driver: `pi -p --mode json`.

Facts this driver relies on (pi-mono, checked against 0.84):
- one JSON object per line: session {id} -> agent_start -> ... message_end {message} ... -> agent_end
- an assistant message has usage.cost.total (pi prices every model) and, on failure,
  stopReason:"error" + errorMessage (the provider text passes through; the openai-codex
  provider rewrites limits as "You have hit your ChatGPT usage limit (...). Try again in ~N min.")
- auto_retry_start / auto_retry_end events wrap pi's own retries
- the process exits 0 even on a provider error in json mode
- --session-id <id> creates or resumes a session with that exact id
"""

from __future__ import annotations

import json
import re
import shutil
import uuid
from pathlib import Path

from . import backend_headless as backend
from .base import Result

READONLY_TOOLS = "read,grep,find,ls"
_AUTH = re.compile(r"No API key for provider|credentials_not_configured|not_ready|unauthorized|401", re.IGNORECASE)


class PiHarness:
    name = "pi"

    def __init__(self, binary: str = "pi") -> None:
        self.binary = shutil.which(binary) or binary

    def build_cmd(
        self, *, session_id: str, model: str | None, system_append: str | None,
        tools: str | None = None,
    ) -> list[str]:
        cmd = [self.binary, "-p", "--mode", "json", "--session-id", session_id]
        if model:
            cmd += ["--model", model]
        if system_append:
            cmd += ["--append-system-prompt", system_append]
        if tools == "none":
            cmd += ["--no-tools"]
        elif tools == "readonly":
            cmd += ["--tools", READONLY_TOOLS]
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
        session_id = resume or str(uuid.uuid4())
        if json_schema:
            prompt += ("\n\nReply with one JSON object and nothing else. It must match this JSON schema:\n"
                       + json.dumps(json_schema))
        cmd = self.build_cmd(session_id=session_id, model=model, system_append=system_append, tools=tools)
        cmd += ["--", prompt]
        proc = backend.run(cmd, cwd=cwd, timeout=timeout, stdin_text="", log_path=log_path)
        result = self.parse(proc.stdout, proc.stderr, proc.exit_code, proc.timed_out, log_path)
        result.session_id = result.session_id or session_id
        return result

    @staticmethod
    def parse(stdout: str, stderr: str, exit_code: int, timed_out: bool, raw_path: Path | None) -> Result:
        session_id: str | None = None
        texts: list[str] = []
        errors: list[str] = []
        cost = 0.0
        saw_cost = False
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
            if t == "session":
                session_id = ev.get("id") or session_id
            elif t == "message_end":
                msg = ev.get("message") or {}
                if msg.get("role") != "assistant":
                    continue
                turns += 1
                parts = [c.get("text", "") for c in msg.get("content") or [] if isinstance(c, dict) and c.get("type") == "text"]
                if any(parts):
                    texts.append("".join(parts))
                total = ((msg.get("usage") or {}).get("cost") or {}).get("total")
                if isinstance(total, (int, float)):
                    cost += total
                    saw_cost = True
                if msg.get("stopReason") == "error":
                    errors.append(str(msg.get("errorMessage") or "provider error"))
                elif msg.get("stopReason") == "aborted":
                    errors.append("aborted")
            elif t == "auto_retry_end" and ev.get("success") is False:
                errors.append(str(ev.get("finalError") or "retries exhausted"))
        text = texts[-1] if texts else ""
        error: str | None = None
        # the last assistant message decides: a retry that later succeeded is not an error
        last_failed = bool(errors) and stdout.rstrip().splitlines()[-1:] and '"stopReason":"error"' in stdout[-4000:]
        if exit_code != 0:
            error = "; ".join(dict.fromkeys(errors)) or stderr.strip() or f"pi exit {exit_code}"
        elif last_failed or (errors and not text):
            error = "; ".join(dict.fromkeys(errors))
        elif not stdout.strip() and not timed_out:
            error = stderr.strip() or "no JSON output"
        extra: dict = {}
        if error and _AUTH.search(error) and "limit" not in error.lower():
            extra["kind"] = "auth"
        return Result(
            text=text, session_id=session_id, cost_usd=cost if saw_cost else None, turns=turns or None,
            exit_code=exit_code if (exit_code != 0 or not error) else 1,
            raw_path=raw_path, timed_out=timed_out, error=error, extra=extra,
        )
