"""Configuration and data directory handling."""
from __future__ import annotations

import json
import logging
import os
import secrets
import shutil
import socket
from pathlib import Path

log = logging.getLogger("neondeck.config")

DEFAULT_PORT = 8787


def env_home() -> str | None:
    """NEONDECK_HOME, falling back to the pre-rename CLAUDE_DECK_HOME."""
    return os.environ.get("NEONDECK_HOME") or os.environ.get("CLAUDE_DECK_HOME") or None


def migrate_legacy(new: Path, old: Path) -> bool:
    """Copy the pre-rename data dir (config, buttons, icons) once; the old one stays in place."""
    if new.exists() or not old.is_dir():
        return False
    new.mkdir(parents=True)
    os.chmod(new, 0o700)
    for name in ("config.json", "buttons.json"):
        if (old / name).is_file():
            shutil.copy2(old / name, new / name)
    if (new / "config.json").is_file():
        os.chmod(new / "config.json", 0o600)
    if (old / "icons").is_dir():
        shutil.copytree(old / "icons", new / "icons", copy_function=shutil.copy2)
    log.warning("Migrated settings from %s to %s (the old directory was left in place)", old, new)
    return True


def home_dir() -> Path:
    env = env_home()
    if env:
        base = Path(env).expanduser()
    else:
        from . import platform

        base = platform.current().config_dir()
        try:
            migrate_legacy(base, base.parent / "claude-deck")
        except OSError as exc:
            log.warning("Migration from the old settings directory failed: %s", exc)
    base.mkdir(parents=True, exist_ok=True)
    (base / "icons").mkdir(exist_ok=True)
    try:
        os.chmod(base, 0o700)  # holds the token and the bridge socket
    except OSError:
        pass
    return base


def atomic_write_json(path: Path, data) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def load_config(home: Path) -> dict:
    path = home / "config.json"
    cfg: dict = {}
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(cfg, dict):
            cfg = {}
    except (OSError, ValueError):
        cfg = {}
    changed = False
    if not cfg.get("token"):
        cfg["token"] = secrets.token_urlsafe(24)
        changed = True
    if not cfg.get("admin_key"):
        cfg["admin_key"] = secrets.token_urlsafe(24)
        changed = True
    if not isinstance(cfg.get("trusted_networks"), list):
        cfg["trusted_networks"] = []
        changed = True
    if not isinstance(cfg.get("port"), int):
        cfg["port"] = DEFAULT_PORT
        changed = True
    if changed or not path.exists():
        atomic_write_json(path, cfg)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    return cfg


def config_mtime(home: Path) -> int:
    try:
        return (home / "config.json").stat().st_mtime_ns
    except OSError:
        return 0


def save_config(home: Path, cfg: dict) -> None:
    path = home / "config.json"
    atomic_write_json(path, cfg)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def lan_ips() -> list[str]:
    ips: list[str] = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))  # no packet is sent
        ips.append(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127.") and ip not in ips:
                ips.append(ip)
    except OSError:
        pass
    return ips


def preview_url(cfg: dict, ips: list[str] | None = None) -> str:
    ips = lan_ips() if ips is None else ips
    host = ips[0] if ips else "127.0.0.1"
    return f"http://{host}:{cfg['port']}/preview?token={cfg['token']}"
