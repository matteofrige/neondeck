import os
import stat

from neondeck import config, server


def test_env_home_prefers_new_variable(monkeypatch):
    monkeypatch.setenv("CLAUDE_DECK_HOME", "/old")
    assert config.env_home() == "/old"
    monkeypatch.setenv("NEONDECK_HOME", "/new")
    assert config.env_home() == "/new"


def test_migrate_legacy_copies_and_keeps_old(tmp_path):
    old, new = tmp_path / "claude-deck", tmp_path / "neondeck"
    (old / "icons").mkdir(parents=True)
    (old / "config.json").write_text('{"token": "t"}')
    (old / "buttons.json").write_text("[]")
    (old / "icons" / "a.png").write_bytes(b"x")
    (old / "bridge.sock.tmp").write_text("ignored")
    assert config.migrate_legacy(new, old)
    assert (new / "icons" / "a.png").read_bytes() == b"x" and not (new / "bridge.sock.tmp").exists()
    assert (old / "config.json").exists()
    assert stat.S_IMODE(os.stat(new).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(new / "config.json").st_mode) == 0o600
    assert not config.migrate_legacy(new, old)  # already there: no-op


def test_migrate_legacy_skipped_without_old_dir(tmp_path):
    assert not config.migrate_legacy(tmp_path / "neondeck", tmp_path / "missing")
    assert not (tmp_path / "neondeck").exists()


def test_icon_is_valid_png():
    for size in (180, 192, 512):
        png = server.make_icon(size)
        assert png.startswith(b"\x89PNG\r\n\x1a\n") and int.from_bytes(png[16:20], "big") == size
