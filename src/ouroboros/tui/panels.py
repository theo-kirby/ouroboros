"""The panels: run strip, stages, iterations, load, feed, overseer, plan, log."""

from __future__ import annotations

import curses
import time
from typing import Callable, Dict

from .layout import Rect
from .state import ROLE_ORDER, STAGES, Snapshot, harness_roster
from .theme import (
    PAIR_BOX_FEED, PAIR_BOX_ITER, PAIR_BOX_LOAD, PAIR_BOX_RUN, PAIR_BOX_STAGES, PAIR_CYAN, PAIR_DIM, PAIR_DIV,
    PAIR_GREEN, PAIR_HI, PAIR_INACTIVE, PAIR_MAGENTA, PAIR_PROMPT, PAIR_PURPLE, PAIR_RED, PAIR_TITLE, PAIR_YELLOW, Theme,
)
from .widgets import braille_chart, chart_bounds, clip, fmt_duration, hbar, wrap

_ROUND = {"lu": "╭", "ru": "╮", "ld": "╰", "rd": "╯"}
_H, _V = "─", "│"
_SUP = ("⁰", "¹", "²", "³", "⁴", "⁵", "⁶", "⁷", "⁸", "⁹")

VERDICT_PAIR = {
    "continue": PAIR_GREEN, "answer": PAIR_CYAN, "done_rejected": PAIR_YELLOW, "done_accepted": PAIR_PURPLE,
    "stuck": PAIR_MAGENTA, "revert": PAIR_RED,
}
# The history strip is read at a glance, and a glance holds two things, not six: the
# iteration went through, or it ran into something. `answer` and `done_accepted` are
# the loop working; the rest is work thrown away or not done at all.
DOT = "●"
BLOCKED = {"stuck", "revert", "done_rejected"}
STATE_PAIR = {
    "work": PAIR_GREEN, "critique": PAIR_CYAN, "oversee": PAIR_YELLOW, "reconcile": PAIR_PURPLE, "plan": PAIR_MAGENTA,
    "idle": PAIR_DIM, "backoff": PAIR_RED, "starting": PAIR_DIM, "stopped": PAIR_RED,
}


class Painter:
    def __init__(self, win, theme: Theme) -> None:
        self.win = win
        self.theme = theme

    def text(self, y: int, x: int, s: str, attr: int = 0, width: int | None = None) -> None:
        if y < 0 or x < 0 or not s:
            return
        if width is not None:
            s = clip(s, width)
        try:
            self.win.addstr(y, x, s, attr)
        except curses.error:
            pass

    def box(self, rect: Rect, title: str, num: int = 0, border_pair: int = PAIR_DIV, title2: str = "") -> Rect:
        t = self.theme
        y, x, h, w = rect.y, rect.x, rect.h, rect.w
        if h < 2 or w < 4:
            return Rect(y, x, 0, 0)
        border = t.attr(border_pair)
        line = _H * (w - 2)
        self.text(y, x, _ROUND["lu"] + line + _ROUND["ru"], border)
        self.text(y + h - 1, x, _ROUND["ld"] + line + _ROUND["rd"], border)
        for i in range(1, h - 1):
            self.text(y + i, x, _V, border)
            self.text(y + i, x + w - 1, _V, border)
        cx = x + 2
        self.text(y, cx, "┐", border)
        cx += 1
        if num:
            self.text(y, cx, _SUP[min(9, num)], t.attr(PAIR_HI, bold=True))
            cx += 1
        label = f" {title} "
        self.text(y, cx, label, t.attr(PAIR_TITLE, bold=True))
        cx += len(label)
        self.text(y, cx, "┌", border)
        if title2 and len(title2) + 6 < w:
            bx = x + w - len(title2) - 5
            self.text(y + h - 1, bx, "┘", border)
            self.text(y + h - 1, bx + 1, f" {title2} ", t.attr(PAIR_DIM))
            self.text(y + h - 1, bx + len(title2) + 3, "└", border)
        return Rect(y + 1, x + 1, h - 2, w - 2)


