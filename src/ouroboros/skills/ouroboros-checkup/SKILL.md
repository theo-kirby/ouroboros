---
name: ouroboros-checkup
description: Check in on an Ouroboros run, live or finished. Reads the run directory, git, and the memory - what landed, whether the loop is moving or going in circles, decisions the critic made on the user's behalf, rejects and reverts, usage, and either a merge recommendation or a mid-run correction. Use when the user asks how a run is going or how one went.
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

## Then: what came before

`.ouroboros/RUNS.md` is every run against this repo, oldest first, and
`.ouroboros/history/<run>.md` is one digest each. **Read the previous run's
digest before you read this one's logs**, starting with the notes below the
marker: those are what a human concluded, and they are the only part that is not
derivable from the logs you are about to read.

Two columns in the index carry most of the signal. `ticked` is what a run closed
on the charter -- not what is checked at its tip, which includes everything the
charter opened with. `commits` beside it says how much the run wrote. A run with
a large `commits` and a `ticked` of 0 is the pattern to name out loud, and if
several runs in a row read that way, say so: that is a charter problem, and no
amount of loop detection fixes it.

## Gather

1. `ouroboros status`, then `ouroboros report` (writes `.ouroboros/runs/<run>/REPORT.md`).
   If the run name is unclear, list `.ouroboros/runs/`.
2. `.ouroboros/runs/<run>/NEEDS_HUMAN.md` if it exists: read it first, it is the
   one thing that blocked the loop.
3. `.ouroboros/runs/<run>/iterations.jsonl`: steps `actor`, `commit`, `critique`,
   `loop`, `revert` (older runs also have `oversee`, `reconcile` and `plan`, from
   roles the loop no longer has).
   `critic.jsonl` (older runs: `overseer.jsonl`) for every verdict with its
   reason, reply, and `fix_first`. `loop.log` for backoffs, limit waits, and
   harness switches.
4. The next units: the critic's `reply` and `doing` in `critic.jsonl`, and in a
   handoff repo the actor's `.ouroboros/plan.md`.
5. Git: `git log --oneline main..<run branch>` and `git diff --stat main...<run branch>`.
   Records are never in git: `governor snapshot` lists them (`kind: record`, with `at` and `## Recorded`
   naming the branch), `governor fold` shows what is still unfolded, and `governor check` must exit 0.
6. The frontier before and after (`git show main:STATE.md` vs the branch), compared
   **by node status**, not by prose.

## Is it moving, or going in circles?

This is the question that matters most, and the one the raw numbers hide. A run can
write twenty thousand lines and move nothing.

- **`step="loop"` rows in `iterations.jsonl`** are the detector firing: `signal`
  (`no_product` / `no_frontier`; older runs also `repeat_bet`), `streak`,
  `escalation` (1 name it, 2 rotate the model) and `acted`. Report the **first** firing and the
  **longest** streak, not every row.
- **`looping` verdicts** in `critic.jsonl`.
- **Count what actually moved.** Nodes that changed status, and charter criteria
  ticked. If that count is near zero over a long run, say so plainly at the top,
  whatever the diff size says.
- **Read the critic's replies in order.** If they restate each other in different
  words, the steering is the thing that is stuck, not the actor.

Do not soften this. A run that is busy and going nowhere looks healthy in every
other metric.

## Report, in this order

1. **Blockers.** `NEEDS_HUMAN`, auth failures, a harness limited for hours.
2. **What landed.** Two sentences. Then a table: iterations, productive (changed
   and recorded), reverted, empty, wall clock, harness switches, and **usage per
   subscription window**. Only report money for a harness that bills per call.
3. **Moving or circling.** The section above. If the detector fired, lead with it.
4. **Decisions the critic made for you.** Every `answer` verdict: the question
   the actor asked and the answer given. Every `done_rejected` with the reason.
5. **Critic rejects and reverts.** What was thrown away and why; whether a
   reverted patch under `.ouroboros/runs/<run>/reverted/` holds anything worth
   keeping. **Check whether a reject was fixed forward by a later iteration
   before recommending anything be undone.**
6. **The frontier moved.** Gaps closed (open → working), gaps opened, gaps blocked.
7. **Review hardest.** The largest or riskiest diffs: deletions, protocol changes,
   generated files committed by accident, anything touching the constraints' zones.
8. **The recommendation.**
   - Run finished → merge with a merge commit (record nodes cite commit SHAs;
     **never squash a governor run**), merge after fixing X, or do not merge.
   - Run live → the one correction worth making now, or "leave it alone".
9. **Charter changes to consider.** What this run showed the charter got wrong:
    a rung that was empty, a constraint that was missing, a criterion that was
    vague, a gate the actor had no way to satisfy.

Keep it short. Link every claim to a file or a commit so the user can check it.

## Last: leave the record better than you found it

A run that is over must leave something the next operator can read.

1. `ouroboros archive` -- rewrites this run's digest and the index. Run it again
   after a merge: `merged:` is a fact about the base branch and changes without
   the run changing.
2. Write **`## What this taught`** in `.ouroboros/history/<run>.md`, below the
   notes marker. Everything above the marker is measured and will be rewritten;
   below it is yours and is preserved. Two or three sentences, and only what the
   numbers cannot say themselves: a criterion that could never close, a model
   that stalled, a config that burned a window for nothing, a constraint the
   charter was missing.
3. Commit the digest and the index with the merge. The run directory is
   gitignored and does not leave the machine; the digest is the only part of the
   run that outlives it.

Do not write into the digest what the generated part already says. A number that
appears twice will disagree with itself eventually.
