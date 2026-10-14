"""Button storage, validation and execution."""
from __future__ import annotations

import asyncio
import json
import re
import uuid
from pathlib import Path
from typing import Awaitable, Callable

from . import platform
from .config import atomic_write_json
from .ledfont import ICON_NAMES

COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
ICON_RE = re.compile(r"^icons/[A-Za-z0-9_-]+\.(png|jpg|jpeg|gif|webp|svg)$")
ACTION_TYPES = ("send_keys", "shell", "open_url", "focus_session")

# (title, LED glyph, keys)
DEFAULTS = [
    ("Esc", "esc", "\u001b"),
    ("Invio", "enter", "\r"),
    ("Interrompi", "stop", "\u0003"),
    ("Cambia modalità", "mode", "\u001b[Z"),
]
# Emoji the default buttons used before the LED glyphs: a button that still matches one of these is upgraded on load.
LEGACY_DEFAULTS = {("Esc", "⎋"): "esc", ("Invio", "↵"): "enter", ("Interrompi", "🛑"): "stop", ("Cambia modalità", "🔀"): "mode"}


class ValidationError(ValueError):
    pass


def validate_button(raw: dict, keep_id: str | None = None) -> dict:
    if not isinstance(raw, dict):
        raise ValidationError("Pulsante non valido")
    title = str(raw.get("title", "")).strip()
    if not title or len(title) > 40:
        raise ValidationError("Il titolo è obbligatorio (max 40 caratteri)")
    icon = raw.get("icon") or {}
    itype, ivalue = icon.get("type"), str(icon.get("value", "")).strip()
    if itype == "emoji":
        if len(ivalue) > 16:
            raise ValidationError("Icona emoji troppo lunga")
    elif itype == "glyph":
        if ivalue not in ICON_NAMES:
            raise ValidationError("Simbolo LED non valido")
    elif itype == "image":
        if not ICON_RE.match(ivalue):
            raise ValidationError("Percorso immagine non valido")
    else:
        raise ValidationError("Tipo di icona non valido")
    out = {
        "id": keep_id or str(raw.get("id") or uuid.uuid4()),
        "title": title,
        "icon": {"type": itype, "value": ivalue},
    }
    color = raw.get("color")
    if color:
        if not COLOR_RE.match(str(color)):
            raise ValidationError("Colore non valido (formato #rrggbb)")
        out["color"] = str(color).lower()

    act = raw.get("action")
    if not isinstance(act, dict) or act.get("type") not in ACTION_TYPES:
        raise ValidationError("Tipo di azione non valido")
    t = act["type"]
    if t == "send_keys":
        text = act.get("text")
        if not isinstance(text, str) or not text:
            raise ValidationError("Inserisci i tasti da inviare")
        target = act.get("target", "current")
        if target not in ("current", "selected"):
            raise ValidationError("Destinazione non valida")
        action = {"type": t, "text": text, "target": target}
    elif t == "shell":
        cmd = act.get("command")
        if not isinstance(cmd, str) or not cmd.strip():
            raise ValidationError("Inserisci il comando da eseguire")
        try:
            timeout = int(act.get("timeout", 10))
        except (TypeError, ValueError):
            raise ValidationError("Timeout non valido")
        if not 1 <= timeout <= 600:
            raise ValidationError("Il timeout deve essere tra 1 e 600 secondi")
        action = {"type": t, "command": cmd, "timeout": timeout}
    elif t == "open_url":
        url = str(act.get("url", "")).strip()
        if not re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", url):
            raise ValidationError("URL non valido (serve uno schema, es. https://)")
        action = {"type": t, "url": url}
    else:
        match = str(act.get("match", "")).strip()
        if not match:
            raise ValidationError("Inserisci il testo da cercare nel nome o nel progetto")
        action = {"type": t, "match": match}
    out["action"] = action
    return out


class ButtonStore:
    def __init__(self, home: Path) -> None:
        self.path = home / "buttons.json"
        self.buttons: list[dict] = []
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.buttons = [validate_button(b, b.get("id")) for b in data]
            if self._upgrade_legacy():
                self._save()
            return
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError, AttributeError):
            self.buttons = []
            return
        self.buttons = [
            validate_button({
                "title": t, "icon": {"type": "glyph", "value": e},
                "action": {"type": "send_keys", "text": k, "target": "current"},
            }) for t, e, k in DEFAULTS
        ]
        self._save()

    def _upgrade_legacy(self) -> bool:
        changed = False
        for b in self.buttons:
            glyph = LEGACY_DEFAULTS.get((b["title"], b["icon"]["value"])) if b["icon"]["type"] == "emoji" else None
            if glyph:
                b["icon"] = {"type": "glyph", "value": glyph}
                changed = True
        return changed

    def _save(self) -> None:
        atomic_write_json(self.path, self.buttons)

    def get(self, bid: str) -> dict | None:
        return next((b for b in self.buttons if b["id"] == bid), None)

    def replace_all(self, items: list) -> None:
        if not isinstance(items, list):
            raise ValidationError("Elenco non valido")
        new = [validate_button(b, str(b.get("id") or uuid.uuid4())) for b in items]
        if len({b["id"] for b in new}) != len(new):
            raise ValidationError("ID duplicati")
        self.buttons = new
        self._save()

    def create(self, raw: dict) -> dict:
        b = validate_button({**raw, "id": None})
        self.buttons.append(b)
        self._save()
        return b

    def update(self, bid: str, raw: dict) -> dict | None:
        old = self.get(bid)
        if old is None:
            return None
        b = validate_button(raw, bid)
        self.buttons[self.buttons.index(old)] = b
        self._save()
        return b

    def delete(self, bid: str) -> bool:
        old = self.get(bid)
        if old is None:
            return False
        self.buttons.remove(old)
        self._save()
        return True


async def _run_shell(command: str, timeout: int) -> tuple[bool, str]:
    proc = await asyncio.create_subprocess_exec(
        *platform.current().shell_argv(command),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return False, f"Timeout dopo {timeout} s"
    text = out.decode(errors="replace").strip()[-500:]
    detail = f"Codice di uscita {proc.returncode}"
    return proc.returncode == 0, f"{detail}\n{text}" if text else detail


async def run_button(
    button: dict,
    pid: int | str | None,
    *,
    focus_pid: Callable[[int], Awaitable[None]],
    find_session: Callable[[str], dict | None],
    send_text: Callable[[str, int | None], Awaitable[None]],
) -> tuple[bool, str]:
    """Execute a button action. Returns (ok, detail)."""
    from .errors import ControlError

    a = button["action"]
    try:
        if a["type"] == "send_keys":
            use_pid = pid if a.get("target") == "selected" else None
            await send_text(a["text"], use_pid)
            return True, "Inviato"
        if a["type"] == "shell":
            return await _run_shell(a["command"], int(a.get("timeout", 10)))
        if a["type"] == "open_url":
            argv = platform.current().open_argv(a["url"])
            if argv is None:
                return False, "xdg-open non trovato: installa xdg-utils"
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
            )
            _, err = await proc.communicate()
            if proc.returncode == 0:
                return True, "Aperto"
            return False, err.decode(errors="replace").strip()[-500:] or "Apertura non riuscita"
        if a["type"] == "focus_session":
            s = find_session(a["match"])
            if s is None:
                return False, "Nessuna sessione corrisponde"
            await focus_pid(s.get("id") or s["pid"])
            return True, f"In primo piano: {s['name']}"
    except ControlError as exc:
        return False, str(exc)
    except Exception as exc:  # never let an action crash the server
        return False, f"Errore: {exc}"
    return False, "Azione sconosciuta"
