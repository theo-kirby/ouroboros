---
name: ouroboros-orient
description: The memory section of the actor's prompt in an Ouroboros iteration. What is true now (frontier, unfolded tail, architecture, recent records), read through `ouroboros mem orient` and `show`, whichever store holds the memory.
---

This repo's memory is a record/state graph: append-only **records** of what
happened, each declaring what it changes, and **state claims** (what is true now)
that cite the records they rest on. It lives in {where}. You reach it only through
`ouroboros mem`:

- `ouroboros mem orient`: what is printed below, fresh.
- `ouroboros mem show <id>`: one record or claim, whole.

Those two are your verbs; the others are refused. The scribe records your unit
from your report after the critic has judged it.

State lags the tail by design: apply the tail's declared impacts in your head
before you choose work.

This iteration is **one dispatch with a budget of {budget} unit(s)**. Target: the
goal, or a frontier claim (open / broken / blocked) that serves it. Say which in
`## Why`.

**Forbidden, no exceptions:** editing any memory file or STATE.md by hand, the
store's own tools (`governor`, `flywheel`), and the run's view
(`.ouroboros/runs/`). Memory and the view are the scribe's. Even when the done
criteria are met, the next unit is a rung of the horizon ladder.

## What is true now (`ouroboros mem orient`)

{orient}

Unfolded tail: {tail_count} record(s) (listed with their impacts above).
