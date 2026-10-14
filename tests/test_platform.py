import ipaddress
import os
import subprocess
import sys
from pathlib import Path

import pytest

from neondeck import netguard as ng
from neondeck import platform
from neondeck.platform import linux, linuxnet
from neondeck.sessions import host_of

# ---------------------------------------------------------------- dispatch


def test_dispatch_by_sys_platform(monkeypatch):
    monkeypatch.setattr("sys.platform", "darwin")
    assert platform.current().name == "darwin"
    monkeypatch.setattr("sys.platform", "linux")
    assert platform.current().name == "linux"
    monkeypatch.setattr("sys.platform", "win32")
    with pytest.raises(platform.UnsupportedPlatform, match="Piattaforma non supportata"):
        platform.current()


# ---------------------------------------------------------------- /proc

def test_parse_proc_stat_handles_spaces_and_parentheses_in_comm():
    assert linux.parse_proc_stat("1234 (bash) S 1200 1234 1234 34819 1234 4194304 1 0") == (1200, 34819)
    assert linux.parse_proc_stat("4321 (tmux: server (x) y) S 1 4321 4321 0 -1 4194560 9") == (1, 0)
    assert linux.parse_proc_stat("77 (a b) R 5 77 77 1025 77") == (5, 1025)
    assert linux.parse_proc_stat("garbage") is None
    assert linux.parse_proc_stat("1 (x) S") is None


def test_tty_from_nr_maps_pts_and_virtual_consoles():
    assert linux.tty_from_nr(0) is None
    assert linux.tty_from_nr((136 << 8) | 3) == "/dev/pts/3"
    assert linux.tty_from_nr(137 << 8) == "/dev/pts/256"  # minors above 255 spill into the next major
    assert linux.tty_from_nr((136 << 8) | 44 | (1 << 20)) == "/dev/pts/300"  # high minor bits live at bit 20
    assert linux.tty_from_nr((4 << 8) | 1) == "/dev/tty1"
    assert linux.tty_from_nr((4 << 8) | 65) == "/dev/ttyS1"
    assert linux.tty_from_nr((5 << 8) | 0) is None  # /dev/tty and friends: not a real terminal


def make_proc(root: Path, pid: int, stat: str, comm: str, exe: str | None = None) -> None:
    d = root / str(pid)
    d.mkdir()
    (d / "stat").write_text(stat)
    (d / "comm").write_text(comm + "\n")
    if exe:
        os.symlink(exe, d / "exe")


def test_read_proc_table_from_a_fake_proc_tree(tmp_path):
    make_proc(tmp_path, 100, "100 (bash) S 1 100 100 34819 100 0", "bash", "/usr/bin/bash")
    make_proc(tmp_path, 200, "200 (sleep) S 100 200 100 34819 100 0", "sleep")  # exe unreadable: comm is used
    make_proc(tmp_path, 300, "300 (kworker/0:1) I 2 0 0 0 -1 0", "kworker/0:1")
    (tmp_path / "400").mkdir()  # exited between listdir and open
    (tmp_path / "self").mkdir()
    table = linux.read_proc_table(str(tmp_path))
    assert table == {100: (1, "/dev/pts/3", "/usr/bin/bash"), 200: (100, "/dev/pts/3", "sleep"),
                     300: (2, "?", "kworker/0:1")}
    assert linux.read_proc_table(str(tmp_path / "missing")) == {}
    assert linux.proc_name(100, str(tmp_path)) == "bash" and linux.proc_name(200, str(tmp_path)) == "sleep"


def test_host_of_recognizes_linux_executables():
    assert host_of("/usr/bin/tmux") == "tmux" and host_of("tmux: server") == "tmux"
    assert host_of("/opt/homebrew/bin/tmux") == "tmux"
    assert host_of("/usr/share/code/code") == "vscode" and host_of("/usr/bin/codium") == "vscode"
    assert host_of("/opt/Cursor/cursor") == "vscode"
    assert host_of("/usr/bin/bash") is None and host_of("/usr/bin/alacritty") is None


# ---------------------------------------------------------------- network (ip -j and /proc/net)

