<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/logo-dark.svg">
    <img src="docs/logo.svg" alt="Ouroboros logo: a serpent that eats its tail" width="120">
  </picture>
</p>

<h1 align="center">Ouroboros</h1>

<p align="center"><b>Autonomous framework for long-running agents</b></p>

<p align="center">
  <a href="https://borris.dev"><img alt="Site: borris.dev" src="https://img.shields.io/badge/site-borris.dev-08519c"></a>
  <img alt="Python 3.12+" src="https://img.shields.io/badge/python-3.12%2B-3776AB?logo=python&logoColor=white">
  <a href="https://docs.astral.sh/uv/"><img alt="Built with uv" src="https://img.shields.io/badge/built%20with-uv-2ca02c"></a>
  <img alt="Harnesses: Claude Code, Codex, Pi" src="https://img.shields.io/badge/harnesses-Claude%20Code%20%C2%B7%20Codex%20%C2%B7%20Pi-4292c6">
  <img alt="Tests: pytest" src="https://img.shields.io/badge/tests-pytest-0A9EDC?logo=pytest&logoColor=white">
  <a href="LICENSE"><img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-green"></a>
</p>

---

Agents are good at staying busy. Leave one alone overnight and it drifts off
the goal, calls things done that aren't, and hands you 200 commits with no
story behind them. Ouroboros is an attempt to make long runs work. A charter
guides Claude Code, Codex or Pi through a loop that runs for hours: one agent
makes a change, another checks it against the charter and reverts it if it
doesn't hold up, and a third writes down what happened. In the morning you get
a branch you can read and a report on what's worth merging.

<a href="https://borris.dev">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/site-dark.png">
    <img src="docs/site-light.png" alt="The borris.dev page in the style of an Ouroboros run report: the loop as a dial, the key figures, and gauges for time, usage and progress toward done">
  </picture>
</a>

## The five rules

These rules win over every other rule in a run.

1. **Follow the charter.** The critic grades every change against your goal,
   your constraints and your quality bar. A change that fails is reverted. No
   agent can edit the charter.
2. **Prove every claim.** The critic refuses a claim that the diff and the tests
   do not support, and a claim of "done" most of all.
3. **Report as it goes.** Every iteration writes a record of what changed and
   why. The run page stays current, a digest goes to your phone, and
   `ouroboros report` gives the full account.
4. **Do work, not motion.** One bounded unit per iteration, done to the end. No
   change, no commit. Two counters catch a loop that is busy but ships nothing.
5. **Never block, never stop early.** The critic answers the actor's questions
   from your question policy. A usage limit switches to a fallback harness.
   Only a stop condition that you set ends a run.

| The problem of an unattended agent | What Ouroboros does |
|---|---|
| It drifts from the goal. | The critic picks each next step from the charter and reverts a change that goes outside it. |
| It says "done" too early. | The critic refuses a claim of completion that the diff and the tests do not support. |
| Nobody can tell what it did. | Each step is a record with its reason, and each claim in state cites a record. |
| It asks a question, or hits a limit, and stops. | The critic answers as you would, and the role falls back to another harness. |
| It keeps busy but ships nothing. | Watched, not solved: two counters name the loop and change the model. |

To run for a long time is the easy part. To run well for a long time is the
problem.

## Install

