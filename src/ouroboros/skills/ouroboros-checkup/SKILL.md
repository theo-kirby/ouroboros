---
name: ouroboros-checkup
description: Check in on an Ouroboros run, live or finished. Reads the run directory, git, and the plan - what landed, whether the loop is moving or going in circles, bets the planner changed, decisions the overseer made on the user's behalf, critic rejects, usage, and either a merge recommendation or a mid-run correction. Use when the user asks how a run is going or how one went.
---

# Check in on a run

A loop is working, or has finished working, unattended. Give the user the read in
five minutes, most important first, so they can decide: **let it run, correct it,
stop it, merge it, or re-charter it.**

Runs are no longer nights. One may start in the morning and go for two days. Never
assume the run is over, never assume the user was asleep, and never assume the
elapsed time is the whole story — check the state first and say which read this is.

## First: is it still running?

```
ouroboros status
```

`state=work` with a live pid means **this is a mid-run check-in**. The run
directory is being written while you read it, so:

- Do not run `ouroboros report` expecting a final number; iteration counts move.
- Do not recommend a merge. Recommend a **correction** instead — something the
  user can change now: the charter, the config, or a stop.
- Never edit files the loop is working in. A charter edit takes effect on the
  next run start, not this one; say so when you suggest one.

`state=stopped` or `state=killed` means the run is over: give the full read and a
merge recommendation.

## Gather

1. `ouroboros status`, then `ouroboros report` (writes `.ouroboros/runs/<run>/REPORT.md`).
   If the run name is unclear, list `.ouroboros/runs/`.
2. `.ouroboros/runs/<run>/NEEDS_HUMAN.md` if it exists: read it first, it is the
   one thing that blocked the loop.
3. `.ouroboros/runs/<run>/iterations.jsonl`: steps `actor`, `commit`, `critique`,
   `oversee`, `loop`, `revert`, `reconcile`, `plan`. `overseer.jsonl` for every
   verdict with its reason and reply. `loop.log` for backoffs, limit waits, and
   harness switches.
4. The plan: `PLAN.md` and the `plan` view nodes under `.hypergraph/graph/plan/`
   (hypergraph repos) or `.ouroboros/plan.md` and `.ouroboros/bets.md`.
5. Git: `git log --oneline main..<run branch>` and `git diff --stat main...<run branch>`.
   Record nodes added: `git diff --stat main...<branch> -- .hypergraph/graph/record`.
6. The frontier before and after (`git show main:STATE.md` vs the branch), compared
   **by node status**, not by prose.

## Is it moving, or going in circles?

This is the question that matters most, and the one the raw numbers hide. A run can
write twenty thousand lines and move nothing.

- **`step="loop"` rows in `iterations.jsonl`** are the detector firing: `signal`
  (`no_product` / `no_frontier` / `repeat_bet`), `streak`, `escalation` (1 name it,
  2 re-plan, 3 rotate the model) and `acted`. Report the **first** firing and the
  **longest** streak, not every row.
- **`looping` verdicts** in `overseer.jsonl`.
- **Count what actually moved.** Nodes that changed status, and charter criteria
  ticked. If that count is near zero over a long run, say so plainly at the top,
  whatever the diff size says.
- **Read the bets in order.** If they restate each other in different words, the
  plan is the thing that is stuck, not the actor.

Do not soften this. A run that is busy and going nowhere looks healthy in every
other metric.

## Report, in this order

1. **Blockers.** `NEEDS_HUMAN`, auth failures, a harness limited for hours.
2. **What landed.** Two sentences. Then a table: iterations, productive (changed
   and recorded), reverted, empty, wall clock, harness switches, and **usage per
   subscription window**. Only report money for a harness that bills per call.
3. **Moving or circling.** The section above. If the detector fired, lead with it.
4. **Bets the planner changed.** Each `Bet:` record with its `## Why`. Mark the
   ones the user might want to overrule, and say how: edit `.ouroboros/goal.md`
   (a new charter version) or tell the next run's planner in the ladder.
5. **Decisions the overseer made for you.** Every `answer` verdict: the question
   the actor asked and the answer given. Every `done_rejected` with the reason.
6. **Critic rejects and reverts.** What was thrown away and why; whether a
   reverted patch under `.ouroboros/runs/<run>/reverted/` holds anything worth
   keeping. **Check whether a reject was fixed forward by a later iteration
   before recommending anything be undone.**
7. **The frontier moved.** Gaps closed (open → working), gaps opened, gaps blocked.
8. **Review hardest.** The largest or riskiest diffs: deletions, protocol changes,
   generated files committed by accident, anything touching the constraints' zones.
9. **The recommendation.**
   - Run finished → merge with a merge commit (record nodes cite commit SHAs;
     **never squash a hypergraph run**), merge after fixing X, or do not merge.
   - Run live → the one correction worth making now, or "leave it alone".
10. **Charter changes to consider.** What this run showed the charter got wrong:
    a rung that was empty, a constraint that was missing, a criterion that was
    vague, a gate the actor had no way to satisfy.

Keep it short. Link every claim to a file or a commit so the user can check it.
