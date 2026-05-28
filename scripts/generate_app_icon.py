#!/usr/bin/env python3
"""Generate the Cahoot.app icon from the letter "C" in the banner.

Pipeline:

1. Slice the first 17 columns of every line of :data:`cahoot.banner.BANNER_ART`.
   That's exactly the bubble "C" — same character art the README banner uses.
2. Build an SVG with a rounded-rect dark background and the C rendered in
   the same yellow→deep-orange vertical gradient as the README banner.
3. Render PNGs at every size macOS asks for (16, 32, 64, 128, 256, 512, 1024).
4. Run ``iconutil -c icns`` to bundle them into ``icon.icns`` inside the
   ``.app`` bundle.

Re-run after editing ``BANNER_ART`` so the icon stays in sync:

    python scripts/generate_app_icon.py
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from xml.sax.saxutils import escape

import cairosvg

from cahoot.banner import BANNER_ART

# Gradient endpoints — must match cahoot/banner.py and docs/assets/banner.svg.
TOP = "#E65100"
BOTTOM = "#FFD54F"
BG = "#0d1117"

# C bounding box inside the banner art (8 rows x first 17 columns).
C_COLS = 17

# Canvas dimensions (SVG userspace units; the rasteriser scales to pixel sizes).
CANVAS = 1024
# Inset so the letter has breathing room against the rounded rect.
MARGIN = 96

# macOS iconset sizes (logical size, pixel size). Each pair becomes one PNG.
ICONSET_SIZES: list[tuple[str, int]] = [
    ("icon_16x16.png", 16),
    ("icon_16x16@2x.png", 32),
    ("icon_32x32.png", 32),
    ("icon_32x32@2x.png", 64),
    ("icon_128x128.png", 128),
    ("icon_128x128@2x.png", 256),
    ("icon_256x256.png", 256),
    ("icon_256x256@2x.png", 512),
    ("icon_512x512.png", 512),
    ("icon_512x512@2x.png", 1024),
]


def _extract_c() -> list[str]:
    lines = BANNER_ART.rstrip("\n").split("\n")
    return [line[:C_COLS].rstrip() for line in lines]


def _build_svg() -> str:
    rows = _extract_c()
    n_rows = len(rows)
    # Pick a font-size that fills the canvas vertically with the rows.
    inner = CANVAS - 2 * MARGIN
    line_h = inner / n_rows
    # Slightly smaller font than line_h so descenders/glyph metrics don't clip.
    font_size = line_h * 0.92
    # cairosvg's monospace fallback renders narrower than the 0.6em rule of
    # thumb — empirically ≈ 0.42em per glyph. Using that lets us actually
    # centre the letter on the canvas instead of pushing it left.
    char_w = font_size * 0.42
    max_chars = max(len(r) for r in rows) if rows else 1
    text_block_w = max_chars * char_w
    # Centre the block horizontally.
    x_origin = (CANVAS - text_block_w) / 2

    tspans = []
    for i, row in enumerate(rows):
        y = MARGIN + (i + 1) * line_h - line_h * 0.18
        tspans.append(
            f'<tspan x="{x_origin:.1f}" y="{y:.1f}" xml:space="preserve">{escape(row)}</tspan>'
        )

    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {CANVAS} {CANVAS}">
  <defs>
    <linearGradient id="c-gradient" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="{TOP}"/>
      <stop offset="100%" stop-color="{BOTTOM}"/>
    </linearGradient>
  </defs>
  <rect x="0" y="0" width="{CANVAS}" height="{CANVAS}" rx="180" ry="180" fill="{BG}"/>
  <g font-family="ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, 'Liberation Mono', 'Courier New', monospace" \
font-weight="600" font-size="{font_size:.1f}" fill="url(#c-gradient)">
    <text xml:space="preserve">{"".join(tspans)}</text>
  </g>
</svg>
"""


def _render_pngs(svg: str, dst: Path) -> None:
    dst.mkdir(parents=True, exist_ok=True)
    svg_bytes = svg.encode("utf-8")
    for name, size in ICONSET_SIZES:
        out = dst / name
        cairosvg.svg2png(
            bytestring=svg_bytes,
            write_to=str(out),
            output_width=size,
            output_height=size,
        )
        print(f"  rendered {name} ({size}x{size})")


def _make_icns(iconset_dir: Path, icns_path: Path) -> None:
    # macOS iconutil expects the dir name to end in `.iconset`.
    if not iconset_dir.name.endswith(".iconset"):
        renamed = iconset_dir.with_suffix(".iconset")
        if renamed.exists():
            shutil.rmtree(renamed)
        iconset_dir.rename(renamed)
        iconset_dir = renamed
    cmd = ["iconutil", "-c", "icns", "-o", str(icns_path), str(iconset_dir)]
    subprocess.run(cmd, check=True)
    print(f"  wrote {icns_path}")


def main() -> None:
    repo = Path(__file__).resolve().parents[1]
    app_resources = repo / "scripts" / "Cahoot.app" / "Contents" / "Resources"
    app_resources.mkdir(parents=True, exist_ok=True)

    svg = _build_svg()
    # Persist the SVG too so the README / docs can use it.
    svg_out = repo / "docs" / "assets" / "app_icon.svg"
    svg_out.parent.mkdir(parents=True, exist_ok=True)
    svg_out.write_text(svg, encoding="utf-8")
    print(f"  wrote {svg_out.relative_to(repo)}")

    # Render PNG iconset in a temp folder we then convert with iconutil.
    iconset = repo / "scripts" / "Cahoot.app" / "Contents" / "Resources" / "_iconset"
    if iconset.exists():
        shutil.rmtree(iconset)
    _render_pngs(svg, iconset)

    icns = app_resources / "icon.icns"
    try:
        _make_icns(iconset, icns)
    finally:
        # Clean up the staging folder regardless of icns success.
        staged = iconset.with_suffix(".iconset")
        for d in (iconset, staged):
            if d.exists():
                shutil.rmtree(d)


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as exc:
        print(f"iconutil failed: {exc}", file=sys.stderr)
        sys.exit(1)
