"""`ouroboros mem`: the only memory surface an agent sees.

Every verb does the same thing on either store, so a prompt never says which
store it is talking to. Exit codes follow governor: 0 done, 1 `check` found a
violation, 2 nothing was written (the message says why).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .memory import store_for_cli
from .memory.graph import Graph, parse_pair, tail_listing

FOLD_SHAPE = (
    '{"claims": [{"slug": "<claim-id>", "status": "working", "current": "- … [rec: <record-id>]", '
    '"negative": "- …", "cites": ["<record-id>"]}, {"new": "<name>", "parent": "root", "title": "<Title>", '
    '"status": "open", "current": "- …", "cites": ["<record-id>"]}], "folded": ["<every tail record id>"]}'
)


def add_parser(sub) -> None:
    m = sub.add_parser("mem", help="the run's memory: orient, show, record, fold, check, render")
    verbs = m.add_subparsers(dest="verb", required=True)
    verbs.add_parser("orient", help="what is true now: frontier, tail, architecture, recent records")
    s = verbs.add_parser("show", help="one record or claim, whole")
    s.add_argument("id")
    r = verbs.add_parser("record", help="append one record with its declared impact")
    r.add_argument("--title", required=True)
    r.add_argument("--body", default="-", help="a file, or - for stdin (the default)")
    r.add_argument("--parent", help="the causal parent record (default: the newest record)")
    r.add_argument("--impact", action="append", default=[], metavar="'<claim>: <delta>'")
    r.add_argument("--new", action="append", default=[], metavar="'<name>: <delta>'")
    r.add_argument("--none", metavar="REASON")
    f = verbs.add_parser("fold", help="list the tail; --apply FILE writes a fold and renders STATE.md")
    f.add_argument("--apply", metavar="FILE", help="the fold as JSON (- for stdin)")
    verbs.add_parser("check", help="the memory's rules")
    verbs.add_parser("render", help="write STATE.md")


def run(args: argparse.Namespace, repo: Path, configured: str) -> int:
    try:
        store = store_for_cli(repo, configured)
    except ValueError as exc:
        print(f"ouroboros mem: {exc}", file=sys.stderr)
        return 2
    try:
        return _VERBS[args.verb](store, args)
    except ValueError as exc:
        print(f"ouroboros mem {args.verb}: {exc}", file=sys.stderr)
        return 2


def _read(path: str) -> str:
    return sys.stdin.read() if path == "-" else Path(path).read_text()


def _orient(store, args) -> int:
    print(store.orient())
    return 0


def _show(store, args) -> int:
    g: Graph = store.snapshot()
    n = g.by_slug(args.id)
    if n is None:
        print(f"ouroboros mem show: no record or claim `{args.id}`", file=sys.stderr)
        return 2
    status = f" [{n['status']}]" if n.get("status") else ""
    folded = " (folded)" if n.get("kind") == "record" and n.get("folded") else ""
    print(f"# {n.get('title', '')}\n\n`{g.label(n['id'])}` {n.get('kind')}{status}{folded}; "
          f"parents: {', '.join(g.label(p) for p in n.get('parents') or []) or '-'}\n\n{(n.get('content') or '').strip()}")
    return 0


def _record(store, args) -> int:
    impacts = [parse_pair(x, "--impact") for x in args.impact]
    news = [parse_pair(x, "--new") for x in args.new]
    body = _read(args.body)
    parent = args.parent
    if not parent:
        records = store.snapshot().records()
        parent = records[-1].get("slug") if records else "root"
    slug, message = store.record(title=args.title, body=body, parent=parent, impacts=impacts, news=news,
                                 none=args.none)
    if slug is None:
        print(f"nothing written:\n{message}", file=sys.stderr)
        return 2
    print(message)
    return 0


def _fold(store, args) -> int:
    if not args.apply:
        count, listing = tail_listing(store.snapshot())
        print(listing)
        if count:
            print(f"\nApply with `ouroboros mem fold --apply fold.json`, shaped:\n{FOLD_SHAPE}")
        return 0
    try:
        plan = json.loads(_read(args.apply))
    except json.JSONDecodeError as exc:
        print(f"nothing written: the fold is not JSON ({exc})", file=sys.stderr)
        return 2
    if not isinstance(plan, dict):
        print("nothing written: a fold is a JSON object", file=sys.stderr)
        return 2
    ok, message = store.fold(plan)
    if not ok:
        print(f"nothing written:\n{message}", file=sys.stderr)
        return 2
    print(message)
    return 0


def _check(store, args) -> int:
    ok, text = store.check()
    print(text.strip() or ("ok" if ok else "failed"))
    return 0 if ok else 1


def _render(store, args) -> int:
    if not store.render():
        print(f"ouroboros mem render: {getattr(store, 'last_error', '') or 'failed'}", file=sys.stderr)
        return 2
    print(f"wrote {store.state_md}")
    return 0


_VERBS = {"orient": _orient, "show": _show, "record": _record, "fold": _fold, "check": _check, "render": _render}
