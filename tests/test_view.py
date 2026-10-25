"""The run view: render is deterministic and never raises; serve shows view/ and nothing else."""

import http.client
import json
import threading
from pathlib import Path

import pytest

from ouroboros import view
from ouroboros.memory.files import FilesStore

GOAL = ("# Goal: Ship the widget\n\n## Mission\n\nMake the widget build & ship <fast>.\n\n## Done criteria\n\n"
        "- [ ] The build passes on a clean clone.\n- [x] A README exists.\n")
BODY = "## What\n\nDid {what}.\n\n## Why\n\ny\n\n## Method\n\nz\n\n## Result\n\n{result}\n"


def body(what="a thing", result="it works"):
    return BODY.format(what=what, result=result)


@pytest.fixture
def run_dir(repo):
    d = repo / ".ouroboros" / "runs" / "nt9"
    d.mkdir(parents=True)
    (d / "status.json").write_text(json.dumps({
        "run": "nt9", "state": "critique", "iteration": 7, "branch": "ouroboros/nt9", "harness": "claude",
        "memory": "files", "elapsed_s": 3725, "usage_compact": "claude 5h 31%", "last_verdict": "continue",
        "epoch": 1_800_000_000.0, "stage_since": 1_799_999_900.0,
        "usage": {"claude": {"windows": {"five_hour": {"utilization": 0.62, "resets_at": 1_800_004_000.0, "minutes": 300},
                                         "seven_day": {"utilization": 0.2, "minutes": 10080}},
                             "first_windows": {"five_hour": 0.27}}},
    }))
    (d / "critic.jsonl").write_text("\n".join(json.dumps(x) for x in [
        {"iteration": 5, "verdict": "continue", "reason": "good unit", "did": "Fix <script>alert(1)</script>",
         "doing": "next: tests"},
        {"iteration": 6, "verdict": "reject", "reason": "broke <b>build</b>", "fix_first": "fix it"},
    ]) + "\nnot json\n")
    (d / "iterations.jsonl").write_text("\n".join(json.dumps(x) for x in [
        {"ts": "2027-01-15T07:57:00+00:00", "iteration": 5, "step": "commit", "sha": "abc", "changed": True},
        {"ts": "2027-01-15T07:58:00+00:00", "iteration": 5, "step": "critique", "verdict": "continue"},
        {"ts": "2027-01-15T07:59:00+00:00", "iteration": 5, "step": "scribe", "recorded": True, "fold_due": True},
        {"ts": "2027-01-15T08:03:00+00:00", "iteration": 6, "step": "commit", "sha": "def", "changed": True},
        {"ts": "2027-01-15T08:03:30+00:00", "iteration": 6, "step": "critique", "verdict": "reject"},
        {"ts": "2027-01-15T08:03:31+00:00", "iteration": 6, "step": "revert", "to": "ok-5"},
    ]) + "\n")
    return d


@pytest.fixture
def graph(repo):
    s = FilesStore(repo)
    s.prepare()
    s.record(title="Directive", body=body(), parent="root", impacts=[],
             news=[("gap-build-passes-clean-clone", "open")], none=None)
    s.record(title="Fix <script>alert(1)</script>", body=body("the fix", "build green"), parent="r-0001",
             impacts=[], news=[], none="test")
    ok, msg = s.fold({"claims": [{"new": "gap-build-passes-clean-clone", "parent": "root", "title": "Build passes clean",
                                  "status": "working", "current": "- passes on CI [rec: r-0002]", "cites": ["r-0002"]},
                                 {"new": "flaky-net", "parent": "root", "title": "Network test flakes",
                                  "status": "broken", "current": "- fails 1 in 5", "cites": ["r-0001"]}],
                      "folded": ["r-0001", "r-0002"]})
    assert ok, msg
    s.record(title="Third", body=body(), parent="r-0002", impacts=[], news=[], none="n")
    return s.snapshot()


def page(run_dir):
    return (run_dir / "view" / "index.html").read_text()


