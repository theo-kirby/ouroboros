"""The run's findings: what the run now knows, told to a person, each tied to the memory graph.

The writer keeps `view/findings.json` in the run directory (the `ouroboros-findings`
skill says how). One finding is:

    {"id": "chunked-reading", "title": "Reading in chunks makes 50 MB take 1.4 s",
     "summary": "<at most three sentences, plain words>", "status": "holds",
     "claims": ["<claim ids>"], "records": ["<record ids>"],
     "visuals": [{"file": "assets/speed.png", "caption": "<what to look at>"}],
     "notes": "<markdown, the detail; shown folded>"}

The run page renders each one as its title, its summary, the part of the graph it
rests on (its claims, what hangs under them, and the records that carry them), its
visuals, and its notes. `check` says what is wrong with the file, for the next writer.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

FILE = "findings.json"
STATUSES = ("holds", "revised", "retracted")
IMAGE = (".png", ".svg", ".jpg", ".jpeg", ".webp", ".gif")
KEBAB = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
TITLE_MAX, SUMMARY_MAX, SENTENCES_MAX, NOTES_MAX, VISUALS_MAX = 120, 700, 3, 6000, 4
RECORDS_SHOWN = 24
# view/summary.txt, the page's opening: what the run is for, then where it stands, in at most
# this many sentences each
SUMMARY_FILE, SUMMARY_SHAPE = "summary.txt", (3, 2)


def path(run_dir: Path) -> Path:
    return Path(run_dir) / "view" / FILE


def load(run_dir: Path) -> list[dict]:
    """The findings, in the writer's order; [] when there is no file or it does not parse.
    Entries that are not objects with a title are skipped, never raised on."""
    try:
        data = json.loads(path(run_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    items = data.get("findings") if isinstance(data, dict) else data
    out = []
    for f in items if isinstance(items, list) else []:
        if isinstance(f, dict) and str(f.get("title") or "").strip():
            out.append(f)
    return out


def sentences(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.!?])\s+(?=[A-Z0-9(\"'`=])", text.strip()) if s.strip()]


def _sentences(text: str) -> int:
    return len(sentences(text))


def summary_paragraphs(text: str) -> list[str]:
    """The summary as the page shows it: its first two paragraphs, each cut to SUMMARY_SHAPE's sentences."""
    paras = [" ".join(p.split()) for p in re.split(r"\n\s*\n", text or "") if p.strip()]
    return [" ".join(sentences(p)[:n]) for p, n in zip(paras, SUMMARY_SHAPE)]


def check_summary(run_dir: Path) -> list[str]:
    """What is wrong with view/summary.txt's shape, one plain line each; [] when it is right or absent."""
    try:
        text = (path(run_dir).parent / SUMMARY_FILE).read_text(encoding="utf-8").strip()
    except OSError:
        return []
    paras = [p for p in re.split(r"\n\s*\n", text) if p.strip()]
    first, second = SUMMARY_SHAPE
    problems = []
    if len(paras) > 2:
        problems.append(f"{SUMMARY_FILE} has {len(paras)} paragraphs; two at most (the page shows two)")
    for i, (p, most) in enumerate(zip(paras, SUMMARY_SHAPE)):
        n = _sentences(p)
        if n > most:
            problems.append(f"{SUMMARY_FILE}: paragraph {i + 1} is {n} sentences; at most {most} "
                            f"(the page cuts it after {most})")
    return problems