def meter(p: Painter, y: int, x: int, w: int, label: str, value: float, vmax: float, text: str,
          pair: int | None = None, label_w: int = 11) -> None:
    """`label ████░░░░ text`: a btop-style meter, gradient by fill unless `pair` is given."""
    t = p.theme
    bar_w = max(4, w - label_w - len(text) - 2)
    p.text(y, x, f"{label:<{label_w}}", t.attr(PAIR_DIM), width=label_w)
    frac = 0.0 if vmax <= 0 else max(0.0, min(1.0, value / vmax))
    filled = int(round(frac * bar_w))
    for i in range(bar_w):
        if i < filled:
            attr = t.attr(pair, bold=True) if pair is not None else t.grad_attr(i / max(1, bar_w - 1))
            p.text(y, x + label_w + i, "█", attr)
        else:
            p.text(y, x + label_w + i, "░", t.attr(PAIR_INACTIVE))
    p.text(y, x + label_w + bar_w + 1, text, t.attr(PAIR_TITLE))


def _pipeline(p: Painter, y: int, x: int, w: int, s: Snapshot) -> int:
    """actor→critic→overseer→maintainer→planner with the active stage lit; returns the width used."""
    t = p.theme
    cx = x
    for name in ("actor", "critic", "overseer", "maintainer", "planner"):
        if cx > x:
            p.text(y, cx, "→", t.attr(PAIR_INACTIVE)); cx += 1
        active = name == s.stage
        pair = STATE_PAIR.get({"actor": "work", "critic": "critique", "overseer": "oversee",
                               "maintainer": "reconcile", "planner": "plan"}[name], PAIR_DIM)
        p.text(y, cx, name, t.attr(pair if active else PAIR_INACTIVE, bold=active), width=max(0, x + w - cx))
        cx += len(name)
    if s.stage not in ("actor", "critic", "overseer", "maintainer", "planner"):
        word = f" {s.stage}"
        p.text(y, cx, word, t.attr(STATE_PAIR.get(s.stage, PAIR_DIM), bold=True), width=max(0, x + w - cx))
        cx += len(word)
    return cx - x


def _summary_line(s: Snapshot) -> str:
    """How far through the charter, and where the night went.

    Both used to be panels. Neither earned one: the charter moves a few times a night,
    and the stage split is ~80% actor in every run there has ever been. A constant does
    not need a chart, it needs a line.
    """
    parts = []
    f = s.frontier or {}
    total = f.get("gaps_total", 0)
    if total:
        parts.append(f"charter {total - f.get('gaps_unchecked', total)}/{total}")
    for status in ("working", "open", "blocked", "broken"):
        n = f.get("all_" + status, 0)
        if n:
            parts.append(f"{n} {status}")
    spent = sum(st.total for n, st in s.stages.items() if n != "commit")
    if spent > 0:
        parts.append("time " + " ".join(
            f"{_ROLE_SHORT.get(n, n)} {s.stages[n].total / spent * 100:.0f}%"
            for n in ROLE_ORDER if s.stages.get(n) and s.stages[n].count))
    return "  ·  ".join(parts)


# ---------------------------------------------------------------- run strip
def _usage_row(p: Painter, y: int, x: int, w: int, s: Snapshot) -> None:
    """`usage  claude 7d 54% +19 · codex 7d 42% +36 · pi $2.50`, coloured by how full."""
    t = p.theme
    p.text(y, x, "usage ", t.attr(PAIR_DIM))
    cx = x + 6
    parts = s.usage_parts
    if not parts:
        p.text(y, cx, "no harness has reported a window yet", t.attr(PAIR_INACTIVE), width=max(0, x + w - cx))
        return
    for i, (label, fill) in enumerate(parts):
        if i:
            p.text(y, cx, "  ·  ", t.attr(PAIR_INACTIVE)); cx += 5
        if cx >= x + w:
            return
        # Money has no ceiling to be a fraction of, so it stays plain.
        attr = t.grad_attr(fill) if fill is not None else t.attr(PAIR_TITLE)
        p.text(y, cx, label, attr, width=max(0, x + w - cx))
        cx += len(label)


