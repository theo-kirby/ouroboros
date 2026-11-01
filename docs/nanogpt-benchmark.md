# nanoGPT research-system benchmark

2026-10-29. Theo approved pursuing this benchmark. Status: design and calibration pending.

## Question

Starting from a frozen training implementation, can the research system reduce time
to a fixed validation-loss target within a fixed experimental budget, and demonstrate
that its improvement survives independent repetition?

Reference: https://github.com/KellerJordan/modded-nanogpt — the official speedrun
uses eight H100 GPUs and a FineWeb validation cross-entropy target of 3.28. Pin an
exact commit and its rules before adopting them; do not infer current rules from
historical forks. A smaller hardware-adapted task is a separately named local benchmark,
not an official speedrun result. A local target must be calibrated and frozen.

## Experimental contract to freeze after calibration

- Same baseline commit, hardware class, software environment, token streams,
  validation protocol, tools, model/effort and total agent/GPU budgets for every cell.
- Start with a reproducible baseline that has useful optimization headroom. Decide
  whether prior speedrun solutions are permitted; distinguish retrieval from discovery.
- Target 1–2 hours per agent run and trials lasting a few minutes. These are desired
  durations, unverified until hardware calibration. Bound total GPU-seconds and trials.
- Separate systems-only changes from changes to training behavior; record architecture,
  parameter count, precision, optimizer, batch size, token exposure and evaluation changes.
- Reserve fresh evaluation seeds for final confirmation; freeze a rule for candidate
  selection, repetitions and uncertainty before search. Avoid picking the luckiest seed.
- Count crashes, compilation, warmup, data preparation, debugging and failed experiments
  in total campaign cost. Report benchmark training time separately, with timing boundaries.
- An independent evaluator checks data/evaluation integrity and the final candidate.
  Validation access and repeated adaptive selection require explicit controls.
- Stop on evidence-backed completion or a hard compute/time cap. A substantiated
  no-improvement result is valid. Do not require endless search for a positive result.

## Required report and evidence

Explain the hypothesis in plain language, why the change should work, its observed
result, and what remains uncertain. Preserve every attempt, exact commands, Git revisions,
environment/hardware receipts, seeds, timings, loss traces and rejected candidates.
Show loss versus wall-clock time, repeated baseline/candidate comparisons, variation,
and an ablation testing the claimed mechanism. Separate proposed mechanisms from
measured causes. Link native figures to source data and preserve trace identities.

Evaluate research judgment, correctness, creative interventions, all-role/compute
cost, stopping and recovery. Human questions: what improved, by how much, at what
quality/cost, why believe it, and when might it fail? Score actual answers separately
from automated report checks. The baseline is not assumed globally optimal.

## Launch readiness

Create a dedicated experiment directory, Git repository and local Governor/Flywheel
graph; designate one memory writer. Probe authorized hardware without provisioning
paid resources. Reproduce the pinned baseline, calibrate duration/variance and choose
the official or local track. Then freeze executable rules, held-out confirmation,
resource limits, recording and charter. First perform one bounded smoke test.

No GPU availability, runtime, achievable improvement or launched run is claimed here.
The ten-run bone screening proposal and this new benchmark are distinct proposed
batches; decide the first calibrated batch before spending its budget.

## Discovery session — October 29

Calibration project: /home/theo/research-projects/nanogpt-evaluation.
Pinned nanoGPT 3adf61e154c3fe3fca428ad6bc3818b27a3b8291.
Notebook lacks CUDA/PyTorch; Heavy is online but SSH authentication fails. Its dated
inventory lists an RTX 5090; Light is offline. Bounded synthetic throughput probe
prepared, syntax/negative-path checks passed. GPU timings and learning-curve
calibration remain blocked on access. See calibration/findings.md and discovery receipts.

## RunPod follow-up — October 29

Theo authorized paid RunPod calibration using the existing key. One secure A40
($0.59/hour quoted) replaced unavailable community capacity; cleanup is bounded
by a 30-minute local timer. Measured eager BF16 throughput and three-seed short
FineWeb learning curves; longer 3000-step GPT2 trials are in progress. See the
calibration project for final receipts rather than inferring status from this note.
The project now has a local Governor/Flywheel graph; operator is sole writer until
transfer to the future run scribe. No autonomous research campaign launched.

## Calibration completed — October 29

Results: /home/theo/research-projects/nanogpt-evaluation/calibration/results.md.
124M/3000-step trials:245.69–246.91seconds; final mean loss5.93979 (sample SD0.03835).
All three first sampled loss6.1 passes occur by205.77seconds on the development
prefix. This is a post-calibration candidate target, not a frozen acceptance rule.
Owned RunPod terminated and absent; other pods unchanged. Estimated GPU rent$0.190,
excluding disk/other charges. Learning curves, raw timings, source/data hashes and
local graph retained. Next: freeze independent validation/seed/timing rules and charter.
