"""Drawing primitives for the btop look (braille charts, bars, formatting); from vllmtop."""

from __future__ import annotations

import math
from typing import List, Sequence

_DOT_BITS = ((0x01, 0x02, 0x04, 0x40), (0x08, 0x10, 0x20, 0x80))
_BRAILLE_BASE = 0x2800


def braille_chart(series: Sequence[float], width: int, height: int, vmin: float, vmax: float,
                  baseline: bool = True) -> List[str]:
    """`height` rows of `width` braille glyphs; the newest samples sit on the right."""
    if width <= 0 or height <= 0:
        return []
    dot_cols, dot_rows = 2 * width, 4 * height
    vals = list(series)[-dot_cols:]
    pad = dot_cols - len(vals)
    span = (vmax - vmin) or 1.0
    cells = [[0] * width for _ in range(height)]
    for i, value in enumerate(vals):
        gc = pad + i
        norm = min(1.0, max(0.0, (value - vmin) / span))
        filled = int(round(norm * dot_rows))
        if baseline:
            filled = max(1, filled)
        elif filled <= 0:
            continue
        cell_col, sub_col = gc // 2, gc % 2
        for k in range(filled):
            gr = dot_rows - 1 - k
            cells[gr // 4][cell_col] |= _DOT_BITS[sub_col][gr % 4]
    return ["".join(chr(_BRAILLE_BASE + bits) for bits in row) for row in cells]


def hbar(value: float, vmax: float, width: int) -> str:
    if width <= 0:
        return ""
    frac = 0.0 if vmax <= 0 else max(0.0, min(1.0, value / vmax))
    filled = int(round(frac * width))
    return "█" * filled + "░" * (width - filled)


def histogram_bars(values: Sequence[float], vmax: float) -> str:
    blocks = " ▁▂▃▄▅▆▇█"
    vmax = vmax or 1.0
    return "".join(blocks[int(round(max(0.0, min(1.0, v / vmax)) * 8))] for v in values)


def fmt_duration(seconds: float | None) -> str:
    if seconds is None or not math.isfinite(seconds) or seconds < 0:
        return "—"
    s = int(seconds)
    days, rem = divmod(s, 86400)
    hours, rem = divmod(rem, 3600)
    mins, secs = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {mins:02d}m"
    if mins:
        return f"{mins}m {secs:02d}s"
    return f"{secs}s"


def clip(s: str, width: int) -> str:
    s = s.replace("\n", " ").replace("\t", " ")
    if width <= 0:
        return ""
    return s if len(s) <= width else s[: max(0, width - 1)] + "…"


def wrap(s: str, width: int, max_lines: int) -> List[str]:
    """Greedy word wrap into at most `max_lines` lines; the last line is clipped."""
    words = s.replace("\n", " ").split()
    lines: List[str] = []
    cur = ""
    for w in words:
        if not cur:
            cur = w
        elif len(cur) + 1 + len(w) <= width:
            cur += " " + w
        else:
            lines.append(cur)
            cur = w
            if len(lines) == max_lines:
                break
    if cur and len(lines) < max_lines:
        lines.append(cur)
    if len(lines) == max_lines and len(" ".join(words)) > sum(len(l) for l in lines) + len(lines):
        lines[-1] = clip(lines[-1] + " …", width)
    return [clip(l, width) for l in lines]
