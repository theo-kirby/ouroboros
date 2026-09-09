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
ouroboros run --for 10h       # preflight (git, charter filled in, logins), then tmux session ouroboros-<run>
ouroboros status --watch      # the live TUI (also `ouroboros top`); --plain for the text loop
ouroboros stop                # SIGTERM the loop and its children, then the tmux session
ouroboros report              # REPORT.md: bets, plan, verdicts, cost
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

**Preflight** runs before `run` detaches into tmux, so its messages land in the
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
| Memory between iterations | `hypergraph-protocol` when `.hypergraph/config.yml` exists. Handoff documents when it does not. |
| Who answers questions and judges "done" | An overseer agent. Rules as fallback. |
| Git | Auto-commit every iteration on `ouroboros/<run-name>`. |

## 5. Architecture

```
ouroboros run
 └── Loop engine            .................. the state machine (section 6)
      ├── Harness pool       .................. one chain per role, one limit board per run (section 9)
      │    ├── Driver        .................. claude | codex | pi
      │    └── Backend       .................. headless | tmux
      ├── Memory adapter     .................. hypergraph | handoff  (section 7)
      │    └── Charter parser ................. goal.md → gaps, ladder, policies (section 20)
      ├── Critic / Council   .................. grades the diff, actor-critic and council modes (section 10)
      ├── Overseer           .................. answers, judges, unsticks (section 8)
      ├── Planner            .................. bets into the plan view after each reconcile (section 20)
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
  ORIENT ──► WORK ──► RECORD ──► COMMIT ──► CRITIC? ──► OVERSEE ──► RECONCILE? ──► PLAN?
```

| Step | What happens | Who runs it |
|---|---|---|
| **ORIENT** | Read the charter, the memory (STATE.md, the plan, the unreconciled tail, or the recent handoffs), the overseer's message. Pick the next unit from the plan's `short` horizon or a frontier node. | actor (headless call) |
| **WORK** | Do one bounded unit of work. Budget: N units (default 1). | actor (same call) |
| **RECORD** | Write the memory entry for that unit (hypergraph record node or handoff file). | actor (same call) |
| **COMMIT** | Git guard commits whatever changed on the run branch. No change, no commit. | ouroboros (local) |
| **CRITIC** | `actor-critic` and `council` modes, only when something changed. Reads the diff with read-only tools. `accept` or `reject` with `must_fix`. | critic (headless call) |
| **OVERSEE** | Read the actor's final message, the frontier and plan, the critique, the diff stat. Did it ask a question? Did it claim done? Is it stuck? Answer as the user would. | overseer (headless call, cheap model) |
| **RECONCILE** | Hypergraph only. Fold recorded impacts into the state graph. Runs after `reconcile_every` iterations with changes or records, at `pressure` unreconciled nodes, or at once after a directive that declared gaps. | maintainer (headless call) |
| **PLAN** | After every successful RECONCILE, and after `done_accepted`. One `Bet:` record folded into the plan view. Handoff repos: every `plan.every` worked iterations. | planner (headless call) |

**ORIENT, WORK, and RECORD are one call**, not three. The prompt tells the
agent to do all three. Splitting them wastes context and money. The recorder
checks after the call that RECORD really happened. If not, the next iteration
starts with "you forgot to record, do it first."

**Fresh context every iteration.** Each call is a new session. The agent reads
memory from disk. Optional: `resume_for: N` keeps one session for N iterations,
then resets. Default is 1 (always fresh). A session is only resumed on the
harness that minted it.

**The actor's last message is data.** The overseer reads it. It does not reach
a human. Any question in it gets answered by the overseer and injected as the
first line of the next prompt.

**A failed iteration is quiet.** When the actor call fails (after the retry
policy of section 12), nothing is committed, the reconcile counter does not
tick, no maintainer runs, and the error rides into the next prompt. Repeated
failures back off 1m, 2m, 5m, 10m.

**A stuck iteration is expensive.** `stuck` means the opposite of a failure: the
actor ran fine and changed nothing, because it is waiting on a clock, a quota,
or an instruction it cannot act on. Nothing about repeating it immediately makes
it truer, and every repeat still pays for a critic, a maintainer, a planner and
an overseer call. So a run of `stuck` verdicts backs off on the same table --
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

So `loops.py` measures the other thing. Three counters, all mechanical, because
an agent asked "are you looping?" always finds a reason why this time is
different:

| counter | what it counts | default |
|---|---|---|
| `no_product` | iterations whose diff touched only `.hypergraph/`, `.ouroboros/`, `STATE.md`, `PLAN.md` | 8 |
| `no_frontier` | iterations after which the memory's frontier digest was unchanged | 30 |
| `repeat_bet` | planner bets whose content words overlap a recent bet by ≥ 0.5 | 3 |

Each resets the moment the thing it measures happens. A memory adapter with no
frontier returns `None` and switches that counter off: unknown is not the same
as unmoved. The numbers reach the overseer in its signal block, and the overseer
may disagree with them — but the ladder below runs on the numbers regardless.

**The ladder, and why it has no fourth step.**

1. **Name it.** The evidence goes into the actor's next prompt: the streak, and
   the subjects of the commits that made it.
2. **Ban the bet.** The next planner pass is forced, and told to pick a
   different charter criterion.
3. **Change the model.** The actor's harness is blocked on the shared
   `LimitBoard`, so its pool falls back and the board un-blocks it later on its
   own cooldown.

There is no step 4. A loop is a thing to break out of, not a thing to die of, so
none of this can stop a run: a false positive costs one wasted prompt, never a
run nobody is awake for. Step 1 is free and repeats every iteration; steps 2
and 3 cost real calls, so they fire once per `escalate_every` iterations rather
than on every one — without that gate, nt3's 157-iteration frontier streak would
have forced 157 re-plans.

**Replayed against the real nt3 log**, each counter alone would first have fired
at iteration **#50** (`no_product`, one iteration into the stall), **#74**
(`no_frontier`) and **#118** (`repeat_bet`), against zero firings in the run's
two productive stretches. `no_product` is the signal that earns its place;
`repeat_bet` is a backstop for a planner that repeats itself outright, and does
not catch a paraphrase.

## 7. Memory adapters

### 7a. Hypergraph (default when available)

Detection: `.hypergraph/config.yml` exists and `hypergraph` is on `PATH`.

The mapping is direct. One Ouroboros iteration **is** one hypergraph dispatch:

| Ouroboros step | Hypergraph verb / skill |
|---|---|
| ORIENT | `hypergraph-orient` — read `STATE.md`, replay the unreconciled tail, land on the frontier |
| WORK | `hypergraph-dispatch` — target = the goal doc or a frontier slug, budget = 1 unit |
| RECORD | `hypergraph-record` — one causally-parented record node with `## State Impact` |
| RECONCILE | `hypergraph-reconcile` — single writer, folds impacts, advances the high-water mark |

Rules Ouroboros enforces for hypergraph mode:

- The run branch `ouroboros/<run>` is the **single-writer branch for the run**.
  Only the RECONCILE step writes state nodes. Only one RECONCILE runs at a time.