def draw_run(p: Painter, rect: Rect, s: Snapshot) -> None:
    t = p.theme
    st = s.status or {}
    state = st.get("state", "no status")
    inner = p.box(rect, f"ouroboros · {st.get('run', '?')}", 0, PAIR_BOX_RUN,
                  title2=f"{s.mode} · {st.get('memory', '?')} · {st.get('branch', '?')}")
    if inner.h <= 0:
        return
    y, x, w = inner.y, inner.x + 1, inner.w - 2
    proc = "alive" if s.alive else "NOT RUNNING"
    it = st.get("iteration", 0)
    cap = f"/{s.max_iterations}" if s.max_iterations else ""
    used = _pipeline(p, y, x, w, s)
    line1 = [
        ("   iteration ", PAIR_DIM), (f"{it}{cap}", PAIR_TITLE),
        ("  elapsed ", PAIR_DIM), (fmt_duration(s.elapsed), PAIR_TITLE),
        ("  pid ", PAIR_DIM), (f"{s.pid or '-'} {proc}", PAIR_GREEN if s.alive else PAIR_RED),
    ]
    cx = x + used
    for text, pair in line1:
        p.text(y, cx, text, t.attr(pair, bold=pair == PAIR_TITLE), width=max(0, x + w - cx))
        cx += len(text)
    if inner.h >= 2:
        limited = st.get("limited") or {}
        cx = x
        p.text(y + 1, cx, "harness ", t.attr(PAIR_DIM)); cx += 8
        active = st.get("harness", "?")
        chain = s.chains.get("actor") or [active]
        for i, name in enumerate(chain):
            if i:
                p.text(y + 1, cx, " → ", t.attr(PAIR_INACTIVE)); cx += 3
            if name in limited:
                pair, mark = PAIR_RED, f"{name} (limited {str(limited[name]).split(' (')[0]})"
            elif name == active:
                pair, mark = PAIR_GREEN, name
            else:
                pair, mark = PAIR_INACTIVE, name
            p.text(y + 1, cx, mark, t.attr(pair, bold=name == active)); cx += len(mark)
        tail = f"  ·  {s.switches} switches" if s.switches else ""
        p.text(y + 1, cx, tail, t.attr(PAIR_DIM), width=max(0, x + w - cx))
    # What the night is spending, on its own row. A subscription bills a flat fee and
    # rations by window, so the window is the cost; money shows only for a harness
    # billed per call. This is the number a person checks before going back to sleep,
    # so it gets a line of its own rather than a tail on someone else's.
    if inner.h >= 3:
        _usage_row(p, y + 2, x, w, s)
    if inner.h >= 4:
        p.text(y + 3, x, _summary_line(s), t.attr(PAIR_DIM), width=w)
    if inner.h >= 3:
        # One row, three claimants, in the order a person needs them: a run asking for
        # help, then a run that has stopped saying why, then the clock. They used to
        # overprint each other, which left a banner with a progress bar behind its tail.
        row = y + inner.h - 1
        stopped = st.get("state") == "stopped"
        reason = str(st.get("stop_reason") or "")
        if s.needs_human:
            first = s.needs_human.strip().splitlines()
            msg = next((l for l in first if l and not l.startswith("#") and not l[0].isdigit()), "see NEEDS_HUMAN.md")
            p.text(row, x, clip("!! NEEDS HUMAN: " + msg, w).ljust(w), t.attr(PAIR_RED, bold=True))
        elif stopped:
            p.text(row, x, clip("stopped: " + (reason or "no reason recorded"), w).ljust(w),
                   t.attr(PAIR_YELLOW, bold=True))
        elif s.stop_after_s:
            label = f"{fmt_duration(s.stop_after_s - s.elapsed)} left of {fmt_duration(s.stop_after_s)}"
            bw = max(10, w - len(label) - 2)
            bar = hbar(s.elapsed, s.stop_after_s, bw)
            for i, ch in enumerate(bar):
                p.text(row, x + i, ch, t.grad_attr(i / max(1, bw - 1)) if ch == "█" else t.attr(PAIR_INACTIVE))
            p.text(row, x + bw + 1, label, t.attr(PAIR_DIM))


