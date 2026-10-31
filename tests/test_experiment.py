import json
import os

import pytest

from ouroboros.experiment import ExperimentConfig, ExperimentRunner
from ouroboros.harness.base import Result
from ouroboros.memcli import allowed


def answer(done=False, handoff='next', **kw):
    return Result(text=json.dumps(dict(report='measured work', handoff=handoff, done=done)),
                  session_id='session', tokens={'input': 10, 'output': 2}, **kw)


class Script:
    name = 'fake'

    def __init__(self, results=()):
        self.results = list(results)
        self.calls = []

    def run(self, prompt, **kw):
        self.calls.append(dict(prompt=prompt, principal=os.environ.get('OUROBOROS_ROLE'), **kw))
        result = self.results.pop(0) if self.results else answer()
        if isinstance(result, BaseException):
            raise result
        if result.session_id == 'session':
            result.session_id += '-' + os.environ.get('OUROBOROS_EXPERIMENT_AGENT','unknown')
        return result


def runner(repo, tmp_path, topology='standard', results=(), **kwargs):
    h = Script(results)
    cfg = ExperimentConfig(topology=topology, harness='fake', model='test', max_calls=12,
                           confinement=kwargs.pop('confinement','none'), **kwargs)
    return ExperimentRunner(cfg, repo, tmp_path / 'control', harness=h), h


def test_planner_before_every_actor_and_never_writer(repo, tmp_path):
    r, h = runner(repo, tmp_path, 'planner', max_cycles=2)
    r.run()
    assert [x['agent'] for x in r.state['receipts']] == ['planner','actor','critic','scribe'] * 2
    assert h.calls[0]['tools'] == 'readonly'
    assert not allowed('record', h.calls[0]['principal'])
    assert 'next' in h.calls[1]['prompt']


def test_freeflow_persists_session_and_has_no_hidden_agents(repo, tmp_path):
    r, h = runner(repo, tmp_path, 'freeflow', results=[answer(), answer(True)])
    assert r.run() == 'done_claimed'
    assert [x['agent'] for x in r.state['receipts']] == ['researcher'] * 2
    assert h.calls[0]['resume'] is None and h.calls[1]['resume'] == 'session-researcher'
    assert all(allowed('record', x['principal']) for x in h.calls)
    assert r.state['done_source'] == 'self_review'


def test_peers_alternate_and_resume_their_own_context(repo, tmp_path):
    r, h = runner(repo, tmp_path, 'peers', max_cycles=4)
    r.run()
    assert [x['agent'] for x in r.state['receipts']] == ['peer_a','peer_b','peer_a','peer_b']
    assert [x['resume'] for x in h.calls] == [None,None,'session-peer_a','session-peer_b']
    assert all(x['principal'] == 'scribe' for x in h.calls)


def test_standard_actor_cannot_finish_without_critic_and_scribe(repo, tmp_path):
    r, h = runner(repo, tmp_path, results=[answer(True),answer(True),answer()])
    assert r.run() == 'done_claimed'
    assert [x['agent'] for x in r.state['receipts']] == ['actor','critic','scribe']
    assert r.state['done_source'] == 'critic_reported'


def test_all_agents_share_call_and_token_limits(repo, tmp_path):
    r, h = runner(repo, tmp_path, 'planner', max_tokens=24)
    assert r.run() == 'max_tokens'
    assert len(h.calls) == 2
    assert r.state['tokens'] == 24


def test_failed_attempt_is_charged_and_not_silently_advanced(repo, tmp_path):
    r, h = runner(repo, tmp_path, results=[answer(exit_code=1,error='failure'),answer()], max_failures=1)
    assert r.run() == 'failure_limit'
    assert r.state['calls'] == 1 and r.state['tokens'] == 12
    assert r.state['cursor'] == 0


def test_missing_meter_cannot_be_zero_spend(repo, tmp_path):
    r, h = runner(repo, tmp_path, results=[Result(text='x')], max_tokens=100)
    assert r.run() == 'meter_unavailable'


def test_persistent_agent_session_loss_does_not_start_fresh(repo, tmp_path):
    r, h = runner(repo, tmp_path, 'freeflow', results=[Result(text=answer().text,tokens={})])
    assert r.run() == 'session_unavailable'


