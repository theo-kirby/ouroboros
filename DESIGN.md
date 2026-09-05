# Ouroboros — design

**One line:** Ouroboros is a loop runner for agent harnesses. You give it a goal.
It runs Claude Code, Codex, or Pi again and again, never blocks on a question,
never believes "done" too early, and leaves a clean memory trail for the morning.

Status: design v0. Phase 1 (walking skeleton) is built and smoke-tested.

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
uv tool install ouroboros
ouroboros skills install      # → ./.claude/skills, ./.agents/skills
ouroboros init                # → .ouroboros/config.yml
ouroboros run --for 10h       # runs inside tmux
ouroboros status              # what is it doing right now
ouroboros report              # the morning read
```

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
      ├── Harness driver     .................. claude | codex | pi   (section 9)
      │    └── Backend       .................. headless | tmux
      ├── Memory adapter     .................. hypergraph | handoff  (section 7)
      ├── Overseer           .................. answers, judges, unsticks (section 8)
      ├── Git guard          .................. branch, commit, tag, revert (section 11)
      ├── Budget clock       .................. time, iterations, cost (section 10)
      └── Recorder           .................. JSONL log, status, report (section 12)
```

Every box is a Python module with one job. Every box talks to the others
through plain data classes, never through a harness-specific type.

## 6. The iteration (state machine)

One iteration is one pass through this machine. Every step is one headless
call or one local command. Every step has a timeout. A timeout is not a
failure; it is a recorded event and the loop continues.

```
     ┌────────────────────────────────────────────────────────┐
     │                                                        │
     ▼                                                        │
  ORIENT ──► WORK ──► RECORD ──► COMMIT ──► CRITIC? ──► OVERSEE ──► RECONCILE?
```

| Step | What happens | Who runs it |
|---|---|---|
| **ORIENT** | Read the goal, the memory, the frontier. Pick the next unit of work. | actor (headless call) |
| **WORK** | Do one bounded unit of work. Budget: N units (default 1). | actor (same call) |
| **RECORD** | Write the memory entry for that unit (hypergraph record node or handoff file). | actor (same call) |
| **COMMIT** | Git guard commits everything on the run branch. Never skipped. | ouroboros (local) |
| **CRITIC** | Optional. Read the diff and the record. Verdict: `accept`, `revise`, `reject`. | critic (headless call) |
| **OVERSEE** | Read the actor's final message. Did it ask a question? Did it claim done? Is it stuck? Answer as the user would. | overseer (headless call, cheap model) |
| **RECONCILE** | Optional. Fold recorded impacts into the state graph. Runs every N iterations or when orient reports pressure. | maintainer (headless call) |

**ORIENT, WORK, and RECORD are one call**, not three. The prompt tells the
agent to do all three. Splitting them wastes context and money. The recorder
checks after the call that RECORD really happened. If not, the next iteration
starts with "you forgot to record, do it first."

**Fresh context every iteration.** Each call is a new session. The agent reads
memory from disk. Optional: `resume_for: N` keeps one session for N iterations,
then resets. Default is 1 (always fresh).

**The actor's last message is data.** The overseer reads it. It does not reach
a human. Any question in it gets answered by the overseer and injected as the
first line of the next prompt.

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
- The critic reads the record node, not just the diff. A unit with no record
  node is a `revise`.
- `hypergraph check --since <run-start>` runs before every commit. A failing
  check does not stop the loop. It becomes the next unit of work.
- The goal doc is recorded once, at run start, as an Operator-directive
  decision node. Every dispatch node in the run has it as a causal ancestor.

If the target repo does not have hypergraph, `ouroboros init` offers to run
`hypergraph-init`. It does not force it.

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

The overseer is the sleeping user. It is a cheap, fast headless call. It gets:

- the goal doc,
- the actor's final message,
- the git diff stat for the iteration,
- the last 3 overseer decisions (so it does not flip-flop).

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

### tmux backend (second)

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

## 10. Modes

A mode is a pipeline of roles run per iteration. Built-ins:

| Mode | Pipeline | Use when |
|---|---|---|
| `single` | actor → overseer | cheap, simple goals |
| `actor-critic` | actor → critic → overseer | default. critic can `revise` (next iteration fixes) or `reject` (revert) |
| `council` | actor → critic×N (parallel) → overseer merges verdicts | high-stakes work; majority or unanimous rule |
| `ping-pong` | actor A → actor B (different harness) alternate iterations | when one harness keeps missing the same thing |
| `plan-execute` | planner every K iterations → actor otherwise | very long runs; keeps the plan fresh |

Custom modes are a list in config. No code needed:

