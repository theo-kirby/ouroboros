import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from ouroboros.harness.base import Result
from ouroboros.memory import GovernorMemory, make_memory
from ouroboros.memory.handoff import HandoffMemory

from conftest import git
from fake_harness import FakeHarness
from test_engine import make_engine

pytestmark = pytest.mark.skipif(
    shutil.which("governor") is None or shutil.which("flywheel") is None,
    reason="governor / flywheel CLI not installed",
)

GOAL = "# Goal: t\n\n## Mission\n\nkeep the graph honest.\n\n## Exhaustion policy\n\nmaintain\n"
GOAL_WITH_GAPS = GOAL + "\n## Done criteria\n\n- [ ] The build passes on a clean clone.\n- [ ] The README says how to run it.\n"
GOAL_WITH_LADDER = GOAL_WITH_GAPS + (
    "\n## Horizon ladder\n\n- **short-term:** fix the build. write the README.\n"
    "- **medium-term:** add CI.\n- **long-term:** a second platform. keep it green.\n"
)
BODY = "## What\n\nx\n\n## Why\n\ny\n\n## Method\n\nm\n\n## Result\n\nDispatch closed: 1 unit — x\n"


def gv(repo: Path, *args: str, stdin: str | None = None, ok: bool = True) -> str:
    proc = subprocess.run(["governor", *args], cwd=repo, input=stdin, capture_output=True, text=True)
    if ok:
        assert proc.returncode == 0, proc.stdout + proc.stderr
    return proc.stdout.strip()


def snap(repo: Path) -> dict:
    return json.loads(gv(repo, "snapshot"))


def node(repo: Path, slug: str) -> dict:
    return next(n for n in snap(repo)["nodes"].values() if n["slug"] == slug)


@pytest.fixture(scope="session")
def gov_template(tmp_path_factory) -> Path:
    """One initialised governor repo per session; each test gets a copy (init costs seconds)."""
    r = tmp_path_factory.mktemp("govtemplate") / "govproj"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    git(r, "config", "user.email", "t@example.com")
    git(r, "config", "user.name", "t")
    (r / ".gitignore").write_text(".ouroboros/runs/\n.flywheel/\n.governor/\n")
    subprocess.run(["flywheel", "repo:init"], cwd=r, capture_output=True, text=True, check=True)
    gv(r, "init", "--title", "t")
    HandoffMemory(r / ".ouroboros").ensure()
    (r / ".ouroboros" / "goal.md").write_text(GOAL)
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "init")
    shutil.rmtree(r / ".governor", ignore_errors=True)   # locks and the snapshot cache stay per copy
    return r


@pytest.fixture
def gov_repo(gov_template: Path, tmp_path: Path) -> Path:
    r = tmp_path / "govproj"
    shutil.copytree(gov_template, r, symlinks=True)
    return r


def mint(repo: Path, title: str, parent: str, *flags: str) -> str:
    out = gv(repo, "record", "--title", title, "--body", "-", "--parent", parent, *(flags or ("--none", "test")), stdin=BODY)
    return out.split()[0]


def start(mem: GovernorMemory, repo: Path, goal: str = GOAL, run: str = "t") -> None:
    mem.start(run=run, goal_text=goal, run_dir=repo / ".ouroboros/runs" / run, branch=f"ouroboros/{run}")


def fold_all(repo: Path, claims: list[dict] | None = None, view: str | None = None, folded: list[str] | None = None) -> None:
    tail = [n["slug"] for n in snap(repo)["nodes"].values() if n["kind"] == "record" and not n["folded"]]
    plan = {"claims": claims or [], "folded": tail if folded is None else folded}
    if view:
        plan["view"] = view
    (repo.parent / "fold.json").write_text(json.dumps(plan))
    gv(repo, "fold", "--apply", str(repo.parent / "fold.json"))


# -- detection -----------------------------------------------------------

