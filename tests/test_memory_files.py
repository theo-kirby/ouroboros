"""The files memory: governor's record/state model as markdown in git, behind `ouroboros mem`."""

import json
from pathlib import Path

import pytest
import yaml

from ouroboros import cli
from ouroboros.config import Config
from ouroboros.memory import ENV, FilesMemory, FilesStore, make_memory
from ouroboros.memory.files import _parse

from conftest import git
from fake_harness import FakeHarness
from ouroboros.harness.base import Result

GOAL = "# Goal: t\n\n## Mission\n\nship it.\n\n## Done criteria\n\n- [ ] The build passes on a clean clone.\n- [ ] A README exists.\n"
BODY = "## What\n\nx\n\n## Why\n\ny\n\n## Method\n\nz\n\n## Result\n\nok\n"


@pytest.fixture
def store(repo):
    s = FilesStore(repo)
    s.prepare()
    return s


def mem_cli(repo, monkeypatch, *args, stdin=None, capsys=None):
    monkeypatch.chdir(repo)
    if stdin is not None:
        import io
        monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    return cli.main(["mem", *args])


# -- the store -----------------------------------------------------------------

def test_a_record_is_a_governor_shaped_file(store, repo):
    slug, msg = store.record(title="First", body=BODY, parent="root", impacts=[], news=[("build-clean", "fails today")], none=None)
    assert slug == "r-0001" and "r-0001" in msg
    meta, body = _parse((repo / ".ouroboros/graph/records/r-0001.md").read_text())
    assert list(meta) == ["id", "kind", "title", "parents", "at", "node_id", "digest"]   # the fixed key order
    assert meta["kind"] == "record" and meta["parents"] == ["root"] and len(meta["node_id"]) == 36
    assert "## Recorded" in body and "- branch: main" in body
    assert body.rstrip().endswith("- NEW build-clean — fails today")


def test_record_refuses_what_governor_refuses(store):
    def problems(**kw):
        args = dict(title="t", body=BODY, parent="root", impacts=[], news=[], none="n")
        args.update(kw)
        slug, msg = store.record(**args)
        assert slug is None
        return msg
    assert "declare the impact" in problems(none=None)
    assert "not a claim" in problems(none=None, impacts=[("nope", "x")])
    assert "generated" in problems(body=BODY + "\n## State Impact\n\n- x — y\n")
    assert "not a record" in problems(parent="r-0099")
    assert "kebab" in problems(none=None, news=[("Not Kebab", "x")])
    assert not list((store.records_dir).glob("*.md")), "a refused record writes nothing"


def test_a_fold_writes_claims_logs_the_fold_and_renders_state(store, repo):
    store.record(title="First", body=BODY, parent="root", impacts=[], news=[("build-clean", "fails")], none=None)
    ok, msg = store.fold({"claims": [{"new": "build-clean", "parent": "root", "title": "Build passes clean",
                                      "status": "open", "current": "- fails today [rec: r-0001]", "cites": ["r-0001"]}],
                          "folded": ["r-0001"]})
    assert ok, msg
    g = store.snapshot()
    claim = g.nodes["build-clean"]
    assert claim["status"] == "open" and [r["id"] for r in g.cites(claim)] == ["r-0001"]
    assert g.tail() == []
    assert json.loads((repo / ".ouroboros/graph/folds/f-0001.json").read_text())["folded"] == ["r-0001"]
    state = (repo / ".ouroboros/graph/STATE.md").read_text()
    assert "- [open] **Build passes clean** (`build-clean`) — fails today" in state

    store.record(title="Fix", body=BODY, parent="r-0001", impacts=[("build-clean", "passes now")], news=[], none=None)
    g = store.snapshot()
    assert [(lbl, d) for _, lbl, d in g.impacts_of(g.nodes["r-0002"])] == [("build-clean", "passes now")]
    ok, msg = store.fold({"claims": [{"slug": "build-clean", "status": "working", "current": "- passes [rec: r-0002]",
                                      "cites": ["r-0002"]}], "folded": ["r-0002"]})
    assert ok, msg
    claim = store.snapshot().nodes["build-clean"]
    assert claim["status"] == "working"
    assert "- r-0001" in claim["content"] and "- r-0002" in claim["content"], "citations accumulate"