def test_resume_preserves_budget_deadline_cursor_and_identity(repo, tmp_path):
    r, h = runner(repo, tmp_path, 'peers')
    r.step()
    before = r.state['started_at']
    resumed = ExperimentRunner(r.config, repo, tmp_path/'control', harness=h)
    assert resumed.state['started_at'] == before
    resumed.step()
    assert resumed.state['calls'] == 2
    assert [x['agent'] for x in resumed.state['receipts']] == ['peer_a','peer_b']
    resumed.step()
    assert h.calls[-1]['resume'] == 'session-peer_a'


def test_inflight_interrupt_is_not_reexecuted_on_restart(repo, tmp_path):
    r, h = runner(repo, tmp_path, 'freeflow', results=[KeyboardInterrupt()])
    assert r.run() == 'interrupted_inflight'
    resumed = ExperimentRunner(r.config, repo, tmp_path/'control', harness=h)
    assert resumed.run() == 'interrupted_inflight'
    assert len(h.calls) == 1


def test_changed_config_or_charter_refused_on_resume(repo, tmp_path):
    r, h = runner(repo, tmp_path)
    r.step()
    (repo/'.ouroboros/goal.md').write_text('different goal')
    with pytest.raises(ValueError, match='fingerprint'):
        ExperimentRunner(r.config, repo, tmp_path/'control', harness=h)


def test_remaining_wall_clamps_call_timeout_and_restart_does_not_reset_it(repo, tmp_path):
    r, h = runner(repo, tmp_path, after_seconds=10)
    r.now = lambda: r.state['started_at'] + 9
    r.step()
    assert 0 < h.calls[0]['timeout'] <= 1
    r.now = lambda: r.state['started_at'] + 11
    assert r.run() == 'deadline'
    assert len(h.calls) == 1


def test_malformed_completion_is_never_accepted(repo, tmp_path):
    r, h = runner(repo, tmp_path, 'freeflow', results=[Result(text='{"done":"yes"}',session_id='s',tokens={})], max_failures=1)
    assert r.run() == 'failure_limit'
    assert not r.state.get('done_source')


def test_readonly_roles_never_resume_into_writable_session(repo, tmp_path):
    r,h=runner(repo,tmp_path,'planner',max_cycles=2)
    r.run()
    assert all(c['resume'] is None for c in h.calls if c['tools']=='readonly')


def test_blind_first_passes_do_not_receive_other_solution(repo,tmp_path):
    import shutil
    a=tmp_path/'private-a';b=tmp_path/'private-b'
    shutil.copytree(repo,a);shutil.copytree(repo,b)
    h=Script([answer(handoff='secret-a'),answer(handoff='secret-b'),answer()])
    cfg=ExperimentConfig(topology='blind',harness='fake',model='test',max_calls=3)
    r=ExperimentRunner(cfg,repo,tmp_path/'control',harness=h,
                       private_workspaces={'solver_a':a,'solver_b':b})
    r.run()
    assert [x['agent'] for x in r.state['receipts']]==['solver_a','solver_b','actor']
    assert 'secret-a' not in h.calls[1]['prompt']
    assert 'secret-b' not in h.calls[0]['prompt']
    assert h.calls[0]['cwd']!=h.calls[1]['cwd']
    assert 'solver_a' in h.calls[2]['prompt'] and 'solver_b' in h.calls[2]['prompt']


def test_unknown_budget_fields_are_not_silently_ignored():
    with pytest.raises(ValueError):
        ExperimentConfig(model='test',token_buget=100)


def test_total_cost_stops_before_another_agent_call(repo,tmp_path):
    r,h=runner(repo,tmp_path,'planner',results=[answer(cost_usd=.2)],max_cost_usd=.1)
    assert r.run()=='max_cost_usd'
    assert len(h.calls)==1 and r.state['cost_usd']==pytest.approx(.2)


def test_provider_model_change_is_not_silent(repo,tmp_path):
    r,h=runner(repo,tmp_path,results=[answer(model='different-model')])
    assert r.run()=='model_changed'
    assert len(h.calls)==1


def test_persistent_resume_id_change_is_terminal(repo,tmp_path):
    r,h=runner(repo,tmp_path,'freeflow',results=[answer(),Result(text=answer().text,session_id='different',tokens={})])
    assert r.run()=='session_unavailable'
    assert len(h.calls)==2