ROUTE_JSON = ('[{"dst":"default","gateway":"192.168.50.1","dev":"wlan0","protocol":"dhcp","metric":600},'
              '{"dst":"default","gateway":"192.168.50.1","dev":"eth0","protocol":"dhcp","metric":100},'
              '{"dst":"default","dev":"wg0","scope":"link","metric":50},'
              '{"dst":"192.168.50.0/24","dev":"eth0","scope":"link"}]')
NEIGH_JSON = '[{"dst":"192.168.50.1","dev":"eth0","lladdr":"02:00:5E:10:00:01","state":["REACHABLE"]}]'
NEIGH_FAILED = '[{"dst":"192.168.50.1","dev":"eth0","state":["FAILED"]}]'
ADDR_JSON = ('[{"ifindex":1,"ifname":"lo","addr_info":[{"family":"inet","local":"127.0.0.1","prefixlen":8,"scope":"host"}]},'
             '{"ifindex":2,"ifname":"eth0","addr_info":[{"family":"inet","local":"192.168.50.23","prefixlen":24,"scope":"global"},'
             '{"family":"inet6","local":"2001:db8::5","prefixlen":64,"scope":"global"},'
             '{"family":"inet6","local":"fe80::1c2b:3d4e:5f60:7182","prefixlen":64,"scope":"link"}]},'
             '{"ifindex":3,"ifname":"tun0","addr_info":[{"family":"inet","local":"10.8.0.2","prefixlen":22,"scope":"global"}]}]')
PROC_ROUTE = ("Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT\n"
              "eth0\t00000000\t0132A8C0\t0003\t0\t0\t100\t00000000\t0\t0\t0\n"
              "wlan0\t00000000\t0132A8C0\t0003\t0\t0\t600\t00000000\t0\t0\t0\n"
              "eth0\t0032A8C0\t00000000\t0001\t0\t0\t100\t00FFFFFF\t0\t0\t0\n")
PROC_ARP = ("IP address       HW type     Flags       HW address            Mask     Device\n"
            "192.168.50.77    0x1         0x0         00:00:00:00:00:00     *        eth0\n"
            "192.168.50.1     0x1         0x2         02:00:5e:10:00:01     *        eth0\n")


def test_parse_ip_route_json_picks_lowest_metric_with_gateway():
    assert linuxnet.parse_ip_route_json(ROUTE_JSON) == ("192.168.50.1", "eth0")
    assert linuxnet.parse_ip_route_json("[]") == (None, None)
    assert linuxnet.parse_ip_route_json("not json") == (None, None)
    assert linuxnet.parse_ip_route_json('[{"dst":"default","dev":"wg0"}]') == (None, None)


def test_parse_proc_net_route_and_arp():
    assert linuxnet.parse_proc_net_route(PROC_ROUTE) == ("192.168.50.1", "eth0")
    assert linuxnet.parse_proc_net_route("Iface\n") == (None, None)
    assert linuxnet.parse_proc_net_arp(PROC_ARP, "192.168.50.1") == "02:00:5e:10:00:01"
    assert linuxnet.parse_proc_net_arp(PROC_ARP, "192.168.50.77") is None  # incomplete entry
    assert linuxnet.parse_proc_net_arp(PROC_ARP, "192.168.50.1", "wlan0") is None  # other device
    assert linuxnet.parse_proc_net_arp("", "192.168.50.1") is None


def test_parse_ip_neigh_json():
    assert linuxnet.parse_ip_neigh_json(NEIGH_JSON) == "02:00:5e:10:00:01"  # normalized lowercase
    assert linuxnet.parse_ip_neigh_json(NEIGH_FAILED) is None
    assert linuxnet.parse_ip_neigh_json("[]") is None


def test_parse_ip_addr_json_skips_loopback_and_link_local():
    by_iface = linuxnet.parse_ip_addr_json(ADDR_JSON)
    assert list(by_iface) == ["eth0", "tun0"]
    assert ipaddress.ip_interface("192.168.50.23/24") in by_iface["eth0"]
    assert ipaddress.ip_interface("2001:db8::5/64") in by_iface["eth0"]
    assert len(by_iface["eth0"]) == 2
    flat = [i for lst in by_iface.values() for i in lst]
    assert ng.in_local_subnet("10.8.3.9", flat) and not ng.in_local_subnet("10.8.4.1", flat)


