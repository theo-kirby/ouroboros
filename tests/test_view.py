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
    assert '<path class="arc on"' in html and '<g class="node on">' in html and "now the critic" in html
    assert 'id="stage-for" data-since="1799999900"' in html
    assert "Commits kept</dt><dd><span class=\"num\">1</span><small>1 reverted</small>" in html   # 2 made, 1 reverted
    assert "Build passes clean" in html and "passes on CI" in html and ">met<" in html
    assert "Network test flakes" in html and ">broken<" in html
    assert "1</span> record(s) not folded" in html
    assert "{{" not in html
    assert not list((run_dir / "view").glob(".*tmp"))


def test_usage_is_a_row_of_cells_per_window(run_dir, repo):
    view.render(run_dir, repo, goal_text=GOAL)
    html = page(run_dir)
    assert html.count('<div class="gauge">') == 2
    assert 'aria-valuenow="62"' in html and html.count('<i class="on"') == 12 + 4   # 62% and 20% of 20 cells
    assert "+35 this run" in html and 'data-at="1800004000"' in html and "weekly window" in html


def test_the_timeline_has_a_column_and_a_row_per_iteration(run_dir, repo, graph):
    view.render(run_dir, repo, goal_text=GOAL, graph=graph)
    html = page(run_dir)
    assert 'href="#it-5"' in html and 'href="#it-6"' in html
    assert '<tr id="it-6">' in html and html.index('<tr id="it-6">') < html.index('<tr id="it-5">'), "newest first"
    assert 'class="fold"' in html and 'class="x"' in html   # iteration 5 folded, iteration 6 reverted
    assert "next: tests" in html and "Critic: good unit" in html
    assert "typ. 1 min" in html   # the critic and the scribe each took a minute in iteration 5


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
