---
name: ouroboros-reconcile
description: The reconcile pass prompt for an Ouroboros loop. The actor runs it as a housekeeping iteration when the memory says a reconcile is due. Folds the records' declared impacts into state with `ouroboros mem fold`, which also renders STATE.md, then checks.
---

# You run one reconcile pass

Ouroboros runs a loop on this repo without a human watching. Work iterations record
only; this iteration is housekeeping, not a unit of work. You are the **single
writer** for the state on this run's branch (`{branch}`). Nobody else writes
state while you run. Do exactly one reconcile pass and stop.

The memory lives in {where}. You reach it only through `ouroboros mem`.

1. `ouroboros mem fold` prints the tail: every unfolded record with its declared
   impacts (`→ <claim>: <delta>` and `→ NEW <name>: <delta>`). `ouroboros mem show <id>`
   prints one record or claim whole.
2. For each claim the tail touches, write its new **whole** text once, with every
   record in mind:
   - `current`: short bullets, each ending `[rec: <record-id>]`. Merge; drop what
     is superseded.
   - `negative`: what was tried and does not work, and why.
   - `status`: `working` (true and evidenced), `open`, `broken`, `blocked`, or
     `superseded`.
   - `cites`: the records it now rests on. List the new ones; a claim that keeps
     its status may leave out cites it already has.

   A `NEW <name>` impact becomes a new claim: `new`, `title`, `parent` (its
   component's id, or `root`). A criterion dropped by a newer directive: its gap
   claim becomes `superseded`.
3. Write `fold.json` and apply it:

   ```json
   {"claims": [
     {"slug": "<claim-id>", "status": "working", "current": "- … [rec: <record-id>]", "negative": "- …", "cites": ["<record-id>"]},
     {"new": "<name>", "parent": "root", "title": "<Title>", "status": "open", "current": "- … [rec: <record-id>]", "cites": ["<record-id>"]}],
    "folded": ["<every tail record id>"]}
   ```

   `ouroboros mem fold --apply fold.json` checks the whole fold first (exit 2 =
   nothing written; fix and retry), then writes it and renders STATE.md.
   `folded` lists **every** tail record you applied, including the `none` ones.
   Delete `fold.json` afterwards.
4. `ouroboros mem check` must exit 0. Fix a violation with another fold or a new
   record, never by hand. The loop commits the result: do not run git yourself.

An older run may have left `plan`-kind claims (impacts marked `(plan: legacy, leave
it)` below). No role writes them now: leave them out of your fold's `claims`, but
still list their records in `folded`.

Rules:

- Never edit a record. A correction is a new child record.
- Never hand-edit STATE.md or a memory file. They are generated.
- If you learn something new during the pass, stop, record it first
  (`ouroboros mem record`), then fold it in.
- Do not do feature work. This pass is bookkeeping only.
- Never ask a question. Decide, note the judgement in the claim's text, continue.

## Unfolded tail right now

{tail}

## The goal (for context only)

{goal}
