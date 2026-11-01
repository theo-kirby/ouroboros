# The keeper loop (design, 2026-11-01)

Status: agreed with Theo, not built. A second production loop beside
actor-critic-scribe, chosen by config, for day-to-day use and comparison. This
file records the structure, the decisions and why we made them. When it is
built, the matching DESIGN.md sections take over and this file points to them.

## Why a second loop

Ouroboros exists to make long runs work: keep a run going across many fresh
context windows without stopping early, going in circles or forgetting what it
learned. It does not need to dictate how each turn does its work. Seen that way,
the memory graph and the orientation of fresh agents are the core, and the role
structure should be as small as it can be.

What the runs so far said (see the topology screen and the evolution campaign):

- Single-agent freeflow was competitive with actor-critic-scribe; review lenses
  scored lowest and cost the most (gen-0 wave 1, n = 1 each).
- Keeping a report current every turn took 40% of one arm's tokens.
- Runs plateau within 40-110 minutes unless something changes direction.
- The critic's clear value was catching false "done" claims and redirecting a
  stuck run, not grading every turn.

Theo, 2026-11-01: "the purpose of ouroboros is to make long runs work, not
necessarily dictate everything about how they work", and "a fresh context
window is gold for long runs if we can efficiently leverage the orientation of
fresh runs".

## The structure

Two roles.

- **Actor.** Does the work. Fresh context every turn. Gets a prompt, orients
  (the graph, read-only, through `ouroboros mem`), does one turn of work,
  commits, and ends with a report for the keeper. Never writes the graph.
- **Keeper.** Owns the run's state and direction. Fresh context every turn.
  Reads the actor's report, the diff and the commits; reviews the work against
  the charter; writes the graph (records, nodes, connections, folds); then
  writes the next prompt. The only memory writer.

One cycle:

```
turn 0   keeper   charter -> seed the graph (criteria as open gaps, constraints) -> prompt 1
turn 1   actor    prompt 1 + footer -> orient -> work -> commit -> report
turn 2   keeper   prompt 1 + report + diff + graph -> review -> write graph -> prompt 2
turn 3   actor    prompt 2 + footer -> ...
```

The engine keeps what it has now and needs no model call for: stop rules, the
no-early-exit rule, loop detection, limits and backoff, the final report pass.

### The actor's prompt

The keeper writes the next-step prompt. The engine appends a fixed footer the
keeper cannot leave out:

- the charter's path, to read when needed (not inlined every turn);
- how to orient: `ouroboros mem brief` and the read verbs;
- the hard rules: commit your work, end with a report in this shape.

### Two handoffs, kept apart

- **Actor -> keeper: the report.** Always on. It is the keeper's main input,
  alongside the diff; rebuilding everything from the diff alone is expensive.
- **Keeper -> actor: the prompt.** Optional, because whether a written next step
  beats orienting from the graph alone is an open question. One config key:
  - `handoff: both`: prompt, then orient from the graph (default);
  - `handoff: graph`: no prompt; the actor reads the frontier and picks;
  - `handoff: prompt`: the prompt only, no graph orientation asked for.

### Config sketch

```yaml
loop: keeper            # standard (actor-critic-scribe, the default) | keeper
handoff: both           # keeper loop only: both | prompt | graph
roles:
  actor:  { harness: codex, timeout: 45m }
  keeper: { harness: codex, timeout: 20m }
```

`loop: standard` stays the default, so existing runs are unchanged.

## Decisions and why