def live(run_dir):
    return json.loads((run_dir / "view" / "live.json").read_text())


def test_render_writes_the_page_and_live_json(run_dir, repo, graph):
    out = view.render(run_dir, repo, goal_text=GOAL, graph=graph, now=1_800_000_030.0)
    assert out == run_dir / "view" / "index.html"
    assert (run_dir / "view" / "assets").is_dir()
    html = page(run_dir)
    j = live(run_dir)
    assert j["state"] == "critique" and j["iteration"] == 7 and j["ts"] == 1_800_000_000.0
    assert f'data-version="{j["version"]}"' in html
    for fact in ("nt9", "Ship the widget", "ouroboros/nt9", "1 h 02", "updated 30 s ago"):
        assert fact in html
    assert 'class="ring" data-state="critique"' in html and '<g class="node on" data-key="critique"' in html
    assert html.count('class="oarc still"') == 1 and html.count('class="orb"') == 3 and 'class="arc on"' not in html and "now the critic" in html
    assert 'id="stage-for" data-since="1799999900"' in html
    assert "Commits kept</dt><dd><span class=\"num\">1</span><small>1 reverted</small>" in html   # 2 made, 1 reverted
    assert "Build passes clean" in html and "passes on CI" in html and ">met<" in html
    assert "Network test flakes" in html and ">broken<" in html
    assert "1</span> record(s) not folded" in html
    assert "{{" not in html
    assert not list((run_dir / "view").glob(".*tmp"))


def test_usage_bars_are_the_runs_own_share_of_each_window(run_dir, repo):
    view.render(run_dir, repo, goal_text=GOAL)
    html = page(run_dir)
    # no run_spent (an older status): the rise since the first reading, 62 - 27 = 35%;
    # the weekly window has no first reading, so it has no row
    assert html.count('<div class="gauge">') == 1
    assert 'aria-valuenow="35"' in html and 'data-at="1800004000"' in html and "5-hour window" in html
    assert 'aria-valuenow="62"' not in html, "the account's own level is not drawn"
    status = json.loads((run_dir / "status.json").read_text())
    status["usage"]["claude"]["run_spent"] = {"five_hour": 1.3, "seven_day": 0.05}
    status["tokens"] = {"claude": {"input": 1200, "output": 640_000, "cache_read": 52_000_000}}
    status["models"] = {"claude": {"claude-opus-5-5": ["actor", "critic"], "claude-sonnet-5": ["scribe"]}}
    (run_dir / "status.json").write_text(json.dumps(status))
    view.render(run_dir, repo, goal_text=GOAL)
    html = page(run_dir)
    assert 'aria-valuenow="130"' in html and "130<small>%</small>" in html and 'aria-valuenow="5"' in html
    assert html.count('<i class="on"') == 20 + 1, "past a whole window every cell is lit"
    assert "<code>claude-opus-5-5</code> <span class=\"quiet\">actor, critic</span>" in html
    assert "52.6 M" in html and "640 k out" in html and 'class="t-cache_read"' in html


def test_the_time_budget_is_a_row_of_cells(run_dir, repo):
    status = json.loads((run_dir / "status.json").read_text())
    view.render(run_dir, repo, goal_text=GOAL)
    assert "no time limit set" in page(run_dir)
    status.update(time_budget_s=4 * 3600, started_at=1_799_996_275.0)
    (run_dir / "status.json").write_text(json.dumps(status))
    view.render(run_dir, repo, goal_text=GOAL)
    html = page(run_dir)
    assert '<div class="gauge clock" data-start="1799996275" data-budget="14400">' in html
    assert 'aria-label="time budget used"' in html and "1 h 02</span> elapsed" in html and "2 h 57</span> left" in html