def test_auto_detects_governor(gov_repo):
    assert isinstance(make_memory("auto", gov_repo), GovernorMemory)
    assert isinstance(make_memory("governor", gov_repo), GovernorMemory)
    assert isinstance(make_memory("handoff", gov_repo), HandoffMemory)


def test_auto_prefers_hypergraph_config_and_falls_back_without_a_repository(gov_repo, tmp_path):
    (gov_repo / ".hypergraph").mkdir()
    (gov_repo / ".hypergraph" / "config.yml").write_text("project: t\n")
    assert not isinstance(make_memory("auto", gov_repo), GovernorMemory)
    bare = tmp_path / "bare"
    bare.mkdir()
    git(bare, "init", "-q", "-b", "main")
    assert isinstance(make_memory("auto", bare), HandoffMemory)
    with pytest.raises(ValueError, match="governor requested"):
        make_memory("governor", bare)


def test_auto_finds_the_repository_through_a_worktree(gov_repo, tmp_path):
    wt = tmp_path / "wt"
    git(gov_repo, "worktree", "add", "-q", "-b", "side", str(wt))
    mem = make_memory("auto", wt)
    assert isinstance(mem, GovernorMemory)
    start(mem, wt)
    assert mem.directive_slug and node(gov_repo, mem.directive_slug)["kind"] == "record"


def test_config_and_cli_accept_governor():
    from ouroboros.config import Config
    assert Config(memory="governor").memory == "governor"
    with pytest.raises(Exception):
        Config(memory="nope")


# -- the directive -------------------------------------------------------

def test_start_records_directive_once(gov_repo):
    mem = GovernorMemory(gov_repo)
    start(mem, gov_repo)
    slug = mem.directive_slug
    n = node(gov_repo, slug)
    assert n["kind"] == "record" and "keep the graph honest" in n["content"] and n["title"].startswith("Ouroboros run: t [")
    n_before = len(mem.record_ids())
    mem2 = GovernorMemory(gov_repo)
    start(mem2, gov_repo)
    assert mem2.directive_slug == slug and len(mem2.record_ids()) == n_before
    assert (gov_repo / "STATE.md").exists() and "Generated by `governor render`" in (gov_repo / "STATE.md").read_text()
    assert mem.check_report() is None


def test_edited_goal_gets_a_new_directive_with_the_old_as_parent(gov_repo):
    mem = GovernorMemory(gov_repo)
    start(mem, gov_repo)
    old = mem.directive_slug
    mem2 = GovernorMemory(gov_repo)
    start(mem2, gov_repo, GOAL + "\nmore\n")
    new = mem2.directive_slug
    assert new != old
    s = snap(gov_repo)
    by_id = s["nodes"]
    n = next(x for x in by_id.values() if x["slug"] == new)
    assert [by_id[p]["slug"] for p in n["parents"]] == [old]
    assert f"supersedes `{old}`" in n["content"]
    assert mem2.check_report() is None


def test_a_run_name_prefix_does_not_supersede_another_run(gov_repo):
    a = GovernorMemory(gov_repo)
    start(a, gov_repo, run="t")
    b = GovernorMemory(gov_repo)
    start(b, gov_repo, run="t2")
    assert "supersedes" not in b.read_record(b.directive_slug)


def test_directive_declares_gap_impacts(gov_repo):
    mem = GovernorMemory(gov_repo)
    start(mem, gov_repo, GOAL_WITH_GAPS)
    body = mem.read_record(mem.directive_slug)
    assert "- NEW gap-build-passes-clean-clone — Charter gap" in body and "NEW gap-readme-says-how-run" in body
    assert "- [gap] gap-build-passes-clean-clone: The build passes on a clean clone." in body
    assert mem.needs_reconcile()   # the gaps must reach the frontier at the first chance
    mem.mark_reconciled()
    assert not mem.needs_reconcile()