# ---------------------------------------------------------------- stages
def draw_stages(p: Painter, rect: Rect, s: Snapshot, num: int) -> None:
    t = p.theme
    inner = p.box(rect, "stages", num, PAIR_BOX_STAGES)
    if inner.h <= 0:
        return
    y, x, w = inner.y, inner.x + 1, inner.w - 2
    cx = x
    for i, name in enumerate(STAGES):
        if i:
            p.text(y, cx, "→", t.attr(PAIR_INACTIVE)); cx += 1
        active = name == s.stage
        pair = STATE_PAIR.get({"actor": "work", "critic": "critique", "overseer": "oversee",
                               "maintainer": "reconcile", "planner": "plan"}.get(name, ""), PAIR_DIM)
        label = f"[{name}]" if active else f" {name} "
        p.text(y, cx, label, t.attr(pair if active else PAIR_INACTIVE, bold=active), width=max(0, x + w - cx))
        cx += len(label)
    if s.stage in ("idle", "backoff", "stopped", "starting"):
        p.text(y, min(cx + 1, x + w), f" {s.stage}", t.attr(STATE_PAIR.get(s.stage, PAIR_DIM), bold=True), width=max(0, x + w - cx - 1))
    if inner.h < 3:
        return
    p.text(y + 1, x, f"{'stage':<11}{'runs':>5}{'last':>9}{'avg':>9}{'total':>9}", t.attr(PAIR_INACTIVE), width=w)
    row = y + 2
    for name in STAGES:
        if row >= inner.y + inner.h:
            break
        st = s.stages.get(name)
        if st is None:
            continue
        active = name == s.stage
        if name == "commit":
            line = f"{name:<11}{st.count:>5}{'':>9}{'':>9}{'':>9}"
        else:
            line = f"{name:<11}{st.count:>5}{fmt_duration(st.last) if st.count else '—':>9}{fmt_duration(st.avg) if st.count else '—':>9}{fmt_duration(st.total) if st.count else '—':>9}"
        if active and name != "commit":
            line = line[:16] + f"{'now ' + fmt_duration(s.now - s.stage_since):>9}" + line[25:]
        p.text(row, x, line, t.attr(PAIR_TITLE if active else PAIR_DIM, bold=active), width=w)
        row += 1


# ---------------------------------------------------------------- loop (the compact default)
def _outcome_glyph(it) -> tuple[str, int]:
    if it.reverted:
        return "✗", PAIR_RED
    if it.bet:
        return "◆", PAIR_MAGENTA
    if it.changed and it.recorded:
        return "■", PAIR_GREEN
    if it.changed:
        return "▪", PAIR_YELLOW
    if it.error:
        return "!", PAIR_RED
    return "·", PAIR_INACTIVE


def draw_loop(p: Painter, rect: Rect, s: Snapshot, num: int) -> None:
    t = p.theme
    inner = p.box(rect, "loop", num, PAIR_BOX_STAGES)
    if inner.h <= 0:
        return
    y, x, w = inner.y, inner.x + 1, inner.w - 2
    # 1. the pipeline, active stage lit
    cx = x
    for i, name in enumerate(STAGES):
        if name == "commit":
            continue
        if cx > x:
            p.text(y, cx, "→", t.attr(PAIR_INACTIVE)); cx += 1
        active = name == s.stage
        pair = STATE_PAIR.get({"actor": "work", "critic": "critique", "overseer": "oversee",
                               "maintainer": "reconcile", "planner": "plan"}[name], PAIR_DIM)
        p.text(y, cx, name, t.attr(pair if active else PAIR_INACTIVE, bold=active), width=max(0, x + w - cx))
        cx += len(name)
    if s.stage not in ("actor", "critic", "overseer", "maintainer", "planner"):
        p.text(y, min(cx + 2, x + w), s.stage, t.attr(STATE_PAIR.get(s.stage, PAIR_DIM), bold=True), width=max(0, x + w - cx - 2))
    if inner.h < 2:
        return
    # 2. outcome strip, newest right
    its = s.iterations
    strip = its[-(w - 12):]
    p.text(y + 1, x, f"{len(its):>4} iter ", t.attr(PAIR_DIM))
    for i, it in enumerate(strip):
        ch, pair = _outcome_glyph(it)
        p.text(y + 1, x + 10 + i, ch, t.attr(pair, bold=True))
    if inner.h < 3:
        return
    # 3. counts
    productive = sum(1 for i in its if i.changed and i.recorded)
    empty = sum(1 for i in its if not i.changed)
    reverts = sum(1 for i in its if i.reverted)
    p.text(y + 2, x, f"{productive} productive · {empty} empty · {reverts} reverted · {s.reconciles} reconciles · {s.plans} bets",
           t.attr(PAIR_DIM), width=w)
    if inner.h < 4:
        return
    # 4. the current stage's clock and the last iteration
    st = s.stages.get(s.stage)
    avg = f", avg {fmt_duration(st.avg)}" if st and st.count else ""
    p.text(y + 3, x, f"{s.stage} for {fmt_duration(s.now - s.stage_since)}{avg}", t.attr(PAIR_TITLE), width=w)
    if inner.h >= 5 and its:
        last = its[-1]
        line = f"last #{last.n}: {fmt_duration(last.actor_seconds)} actor · {last.verdict or '…'}"
        if last.critique:
            line += f" · critic {last.critique}"
        if last.error:
            line += f" · {last.error}"
        p.text(y + 4, x, line, t.attr(PAIR_DIM), width=w)
    if inner.h >= 6:
        limited = (s.status or {}).get("limited") or {}
        if limited:
            note = "limited: " + ", ".join(f"{k} {str(v).split(' (')[0]}" for k, v in limited.items())
            p.text(y + 5, x, note, t.attr(PAIR_RED), width=w)


