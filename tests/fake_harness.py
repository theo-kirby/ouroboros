"""A scripted harness. Each behavior is a callable(cwd, prompt) -> Result."""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from ouroboros.harness.base import Result
from ouroboros.memory.handoff import HandoffMemory

Behavior = Callable[[Path, str], Result]


def _handoff(cwd: Path, did: str) -> None:
    mem = HandoffMemory(cwd / ".ouroboros")
    mem.ensure()
    mem.next_path().write_text(f"## Did\n\n{did}\n\n## Learned\n\n-\n\n## Assumed\n\n-\n\n## Next\n\nmore\n")
    with mem.journal.open("a") as f:
        f.write(f"\n## Iteration\n\n{did}\n")


def works(did: str = "did a unit") -> Behavior:
    def b(cwd: Path, prompt: str) -> Result:
        (cwd / "work.txt").open("a").write(did + "\n")
        _handoff(cwd, did)
        return Result(text=f"I {did}. Next: more.", session_id="s1", cost_usd=0.5, turns=3)
    return b


def records_only(did: str = "recorded the handoff") -> Behavior:
    """Writes a handoff and nothing else: a diff that is all bookkeeping."""
    def b(cwd: Path, prompt: str) -> Result:
        _handoff(cwd, did)
        return Result(text=f"I {did}. Next: more.", session_id="s1", cost_usd=0.5, turns=3)
    return b


def asks_question() -> Behavior:
    def b(cwd: Path, prompt: str) -> Result:
        _handoff(cwd, "paused to ask")
        return Result(text="I could use A or B. Which one do you want?", session_id="s1", cost_usd=0.2)
    return b


def claims_done() -> Behavior:
    def b(cwd: Path, prompt: str) -> Result:
        _handoff(cwd, "finished everything")
        return Result(text="All tasks are complete. Nothing left to do.", session_id="s1", cost_usd=0.2)
    return b


def crashes(msg: str = "boom") -> Behavior:
    return lambda cwd, prompt: Result(text="", exit_code=1, error=msg)


def rate_limited() -> Behavior:
    return lambda cwd, prompt: Result(text="", exit_code=1, error="429 rate limit exceeded")


def times_out() -> Behavior:
    return lambda cwd, prompt: Result(text="partial", timed_out=True)


def no_record() -> Behavior:
    def b(cwd: Path, prompt: str) -> Result:
        (cwd / "work.txt").open("a").write("forgot to record\n")
        return Result(text="did stuff, forgot the handoff", session_id="s1")
    return b


def raises() -> Behavior:
    def b(cwd: Path, prompt: str) -> Result:
        raise RuntimeError("driver exploded")
    return b


def nothing() -> Behavior:
    return lambda cwd, prompt: Result(text="looked around, changed nothing", session_id="s1")


class FakeHarness:
    name = "fake"

    def __init__(self, script: list[Behavior], *, default: Behavior | None = None) -> None:
        self.script = list(script)
        self.default = default or works()
        self.prompts: list[str] = []
        self.calls: list[dict] = []

    def run(self, prompt, *, cwd, timeout, resume=None, model=None, system_append=None, log_path=None, **kw) -> Result:
        self.prompts.append(prompt)
        self.calls.append({"resume": resume, "model": model, "timeout": timeout, **kw})
        behavior = self.script.pop(0) if self.script else self.default
        return behavior(Path(cwd), prompt)


def verdict(v: str, reply: str = "", reason: str = "r") -> Behavior:
    import json
    return lambda cwd, prompt: Result(text=json.dumps({"verdict": v, "reply": reply, "reason": reason}), cost_usd=0.01)


def garbage() -> Behavior:
    return lambda cwd, prompt: Result(text="I think you should continue, probably?")
