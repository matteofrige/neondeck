"""Generate docs/assets/logo.svg and banner.svg: the mascot and the wordmark as round LEDs on a faint unlit dot field.

Lit pixels are one <path> of squares filled with an SVG <pattern> that holds a circle, so each pixel reads as a dot.
The glyph table is the one the UI uses (src/neondeck/ledfont.py).
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from neondeck.ledfont import MASCOT, bitmap_cells, text_cells  # noqa: E402

OUT = ROOT / "docs" / "assets"

GROUND, UNLIT = "#050505", "#1d1a14"
AMBER, MAGENTA, LED = ("#ffb000", 1.0), ("#ff2e88", 1.0), ("#e8eefc", 1.0)
LED_MID = ("#e8eefc", 0.62)


class Board:
    """Accumulates <pattern> definitions and <path> layers for one SVG."""

    def __init__(self, width: int, height: int, pitch: int, label: str) -> None:
        self.w, self.h, self.pitch, self.label = width, height, pitch, label
        self.defs: list[str] = []
        self.body: list[str] = []

    def _pattern(self, cell: float, ox: float, oy: float, ink: tuple[str, float]) -> str:
        pid = f"p{len(self.defs)}"
        color, opacity = ink
        op = "" if opacity >= 1 else f' fill-opacity="{opacity}"'
        self.defs.append(
            f'<pattern id="{pid}" x="{ox}" y="{oy}" width="{cell}" height="{cell}" patternUnits="userSpaceOnUse">'
            f'<circle cx="{cell / 2}" cy="{cell / 2}" r="{cell * 0.43:.2f}" fill="{color}"{op}/></pattern>')
        return pid

    def layer(self, cells, cell: float, ox: float, oy: float, ink: tuple[str, float]) -> None:
        rows: dict[int, list[int]] = {}
        for x, y in cells:
            rows.setdefault(y, []).append(x)
        d = []
        for y in sorted(rows):
            xs = sorted(rows[y])
            start = prev = xs[0]
            for x in xs[1:] + [None]:
                if x is not None and x == prev + 1:
                    prev = x
                    continue
                d.append(f"M{ox + start * cell:g} {oy + y * cell:g}h{(prev - start + 1) * cell:g}v{cell:g}h-{(prev - start + 1) * cell:g}z")
                start = prev = x if x is not None else 0
        pid = self._pattern(cell, ox, oy, ink)
        self.body.append(f'<path fill="url(#{pid})" d="{"".join(d)}"/>')

    def text(self, s: str, cell: float, ox: float, oy: float, ink) -> None:
        cells, _w, top = text_cells(s)
        self.layer(cells, cell, ox, oy, ink)

    def svg(self) -> str:
        field = (f'<pattern id="field" width="{self.pitch}" height="{self.pitch}" patternUnits="userSpaceOnUse">'
                 f'<circle cx="{self.pitch / 2}" cy="{self.pitch / 2}" r="{self.pitch * 0.38:.2f}" fill="{UNLIT}"/></pattern>')
        return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {self.w} {self.h}" width="{self.w}" height="{self.h}" '
                f'role="img" aria-label="{self.label}">\n<defs>{field}{"".join(self.defs)}</defs>\n'
                f'<rect width="{self.w}" height="{self.h}" fill="{GROUND}"/>\n'
                f'<rect width="{self.w}" height="{self.h}" fill="url(#field)"/>\n' + "\n".join(self.body) + "\n</svg>\n")


def logo() -> str:
    b = Board(160, 160, 4, "Neondeck logo")
    b.layer(bitmap_cells(MASCOT), 8, 16, 24, MAGENTA)
    return b.svg()


def banner() -> str:
    b = Board(1280, 400, 3, "Neondeck: a Stream Deck for your Claude Code sessions")
    b.text("NEONDECK", 9, 60, 36, MAGENTA)
    rows = (("WEBSHOP      TOCCA A TE", AMBER), ("INFRA        IN CORSO", LED_MID), ("BLOG-ENGINE  COMANDO", LED_MID))
    for i, (text, ink) in enumerate(rows):
        b.text(text, 6, 60, 132 + i * 54, ink)
    b.text("A STREAM DECK FOR YOUR CLAUDE CODE SESSIONS", 3, 60, 330, LED)
    b.layer(bitmap_cells(MASCOT), 12, 1020, 117, MAGENTA)
    return b.svg()


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "logo.svg").write_text(logo(), encoding="utf-8")
    (OUT / "banner.svg").write_text(banner(), encoding="utf-8")
    print(OUT)
