import re
import shutil
import subprocess
from pathlib import Path

import pytest

from ouroboros.config import StopConfig
from ouroboros.harness.base import Result
from ouroboros.memory import HypergraphMemory, make_memory
from ouroboros.memory.handoff import HandoffMemory

from conftest import git
from fake_harness import FakeHarness, verdict
from test_engine import make_engine

pytestmark = pytest.mark.skipif(shutil.which("hypergraph") is None, reason="hypergraph CLI not installed")

GOAL = "# Goal: t\n\n## Mission\n\nkeep the graph honest.\n\n## Exhaustion policy\n\nmaintain\n"


def hg(repo: Path, *args: str, stdin: str | None = None) -> str:
    proc = subprocess.run(["hypergraph", *args], cwd=repo, input=stdin, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return proc.stdout.strip()


@pytest.fixture
def hg_repo(tmp_path: Path) -> Path:
    r = tmp_path / "hgproj"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    git(r, "config", "user.email", "t@example.com")
    git(r, "config", "user.name", "t")
    (r / ".gitignore").write_text(".ouroboros/runs/\n.hypergraph/cache/\n")
    (r / ".hypergraph").mkdir()
    (r / ".hypergraph" / "config.yml").write_text(
        "project: t\ngraph_dir: .hypergraph/graph\ncache_dir: .hypergraph/cache\nstate_md: STATE.md\n"
    )
    cfg = ["--config", ".hypergraph/config.yml"]
    hg(r, "new", "record", *cfg, "--root", "--title", "t — record", "--body", "-", stdin="A fresh project.\n")
    hg(r, "new", "state", *cfg, "--root", "--reconcile", "--title", "t — state", "--body", "-", stdin="A fresh project.\n")
    hg(r, "sync", *cfg)
    HandoffMemory(r / ".ouroboros").ensure()
    (r / ".ouroboros" / "goal.md").write_text(GOAL)
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "init")
    return r


def mint(repo: Path, title: str, parent: str) -> str:
    body = "## What\n\nx\n\n## Why\n\ny\n\n## Method\n\nm\n\n## Result\n\nDispatch closed: 1 unit — x\n"
    out = hg(repo, "new", "record", "--config", ".hypergraph/config.yml", "--title", title, "--body", "-",
             "--parent", parent, "--none", "test", "--repo-auto", stdin=body)
    return out.split()[0]


def test_auto_detects_hypergraph(hg_repo):
    assert isinstance(make_memory("auto", hg_repo), HypergraphMemory)
    assert isinstance(make_memory("handoff", hg_repo), HandoffMemory)


def test_start_records_directive_once(hg_repo):
    mem = HypergraphMemory(hg_repo)
    mem.start(run="t", goal_text=GOAL, run_dir=hg_repo / ".ouroboros/runs/t", branch="ouroboros/t")
    slug = mem.directive_slug
    assert slug and (hg_repo / ".hypergraph/graph/record" / f"{slug}.md").exists()
    node = (hg_repo / ".hypergraph/graph/record" / f"{slug}.md").read_text()
    assert "keep the graph honest" in node and "Ouroboros run: t" in node
    n_before = len(mem.record_files())
    mem2 = HypergraphMemory(hg_repo)
    mem2.start(run="t", goal_text=GOAL, run_dir=hg_repo / ".ouroboros/runs/t", branch="ouroboros/t")
    assert mem2.directive_slug == slug and len(mem2.record_files()) == n_before


def test_edited_goal_gets_a_new_directive_with_the_old_as_parent(hg_repo):
    mem = HypergraphMemory(hg_repo)
    mem.start(run="t", goal_text=GOAL, run_dir=hg_repo / ".ouroboros/runs/t", branch="ouroboros/t")
    old = mem.directive_slug
    mem2 = HypergraphMemory(hg_repo)
    mem2.start(run="t", goal_text=GOAL + "\nmore\n", run_dir=hg_repo / ".ouroboros/runs/t", branch="ouroboros/t")
    new = mem2.directive_slug
    assert new != old
    node = (hg_repo / ".hypergraph/graph/record" / f"{new}.md").read_text()
    assert f"- {old}" in node.partition("\n---\n")[0] and f"supersedes `{old}`" in node
    assert mem2.check_report() is None


def test_prompts_and_verify(hg_repo):
    mem = HypergraphMemory(hg_repo)
    mem.start(run="t", goal_text=GOAL, run_dir=hg_repo / ".ouroboros/runs/t", branch="ouroboros/t")
    orient = mem.orient_prompt()
    assert "## STATE.md" in orient and "Unreconciled tail" in orient and "one dispatch" in orient
    rec = mem.record_prompt()
    assert f"--parent {mem.directive_slug}" in rec and "hypergraph new record" in rec
    before = mem.snapshot()
    assert not mem.verify_recorded(before)
    slug = mint(hg_repo, "Did a unit", mem.directive_slug)
    assert mem.verify_recorded(before)
    assert mem.last_record_slug == slug
    assert mem.last_summary() == "Did a unit"
    assert f"--parent {slug}" in mem.record_prompt()
    assert mem.check_report() is None


