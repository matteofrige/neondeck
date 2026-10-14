"""iTerm2 backend: adapter around ItermClient (the iterm2 package is imported only on macOS)."""
from __future__ import annotations

from typing import Callable

from .base import Backend, loc_of, report


class ItermBackend(Backend):
    name = "iterm"
    label = "iTerm2"

    def __init__(self, on_change: Callable[[], None] | None = None) -> None:
        from ..iterm import ItermClient

        self.client = ItermClient(on_change)

    def available(self) -> bool:
        return self.client.connected

    def start(self) -> None:
        self.client.start()

    async def stop(self) -> None:
        await self.client.stop()

    def want(self, ttl: float) -> None:
        self.client.want(ttl)

    async def wait_ready(self, timeout: float) -> bool:
        return await self.client.wait_connected(timeout)

    async def snapshot(self, details: bool = True) -> list[dict]:
        _, _, sessions = await self.client.snapshot(details=details)
        return [{
            "backend": self.name, "pane_id": it["session_id"], "tty": it["tty"], "title": it["title"],
            "job": it["job"], "cwd": it["path"], "loc": loc_of(it), "active": it["current"],
            "apps": ["iTerm2"], "window": it["window"], "tab": it["tab"], "pane": it["pane"],
            "panes": it["panes"],
        } for it in sessions]

    async def focus(self, pane_id: str) -> None:
        await self.client.focus(session_id=pane_id)

    async def focus_tty(self, tty: str) -> None:
        await self.client.focus(tty if tty.startswith("/dev/") else "/dev/" + tty)

    async def send_text(self, pane_id: str, text: str) -> None:
        await self.client.send_text(text, session_id=pane_id)

    async def report(self) -> dict:
        ok = self.client.connected
        return report(self, ok, "iTerm2: " + ("connesso" if ok else "non connesso (API Python disattivata o app chiusa)"))
