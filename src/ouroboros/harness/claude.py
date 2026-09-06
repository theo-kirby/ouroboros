"""Claude Code driver: `claude -p --output-format stream-json --verbose`."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from . import backend_headless as backend
from .base import Result


class ClaudeHarness:
    name = "claude"

    def __init__(self, binary: str = "claude") -> None:
        self.binary = shutil.which(binary) or binary

    READONLY_TOOLS = "Read,Grep,Glob"

    def build_cmd(
        self, *, resume: str | None, model: str | None, system_append: str | None,
        tools: str | None = None, json_schema: dict | None = None, max_turns: int | None = None,
    ) -> list[str]:
        # stream-json in every backend: the transcript file grows while the call runs (the
        # status TUI tails it) and the last line is the same result object `json` gives.
        cmd = [self.binary, "-p", "--output-format", "stream-json", "--verbose", "--dangerously-skip-permissions"]
        if model:
            cmd += ["--model", model]
        if resume:
            cmd += ["--resume", resume]
        if system_append:
            cmd += ["--append-system-prompt", system_append]
        if tools == "none":
            cmd += ["--tools", ""]
        elif tools == "readonly":
            cmd += ["--tools", self.READONLY_TOOLS]
        if json_schema:
            cmd += ["--json-schema", json.dumps(json_schema)]
        if max_turns:
            cmd += ["--max-turns", str(max_turns)]
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
        cmd = self.build_cmd(resume=resume, model=model, system_append=system_append,
                             tools=tools, json_schema=json_schema, max_turns=max_turns)
        proc = backend.run(cmd, cwd=cwd, timeout=timeout, stdin_text=prompt, log_path=log_path)
        return self.parse(proc.stdout, proc.stderr, proc.exit_code, proc.timed_out, log_path)

    @staticmethod
    def parse(stdout: str, stderr: str, exit_code: int, timed_out: bool, raw_path: Path | None) -> Result:
        data: dict | None = None
        try:
            data = json.loads(stdout) if stdout.strip() else None
        except json.JSONDecodeError:
            # stream-ish or partial output: take the last JSON object we can find
            for line in reversed(stdout.splitlines()):
                line = line.strip()
                if line.startswith("{"):
                    try:
                        data = json.loads(line)
                        break
                    except json.JSONDecodeError:
                        continue
        if isinstance(data, dict):
            text = data.get("result") or ""
            structured = data.get("structured_output")
            if structured is not None and not text:
                text = json.dumps(structured)
            is_error = bool(data.get("is_error"))
            return Result(
                text=text if isinstance(text, str) else json.dumps(text),
                session_id=data.get("session_id"),
                cost_usd=data.get("total_cost_usd"),
                turns=data.get("num_turns"),
                exit_code=exit_code,
                raw_path=raw_path,
                timed_out=timed_out,
                error=(stderr.strip() or text or "claude reported is_error") if (is_error or exit_code != 0) else None,
                extra={"subtype": data.get("subtype")},
            )
        return Result(
            text=stdout,
            exit_code=exit_code,
            raw_path=raw_path,
            timed_out=timed_out,
            error=(stderr.strip() or "no JSON output") if (exit_code != 0 or timed_out or not stdout.strip()) else None,
        )
