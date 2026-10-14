"""Terminal backends: iTerm2 (macOS), tmux, WezTerm, kitty."""
from __future__ import annotations

import asyncio
from typing import Callable

from .base import BACKEND_ORDER, Backend


def make_backends(plat, config: dict, on_change: Callable[[], None]) -> list[Backend]:
    """Backends for this platform, in display order. They are cheap until they are polled."""
    from .kitty import KittyBackend
    from .tmux import TmuxBackend
    from .wezterm import WeztermBackend

    backends: list[Backend] = []
    if plat.name == "darwin":
        from .iterm import ItermBackend
        backends.append(ItermBackend(on_change))
    backends.append(TmuxBackend(socket=config.get("tmux_socket") or None))
    backends.append(WeztermBackend())
    backends.append(KittyBackend(socket=config.get("kitty_socket") or None))
    return backends


async def reports(backends: list[Backend]) -> list[dict]:
    """Fresh status line of every backend (for `neondeck status` and the admin info)."""
    res = await asyncio.gather(*(b.report() for b in backends), return_exceptions=True)
    return [r if isinstance(r, dict) else {"name": b.name, "label": b.label, "available": False,
                                           "text": f"{b.label}: errore ({r})"}
            for b, r in zip(backends, res)]


__all__ = ["BACKEND_ORDER", "Backend", "make_backends", "reports"]
