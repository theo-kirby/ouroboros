---
name: ouroboros-orient
description: The memory section of an Ouroboros work iteration. What is true now (frontier, unfolded tail, architecture, recent records) and the rules for a contributor. Read through `ouroboros mem`, whichever store holds the memory.
---

This repo's memory is a record/state graph: append-only **records** of what
happened, each declaring what it changes, and **state claims** (what is true now)
that cite the records they rest on. It lives in {where}. You reach it only through
`ouroboros mem`:

- `ouroboros mem orient`: what is printed below, fresh.
- `ouroboros mem show <id>`: one record or claim, whole.
- `ouroboros mem record ...`: append your record (see "Record before you stop").

State lags the tail by design: apply the tail's declared impacts in your head
before you choose work.

This iteration is **one dispatch with a budget of {budget} unit(s)**. Target: the
goal, or a frontier claim (open / broken / blocked) that serves it. Say which in
`## Why`.

**Forbidden in a work iteration, no exceptions:** `ouroboros mem fold`, editing
any memory file or STATE.md by hand, and the store's own tools (`governor`,
`flywheel`). You are a contributor: you record; a separate reconcile pass applies
the tail. If the tail looks fat, say so in `## Result` and keep working. Even when
the done criteria are met, the next unit is a rung of the horizon ladder, never a
fold.

## What is true now (`ouroboros mem orient`)

{orient}

Unfolded tail: {tail_count} record(s) (listed with their impacts above).
