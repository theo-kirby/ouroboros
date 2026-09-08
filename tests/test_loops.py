"""The loop detector: motion that is not progress.

Every threshold case here is taken from the nt3 run, because that is the run the
module was written against: 201 iterations, 22,437 lines, one node moved.
"""

import pytest

from ouroboros.config import LoopConfig
from ouroboros.loops import (LoopDetector, content_words, is_product_change, loop_reply,
                             normalise, similarity)
from ouroboros.roles.overseer import RulesOverseer, Signals


# -- what counts as work ----------------------------------------------------

def test_a_diff_confined_to_the_memory_graph_is_not_product():
    assert not is_product_change([
        ".hypergraph/graph/record/witty-brook-9419.md",
        ".hypergraph/cache/state.json",
        "STATE.md",
        "PLAN.md",
        ".ouroboros/runs/nt3/status.json",
    ])


@pytest.mark.parametrize("path", [
    "cli/cadex_cli/clearance.py",
    "docs/CLI.md",                 # a documentation fix is real work, not bookkeeping
    "src/Mod/cadex/test_x.py",
    "./pixi.toml",
])
def test_anything_outside_the_bookkeeping_dirs_is_product(path):
    assert is_product_change([".hypergraph/graph/record/a.md", path])


def test_no_files_at_all_is_not_product():
    assert not is_product_change([])


# -- telling one bet from the same bet --------------------------------------

def test_numbers_collapse_so_a_counter_cannot_disguise_a_repeat():
    # The nt3 planner wrote this bet twenty times. Only the count moved.
    a = "Bet: retain the conditional rehearsal after fifteen unchanged iterations"
    b = "Bet: retain the conditional rehearsal after twenty-four unchanged iterations"
    assert normalise(a) == normalise(b)
    assert similarity(a, b) == 1.0


def test_digits_and_number_words_collapse_the_same_way():
    assert normalise("after 24 iterations") == normalise("after twenty four iterations")


def test_genuinely_different_bets_stay_far_apart():
    a = "Bet: close the walk criterion, then rerun the headless review"
    b = "Bet: bound the payload identity guarantee to its actual check"
    assert similarity(a, b) < 0.2


def test_the_slug_prefix_a_bet_arrives_with_is_not_part_of_the_bet():
    """Every bet carries a fresh slug. Left in, it drags every comparison down."""
    a = "long-lily-6043 \u2014 Bet: retain the conditional rehearsal"
    b = "snowy-grove-2880 \u2014 Bet: retain the conditional rehearsal"
    assert content_words(a) == content_words(b) == {"retain", "conditional", "rehearsal"}
    assert similarity(a, b) == 1.0


def test_a_loose_paraphrase_scores_between_healthy_and_identical():
    """Real nt3 bets, five iterations apart, and the honest limit of a lexical metric.

    These two say the same thing in different words. Content overlap puts them at
    0.43 -- above the 0.33 that healthy bets in the same run reached, but below
    the 0.5 threshold, so this pair alone does not fire. That is deliberate: the
    margin between 0.33 and 0.43 is too thin to spend a false positive on, and
    `no_product` had already fired 28 iterations earlier. `repeat_bet` is the
    backstop for a planner that repeats itself outright, not a paraphrase
    detector.
    """
    a = "long-lily-6043 \u2014 Bet: preserve the rehearsal hold after the eligibility observation"
    b = "snowy-grove-2880 \u2014 Bet: hold the rehearsal pending new eligibility evidence"
    assert 0.33 < similarity(a, b) < 0.5


def test_two_healthy_bets_from_the_same_run_stay_below_the_threshold():
    a = "amber-fjord-1560 \u2014 Bet: repair worker snapshot integrity before the next lifecycle rehearsal"
    b = "brave-pebble-4147 \u2014 Bet: fold qualified worker repair and promote bounded review discovery"
    assert similarity(a, b) < 0.5


def test_similarity_of_nothing_is_zero():
    assert similarity("", "anything") == 0.0


# -- the three signals ------------------------------------------------------

def detector(**kw):
    return LoopDetector(LoopConfig(**kw))


def test_bookkeeping_only_iterations_accumulate_and_a_real_change_clears_them():
    d = detector(product_after=3)
    for _ in range(3):
        d.observe_iteration(files=[".hypergraph/graph/record/a.md"], frontier=None, subject="Record x")
    report = d.report()
    assert report is not None and report.signal == "no_product" and report.streak == 3
    d.observe_iteration(files=["cli/main.py"], frontier=None, subject="fix")
    assert d.report() is None and d.no_product == 0


def test_the_streak_carries_the_subjects_as_evidence():
    d = detector(product_after=2)
    d.observe_iteration(files=[".hypergraph/x.md"], frontier=None, subject="Record actor dispatch conflict")
    d.observe_iteration(files=[".hypergraph/y.md"], frontier=None, subject="Record blocked role transition")
    report = d.report()
    assert "Record actor dispatch conflict" in report.describe()
    assert "Record blocked role transition" in report.describe()


def test_an_unmoved_frontier_fires_even_while_files_keep_changing():
    d = detector(product_after=None, frontier_after=3)
    for _ in range(4):
        d.observe_iteration(files=["cli/main.py"], frontier="same-digest", subject="work")
    report = d.report()
    assert report is not None and report.signal == "no_frontier"
    # The first observation only establishes the baseline; three repeats follow.
    assert report.streak == 3
    d.observe_iteration(files=["cli/main.py"], frontier="moved", subject="work")
    assert d.report() is None


