import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_docs_demo_is_in_sync():
    spec = importlib.util.spec_from_file_location("build_demo", ROOT / "scripts" / "build_demo.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    html = mod.build()
    assert 'name="neondeck-demo"' in html and 'rel="manifest"' not in html
    assert (ROOT / "docs" / "demo.html").read_text(encoding="utf-8") == html, "run scripts/build_demo.py"
