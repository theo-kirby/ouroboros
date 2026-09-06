---
name: ouroboros-design
description: Interview the user and write the charter for an Ouroboros run - .ouroboros/goal.md and .ouroboros/config.yml. Use when the user wants to set up, redesign, or re-version the goal of an unattended loop. Reads the repo first, then grills the user rung by rung until every horizon has three concrete items.
---

# Design an Ouroboros charter

You are helping a human write the **charter** for an unattended agent loop. The
charter is the one document the human owns. Everything else, the plan, the bets, the
frontier, is written by agents while the human sleeps. So the charter must say what
the human would say if they were there, and it must never run out.

Read `DESIGN.md` section 20 in the Ouroboros repo if you can; the short form is below.

## Before you ask anything

1. Read the repo: `AGENTS.md` / `CLAUDE.md`, `README.md`, `STATE.md` and `PLAN.md`
   if present (a hypergraph-protocol repo), `docs/ROADMAP.md` or similar, the test
   command, the last 30 commits. Form a view of what is broken, what is next, and
   what the repo's own rules are.
2. Check for an existing charter: `.ouroboros/goal.md`. If it exists, this is a
   **re-version**: read it, read `.ouroboros/runs/*/REPORT.md` and the bets in
   `PLAN.md` or `.ouroboros/bets.md`, and start the interview from what changed.
3. Check which harnesses are installed and logged in: `claude`, `codex`, `pi`.

## The interview

Ask in rounds. One round per section. Propose a draft from what you read, then ask
the user to correct it. Never accept a vague answer; ask for the concrete version.

1. **Mission.** One paragraph: what and why, in priority order if there are several
   thrusts. Ask: "If the loop only achieved one thing tonight, what must it be?"
2. **Done criteria.** Claims about the world, not tasks. Each becomes an open gap
   on the frontier that work must falsify. Bad: "add tests". Good: "the parser
   suite passes on a clean clone". Ask for the test or evidence that proves each.
3. **Horizon ladder.** The trick that makes a run never end. For each rung, ask
   "what should it do if it is still running after this long?":
   the next hour, the next day, the next week, the next month, the next year.
   **Refuse to finish until every rung has at least three concrete items.** The
   year rung is usually maintenance: "keep every gate green, every doc true".
   Tell the user: the agents will re-plan from this ladder every few iterations
   and record their bets; the ladder is the first plan, not the last.
4. **Constraints.** Never-do and always-keep. Which directories are off limits,
   which commands must never run (GUI launches, deploys, GPU boxes), what must
   never be committed (secrets, machine paths, build outputs), which repo rules
   are the contract (usually "obey AGENTS.md").
5. **Question policy.** How to decide when nobody answers. Reversible over
   irreversible; smallest unit in the highest-ranked open item; code wins over
   docs; what to do with pre-existing failures; "never wait for a human".
6. **Exhaustion policy.** `creative` (the planner proposes new directions),
   `maintain` (tests, flakes, docs, refactors forever), or `report_done` (idle and
   poll). Explain that with the planner on, `creative` is the default behaviour of
   an empty frontier anyway.
7. **Quality bar.** What the critic grades against: gates, commit shape, record
   node rules, doc-with-code rules.
8. **Run shape.** Duration (`stop.after`), harness chain and fallbacks, mode
   (`single`, `actor-critic`, `council`), overseer model, planner on or off, memory
   (`auto` picks hypergraph when the repo has it).

## Write the files

- `.ouroboros/goal.md` with exactly these sections, in this order:
  `# Goal: <name>`, `## Mission`, `## Done criteria`, `## Horizon ladder`,
  `## Constraints`, `## Question policy`, `## Exhaustion policy`, `## Quality bar`,
  and for hypergraph repos `## Reconcile`. Done criteria are `- [ ]` checkboxes.
  Ladder rungs are `- **the next hour:** ...` lines.
- `.ouroboros/config.yml` (run `ouroboros init` first if missing). Example:

```yaml
run: nt2
memory: auto
mode: actor-critic
overseer: agent
roles:
  actor:      { harness: claude, timeout: 90m, fallback: [{ harness: codex }] }
  critic:     { harness: codex, timeout: 15m }
  overseer:   { harness: claude, model: haiku, timeout: 5m, fallback: [{ harness: codex }] }
  maintainer: { harness: claude, timeout: 30m, fallback: [{ harness: codex }] }
  planner:    { harness: claude, timeout: 20m, fallback: [{ harness: codex }] }
stop: { after: 10h }
hypergraph: { reconcile_every: 5, pressure: 3, budget_units: 1 }
plan: { enabled: true, every: 5, max_new_directions: 3 }
```

- Read both files back to the user in full. Then say: `ouroboros run` starts it,
  `ouroboros status --watch` watches it, `ouroboros stop` stops it, and in the
  morning `/ouroboros-morning` reads the night.

## Rules

- The human owns the charter; do not let it drift into a task list. If the user
  starts listing tasks, put them on the ladder's hour and day rungs and turn the
  outcome into a done criterion.
- A re-version replaces the file; Ouroboros mints a new directive record for it
  and declares only the criteria it adds. Say so.
- Never write the plan, `PLAN.md`, or `.ouroboros/plan.md` yourself. The planner
  seeds it from the ladder.
