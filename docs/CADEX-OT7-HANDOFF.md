# Cadex ot7 — operator handoff

Verified against source: 2026-09-19, approximately 17:36 UTC.
This is a point-in-time operational note, not a claim that the live run has finished.

## Where the run stands

- Target repository: `theo-kirby/cadex`, branch `ouroboros/ot7`.
- Resumed September 19 at 17:29:36 UTC, iteration 152, on the existing branch.
  Actor, critic and reporter select `claude-opus-5`; no alternate-model fallback.
- Owner authorized a fresh 48-hour budget. Config now uses `after: 48h` and
  `until: null`, giving an expected deadline of September 21 at 17:29:36 UTC.
  Two accepted done verdicts can end it earlier; other configured stops remain.
- The loop and Pushover watcher were both confirmed alive after this restart.
  The actor is handling Robin's failed reopen. Do not edit its working files
  or restart it simply because this note lists unresolved bugs.
- The charter remains the authority for experiment scope. F4 (repair) and F5
  (arm) are exhausted with measured shortcomings; do not reopen them. F6
  (Robin balancer) is next, then F7 (Plover biped), then final verification
  and the closing report. No actor edits to project designs are allowed.

Cadex commits worth starting from:

- `bcfa6914`: owner-directed Fable restart, ADR-383.
- `5a639c08`: Opus switch and explicit resume model override, ADR-384.
- `e47a2899`: iteration 151's completed reconciliation.
- `e3d77f69`: relative-budget recovery, ADR-385.
- `6a7c9819`: September 19 run-start directive.

These are on [Cadex's run branch](https://github.com/theo-kirby/cadex/tree/ouroboros/ot7).
The run directory is local and gitignored: `.ouroboros/runs/ot7/` in Cadex.
Use `ouroboros status`, `loop.log`, `iterations.jsonl`, transcript files and
`reporter.json` there for fresh facts. Project receipts live outside the
checkout in the operator's `cadex-projects` directory.

## What actually happened

Robin's first complete design turn ran on Fable in iteration 148, on project
`ot7-robin-c`. It passed all 276 static fit checks and used catalog hardware
for every purchased part. Swept coverage was incomplete because both wheel
joints lacked limits; smoke had not run. Its create prompt was spent and
three continuations remained. Plover was not attempted.

Fable then ran out of usage. A subsequent account refresh passed a small
availability probe but the actual actor call was refused for exhausted
credits. The owner switched everything to Opus. The Opus restart completed
one reconciliation, then crashed before dispatching a design continuation.

On September 19, the relative-budget recovery and Opus window probe passed
(1% five-hour and 1% weekly usage). Iteration 152 attempted Robin continue-1
with the new explicit model override. The product CLI exited 1 in about six
seconds before a model session began:

> The restore pass digest does not match the accepted digest.

The accepted revision was not changed by the operator. The live actor is
investigating; no successful Opus design result is established by this note.

## Runner defects and follow-up work

### 1. Absolute deadline crashes the outer loop — still unfixed here

`BudgetClock.should_stop()` in `src/ouroboros/budget.py` compares naive
`datetime.fromtimestamp(self.now())` against `StopConfig.until_dt`. An ISO
value with `+00:00` produces an aware datetime and raises:

```
TypeError: can't compare offset-naive and offset-aware datetimes
```

The configured value was `2026-09-19T14:53:10+00:00`. The traceback was visible
in the tmux pane, not recorded as an engine_error: `Engine.run()` calls
`budget.should_stop()` outside the step's exception handler. The loop died
September 17 at about 16:25:42 UTC, leaving status `idle`, verdict `continue`.

Cadex now avoids this path with `until: null`; this is a configuration
workaround, not a fix to Ouroboros. Follow-up tests should cover Z/offset and
legacy naive deadlines, before/at expiry, and failure handling in the outer
stop check. Define the naive-time convention explicitly rather than silently
changing existing local-time behavior.

### 2. Notifier can exit on stale terminal status during a restart

Both September 17 restarts produced a watcher pane saying "the run is over
and reported; exiting" while the new loop was starting. Manually starting
`ouroboros watch` after confirming live loop status worked.

This was observed with the existing startup-ordering fixes already present
on this branch (including `1030b77` and `9b8dcc6`). Investigate a resumed run
with an already-primed cursor and `terminal_sent=true`: `Watcher.check()`'s
startup guard is conditional on an unprimed cursor, while its terminal exit
uses the saved terminal flag. Do not assume the existing test coverage
reproduces the old-cursor case.

Once restarted, the notifier did report the later loop crash at 12:25:52
Eastern on September 17 and kept sending four-hour reports through September
19. Pushover send logs establish service-reported sends, not phone display.
The September 19 recovery refreshed the watcher after the loop was live.

### 3. Waiting and refresh behavior deserve separate treatment

Owner-directed waiting still consumed actor and critic calls and triggered
no-frontier model rotation before rotation was disabled. A runner-level wait
should not repeatedly ask models to confirm that capacity is unavailable.

`ouroboros refresh` correctly wakes backoff and probes the current account,
but a tiny successful probe does not guarantee an actual Fable call will
succeed. Usage deltas across an account change are not consumption by this
run. Preserve the new account snapshot without presenting the jump as spend.

### 4. Robin's pre-model failure was counted as a completed turn — Cadex-side

At this snapshot, `ot7-robin-c/evidence/attempt.json` says `status=failed`,
`slots_spent=2`; its second row says `status=completed`, model Opus, exit 1,
with no void/interruption classification. `remaining` reports no next prompt
because the attempt is closed. The CLI envelope instead shows a reopen
failure, empty session id and no accepted revision.

This is not evidence that a product model completed a continuation. The
Cadex collector currently marks an ordinary non-limit, non-interruption
failure completed before closing the attempt. The live actor must preserve
the raw failure and establish a charter-consistent slot ruling; do not
silently rewrite the receipt or infer that all continuation prompts ran.
Investigate deterministic project restore separately from provider access.

## Verification and safe continuation

The last code-change baseline in Cadex passed 832 CLI tests with 1 skip;
the preceding engine baseline passed 2169 with 53 skips. The September 19
change was configuration and documentation only. Its actual installed
BudgetClock was exercised at startup, at 48h minus one second, and exactly
48h; it returned no stop, no stop, then the expected wall-clock stop. Graph
export/check passed. The general absolute-deadline bug remains reproducible
in the external runner code.

Before declaring a launch healthy, check the active model, actual product
receipt, live watcher and a stop-condition evaluation—not just a live PID.
Keep run commits intact: Cadex graph records cite SHAs, so never squash this
run when it is eventually reviewed and merged.