| Decision | Why |
|---|---|
| Modify the existing loop rather than build a new system. | The engine, memory, stop rules and view already work; only the roles change. |
| Two roles: actor and keeper. | Simpler. Leaves the work to the agent; Ouroboros owns continuity. |
| Merge the critic's steering and the scribe's memory work into the keeper. | The agent that decides what is next is the one that just organised what is known; that makes the next prompt good. Keeps one memory writer for free. |
| The actor never writes the graph. | Clean separation: the actor works, the keeper keeps. Matches `memcli.VERBS_OF` (actor: read verbs only). |
| Both roles start fresh every turn. | Fresh context is the asset. The keeper lives off the graph it keeps, so a sloppy graph costs it directly. Removes any compaction machinery (an earlier draft had a persistent planner auto-compacting at 40%). |
| The keeper goes first (turn 0). | In an empty repo it seeds the graph from the charter and writes prompt 1, so every turn has the same shape and the actor never starts cold. |
| No reverting. | The keeper records a bad turn as failed in the graph, says why, and the next prompt fixes it forward. Simpler; may come back as a switch. |
| Charter by path, not inlined. | The prompt carries what to do next; the charter is there when the actor wants it. The engine's footer guarantees the pointer. |
| Prompt handoff optional (`handoff`). | Prompt-plus-graph vs graph-only is the comparison Theo wants to run. |
| Figures and notes are not a per-turn keeper job. | The keeper already reviews, keeps and plans. The notebook is rendered from the graph; figures are the actor's work when a prompt asks for them; the paper is the single final pass (tested 2026-11-01, PR #1). |
| Not run in the background. | A background memory writer would leave the next actor orienting on a graph one turn behind; considered and dropped. |
| Name: keeper. | Keeper of the graph and of the run's direction. Can change. |

Considered and set aside: a planner with persistent history that writes every
prompt (anchoring, compaction machinery, checks its own homework); a background
scribe; a separate critic every turn.

## Open questions

- **Orientation.** What a fresh actor and a fresh keeper should read, in what
  size: a brief written for a newcomer (goal, best result, frontier, what was
  ruled out and why), the previous handoff, and on-demand `mem` queries. The
  transcript study measures today's orientation cost first; then an orientation
  lab tests brief formats on frozen run states.
- **Turn length.** One bounded unit per actor turn (as now), or work until the
  context is about 40% full and then hand off.
- **Done.** The keeper judges done; the engine's no-early-exit rule still holds.
  Whether done needs a second opinion is open.
- **AGENTS.md.** "Production: three roles, one writer" becomes "standard: three
  roles; keeper: two roles; one writer either way". Update it when this is built.

## What the transcript study found (2026-11-01)

Full report: `research-projects/orientation-study/REPORT.md` (736 calls from 52
runs; no model calls). What bears on this design:

- Actors orient cheaply, in about 4 tool calls and about 10k tokens of reading
  (n = 258). They are steered by the handoff, not the graph: `ouroboros mem` is
  0-3% of what they read, and in 25 of 30 sampled later turns the handoff
  already named the unit.
- Repeats of ruled-out work are rare (1 in 30). What goes wrong is weak
  handoffs: steps impossible on the host, constraints with no next step,
  missing environment facts, and "dead end" meaning both "do not retry" and
  "fix and resubmit".
- Scribes and critics are the expensive orientation: 35-50k tokens each before
  their first useful action. A keeper handed nothing better would start there.
- Avoidable reading: AGENTS.md hunting in most adapter turns, and production
  actors and critics reading a stale installed copy of their role skill every
  turn (fixed: `skills install` now installs only the user skills).

So, for the keeper loop:

- The next-step prompt is the keeper's main product: the unit and its stopping
  rule, 2-5 starting files, the expected observation, what is blocked on this
  host.
- The actor gets a brief of 1.5-3k tokens that stays that size as the run grows:
  the goal in a paragraph (charter by path), the best result with numbers, 3-5
  frontier items with their claim text, what was ruled out and why (apart from
  fix-and-resubmit), environment facts.
- The footer says where AGENTS.md is (or that there is none), that the prompt
  is the whole role, and gives the exact `mem` verbs instead of `--help`.
- The keeper is handed the actor's report, the diff, the claims the actor
  touched (with negative knowledge), the frontier and the previous prompt:
  about 20-25k tokens at its first write instead of about 50k.
- The keeper fills negative knowledge on every failure. Save every prompt.

## Next

1. Transcript study (done, above).
2. Orientation lab: frozen states, alternative briefs, scored on the first move.
3. Build `loop: keeper` with the winning brief; tests against the fake harness
   for the cycle order, turn 0, the footer, `handoff` modes and the keeper as the
   only writer.
4. Use it day to day beside actor-critic-scribe.
