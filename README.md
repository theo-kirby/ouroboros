# Ouroboros

A loop runner for agent harnesses: Claude Code, Codex, and Pi.

You write a **charter** (a goal with a horizon ladder). Ouroboros runs an agent
on it again and again, overnight, in tmux. It never blocks on a question, never
believes "done" too early, never stops for a rate limit or a usage limit, and
leaves a memory trail plus a plan the agents wrote for the morning.

[DESIGN.md](DESIGN.md) is the full design and is kept true to the code.
Section 20 (charter and plan) and section 21 (what the first night taught) are
the parts to read first.

## Install

```bash
git clone <this repo> ~/src/ouroboros
uv tool install --editable ~/src/ouroboros      # the `ouroboros` command; edits are live
ouroboros skills install --user                 # the skills, into every harness you have: claude, codex, pi
```

You need one harness installed and logged in: `claude`, `codex`, or `pi`.
`ouroboros run` checks the logins before it starts and says what is missing.

Optional but recommended: [hypergraph-protocol](https://github.com/theo-kirby/hypergraph-protocol)
(`uv tool install hypergraph-protocol`). A repo with `.hypergraph/config.yml`
gets graph memory, the frontier, and the self-evolving plan; any other repo
gets handoff files with the same plan layer.

## Run a night

```bash
cd your-repo
ouroboros                      # the menu: init / design / run / monitor
ouroboros init                 # .ouroboros/config.yml + goal.md
ouroboros design               # interview → charter + config, in claude, codex, or pi
ouroboros run                  # refuses a dirty tree, a template charter, or no login; then tmux session ouroboros-<run>
ouroboros status --watch       # the live TUI: stages, iterations, load, messages, overseer, plan (or `ouroboros top`)
ouroboros stop                 # kills the loop and its children
ouroboros report               # REPORT.md; or /ouroboros-morning in Claude Code
```

Merge the run branch with a merge commit. Record nodes cite commit SHAs, so a
squash or rebase dangles them.

## What runs each iteration

```
actor (orient → one unit of work → record) → commit → critic? → overseer → reconcile? → plan?
```

| Role | Job |
|---|---|
| actor | one bounded unit from the plan's `short` horizon, then a record node or handoff |
| critic | `actor-critic` / `council` modes: grades the diff; a reject reverts the iteration |
| overseer | the sleeping user: answers questions, rejects false "done", unsticks; judges against the frontier |
| maintainer | hypergraph reconcile pass: folds record impacts into the state graph |
| planner | after each reconcile: one `Bet:` record folded into the `plan` view (`short` / `medium` / `long`) |

Every role can carry a fallback chain. On a usage limit the loop switches
harness in the same call and comes back when the limit resets.

```yaml
roles:
  actor:   { harness: claude, timeout: 90m, fallback: [{ harness: codex }] }
  critic:  { harness: codex,  timeout: 15m }
mode: actor-critic
stop: { after: 15h }
```

## The charter

`.ouroboros/goal.md` is the one document the human owns. No agent role edits
it. Its done criteria become open gaps on the frontier; its horizon ladder
(short, medium, long: granularity, not time) seeds the plan; the agents re-plan from there and record their bets. You
overrule them by editing the charter and running again.

## Cost

Claude Code reports dollars computed from tokens at API list prices even on a
subscription. Ouroboros sums that and calls it API-equivalent cost. It is a
measure of work, not a bill. Time and iterations are the real caps.

## Development

```bash
uv run pytest -q     # 104 tests; hypergraph and tmux tests skip when the tools are missing
```

Role prompts are skills under `src/ouroboros/skills/`. Edit them there.