def test_second_charter_version_declares_only_new_gaps(gov_repo):
    mem = GovernorMemory(gov_repo)
    start(mem, gov_repo, GOAL_WITH_GAPS)
    v2 = GOAL + "\n## Done criteria\n\n- [ ] The build passes on a clean clone.\n- [ ] CI runs the tests.\n"
    mem2 = GovernorMemory(gov_repo)
    start(mem2, gov_repo, v2)
    body = mem2.read_record(mem2.directive_slug)
    assert "NEW gap-ci-runs-tests" in body
    assert "NEW gap-build-passes-clean-clone" not in body           # already declared by v1
    assert "drops" in body and "gap-readme-says-how-run" in body   # named for the reconcile pass


def test_directive_without_criteria_declares_none(gov_repo):
    mem = GovernorMemory(gov_repo)
    start(mem, gov_repo)
    assert "none:" in mem.read_record(mem.directive_slug)
    assert not mem.needs_reconcile()


def test_live_charter_revision_reuses_memory_and_records_once(gov_repo):
    mem = GovernorMemory(gov_repo)
    start(mem, gov_repo, GOAL_WITH_GAPS)
    old = mem.directive_slug
    revised = GOAL_WITH_GAPS + "\nUse the dashboard while experiments run.\n"
    start(mem, gov_repo, revised)
    assert mem.goal_text == revised and mem.directive_slug != old
    body = mem.read_record(mem.directive_slug)
    assert f"supersedes `{old}`" in body and "NEW gap-" not in body
    ids = mem.record_ids()
    start(mem, gov_repo, revised)
    assert mem.record_ids() == ids


def test_start_gitignores_the_repository(gov_repo):
    (gov_repo / ".gitignore").write_text(".ouroboros/runs/\n")
    start(GovernorMemory(gov_repo), gov_repo)
    assert ".flywheel/" in (gov_repo / ".gitignore").read_text()
    assert ".flywheel" not in git(gov_repo, "status", "--porcelain")


# -- one iteration ---------------------------------------------------------

def test_prompts_and_verify(gov_repo):
    mem = GovernorMemory(gov_repo)
    start(mem, gov_repo)
    orient = mem.orient_prompt()
    assert "## governor orient" in orient and "## Frontier" in orient and "one dispatch" in orient
    assert "Forbidden" in orient and "governor fold --apply" in orient
    rec = mem.record_prompt()
    assert f"--parent {mem.directive_slug}" in rec and "governor record" in rec and "nothing to `git add`" in rec
    before = mem.snapshot()
    assert not mem.verify_recorded(before)
    slug = mint(gov_repo, "Did a unit", mem.directive_slug)
    assert mem.verify_recorded(before)
    assert mem.last_record_slug == slug
    assert mem.last_summary() == "Did a unit"
    assert f"--parent {slug}" in mem.record_prompt()
    assert mem.check_report() is None


def test_needs_reconcile_pressure_and_cadence(gov_repo):
    mem = GovernorMemory(gov_repo, reconcile_every=0, pressure=3)
    start(mem, gov_repo)
    assert mem.unreconciled()[0] == 1 and not mem.needs_reconcile()
    a = mint(gov_repo, "one", mem.directive_slug)
    mint(gov_repo, "two", a)
    count, listing = mem.unreconciled()
    assert count == 3 and mem.needs_reconcile() and "none: test" in listing
    cad = GovernorMemory(gov_repo, reconcile_every=2, pressure=99)
    assert not cad.needs_reconcile()
    cad.mark_iteration(); cad.mark_iteration()
    assert cad.needs_reconcile()
    cad.mark_reconciled()
    assert not cad.needs_reconcile()
    prompt = cad.reconcile_prompt()
    assert "one reconcile pass" in prompt and "governor fold --apply" in prompt and "governor render -o STATE.md" in prompt
    assert "two" in prompt and "{procedure}" not in prompt
    fold_all(gov_repo)
    assert mem.unreconciled()[0] == 0 and not mem.needs_reconcile()


