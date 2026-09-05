#!/usr/bin/env python
"""Regenerate Tally's icon set from one geometric source (DESIGN.md §7.4 palette).

The mark is a tally: four upright strokes crossed by a diagonal fifth. It replaced
the spruce "U" left over from the app's old name (Usual) when the rename landed.
Colours are globals.css tokens -- accent --spruce #ff8a4c on ground --bg #121318.

Everything except the OG card is pure geometry, so `python scripts/gen-icons.py`
reproduces it byte-for-byte anywhere. The OG card needs the display font: pass
--font path/to/BricolageGrotesque-ExtraBold.ttf (or a .woff2 -- it converts one in
memory). Without it the card is skipped rather than silently drawn in a wrong face.

Requires Pillow; fontTools+brotli only for --font. Both are dev-time tools for
regenerating committed assets, not project dependencies.
"""

from __future__ import annotations

import argparse
import io
import struct
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

BG = (18, 19, 24)  # --bg   #121318
FG = (255, 138, 76)  # --spruce #ff8a4c
TEXT = (244, 243, 239)  # --text  #f4f3ef
MUTED = (165, 167, 179)  # --muted #a5a7b3

# Geometry on a 100x100 canvas, centred on (50, 50). `compact` drops one upright
# and fattens the rest: at 16px the four-bar mark collapses into an orange smear.
# Two things were tuned off rendered proofs rather than guessed. Gaps stay wider
# than the strokes (gap 7 / stroke 6) or the uprights blur into one bar well
# before 16px; and the fifth stroke lies shallow (~24 deg) instead of the ~45 deg
# a tally is usually drawn with, because near-vertical it merges into the four it
# crosses -- at 16px that turns the whole mark into a smear.
FULL = dict(bars=[30.5, 43.5, 56.5, 69.5], y1=26.0, y2=74.0,
            diag=((23.0, 63.0), (77.0, 37.0)), stroke=6.0)
# 16px cannot hold four uprights plus a crossing stroke; the small sizes drop to
# three and fatten up. Same mark, one stroke fewer -- nobody counts a favicon.
COMPACT = dict(bars=[34.0, 50.0, 66.0], y1=26.0, y2=74.0,
               diag=((26.0, 63.0), (74.0, 37.0)), stroke=7.5)

# Glyph scale about the centre. MASKABLE keeps every pixel inside the 80%-diameter
# safe circle Android crops to (worst-case radius here is 33.9 units before
# scaling, so 1.12 lands at 38 of the 40 available); ANY fills the square the way
# a favicon should; APPLE sits between, since iOS crops only a squircle's corners.
SCALE_ANY = 1.42
SCALE_MASKABLE = 1.12
SCALE_APPLE = 1.25


def _geom(compact: bool, scale: float, size: int) -> tuple[list, float]:
    """Return (strokes, width) in pixels: each stroke is ((x1,y1),(x2,y2))."""
    g = COMPACT if compact else FULL
    k = size / 100.0

    def pt(x: float, y: float) -> tuple[float, float]:
        return (50 + (x - 50) * scale) * k, (50 + (y - 50) * scale) * k

    strokes = [(pt(x, g["y1"]), pt(x, g["y2"])) for x in g["bars"]]
    strokes.append((pt(*g["diag"][0]), pt(*g["diag"][1])))
    return strokes, g["stroke"] * scale * k


def render(size: int, scale: float, *, compact: bool = False,
           bg: tuple | None = BG) -> Image.Image:
    """Draw the mark at `size`, supersampled 8x for clean round caps."""
    ss = 8
    mode = "RGB" if bg else "RGBA"
    img = Image.new(mode, (size * ss, size * ss), bg if bg else (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    strokes, w = _geom(compact, scale, size * ss)
    r = w / 2.0
    for (x1, y1), (x2, y2) in strokes:
        d.line((x1, y1, x2, y2), fill=FG, width=int(round(w)))
        for cx, cy in ((x1, y1), (x2, y2)):  # round caps
            d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=FG)
    return img.resize((size, size), Image.LANCZOS)


def svg(scale: float = SCALE_ANY, compact: bool = False) -> str:
    strokes, w = _geom(compact, scale, 100)
    lines = "\n".join(
        f'    <line x1="{x1:.2f}" y1="{y1:.2f}" x2="{x2:.2f}" y2="{y2:.2f}"/>'
        for (x1, y1), (x2, y2) in strokes
    )
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100" '
        'role="img" aria-label="Tally">\n'
        f'  <rect width="100" height="100" fill="#{BG[0]:02x}{BG[1]:02x}{BG[2]:02x}"/>\n'
        f'  <g stroke="#{FG[0]:02x}{FG[1]:02x}{FG[2]:02x}" stroke-width="{w:.2f}" '
        'stroke-linecap="round">\n'
        f"{lines}\n"
        "  </g>\n"
        "</svg>\n"
    )


