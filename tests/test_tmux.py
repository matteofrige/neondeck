import pytest

from neondeck.errors import ControlError
from neondeck.terminals import tmux
from neondeck.terminals.tmux import TmuxBackend


def pane_line(session="main", sid="$0", win="1", wactive="1", idx="0", pid_="%0", tty="/dev/pts/3", pactive="1",
              attached="1", cmd="zsh", wname="code", path="/home/u/proj", title="host"):
    return "\t".join(["P", session, sid, win, wactive, idx, pid_, tty, pactive, attached, cmd, wname, path, title])


def client_line(name="/dev/pts/20", tty="/dev/pts/20", session="main", activity="1000", pid="900"):
    return "\t".join(["C", name, tty, session, activity, pid])


OUTPUT = "\n".join([
    pane_line(idx="0", pid_="%0", tty="/dev/pts/3", cmd="claude", title="✳ Claude Code"),
    pane_line(idx="1", pid_="%1", tty="/dev/pts/4", pactive="0", cmd="vim", title="a\tb"),
    pane_line(sid="$1", session="logs", win="2", idx="0", pid_="%2", tty="/dev/pts/5", attached="0", cmd="tail"),
    client_line(),
    client_line(name="/dev/pts/21", tty="/dev/pts/21", session="logs", activity="2000", pid="901"),
    "garbage line", "P\ttoo\tshort",
]) + "\n"


def test_parse_output_panes_and_clients():
    panes, clients = tmux.parse_output(OUTPUT)
    assert [p["pane_id"] for p in panes] == ["%0", "%1", "%2"]
    assert panes[1]["pane_title"] == "a\tb"  # free text last: tabs survive
    assert [c["activity"] for c in clients] == [1000, 2000] and clients[1]["pid"] == 901
    assert clients[0]["name"] == "/dev/pts/20" and clients[0]["session"] == "main"


def test_pick_client_most_recent_activity():
    _, clients = tmux.parse_output(OUTPUT)
    assert tmux.pick_client(clients)["session"] == "logs"
    assert tmux.pick_client([]) is None
    tie = [{"activity": 5, "name": "a"}, {"activity": 5, "name": "b"}]
    assert tmux.pick_client(tie)["name"] == "b"


def test_build_panes_marks_only_the_pane_the_latest_client_looks_at():
    raw, clients = tmux.parse_output(OUTPUT)
    panes = tmux.build_panes(raw, clients, ["alacritty"])
    by_id = {p["pane_id"]: p for p in panes}
    assert by_id["%2"]["active"] and not by_id["%0"]["active"] and not by_id["%1"]["active"]
    assert by_id["%0"]["loc"] == "tmux main:1.0" and by_id["%2"]["loc"] == "tmux logs:2.0"
    assert by_id["%0"]["tty"] == "/dev/pts/3" and by_id["%0"]["job"] == "claude"
    assert by_id["%0"]["panes"] == 2 and by_id["%2"]["panes"] == 1 and by_id["%0"]["apps"] == ["alacritty"]
    assert (by_id["%0"]["window"], by_id["%2"]["window"]) == (1, 2)  # session ordinals
    # only the older client: the pane of its session is the active one
    panes = tmux.build_panes(raw, clients[:1], [])
    assert {p["pane_id"] for p in panes if p["active"]} == {"%0"}
    # no client attached: nothing is focused
    assert not any(p["active"] for p in tmux.build_panes(raw, [], []))


def test_hex_encoding_of_keys():
    assert tmux.hex_bytes("\u001b") == ["1b"]
    assert tmux.hex_bytes("\u0003") == ["03"]
    assert tmux.hex_bytes("\u001b[Z") == ["1b", "5b", "5a"]  # Shift+Tab
    assert tmux.hex_bytes("è") == ["c3", "a8"]
    assert tmux.hex_bytes("ciao\n") == ["63", "69", "61", "6f", "0a"]
    assert tmux.hex_bytes("\r") == ["0d"]


def test_parse_version_and_default_socket():
    assert tmux.parse_version("tmux 3.4") == (3, 4) and tmux.parse_version("tmux 3.3a") == (3, 3)
    assert tmux.parse_version("tmux next-3.5") == (3, 5) and tmux.parse_version("tmux master") is None
    assert tmux.default_socket({}, uid=1000) == "/tmp/tmux-1000/default"
    assert tmux.default_socket({"TMUX_TMPDIR": "/run/user/1000"}, uid=1000) == "/run/user/1000/tmux-1000/default"


class Runner:
    """Records tmux invocations and answers from a script."""

    def __init__(self, list_output=OUTPUT, version="tmux 3.4\n"):
        self.calls: list[list[str]] = []
        self.list_output, self.version = list_output, version
        self.fail: set[str] = set()

    async def __call__(self, argv, timeout=5.0, input=None):
        self.calls.append(argv)
        cmd = [a for a in argv[1:] if a not in ("-S",) and not a.startswith("/")][0]
        if argv[1] == "-V":
            return 0, self.version, ""
        if cmd in self.fail:
            return 1, "", f"{cmd}: failed\n"
        return 0, self.list_output if cmd == "list-panes" else "", ""

    def commands(self):
        return [[a for a in c if a != "-S" and not a.startswith("/tmp/")] for c in self.calls]