```yaml
mode:
  pipeline: [actor, critic, overseer]
  critic_rule: majority        # for council
  plan_every: 10               # for plan-execute
```

## 11. Git guard

- On start: create `ouroboros/<run-name>` from the current HEAD. Refuse to run
  on a dirty tree unless `--allow-dirty`.
- After every WORK: `git add -A && git commit -m "ouroboros #<n>: <summary>"`.
  The summary is the first line of the actor's record. Always commits, even
  if the only change is the memory file.
- After `critic: accept`: tag `ouroboros/<run>/ok-<n>`. This is the revert
  target.
- After `critic: reject` or `overseer: revert`: `git revert` back to the last
  `ok-*` tag. The reverted diff is saved to `runs/<run>/reverted/<n>.patch`
  so the memory can cite it as a dead end.
- Never force-push. Never touch `main`. Never rebase. The morning merge is
  yours.

## 12. Durations, budgets, and never stopping

Stop conditions. Any one of them ends the run cleanly. All are optional. With
none set, the loop runs until you kill it.

```yaml
stop:
  after: 10h              # wall clock
  max_iterations: 200
  max_cost_usd: 50        # best effort; harnesses that do not report cost are estimated
  until: "2026-09-06T07:30"
  on_done_accepted: 3     # overseer accepted "done" this many times in a row
```

Resilience. These are not stop conditions. The loop absorbs them:

| Event | Response |
|---|---|
| Harness exits non-zero | Log it. Retry once. Then continue to the next iteration with the error in the prompt. |
| Timeout (default 45 min per call) | Kill the process group. Commit whatever landed. Continue. |
| Rate limit / 429 / "overloaded" | Backoff: 1m, 2m, 5m, 10m, then 10m forever. Never exit. Log every wait. |
| Auth expired | Backoff 10m and retry forever. Write `NEEDS_HUMAN.md` so the morning read shows it first. |
| Context exhausted mid-call | The call ends. The recorder notices no record. Next prompt starts with "record first". |
| Disk / git error | Log, wait 1m, retry. After 5 failures, freeze WORK and run only ORIENT+OVERSEE every 10m so the run stays alive and visible. |
| Ouroboros itself crashes | `ouroboros run` writes a pid file and a checkpoint every step. `ouroboros resume <run>` picks up at the last step. The tmux session is named `ouroboros-<run>` so you can find it. |

## 13. The goal doc (the contract)

`.ouroboros/goal.md` is the one document every role reads. The `ouroboros-design`
skill writes it through an interview. Its sections are fixed:

```markdown
# Goal: <name>

## Mission            — one paragraph. what and why.
## Done criteria      — a checklist. the overseer tests "done" against this.
## Horizon ladder     — what to do if this runs for:
   - the next hour
   - the next day
   - the next week
   - the next month
   - the next year        ← the interview forces you to fill this in
## Constraints        — never do X. always keep Y green. stay inside dir Z.
## Question policy    — how to decide for me. e.g. "prefer the reversible option",
                        "when unsure, match the existing style", "never add deps".
## Exhaustion policy  — when the ladder is empty:
                        creative | maintain | report_done
   - creative:  propose and record 3 new directions, pick one, continue
   - maintain:  run tests, fix flakes, improve docs, refactor, forever
   - report_done: overseer may accept done; loop idles and polls every 30m
## Quality bar        — what the critic grades against.
## Reconcile          — hypergraph only: how often, what counts as pressure.
```

The horizon ladder is the trick. A goal that has a "next year" line never
runs out of work.

## 14. Skills

Installed by `ouroboros skills install`. Plain `SKILL.md` folders. They work in
Claude Code, Codex, and Pi (Pi gets `--skill` on every call).

| Skill | What it does |
|---|---|
| `ouroboros-design` | The interview. Reads the repo first. Then asks, in rounds: mission, done criteria, each rung of the horizon ladder, constraints, question policy, exhaustion policy, quality bar. Refuses to finish until every rung has at least 3 concrete items. Writes `goal.md` and a matching `config.yml`. |
| `ouroboros-morning` | The morning read. Summarizes the run: iterations, accepted vs reverted, open questions the overseer answered for you (so you can overrule), what the critic kept rejecting, cost, and a merge recommendation. |
| `ouroboros-actor` | Internal. The role prompt for WORK. Includes the memory adapter's orient and record instructions. |
| `ouroboros-critic` | Internal. The role prompt for CRITIC. Grades against the quality bar, returns strict JSON. |
| `ouroboros-overseer` | Internal. The sleeping user. Returns strict JSON (section 8). |

Role prompts live as skills, not as Python strings, so you can edit them
without touching code and so a harness can load them natively.

