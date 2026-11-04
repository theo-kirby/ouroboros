"""The poster as one picture: render `view/poster.html` to `poster.png` and `poster.pdf`, and measure it.

A poster (DESIGN.md section 26) is a standalone HTML page laid out on a fixed canvas,
CANVAS px wide and high (36 x 48 in, portrait, at 50 px per inch). The run page shows the
rendered PNG, not the HTML, so it reads as one composed sheet. `render` uses the same
headless Chromium as `ouroboros shot`: once with a measuring script injected (`--dump-dom`),
once for the PNG, once for the PDF. What it measures is what `publication.check` holds the
poster to: nothing outside the canvas or clipped, the canvas filled, large type, few words,
text in black, greys or white, and no stat cards.
"""

from __future__ import annotations

import html
import json
import re
import shutil
import subprocess
from importlib import resources
from pathlib import Path

from .figures import chromium, screenshot

CANVAS = (1800, 2400)
FONTS_CSS = "assets/fonts/fonts.css"
# Open-licensed (SIL OFL) fonts that ship with Ouroboros, so a poster looks the same on any
# machine: family -> (file, weight range or weight, style). `install_fonts` copies them
# beside the poster with an @font-face sheet at FONTS_CSS.
FONTS = {
    "Inter": ("Inter.ttf", "100 900"),
    "IBM Plex Sans": ("IBMPlexSans.ttf", "100 700"),
    "Source Serif 4": ("SourceSerif4.ttf", "200 900"),
    "Space Grotesk": ("SpaceGrotesk.ttf", "300 700"),
    "Barlow Condensed": ("BarlowCondensed-Regular.ttf", "400"),
    "Barlow Condensed ": ("BarlowCondensed-SemiBold.ttf", "600"),
}


def install_fonts(view_dir: Path) -> Path:
    """Copy the bundled fonts into <view>/assets/fonts with fonts.css; returns the css path."""
    out = Path(view_dir) / "assets" / "fonts"
    out.mkdir(parents=True, exist_ok=True)
    src = resources.files("ouroboros").joinpath("fonts")
    faces = []
    for family, (file, weight) in FONTS.items():
        dest = out / file
        if not dest.exists():
            with resources.as_file(src.joinpath(file)) as f:
                shutil.copyfile(f, dest)
        faces.append(f'@font-face{{font-family:"{family.strip()}";src:url("{file}") format("truetype");'
                     f'font-weight:{weight};font-style:normal;font-display:block}}')
    for lic in src.iterdir():
        if lic.name.startswith("OFL-"):
            with resources.as_file(lic) as f:
                shutil.copyfile(f, out / lic.name)
    css = out / "fonts.css"
    css.write_text("\n".join(faces) + "\n", encoding="utf-8")
    return css
HTML, PNG, PDF, MEASURE = "poster.html", "poster.png", "poster.pdf", "poster.render.json"

# Run inside the poster after it loads; writes what it finds into <pre id="__poster_measure">.
_MEASURE_JS = r"""
<script>
window.addEventListener("load", function () {
  var W = %(w)d, H = %(h)d, out = {}, d = document.documentElement, b = document.body;
  function name(el) {
    var t = el.tagName.toLowerCase();
    if (el.id) t += "#" + el.id;
    if (el.classList && el.classList.length) t += "." + Array.prototype.slice.call(el.classList, 0, 2).join(".");
    var s = (el.innerText || el.getAttribute("alt") || el.getAttribute("src") || "").trim().replace(/\s+/g, " ");
    return s ? t + " \"" + s.slice(0, 40) + "\"" : t;
  }
  out.scroll = [Math.max(d.scrollWidth, b.scrollWidth), Math.max(d.scrollHeight, b.scrollHeight)];
  var bottom = 0, outside = [], clipped = [], small = [], cards = [], colored = [];
  function chroma(c) {
    var m = (c || "").match(/[\d.]+/g);
    if (!m || m.length < 3 || (m.length > 3 && +m[3] === 0)) return 0;
    return Math.max(+m[0], +m[1], +m[2]) - Math.min(+m[0], +m[1], +m[2]);
  }
  var all = b.querySelectorAll("*");
  for (var i = 0; i < all.length; i++) {
    var el = all[i];
    if (el.id === "__poster_measure" || el.closest("script,style")) continue;
    var cs = getComputedStyle(el);
    if (cs.display === "none" || cs.visibility === "hidden") continue;
    var r = el.getBoundingClientRect();
    if (!r.width || !r.height) continue;
    var cls = (typeof el.className === "string" ? el.className : "") + " " + (el.id || "");
    if (/(^|[\s_-])(stat|stats|kpi|kpis|card|cards|tile|tiles|metric|metrics)([\s_-]|$)/i.test(cls)) cards.push(name(el));
    var text = Array.prototype.some.call(el.childNodes, function (n) { return n.nodeType === 3 && n.textContent.trim(); });
    var pic = /^(img|svg|canvas|video)$/i.test(el.tagName);
    if (!text && !pic) {
      if (cs.overflow !== "visible" && el !== d && el !== b &&
          (el.scrollHeight > el.clientHeight + 2 || el.scrollWidth > el.clientWidth + 2)) clipped.push(name(el));
      continue;
    }
    bottom = Math.max(bottom, r.bottom);
    if (r.right > W + 2 || r.bottom > H + 2 || r.left < -2 || r.top < -2) outside.push(name(el));
    if (text && !el.closest("footer,.src,.sources") && parseFloat(cs.fontSize) < 18) small.push(name(el) + " " + cs.fontSize);
    if (text && chroma(cs.color) > 60) colored.push(name(el) + " " + cs.color);
  }
  out.bottom = Math.round(bottom);
  out.outside = outside; out.clipped = clipped; out.small = small; out.cards = cards; out.colored = colored;
  out.figures = Array.prototype.map.call(b.querySelectorAll("figure"), function (f) {
    var r = f.getBoundingClientRect(); return {name: name(f), area: Math.round(r.width * r.height)};
  });
  var imgs = b.querySelectorAll("img");
  out.images = imgs.length + b.querySelectorAll("svg").length;
  out.broken = Array.prototype.filter.call(imgs, function (im) { return !im.naturalWidth; })
    .map(function (im) { return im.getAttribute("src") || ""; });
  var c = b.cloneNode(true);
  Array.prototype.forEach.call(c.querySelectorAll("footer,.src,.sources,script,style,#__poster_measure"),
                               function (n) { n.remove(); });
  out.words = (c.textContent || "").split(/\s+/).filter(function (w) { return /[A-Za-z0-9]/.test(w); }).length;
  var h1 = b.querySelector("h1");
  out.title = h1 ? h1.textContent.trim().replace(/\s+/g, " ") : "";
  var pre = document.createElement("pre");
  pre.id = "__poster_measure"; pre.textContent = JSON.stringify(out);
  b.appendChild(pre);
});
</script>
"""

