import json
import re
from pathlib import Path

import pytest

from ouroboros import publication
from ouroboros.budget import BudgetClock
from ouroboros.config import Config, StopConfig
from ouroboros.engine import Engine
from ouroboros.gitguard import GitGuard
from ouroboros.harness.base import Result
from ouroboros.memory.files import FilesMemory
from ouroboros.recorder import Recorder
from ouroboros.roles.actor import build_actor_prompt
from ouroboros.roles.scribe import Scribe, build_scribe_prompt

from fake_harness import FakeHarness, scribes, works

# Every placeholder the publication skills use; none may reach a model.
PLACEHOLDERS = ("story", "publish", "ledger_rules", "template", "figures", "main_max", "panels",
                "supplementary", "ledger_path", "story_path", "assets_dir", "view_dir", "summary_path",
                "pass", "problems", "reason", "where", "goal", "profile")


def leftovers(text: str) -> list[str]:
    return [p for p in PLACEHOLDERS if "{" + p + "}" in text]


def cfg(**kw) -> Config:
    return Config.model_validate({"run": "t", **kw})


# -- config ---------------------------------------------------------------------

def test_the_profile_picks_the_template_unless_the_config_names_one():
    assert cfg().profile == "freeform" and cfg().template == "notebook"
    assert cfg(profile="research").template == "paper"
    assert cfg(profile="development").template == "product"
    assert cfg(profile="research", publication={"template": "notebook"}).template == "notebook"
    c = cfg(publication={"figures": {"enabled": False}, "consolidate_every": None})
    assert c.publication.figures.enabled is False and c.publication.consolidate_every is None
    with pytest.raises(ValueError):
        cfg(profile="essay")


def test_mid_run_drafts_are_opt_in_and_never_for_a_notebook():
    assert not publication.publish_due(cfg(), 99)
    assert not publication.publish_due(cfg(profile="research"), 99)              # by default: only at the end
    every5 = cfg(profile="research", publication={"consolidate_every": 5})
    assert not publication.publish_due(every5, 4) and publication.publish_due(every5, 5)


def test_the_final_pass_skips_interrupts_and_spend_caps():
    r = cfg(profile="research")
    assert publication.final_due(r, "wall clock: 2h elapsed")
    assert publication.final_due(r, "critic accepted done 1x in a row")
    assert not publication.final_due(r, "interrupted by user (0 child process(es) killed)")
    assert not publication.final_due(r, "max_cost_usd 5 reached (~$5.10)")
    assert not publication.final_due(r, "max_usage 80% reached (claude week at 81%)")
    assert not publication.final_due(cfg(), "wall clock: 2h elapsed")
    assert not publication.final_due(cfg(profile="research", publication={"final": False}), "wall clock: 2h elapsed")


# -- prompts --------------------------------------------------------------------

def test_profiles_reach_the_actor_and_the_critic_and_freeform_adds_nothing():
    assert publication.profile_text("freeform", "actor") == ""
    assert "Negative results are results" in publication.profile_text("research", "actor")
    assert "independent check" in publication.profile_text("research", "critic")
    assert "demoted answer" in publication.profile_text("research", "critic")
    assert "best-powered estimate" in publication.profile_text("research", "actor")
    assert "Use it as a user would" in publication.profile_text("development", "actor")

    class Mem:
        def orient_prompt(self):
            return "memory"
    plain = build_actor_prompt(goal_text="# g", memory=Mem(), iteration=1)
    research = build_actor_prompt(goal_text="# g", memory=Mem(), iteration=1,
                                  profile=publication.profile_text("research", "actor"))
    assert "{profile}" not in plain and "profile: research" not in plain
    assert research.index("profile: research") < research.index("## The goal")


def scribe_prompt(c: Config, due: bool) -> str:
    return build_scribe_prompt(iteration=3, goal_text="# g", where="files", report="## What\n\nx", verdict="continue — ok",
                               fix_first="", diff_stat="", commit="abc", fold=None, view_dir="v",
                               story_path="v/story.html", assets_dir="v/assets", summary_path="v/summary.txt",
                               story=publication.story_rules(c), publish=publication.pass_rules(c) if due else None)


def test_the_notebook_scribe_keeps_the_run_book():
    text = scribe_prompt(cfg(), due=False)
    assert "**`v/story.html`: the notebook.**" in text and "Curate, do not append" in text
    assert 'section id="tried"' in text and "figures.json" not in text
    assert "No report is due" in text and not leftovers(text)


@pytest.mark.parametrize("profile,parts", [("research", publication.REQUIRED["paper"]),
                                           ("development", publication.REQUIRED["product"])])
def test_a_due_pass_carries_the_template_and_the_figure_contract(profile, parts):
    c = cfg(profile=profile, publication={"figures": {"main_max": 4}})
    live = scribe_prompt(c, due=False)
    assert "the notebook.**" in live and "v/figures.json" in live and "No report is due" in live
    assert "The report is due" not in live
    text = scribe_prompt(c, due=True)
    assert "The report is due" in text and "write `v/paper.html`" in text and "**4 main figures**" in text
    assert 'data-panels="A B C"' in text and "Each main figure has at least two panels." in text
    for part in parts:
        assert f"`{part}`" in text, part
    assert not leftovers(text), leftovers(text)


