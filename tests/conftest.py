import subprocess
from pathlib import Path

import pytest

from ouroboros.memory.handoff import HandoffMemory


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "proj"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    git(r, "config", "user.email", "t@example.com")
    git(r, "config", "user.name", "t")
    (r / ".gitignore").write_text(".ouroboros/runs/\n")
    (r / "README.md").write_text("hi\n")
    mem = HandoffMemory(r / ".ouroboros")
    mem.ensure()
    (r / ".ouroboros" / "goal.md").write_text("# Goal: test\n\n## Mission\n\nkeep going.\n")
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "init")
    return r
