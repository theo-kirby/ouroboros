import pytest

from ouroboros.config import Config, parse_duration


@pytest.mark.parametrize("v,s", [("10h", 36000), ("45m", 2700), ("1h30m", 5400), ("90s", 90), ("90", 90), (60, 60), (None, None)])
def test_parse_duration(v, s):
    assert parse_duration(v) == s


def test_bad_duration():
    with pytest.raises(ValueError):
        parse_duration("soon")


def _written(tmp_path, text):
    p = tmp_path / "w.yml"
    p.write_text(text)
    return p


def test_config_roundtrip(tmp_path):
    cfg = Config(run="x", loop="standard")
    p = tmp_path / "c.yml"
    p.write_text(cfg.dump())
    back = Config.load(p)
    assert back.branch == "ouroboros/x"
    assert back.role("actor").timeout_seconds == 2700
    assert back.role("critic").timeout_seconds == 600 and back.critic == "agent"
    assert back.active_roles == ["actor", "critic", "scribe"]
    assert back.role("actor").chain == [("claude", None), ("codex", None)]   # codex carries the actor while claude is limited
    ww = Config.load(_written(tmp_path, Config(run="y").dump()))           # the default loop round-trips too
    assert ww.worker_writer and ww.active_roles == ["worker", "writer"]
    assert ww.role("writer").chain == [("claude", "claude-opus-5-5"), ("codex", None)]


def test_old_config_keys_are_ignored_not_fatal(tmp_path):
    """A config from before the merge names roles that no longer exist; it still loads."""
    p = tmp_path / "c.yml"
    p.write_text("run: x\nmode: actor-critic\noverseer: agent\ncouncil: []\nroles:\n  overseer: {model: haiku}\n"
                 "  maintainer: {timeout: 20m}\n  planner: {timeout: 20m}\nmaintainer: true\nplanner: true\n"
                 "plan:\n  every: 5\nloop:\n  kind: standard\n  repeat_bet_after: 3\n")
    cfg = Config.load(p)
    assert cfg.active_roles == ["actor", "critic", "scribe"]
    assert not hasattr(cfg, "planner") and not hasattr(cfg, "plan")


def test_graph_cadence_is_read_from_the_graph_key(tmp_path):
    p = tmp_path / "c.yml"
    p.write_text("run: x\nmemory: governor\ngraph:\n  reconcile_every: 7\n  pressure: 2\n")
    cfg = Config.load(p)
    assert cfg.graph.reconcile_every == 7 and cfg.graph.pressure == 2 and cfg.graph.budget_units == 1


def test_a_rules_role_calls_no_model():
    assert Config(loop="standard", scribe="rules").active_roles == ["actor", "critic"]
    assert Config(loop="standard", critic="rules", scribe="rules").active_roles == ["actor"]
    assert [h for h, _ in Config().role("scribe").chain] == ["claude", "codex"]


# -- the worker-writer loop -------------------------------------------------------

def test_the_worker_writer_loop_is_the_default_on_claude_opus():
    cfg = Config()
    assert cfg.loop.kind == "worker-writer" and cfg.handoff == "graph" and cfg.worker_writer
    assert cfg.active_roles == ["worker", "writer"] and cfg.primary_role == "worker"
    for role in ("worker", "writer"):
        assert cfg.role(role).chain[0] == ("claude", "claude-opus-5-5")
    std = Config(loop="standard")
    assert not std.worker_writer and std.active_roles == ["actor", "critic", "scribe"] and std.primary_role == "actor"


def test_loop_worker_writer_by_name_with_its_roles(tmp_path):
    p = tmp_path / "c.yml"
    p.write_text("loop: worker-writer\nhandoff: graph\nroles:\n  worker: {harness: codex, timeout: 30m}\n"
                 "  writer: {harness: claude, model: opus, fallback: [{harness: codex}]}\n")
    cfg = Config.load(p)
    assert cfg.worker_writer and cfg.handoff == "graph" and cfg.primary_role == "worker"
    assert cfg.active_roles == ["worker", "writer"]
    assert cfg.role("worker").chain == [("codex", None)] and cfg.role("worker").timeout_seconds == 1800
    assert cfg.role("writer").chain == [("claude", "opus"), ("codex", None)]
    assert cfg.loop.product_after == 8, "the detector keeps its defaults beside the kind"
    back = tmp_path / "back.yml"
    back.write_text(cfg.dump())
    assert Config.load(back).worker_writer, "run.yml round-trips the loop"


def test_worker_writer_roles_have_defaults_when_the_config_names_none(tmp_path):
    p = tmp_path / "c.yml"
    p.write_text("loop: {kind: worker-writer, product_after: 4}\n")
    cfg = Config.load(p)
    assert cfg.loop.product_after == 4
    assert cfg.role("worker").chain == [("claude", "claude-opus-5-5"), ("codex", None)] and cfg.role("worker").timeout_seconds == 2700
    assert cfg.role("writer").timeout_seconds == 1200 and cfg.role("writer").chain[-1] == ("codex", None)


@pytest.mark.parametrize("text,words", [
    ("loop: actor-critic\n", "is not a loop"),
    ("loop: {kind: keeper}\n", "loop.kind"),
    ("loop: worker-writer\nhandoff: brief\n", "handoff"),
    ("loop: standard\nhandoff: both\n", "applies only to loop: worker-writer"),
    ("loop: worker-writer\ncritic: rules\n", "critic: rules does not apply"),
    ("loop: worker-writer\nscribe: rules\n", "scribe: rules does not apply"),
    ("loop: worker-writer\nroles:\n  worker: {resume_for: 3}\n", "start fresh every turn"),
])
def test_a_loop_that_cannot_run_is_refused_in_one_line(tmp_path, text, words):
    from ouroboros.config import ConfigError
    p = tmp_path / "c.yml"
    p.write_text(text)
    with pytest.raises(ConfigError) as exc:
        Config.load(p)
    assert words in str(exc.value) and str(p) in str(exc.value) and "\n" not in str(exc.value)


def test_the_cli_says_why_a_config_is_refused(repo, monkeypatch, capsys):
    from ouroboros import cli
    (repo / ".ouroboros" / "config.yml").write_text("loop: standard\nhandoff: prompt\n")
    monkeypatch.chdir(repo)
    assert cli.main(["preflight"]) == 2
    assert "applies only to loop: worker-writer" in capsys.readouterr().err


def test_harness_and_model_flags_set_the_worker_in_worker_writer(repo, monkeypatch):
    import argparse
    from ouroboros import cli
    (repo / ".ouroboros" / "config.yml").write_text("loop: worker-writer\n")
    cfg = cli.load_config(argparse.Namespace(harness="pi", model="m"), repo)
    assert cfg.role("worker").chain[0] == ("pi", "m") and cfg.role("actor").harness == "claude"


def test_a_run_keeps_the_loop_it_started_with(tmp_path):
    """worker-writer became the default; a standard run continued from an old config must not switch."""
    from ouroboros import cli
    run_dir = cli.run_dir_for(tmp_path, "old")
    run_dir.mkdir(parents=True)
    (run_dir / "run.yml").write_text("run: old\nloop:\n  product_after: 8\nstarted: 2026-10-30T11:47:05\n")
    problem = cli.loop_switch_problem(Config(run="old"), tmp_path)
    assert "started as loop: standard" in problem and "loop: worker-writer" in problem and "\n" not in problem
    assert cli.loop_switch_problem(Config(run="old", loop="standard"), tmp_path) is None
    assert cli.loop_switch_problem(Config(run="new"), tmp_path) is None   # a new run is free to choose