def test_a_bad_fold_writes_nothing(store):
    store.record(title="First", body=BODY, parent="root", impacts=[], news=[], none="n")
    ok, msg = store.fold({"claims": [{"slug": "ghost", "status": "working", "current": "x"}], "folded": ["r-0001", "r-0009"]})
    assert not ok and "ghost" in msg and "r-0009" in msg
    ok, msg = store.fold({"claims": [{"new": "w", "title": "W", "status": "working", "current": "x"}], "folded": []})
    assert not ok and "cites at least one record" in msg
    assert not list(store.claims_dir.glob("*.md")) and not list(store.folds_dir.glob("*.json"))


def test_check_catches_hand_edits_and_dangling_references(store):
    store.record(title="First", body=BODY, parent="root", impacts=[], news=[("c", "d")], none=None)
    store.fold({"claims": [{"new": "c", "title": "C", "status": "open", "current": "- c [rec: r-0001]", "cites": ["r-0001"]}],
                "folded": ["r-0001"]})
    ok, text = store.check()
    assert ok, text
    rec = store.records_dir / "r-0001.md"
    rec.write_text(rec.read_text().replace("## Result\n\nok", "## Result\n\nedited"))
    claim = store.claims_dir / "c.md"
    claim.write_text(claim.read_text().replace("- c [rec: r-0001]", "- c [rec: r-0001] [rec: r-0042]"))
    ok, text = store.check()
    assert not ok
    assert "VIOLATION G2 r-0001" in text and "VIOLATION G7 c" in text and "r-0042" in text


def test_a_broken_file_is_reported_not_fatal(store):
    (store.records_dir / "r-0001.md").write_text("no frontmatter\n")
    g = store.snapshot()
    assert g.ok and g.records() == []
    ok, text = store.check()
    assert not ok and "G1" in text


# -- the memory in the loop -------------------------------------------------------

def test_start_records_the_directive_and_gaps_reach_the_frontier_through_a_fold(repo):
    mem = FilesMemory(repo)
    mem.start(run="nt1", goal_text=GOAL, run_dir=repo / ".ouroboros/runs/nt1", branch="ouroboros/nt1")
    assert mem.directive_slug == "r-0001" and mem.needs_reconcile()
    tail = mem.unreconciled()[1]
    assert tail.count("→ `NEW ") == 2
    assert mem.frontier_fingerprint() is None, "no claims yet: unknown, not unmoved"
    g = mem.graph()
    news = [lbl[4:] for _, lbl, _ in g.impacts_of(g.nodes["r-0001"])]
    ok, msg = mem.store.fold({"claims": [{"new": n, "parent": "root", "title": n, "status": "open",
                                          "current": f"- open [rec: r-0001]", "cites": ["r-0001"]} for n in news],
                              "folded": ["r-0001"]})
    assert ok, msg
    mem.mark_reconciled()
    assert not mem.needs_reconcile()
    assert mem.frontier_fingerprint() is not None
    assert "[open]" in mem.critic_context()
    # a second start with the same charter records nothing new
    again = FilesMemory(repo)
    again.start(run="nt1", goal_text=GOAL, run_dir=repo / ".ouroboros/runs/nt1", branch="ouroboros/nt1")
    assert again.directive_slug == "r-0001" and len(again.graph().records()) == 1


def test_the_prompts_name_only_ouroboros_mem(repo):
    from ouroboros.roles.scribe import build_scribe_prompt
    mem = make_memory("files", repo)
    mem.start(run="x", goal_text=GOAL, run_dir=repo / "r", branch="b")
    actor = mem.orient_prompt()
    assert "ouroboros mem orient" in actor and "ouroboros mem show" in actor
    assert "ouroboros mem record" not in actor and "ouroboros mem fold" not in actor
    scribe = build_scribe_prompt(iteration=2, goal_text=GOAL, where=mem.store.where, report="## What\n\nx", verdict="continue — ok",
                                 fix_first="", diff_stat="", commit="abc", fold=mem.fold_prompt(), view_dir="v",
                                 story_path="v/story.html", assets_dir="v/assets")
    assert "ouroboros mem record" in scribe and "ouroboros mem fold --apply" in scribe and "v/story.html" in scribe
    assert "v/summary.txt" in scribe and "{summary_path}" not in scribe
    for text in (actor, scribe):
        assert "governor record" not in text and "governor fold" not in text
        assert "{where}" not in text and "{" + "orient}" not in text and "{fold}" not in text


