"""Drawing primitives for the btop look (braille charts, bars, formatting); from vllmtop."""

from __future__ import annotations

import math
from typing import List, Sequence

_DOT_BITS = ((0x01, 0x02, 0x04, 0x40), (0x08, 0x10, 0x20, 0x80))
_BRAILLE_BASE = 0x2800


def resample(series: Sequence[float], n: int) -> List[float]:
    """Exactly `n` samples: bucket peaks when there are too many, hold when too few.

    A bucket reports its largest value rather than its mean, because on a duration or
    load series the spike is the thing worth seeing; averaging it away is how a chart
    ends up saying a night went smoothly when it did not.
    """
    vals = [float(v) for v in series]
    if n <= 0 or not vals:
        return []
    if len(vals) == n:
        return vals
    if len(vals) > n:
        return [max(vals[i * len(vals) // n: max(i * len(vals) // n + 1, (i + 1) * len(vals) // n)])
                for i in range(n)]
    return [vals[i * len(vals) // n] for i in range(n)]


def braille_chart(series: Sequence[float], width: int, height: int, vmin: float, vmax: float,
                  baseline: bool = True, fill: bool = False) -> List[str]:
    """`height` rows of `width` braille glyphs; the newest samples sit on the right.

    A series longer than the panel is always resampled rather than cropped: dropping
    the head silently would hide most of a long run. `fill` also stretches a short
    series across the full width, which is what a per-iteration chart wants and a
    rolling history -- which should grow in from the right -- does not.
    """
    if width <= 0 or height <= 0:
        return []
    dot_cols, dot_rows = 2 * width, 4 * height
    vals = list(series)
    if len(vals) > dot_cols or (fill and vals):
        vals = resample(vals, dot_cols)
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


def chart_bounds(series: Sequence[float]) -> tuple[List[float], float, float, str]:
    """Pick a scale that shows variation, and say which one it picked.

    A run's per-iteration numbers usually cluster: charted from zero they all reach
    the same height and the panel becomes a solid block. Two fixes, and which one
    applies depends on the spread. When the largest value dwarfs the smallest, use a
    log scale so the small ones stay legible beside an outlier. Otherwise keep it
    linear but lift the floor to just under the minimum, so the band fills the panel.

    Returns the values to plot (already transformed for log), the bounds to plot them
    against, and a note for the axis label -- empty when the scale is plain linear.
    """
    vals = [float(v) for v in series if v is not None]
    positive = [v for v in vals if v > 0]
    if not positive:
        return vals, 0.0, 1.0, ""
    lo, hi = min(positive), max(vals)
    # The floor comes from the smallest real value, not the smallest number: a run's
    # first iteration has no previous step to measure from and reads as zero, and one
    # such artefact must not decide the scale for everything after it.
    if hi / lo >= 8:
        return [math.log10(max(v, lo)) for v in vals], math.log10(lo), math.log10(hi), "log"
    span = hi - lo
    return vals, (max(0.0, lo - span * 0.25) if span > 0 else 0.0), hi, ""


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