def test_right_now_says_what_the_stage_is_working_on(run_dir, repo):
    view.render(run_dir, repo, goal_text=GOAL)   # the critic on 7; the last verdict is 6's, with no `doing`
    html = page(run_dir)
    assert '<section class="now"' in html and "The critic is checking the work · iteration 7" in html
    with (run_dir / "critic.jsonl").open("a") as f:
        f.write(json.dumps({"iteration": 7, "verdict": "continue", "did": "Made the **build** green.",
                            "doing": "Writing <the> tests."}) + "\n")
    status = json.loads((run_dir / "status.json").read_text())
    status["state"] = "scribe"
    (run_dir / "status.json").write_text(json.dumps(status))
    view.render(run_dir, repo, goal_text=GOAL)
    html = page(run_dir)
    assert "The scribe is writing it down" in html and "Made the <strong>build</strong> green." in html
    assert "Next: Writing &lt;the&gt; tests." in html
    status.update(state="work", iteration=8)
    (run_dir / "status.json").write_text(json.dumps(status))
    view.render(run_dir, repo, goal_text=GOAL)
    assert '<p class="now-t">Writing &lt;the&gt; tests.</p>' in page(run_dir)


def test_a_legacy_plan_node_is_drawn_as_a_claim(graph):
    """Governor's embed has a light palette only in its hub layout, which any `plan` node turns off."""
    nodes = dict(graph.nodes)
    nodes["p1"] = {"id": "p1", "kind": "plan", "title": "Old plan", "parents": [graph.root], "status": "open"}
    nodes["o1"] = {"id": "o1", "kind": "mystery", "title": "?", "parents": [graph.root]}
    data = view.report_graph(view.Graph({"root": graph.root, "nodes": nodes, "edges": graph.edges}))
    kinds = {t["name"] for n in data["nodes"] for t in n["tags"] if t["id"].startswith("graph:")}
    assert kinds == {"root", "state", "record"}


def test_the_findings_span_the_column():
    assert "76ch" not in (Path(view.__file__).parent / view.TEMPLATE).read_text()


def test_the_timeline_has_a_column_and_a_row_per_iteration(run_dir, repo, graph):
    view.render(run_dir, repo, goal_text=GOAL, graph=graph)
    html = page(run_dir)
    assert 'href="#it-5"' in html and 'href="#it-6"' in html
    six, five = '<details class="it" id="it-6">', '<details class="it" id="it-5">'
    assert six in html and html.index(six) < html.index(five), "newest first, each folded"
    assert 'class="fold"' in html and 'class="x"' in html   # iteration 5 folded, iteration 6 reverted
    assert "<dt>Next</dt><dd>next: tests</dd>" in html and "<dt>Critic</dt><dd>good unit</dd>" in html
    assert "typ. 1 min" in html   # the critic and the scribe each took a minute in iteration 5


def test_each_finding_folds_under_its_heading(run_dir, repo):
    view.render(run_dir, repo, goal_text=GOAL)
    view.story_path(run_dir).write_text(
        '<p>lead</p><section><h2 class="x">Fast now</h2><p>body</p><section><h3>inner</h3></section></section>'
        '<section><p>no heading</p></section>')
    view.render(run_dir, repo, goal_text=GOAL)
    html = page(run_dir)
    assert ('<section><details class="finding" id="f-') in html and \
        ' open><summary><h2 class="x">Fast now</h2></summary><p>body</p><section><h3>inner</h3></section></details></section>' in html
    assert html.count('class="finding"') == 1, "nested sections and headless ones pass through"
    assert "<p>lead</p>" in html and "<section><p>no heading</p></section>" in html


def test_the_memory_graph_comes_before_the_findings(run_dir, repo, graph):
    view.render(run_dir, repo, goal_text=GOAL, graph=graph)
    html = page(run_dir)
    assert html.index('id="h-graph"') < html.index('id="h-story"') < html.index('id="h-progress"')


def test_the_summary_is_the_scribes_else_the_mission(run_dir, repo):
    view.render(run_dir, repo, goal_text=GOAL)
    assert "From the charter" in page(run_dir) and "Make the widget build &amp; ship &lt;fast&gt;." in page(run_dir)
    view.summary_path(run_dir).write_text("It builds <now>.\n\nNext: ship it.")
    view.render(run_dir, repo, goal_text=GOAL)
    html = page(run_dir)
    assert "<p>It builds &lt;now&gt;.</p><p>Next: ship it.</p>" in html and "From the charter" not in html


