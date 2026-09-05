"""Everything the morning read needs: JSONL logs, status.json, NEEDS_HUMAN.md."""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path


def _ts() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Recorder:
    def __init__(self, run_dir: Path, *, echo=print) -> None:
        self.run_dir = run_dir
        self.echo = echo
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "transcripts").mkdir(exist_ok=True)
        (run_dir / "reverted").mkdir(exist_ok=True)
        self.iterations = run_dir / "iterations.jsonl"
        self.overseer = run_dir / "overseer.jsonl"
        self.status_path = run_dir / "status.json"
        self.needs_human_path = run_dir / "NEEDS_HUMAN.md"
        self.log_path = run_dir / "loop.log"

    def _append(self, path: Path, record: dict) -> None:
        record = {"ts": _ts(), **record}
        with path.open("a") as f:
            f.write(json.dumps(record, default=str) + "\n")

    def step(self, **record) -> None:
        self._append(self.iterations, record)
        self.log(f"[{record.get('iteration', '-')}] {record.get('step', '?')}: " + ", ".join(
            f"{k}={v}" for k, v in record.items() if k not in {"iteration", "step"} and v is not None
        ))

    def decision(self, **record) -> None:
        self._append(self.overseer, record)

    def log(self, line: str) -> None:
        stamped = f"{_ts()} {line}"
        with self.log_path.open("a") as f:
            f.write(stamped + "\n")
        self.echo(stamped)

    def status(self, **fields) -> None:
        data = {"ts": _ts(), "epoch": time.time(), **fields}
        tmp = self.status_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, default=str))
        os.replace(tmp, self.status_path)

    def read_status(self) -> dict | None:
        if not self.status_path.exists():
            return None
        return json.loads(self.status_path.read_text())

    def needs_human(self, text: str) -> None:
        self.needs_human_path.write_text(f"# Needs a human\n\n{_ts()}\n\n{text}\n")
        self.log(f"NEEDS_HUMAN: {text.splitlines()[0] if text else ''}")

    def clear_needs_human(self) -> None:
        if self.needs_human_path.exists():
            self.needs_human_path.unlink()

    def transcript_path(self, iteration: int, role: str, attempt: int = 0) -> Path:
        suffix = f"-{attempt}" if attempt else ""
        return self.run_dir / "transcripts" / f"{iteration:04d}-{role}{suffix}.json"

    def read_jsonl(self, path: Path) -> list[dict]:
        if not path.exists():
            return []
        return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
