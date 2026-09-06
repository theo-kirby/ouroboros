"""The panels: run strip, stages, iterations, load, feed, overseer, plan, log."""

from __future__ import annotations

import curses
import time
from typing import Callable, Dict

from .layout import Rect
from .state import STAGES, Snapshot
from .theme import (
    PAIR_BOX_FEED, PAIR_BOX_ITER, PAIR_BOX_LOAD, PAIR_BOX_RUN, PAIR_BOX_STAGES, PAIR_CYAN, PAIR_DIM, PAIR_DIV,
    PAIR_GREEN, PAIR_HI, PAIR_INACTIVE, PAIR_MAGENTA, PAIR_PROMPT, PAIR_PURPLE, PAIR_RED, PAIR_TITLE, PAIR_YELLOW, Theme,
)
from .widgets import braille_chart, clip, fmt_duration, hbar, wrap

_ROUND = {"lu": "╭", "ru": "╮", "ld": "╰", "rd": "╯"}
_H, _V = "─", "│"
_SUP = ("⁰", "¹", "²", "³", "⁴", "⁵", "⁶", "⁷", "⁸", "⁹")

VERDICT_PAIR = {
    "continue": PAIR_GREEN, "answer": PAIR_CYAN, "done_rejected": PAIR_YELLOW, "done_accepted": PAIR_PURPLE,
    "stuck": PAIR_MAGENTA, "revert": PAIR_RED,
}
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