def test_the_memory_graph_is_governors_report_shape(graph):
    data = view.report_graph(graph)
    ids = {n["id"] for n in data["nodes"]}
    assert data["schemaVersion"] == 1 and data["head"] is None and len(ids) == len(data["nodes"])
    assert all(n["parentId"] is None or n["parentId"] in ids for n in data["nodes"])
    assert all(e["source"] in ids and e["target"] in ids for e in data["edges"])
    assert len({e["id"] for e in data["edges"]}) == len(data["edges"])
    tags = {t["id"] for n in data["nodes"] for t in n["tags"]}
    assert {"graph:root", "graph:state", "graph:record", "status:working", "status:broken", "governor:folded"} <= tags
    assert any(e["relation"] == "citation" for e in data["edges"])
    assert data["loadedAt"] == max(n["createdAt"] for n in data["nodes"] if "createdAt" in n), "stable, not now"


def test_the_graph_page_is_written_once_and_framed(run_dir, repo, graph):
    view.render(run_dir, repo, goal_text=GOAL, graph=graph)
    gp = run_dir / "view" / "graph.html"
    text = gp.read_text()
    assert 'id="flywheel-snapshot"' in text and "<script>alert(1)" not in text and "\\u003cscript>alert(1)" in text
    html = page(run_dir)
    assert '<iframe class="graph" data-src="graph.html?v=' in html and 'id="graph-sec" data-v="' in html
    mtime = gp.stat().st_mtime_ns
    view.render(run_dir, repo, goal_text=GOAL, graph=graph, now=1_800_009_999.0)
    assert gp.stat().st_mtime_ns == mtime, "an unchanged memory does not rewrite the graph page"
    view.render(run_dir, repo, goal_text=GOAL, graph=None)
    assert "The memory could not be read." in page(run_dir)


def test_everything_but_the_story_is_escaped(run_dir, repo, graph):
    view.render(run_dir, repo, goal_text=GOAL, graph=graph)
    html = page(run_dir)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "broke &lt;b&gt;build&lt;/b&gt;" in html
    assert "&lt;fast&gt;" in html


def test_the_story_goes_in_raw_and_a_placeholder_shows_without_it(run_dir, repo, graph):
    view.render(run_dir, repo, goal_text=GOAL, graph=graph)
    assert "The scribe has not written the story yet." in page(run_dir)
    view.story_path(run_dir).write_text('<h2>Night one</h2><figure><img src="assets/x.png"></figure> {{title}}')
    view.render(run_dir, repo, goal_text=GOAL, graph=graph)
    html = page(run_dir)
    assert '<h2>Night one</h2><figure><img src="assets/x.png"></figure> {{title}}' in html   # no second fill
    assert "The scribe has not written" not in html


def test_a_repo_template_overrides_the_default(run_dir, repo):
    t = repo / ".ouroboros" / "view" / "template.html"
    t.parent.mkdir(parents=True)
    t.write_text("<main id=view>MINE {{iteration}} {{unknown}}</main>")
    view.render(run_dir, repo, goal_text=GOAL)
    assert page(run_dir) == "<main id=view>MINE 7 {{unknown}}</main>"


def test_no_graph_falls_back_to_the_charter(run_dir, repo):
    assert view.render(run_dir, repo, goal_text=GOAL, graph=None)
    html = page(run_dir)
    assert "The build passes on a clean clone." in html and ">ticked<" in html and "1 of 2" in html
    assert "The memory could not be read." in html


def test_broken_inputs_do_not_raise(run_dir, repo):
    (run_dir / "status.json").write_text("{nope")
    (run_dir / "critic.jsonl").write_text("[1,2]\n{\n")
    assert view.render(run_dir, repo, goal_text="", graph="not a graph") is not None
    assert "could not be made" not in page(run_dir) and "No usage reading yet." in page(run_dir)