def write_ico(path: Path, images: list[Image.Image]) -> None:
    """ICO with PNG payloads (universally supported since Vista/Safari 4)."""
    blobs = []
    for im in images:
        b = io.BytesIO()
        im.save(b, format="PNG", optimize=True)
        blobs.append(b.getvalue())
    out = bytearray(struct.pack("<HHH", 0, 1, len(blobs)))
    offset = 6 + 16 * len(blobs)
    for im, blob in zip(images, blobs):
        w = 0 if im.width >= 256 else im.width
        h = 0 if im.height >= 256 else im.height
        out += struct.pack("<BBBBHHII", w, h, 0, 0, 1, 32, len(blob), offset)
        offset += len(blob)
    for blob in blobs:
        out += blob
    path.write_bytes(bytes(out))


def load_font(spec: str, size: int) -> ImageFont.FreeTypeFont:
    """Load a .ttf/.otf, or convert a .woff2 in memory (next/font ships woff2)."""
    if spec.lower().endswith(".woff2"):
        from fontTools.ttLib import TTFont

        f = TTFont(spec, fontNumber=0)
        f.flavor = None
        buf = io.BytesIO()
        f.save(buf)
        buf.seek(0)
        return ImageFont.truetype(buf, size)
    return ImageFont.truetype(spec, size)


def _wrap(text: str, font: ImageFont.FreeTypeFont, width: float) -> str:
    lines: list[str] = []
    line = ""
    for word in text.split():
        trial = f"{line} {word}".strip()
        if font.getlength(trial) <= width or not line:
            line = trial
        else:
            lines.append(line)
            line = word
    if line:
        lines.append(line)
    return "\n".join(lines)


def og_card(display: str, body: str | None) -> Image.Image:
    """1200x630 social card: the mark, the wordmark, the one-line pitch."""
    W, H = 1200, 630
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)

    d.rectangle((0, 0, W, 10), fill=FG)  # accent rule along the top

    mark = render(208, SCALE_ANY)
    img.paste(mark, (104, 211))

    f_name = load_font(display, 128)
    f_tag = load_font(body or display, 40)
    f_sub = load_font(body or display, 30)

    x = 384
    right = W - 80  # keep every line inside the safe margin
    d.text((x, 186), "Tally", font=f_name, fill=TEXT)
    d.text((x + 4, 336), "Say what you ate.", font=f_tag, fill=FG)
    sub = "Voice-first nutrition logging — real database numbers, not estimates."
    d.multiline_text((x + 4, 398), _wrap(sub, f_sub, right - x - 4),
                     font=f_sub, fill=MUTED, spacing=12)
    return img


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--font", help="Bricolage Grotesque ExtraBold (.ttf/.otf/.woff2)")
    ap.add_argument("--body-font", help="Archivo (.ttf/.otf/.woff2) for the OG subtitle")
    ap.add_argument("--out", default="frontend/public", type=Path)
    a = ap.parse_args()

    pub: Path = a.out
    icons = pub / "icons"
    icons.mkdir(parents=True, exist_ok=True)

    made = []
    for size in (192, 512):
        p = icons / f"icon-{size}.png"
        render(size, SCALE_ANY).save(p, optimize=True)
        made.append(p)
        p = icons / f"icon-maskable-{size}.png"
        render(size, SCALE_MASKABLE).save(p, optimize=True)
        made.append(p)

    p = pub / "apple-touch-icon.png"
    render(180, SCALE_APPLE).save(p, optimize=True)  # opaque: iOS renders alpha black
    made.append(p)

    p = icons / "icon.svg"
    p.write_text(svg(), encoding="utf-8")
    made.append(p)

    p = pub / "favicon.ico"
    write_ico(p, [render(16, SCALE_ANY, compact=True),
                  render(32, SCALE_ANY, compact=True),
                  render(48, SCALE_ANY)])
    made.append(p)

    if a.font:
        p = pub / "og.png"
        og_card(a.font, a.body_font).save(p, optimize=True)
        made.append(p)
    else:
        print("no --font: skipped og.png")

    for p in made:
        print(f"  {p}  {p.stat().st_size:,} bytes")


if __name__ == "__main__":
    main()
