# Ouroboros — design

**One line:** Ouroboros is a loop runner for agent harnesses. You give it a goal.
It runs Claude Code, Codex, or Pi again and again, never blocks on a question,
never believes "done" too early, and leaves a clean memory trail for the morning.

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
ouroboros skills install      # → ./.claude/skills, ./.agents/skills (--user for ~/.claude/skills)
ouroboros init                # → .ouroboros/config.yml + goal.md (the charter)
ouroboros run --for 10h       # runs inside tmux session ouroboros-<run>
ouroboros status --watch      # what is it doing right now; which harness; limit blocks
ouroboros stop                # SIGTERM the loop and its children, then the tmux session
ouroboros report              # REPORT.md: bets, plan, verdicts, cost
```

A second `ouroboros run` with the same run name continues the run: iteration
numbers, the ok tag, and the branch carry over.

Python 3.12+, `uv`, no daemon, no database. All state is files in the repo.

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
| **ORIENT** | Read the charter, the memory (STATE.md, the plan, the unreconciled tail, or the recent handoffs), the overseer's message. Pick the next unit from the plan's `now` horizon or a frontier node. | actor (headless call) |
| **WORK** | Do one bounded unit of work. Budget: N units (default 1). | actor (same call) |
| **RECORD** | Write the memory entry for that unit (hypergraph record node or handoff file). | actor (same call) |
| **COMMIT** | Git guard commits whatever changed on the run branch. No change, no commit. | ouroboros (local) |
| **CRITIC** | `actor-critic` and `council` modes, only when something changed. Reads the diff with read-only tools. `accept` or `reject` with `must_fix`. | critic (headless call) |
| **OVERSEE** | Read the actor's final message, the frontier and plan, the critique, the diff stat. Did it ask a question? Did it claim done? Is it stuck? Answer as the user would. | overseer (headless call, cheap model) |
| **RECONCILE** | Hypergraph only. Fold recorded impacts into the state graph. Runs after `reconcile_every` worked iterations, at `pressure` unreconciled nodes, or at once after a directive that declared gaps. | maintainer (headless call) |
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
- RECONCILE triggers: every `reconcile_every: 5` iterations, **or** when ORIENT
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
  plan (`now` / `soon` / `later`); for handoff repos the plan file and the last
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
{ "verdict": "continue | answer | done_rejected | done_accepted | stuck | revert",
  "reply": "text injected into the next prompt",
  "reason": "one sentence for the log" }
```

Triggers, in order:

| Signal in the actor output | Overseer action |
|---|---|
| Ends with a question | Answer it using the goal doc's **question policy**. Inject the answer. |
| Claims "done" or "complete" | Check against **done criteria**. Usually `done_rejected` + "here is what is still open". |
| No file changes for 3 iterations | `stuck`. Inject a nudge from the **exhaustion policy**. |
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
- Never force-push. Never touch `main`. Never rebase. The morning merge is
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
  until: "2026-09-06T07:30"
  on_done_accepted: 3     # overseer accepted "done" this many times in a row
```

**What "cost" means.** Claude Code prints `total_cost_usd` in its JSON result.
It computes that number from token counts at API list prices, even when you
are logged in with a subscription and nothing is billed per call. Ouroboros
sums it and labels it *API-equivalent cost*: a measure of how much work the
run did, and a cap you can set, not a bill. Codex and Pi report tokens rather
than dollars; their cost stays at zero until a price table exists.

Resilience. These are not stop conditions. The loop absorbs them:

| Event | Response |
|---|---|
| Harness exits non-zero (a plain error) | Log it. Retry once after 5 s. Then give up the iteration: no commit, the error rides into the next prompt. |
| Timeout (`roles.<role>.timeout`, default 45 min) | Kill the process group (or the tmux window). Commit whatever landed. Continue. |
| Rate limit / 429 / "overloaded" | Backoff: 1m, 2m, 5m, 10m, then 10m forever. Never exit. Log every wait. Three in a row count as a usage limit. |
| Usage limit ("session limit · resets 2:50am (Europe/Madrid)", `usage_limit_reached`) | Switch to the next harness in the chain. With no chain, sleep until one minute past the reset (capped at 6 h) or a growing cooldown when the message has no time. |
| Auth expired | Block the harness on the limit board and use the next one. With no chain, backoff 10m and retry forever. Write `NEEDS_HUMAN.md` so the morning read shows it first. |
| Context exhausted mid-call | The call ends. The recorder notices no record. Next prompt starts with "record first". |
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
## Horizon ladder     — `- **the next hour:**` … day, week, month, year.
                        the first plan; the planner re-plans from it.
## Constraints        — never do X. always keep Y green. stay inside dir Z.
## Question policy    — how to decide for me. e.g. "prefer the reversible option".
## Exhaustion policy  — creative | maintain | report_done
   - creative:  the planner proposes three directions, picks one, continues
   - maintain:  tests, flakes, docs, refactors, forever
   - report_done: overseer may accept done; loop idles and polls every idle_interval
## Quality bar        — what the critic grades against (with Constraints).
## Reconcile          — hypergraph only: prose for the maintainer and planner.
```

