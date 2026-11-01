"""The graph as the run's spine: the claim tree, its frontier leaves, the target a next
step names, records resting on several records, and governor as the standard store."""

import json
import subprocess
from pathlib import Path

import ouroboros.memory as memory
from ouroboros import cli
from ouroboros.config import Config
from ouroboros.criteria import name_of, tag_fold
from ouroboros.memory.files import FilesStore
from ouroboros.memory.graph import (Graph, brief_text, claim_path, frontier_leaves, record_parents, target_text,
                                    tree_text)
from ouroboros.roles.worker import build_worker_prompt, claimed_ids
from ouroboros.roles.writer import WRITER_SCHEMA, parse_reply
from ouroboros.worker_writer import NEXT_TARGET

from conftest import git
from fake_harness import FakeHarness, works_json
from test_worker_writer import make_ww

BODY = "## What\n\nx\n\n## Why\n\ny\n\n## Method\n\nz\n\n## Result\n\nok\n"
GOAL = "# Goal: tree\n\n## Mission\n\nGrow a tree.\n\n## Done criteria\n\n- [ ] It parses.\n"


def _rec(store: FilesStore, title: str, *, parent=None, news=(), impacts=(), none=None) -> str:
    slug, msg = store.record(title=title, body=BODY, parent=parent, impacts=list(impacts), news=list(news),
                             none=none if (none or news or impacts) else "nothing")
    assert slug, msg
    return slug


def _fold(store: FilesStore, claims: list[dict], folded: list[str]) -> None:
    ok, msg = store.fold({"claims": claims, "folded": folded})
    assert ok, msg


def tree_store(repo: Path) -> FilesStore:
    """gap-parse (open) with two sub-claims, one open with a Next line, one working;
    an environment claim; a superseded hypothesis."""
    s = FilesStore(repo)
    r1 = _rec(s, "Seed", news=[("gap-parse", "asks"), ("environment", "host")])
    _fold(s, [{"new": "gap-parse", "parent": "root", "title": "It parses", "status": "open",
               "current": f"- Asks: it parses [rec: {r1}]", "cites": [r1]},
              {"new": "environment", "parent": "root", "title": "Host", "status": "working",
               "current": f"- Python 3.12, no network [rec: {r1}]", "cites": [r1]}], [r1])
    r2 = _rec(s, "Split the parser", news=[("lexer", "a"), ("grammar", "b"), ("fast-path-helps", "c")])
    _fold(s, [{"new": "lexer", "parent": "gap-parse", "title": "The lexer tokenizes", "status": "working",
               "current": f"- 40 tokens/ms [rec: {r2}]", "cites": [r2]},
              {"new": "grammar", "parent": "gap-parse", "title": "The grammar covers the spec", "status": "open",
               "current": f"- 3 of 9 rules [rec: {r2}]\n- Next: add rules 4-6 in `src/grammar.py`; stop when "
                          f"`tests/test_grammar.py` passes [rec: {r2}]",
               "negative": f"- ruled out: a PEG generator — no network to install it [rec: {r2}]", "cites": [r2]},
              {"new": "fast-path-helps", "parent": "gap-parse", "title": "A fast path halves the time",
               "status": "superseded", "current": f"- measured 2% [rec: {r2}]", "cites": [r2]}], [r2])
    return s


def test_the_frontier_is_the_trees_open_leaves(repo):
    g = tree_store(repo).snapshot()
    leaves = [c["id"] for c in frontier_leaves(g)]
    assert leaves == ["grammar"], "gap-parse has an open claim under it, so the frontier reaches it through that"
    assert [c["id"] for c in claim_path(g, g.nodes["grammar"])] == ["gap-parse", "grammar"]
    brief = brief_text(g, goal_text=GOAL, goal_path=".ouroboros/goal.md")
    front = brief.split("## Frontier", 1)[1].split("##", 1)[0]
    assert "The grammar covers the spec" in front and "under **It parses**" in front
    assert "It parses**" not in front.split("under", 1)[0], "the parent is not listed as its own item"
    assert "Next: add rules 4-6" in front


def test_the_tree_outline_nests_and_marks_the_target(repo):
    g = tree_store(repo).snapshot()
    t = tree_text(g, mark="grammar")
    lines = t.splitlines()
    parse = next(i for i, x in enumerate(lines) if "It parses" in x)
    assert lines[parse].startswith("- [open]")
    assert any(x.startswith("  - [working] **The lexer tokenizes**") for x in lines)
    assert any(x.startswith("  - [open] **The grammar covers the spec**") and "← target" in x for x in lines)
    assert "A fast path halves the time" not in t and "1 superseded claim(s) not shown" in t


def test_the_target_is_the_claim_whole_with_its_place_in_the_tree(repo):
    g = tree_store(repo).snapshot()
    t = target_text(g, "grammar")
    assert t.startswith("## The grammar covers the spec (`grammar`, open)")
    assert "Under: It parses (`gap-parse`)" in t
    assert "ruled out: a PEG generator" in t and "Next: add rules 4-6" in t
    assert "### The records it rests on" in t and "Split the parser" in t
    parent = target_text(g, "gap-parse")
    assert "### The claims under it" in parent and "The lexer tokenizes" in parent
    assert "A fast path halves the time" not in parent, "superseded children are left out"
    assert target_text(g, "fast-path-helps") == "" and target_text(g, "nope") == "" and target_text(g, "") == ""


