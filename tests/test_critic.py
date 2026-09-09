"""The critic: one read-only call that grades the diff, answers as the user, and writes the next prompt."""

from __future__ import annotations

import json
from pathlib import Path

from ouroboros.config import StopConfig
from ouroboros.engine import exhaustion_policy
from ouroboros.harness.base import Result
from ouroboros.roles.critic import AgentCritic, Signals, build_critic_prompt, parse_verdict

from conftest import git
from fake_harness import FakeHarness, claims_done, garbage, raises, verdict, works
from test_engine import make_engine

GOAL = """# Goal: g

## Mission

ship it

## Done criteria

- [ ] fizz.py exists

## Constraints

- never delete tests

## Exhaustion policy

report_done

## Quality bar

- tests pass
"""


def sig(text="ok", **kw) -> Signals:
    base = dict(text=text, changed=True, recorded=True, no_change_streak=0, error_streak=0, iteration=3,
                diff_stat=" a.py | 2 +-", diff="+a")
    base.update(kw)
    return Signals(**base)


def critique(v: str, reply: str = "", fix_first: str = "", reason: str = "r"):
    return lambda cwd, prompt: Result(text=json.dumps({"verdict": v, "reply": reply, "reason": reason,
                                                       "did": "d", "doing": "c", "fix_first": fix_first}), cost_usd=0.02)


# -- parsing and the prompt -------------------------------------------------

def test_parse_verdict_lenient():
    assert parse_verdict('{"verdict":"answer","reply":"use A","reason":"x"}').verdict == "answer"
    v = parse_verdict('Sure! Here it is:\n```json\n{"verdict": "Continue", "reply": "", "reason": "fine"}\n```')
    assert v.verdict == "continue" and v.source == "agent"
    assert parse_verdict('{"verdict":"explode","reply":"","reason":""}') is None
    assert parse_verdict("no json here") is None


def test_parse_reads_the_older_verdict_names_as_what_they_meant():
    assert parse_verdict('{"verdict": "REJECT", "reply": "fix a.py", "reason": "one", "must_fix": "fix a.py"}').rejected
    assert parse_verdict('{"verdict": "revert", "reply": "", "reason": ""}').verdict == "reject"
    assert parse_verdict('{"verdict": "accept", "reply": "", "reason": ""}').verdict == "continue"
    v = parse_verdict('{"verdict": "continue", "reply": "", "reason": "", "fix_first": ["link rec-1", "fix impact"]}')
    assert v.fix_first == "link rec-1\nfix impact"


def test_prompt_contains_everything():
    s = sig("Which db should I use?", history=[{"iteration": 2, "verdict": "continue", "reason": "ok"}],
            memory="### Frontier\n\n- [open] **gap-build** (`x-1`)")
    p = build_critic_prompt(goal_text=GOAL, s=s)
    for needle in ("ship it", "never delete tests", "tests pass", "Which db should I use?", "a.py | 2 +-", "+a",
                   "#2: continue", "ends with a question", "iteration 3", "## What is true now", "gap-build"):
        assert needle in p
    assert "(no memory context)" in build_critic_prompt(goal_text="# g", s=sig())
    assert "(no diff)" in build_critic_prompt(goal_text="# g", s=sig(diff=""))


def test_a_housekeeping_iteration_is_named_in_the_signals():
    s = sig("reconciled", recorded=False, housekeeping=True)
    assert "housekeeping iteration" in s.describe()


# -- the agent call -----------------------------------------------------------

def test_agent_verdict_used_with_readonly_tools(tmp_path):
    h = FakeHarness([verdict("answer", "use sqlite", "policy says reversible")])
    c = AgentCritic(h, goal_text=GOAL, cwd=tmp_path)
    v = c.judge(sig("Which db?"))
    assert (v.verdict, v.reply, v.source) == ("answer", "use sqlite", "critic:fake")
    assert c.last_cost == 0.01
    assert h.calls[0]["tools"] == "readonly" and h.calls[0]["max_turns"] == 12 and h.calls[0]["json_schema"]


def test_garbage_twice_falls_back_to_rules(tmp_path):
    h = FakeHarness([garbage(), garbage()], default=verdict("continue"))
    c = AgentCritic(h, goal_text=GOAL, cwd=tmp_path)
    v = c.judge(sig("Which one do you want?"))
    assert v.verdict == "answer" and v.source == "rules" and "fallback" in v.reason
    assert len(h.prompts) == 2 and "not valid JSON" in h.prompts[1]


def test_harness_exception_falls_back(tmp_path):
    h = FakeHarness([raises(), raises()])
    c = AgentCritic(h, goal_text=GOAL, cwd=tmp_path)
    v = c.judge(sig("all tasks are complete"))
    assert v.verdict == "done_rejected" and v.source == "rules"


def test_rules_do_not_demand_a_record_from_housekeeping(tmp_path):
    from ouroboros.roles.critic import RulesCritic
    assert "handoff" in RulesCritic().judge(sig("x", recorded=False)).reply
    v = RulesCritic().judge(sig("x", recorded=False, housekeeping=True))
    assert v.verdict == "continue" and v.reply == "" and v.reason == "housekeeping done"


# -- in the loop --------------------------------------------------------------