# ---------------------------------------------------------------- iterations
def draw_iterations(p: Painter, rect: Rect, s: Snapshot, num: int) -> None:
    t = p.theme
    its = s.iterations
    productive = sum(1 for i in its if i.changed and i.recorded)
    empty = sum(1 for i in its if not i.changed)
    reverts = sum(1 for i in its if i.reverted)
    inner = p.box(rect, "iterations · actor minutes", num, PAIR_BOX_ITER,
                  title2=f"{productive} productive · {empty} empty · {reverts} reverted")
    if inner.h <= 0:
        return
    y, x, w = inner.y, inner.x + 1, inner.w - 2
    if not its:
        p.text(y, x, "no iterations yet", t.attr(PAIR_INACTIVE))
        return
    if inner.h < 2:
        return
    # actor minutes per iteration, on whatever scale shows the variation
    ch_h = inner.h - 1
    plot, vmin, vmax, note = chart_bounds([it.actor_seconds / 60 for it in its])
    lo, hi = (10 ** vmin, 10 ** vmax) if note == "log" else (vmin, vmax)
    label_w = 7
    rows = braille_chart(plot, max(1, w - label_w), ch_h, vmin, vmax, fill=True)
    for i, row in enumerate(rows):
        p.text(y + i, x + label_w, row, t.grad_attr((len(rows) - i) / len(rows)))
    p.text(y, x, f"{hi:5.1f}m", t.attr(PAIR_INACTIVE))
    if ch_h > 1:
        p.text(y + ch_h - 1, x, f"{lo:5.1f}m", t.attr(PAIR_INACTIVE))
    if note and ch_h > 2:
        p.text(y + ch_h - 2, x, f"{note:>6}", t.attr(PAIR_INACTIVE))
    last = its[-1] if its else None
    foot = (f"last #{last.n}: {fmt_duration(last.actor_seconds)} actor · {last.verdict or '…'}"
            + (f" · critic {last.critique}" if last.critique else "")) if last else ""
    p.text(y + inner.h - 1, x, foot, t.attr(PAIR_DIM), width=w)


# ---------------------------------------------------------------- load
class LoadHistory:
    def __init__(self, n: int = 600) -> None:
        self.cpu: list[float] = []
        self.n = n

    def push(self, v: float) -> None:
        self.cpu.append(v)
        del self.cpu[: -self.n]


def draw_load(p: Painter, rect: Rect, s: Snapshot, num: int, hist: LoadHistory) -> None:
    """Who is doing the work, what it has cost their subscription, and what the box is doing.

    The harnesses are the point of this panel and get the room; the machine is one
    footer line, because a number nobody acts on does not deserve half the box.
    """
    t = p.theme
    rows = harness_rows(s)
    inner = p.box(rect, "activity · harnesses", num, PAIR_BOX_LOAD, title2=usage_window_label(rows))
    if inner.h <= 0:
        return
    y, x, w = inner.y, inner.x + 1, inner.w - 2

    stats = f"cpu {s.cpu_pct:.0f}%  ·  {s.procs} proc {s.rss_mb:,.0f} MB  ·  load {s.loadavg[0]:.1f}"
    # The harnesses come first: they may take every row if they need it, because a
    # harness that silently vanishes is worse than no cpu trace. Whatever they leave
    # goes to the trace, which is the one thing here that reads better the taller it is.
    # The two reserved rows are for the trace and the stats, but never at the price of
    # a harness: a short panel drops the trace first and the meters last.
    budget = min(inner.h, max(len(rows), inner.h - 2)) if rows else inner.h
    used = draw_harness_rows(p, y, x, w, budget, rows, s.now)
    tail = inner.h - used
    if tail >= 2:
        spark = braille_chart(hist.cpu, w, min(4, tail - 1), 0.0, max(100.0, max(hist.cpu or [0.0])))
        for i, line in enumerate(spark):
            p.text(y + inner.h - 1 - len(spark) + i, x, line, t.grad_attr(0.5))
    if tail >= 1:
        p.text(y + inner.h - 1, x, stats, t.attr(PAIR_INACTIVE), width=w)


