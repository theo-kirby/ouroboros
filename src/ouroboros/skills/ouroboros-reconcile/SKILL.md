---
name: ouroboros-reconcile
description: The reconcile pass prompt for an Ouroboros loop on a repo with graph memory (governor or hypergraph-protocol). The actor runs it as a housekeeping iteration when the memory says a reconcile is due. Folds the records' declared impacts into state, regenerates STATE.md, checks, commits.
---

# You run one reconcile pass

Ouroboros runs a loop on this repo without a human watching. Work iterations record
only; this iteration is housekeeping, not a unit of work. You are the **single
writer** for the state graph on this run's branch (`{branch}`). Nobody else writes
state while you run. Do exactly one reconcile pass and stop.

{procedure}

Rules:

- Never edit a record. A correction is a new child record.
- Never hand-edit STATE.md. It is generated.
- If you learn something new during the pass, stop, record it first, then fold
  it in.
- Do not do feature work. This pass is bookkeeping only.
- Never ask a question. Decide, note the judgement in the claim's text, continue.

## Unfolded tail right now

{tail}

## The goal (for context only)

{goal}
