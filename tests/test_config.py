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
    assert not back.maintainer and not back.planner


def test_old_config_keys_are_ignored_not_fatal(tmp_path):
    """A config from before the merge names roles that no longer exist; it still loads."""
    p = tmp_path / "c.yml"
    p.write_text("run: x\nmode: actor-critic\noverseer: agent\ncouncil: []\nroles:\n  overseer: {model: haiku}\nplan:\n  enabled: true\n")
    cfg = Config.load(p)
    assert cfg.active_roles == ["actor", "critic"] and cfg.plan.every == 5
    cfg.maintainer = cfg.planner = True
    assert cfg.active_roles == ["actor", "critic", "maintainer", "planner"]
