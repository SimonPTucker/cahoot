#!/usr/bin/env python3
"""Generate ``docs/assets/banner.svg`` from the in-tree ASCII art.

We can't show ANSI escapes in a GitHub README, so we render the same art
into an SVG with a vertical ``linearGradient`` matching the terminal colours
(deep-orange #E65100 → amber #FFD54F). Run after editing the banner art or
the gradient endpoints:

    python scripts/generate_banner_svg.py

The output is checked in so casual cloners get the rendered README without
a build step.
"""

from __future__ import annotations

from pathlib import Path
from xml.sax.saxutils import escape

from cahoot import __version__
from cahoot.banner import AUTHOR, BANNER_ART, TAGLINE, YEAR

# Gradient endpoints — must match banner.py.
TOP = "#E65100"
BOTTOM = "#FFD54F"

# Layout tuning. Char width is approximate; SVG text scales fluidly so any
# reasonable values work — these are just the canvas dimensions.
CHAR_W = 9.6
LINE_H = 18
FONT_SIZE = 15
TOP_PAD = 14
BOTTOM_PAD = 14
SIDE_PAD = 16


def _build_svg() -> str:
    art_lines = BANNER_ART.rstrip("\n").splitlines()
    longest = max(len(line) for line in art_lines)
    art_width = int(longest * CHAR_W)
    art_height = len(art_lines) * LINE_H

    tagline = TAGLINE
    credit = f"v{__version__}  ·  by {AUTHOR}  ·  {YEAR}"

    width = art_width + SIDE_PAD * 2
    # Total = padding + art + small gap + tagline + credit + padding.
    extra_text_block = LINE_H * 3
    height = TOP_PAD + art_height + extra_text_block + BOTTOM_PAD

    # Build art tspans.
    tspans: list[str] = []
    for i, line in enumerate(art_lines):
        y = TOP_PAD + (i + 1) * LINE_H - 4  # baseline within the row
        tspans.append(f'<tspan x="{SIDE_PAD}" y="{y}" xml:space="preserve">{escape(line)}</tspan>')

    # Tagline + credit positioned below the art.
    base_after_art = TOP_PAD + art_height + LINE_H
    tagline_y = base_after_art
    credit_y = base_after_art + LINE_H

    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" \
role="img" aria-label="Cahoot — {escape(tagline)}">
  <title>Cahoot — {escape(tagline)}</title>
  <defs>
    <linearGradient id="cahoot-gradient" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="{TOP}"/>
      <stop offset="100%" stop-color="{BOTTOM}"/>
    </linearGradient>
  </defs>
  <rect width="100%" height="100%" fill="#0d1117"/>
  <g font-family="ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, \
'Liberation Mono', 'Courier New', monospace" font-size="{FONT_SIZE}">
    <text fill="url(#cahoot-gradient)" xml:space="preserve">
      {"".join(tspans)}
    </text>
    <text x="{width / 2}" y="{tagline_y}" fill="#c9d1d9" text-anchor="middle" \
font-size="{FONT_SIZE - 2}">{escape(tagline)}</text>
    <text x="{width / 2}" y="{credit_y}" fill="#8b949e" text-anchor="middle" \
font-size="{FONT_SIZE - 3}">{escape(credit)}</text>
  </g>
</svg>
"""


def main() -> None:
    out = Path(__file__).resolve().parents[1] / "docs" / "assets" / "banner.svg"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(_build_svg(), encoding="utf-8")
    print(f"wrote {out.relative_to(Path.cwd())}")


if __name__ == "__main__":
    main()
