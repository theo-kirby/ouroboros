"""The run's publication: the notebook, the report, the scribe's instructions, the check.

The scribe keeps two things (DESIGN.md section 23). Every iteration it curates the
notebook, `view/story.html`: the run book of findings and failed approaches. Under
`paper` and `product` it also keeps a figure ledger, and writes the report,
`view/paper.html` (`view/poster.html` for a poster), once when the run stops (and as a mid-run draft every
`publication.consolidate_every` worked iterations when that is set). The run page
shows both, behind a Run / Paper toggle. `check` is deterministic: it reads
paper.html back against the contract, and what it finds reaches the next pass.
"""

from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from pathlib import Path

from .config import Config
from .memory.graph import section, skill

LEDGER = "figures.json"
PAPER = "paper.html"
POSTER = "poster.html"

# The top-level <section id> each template must have, in order; the skill names them.
REQUIRED = {
    "paper": ("answer", "findings", "results", "negative", "limitations", "methods"),
    "product": ("what", "demo", "status", "negative", "known", "run"),
}

# The poster's form (DESIGN.md section 26): one composed sheet, read whole in two minutes.
POSTER_WORDS = 250           # words to read on the sheet, its footer and source lines aside
POSTER_TITLE_WORDS = 16      # the <h1>: the run's finding
POSTER_FIGURES = 3           # <figure>s at least: a hero and the pictures that support it
POSTER_FILL = 0.9            # the lowest text or picture reaches this share of the canvas height


def report_file(template: str) -> str:
    """The report's file name in the view for a template: poster.html, else paper.html."""
    return POSTER if template == "poster" else PAPER

# "Fig. 1A", "Figure 2", "Fig. 1A–C", "Fig. 3B, D": a figure number and the panels it cites.
_REF = re.compile(r"\bFig(?:ure|\.)?\s*(\d+)\s*([A-Z](?:\s*(?:[–—-]|,|and)\s*[A-Z])*)?(?![A-Za-z])")


def ledger_path(view_dir: Path) -> Path:
    return Path(view_dir) / LEDGER


def profile_text(profile: str, *roles: str) -> str:
    """The profile's guidance for the roles (actor, critic) as one prompt section; '' for freeform.

    An experiment generalist that holds both responsibilities gets both."""
    s = skill("ouroboros-profile")
    text = "\n\n".join(t for t in (section(s, f"{profile} {r}") for r in roles) if t)
    return f"## This run's profile: {profile}\n\n{text}\n\n" if text else ""


def story_rules(config: Config) -> str:
    """What the scribe does with the notebook (story.html) every iteration: the same for
    every template, plus the figure ledger when the run will end in a report."""
    s = skill("ouroboros-publish")
    if config.template == "notebook":
        return section(s, "notebook")
    return section(s, "notebook") + "\n\n" + section(s, "notebook ledger").replace("{ledger_rules}", section(s, "ledger"))


def pass_rules(config: Config, direction: str | None = None) -> str:
    """The report pass: write the report (paper.html, or poster.html) under its template and figure contract."""
    s = skill("ouroboros-publish")
    f = config.publication.figures
    path = "{view_dir}/" + report_file(config.template)
    if config.template == "poster":
        brief = section(s, f"poster style {config.publication.style}")
        if direction:
            brief += f"\n\n**The design direction for this poster** (it overrides the style where they differ). {direction.strip()}"
        return (section(s, "poster pass").replace("{template}", section(s, "poster"))
                .replace("{palette}", section(s, "palette")).replace("{direction}", brief)
                .replace("{paper_path}", path))
    if f.enabled:
        figures = (section(s, "figures")
                   .replace("{main_max}", str(f.main_max))
                   .replace("{panels}", "Each main figure has at least two panels." if f.panels
                            else "A main figure may be a single panel.")
                   .replace("{supplementary}",
                            "Every ledger entry that is not a panel of a main figure is listed in a last "
                            "`<section id=\"supplementary\">` as Figure S1, S2, ..., each with its one-sentence "
                            "claim and a link to its file." if f.supplementary
                            else "Plots that are not in a main figure stay out of the publication; the ledger keeps them."))
    else:
        figures = section(s, "no figures")
    return (section(s, "pass").replace("{template}", section(s, config.template)).replace("{figures}", figures)
            .replace("{palette}", section(s, "palette")).replace("{paper_path}", path))


def publish_due(config: Config, worked_since: int) -> bool:
    every = config.publication.consolidate_every
    return config.template != "notebook" and bool(every) and worked_since >= every


def final_due(config: Config, reason: str) -> bool:
    """One last pass when the run stops, except when the operator interrupted it or a spend cap stopped it."""
    if config.template == "notebook" or not config.publication.final:
        return False
    return not reason.startswith(("interrupted", "max_cost_usd", "max_usage"))


def fill(text: str, *, view_dir: str, story_path: str, assets_dir: str, summary_path: str) -> str:
    return (text.replace("{story_path}", story_path)
            .replace("{paper_path}", f"{view_dir}/{PAPER}")
            .replace("{assets_dir}", assets_dir)
            .replace("{ledger_path}", f"{view_dir}/{LEDGER}")
            .replace("{summary_path}", summary_path)
            .replace("{view_dir}", view_dir))