# ---------------------------------------------------------------- feed
KIND_STYLE = {
    "text": ("│ ", PAIR_TITLE), "think": ("┆ ", PAIR_INACTIVE), "tool": ("▸ ", PAIR_CYAN), "tool_out": ("  ", PAIR_INACTIVE),
    "result": ("● ", PAIR_GREEN), "error": ("✗ ", PAIR_RED), "init": ("○ ", PAIR_INACTIVE),
}


def draw_feed(p: Painter, rect: Rect, s: Snapshot, num: int, show_output: bool) -> None:
    t = p.theme
    age = f"{fmt_duration(s.feed_age)} ago" if s.feed_age is not None else ""
    inner = p.box(rect, f"messages · {s.feed_role or 'no transcript'}", num, PAIR_BOX_FEED, title2=age)
    if inner.h <= 0:
        return
    x, w = inner.x + 1, inner.w - 2
    lines: list[tuple[str, int, bool]] = []
    for ev in s.feed:
        if ev.kind == "tool_out" and not show_output:
            continue
        if ev.kind in ("think", "init"):
            continue
        prefix, pair = KIND_STYLE.get(ev.kind, ("  ", PAIR_DIM))
        max_lines = 3 if ev.kind == "text" else 1 if ev.kind in ("tool", "init", "result") else 2
        for j, l in enumerate(wrap(ev.text, w - len(prefix), max_lines)):
            lines.append(((prefix if j == 0 else " " * len(prefix)) + l, pair, j == 0))
    if not lines:
        p.text(inner.y, x, "waiting for the first message…", t.attr(PAIR_INACTIVE))
        return
    shown = lines[-inner.h:]
    for i, (text, pair, first) in enumerate(shown):
        p.text(inner.y + i, x, text, t.attr(pair, bold=first and pair == PAIR_CYAN), width=w)


# ---------------------------------------------------------------- overseer
def draw_overseer(p: Painter, rect: Rect, s: Snapshot, num: int) -> None:
    t = p.theme
    counts: Dict[str, int] = {}
    for d in s.decisions:
        counts[d.get("verdict", "?")] = counts.get(d.get("verdict", "?"), 0) + 1
    inner = p.box(rect, "overseer", num, PAIR_BOX_STAGES,
                  title2=" ".join(f"{k} {v}" for k, v in sorted(counts.items())) or "")
    if inner.h <= 0:
        return
    x, w = inner.x + 1, inner.w - 2
    y = inner.y
    last = s.decisions[-1] if s.decisions else None
    if last is None:
        p.text(y, x, "no verdicts yet", t.attr(PAIR_INACTIVE))
        return
    # The shape of the night and its latest word share the top line, so the words the
    # overseer actually wrote get the rest of a panel that is wide rather than tall.
    # Six letters asked the reader to decode a legend at a glance, which is the one
    # thing a glance cannot do. The only question the strip answers is whether the
    # night ran or hit something, so it has two states and one shape.
    v = last.get("verdict", "?")
    tag = f"#{last.get('iteration', '?')} {v}"
    strip_w = max(4, w - len(tag) - 12)
    strip = s.decisions[-strip_w:]
    p.text(y, x, "history ", t.attr(PAIR_DIM))
    for i, d in enumerate(strip):
        vv = d.get("verdict", "?")
        p.text(y, x + 8 + i, DOT, t.attr(PAIR_RED if vv in BLOCKED else PAIR_GREEN, bold=True))
    p.text(y, x + w - len(tag), tag, t.attr(PAIR_RED if v in BLOCKED else PAIR_GREEN, bold=True))
    left = inner.h - 1
    if left <= 0:
        return
    reason = " ".join(str(last.get("reason", "")).split())
    reply = " ".join(str(last.get("reply") or "").split())
    lines = wrap(reason, w, (1 if reply and left > 1 else left)) if reason else []
    if reply and len(lines) < left:
        lines += [("→ " + l if i == 0 else "  " + l)
                  for i, l in enumerate(wrap(reply, w - 2, left - len(lines)))]
    for i, l in enumerate(lines[:left]):
        p.text(y + 1 + i, x, l, t.attr(PAIR_PROMPT if l.startswith(("→", "  ")) else PAIR_DIM), width=w)


