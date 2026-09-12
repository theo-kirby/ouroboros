"""Operator edits take effect between complete actor/critic iterations."""
from fake_harness import FakeHarness, works
from test_engine import make_engine
from ouroboros.roles.critic import RulesCritic


def test_edit_during_actor_reaches_next_actor_and_critic(repo):
    path = repo / '.ouroboros/goal.md'
    old = path.read_text()
    new = '# Goal: inspect live dashboard\n\nUse the dashboard during every experiment.\n'
    seen = []

    class Critic(RulesCritic):
        goal_text = old
        def judge(self, signals):
            seen.append(self.goal_text)
            return super().judge(signals)

    def edit(cwd, prompt):
        assert old.strip() in prompt
        path.write_text(new)
        return works('first')(cwd, prompt)

    h = FakeHarness([edit, works('second')])
    eng = make_engine(repo, h, max_iterations=2, resume_for=10, critic=Critic())
    eng.goal_path = path
    eng.run()
    assert seen == [old, new]
    assert new.strip() in h.prompts[1]
    assert eng.memory.goal_text == new
    assert h.calls[1]['resume'] is None
    assert eng.iteration == 2
    events = eng.recorder.read_jsonl(eng.recorder.iterations)
    assert len([e for e in events if e['step'] == 'goal_reload']) == 1


def test_invalid_or_missing_goal_keeps_last_good_version(repo):
    h = FakeHarness([works(), works(), works()])
    eng = make_engine(repo, h)
    eng.goal_path = repo / '.ouroboros/goal.md'
    old = eng.goal_text
    eng.goal_path.write_text('  ')
    eng.step()
    assert eng.goal_text == old
    eng.goal_path.unlink()
    eng.step()
    assert eng.goal_text == old
    eng.goal_path.write_text('# Goal: recovered\n\nInspect dashboard.\n')
    eng.step()
    assert 'Inspect dashboard.' in h.prompts[-1]


def test_failed_directive_registration_retries_before_adopting(repo, monkeypatch):
    eng = make_engine(repo, FakeHarness([works(), works(), works()]))
    eng.goal_path = repo / '.ouroboros/goal.md'
    old = eng.goal_text
    new = '# Goal: new review\n'
    eng.goal_path.write_text(new)
    start = eng.memory.start
    calls = []
    def fail_once(**kw):
        calls.append(kw['goal_text'])
        eng.memory.goal_text = kw['goal_text']
        if len(calls) == 1:
            raise OSError('record unavailable')
        start(**kw)
    monkeypatch.setattr(eng.memory, 'start', fail_once)
    eng.step()
    assert eng.goal_text == old
    assert eng.memory.goal_text == old
    eng.step()
    eng.step()
    assert eng.goal_text == new
    assert calls == [new, new]
