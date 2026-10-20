"""Read-only criterion accounting, independent of store-specific claim IDs."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path

from . import goal

MARKER = re.compile(r'<!-- ouroboros-criterion: (gap-[a-z0-9-]+) -->')


def identity(node: dict) -> str | None:
    match = MARKER.search(node.get('content') or '')
    if match:
        return match[1]
    slug = node.get('slug') or node.get('id') or ''
    return slug if slug.startswith('gap-') else None


def tag_fold(plan: dict, graph) -> dict:
    """Keep criterion identity through a fold, even when Governor renames claims."""
    plan = copy.deepcopy(plan)
    if not isinstance(plan.get('claims'), list):
        return plan
    for claim in plan['claims']:
        if not isinstance(claim, dict):
            continue
        slug = claim.get('slug')
        node = graph.by_slug(slug) if isinstance(slug, str) else None
        key = identity(node) if node else claim.get('new')
        if isinstance(key, str) and re.fullmatch(r'gap-[a-z0-9-]+', key) and isinstance(claim.get('current', ''), str):
            current = MARKER.sub('', claim.get('current') or '').strip()
            claim['current'] = f'<!-- ouroboros-criterion: {key} -->\n{current}'
    return plan


def rows(graph, text: str) -> list[dict]:
    candidates: dict[str, list] = {}
    if graph is not None and getattr(graph, 'ok', False):
        for node in graph.claims('state'):
            key = identity(node)
            if key:
                candidates.setdefault(key, []).append(node)
    result, seen = [], {}
    unchecked = goal.done_criteria(text)
    for index, criterion in enumerate(goal.done_criteria(text, include_checked=True)):
        checked = criterion not in unchecked
        key = goal.gap_name(criterion)
        if not checked:
            seen[key] = seen.get(key, 0) + 1
            if seen[key] > 1:
                key += f'-{seen[key]}'
        matches = candidates.get(key, [])
        node = matches[0] if len(matches) == 1 else None
        status = 'checked' if checked else (node or {}).get('status', 'unknown')
        if status == 'working' and not graph.cites(node):
            status = 'unknown'
        result.append({'key': f'checked-{index}' if checked else key, 'text': criterion,
                       'status': status, 'met': status in ('checked', 'working'),
                       'title': (node or {}).get('title') or criterion, 'current': (node or {}).get('content') or ''})
    return result


def save(run_dir: Path, text: str, graph) -> None:
    """Freeze run-local counts; archiving never consults a later run's live graph."""
    if graph is None or not getattr(graph, 'ok', False):
        return  # A transient read failure must not erase the last evidenced result.
    path = run_dir / 'criteria.json'
    digest = hashlib.sha256(text.encode()).hexdigest()
    current = rows(graph, text)
    try:
        previous = json.loads(path.read_text())
    except (OSError, ValueError):
        previous = {}
    # Directive edits keep the baseline of unchanged identities; new gaps start open.
    if not isinstance(previous, dict):
        previous = {}
    initial = previous.get('initial', {})
    if not isinstance(initial, dict):
        initial = {}
    directives = [r for r in graph.records() if not r.get('folded')
                  and (r.get('title') or '').startswith(f'Ouroboros run: {run_dir.name} [')]
    for row in current:
        key = row['key']
        if key not in initial or (initial[key] == 'unknown' and not row['met']):
            initial[key] = row['status']
        if initial[key] == 'unknown' and any(
            re.search(rf'^- NEW {re.escape(key)} — Charter gap, status open:', r.get('content') or '', re.M)
            for r in directives
        ):
            initial[key] = 'open'  # This run explicitly declared a new, not-yet-folded gap.
    data = {'version': 1, 'goal_sha256': digest, 'initial': initial, 'rows': current}
    fd, temporary = tempfile.mkstemp(prefix='.criteria-', suffix='.tmp', dir=run_dir)
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(json.dumps(data, indent=2) + '\n')
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def add_facts(run_dir: Path, facts) -> bool:
    try:
        data = json.loads((run_dir / 'criteria.json').read_text())
        if data.get('version') != 1 or not isinstance(data.get('initial'), dict):
            return False
        values = data['rows']
        if not isinstance(values, list) or any(
            not isinstance(r, dict) or not isinstance(r.get('key'), str)
            or not isinstance(r.get('met'), bool) or r.get('status') not in
            ('checked', 'working', 'open', 'broken', 'blocked', 'superseded', 'unknown') for r in values
        ):
            return False
        facts.criteria_total = len(values)
        facts.criteria_closed = sum(r['met'] for r in values)
        facts.criteria_unknown = sum(r['status'] == 'unknown' for r in values)
        met = [r for r in values if r['met']]
        initial = data['initial']
        facts.criteria_ticked = (None if any(initial.get(r['key'], 'unknown') == 'unknown' for r in met)
                                 else sum(initial[r['key']] not in ('checked', 'working') for r in met))
        if facts.criteria_ticked == 0 and facts.criteria_unknown:
            facts.criteria_ticked = None
        facts.criteria_source = 'run-local memory snapshot'
        return True
    except (OSError, ValueError, KeyError, TypeError):
        return False
