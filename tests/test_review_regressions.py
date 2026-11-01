"""Consequential failures observed in the October research portfolio."""
import json
from types import SimpleNamespace

from ouroboros import cli, view
from ouroboros.config import Config
from ouroboros.memory.graph import GraphMemory
from ouroboros.recorder import Recorder
from ouroboros.roles.critic import RulesCritic, Signals, build_critic_prompt, parse_verdict
from ouroboros.watch import _iteration_counts


def test_recording_receipts_are_counted_once_across_old_and_new_formats():
    steps = [
        {'step': 'commit', 'iteration': 1, 'changed': True, 'recorded': True},
        {'step': 'scribe', 'iteration': 1, 'recorded': True},
        {'step': 'commit', 'iteration': 2, 'changed': True},
        {'step': 'scribe', 'iteration': 2, 'recorded': True},
        {'step': 'scribe', 'iteration': 2, 'recorded': True},
        {'step': 'commit', 'iteration': 3, 'changed': False},
        {'step': 'scribe', 'iteration': 3, 'recorded': False},
    ]
    assert '3 iterations, 2 changed, 2 recorded' in _iteration_counts(steps)


def test_critic_can_compare_previous_directions_not_only_verdicts():
    s = Signals('## What\nA result', True, 0, 0,
                history=[{'iteration': 1, 'verdict': 'continue', 'reason': 'Arithmetic holds',
                          'reply': 'Seek independent anatomy, not another assumed grid.',
                          'doing': 'Tests the missing anatomical evidence.',
                          'verification': 'unavailable', 'verification_evidence': 'Sandbox failed.'}])
    prompt = build_critic_prompt(goal_text='goal', s=s)
    for text in ['Arithmetic holds', 'Seek independent anatomy', 'Tests the missing', 'unavailable', 'Sandbox failed']:
        assert text in prompt


def test_old_and_rules_reviews_are_not_promoted_to_reproduced():
    old = parse_verdict('{"verdict":"continue","reason":"ok"}')
    assert old.verification == 'not_reported'
    assert RulesCritic().judge(Signals('## What\nx', True, 0, 0)).verification == 'rules_only'
    new = parse_verdict(json.dumps({'verdict': 'continue', 'verification': 'unavailable',
                                   'verification_evidence': 'The tool call never completed.'}))
    assert new.verification == 'unavailable'
    assert 'never completed' in new.verification_evidence
    invalid = parse_verdict('{"verdict":"continue","verification":"scientifically_proven"}')
    assert invalid.verification == 'not_reported'


def test_new_auxiliary_claims_do_not_reset_a_fixed_mission():
    goal = '## Done criteria\n- [ ] An answer exists.\n'
    main = {'id': 'a', 'slug': 'renamed-gap', 'status': 'open',
            'content': '<!-- ouroboros-criterion: gap-answer-exists -->'}
    claims = [main]
    memory = object.__new__(GraphMemory)
    memory.goal_text = goal
    memory.graph = lambda: SimpleNamespace(ok=True, claims=lambda kind: claims, cites=lambda node: [1])
    before = memory.frontier_fingerprint()
    claims.append({'id': 'b', 'slug': 'new-scenario', 'status': 'working'})
    assert memory.frontier_fingerprint() == before
    main['status'] = 'blocked'
    assert memory.frontier_fingerprint() != before
    blocked = memory.frontier_fingerprint()
    main['slug'] = 'another-display-name'
    assert memory.frontier_fingerprint() == blocked


def test_stop_finalizes_a_dead_process_and_refreshes_its_saved_page(repo, monkeypatch):
    monkeypatch.chdir(repo)
    cfg = Config(run='ended')
    (repo / '.ouroboros/config.yml').write_text(cfg.dump())
    rd = repo / '.ouroboros/runs/ended'
    rec = Recorder(rd, echo=lambda *_: None)
    rec.status(run='ended', state='work', iteration=4, started_at=100, elapsed_s=20)
    view.render(rd, repo, goal_text='## Mission\nTest stop')
    monkeypatch.setattr(cli, '_pid_alive', lambda _: (123, False))
    monkeypatch.setattr(cli, '_kill_launched', lambda *_: None)
    assert cli.main(['stop']) == 0
    assert rec.read_status()['state'] == 'killed'
    assert rec.read_status()['stop_reason'] == 'stopped by operator'
    assert json.loads((rd / 'view/live.json').read_text())['state'] == 'killed'


def test_final_render_failure_does_not_hide_terminal_status(repo, monkeypatch):
    monkeypatch.chdir(repo)
    cfg = Config(run='broken-view')
    (repo / '.ouroboros/config.yml').write_text(cfg.dump())
    rd = repo / '.ouroboros/runs/broken-view'
    rec = Recorder(rd, echo=lambda *_: None)
    rec.status(run=cfg.run, state='critique', iteration=2)
    monkeypatch.setattr(cli, '_pid_alive', lambda _: (123, False))
    monkeypatch.setattr(cli, '_kill_launched', lambda *_: None)
    monkeypatch.setattr(cli.runview, 'render', lambda *a, **kw: (_ for _ in ()).throw(RuntimeError('broken renderer')))
    assert cli.main(['stop']) == 0
    assert rec.read_status()['state'] == 'killed'
    assert (repo / '.ouroboros/history/broken-view.md').exists()


def test_history_uses_modern_scribe_receipts(repo):
    from ouroboros import history
    rd = repo / '.ouroboros/runs/r'
    rec = Recorder(rd, echo=lambda *_: None)
    rec.step(step='commit', iteration=1, changed=True)
    rec.step(step='scribe', iteration=1, recorded=True)
    rec.step(step='scribe', iteration=1, recorded=True)
    assert history.gather(repo, Config(run='r'), rd).recorded == 1


def test_verification_survives_engine_receipts_and_scribe_context(repo):
    from fake_harness import FakeHarness
    from ouroboros.harness.base import Result
    from ouroboros.roles.critic import Verdict
    from test_engine import make_engine
    class Critic:
        name = 'test'
        def judge(self, signals):
            return Verdict('continue', '', 'Useful inspection', verification='unavailable',
                           verification_evidence='Read-only execution failed.')
    actor = FakeHarness([Result(text='## What\nA bounded result')])
    engine = make_engine(repo, actor, critic=Critic())
    engine.step()
    d = engine.recorder.read_decisions()[0]
    assert d['verification'] == 'unavailable'
    assert d['verification_evidence'] == 'Read-only execution failed.'
    step = next(r for r in engine.recorder.read_jsonl(engine.recorder.iterations) if r['step'] == 'critique')
    assert step['verification'] == 'unavailable'
