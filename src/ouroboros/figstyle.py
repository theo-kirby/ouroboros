"""The look of every figure an Ouroboros run makes, and the helpers that keep it.

`ouroboros plot SCRIPT` runs a script with this package's own Python, which has
matplotlib, seaborn, pandas and numpy, after `apply()`: so a run on any project (C++,
Rust, a website) can draw proper figures without installing anything in it. In the
script:

    from ouroboros.figstyle import PALETTE, WARM, COOL, GREY, save
    import seaborn as sns, pandas as pd
    ...
    save(fig, "speed-by-size")      # assets/speed-by-size.png (+ .svg), checked for clashing text

Colours are Theo's palette and nothing else (plus black, white and greys).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

WARM = {"rose": "#D46A8C", "brick": "#B5473A", "mustard": "#E0B04A", "amber": "#EE9F4F"}
COOL = {"sky": "#6FA8C9", "teal": "#4E9A8F", "periwinkle": "#8C9FD1", "sage": "#88B98A",
        "pale": "#A3C4D9", "slate": "#5F7FA8", "blue": "#3D6FB0"}
GREY = {"light": "#DCDCDC", "mid": "#9A9A9A", "dark": "#555555", "ink": "#222222"}
# The categorical order: strong and distinct first, so two or three groups never look alike.
PALETTE = ["#3D6FB0", "#D46A8C", "#E0B04A", "#4E9A8F", "#B5473A", "#8C9FD1", "#EE9F4F", "#88B98A",
           "#5F7FA8", "#6FA8C9", "#A3C4D9"]
ALLOWED = {c.upper() for c in [*WARM.values(), *COOL.values(), *GREY.values(), "#000000", "#FFFFFF"]}
ASSETS_ENV = "OUROBOROS_ASSETS"   # where `save` writes by default; `ouroboros plot` sets it for the run


def apply() -> None:
    """Set matplotlib and seaborn to the house look: white ground, light grid, the palette."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from cycler import cycler
    try:
        import seaborn as sns
        sns.set_theme(style="ticks", context="notebook", palette=PALETTE)
        _full_saturation(sns)
    except ImportError:   # matplotlib alone still gets the colours
        pass
    plt.rcParams.update({
        "axes.prop_cycle": cycler(color=PALETTE),
        "figure.dpi": 110, "savefig.dpi": 200, "savefig.bbox": "tight", "savefig.facecolor": "white",
        "figure.facecolor": "white", "axes.facecolor": "white",
        "axes.spines.top": False, "axes.spines.right": False,
        "axes.edgecolor": GREY["dark"], "axes.labelcolor": GREY["ink"], "text.color": GREY["ink"],
        "xtick.color": GREY["dark"], "ytick.color": GREY["dark"],
        "axes.grid": True, "grid.color": "#EDEDED", "grid.linewidth": 0.8,
        "axes.titleweight": "bold", "axes.titlesize": 12, "axes.labelsize": 10.5,
        "xtick.labelsize": 9.5, "ytick.labelsize": 9.5, "legend.frameon": False, "legend.fontsize": 9.5,
        "font.size": 10.5, "svg.fonttype": "none", "image.cmap": "ouroboros",
    })
    _register_cmaps()


def _full_saturation(sns) -> None:
    """Seaborn's bar, box, violin and count plots fade their colours to 75% by default, which
    takes them off the palette; here they keep the palette's colours unless asked otherwise."""
    import functools
    for name in ("barplot", "boxplot", "violinplot", "boxenplot", "countplot"):
        fn = getattr(sns, name, None)
        if fn is None or getattr(fn, "_ouroboros", False):
            continue

        @functools.wraps(fn)
        def wrapped(*args, _fn=fn, **kwargs):
            kwargs.setdefault("saturation", 1)
            return _fn(*args, **kwargs)
        wrapped._ouroboros = True
        setattr(sns, name, wrapped)


def _register_cmaps() -> None:
    """Sequential and diverging colormaps built from the palette (`ouroboros`, `ouroboros_div`)."""
    import matplotlib
    from matplotlib.colors import LinearSegmentedColormap
    maps = {
        "ouroboros": ["#FFFFFF", "#A3C4D9", "#6FA8C9", "#3D6FB0", "#22406B"],
        "ouroboros_warm": ["#FFFFFF", "#E0B04A", "#EE9F4F", "#B5473A"],
        "ouroboros_div": ["#3D6FB0", "#A3C4D9", "#F4F4F4", "#EE9F4F", "#B5473A"],
    }
    for name, colours in maps.items():
        if name not in matplotlib.colormaps:
            matplotlib.colormaps.register(LinearSegmentedColormap.from_list(name, colours))


