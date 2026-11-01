"""The run's publication: the notebook, the report, the scribe's instructions, the check.

The scribe keeps two things (DESIGN.md section 23). Every iteration it curates the
notebook, `view/story.html`: the run book of findings and failed approaches. Under
`paper` and `product` it also keeps a figure ledger, and writes the report,
`view/paper.html`, once when the run stops (and as a mid-run draft every
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

# The top-level <section id> each template must have, in order; the skill names them.
REQUIRED = {
    "paper": ("answer", "findings", "results", "negative", "limitations", "methods"),
    "product": ("what", "demo", "status", "negative", "known", "run"),
}

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


def pass_rules(config: Config) -> str:
    """The report pass: write paper.html under its template and figure contract."""
    s = skill("ouroboros-publish")
    f = config.publication.figures
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
    return section(s, "pass").replace("{template}", section(s, config.template)).replace("{figures}", figures)


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
                 view_dir: str, story_path: str, assets_dir: str, summary_path: str) -> str:
    text = (section(skill("ouroboros-publish"), "final")
            .replace("{pass}", pass_rules(config))
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


def check(config: Config, view_dir: Path, story_html: str | None = None) -> list[str]:
    """What the report (paper.html) gets wrong against its template and figure contract. [] is clean.

    Only `paper` and `product` have a contract; a notebook is never checked.
    """
    template = config.template
    if template == "notebook":
        return []
    view_dir = Path(view_dir)
    if story_html is None:
        p = view_dir / PAPER
        story_html = p.read_text(encoding="utf-8", errors="replace") if p.exists() else ""
    if not story_html.strip():
        return [f"{PAPER} is empty: the report has not been written."]
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


def describe(problems: list[str]) -> str | None:
    """The check's findings as one fix-first note for the scribe; None when clean."""
    if not problems:
        return None
    return "The publication check found:\n" + "\n".join(f"- {p}" for p in problems)
