# Experimental research topologies

The opt-in `python -m ouroboros.experiment` adapter supports the chromatin system
screen. The production `ouroboros run` engine and its default three-role contract
are unchanged. The adapter has its own pinned prompt skill and strict JSON turn
protocol. Its standard arm reproduces the three-role architecture, not every
production retry/revert/exhaustion behavior; use this adapter for all arms in a
batch, and do not pool its control with historical production runs.

## Execution shapes

| topology | Dispatch order | Completion judgment | Scientific memory writer |
|---|---|---|---|
| standard | actor → critic → scribe | critic, then successful scribe turn | scribe |
| planner | planner → actor → critic → scribe, every cycle | critic, then successful scribe turn | scribe |
| freeflow | one persistent researcher | self-review, explicitly labeled | researcher |
| peers | persistent peer_a ↔ peer_b, no specialized roles | whichever peer claims done | active peer only |
| blind | N private solver_a turns, N private solver_b turns, then standard cycles | shared critic, then scribe | active solver in its private store; shared scribe |

Planner and critic use read-only tools and never resume a writable session. The
planner recommends one next action before every actor turn, including the first;
its completion recommendation does not stop the run. Freeflow and peers have no
model-backed reviewer, planner, scribe or reporter. The renderer is deterministic.
Blind solvers start from separately prepared workspaces with the same charter and
hash-pinned inputs; neither receives another solver's reports/handoffs. First-pass
changed files and their hashes are preserved for shared synthesis. Sessions cannot
silently disappear, change IDs or be reused by another persistent identity.

Every identity uses the same configured model/harness, without silent model
fallback. Guidance strings may differ by declared treatment. A peer handoff is
explicit text plus shared files/memory, not private chain-of-thought transfer.
Canonical memory still uses `ouroboros mem`; active generalists receive the
existing scribe permission principal, not new verbs. Identity and permission
principal are distinct. The deterministic host journal preserves every call even
if an agent fails to record scientific memory; a journal entry is not a scientific
acceptance or a synthesized finding.

## Running and configuration

Prepare a fresh Git research workspace, `.ouroboros/goal.md`, and the selected
local memory backend. Put the private control directory outside every research
workspace. For blind runs, prepare two additional independent Git workspaces,
including actual input bytes, before launch. Shared/non-nested paths are checked;
when input_manifest is configured, all workspaces must reproduce its hashes.

```yaml
run: chromatin-control
model: gpt-6.1-sol
harness: codex
topology: standard
memory: files
input_manifest: inputs.json
after_seconds: 7200
timeout_seconds: 1200
max_calls: 60
max_cycles: 60
max_tokens: 3000000
max_failures: 3
memory_bytes: 8589934592
confinement: landlock
guidance: {}
profile: research          # optional: research | development | freeform (default)
publication:               # optional: production's keys, DESIGN.md section 23
  template: auto
  figures: { enabled: true, main_max: 5, panels: true, supplementary: true }
  consolidate_every: null  # cycles between mid-run report drafts here; null: only the closing pass
  final: true
```

These budgets are example calibration candidates, not a frozen campaign contract.
Unknown fields are rejected, inside `publication` too. `profile` gives the actor
its section of the `ouroboros-profile` skill, and the critic and planner the critic's
section. Generalists (researcher, peers, solvers) get both. Under a `paper` or
`product` template the writers (scribe, generalists) keep the notebook
(story.html) and the figure ledger every call, and write the report (paper.html)
under the `ouroboros-publish` rules; a host-side check of the report feeds its
findings to the next report pass. The report pass happens inside existing calls: the scribe after a
critic's done, writer calls when the calls, cycles or time left fall below one
cycle or two call timeouts, and a generalist turn that claims done. It never adds a
call. A notebook arm's prompt is unchanged by these keys. The fixed prompt leaves
data gathering to the charter. A before-every-actor planner uses topology: planner;
blind additionally uses blind_calls and --private-a/--private-b. Guidance is a map
of identity to fixed appended instructions.

```sh
uv run python -m ouroboros.experiment \
  --config /path/to/frozen-config.yml \
  --repo /path/to/agent-workspace \
  --control /path/to/private-control
```

Landlock ABI3+ on Linux is the default, with no unrestricted fallback. The wrapper
confines the CLI and tool descendants to the current workspace, a private empty
identity home, and allowlisted runtime paths. Planner/critic research workspaces
are read-only. The wrapper applies one CPU's affinity and an inherited per-process
address-space limit, and sets common numerical libraries to one thread. Use the
existing outer run cgroup for whole-run memory/CPU enforcement and descendant
cleanup: per-process address limits are not aggregate cgroup memory limits.
Optional read_paths can expose additional runtime dependencies; paths overlapping
private control or another workspace are refused. Landlock does not restrict
network access. No model-key or host-auth file is copied, linked or made visible.
A real authenticated model smoke test requires a separately approved credential
boundary/broker; it was not performed by credential-free validation. A runnable
configuration file alone is not an authenticated-launch readiness receipt.

The explicitly named --allow-unconfined option permits confinement: none for local
fixtures; it is not a valid blind-evaluation isolation receipt. The production
headless backend only prepends a command when the adapter sets its ContextVar;
the default path remains unchanged. The experimental prefix refuses tmux backend
execution, so process confinement cannot silently disappear in another backend.

## Evidence, limits and recovery

The control directory holds a configuration/charter/prompt/source fingerprint,
atomic checkpoint, prompts, raw driver transcripts, durable result receipts,
identity sessions, all-agent accounting, native status and view/index.html. Agents
author story.html (the notebook), paper.html (the report), summary.txt and figures in their workspace's run-view directory;
the host copies these to the dashboard without an extra model call. Unknown token
and cost reports are counted explicitly; if an active cap needs a missing meter,
the run stops instead of treating unknown spending as zero.

The call ceiling is a CLI-dispatch ceiling. Token/cost/CPU caps are checked between
calls from reported usage; they can overshoot by the final call and are not a hard
provider-side token limit. Call timeout is clamped to the original remaining wall
time. Descendant CPU time is receipted through getrusage; RSS is the cumulative
child high-water value, not an isolated per-call or whole-cgroup measurement.
The run's original start/deadline/counters survive restart. Model-internal retries
and tools must be audited through raw transcripts; dispatch count is not API
request count. No extra agents are authorized by the role prompts.

Only one process may own a run. The runner writes pending-call state before
invocation and a durable receipt before applying the outcome. A crash after a
receipt is recovered without calling the model again. A pending call without a
receipt has unknown work/spend and stops as interrupted_inflight; it is never
silently replayed or recharged as zero. SIGTERM/Ctrl-C kill owned active harness
processes. Use a new run identity for a new trial, never edit checkpoints to reset
usage or deadlines. Configuration, charter, prompt or implementation drift refuses
resume. Configured input changes stop before another call.

This adapter fails closed on integrity failures and bounded repeated call failures;
it does not inherit production's indefinite retry behavior. Failed work remains
on the experiment branch for correction and audit, without automatic reverts.
Completion is a recorded claim. It does not imply a passing numerical gate,
independent scientific verification or measured human comprehension. Evaluate the
actual final report and code externally for every topology.