def assets_dir() -> Path:
    return Path(os.environ.get(ASSETS_ENV) or "assets")


def _drawn_texts(fig):
    """Every piece of text the figure will show: titles, labels, annotations, legends, and the
    tick labels of ticks inside the axis range (matplotlib keeps labels for ticks it never draws)."""
    out = list(fig.texts)
    for ax in fig.axes:
        out += [ax.title, ax._left_title, ax._right_title, ax.xaxis.label, ax.yaxis.label, *ax.texts]
        leg = ax.get_legend()
        if leg is not None:
            out += list(leg.get_texts())
        for axis, (lo, hi) in ((ax.xaxis, ax.get_xlim()), (ax.yaxis, ax.get_ylim())):
            lo, hi = min(lo, hi), max(lo, hi)
            for tick in axis.get_major_ticks():
                try:
                    inside = lo - 1e-9 * abs(hi - lo) <= tick.get_loc() <= hi + 1e-9 * abs(hi - lo)
                except Exception:
                    inside = True
                if inside:
                    out += [tick.label1, tick.label2]
    return out


def clashes(fig) -> list[str]:
    """Text that overlaps other text, as a list of plain sentences. (Off-canvas text is not
    a problem: `save` crops to what is drawn.)"""
    fig.canvas.draw()
    renderer = fig.canvas.get_renderer()
    texts = []
    for t in _drawn_texts(fig):
        try:
            if not t.get_visible() or not str(t.get_text()).strip():
                continue
            box = t.get_window_extent(renderer)
        except Exception:
            continue
        if box.width > 0 and box.height > 0:
            texts.append((str(t.get_text()).strip()[:40], box))
    out = []
    for i, (a, ba) in enumerate(texts):
        for b, bb in texts[i + 1:]:
            if ba.overlaps(bb) and a != b:
                out.append(f"`{a}` overlaps `{b}`")
    return out[:20]


def off_palette(fig) -> list[str]:
    """Fill and line colours of data marks that are not in the palette (greys pass)."""
    from matplotlib.colors import to_hex
    seen = set()
    for a in fig.findobj():
        for getter in ("get_facecolor", "get_color", "get_edgecolor"):
            fn = getattr(a, getter, None)
            if fn is None:
                continue
            try:
                vals = fn()
            except Exception:
                continue
            arr = vals if hasattr(vals, "__len__") and not isinstance(vals, str) and len(vals) and hasattr(vals[0], "__len__") else [vals]
            for v in arr:
                try:
                    if len(v) == 4 and v[3] == 0:
                        continue
                    h = to_hex(v).upper()
                except Exception:
                    continue
                r, g, b = int(h[1:3], 16), int(h[3:5], 16), int(h[5:7], 16)
                if max(r, g, b) - min(r, g, b) <= 12:   # a grey, black or white
                    continue
                if h not in ALLOWED:
                    seen.add(h)
    return sorted(seen)[:10]


def save(fig, name: str, *, out: str | Path | None = None, svg: bool = True) -> Path:
    """Save `fig` as `<assets>/<name>.png` (and `.svg`), then print what the check found:
    text that clashes or leaves the canvas, and colours outside the palette. Fix those and
    save again; then look at the PNG itself."""
    base = Path(out) if out else assets_dir() / name
    if base.suffix.lower() in (".png", ".svg"):
        base = base.with_suffix("")
    base.parent.mkdir(parents=True, exist_ok=True)
    problems = clashes(fig)
    colours = off_palette(fig)
    png = base.with_suffix(".png")
    fig.savefig(png)
    if svg:
        fig.savefig(base.with_suffix(".svg"))
    print(f"saved {png}" + (f" and {base.with_suffix('.svg')}" if svg else ""))
    for p in problems:
        print(f"  check: {p}", file=sys.stderr)
    if colours:
        print(f"  check: colours outside the palette: {', '.join(colours)}", file=sys.stderr)
    if not problems and not colours:
        print("  check: no clashing text, every colour in the palette; now look at the picture")
    return png