def test_needs_reconcile_pressure_and_cadence(hg_repo):
    mem = HypergraphMemory(hg_repo, reconcile_every=0, pressure=3)
    mem.start(run="t", goal_text=GOAL, run_dir=hg_repo / ".ouroboros/runs/t", branch="ouroboros/t")
    assert mem.unreconciled()[0] == 1 and not mem.needs_reconcile()
    a = mint(hg_repo, "one", mem.directive_slug)
    mint(hg_repo, "two", a)
    assert mem.unreconciled()[0] == 3 and mem.needs_reconcile()
    cad = HypergraphMemory(hg_repo, reconcile_every=2, pressure=99)
    assert not cad.needs_reconcile()
    cad.mark_iteration(); cad.mark_iteration()
    assert cad.needs_reconcile()
    cad.mark_reconciled()
    assert not cad.needs_reconcile()
    prompt = cad.reconcile_prompt()
    assert "one reconcile pass" in prompt and "hypergraph sync" in prompt and "two" in prompt
    assert "{procedure}" not in prompt and "{plan_rule}" not in prompt


def test_engine_runs_the_reconcile_pass_on_cadence(hg_repo):
    mem = HypergraphMemory(hg_repo, reconcile_every=2, pressure=99)

    def actor(cwd: Path, prompt: str) -> Result:
        if "You run one reconcile pass" in prompt[:200]:
            return Result(text="reconciled", session_id="s", cost_usd=0.2)
        parent = re.search(r"--parent (\S+)", prompt).group(1)
        (cwd / "work.txt").open("a").write("x\n")
        mint(cwd, "unit", parent)
        return Result(text="did a unit", session_id="s", cost_usd=0.1)

    harness = FakeHarness([], default=actor)
    eng = make_engine(hg_repo, harness, critic=FakeCritic(), max_iterations=3)
    eng.memory = mem
    eng.goal_text = GOAL
    eng.run()
    assert eng.iteration == 3
    # cadence 2: #1 and #2 work, #3 is the actor's reconcile pass
    assert ["You run one reconcile pass" in p[:200] for p in harness.prompts] == [False, False, True]
    assert [o.housekeeping for o in eng.outcomes] == [False, False, True]
    assert not any(s.get("step") == "reconcile" for s in eng.recorder.read_jsonl(eng.recorder.iterations))
    log = git(hg_repo, "log", "--oneline")
    assert "ouroboros #1: unit" in log
    assert git(hg_repo, "status", "--porcelain") == ""
    assert eng.outcomes[0].recorded and eng.outcomes[1].recorded


class FakeCritic:
    name = "fake"

    def judge(self, s):
        from ouroboros.roles.critic import Verdict
        return Verdict("continue", "", "ok", source="fake")


def test_the_actor_runs_the_reconcile_pass_as_housekeeping(hg_repo):
    """Two roles: a due reconcile is the actor's next iteration, judged like any other."""
    mem = HypergraphMemory(hg_repo, reconcile_every=99, pressure=99)
    prompts = []

    def actor(cwd: Path, prompt: str) -> Result:
        prompts.append(prompt)
        if "You run one reconcile pass" in prompt[:200]:
            (cwd / "STATE.md").write_text("# State\n\n## Frontier\n\n- [open] **gap** (`gap-1`)\n")
            return Result(text="reconciled", session_id="s", cost_usd=0.1)
        parent = re.search(r"--parent (\S+)", prompt).group(1)
        (cwd / "work.txt").open("a").write("x\n")
        mint(cwd, "unit", parent)
        return Result(text="did a unit", session_id="s", cost_usd=0.1)

    eng = make_engine(hg_repo, FakeHarness([], default=actor), critic=FakeCritic(), max_iterations=3)
    eng.memory = mem
    eng.goal_text = GOAL_WITH_GAPS
    eng.run()
    # the directive's gap impacts make a reconcile due at once, so #1 is housekeeping; #2 and #3 work
    assert ["You run one reconcile pass" in p[:200] for p in prompts] == [True, False, False]
    assert not mem.needs_reconcile() and mem.since_reconcile == 2
    steps = eng.recorder.read_jsonl(eng.recorder.iterations)
    assert [s.get("housekeeping") for s in steps if s.get("step") == "commit"] == [True, None, None]
    assert not any(s.get("step") == "reconcile" for s in steps)
    assert git(hg_repo, "log", "-1", "--format=%s", "ouroboros/t~2") == "ouroboros #1: housekeeping"


def test_failed_iterations_do_not_trigger_reconcile(hg_repo):
    from fake_harness import crashes
    mem = HypergraphMemory(hg_repo, reconcile_every=1, pressure=99)
    harness = FakeHarness([], default=crashes("boom"))
    eng = make_engine(hg_repo, harness, critic=FakeCritic(), max_iterations=2)
    eng.memory = mem
    eng.goal_text = GOAL
    eng.run()
    assert not any("You run one reconcile pass" in p[:200] for p in harness.prompts) and mem.since_reconcile == 0


GOAL_WITH_GAPS = GOAL + "\n## Done criteria\n\n- [ ] The build passes on a clean clone.\n- [ ] The README says how to run it.\n"