def check(run_dir: Path, graph=None) -> list[str]:
    """What is wrong with findings.json and summary.txt, one plain line each; [] when they are right or absent."""
    p = path(run_dir)
    problems: list[str] = check_summary(run_dir)
    if not p.exists():
        return problems
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return problems + [f"{FILE} does not parse as JSON: {exc}"]
    items = data.get("findings") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return problems + [f'{FILE} must be {{"findings": [ ... ]}}']
    seen: set[str] = set()
    known = None
    if graph is not None and getattr(graph, "ok", False):
        known = {n.get("slug") or nid for nid, n in graph.nodes.items()} | set(graph.nodes)
    vdir = p.parent
    for i, f in enumerate(items):
        if not isinstance(f, dict):
            problems.append(f"finding {i + 1} is not an object")
            continue
        fid = str(f.get("id") or "")
        name = f"`{fid}`" if fid else f"finding {i + 1}"
        if not KEBAB.match(fid):
            problems.append(f"{name}: `id` must be kebab-case")
        elif fid in seen:
            problems.append(f"{name}: the id is used twice")
        seen.add(fid)
        title = str(f.get("title") or "").strip()
        if not title:
            problems.append(f"{name}: no `title`")
        elif len(title) > TITLE_MAX:
            problems.append(f"{name}: the title is {len(title)} characters; keep it under {TITLE_MAX}")
        summary = str(f.get("summary") or "").strip()
        if not summary:
            problems.append(f"{name}: no `summary`")
        elif _sentences(summary) > SENTENCES_MAX or len(summary) > SUMMARY_MAX:
            problems.append(f"{name}: the summary is {_sentences(summary)} sentences and {len(summary)} characters; "
                            f"at most {SENTENCES_MAX} and {SUMMARY_MAX} (detail goes in `notes`)")
        if str(f.get("status") or "holds") not in STATUSES:
            problems.append(f"{name}: `status` is one of {', '.join(STATUSES)}")
        claims = f.get("claims") if isinstance(f.get("claims"), list) else []
        if not claims:
            problems.append(f"{name}: name the claims it rests on in `claims`")
        for key in ("claims", "records"):
            for x in f.get(key) or []:
                if known is not None and str(x) not in known:
                    problems.append(f"{name}: `{x}` in `{key}` is not in the memory")
        visuals = f.get("visuals") if isinstance(f.get("visuals"), list) else []
        if len(visuals) > VISUALS_MAX:
            problems.append(f"{name}: {len(visuals)} visuals; at most {VISUALS_MAX}, the ones that tell it")
        for v in visuals:
            file = str((v or {}).get("file") or "") if isinstance(v, dict) else ""
            if not file:
                problems.append(f"{name}: a visual without `file`")
                continue
            if not file.lower().endswith(IMAGE):
                problems.append(f"{name}: `{file}` is not an image ({', '.join(IMAGE)})")
            elif not (vdir / file).is_file():
                problems.append(f"{name}: `{file}` does not exist (paths are relative to {vdir})")
            if not str(v.get("caption") or "").strip():
                problems.append(f"{name}: `{file}` has no caption")
        if len(str(f.get("notes") or "")) > NOTES_MAX:
            problems.append(f"{name}: `notes` is over {NOTES_MAX} characters")
    return problems[:20]


def related(graph, finding: dict) -> tuple[list[dict], list[dict]]:
    """(claims, records) of the graph a finding rests on, both in graph order.

    Claims: the ones it names and every claim under them. Records: the ones it
    names and every record those claims cite, the newest RECORDS_SHOWN kept."""
    if graph is None or not getattr(graph, "ok", False):
        return [], []
    claims: dict[str, dict] = {}
    todo = [graph.by_slug(str(x)) for x in finding.get("claims") or []]
    while todo:
        c = todo.pop()
        if c is None or c["id"] in claims or c.get("kind") != "state":
            continue
        claims[c["id"]] = c
        # everything under it, rejected hypotheses included: they are part of how it was found
        todo += [n for n in graph.nodes.values() if n.get("kind") == "state" and c["id"] in (n.get("parents") or [])]
    records: dict[str, dict] = {}
    for x in finding.get("records") or []:
        r = graph.by_slug(str(x))
        if r is not None and r.get("kind") == "record":
            records[r["id"]] = r
    for c in claims.values():
        for r in graph.cites(c):
            records[r["id"]] = r
    for e in graph.edges:   # impacts declared on these claims, folded or not
        if e.get("target") in claims and (graph.nodes.get(e.get("source", "")) or {}).get("kind") == "record":
            records[e["source"]] = graph.nodes[e["source"]]
    recs = sorted(records.values(), key=lambda n: n.get("seq", 0))[-RECORDS_SHOWN:]
    return sorted(claims.values(), key=lambda n: n.get("seq", 0)), recs
