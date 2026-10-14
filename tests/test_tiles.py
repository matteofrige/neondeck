import pytest

from neondeck import server
from neondeck.config import load_config
from neondeck.errors import ControlError
from neondeck.netguard import NetworkMonitor
from neondeck.sessions import Context, SessionTracker, loc_of, merge_tiles, pane_focused, terminal_tile

from conftest import FakeBackend


def pane(backend, pid_, w, t, p, panes=1, tty=None, title="zsh", job=None, cwd=None, active=False, apps=None, **kw):
    return {"backend": backend, "pane_id": pid_, "window": w, "tab": t, "pane": p, "panes": panes, "tty": tty,
            "title": title, "job": job, "cwd": cwd, "active": active, "apps": apps or [backend],
            "loc": loc_of({"window": w, "tab": t, "pane": p, "panes": panes}), **kw}


def it(sid, w, t, p, panes=1, tty=None, title="zsh", job=None, path=None, current=False):
    return pane("iterm", sid, w, t, p, panes, tty, title, job, path, current, ["iTerm2"])


def claude(pid, tty, controllable=True, host="iterm", ancestors=()):
    return {"kind": "claude", "id": f"claude:{pid}", "pid": pid, "name": "p", "project": "p", "tty": tty,
            "host": host, "controllable": controllable, "focused": False, "status": "idle",
            "_ancestors": list(ancestors)}


def test_loc_of():
    assert loc_of({"window": 1, "tab": 2, "pane": 1}) == "F1 · T2"
    assert loc_of({"window": 1, "tab": 2, "pane": 2, "panes": 3}) == "F1 · T2 · P2"


def test_terminal_tile_fields():
    tile = terminal_tile(it("S1", 2, 1, 1, tty="/dev/ttys004", title="~ (zsh)", job="vim",
                            path="/Users/alice/proj/", current=True), True)
    assert tile["id"] == "iterm:S1" and tile["kind"] == "terminal" and tile["host"] == "iterm"
    assert tile["title"] == "vim" and tile["project"] == "proj" and tile["loc"] == "F2 · T1"
    assert tile["focused"] and tile["controllable"] and tile["pid"] is None
    assert tile["backend"] == "iterm" and tile["paneId"] == "S1"
    # job missing: title is used
    t2 = terminal_tile(it("S2", 1, 1, 1, title="build"), False)
    assert t2["title"] == "build" and t2["project"] == "" and not t2["focused"]
    assert terminal_tile(pane("tmux", "%3", 1, 1, 1), False)["id"] == "tmux:%3"


def test_pane_focused_needs_active_and_matching_front_app():
    p = it("A", 1, 1, 1, current=True)
    assert pane_focused(p, "iTerm2") and pane_focused(p, "iterm2")
    assert not pane_focused(p, "Safari") and not pane_focused(p, None)
    assert not pane_focused(it("B", 1, 1, 1, current=False), "iTerm2")
    assert pane_focused(p, None, front_known=False)  # platform cannot tell: trust the terminal
    tm = pane("tmux", "%1", 1, 1, 1, active=True, apps=["alacritty", "Alacritty"])
    assert pane_focused(tm, "Alacritty") and not pane_focused(tm, "firefox")
    assert not pane_focused(pane("tmux", "%1", 1, 1, 1, active=True, apps=[]), "Alacritty")


def test_merge_claude_tty_match_and_ordering():
    cl = [claude(1, "/dev/ttys001"), claude(2, "/dev/ttys099", controllable=False, host="other"),
          claude(3, None, host="vscode")]
    cl[2]["controllable"] = True  # controllable through the VS Code bridge
    panes = [
        it("A", 1, 1, 1, tty="/dev/ttys001", job="node"),   # belongs to Claude pid 1
        it("C", 2, 1, 1, tty="/dev/ttys003", job="ssh"),
        it("B", 1, 2, 1, panes=2, tty="/dev/ttys002", job="zsh"),
        it("D", 1, 2, 2, panes=2, tty="/dev/ttys004", job="htop"),
    ]
    out = merge_tiles(cl, panes, "iTerm2")
    assert [t["id"] for t in out] == ["claude:1", "claude:3", "iterm:B", "iterm:D", "iterm:C", "claude:2"]
    assert next(t for t in out if t["id"] == "claude:1")["loc"] == "F1 · T1"
    assert next(t for t in out if t["id"] == "claude:3")["loc"] is None
    assert not any(t["id"] == "iterm:A" for t in out)  # Claude owns that tty: no duplicate tile


