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


def test_the_page_draws_each_finding_title_summary_graph_visuals_notes(repo, run):
    g = tree_store(repo).snapshot()
    (run / "view" / "assets" / "rules.png").write_bytes(PNG)
    write(run, [GOOD, dict(GOOD, id="old", title="An overturned idea", status="retracted", visuals=[], notes="")])
    view.render(run, repo, goal_text=GOAL, graph=g)
    html = page(run)
    sec = html.split('id="h-story"', 1)[1].split('id="graph-sec"', 1)[0]
    f1 = sec.split('id="finding-grammar-rules"', 1)[1].split("</article>", 1)[0]
    order = [f1.index(x) for x in ("Three grammar rules", "Rules 1-3 parse", '<svg class="ev"', 'src="assets/rules.png"',
                                   'class="fnd-notes"')]
    assert order == sorted(order), "title, summary, the graph, the visuals, the notes"
    assert "<li>ran <code>pytest</code></li>" in f1 and "<pre><code>python -m x</code></pre>" in f1
    assert 'data-focus="f:grammar-rules"' in f1 and 'data-focus="n:grammar"' in f1
    assert "fast-path-helps" in f1 or "A fast path halves the time" in f1
    assert 'class="fnd gone"' in sec and "No visual yet." in sec
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
