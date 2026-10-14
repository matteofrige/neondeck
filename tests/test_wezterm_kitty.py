import json
import os
import stat as st

import pytest

from neondeck.errors import ControlError
from neondeck.terminals import kitty, wezterm
from neondeck.terminals.kitty import KittyBackend
from neondeck.terminals.wezterm import WeztermBackend

WEZ_LIST = json.dumps([
    {"window_id": 0, "tab_id": 0, "pane_id": 0, "workspace": "default", "title": "zsh",
     "cwd": "file://devbox/home/u/my%20proj", "is_active": True, "is_zoomed": False, "tty_name": "/dev/pts/6"},
    {"window_id": 0, "tab_id": 0, "pane_id": 3, "workspace": "default", "title": "vim",
     "cwd": "file://devbox/home/u/proj", "is_active": False, "is_zoomed": False, "tty_name": "/dev/pts/7"},
    {"window_id": 0, "tab_id": 5, "pane_id": 4, "workspace": "default", "title": "htop",
     "cwd": None, "is_active": True, "is_zoomed": False, "tty_name": None},
    {"window_id": 2, "tab_id": 9, "pane_id": 8, "workspace": "default", "title": "ssh",
     "cwd": "/srv", "is_active": True, "is_zoomed": False, "tty_name": "/dev/pts/9"},
])
WEZ_CLIENTS = json.dumps([{"username": "u", "hostname": "devbox", "pid": 4321, "workspace": "default",
                           "focused_pane_id": 3}])


def test_wezterm_parse_panes():
    panes = wezterm.parse_panes(json.loads(WEZ_LIST), {3})
    assert [p["pane_id"] for p in panes] == ["0", "3", "4", "8"]
    p0, p3, p4, p8 = panes
    assert p0["tty"] == "/dev/pts/6" and p0["cwd"] == "/home/u/my proj" and p0["title"] == "zsh"
    assert p0["loc"] == "wezterm 1:1.1" and p3["loc"] == "wezterm 1:1.2" and p4["loc"] == "wezterm 1:2.1"
    assert p8["loc"] == "wezterm 2:1.1" and p0["panes"] == 2 and p4["tty"] is None
    assert [p["active"] for p in panes] == [False, True, False, False]  # only the pane a GUI client focuses
    assert all(p["apps"] == ["wezterm"] for p in panes)
    assert wezterm.parse_panes("nope") == [] and wezterm.parse_panes([{"pane_id": "x"}]) == []
    assert wezterm.parse_focused(json.loads(WEZ_CLIENTS)) == {3}
    assert wezterm.parse_client_pids(json.loads(WEZ_CLIENTS)) == [4321]


class Runner:
    def __init__(self, script):
        self.script, self.calls = script, []

    async def __call__(self, argv, timeout=5.0, input=None):
        self.calls.append((argv, input))
        for prefix, reply in self.script.items():
            if " ".join(argv).startswith(prefix):
                return reply if isinstance(reply, tuple) else (0, reply, "")
        return 1, "", "unknown command"


class FakePlat:
    def __init__(self):
        self.raised, self.apps = [], []

    async def process_table(self):
        return {1: (0, "?", "x")}

    async def raise_pids(self, pids, table):
        self.raised.append(pids)
        return bool(pids)

    async def raise_app(self, name):
        self.apps.append(name)
        return True


class Clock:
    t = 0.0

    def __call__(self):
        return self.t


async def test_wezterm_snapshot_focus_send():
    r = Runner({"wezterm cli list --format json": WEZ_LIST, "wezterm cli list-clients": WEZ_CLIENTS,
                "wezterm cli activate-pane": "", "wezterm cli send-text": ""})
    plat = FakePlat()
    b = WeztermBackend(run=r, plat=plat, which=lambda n: "/usr/bin/wezterm")
    assert await b.probe() and b.available()
    panes = await b.snapshot()
    assert [p["pane_id"] for p in panes if p["active"]] == ["3"]
    r.calls.clear()
    await b.focus("3")
    assert r.calls[0][0] == ["wezterm", "cli", "activate-pane", "--pane-id", "3"]
    assert plat.raised == [[4321]]
    await b.send_text("4", "\u001b[Z")
    assert r.calls[-1][0] == ["wezterm", "cli", "send-text", "--no-paste", "--pane-id", "4", "--", "\u001b[Z"]


async def test_wezterm_focus_falls_back_to_raising_the_app_and_reports_errors():
    r = Runner({"wezterm cli activate-pane": "", "wezterm cli list-clients": (1, "", "no")})
    plat = FakePlat()
    b = WeztermBackend(run=r, plat=plat)
    await b.focus("1")
    assert plat.apps == ["WezTerm"]
    bad = WeztermBackend(run=Runner({"wezterm cli activate-pane": (1, "", "pane 9 not found\n")}), plat=FakePlat())
    with pytest.raises(ControlError, match="not found"):
        await bad.focus("9")


async def test_wezterm_probe_is_cached_30_seconds_when_unreachable():
    clock = Clock()
    r = Runner({})  # every call fails: WezTerm not running
    b = WeztermBackend(run=r, which=lambda n: "/usr/bin/wezterm", clock=clock)
    assert not await b.probe() and len(r.calls) == 1
    clock.t = 10
    assert not await b.probe() and len(r.calls) == 1
    clock.t = 31
    assert not await b.probe() and len(r.calls) == 2
    assert not await WeztermBackend(run=r, which=lambda n: None).probe()
    assert (await WeztermBackend(which=lambda n: None).report())["text"] == "WezTerm: non trovato"
    assert (await b.report())["text"] == "WezTerm: non in esecuzione"


