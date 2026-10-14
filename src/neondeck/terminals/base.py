"""Terminal backend interface. A backend lists its panes and can focus a pane or type into it.

A pane is a dict with: backend, pane_id (str, unique within the backend), tty (device path or None),
pid (shell pid, only when there is no tty), title, job, cwd, loc (label), active (focused inside its
terminal app), apps (names of the hosting app, matched against the frontmost app), window / tab / pane
(1-based, for ordering) and panes (panes in the same tab).
"""
from __future__ import annotations

from ..platform.base import run_cmd  # noqa: F401  (the default command runner of the backends)

BACKEND_ORDER = ("iterm", "tmux", "wezterm", "kitty")


class Backend:
    name = ""
    label = ""

    def available(self) -> bool:
        """Cheap and synchronous: no subprocess unless the backend caches its answer."""
        return False

    async def probe(self) -> bool:
        """Refresh availability (may spawn a rate-limited subprocess)."""
        return self.available()

    def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    def want(self, ttl: float) -> None:
        """Hint that someone needs fresh data for `ttl` seconds (only iTerm2 keeps a connection)."""

    async def wait_ready(self, timeout: float) -> bool:
        return self.available()

    async def snapshot(self, details: bool = True) -> list[dict]:
        raise NotImplementedError

    async def focus(self, pane_id: str) -> None:
        raise NotImplementedError

    async def send_text(self, pane_id: str, text: str) -> None:
        raise NotImplementedError

    async def report(self) -> dict:
        """{"name", "label", "available", "text"} for `neondeck status`."""
        raise NotImplementedError


def loc_of(it: dict) -> str:
    """'F1 · T2' or 'F1 · T2 · P3' (pane only when the tab is split)."""
    loc = f"F{it['window']} · T{it['tab']}"
    return f"{loc} · P{it['pane']}" if it.get("panes", 1) > 1 else loc


def ordinals(ids) -> dict:
    """id -> 1-based rank, in sorted order."""
    return {i: n for n, i in enumerate(sorted(set(ids)), 1)}


def report(backend: Backend, available: bool, text: str) -> dict:
    return {"name": backend.name, "label": backend.label, "available": available, "text": text}
