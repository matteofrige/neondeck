"""Linux: /proc, xdotool (X11, best effort), ip, systemd --user, xdg-open."""
from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
from pathlib import Path

from ..config import env_home
from .base import Platform, Service, cli_path, run_cmd, warn_procs
from .linuxnet import LinuxProbe, diagnose_linux

UNIT_NAME = "neondeck.service"
EDITOR_NAMES = ("code", "code-insiders", "codium", "cursor")
EDITOR_PATHS = ("/usr/share/code/bin/code", "/snap/bin/code")


# ---------------------------------------------------------------- /proc parsers

def parse_proc_stat(text: str) -> tuple[int, int] | None:
    """/proc/<pid>/stat -> (ppid, tty_nr). comm may contain spaces and parentheses: split after the last ')'."""
    end = text.rfind(")")
    if end < 0:
        return None
    fields = text[end + 1:].split()  # state ppid pgrp session tty_nr ...
    try:
        return int(fields[1]), int(fields[4])
    except (IndexError, ValueError):
        return None


def tty_from_nr(nr: int) -> str | None:
    """Decode the tty_nr device number of /proc/<pid>/stat to a device path (None when no controlling tty)."""
    if nr <= 0:
        return None
    major = (nr >> 8) & 0xFFF
    minor = (nr & 0xFF) | ((nr >> 12) & 0xFFF00)
    if 136 <= major <= 143:
        return f"/dev/pts/{(major - 136) * 256 + minor}"
    if major == 4:
        return f"/dev/tty{minor}" if minor < 64 else f"/dev/ttyS{minor - 64}"
    return None


def read_proc_table(root: str = "/proc") -> dict[int, tuple[int, str, str]]:
    """pid -> (ppid, tty or "?", exe path or comm)."""
    table: dict[int, tuple[int, str, str]] = {}
    try:
        names = os.listdir(root)
    except OSError:
        return table
    for name in names:
        if not name.isdigit():
            continue
        base = f"{root}/{name}"
        try:
            with open(f"{base}/stat", encoding="utf-8", errors="replace") as f:
                parsed = parse_proc_stat(f.read())
            if parsed is None:
                continue
            with open(f"{base}/comm", encoding="utf-8", errors="replace") as f:
                comm = f.read().strip()
        except OSError:
            continue  # the process exited meanwhile
        try:
            exe = os.readlink(f"{base}/exe").removesuffix(" (deleted)")
        except OSError:  # kernel thread or another user's process
            exe = ""
        table[int(name)] = (parsed[0], tty_from_nr(parsed[1]) or "?", exe or comm)
    return table


def proc_name(pid: int, root: str = "/proc") -> str | None:
    """Short name of a process: basename of its exe, else its comm."""
    try:
        return os.path.basename(os.readlink(f"{root}/{pid}/exe").removesuffix(" (deleted)"))
    except OSError:
        pass
    try:
        with open(f"{root}/{pid}/comm", encoding="utf-8", errors="replace") as f:
            return f.read().strip() or None
    except OSError:
        return None


# ---------------------------------------------------------------- systemd

def _quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'


def render_unit(exec_path: str, env: dict[str, str]) -> str:
    lines = ["[Unit]", "Description=Neondeck agent", "", "[Service]", "Type=simple",
             f"ExecStart={_quote(exec_path)} run", "Restart=on-failure", "RestartSec=3"]
    lines += [f"Environment={_quote(f'{k}={v}')}" for k, v in env.items()]
    lines += ["", "[Install]", "WantedBy=default.target", ""]
    return "\n".join(lines)


def unit_env() -> dict[str, str]:
    env = {"PATH": f"{Path.home()}/.local/bin:/usr/local/bin:/usr/bin:/bin:/usr/local/sbin:/usr/sbin:/sbin"}
    if env_home():
        env["NEONDECK_HOME"] = env_home()
    return env


def systemd_unit_path() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "systemd" / "user" / UNIT_NAME


def systemctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True)


def systemd_available() -> bool:
    if not shutil.which("systemctl"):
        return False
    try:
        return systemctl("show-environment").returncode == 0
    except OSError:
        return False


def parse_show(text: str) -> dict[str, str]:
    return dict(l.partition("=")[::2] for l in text.splitlines() if "=" in l)