def test_interfaces_from_proc_pairs_local_ips_with_onlink_routes():
    d = linuxnet.interfaces_from_proc(PROC_ROUTE, ["192.168.50.23", "127.0.0.1", "203.0.113.9"])
    assert d == {"eth0": [ipaddress.ip_interface("192.168.50.23/24")]}


class FakeSys:
    """Fake `ip` output and /proc files for diagnose_linux."""

    def __init__(self, route=ROUTE_JSON, neigh=NEIGH_JSON, addr=ADDR_JSON, files=None, after_poke=None):
        self.out = {("ip", "-j", "route"): route, ("ip", "-j", "neigh"): neigh, ("ip", "-j", "addr"): addr}
        self.files = files or {}
        self.after_poke = after_poke
        self.pokes: list[str] = []
        self.calls = 0

    def run(self, cmd):
        self.calls += 1
        return self.out.get(tuple(cmd[:3]), "")

    def read(self, path):
        return self.files.get(path, "")

    def poke(self, gw):
        self.pokes.append(gw)
        if self.after_poke is not None:
            self.out[("ip", "-j", "neigh")] = self.after_poke


def test_diagnose_linux_with_ip_json():
    fs = FakeSys()
    net, reason = linuxnet.diagnose_linux(fs.run, fs.poke, fs.read)
    assert reason is None and fs.pokes == []
    assert net["mac"] == "02:00:5e:10:00:01" and net["gateway"] == "192.168.50.1"
    assert net["interface"] == "eth0" and net["ip"] == "192.168.50.23" and net["subnet"] == "192.168.50.0/24"
    assert ipaddress.ip_interface("10.8.0.2/22") in net["interfaces"]


def test_diagnose_linux_pokes_gateway_once_when_neighbor_is_missing():
    fs = FakeSys(neigh="[]", after_poke=NEIGH_JSON)
    net, reason = linuxnet.diagnose_linux(fs.run, fs.poke, fs.read)
    assert net["mac"] == "02:00:5e:10:00:01" and fs.pokes == ["192.168.50.1"]
    fs = FakeSys(neigh="[]")
    assert linuxnet.diagnose_linux(fs.run, fs.poke, fs.read) == (None, ng.NO_ARP)
    assert fs.pokes == ["192.168.50.1"]


def test_diagnose_linux_without_ip_uses_proc_files(monkeypatch):
    monkeypatch.setattr("neondeck.config.lan_ips", lambda: ["192.168.50.23"])
    fs = FakeSys(route="", neigh="", addr="", files={"/proc/net/route": PROC_ROUTE, "/proc/net/arp": PROC_ARP})
    net, reason = linuxnet.diagnose_linux(fs.run, fs.poke, fs.read)
    assert reason is None and net["mac"] == "02:00:5e:10:00:01" and net["interface"] == "eth0"
    assert net["subnet"] == "192.168.50.0/24" and net["ip"] == "192.168.50.23"


def test_diagnose_linux_no_route():
    fs = FakeSys(route="[]")
    assert linuxnet.diagnose_linux(fs.run, fs.poke, fs.read) == (None, ng.NO_ROUTE)


def test_linux_mac_matches_entries_trusted_on_macos():
    fs = FakeSys()
    net = linuxnet.diagnose_linux(fs.run, fs.poke, fs.read)[0]
    assert ng.is_trusted(net["mac"], [{"mac": "02:00:5E:10:0:1", "name": "casa"}])  # macOS-style entry


def test_linux_probe_is_cheap_in_steady_state():
    fs = FakeSys()
    t = [0.0]
    probe = linuxnet.LinuxProbe(fs.run, lambda: t[0], fs.poke, fs.read)
    assert probe()["mac"] == "02:00:5e:10:00:01"
    calls = fs.calls
    probe(); probe()
    assert fs.calls == calls + 2  # one route read per call, no neigh/addr
    t[0] += linuxnet.LinuxProbe.REFRESH + 1
    probe()
    assert fs.calls > calls + 3
    fs.out[("ip", "-j", "route")] = "[]"
    assert probe() is None and probe.reason == ng.NO_ROUTE