The horizon ladder is the trick. A goal that has a "next year" line never
runs out of work. With the planner on, the ladder is only the first plan.

## 14. Skills

Installed by `ouroboros skills install`. Plain `SKILL.md` folders. They work in
Claude Code, Codex, and Pi (Pi gets `--skill` on every call).

| Skill | What it does |
|---|---|
| `ouroboros-design` | The interview (built). Reads the repo first. Then asks, in rounds: mission, done criteria as claims, each rung of the horizon ladder, constraints, question policy, exhaustion policy, quality bar, run shape. Refuses to finish until every rung has at least 3 concrete items. Writes the charter `goal.md` and a matching `config.yml`; never the plan. |
| `ouroboros-morning` | The morning read (built). Blockers first, then what landed, the bets the planner changed (so you can overrule), decisions the overseer made for you, critic rejects and reverts, how the frontier moved, what to review hardest, a merge recommendation, and charter changes to consider. |
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
  status.json                # state, iteration, harness, api-equivalent cost, elapsed, last verdict, limit blocks
  transcripts/NNNN-<role>[-attempt].json   # raw harness output (+ .stderr); roles: actor, critic[N], overseer, maintainer, planner
  reverted/NNNN.patch        # what a revert threw away
  NEEDS_HUMAN.md             # only exists while something needs you (auth failure)
  REPORT.md                  # written by `ouroboros report`
```

`status.json` states: `starting`, `work`, `critique`, `oversee`, `reconcile`,
`plan`, `backoff` (with `why` and `seconds`), `idle`, `stopped`, `killed`.
`limited` lists blocked harnesses with the minutes left.

tmux layout when you attach: pane 1 is the loop log, pane 2 shows
`ouroboros status --watch`. With `backend: tmux`, each harness call also opens
its own window while it runs.

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
  until: null               # ISO timestamp
  on_done_accepted: null    # this many done_accepted in a row
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
plan:
  enabled: null             # null = on; false turns the planner and plan view off
  every: 5                  # handoff repos: planner pass every N worked iterations
  view: plan                # the hypergraph view name
  md: PLAN.md               # its rendered snapshot
  max_new_directions: 3
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
    cli.py                  # init | run | stop | status | report | skills install; preflight; signals
    engine.py               # the state machine (section 6): step(), retry policy, reconcile, plan, critique
    config.py               # pydantic models for config.yml (section 16)
    goal.py                 # the charter parser: sections, done criteria → gap names, ladder, policies
    harness/
      base.py               # Harness protocol, Result (+ kind: ok|timeout|limit|auth|transient|error, reset time parsing)
      claude.py  codex.py  pi.py
      pool.py               # LimitBoard (per run) + PooledHarness (per role): fallback chains
      backend_headless.py   # subprocess in its own process group, timeouts, kill_active, backend switch
      backend_tmux.py       # one tmux window per call, output to files
    memory/
      base.py  hypergraph.py  handoff.py   # orient/record prompts, verify, reconcile, plan view, planner prompt
    roles/
      actor.py  critic.py  overseer.py     # prompt assembly, JSON parsing, rules fallback, council
    gitguard.py             # branch, commit, tag, diff, revert_to
    budget.py               # BudgetClock, backoff table
    recorder.py             # jsonl, status.json, loop.log, NEEDS_HUMAN.md
    tmux.py                 # the run session: launch, kill
    skills/                 # packaged with the wheel; `ouroboros skills install` copies them out
      ouroboros-actor/  ouroboros-critic/  ouroboros-overseer/  ouroboros-maintainer/
      ouroboros-planner/  ouroboros-design/  ouroboros-morning/     (each SKILL.md)
  tests/                    # 104 tests; fake_harness.py scripts actors, critics, overseers
    test_engine.py test_pool.py test_critic.py test_goal.py test_overseer.py
    test_memory_handoff.py test_memory_hypergraph.py (needs the hypergraph CLI)
    test_claude_parse.py test_drivers_parse.py test_headless.py test_backend_tmux.py (needs tmux)
    test_gitguard.py test_recorder.py test_config.py
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
6. **Skills.** `ouroboros-design` interview, `ouroboros-morning` report.
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
3. **Skills for Codex.** Decided: the role prompt text is inlined in every
   call; `skills install` also copies to `.claude/skills` and `.agents/skills`
   for humans and interactive sessions.

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

| Grain | Horizon | Where it lives | Who writes it |
|---|---|---|---|
| Unit | this hour | the dispatch record node of the iteration | actor |
| Gap | this week | open, broken, blocked state nodes on the frontier | maintainer, from declared impacts |
| Bet | this month | `Bet:` decision records, folded into the `plan` view (`now`, `soon`, `later`) | planner |
| Mission | this year | the charter | human |

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
   `plan/NEW now` (hour and day rungs), `plan/NEW soon` (week),
   `plan/NEW later` (month and year). Each node's `## Current` is the ranked plan
   at that horizon, with citations. The `now` node holds the next two or three
   units, never a task list.
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
6. **The morning report leads with the bets changed tonight,** each with its
   why, then the current `PLAN.md`, then the decisions the overseer made for
   you.

