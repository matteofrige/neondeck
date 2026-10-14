"""kitty backend (macOS and Linux) through `kitty @` remote control."""
from __future__ import annotations

import glob
import json
import logging
import os
import shutil
import stat
import time
from typing import Awaitable, Callable

from .. import platform
from ..errors import ControlError
from .base import Backend, ordinals, report, run_cmd

log = logging.getLogger("neondeck.kitty")
PROBE_TIMEOUT = 1.5
PROBE_EVERY = 30.0
MAC_BINARY = "/Applications/kitty.app/Contents/MacOS/kitty"

Runner = Callable[..., Awaitable[tuple[int, str, str]]]


class KittyError(ControlError):
    pass


def _json(text: str):
    try:
        return json.loads(text)
    except ValueError:
        return None


def parse_panes(raw) -> list[dict]:
    """`kitty @ ls` -> normalized panes. kitty exposes no tty: the shell pid is used to match sessions."""
    if not isinstance(raw, list):
        return []
    os_ids = ordinals(o.get("id", 0) for o in raw if isinstance(o, dict))
    panes = []
    for osw in raw:
        if not isinstance(osw, dict):
            continue
        tabs = [t for t in osw.get("tabs") or [] if isinstance(t, dict)]
        for ti, tab in enumerate(tabs, 1):
            wins = [w for w in tab.get("windows") or [] if isinstance(w, dict) and isinstance(w.get("id"), int)]
            for wi, w in enumerate(wins, 1):
                fg = [p for p in w.get("foreground_processes") or [] if isinstance(p, dict)]
                cmd = (fg[-1].get("cmdline") or [""])[0] if fg else ""
                n = os_ids.get(osw.get("id", 0), 0)
                focused = (bool(w.get("is_focused")) and bool(tab.get("is_focused", True))
                           and bool(osw.get("is_focused", True)))
                panes.append({
                    "backend": "kitty", "pane_id": str(w["id"]), "tty": None,
                    "pid": w["pid"] if isinstance(w.get("pid"), int) else None,
                    "title": w.get("title") or None, "job": os.path.basename(cmd) or None,
                    "cwd": w.get("cwd") or None, "loc": f"kitty {n}:{ti}.{wi}", "active": focused,
                    "apps": ["kitty"], "window": n, "tab": ti, "pane": wi, "panes": len(wins),
                    "kitty_pid": next(iter(_kitty_pids(w)), None),
                })
    return panes


def _kitty_pids(w: dict) -> list[int]:
    env = w.get("env")
    pid = env.get("KITTY_PID") if isinstance(env, dict) else None
    return [int(pid)] if isinstance(pid, str) and pid.isdigit() else []


def socket_candidates(configured: str | None, env=None, tmp_dirs=("/tmp",)) -> list[list[str]]:
    """`--to` argument lists to try, most specific first. [] means "no --to" (kitty uses $KITTY_LISTEN_ON)."""
    env = os.environ if env is None else env
    if configured:
        return [["--to", configured if ":" in configured else f"unix:{configured}"]]
    out: list[list[str]] = []
    if env.get("KITTY_LISTEN_ON"):
        out.append([])
    for d in tmp_dirs:
        for path in sorted(glob.glob(os.path.join(d, "kitty*"))):
            try:
                if stat.S_ISSOCK(os.stat(path).st_mode):
                    out.append(["--to", f"unix:{path}"])
            except OSError:
                continue
    return out


class KittyBackend(Backend):
    name = "kitty"
    label = "kitty"

    def __init__(self, socket: str | None = None, run: Runner = run_cmd, plat=None, which=shutil.which,
                 clock: Callable[[], float] = time.monotonic, candidates=None) -> None:
        self.socket = socket
        self._run, self._plat, self._which, self._clock = run, plat, which, clock
        self._candidates = candidates or (lambda: socket_candidates(self.socket))
        self._to: list[str] | None = None  # the working --to arguments
        self._ok = False
        self._probed_at = -PROBE_EVERY
        self._pids: dict[str, int | None] = {}

    @property
    def plat(self):
        return self._plat or platform.current()

    def binary(self) -> str | None:
        found = self._which("kitty")
        if found:
            return found
        return MAC_BINARY if os.path.isfile(MAC_BINARY) and os.access(MAC_BINARY, os.X_OK) else None

    def _argv(self, *args: str) -> list[str]:
        return [self.binary() or "kitty", "@", *(self._to or []), *args]

    def available(self) -> bool:
        return self._ok

    async def _ls(self, timeout: float):
        """Run `kitty @ ls`; when no socket is known yet, try every candidate."""
        tries = [self._to] if self._to is not None else self._candidates() or [[]]
        for to in tries:
            rc, out, _ = await self._run([self.binary() or "kitty", "@", *to, "ls"], timeout)
            data = _json(out) if rc == 0 else None
            if isinstance(data, list):
                self._to = to
                return data
        return None

    async def probe(self, force: bool = False) -> bool:
        if self._ok and not force:
            return True
        if not self.binary():
            self._ok = False
            return False
        now = self._clock()
        if not force and now - self._probed_at < PROBE_EVERY:
            return False
        self._probed_at = now
        self._ok = await self._ls(PROBE_TIMEOUT) is not None
        return self._ok

    async def snapshot(self, details: bool = True) -> list[dict]:
        raw = await self._ls(PROBE_TIMEOUT * 2)
        if raw is None:
            self._ok, self._probed_at, self._to = False, self._clock(), None
            return []
        panes = parse_panes(raw)
        self._pids = {p["pane_id"]: p.pop("kitty_pid") for p in panes}
        return panes

    async def focus(self, pane_id: str) -> None:
        rc, _, err = await self._run(self._argv("focus-window", "--match", f"id:{pane_id}"), PROBE_TIMEOUT * 2)
        if rc != 0:
            raise KittyError(err.strip().splitlines()[-1] if err.strip() else "kitty non risponde")
        pid = self._pids.get(pane_id)
        if pid and await self.plat.raise_pids([pid], await self.plat.process_table()):
            return
        await self.plat.raise_app("kitty")

    async def send_text(self, pane_id: str, text: str) -> None:
        if not text:
            return
        # --stdin sends the text as is: argv text would be interpreted for escape sequences
        rc, _, err = await self._run(self._argv("send-text", "--match", f"id:{pane_id}", "--stdin"),
                                     PROBE_TIMEOUT * 2, text)
        if rc != 0:
            raise KittyError(err.strip().splitlines()[-1] if err.strip() else "Invio a kitty fallito")

    async def report(self) -> dict:
        if not self.binary():
            return report(self, False, "kitty: non trovato")
        raw = await self._ls(PROBE_TIMEOUT)
        if raw is None:
            return report(self, False, "kitty: remote control disattivato o non in esecuzione")
        panes = parse_panes(raw)
        wins = len({p["window"] for p in panes})
        return report(self, True, f"kitty: attivo ({wins} {'finestra' if wins == 1 else 'finestre'}, "
                                  f"{len(panes)} {'pannello' if len(panes) == 1 else 'pannelli'})")