def test_merge_orders_terminals_by_backend_window_tab_pane():
    panes = [pane("tmux", "%1", 1, 1, 1, tty="/dev/pts/9"), it("3", 2, 1, 1, tty="/dev/t3"),
             it("2", 1, 1, 2, panes=2, tty="/dev/t2"), it("1", 1, 1, 1, panes=2, tty="/dev/t1")]
    assert [t["id"] for t in merge_tiles([], panes, None)] == ["iterm:1", "iterm:2", "iterm:3", "tmux:%1"]


def test_claude_matched_to_tmux_pane_by_tty():
    tm = pane("tmux", "%4", 1, 2, 1, tty="/dev/pts/7", job="claude", active=True, apps=["alacritty"])
    other = pane("tmux", "%5", 1, 2, 2, tty="/dev/pts/8", job="vim")
    c = claude(10, "/dev/pts/7", controllable=False, host="tmux")  # heuristic host, not yet matched
    out = merge_tiles([c], [tm, other], "alacritty")
    assert [t["id"] for t in out] == ["claude:10", "tmux:%5"]
    assert out[0]["host"] == "tmux" and out[0]["controllable"] and out[0]["focused"]
    assert out[0]["loc"] == tm["loc"] and out[0]["_pane"] == ("tmux", "%4")


def test_claude_matched_by_shell_pid_when_pane_has_no_tty():
    kt = pane("kitty", "12", 1, 1, 1, pid=555, tty=None, active=False)
    c = claude(10, "/dev/pts/7", controllable=False, host="other", ancestors=[600, 555, 1])
    out = merge_tiles([c], [kt], None)
    assert [t["id"] for t in out] == ["claude:10"] and out[0]["host"] == "kitty" and out[0]["controllable"]


def test_show_all_off_hides_terminal_tiles_but_keeps_claude_tags():
    panes = [it("A", 1, 1, 1, tty="/dev/ttys001"), it("B", 1, 2, 1, tty="/dev/ttys002")]
    out = merge_tiles([claude(1, "/dev/ttys001")], panes, None, show_all=False)
    assert [t["id"] for t in out] == ["claude:1"] and out[0]["loc"] == "F1 · T1"


def test_iterm_tmux_gateway_hidden_and_claude_goes_to_tmux():
    gateway = it("G", 1, 1, 1, tty="/dev/ttys010", job="tmux")  # iTerm2 session running `tmux -CC`
    tm = pane("tmux", "%1", 1, 1, 1, tty="/dev/ttys011", job="claude")
    out = merge_tiles([claude(7, "/dev/ttys011", controllable=False, host="tmux")], [gateway, tm], None)
    assert [t["id"] for t in out] == ["claude:7"] and out[0]["host"] == "tmux"
    # without a Claude session the tmux pane gets a tile and the gateway still does not
    out = merge_tiles([], [gateway, tm], None)
    assert [t["id"] for t in out] == ["tmux:%1"]


def test_signature_changes_with_panes_and_focus(tmp_path):
    tr = SessionTracker()
    a = tr.signature(Context(panes=[it("1", 1, 1, 1, job="zsh")]))
    b = tr.signature(Context(panes=[it("1", 1, 1, 1, job="vim")]))
    assert a != b
    on = tr.signature(Context(panes=[it("1", 1, 1, 1, current=True)], front_app="iTerm2"))
    off = tr.signature(Context(panes=[it("1", 1, 1, 1, current=True)], front_app="Safari"))
    assert on != off


@pytest.fixture
def core(tmp_path):
    iterm, tmux = FakeBackend("iterm", "iTerm2"), FakeBackend("tmux", "tmux")
    c = server.Core(tmp_path, load_config(tmp_path), NetworkMonitor(lambda: None), backends=[iterm, tmux])
    panes = [it("A", 1, 1, 1, tty="/dev/ttys001"), it("B", 1, 2, 1, tty="/dev/ttys002", job="vim"),
             pane("tmux", "%3", 1, 1, 1, tty="/dev/pts/3", job="htop")]
    cl = [claude(7, "/dev/ttys001"), claude(8, "/dev/pts/9", controllable=False, host="other")]
    c.tracker._sessions = merge_tiles(cl, panes, "iTerm2")
    c.tracker._public = c.tracker._sessions
    return c, iterm, tmux


async def test_focus_routes_by_id_and_pid(core):
    c, iterm, tmux = core
    assert await c.do_focus("iterm:B") == (True, "Sessione in primo piano")
    assert iterm.focused == ["B"]
    await c.do_focus("claude:7")
    assert iterm.focused == ["B", "A"]
    await c.do_focus(7)  # legacy pid
    assert iterm.focused == ["B", "A", "A"]
    await c.do_focus("tmux:%3")
    assert tmux.focused == ["%3"]
    ok, detail = await c.do_focus("iterm:nope")
    assert not ok and "non trovata" in detail
    ok, detail = await c.do_focus("claude:8")  # a terminal the agent cannot drive
    assert not ok and "non supportato" in detail


