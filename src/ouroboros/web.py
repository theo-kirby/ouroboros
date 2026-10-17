"""A small, read-only, loopback dashboard. No connection to the loop process."""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import json
import re
import math
import time
import subprocess
from collections import Counter
from functools import lru_cache

from .tui.state import load_snapshot, _read_jsonl


_DEMO_STARTED = time.time()

@lru_cache(maxsize=128)
def _commit_records(repo: str, sha: str) -> tuple:
    if not re.fullmatch(r"[0-9a-f]{7,40}", sha):
        return ()

    def git(*args):
        return subprocess.run(['git', '-C', repo, *args], capture_output=True,
                              text=True, timeout=2, check=True).stdout

    paths = git('diff-tree', '--root', '--no-commit-id', '--name-only', '-r', sha).splitlines()
    records = []
    for path in paths:
        if not path.endswith('.md') or not (path.startswith('.ouroboros/handoff/') or
                                            '/record/' in path):
            continue
        body = git('show', f'{sha}:{path}')[:16000]
        parts = re.split(r'^## (.+)\s*$', body, flags=re.MULTILINE)
        sections = {parts[i].strip(): parts[i+1].strip() for i in range(1, len(parts)-1, 2)}
        if not any(k in sections for k in ('Did', 'What')):
            continue
        records.append(dict(path=path, sections=sections))
        if len(records) == 3:
            break
    return tuple(records)


def work_records(repo: Path, steps: list[dict]) -> list[dict]:
    records = []
    commits = [s for s in steps if s.get('step') == 'commit' and s.get('recorded') and s.get('sha')]
    for step in reversed(commits[-5:]):
        try:
            records.extend(dict(record, iteration=step.get('iteration'))
                           for record in _commit_records(str(repo), step['sha']))
        except (OSError, subprocess.SubprocessError):
            continue
    return records


def metrics(iterations, decisions, steps):
    return dict(kept=sum(i.changed and not i.reverted for i in iterations),
                reverted=sum(i.reverted for i in iterations),
                recorded=sum(i.recorded for i in iterations),
                errors=sum(bool(s.get('error')) for s in steps),
                verdicts=dict(Counter(d.get('verdict', 'unknown') for d in decisions)))


def snapshot(run_dir: Path, repo: Path) -> dict:
    s = load_snapshot(run_dir, repo=repo)
    steps = _read_jsonl(run_dir / 'iterations.jsonl')
    return {
        'sampled_at': s.now,
        'resources': dict(cpu_pct=s.cpu_pct, rss_mb=s.rss_mb, processes=s.procs),
        'metrics': metrics(s.iterations, s.decisions, steps),
        'records': work_records(repo, steps),
        'feed': [asdict(e) for e in s.feed if e.kind != 'think'][-30:],
        'feed_role': s.feed_role, 'feed_age': s.feed_age,
        'stage_since': s.stage_since,
        'run': run_dir.name, 'status': s.status, 'alive': s.alive,
        'did': s.did, 'doing': s.doing,
        'message': s.last_message.text if s.last_message else '',
        'needs_human': s.needs_human,
        'iterations': [asdict(i) for i in s.iterations],
        'stages': {name: asdict(stat) for name, stat in s.stages.items()},
        'decisions': s.decisions[-5:],
    }


def demo_snapshot() -> dict:
    """Illustrative run data kept entirely in memory."""
    now = datetime.now(timezone.utc)
    durations = [132, 198, 165, 284, 213, 176, 320, 242, 189, 145, 267, 231,
                 194, 356, 218, 172, 249, 302, 185, 224, 291, 156, 208, 263]
    iterations = [dict(n=n, actor_seconds=seconds, changed=True,
                       reverted=n in (7, 14), verdict='reject' if n in (7, 14) else 'continue')
                  for n, seconds in enumerate(durations, 1)]
    descriptions = [
        'Added coverage for interrupted harness sessions.',
        'Simplified the handoff summary for the next actor.',
        'Fixed retry timing when a usage window resets.',
        'Verified that partial event records are safely skipped.',
        'Added tests for recovery after a failed critic call.',
    ]
    doing = 'Improving recovery when a harness disconnects mid-iteration.'
    return {
        'sampled_at': now.timestamp(),
        'resources': dict(cpu_pct=54 + 28*math.sin((now.timestamp()-_DEMO_STARTED)/6),
                          rss_mb=684 + 32*math.sin((now.timestamp()-_DEMO_STARTED)/15), processes=6),
        'metrics': dict(kept=22, reverted=2, recorded=24, errors=1,
                        verdicts={'continue': 22, 'reject': 2}),
        'stage_since': _DEMO_STARTED - 92,
        'feed_role': '0025-actor', 'feed_age': 0,
        'feed': [dict(kind='text', text=doing),
                 dict(kind='tool', text='$ uv run pytest -q tests/test_recovery.py'),
                 dict(kind='tool_out', text='12 passed in 1.42s'),
                 dict(kind='text', text='Recovery tests pass. Checking the disconnect handling next.')],
        'records': [dict(iteration=24, path='.ouroboros/handoff/0024.md', sections={
            'Did': descriptions[-1], 'Learned': 'A failed critic call must preserve the next-unit direction.',
            'Assumed': 'Transient failures can use the existing deterministic fallback.',
            'Next': doing})],
        'demo': True, 'run': 'overnight-demo', 'alive': True,
        'status': dict(state='work', iteration=25, elapsed_s=7842 + int(now.timestamp()-_DEMO_STARTED),
                       branch='ouroboros/recovery', harness='codex',
                       usage_compact='7d window · 34% used', ts=now.isoformat()),
        'did': descriptions[-1], 'doing': doing,
        'message': 'Running the recovery tests before checking the next change.',
        'needs_human': None, 'iterations': iterations,
        'stages': {name: dict(total=seconds, count=count, last=0)
                   for name, seconds, count in [('actor', sum(durations), 24),
                       ('critic', 1428, 24)]},
        'decisions': [dict(iteration=20+i, ts=(now - timedelta(minutes=(5-i)*6)).isoformat(),
                           verdict='continue', did=description, doing=doing,
                           reason='The change has focused test coverage and advances run recovery.',
                           reply=doing, fix_first='')
                      for i, description in enumerate(descriptions)],
    }


def make_server(run_dir: Path, repo: Path, *, port: int = 8765, demo: bool = False) -> ThreadingHTTPServer:
    page = Path(__file__).with_name('web.html').read_bytes()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            route = self.path.split('?', 1)[0]
            if route == '/':
                body, content_type, code = page, 'text/html; charset=utf-8', 200
            elif route == '/api/status':
                try:
                    body = json.dumps(demo_snapshot() if demo else snapshot(run_dir, repo)).encode()
                    code = 200
                except Exception:
                    # A concurrent file update must not take down the monitor.
                    body = b'{"error":"Run data is temporarily unavailable. Retrying shortly."}'
                    code = 503
                content_type = 'application/json'
            else:
                self.send_error(404)
                return
            self.send_response(code)
            self.send_header('Content-Type', content_type)
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            pass

    return ThreadingHTTPServer(('127.0.0.1', port), Handler)


def serve(run_dir: Path, repo: Path, *, port: int = 8765, demo: bool = False) -> None:
    with make_server(run_dir, repo, port=port, demo=demo) as server:
        print(f'Ouroboros dashboard: http://127.0.0.1:{server.server_port}', flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
