---
name: ouroboros-fold
description: How an Ouroboros scribe folds the memory's tail into state with `ouroboros mem fold`, which also renders STATE.md. Included in the scribe's prompt when a fold is due; the same procedure on every memory store.
---

A fold is due. You are the **single writer** of state on this run's branch
(`{branch}`); nobody else writes it while you run. Fold before you record this
iteration, so the claims your record names exist; your record waits in the tail
for the next fold.

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

An older run may have left `plan`-kind claims (impacts marked `(plan: legacy, leave
it)` in the tail). No role writes them now: leave them out of your fold's `claims`,
but still list their records in `folded`.

The tail right now (before your record of this iteration):

{tail}