def test_directive_declares_gap_impacts(hg_repo):
    mem = HypergraphMemory(hg_repo)
    mem.start(run="t", goal_text=GOAL_WITH_GAPS, run_dir=hg_repo / ".ouroboros" / "runs" / "t", branch="ouroboros/t")
    body = mem.read_record(mem.directive_slug)
    assert "NEW gap-build-passes-clean-clone" in body and "NEW gap-readme-says-how-run" in body
    assert "- [gap] gap-build-passes-clean-clone: The build passes on a clean clone." in body
    assert mem.needs_reconcile()   # the gaps must reach the frontier at the first chance
    mem.mark_reconciled()
    assert not mem.needs_reconcile()


def test_second_charter_version_declares_only_new_gaps(hg_repo):
    mem = HypergraphMemory(hg_repo)
    mem.start(run="t", goal_text=GOAL_WITH_GAPS, run_dir=hg_repo / ".ouroboros" / "runs" / "t", branch="ouroboros/t")
    v2 = GOAL + "\n## Done criteria\n\n- [ ] The build passes on a clean clone.\n- [ ] CI runs the tests.\n"
    mem2 = HypergraphMemory(hg_repo)
    mem2.start(run="t", goal_text=v2, run_dir=hg_repo / ".ouroboros" / "runs" / "t", branch="ouroboros/t")
    body = mem2.read_record(mem2.directive_slug)
    assert "NEW gap-ci-runs-tests" in body
    assert "NEW gap-build-passes-clean-clone" not in body           # already declared by v1
    assert "drops" in body and "gap-readme-says-how-run" in body   # named for the reconcile pass


def test_directive_without_criteria_declares_none(hg_repo):
    mem = HypergraphMemory(hg_repo)
    mem.start(run="t", goal_text=GOAL, run_dir=hg_repo / ".ouroboros" / "runs" / "t", branch="ouroboros/t")
    assert "none:" in mem.read_record(mem.directive_slug)
    assert not mem.needs_reconcile()


def test_critic_context_has_frontier_and_tail(hg_repo):
    from ouroboros.memory.hypergraph import frontier_of
    mem = HypergraphMemory(hg_repo)
    (hg_repo / "STATE.md").write_text("# t\n\n## Frontier\n\n- [open] **Gap A** (`a-1`) — todo\n\n## Architecture\n\n- root\n")
    ctx = mem.critic_context()
    assert "Gap A" in ctx and "Unreconciled record nodes" in ctx and "Architecture" not in ctx
    assert frontier_of("# x\n\n## Frontier\n\n(empty)\n") == "(empty)"
    (hg_repo / "PLAN.md").write_text("# plan\n\n## short\n\n- do A\n")
    assert "Plan" not in mem.critic_context()   # no plan layer: a stale PLAN.md is not read


def test_live_charter_revision_reuses_memory_and_records_once(hg_repo):
    mem = HypergraphMemory(hg_repo)
    kw = dict(run="live", run_dir=hg_repo / ".ouroboros/runs/live", branch="ouroboros/live")
    mem.start(goal_text=GOAL_WITH_GAPS, **kw)
    old = mem.directive_slug
    revised = GOAL_WITH_GAPS + "\nUse the dashboard while experiments run.\n"
    mem.start(goal_text=revised, **kw)
    assert mem.goal_text == revised
    assert mem.directive_slug != old
    body = mem.read_record(mem.directive_slug)
    assert f"supersedes `{old}`" in body
    assert "NEW gap-" not in body
    files = mem.record_files()
    mem.start(goal_text=revised, **kw)
    assert mem.record_files() == files


def test_a_store_backed_graph_is_read_from_the_main_checkout_and_never_git_added(tmp_path: Path) -> None:
    """hypergraph's Flywheel store: node files are a gitignored working copy at the main checkout."""
    main = tmp_path / "main"
    (main / ".hypergraph" / "graph" / "record").mkdir(parents=True)
    git(main, "init", "-q", "-b", "main")
    git(main, "config", "user.email", "t@example.com")
    git(main, "config", "user.name", "t")
    (main / ".hypergraph" / "config.yml").write_text(
        "project: t\ngraph_dir: .hypergraph/graph\nstore:\n  kind: flywheel-repo\n  flywheel_cli: 0.2.78\n")
    (main / ".gitignore").write_text(".hypergraph/graph/\n")
    git(main, "add", "-A")
    git(main, "commit", "-qm", "init")
    wt = tmp_path / "wt"
    git(main, "worktree", "add", "-q", "-b", "side", str(wt))
    mem = HypergraphMemory(wt)
    assert mem.store_backed
    assert mem.record_dir() == main / ".hypergraph" / "graph" / "record"
    prompt = mem.record_prompt()
    assert "git add .hypergraph/graph" not in prompt and "nothing to `git add`" in prompt


def test_a_plain_graph_still_tells_the_actor_to_git_add_its_node_files(hg_repo: Path) -> None:
    mem = HypergraphMemory(hg_repo)
    assert not mem.store_backed
    assert "git add .hypergraph/graph" in mem.record_prompt()