def test_the_tail_lists_impacts(gov_repo):
    mem = GovernorMemory(gov_repo)
    start(mem, gov_repo, GOAL_WITH_LADDER)
    _, listing = mem.unreconciled()
    assert "→ `NEW gap-build-passes-clean-clone`:" in listing
    assert "plan-short" not in listing                    # no role seeds a plan now
    fold_all(gov_repo, [{"new": "gap-a", "parent": "root", "title": "Gap A", "status": "open",
                         "current": "- todo", "cites": [mem.directive_slug]}])
    claim = next(n for n in snap(gov_repo)["nodes"].values() if n["title"] == "Gap A")
    mint(gov_repo, "touches A", mem.directive_slug, "--impact", f"{claim['slug']}: now half done")
    _, listing = mem.unreconciled()
    assert f"→ `{claim['slug']}`: now half done" in listing


def test_check_report_names_a_violation(gov_repo, monkeypatch):
    mem = GovernorMemory(gov_repo)
    start(mem, gov_repo)
    assert mem.check_report() is None
    findings = [{"level": "violation", "rule": "G3", "node": "x", "message": "declares no state impact"},
                {"level": "warning", "rule": "tail", "message": "3 unfolded"}]
    out = json.dumps({"ok": False, "violations": 1, "warnings": 1, "findings": findings, "head": "h"})
    monkeypatch.setattr(mem, "check", lambda: (1, out))
    report = mem.check_report()
    assert report.startswith("`governor check` exit 1") and "VIOLATION G3 x: declares no state impact" in report
    assert "unfolded" not in report
    monkeypatch.setattr(mem, "check", lambda: (2, "error: no Flywheel repository here"))
    assert "no Flywheel repository" in mem.check_report()


def test_critic_context_and_fingerprint_follow_claim_statuses(gov_repo):
    mem = GovernorMemory(gov_repo)
    start(mem, gov_repo, GOAL_WITH_GAPS)
    assert mem.frontier_fingerprint() is None          # no claims yet: unknown, not unmoved
    ctx = mem.critic_context()
    assert "(empty: no open gaps)" in ctx and "Unfolded records in the tail: 1" in ctx
    fold_all(gov_repo, [{"new": "gap-a", "parent": "root", "title": "Gap A", "status": "open",
                         "current": "- todo [rec: x]", "cites": [mem.directive_slug]}])
    ctx = mem.critic_context()
    assert "[open] **Gap A**" in ctx and "Architecture" not in ctx and "tail: 0" in ctx
    fp = mem.frontier_fingerprint()
    assert fp and fp == mem.frontier_fingerprint()
    slug = next(n for n in snap(gov_repo)["nodes"].values() if n["title"] == "Gap A")["slug"]
    rec = mint(gov_repo, "made it work", mem.directive_slug, "--impact", f"{slug}: works now")
    assert mem.frontier_fingerprint() == fp             # a record alone moves nothing
    fold_all(gov_repo, [{"slug": slug, "status": "working", "current": "- done", "cites": [rec]}])
    assert mem.frontier_fingerprint() != fp
    assert "Gap A" not in mem.critic_context().split("Unfolded")[0]


# -- an old run's plan -----------------------------------------------------

def test_start_creates_no_plan_component(gov_repo):
    mem = GovernorMemory(gov_repo)
    start(mem, gov_repo, GOAL_WITH_LADDER)
    assert not any(n["kind"] == "plan" for n in snap(gov_repo)["nodes"].values())
    assert "plan-short" not in mem.read_record(mem.directive_slug)
    assert mem.check_report() is None