async def test_send_text_to_terminal_tile_and_last_focus(core):
    c, iterm, tmux = core
    await c.send_text("x", "iterm:B")
    await c.send_text("y", 7)
    c.last_focused_id = "tmux:%3"
    await c.send_text("z", None)
    assert iterm.sent == [("B", "x"), ("A", "y")] and tmux.sent == [("%3", "z")]


async def test_press_target_id_and_focus_session_matches_terminals(core):
    c, iterm, _ = core
    c.store.buttons = [
        {"id": "k", "title": "k", "icon": {"type": "emoji", "value": "x"},
         "action": {"type": "send_keys", "text": "q", "target": "selected"}},
        {"id": "f", "title": "f", "icon": {"type": "emoji", "value": "x"},
         "action": {"type": "focus_session", "match": "vim"}},
    ]
    assert (await c.do_press("k", None, "iterm:B"))[0]
    assert iterm.sent == [("B", "q")]
    assert (await c.do_press("f", None))[0]
    assert iterm.focused == ["B"] and c.last_focused_id == "iterm:B"


def test_show_all_terminals_default_off_and_legacy_key(core):
    c, _, _ = core
    assert c.show_all_terminals
    c.config["show_all_iterm"] = False  # pre-0.4 key still honored
    assert not c.show_all_terminals
    c.config["show_all_terminals"] = True  # the new key wins
    assert c.show_all_terminals


async def test_unavailable_backend_is_a_control_error(core):
    c, iterm, _ = core
    iterm.ok = False
    ok, detail = await c.do_focus("iterm:B")
    assert not ok and "non raggiungibile" in detail


async def test_unknown_target_raises_control_error(core):
    c, _, _ = core
    with pytest.raises(ControlError):
        await c.focus_pid("zzz:1")


async def test_tracker_resolves_linux_style_process_table_to_a_tmux_tile(tmp_path, monkeypatch):
    """A Claude session file + /proc-style table (pts tty, tmux server ancestor) + a tmux pane list -> tmux tile."""
    import os

    from neondeck import sessions as sess

    sf = tmp_path / f"{os.getpid()}.json"  # a live pid, so the session counts as running
    sf.write_text('{"pid": %d, "kind": "interactive", "cwd": "/srv/proj", "status": "busy", "sessionId": "s1"}'
                  % os.getpid())
    entry = next(e for e in os.scandir(tmp_path))
    table = {os.getpid(): (300, "/dev/pts/7", "/usr/bin/node"), 300: (200, "/dev/pts/7", "/usr/bin/bash"),
             200: (1, "?", "/usr/bin/tmux")}

    async def fake_table():
        return table

    monkeypatch.setattr(sess, "_json_files", lambda: [entry])
    monkeypatch.setattr(sess, "process_table", fake_table)
    tm = pane("tmux", "%7", 1, 1, 1, tty="/dev/pts/7", job="node", active=True, apps=["alacritty"])
    tr = SessionTracker()
    out = await tr.refresh(Context(panes=[tm], connected=frozenset({"tmux"}), front_app="Alacritty"))
    assert [t["id"] for t in out] == [f"claude:{os.getpid()}"]
    s = out[0]
    assert s["host"] == "tmux" and s["controllable"] and s["focused"] and s["loc"] == tm["loc"]
    assert s["tty"] == "/dev/pts/7" and tr.procs
    assert "_pane" not in tr.sessions[0]  # internal fields never reach the devices
    # tmux backend gone: the heuristic host stays, but nothing can drive it
    out = await tr.refresh(Context(panes=[], connected=frozenset()))
    assert out[0]["host"] == "tmux" and not out[0]["controllable"]


def test_pane_focused_ignores_very_short_app_names():
    p = pane("tmux", "%1", 1, 1, 1, active=True, apps=["zsh", "sh"])
    assert not pane_focused(p, "Notes") and not pane_focused(p, "zsh-something")
    assert pane_focused(pane("tmux", "%1", 1, 1, 1, active=True, apps=["zsh"]), "zsh")  # exact match always counts


async def test_backend_report_lists_every_backend_and_the_vscode_bridge(tmp_path):
    c = server.Core(tmp_path, load_config(tmp_path), NetworkMonitor(lambda: None),
                    backends=[FakeBackend("tmux", "tmux"), FakeBackend("kitty", "kitty", ok=False)])
    rows = await c.backend_report()
    assert [r["name"] for r in rows] == ["tmux", "kitty", "vscode"]
    assert rows[-1]["text"] == "VS Code bridge: 0 finestre"