def test_each_role_gets_only_its_verbs(repo, monkeypatch, capsys):
    monkeypatch.setenv(ENV, "files")
    monkeypatch.setenv("OUROBOROS_ROLE", "actor")
    assert mem_cli(repo, monkeypatch, "orient") == 0
    assert mem_cli(repo, monkeypatch, "record", "--title", "t", "--none", "x", stdin=BODY) == 2
    assert "the actor does not record" in capsys.readouterr().err
    monkeypatch.setenv("OUROBOROS_ROLE", "critic")
    assert mem_cli(repo, monkeypatch, "check") == 0 and mem_cli(repo, monkeypatch, "fold") == 2
    monkeypatch.setenv("OUROBOROS_ROLE", "scribe")
    assert mem_cli(repo, monkeypatch, "record", "--title", "t", "--none", "x", stdin=BODY) == 0


def test_handoff_is_the_old_name_of_files(repo):
    assert isinstance(make_memory("handoff", repo), FilesMemory)
    assert isinstance(make_memory("auto", repo), FilesMemory)   # no governor graph here
    assert Config.model_validate({"memory": "handoff"}).memory == "files"


def test_the_last_handoff_carries_over_while_the_memory_is_young(repo):
    old = repo / ".ouroboros" / "handoff"
    old.mkdir(parents=True)
    (old / "0007.md").write_text("## Did\n\nthe old way\n")
    mem = FilesMemory(repo)
    mem.start(run="x", goal_text="# Goal: t\n", run_dir=repo / "r", branch="b")
    assert "the old way" in mem.orient_prompt()


# -- the command -------------------------------------------------------------

def test_the_cli_records_folds_shows_and_checks(repo, monkeypatch, capsys):
    monkeypatch.setenv(ENV, "files")
    assert mem_cli(repo, monkeypatch, "record", "--title", "Did it", "--new", "readme: missing", stdin=BODY) == 0
    assert "r-0001" in capsys.readouterr().out
    # the parent defaults to the newest record
    assert mem_cli(repo, monkeypatch, "record", "--title", "Then", "--none", "nothing changed", stdin=BODY) == 0
    assert yaml.safe_load((repo / ".ouroboros/graph/records/r-0002.md").read_text().split("---")[1])["parents"] == ["r-0001"]
    assert mem_cli(repo, monkeypatch, "record", "--title", "Bad", stdin=BODY) == 2
    assert "nothing written" in capsys.readouterr().err

    assert mem_cli(repo, monkeypatch, "fold") == 0
    out = capsys.readouterr().out
    assert "2 unfolded record(s)" in out and "NEW readme`: missing" in out and "--apply" in out
    fold = repo / "fold.json"
    fold.write_text(json.dumps({"claims": [{"new": "readme", "parent": "root", "title": "README", "status": "open",
                                            "current": "- missing [rec: r-0001]", "cites": ["r-0001"]}],
                                "folded": ["r-0001", "r-0002"]}))
    assert mem_cli(repo, monkeypatch, "fold", "--apply", str(fold)) == 0
    assert "new: readme" in capsys.readouterr().out
    assert mem_cli(repo, monkeypatch, "show", "readme") == 0
    assert "## Current" in capsys.readouterr().out
    assert mem_cli(repo, monkeypatch, "check") == 0
    assert mem_cli(repo, monkeypatch, "orient") == 0
    out = capsys.readouterr().out
    assert "## Frontier" in out and "## Most recent records" in out


def test_the_cli_follows_the_config_when_no_run_set_it(repo, monkeypatch, capsys):
    monkeypatch.delenv(ENV, raising=False)
    (repo / ".ouroboros" / "config.yml").write_text("memory: handoff\n")
    assert mem_cli(repo, monkeypatch, "orient") == 0
    assert "Ouroboros memory" in capsys.readouterr().out


