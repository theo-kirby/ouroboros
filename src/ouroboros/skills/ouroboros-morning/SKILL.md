---
name: ouroboros-morning
description: The morning read after an unattended Ouroboros run. Summarizes the night from the run directory, git, and the plan - bets the planner changed, decisions the overseer made on the user's behalf, critic rejects and reverts, cost, a merge recommendation, and what to change in the charter. Use when the user asks how a run went.
---

# Read the night

The user slept while a loop worked. Give them the morning read in five minutes, most
important first, so they can decide: merge, overrule, re-version, or run again.

## Gather

1. `ouroboros report` (writes `.ouroboros/runs/<run>/REPORT.md`) and `ouroboros status`.
   If the run name is unclear, list `.ouroboros/runs/`.
2. `.ouroboros/runs/<run>/NEEDS_HUMAN.md` if it exists: read it first, it is the
   one thing that blocked the loop.
3. `.ouroboros/runs/<run>/overseer.jsonl`: every verdict with its reason and reply.
   `iterations.jsonl`: steps `actor`, `commit`, `critique`, `oversee`, `revert`,
   `reconcile`, `plan`. `loop.log` for backoffs, limit waits, harness switches.
4. The plan: `PLAN.md` and the `plan` view nodes under `.hypergraph/graph/plan/`
   (hypergraph repos) or `.ouroboros/plan.md` and `.ouroboros/bets.md`.
5. Git: `git log --oneline main..<run branch>` and `git diff --stat main...<run branch>`.
   Record nodes added: `git diff --stat main...<branch> -- .hypergraph/graph/record`.
6. `STATE.md` frontier before and after (`git show main:STATE.md` vs the branch).

## Report, in this order

1. **Blockers.** NEEDS_HUMAN, auth failures, a harness that stayed limited all night.
2. **What landed.** Two sentences. Then a table: iterations, productive (changed and
   recorded), reverted, empty, wall clock, API-equivalent cost, harness switches.
3. **Bets the planner changed.** Each `Bet:` record with its `## Why`. Mark the ones
   the user might want to overrule. Say how: edit `.ouroboros/goal.md` (a new charter
   version) or tell the next run's planner in the ladder.
4. **Decisions the overseer made for you.** Every `answer` verdict: the question the
   actor asked and the answer given. Every `done_rejected` with the reason.
5. **Critic rejects and reverts.** What was thrown away and why; whether the reverted
   patch under `.ouroboros/runs/<run>/reverted/` holds anything worth keeping.
6. **The frontier moved.** Gaps closed (open to working), gaps opened, gaps blocked.
7. **Review hardest.** The largest or riskiest diffs: deletions, protocol changes,
   anything touching the constraints' zones.
8. **Merge recommendation.** Merge with a merge commit (record nodes cite commit
   SHAs; never squash a hypergraph run), merge after fixing X, or do not merge.
9. **Charter changes to consider.** What the night showed the charter got wrong:
   a rung that was empty, a constraint that was missing, a criterion that was vague.

Keep it short. Link every claim to a file or a commit so the user can check it.
