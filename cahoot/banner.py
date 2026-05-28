"""Startup splash banner.

Renders the Cahoot wordmark in a yellow→deep-orange gradient using 24-bit
truecolor ANSI escapes. Falls back to plain text when:

* stdout isn't a TTY (piped, redirected to a file)
* the ``NO_COLOR`` environment variable is set
  (https://no-color.org/)
* the terminal doesn't advertise truecolor support via ``COLORTERM``

The art is preserved verbatim from the original design; only the
colourisation is added at render time.
"""

from __future__ import annotations

import os
import sys

from . import __version__

# Raw art — bubble letters spelling "CAHOOT" across two rows.
# Do not auto-format or reflow this string; the spacing is the design.
BANNER_ART = """\
      _  _  _                     _                             
   _ (_)(_)(_) _                 (_)                            
  (_)         (_)   _  _  _      (_) _  _  _       _  _  _     
  (_)              (_)(_)(_) _   (_)(_)(_)(_)_  _ (_)(_)(_) _  
  (_)               _  _  _ (_)  (_)        (_)(_)         (_) 
  (_)          _  _(_)(_)(_)(_)  (_)        (_)(_)         (_) 
  (_) _  _  _ (_)(_)_  _  _ (_)_ (_)        (_)        _  _(_) 
     (_)(_)(_)     (_)(_)(_)  (_)(_)        (_)        (_)(_)  
                                                               
                    _                                          
                   (_)                                         
      _  _  _    _ (_) _  _                                    
   _ (_)(_)(_) _(_)(_)(_)(_)                                   
  (_)         (_)  (_)                                         
  (_)         (_)  (_)     _                                   
  (_) _  _  _ (_)  (_)_  _(_)                                  
     (_)(_)(_)       (_)(_)                                    
"""

TAGLINE = "mission control for agent fleets"
AUTHOR = "Kenjin"
YEAR = "2026"


# Gradient endpoints — deep orange at the top, gold-yellow at the bottom.
# Material Design palette: deep-orange 900 → amber 400.
_TOP_RGB: tuple[int, int, int] = (230, 81, 0)  # #E65100
_BOTTOM_RGB: tuple[int, int, int] = (255, 213, 79)  # #FFD54F

_RESET = "\x1b[0m"
_DIM = "\x1b[2m"


def _ansi_fg(r: int, g: int, b: int) -> str:
    """24-bit truecolor foreground."""
    return f"\x1b[38;2;{r};{g};{b}m"


def _gradient(
    top: tuple[int, int, int],
    bottom: tuple[int, int, int],
    n: int,
) -> list[tuple[int, int, int]]:
    """Linearly interpolate ``n`` RGB stops between ``top`` and ``bottom``."""
    if n <= 1:
        return [top]
    return [
        (
            int(top[0] + (bottom[0] - top[0]) * i / (n - 1)),
            int(top[1] + (bottom[1] - top[1]) * i / (n - 1)),
            int(top[2] + (bottom[2] - top[2]) * i / (n - 1)),
        )
        for i in range(n)
    ]


def supports_truecolor(stream: object | None = None) -> bool:
    """Conservative check: only enable colour when we're confident.

    We require:
    * The stream is a TTY.
    * NO_COLOR is not set (https://no-color.org/).
    * Either COLORTERM advertises truecolor / 24bit, or TERM ends in
      ``-256color`` (a reasonable proxy on modern terminals).
    """
    stream = stream or sys.stdout
    isatty = getattr(stream, "isatty", None)
    if not callable(isatty) or not isatty():
        return False
    if os.environ.get("NO_COLOR") is not None:
        return False
    colorterm = os.environ.get("COLORTERM", "").lower()
    if "truecolor" in colorterm or "24bit" in colorterm:
        return True
    term = os.environ.get("TERM", "")
    return term.endswith("-256color")


def render(use_color: bool | None = None, width: int = 72) -> str:
    """Return the splash banner as a string.

    Args:
        use_color: ``True`` to force colour, ``False`` to force plain.
            ``None`` (the default) auto-detects via :func:`supports_truecolor`.
        width: width to centre the tagline and credit under, in columns.

    The output is ready to ``print`` and includes leading/trailing blank
    lines for visual breathing room.
    """
    if use_color is None:
        use_color = supports_truecolor()

    lines = BANNER_ART.rstrip("\n").split("\n")

    if use_color:
        colours = _gradient(_TOP_RGB, _BOTTOM_RGB, len(lines))
        body_lines = [
            f"{_ansi_fg(r, g, b)}{line}{_RESET}"
            for line, (r, g, b) in zip(lines, colours, strict=True)
        ]
    else:
        body_lines = lines

    tagline = TAGLINE.center(width)
    credit = f"v{__version__}  ·  by {AUTHOR}  ·  {YEAR}".center(width)

    if use_color:
        tagline = f"{_ansi_fg(200, 200, 200)}{tagline}{_RESET}"
        credit = f"{_DIM}{_ansi_fg(160, 160, 160)}{credit}{_RESET}"

    return "\n".join(
        [
            "",
            *body_lines,
            "",
            tagline,
            credit,
            "",
        ]
    )


def print_banner(use_color: bool | None = None) -> None:
    """Print the splash banner to stdout."""
    sys.stdout.write(render(use_color=use_color))
    sys.stdout.write("\n")
    sys.stdout.flush()


if __name__ == "__main__":
    # Allow `python -m cahoot.banner` for previewing the splash without
    # starting the full app.
    print_banner()
