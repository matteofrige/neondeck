"""Build docs/demo.html: the device UI with demo mode forced, for static hosting (GitHub Pages, file://)."""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "neondeck" / "static" / "preview.html"
DEST = ROOT / "docs" / "demo.html"


def build() -> str:
    html = SRC.read_text(encoding="utf-8")
    html = re.sub(r'<link rel="(manifest|apple-touch-icon)"[^>]*>\n', "", html)  # absolute agent URLs, absent when static
    return html.replace('<meta charset="utf-8">', '<meta charset="utf-8">\n<meta name="neondeck-demo" content="1">', 1)


if __name__ == "__main__":
    out = build()
    if "--check" in sys.argv:
        sys.exit(0 if DEST.exists() and DEST.read_text(encoding="utf-8") == out else "docs/demo.html is stale: run scripts/build_demo.py")
    DEST.write_text(out, encoding="utf-8")
    print(DEST)