class SystemdService(Service):
    kind = "servizio systemd utente"

    def installed(self) -> bool:
        return systemd_unit_path().exists()

    def install(self) -> int:
        path = systemd_unit_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render_unit(cli_path(), unit_env()), encoding="utf-8")
        print(f"Unit systemd scritta: {path}")
        if not systemd_available():
            print("systemd utente non disponibile (container o sessione senza `systemctl --user`).\n"
                  "Avvia l'agent a mano con `neondeck run` (ad esempio dentro tmux o con nohup).",
                  file=sys.stderr)
            return 0
        for args in (("daemon-reload",), ("enable", "--now", UNIT_NAME)):
            res = systemctl(*args)
            if res.returncode != 0:
                print(f"systemctl --user {' '.join(args)} non riuscito: {res.stderr.strip()}", file=sys.stderr)
                return 1
        print("Servizio systemd utente attivato: neondeck.service")
        print(f"Per farlo partire anche senza una sessione di login: sudo loginctl enable-linger {os.environ.get('USER', '$USER')}")
        self.started = True
        return 0

    def uninstall(self) -> None:
        if systemd_available():
            systemctl("disable", "--now", UNIT_NAME)
        systemd_unit_path().unlink(missing_ok=True)
        if systemd_available():
            systemctl("daemon-reload")
        print("Servizio systemd rimosso.")

    def restart(self) -> int:
        if not systemd_available():
            print("systemd utente non disponibile: riavvia a mano `neondeck run`.", file=sys.stderr)
            return 1
        res = systemctl("restart", UNIT_NAME)
        if res.returncode != 0:
            print(f"Riavvio non riuscito: {res.stderr.strip()} (hai eseguito `neondeck install`?)", file=sys.stderr)
            return 1
        print("Agent riavviato.")
        return 0

    def status_lines(self) -> list[str]:
        path = systemd_unit_path()
        lines = [f"Servizio systemd installato: {'sì' if path.exists() else 'no'} ({path})"]
        if not systemd_available():
            lines.append("systemd: utente non disponibile (esegui `neondeck run` a mano)")
            return lines
        active = systemctl("is-active", UNIT_NAME).stdout.strip() or "sconosciuto"
        pid = parse_show(systemctl("show", "-p", "MainPID", UNIT_NAME).stdout).get("MainPID", "0")
        lines.append(f"systemd: {'attivo' if active == 'active' else active}" + (f" (PID {pid})" if pid not in ("", "0") else ""))
        return lines

    def log_hint(self) -> str:
        return "Log: journalctl --user -u neondeck -f"


# ---------------------------------------------------------------- platform

class LinuxPlatform(Platform):
    name = "linux"

    async def process_table(self) -> dict[int, tuple[int, str, str]]:
        table = await asyncio.to_thread(read_proc_table)
        if not table:
            warn_procs("/proc non leggibile")
        return table

    def _x11(self) -> bool:
        return (bool(os.environ.get("DISPLAY")) and os.environ.get("XDG_SESSION_TYPE") != "wayland"
                and shutil.which("xdotool") is not None)

    async def notify(self, title: str, text: str) -> None:
        if shutil.which("notify-send"):
            await run_cmd(["notify-send", title, text], 5)

    def front_supported(self) -> bool:
        return self._x11()

    async def frontmost_app(self) -> str | None:
        if not self._x11():
            return None
        rc, out, _ = await run_cmd(["xdotool", "getactivewindow", "getwindowpid"], 2)
        if rc != 0 or not out.strip().isdigit():
            return None
        return await asyncio.to_thread(proc_name, int(out.strip()))

    async def raise_pids(self, pids: list[int], table: dict) -> bool:
        if not self._x11():
            return False  # Wayland: no portable way, skip silently
        for pid in pids:
            rc, _, _ = await run_cmd(["xdotool", "search", "--onlyvisible", "--pid", str(pid), "windowactivate"], 3)
            if rc == 0:
                return True
        return False

    def network_probe(self):
        return LinuxProbe()

    def diagnose_network(self) -> tuple[dict | None, str | None]:
        return diagnose_linux()

    def config_dir(self) -> Path:
        base = os.environ.get("XDG_CONFIG_HOME")
        return (Path(base).expanduser() if base and os.path.isabs(base) else Path.home() / ".config") / "neondeck"

    def shell_argv(self, command: str) -> list[str]:
        if os.path.exists("/bin/bash"):
            return ["/bin/bash", "-lc", command]
        return ["/bin/sh", "-c", command]

    def open_argv(self, url: str) -> list[str] | None:
        return ["xdg-open", url] if shutil.which("xdg-open") else None

    def editor_clis(self) -> list[str]:
        found: dict[str, str] = {}
        for c in [shutil.which(n) for n in EDITOR_NAMES] + list(EDITOR_PATHS):
            if c and os.path.isfile(c) and os.access(c, os.X_OK):
                found.setdefault(os.path.realpath(c), c)
        return list(found.values())

    def editor_raise_argv(self, app_name: str, app_path: str | None, folders: list[str]) -> list[str] | None:
        if not folders:
            return None
        low = app_name.lower()
        want = ("code-insiders" if "insiders" in low else "codium" if "codium" in low
                else "cursor" if "cursor" in low else "code")
        clis = self.editor_clis()
        cli = next((c for c in clis if os.path.basename(c) == want), None) or (clis[0] if clis else None)
        return [cli, folders[0]] if cli else None

    def service(self) -> Service:
        return SystemdService()
