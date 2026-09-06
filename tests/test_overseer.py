import json

from ouroboros.config import StopConfig
from ouroboros.engine import exhaustion_policy
from ouroboros.roles.overseer import AgentOverseer, Signals, build_overseer_prompt, parse_verdict

from fake_harness import FakeHarness, claims_done, garbage, raises, verdict, works
from test_engine import make_engine

GOAL = """# Goal: g

## Mission

ship it

## Done criteria

- [ ] fizz.py exists

## Exhaustion policy

report_done
"""


def sig(text="ok", **kw) -> Signals:
    base = dict(text=text, changed=True, recorded=True, no_change_streak=0, error_streak=0, iteration=3, diff_stat=" a.py | 2 +-")
    base.update(kw)
    return Signals(**base)


def test_parse_verdict_lenient():
    assert parse_verdict('{"verdict":"answer","reply":"use A","reason":"x"}').verdict == "answer"
    v = parse_verdict('Sure! Here it is:\n```json\n{"verdict": "Continue", "reply": "", "reason": "fine"}\n```')
    assert v.verdict == "continue" and v.source == "agent"
    assert parse_verdict('{"verdict":"explode","reply":"","reason":""}') is None
    assert parse_verdict("no json here") is None


def test_prompt_contains_everything():
    s = sig("Which db should I use?", history=[{"iteration": 2, "verdict": "continue", "reason": "ok"}])
    p = build_overseer_prompt(goal_text=GOAL, s=s)
    for needle in ("ship it", "Which db should I use?", "a.py | 2 +-", "#2: continue", "ends with a question", "iteration 3"):
        assert needle in p


def test_agent_verdict_used_and_tools_off(tmp_path):
    h = FakeHarness([verdict("answer", "use sqlite", "policy says reversible")])
    o = AgentOverseer(h, goal_text=GOAL, cwd=tmp_path)
    v = o.judge(sig("Which db?"))
    assert (v.verdict, v.reply, v.source) == ("answer", "use sqlite", "agent")
    assert o.last_cost == 0.01
    assert h.calls[0]["tools"] == "none" and h.calls[0]["max_turns"] == 3 and h.calls[0]["json_schema"]


def test_garbage_twice_falls_back_to_rules(tmp_path):
    h = FakeHarness([garbage(), garbage()], default=verdict("continue"))
    o = AgentOverseer(h, goal_text=GOAL, cwd=tmp_path)
    v = o.judge(sig("Which one do you want?"))
    assert v.verdict == "answer" and v.source == "rules" and "fallback" in v.reason
    assert len(h.prompts) == 2 and "not valid JSON" in h.prompts[1]


def test_harness_exception_falls_back(tmp_path):
    h = FakeHarness([raises(), raises()])
    o = AgentOverseer(h, goal_text=GOAL, cwd=tmp_path)
    v = o.judge(sig("all tasks are complete"))
    assert v.verdict == "done_rejected" and v.source == "rules"


def test_exhaustion_policy_parse():
    assert exhaustion_policy(GOAL) == "report_done"
    assert exhaustion_policy("## Exhaustion policy\n\n**maintain**\n") == "maintain"
    assert exhaustion_policy("nothing") == "creative"


def test_done_accepted_report_done_stops(repo):
    actor = FakeHarness([claims_done(), works()])
    ov = AgentOverseer(FakeHarness([verdict("done_accepted", "looks done")], default=verdict("continue")), goal_text=GOAL, cwd=repo)
    eng = make_engine(repo, actor, overseer=ov, goal_text=GOAL, stop=StopConfig(max_iterations=5, on_done_accepted=1))
    reason = eng.run()
    assert "accepted done" in reason and eng.iteration == 1


def test_done_accepted_creative_continues(repo):
    goal = GOAL.replace("report_done", "creative")
    actor = FakeHarness([claims_done(), works(), works()])
    ov = AgentOverseer(FakeHarness([verdict("done_accepted", "yes")], default=verdict("continue")), goal_text=goal, cwd=repo)
    eng = make_engine(repo, actor, overseer=ov, goal_text=goal, max_iterations=3)
    eng.run()
    assert eng.iteration == 3
    assert "three new directions" in actor.prompts[1] and "yes" in actor.prompts[1]


def test_done_accepted_report_done_idles(repo):
    actor = FakeHarness([claims_done(), works(), works()])
    ov = AgentOverseer(FakeHarness([verdict("done_accepted", "yes")], default=verdict("continue")), goal_text=GOAL, cwd=repo)
    sleeps = []
    eng = make_engine(repo, actor, overseer=ov, goal_text=GOAL, max_iterations=2, sleeps=sleeps)
    eng.run()
    assert sleeps == [1800.0]


def test_overseer_bug_does_not_stop_loop(repo):
    class Broken:
        name = "broken"

        def judge(self, s):
            raise RuntimeError("bug")

    eng = make_engine(repo, FakeHarness([works(), works()]), overseer=Broken(), max_iterations=2)
    eng.run()
    assert eng.iteration == 2 and eng.outcomes[0].verdict.source == "rules"


def test_prompt_carries_memory_context():
    from ouroboros.roles.overseer import build_overseer_prompt
    s = sig(memory="### Frontier\n\n- [open] **gap-build** (`x-1`)")
    prompt = build_overseer_prompt(goal_text="# g", s=s)
    assert "## What is true now" in prompt and "gap-build" in prompt
    assert "(no memory context)" in build_overseer_prompt(goal_text="# g", s=sig())