def test_the_critic_runs_every_iteration_and_sees_the_diff(repo: Path):
    from fake_harness import nothing
    actor = FakeHarness([works("good"), nothing()])
    critic_h = FakeHarness([], default=critique("continue"))
    eng = make_engine(repo, actor, max_iterations=2, critic=AgentCritic(critic_h, goal_text=GOAL, cwd=repo))
    eng.run()
    assert len(critic_h.prompts) == 2                       # changed or not, the critic is called
    assert "+good" in critic_h.prompts[0]                    # the diff itself, not only its stat
    assert "(no diff)" in critic_h.prompts[1]
    steps = eng.recorder.read_jsonl(eng.recorder.iterations)
    assert [s["verdict"] for s in steps if s.get("step") == "critique"] == ["continue", "continue"]
    assert not any(s.get("step") == "oversee" for s in steps)
    assert abs(eng.budget.cost_usd - 0.54) < 1e-9            # actor 0.5 + critic 2 x 0.02: both count


def test_reject_reverts_and_steers(repo: Path):
    actor = FakeHarness([works("good"), works("bad"), works("fixed")])
    critic_h = FakeHarness([critique("continue"), critique("reject", "restore the deleted test", reason="removed a test"),
                            critique("continue")])
    eng = make_engine(repo, actor, max_iterations=3, critic=AgentCritic(critic_h, goal_text=GOAL, cwd=repo))
    eng.goal_text = GOAL
    eng.run()
    steps = eng.recorder.read_jsonl(eng.recorder.iterations)
    assert any(s.get("step") == "revert" for s in steps)
    assert "restore the deleted test" in actor.prompts[2]                      # the fix reached the next actor
    assert "bad" not in (repo / "work.txt").read_text() and "fixed" in (repo / "work.txt").read_text()
    assert eng.outcomes[1].verdict.rejected and "removed a test" in eng.outcomes[1].verdict.reason
    assert git(repo, "tag").splitlines() == ["ouroboros/t/ok-0001", "ouroboros/t/ok-0003"]   # no tag for the reject


def test_reject_without_revert_keeps_the_commit_and_gives_no_ok_tag(repo: Path):
    actor = FakeHarness([works("good"), works("bad"), works("fixed")])
    critic_h = FakeHarness([critique("continue"), critique("reject", "fix it"), critique("continue")])
    eng = make_engine(repo, actor, max_iterations=3, critic=AgentCritic(critic_h, goal_text=GOAL, cwd=repo))
    eng.config.git.revert_on_reject = False
    eng.run()
    assert "bad" in (repo / "work.txt").read_text()
    assert "The critic rejected the last iteration. Fix this first:\nfix it" in actor.prompts[2]
    assert git(repo, "tag").splitlines() == ["ouroboros/t/ok-0001", "ouroboros/t/ok-0003"]
    assert not any(s.get("step") == "revert" for s in eng.recorder.read_jsonl(eng.recorder.iterations))


def test_fix_first_reaches_the_actor_before_its_unit(repo: Path):
    actor = FakeHarness([works(), works()])
    critic_h = FakeHarness([critique("continue", "then do B", fix_first="record rec-7 cites a slug that does not exist"),
                            critique("continue")])
    eng = make_engine(repo, actor, max_iterations=2, critic=AgentCritic(critic_h, goal_text=GOAL, cwd=repo))
    eng.run()
    assert "Fix these first, before the unit:\nrecord rec-7 cites a slug" in actor.prompts[1]
    assert actor.prompts[1].index("rec-7") < actor.prompts[1].index("then do B")
    d = eng.recorder.read_decisions()[0]
    assert d["fix_first"].startswith("record rec-7") and d["source"] == "critic:fake" and "overseer" not in d


def test_critic_bug_does_not_stop_loop(repo):
    class Broken:
        name = "broken"

        def judge(self, s):
            raise RuntimeError("bug")

    eng = make_engine(repo, FakeHarness([works(), works()]), critic=Broken(), max_iterations=2)
    eng.run()
    assert eng.iteration == 2 and eng.outcomes[0].verdict.source == "rules"


# -- done and the exhaustion policy ------------------------------------------

def test_exhaustion_policy_parse():
    assert exhaustion_policy(GOAL) == "report_done"
    assert exhaustion_policy("## Exhaustion policy\n\n**maintain**\n") == "maintain"
    assert exhaustion_policy("nothing") == "creative"


def test_done_accepted_report_done_stops(repo):
    actor = FakeHarness([claims_done(), works()])
    c = AgentCritic(FakeHarness([verdict("done_accepted", "looks done")], default=verdict("continue")), goal_text=GOAL, cwd=repo)
    eng = make_engine(repo, actor, critic=c, goal_text=GOAL, stop=StopConfig(max_iterations=5, on_done_accepted=1))
    reason = eng.run()
    assert "accepted done" in reason and eng.iteration == 1


def test_done_accepted_creative_continues(repo):
    goal = GOAL.replace("report_done", "creative")
    actor = FakeHarness([claims_done(), works(), works()])
    c = AgentCritic(FakeHarness([verdict("done_accepted", "yes")], default=verdict("continue")), goal_text=goal, cwd=repo)
    eng = make_engine(repo, actor, critic=c, goal_text=goal, max_iterations=3)
    eng.run()
    assert eng.iteration == 3
    assert "three new directions" in actor.prompts[1] and "yes" in actor.prompts[1]


def test_done_accepted_report_done_idles(repo):
    actor = FakeHarness([claims_done(), works(), works()])
    c = AgentCritic(FakeHarness([verdict("done_accepted", "yes")], default=verdict("continue")), goal_text=GOAL, cwd=repo)
    sleeps = []
    eng = make_engine(repo, actor, critic=c, goal_text=GOAL, max_iterations=2, sleeps=sleeps)
    eng.run()
    assert sleeps == [1800.0]