def test_figures_off_drops_the_figure_contract():
    text = scribe_prompt(cfg(profile="research", publication={"figures": {"enabled": False}}), due=True)
    assert "No numbered figures in this run" in text and "main figures**" not in text and not leftovers(text)


def test_the_final_prompt_is_complete():
    text = publication.final_prompt(cfg(profile="research"), reason="wall clock: 2h elapsed", where="files",
                                    goal_text="# Goal: x", problems="- Figure 1 is never cited",
                                    view_dir="v", story_path="v/story.html", assets_dir="v/assets",
                                    summary_path="v/summary.txt")
    assert "has stopped (wall clock: 2h elapsed)" in text and "Figure 1 is never cited" in text
    assert "The report is due" in text and "v/paper.html" in text and "# Goal: x" in text and not leftovers(text)


# -- the check ------------------------------------------------------------------

def figure(n: int, panels: str = "A B", caption: str | None = None, src: bool = True) -> str:
    divs = "".join(f'<div class="panel" data-panel="{p}"><img src="assets/f{n}{p}.svg" alt=""></div>' for p in panels.split())
    cap = caption if caption is not None else f"<b>Figure {n}. A claim.</b> <b>A</b>, one. <b>B</b>, two."
    return (f'<figure class="main" id="fig{n}" data-panels="{panels}"><div class="panels">{divs}</div>'
            f'<figcaption>{cap}</figcaption>{"<p class=src>fig.py</p>" if src else ""}</figure>')


def paper(*figs: str, cites: str = "Fig. 1A and Fig. 1B", order=publication.REQUIRED["paper"]) -> str:
    body = {s: f"<section id=\"{s}\"><h2>{s}</h2><p>text</p></section>" for s in order}
    body["results"] = f'<section id="results"><h2>Results</h2><p>As {cites} show.</p>{"".join(figs)}</section>'
    return "".join(body[s] for s in order)


@pytest.fixture
def view(tmp_path: Path) -> Path:
    (tmp_path / "assets").mkdir()
    for n in (1, 2, 3):
        for p in "ABCD":
            (tmp_path / "assets" / f"f{n}{p}.svg").write_text("<svg/>")
    (tmp_path / "figures.json").write_text(json.dumps([{"file": "assets/f1A.svg", "claim": "c", "source": "s",
                                                        "record": "r1", "iteration": 1}]))
    return tmp_path


def test_a_clean_paper_passes(view):
    assert publication.check(cfg(profile="research"), view, paper(figure(1))) == []
    assert publication.check(cfg(), view, "<p>anything at all</p>") == []   # a notebook has no contract


def test_the_check_names_what_is_wrong(view):
    c = cfg(profile="research", publication={"figures": {"main_max": 2}})
    problems = "\n".join(publication.check(c, view, paper(
        figure(1, "A"), figure(2, caption="A claim with no number."), figure(3, "A B C", src=False),
        cites="Fig. 1A, Fig. 2A–B and Fig. 3B", order=("answer", "results", "findings", "negative", "methods"))))
    assert "missing top-level sections: <section id='limitations'>" in problems
    assert "3 main figures; at most 2" in problems
    assert "Figure 1 (id 'fig1') has one panel" in problems
    assert 'Figure 2 (id \'fig2\'): the caption must begin "Figure 2."' in problems
    assert "Figure 3 (id 'fig3') has no <p class=\"src\">" in problems
    assert "Figure 3 (id 'fig3'): panel(s) A, C never cited" in problems
    assert "Figure 2 (id 'fig2'): panel" not in problems   # A–B is a range


def test_order_links_ledger_and_figures_off(view):
    c = cfg(profile="research")
    swapped = list(publication.REQUIRED["paper"])
    swapped[1], swapped[2] = swapped[2], swapped[1]
    assert any("out of order" in p for p in publication.check(c, view, paper(figure(1), order=swapped)))
    broken = paper(figure(1)).replace("assets/f1B.svg", "assets/gone.svg")
    assert any("assets/gone.svg" in p for p in publication.check(c, view, broken))
    assert any("never cited" in p for p in publication.check(c, view, paper(figure(1), cites="the plot")))
    (view / "figures.json").write_text("{not json")
    assert any("not a valid ledger" in p for p in publication.check(c, view, paper(figure(1))))
    (view / "figures.json").unlink()
    assert any("no figure ledger" in p for p in publication.check(c, view, paper(figure(1))))
    off = cfg(profile="research", publication={"figures": {"enabled": False}})
    assert publication.check(off, view, paper(cites="")) == []
    assert any("figures are off" in p for p in publication.check(off, view, paper(figure(1))))
    assert publication.check(c, view, "") == ["paper.html is empty: the report has not been written."]


