# Ouroboros — design

**One line:** Ouroboros is a loop runner for agent harnesses. You give it a goal.
It runs Claude Code, Codex, or Pi again and again, never blocks on a question,
never believes "done" too early, and leaves a clean memory trail for the check-in.

Status: every phase of section 18 is built and tested (104 tests). Sections 1–5
are the intent; sections 6–17 describe what is built and are kept true to the
code; section 20 is the charter/plan layer; section 21 is what the first real
night (cadex nt1, 2026-09-05) taught and what changed because of it. When code
and this file disagree, fix this file in the same commit.

---

## 1. The problem

You write a long prompt. You go to bed. One of three things happens:

1. The agent asks a question after 5 minutes. It waits 10 hours.
2. The agent says "done" after 20 minutes. It read the goal wrong.
3. The agent crashes, hits a rate limit, or runs out of context. It stops.

Ouroboros makes all three impossible. The loop continues at all costs.

## 2. Non-negotiables

These rules win over every other rule in this document.

1. **Never block.** No step of the loop can wait for a human.
2. **Never stop early.** Only a stop condition you configured can end a run.
3. **Always leave memory.** Every iteration writes something the next one reads.
4. **Always commit.** No iteration ends with work only in the working tree.
5. **Harness-agnostic.** Claude Code, Codex, and Pi are equal citizens.

## 3. Shape of the deliverable

One `uv` tool plus a small set of skills.

```
uv tool install --editable .  # from this repo; edits are live for new processes
ouroboros                    # ASCII banner and the menu: init / design / run / monitor
ouroboros skills install      # → ./.claude/skills, ./.agents/skills, ./.pi/skills
                              #   --user → ~/.claude, ~/.codex, ~/.pi/agent (those present) and ~/.agents
ouroboros init                # → .ouroboros/config.yml + goal.md (the charter)
ouroboros design              # the charter interview, in claude, codex, or pi (--harness)
ouroboros run --for 10h       # preflight (git, charter filled in, logins), then the configured launcher: a systemd unit, a tern session, or tmux
ouroboros status --watch      # the live TUI (also `ouroboros top`); --plain for the text loop
ouroboros stop                # SIGTERM the loop and its children, then the tmux window, tern session or unit it took
ouroboros report              # REPORT.md: verdicts, decisions, reverts, cost
```

A second `ouroboros run` with the same run name continues the run: iteration
numbers, the ok tag, and the branch carry over.

With no arguments, the CLI prints the ASCII serpent with `OUROBOROS` centered
inside the coil, followed by `an infinite loop` with a blank line before and
after the tagline, then a numbered menu: `1. init`, `2. design`, `3. run`,
`4. monitor`: the first-run path in order. In an interactive terminal, entering a
number runs that command (`4` is `status --watch`). Only the options are shown, with no selection
prompt; invalid selections retry silently. Ctrl-C or EOF
exits the menu cleanly; Ctrl-C also exits the selected monitor without stopping
the agent run. When stdin or stdout is not a terminal, the CLI prints the menu
and exits without reading input. Use `ouroboros --help` for command help.
The banner prints once as static text, without delays or terminal control
sequences, in both terminals and pipes.
The artwork lives in `src/ouroboros/banner.py`, which can also be run directly
with Python to print the same artwork and description. Explicit commands and flags keep their
normal output without the banner.

Python 3.12+, `uv`, no daemon, no database. All state is files in the repo.

**Preflight** runs before `run` hands the loop to its launcher, so its messages land in the
human's terminal: a git repo with a commit and a clean tree; a charter that is
not the `init` template any more (mission, at least one done criterion, a
ladder rung: `goal.unfilled`); and a login check per harness in the config
(`claude auth status --json`, `codex login status`, `pi auth check --provider
<default>`). A role whose first harness is out but whose fallback is in starts
with a note; a role with no logged-in harness refuses to start. After start the
loop never asks again: an auth failure mid-run is NEEDS_HUMAN plus a retry every
ten minutes.

## 4. Decisions already made

| Question | Decision |
|---|---|
| How to drive a harness | Headless subprocess by default. A tmux TUI driver is a second backend. |
| Memory between iterations | One record/state model: a governor graph when `.flywheel/db.sqlite` exists, else the same model as files in `.ouroboros/graph/`. |
| Who judges the change, answers questions, and writes the next prompt | One critic agent, read-only, every iteration. Rules as fallback. |
| Git | Auto-commit every iteration on `ouroboros/<run-name>`. |

## 5. Architecture

```
ouroboros run
 └── Loop engine            .................. the state machine (section 6)
      ├── Harness pool       .................. one chain per role, one limit board per run (section 9)
      │    ├── Driver        .................. claude | codex | pi
      │    └── Backend       .................. headless | tmux
      ├── Memory (ouroboros mem) ............. governor | files  (section 7)
      │    └── Charter parser ................. goal.md → gaps, ladder, policies (section 20)
      ├── Critic             .................. grades the diff, answers, judges done, writes the next prompt (section 8)
      ├── Scribe             .................. records the unit, folds the memory, writes the story (section 10)
      ├── View               .................. the run's live page, view/index.html (section 15)
      ├── Git guard          .................. branch, commit, tag, revert (section 11)
      ├── Budget clock       .................. time, iterations, cost (section 12)
      └── Recorder           .................. JSONL log, status, report (section 15)
```

Every box is a Python module with one job. Every box talks to the others
through plain data classes, never through a harness-specific type.

## 6. The iteration (state machine)

One iteration is one pass through this machine. Every step is one headless
call or one local command. Every step has a timeout. A timeout is not a
failure; it is a recorded event and the loop continues.

```
     ┌──────────────────────────────────────────────────────────────────────┐
     │                                                                      │
     ▼                                                                      │
  ORIENT ──► WORK ──► REPORT ──► COMMIT ──► CRITIC ──► (REVERT) ──► SCRIBE ──► COMMIT
```

| Step | What happens | Who runs it |
|---|---|---|
| **ORIENT** | Read the charter, the memory (`ouroboros mem orient` and `show`: the frontier, the unfolded tail, the architecture, the recent records), the critic's message. Pick the next unit from the critic's steer, a frontier claim, or the charter's horizon ladder. | actor (headless call) |
| **WORK** | Do one bounded unit of work. Budget: N units (default 1). | actor (same call) |
| **REPORT** | End with the report: `## What / ## Why / ## Method / ## Result / ## Claims`. The actor records nothing itself. | actor (same call) |
| **COMMIT** | Git guard commits whatever changed on the run branch, subject from the report's `## What`. No change, no commit. | ouroboros (local) |
| **CRITIC** | Every iteration, changed or not. Reads the diff, the report, the frontier, and the loop's signals, with read-only tools. Grades the change, answers a question as the user would, refuses a false "done", names a loop, and writes the message the next iteration starts from. | critic (headless call) |
| **REVERT** | On `reject` (with `git.revert_on_reject` and an accepted iteration behind it): revert commits that undo this iteration's commits only, back to the head the actor started from. | ouroboros (local) |
| **SCRIBE** | Every iteration, after the verdict. Fixes what the critic's `fix_first` names, folds the tail when a fold is due, records the unit as one record (a reverted one as a dead end; the verdict is in its `## Result`), checks the memory, and updates the run's story when there is something new. | scribe (headless call) |
| **COMMIT** | The memory the scribe wrote (`ouroboros #<n>: record — <title>`). An accepted iteration's `ok` tag goes on this commit. | ouroboros (local) |

**The actor reports; the scribe records.** A record written after the verdict
can say what the verdict was, and a record committed after the revert is never
undone by it. When the scribe writes no record (it failed, or `scribe: rules`),
the loop writes one itself from the report, with `none: the scribe did not
record this iteration`, so no iteration is ever missing from the memory. A fold is
due after `reconcile_every` worked iterations, at `pressure` unfolded records, or
at once after a directive that declared gaps; the scribe folds before it records,
so the claims its record names exist. The actor never spends an iteration on it.

**Fresh context every iteration.** Each call is a new session. The agent reads
memory from disk. Optional: `resume_for: N` keeps one session for N iterations,
then resets. Default is 1 (always fresh). A session is only resumed on the
harness that minted it.

**The actor's last message is data.** The critic reads it. It does not reach
a human. Any question in it gets answered by the critic and injected as the
first line of the next prompt.

**A failed iteration is quiet.** When the actor call fails (after the retry
policy of section 12), no work is committed, the fold counter does not tick, the
scribe records the failure, and the error rides into the next prompt. Repeated
failures back off 1m, 2m, 5m, 10m.

**A stuck iteration is expensive.** `stuck` means the opposite of a failure: the
actor ran fine and changed nothing, because it is waiting on a clock, a quota,
or an instruction it cannot act on. Nothing about repeating it immediately makes
it truer, and every repeat still pays for a critic call. So a run of `stuck`
verdicts backs off on the same table --
1m, 2m, 5m, 10m, then 10m forever -- and `stop.max_stuck` (25 by default) ends a
run that is not coming back. Any verdict other than `stuck` clears the streak.
The nt3 run on cadex is why this exists: 113 consecutive stuck iterations, at
roughly one every 15 seconds, waiting for a wall-clock boundary the planner had
written into the plan.

### 6b. Motion is not progress

`stuck` measures the filesystem: an iteration that produced no diff. That is the
cheap half of the problem. The expensive half is a loop that keeps working and
ships nothing — records about records, audits of what was already audited, plans
that restate the last plan, handoffs from the actor to a role that is the same
agent under a different prompt. All of those write files, so a diff-based signal
reads them as progress and the overseer says `continue`.

The same nt3 run is the worked example on both counts: 201 iterations, 22,437
lines, 128 record nodes, and **one** node moved on the frontier.

So `loops.py` measures the other thing. Two counters, all mechanical, because
an agent asked "are you looping?" always finds a reason why this time is
different:

