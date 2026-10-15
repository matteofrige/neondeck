"""Contract of the OS-specific layer (macos.py and linux.py implement it) and helpers shared by both."""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import sys
import time
from pathlib import Path

log = logging.getLogger("neondeck.platform")
PROCS_WARN_EVERY = 300  # seconds between repeated process-table warnings
_last_procs_warn = 0.0


class UnsupportedPlatform(RuntimeError):
    """Raised at startup on an OS the agent cannot run on."""


def warn_procs(reason: str) -> None:
    global _last_procs_warn
    now = time.monotonic()
    if _last_procs_warn and now - _last_procs_warn < PROCS_WARN_EVERY:
        return
    _last_procs_warn = now
    log.warning("Tabella dei processi non disponibile (%s): impossibile riconoscere i terminali, "
                "sessioni non comandabili. L'agent gira in una sandbox?", reason)


async def run_cmd(argv: list[str], timeout: float = 5.0, input: str | None = None) -> tuple[int, str, str]:
    """Run a command: (returncode, stdout, stderr). (-1, "", reason) when it cannot start or times out."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, stdin=asyncio.subprocess.PIPE if input is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    except OSError as exc:
        return -1, "", str(exc)
    try:
        out, err = await asyncio.wait_for(proc.communicate(input.encode() if input is not None else None), timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
            await proc.wait()
        except ProcessLookupError:
            pass
        return -1, "", "timeout"
    return proc.returncode, out.decode(errors="replace"), err.decode(errors="replace")


def outer_app(comm: str) -> str | None:
    """'/Applications/Foo.app/Contents/MacOS/foo' -> '/Applications/Foo.app'."""
    i = comm.find(".app")
    return comm[:i + 4] if i > 0 else None


def cli_path() -> str:
    found = shutil.which("neondeck")
    return str(Path(found or sys.argv[0]).resolve())


def ancestors_of(pid: int, table: dict, limit: int = 64) -> list[int]:
    """Parent chain of pid (nearest first, pid itself excluded)."""
    out: list[int] = []
    cur = table[pid][0] if pid in table else 0
    while cur > 1 and cur not in out and len(out) < limit and cur in table:
        out.append(cur)
        cur = table[cur][0]
    return out


class Service:
    """Background service manager of the agent (launchd or systemd --user)."""

    kind = ""
    started = False  # True once install() has started the agent

    def installed(self) -> bool:
        raise NotImplementedError

    def install(self) -> int:
        raise NotImplementedError

    def uninstall(self) -> None:
        raise NotImplementedError

    def restart(self) -> int:
        raise NotImplementedError

    def status_lines(self) -> list[str]:
        raise NotImplementedError

    def log_hint(self) -> str:
        raise NotImplementedError


class Platform:
    name = ""

    # --- processes and windows
    async def process_table(self) -> dict[int, tuple[int, str, str]]:
        """pid -> (ppid, tty, comm/exe). Empty (with a warning) when unreadable."""
        raise NotImplementedError

    async def frontmost_app(self) -> str | None:
        raise NotImplementedError

    def front_supported(self) -> bool:
        """False when the frontmost app cannot be determined at all (focus then relies on the terminals)."""
        return False

    async def raise_pids(self, pids: list[int], table: dict) -> bool:
        """Best effort: bring the window of the first of these processes to the front."""
        return False

    async def raise_app(self, name: str) -> bool:
        return False

    # --- notifications
    async def notify(self, title: str, text: str) -> None:
        """Best-effort desktop notification; never raises."""

    # --- network
    def network_probe(self):
        raise NotImplementedError

    def diagnose_network(self) -> tuple[dict | None, str | None]:
        raise NotImplementedError

    # --- environment
    def config_dir(self) -> Path:
        return Path.home() / ".config" / "neondeck"

    def shell_argv(self, command: str) -> list[str]:
        raise NotImplementedError

    def open_argv(self, url: str) -> list[str] | None:
        raise NotImplementedError

    def editor_clis(self) -> list[str]:
        raise NotImplementedError

    def editor_raise_argv(self, app_name: str, app_path: str | None, folders: list[str]) -> list[str] | None:
        raise NotImplementedError

    def service(self) -> Service:
        raise NotImplementedError