async def test_network_monitor_uses_the_platform_probe_by_default(monkeypatch):
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setattr(linux.LinuxPlatform, "network_probe", lambda self: (lambda: {"mac": "02:00:5e:10:00:01"}))
    mon = ng.NetworkMonitor()
    assert (await mon.refresh())["mac"] == "02:00:5e:10:00:01"


# ---------------------------------------------------------------- systemd

def test_render_unit():
    unit = linux.render_unit("/home/u/.local/bin/neondeck", {"PATH": "/home/u/.local/bin:/usr/bin",
                                                             "NEONDECK_HOME": "/srv/nd"})
    assert 'ExecStart="/home/u/.local/bin/neondeck" run' in unit
    assert "Restart=on-failure" in unit and "WantedBy=default.target" in unit
    assert 'Environment="PATH=/home/u/.local/bin:/usr/bin"' in unit
    assert 'Environment="NEONDECK_HOME=/srv/nd"' in unit
    assert "%%" in linux.render_unit("/a/neondeck", {"X": "50%"})


def test_unit_env_includes_local_bin_and_optional_home(monkeypatch):
    monkeypatch.delenv("NEONDECK_HOME", raising=False)
    monkeypatch.delenv("CLAUDE_DECK_HOME", raising=False)
    env = linux.unit_env()
    assert f"{Path.home()}/.local/bin" in env["PATH"] and "NEONDECK_HOME" not in env
    monkeypatch.setenv("NEONDECK_HOME", "/srv/nd")
    assert linux.unit_env()["NEONDECK_HOME"] == "/srv/nd"


