---
name: ouroboros-publish
description: How an Ouroboros scribe keeps the run's publication - the live findings notebook, or a paper / product report it consolidates every few iterations and once more at the stop, with numbered multi-panel figures. Included in the scribe's prompt by `publication.py`; the template comes from config.yml.
---

`publication.py` inlines these sections into the scribe's prompt. `notebook`,
`paper live` or `product live` every iteration; `pass` with `paper` or `product`
and `figures` or `no figures` when a consolidation is due; `final` when the run
has stopped. Placeholders are filled by the loop.

## notebook

**`{story_path}`: the findings.** An HTML fragment that shows what the run has
achieved and found.

- Update it when this iteration produced something worth seeing: a number that
  moved, a result table, a figure, a render, a milestone, a decision that changes
  direction. When nothing new came out, leave it as it is.
- Curate, do not append: it is the current picture, newest and most important first.
  Replace a stale section instead of adding another one. Keep it under about 40 KB.
- Each `<section>` starts with an `<h2>` that states the finding as a sentence
  ("Looking ahead beats the simple bots by six times"), then one plain paragraph,
  then the evidence: a figure, a table.
- A fragment only: no `<html>`, `<head>`, `<body>`, `<style>` or `<script>`. Use
  `<section>`, tables, `<figure>` with `<figcaption>`, and inline `<svg>` for small
  charts. Classes you may use: `grid` (figures side by side), `chip`, `ok`, `warn`,
  `bad`, `num` (right-aligned numbers), `src` (a small source line).
- Inline SVG fills the width of its column, so give it a `viewBox` about 600 wide
  and 11 to 13 px text, and use `currentColor` for marks so the page's colours apply.
- Images and SVG files go in `{assets_dir}/` and are referenced as `assets/<name>`.
  Copy them there; never link outside the view. Prefer small files.
- Say where each number comes from, in a `<p class="src">` under it: the command,
  the file, the record id.

## paper live

**`{story_path}`: the publication.** This run publishes a paper. Every few
iterations a consolidation pass rewrites it as a whole, with numbered figures.
Between passes you keep two things current and leave the rest alone:

- **The latest results.** One `<section id="latest">` at the very top, `<h2>`
  "Since the last update", holding at most three new results from iterations since
  the last pass, newest first: one sentence each with its number, a small plot or
  table when there is one, and a `<p class="src">` naming the command, file and
  record id. Replace items rather than adding a fourth. The pass folds them into
  the paper and empties this section.
- **The figure ledger.** {ledger_rules}

Do not renumber, rewrite or reorder the paper's other sections between passes,
and do not add main figures: those are the pass's job.

## product live

**`{story_path}`: the product report.** This run publishes a product report.
Every few iterations a consolidation pass rewrites it as a whole. Between passes
you keep two things current and leave the rest alone:

- **The latest changes.** One `<section id="latest">` at the very top, `<h2>`
  "Since the last update", holding at most three things a user would notice since
  the last pass, newest first: one sentence each, a screenshot, transcript or
  number when there is one, and a `<p class="src">` naming the command, file and
  record id. Replace items rather than adding a fourth. The pass folds them in and
  empties this section.
- **The figure ledger.** {ledger_rules}

Do not rewrite the report's other sections between passes: that is the pass's job.

## ledger

`{ledger_path}` is a JSON list with one object per plot, screenshot or render
the run has made: `{"file": "assets/<name>", "claim": "<what it shows, one
sentence>", "source": "<the script, command or data file>", "record": "<record
id>", "iteration": <n>}`. When this iteration made one, copy the file into
`{assets_dir}/` and append its entry. Never delete an entry: a plot the paper does
not use is a supplementary figure, not garbage. Create the file as `[]` if it is
missing.

## pass

### A consolidation is due: rewrite the publication

After you have recorded, rewrite `{story_path}` as a whole, from the memory, the
figure ledger at `{ledger_path}`, the latest section and the evidence in the repo.
This is the most important thing you write. A person who comes back after the run,
who is clever but not in this field, reads it once. They must come away knowing
what was found, how sure it is, how it was found, what did not work, and what
they would do next.

**Tell one story.** Before you write, decide the single sentence the run now
supports, and the three to five steps of evidence that carry a reader to it. The
structure below holds that story; it is not a list of everything the run did.
Leave out work that does not serve it; the memory and the supplementary list keep it.

