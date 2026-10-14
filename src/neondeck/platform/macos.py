"""macOS: ps, lsappinfo, scutil, launchd, open."""
from __future__ import annotations

import asyncio
import os
import plistlib
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from ..config import env_home
from .base import Platform, Service, cli_path, outer_app, run_cmd, warn_procs

LABEL = "io.neondeck.agent"
PLIST = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
LOG = Path.home() / "Library" / "Logs" / "neondeck.log"
# Pre-rename (Claude Deck) identifiers, cleaned up by install/uninstall
LEGACY_LABEL = "com.hooxlab.claude-deck"
LEGACY_PLIST = Path.home() / "Library" / "LaunchAgents" / f"{LEGACY_LABEL}.plist"
DOMAIN = f"gui/{os.getuid()}"
EDITOR_BUNDLES = (
    "/Applications/Visual Studio Code.app/Contents/Resources/app/bin/code",
    "/Applications/Cursor.app/Contents/Resources/app/bin/cursor",
)
CLI_NAMES = ("code", "cursor")


def launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def build_plist() -> dict:
    env = {"PATH": f"{Path.home()}/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"}
    if env_home():
        env["NEONDECK_HOME"] = env_home()
    return {
        "Label": LABEL,
        "ProgramArguments": [cli_path(), "run"],
        "RunAtLoad": True,
        "KeepAlive": True,
        "ProcessType": "Interactive",
        "EnvironmentVariables": env,
        "StandardOutPath": str(LOG),
        "StandardErrorPath": str(LOG),
    }


