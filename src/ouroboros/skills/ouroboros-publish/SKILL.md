---
name: ouroboros-publish
description: How an Ouroboros scribe keeps the run's notebook (the run book of findings, every iteration) and writes the report at the end - a poster of three or four figures, or a paper or product report with numbered multi-panel figures. Included in the scribe's prompt by `publication.py`; the template comes from config.yml.
---

`publication.py` inlines these sections into the scribe's prompt. `notebook`
every iteration, with `notebook ledger` when the run will end in a report;
`poster pass` with `poster`, a `poster style` and `palette` for a poster, or `pass`
with `paper` or `product` and `figures` or `no figures`, when the report is written (once when the run stops, and as a mid-run draft every
`consolidate_every` worked iterations when that is set); `final` when the run has
stopped. Placeholders are filled by the loop.

## notebook

**`{story_path}`: the notebook.** An HTML fragment, the run book of what the run
has found and tried. It is notes, not prose: a reader checking in mid-run sees the
current findings, the evidence behind them, and what did not work. The polished
report is written separately at the end; do not write one here.

- Update it when this iteration produced something worth seeing: a number that
  moved, a result table, a plot, a render, a milestone, a decision that changes
  direction, an approach that failed. When nothing new came out, leave it as it is.
- Curate, do not append: it is the current picture, newest and most important first.
  Replace a stale section instead of adding another one. Keep it under about 40 KB.
- Each `<section>` starts with an `<h2>` that states the finding as a sentence
  ("Looking ahead beats the simple bots by six times"), then a few terse lines or
  bullets (the number, how it was measured, the caveat), then the evidence: a plot,
  a table. Short notes are better than polished paragraphs.
- Keep one `<section id="tried">` near the end: a short list of approaches that did
  not work, each with its number and why it was dropped.
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

## notebook ledger

**The figure ledger.** This run ends with a written report built from the
notebook and the ledger, so keep every plot findable. {ledger_rules}

## ledger

`{ledger_path}` is a JSON list with one object per plot, screenshot or render
the run has made: `{"file": "assets/<name>", "claim": "<what it shows, one
sentence>", "source": "<the script, command or data file>", "record": "<record
id>", "iteration": <n>}`. When this iteration made one, copy the file into
`{assets_dir}/` and append its entry. Never delete an entry: a plot the paper does
not use is a supplementary figure, not garbage. Create the file as `[]` if it is
missing.

## pass

### The report is due: write it

After you have recorded, write `{paper_path}` as a whole, from the notebook at
`{story_path}`, the memory, the figure ledger at `{ledger_path}` and the evidence
in the repo. The notebook stays as it is: it is the run book, this is the report.
This is the most important thing you write. A person who comes back after the run,
who is clever but not in this field, reads it once. They must come away knowing
what was found, how sure it is, how it was found, what did not work, and what
they would do next.

**Tell one story.** Before you write, decide the single sentence the run now
supports, and the three to five steps of evidence that carry a reader to it. The
structure below holds that story; it is not a list of everything the run did.
Leave out work that does not serve it; the memory and the supplementary list keep it.

**Build it on the claim tree.** `ouroboros mem tree` prints the memory's claims as
the run grew them: each done criterion's gap, and under it the sub-problems,
hypotheses and questions the work opened, with how many records each rests on. The
story is that tree: the findings are its working claims, each traced to the records
it rests on (cite the claim id beside the record id); what did not work comes from
its superseded hypotheses and `ruled out:` bullets; the open leaves and their
`Next:` lines are what the run would do next.

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
marks. Images live in `{assets_dir}/` as `assets/<name>`. Keep the whole file
under about 60 KB.

After you have written it, the loop checks the report against this contract; a
later pass gets what it finds as a problem to fix first.

## paper

**The paper's parts, in this order** (each a top-level `<section id="...">`):

1. `answer`: the answer. Its `<h2>` *is* the answer, as one plain sentence.
   Then one paragraph of 100 to 200 words: the question in everyday words, why it
   matters, the answer with its key number and uncertainty, and how sure the run
   is and why. Someone who reads only this knows the result. The key number is
   the run's best-powered estimate; a narrower or stricter check that widens it
   is reported beside it, not in its place.
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

**Figures, as a journal would print them.** The report has at most
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

**Show the data, not only the estimates.** A row of estimates with intervals tells
a reader what to believe; a panel of the underlying data shows them why. Lead with
that.

- Figure 1 contains at least one panel drawn from the row-level data (every
  species, every match, every request), with the groups or moments that carry the
  argument in colour and labelled on the panel, the rest in light grey: an annotated
  scatter, a real tree or map with values on it, small multiples of one simple view.
- When the question is about change (before and after, a shock, a trend), Figure
  1 leads with a time series of the outcome itself at a fine grain (by month, round
  or week), the periods that matter shaded and labelled, not counts or tiles of rows.
- Explain a mechanism or a method with a drawing built from the real data: real
  groups, real values, real examples named. No cartoon silhouettes or toy diagrams.
