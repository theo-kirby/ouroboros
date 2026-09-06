from ouroboros import goal

CHARTER = """# Goal: x

## Mission

Ship it.

## Done criteria

File lifecycle:

- [ ] Opening a `.blend` beside its `.cadex` hydrates the model (`load_post`
      queues a rebuild; a test asserts `model_objects_on_open > 0`).
- [x] Save-As carries `.cxpolicy` forward.

Loop:

- [ ] **Iterate works.** Change a part, retrain, compare.

## Horizon ladder

What to do if this runs for:

- **short-term:** orient. Take file lifecycle first,
  with a test.
- **medium-term:** close gaps.
- **long-term:** keep every gate green.

## Exhaustion policy

maintain
"""


def test_done_criteria_join_continuations():
    crits = goal.done_criteria(CHARTER)
    assert len(crits) == 2                                   # the ticked Save-As box declares no gap
    assert crits[0].startswith("Opening a `.blend`") and "model_objects_on_open > 0" in crits[0]
    assert crits[1] == "**Iterate works.** Change a part, retrain, compare."
    assert len(goal.done_criteria(CHARTER, include_checked=True)) == 3


def test_gap_names_are_stable_kebab():
    assert goal.gap_name("Opening a `.blend` beside its `.cadex` hydrates the model") == "gap-opening-blend-beside-cadex-hydrates"
    assert goal.gap_name("**Iterate works.** Change a part") == "gap-iterate-works-change-part"
    assert goal.gap_name("") == "gap"


def test_horizon_ladder():
    ladder = goal.horizon_ladder(CHARTER)
    assert ladder["short"] == "orient. Take file lifecycle first, with a test."
    assert ladder["long"].startswith("keep every gate")
    assert list(ladder) == list(goal.RUNGS)


def test_horizon_ladder_folds_legacy_time_rungs():
    old = (
        "## Horizon ladder\n\n- **the next hour:** orient.\n- **the next day:** the lockout box.\n"
        "- **the next week:** close gaps.\n- **the next month:** second mechanism.\n- **the next year:** keep it green.\n"
    )
    ladder = goal.horizon_ladder(old)
    assert ladder == {"short": "orient. the lockout box.", "medium": "close gaps.", "long": "second mechanism. keep it green."}
    assert goal.horizon_ladder("## Horizon ladder\n\n- **Short term:** a\n- **longer-term**: c\n") == {"short": "a", "long": "c"}


def test_sections_and_policy():
    assert goal.mission(CHARTER) == "Ship it."
    assert goal.exhaustion_policy(CHARTER) == "maintain"
    assert goal.exhaustion_policy("# g\n") == "creative"
