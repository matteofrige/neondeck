"""OS-specific pieces (process table, frontmost app, network detection, service, ...) behind one interface."""
from __future__ import annotations

import sys

from .base import Platform, Service, UnsupportedPlatform

_cache: dict[str, Platform] = {}


def current() -> Platform:
    """The Platform for sys.platform; raises UnsupportedPlatform elsewhere."""
    key = sys.platform
    if key not in _cache:
        if key == "darwin":
            from .macos import MacPlatform
            _cache[key] = MacPlatform()
        elif key.startswith("linux"):
            from .linux import LinuxPlatform
            _cache[key] = LinuxPlatform()
        else:
            raise UnsupportedPlatform(f"Piattaforma non supportata: {key} (Neondeck gira su macOS e Linux)")
    return _cache[key]


__all__ = ["Platform", "Service", "UnsupportedPlatform", "current"]