def test_actor_does_not_get_memory_write_authority(repo,tmp_path):
    r,h=runner(repo,tmp_path,max_cycles=1)
    r.run()
    assert not allowed('record',h.calls[0]['principal'])
    assert not allowed('record',h.calls[1]['principal'])
    assert allowed('record',h.calls[2]['principal'])


def test_durable_result_recovered_without_repeating_call(repo,tmp_path,monkeypatch):
    r,h=runner(repo,tmp_path,'freeflow')
    original=r._apply
    monkeypatch.setattr(r,'_apply',lambda _: (_ for _ in ()).throw(OSError('crash after receipt')))
    with pytest.raises(OSError):r.step()
    restored=ExperimentRunner(r.config,repo,tmp_path/'control',harness=h)
    assert restored.state['calls']==1 and restored.state['cursor']==1
    assert restored.state['pending'] is None and len(h.calls)==1


def test_lock_refuses_second_runner_without_mutating_checkpoint(repo,tmp_path):
    import fcntl
    r,h=runner(repo,tmp_path)
    before=r.checkpoint.read_bytes()
    with (r.control/'runner.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        with pytest.raises(BlockingIOError):
            ExperimentRunner(r.config,repo,r.control,harness=h)
    assert r.checkpoint.read_bytes()==before


def test_reports_are_authored_in_workspace_not_private_control(repo,tmp_path):
    r,h=runner(repo,tmp_path,'freeflow',results=[answer(True)])
    report=repo/'.ouroboros/runs/experiment/view'
    report.mkdir(parents=True)
    (report/'story.html').write_text('<p>Measured answer</p>')
    r.run()
    assert str(report/'story.html') in h.calls[0]['prompt']
    assert (r.control/'view/story.html').read_text()=='<p>Measured answer</p>'


def test_landlock_denies_control_peer_and_outside_writes(tmp_path):
    import subprocess,sys
    from ouroboros import experiment_sandbox
    try:version=experiment_sandbox.abi()
    except OSError:pytest.skip('Landlock unavailable')
    if version<3:pytest.skip('Landlock ABI3 required')
    workspace=tmp_path/'workspace';workspace.mkdir()
    private=tmp_path/'private';private.write_text('secret answer')
    peer=tmp_path/'peer';peer.mkdir();(peer/'answer').write_text('peer answer')
    probe=workspace/'probe.py'
    probe.write_text('''from pathlib import Path
import sys
for file in sys.argv[1:]:
 try: Path(file).read_text()
 except PermissionError: pass
 else: raise AssertionError('private evidence visible: '+file)
Path('own-result').write_text('works')
try: Path('../outside').write_text('escape')
except PermissionError: pass
else: raise AssertionError('outside write succeeded')
''')
    command=[sys.executable,'-I',str(__import__('pathlib').Path(experiment_sandbox.__file__)),
             '--read','/usr','--read','/lib','--read',sys.prefix,'--write',str(workspace),
             '--',sys.executable,str(probe),str(private),str(peer/'answer')]
    p=subprocess.run(command,cwd=workspace,capture_output=True,text=True)
    assert p.returncode==0,p.stderr
    assert (workspace/'own-result').read_text()=='works'
    assert not (tmp_path/'outside').exists()


@pytest.mark.parametrize('topology', ['standard','planner','freeflow','peers','blind'])
def test_confined_subprocess_end_to_end(repo,tmp_path,topology):
    import shutil,sys
    from ouroboros.harness import backend_headless
    from ouroboros import experiment_sandbox
    try:
        if experiment_sandbox.abi()<3:pytest.skip('Landlock ABI3 required')
    except OSError:pytest.skip('Landlock unavailable')
    secret=tmp_path/'control/private-answer';other=tmp_path/'unrelated';other.write_text('hidden')
    script='''import json,os,sys
from pathlib import Path
prompt=sys.stdin.read()
for file in sys.argv[1:]:
 try: Path(file).read_text()
 except PermissionError: pass
 else: raise AssertionError('private source was readable')
agent=os.environ['OUROBOROS_EXPERIMENT_AGENT']
marker=Path('attempted-write')
if agent in ('planner','critic'):
 try: marker.write_text('forbidden')
 except PermissionError: pass
 else: raise AssertionError('read-only role wrote workspace')
else:
 Path('work-'+agent).write_text('completed')
home=Path.home(); own=home/'identity'
if own.exists(): assert own.read_text()==agent
own.write_text(agent)
print(json.dumps({'report':'real subprocess evidence','handoff':'next','done':False}))
'''
    class ProcessHarness:
        name='fixture'
        def run(self,prompt,**kw):
            p=backend_headless.run([sys.executable,'-I','-c',script,str(secret),str(other)],
                cwd=kw['cwd'],timeout=kw['timeout'],stdin_text=prompt,log_path=kw['log_path'])
            return Result(text=p.stdout,exit_code=p.exit_code,error=p.stderr or None,
                          timed_out=p.timed_out,session_id='thread-'+os.environ['OUROBOROS_EXPERIMENT_AGENT'],
                          tokens={'input':4,'output':3},cost_usd=0.,model='test')
    private={}
    if topology=='blind':
        for agent in ('solver_a','solver_b'):
            private[agent]=tmp_path/agent;shutil.copytree(repo,private[agent])
    cfg=ExperimentConfig(topology=topology,model='test',harness='fixture',max_calls=6,max_cycles=1)
    r=ExperimentRunner(cfg,repo,tmp_path/'control',harness=ProcessHarness(),private_workspaces=private)
    secret.write_text('private evaluation answer')
    reason=r.run()
    assert reason=='max_cycles',r.state
    assert all(x['ok'] for x in r.state['receipts']),r.state['receipts']
    assert (r.control/'view/index.html').is_file()
    assert r.state['tokens']==r.state['calls']*7
    if topology=='blind':
        assert (repo/'evidence/blind-first-pass/solver_a/work-solver_a').read_text()=='completed'
        assert (repo/'evidence/blind-first-pass/solver_b/work-solver_b').read_text()=='completed'


def test_charter_mutation_detected_before_next_call(repo,tmp_path):
    r,h=runner(repo,tmp_path)
    r.step()
    (repo/'.ouroboros/goal.md').write_text('agent altered charter')
    assert r.run()=='charter_modified'
    assert len(h.calls)==1


def test_confinement_read_allowlist_cannot_include_control(repo,tmp_path):
    r,h=runner(repo,tmp_path,read_paths=(str(tmp_path),),max_failures=1,confinement='landlock')
    assert r.run()=='failure_limit'
    assert not h.calls
    assert 'overlaps private' in r.state['receipts'][0]['error']


@pytest.mark.parametrize('text',['null','true','1','[]','"done"','{"done":true}'])
def test_non_envelope_json_never_claims_done(repo,tmp_path,text):
    r,h=runner(repo,tmp_path,'freeflow',results=[Result(text=text,session_id='s',tokens={})],max_failures=1)
    assert r.run()=='failure_limit'
    assert not r.state.get('done_source')


def test_unsuccessful_critic_does_not_publish_acceptance(repo,tmp_path):
    r,h=runner(repo,tmp_path,results=[answer(),answer(True,exit_code=1,error='failed')],max_failures=1)
    r.run()
    assert not r.recorder.read_decisions()


def test_environment_and_prefix_restored_after_call(repo,tmp_path,monkeypatch):
    from ouroboros.harness.backend_headless import COMMAND_PREFIX
    monkeypatch.setenv('OUROBOROS_ROLE','outer')
    monkeypatch.setenv('CODEX_HOME','/original-home')
    r,h=runner(repo,tmp_path,'freeflow',results=[answer(True)])
    r.run()
    assert os.environ['CODEX_HOME']=='/original-home'
    assert os.environ['OUROBOROS_ROLE']=='outer'
    assert COMMAND_PREFIX.get()==()


def test_input_hashes_frozen_and_mutation_stops_next_call(repo,tmp_path):
    import hashlib
    data=repo/'data.bin';data.write_bytes(b'data')
    manifest={'files':[{'path':'data.bin','bytes':4,'sha256':hashlib.sha256(b'data').hexdigest()}]}
    (repo/'inputs.json').write_text(json.dumps(manifest))
    from conftest import git
    git(repo,'add','data.bin','inputs.json');git(repo,'commit','-qm','freeze inputs')
    r,h=runner(repo,tmp_path,input_manifest='inputs.json')
    r.step();data.write_bytes(b'evil')
    assert r.run()=='input_modified'
    assert len(h.calls)==1


def test_failed_call_cannot_silently_replace_persistent_session(repo,tmp_path):
    r,h=runner(repo,tmp_path,'freeflow',results=[answer(),Result(text='',session_id='new-thread',tokens={},exit_code=1,error='lost original context')])
    assert r.run()=='session_unavailable'
    assert len(h.calls)==2


def test_new_run_refuses_dirty_historical_workspace(repo,tmp_path):
    (repo/'old-untracked-analysis.py').write_text('preserve me')
    before=(repo/'old-untracked-analysis.py').read_bytes()
    with pytest.raises(ValueError,match='clean'):
        runner(repo,tmp_path)
    assert (repo/'old-untracked-analysis.py').read_bytes()==before


# -- profiles and the publication (DESIGN.md section 23) ------------------------

def prompts_of(h, r, agent):
    return [c['prompt'] for c, x in zip(h.calls, r.state['receipts']) if x['agent'] == agent]


def test_a_notebook_arm_prompt_carries_no_profile_or_publication(repo, tmp_path):
    r, h = runner(repo, tmp_path, max_cycles=1)
    r.run()
    for c in h.calls:
        assert "This run's profile" not in c['prompt'] and 'consolidation' not in c['prompt']
        assert '{publication}' not in c['prompt'] and '{profile}' not in c['prompt']


def test_research_roles_get_their_guidance_and_the_scribe_consolidates_on_cadence(repo, tmp_path):
    r, h = runner(repo, tmp_path, max_cycles=4, profile='research', publication={'consolidate_every': 2})
    r.run()
    assert 'Every unit moves or tests the answer' in prompts_of(h, r, 'actor')[0]
    assert 'independent check' in prompts_of(h, r, 'critic')[0]
    assert 'The publication is kept by the memory writer' in prompts_of(h, r, 'actor')[0]
    scribe = prompts_of(h, r, 'scribe')
    assert all('the notebook.**' in p and 'figures.json' in p for p in scribe)
    assert ['The report is due' in p for p in scribe] == [False, False, True, True]   # cadence, then the closing call
    assert 'Fix these in the report first' in scribe[3] and 'paper.html is empty' in scribe[3]
    assert not any('The report is due' in p for p in prompts_of(h, r, 'actor'))
    assert len(h.calls) == 12                                             # no extra call for the final pass


def test_the_planner_reviews_with_the_critics_profile(repo, tmp_path):
    r, h = runner(repo, tmp_path / 'p', 'planner', max_cycles=1, profile='research')
    r.run()
    planner = prompts_of(h, r, 'planner')[0]
    assert 'independent check' in planner and 'Every unit moves' not in planner


def test_the_scribe_after_a_critics_done_does_the_final_pass(repo, tmp_path):
    r, h = runner(repo, tmp_path, profile='research', results=[answer(), answer(True), answer()])
    assert r.run() == 'done_claimed'
    assert 'The report is due' in prompts_of(h, r, 'scribe')[0]


def test_a_generalist_holds_both_profiles_and_consolidates_before_claiming_done(repo, tmp_path):
    r, h = runner(repo, tmp_path, 'freeflow', max_cycles=6, profile='development', results=[answer(), answer(True)])
    r.run()
    first = h.calls[0]['prompt']
    assert 'Use it as a user would' in first and 'demonstrated by running the product' in first
    assert 'If you will set done to true in this turn' in first and '`what`' in first


def test_unknown_publication_keys_are_refused():
    with pytest.raises(ValueError):
        ExperimentConfig(model='t', publication={'figures': {'main_maximum': 3}})
    assert ExperimentConfig(model='t', profile='research').template == 'paper'


def test_the_figure_ledger_reaches_the_dashboard(repo, tmp_path):
    r, h = runner(repo, tmp_path, max_cycles=1, profile='research')
    source = repo / '.ouroboros/runs/experiment/view'
    source.mkdir(parents=True, exist_ok=True)
    (source / 'figures.json').write_text('[]')
    r.run()
    assert (tmp_path / 'control/view/figures.json').read_text() == '[]'