def test_render_returns_none_when_it_cannot_write(tmp_path, repo):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    assert view.render(blocker, repo, goal_text=GOAL) is None
    assert view.render(tmp_path / "file" / "deeper", repo, goal_text=GOAL) is None


def test_the_version_changes_only_with_the_content(run_dir, repo, graph):
    view.render(run_dir, repo, goal_text=GOAL, graph=graph, now=1_800_000_010.0)
    first = live(run_dir)["version"]
    view.render(run_dir, repo, goal_text=GOAL, graph=graph, now=1_800_000_500.0)
    assert live(run_dir)["version"] == first, "only the clock moved"
    view.story_path(run_dir).write_text("<p>new</p>")
    view.render(run_dir, repo, goal_text=GOAL, graph=graph, now=1_800_000_500.0)
    assert live(run_dir)["version"] != first


@pytest.fixture
def server(run_dir, repo):
    view.render(run_dir, repo, goal_text=GOAL)
    view.assets_dir(run_dir).joinpath("x.png").write_bytes(b"\x89PNG fake")
    srv = view.make_server(run_dir, "127.0.0.1", 0)
    t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    t.start()
    yield srv.server_address[1]
    srv.shutdown()
    srv.server_close()


def get(port, path, method="GET"):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request(method, path)
    r = c.getresponse()
    data = r.read()
    c.close()
    return r.status, dict(r.getheaders()), data


def test_serve_shows_the_view(server):
    status, headers, data = get(server, "/")
    assert status == 200 and b'id="view"' in data and headers["Cache-Control"] == "no-store"
    assert headers["Content-Type"].startswith("text/html")
    status, headers, data = get(server, "/live.json")
    assert status == 200 and json.loads(data)["version"] and headers["Cache-Control"] == "no-store"
    status, headers, data = get(server, "/assets/x.png")
    assert status == 200 and data == b"\x89PNG fake" and headers["Content-Type"] == "image/png"
    status, headers, data = get(server, "/", method="HEAD")
    assert status == 200 and data == b""


@pytest.mark.parametrize("path", ["/../status.json", "/%2e%2e/status.json", "/assets/../../status.json",
                                  "/%2e%2e%2fstatus.json", "/..%5cstatus.json", "/assets/", "/assets", "/nope.html",
                                  "/.index.html.1.tmp"])
def test_serve_refuses_anything_else(server, path):
    status, _, data = get(server, path)
    assert status in (403, 404)
    assert b"elapsed_s" not in data


def test_serve_refuses_writes(server):
    assert get(server, "/", method="POST")[0] == 405


def test_inline_markdown_formats_but_never_injects():
    md = view._md
    assert md("**bold**, *em*, _em_ and `x_y*z`") == "<strong>bold</strong>, <em>em</em>, <em>em</em> and <code>x_y*z</code>"
    assert md("snake_case_name, __init__, 2*3*4 and a * b") == "snake_case_name, __init__, 2*3*4 and a * b"
    assert md("[docs](https://x.org/_a_)") == '<a href="https://x.org/_a_" rel="noopener noreferrer">docs</a>'
    assert md("[bad](javascript:alert(1)) <script>") == "[bad](javascript:alert(1)) &lt;script&gt;"
    assert md("**<b>x</b>**") == "<strong>&lt;b&gt;x&lt;/b&gt;</strong>"
    assert md("a\x000\x00b") == "a0b"
    assert view._plain("**a** _b_ `c` [d](https://e)") == "a b c d"


def test_markdown_shows_on_the_page(run_dir, repo):
    (run_dir / "critic.jsonl").write_text(json.dumps({"iteration": 5, "verdict": "continue", "did": "Added **expectimax**",
                                                      "reason": "the `bench` holds"}) + "\n")
    view.summary_path(run_dir).parent.mkdir(parents=True, exist_ok=True)
    view.summary_path(run_dir).write_text("The bot is *fast* now.")
    view.render(run_dir, repo, goal_text=GOAL)
    html = page(run_dir)
    assert "Added <strong>expectimax</strong>" in html and "the <code>bench</code> holds" in html
    assert "The bot is <em>fast</em> now." in html


