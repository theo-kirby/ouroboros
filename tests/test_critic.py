"""actor-critic and council modes: a reject reverts the iteration and steers the next one."""

from __future__ import annotations

import json
from pathlib import Path

from ouroboros.harness.base import Result
from ouroboros.roles.critic import Council, Critic, build_critic_prompt, parse_critique

from conftest import git
from fake_harness import FakeHarness, works
from test_engine import make_engine

GOAL = "# Goal: g\n\n## Mission\n\nm\n\n## Constraints\n\n- never delete tests\n\n## Quality bar\n\n- tests pass\n"


def critique(v: str, must_fix: str = "", reasons=("r",)):
    return lambda cwd, prompt: Result(text=json.dumps({"verdict": v, "reasons": list(reasons), "must_fix": must_fix}), cost_usd=0.02)


def test_parse_and_prompt():
    c = parse_critique('noise {"verdict": "REJECT", "reasons": "one", "must_fix": "fix a.py"} noise')
    assert c.rejected and c.reasons == ["one"] and c.must_fix == "fix a.py"
    assert parse_critique("nope") is None
    p = build_critic_prompt(goal_text=GOAL, iteration=4, actor_output="did x", diff="+a")
    assert "tests pass" in p and "never delete tests" in p and "did x" in p and "+a" in p and "iteration 4" in p


def test_critic_fails_open(tmp_path: Path):
    c = Critic(FakeHarness([lambda cwd, p: Result(text="garbage")] * 2), goal_text=GOAL, cwd=tmp_path)
    assert c.grade(iteration=1, actor_output="", diff="").verdict == "accept"
    c = Critic(FakeHarness([lambda cwd, p: Result(text="", exit_code=1, error="boom")] * 2), goal_text=GOAL, cwd=tmp_path)
    assert c.grade(iteration=1, actor_output="", diff="").verdict == "accept"


def test_council_majority(tmp_path: Path):
    def crit(v):
        return Critic(FakeHarness([critique(v)]), goal_text=GOAL, cwd=tmp_path, name=f"c-{v}")
    assert Council([crit("reject"), crit("accept"), crit("reject")]).grade(iteration=1, actor_output="", diff="").rejected
    assert not Council([crit("reject"), crit("accept")]).grade(iteration=1, actor_output="", diff="").rejected   # tie accepts


def test_actor_critic_reject_reverts_and_steers(repo: Path):
    actor = FakeHarness([works("good"), works("bad"), works("fixed")])
    critic_h = FakeHarness([critique("accept"), critique("reject", "restore the deleted test"), critique("accept")])
    eng = make_engine(repo, actor, max_iterations=3)
    eng.config.mode = "actor-critic"
    eng.goal_text = GOAL
    eng.critic = Critic(critic_h, goal_text=GOAL, cwd=repo)
    eng.run()
    assert len(critic_h.prompts) == 3 and "+bad" in critic_h.prompts[1]          # the critic saw the diff
    steps = eng.recorder.read_jsonl(eng.recorder.iterations)
    assert [s["verdict"] for s in steps if s.get("step") == "critique"] == ["accept", "reject", "accept"]
    assert any(s.get("step") == "revert" for s in steps)
    assert "restore the deleted test" in actor.prompts[2]                          # must_fix reached the next actor
    assert "bad" not in (repo / "work.txt").read_text() and "fixed" in (repo / "work.txt").read_text()
    assert eng.outcomes[1].verdict.verdict == "revert" and "critic rejected" in eng.outcomes[1].verdict.reason
    assert eng.budget.cost_usd > 1.5   # actor 3 x 0.5 + critic 3 x 0.02


def test_single_mode_never_calls_critic(repo: Path):
    critic_h = FakeHarness([critique("reject")])
    eng = make_engine(repo, FakeHarness([works()]), max_iterations=1)
    eng.critic = Critic(critic_h, goal_text=GOAL, cwd=repo)
    eng.run()
    assert critic_h.prompts == []