# ---------------------------------------------------------------- run strip
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
    left = fmt_duration(s.stop_after_s - s.elapsed) + " left" if s.stop_after_s else "no wall-clock cap"
    it = st.get("iteration", 0)
    cap = f"/{s.max_iterations}" if s.max_iterations else ""
    line1 = [
        ("state ", PAIR_DIM), (f"{state:<9}", STATE_PAIR.get(state, PAIR_DIM)),
        ("  iteration ", PAIR_DIM), (f"{it}{cap}", PAIR_TITLE),
        ("  elapsed ", PAIR_DIM), (fmt_duration(s.elapsed), PAIR_TITLE),
        ("  ", PAIR_DIM), (left, PAIR_DIM),
        ("  pid ", PAIR_DIM), (f"{s.pid or '-'} {proc}", PAIR_GREEN if s.alive else PAIR_RED),
    ]
    cx = x
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
        tail = f"  ·  ${s.cost:.2f} api-eq" + (f"  ·  {s.switches} switches" if s.switches else "")
        p.text(y + 1, cx, tail, t.attr(PAIR_DIM), width=max(0, x + w - cx))
    if inner.h >= 3:
        if s.stop_after_s:
            frac = min(1.0, s.elapsed / s.stop_after_s)
            label = f"{frac * 100:3.0f}% of {fmt_duration(s.stop_after_s)}"
            bw = max(10, w - len(label) - 2)
            bar = hbar(s.elapsed, s.stop_after_s, bw)
            for i, ch in enumerate(bar):
                p.text(y + 2, x + i, ch, t.grad_attr(i / max(1, bw - 1)) if ch == "█" else t.attr(PAIR_INACTIVE))
            p.text(y + 2, x + bw + 1, label, t.attr(PAIR_DIM))
        if s.needs_human:
            first = s.needs_human.strip().splitlines()
            msg = next((l for l in first if l and not l.startswith("#") and not l[0].isdigit()), "see NEEDS_HUMAN.md")
            p.text(y + 2, x, clip("!! NEEDS HUMAN: " + msg, w), t.attr(PAIR_RED, bold=True))


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
    # outcome strip: one glyph per iteration, newest on the right
    strip = its[-w:]
    for i, it in enumerate(strip):
        ch, pair = _outcome_glyph(it)
        p.text(y, x + w - len(strip) + i, ch, t.attr(pair, bold=True))
    if not its:
        p.text(y, x, "no iterations yet", t.attr(PAIR_INACTIVE))
    if inner.h < 3:
        return
    # actor minutes per iteration as a braille chart, gradient by height
    ch_h = inner.h - 2
    series = [it.actor_seconds / 60 for it in its]
    vmax = max(series or [1.0]) or 1.0
    label_w = 7
    rows = braille_chart(series, max(1, w - label_w), ch_h, 0.0, vmax)
    for i, row in enumerate(rows):
        p.text(y + 1 + i, x + label_w, row, t.grad_attr((len(rows) - i) / len(rows)))
    p.text(y + 1, x, f"{vmax:5.1f}m", t.attr(PAIR_INACTIVE))
    if ch_h > 1:
        p.text(y + ch_h, x, "  0m", t.attr(PAIR_INACTIVE))
    last = its[-1] if its else None
    foot = (f"last #{last.n}: {fmt_duration(last.actor_seconds)} actor · {last.verdict or '…'}"
            + (f" · critic {last.critique}" if last.critique else "") + f" · ${last.cost:.2f}") if last else ""
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
    t = p.theme
    inner = p.box(rect, "load", num, PAIR_BOX_LOAD)
    if inner.h <= 0:
        return
    y, x, w = inner.y, inner.x + 1, inner.w - 2
    stat_w = min(26, max(18, w // 2))
    chart_w = w - stat_w - 1
    ch_h = inner.h
    if chart_w > 4 and ch_h > 0:
        vmax = max(100.0, max(hist.cpu or [0.0]))
        rows = braille_chart(hist.cpu, chart_w, ch_h, 0.0, vmax)
        for i, row in enumerate(rows):
            p.text(y + i, x, row, t.grad_attr((len(rows) - i) / len(rows)))
        p.text(y, x, f"{vmax:.0f}%", t.attr(PAIR_INACTIVE))
    sx = x + chart_w + 1
    cost_rate = (s.cost / (s.elapsed / 3600)) if s.elapsed > 600 else 0.0
    lines = [
        ("harness cpu", f"{s.cpu_pct:5.0f}%", t.threshold_pair(s.cpu_pct, 100, 300) if hasattr(t, "threshold_pair") else PAIR_TITLE),
        ("procs / rss", f"{s.procs}  {s.rss_mb:,.0f} MB", PAIR_TITLE),
        ("loadavg", f"{s.loadavg[0]:.1f} {s.loadavg[1]:.1f} {s.loadavg[2]:.1f}", PAIR_DIM),
        ("cost / h", f"${cost_rate:.2f}", PAIR_DIM),
        ("cost", f"${s.cost:.2f} api-eq", PAIR_DIM),
    ]
    limited = (s.status or {}).get("limited") or {}
    for name, until in limited.items():
        lines.append((f"limit {name}", str(until), PAIR_RED))
    for i, (k, v, pair) in enumerate(lines[:ch_h]):
        p.text(y + i, sx, f"{k:<12}", t.attr(PAIR_INACTIVE), width=stat_w)
        p.text(y + i, sx + 12, v, t.attr(pair, bold=pair == PAIR_TITLE), width=max(0, stat_w - 12))


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
    rows: list[tuple[str, int]] = []
    for d in s.decisions[-8:]:
        v = d.get("verdict", "?")
        rows.append((clip(f"#{d.get('iteration', '?'):<4}{v:<14}{d.get('reason', '')}", w), VERDICT_PAIR.get(v, PAIR_DIM)))
    if last and last.get("reply"):
        rows.append(("reply → " + " ".join(str(last["reply"]).split()), PAIR_PROMPT))
    if not rows:
        p.text(y, x, "no verdicts yet", t.attr(PAIR_INACTIVE))
        return
    out: list[tuple[str, int]] = []
    for text, pair in rows:
        if pair == PAIR_PROMPT:
            out.extend((l, pair) for l in wrap(text, w, 4))
        else:
            out.append((text, pair))
    for i, (text, pair) in enumerate(out[-inner.h:]):
        p.text(y + i, x, text, t.attr(pair), width=w)


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
