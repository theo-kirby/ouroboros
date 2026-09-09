---
name: ouroboros-launch
description: Start an Ouroboros run on this repo. Reads what the last runs did, revises the charter with the user, runs the pre-flight, picks the run name and stop rules, launches, and says what to watch. Use when the user asks to kick off, start, or set up a run.
---

# Start a run

The user wants a loop working on this repo unattended, for hours or days. Your
job is the twenty minutes before it starts, because almost everything that
wastes a run is decided there and almost nothing can be fixed once it is going.

Read `.ouroboros/AGENTS.md` first if it exists: it is this project's own answer
to where runs happen, how they are named, and what green means here.

## 1. Read what came before

```
cat .ouroboros/RUNS.md
```

Then the last run's digest, `.ouroboros/history/<run>.md`, **notes first**. The
notes are what a human concluded and are the only part not derivable from logs.

Look at the `ticked` column across runs. It is what each run closed on the
charter. If the last two or three runs ticked nothing while writing hundreds of
commits, say so before anything else and treat the charter as the thing to fix:
a criterion nobody can tick will not become tickable by running longer.

## 2. The charter

`.ouroboros/goal.md` is the one document the human owns. No agent role writes
it, including you when a run is live. Read it with the user and ask what has
changed since the last run. To rewrite it by interview, use `ouroboros-design`.

What to push back on:

- **A done criterion must be a claim one run can make true.** "The walk takes a
  mechanism from design to review with no human step" is four criteria wearing
  one box, and it will read as unfinished after every run. Split it.
- **Criteria that are already true** belong checked, or in `## Later criteria`
  where nothing is seeded. Only unchecked boxes under `## Done criteria` become
  gaps on the frontier, and that seeding happens **once**, at the first start of
  a run name.
- **No clocks.** The ladder is granularity, not time. A unit gated on hours or
  iterations is a unit the actor cannot satisfy, and that is what stalled nt3
  for 113 iterations.
- **The quality bar must name a command.** "Tests pass" is not checkable; the
  exact test invocation is.

## 3. Pre-flight

```
ouroboros preflight
```

It checks the repo, the charter, every role's harness chain, and the logins, and
it warns about a subscription window the last run left full. Read the warnings:
a spent window does not fail a launch, it makes the run sleep through its own
wall clock.

What `preflight` cannot check, and you should:

- `git status` with **untracked directories** — the tree must be clean.
- The memory tool's version against what `.ouroboros/config.yml` expects.
- The project's own suites, green, right now. A run that starts red cannot tell
  its own breakage from the one it inherited.
- The machine. If runs belong on a box, be on the box.

## 4. Name it and set the stops

Edit `.ouroboros/config.yml`:

- `run:` — a fresh name starts a fresh frontier and a fresh branch. **Reusing a
  name continues that run**: same branch, same iteration count, and the charter
  gaps are not re-seeded. Both are useful; be deliberate about which.
- `stop:` — `after:` is the wall clock. A usage window is not a good stop: it
  refills, and the loop waits it out on its own.
- Roles and models. The critic burns a window as fast as the actor does; ot4
  left Codex at 99% and locked it out for five days.

## 5. Launch

```
ouroboros run --for 24h
```

If you are inside a tmux session (you usually are: the user opened one to talk
to you in), the loop opens as a new window of that session, `ouroboros-<run>`,
beside this one. Nothing changes in your window. `--own-session` puts it in a
detached session of its own instead, and a bare terminal always does that.

Confirm it is really alive before you tell the user it is:

```
ouroboros status
```

Then tell them, in three lines: what it will work on, when it stops, and where
to look. The place to look is another window of this same session running
`ouroboros top`; `ouroboros status` is the one-shot version, and the
`ouroboros-checkup` skill is for the morning. You stay here: they can ask you how
it is going at any time, and you answer from the run directory, not from memory.

## While it runs

Do not edit files the loop is working in. A charter edit applies at the next run
start, not this one — say so if the user asks for one. If something is wrong
enough to fix now, the honest options are `ouroboros stop`, fix, and start again
under the same run name.