def _claims_graph(repo, n):
    s = FilesStore(repo)
    s.prepare()
    s.record(title="Directive", body=body(), parent="root", impacts=[],
             news=[(f"gap-criterion-{i}", "open") for i in range(n)], none=None)
    ok, msg = s.fold({"claims": [{"new": f"gap-criterion-{i}", "parent": "root", "title": f"Criterion {i}", "status": "open",
                                  "current": "- not yet [rec: r-0001]", "cites": ["r-0001"]} for i in range(n)],
                      "folded": ["r-0001"]})
    assert ok, msg
    return s.snapshot()


def test_long_lists_show_three_then_a_toggle(run_dir, repo):
    view.render(run_dir, repo, goal_text="## Done criteria\n" + "".join(f"- [ ] Criterion {i}\n" for i in range(5)), graph=_claims_graph(repo, 5))
    html = page(run_dir)
    crit = html[html.index('id="h-criteria"'):html.index('id="h-frontier"')]
    assert crit.count('<li class="row') == 5
    assert crit.index('<details class="more-rows" id="more-criteria">') > crit.index('<ul class="rows">')
    assert "Show 2 more" in crit and "Show less" in crit
    head = crit[:crit.index("<details")]
    assert head.count('<li class="row') == 3
    assert 'id="more-frontier"' in html and "Show 2 more" in html[html.index('id="h-frontier"'):]


def test_short_lists_have_no_toggle(run_dir, repo):
    view.render(run_dir, repo, goal_text=GOAL, graph=_claims_graph(repo, 3))
    assert "more-rows" not in page(run_dir).split("</style>", 1)[1]


def test_a_graph_too_big_keeps_the_claims_and_the_newest_records(monkeypatch):
    nodes = {"root": {"id": "root", "kind": "root", "title": "run", "parents": []},
             "c1": {"id": "c1", "kind": "state", "title": "claim", "status": "open", "parents": ["root"]},
             "old": {"id": "old", "kind": "state", "title": "gone", "status": "superseded", "parents": ["root"]}}
    prev = "root"
    for i in range(1, 41):
        nodes[f"r{i}"] = {"id": f"r{i}", "kind": "record", "title": f"rec {i}", "seq": i, "parents": [prev],
                          "at": f"2026-10-17T10:{i:02d}:00Z", "folded": i < 30}
        prev = f"r{i}"
    graph = view.Graph({"root": "root", "nodes": nodes, "edges": [{"source": "c1", "target": "r40", "content": "cites"}]})
    assert view.fit_report_graph(graph)[1] == 0
    monkeypatch.setattr(view, "GRAPH_MAX_NODES", 12)
    data, omitted = view.fit_report_graph(graph)
    ids = {n["id"] for n in data["nodes"]}
    assert omitted == 30 and len(ids) == 12 and {"root", "c1", "r40", "r31"} <= ids and not {"old", "r30", "r1"} & ids
    assert all(n["parentId"] is None or n["parentId"] in ids for n in data["nodes"])
    assert "Only the newest 10 of 40 records are drawn" in view._graph(graph, "v1", omitted)


def test_the_ring_shows_a_command_and_the_background_shells(run_dir, repo):
    st = json.loads((run_dir / "status.json").read_text())
    st["activity"] = {"kind": "command", "what": "uv run pytest -q", "since": 1_799_999_950.0, "background": 2, "id": "x"}
    (run_dir / "status.json").write_text(json.dumps(st))
    html = view.render(run_dir, repo, goal_text=GOAL).read_text()
    assert 'data-activity="command"' in html and "2 background shells" in html
    assert "running a command" in html and '<span class="act-for" data-since="1799999950">' in html
    st["state"] = "idle"   # between stages, an old activity is not shown
    (run_dir / "status.json").write_text(json.dumps(st))
    html = view.render(run_dir, repo, goal_text=GOAL).read_text()
    assert 'data-activity=""' in html and "background shell" not in html