### Planner authority

- May re-rank and add freely.
- May retire a charter-derived gap only by proposing status `blocked` or
  `superseded` with a cited reason. Never delete.
- At most three new directions per night. Each names the mission item it serves.
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
3. Plan view, planner role, morning report.
4. `actor-critic` and `council` modes; a Codex critic on Claude work.
5. `ouroboros-design` interview rewritten for the charter; `ouroboros-morning`.
6. tmux backend.

## 21. What the first night taught (cadex nt1, 2026-09-05/06)

Facts from the run, and what changed because of each.

| Observed | Change |
|---|---|
| "You've hit your session limit · resets 2:50am (Europe/Madrid)" was not recognised as retriable; 63 of 90 iterations were empty commits, pointless reconcile passes, and rules-fallback `stuck`/`revert` verdicts on 0-byte patches. | `Result.kind` classifies limits; reset time parsed from five message shapes; wait until one minute past the reset (cap 6 h); fallback chains switch harness instead of waiting; a limit with no time gets a doubling cooldown. |
| The actor committed on its own, so the loop saw a clean tree and counted "no changes" streaks while work was landing. | Changed = HEAD moved or tree dirty. |
| Failed iterations still committed (empty), ticked the reconcile counter, and triggered maintainer passes. | A failed iteration commits nothing, counts nothing, reconciles nothing. Empty commits are gone entirely. |
| At iteration 50 the overseer rejected "done" with "file lifecycle 0/3" while all three had shipped: it counted boxes in a static file. | The charter/plan split (section 20). Criteria become gaps on the frontier; the overseer judges against the frontier and the plan; a ticked box declares no gap. |
| The frontier had 3 open nodes and the fine grain lived only in goal.md, so the actor was steered by a document nothing updates. | The plan view (`now` / `soon` / `later`) and the planner role. |
| The horizon ladder worked: the actor took rungs in order and never ran dry. | Kept as the seed of the plan. |
| The tmux session was killed from outside; the loop crashed on SIGHUP because the logger raised EIO. | Logger falls back to file only; SIGHUP keeps looping; `ouroboros stop`; status shows the pid. |
| An orphaned `claude -p` survived the tmux kill. | Children run in their own process group and are killed on SIGTERM/Ctrl-C; tmux windows too. |
| The actor reconciled itself once in a work iteration. | An explicit forbidden-verbs list in the orient prompt; the maintainer skill forbids the plan view in turn. |
| Edited goal on restart was not recorded. | One directive per charter hash, parented on the previous one, declaring only the criteria it adds and naming the ones it drops. |
| Outcome: 22 productive iterations, 14 record nodes, ADR-186..198, ~$81 API-equivalent, merged with a merge commit (173 commits, 132 of them empty). | The empty commits are why "no change, no commit" is a rule now. |
