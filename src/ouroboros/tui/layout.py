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


def leaf(panel: str, weight: int = 1) -> Node:
    return Node(weight=weight, panel=panel)


def row(*children: Node, weight: int = 1) -> Node:
    return Node(weight=weight, vertical=False, children=tuple(children))


def col(*children: Node, weight: int = 1) -> Node:
    return Node(weight=weight, vertical=True, children=tuple(children))


def _split_weighted(start: int, total: int, weights: List[int]) -> List[Tuple[int, int]]:
    tw = sum(weights) or 1
    out, pos, used = [], start, 0
    for i, wt in enumerate(weights):
        seg = (total - used) if i == len(weights) - 1 else (total * wt) // tw
        out.append((pos, seg))
        pos += seg
        used += seg
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
        return replace(kids[0], weight=node.weight)
    return replace(node, children=tuple(kids))


def _place(node: Node, rect: Rect, out: Dict[str, Rect]) -> None:
    if node.panel is not None:
        out[node.panel] = rect
        return
    weights = [c.weight for c in node.children]
    if node.vertical:
        for child, (ry, rh) in zip(node.children, _split_weighted(rect.y, rect.h, weights)):
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
