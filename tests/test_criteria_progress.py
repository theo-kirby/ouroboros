import json

from ouroboros import criteria as progress
from ouroboros import history, view
from ouroboros.config import Config
from ouroboros.memory.graph import Graph

GOAL = '# Goal\n\n## Done criteria\n- [ ] The build passes.\n- [ ] The demo runs.\n'


def graph(status='working', slug='random-fern-1234'):
    return Graph({'root':'root','nodes': {
        'r':{'id':'r','kind':'record','content':'evidence','seq':1},
        'c':{'id':'c','slug':slug,'kind':'state','status':status,
             'content':'## Current\n<!-- ouroboros-criterion: gap-build-passes -->\nVerified', 'seq':2}},
        'edges':[{'source':'c','target':'r','content':'cites'}]})


def test_governor_random_ids_and_unrelated_claims_are_counted_by_identity():
    rows=progress.rows(graph(),GOAL)
    assert [r['status'] for r in rows]==['working','unknown']
    assert view._criteria_count(graph(),GOAL)==(1,2)


def test_fold_preserves_identity_when_title_and_content_change():
    plan={'claims':[{'slug':'random-fern-1234','current':'new evidence','status':'working'}]}
    tagged=progress.tag_fold(plan,graph())
    assert 'ouroboros-criterion: gap-build-passes' in tagged['claims'][0]['current']
    assert plan['claims'][0]['current']=='new evidence'
    created=progress.tag_fold({'claims':[{'new':'gap-demo-runs','current':'open'}]},Graph(None))
    assert 'ouroboros-criterion: gap-demo-runs' in created['claims'][0]['current']


def test_working_without_evidence_and_duplicate_identities_are_unknown():
    g=graph();g.edges=[]
    assert progress.rows(g,GOAL)[0]['status']=='unknown'
    g=graph();g.nodes['other']={**g.nodes['c'],'id':'other'}
    assert progress.rows(g,GOAL)[0]['status']=='unknown'


def test_run_snapshot_counts_memory_and_stays_isolated_from_later_runs(tmp_path):
    rd=tmp_path/'old';rd.mkdir()
    progress.save(rd,GOAL,graph('open'))
    progress.save(rd,GOAL,graph())
    facts=history.RunFacts(run='old')
    assert progress.add_facts(rd,facts)
    assert (facts.criteria_closed,facts.criteria_total,facts.criteria_ticked)==(1,2,1)
    assert facts.criteria_unknown==1
    other=tmp_path/'new';other.mkdir()
    progress.save(other,GOAL,graph('broken'))
    assert progress.add_facts(rd,facts) and facts.criteria_closed==1


def test_missing_or_invalid_snapshot_does_not_invent_memory_progress(tmp_path):
    assert not progress.add_facts(tmp_path,history.RunFacts(run='old'))
    (tmp_path/'criteria.json').write_text('{}')
    assert not progress.add_facts(tmp_path,history.RunFacts(run='old'))


def test_preexisting_success_and_reopened_criterion_do_not_count_as_new(tmp_path):
    progress.save(tmp_path,GOAL,graph())
    progress.save(tmp_path,GOAL,graph('broken'))
    progress.save(tmp_path,GOAL,graph())
    facts=history.RunFacts(run='continued')
    assert progress.add_facts(tmp_path,facts)
    assert facts.criteria_closed==1 and facts.criteria_ticked is None
    assert facts.criteria_unknown==1


def test_transient_graph_failure_preserves_last_snapshot(tmp_path):
    progress.save(tmp_path,GOAL,graph())
    before=(tmp_path/'criteria.json').read_bytes()
    progress.save(tmp_path,GOAL,None)
    assert (tmp_path/'criteria.json').read_bytes()==before


def test_legacy_memory_archive_is_unknown_not_zero(repo):
    from test_history import run_dir
    (repo/'.ouroboros/goal.md').write_text(GOAL)
    rd=run_dir(repo,'legacy',steps=[])
    facts=history.gather(repo,Config(run='legacy'),rd)
    assert facts.criteria_ticked is None
    assert facts.criteria_unknown==2
    assert 'memory completion unavailable' in history.digest(facts)


def test_unknown_baseline_cannot_be_credited_as_new_completion(tmp_path):
    progress.save(tmp_path,GOAL,Graph({'root':'root','nodes':{}}))
    progress.save(tmp_path,GOAL,graph())
    facts=history.RunFacts(run='legacy-live')
    assert progress.add_facts(tmp_path,facts)
    assert facts.criteria_closed==1 and facts.criteria_ticked is None


def test_new_directive_can_credit_a_criterion_met_by_its_first_fold(tmp_path):
    rd=tmp_path/'fresh';rd.mkdir()
    g=Graph({'root':'root','nodes':{}})
    progress.save(rd,GOAL,g)
    g.nodes['directive']={'id':'directive','kind':'record','seq':1,'folded':False,
        'title':'Ouroboros run: fresh [12345678] — operator directive',
        'content':'- NEW gap-build-passes — Charter gap, status open: The build passes.'}
    progress.save(rd,GOAL,g)
    progress.save(rd,GOAL,graph())
    facts=history.RunFacts(run='fresh')
    assert progress.add_facts(rd,facts)
    assert facts.criteria_closed==1 and facts.criteria_ticked==1


def test_render_to_archive_reports_folded_success_without_touching_charter(repo):
    from test_history import run_dir
    path=repo/'.ouroboros/goal.md';path.write_text(GOAL)
    original=path.read_bytes()
    rd=run_dir(repo,'accounted',steps=[])
    assert view.render(rd,repo,goal_text=GOAL,graph=graph('open'))
    assert view.render(rd,repo,goal_text=GOAL,graph=graph())
    facts=history.gather(repo,Config(run='accounted'),rd)
    assert (facts.criteria_closed,facts.criteria_ticked,facts.criteria_unknown)==(1,1,1)
    assert 'this run completed 1' in history.digest(facts)
    assert '1 unknown' in history.digest(facts)
    assert path.read_bytes()==original


def test_snapshot_with_only_unmapped_legacy_claims_is_not_zero_progress(tmp_path):
    progress.save(tmp_path,GOAL,Graph({'root':'root','nodes':{}}))
    facts=history.RunFacts(run='unknown')
    assert progress.add_facts(tmp_path,facts)
    assert facts.criteria_unknown==2 and facts.criteria_ticked is None
