import pytest

from ouroboros.config import Config, parse_duration


@pytest.mark.parametrize("v,s", [("10h", 36000), ("45m", 2700), ("1h30m", 5400), ("90s", 90), ("90", 90), (60, 60), (None, None)])
def test_parse_duration(v, s):
    assert parse_duration(v) == s


def test_bad_duration():
    with pytest.raises(ValueError):
        parse_duration("soon")


def test_config_roundtrip(tmp_path):
    cfg = Config(run="x")
    p = tmp_path / "c.yml"
    p.write_text(cfg.dump())
    back = Config.load(p)
    assert back.branch == "ouroboros/x"
    assert back.role("actor").timeout_seconds == 2700
    assert back.role("critic").timeout_seconds == 600 and back.critic == "agent"
    assert back.active_roles == ["actor", "critic"]
    assert back.role("actor").chain == [("claude", None), ("codex", None)]   # codex carries the actor while claude is limited


def test_old_config_keys_are_ignored_not_fatal(tmp_path):
    """A config from before the merge names roles that no longer exist; it still loads."""
    p = tmp_path / "c.yml"
    p.write_text("run: x\nmode: actor-critic\noverseer: agent\ncouncil: []\nroles:\n  overseer: {model: haiku}\n"
                 "  maintainer: {timeout: 20m}\n  planner: {timeout: 20m}\nmaintainer: true\nplanner: true\n"
                 "plan:\n  every: 5\nloop:\n  repeat_bet_after: 3\n")
    cfg = Config.load(p)
    assert cfg.active_roles == ["actor", "critic"]
    assert not hasattr(cfg, "planner") and not hasattr(cfg, "plan")


def test_graph_cadence_is_read_from_the_graph_key(tmp_path):
    p = tmp_path / "c.yml"
    p.write_text("run: x\nmemory: governor\ngraph:\n  reconcile_every: 7\n  pressure: 2\n")
    cfg = Config.load(p)
    assert cfg.graph.reconcile_every == 7 and cfg.graph.pressure == 2 and cfg.graph.budget_units == 1
