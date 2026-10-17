---
name: ouroboros-record
description: How an Ouroboros work iteration records its unit, through `ouroboros mem record`. One record per iteration, with its declared impact on state; the same command for every memory store.
---

When the unit is finished, and before you stop, you MUST record it as ONE record
(never a fold, never a claim):

1. Write the body with exactly these headings: `## What`, `## Why`, `## Method`,
   `## Result` (never `## Recorded` or `## State Impact`: those are generated). A
   dead end is recorded the same way as a success. `## Why` says why this unit,
   and, if you did not do what the critic's message asked, what you did instead
   and why. `## Result` carries what is true now, then any concern, assumption, or
   new dependency the next iteration must know about, and ends with the line
   `Dispatch closed: 1 unit — <summary>`.
2. Record it, with the body on stdin or in a file:

   ```
   ouroboros mem record --title "<title>" --body body.md --impact "<claim-id>: <delta>"
   ```

   - `--impact "<claim-id>: <delta>"` for a claim that exists (ids are in the
     frontier and architecture above); `--new "<kebab-name>: <delta>"` for a claim
     that does not exist yet; `--none "<reason>"` when state truly does not change.
     Several `--impact` and `--new` flags are fine. The delta says what changed,
     not what you did.
   - The parent defaults to the newest record (`{parent}`). Give
     `--parent <record-id>` only when this work truly follows from another record.
   - Exit 2 means nothing was written, and the message says why: fix and retry.
3. Delete the body file if you wrote one. There is nothing else to do: the loop
   commits whatever the memory changed.

An iteration with no new record is counted as lost work.
