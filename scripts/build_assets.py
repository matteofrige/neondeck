"""Generate docs/assets/logo.svg and banner.svg from the pixel-art mascot and wordmark."""
from __future__ import annotations

from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "docs" / "assets"

MAGENTA, CYAN = "#ff3cc7", "#19f0ff"
BODY = ((7, 0, 2, 2), (2, 2, 12, 9), (0, 5, 2, 3), (14, 5, 2, 3), (5, 11, 6, 1), (2, 12, 12, 2))
CUT = ((3, 5, 10, 3), (5, 9, 1, 1), (7, 9, 1, 1), (9, 9, 1, 1), (6, 12, 4, 1))
EYES = ((4, 6, 3, 1), (9, 6, 3, 1))

FONT = {  # 5x7 glyphs
    "N": ["10001", "11001", "10101", "10101", "10011", "10001", "10001"],
    "E": ["11111", "10000", "10000", "11110", "10000", "10000", "11111"],
    "O": ["01110", "10001", "10001", "10001", "10001", "10001", "01110"],
    "D": ["11110", "10001", "10001", "10001", "10001", "10001", "11110"],
    "C": ["01111", "10000", "10000", "10000", "10000", "10000", "01111"],
    "K": ["10001", "10010", "10100", "11000", "10100", "10010", "10001"],
}

GLOW = ('<filter id="glow" x="-30%" y="-30%" width="160%" height="160%">'
        '<feGaussianBlur stdDeviation="{s}" result="b"/>'
        '<feMerge><feMergeNode in="b"/><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge></filter>')


def rects(items, scale, ox, oy, fill):
    d = "".join(f"M{ox + x * scale} {oy + y * scale}h{w * scale}v{h * scale}h-{w * scale}z" for x, y, w, h in items)
    return f'<path fill="{fill}" d="{d}"/>'


def mascot(scale, ox, oy):
    return (rects(BODY, scale, ox, oy, MAGENTA) + rects(CUT, scale, ox, oy, "#000")
            + rects(EYES, scale, ox, oy, CYAN))


def wordmark(text, cell, ox, oy):
    runs, col = [], 0
    for ch in text:
        for y, row in enumerate(FONT[ch]):
            x = 0
            while x < 5:
                if row[x] == "1":
                    w = 1
                    while x + w < 5 and row[x + w] == "1":
                        w += 1
                    runs.append((col + x, y, w, 1))
                    x += w
                else:
                    x += 1
        col += 6
    return rects(runs, cell, ox, oy, "url(#wm)"), (col - 1) * cell


def logo() -> str:
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 160 160" width="160" height="160" shape-rendering="crispEdges" role="img" aria-label="Neondeck logo">
<defs>{GLOW.format(s=3)}</defs>
<rect width="160" height="160" fill="#000"/>
<g filter="url(#glow)">{mascot(8, 16, 24)}</g>
</svg>
"""


def banner() -> str:
    cell = 16
    word, width = wordmark("NEONDECK", cell, 400, 120)
    horizon = 322
    hlines = "".join(f'<line x1="0" x2="1280" y1="{horizon + 78 * (i / 6) ** 2:.1f}" y2="{horizon + 78 * (i / 6) ** 2:.1f}"/>'
                     for i in range(1, 7))
    vlines = "".join(f'<line x1="640" y1="{horizon}" x2="{640 + k * 150}" y2="400"/>' for k in range(-10, 11))
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 400" width="1280" height="400" role="img" aria-label="Neondeck: a Stream Deck for your Claude Code sessions">
<defs>
{GLOW.format(s=7)}
<linearGradient id="wm" gradientUnits="userSpaceOnUse" x1="400" y1="0" x2="{400 + width}" y2="0"><stop offset="0" stop-color="{MAGENTA}"/><stop offset="1" stop-color="{CYAN}"/></linearGradient>
<linearGradient id="fade" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#000" stop-opacity="1"/><stop offset="1" stop-color="#000" stop-opacity="0"/></linearGradient>
<pattern id="scan" width="4" height="4" patternUnits="userSpaceOnUse"><rect width="4" height="1" fill="#fff" opacity="0.07"/></pattern>
<clipPath id="floor"><rect x="0" y="{horizon}" width="1280" height="{400 - horizon}"/></clipPath>
</defs>
<rect width="1280" height="400" fill="#000"/>
<g clip-path="url(#floor)" stroke="{MAGENTA}" stroke-width="1.5" opacity="0.55">{hlines}{vlines}</g>
<rect x="0" y="{horizon}" width="1280" height="26" fill="url(#fade)"/>
<line x1="0" x2="1280" y1="{horizon}" y2="{horizon}" stroke="{CYAN}" stroke-width="2" opacity="0.7"/>
<g shape-rendering="crispEdges">
<g filter="url(#glow)">{mascot(16, 96, 84)}</g>
<g filter="url(#glow)">{word}</g>
</g>
<text x="404" y="276" fill="{CYAN}" font-family="ui-monospace, SFMono-Regular, Menlo, Consolas, 'DejaVu Sans Mono', monospace" font-size="26" letter-spacing="1">A Stream Deck for your Claude Code sessions</text>
<rect width="1280" height="400" fill="url(#scan)"/>
</svg>
"""


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "logo.svg").write_text(logo(), encoding="utf-8")
    (OUT / "banner.svg").write_text(banner(), encoding="utf-8")
    print(OUT)