# ---------------------------------------------------------------- plan
def draw_plan(p: Painter, rect: Rect, s: Snapshot, num: int) -> None:
    t = p.theme
    inner = p.box(rect, "plan · short", num, PAIR_BOX_ITER)
    if inner.h <= 0:
        return
    x, w = inner.x + 1, inner.w - 2
    if not s.plan_short:
        p.text(inner.y, x, "no plan yet (the planner writes it after the first reconcile)", t.attr(PAIR_INACTIVE), width=w)
        return
    lines: list[str] = []
    for i, item in enumerate(s.plan_short):
        for j, l in enumerate(wrap(item, w - 3, 2)):
            lines.append((f"{i + 1}. " if j == 0 else "   ") + l)
    for i, l in enumerate(lines[: inner.h]):
        p.text(inner.y + i, x, l, t.attr(PAIR_DIM if l.startswith("   ") else PAIR_TITLE), width=w)


# ---------------------------------------------------------------- time by stage (meters)
def draw_time(p: Painter, rect: Rect, s: Snapshot, num: int) -> None:
    inner = p.box(rect, "time by stage", num, PAIR_BOX_STAGES)
    if inner.h <= 0:
        return
    x, w = inner.x + 1, inner.w - 2
    total = sum(st.total for n, st in s.stages.items() if n != "commit") or 1.0
    rows = [(n, s.stages[n].total) for n in ("actor", "critic", "overseer", "maintainer", "planner")]
    for i, (name, secs) in enumerate(rows[: inner.h]):
        meter(p, inner.y + i, x, w, name, secs, total, f"{secs / total * 100:3.0f}% {fmt_duration(secs):>7}")


# ---------------------------------------------------------------- harnesses
_ROLE_SHORT = {"actor": "act", "critic": "crit", "overseer": "over", "maintainer": "maint", "planner": "plan"}


def _roles_line(r, width: int) -> str:
    """The jobs a harness holds and what it drives them with, shortened only if it must be.

    A half-written model name says less than none, so the model is the first thing
    dropped once the line stops fitting.
    """
    models = [m.replace("claude-", "") for m in r.models]
    tail = [f"limited {r.limit_note}"] if r.limit_note else []
    for names, show_model in ((r.roles, True), ([_ROLE_SHORT.get(n, n) for n in r.roles], True),
                              ([_ROLE_SHORT.get(n, n) for n in r.roles], False)):
        parts = [" ".join(names) or "standby"]
        if r.backs:
            parts.append(f"backs {len(r.backs)}")
        if models and show_model:
            parts.append(" ".join(models))
        line = " · ".join(parts + tail)
        if len(line) <= width:
            return line
    return line


def harness_rows(s: Snapshot) -> list:
    """One row per configured harness, joined to what it has used."""
    return harness_roster(s.roles or {n: [(h, None) for h in c] for n, c in (s.chains or {}).items()},
                          s.usage, (s.status or {}).get("limited") or {}, now=s.now)


_SHORT_WINDOW_LABEL = {"five_hour": "5h", "hourly": "1h", "daily": "24h"}


def _block(r, level: int) -> list[str]:
    """The lines one harness gets at a level of detail, richest first.

    Every harness keeps its meter at every level. What a short panel gives up is the
    short window, then the roles, in that order: a harness that silently vanishes is
    worse than a harness whose detail is missing.
    """
    kinds = ["meter"]
    if level >= 3 and (r.short_utilization is not None or r.short_expired):
        kinds.append("short")
    if level >= 2:
        kinds.append("roles")
    return kinds