async def test_wezterm_report_counts():
    b = WeztermBackend(run=Runner({"wezterm cli list": WEZ_LIST}), which=lambda n: "/usr/bin/wezterm")
    assert (await b.report())["text"] == "WezTerm: attivo (2 finestre, 4 pannelli)"


# ---------------------------------------------------------------- kitty

KITTY_LS = json.dumps([
    {"id": 1, "is_focused": True, "is_active": True, "tabs": [
        {"id": 1, "is_focused": True, "title": "main", "windows": [
            {"id": 1, "is_focused": True, "title": "claude", "cwd": "/home/u/proj", "pid": 5000,
             "env": {"KITTY_PID": "4999"},
             "foreground_processes": [{"pid": 5100, "cwd": "/home/u/proj", "cmdline": ["/usr/bin/claude", "--x"]}]},
            {"id": 2, "is_focused": False, "title": "zsh", "cwd": "/srv", "pid": 5200, "env": {},
             "foreground_processes": []}]},
        {"id": 2, "is_focused": False, "title": "logs", "windows": [
            {"id": 7, "is_focused": True, "title": "tail", "cwd": "/var/log", "pid": 5300,
             "foreground_processes": [{"pid": 5301, "cmdline": ["tail", "-f", "x"]}]}]}]},
    {"id": 2, "is_focused": False, "is_active": False, "tabs": [
        {"id": 3, "is_focused": True, "windows": [
            {"id": 9, "is_focused": True, "title": "other os window", "cwd": "/", "pid": 5400,
             "foreground_processes": []}]}]},
])


def test_kitty_parse_panes():
    panes = kitty.parse_panes(json.loads(KITTY_LS))
    assert [p["pane_id"] for p in panes] == ["1", "2", "7", "9"]
    p1, p2, p7, p9 = panes
    assert p1["pid"] == 5000 and p1["tty"] is None and p1["job"] == "claude" and p1["cwd"] == "/home/u/proj"
    assert p1["loc"] == "kitty 1:1.1" and p2["loc"] == "kitty 1:1.2" and p7["loc"] == "kitty 1:2.1" and p9["loc"] == "kitty 2:1.1"
    assert p1["panes"] == 2 and p2["job"] is None and p1["kitty_pid"] == 4999
    assert [p["active"] for p in panes] == [True, False, False, False]  # focused window of the focused tab and OS window
    assert kitty.parse_panes("x") == []


def test_kitty_socket_candidates(monkeypatch):
    assert kitty.socket_candidates("/tmp/kitty") == [["--to", "unix:/tmp/kitty"]]
    assert kitty.socket_candidates("tcp:localhost:5000") == [["--to", "tcp:localhost:5000"]]
    monkeypatch.setattr(kitty.glob, "glob", lambda pattern: ["/tmp/kitty-123", "/tmp/kitty.conf"])
    monkeypatch.setattr(kitty.os, "stat", lambda p: os.stat_result(
        ((st.S_IFSOCK if p.endswith("123") else st.S_IFREG) | 0o600, 0, 0, 1, 0, 0, 0, 0, 0, 0)))
    assert kitty.socket_candidates(None, env={}) == [["--to", "unix:/tmp/kitty-123"]]
    assert kitty.socket_candidates(None, env={"KITTY_LISTEN_ON": "unix:/x"})[0] == []


async def test_kitty_probe_tries_candidates_then_uses_the_working_socket():
    calls = []

    async def run(argv, timeout=5.0, input=None):
        calls.append((argv, input))
        if "unix:/tmp/kitty-2" in argv:
            return 0, KITTY_LS if argv[-1] == "ls" else "", ""
        return 1, "", "Could not connect"

    plat = FakePlat()
    b = KittyBackend(run=run, plat=plat, which=lambda n: "/usr/bin/kitty",
                     candidates=lambda: [["--to", "unix:/tmp/kitty-1"], ["--to", "unix:/tmp/kitty-2"]])
    assert await b.probe() and b.available()
    assert [c[0][2:4] for c in calls] == [["--to", "unix:/tmp/kitty-1"], ["--to", "unix:/tmp/kitty-2"]]
    panes = await b.snapshot()
    assert calls[-1][0] == ["/usr/bin/kitty", "@", "--to", "unix:/tmp/kitty-2", "ls"] and len(panes) == 4
    assert "kitty_pid" not in panes[0]
    calls.clear()
    await b.focus("1")
    assert calls[0][0] == ["/usr/bin/kitty", "@", "--to", "unix:/tmp/kitty-2", "focus-window", "--match", "id:1"]
    assert plat.raised == [[4999]]
    await b.send_text("7", "a\\n\u001b")
    argv, stdin = calls[-1]
    assert argv[-4:] == ["send-text", "--match", "id:7", "--stdin"]
    assert stdin == "a\\n\u001b"  # sent as is: no escape-sequence interpretation


async def test_kitty_unavailable_when_remote_control_is_off():
    clock = Clock()

    async def run(argv, timeout=5.0, input=None):
        return 1, "", "Remote control is disabled"

    b = KittyBackend(run=run, which=lambda n: "/usr/bin/kitty", clock=clock, candidates=lambda: [[]])
    assert not await b.probe() and not b.available()
    assert (await b.report())["text"] == "kitty: remote control disattivato o non in esecuzione"
    assert (await KittyBackend(which=lambda n: None).report())["text"] == "kitty: non trovato"


async def test_kitty_report_counts():
    async def run(argv, timeout=5.0, input=None):
        return 0, KITTY_LS, ""

    b = KittyBackend(run=run, which=lambda n: "/usr/bin/kitty", candidates=lambda: [[]])
    assert (await b.report())["text"] == "kitty: attivo (2 finestre, 4 pannelli)"