def test_citations_read_ranges_lists_and_whole_figures():
    assert publication._cited("see Fig. 1A–C, Figure 2 and Fig. 3B, D; Fig 4 A shows") == {
        1: {"A", "B", "C"}, 2: set(), 3: {"B", "D"}, 4: {"A"}}
    assert publication._cited("Figure 1 The rest") == {1: set()}


# -- the engine -----------------------------------------------------------------

def make_engine(repo: Path, actor: FakeHarness, scribe: FakeHarness, config: Config, critic=None) -> Engine:
    git_ = GitGuard(repo, config.branch)
    git_.start()
    kw = {"critic": critic} if critic is not None else {}
    return Engine(config=config, repo=repo, harness=actor, memory=FilesMemory(repo, reconcile_every=0, pressure=10**6),
                  git=git_, recorder=Recorder(repo / ".ouroboros" / "runs" / "t", echo=lambda *_: None),
                  budget=BudgetClock(config.stop), sleeper=lambda s: None, scribe=Scribe(scribe, cwd=repo),
                  goal_text=(repo / ".ouroboros" / "goal.md").read_text(), **kw)


def publishes(cwd: Path, prompt: str) -> Result:
    """A scribe that records, and writes a clean paper whenever a pass is due or the run has stopped."""
    if "has stopped" not in prompt:
        scribes()(cwd, prompt)
    if "The report is due" in prompt:
        vd = cwd / re.search(r"`([^`]*)/story\.html`", prompt).group(1)
        (vd / "assets").mkdir(parents=True, exist_ok=True)
        for p in "AB":
            (vd / "assets" / f"f1{p}.svg").write_text("<svg/>")
        (vd / "figures.json").write_text("[]")
        (vd / "paper.html").write_text(paper(figure(1)))
    return Result(text="ok", cost_usd=0.1)


def test_a_research_run_drafts_on_schedule_and_writes_the_report_at_the_stop(repo: Path):
    config = cfg(profile="research", stop={"max_iterations": 4}, publication={"consolidate_every": 2})
    actor, scribe = FakeHarness([], default=works()), FakeHarness([], default=publishes)
    eng = make_engine(repo, actor, scribe, config)
    reason = eng.run()
    assert "max_iterations" in reason
    assert all("profile: research" in p for p in actor.prompts)
    due = ["The report is due" in p for p in scribe.prompts]
    assert due == [False, True, False, True, True]          # every 2nd worked iteration, then the final pass
    assert "has stopped (max_iterations 4 reached)" in scribe.prompts[-1]
    steps = eng.recorder.read_jsonl(eng.recorder.iterations)
    assert [s.get("problems") for s in steps if s.get("step") == "publish"] == [0, 0, 0]
    assert any(s.get("step") == "publish_final" for s in steps)


def test_publication_problems_reach_the_next_scribe_until_fixed(repo: Path):
    config = cfg(profile="research", stop={"max_iterations": 3}, publication={"consolidate_every": 1, "final": False})
    calls = {"n": 0}

    def sloppy_then_clean(cwd: Path, prompt: str) -> Result:
        calls["n"] += 1
        res = publishes(cwd, prompt)
        if calls["n"] == 1:   # the first pass forgets to cite its figure
            vd = cwd / re.search(r"`([^`]*)/story\.html`", prompt).group(1)
            (vd / "paper.html").write_text(paper(figure(1), cites="the plot"))
        return res
    scribe = FakeHarness([], default=sloppy_then_clean)
    eng = make_engine(repo, FakeHarness([], default=works()), scribe, config)
    eng.run()
    assert "never cited" not in scribe.prompts[0]
    assert "The publication check found:" in scribe.prompts[1] and "never cited" in scribe.prompts[1]
    assert "The publication check found:" not in scribe.prompts[2]
    assert eng.publication_problem is None
    assert len(scribe.prompts) == 3                              # final: false


def test_a_freeform_run_never_consolidates(repo: Path):
    config = cfg(stop={"max_iterations": 2})
    actor, scribe = FakeHarness([], default=works()), FakeHarness([], default=publishes)
    eng = make_engine(repo, actor, scribe, config)
    eng.run()
    assert len(scribe.prompts) == 2 and not any("The report is due" in p for p in scribe.prompts)
    assert not any("profile:" in p for p in actor.prompts)
    assert not any(s.get("step", "").startswith("publish") for s in eng.recorder.read_jsonl(eng.recorder.iterations))


def test_a_broken_check_or_final_pass_never_stops_the_loop(repo: Path, monkeypatch):
    config = cfg(profile="research", stop={"max_iterations": 2}, publication={"consolidate_every": 1})

    def boom(*a, **k):
        raise RuntimeError("check exploded")
    monkeypatch.setattr(publication, "check", boom)
    monkeypatch.setattr(publication, "final_prompt", boom)
    eng = make_engine(repo, FakeHarness([], default=works()), FakeHarness([], default=publishes), config)
    assert "max_iterations" in eng.run()
    assert eng.iteration == 2 and all(o.recorded for o in eng.outcomes)
    log = (eng.recorder.run_dir / "loop.log").read_text()
    assert "publication check raised" in log and "final publication raised" in log