def draw_harness_rows(p: Painter, y: int, x: int, w: int, h: int, rows: list, now: float = 0.0) -> int:
    """Meters and detail lines, as detailed as the height allows. Returns the rows used.

    Detail is handed out greedily rather than at one level for everyone: with room for
    three lines and two harnesses, the first harness keeping its roles beats both of
    them losing it and a row going blank.
    """
    t = p.theme
    if h <= 0:
        return 0
    if not rows:
        p.text(y, x, "no roles configured", t.attr(PAIR_INACTIVE), width=w)
        return 1
    plan = [_block(r, 1) for r in rows]
    used = len(rows)
    for level in (2, 3):   # roles first, then the short window: it is dropped first
        for i, r in enumerate(rows):
            want = _block(r, level)
            if len(want) > len(plan[i]) and used + len(want) - len(plan[i]) <= h:
                used, plan[i] = used + len(want) - len(plan[i]), want
    top, bottom = y, y + h
    for r, kinds in zip(rows, plan):
        for kind in kinds:
            if y >= bottom:
                return y - top
            if kind == "meter":
                util = r.utilization
                delta = f" {r.delta:+.0f}" if r.delta is not None and abs(r.delta) >= 0.5 else ""
                text = ("  —" if r.idle else "  ·") if util is None else f"{util * 100:3.0f}%{delta}"
                if r.expired:
                    text = "reset · awaiting reading"
                pair = PAIR_RED if (r.limited or (util is not None and util >= 0.9)) else None
                label = r.name + (" !" if r.limited else "")
                meter(p, y, x, w, label, (util or 0.0), 1.0, text, pair=pair,
                      label_w=min(10, max(7, len(label) + 1)))
            elif kind == "short":
                # The long window says whether the week survives; this one says whether
                # the next hour does, which is the question at three in the morning.
                util = r.short_utilization or 0.0
                left = (f"  resets {fmt_duration(r.short_resets_at - now)}"
                        if r.short_resets_at and r.short_resets_at > now else "")
                meter(p, y, x + 2, w - 2, _SHORT_WINDOW_LABEL.get(r.short_window, r.short_window),
                      util, 1.0, ("reset · awaiting reading" if r.short_expired else f"{util * 100:3.0f}%{left}"),
                      pair=PAIR_RED if util >= 0.9 else None, label_w=5)
            else:
                p.text(y, x + 2, _roles_line(r, w - 2), t.attr(PAIR_INACTIVE), width=w - 2)
            y += 1
    return y - top


def usage_window_label(rows: list) -> str:
    """Name the window in the title: a bare percentage does not say what it is a share of."""
    windows = {r.window for r in rows if r.window}
    short = {"seven_day": "7d", "five_hour": "5h", "daily": "24h"}
    return short.get(next(iter(windows)), next(iter(windows))) if len(windows) == 1 else ""


# ---------------------------------------------------------------- frontier (meters)
def draw_frontier(p: Painter, rect: Rect, s: Snapshot, num: int) -> None:
    inner = p.box(rect, "frontier", num, PAIR_BOX_RUN)
    if inner.h <= 0:
        return
    x, w = inner.x + 1, inner.w - 2
    f = s.frontier
    if not f:
        p.text(inner.y, x, "no STATE.md (handoff memory)", p.theme.attr(PAIR_INACTIVE), width=w)
        return
    rows = []
    total_gaps = f.get("gaps_total", 0)
    if total_gaps:
        # Both counts have to come from the charter. `gaps_open` is a tally of STATE.md
        # frontier lines, which is a different list of a different length, and mixing
        # the two reported a closed count that was never right.
        closed = total_gaps - f.get("gaps_unchecked", total_gaps)
        rows.append(("gaps done", closed, total_gaps, f"{closed}/{total_gaps}", PAIR_GREEN))
    nodes = f.get("nodes") or 1
    for status, pair in (("working", PAIR_CYAN), ("open", PAIR_YELLOW), ("blocked", PAIR_RED), ("broken", PAIR_RED)):
        n = f.get("all_" + status, 0)
        if n:
            rows.append((status, n, nodes, f"{n:>3}", pair))
    for i, (label, v, vmax, text, pair) in enumerate(rows[: inner.h]):
        meter(p, inner.y + i, x, w, label, v, vmax, text, pair=pair)


# ---------------------------------------------------------------- log
def draw_log(p: Painter, rect: Rect, s: Snapshot, num: int) -> None:
    t = p.theme
    inner = p.box(rect, "loop.log", num, PAIR_DIV)
    if inner.h <= 0:
        return
    x, w = inner.x + 1, inner.w - 2
    for i, line in enumerate(s.log_tail[-inner.h:]):
        body = f"{line[11:19]} {line[26:]}" if len(line) > 26 and line[10] == "T" else line
        pair = PAIR_RED if ("error" in body.lower() or "NEEDS_HUMAN" in body) else PAIR_YELLOW if "harness:" in body else PAIR_DIM
        p.text(inner.y + i, x, clip(body, w), t.attr(pair))


PanelFn = Callable[..., None]
