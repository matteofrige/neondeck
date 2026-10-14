"""WezTerm backend (macOS and Linux) through `wezterm cli`."""
from __future__ import annotations

import json
import logging
import shutil
import time
from typing import Awaitable, Callable
from urllib.parse import unquote, urlparse

from .. import platform
from ..errors import ControlError
from .base import Backend, ordinals, report, run_cmd

log = logging.getLogger("neondeck.wezterm")
PROBE_TIMEOUT = 1.5
PROBE_EVERY = 30.0  # seconds between availability probes while WezTerm is unreachable

Runner = Callable[..., Awaitable[tuple[int, str, str]]]


class WeztermError(ControlError):
    pass


def _json(text: str):
    try:
        return json.loads(text)
    except ValueError:
        return None


def cwd_path(cwd: str | None) -> str | None:
    """WezTerm reports the cwd as a file:// URL."""
    if not cwd:
        return None
    return unquote(urlparse(cwd).path) if cwd.startswith("file://") else cwd


def parse_panes(raw, focused: set[int] | None = None) -> list[dict]:
    """`wezterm cli list --format json` -> normalized panes. `focused` = pane ids that GUI clients focus."""
    if not isinstance(raw, list):
        return []
    rows = [r for r in raw if isinstance(r, dict) and isinstance(r.get("pane_id"), int)]
    wins = ordinals(r.get("window_id", 0) for r in rows)
    tabs = ordinals((r.get("window_id", 0), r.get("tab_id", 0)) for r in rows)
    in_tab: dict[tuple, list[int]] = {}
    for r in rows:
        in_tab.setdefault((r.get("window_id", 0), r.get("tab_id", 0)), []).append(r["pane_id"])
    panes = []
    for r in rows:
        key = (r.get("window_id", 0), r.get("tab_id", 0))
        win = wins[r.get("window_id", 0)]
        tab = 1 + sorted(t for w, t in tabs if w == key[0]).index(key[1])
        idx = 1 + sorted(in_tab[key]).index(r["pane_id"])
        title = r.get("title") or None
        panes.append({
            "backend": "wezterm", "pane_id": str(r["pane_id"]), "tty": r.get("tty_name") or None,
            "title": title, "job": None, "cwd": cwd_path(r.get("cwd")),
            "loc": f"wezterm {win}:{tab}.{idx}",
            "active": bool(focused) and r["pane_id"] in focused,
            "apps": ["wezterm"], "window": win, "tab": tab, "pane": idx, "panes": len(in_tab[key]),
        })
    return panes


def parse_focused(raw) -> set[int]:
    """`wezterm cli list-clients --format json` -> focused pane ids."""
    if not isinstance(raw, list):
        return set()
    return {c["focused_pane_id"] for c in raw if isinstance(c, dict) and isinstance(c.get("focused_pane_id"), int)}


def parse_client_pids(raw) -> list[int]:
    if not isinstance(raw, list):
        return []
    return [c["pid"] for c in raw if isinstance(c, dict) and isinstance(c.get("pid"), int)]


class WeztermBackend(Backend):
    name = "wezterm"
    label = "WezTerm"

    def __init__(self, run: Runner = run_cmd, plat=None, which=shutil.which,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._run, self._plat, self._which, self._clock = run, plat, which, clock
        self._ok = False
        self._probed_at = -PROBE_EVERY
        self.reason = "non trovato"

    @property
    def plat(self):
        return self._plat or platform.current()

    def available(self) -> bool:
        return self._ok

    async def _list(self, timeout: float) -> list[dict] | None:
        rc, out, _ = await self._run(["wezterm", "cli", "list", "--format", "json"], timeout)
        data = _json(out) if rc == 0 else None
        return data if isinstance(data, list) else None

    async def probe(self, force: bool = False) -> bool:
        if self._ok and not force:
            return True
        if not self._which("wezterm"):
            self._ok, self.reason = False, "non trovato"
            return False
        now = self._clock()
        if not force and now - self._probed_at < PROBE_EVERY:
            return False
        self._probed_at = now
        self._ok = await self._list(PROBE_TIMEOUT) is not None
        self.reason = "" if self._ok else "non in esecuzione"
        return self._ok

    async def snapshot(self, details: bool = True) -> list[dict]:
        raw = await self._list(PROBE_TIMEOUT * 2)
        if raw is None:
            self._ok, self._probed_at = False, self._clock()
            return []
        rc, out, _ = await self._run(["wezterm", "cli", "list-clients", "--format", "json"], PROBE_TIMEOUT)
        focused = parse_focused(_json(out)) if rc == 0 else set()
        return parse_panes(raw, focused)

    async def focus(self, pane_id: str) -> None:
        rc, _, err = await self._run(["wezterm", "cli", "activate-pane", "--pane-id", pane_id], PROBE_TIMEOUT * 2)
        if rc != 0:
            raise WeztermError(err.strip().splitlines()[-1] if err.strip() else "WezTerm non risponde")
        rc, out, _ = await self._run(["wezterm", "cli", "list-clients", "--format", "json"], PROBE_TIMEOUT)
        pids = parse_client_pids(_json(out)) if rc == 0 else []
        if pids and await self.plat.raise_pids(pids, await self.plat.process_table()):
            return
        await self.plat.raise_app("WezTerm")

    async def send_text(self, pane_id: str, text: str) -> None:
        if not text:
            return
        rc, _, err = await self._run(
            ["wezterm", "cli", "send-text", "--no-paste", "--pane-id", pane_id, "--", text], PROBE_TIMEOUT * 2)
        if rc != 0:
            raise WeztermError(err.strip().splitlines()[-1] if err.strip() else "Invio a WezTerm fallito")

    async def report(self) -> dict:
        if not self._which("wezterm"):
            return report(self, False, "WezTerm: non trovato")
        raw = await self._list(PROBE_TIMEOUT)
        if raw is None:
            return report(self, False, "WezTerm: non in esecuzione")
        wins = len({r.get("window_id") for r in raw if isinstance(r, dict)})
        return report(self, True, f"WezTerm: attivo ({wins} {'finestra' if wins == 1 else 'finestre'}, "
                                  f"{len(raw)} {'pannello' if len(raw) == 1 else 'pannelli'})")
