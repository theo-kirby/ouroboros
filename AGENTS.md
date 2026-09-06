# Working on Ouroboros

The contract for any agent (or human) changing this repo.

## What this is

A Python 3.12 `uv` tool that loops agent harnesses overnight. `DESIGN.md` is
the design and is kept true to the code: when you change behaviour, change the
matching section in the same commit. Section 20 holds the charter/plan
decisions; section 21 holds what the first real night taught. Do not contradict
either without recording why there.

## Rules

- **Tests first, tests green.** `uv run pytest -q` before every commit. Engine
  behaviour is tested against `tests/fake_harness.py`; drivers against recorded
  event streams in `tests/test_drivers_parse.py`. A change to a driver's
  parsing needs a recorded sample, not a guess.
- **The loop never blocks and never stops early.** Any new code path in
  `engine.py` must survive an exception (log, back off, continue). Never add a
  prompt, an input(), or a bare `raise` on the hot path.
- **Role prompts are skills**, under `src/ouroboros/skills/*/SKILL.md`, inlined
  into every call by the roles and memory modules. Edit the skill, not a
  Python string. Placeholders are `{name}`.
- **Single writer per graph.** The maintainer writes the state graph; the
  planner writes the `plan` view; actors write neither. Keep the forbidden-verb
  lists in the prompts in step with that.
- **The charter is read-only to agents.** Nothing in this repo may write
  `.ouroboros/goal.md` of a target repo.
- **No empty commits, no history rewrites** in target repos. Reverts are
  revert commits.
- **Commit messages** end with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`
  when Claude wrote the change. PR bodies end with
  `🤖 Generated with [Claude Code](https://claude.com/claude-code)`.
- **Cost is API-equivalent.** Never present `cost_usd` as a bill.

## Layout

See `DESIGN.md` section 17. Short form: `cli.py` (commands, signals),
`engine.py` (the state machine), `config.py`, `goal.py` (charter parser),
`harness/` (drivers, pool, backends), `memory/` (hypergraph, handoff),
`roles/` (prompt assembly and JSON parsing), `gitguard.py`, `budget.py`,
`recorder.py`, `tmux.py`, `skills/`.

## Smoke tests that cost real usage

`tests/test_memory_hypergraph.py` needs the `hypergraph` CLI and runs offline.
Anything that calls a real harness (`claude`, `codex`, `pi`) is a manual smoke
test, not a pytest; do it on a scratch repo under the scratchpad directory and
say so in the commit message.
