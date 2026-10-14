"""Bridge to the VS Code extension: Unix socket server, newline-delimited JSON."""
from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Callable

from . import platform
from .errors import ControlError

log = logging.getLogger("neondeck.vscode")
REQUEST_TIMEOUT = 3
RAISE_TIMEOUT = 5


class BridgeError(ControlError):
    pass


class Window:
    def __init__(self, window_id: str, writer: asyncio.StreamWriter) -> None:
        self.id = window_id
        self.writer = writer
        self.app = ""
        self.folders: list[str] = []
        self.focused = False
        self.terminals: list[dict] = []
        self.pending: dict[int, asyncio.Future] = {}


def _clean_terminals(raw) -> list[dict]:
    out = []
    for t in raw if isinstance(raw, list) else []:
        if (isinstance(t, dict) and isinstance(t.get("id"), int)
                and isinstance(t.get("processId"), int)):
            out.append({"id": t["id"], "processId": t["processId"],
                        "name": str(t.get("name") or ""), "active": bool(t.get("active"))})
    return out


class BridgeServer:
    def __init__(self, home: Path, on_change: Callable[[], None]) -> None:
        self.path = home / "bridge.sock"
        self.on_change = on_change
        self.windows: dict[str, Window] = {}
        self.version = 0  # bumped whenever the visible terminal state changes
        self._key = ""
        self._req = 0
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        try:
            self.path.unlink()  # stale socket from a previous run
        except FileNotFoundError:
            pass
        except OSError as exc:
            log.warning("Socket bridge non rimovibile: %s", exc)
        try:
            self._server = await asyncio.start_unix_server(self._client, path=str(self.path), limit=1 << 20)
            os.chmod(self.path, 0o600)
            log.info("Bridge VS Code in ascolto su %s", self.path)
        except OSError as exc:
            log.warning("Bridge VS Code non attivo: %s", exc)

    async def stop(self) -> None:
        if self._server:
            self._server.close()
        for w in list(self.windows.values()):
            w.writer.close()
        try:
            self.path.unlink()
        except OSError:
            pass

    # --- connection handling
    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        win: Window | None = None
        try:
            async for line in reader:
                try:
                    m = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(m, dict):
                    continue
                kind = m.get("type")
                if kind == "hello":
                    wid = m.get("windowId")
                    if not isinstance(wid, str) or not wid or len(wid) > 100:
                        continue
                    old = self.windows.get(wid)
                    if old is not None and old is not win:
                        self._drop(old)
                    win = Window(wid, writer)
                    self.windows[wid] = win
                    self._apply(win, m)
                elif win is None:
                    continue
                elif kind == "terminals":
                    self._apply(win, m)
                elif kind == "result":
                    fut = win.pending.get(m.get("req"))
                    if fut is not None and not fut.done():
                        fut.set_result((bool(m.get("ok")), str(m.get("detail") or "")))
                self._changed()
        except (ConnectionError, ValueError, asyncio.LimitOverrunError):
            pass
        finally:
            if win is not None and self.windows.get(win.id) is win:
                self._drop(win)
            else:
                writer.close()
            self._changed()

    def _drop(self, win: Window) -> None:
        if self.windows.get(win.id) is win:
            del self.windows[win.id]
        for fut in win.pending.values():
            if not fut.done():
                fut.set_exception(BridgeError("Connessione a VS Code persa"))
        win.writer.close()

    def _apply(self, win: Window, m: dict) -> None:
        if "app" in m:
            win.app = str(m["app"])[:100]
        if isinstance(m.get("folders"), list):
            win.folders = [f for f in m["folders"] if isinstance(f, str)][:20]
        if "focused" in m:
            win.focused = bool(m["focused"])
        if "terminals" in m:
            win.terminals = _clean_terminals(m["terminals"])

    def _changed(self) -> None:
        key = json.dumps(sorted(
            (w.id, w.focused, w.folders, w.terminals) for w in self.windows.values()))
        if key != self._key:
            self._key = key
            self.version += 1
            self.on_change()

    # --- queries
    def match(self, ancestors: list[int]) -> tuple[Window, dict] | None:
        """Find the terminal whose shell pid is the nearest ancestor of a session."""
        by_pid = {t["processId"]: (w, t) for w in self.windows.values() for t in w.terminals}
        for pid in ancestors:
            if pid in by_pid:
                return by_pid[pid]
        return None

    def is_focused(self, ancestors: list[int]) -> bool:
        m = self.match(ancestors)
        return bool(m and m[0].focused and m[1]["active"])

    # --- commands
    async def _request(self, win: Window, msg: dict) -> None:
        self._req += 1
        req = self._req
        fut = asyncio.get_running_loop().create_future()
        win.pending[req] = fut
        try:
            win.writer.write(json.dumps({**msg, "req": req}).encode() + b"\n")
            await win.writer.drain()
            ok, detail = await asyncio.wait_for(fut, REQUEST_TIMEOUT)
        except asyncio.TimeoutError:
            raise BridgeError("VS Code non risponde") from None
        except OSError:
            raise BridgeError("Connessione a VS Code persa") from None
        finally:
            win.pending.pop(req, None)
        if not ok:
            raise BridgeError(detail or "Operazione rifiutata da VS Code")

    def _resolve(self, session: dict) -> tuple[Window, dict]:
        m = self.match(session["_ancestors"])
        if m is None:
            raise BridgeError("Terminale VS Code non trovato (estensione non connessa?)")
        return m

    async def focus(self, session: dict) -> None:
        win, term = self._resolve(session)
        await self._request(win, {"type": "focus", "terminalId": term["id"]})
        await raise_window(win, session.get("_app"))

    async def send(self, session: dict, text: str) -> None:
        win, term = self._resolve(session)
        await self._request(win, {"type": "send", "terminalId": term["id"], "text": text})


async def raise_window(win: Window, app_path: str | None) -> None:
    """The extension API cannot raise the OS window: use the editor CLI (or `open`) instead."""
    if os.environ.get("NEONDECK_NO_RAISE") or os.environ.get("CLAUDE_DECK_NO_RAISE"):
        return
    argv = platform.current().editor_raise_argv(win.app, app_path, win.folders)
    if not argv:
        return
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await asyncio.wait_for(proc.wait(), RAISE_TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        log.warning("Timeout nel portare in primo piano la finestra di VS Code")
    except OSError as exc:
        log.warning("Impossibile portare in primo piano VS Code: %s", exc)