class FakePlat:
    name = "linux"

    def __init__(self):
        self.raised = []

    async def process_table(self):
        return {900: (800, "/dev/pts/20", "/usr/bin/zsh"), 800: (700, "?", "/usr/bin/alacritty"),
                901: (800, "/dev/pts/21", "/usr/bin/zsh"), 700: (1, "?", "/usr/lib/systemd")}

    async def raise_pids(self, pids, table):
        self.raised.append(pids)
        return True


def backend(runner=None, **kw):
    return TmuxBackend(socket="/tmp/x/sock", run=runner or Runner(), plat=FakePlat(), **kw)


async def test_snapshot_one_call_and_host_apps():
    r = Runner()
    b = backend(r)
    panes = await b.snapshot()
    assert len(r.calls) == 1
    argv = r.calls[0]
    assert argv[:3] == ["tmux", "-S", "/tmp/x/sock"] and argv.index(";") > argv.index("list-panes")
    assert "list-panes" in argv and "-a" in argv and "list-clients" in argv
    assert tmux.PANE_FMT in argv and tmux.CLIENT_FMT in argv
    assert [p["pane_id"] for p in panes] == ["%0", "%1", "%2"]
    assert panes[0]["apps"] == ["alacritty", "systemd"]  # ancestors of the most recent client
    assert all(p["backend"] == "tmux" for p in panes)


async def test_snapshot_without_server_is_empty():
    r = Runner()
    r.fail.add("list-panes")
    assert await backend(r).snapshot() == []


async def test_focus_switches_client_selects_window_and_pane_then_raises():
    r, plat = Runner(), FakePlat()
    b = TmuxBackend(socket="/tmp/x/sock", run=r, plat=plat)
    await b.snapshot()
    r.calls.clear()
    await b.focus("%0")
    cmds = r.commands()
    assert cmds[0] == ["tmux", "list-panes", "-a", "-F", tmux.PANE_FMT, ";", "list-clients", "-F", tmux.CLIENT_FMT]
    assert cmds[1:] == [["tmux", "switch-client", "-c", "/dev/pts/21", "-t", "$0"],  # the most recently active client
                        ["tmux", "select-window", "-t", "$0:1"],
                        ["tmux", "select-pane", "-t", "%0"]]
    assert plat.raised == [[800, 700]]  # ancestors of client pid 901, nearest first


async def test_focus_uses_raiser_hook_with_the_client():
    r = Runner()
    b = backend(r)
    seen = []

    async def hook(client, chain, table):
        seen.append((client["name"], chain, sorted(table)))

    b.raiser = hook
    await b.snapshot()
    await b.focus("%2")
    assert seen == [("/dev/pts/21", [800, 700], [700, 800, 900, 901])]


async def test_focus_without_attached_client_still_selects_then_reports():
    clientless = "\n".join(l for l in OUTPUT.splitlines() if not l.startswith("C\t")) + "\n"
    r = Runner(clientless)
    b = backend(r)
    with pytest.raises(ControlError, match="Nessun client tmux collegato"):
        await b.focus("%1")
    cmds = r.commands()
    assert ["tmux", "select-window", "-t", "$0:1"] in cmds and ["tmux", "select-pane", "-t", "%1"] in cmds
    assert not any(c[1] == "switch-client" for c in cmds)


async def test_focus_unknown_pane():
    with pytest.raises(ControlError, match="non trovato"):
        await backend().focus("%99")


async def test_send_text_hex_and_literal_fallback():
    r = Runner()
    b = backend(r)
    await b.send_text("%0", "\u001b[Z")
    await b.send_text("%0", "è")
    assert r.commands()[-2:] == [["tmux", "send-keys", "-t", "%0", "-H", "1b", "5b", "5a"],
                                 ["tmux", "send-keys", "-t", "%0", "-H", "c3", "a8"]]
    old = Runner(version="tmux 2.9\n")
    b = backend(old)
    await b.send_text("%0", "ciao")
    assert old.commands()[-1] == ["tmux", "send-keys", "-t", "%0", "-l", "ciao"]
    assert sum(1 for c in old.calls if c[1] == "-V") == 1  # version checked once
    await b.send_text("%0", "")  # nothing to send
    assert len(old.calls) == 2


def test_available_needs_binary_and_server_socket(monkeypatch):
    import os
    import stat as st

    modes = {"/run/s": st.S_IFSOCK | 0o600, "/run/plain": st.S_IFREG | 0o600}

    def fake_stat(path):
        if path not in modes:
            raise FileNotFoundError(path)
        return os.stat_result((modes[path], 0, 0, 1, 0, 0, 0, 0, 0, 0))

    monkeypatch.setattr(tmux.os, "stat", fake_stat)
    has = lambda n: "/usr/bin/tmux"
    assert TmuxBackend(socket="/run/s", which=has).available()
    assert not TmuxBackend(socket="/run/s", which=lambda n: None).available()
    assert not TmuxBackend(socket="/run/missing", which=has).available()
    assert not TmuxBackend(socket="/run/plain", which=has).available()  # not a socket


async def test_report_lines(tmp_path):
    r = Runner()
    b = TmuxBackend(socket="/tmp/x/sock", run=r, plat=FakePlat(), which=lambda n: "/usr/bin/tmux")
    b.available = lambda: True
    rep = await b.report()
    assert rep["available"] and rep["text"] == "tmux: attivo (2 sessioni, 3 pannelli)"
    assert (await TmuxBackend(which=lambda n: None).report())["text"] == "tmux: non trovato"
    nosrv = TmuxBackend(socket=str(tmp_path / "none"), which=lambda n: "/usr/bin/tmux")
    assert (await nosrv.report())["text"] == "tmux: nessun server in esecuzione"
