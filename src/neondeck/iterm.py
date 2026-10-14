"""iTerm2 control through the official Python API (persistent connection)."""
from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from typing import Callable

import iterm2
from iterm2 import api_pb2
from iterm2.notifications import async_subscribe_to_variable_change_notification, async_unsubscribe
from iterm2.rpc import async_variable

from .errors import ControlError

log = logging.getLogger("neondeck.iterm")
WATCHED_VARS = ("jobName", "path")  # `name` comes for free with the app refresh (and flickers with spinners)
VARS_MIN_INTERVAL = 2.0  # seconds between variable fetches


class ItermError(ControlError):
    pass


class ItermClient:
    """Persistent iTerm2 connection; it is only (re)established while somebody wants it."""

    def __init__(self, on_change: Callable[[], None] | None = None) -> None:
        self._conn: iterm2.Connection | None = None
        self._task: asyncio.Task | None = None
        self._monitors: asyncio.Task | None = None
        self._on_change = on_change or (lambda: None)
        self._want = asyncio.Event()
        self._want_until = 0.0
        self._lost = asyncio.Event()
        self._ready = asyncio.Event()
        self._last_fail = -math.inf
        self._tty_by_sid: dict[str, str] = {}  # session id -> tty (a session never changes tty)
        self._vars: dict[str, dict] = {}  # session id -> {"job", "path"}
        self._dirty: set[str] = set()  # sessions whose watched variables changed since the last fetch
        self._vars_push = False  # True while variable-change notifications are subscribed
        self._vars_at = -math.inf

    @property
    def connected(self) -> bool:
        return self._conn is not None

    def start(self) -> None:
        self._task = asyncio.create_task(self._connect_loop())

    async def stop(self) -> None:
        for t in (self._task, self._monitors):
            if t:
                t.cancel()

    def want(self, ttl: float = math.inf) -> None:
        """Ask for a connection for `ttl` seconds (forever by default)."""
        self._want_until = max(self._want_until, time.monotonic() + ttl)
        self._want.set()

    async def wait_connected(self, timeout: float) -> bool:
        """Wait briefly for a first connection; returns at once if iTerm was unreachable recently."""
        if self._conn is not None:
            return True
        if time.monotonic() - self._last_fail < 30:
            return False
        try:
            await asyncio.wait_for(self._ready.wait(), timeout)
        except asyncio.TimeoutError:
            return False
        return True

    async def _connect_loop(self) -> None:
        delay = 2
        while True:
            await self._want.wait()  # idle: no connection attempts, no timers
            if self._conn is not None:
                await self._lost.wait()  # connected: block until an RPC fails
                self._lost.clear()
                continue
            try:
                self._conn = await asyncio.wait_for(iterm2.Connection.async_create(), 10)
                log.info("Connesso a iTerm2")
                delay = 2
                self._tty_by_sid.clear()
                self._vars.clear()
                self._monitors = asyncio.create_task(self._watch_changes(self._conn))
                self._ready.set()
                self._on_change()
            except (asyncio.CancelledError, KeyboardInterrupt):
                raise
            except BaseException as exc:  # the library may even raise SystemExit
                log.debug("iTerm2 non raggiungibile: %r", exc)
                self._conn = None
                self._last_fail = time.monotonic()
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30)
                if time.monotonic() > self._want_until:
                    self._want.clear()

    async def _watch_changes(self, conn) -> None:
        """Wake the server on focus/layout changes; polling still covers a failure here."""
        async def focus():
            async with iterm2.FocusMonitor(conn) as mon:
                while True:
                    await mon.async_get_next_update()
                    self._on_change()

        async def layout():
            async with iterm2.LayoutChangeMonitor(conn) as mon:
                while True:
                    await mon.async_get()
                    self._on_change()

        async def variables():
            """Mark sessions dirty when jobName/path change (identifier "all" covers every session)."""
            async def cb(_conn, msg):
                self._dirty.add(msg.identifier)
                self._on_change()

            tokens = []
            try:
                for name in WATCHED_VARS:
                    tokens.append(await async_subscribe_to_variable_change_notification(
                        conn, cb, iterm2.VariableScopes.SESSION.value, name, "all"))
                self._vars_push = True
                await asyncio.Event().wait()
            finally:
                self._vars_push = False
                for t in tokens:
                    try:
                        await async_unsubscribe(conn, t)
                    except Exception:
                        pass

        try:
            await asyncio.gather(focus(), layout(), variables())
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            log.debug("Monitor iTerm2 non disponibili, uso il polling: %r", exc)

    def _drop(self, exc: BaseException) -> None:
        log.warning("Connessione a iTerm2 persa: %r", exc)
        self._conn = None
        self._tty_by_sid.clear()
        self._vars.clear()
        self._ready.clear()
        if self._monitors:
            self._monitors.cancel()
        self._lost.set()

    async def _app(self):
        if self._conn is None:
            raise ItermError("iTerm2 non raggiungibile (API Python disattivata o app chiusa)")
        try:
            app = await asyncio.wait_for(iterm2.async_get_app(self._conn), 5)
        except Exception as exc:
            self._drop(exc)
            raise ItermError("Connessione a iTerm2 persa") from exc
        if app is None:
            raise ItermError("iTerm2 non disponibile")
        return app

    @staticmethod
    def _all_sessions(app):
        for w in app.windows:
            for t in w.tabs:
                yield from t.sessions

    @staticmethod
    def layout_of(app) -> list[tuple[int, int, int, int, object]]:
        """[(window, tab, pane, panes in tab, session)], all 1-based, in app order."""
        out = []
        for wi, w in enumerate(app.windows, 1):
            for ti, t in enumerate(w.tabs, 1):
                sessions = list(t.sessions)
                for pi, s in enumerate(sessions, 1):
                    out.append((wi, ti, pi, len(sessions), s))
        return out

    async def _fetch_vars(self, s) -> None:
        """One RPC per session: tty, jobName and path together."""
        res = await async_variable(self._conn, s.session_id, gets=["tty", *WATCHED_VARS])
        resp = res.variable_response
        if resp.status != api_pb2.VariableResponse.Status.Value("OK") or len(resp.values) < 3:
            raise ItermError("variabili non leggibili")
        tty, job, path = (json.loads(v) for v in resp.values[:3])
        if tty:
            self._tty_by_sid[s.session_id] = tty
        self._vars[s.session_id] = {"job": job or None, "path": path or None}

    async def _refresh_vars(self, sessions: list) -> None:
        """Fetch variables only for new or changed sessions, at most every VARS_MIN_INTERVAL seconds."""
        live = {s.session_id for s in sessions}
        for sid in list(self._vars):
            if sid not in live:
                del self._vars[sid]
        self._dirty &= live
        now = time.monotonic()
        todo = [s for s in sessions if s.session_id not in self._vars]
        if now - self._vars_at >= VARS_MIN_INTERVAL:
            if self._vars_push:
                todo += [s for s in sessions if s.session_id in self._dirty and s not in todo]
            else:  # no notifications: poll everything
                todo = list(sessions)
            self._vars_at = now
        if not todo:
            return
        self._dirty.difference_update(s.session_id for s in todo)
        results = await asyncio.gather(*(self._fetch_vars(s) for s in todo), return_exceptions=True)
        for s, r in zip(todo, results):
            if isinstance(r, BaseException):
                log.debug("variabili non leggibili: %r", r)
                self._dirty.add(s.session_id)

    async def _tty(self, s) -> str | None:
        """tty of a session: one RPC the first time, then from the cache."""
        tty = self._tty_by_sid.get(s.session_id)
        if tty is None:
            tty = await s.async_get_variable("tty")
            if tty:
                self._tty_by_sid[s.session_id] = tty
        return tty or None

    async def _scan(self, app) -> dict[str, object]:
        """tty -> session; RPCs only for sessions not seen before."""
        sessions = list(self._all_sessions(app))
        live = {s.session_id for s in sessions}
        for sid in list(self._tty_by_sid):
            if sid not in live:
                del self._tty_by_sid[sid]
        found = {}
        for s in sessions:
            try:
                tty = await self._tty(s)
            except Exception as exc:
                log.debug("variabile tty non leggibile: %r", exc)
                continue
            if tty:
                found[tty] = s
        return found

    async def find_session_by_tty(self, tty: str):
        app = await self._app()
        found = await self._scan(app)
        if tty not in found:  # miss: rebuild the map from scratch once
            self._tty_by_sid.clear()
            found = await self._scan(app)
        return app, found.get(tty)

    async def snapshot(self, details: bool = False) -> tuple[str | None, frozenset[str], list[dict]]:
        """One scan: (tty of iTerm's current session, ttys of all sessions, session details).

        Details (position, title, job, cwd) are only gathered when asked for.
        """
        if self._conn is None:
            return None, frozenset(), []
        try:
            app = await self._app()
            lay = self.layout_of(app)
            if details:
                await self._refresh_vars([x[-1] for x in lay])
            found = await self._scan(app)
            win = app.current_terminal_window
            tab = win.current_tab if win else None
            cur = tab.current_session if tab else None
            current = await self._tty(cur) if cur else None
            sessions = []
            if details:
                for wi, ti, pi, n, s in lay:
                    v = self._vars.get(s.session_id, {})
                    sessions.append({
                        "session_id": s.session_id, "window": wi, "tab": ti, "pane": pi, "panes": n,
                        "tty": self._tty_by_sid.get(s.session_id), "title": s.name or None,
                        "job": v.get("job"), "path": v.get("path"),
                        "current": cur is not None and s.session_id == cur.session_id,
                    })
            return current, frozenset(found), sessions
        except Exception as exc:
            log.debug("scansione iTerm2 fallita: %r", exc)
            return None, frozenset(), []

    async def find_session_by_id(self, session_id: str):
        app = await self._app()
        return app, next((x[-1] for x in self.layout_of(app) if x[-1].session_id == session_id), None)

    async def focus(self, tty: str | None = None, session_id: str | None = None) -> None:
        if session_id:
            app, s = await self.find_session_by_id(session_id)
        elif tty:
            app, s = await self.find_session_by_tty(tty)
        else:
            raise ItermError("TTY della sessione sconosciuta")
        if s is None:
            raise ItermError("Sessione non trovata in iTerm2 (forse è in un altro terminale)")
        await s.async_activate(select_tab=True, order_window_front=True)
        await app.async_activate()

    async def send_text(self, text: str, tty: str | None = None, session_id: str | None = None) -> None:
        app = await self._app()
        if session_id:
            _, s = await self.find_session_by_id(session_id)
            if s is None:
                raise ItermError("Sessione non trovata in iTerm2")
        elif tty:
            _, s = await self.find_session_by_tty(tty)
            if s is None:
                raise ItermError("Sessione non trovata in iTerm2")
        else:
            win = app.current_terminal_window
            tab = win.current_tab if win else None
            s = tab.current_session if tab else None
            if s is None:
                raise ItermError("Nessuna sessione iTerm2 in primo piano")
        try:
            await s.async_send_text(text)
        except Exception as exc:
            self._drop(exc)
            raise ItermError("Invio fallito: connessione a iTerm2 persa") from exc
