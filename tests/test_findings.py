"""The run's findings (findings.py), how the run page draws them against the memory graph,
and the figure kit every run gets (`ouroboros plot`, figstyle)."""

import json
import re
import subprocess
import sys

import pytest

from ouroboros import findings, figstyle, view
from ouroboros.memory.files import FilesStore
from ouroboros.roles.writer import build_writer_prompt

from test_claim_tree import tree_store
from test_view import GOAL, page

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


@pytest.fixture
def run(repo):
    d = repo / ".ouroboros" / "runs" / "t"
    (d / "view" / "assets").mkdir(parents=True)
    (d / "status.json").write_text(json.dumps({"run": "t", "state": "work", "iteration": 2, "loop": "worker-writer",
                                               "epoch": 1_800_000_000.0, "elapsed_s": 600}))
    return d


def write(run, items):
    findings.path(run).write_text(json.dumps({"findings": items}))


GOOD = {"id": "grammar-rules", "title": "Three grammar rules cover the spec's first section",
        "summary": "Rules 1-3 parse every example of section 1. Rules 4-9 are next.", "status": "holds",
        "claims": ["gap-parse"], "records": [],
        "visuals": [{"file": "assets/rules.png", "caption": "Examples parsed per rule."}],
        "notes": "Method:\n\n- ran `pytest`\n- counted\n\n```\npython -m x\n```"}


def test_a_good_findings_file_checks_clean_and_a_bad_one_says_why(repo, run):
    g = tree_store(repo).snapshot()
    (run / "view" / "assets" / "rules.png").write_bytes(PNG)
    write(run, [GOOD])
    assert findings.check(run, g) == []
    assert findings.check(run.parent / "none", g) == [], "no file is not a problem"
    write(run, [GOOD, dict(GOOD, title="x" * 130, summary="One. Two. Three. Four.", status="maybe",
                           claims=["nope"], visuals=[{"file": "assets/gone.png"}, {"file": "notes.txt", "caption": "c"}])])
    got = "\n".join(findings.check(run, g))
    for bit in ("used twice", "under 120", "4 sentences", "`status` is one of", "`nope` in `claims` is not in the memory",
                "`assets/gone.png` does not exist", "has no caption", "`notes.txt` is not an image"):
        assert bit in got, bit
    findings.path(run).write_text("{not json")
    assert "does not parse" in findings.check(run, g)[0]
    assert findings.load(run) == []


def test_a_finding_rests_on_its_claims_what_hangs_under_them_and_their_records(repo):
    g = tree_store(repo).snapshot()
    claims, records = findings.related(g, {"claims": ["gap-parse"]})
    assert [c["id"] for c in claims] == ["gap-parse", "lexer", "grammar", "fast-path-helps"], \
        "every claim under it, the rejected hypothesis included"
    assert [r["id"] for r in records] == ["r-0001", "r-0002"]


def test_the_page_draws_each_finding_title_summary_visuals_graph_notes(repo, run):
    g = tree_store(repo).snapshot()
    (run / "view" / "assets" / "rules.png").write_bytes(PNG)
    write(run, [GOOD, dict(GOOD, id="old", title="An overturned idea", status="retracted", visuals=[], notes="")])
    view.render(run, repo, goal_text=GOAL, graph=g)
    html = page(run)
    sec = html.split('id="h-story"', 1)[1].split('id="graph-sec"', 1)[0]
    f1 = sec.split('id="finding-grammar-rules"', 1)[1].split('id="finding-old"', 1)[0]
    order = [f1.index(x) for x in ("Three grammar rules", "Rules 1-3 parse", 'src="assets/rules.png"', '<div class="ev"',
                                   'class="fnd-notes"')]
    assert order == sorted(order), "title, summary, the visuals, the graph, the notes"
    assert "<li>ran <code>pytest</code></li>" in f1 and "<pre><code>python -m x</code></pre>" in f1
    assert 'data-focus="f:grammar-rules"' in f1 and 'data-focus="n:grammar"' in f1
    assert "fast-path-helps" in f1 or "A fast path halves the time" in f1
    assert 'class="fnd gone"' in sec and "No visual yet." not in sec, "a finding may go without a visual"
    m = json.loads(re.search(r'<script id="run-map" type="application/json">(.*?)</script>', html, re.S).group(1))
    assert set(m["keys"]["f:grammar-rules"]) >= {"gap-parse", "grammar", "r-0002"}


def test_without_findings_a_worker_writer_page_says_the_writer_adds_them(repo, run):
    view.render(run, repo, goal_text=GOAL, graph=tree_store(repo).snapshot())
    assert "The writer adds one when the run first knows something" in page(run)


def test_the_writer_is_told_the_findings_file_its_problems_and_the_figure_kit():
    p = build_writer_prompt(iteration=3, handoff="graph", where="w", goal_text=GOAL, charter_path="g.md", fold="F",
                            findings_path="/r/view/findings.json", assets_dir="/r/view/assets",
                            findings='{"findings": []}', findings_problems="- `x`: no `summary`")
    assert "/r/view/findings.json" in p and "/r/view/summary.txt" in p and "- `x`: no `summary`" in p
    assert "ouroboros plot fig.py" in p and "/r/view/assets/<name>.png" in p and "#D46A8C" in p
    assert "Update the findings and their visuals" in p


