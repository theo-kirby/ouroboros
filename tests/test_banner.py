import io
import sys
import time

import pytest

from ouroboros import banner


@pytest.mark.parametrize("is_terminal", [False, True])
def test_banner_prints_once_without_animation(monkeypatch, is_terminal):
    out = io.StringIO()
    monkeypatch.setattr(out, "isatty", lambda: is_terminal)
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setenv("COLUMNS", "120")
    monkeypatch.setenv("LINES", "60")
    monkeypatch.setattr(time, "sleep", lambda _: pytest.fail("banner must not wait"))

    banner.render()

    assert out.getvalue() == banner.BANNER + "\nan infinite loop\n\n"
