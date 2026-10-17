import json
import threading
from urllib.error import HTTPError
from urllib.request import urlopen

import pytest

from ouroboros.web import make_server, snapshot


def test_snapshot_empty_and_recorded_run(tmp_path):
    rd = tmp_path / '.ouroboros/runs/night'
    assert snapshot(rd, tmp_path)['status'] is None
    rd.mkdir(parents=True)
    (rd / 'status.json').write_text(json.dumps({'state': 'work', 'iteration': 2}))
    (rd / 'critic.jsonl').write_text(json.dumps({'did': 'Added tests', 'doing': 'Build dashboard'}) + '\n')
    (rd / 'iterations.jsonl').write_text('\n'.join(json.dumps(r) for r in [
        {'ts': '2026-01-01T00:00:00Z', 'iteration': 1, 'step': 'actor'},
        {'ts': '2026-01-01T00:01:00Z', 'iteration': 1, 'step': 'critique', 'verdict': 'accept'},
    ]) + '\n{"partial":')
    data = snapshot(rd, tmp_path)
    assert data['doing'] == 'Build dashboard'
    assert data['did'] == 'Added tests'
    assert data['iterations'][0]['verdict'] == 'accept'
    assert data['stages']['critic']['total'] == 60
    assert data['alive'] is False


def test_http_routes_do_not_serve_repo_files(tmp_path):
    server = make_server(tmp_path / 'run', tmp_path, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f'http://127.0.0.1:{server.server_port}'
    try:
        with urlopen(base + '/') as r:
            assert b'Ouroboros' in r.read()
        with urlopen(base + '/api/status') as r:
            assert r.headers['Cache-Control'] == 'no-store'
            assert json.load(r)['status'] is None
        for path in ['/../goal.md', '/status.json']:
            with pytest.raises(HTTPError) as exc:
                urlopen(base + path)
            assert exc.value.code == 404
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_web_command_selects_run_and_port(tmp_path, monkeypatch):
    from ouroboros import cli, web

    monkeypatch.setattr(cli, 'repo_root', lambda: tmp_path)
    calls = []
    monkeypatch.setattr(web, 'serve', lambda run_dir, repo, **kw: calls.append((run_dir, repo, kw)))
    assert cli.main(['web', '--run-name', 'night', '--port', '9123']) == 0
    assert calls == [(tmp_path / '.ouroboros/runs/night', tmp_path, {'port': 9123, 'demo': False})]


def test_demo_snapshot_is_labeled_and_does_not_write_files(tmp_path):
    from ouroboros.web import demo_snapshot

    data = demo_snapshot()
    assert data['demo'] is True
    assert data['status']['state'] == 'work'
    assert len(data['iterations']) == 24
    assert any(i['reverted'] for i in data['iterations'])
    assert len(data['decisions']) == 5
    assert data['doing']
    assert not list(tmp_path.iterdir())


def test_live_snapshot_updates_feed_decisions_and_counts(tmp_path):
    rd = tmp_path / 'run'
    (rd / 'transcripts').mkdir(parents=True)
    transcript = rd / 'transcripts/0001-actor.json'
    event = {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'Checking recovery behavior.'}}
    transcript.write_text(json.dumps(event) + '\n' + ' ' * 210)
    (rd / 'iterations.jsonl').write_text(json.dumps({'iteration': 1, 'step': 'actor', 'error': 'timeout'}) + '\n')
    first = snapshot(rd, tmp_path)
    assert first['feed'][-1]['text'] == 'Checking recovery behavior.'
    assert first['metrics']['errors'] == 1
    (rd / 'critic.jsonl').write_text(json.dumps({'iteration': 1, 'reason': 'Needs a regression test', 'fix_first': 'Add test', 'verdict': 'reject'}) + '\n')
    second = snapshot(rd, tmp_path)
    assert second['decisions'][-1]['fix_first'] == 'Add test'
    assert second['metrics']['verdicts'] == {'reject': 1}


def test_work_records_come_from_run_commits(tmp_path):
    import subprocess
    from ouroboros.web import work_records

    def git(*args):
        return subprocess.check_output(['git', '-C', str(tmp_path), *args], text=True).strip()

    git('init', '-q')
    git('config', 'user.name', 'Test')
    git('config', 'user.email', 'test@example.com')
    path = tmp_path / '.ouroboros/handoff/0001.md'
    path.parent.mkdir(parents=True)
    path.write_text('## Did\nFixed retries\n## Learned\nRetries need bounds\n## Assumed\nOffline\n## Next\nTest recovery\n')
    git('add', '.')
    git('commit', '-qm', 'unit')
    sha = git('rev-parse', 'HEAD')
    path.write_text('Unrelated current checkout content')
    records = work_records(tmp_path, [{'step': 'commit', 'iteration': 1, 'recorded': True, 'sha': sha}])
    assert records[0]['iteration'] == 1
    assert records[0]['sections']['Did'] == 'Fixed retries'
    assert records[0]['sections']['Next'] == 'Test recovery'
    assert work_records(tmp_path, []) == []


def test_snapshot_reports_measured_resources_without_a_running_process(tmp_path):
    data = snapshot(tmp_path / 'missing-run', tmp_path)
    assert data['resources'] == {'cpu_pct': 0.0, 'rss_mb': 0.0, 'processes': 0}
    assert data['sampled_at'] > 0