def test_the_figure_kit_draws_in_the_palette_and_flags_clashing_text(tmp_path, monkeypatch):
    monkeypatch.setenv(figstyle.ASSETS_ENV, str(tmp_path))
    figstyle.apply()
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots()
    ax.plot([0, 1], [0, 1])
    ax.text(0.5, 0.5, "label one")
    ax.text(0.5, 0.5, "label two")
    assert any("overlaps" in x for x in figstyle.clashes(fig))
    assert figstyle.off_palette(fig) == []
    ax.plot([0, 1], [1, 0], color="#00ff00")
    assert "#00FF00" in figstyle.off_palette(fig)
    out = figstyle.save(fig, "t")
    assert out == tmp_path / "t.png" and out.exists() and (tmp_path / "t.svg").exists()
    plt.close(fig)


def test_ouroboros_plot_runs_a_script_with_seaborn_into_the_assets(tmp_path):
    script = tmp_path / "f.py"
    script.write_text("import seaborn as sns, pandas as pd, matplotlib.pyplot as plt\n"
                      "from ouroboros.figstyle import save\n"
                      "fig, ax = plt.subplots()\n"
                      "sns.barplot(data=pd.DataFrame({'a': ['x', 'y'], 'b': [1, 2]}), x='a', y='b', ax=ax)\n"
                      "save(fig, 'bars')\n")
    proc = subprocess.run([sys.executable, "-m", "ouroboros.cli", "plot", str(script)], capture_output=True, text=True,
                          env={**__import__("os").environ, figstyle.ASSETS_ENV: str(tmp_path / "assets")})
    assert proc.returncode == 0, proc.stderr
    assert (tmp_path / "assets" / "bars.png").exists()
    assert "every colour in the palette" in proc.stdout


def test_the_check_says_when_the_summary_is_too_long(repo, run):
    summary = findings.path(run).parent / "summary.txt"
    summary.write_text("What it is for. Why. How.\n\nWhere it stands. Next.")
    assert findings.check_summary(run) == []
    summary.write_text("One. Two. Three. Four.\n\nFive. Six. Seven.\n\nEight.")
    got = "\n".join(findings.check(run, None))
    assert "3 paragraphs" in got and "paragraph 1 is 4 sentences; at most 3" in got and "paragraph 2 is 3 sentences; at most 2" in got


def test_findings_allow_only_one_figure_and_a_short_caption(run):
    (run / 'view/assets/rules.png').write_bytes(PNG)
    write(run, [dict(GOOD, visuals=GOOD['visuals'] * 2)])
    assert any('at most 1' in p for p in findings.check(run))
    write(run, [dict(GOOD, visuals=[dict(GOOD['visuals'][0], caption='One. Two. Three.')])])
    assert any('caption' in p and 'two sentences' in p for p in findings.check(run))


def test_five_findings_open_and_graph_details_closed(repo, run):
    write(run, [dict(GOOD, id=f'finding-{i}') for i in range(8)])
    html = view._findings_ledger(run, tree_store(repo).snapshot(), [])
    tags = re.findall(r'<details class="fnd"[^>]*>', html)
    assert len(tags) == 8
    assert all(' open' in tag for tag in tags[:5])
    assert all(' open' not in tag for tag in tags[5:])
    graph_tags = re.findall(r'<details class="fnd-graph"[^>]*>', html)
    assert len(graph_tags) == 8 and all(' open' not in tag for tag in graph_tags)
    assert '4 claims and 2 records' in html
    assert 'Expand all' in html and 'Collapse all' in html


def test_legacy_visuals_render_as_one_image_and_caption(run):
    (run / 'view/assets/rules.png').write_bytes(PNG)
    write(run, [dict(GOOD, visuals=GOOD['visuals'] * 2)])
    html = view._findings_ledger(run, None, [])
    assert html.count('<figure>') == 1
    assert html.count('<img ') == 1
    assert html.count('<figcaption>') == 1
    assert 'data:image/svg+xml;base64,' in html


def test_composed_figure_keeps_all_panels_and_rejects_escaping_paths(run, tmp_path):
    import base64
    import xml.etree.ElementTree as ET

    (run / 'view/assets/rules.png').write_bytes(PNG)
    outside = tmp_path / 'outside.png'
    outside.write_bytes(PNG)
    visuals = GOOD['visuals'] * 4 + [{'file': str(outside), 'caption': 'Outside.'}]
    write(run, [dict(GOOD, visuals=visuals)])
    html = view._findings_ledger(run, None, [])
    encoded = re.search(r'<img src="data:image/svg\+xml;base64,([^"]+)"', html).group(1)
    svg = ET.fromstring(base64.b64decode(encoded))
    panels = svg.findall('{http://www.w3.org/2000/svg}image')
    assert len(panels) == 4
    assert {(p.get('x'), p.get('y')) for p in panels} == {
        ('16', '16'), ('816', '16'), ('16', '616'), ('816', '616')}
    assert all(base64.b64decode(p.get('href').split(',', 1)[1]) == PNG for p in panels)
    assert 'Outside.' not in html