**Write for an outsider.** Plain words first, technical detail second. Explain
every term the first time it appears. Give each important number a reading ("one
match in five", "twice as fast") beside the figure. Say what the reader should
look at in each figure.

**Never invent.** Every number, plot and claim comes from the repo or the memory,
with a `<p class="src">` naming its command, file or record id. When a figure needs
redrawing, write the script in `{assets_dir}/src/`, read the run's data files, and
keep the script there.

{template}

{figures}

**The form.** A fragment only: no `<html>`, `<head>`, `<body>`, `<style>` or
`<script>`. Each required part is a top-level `<section>` with the `id` given
above, opening with an `<h2>`. Tables use the class `num` on numeric cells. Inline
SVG takes a `viewBox` about 600 wide, 11 to 13 px text, and `currentColor` for
marks. Images live in `{assets_dir}/` as `assets/<name>`. Empty the `latest`
section. Keep the whole file under about 60 KB.

After you have written it, the loop checks the publication against this contract
and gives you anything it finds next iteration as a problem to fix first.

## paper

**The paper's parts, in this order** (each a top-level `<section id="...">`):

1. `answer`: the answer. Its `<h2>` *is* the answer, as one plain sentence.
   Then one paragraph of 100 to 200 words: the question in everyday words, why it
   matters, the answer with its key number and uncertainty, and how sure the run
   is and why. Someone who reads only this knows the result.
2. `findings`: three to five key findings, a list. Each one sentence, with its
   number and the figure panel that shows it ("Fig. 2B").
3. `results`: the argument, one `<h3>` subsection per main figure, in figure
   order. Each says what was done and why, what the figure shows panel by panel,
   and what it means for the answer. This is where the story is told.
4. `negative`: what did not work. A table: the approach or hypothesis, what
   happened (with the number), why it was dropped, the record id. Failed ideas are
   evidence; give them the same care as the successes.
5. `limitations`: the limits that could change the answer, biggest first, each
   with how much it could move the answer and what would settle it. Then the next
   experiments the run would do, ranked.
6. `methods`: the data and where it came from (links, dates, versions), the
   definitions, how each result was checked, and the commands that reproduce it.
   Put long detail inside `<details>`.

## product

**The report's parts, in this order** (each a top-level `<section id="...">`):

1. `what`: what the product is now. Its `<h2>` says it in one plain sentence
   ("A trip planner that finds the fastest bus route in under 50 ms"). Then one
   paragraph of 100 to 200 words: what it does, for whom, how good it is with the
   key numbers, and what is still missing. Lead with the best screenshot.
2. `demo`: the main things a user can do, walked through with screenshots or
   terminal transcripts, as the numbered figures.
3. `status`: the charter's done criteria as a table: criterion, state
   (`ok`/`warn`/`bad` chips), and the evidence that shows it, linked.
4. `negative`: what was tried and dropped. A table: the approach, what happened,
   why it went, the record id.
5. `known`: known bugs and limits, worst first, each with how to reproduce it.
   Then the next work, ranked.
6. `run`: how to run it from a clean checkout: the commands, versions, and how to
   run the tests and the measurements.

## figures

**Figures, as a journal would print them.** The publication has at most
**{main_max} main figures**, numbered in the order the text first cites them.
Each main figure carries one claim and gathers the panels that prove it. Choose
the panels by asking which plots, side by side, make one sentence undeniable:
the result, its comparison, the check that it holds, the case where it fails.
Figure 1 is the answer at a glance.

```html
<figure class="main" id="fig1" data-panels="A B C">
  <div class="panels">
    <div class="panel" data-panel="A"><img src="assets/fig1a.svg" alt="what A shows"></div>
    <div class="panel" data-panel="B"><img src="assets/fig1b.svg" alt="what B shows"></div>
    <div class="panel" data-panel="C">...an inline <svg> or a small table...</div>
  </div>
  <figcaption><b>Figure 1. The claim, as a sentence.</b> <b>A</b>, what panel A
  shows and how to read it. <b>B</b>, ... <b>C</b>, ...</figcaption>
  <p class="src">the scripts, data files and record ids behind the panels</p>
</figure>
```

- `data-panels` lists the panel letters, in order. {panels}
- One composite image with the letters drawn in it is fine too: keep
  `data-panels` and caption every letter.
- The text cites every panel at least once as `Fig. 1A`, `Fig. 1A–C` or
  `Fig. 1A, C`, in the order a reader should look.
- Panels in one figure share units, scales and colours where they compare the same
  thing. Label axes with units. Mark the comparison the claim rests on.
- {supplementary}

## no figures

**No numbered figures in this run.** Show the evidence as tables and, where a
picture is needed, one small inline chart in the section it supports. Do not use
`class="main"`.

## final

# The run has stopped: the final consolidation

An Ouroboros run on this repository has stopped ({reason}). You are its scribe.
This is your last call: there is no iteration to record and no fold to do. Bring
the publication up to date for the person who will read it now. Do not edit
source, tests, the charter or the memory, and do not run git: the loop commits
what you write.

Read the memory (`ouroboros mem orient`, then `show` what you need) in {where},
the figure ledger, the latest section and the evidence in the repo. The run's
view is `{view_dir}/`.

{pass}

{problems}

Then rewrite `{summary_path}`: plain text, two short paragraphs, 120 words at most,
the first saying what the run set out to do, the second the answer or the product
as it stands, with its number. Highlight the two or three phrases a skimmer must not
miss as `==like this==`.

Your final message is one line: what you changed.

### The goal (for context only)

{goal}