def test_a_record_rests_on_several_records_on_the_files_store(repo):
    s = FilesStore(repo)
    a = _rec(s, "Experiment A")
    b = _rec(s, "Experiment B", parent=a)
    c = _rec(s, "A and B together", parent=[a, b, a])
    assert s.snapshot().nodes[c]["parents"] == [a, b]
    assert record_parents(None) == ["root"] and record_parents("`r-0001`") == ["r-0001"]
    bad, msg = s.record(title="x", body=BODY, parent=[a, "gap-nope"], impacts=[], news=[], none="n")
    assert bad is None and "`gap-nope` is not a record" in msg
    ok, report = s.check()
    assert ok, report


def test_mem_record_takes_parent_more_than_once(repo, monkeypatch):
    import io
    monkeypatch.setenv(memory.ENV, "files")
    monkeypatch.chdir(repo)
    s = FilesStore(repo)
    a, b = _rec(s, "A"), _rec(s, "B")
    monkeypatch.setattr("sys.stdin", io.StringIO(BODY))
    assert cli.main(["mem", "record", "--title", "both", "--parent", a, "--parent", b, "--none", "x"]) == 0
    newest = s.snapshot().records()[-1]
    assert newest["title"] == "both" and newest["parents"] == [a, b]
    monkeypatch.setattr("sys.stdin", io.StringIO(BODY))
    assert cli.main(["mem", "record", "--title", "default", "--none", "x"]) == 0
    assert s.snapshot().records()[-1]["parents"] == [newest["id"]], "left out, the parent is the newest record"


def test_named_claims_keep_their_name_when_governor_renames_them():
    plan = tag_fold({"claims": [{"new": "environment", "current": "- py"}, {"new": "other", "current": "- x"}]},
                    Graph(None))
    assert plan["claims"][0]["current"].startswith("<!-- ouroboros-claim: environment -->")
    assert "ouroboros-claim" not in plan["claims"][1]["current"]
    # Governor minted `wild-star-6005` for it; the brief still finds the host facts.
    node = {"id": "n1", "slug": "wild-star-6005", "kind": "state", "title": "Host", "status": "working",
            "parents": ["root"], "seq": 2, "content": "## Current\n\n<!-- ouroboros-claim: environment -->\n- no GPU here\n"}
    root = {"id": "root", "slug": "root", "kind": "root", "parents": [], "seq": 0}
    g = Graph({"root": "root", "nodes": {"root": root, "n1": node}, "edges": []})
    assert name_of(node) == "environment"
    brief = brief_text(g, goal_text=GOAL, goal_path="g.md")
    assert "no GPU here" in brief.split("## Environment", 1)[1]
    again = tag_fold({"claims": [{"slug": "wild-star-6005", "current": "- GPU arrived"}]}, g)
    assert again["claims"][0]["current"].startswith("<!-- ouroboros-claim: environment -->"), "a later fold keeps it"


def test_the_writer_names_a_target_and_the_worker_prompt_carries_it():
    assert "target" in WRITER_SCHEMA["required"]
    r = parse_reply(json.dumps({"turn": "ok", "done": False, "reason": "r", "did": "d", "doing": "n",
                                "target": "`grammar`", "prompt": ""}))
    assert r.target == "grammar"
    p = build_worker_prompt(iteration=3, handoff="graph", prompt="", brief="# Brief: b", charter_path="g.md",
                            agents=None, target="## The grammar (`grammar`, open)\n\n- Next: add rules")
    assert p.index("# Your target") < p.index("# Brief: b") < p.index("# How this turn works")
    assert "its `Next:` line is this turn's unit" in " ".join(p.split())
    assert "No next step was written" not in p
    none = build_worker_prompt(iteration=3, handoff="graph", prompt="", brief="# Brief: b", charter_path="g.md",
                               agents=None)
    assert "# Your target" not in none and "No next step was written for you" in none
    assert claimed_ids('{"did": "x", "target": "grammar", "claims": [{"claim": "NEW tokens"}]}') == ["grammar", "tokens"]