| counter | what it counts | default |
|---|---|---|
| `no_product` | iterations whose diff touched only `.ouroboros/`, `.flywheel/`, `.governor/`, `STATE.md`, `PLAN.md` | 8 |
| `no_frontier` | iterations after which the memory's frontier digest was unchanged | 30 |

Each resets the moment the thing it measures happens. A memory adapter with no
frontier returns `None` and switches that counter off: unknown is not the same
as unmoved. The numbers reach the overseer in its signal block, and the overseer
may disagree with them — but the ladder below runs on the numbers regardless.

**The ladder, and why it has no third step.**

1. **Name it.** The evidence goes into the actor's next prompt: the streak, and
   the subjects of the commits that made it.
2. **Change the model.** The actor's harness is blocked on the shared
   `LimitBoard`, so its pool falls back and the board un-blocks it later on its
   own cooldown.

There is no step 3. A loop is a thing to break out of, not a thing to die of, so
none of this can stop a run: a false positive costs one wasted prompt, never a
run nobody is awake for. Step 1 is free and repeats every iteration; step 2
costs a model switch, so it fires once per `escalate_every` iterations rather
than on every one -- without that gate, nt3's 157-iteration frontier streak
would have rotated the harness 157 times. (Until 2026-10-17 a middle step forced
a planner pass and banned the current bet; it went with the planner, section 20.)

**Replayed against the real nt3 log**, each counter alone would first have fired
at iteration **#50** (`no_product`, one iteration into the stall) and **#74**
(`no_frontier`), against zero firings in the run's two productive stretches.
`no_product` is the signal that earns its place. A third counter, `repeat_bet`
(planner bets restating each other, first firing #118), went with the planner.

## 7. Memory: one model, two stores

Every run keeps the same memory model, governor's (its `docs/CONVENTIONS.md`):
append-only **records**, each declaring what it changes in state (`--impact
"claim: Δ"`, `--new "name: Δ"`, or `--none "why"`), and **state claims** that cite
the records they rest on. A **fold** is the single writer of state: it applies the
unfolded tail, tags it folded, and renders STATE.md. Only where the model is kept
differs:

| `memory:` | Store | Where | In git? |
|---|---|---|---|
| `governor` | `memory/governor.py`, the `governor` CLI | the Flywheel repository, `.flywheel/` | no; only STATE.md |
| `files` | `memory/files.py`, Python, no dependency | `.ouroboros/graph/` (records, claims, folds, STATE.md) | yes, on the run branch |

`auto` picks `governor` when `.flywheel/db.sqlite` is at the checkout or its main
checkout and `governor` is on `PATH`, else `files`. `handoff`, the old name of the
fallback, reads as `files`. Ouroboros does not run `flywheel repo:init` or
`governor init` for you.

### 7a. `ouroboros mem`: the only surface an agent sees

The agent never learns which store it has. Every prompt names one command, and
the command does whatever the store needs (`memcli.py`):

| Verb | Governor store | Files store |
|---|---|---|
| `orient` | `governor orient` | the same text, rendered in Python (`graph.orient_text`) |
| `show <id>` | the node from `governor snapshot` | the node file |
| `record --title … (--impact \| --new \| --none)` | `governor record --json`; parent defaults to the newest record | writes `records/r-NNNN.md`; same default |
| `fold` | the tail with its impacts (shared code) | the same |
| `fold --apply FILE` | `governor fold --apply`, then `governor render -o STATE.md` | validates like governor, writes `claims/`, logs `folds/f-NNNN.json`, renders |
| `check` | `governor check --json` | the same rules (below) in Python |

`run` sets `OUROBOROS_MEMORY` for the harnesses it starts, so their `ouroboros mem`
acts on the run's store even when `--memory` overrode the config. The prompts are
skills written once for both stores: `ouroboros-orient` (the actor's view of the
memory), `ouroboros-scribe` (recording) and `ouroboros-fold` (the fold procedure). The loop logic is written once too: `graph.GraphMemory`
holds the directive, the reconcile cadence, the frontier fingerprint, the prompts
and the critic's context, over either store's snapshot in governor's snapshot
shape. A difference between the stores is a failing test, not a second prompt.

No role runs git for the memory: the loop's commit after the scribe (`git add -A`)
carries STATE.md and, in a files repo, the memory files.

### 7b. The rules, on both stores

- The run branch `ouroboros/<run>` is the **single-writer branch for the run**.
  Only the scribe writes the memory, and only its fold writes claims. The actor and
  the critic read it; `ouroboros mem` refuses them any other verb.
- Fold triggers: every `graph.reconcile_every` iterations with changes, at
  `graph.pressure` unfolded records, or at once after a directive that declared gaps.
- Every iteration gets exactly one record: the scribe's, or the loop's fallback.
- The memory's check runs after every scribe. A failing check does not stop the
  loop. Its violations go to the next scribe as the first thing to fix, and to the
  next critic as a signal.
- The charter is recorded once per version, at run start, as an
  operator-directive record titled `Ouroboros run: <run> [<sha256[:8]>]`, with one
  `--new gap-*` per open done criterion. A re-versioned charter gets a new
  directive whose parent is the previous one. Every record of the run descends
  from a directive. The engine commits what the start wrote before iteration 1.
- No plan component. An older governor graph's `plan`-kind claims stay where they
  are: the tail marks their impacts `(plan: legacy, leave it)`, and the fold lists
  those records in `folded` but leaves the claims out.
- The frontier fingerprint is every state claim's status, from the snapshot. A
  files repo has a frontier too, so the `no_frontier` loop signal works there.

### 7c. The files store

```
.ouroboros/graph/
  records/r-0007.md     frontmatter (id, kind, title, parents, at, node_id, digest) + body
  claims/<name>.md      frontmatter (id, kind, title, status, parents, seq, node_id, digest) + body
  folds/f-0003.json     {at, folded, claims, created}
  STATE.md              rendered; never edited by hand
  README.md             what the files are
```

The idea is flywheel-local's (one markdown file per node, canonical frontmatter in
a fixed key order, a doctor), without its snapshot store: git versions the files
and the run branch carries them next to the code. The bodies are exactly what a
governor node holds in `content`: a record's `## What / ## Why / ## Method /
## Result` and the generated `## Recorded` (at, branch, commit) and `## State
Impact`; a claim's `## Current / ## Negative knowledge / ## Evidence`. A uuid
`node_id` per node lets a later import into a Flywheel graph run twice without
duplicating anything; record ids (`r-0007`) and claim names map to Flywheel slugs
at that import. The importer is not built yet; the format is what makes it simple.

`check` keeps governor's rules: G1 every file parses, its id matches its name, its
kind is right; G2 a record's body matches its `digest` (append-only); G3 every
record declares an impact; G4 every claim but a component has one status and cites
a record; G5 impacts name claims and citations name records; G6 lineage stays
within a kind, with no cycles; G7 a claim's body matches its `digest`, which only
a fold writes. Where governor reads G2 and G7 from Flywheel's commit log, this
store reads them from the digests. A tail over 3 is a warning.

While the files memory is young (three records or fewer), the orient text carries
the last `.ouroboros/handoff/NNNN.md` from the memory it replaced, read-only.

## 8. The critic

The critic is the reviewer and the sleeping user in one. It used to be two
roles, and each was half blind: the critic graded the diff without seeing the
plan, the overseer steered from the plan without seeing the diff. nt3 ran 200
iterations and closed no criterion because every role agreed with every other.
Both are judgment jobs over the same evidence, so they are one read-only call
now, after every actor turn, changed or not. It gets:

