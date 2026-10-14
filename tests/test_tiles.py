from unittest import mock

import pytest

from neondeck import server
from neondeck.config import load_config
from neondeck.errors import ControlError
from neondeck.netguard import NetworkMonitor
from neondeck.sessions import SessionTracker, Context, loc_of, merge_tiles, terminal_tile


def it(sid, w, t, p, panes=1, tty=None, title="zsh", job=None, path=None, current=False):
    return {"session_id": sid, "window": w, "tab": t, "pane": p, "panes": panes, "tty": tty,
            "title": title, "job": job, "path": path, "current": current}


def claude(pid, tty, controllable=True, host="iterm"):
    return {"kind": "claude", "id": f"claude:{pid}", "pid": pid, "name": "p", "project": "p", "tty": tty,
            "host": host, "controllable": controllable, "focused": False, "status": "idle"}


def test_loc_of():
    assert loc_of(it("a", 1, 2, 1)) == "F1 · T2"
    assert loc_of(it("a", 1, 2, 2, panes=3)) == "F1 · T2 · P2"


def test_terminal_tile_fields():
    tile = terminal_tile(it("S1", 2, 1, 1, tty="/dev/ttys004", title="~ (zsh)", job="vim",
                           path="/Users/alice/proj/", current=True), "iTerm2")
    assert tile["id"] == "iterm:S1" and tile["kind"] == "terminal" and tile["host"] == "iterm"
    assert tile["title"] == "vim" and tile["project"] == "proj" and tile["loc"] == "F2 · T1"
    assert tile["focused"] and tile["controllable"] and tile["pid"] is None
    # job missing: title is used; focus requires iTerm to be frontmost
    t2 = terminal_tile(it("S2", 1, 1, 1, title="build", current=True), "Safari")
    assert t2["title"] == "build" and t2["project"] == "" and not t2["focused"]


def test_merge_claude_tty_match_and_ordering():
    cl = [claude(1, "/dev/ttys001"), claude(2, "/dev/ttys099", controllable=False, host="other"),
          claude(3, None, host="vscode")]
    sessions = [
        it("A", 1, 1, 1, tty="/dev/ttys001", job="node"),   # belongs to Claude pid 1
        it("C", 2, 1, 1, tty="/dev/ttys003", job="ssh"),
        it("B", 1, 2, 1, panes=2, tty="/dev/ttys002", job="zsh"),
        it("D", 1, 2, 2, panes=2, tty="/dev/ttys004", job="htop"),
    ]
    out = merge_tiles(cl, sessions, "iTerm2")
    assert [t["id"] for t in out] == ["claude:1", "claude:3", "iterm:B", "iterm:D", "iterm:C", "claude:2"]
    assert next(t for t in out if t["id"] == "claude:1")["loc"] == "F1 · T1"
    assert next(t for t in out if t["id"] == "claude:3")["loc"] is None
    assert not any(t["id"] == "iterm:A" for t in out)  # Claude owns that tty: no duplicate tile


def test_merge_orders_terminals_by_window_tab_pane():
    sessions = [it("3", 2, 1, 1, tty="/dev/t3"), it("2", 1, 1, 2, panes=2, tty="/dev/t2"),
                it("1", 1, 1, 1, panes=2, tty="/dev/t1")]
    assert [t["id"] for t in merge_tiles([], sessions, None)] == ["iterm:1", "iterm:2", "iterm:3"]


def test_signature_changes_with_iterm_sessions(tmp_path):
    tr = SessionTracker()
    a = tr.signature(Context(iterm_sessions=[it("1", 1, 1, 1, job="zsh")]))
    b = tr.signature(Context(iterm_sessions=[it("1", 1, 1, 1, job="vim")]))
    assert a != b


@pytest.fixture
def core(tmp_path):
    c = server.Core(tmp_path, load_config(tmp_path), NetworkMonitor(lambda: None))
    c.iterm.focus = mock.AsyncMock()
    c.iterm.send_text = mock.AsyncMock()
    c.tracker._sessions = merge_tiles(
        [claude(7, "/dev/ttys001")], [it("A", 1, 1, 1, tty="/dev/ttys001"), it("B", 1, 2, 1, tty="/dev/ttys002", job="vim")],
        "iTerm2")
    c.tracker._public = c.tracker._sessions
    return c


async def test_focus_routes_by_id_and_pid(core):
    assert await core.do_focus("iterm:B") == (True, "Sessione in primo piano")
    core.iterm.focus.assert_awaited_with(session_id="B")
    await core.do_focus("claude:7")
    core.iterm.focus.assert_awaited_with("/dev/ttys001")
    await core.do_focus(7)  # legacy pid
    assert core.iterm.focus.await_count == 3
    ok, detail = await core.do_focus("iterm:nope")
    assert not ok and "non trovata" in detail


async def test_send_text_to_terminal_tile_and_last_focus(core):
    await core.send_text("x", "iterm:B")
    core.iterm.send_text.assert_awaited_with("x", session_id="B")
    await core.send_text("y", 7)
    core.iterm.send_text.assert_awaited_with("y", "/dev/ttys001")
    core.last_focused_id = "iterm:B"
    await core.send_text("z", None)
    core.iterm.send_text.assert_awaited_with("z", session_id="B")


async def test_press_target_id_and_focus_session_matches_terminals(core):
    core.store.buttons = [
        {"id": "k", "title": "k", "icon": {"type": "emoji", "value": "x"},
         "action": {"type": "send_keys", "text": "q", "target": "selected"}},
        {"id": "f", "title": "f", "icon": {"type": "emoji", "value": "x"},
         "action": {"type": "focus_session", "match": "vim"}},
    ]
    assert (await core.do_press("k", None, "iterm:B"))[0]
    core.iterm.send_text.assert_awaited_with("q", session_id="B")
    assert (await core.do_press("f", None))[0]
    core.iterm.focus.assert_awaited_with(session_id="B")
    assert core.last_focused_id == "iterm:B"


def test_show_all_iterm_default_and_off(core):
    assert core.show_all_iterm
    core.config["show_all_iterm"] = False
    assert not core.show_all_iterm


async def test_unknown_target_raises_control_error(core):
    with pytest.raises(ControlError):
        await core.focus_pid("zzz:1")