def test_a_memory_without_a_frontier_disables_the_signal_rather_than_firing_it():
    d = detector(product_after=None, frontier_after=2)
    for _ in range(6):
        d.observe_iteration(files=["cli/main.py"], frontier=None, subject="work")
    assert d.no_frontier == 0 and d.report() is None


def test_a_planner_restating_itself_fires_and_a_fresh_bet_clears_it():
    d = detector(repeat_bet_after=3)
    d.observe_bet("Bet: retain the conditional rehearsal after fifteen unchanged iterations")
    for n in ("twenty", "twenty-four", "thirty-four"):
        d.observe_bet(f"Bet: retain the conditional rehearsal after {n} unchanged iterations")
    report = d.report()
    assert report is not None and report.signal == "repeat_bet" and report.streak == 3
    d.observe_bet("Bet: qualify the linked-part restore with its source absent")
    assert d.repeat_bet == 0


def test_an_empty_bet_is_not_a_repeat():
    d = detector(repeat_bet_after=1)
    d.observe_bet("Bet: something")
    assert d.observe_bet(None) == 0.0
    assert d.repeat_bet == 0


def test_a_null_threshold_switches_one_signal_off():
    d = detector(product_after=None)
    for _ in range(50):
        d.observe_iteration(files=[".hypergraph/a.md"], frontier=None, subject="Record")
    assert d.no_product == 50 and d.report() is None


def test_the_signal_furthest_past_its_threshold_is_the_one_reported():
    d = detector(product_after=2, frontier_after=2)
    for _ in range(9):
        d.observe_iteration(files=[".hypergraph/a.md"], frontier="fixed", subject="Record")
    # no_product is 9 (over by 7); no_frontier is 8 (over by 6, one iteration is the baseline)
    assert d.report().signal == "no_product"


# -- escalation -------------------------------------------------------------

def test_escalation_climbs_with_the_streak_and_stops_at_three():
    d = detector(product_after=4, escalate_every=5)
    steps = []
    for i in range(1, 21):
        d.observe_iteration(files=[".hypergraph/a.md"], frontier=None, subject="Record")
        report = d.report()
        steps.append(d.escalation(report) if report else 0)
    assert steps[3] == 1        # the iteration the threshold is crossed
    assert steps[8] == 2        # five past it
    assert steps[13] == 3       # ten past it
    assert max(steps) == 3, "nothing above 3: the ladder never stops a run"


def test_the_reply_names_the_loop_and_grows_with_the_step():
    d = detector(product_after=1)
    d.observe_iteration(files=[".hypergraph/a.md"], frontier=None, subject="Record the handoff")
    report = d.report()
    first = loop_reply(report, 1)
    assert "Record the handoff" in first
    assert "banned" not in first and "different model" not in first
    assert "banned" in loop_reply(report, 2)
    assert "different model" in loop_reply(report, 3)


# -- what the overseer is told ----------------------------------------------

def test_the_rules_overseer_returns_looping_when_a_signal_has_fired():
    s = Signals("all good", changed=True, recorded=True, no_change_streak=0, error_streak=0,
                loop_fired="8 iterations in a row changed only bookkeeping")
    v = RulesOverseer().judge(s)
    assert v.verdict == "looping"
    assert "not the work" in v.reply


def test_nothing_changing_still_reads_as_stuck_not_looping():
    """`stuck` is no diff at all; `looping` is diffs that move nothing. Keep them apart."""
    s = Signals("", changed=False, recorded=True, no_change_streak=5, error_streak=0,
                loop_fired="30 iterations in a row left the frontier unchanged")
    assert RulesOverseer().judge(s).verdict == "stuck"


def test_the_counters_reach_the_overseers_signal_block():
    d = detector(product_after=2)
    for _ in range(3):
        d.observe_iteration(files=[".hypergraph/a.md"], frontier=None, subject="Record")
    s = Signals("x", changed=True, recorded=True, no_change_streak=0, error_streak=0,
                loop=d.describe(), loop_fired=d.report().describe())
    described = s.describe()
    assert "changing only bookkeeping: 3" in described
    assert "LOOP DETECTED" in described


def test_a_long_streak_acts_periodically_rather_than_every_iteration():
    """nt3's frontier did not move for 157 iterations. An ungated ladder re-plans 157 times."""
    d = detector(product_after=4, escalate_every=5)
    acted = []
    for i in range(1, 41):
        d.observe_iteration(files=[".hypergraph/a.md"], frontier=None, subject="Record")
        report = d.report()
        if report and d.should_act(report):
            acted.append((i, d.escalation(report)))
    assert acted == [(4, 1), (9, 2), (14, 3), (19, 3), (24, 3), (29, 3), (34, 3), (39, 3)]


def test_the_gate_reopens_once_the_streak_is_broken():
    d = detector(product_after=2, escalate_every=5)
    for _ in range(2):
        d.observe_iteration(files=[".hypergraph/a.md"], frontier=None, subject="Record")
    assert d.should_act(d.report()) and not d.should_act(d.report())
    d.observe_iteration(files=["src/real.py"], frontier=None, subject="work")
    for _ in range(2):
        d.observe_iteration(files=[".hypergraph/a.md"], frontier=None, subject="Record")
    assert d.should_act(d.report()), "a fresh loop is a fresh escalation"