_PRINT_CSS = "<style>@page{size:%(w)dpx %(h)dpx;margin:0}html,body{margin:0}</style>"


def _inject(text: str, extra: str) -> str:
    """`extra` just before </body> (or </html>, or at the end)."""
    for tag in ("</body>", "</html>"):
        i = text.lower().rfind(tag)
        if i >= 0:
            return text[:i] + extra + text[i:]
    return text + extra


def _chrome(argv: list[str], timeout: float = 120) -> subprocess.CompletedProcess | None:
    exe = chromium()
    if exe is None:
        return None
    try:
        return subprocess.run([exe, "--headless", "--disable-gpu", "--no-sandbox", "--hide-scrollbars", *argv],
                              capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None


def measure(view_dir: Path, wait_ms: int = 2500) -> dict | None:
    """What the measuring script finds in poster.html at the canvas size; None when it cannot run."""
    view_dir = Path(view_dir)
    src = view_dir / HTML
    if not src.exists():
        return None
    w, h = CANVAS
    tmp = view_dir / ".poster-measure.html"
    tmp.write_text(_inject(src.read_text(encoding="utf-8", errors="replace"), _MEASURE_JS % {"w": w, "h": h}),
                   encoding="utf-8")
    try:
        proc = _chrome([f"--window-size={w},{h}", f"--virtual-time-budget={wait_ms}", "--dump-dom",
                        tmp.resolve().as_uri()])
    finally:
        tmp.unlink(missing_ok=True)
    if proc is None:
        return None
    m = re.search(r'<pre id="__poster_measure">(.*?)</pre>', proc.stdout or "", re.S)
    if not m:
        return None
    try:
        return json.loads(html.unescape(m.group(1)))
    except ValueError:
        return None


def render(view_dir: Path) -> tuple[dict | None, list[str]]:
    """Render poster.html to poster.png and poster.pdf and measure it. (measurements, errors)."""
    view_dir = Path(view_dir)
    src = view_dir / HTML
    if not src.exists():
        return None, [f"no {HTML}: the poster has not been written"]
    errors: list[str] = []
    w, h = CANVAS
    install_fonts(view_dir)
    ok, msg = screenshot(str(src), view_dir / PNG, size=f"{w}x{h}", wait_ms=2500)
    if not ok:
        errors.append(f"could not render {PNG}: {msg}")
    tmp = view_dir / ".poster-print.html"
    tmp.write_text(_inject(src.read_text(encoding="utf-8", errors="replace"), _PRINT_CSS % {"w": w, "h": h}),
                   encoding="utf-8")
    try:
        pdf = (view_dir / PDF).resolve()
        proc = _chrome([f"--window-size={w},{h}", "--virtual-time-budget=2500", "--no-pdf-header-footer",
                        f"--print-to-pdf={pdf}", tmp.resolve().as_uri()])
        if proc is None or not pdf.exists():
            errors.append(f"could not render {PDF}")
    finally:
        tmp.unlink(missing_ok=True)
    m = measure(view_dir)
    if m is None:
        errors.append("could not measure the poster (no headless Chromium, or the page did not load)")
    else:
        (view_dir / MEASURE).write_text(json.dumps(m, indent=1), encoding="utf-8")
    return m, errors


def png_size(path: Path) -> tuple[int, int] | None:
    try:
        with open(path, "rb") as f:
            head = f.read(24)
    except OSError:
        return None
    if head[:8] != b"\x89PNG\r\n\x1a\n" or head[12:16] != b"IHDR":
        return None
    return int.from_bytes(head[16:20], "big"), int.from_bytes(head[20:24], "big")