def test_an_engine_run_records_and_folds_through_the_cli(repo, monkeypatch):
    """The whole loop on files memory: the actor works and reports, the scribe records and folds."""
    import io
    import re
    from test_engine import make_engine
    monkeypatch.setenv(ENV, "files")
    monkeypatch.chdir(repo)

    def actor(cwd: Path, prompt: str) -> Result:
        assert "ouroboros mem orient" in prompt
        (cwd / "README.md").write_text("now real\n")
        return Result(text="Wrote it.\n\n" + BODY + "\n## Claims\n\n- gap-readme-exists: written", session_id="s")

    def scribe(cwd: Path, prompt: str) -> Result:
        report = prompt.split("## The actor's report", 1)[1]
        claims = re.findall(r"^- (gap-[a-z-]+): (.*)$", report, re.MULTILINE)
        monkeypatch.setattr("sys.stdin", io.StringIO(BODY))
        known = FilesStore(cwd).snapshot().nodes
        flags = [x for slug, delta in claims
                 for x in (("--impact" if slug in known else "--new"), f"{slug}: {delta}")] or ["--none", "nothing"]
        assert cli.main(["mem", "record", "--title", "A unit", *flags]) == 0
        if "A fold is due" in prompt:
            g = FilesStore(cwd).snapshot()
            claims_out = {}
            for r in g.tail():
                for target, label, delta in g.impacts_of(r):
                    name = label[4:] if target is None else label
                    c = claims_out.setdefault(name, {("new" if target is None else "slug"): name, "title": name,
                                                     "parent": "root", "status": "open", "cites": []})
                    c.update(current=f"- {delta} [rec: {r['id']}]", cites=c["cites"] + [r["id"]])
                    if "written" in delta:
                        c["status"] = "working"
            for c in claims_out.values():
                if "slug" in c:
                    c.pop("parent"), c.pop("title")
            (cwd / "fold.json").write_text(json.dumps({"claims": list(claims_out.values()),
                                                       "folded": [r["id"] for r in g.tail()]}))
            assert cli.main(["mem", "fold", "--apply", "fold.json"]) == 0
            (cwd / "fold.json").unlink()
        return Result(text="recorded", session_id="s")

    (repo / ".ouroboros" / "goal.md").write_text(GOAL)
    git(repo, "commit", "-qam", "goal")
    eng = make_engine(repo, FakeHarness([], default=actor), max_iterations=2, scribe=FakeHarness([], default=scribe))
    eng.memory = FilesMemory(repo, reconcile_every=0, pressure=10**6)
    eng.run()
    # 1: the directive's gaps make a fold due, so the scribe records #1 and folds (claims appear,
    #    the impact on a claim that did not exist yet stays in the tail); 2: records again
    state = (repo / ".ouroboros/graph/STATE.md").read_text()
    assert "gap-readme-exists" in state
    assert eng.memory.store.check()[0], eng.memory.store.check()[1]
    tracked = git(repo, "ls-files", ".ouroboros/graph")
    assert "records/r-0003.md" in tracked and "claims/gap-readme-exists.md" in tracked, "the memory is committed with the code"
    assert (repo / ".ouroboros/runs/t/view/index.html").exists(), "the run keeps its page"


def test_criterion_identity_survives_evidenced_fold_and_renaming(store):
    from ouroboros.criteria import rows
    key="gap-build-passes-clean-clone"
    rid,_=store.record(title="Directive",body=BODY,parent="root",impacts=[],news=[(key,"open")],none=None)
    ok,msg=store.fold({"claims":[{"new":key,"title":"Renamed", "status":"working",
        "current":"passes", "cites":[rid]}],"folded":[rid]})
    assert ok,msg
    assert rows(store.snapshot(),GOAL)[0]['met']
    rid2,_=store.record(title="Review",body=BODY,parent=rid,impacts=[(key,"still passes")],news=[],none=None)
    ok,msg=store.fold({"claims":[{"slug":key,"title":"Different title", "status":"working",
        "current":"new prose", "cites":[rid2]}],"folded":[rid2]})
    assert ok,msg
    assert rows(store.snapshot(),GOAL)[0]['met']
