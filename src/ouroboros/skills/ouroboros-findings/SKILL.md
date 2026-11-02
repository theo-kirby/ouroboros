---
name: ouroboros-findings
description: How the Ouroboros writer keeps the run's findings - view/findings.json, one entry per thing the run now knows, each with a title, a summary of at most three sentences, the claims and records of the memory graph it rests on, visuals made with `ouroboros plot` or `ouroboros shot`, and folded notes - and the rules for those visuals. Inlined into the writer's review prompt by roles/writer.py.
---

`roles/writer.py` puts `writer` into every review prompt of the worker-writer loop,
with `{findings_path}`, `{assets_dir}`, `{findings}` (the file as it is now) and
`{problems}` (what the loop's check found last turn) and `{summary_path}` filled in.

## writer

### The findings: what the run now knows, told to a person

The run page leads with the findings. A person who comes back after twelve hours
reads them first, so they are the run's story: a result with its number, a method
that works, a hypothesis the evidence rejected, a feature that now works. They are
not a log: the timeline already shows every turn, and most turns change no finding,
or sharpen one. Keep them in `{findings_path}`; you are the only one who edits it.

```json
{"findings": [
  {"id": "chunked-reading",
   "title": "Reading in chunks makes a 50 MB file take 1.4 s",
   "summary": "At most three plain sentences: what was found, the number, how sure it is.",
   "status": "holds",
   "claims": ["<the claim ids it rests on>"],
   "records": ["<record ids that carry it, beyond those its claims cite>"],
   "visuals": [{"file": "assets/speed-by-size.png", "caption": "What to look at, in one sentence."}],
   "notes": "Markdown: the detail, the method, the caveats, the commands. Shown folded."}
]}
```

- **When.** After you fold: add a finding when a claim became `working` with
  evidence, or a hypothesis was rejected with a number; update one when the turn
  changed what it says. Never delete one: a finding the evidence overturned becomes
  `"status": "retracted"` (or `"revised"`), with the reason in `notes`.
- **Order.** Newest findings first: prepend new entries, and keep existing entries
  in their relative order when updating them. Only the first five start expanded
  on the page; older findings can be opened individually or with Expand all.
- **Title**: under a sentence, the finding itself, not a topic ("Chunked reading is
  6.7 times faster", not "Performance").
- **Summary**: at most three sentences in plain words, with the key number and how
  sure the run is. Explain any term the first time it appears.
- **Claims and records** tie the finding to the memory graph. The page draws them:
  the claims it names, everything under them, and the records they rest on, by
  iteration. Name the claims a reader should see.
- **Notes**: the detail a curious reader wants: method, numbers, caveats, the
  commands that reproduce it.

**The summary.** Keep `{summary_path}` too: plain text at the top of the page, in
two paragraphs. The first, at most three sentences, says what the run set out to
do and why, in words anyone can follow. The second, at most two sentences, says
where it stands now: the best result with its number, or the blocker, and what
comes next. Mark the one or two phrases that matter most `==like this==`. The page
cuts anything longer, and the loop's check says so. Rewrite it whenever it is no
longer true.

Every check of this file the loop made last turn: {problems}

The file now:

{findings}

### Visuals: every finding gets exactly one figure

A finding is told with a picture: a plot of the measurement, a screenshot of the
feature, a render of the output. A finding without a visual is not finished; make
one now, or put the data or the picture it needs into the next `Next:` line.

- **Plots**: `ouroboros plot fig.py` runs your script with matplotlib, seaborn,
  pandas and numpy in the house style, whatever this repo is written in. In the
  script, read the numbers from the files the worker saved (never type them in),
  then `from ouroboros.figstyle import save, PALETTE, WARM, COOL, GREY` and
  `save(fig, "<name>")`: it writes `{assets_dir}/<name>.png` (and `.svg`) and prints
  any text that overlaps other text and any colour outside the palette. Keep the
  script beside it in `{assets_dir}/src/`.
- **Screenshots**: `ouroboros shot <url-or-file> <name>` writes `{assets_dir}/<name>.png`
  with a headless browser, when this host has one.
- In `visuals`, include exactly one entry: `assets/<name>.png` and one caption
  of one or two sentences explaining the whole figure. Never list separate panels
  or put separate captions beneath individual charts. Compose plots, screenshots,
  images and references into one continuous image with a deliberate grid and
  consistent sizing; use subplots/gridspec or stitch images onto one canvas.
  Consolidate existing multi-image findings when you next update them.

The rules for a figure:

- **One message per figure**, its title the message as a sentence. Show the data,
  not only the summary: the points, the runs, the before and after, with the case
  that carries the finding in colour and labelled on the plot, the rest in grey.
- **Colours: only the palette**, which `ouroboros plot` sets: warm rose `#D46A8C`,
  brick `#B5473A`, mustard `#E0B04A`, amber `#EE9F4F`; cool sky `#6FA8C9`, teal
  `#4E9A8F`, periwinkle `#8C9FD1`, sage `#88B98A`, pale blue `#A3C4D9`, slate
  `#5F7FA8`, blue `#3D6FB0`; light grey `#DCDCDC`, plus black, white and greys. The
  same thing in the same colour on every figure. Colormaps: `ouroboros`,
  `ouroboros_warm`, `ouroboros_div`.
- Label axes with units; label lines and groups on the plot instead of a legend;
  mark the comparison the finding rests on (a target line, a baseline).
- A figure with several panels: one canvas (gridspec or subplots), bold panel
  letters at the top left, panels in the order of the argument.
- **Look at it** before you list it: open the PNG with your image viewer. Is the
  message visible in five seconds? Is anything crowded, clipped or ambiguous? Fix
  what `save` printed and what you saw, and save again.