- The actor prompt says: *never reconcile, never write state nodes, record only.*
- RECONCILE triggers: every `reconcile_every: 5` iterations with changes or records, **or** when ORIENT
  reports a fat unreconciled tail (the skill's own pressure trigger).
- The critic sees the diff, which includes the record node file. A unit with
  no record node is reported to the overseer as `handoff recorded: NO` and the
  next prompt starts with "record first".
- `hypergraph check` runs after every commit. A failing check does not stop
  the loop. Its output is appended to the next prompt as the first thing to fix.
- The charter is recorded once per version, at run start, as an
  Operator-directive decision node titled `Ouroboros run: <run> [<sha256[:8]>]`.
  Its `## State Impact` declares one `NEW gap-*` per open done criterion and
  the three plan-view seeds (section 20). A re-versioned charter gets a new
  directive whose parent is the previous one. Every work node of the run
  descends from a directive.
- The `plan` view is declared by Ouroboros at run start when missing
  (`hypergraph views add plan --md PLAN.md --reconcile`). The maintainer never
  writes it; the planner is its single writer.

A repo without `.hypergraph/config.yml` gets handoff memory. Ouroboros does
not run `hypergraph-init` for you.

### 7b. Handoff documents (fallback)

```
.ouroboros/
  goal.md            # the contract (section 13)
  plan.md            # living plan, agent-owned, rewritten freely
  journal.md         # append-only, one block per iteration
  handoff/
    0001.md          # "what I did, what I learned, what is next, what I assumed"
    0002.md
  runs/<run>/        # ouroboros-owned, machine state (section 12)
```

The actor prompt says: read `goal.md`, `plan.md`, and the last 3 handoffs.
Do one unit. Write the next handoff. Append to the journal.

Both adapters expose the same interface to the loop engine:

```python
class MemoryAdapter(Protocol):
    def orient_prompt(self) -> str          # text injected before the work prompt
    def record_prompt(self) -> str          # text that tells the agent how to record
    def verify_recorded(self, before, after) -> bool
    def needs_reconcile(self) -> bool
    def reconcile_prompt(self) -> str | None
```

## 8. The overseer

The overseer is the sleeping user. It is a cheap, fast headless call with tools
off, a JSON schema, and three turns at most. It gets:

- the charter (the user's voice),
- what is true now: the STATE.md frontier, the unreconciled count, and the
  plan (`short` / `medium` / `long`); for handoff repos the plan file and the last
  handoff's `## Next`,
- the actor's final message,
- the loop's signals: changed, recorded, no-change streak, error streak, the
  critic's verdict when one ran,
- the git diff stat for the iteration,
- the last 3 overseer decisions (so it does not flip-flop).

"Done" is judged against the frontier, never against the checklist in the
charter (section 21 says why).

It returns strict JSON:

```json
{ "verdict": "continue | answer | done_rejected | done_accepted | stuck | looping | revert",
  "reply": "text injected into the next prompt",
  "reason": "one sentence for the log" }
```

Triggers, in order:

| Signal in the actor output | Overseer action |
|---|---|
| Ends with a question | Answer it using the goal doc's **question policy**. Inject the answer. |
| Claims "done" or "complete" | Check against **done criteria**. Usually `done_rejected` + "here is what is still open". |
| No file changes for 3 iterations | `stuck`. Inject a nudge from the **exhaustion policy**. |
| Files change but nothing moves (section 6b) | `looping`. Name the loop with its evidence, then force a re-plan, then change the model. |
| Same error 3 times in a row | `revert` to the last accepted commit, inject "that path is dead, record it as a dead end". |
| Anything else | `continue`. |

**Rules fallback.** If the overseer call fails, times out, or returns bad JSON
twice, a fixed reply applies:

> Decide for yourself. Pick the option that is most reversible. Write your
> assumption in the record. Continue.

The loop never waits on the overseer either.

**Done is never trusted from the actor.** Only the overseer can say
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
dialogs with per-harness regexes. Types the overseer's answer.

Use it only when a harness has a feature headless mode does not. It is
fragile. The design isolates it behind the same `Harness` protocol so the loop
engine never knows which backend ran.

### Mixed harnesses

Every role in a mode has its own harness and model:

```yaml
roles:
  actor:    { harness: claude, model: opus }
  critic:   { harness: codex,  model: gpt-5-codex }
  overseer: { harness: claude, model: haiku }
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
  an account, not to a role. A limit seen by the overseer blocks that harness
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

## 10. Modes

A mode is the set of roles that run per iteration. Three are built:

| Mode | Pipeline | Use when |
|---|---|---|
| `single` | actor → overseer | cheap, simple goals; the first night ran this |
| `actor-critic` | actor → critic → overseer | default for a production repo. A `reject` reverts the iteration and puts `must_fix` at the top of the next prompt. |
| `council` | actor → critic×N → overseer | high-stakes work. `roles.critic` plus every entry of `config.council`; a majority of rejects rejects, ties accept. |

The critic runs only when the iteration changed something. It reads with
read-only tools (`Read, Grep, Glob` for Claude, a read-only sandbox for Codex,
`read,grep,find,ls` for Pi), returns strict JSON, and fails open: a critic that
crashes or returns garbage twice counts as `accept`.

```yaml
mode: council
roles:
  critic: { harness: codex, timeout: 15m }
council:
  - { harness: claude, model: sonnet }
  - { harness: pi, model: openai-codex/gpt-5 }
```

Not built, deliberately: `ping-pong` (alternate harnesses per iteration) and
custom pipelines. Fallback chains cover the first case; the second has no use
yet.

## 11. Git guard

- On start: create `ouroboros/<run-name>` from the current HEAD (or check it
  out when it exists). Refuse to run on a dirty tree unless `--allow-dirty`.
- After every WORK: `git add -A && git commit -m "ouroboros #<n>: <summary>"`,
  where the summary is the title of the record node or the handoff's first
  line. **No change, no commit.** The actor may commit on its own; the loop
  counts a moved HEAD as change.
- After a `continue`, `answer`, `done_rejected`, or `done_accepted` verdict:
  tag `ouroboros/<run>/ok-<n>`. This is the revert target.
- After a critic `reject` or an overseer `revert`: revert commits back to the
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
  on_done_accepted: 3     # overseer accepted "done" this many times in a row
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
later critic, overseer, maintainer, or planner readings. The TUI treats a window
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
                        granularity, not time. the first plan; the planner re-plans from it.
## Constraints        — never do X. always keep Y green. stay inside dir Z.
## Question policy    — how to decide for me. e.g. "prefer the reversible option".
## Exhaustion policy  — creative | maintain | report_done
   - creative:  the planner proposes three directions, picks one, continues
   - maintain:  tests, flakes, docs, refactors, forever
   - report_done: overseer may accept done; loop idles and polls every idle_interval
## Quality bar        — what the critic grades against (with Constraints).
## Reconcile          — hypergraph only: prose for the maintainer and planner.
```

The horizon ladder is the trick. A goal whose long-term rung holds standing
work never runs out. With the planner on, the ladder is only the first plan.

## 14. Skills

Installed by `ouroboros skills install`. Plain `SKILL.md` folders in the format
all three harnesses read: Claude Code invokes one as `/name`, Codex as `$name`,
Pi by discovery or `--skill <dir>`. `--user` copies them into every harness home
that exists (`~/.claude/skills`, `~/.codex/skills`, `~/.pi/agent/skills`) plus
`~/.agents/skills`. `ouroboros design` opens the interview in whichever harness
the actor chain names first, by skill name when installed and inline otherwise.

| Skill | What it does |
|---|---|
| `ouroboros-design` | The interview (built). Reads the repo first. Then asks, in rounds: mission, done criteria as claims, each rung of the horizon ladder, constraints, question policy, exhaustion policy, quality bar, run shape. Refuses to finish until every rung has at least 3 concrete items. Writes the charter `goal.md` and a matching `config.yml`; never the plan. |
| `ouroboros-checkup` | The check-in (built), for a live run or a finished one. Blockers first, then what landed, **whether the loop is moving or going in circles** (section 6b), the bets the planner changed (so you can overrule), decisions the overseer made for you, critic rejects and reverts, how the frontier moved, what to review hardest, then either a merge recommendation or the one correction worth making mid-run, and charter changes to consider. |
| `ouroboros-launch` | Starting one (built). Reads `RUNS.md` and the last digest's notes before anything else, pushes back on a charter criterion no single run can tick, runs `ouroboros preflight`, names the run and its stops, launches, and says what to watch. Most of what wastes a run is decided in the twenty minutes before it starts. |
| `ouroboros-planner` | Internal. The planner role: one `Bet:` record per pass, folded into the `plan` view (section 20). |
| `ouroboros-actor` | Internal. The role prompt for WORK. Includes the memory adapter's orient and record instructions. |
| `ouroboros-critic` | Internal. The role prompt for CRITIC. Grades against the quality bar, returns strict JSON. |
| `ouroboros-overseer` | Internal. The sleeping user. Returns strict JSON (section 8). |

Role prompts live as skills, not as Python strings, so you can edit them
without touching code and so a harness can load them natively.

## 15. Observability

```
.ouroboros/runs/<run>/
  run.yml                    # frozen config + start time + version
  pid                        # the loop's pid; status and stop read it
  loop.log                   # every step, backoff, harness switch, limit wait
  iterations.jsonl           # one line per step: actor | commit | critique | oversee | revert | reconcile | plan
  overseer.jsonl             # every decision the overseer made for you (verdict, reason, reply)
  status.json                # state, iteration, harness, usage windows, elapsed, last verdict, limit blocks
  transcripts/NNNN-<role>[-attempt].json   # raw harness output (+ .stderr); roles: actor, critic[N], overseer, maintainer, planner
  reverted/NNNN.patch        # what a revert threw away
  NEEDS_HUMAN.md             # only exists while something needs you (auth failure)
  REPORT.md                  # written by `ouroboros report`
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

`status.json` states: `starting`, `work`, `critique`, `oversee`, `reconcile`,
`plan`, `backoff` (with `why` and `seconds`), `idle`, `stopped`, `killed`.
`limited` lists blocked harnesses with the minutes left.

tmux layout when you attach: pane 1 is the loop log, pane 2 shows
`ouroboros status --watch`. With `backend: tmux`, each harness call also opens
its own window while it runs.

**The status TUI** (`ouroboros status --watch` on a terminal, or `ouroboros top`)
is a btop-style curses monitor in `tui/`, ported from the author's vllmtop. It
reads only the run directory and the repo, never the loop process, on a poller
thread every two seconds. A run strip on top: state, iteration, time in stage,
elapsed and left with a gradient bar, the harness chain with the active one
green and limited ones red, a usage row of one meter per window, pid alive;
the pipeline
`actor→critic→overseer→maintainer→planner` sits in the strip with the active
stage lit. Below it the default view is chart-first, an overview and not an
analysis: **iterations** (outcome strip over a braille chart of actor minutes),
**activity** (harness CPU over time), three meter panels, **time by stage**,
**verdicts**, and **frontier** (charter gaps done, and the state graph's
working / open / blocked split), then **messages** and a compact **overseer**
(verdict history strip, the last verdict, its reason and reply). **plan** is
off by default; the text panels **loop**,
**stages**, and **log** are reachable through the help. Panels toggle by their
superscript number. Details: **stages** (the pipeline with the active stage lit and
per-stage run counts, last, average, total durations from `iterations.jsonl`),
**iterations** (an outcome strip, ■ changed and recorded, ▪ changed, · empty,
✗ reverted, ◆ bet, ! error, over a braille chart of actor minutes per
iteration), **load** (a braille chart of the harness process tree's CPU, procs
and RSS, loadavg, limit blocks), **messages** (the newest
transcript tailed live: assistant text, tool calls with the command or path,
errors, the result line; `o` shows tool output too; Claude stream-json, Codex
and Pi event lines all parse), **overseer** (a history strip of one dot per
verdict -- green for an iteration that went through, red for one that hit
something: `stuck`, `revert`, or a `done` the overseer rejected -- then the
last verdict with its reason and reply. Six lettered glyphs asked a reader to
decode a legend at a glance, which is the one thing a glance cannot do; the
only question the strip answers is whether the run is moving), **plan · short** (the `short` plan node's items, read from the
hypergraph plan view or the handoff plan file), and **loop.log**. For the feed
to be live the headless backend streams each call's stdout to the transcript
file line by line, and Claude runs with `stream-json` in every backend.

`ouroboros report` renders the run into `REPORT.md`: bets changed, the plan as
it stands, verdict counts, the decisions the overseer made for you, cost.

## 16. Config

`.ouroboros/config.yml`, written by `ouroboros init` and the `ouroboros-design`
skill, editable by hand. Every key with its default:

```yaml
run: run                    # branch ouroboros/<run>, run dir .ouroboros/runs/<run>
goal: .ouroboros/goal.md    # the charter
memory: auto                # auto | hypergraph | handoff
backend: headless           # headless | tmux (one window per harness call)
mode: single                # single | actor-critic | council
overseer: agent             # agent | rules
idle_interval: 30m          # sleep after done_accepted under report_done
roles:                      # each role: harness, model, timeout, resume_for, fallback
  actor:      { harness: claude, model: null, timeout: 45m, resume_for: 1, fallback: [] }
  overseer:   { harness: claude, model: haiku, timeout: 3m }
  maintainer: { harness: claude, timeout: 20m }
  planner:    { harness: claude, timeout: 20m }
  critic:     { harness: claude, timeout: 15m }
council: []                 # extra critics in council mode: [{harness, model}]
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
handoff:
  recent: 3                 # handoff files shown to the actor
hypergraph:
  reconcile_every: 5        # maintainer pass after this many worked iterations
  pressure: 3               # ...or at this many unreconciled record nodes
  budget_units: 1           # dispatch budget per iteration
limits:
  cooldown: 30m             # block a limited harness this long when the reset time is unknown; doubles
  max_cooldown: 3h
  transient_strikes: 3      # 429/overloaded errors in a row that count as a limit
  reserve: {}               # harness -> 0..1: stop using it at this much of a long window
plan:
  enabled: null             # null = on; false turns the planner and plan view off
  every: 5                  # handoff repos: planner pass every N worked iterations
  view: plan                # the hypergraph view name
  md: PLAN.md               # its rendered snapshot
  max_new_directions: 1     # new directions per planner pass
loop:                       # motion-without-progress detection (section 6b)
  product_after: 8          # iterations changing only bookkeeping; null turns it off
  frontier_after: 30        # iterations with the frontier digest unmoved
  repeat_bet_after: 3       # planner bets restating a recent bet
  bet_similarity: 0.5       # content-word overlap that counts as a restatement
  bet_window: 6             # how many recent bets a new one is compared against
  escalate_every: 5         # iterations past the threshold per rung of the ladder
  rotate: true              # rung 3 may switch the actor to its fallback
```

A fallback is `{ harness: codex, model: null }`; the chain is the role's own
harness first, then its fallbacks in order (section 9).

CLI flags override config: `--run-name --for --max-iterations --max-cost --mode
--harness --model --allow-dirty --overseer --memory --foreground`.

## 17. Repo layout

```
ouroboros/
  pyproject.toml            # [project.scripts] ouroboros = "ouroboros.cli:main"; pydantic, pyyaml; pytest
  src/ouroboros/
    cli.py                  # init | design | preflight | run | stop | status | report | archive | skills install; menu; signals
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
      base.py  hypergraph.py  handoff.py   # orient/record prompts, verify, reconcile, plan view, planner prompt
    roles/
      actor.py  critic.py  overseer.py     # prompt assembly, JSON parsing, rules fallback, council
    gitguard.py             # branch, commit, tag, diff, revert_to
    budget.py               # BudgetClock, backoff table
    recorder.py             # jsonl, status.json, loop.log, NEEDS_HUMAN.md
    history.py              # the durable record: .ouroboros/history/<run>.md digests + RUNS.md index
    operator.py             # .ouroboros/AGENTS.md, the guide for the agent that drives runs in the target repo
    tmux.py                 # the run session: launch, kill
    tui/                    # the status TUI: theme, widgets, layout (from vllmtop), state (run-dir reader), panels, app
    skills/                 # packaged with the wheel; `ouroboros skills install` copies them out
      ouroboros-actor/  ouroboros-critic/  ouroboros-overseer/  ouroboros-maintainer/
      ouroboros-planner/  ouroboros-design/  ouroboros-launch/  ouroboros-checkup/   (each SKILL.md)
  tests/                    # 104 tests; fake_harness.py scripts actors, critics, overseers
    test_engine.py test_pool.py test_critic.py test_goal.py test_overseer.py
    test_memory_handoff.py test_memory_hypergraph.py (needs the hypergraph CLI)
    test_claude_parse.py test_drivers_parse.py test_headless.py test_backend_tmux.py (needs tmux)
    test_gitguard.py test_recorder.py test_config.py test_history.py test_operator.py
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
3. **Hypergraph adapter.** Orient / dispatch / record / reconcile mapping.
   Directive node per charter version. `check` after commit.
4. **Codex and Pi drivers.** Fallback chains and the limit board.
5. **Critic + modes.** `actor-critic`, `council`, revert on reject, tags.
6. **Skills.** `ouroboros-design` interview, `ouroboros-launch` start, `ouroboros-checkup` report.
7. **tmux backend.** One window per call.
8. **Charter and plan** (section 20): gaps from criteria, the plan view, the
   planner role, the overseer on the frontier.

## 19. Open questions, resolved

1. **Cost when the harness does not report it.** Pi reports real cost
   (`usage.cost.total`, every model priced). Codex reports tokens only; its cost
   stays zero. Claude reports API-equivalent dollars. Reports label the sum
   `api-equivalent` and no price table exists. Time and iterations are the
   real caps.
2. **Reject means revert.** Decided: on (`git.revert_on_reject`). A critic
   reject reverts the iteration and the next actor starts from `must_fix`.
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
| **Charter** | the human | only between runs, by editing `goal.md` | `.ouroboros/goal.md`: mission, constraints, question policy, quality bar, exhaustion policy, done criteria, first horizon ladder |
| **Plan** | the agents | every few iterations | the hypergraph: open state nodes (gaps), `Bet:` decision records, and a `plan` view rendered to `PLAN.md` |

No agent role may edit the charter. An agent that can edit the criteria can
meet the criteria by editing. The human overrules the agents by writing a new
charter version; Ouroboros mints a new directive record for it.

### Four grains of goal

| Grain | Size | Where it lives | Who writes it |
|---|---|---|---|
| Unit | one iteration | the dispatch record node of the iteration | actor |
| Gap | several units | open, broken, blocked state nodes on the frontier | maintainer, from declared impacts |
| Bet | a direction | `Bet:` decision records, folded into the `plan` view (`short`, `medium`, `long`) | planner |
| Mission | the whole | the charter | human |

Grains are sizes, not times (decided 2026-09-06 before nt2). Agents have no
clock: "this week" means nothing to an actor that sees one iteration, and a
charter written for a night is wrong on day two of a week. The ladder rungs are
`short-term` (units), `medium-term` (gaps), `long-term` (directions and standing
work), and the planner reads the run budget from the loop signals (iterations
and hours elapsed and left), never from the charter. Charters with the old
`next hour … next year` rungs still parse: hour and day fold to short, week to
medium, month and year to long.

### Mechanics

1. **Done criteria become gaps.** The directive record node declares one
   `NEW <gap>` impact per done criterion. The maintainer folds them into open
   state nodes. Work falsifies them through the ordinary impact channel
   (`target: <gap> — open → working`). A later charter version declares `NEW`
   only for criteria it adds; criteria it drops are named in the body so the
   maintainer can mark them superseded.
2. **The horizon ladder becomes the `plan` view.** Ouroboros declares the view
   (`hypergraph views add plan --md PLAN.md --reconcile`) at run start when
   missing. The directive seeds three view nodes through impacts:
   `plan/NEW short`, `plan/NEW medium`, `plan/NEW long`, one per ladder rung.
   Each node's `## Current` is the ranked plan at that grain, with citations.
   The `short` node holds the next two or three units, never a task list.
3. **The planner writes bets.** A planner pass runs right after every maintainer
   pass (about every five iterations), and after a `done_accepted` verdict. It
   reads the charter, `STATE.md`, `PLAN.md`, the pending plan impacts, and the
   night's record nodes. It writes exactly one `Bet:` decision record whose
   impacts target the plan view, then, as the plan view's single writer, folds
   them, advances the view's high-water mark, and commits `plan:`. When the
   frontier is empty, the bet must propose three directions, pick one, and name
   the mission item it serves. This replaces the `creative` exhaustion policy.
4. **Single writer per view.** The maintainer writes the state graph and never
   the plan view. The planner writes the plan view and never the state graph.
   Actors write neither.
5. **The overseer reads the frontier and the plan,** not the checklist. "Done"
   means no charter-derived gap is open.
6. **The check-in leads with the bets the planner changed this run,** each with its
   why, then the current `PLAN.md`, then the decisions the overseer made for
   you.

### Planner authority

- May re-rank and add freely.
- May retire a charter-derived gap only by proposing status `blocked` or
  `superseded` with a cited reason. Never delete.
- At most `plan.max_new_directions` new directions per planner pass (default one).
  Each names the mission item it serves.
- Every plan change is a record node with `## Why`. Every plan node cites its
  bets. `hypergraph check` keeps that honest.

### Without hypergraph

Handoff repos get the same three horizons in `.ouroboros/PLAN.md` and a bets
log in `.ouroboros/bets.md`, written by the same planner role every
`plan.every` iterations. Same rules, less audit. Set `plan.enabled: false` to
turn the self-evolving layer off in either memory.

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
| The frontier had 3 open nodes and the fine grain lived only in goal.md, so the actor was steered by a document nothing updates. | The plan view (`short` / `medium` / `long`) and the planner role. |
| The horizon ladder worked: the actor took rungs in order and never ran dry. | Kept as the seed of the plan. |
| The tmux session was killed from outside; the loop crashed on SIGHUP because the logger raised EIO. | Logger falls back to file only; SIGHUP keeps looping; `ouroboros stop`; status shows the pid. |
| An orphaned `claude -p` survived the tmux kill. | Children run in their own process group and are killed on SIGTERM/Ctrl-C; tmux windows too. |
| The actor reconciled itself once in a work iteration. | An explicit forbidden-verbs list in the orient prompt; the maintainer skill forbids the plan view in turn. |
| Edited goal on restart was not recorded. | One directive per charter hash, parented on the previous one, declaring only the criteria it adds and naming the ones it drops. |
| Outcome: 22 productive iterations, 14 record nodes, ADR-186..198, ~$81 API-equivalent, merged with a merge commit (173 commits, 132 of them empty). | The empty commits are why "no change, no commit" is a rule now. |