You need Python 3.12+, [`uv`](https://docs.astral.sh/uv/), and at least one
agent CLI that is logged in: `claude`, `codex` or `pi`. To outlive the terminal,
a run needs one of tmux, tern, or a systemd user session (`launcher:` in the
config).

```bash
git clone https://github.com/theo-kirby/ouroboros.git
cd ouroboros
uv tool install --editable .
ouroboros skills install --user
```

## Run

In the repo you want worked on:

```bash
ouroboros init            # config and charter template
ouroboros design          # write the charter with an agent
ouroboros run --for 8h    # start the loop
ouroboros top             # watch it
ouroboros stop            # stop early if you must
ouroboros report          # read what happened
ouroboros watch           # alerts and a digest to your phone (run starts it for you)
```

Start with a clean working tree. Read the report and the run branch before you
merge. Run `ouroboros` with no arguments for an interactive menu.

## Three roles, and no others

| Role | What it does | Memory verbs |
|---|---|---|
| **Actor** | Picks one unit of work from the critic's message. Does it to the end, tests it, and reports it. | `orient` `show` |
| **Critic** | One read-only call after every actor turn. Grades the change against your quality bar, answers questions as you would, refuses false "done", names loops, and writes the next prompt. A rejected change is reverted. | `orient` `show` `check` |
| **Scribe** | Records the unit in memory (a rejected one as a dead end), folds the memory when a fold is due, and keeps the run's page up to date. | `record` `fold` `render` |

The actor reads, the critic reads and checks, the scribe writes. No agent can
edit the charter.

## The charter

The charter is the contract for the run. It lives in `.ouroboros/goal.md`.
`ouroboros design` writes it with you, in an interview. The critic picks each
next step from it, and grades each change against it. While you are away, the
charter speaks for you.

```markdown
# Goal: <name>
## Mission            one paragraph: what and why, priorities in order
## Done criteria      - [ ] claims about the world, not tasks
## Horizon ladder     short-term, medium-term, long-term
## Constraints        never do X; always keep Y green
## Question policy    how to decide for me: "prefer the reversible option"
## Quality bar        what the critic grades against
```

"Done" is a verdict, not a stop condition. When the done criteria are met, the
run continues up the horizon ladder.

## Motion is not progress

A loop that keeps working can ship nothing: records about records, audits of
audits, plans that restate the last plan. All of these write files, so a diff
looks like progress. One real run did 201 iterations, changed 22,437 lines,
wrote 128 records, and moved the frontier by 1 node.

An agent that you ask "are you looping?" always finds a reason why this time is
different. So Ouroboros counts two things that an agent cannot argue with:

| Counter | Counts iterations that | Fires at |
|---|---|---|
| `no_product` | changed only memory files, not the product | 8 |
| `no_frontier` | left the frontier unchanged | 30 |

Step 1: name the loop in the next prompt. Step 2: change the model. There is no
step 3. A loop is a thing to break out of, not a thing to die of.

## The run's page

Every run keeps a live page at `.ouroboros/runs/<run>/view/index.html`, written
for someone who does not know the code. Ouroboros fills in the facts: the loop
as a dial with the stage running now, the key figures, gauges for the time
budget, usage and progress toward done, the criteria, the open problems, a
timeline of every iteration, and the memory graph. The scribe writes a
plain-language summary and the findings. `ouroboros view` serves it;
`ouroboros view --host 0.0.0.0` lets a phone open it, for example over
Tailscale. A repo can bring its own template at `.ouroboros/view/template.html`.

See [borris.dev](https://borris.dev) for a page in this style; its timeline is
from a real run.

## Configuration and usage

If an agent hits a usage limit, Ouroboros switches to a configured fallback or
waits for access to return. By default the actor falls back from Claude to
Codex. Set roles and limits in `.ouroboros/config.yml`:

```yaml
roles:
  actor: { harness: claude, timeout: 90m, fallback: [{ harness: codex }] }
  critic: { harness: codex, timeout: 10m, fallback: [{ harness: claude }] }
  scribe: { harness: claude, model: sonnet, timeout: 20m, fallback: [{ harness: codex }] }
stop: { after: 8h, max_usage: 0.8 }
```

Efficient work includes efficient spend. A subscription charges a flat fee and
then rations by window, so a run meters windows, not dollars: windows for Claude
Code and Codex, dollars only for a harness on an API key. `max_usage` is a
fraction of a subscription's **weekly** window, because a five-hour window
refills on its own and the fallback covers the wait. `ouroboros status` and the
report show where each window sits and how far the run moved it.

A subscription's `total_cost_usd` is priced from token counts at API list rates.
It is not your bill.

After you switch a Claude or Codex login, or get usage back, run
`ouroboros refresh` in the project. It checks the configured providers again
with small no-tool probes and wakes a waiting run. Active calls finish first.
A configuration change still needs a restart.

## On your phone

`ouroboros watch` is a reporter outside the loop. It reads the run directory,
never the loop, and pushes to [Pushover](https://pushover.net). Put your user
key and an application token in `.env`, `.ouroboros/.env`, or
`~/.ouroboros/.env`:

```
PO_USER=...     # your user key
PO_TOKEN=...    # an application token from pushover.net/apps/build
```

`ouroboros run` then starts the reporter next to the loop. It pushes fixed-text
alerts as soon as they show in the files: the run stopped, a harness ran out of
usage, `NEEDS_HUMAN.md` appeared, the critic called it stuck, the process died.
It also sends a model-written digest every four hours and when the run ends:
what landed, what is next, what is broken, the numbers. Tune it under `report:`
in the config. `ouroboros watch --test` checks the phone; `--once` sends a
digest now.

## Memory

Memory is how a run explains itself, to the next iteration and to you. Records
are append-only. Each one says what it changes. State is a set of claims that
cite records, and only a fold writes state. The next actor reads the state and
the records that are not folded yet.

Every run keeps this [governor](https://github.com/theo-kirby/governor) model.
A repo with a governor graph (`.flywheel/db.sqlite` at the checkout, `governor`
on `PATH`) keeps it there. Any other repo keeps the same records and claims as
markdown files under `.ouroboros/graph/`, committed with the code, in a shape
that can seed a Flywheel graph later. `memory: auto` picks governor when it is
there and `files` if not; `--memory governor|files` forces one.

Agents see one command for both: `ouroboros mem orient | show | record | fold |
check | render`. You can run it too. Keep `.flywheel/` and `.governor/`
gitignored (a governor run adds them to `.gitignore` if they are not there).

Use a merge commit for graph-memory runs: their records reference commit IDs,
and a squash or a rebase changes them.

## Development

```bash
uv run pytest -q
```

Role prompts are skills in `src/ouroboros/skills/`. [AGENTS.md](AGENTS.md) is
the contract for changes to this repo. [DESIGN.md](DESIGN.md) holds the design,
the decisions, and the lessons from real runs.
