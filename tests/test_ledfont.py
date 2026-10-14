import importlib.util
import json
import re
from pathlib import Path

import pytest

from neondeck import ledfont
from neondeck.buttons import DEFAULTS, ButtonStore, ValidationError, validate_button

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "src" / "neondeck" / "static"
PAGES = ("preview.html", "admin.html")
REQUIRED = (list("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 .,:;-_/\\?!'\"()+=%#@&*<>[]") + ["·", "À", "È", "É", "Ì", "Ò", "Ù"])


def page(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def test_font_is_complete_and_well_formed():
    missing = [c for c in REQUIRED if c not in ledfont.FONT]
    assert not missing, missing
    for ch, bitmap in ledfont.FONT.items():
        rows = ledfont.rows(bitmap)
        assert all(len(r) == ledfont.GLYPH_COLS and set(r) <= {"#", "."} for r in rows), ch
        assert len(rows) == (9 if ch in "ÀÈÉÌÒÙ" else ledfont.GLYPH_ROWS), ch
    for accented, base in (("À", "A"), ("È", "E"), ("É", "E"), ("Ì", "I"), ("Ò", "O"), ("Ù", "U")):
        assert ledfont.rows(ledfont.FONT[accented])[2:] == ledfont.rows(ledfont.FONT[base])
        assert "#" in "".join(ledfont.rows(ledfont.FONT[accented])[:2])


def test_icons_mascot_terminal_shapes():
    assert set(ledfont.ICON_NAMES) >= {"esc", "enter", "stop", "mode", "tab", "up", "down", "rocket", "book", "bolt",
                                       "terminal", "refresh", "play", "check"}
    for name, bitmap in ledfont.ICONS.items():
        rows = ledfont.rows(bitmap)
        assert len(rows) == 11 and all(len(r) == 11 for r in rows), name
    for bitmap in (ledfont.MASCOT, ledfont.TERMINAL):
        rows = ledfont.rows(bitmap)
        assert len(rows) == 14 and all(len(r) == 16 for r in rows)


def test_text_cells_baseline_and_accent():
    cells, width, top = ledfont.text_cells("AB")
    assert width == 11 and top == 0 and max(y for _, y in cells) == 6
    _, _, top = ledfont.text_cells("à")  # lowercase maps to the accented capital
    assert top == -2


@pytest.mark.parametrize("name", PAGES)
def test_html_embeds_the_python_tables(name):
    m = re.search(r"const LED = (\{.*\});\n", page(name))
    assert m, "LED data not found"
    assert json.loads(m.group(1)) == ledfont.DATA


def test_shared_module_is_identical_in_both_pages():
    for tag in ("led", "led-css"):
        blocks = []
        for name in PAGES:
            m = re.search(rf"/\*{tag}:begin\*/.*?/\*{tag}:end\*/", page(name), re.S)
            assert m, (name, tag)
            blocks.append(m.group(0))
        assert blocks[0] == blocks[1], tag


def test_assets_are_in_sync():
    spec = importlib.util.spec_from_file_location("build_assets", ROOT / "scripts" / "build_assets.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert (ROOT / "docs" / "assets" / "logo.svg").read_text(encoding="utf-8") == mod.logo(), "run scripts/build_assets.py"
    assert (ROOT / "docs" / "assets" / "banner.svg").read_text(encoding="utf-8") == mod.banner(), "run scripts/build_assets.py"


def button(icon):
    return {"title": "T", "icon": icon, "action": {"type": "send_keys", "text": "x", "target": "current"}}


def test_glyph_icon_validation():
    assert validate_button(button({"type": "glyph", "value": "rocket"}))["icon"] == {"type": "glyph", "value": "rocket"}
    for bad in ("", "nope", "../x"):
        with pytest.raises(ValidationError):
            validate_button(button({"type": "glyph", "value": bad}))


def test_default_buttons_use_glyphs(tmp_path):
    store = ButtonStore(tmp_path)
    assert [(b["title"], b["icon"]) for b in store.buttons] == [
        ("Esc", {"type": "glyph", "value": "esc"}), ("Invio", {"type": "glyph", "value": "enter"}),
        ("Interrompi", {"type": "glyph", "value": "stop"}), ("Cambia modalità", {"type": "glyph", "value": "mode"})]
    assert len(DEFAULTS) == 4


def test_legacy_default_emoji_are_migrated(tmp_path):
    def b(title, emoji, bid):
        return {"id": bid, "title": title, "icon": {"type": "emoji", "value": emoji},
                "action": {"type": "send_keys", "text": "x", "target": "current"}}
    legacy = [b("Esc", "⎋", "1"), b("Invio", "↵", "2"), b("Interrompi", "🛑", "3"), b("Cambia modalità", "🔀", "4"),
              b("Esc", "🚪", "5"),   # user content: same title, other emoji
              b("Deploy", "🚀", "6")]
    (tmp_path / "buttons.json").write_text(json.dumps(legacy), encoding="utf-8")
    store = ButtonStore(tmp_path)
    assert [x["icon"]["value"] for x in store.buttons] == ["esc", "enter", "stop", "mode", "🚪", "🚀"]
    assert [x["icon"]["type"] for x in store.buttons] == ["glyph"] * 4 + ["emoji"] * 2
    assert json.loads((tmp_path / "buttons.json").read_text(encoding="utf-8"))[0]["icon"] == {"type": "glyph", "value": "esc"}