def wait_unloaded(target: str, timeout: float = 5.0) -> None:
    """bootout returns before launchd has torn the job down; bootstrap meanwhile fails with EIO (5)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and launchctl("print", target).returncode == 0:
        time.sleep(0.2)


def remove_legacy_agent() -> None:
    res = launchctl("bootout", f"{DOMAIN}/{LEGACY_LABEL}")
    wait_unloaded(f"{DOMAIN}/{LEGACY_LABEL}")
    if LEGACY_PLIST.exists():
        LEGACY_PLIST.unlink()
        print(f"Rimosso il vecchio LaunchAgent {LEGACY_PLIST}")
    elif res.returncode == 0:
        print(f"Scaricato il vecchio LaunchAgent {LEGACY_LABEL}")


class MacService(Service):
    kind = "LaunchAgent"

    def installed(self) -> bool:
        return PLIST.exists()

    def install(self) -> int:
        PLIST.parent.mkdir(parents=True, exist_ok=True)
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with PLIST.open("wb") as f:
            plistlib.dump(build_plist(), f)
        remove_legacy_agent()  # frees the port the old agent is holding
        launchctl("bootout", f"{DOMAIN}/{LABEL}")  # fails when not loaded: ignore
        wait_unloaded(f"{DOMAIN}/{LABEL}")
        res = launchctl("bootstrap", DOMAIN, str(PLIST))
        if res.returncode != 0 and "5:" in res.stderr:  # still tearing down: one more try
            time.sleep(1.5)
            res = launchctl("bootstrap", DOMAIN, str(PLIST))
        if res.returncode != 0:
            print(f"launchctl bootstrap non riuscito: {res.stderr.strip()}", file=sys.stderr)
            return 1
        print(f"LaunchAgent installato: {PLIST}")
        self.started = True
        return 0

    def uninstall(self) -> None:
        launchctl("bootout", f"{DOMAIN}/{LABEL}")
        PLIST.unlink(missing_ok=True)
        print("LaunchAgent rimosso.")
        remove_legacy_agent()

    def restart(self) -> int:
        res = launchctl("kickstart", "-k", f"{DOMAIN}/{LABEL}")
        if res.returncode != 0:
            print(f"Riavvio non riuscito: {res.stderr.strip()} (hai eseguito `neondeck install`?)",
                  file=sys.stderr)
            return 1
        print("Agent riavviato.")
        return 0

    def status_lines(self) -> list[str]:
        lines = []
        if LEGACY_PLIST.exists():
            lines.append(f"Attenzione: esiste ancora il vecchio LaunchAgent {LEGACY_PLIST} (esegui `neondeck install`)")
        lines.append(f"LaunchAgent installato: {'sì' if PLIST.exists() else 'no'} ({PLIST})")
        res = launchctl("print", f"{DOMAIN}/{LABEL}")
        if res.returncode == 0:
            extra = [l.strip() for l in res.stdout.splitlines() if l.strip().startswith(("state =", "pid ="))]
            lines.append("launchd: caricato" + "".join(f"\n  {l}" for l in extra))
        else:
            lines.append("launchd: non caricato")
        return lines

    def log_hint(self) -> str:
        return f"Log: {LOG}"


class MacPlatform(Platform):
    name = "darwin"

    async def process_table(self) -> dict[int, tuple[int, str, str]]:
        table: dict[int, tuple[int, str, str]] = {}
        try:
            proc = await asyncio.create_subprocess_exec(
                "ps", "-axo", "pid=,ppid=,tty=,comm=",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            out, err = await proc.communicate()
        except OSError as exc:
            warn_procs(str(exc))
            return table
        if proc.returncode != 0:
            warn_procs(err.decode(errors="replace").strip() or f"codice {proc.returncode}")
            return table
        for line in out.decode(errors="replace").splitlines():
            parts = line.split(None, 3)  # comm may contain spaces
            if len(parts) == 4 and parts[0].isdigit() and parts[1].isdigit():
                table[int(parts[0])] = (int(parts[1]), parts[2], parts[3])
        if not table:
            warn_procs("nessun processo restituito")
        return table

    async def frontmost_app(self) -> str | None:
        try:
            proc = await asyncio.create_subprocess_exec(
                "/bin/sh", "-c", 'lsappinfo info -only name "$(lsappinfo front)"',
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            out, _ = await asyncio.wait_for(proc.communicate(), 3)
        except (OSError, asyncio.TimeoutError):
            return None
        line = out.decode(errors="replace").splitlines()[:1]
        quoted = re.findall(r'"([^"]*)"', line[0]) if line else []
        return quoted[-1] if quoted else None  # `"iTerm2" ASN:...` or `"LSDisplayName"="iTerm2"`

    def front_supported(self) -> bool:
        return True

    async def raise_pids(self, pids: list[int], table: dict) -> bool:
        for pid in pids:
            app = outer_app(table[pid][2]) if pid in table else None
            if app:
                rc, _, _ = await run_cmd(["open", "-a", app], 5)
                return rc == 0
        return False

    async def raise_app(self, name: str) -> bool:
        rc, _, _ = await run_cmd(["open", "-a", name], 5)
        return rc == 0

    def network_probe(self):
        from ..netguard import NetworkProbe
        return NetworkProbe()

    def diagnose_network(self) -> tuple[dict | None, str | None]:
        from ..netguard import diagnose_network
        return diagnose_network()

    def shell_argv(self, command: str) -> list[str]:
        return ["/bin/zsh", "-lc", command]

    def open_argv(self, url: str) -> list[str] | None:
        return ["open", url]

    def editor_clis(self) -> list[str]:
        found: dict[str, str] = {}
        for c in [shutil.which(n) for n in CLI_NAMES] + list(EDITOR_BUNDLES):
            if c and os.path.isfile(c) and os.access(c, os.X_OK):
                found.setdefault(os.path.realpath(c), c)
        return list(found.values())

    def editor_raise_argv(self, app_name: str, app_path: str | None, folders: list[str]) -> list[str] | None:
        if not app_path:
            return None
        cli = None
        for name in CLI_NAMES:
            cand = Path(app_path) / "Contents" / "Resources" / "app" / "bin" / name
            if cand.is_file() and os.access(cand, os.X_OK):
                cli = str(cand)
                break
        return [cli, folders[0]] if cli and folders else ["open", "-a", app_path]

    def service(self) -> Service:
        return MacService()
