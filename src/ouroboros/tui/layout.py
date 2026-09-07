"""A recursive split tree placed into terminal rects (from vllmtop)."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Dict, List, Optional, Set, Tuple

MIN_COLS = 70
MIN_LINES = 22


@dataclass(frozen=True)
class Rect:
    y: int
    x: int
    h: int
    w: int


@dataclass(frozen=True)
class Node:
    weight: int = 1
    panel: Optional[str] = None
    vertical: bool = False
    children: Tuple["Node", ...] = ()
    min_h: int = 0


def leaf(panel: str, weight: int = 1, min_h: int = 0) -> Node:
    return Node(weight=weight, panel=panel, min_h=min_h)


def row(*children: Node, weight: int = 1) -> Node:
    return Node(weight=weight, vertical=False, children=tuple(children))


def col(*children: Node, weight: int = 1) -> Node:
    return Node(weight=weight, vertical=True, children=tuple(children))


def _split_weighted(start: int, total: int, weights: List[int],
                    mins: Optional[List[int]] = None) -> List[Tuple[int, int]]:
    """Split `total` by weight, then raise any share below its minimum.

    A box shorter than its own border holds nothing, so a panel that asks for a floor
    gets it, paid for by whichever sibling has the most to spare. Weight decides the
    share; the minimum only decides whether the share is usable.
    """
    mins = list(mins or [0] * len(weights))
    tw = sum(weights) or 1
    segs, used = [], 0
    for i, wt in enumerate(weights):
        seg = (total - used) if i == len(weights) - 1 else (total * wt) // tw
        segs.append(seg)
        used += seg
    if sum(mins) <= total:
        for i, m in enumerate(mins):
            while segs[i] < m:
                j = max(range(len(segs)), key=lambda k: segs[k] - mins[k])
                if j == i or segs[j] - mins[j] <= 0:
                    break
                segs[j] -= 1
                segs[i] += 1
    out, pos = [], start
    for seg in segs:
        out.append((pos, seg))
        pos += seg
    return out


def prune(node: Optional[Node], available: Set[str]) -> Optional[Node]:
    if node is None:
        return None
    if node.panel is not None:
        return node if node.panel in available else None
    kids = [k for k in (prune(c, available) for c in node.children) if k]
    if not kids:
        return None
    if len(kids) == 1:
        return replace(kids[0], weight=node.weight, min_h=max(node.min_h, kids[0].min_h))
    return replace(node, children=tuple(kids))


def _place(node: Node, rect: Rect, out: Dict[str, Rect]) -> None:
    if node.panel is not None:
        out[node.panel] = rect
        return
    weights = [c.weight for c in node.children]
    if node.vertical:
        mins = [c.min_h for c in node.children]
        for child, (ry, rh) in zip(node.children, _split_weighted(rect.y, rect.h, weights, mins)):
            _place(child, Rect(ry, rect.x, rh, rect.w), out)
    else:
        for child, (rx, rw) in zip(node.children, _split_weighted(rect.x, rect.w, weights)):
            _place(child, Rect(rect.y, rx, rect.h, rw), out)


def compute_layout(rect: Rect, view: Node, available: Set[str]) -> Dict[str, Rect]:
    """Place the available panels of `view` into `rect`. Empty when nothing is available."""
    pruned = prune(view, available)
    out: Dict[str, Rect] = {}
    if pruned is not None and rect.h > 0 and rect.w > 0:
        _place(pruned, rect, out)
    return out