def final_prompt(config: Config, *, reason: str, where: str, goal_text: str, problems: str | None,
                 view_dir: str, story_path: str, assets_dir: str, summary_path: str,
                 direction: str | None = None) -> str:
    text = (section(skill("ouroboros-publish"), "final")
            .replace("{pass}", pass_rules(config, direction))
            .replace("{problems}", f"The last check of the publication found these; fix them too:\n\n{problems}"
                     if problems else ""))
    text = fill(text, view_dir=view_dir, story_path=story_path, assets_dir=assets_dir, summary_path=summary_path)
    return (text.replace("{reason}", reason).replace("{where}", where)
            .replace("{goal}", (goal_text or "").strip()[:4000] or "(none)"))


# -- the check -----------------------------------------------------------------

class _Reader(HTMLParser):
    """The parts of story.html the contract speaks about: top-level section ids, main
    figures, the text outside them, and every local file the fragment points at."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.sections: list[str] = []
        self.figures: list[dict] = []
        self.text: list[str] = []
        self.links: list[str] = []
        self._depth = 0          # <section> nesting
        self._fig = 0            # <figure> nesting inside a main figure; 0 outside one
        self._cap = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        for key in ("src", "href"):
            if (a.get(key) or "").startswith("assets/"):
                self.links.append(a[key].split("#")[0].split("?")[0])
        if tag == "section":
            if self._depth == 0:
                self.sections.append(a.get("id") or "")
            self._depth += 1
        elif tag == "figure":
            if self._fig:
                self._fig += 1
            elif "main" in (a.get("class") or "").split():
                self._fig = 1
                self.figures.append({"id": a.get("id") or "", "panels": (a.get("data-panels") or "").split(),
                                     "caption": "", "src": False})
        elif self._fig and tag == "figcaption":
            self._cap = True
        elif self._fig and tag == "p" and "src" in (a.get("class") or "").split():
            self.figures[-1]["src"] = True

    def handle_endtag(self, tag):
        if tag == "section" and self._depth:
            self._depth -= 1
        elif tag == "figure" and self._fig:
            self._fig -= 1
        elif tag == "figcaption":
            self._cap = False

    def handle_data(self, data):
        if self._cap:
            self.figures[-1]["caption"] += data
        elif not self._fig:
            self.text.append(data)


def _cited(text: str) -> dict[int, set[str]]:
    """Figure number -> panel letters the text cites; an empty set means the figure as a whole."""
    out: dict[int, set[str]] = {}
    for m in _REF.finditer(text):
        letters: set[str] = set()
        tokens = re.findall(r"[A-Z]|[–—-]", m.group(2) or "")
        for i, tok in enumerate(tokens):
            if tok in "–—-" and 0 < i < len(tokens) - 1:
                letters |= {chr(c) for c in range(ord(tokens[i - 1]), ord(tokens[i + 1]) + 1)}
            elif tok not in "–—-":
                letters.add(tok)
        out.setdefault(int(m.group(1)), set()).update(letters)
    return out


def check(config: Config, view_dir: Path, story_html: str | None = None, measured: dict | None = None) -> list[str]:
    """What the report (paper.html, or poster.html) gets wrong against its template and figure
    contract. [] is clean.

    Only `poster`, `paper` and `product` have a contract; a notebook is never checked.
    """
    template = config.template
    if template == "notebook":
        return []
    view_dir = Path(view_dir)
    name = report_file(template)
    if template == "poster":
        return _poster(view_dir, measured)
    if story_html is None:
        p = view_dir / name
        story_html = p.read_text(encoding="utf-8", errors="replace") if p.exists() else ""
    if not story_html.strip():
        return [f"{name} is empty: the report has not been written."]
    r = _Reader()
    r.feed(story_html)
    r.close()
    problems: list[str] = []

    missing = [s for s in REQUIRED[template] if s not in r.sections]
    if missing:
        problems.append(f"missing top-level sections: {', '.join(f'<section id={s!r}>' for s in missing)}")
    present = [s for s in r.sections if s in REQUIRED[template]]
    if not missing and present != list(REQUIRED[template]):
        problems.append(f"sections out of order: {' → '.join(present)}; the template's order is "
                        f"{' → '.join(REQUIRED[template])}")

    figs = config.publication.figures
    if not figs.enabled and r.figures:
        problems.append(f"{len(r.figures)} main figure(s) although figures are off: drop class=\"main\"")
    elif figs.enabled:
        if len(r.figures) > figs.main_max:
            problems.append(f"{len(r.figures)} main figures; at most {figs.main_max}. Merge them into "
                            f"multi-panel figures or move the rest to the supplement")
        cited = _cited(" ".join(r.text))
        for n, f in enumerate(r.figures, 1):
            name = f"Figure {n}" + (f" (id {f['id']!r})" if f["id"] else "")
            if not f["panels"]:
                problems.append(f"{name} has no data-panels attribute listing its panel letters")
            elif figs.panels and len(f["panels"]) < 2:
                problems.append(f"{name} has one panel; a main figure gathers at least two")
            if not re.match(rf"\s*Figure\s+{n}\b", f["caption"]):
                problems.append(f"{name}: the caption must begin \"Figure {n}.\" with the figure's claim")
            if not f["src"]:
                problems.append(f"{name} has no <p class=\"src\"> naming the scripts, data and records behind it")
            if n not in cited:
                problems.append(f"{name} is never cited in the text (write \"Fig. {n}A\")")
            elif len(f["panels"]) > 1:
                uncited = [p for p in f["panels"] if p not in cited[n]]
                if uncited:
                    problems.append(f"{name}: panel(s) {', '.join(uncited)} never cited in the text "
                                    f"(write \"Fig. {n}{uncited[0]}\")")

    broken = sorted({link for link in r.links if not (view_dir / link).exists()})
    if broken:
        problems.append(f"links to missing files: {', '.join(broken[:8])}" + (" ..." if len(broken) > 8 else ""))

    ledger = ledger_path(view_dir)
    if figs.enabled:
        if not ledger.exists():
            problems.append(f"no figure ledger at {LEDGER}: write [] and append one entry per plot")
        else:
            try:
                entries = json.loads(ledger.read_text(encoding="utf-8"))
                if not isinstance(entries, list) or not all(isinstance(e, dict) and e.get("file") for e in entries):
                    raise ValueError("not a list of objects with a file")
                gone = sorted({e["file"] for e in entries if not (view_dir / e["file"]).exists()})
                if gone:
                    problems.append(f"ledger entries whose file is missing: {', '.join(gone[:8])}")
            except ValueError as exc:
                problems.append(f"{LEDGER} is not a valid ledger ({exc})")
    return problems


def _poster(view_dir: Path, measured: dict | None = None) -> list[str]:
    """The poster's contract, read off the rendered sheet (poster.render): it fits the canvas
    and fills it, its type is large, neutral in colour and its words few, and no stat cards.
    Only what breaks a poster is checked; the layout is the writer's (DESIGN.md section 26)."""
    from . import poster
    if not (view_dir / poster.HTML).exists():
        return [f"{poster.HTML} is empty: the poster has not been written."]
    errors: list[str] = []
    if measured is None:
        measured, errors = poster.render(view_dir)
    if measured is None:
        return errors or ["the poster could not be measured"]
    w, h = poster.CANVAS
    problems = list(errors)
    sw, sh = (measured.get("scroll") or [0, 0])[:2]
    if sw > w + 2 or sh > h + 2:
        problems.append(f"the page is {sw}x{sh} px; it must fit the {w}x{h} canvas exactly: cut words or shrink figures")
    for key, what in (("outside", "runs off the canvas"), ("clipped", "is cut off by its box")):
        found = measured.get(key) or []
        if found:
            problems.append(f"{len(found)} element(s) {what}: " + "; ".join(found[:5]))
    bottom = measured.get("bottom") or 0
    if bottom < POSTER_FILL * h:
        problems.append(f"the content ends at {bottom} px of {h}: the lower {h - bottom} px are empty; "
                        f"make the figures larger or the layout taller")
    small = measured.get("small") or []
    if small:
        problems.append(f"{len(small)} piece(s) of text under 18 px: " + "; ".join(small[:5]))
    words = measured.get("words") or 0
    if words > POSTER_WORDS:
        problems.append(f"{words} words on the sheet; at most {POSTER_WORDS} outside the footer and source lines. "
                        f"Let the figures speak and cut the rest")
    title = (measured.get("title") or "").split()
    if not title:
        problems.append("no <h1>: the title is the run's finding, in a few words")
    elif len(title) > POSTER_TITLE_WORDS:
        problems.append(f"the title is {len(title)} words; at most {POSTER_TITLE_WORDS}")
    figs = measured.get("figures") or []
    if len(figs) < POSTER_FIGURES:
        problems.append(f"{len(figs)} <figure>(s); a poster has at least {POSTER_FIGURES}")
    colored = measured.get("colored") or []
    if colored:
        problems.append("text in colour: keep text black, grey, or white on a dark band; colour belongs to the "
                        "marks in figures and to rules and fills: " + "; ".join(colored[:5]))
    cards = measured.get("cards") or []
    if cards:
        problems.append("stat cards, tiles or KPI boxes are not allowed on a poster (put the number on its figure): "
                        + "; ".join(cards[:5]))
    broken = measured.get("broken") or []
    if broken:
        problems.append(f"images that did not load: {', '.join(broken[:6])}")
    size = poster.png_size(view_dir / poster.PNG)
    if size != (w, h):
        problems.append(f"{poster.PNG} is {size or 'missing'}; it should be {w}x{h}")
    return problems


def describe(problems: list[str]) -> str | None:
    """The check's findings as one fix-first note for the scribe; None when clean."""
    if not problems:
        return None
    return "The publication check found:\n" + "\n".join(f"- {p}" for p in problems)
