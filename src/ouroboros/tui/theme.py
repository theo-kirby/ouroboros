"""Curses colors matching btop's default theme, with fallbacks (from vllmtop)."""

from __future__ import annotations

import curses

PAIR_DEFAULT = 0
PAIR_TITLE = 1
PAIR_GREEN = 2
PAIR_YELLOW = 3
PAIR_RED = 4
PAIR_CYAN = 5
PAIR_DIM = 6
PAIR_MAGENTA = 7
PAIR_HI = 8
PAIR_DIV = 9
PAIR_INACTIVE = 10
PAIR_BOX_RUN = 11
PAIR_BOX_STAGES = 12
PAIR_BOX_ITER = 13
PAIR_BOX_LOAD = 14
PAIR_BOX_FEED = 15
PAIR_PURPLE = 16
PAIR_PINK = 17
PAIR_PROMPT = 18

GRAD_STEPS = 48
GRAD_PAIR_BASE = 19

_GRAD_STOPS = ((0x77, 0xCA, 0x9B), (0xCB, 0xC0, 0x6C), (0xDC, 0x4C, 0x4C))

_PALETTE = {
    "title": "#ffffff", "green": "#77ca9b", "yellow": "#cbc06c", "red": "#dc4c4c", "cyan": "#74e6fc",
    "text": "#aaaaaa", "magenta": "#d9626d", "hi": "#b54040", "div": "#30", "inactive": "#40",
    "box_run": "#556d59", "box_stages": "#5c588d", "box_iter": "#6c6c4b", "box_load": "#805252",
    "box_feed": "#556d59", "purple": "#9a5feb", "pink": "#ff5fb0", "prompt": "#e8a76a",
}
_PAIR_ROLE = {
    PAIR_TITLE: "title", PAIR_GREEN: "green", PAIR_YELLOW: "yellow", PAIR_RED: "red", PAIR_CYAN: "cyan",
    PAIR_DIM: "text", PAIR_MAGENTA: "magenta", PAIR_HI: "hi", PAIR_DIV: "div", PAIR_INACTIVE: "inactive",
    PAIR_BOX_RUN: "box_run", PAIR_BOX_STAGES: "box_stages", PAIR_BOX_ITER: "box_iter", PAIR_BOX_LOAD: "box_load",
    PAIR_BOX_FEED: "box_feed", PAIR_PURPLE: "purple", PAIR_PINK: "pink", PAIR_PROMPT: "prompt",
}
_BASIC = {
    "title": curses.COLOR_WHITE, "text": curses.COLOR_WHITE, "inactive": curses.COLOR_WHITE, "div": curses.COLOR_WHITE,
    "green": curses.COLOR_GREEN, "yellow": curses.COLOR_YELLOW, "red": curses.COLOR_RED, "cyan": curses.COLOR_CYAN,
    "magenta": curses.COLOR_MAGENTA, "hi": curses.COLOR_RED, "box_run": curses.COLOR_GREEN, "box_stages": curses.COLOR_BLUE,
    "box_iter": curses.COLOR_YELLOW, "box_load": curses.COLOR_RED, "box_feed": curses.COLOR_GREEN,
    "purple": curses.COLOR_MAGENTA, "pink": curses.COLOR_MAGENTA, "prompt": curses.COLOR_YELLOW,
}


def _lerp(a, b, t):
    return tuple(round(a[k] + (b[k] - a[k]) * t) for k in range(3))


def _gradient(n, stops):
    segs = len(stops) - 1
    out = []
    for i in range(n):
        x = (i / (n - 1) if n > 1 else 0.0) * segs
        k = min(int(x), segs - 1)
        out.append(_lerp(stops[k], stops[k + 1], x - k))
    return out


def _parse_hex(h: str):
    h = h.lstrip("#")
    if len(h) == 2:
        v = int(h, 16)
        return v, v, v
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _scale(v: int) -> int:
    return round(v / 255 * 1000)


_CUBE = (0, 95, 135, 175, 215, 255)


def _to_256(r: int, g: int, b: int) -> int:
    def cube(v):
        i = min(range(6), key=lambda k: abs(_CUBE[k] - v))
        return i, _CUBE[i]
    ri, rv = cube(r); gi, gv = cube(g); bi, bv = cube(b)
    cube_idx = 16 + 36 * ri + 6 * gi + bi
    cube_err = (rv - r) ** 2 + (gv - g) ** 2 + (bv - b) ** 2
    gray = round((r + g + b) / 3)
    gi2 = min(23, max(0, round((gray - 8) / 10)))
    gv2 = 8 + 10 * gi2
    gray_err = sum((gv2 - c) ** 2 for c in (r, g, b))
    return cube_idx if cube_err <= gray_err else 232 + gi2


class Theme:
    def __init__(self) -> None:
        self.has_color = False
        self._grad: list[int] = []

    def init(self) -> None:
        if not curses.has_colors():
            return
        curses.start_color()
        try:
            curses.use_default_colors()
            bg = -1
        except curses.error:
            bg = curses.COLOR_BLACK
        colors = getattr(curses, "COLORS", 8)
        try:
            can_change = curses.can_change_color() and colors >= 256
        except curses.error:
            can_change = False
        fg: dict[str, int] = {}
        if can_change:
            slot = 16
            for name, hexv in _PALETTE.items():
                r, g, b = _parse_hex(hexv)
                try:
                    curses.init_color(slot, _scale(r), _scale(g), _scale(b))
                    fg[name] = slot
                    slot += 1
                except curses.error:
                    fg[name] = _BASIC[name]
        elif colors >= 256:
            fg = {n: _to_256(*_parse_hex(h)) for n, h in _PALETTE.items()}
        else:
            fg = dict(_BASIC)
        for pair_id, role in _PAIR_ROLE.items():
            try:
                curses.init_pair(pair_id, fg[role], bg)
            except curses.error:
                pass
        grad = _gradient(GRAD_STEPS, _GRAD_STOPS)
        slot = 16 + len(_PALETTE)
        try:
            for i, (r, g, b) in enumerate(grad):
                if can_change:
                    curses.init_color(slot + i, _scale(r), _scale(g), _scale(b))
                    curses.init_pair(GRAD_PAIR_BASE + i, slot + i, bg)
                elif colors >= 256:
                    curses.init_pair(GRAD_PAIR_BASE + i, _to_256(r, g, b), bg)
                else:
                    break
                self._grad.append(GRAD_PAIR_BASE + i)
        except curses.error:
            self._grad = []
        self.has_color = True

    def attr(self, pair: int, bold: bool = False, dim: bool = False) -> int:
        a = curses.color_pair(pair) if self.has_color else curses.A_NORMAL
        if bold:
            a |= curses.A_BOLD
        if dim:
            a |= curses.A_DIM
        return a

    def grad_attr(self, frac: float, bold: bool = False) -> int:
        if not self.has_color:
            return curses.A_BOLD if bold else curses.A_NORMAL
        if self._grad:
            idx = max(0, min(len(self._grad) - 1, round(frac * (len(self._grad) - 1))))
            a = curses.color_pair(self._grad[idx])
        else:
            a = curses.color_pair(PAIR_RED if frac >= 0.85 else PAIR_YELLOW if frac >= 0.5 else PAIR_GREEN)
        return a | curses.A_BOLD if bold else a