def _tree_writer(target: str, log: list):
    """A writer that grows the tree: it opens `grammar` under the criterion with a Next line."""
    def b(cwd: Path, prompt: str):
        from ouroboros.harness.base import Result
        log.append(prompt)
        s = FilesStore(cwd)
        g = s.snapshot()
        tail = [r["id"] for r in g.tail()]
        if "grammar" not in g.nodes:   # turn 0: the directive's gap waits in the tail as a NEW impact
            gap = next(label[4:] for r in g.tail() for _, label, _ in g.impacts_of(r) if label.startswith("NEW gap-"))
            r = _rec(s, "Plan the grammar", news=[("grammar", "opened")])
            claims = [{"new": gap, "parent": "root", "title": "It parses", "status": "open",
                       "current": f"- Asks: it parses [rec: {r}]", "cites": [r]},
                      {"new": "grammar", "parent": gap, "title": "The grammar covers the spec", "status": "open",
                       "current": f"- 0 of 9 rules [rec: {r}]\n- Next: add rule 1 [rec: {r}]", "cites": [r]}]
            claims[1]["parent"] = gap
        else:
            r = _rec(s, "A rule landed", impacts=[("grammar", "one more rule")])
            claims = [{"slug": "grammar", "status": "open", "current": f"- 1 of 9 [rec: {r}]\n- Next: rule 2 [rec: {r}]",
                       "cites": [r]}]
        _fold(s, claims, tail + [r])
        return Result(text=json.dumps({"turn": "ok", "done": False, "reason": "r", "did": "d", "doing": "n",
                                       "target": target, "prompt": ""}))
    return b


def test_the_loop_hands_the_target_claim_to_the_worker(repo):
    log: list = []
    worker = FakeHarness([], default=works_json())
    eng = make_ww(repo, worker, FakeHarness([], default=_tree_writer("grammar", log)), handoff="graph",
                  max_iterations=2, goal=GOAL)
    eng.run()
    first = worker.prompts[0]
    assert "# Your target" in first and "## The grammar covers the spec (`grammar`, open)" in first
    assert "Under: It parses" in first
    assert "Next: add rule 1" in first
    review = log[1]
    assert "The target: `grammar`" in review
    assert "← target" in review and "The grammar covers the spec" in review.split("### The claim tree", 1)[1]
    assert (eng.recorder.run_dir / NEXT_TARGET).read_text() == "grammar"
    decisions = eng.recorder.read_decisions()
    assert decisions[0]["worked_on"] == "grammar" and decisions[0]["target"] == "grammar"


def test_a_target_the_memory_does_not_hold_lets_the_worker_pick(repo):
    worker = FakeHarness([], default=works_json())
    eng = make_ww(repo, worker, FakeHarness([], default=_tree_writer("no-such-claim", [])), handoff="graph",
                  max_iterations=1, goal=GOAL)
    eng.run()
    assert "# Your target" not in worker.prompts[0]
    assert "No next step was written for you" in worker.prompts[0]


def test_graph_is_the_default_handoff_and_belongs_to_worker_writer():
    assert Config().handoff == "graph"
    assert Config(loop="standard").problem() is None
    import pytest
    with pytest.raises(ValueError, match="applies only to loop: worker-writer"):
        Config(loop="standard", handoff="both")


def test_auto_creates_a_governor_graph_when_flywheel_and_governor_are_installed(repo, monkeypatch):
    calls: list = []
    monkeypatch.setattr(memory, "can_bootstrap", lambda: True)
    monkeypatch.setattr(memory, "governor_repo", lambda r: None)

    real = subprocess.run

    def fake_run(argv, **kw):
        if argv[0] == "git":   # finding the main checkout
            return real(argv, **kw)
        calls.append((argv, kw.get("cwd")))
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(memory.subprocess, "run", fake_run)
    line = memory.bootstrap("auto", repo, title="Goal: tree")
    assert [a[:2] for a, _ in calls] == [["flywheel", "repo:init"], ["governor", "init"]]
    assert calls[1][0][3] == "Goal: tree" and calls[0][1] == str(repo)
    assert "created a governor graph" in line
    calls.clear()
    assert memory.bootstrap("files", repo, title="t") is None and not calls, "files asked for by name stays files"


def test_a_files_run_keeps_its_store_and_a_failed_init_falls_back(repo, monkeypatch):
    monkeypatch.setattr(memory, "can_bootstrap", lambda: True)
    monkeypatch.setattr(memory, "governor_repo", lambda r: None)
    real = subprocess.run
    monkeypatch.setattr(memory.subprocess, "run", lambda argv, **kw: real(argv, **kw) if argv[0] == "git"
                        else subprocess.CompletedProcess(argv, 1, "", "no disk"))
    _rec(FilesStore(repo), "an earlier run's record")
    assert memory.bootstrap("auto", repo, title="t") is None, "a repo with a files memory keeps it"
    line = memory.bootstrap("governor", repo, title="t")
    assert "exited 1" in line and "files store" in line


def test_without_the_clis_nothing_is_created(repo):
    assert memory.bootstrap("auto", repo, title="t") is None   # conftest: can_bootstrap is False
    assert memory.resolve_kind("auto", repo) == "files"


def test_mem_tree_on_the_files_store_is_a_read_verb(repo, monkeypatch, capsys):
    tree_store(repo)
    monkeypatch.setenv(memory.ENV, "files")
    monkeypatch.setenv("OUROBOROS_ROLE", "worker")
    monkeypatch.chdir(repo)
    assert cli.main(["mem", "tree"]) == 0
    out = capsys.readouterr().out
    assert "- [open] **It parses** (`gap-parse`" in out and "  - [open] **The grammar covers the spec**" in out