def test_an_old_plan_claim_is_marked_legacy_in_the_tail_and_left_out_of_the_prompts(gov_repo):
    """A graph from a run with the planner on keeps its plan claims; the fold leaves them alone."""
    mint(gov_repo, "seed", "root")
    fold_all(gov_repo, [{"new": "plan", "parent": "root", "title": "plan", "component": True,
                         "current": "- bets"}], view="plan", folded=[])
    plan = next(n for n in snap(gov_repo)["nodes"].values() if n["kind"] == "plan")
    mem = GovernorMemory(gov_repo)
    start(mem, gov_repo, GOAL)
    mint(gov_repo, "Bet: old", mem.directive_slug, "--impact", f"{plan['slug']}: a horizon moved")
    _, listing = mem.unreconciled()
    assert "(plan: legacy, leave it)" in listing
    assert "leave them out of your fold's `claims`" in mem.reconcile_prompt()
    assert "- bets" not in mem.orient_prompt() and "- bets" not in mem.critic_context()


# -- the engine ------------------------------------------------------------

class FakeCritic:
    name = "fake"

    def judge(self, s):
        from ouroboros.roles.critic import Verdict
        return Verdict("continue", "", "ok", source="fake")


def records(cwd: Path, prompt: str) -> Result:
    parent = re.search(r"--parent (\S+)", prompt).group(1)
    (cwd / "work.txt").open("a").write("x\n")
    mint(cwd, "unit", parent)
    return Result(text="did a unit", session_id="s", cost_usd=0.1)


def folds(cwd: Path, prompt: str) -> Result:
    """A maintainer that really folds: every tail record, no claims."""
    fold_all(cwd)
    gv(cwd, "render", "-o", "STATE.md")
    return Result(text="folded", cost_usd=0.2)


def housekeeps(cwd: Path, prompt: str) -> Result:
    """An actor that folds when the iteration is housekeeping and records otherwise."""
    return folds(cwd, prompt) if "You run one reconcile pass" in prompt[:200] else records(cwd, prompt)


def test_engine_counts_a_record_and_runs_the_fold_on_pressure(gov_repo):
    mem = GovernorMemory(gov_repo, reconcile_every=0, pressure=3)
    actor = FakeHarness([], default=housekeeps)
    eng = make_engine(gov_repo, actor, critic=FakeCritic(), max_iterations=4)
    eng.memory = mem
    eng.goal_text = GOAL
    eng.run()
    assert eng.iteration == 4
    # directive + #1 + #2 = 3 unfolded: pressure makes #3 housekeeping; #4 leaves one in the tail
    assert [o.housekeeping for o in eng.outcomes] == [False, False, True, False]
    assert "governor fold --apply" in actor.prompts[2]
    assert eng.outcomes[0].recorded and eng.outcomes[1].recorded and eng.outcomes[3].recorded
    assert not any(s.get("step") == "reconcile" for s in eng.recorder.read_jsonl(eng.recorder.iterations))
    assert mem.unreconciled()[0] == 1
    log = git(gov_repo, "log", "--oneline")
    assert "ouroboros #1: unit" in log
    assert git(gov_repo, "status", "--porcelain") == ""
    assert mem.check_report() is None


def test_the_actor_runs_the_fold_as_housekeeping(gov_repo):
    mem = GovernorMemory(gov_repo, reconcile_every=99, pressure=99)
    prompts = []

    def actor(cwd: Path, prompt: str) -> Result:
        prompts.append(prompt)
        if "You run one reconcile pass" in prompt[:200]:
            return folds(cwd, prompt)
        return records(cwd, prompt)

    eng = make_engine(gov_repo, FakeHarness([], default=actor), critic=FakeCritic(), max_iterations=3)
    eng.memory = mem
    eng.goal_text = GOAL_WITH_GAPS
    eng.run()
    # the directive's gap impacts make a fold due at once, so #1 is housekeeping; #2 and #3 work
    assert ["You run one reconcile pass" in p[:200] for p in prompts] == [True, False, False]
    assert not mem.needs_reconcile() and mem.since_reconcile == 2
    assert eng.outcomes[1].recorded and eng.outcomes[2].recorded
    steps = eng.recorder.read_jsonl(eng.recorder.iterations)
    assert [s.get("housekeeping") for s in steps if s.get("step") == "commit"] == [True, None, None]
    assert git(gov_repo, "status", "--porcelain") == ""