## 15. Observability

```
.ouroboros/runs/<run>/
  run.yml              # frozen config + start time + branch + harness versions
  iterations.jsonl     # one line per step: ts, step, role, harness, session_id, cost, verdict, commit
  overseer.jsonl       # every decision the overseer made for you
  transcripts/<n>-<role>.json    # raw harness JSON
  reverted/<n>.patch
  status.json          # current step, iteration, uptime, cost so far — updated every step
  NEEDS_HUMAN.md       # only exists when something needs you
```

tmux layout when you attach: pane 1 tails the loop log, pane 2 shows
`ouroboros status --watch`. `ouroboros report` renders `iterations.jsonl` and
`overseer.jsonl` into `REPORT.md` for the morning.

## 16. Config

`.ouroboros/config.yml`, written by `ouroboros-design`, editable by hand:

```yaml
run: nightly-refactor
goal: .ouroboros/goal.md
memory: auto            # auto | hypergraph | handoff
backend: headless       # headless | tmux
mode: actor-critic
roles:
  actor:    { harness: claude, model: opus,  timeout: 45m, resume_for: 1 }
  critic:   { harness: codex,  model: gpt-5-codex, timeout: 15m }
  overseer: { harness: claude, model: haiku, timeout: 3m }
  maintainer: { harness: claude, model: opus, timeout: 20m }   # RECONCILE
hypergraph:
  reconcile_every: 5
  budget_units: 1
git:
  branch: ouroboros/nightly-refactor
  tag_on_accept: true
  revert_on_reject: true
stop:
  after: 10h
  max_cost_usd: 60
```

CLI flags override config. `ouroboros run --for 2h --mode single` is enough for
a quick test.

## 17. Repo layout

```
ouroboros/
  pyproject.toml            # [project.scripts] ouroboros = "ouroboros.cli:main"
  src/ouroboros/
    cli.py                  # run | resume | status | report | init | skills
    engine.py               # the state machine (section 6)
    config.py               # pydantic models for config.yml + goal.md front matter
    harness/
      base.py               # Harness protocol, Result
      claude.py  codex.py  pi.py
      backend_headless.py   # subprocess + JSON parsing + timeouts + backoff
      backend_tmux.py       # send-keys / capture-pane driver
    memory/
      base.py  hypergraph.py  handoff.py
    roles/
      actor.py  critic.py  overseer.py  maintainer.py   # prompt assembly only
    gitguard.py
    budget.py
    recorder.py             # jsonl, status.json, REPORT.md
    tmux.py                 # session/pane management
    skills/                 # packaged with the wheel; `ouroboros skills install` copies them out
      ouroboros-design/SKILL.md
      ouroboros-morning/SKILL.md
      ouroboros-actor/SKILL.md
      ouroboros-critic/SKILL.md
      ouroboros-overseer/SKILL.md
  tests/
    fake_harness.py         # a scripted harness that asks questions, claims done, crashes
    test_engine.py          # the loop against the fake harness: never blocks, never stops
    test_gitguard.py
    test_memory_handoff.py
    test_memory_hypergraph.py
  DESIGN.md                 # this file
```

## 18. Build order

Each phase ends with something you can run overnight.

1. **Walking skeleton.** `single` mode, Claude only, handoff memory, git
   guard, `--for`, JSONL log. Fake harness tests prove "never blocks".
2. **Overseer.** JSON verdicts, question answering, done rejection, rules
   fallback, backoff table.
3. **Hypergraph adapter.** Orient / dispatch / record / reconcile mapping.
   `check --since` before commit.
4. **Codex and Pi drivers.** Same tests, three harnesses.
5. **Critic + modes.** `actor-critic`, `council`, revert on reject, tags.
6. **Skills.** `ouroboros-design` interview, `ouroboros-morning` report.
7. **tmux backend.** Last, because it is the fragile one.

## 19. Open questions

Three things I could not decide from the repo or the tools. Defaults are set;
say if you want different ones.

1. **Cost when the harness does not report it.** Codex and Pi do not return
   dollars. Default: estimate from token counts with a price table in config,
   and mark the number `~` in reports. Alternative: ignore cost for those and
   only cap time.
2. **Reject means revert.** A critic `reject` reverts the whole iteration.
   Default: on. Alternative: `reject` only annotates and the next actor
   decides. Revert is safer for overnight. Annotate is cheaper.
3. **Skills for Codex.** Codex reads skills from a project directory, but the
   exact path changed across versions. Default: install to both
   `.claude/skills/` and `.agents/skills/`, and also inline the role prompt
   text in every call so no harness depends on skill discovery.