- the charter (the user's voice),
- what is true now: the frontier of open, broken and blocked claims, and the
  unfolded count,
- the actor's final message, with its report,
- the loop's signals: changed, no-change streak, error streak, the loop
  detector's counters, a missing report, the memory checker's last complaint,
- the git diff stat and the diff itself (truncated), with read-only tools to
  open what the diff touches,
- the last 20 decisions (so it does not flip-flop, and so it can see a loop).

"Done" is judged against the frontier, never against the checklist in the
charter (section 21 says why).

It returns strict JSON:

```json
{ "verdict": "continue | answer | done_rejected | done_accepted | stuck | looping | reject",
  "reply": "text injected into the next prompt: the fix, the answer, or the next unit and why",
  "reason": "one sentence for the log",
  "did": "one sentence for the monitor: the unit that just finished",
  "doing": "one sentence for the monitor: the unit the next iteration starts",
  "fix_first": "what the scribe must fix in the memory before it records; empty when the memory is clean" }
```

Triggers, in order:

| Signal | Critic action |
|---|---|
| The diff has a real defect against the quality bar | `reject`. The loop reverts this iteration's commits (when `git.revert_on_reject` is on; else the commit stays and gets no ok tag) and `reply` says what to fix. Style is not a defect; a wrong reject throws away good work. |
| Ends with a question | `answer` it using the goal doc's **question policy**. Inject the answer. |
| Claims "done" or "complete" | Check against the frontier. Usually `done_rejected` + "here is what is still open". |
| No file changes for 3 iterations | `stuck`. Inject a nudge from the **exhaustion policy**. |
| Files change but nothing moves (section 6b) | `looping`. Name the loop with its evidence, then change the model. |
| Same error 3 times in a row | `reject`: "that path is dead, record it as a dead end". |
| Anything else | `continue`, with `reply` naming the next unit when it is not obvious. |

**The critic writes the plan one sentence at a time.** There is no planner.
The highest open gap or the next rung of the ladder: the critic picks and says why in the `reply`. When the frontier runs
dry it proposes the next direction from the mission.

**The one that judges never writes.** The critic is trustworthy because it has
no stake in the branch. A memory fix it wants -- a record that does not link, a
wrong impact, a stale frontier -- goes in `fix_first`, and the scribe, which runs
right after, does it before it records. It may run `ouroboros mem orient`, `show`
and `check`; nothing else in the memory.

**Rules fallback.** If the critic call fails, times out, or returns bad JSON
twice, a deterministic critic answers from the signals alone: reject on three
identical errors, stuck on three unchanged iterations, looping when the detector
fired, done_rejected on a done claim, answer on a question, otherwise continue.
`critic: rules` in the config runs only this, with no model call at all.

The loop never waits on the critic either.

**Done is never trusted from the actor.** Only the critic can say
`done_accepted`, and only when the goal doc's done criteria are met. Even then,
the goal doc's **exhaustion policy** decides what happens next (section 13).

## 9. Harness drivers

One driver per harness. Each driver implements:

```python
class Harness(Protocol):
    name: str
    def run(self, prompt: str, *, cwd, timeout, resume: str | None,
            model: str | None, system_append: str | None) -> Result
    # Result: text, session_id, cost_usd | None, turns, exit_code, raw_json_path
```

### Headless backend (default)

| | Claude Code | Codex | Pi |
|---|---|---|---|
| Command | `claude -p --output-format json` | `codex exec --json` | `pi -p --mode json` |
| Prompt | positional / stdin | positional | positional (`--` first) |
| Permissions | `--dangerously-skip-permissions` | `--dangerously-bypass-approvals-and-sandbox` | none needed |
| Resume | `--resume <id>` | `codex exec resume <id>` | `--session-id <id>` |
| System append | `--append-system-prompt` | `-c` config or prompt prefix | `--append-system-prompt` |
| Last message | JSON `result` | `-o <file>` | JSON stream, last assistant |
| Cost | `total_cost_usd` in JSON | not reported → estimate from tokens | not reported → estimate |
| Skills path | `.claude/skills/` | `.agents/skills/` (verify) | `--skill <dir>` per call |

Ouroboros runs each harness with `--dangerously-…` flags. That is the point.
The run branch and the critic are the safety net, not the permission prompt.

### tmux backend (built)

`backend: tmux` in the config. Every harness call runs in its own window of the
run's tmux session (`ouroboros-<run>`), named after the transcript
(`0007-actor-0`), so a human who attaches sees the current call stream. Claude
runs with `--output-format stream-json --verbose` there; Codex and Pi already
stream JSONL. The call still lands as files under `transcripts/` (stdout,
stderr, exit code), so the drivers parse exactly what they parse headless.
The window closes when the call ends; a timeout kills the window. Without a
reachable session (a `--foreground` run, no tmux) calls fall back to headless.
`ouroboros stop` and SIGTERM kill active windows as well as child processes.


Starts the harness's normal TUI in a tmux pane. Sends the prompt with
`send-keys`. Polls `capture-pane`. Detects the idle prompt and question
dialogs with per-harness regexes. Types the critic's answer.

Use it only when a harness has a feature headless mode does not. It is
fragile. The design isolates it behind the same `Harness` protocol so the loop
engine never knows which backend ran.

### Mixed harnesses

Every role has its own harness and model:

```yaml
roles:
  actor:  { harness: claude, model: opus }
  critic: { harness: codex,  model: gpt-5-codex }
```

Different harnesses disagree in useful ways. A Codex critic on Claude work is
a real second opinion.

### Fallback chains (built)

Every role can carry an ordered list of fallbacks. The loop uses the first
harness that is not out of usage, and returns to the preferred one as soon as
its limit resets:

```yaml
roles:
  actor:
    harness: claude
    fallback:
      - { harness: codex, model: gpt-5-codex }
      - { harness: pi }
limits:
  cooldown: 30m          # block length when the harness does not say when it resets; doubles each repeat
  max_cooldown: 3h
  transient_strikes: 3   # this many 429/overloaded errors in a row count as a limit
```

Rules:

- One **limit board** per run, shared by every role, because limits belong to
  an account, not to a role. A limit seen by the critic blocks that harness
  for the actor too.
- A result is classified as `ok | timeout | limit | auth | transient | error`.
  A driver that knows the structured error (Codex `usage_limit_reached`, an
  epoch in `resets_at`) sets the kind itself; otherwise the message text decides.
- `limit` and `auth` block the harness on the board and the pool moves to the
  next entry in the same call. `transient` returns to the engine, which backs
  off and calls again on the same harness; the third strike in a row turns
  into a block.
- The block ends one minute after the parsed reset time. When no reset time is
  known, the cooldown doubles on every repeat up to `max_cooldown`.
- At each actor/agent-critic transition (both directions), probe each blocked
  provider once with a fresh CLI process, a 20-second timeout, an empty working
  directory, no tools, and a minimal availability prompt. This picks up a new
  login without restarting the run. Checks consume a small amount of usage and
  their reported cost/usage is included in the run ledger. Success clears the
  cooldown; configured reserves additionally require a fresh long-window reading
  below the reserve. Failures and missing reserve readings retain the original
  deadline. Deliberate loop-rotation blocks are not probed. Actor retries and
  rules-only critics do not trigger checks.
- When every entry is blocked, the engine sleeps until the earliest block ends
  (capped at 6 h), then tries again.
- Session ids are remembered per harness; a resume only happens on the harness
  that minted the session.
- `ouroboros status` shows `limited: claude for 112m (...)`.

**Codex structured output is strict.** `--output-schema` goes through OpenAI's
strict mode: every object needs `additionalProperties: false` and all of its
properties in `required`, or the API answers 400 `invalid_json_schema` before
the model runs. The driver rewrites the schema (`strict_schema`) on the way
out. Learned live on cadex nt2: the critic failed open and the overseer fell to
rules on every Codex call until this landed.

## 10. Roles

Three roles run every iteration, in this order:

| Role | Runs | `ouroboros mem` verbs |
|---|---|---|
| actor | every iteration; writes code and tests, ends with a report | `orient`, `show` |
| critic | every iteration; read-only; the whole of section 8 (`critic: rules` drops the model call) | `orient`, `show`, `check` |
| scribe | every iteration, after the verdict and any revert; records, folds when due, checks, writes the story (`scribe: rules` drops the model call; the loop then writes each record itself) | `orient`, `show`, `record`, `fold`, `check`, `render` |

The engine sets `OUROBOROS_ROLE` for each call (and carries it, with
`OUROBOROS_MEMORY`, into a tmux window); `ouroboros mem` refuses a verb outside the
role's set with exit 2. A person at a shell has every verb.

The scribe is book-keeping, not judgment: it does not grade, steer or work, so it
does not bring back what merging the overseer into the critic removed (every role
agreeing with every other). It writes only the memory and the view; it never runs
git. Its default chain is Claude then Codex, with a 20-minute timeout; a lighter
model does the job (`roles.scribe.model`).

A separate maintainer and planner existed until 2026-10-17. They were on for nt1
through ot4 and cost more than they moved: up to five model calls per iteration,
and a plan that restated itself (section 6b). They were removed on 2026-10-17
(section 20). The maintainer's fold is now the scribe's; the planner's next unit
is one sentence in the critic's reply. A config that still names `maintainer:`,
`planner:` or `plan:` loads; the keys are ignored.

The critic reads with read-only tools (`Read, Grep, Glob` for Claude, a
read-only sandbox for Codex, `read,grep,find,ls` for Pi), returns strict JSON,
and fails to rules, never open: a critic that crashes or returns garbage twice
is replaced by the deterministic one for that iteration.

```yaml
critic: agent
scribe: agent
roles:
  actor:  { harness: claude, fallback: [{ harness: codex }] }   # the default chain
  critic: { harness: codex, timeout: 10m, fallback: [{ harness: claude }] }
  scribe: { harness: claude, timeout: 20m, fallback: [{ harness: codex }] }
```

Not built, deliberately: `ping-pong` (alternate harnesses per iteration) and
custom pipelines. Fallback chains cover the first case; the second has no use
yet.

## 11. Git guard

- On start: create `ouroboros/<run-name>` from the current HEAD (or check it
  out when it exists). Refuse to run on a dirty tree unless `--allow-dirty`.
- After every WORK: `git add -A && git commit -m "ouroboros #<n>: <summary>"`,
  where the summary is the title of the newest record. **No change, no commit.** The actor may commit on its own; the loop
  counts a moved HEAD as change.
- After a `continue`, `answer`, `done_rejected`, or `done_accepted` verdict:
  tag `ouroboros/<run>/ok-<n>`. This is the revert target.
- After a `reject`: revert commits back to the
  last `ok-*` tag (never a history rewrite). The reverted diff is saved to
  `runs/<run>/reverted/<n>.patch` so the memory can cite it as a dead end.
- Reconcile and plan passes commit as `reconcile:` / `plan:` (the agent's own
  commit) or `ouroboros #<n>: reconcile|plan — ...` (the loop's catch-all).
  All catch-all commits skip an unchanged tree, including when the role already
  committed its work. Unchanged, unrecorded actor turns do not advance maintenance
  or planning counters or dispatch those passes.
- Never force-push. Never touch `main`. Never rebase. The merge after a run is
  yours. Merge with a merge commit: record nodes cite commit SHAs, so a squash
  or rebase dangles them.

## 12. Durations, budgets, and never stopping

Stop conditions. Any one of them ends the run cleanly. All are optional. With
none set, the loop runs until you kill it.

```yaml
stop:
  after: 10h              # wall clock
  max_iterations: 200
  max_cost_usd: 50        # API-equivalent dollars; see the note below
  max_usage: 0.8          # 0..1 of a subscription window; the meter for a subscription
  until: "2026-09-06T07:30"
  on_done_accepted: 3     # the critic accepted "done" this many times in a row
  max_stuck: 25           # consecutive `stuck` verdicts; null to never stop
```

**What "cost" means.** Claude Code prints `total_cost_usd` in its JSON result.
It computes that number from token counts at API list prices, even when you
are logged in with a subscription and nothing is billed per call. That number
is not money anyone pays, so nothing shows it: not the TUI, not `ouroboros
status`, not the report. It survives only as `stop.max_cost_usd`, for a run
whose harness really is billed per call.

**What a run actually costs is a slice of a rationing window.** That is what
the run strip, the status line and the report all report: `claude 7d 54% +19 ·
claude 5h 65% +52 · codex 7d 42% +36`, each window with where it sits and how
far this run moved it. Money appears beside them for one case only -- a harness
that reports no windows at all, meaning an API key rather than a subscription,
where the dollars are an invoice: `pi $2.50`. `BudgetClock.money` is that rule
in one line, and it is why cost is tracked per harness rather than as a total.

Subscription readings are recorded in call order, including failed attempts
before a fallback switches providers. An earlier actor reading must not overwrite
later critic readings. The TUI treats a window
whose reset time has passed as **reset · awaiting reading**, until the provider
reports a fresh window; it does not invent 0% or 100% from the clock or a limit error.

**A reserve stops a harness, not a run.** `stop.max_usage` ends the run when any
long window crosses a fraction. That is the right tool for a run that must not
overshoot, and the wrong one for a two-day run: the windows reset while it is
still going, so ending the run throws away the recovery. `limits.reserve`
(`{claude: 0.85}`) is the other half — the harness is blocked until that window
resets, exactly as a real usage limit blocks it, so the pool falls back and the
loop continues. It is also how you keep a long run from eating the whole 7-day
window you work out of yourself. Short windows never trip a reserve; they heal
on their own.

Resilience. These are not stop conditions. The loop absorbs them:

| Event | Response |
|---|---|
| Harness exits non-zero (a plain error) | Log it. Retry once after 5 s. Then give up the iteration: no commit, the error rides into the next prompt. |
| Timeout (`roles.<role>.timeout`, default 45 min) | Kill the process group (or the tmux window). Commit whatever landed. Continue. |
| Rate limit / 429 / "overloaded" | Backoff: 1m, 2m, 5m, 10m, then 10m forever. Never exit. Log every wait. Three in a row count as a usage limit. |
| Usage limit ("session limit · resets 2:50am (Europe/Madrid)", `usage_limit_reached`) | Switch to the next harness in the chain. With no chain, sleep until one minute past the reset (capped at 6 h) or a growing cooldown when the message has no time. |
| Auth expired | Block the harness on the limit board and use the next one. With no chain, backoff 10m and retry forever. Write `NEEDS_HUMAN.md` so the check-in shows it first. |
| Context exhausted mid-call | The call ends. The recorder notices no record. Next prompt starts with "record first". |
| Overseer returns `stuck` | Backoff: 1m, 2m, 5m, 10m, then 10m forever. The streak clears on any other verdict; `stop.max_stuck` in a row ends the run. |
| Engine error (git, disk, a bug in a step) | Logged, 60 s backoff, next iteration. The loop only exits on a stop condition, Ctrl-C, or SIGTERM. |
| Terminal dies (SIGHUP) | The logger falls back to file only and the loop keeps running. |
| SIGTERM / `ouroboros stop` / Ctrl-C | Every harness child (process group, tmux window) is killed; status says `killed`. |
| Ouroboros itself crashes | `ouroboros run` with the same run name continues: iteration numbers from `iterations.jsonl`, the ok tag from `status.json`, the branch as it is. The tmux session is named `ouroboros-<run>` so you can find it; `status` shows whether the pid is alive. |

## 13. The goal doc (the charter)

`.ouroboros/goal.md` is the **charter**: the one document the human owns and
no agent role edits (section 20). The `ouroboros-design` skill writes it
through an interview. Its sections are fixed; `goal.py` parses them:

```markdown
# Goal: <name>

## Mission            — one paragraph. what and why. priorities in order.
## Done criteria      — `- [ ]` claims about the world, not tasks. each open box
                        becomes an open gap on the frontier; a ticked box is a
                        criterion the human already accepts and declares no gap.
## Horizon ladder     — `- **short-term:**`, `- **medium-term:**`, `- **long-term:**`.
                        granularity, not time. the critic steers from it.
## Constraints        — never do X. always keep Y green. stay inside dir Z.
## Question policy    — how to decide for me. e.g. "prefer the reversible option".
## Exhaustion policy  — creative | maintain | report_done
   - creative:  the critic proposes three directions, picks one, continues
   - maintain:  tests, flakes, docs, refactors, forever
   - report_done: the critic may accept done; loop idles and polls every idle_interval
## Quality bar        — what the critic grades against (with Constraints).
## Reconcile          — graph memory only: prose for the reconcile pass.
```

The horizon ladder is the trick. A goal whose long-term rung holds standing
work never runs out. It is the only plan the loop has.

## 14. Skills

Installed by `ouroboros skills install`. Plain `SKILL.md` folders in the format
all three harnesses read: Claude Code invokes one as `/name`, Codex as `$name`,
Pi by discovery or `--skill <dir>`. `--user` copies them into every harness home
that exists (`~/.claude/skills`, `~/.codex/skills`, `~/.pi/agent/skills`) plus
`~/.agents/skills`. `ouroboros design` opens the interview in whichever harness
the actor chain names first, by skill name when installed and inline otherwise.

| Skill | What it does |
|---|---|
| `ouroboros-design` | The interview (built). Reads the repo first. Then asks, in rounds: mission, done criteria as claims, each rung of the horizon ladder, constraints, question policy, exhaustion policy, quality bar, run shape. Refuses to finish until every rung has at least 3 concrete items. Writes the charter `goal.md` and a matching `config.yml`. |
| `ouroboros-checkup` | The check-in (built), for a live run or a finished one. Blockers first, then what landed, **whether the loop is moving or going in circles** (section 6b), decisions the critic made for you, rejects and reverts, how the frontier moved, what to review hardest, then either a merge recommendation or the one correction worth making mid-run, and charter changes to consider. |
| `ouroboros-launch` | Starting one (built). Reads `RUNS.md` and the last digest's notes before anything else, pushes back on a charter criterion no single run can tick, runs `ouroboros preflight`, names the run and its stops, launches, and says what to watch. Most of what wastes a run is decided in the twenty minutes before it starts. |
| `ouroboros-actor` | Internal. The role prompt for WORK and REPORT. Includes `ouroboros-orient`, the memory as the actor reads it. |
| `ouroboros-critic` | Internal. The role prompt for CRITIC: the reviewer and the sleeping user. Returns strict JSON (section 8). |
| `ouroboros-scribe` | Internal. The role prompt for SCRIBE: fix, fold when due, record, check, write the story. |
| `ouroboros-fold` | Internal. The fold procedure, put into the scribe's prompt when a fold is due. |

Role prompts live as skills, not as Python strings, so you can edit them
without touching code and so a harness can load them natively.

## 15. Observability

```
.ouroboros/runs/<run>/
  run.yml                    # frozen config + start time + version
  pid                        # the loop's pid; status and stop read it
  loop.log                   # every step, backoff, harness switch, limit wait
  iterations.jsonl           # one line per step: actor | commit | critique | loop | revert | scribe | check
                             # (older runs: reconcile, plan, and `housekeeping` on a commit)
  critic.jsonl               # every decision the critic made for you (verdict, reason, reply, did, doing, fix_first)
                             # (runs from before the merge have overseer.jsonl and an `oversee` step; readers accept both)
  status.json                # state, iteration, harness, usage windows, elapsed, last verdict, limit blocks
  transcripts/NNNN-<role>[-attempt].json   # raw harness output (+ .stderr); roles: actor, critic, scribe (older runs: housekeeping, maintainer, planner)
  reverted/NNNN.patch        # what a revert threw away
  view/index.html            # the run's live page; live.json, summary.txt + story.html (the scribe's), assets/
  NEEDS_HUMAN.md             # only exists while something needs you (auth failure)
  REPORT.md                  # written by `ouroboros report`
  reporter.json              # where the reporter (`ouroboros watch`) is up to; reports/ holds its digests
  watch.pid  tmux-watch      # the reporter's pid and window, so `stop` can take it down after the loop
  tmux | launcher            # where `run` put the loop (tmux: window/session; else `tern <session>` or `systemd <unit>`)
  launcher-watch             # the same for the reporter (`tern-tab <block>` or `systemd <unit>`)
```

None of that leaves the machine: `.ouroboros/runs/` is gitignored, and it is
tens of megabytes of transcripts. So the run directory is not the record. The
record is what `ouroboros archive` writes beside it, in the project's own git:

```
.ouroboros/
  AGENTS.md                  # how runs are operated here; written once by `init`, then the project's
  RUNS.md                    # the index: one row per run. generated; never hand-edited
  history/<run>.md           # one digest per run: front matter, the read, then the human's notes
```

`archive` runs automatically at the end of `run` and after `stop`, and can be
run by hand (`--all` backfills, `--machine` names a box other than this one).
Everything above the notes marker in a digest is measured from the logs or from
git; everything below it belongs to whoever writes what the run taught, and a
rewrite preserves it. `RUNS.md` is generated from the digests' front matter,
oldest first, the way `STATE.md` is generated from record nodes.

The index leads with **what the run ticked**, not with how many criteria are
checked at its tip: a charter usually opens with boxes already checked, so the
tip count flatters. cadex's nt3 tip reads 9 of 13 and nt3 closed none of them.
The delta is measured against the charter at the merge base.

`status.json` states: `starting`, `work`, `critique`, `scribe`, `backoff` (with
`why` and `seconds`), `idle`, `stopped`, `killed`. Older runs also wrote
`oversee`, `reconcile` and `plan`, and `work` with `housekeeping`.

**The run's page** (`view.py`, `view_template.html`) is the view a person opens:
`.ouroboros/runs/<run>/view/index.html`. It is two things on one page:

- **The facts, which need no model.** The engine re-renders the page at every
  status change, and once a minute while a model call runs (the heartbeat, so a
  long turn does not read as a dead loop). Every 5 s the heartbeat also reads the
  end of the call's live transcript (`activity.py`): an open tool call with no
  result yet (Claude `tool_use`/`tool_result`, Codex `item.started`/
  `item.completed`, paired by id) and Claude's background shells. It writes that
  as `activity` in status.json (thinking, tool, command, or wait: a Monitor, a
  ScheduleWakeup, a `sleep`/`until` loop) and renders again when it changes. Pi
  has no recorded sample with call ids, so a Pi call reads as thinking. It reads the run directory and the
  memory snapshot, escapes everything (then turns inline markdown into HTML: `**bold**`, `*em*` or
  `_em_`, `` `code` ``, `[text](https://...)`; only `**` is bold, so `__init__`
  stays a name), and shows: the run and the goal; the loop
  as a dial (one turn is the smallest round span, 5 min to 16 h, that holds this
  iteration and the three before it; a band fills clockwise from twelve o'clock as
  the iteration runs, coloured by stage and labelled with each stage's time, the
  running stage's from `stage_since` in status.json; a hand at now, a tick for the
  critic's verdict, and the three iterations before as thin rings inside, on the
  same clock. `view.py` puts the iteration on the svg as data and the page's script
  draws it every second; the centre names the iteration, the stage, what the call
  is doing (a command, a wait, a tool) and its background shells, and "Right now"
  names the command and how long it has run); the key
  figures (iteration, elapsed, criteria met, commits kept and reverted, last
  verdict); the time budget (`stop.after` or `stop.until`, the sooner) as one
  row of 20 cells that ramp along viridis, ticking in the browser; usage per
  harness: the models it ran and for which roles, one row of cells per rationing
  window and the tokens. A window's row is this run's own share of one whole
  window, not the account's level (other use of the account fills that too): the
  ledger sums the rises between readings and counts a new window from zero once
  its reset time moves on, so a long run can pass 100%. Tokens are fresh input,
  cache read, cache write and output, summed per harness from each call's result
  (Claude's result line, Codex's `turn.completed`, Pi's messages; the model from
  Claude's `modelUsage`, Codex's rollout `turn_context`, else the configured
  one). Then a highlighted "Right now" block: the stage, and the critic's plain
  `doing` for the iteration being worked on (its `did` and the next `doing` once
  it has judged); the criteria (the gap claims,
  or the charter's boxes) and the open problems (the frontier), each the first
  three rows with the rest behind a "show N more" toggle; and the timeline:
  one column per iteration (stacked by stage time, the verdict under it, a fold
  above it) and one folded row per iteration, newest first (the critic's `did`
  and the time taken; opened, its `reason` and `doing` and the records the scribe
  wrote; a column's link opens its row). A record belongs to the iteration whose
  scribe step is the first at or after its time. Top to bottom, the sections run:
  progress and open problems, the memory graph, the findings, the timeline.
- **The memory graph, drawn by Governor.** `view/graph.html` holds Governor's
  report embed (its hub renderer: the root in the centre, claims around it by
  status, records on the rim; the only layout with a light palette, and only for
  root, state and record nodes, so a legacy `plan` claim or an unknown kind is
  drawn as a state claim) and the memory as Governor's `ReportGraph`, which
  `view.report_graph` builds from the snapshot either store gives (the same
  mapping as Governor's `toViewer`). The page frames it and gives it its colours
  (`view.GRAPH_COLOURS`, seaborn's Blues: met claims darkest, open ones pale,
  records between and paler with age); a plain wheel scrolls the page, Shift +
  wheel zooms, and a button (or N) names the nodes. The embed is vendored in
  `src/ouroboros/vendor/` (`scripts/vendor-governor-report.sh` refreshes it from a
  Governor checkout; `GOVERNOR_VERSION` names the commit), so a files run draws
  its graph with no Node and no Governor installed. The graph page is rewritten
  only when the memory changes, and a live swap keeps its frame, so pan and zoom
  survive. When the whole memory is too big for the embed (5,000 nodes,
  20,000 edges, 1 MB), the page draws the root, every live claim and the newest
  records that fit, and its caption says how many records were left out: the old
  ones live on in the claims they were folded into.
- **The scribe's two files, which need judgment.** `view/summary.txt` is plain
  text: what the run is for and where it stands, in words for someone who does
  not know the code (before the scribe writes one, the charter's mission stands
  in). `view/story.html` is an HTML fragment with what the run has found, each
  section a finding stated as a sentence, then its evidence (tables, figures,
  small inline SVG charts), curated rather than appended, images in
  `view/assets/`. On the page each top-level `<section>` that opens with an
  `<h2>` or `<h3>` folds under that heading (open by default). The template gives it a few classes (`grid`, `chip`,
  `ok`/`warn`/`bad`, `num`, `src`).

The page looks like a report, not a dashboard: light by default (a button
switches to dark and the browser remembers it), serif text, ruled numbered
sections instead of boxes, the Ouroboros logo in the masthead and as the icon.
The reader it is written for opens it once a month among many runs. It has no
external resources, fits a phone (no sideways page scroll; wide tables scroll in
place) and works without JavaScript. With it, a small script polls `live.json`
every 10 s while the tab is visible and swaps the page in place when its version
changes, keeping open sections open, and ticks the clocks (stage timer, "updated
N s ago", the time budget, window resets). Past two minutes in a live state the page says it is
stale, which means the loop process is gone. A repo can replace the template with
`.ouroboros/view/template.html`. Slots: `{{header}}`, `{{summary}}`, `{{loop}}`,
`{{figures}}`, `{{time_budget}}`, `{{usage}}`, `{{now}}`, `{{criteria}}`, `{{frontier}}`, `{{story}}`,
`{{progress}}`, `{{graph}}` (with `{{graph_version}}`), `{{logo}}`, `{{favicon}}`, plus the older `{{facts}}`,
`{{stages}}`, `{{decisions}}`, `{{timeline}}` and the plain values `{{title}}`,
`{{run}}`, `{{goal}}`, `{{mission}}`, `{{state}}`, `{{iteration}}`,
`{{generated}}`, `{{version}}`. Unknown slots are kept.

`ouroboros view` serves the view directory only (GET and HEAD, no listings, no
path outside it, `no-store` on the live files) on `127.0.0.1:8765`; `--host
0.0.0.0` reaches a phone, for example over Tailscale, and it prints the
addresses. It renders once before it serves, so a stopped run still has a page.
It replaced `ouroboros web` (2026-10-15 to 10-17), a telemetry dashboard that
showed CPU and stage clocks but nothing of what the run had found.

`ouroboros view --debug` serves the same page from a temp copy of the run
(`viewdebug.py`), with a panel on top: the stage, the iteration, the time in
the stage, the elapsed time and budget, the usage of each window, and the
harness of the actor, what the agent is doing (thinking, tool, command, wait)
and its background shells. Each change writes the copy's `status.json`, renders the
page again and swaps it in (`window.ouroborosSwap`); auto-play steps actor,
critic, scribe, idle. It is for work on the page's look and animations; the
run's own directory is never written.
`limited` lists blocked harnesses with the minutes left.

**Where the loop lives** is `launcher:` (`launch.py`): `tmux`, `tern`, `systemd`
or `foreground`, and `auto` (the default) picks tmux inside tmux, tern inside a
tern pane, else tmux if installed, else the foreground. Every launcher marks the
loop it starts with `OUROBOROS_LAUNCHED=1`, so that loop runs in place, and
notes what it started in the run directory, so `stop` takes down exactly that.

- **systemd** starts a transient user service, `ouroboros-<run>.service`
  (`systemd-run --user --collect`), in the repo, with the operator's environment
  minus its terminal, multiplexer and agent-session variables. It belongs to no
  terminal, so a tmux or tern restart cannot reach it; with lingering it outlives
  logout. `memory_max:` sets `MemoryMax` (and no swap) on the unit, which covers
  every process the run starts. A runaway child is then killed alone instead of
  the OOM killer taking the operator's session (it took a tmux scope once, when
  a 60 GB FreeCADCmd ran away in cadex). The reporter gets a unit of its own,
  `ouroboros-<run>-watch.service`. The journal keeps the console output.
- **tern** starts a tern session of its own, `ouroboros-<run>`, with the loop's
  block kept open after it exits so its last lines stay readable. The reporter
  gets a tab in it. Tern's session daemon lives inside the `tern-remote` user
  service, so restarting that service ends the run with it. That is the reason
  `systemd` is the launcher for long unattended runs.
- **tmux**: started from inside a session -- the operator's,
where an agent and a human are already talking about the run -- it takes a new
window of that session, `ouroboros-<run>`, and they open another for `ouroboros
top`. From a bare terminal, or with `--own-session`, it gets a detached session
of its own: pane 1 the loop log, pane 2 `ouroboros status --watch`. `run` notes
which in `runs/<run>/tmux`, so `stop` kills the window and never the operator's
session. With `backend: tmux`, each harness call also opens its own window in
that session while it runs.

**The status TUI** (`ouroboros status --watch` on a terminal, or `ouroboros top`)
is a btop-style curses monitor in `tui/`, ported from the author's vllmtop. It
reads only the run directory and the repo, never the loop process, on a poller
thread every two seconds. A run strip on top: state, iteration, time in stage,
elapsed and left with a gradient bar, the harness chain with the active one
green and limited ones red, a usage row of one meter per window, pid alive;
the pipeline
`actor→critic` sits in the strip with the active
stage lit. Below it the default view is chart-first, an overview and not an
analysis: **iterations** (outcome strip over a braille chart of actor minutes),
**activity** (harness CPU over time), and **status**. **time by stage**,
**frontier** (charter gaps done, and the state graph's working / open / blocked
split), **messages**, **loop**, **stages** and **log** are off by
default and reachable through the help. Panels toggle by their superscript
number.

**status** is what a person actually watches, and it is where the verdict panel
and the message feed went. They were two panels holding half an answer each --
one said what was decided, the other said what was being typed, and joining them
was the reader's job. It carries, top to bottom: the verdict history strip and
the latest verdict; the pipeline with the stage lit and how long it has been
held; the last three verdicts, one line each, newest last, with the newest one's
reply under it; and then the rows that are the point of it:

    last     the unit that finished    (one sentence, written by the critic)
    current  the unit now running      (one sentence, written by the critic)
             the newest line the running agent said or ran

The verdict list is capped at three so the summaries get the room: they wrap into
whatever is under the list rather than clipping at a row each, sharing it when
both are long, and the raw line holds the bottom row. A blank row sits between the
list and `LAST` and between `LAST` and `CURRENT`, and the labels are the loud ones
on the panel; when the panel is short the verdict rows go before the blank ones.

`did` and `doing` are two extra fields on the critic's JSON verdict
(`roles/critic.py`). The critic writes them because it is the role that runs
every iteration and sees the actor's whole output; nothing in the engine
branches on them, and when the critic is `rules` the panel falls back to
the reason and the reply, which are always there. When the panel is short the
verdict list is the first thing given up and the summaries the last. Details: **stages** (the pipeline with the active stage lit and
per-stage run counts, last, average, total durations from `iterations.jsonl`),
**iterations** (an outcome strip, ■ changed and recorded, ▪ changed, · empty,
✗ reverted, ! error, over a braille chart of actor minutes per
iteration), **load** (a braille chart of the harness process tree's CPU, procs
and RSS, loadavg, limit blocks), **messages** (the newest
transcript tailed live: assistant text, tool calls with the command or path,
errors, the result line; `o` shows tool output too; Claude stream-json, Codex
and Pi event lines all parse), the verdict history strip inside **status** (one
dot per verdict -- green for an iteration that went through, red for one that hit
something: `stuck`, `looping`, `reject`, or a `done` the critic rejected. Six
lettered glyphs asked a reader to decode a legend at a glance, which is the one
thing a glance cannot do; the only question the strip answers is whether the run
is moving), and **loop.log**. For the feed
to be live the headless backend streams each call's stdout to the transcript
file line by line, and Claude runs with `stream-json` in every backend.

`ouroboros report` renders the run into `REPORT.md`: the STATE.md frontier,
verdict counts, the decisions the critic made for you, cost.

**The reporter** (`ouroboros watch`, `watch.py`, `notify.py`) is the run's voice on
a phone. It is a separate process that reads the run directory and never the
loop, so the loop cannot tell whether one exists, and a reporter that crashes,
hangs, or runs out of usage costs the run nothing. `run` starts it in a window
beside the loop when it has somewhere to push to; `stop` kills the loop first,
then SIGTERMs the reporter and gives it a moment to say the run stopped.

It has two tiers, because the urgent messages must not depend on a model: when
usage runs out, the reporter's model is usually out too.

- **Triggers** are facts read straight off the files and go out as fixed text the
  moment they are seen: `needs_human` (the file appeared), `stopped` (state
  `stopped` or `killed`, with the reason), `limited` (a harness blocked in
  `loop.log`; urgent when every harness is), `stuck` and `looping` (three of the
  critic's verdicts in a row, then at ten and twenty; a `loop` step that acted at
  escalation 2 or 3), `engine_error` (once an hour), `silent` (the pid is gone
  while the run says it is working, urgent; or `status.json` older than
  `report.silent_after`), `done_accepted`, and `reject` (off by default -- a
  revert is routine and the digest counts them). `report.on` picks the set.
- **Digests** are one read-only model call (`roles.reporter`, the
  `ouroboros-reporter` skill) on a timer (`report.every`) and on the milestones
  `stopped` and `done_accepted`: what landed, what is next, what is broken, the
  numbers, in `report.max_chars` of plain text. The prompt carries the measured
  numbers, the verdicts, the commits and the log since the previous digest, so
  each one is a delta and not a re-telling. When no harness answers, the numbers
  go out alone. The full text is kept in `runs/<run>/reports/`.

The reporter keeps its place in `reporter.json` -- how many lines of each file it
has read, when the last digest went out, which limits it has already mentioned --
so a restart does not replay the run's history, and a reporter started on a run
already under way reports from there. Alerts are about things happening, so a
reporter never replays the ones it was not there for; a digest is an orientation,
so the **first** one covers the whole run and every one after it is a delta. Until
a digest has really gone out there is no "since the last report" to speak of,
which is what `ouroboros watch --once` asks for while a reporter is watching but
has not yet had anything to say. `reporter.json` then has two writers, and they
keep different halves of it: the watching reporter owns how far the alerts have
read, and whoever produced a digest most recently owns the digest window. Each
save keeps the other's half, or a manual report and the watcher next to it erase
each other (ot7, 2026-09-16).

One ordering matters enough to name. `run` starts the reporter and the loop in
the same breath, and the loop takes a second or two to overwrite `status.json`,
so for that moment the directory still holds the *previous* run's `killed`. A
reporter that finds a finished run on its **first** pass therefore waits three
minutes for one to come alive before believing it, and then gives up in the log
rather than on the phone. Without that, every launch pushed the last run's
ending and exited before the loop it was watching had drawn breath (ot7,
2026-09-16). Pushover is the one channel (`PO_USER` and
`PO_TOKEN`, from the environment or a `.env` in the target repo, its `.ouroboros/`,
or `~/.ouroboros/`); `report.notify: auto` uses it when the credentials are there
and stays quiet otherwise. `ouroboros watch --test` proves the phone is reachable,
`--once` writes one digest now, `--console` prints instead of pushing.
## 16. Config

`.ouroboros/config.yml`, written by `ouroboros init` and the `ouroboros-design`
skill, editable by hand. Every key with its default:

```yaml
run: run                    # branch ouroboros/<run>, run dir .ouroboros/runs/<run>
goal: .ouroboros/goal.md    # the charter
memory: auto                # auto | governor | files  (handoff = files)
backend: headless           # headless | tmux (one window per harness call)
launcher: auto              # auto | tmux | tern | systemd | foreground: where `run` puts the loop (section 15)
memory_max: null            # systemd only: MemoryMax on the run's unit, e.g. 40G
critic: agent               # agent | rules (rules: the deterministic fallback only, no model call)
scribe: agent               # agent | rules (rules: the loop writes each record itself, no model call)
idle_interval: 30m          # sleep after done_accepted under report_done
roles:                      # each role: harness, model, timeout, resume_for, fallback
  actor:      { harness: claude, model: null, timeout: 45m, resume_for: 1, fallback: [{ harness: codex }] }
  critic:     { harness: claude, timeout: 10m }
  scribe:     { harness: claude, timeout: 20m, fallback: [{ harness: codex }] }
  reporter:   { harness: claude, timeout: 10m }   # writes the digests; not in the pipeline
git:
  branch: null              # -> ouroboros/<run>
  tag_on_accept: true
  revert_on_reject: true
  allow_dirty: false
stop:                       # all optional; none set = run until killed
  after: null               # wall clock, e.g. 15h
  max_iterations: null
  max_cost_usd: null        # API-equivalent dollars, not a bill (section 12)
  max_usage: null           # 0..1 of a subscription window
  until: null               # ISO timestamp
  on_done_accepted: null    # this many done_accepted in a row
  max_stuck: 25             # this many `stuck` verdicts in a row; null to never stop
graph:                      # when the scribe folds, either store
  reconcile_every: 5        # a fold is due after this many worked iterations
  pressure: 3               # ...or at this many unfolded records
  budget_units: 1           # dispatch budget per iteration
limits:
  cooldown: 30m             # block a limited harness this long when the reset time is unknown; doubles
  max_cooldown: 3h
  transient_strikes: 3      # 429/overloaded errors in a row that count as a limit
  reserve: {}               # harness -> 0..1: stop using it at this much of a long window
loop:                       # motion-without-progress detection (section 6b)
  product_after: 8          # iterations changing only bookkeeping; null turns it off
  frontier_after: 30        # iterations with the frontier digest unmoved
  escalate_every: 5         # iterations past the threshold per rung of the ladder
  rotate: true              # rung 2 may switch the actor to its fallback
report:                     # the reporter, `ouroboros watch` (section 15)
  notify: auto              # auto | pushover | none; auto pushes when PO_USER and PO_TOKEN are set
  every: 4h                 # a model-written digest this often; null: only on stopped and done_accepted
  on: [needs_human, stopped, limited, stuck, looping, engine_error, silent, done_accepted]   # add reject for every revert
  silent_after: 2h          # status.json this stale while the run says it is working
  poll: 30s
  max_chars: 1000           # one push notification; Pushover takes 1024
```

A fallback is `{ harness: codex, model: null }`; the chain is the role's own
harness first, then its fallbacks in order (section 9).

CLI flags override config: `--run-name --for --max-iterations --max-cost
--harness --model --allow-dirty --critic --memory --foreground`.

## 17. Repo layout

```
ouroboros/
  pyproject.toml            # [project.scripts] ouroboros = "ouroboros.cli:main"; pydantic, pyyaml; pytest
  src/ouroboros/
    cli.py                  # init | design | preflight | run | stop | status | watch | report | archive | skills install; menu; signals
    engine.py               # the state machine (section 6): step(), retry policy, reconcile, plan, critique
    config.py               # pydantic models for config.yml (section 16)
    goal.py                 # the charter parser: sections, done criteria → gap names, ladder, policies, template check
    harness/
      base.py               # Harness protocol, Result (+ kind: ok|timeout|limit|auth|transient|error, reset time parsing)
      claude.py  codex.py  pi.py
      pool.py               # LimitBoard (per run) + PooledHarness (per role): fallback chains
      login.py              # login checks per harness for preflight (claude auth status, codex login status, pi auth check)
      backend_headless.py   # subprocess in its own process group, timeouts, kill_active, backend switch
      backend_tmux.py       # one tmux window per call, output to files
    memory/
      graph.py              # the model, the snapshot, rendering, GraphMemory: the loop logic for both stores
      governor.py  files.py # the two stores
      base.py               # the adapter protocol
    memcli.py               # `ouroboros mem`: the agent's only memory surface
    roles/
      actor.py  critic.py  scribe.py       # prompt assembly, JSON parsing, the rules critic, the scribe call
    gitguard.py             # branch, commit, tag, diff, revert_to
    budget.py               # BudgetClock, backoff table
    recorder.py             # jsonl, status.json, loop.log, NEEDS_HUMAN.md
    watch.py                # the reporter: triggers off the run directory, digests since the last one
    view.py  view_template.html   # the run's live page: render (facts + the scribe's story), serve
    viewdebug.py                  # `ouroboros view --debug`: the page on a copy, with a state panel
    activity.py                   # what a running call does now, from its live transcript
    logo.svg  vendor/governor-report.{js,css}   # the page's logo; Governor's graph embed (vendored)
    notify.py               # Pushover; credentials from the environment or .env files
    history.py              # the durable record: .ouroboros/history/<run>.md digests + RUNS.md index
    operator.py             # .ouroboros/AGENTS.md, the guide for the agent that drives runs in the target repo
    launch.py               # where the loop lives: auto/tmux/tern/systemd/foreground, notes for `stop`
    tmux.py                 # the run session: launch, kill
    tui/                    # the status TUI: theme, widgets, layout (from vllmtop), state (run-dir reader), panels, app
    skills/                 # packaged with the wheel; `ouroboros skills install` copies them out
      ouroboros-actor/  ouroboros-orient/  ouroboros-critic/  ouroboros-scribe/  ouroboros-fold/
      ouroboros-reporter/  ouroboros-design/  ouroboros-launch/  ouroboros-checkup/   (each SKILL.md)
  tests/                    # ~430 tests; fake_harness.py scripts actors, critics and scribes
    test_engine.py test_pool.py test_critic.py test_goal.py
    test_memory_files.py test_memory_governor.py (needs the governor and flywheel CLIs)
    test_claude_parse.py test_drivers_parse.py test_headless.py test_backend_tmux.py (needs tmux)
    test_gitguard.py test_recorder.py test_config.py test_history.py test_operator.py
    test_watch.py test_notify.py (the reporter and its channel) test_view.py (the run's page) test_viewdebug.py (its debug panel) test_activity.py (recorded samples in tests/samples)
  DESIGN.md                 # this file
  README.md  AGENTS.md
```

Role prompts live as skills, not as Python strings, so you can edit them
without touching code. Ouroboros inlines the skill text in every call, so no
harness depends on skill discovery.

## 18. Build order (all built)

1. **Walking skeleton.** `single` mode, Claude only, handoff memory, git
   guard, `--for`, JSONL log. Fake harness tests prove "never blocks".
2. **Overseer.** JSON verdicts, question answering, done rejection, rules
   fallback, backoff table.
3. **Graph memory adapter.** Orient / dispatch / record / reconcile mapping.
   Directive node per charter version. `check` after commit.
4. **Codex and Pi drivers.** Fallback chains and the limit board.
5. **Critic.** Revert on reject, tags. (Modes and the council were built, then removed when the critic and the overseer became one role.)
6. **Skills.** `ouroboros-design` interview, `ouroboros-launch` start, `ouroboros-checkup` report.
7. **tmux backend.** One window per call.
8. **Charter and plan** (section 20): gaps from criteria, the plan view, the
   planner role, the critic on the frontier.
9. **Two roles** (2026-09-09): the critic and the overseer merged into one
   read-only call; the maintainer and the planner off by default, housekeeping
   as an actor iteration.
10. **Only two roles** (2026-10-17): the maintainer, the planner and the plan
    layer removed; the actor falls back to Codex by default (section 20).
11. **Files memory and `ouroboros mem`** (2026-10-17): the handoff memory
    became the governor model on files; agents reach memory only through one command.
12. **The scribe and the run's page** (2026-10-17): a third role records, folds and
    writes the story; `view/index.html` and `ouroboros view` replace `ouroboros web`.

## 19. Open questions, resolved

1. **Cost when the harness does not report it.** Pi reports real cost
   (`usage.cost.total`, every model priced). Codex reports tokens only; its cost
   stays zero. Claude reports API-equivalent dollars. Reports label the sum
   `api-equivalent` and no price table exists. Time and iterations are the
   real caps.
2. **Reject means revert.** Decided: on (`git.revert_on_reject`). A critic
   reject reverts the iteration and the next actor starts from the `reply`.
3. **Skills for Codex and Pi.** Decided: the role prompt text is inlined in
   every loop call; `skills install` copies the same folders into each
   harness's skill directory for interactive use, and `ouroboros design` runs
   the interview in any of the three.

Still open:

4. **Ping-pong harnesses** (a different harness every other iteration) as a
   way to break a fixation. Cheap to add on the pool. Not needed yet.
5. **Views beyond `plan`.** An RL repo may want a `policy` view. The adapter
   hard-codes one view name; generalizing is a config change when someone
   needs it.

## 20. Charter and plan: self-evolving goals

Decided 2026-09-06 after the first night run. The goal document rotted in one
night: the overseer rejected "done" by counting unchecked boxes in a static
file while the work was already shipped. The horizon ladder, on the other hand,
worked: the actor took rungs in order and never ran dry. So the ladder is the
right idea in the wrong place.

### The split

The goal splits into two layers with different owners.

| Layer | Owner | Changes | Lives in |
|---|---|---|---|
| **Charter** | the human | at iteration boundaries, by editing `goal.md` | `.ouroboros/goal.md`: mission, constraints, question policy, quality bar, exhaustion policy, done criteria, first horizon ladder |
| **Plan** | the agents | every iteration | the frontier (open state nodes: gaps) and the critic's `reply` |

No agent role may edit the charter. An agent that can edit the criteria can
meet the criteria by editing. The human overrules the agents by writing a new
charter version; Ouroboros mints a new directive record for it.

### A scribe, and the run's page (2026-10-17)

The operator asked for a third role between the actor's work and the next
iteration, and for every run to keep a web page about itself that a person can
open on a desktop or a phone. Both are the **scribe**: it records each unit, folds
the memory when a fold is due, and writes the page's story.

- **Order: actor → critic → scribe**, not actor → scribe → critic as first asked.
  After the verdict, the record can carry it, a rejected unit is recorded as a
  dead end instead of as a success, and the record lands after the revert, so no
  revert undoes it (a files-store revert did, until now). The critic still
  checks the memory: its `fix_first` goes to the scribe that runs right after it.
- **The actor reports instead of recording**, with `## Claims` lines the scribe
  checks against the diff, and no longer spends iterations on housekeeping.
- **Each role gets its own `ouroboros mem` verbs**, enforced by the command, not
  only asked for in a prompt.
- **A reject reverts this iteration's commits only** (to the head the actor
  started from), not everything since the last `ok` tag, because the scribe's
  record commits sit between ok tags now. The `ok` tag goes on the scribe's commit.
- **The page is a template plus a story.** The cadex orun8 page (written by the
  actor as a charter criterion) showed that a page about the work is worth having,
  and also that half of it repeats what the memory knows (criteria states are gap
  claims, its timeline is the records) while a hand-edited 58 KB page grows
  without end. So the facts are rendered by code at every stage change, and the
  scribe writes only what needs judgment. The `ouroboros web` telemetry dashboard
  is removed.
- **The page is for an outsider** (2026-10-17, after the first look at it). The
  first template was a dark grid of boxed cards: it read as a tool for whoever
  wrote the code. The reader is an operator who checks in rarely, across many
  runs, so the page now opens with the scribe's plain-language summary, shows the
  loop as a ring and usage as a gauge, and merges the decisions and the records
  into one timeline. Technical detail stays, under the plain sentence it supports.
- One more model call per iteration. The scribe's default chain can take a
  lighter model; `scribe: rules` runs without it.


The operator asked for the simplest loop: an actor and a critic, nothing else.
The maintainer and the planner had been off by default since 2026-09-09 and did
not come back on; the plan layer only the planner wrote (the `plan` view,
`PLAN.md`, `Bet:` records, the plan component in governor, `.ouroboros/bets.md`,
the `repeat_bet` loop counter and the "ban the bet" escalation rung) was dead
weight in every prompt and every test. All of it is gone. What replaced it:

- The reconcile pass is always an actor housekeeping iteration
  (`skills/ouroboros-reconcile`, formerly `ouroboros-maintainer`).
- The next unit is always the critic's `reply`, steered by the frontier and the
  charter's horizon ladder. The directive seeds no plan.
- The loop ladder is two rungs: name it, change the model.
- The actor's default chain is `claude`, then `codex`: in cadex nt2 Codex
  carried 54 of 68 actor turns while Claude was limited. A missing codex is a
  preflight note, never a refusal.
- Graphs from older runs keep their plan nodes. Nothing reads them, and the
  reconcile pass is told to leave them and their impacts alone.

The rest of this section is the 2026-09-06 design. Where it names the planner,
the plan view, or bets, read it as history.

### Governor is the only graph memory (2026-10-17)

The operator decided that Ouroboros, Flywheel and Governor are the whole stack.
The earlier graph-memory adapter, its CLI, its config detection and its tests
are gone; `memory:` takes `auto`, `governor` or `handoff` (now `files`, below). The ideas carry over
unchanged in governor: append-only records with declared impacts, a fold as the
single writer of state, a frontier of open claims, a directive per charter
version. The config key for the housekeeping cadence is now `graph:`; an old
key under another name is ignored, and its defaults are the same.

### One model on two stores, behind `ouroboros mem` (2026-10-17)

The handoff memory (plan.md, journal.md, `handoff/NNNN.md`) kept no state and was
checked by nothing, so a repo without governor got a weaker loop: no frontier, no
`no_frontier` signal, no fold, and notes that could not seed a graph later. The
operator asked for the fallback to follow governor's ideas instead, so that what a
run leaves behind can fill a Flywheel graph's gaps later. It is now the `files`
store: the same records, claims and folds as markdown under `.ouroboros/graph/`
(section 7c). `handoff` reads as `files`; `plan.md` and the journal are gone, and
the reporter and the report show the STATE.md frontier instead.

The operator also asked that the agent not have to choose between two sets of
instructions. So the agent sees one command, `ouroboros mem`, and the machinery
behind it is deterministic: the same verbs, the same fold JSON, the same rules, on
either store. That forces the two stores to stay aligned: they share the loop
logic, the prompts and the tail listing, and a test drives each through the same
verbs. Two choices fell out of that: neither the actor nor the reconcile pass runs
git for the memory (the loop's commit does), and a fold renders STATE.md itself.

Not built yet, by choice: the importer that loads a files memory into a governor
graph, and a files run that reads a governor graph's state as its starting point.
The second is the answer to "one graph, one writer" (Cadex, while sb1x writes its
graph): a run records into files on its branch, and the merge imports them, so the
big graph keeps one writer. Separate roots, run tags, or a cloned Flywheel
repository per run were considered and rejected: separate roots give parallel
truths, the directive lineage and `## Recorded`'s branch already say which run
wrote a record, and Flywheel has no repository branches, so a clone per run means
rebasing graph conflicts.

### Live charter edits (2026-09-12)

The operator requested steering an active Cadex run toward its new dashboard
without stopping training or restarting the loop for every instruction. The CLI
now passes the configured charter path to the engine. Before each iteration it
reads that file and adopts changed, nonempty UTF-8 content. Save edits atomically
(for example, write a temporary file then rename it) so an intermediate editor
write cannot become a directive. Config, models, stop limits and the run timer
are not reloaded.

An actor and its critic share one charter version: edits during a call wait until
the next iteration, rather than changing the standard halfway through review.
Reload updates memory (including the existing versioned directive),
the actor and critic; it clears model-session
continuation and tells the actor to reconsider the prior next-unit handoff while
retaining applicable correctness fixes. A `goal_reload` iteration event records
the old/new SHA-256 values and the log identifies the adoption boundary. Empty,
missing or unreadable files keep the prior charter. Failed directive registration
also keeps the prior version and retries at the next boundary. Existing gap
versioning remains responsible for added/dropped criteria; reload does not write
state nodes or reset iteration counts or budgets.

This runs between complete iterations, not inside an active harness call or a
limit wait. A process launched with older runner code needs one restart to acquire
this capability. Agent roles still may not edit the target project's charter;
the human/operator owns live edits as well as initial authoring.

### Four grains of goal

| Grain | Size | Where it lives | Who writes it |
|---|---|---|---|
| Unit | one iteration | the dispatch record node of the iteration | actor |
| Gap | several units | open, broken, blocked state nodes on the frontier | the reconcile pass (actor housekeeping), from declared impacts |
| Next | a direction | one sentence in the critic's reply | the critic |
| Mission | the whole | the charter | human |

Grains are sizes, not times (decided 2026-09-06 before nt2). Agents have no
clock: "this week" means nothing to an actor that sees one iteration, and a
charter written for a night is wrong on day two of a week. The ladder rungs are
`short-term` (units), `medium-term` (gaps), `long-term` (directions and standing
work), and no rung carries a clock. Charters with the old
`next hour … next year` rungs still parse: hour and day fold to short, week to
medium, month and year to long.

### Mechanics

1. **Done criteria become gaps.** The directive record node declares one
   `NEW <gap>` impact per done criterion. The reconcile pass folds them into open
   state nodes. Work falsifies them through the ordinary impact channel
   (`target: <gap> — open → working`). A later charter version declares `NEW`
   only for criteria it adds; criteria it drops are named in the body so the
   reconcile pass can mark them superseded.
2. **The horizon ladder stays in the charter.** (2026-09-06 to 2026-10-17 it
   seeded a `plan` view that a planner re-ranked; removed, see above.) The
   critic reads it each iteration and names the next unit from it.
3. **Single writer.** The reconcile pass writes the state graph. Actors in a
   work iteration write records only.
4. **The critic reads the frontier,** not the checklist. "Done" means no
   charter-derived gap is open.

### Build order

1. Directive declares `NEW` impacts for done criteria.
2. Overseer prompt gets the frontier and `PLAN.md`.
3. Plan view, planner role, check-in report.
4. `actor-critic` and `council` modes; a Codex critic on Claude work.
5. `ouroboros-design` interview rewritten for the charter; `ouroboros-checkup`.
6. tmux backend.

## 21. What the first night taught (cadex nt1, 2026-09-05/06)

Facts from the run, and what changed because of each.

| Observed | Change |
|---|---|
| "You've hit your session limit · resets 2:50am (Europe/Madrid)" was not recognised as retriable; 63 of 90 iterations were empty commits, pointless reconcile passes, and rules-fallback `stuck`/`revert` verdicts on 0-byte patches. | `Result.kind` classifies limits; reset time parsed from five message shapes; wait until one minute past the reset (cap 6 h); fallback chains switch harness instead of waiting; a limit with no time gets a doubling cooldown. |
| The actor committed on its own, so the loop saw a clean tree and counted "no changes" streaks while work was landing. | Changed = HEAD moved or tree dirty. |
| Failed iterations still committed (empty), ticked the reconcile counter, and triggered maintainer passes. | A failed iteration commits nothing, counts nothing, reconciles nothing. Empty commits are skipped by default, including reconcile catch-all commits (the nt3 audit found that missed path). Unchanged, unrecorded turns no longer trigger bookkeeping passes. |
| At iteration 50 the overseer rejected "done" with "file lifecycle 0/3" while all three had shipped: it counted boxes in a static file. | The charter/plan split (section 20). Criteria become gaps on the frontier; the overseer judges against the frontier and the plan; a ticked box declares no gap. |
| The frontier had 3 open nodes and the fine grain lived only in goal.md, so the actor was steered by a document nothing updates. | The plan view (`short` / `medium` / `long`) and the planner role. (Both removed 2026-10-17; the critic steers from the frontier and the ladder, section 20.) |
| The horizon ladder worked: the actor took rungs in order and never ran dry. | Kept as the seed of the plan. |
| The tmux session was killed from outside; the loop crashed on SIGHUP because the logger raised EIO. | Logger falls back to file only; SIGHUP keeps looping; `ouroboros stop`; status shows the pid. |
| An orphaned `claude -p` survived the tmux kill. | Children run in their own process group and are killed on SIGTERM/Ctrl-C; tmux windows too. |
| The actor reconciled itself once in a work iteration. | An explicit forbidden-verbs list in the orient prompt; the reconcile skill (then `ouroboros-maintainer`) forbade the plan view in turn. |
| Edited goal on restart was not recorded. | One directive per charter hash, parented on the previous one, declaring only the criteria it adds and naming the ones it drops. |
| Outcome: 22 productive iterations, 14 record nodes, ADR-186..198, ~$81 API-equivalent, merged with a merge commit (173 commits, 132 of them empty). | The empty commits are why "no change, no commit" is a rule now. |

### Operator account refresh (2026-09-16)

After changing a harness login or restoring credits, run `ouroboros refresh`
(or `ouroboros --refresh`) from the target repository. It queues a refresh
for the live run. Backoff polls the request once a second; an active harness
call finishes before the request is handled. No signal interrupts a design
turn. Old runners without this capability refuse the command and need one
restart after upgrade.

The refresh probes every configured provider once, using the current login,
a fresh temporary directory, no tools, no resumed session, and a 20-second
timeout. These are small real harness calls and their usage is accounted for.
Fresh successful readings replace stale account meters; cooldowns clear only
when the probe succeeds and configured reserves permit it. Failed probes keep
existing cooldowns. Refresh does not edit credentials, change the charter,
reload configuration or restart the run. A stopped run must be started normally.


### Evidence-backed criterion reporting (2026-10-20)

The human-owned charter stays read-only. Checking its boxes is not the agents'
completion mechanism: the scribe folds a criterion claim to `working` when it is
true and evidenced. Both stores retain a machine-readable `ouroboros-criterion`
comment in the claim's Current section, preserving the original gap key through
renames and whole-text folds. Governor-generated random slugs are not criterion
identities. Unrelated/ambiguous claims and working claims with no record citations
cannot establish completion; unmapped criteria are explicitly unknown.

The live view matches only the current charter's criteria, not all historical gap
claims in a shared graph. Its read-only accounting writes a run-local
`criteria.json` snapshot, retaining initial met identities and current statuses.
The archive uses this snapshot for total verified met and newly met during the
run; it never reads another run's current graph to rewrite history. Preexisting
success is not credited again. Missing or malformed snapshots on legacy graph
runs produce unknown progress rather than a false zero. Explicit human-checked
criteria remain visible; they do not require the loop to edit the charter.

The archive keeps the existing `criteria_ticked` front-matter key for consumers,
but labels the human-facing metric completed and includes source/unknown counts.
Legacy reports can be regenerated with `ouroboros archive`; unknown is retained
when historical memory evidence cannot be mapped reliably. This changes reporting,
not critic verdicts, stop policy or scientific evidence requirements.