def test_systemd_install_runs_enable_now_and_uninstall_removes_unit(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr(linux, "systemd_available", lambda: True)
    calls = []
    monkeypatch.setattr(linux, "systemctl", lambda *a: calls.append(a) or subprocess.CompletedProcess(a, 0, "", ""))
    svc = linux.SystemdService()
    assert svc.install() == 0 and svc.started
    unit = tmp_path / "systemd" / "user" / "neondeck.service"
    assert unit.is_file() and "ExecStart=" in unit.read_text()
    assert calls == [("daemon-reload",), ("enable", "--now", "neondeck.service")]
    assert "loginctl enable-linger" in capsys.readouterr().out
    assert svc.installed() and svc.log_hint() == "Log: journalctl --user -u neondeck -f"
    calls.clear()
    svc.uninstall()
    assert not unit.exists() and ("disable", "--now", "neondeck.service") in calls


def test_systemd_install_degrades_without_user_instance(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr(linux, "systemd_available", lambda: False)
    svc = linux.SystemdService()
    assert svc.install() == 0 and not svc.started
    assert "neondeck run" in capsys.readouterr().err
    assert svc.restart() == 1
    assert any("non disponibile" in l for l in svc.status_lines())


def test_systemd_status_lines(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr(linux, "systemd_available", lambda: True)

    def fake(*a):
        out = {"is-active": "active\n", "show": "MainPID=4242\n"}.get(a[0], "")
        return subprocess.CompletedProcess(a, 0, out, "")

    monkeypatch.setattr(linux, "systemctl", fake)
    lines = linux.SystemdService().status_lines()
    assert lines[-1] == "systemd: attivo (PID 4242)"
    assert linux.parse_show("MainPID=12\nActiveState=active\n") == {"MainPID": "12", "ActiveState": "active"}


# ---------------------------------------------------------------- LinuxPlatform

def test_linux_config_dir_honors_xdg(monkeypatch, tmp_path):
    p = linux.LinuxPlatform()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert p.config_dir() == tmp_path / "neondeck"
    monkeypatch.delenv("XDG_CONFIG_HOME")
    assert p.config_dir() == Path.home() / ".config" / "neondeck"
    monkeypatch.setenv("XDG_CONFIG_HOME", "relative/path")  # the spec says to ignore relative values
    assert p.config_dir() == Path.home() / ".config" / "neondeck"


def test_linux_open_shell_and_front_support(monkeypatch):
    p = linux.LinuxPlatform()
    monkeypatch.setattr("shutil.which", lambda n: "/usr/bin/" + n if n in ("xdg-open", "xdotool") else None)
    assert p.open_argv("https://example.com") == ["xdg-open", "https://example.com"]
    assert p.shell_argv("ls")[-2:] == ["-lc", "ls"] or p.shell_argv("ls")[-2:] == ["-c", "ls"]
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
    assert p.front_supported()
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    assert not p.front_supported()
    monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
    monkeypatch.delenv("DISPLAY")
    assert not p.front_supported()
    monkeypatch.setattr("shutil.which", lambda n: None)
    assert p.open_argv("https://example.com") is None


async def test_linux_frontmost_app_through_xdotool(monkeypatch):
    p = linux.LinuxPlatform()
    monkeypatch.setattr(p, "_x11", lambda: True)
    seen = []

    async def fake_run(argv, timeout=5, input=None):
        seen.append(argv)
        return 0, "4242\n", ""

    monkeypatch.setattr(linux, "run_cmd", fake_run)
    monkeypatch.setattr(linux, "proc_name", lambda pid: "alacritty")
    assert await p.frontmost_app() == "alacritty"
    assert seen == [["xdotool", "getactivewindow", "getwindowpid"]]
    monkeypatch.setattr(p, "_x11", lambda: False)
    assert await p.frontmost_app() is None  # Wayland or no xdotool


async def test_linux_raise_pids_uses_xdotool_per_ancestor(monkeypatch):
    p = linux.LinuxPlatform()
    monkeypatch.setattr(p, "_x11", lambda: True)
    seen = []

    async def fake_run(argv, timeout=5, input=None):
        seen.append(argv[-2])
        return (0 if argv[-2] == "300" else 1), "", ""

    monkeypatch.setattr(linux, "run_cmd", fake_run)
    assert await p.raise_pids([200, 300, 400], {})
    assert seen == ["200", "300"]
    monkeypatch.setattr(p, "_x11", lambda: False)
    assert not await p.raise_pids([200], {})


def test_linux_editor_discovery_and_raise(monkeypatch, tmp_path):
    code, codium = tmp_path / "code", tmp_path / "codium"
    for f in (code, codium):
        f.write_text("#!/bin/sh\n")
        f.chmod(0o755)
    monkeypatch.setattr("shutil.which", lambda n: str(code) if n == "code" else str(codium) if n == "codium" else None)
    monkeypatch.setattr(linux, "EDITOR_PATHS", ())
    p = linux.LinuxPlatform()
    assert p.editor_clis() == [str(code), str(codium)]
    assert p.editor_raise_argv("VSCodium", None, ["/srv/proj"]) == [str(codium), "/srv/proj"]
    assert p.editor_raise_argv("Visual Studio Code", None, ["/srv/proj"]) == [str(code), "/srv/proj"]
    assert p.editor_raise_argv("Visual Studio Code", None, []) is None


async def test_process_table_warns_when_proc_is_unreadable(monkeypatch, caplog):
    import logging

    monkeypatch.setattr(linux, "read_proc_table", lambda root="/proc": {})
    monkeypatch.setattr("neondeck.platform.base._last_procs_warn", 0.0)
    with caplog.at_level(logging.WARNING, logger="neondeck.platform"):
        assert await linux.LinuxPlatform().process_table() == {}
    assert any("Tabella dei processi" in r.getMessage() for r in caplog.records)


def test_linux_never_imports_iterm2():
    """The iterm2 package is a macOS-only dependency: building the Linux backends must not import it."""
    code = (
        "import sys\n"
        "sys.modules['iterm2'] = None  # any import of iterm2 now raises ImportError\n"
        "import neondeck.cli, neondeck.server\n"
        "from neondeck.platform.linux import LinuxPlatform\n"
        "from neondeck.terminals import make_backends\n"
        "names = [b.name for b in make_backends(LinuxPlatform(), {}, lambda: None)]\n"
        "assert names == ['tmux', 'wezterm', 'kitty'], names\n"
        "print('ok')\n")
    res = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert res.returncode == 0 and res.stdout.strip() == "ok", res.stderr


def test_macos_backends_start_with_iterm(monkeypatch):
    pytest.importorskip("iterm2")  # only installed on macOS
    from neondeck.platform.macos import MacPlatform
    from neondeck.terminals import make_backends

    assert [b.name for b in make_backends(MacPlatform(), {"tmux_socket": "/x"}, lambda: None)] == \
        ["iterm", "tmux", "wezterm", "kitty"]