- Estimates with intervals support; they are never Figure 1's largest panel, and
  never a whole figure of near-identical rows. Show the few that settle the question.
- Panels differ in size and kind, composed on one canvas (matplotlib gridspec or
  subfigures, or your tools' equivalent) in the order of the argument, with bold
  panel letters at the top left. Label groups and lines directly instead of using
  legends. Text at least 7 pt at a print width of 180 mm. No chart junk.
- **Colours: only this palette**, plus black, white and greys for text, axes and
  rules, and the same group in the same colour everywhere. Warm: rose pink
  `#D46A8C`, brick red `#B5473A`, mustard `#E0B04A`, amber orange `#EE9F4F`. Cool:
  sky blue `#6FA8C9`, teal `#4E9A8F`, periwinkle `#8C9FD1`, sage green `#88B98A`,
  pale blue `#A3C4D9`, slate blue `#5F7FA8`, medium blue `#3D6FB0`. Other and
  background: light grey `#DCDCDC`. No built-in colormaps unless built from these.

**Plan, draw, check, look, revise.** Before plotting, write each figure's claim,
its reader's question, and each panel's role and form. Draw each main figure with
a script in `{assets_dir}/src/` that reads the run's data. Before every look, have
the script list any text whose rendered box overlaps other text, a data mark or an
axis line, or leaves the canvas, and fix what it finds. Then render the PNG and look
at the picture itself with your image viewer, not at pixel statistics: is the claim
visible in five seconds, where does the eye go first, is anything crowded, clipped
or ambiguous, does every panel earn its place? Revise at least once after the first
look, and stop when another round would not change what the reader takes away.

## palette

**Colours: only this palette**, plus black, white and greys for text, axes and
  rules, and the same group in the same colour everywhere. Warm: rose pink
  `#D46A8C`, brick red `#B5473A`, mustard `#E0B04A`, amber orange `#EE9F4F`. Cool:
  sky blue `#6FA8C9`, teal `#4E9A8F`, periwinkle `#8C9FD1`, sage green `#88B98A`,
  pale blue `#A3C4D9`, slate blue `#5F7FA8`, medium blue `#3D6FB0`. Other and
  background: light grey `#DCDCDC`. No built-in colormaps unless built from these.

## poster pass

### The report is due: make the poster

Make a poster of this run: one designed sheet, like the ones pinned up at a
conference, that a reader takes in standing in front of it for two minutes. You
write it as one standalone HTML page, `{paper_path}`, laid out on a fixed canvas
of **1800 by 2400 px** (36 by 48 in, portrait). The loop renders it to
`poster.png` and `poster.pdf`, and the run page shows that picture, so it is the
sheet that matters, not the HTML.

Build it from what the run found (its findings, `{view_dir}/findings.json`, or
its notebook, `{story_path}`, whichever it kept), the memory and the evidence in
the repo. What the project knew when the run started is in the charter and in
the earlier runs' digests under `.ouroboros/history/`; the run's own commits are
on its branch. Leave the findings and the notebook as they are.

The poster is about **this run's step** of a longer project: what it set out to
do, what it found, and what that changes. It is not a short paper and not a
dashboard. No abstract, no methods, no literature, no list of everything the run
did, no numbers in boxes.

**Choose the story first.** Before you draw or write anything, decide and write
down for yourself:

1. The finding, in a few words. It is the poster's title.
2. The hero: the one picture that shows it best. When the project has pictures of
   what it built or measured (renders, screenshots, photos, frames of a motion; look
   in the run's assets, `docs/media/`, `review/` and the evidence), the hero shows
   the thing itself, with the number that matters drawn on it.
3. Two to four supporting pictures, each answering one question a reader will ask
   after the hero ("compared with what?", "why?", "what fails?").
4. Three to five sentences that carry the reader between them, and what is next.

{template}

**The figures.** Draw each one for its place on the sheet, at the size it will be
shown, with a script `{assets_dir}/src/poster_<n>.py` that reads the run's data
files, saving `{assets_dir}/poster-<n>.png` (or `.svg`). Do not paste the run's
study plots as they are: they have too many panels and labels for a poster. A
poster figure has one message, the two to four things it compares, labels on the
marks instead of legends, the quoted number drawn on it, and text at least 22 px
where it is shown.

**Vary the figures.** Each figure takes the form its question needs, and no two
look alike: an annotated render or photo of the thing, a trace over time, a
before-and-after, a ranked bar, a map, a mechanism drawn from the real data, a
small table set in type. At most one strip or dot plot on a sheet; three charts of
dots on bare axes side by side is the failure to avoid. Design each chart, too: a
quiet background, few gridlines, direct labels, the comparison that matters
emphasised and the rest in grey.

{palette}

**Render, look, revise.** Run `ouroboros poster render {view_dir}`: it renders the
page to `{view_dir}/poster.png` and prints what it measures (anything off the
canvas or cut off, empty space at the bottom, text under 18 px or in colour, the
word count, stat cards). Then open `poster.png` with your image viewer and look at
the whole sheet, then at each quarter. Ask: where does the eye go first, and is
that the finding? Can every label be read? Does anything overlap, crowd, or float
in empty space? Does it look designed, like a poster someone would be proud to
pin up, not like a web page or a grid of equal boxes? Fix what you find and render
again. Do this at least twice, and stop when another round would not change what a
reader takes away.

**Never invent.** Every number, picture and claim comes from the repo or the
memory. Name the scripts, data files, record ids and commits behind each figure in
the footer.

When you finish, the loop renders the page itself and checks only what breaks a
poster: it fits the canvas and fills it (the content reaches 90% of its height),
no text under 18 px, text in black, greys or white, at most 250 words outside the
footer and source lines, a title of at most 16 words in the `<h1>`, at least
three `<figure>`s, no stat cards, tiles or KPI boxes, and every image loads. A
later pass gets what it finds as a problem to fix first.

## poster

**The sheet.** A complete HTML document with its own `<style>` and no scripts.
`body` is exactly 1800 by 2400 px with no margin and does not scroll; lay it out
with CSS grid or absolute positions, and let nothing run past the canvas. Images
are `assets/<name>`. Fonts: link `assets/fonts/fonts.css` (the render puts it
there) and use its families, `Inter`, `IBM Plex Sans`, `Space Grotesk`,
`Barlow Condensed` (400, 600) and `Source Serif 4`, or the system's `Charter` and
`Nimbus Sans`. Use one or two families: a display face for the title and headings
and a text face for the rest.

**The layout is yours.** Choose it for this run's story, the way a designer
would; the shapes below are what good posters share, not a template:

- A clear entry point: the finding as the `<h1>` (at most 16 words, set large),
  with the project, the run's name and dates in small type nearby.
- One dominant picture, usually the thing itself, given far more room than
  anything else; supporting pictures sized by how much they matter.
- Structure you can see: sections as panels, bands or columns, separated by tinted
  backgrounds, rules or gutters, read in an obvious order (numbered when it helps).
  Uneven columns, a full-width band, a picture that breaks the grid: all good.
- Dense, not cramped: margins about 50 to 60 px, gutters 30 to 40 px, panels
  that fill their space. No large empty areas, and no wall of text either.
- Text in black or dark grey on light grounds and white on dark bands; colour is
  for marks, fills, rules and accents, never for words. Body text 24 to 30 px,
  headings that are short claims.
- A short "what's next", and a footer with the sources (commits, record ids,
  scripts, data files) in small type (14 to 16 px is fine there).
- No stat cards: no boxes, tiles or badges holding a number and a label. A number
  belongs on the figure that shows it, or in the sentence that explains it.

Each picture is a `<figure>`; a caption, when it is needed, is one line.

{direction}

## poster style infographic

**The style: infographic** (the default). Title and headings in `Barlow Condensed`
600 (uppercase is fine for section labels), body in `IBM Plex Sans`. A light grey
ground (`#EEF0F2`) with white panels. Tell the story as a visible flow: numbered
steps joined by arrows or a path across the sheet, callout lines from labels to
parts of the hero picture, small diagrams or icons made from the real data where
they help.

## poster style swiss

**The style: Swiss.** `Inter` only: a very large, heavy (800) title set flush left
on white with no band, tight leading. A strict 12-column grid with uneven spans
(8 + 4, 5 + 7), sections divided by solid black 3 px rules, section numbers set
large in black. One accent colour from the palette, for marks only. Precise,
spare, confident.

## poster style dark

**The style: dark.** `Space Grotesk` throughout. A near-black ground (`#121417`)
with white and light-grey (`#B8BDC4`) type, and one bright accent from the palette
for marks and rules only: big numbers and labels drawn into figures stay white or
grey too. A single large hero picture, centred or full width, with every figure
drawn on the same dark ground, and supporting panels framed by thin light rules.

## no figures

**No numbered figures in this run.** Show the evidence as tables and, where a
picture is needed, one small inline chart in the section it supports. Do not use
`class="main"`.

## final

# The run has stopped: write the report

An Ouroboros run on this repository has stopped ({reason}). You are its scribe.
This is your last call: there is no iteration to record and no fold to do. Write
the report for the person who will read it now. Do not edit
source, tests, the charter or the memory, and do not run git: the loop commits
what you write.

Read the memory (`ouroboros mem orient`, then `show` what you need) in {where},
the notebook, the figure ledger and the evidence in the repo. The run's
view is `{view_dir}/`.

{pass}

{problems}

Then rewrite `{summary_path}`: plain text, two paragraphs, the first (at most three
sentences) saying what the run set out to do, the second (at most two sentences)
the answer or the product as it stands, with its number. Highlight the two or three phrases a skimmer must not
miss as `==like this==`.

Your final message is one line: what you changed.

### The goal (for context only)

{goal}
