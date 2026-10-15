"""HTTP/WebSocket server of the Neondeck agent."""
from __future__ import annotations

import asyncio
import collections
import functools
import html
import hmac
import json
import logging
import math
import socket
import struct
import sys
import time
import uuid
import zlib
from pathlib import Path
from urllib.parse import quote, urlparse

from aiohttp import WSMsgType, web

from . import platform, tls
from .buttons import ButtonStore, ValidationError, run_button
from .pairing import TTL as PAIR_TTL, Pairing
from .config import config_mtime, home_dir, lan_ips, load_config, preview_url, save_config, setup_url
from .errors import ControlError
from .ledfont import MASCOT, bitmap_cells
from .netguard import (LOCALNET, NO_ARP, NO_ROUTE, LOCALNET_WARN_EVERY, AuthThrottle, LanController, NetworkMonitor, RateLimitedLog,
                       in_local_subnet, is_trusted, localnet_guidance, new_entry, normalize_mac)
from .sessions import Context, SessionTracker, frontmost_app
from .terminals import Backend, make_backends, reports
from .vscode import BridgeServer

log = logging.getLogger("neondeck")
_bonjour_warn = RateLimitedLog(LOCALNET_WARN_EVERY)
STATIC = Path(__file__).parent / "static"
MAX_UPLOAD = 2 * 1024 * 1024
ICON_EXT = {"png", "jpg", "jpeg", "gif", "webp", "svg"}
UNSUPPORTED = "Sessione in un terminale non supportato"
LOCAL_ADDRS = {"127.0.0.1", "::1"}
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}
DEVICE_PATHS = ("/api/sessions", "/api/buttons", "/api/focus", "/api/press", "/ws")
PAIR_PATHS = ("/api/pair",)  # network checks only: the phone has no token yet
PAIR_RATE = (5, 600.0)  # creations per IP, window in seconds


def json_error(status: int, msg: str) -> web.Response:
    return web.json_response({"ok": False, "error": msg, "detail": msg}, status=status)


def host_only(request: web.Request) -> str:
    host = request.host or ""
    if host.startswith("["):
        return host.split("]")[0] + "]"
    return host.rsplit(":", 1)[0] if ":" in host else host


# ---------------------------------------------------------------- middleware

def short_ua(request: web.Request) -> str:
    return request.headers.get("User-Agent", "-")[:60]


async def reject(request: web.Request, status: int, ws_code: int, msg: str) -> web.StreamResponse:
    if request.path == "/ws":
        # Accept then close with an app-level code so the browser can tell the failure apart.
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.close(code=ws_code, message=msg.encode()[:100])
        return ws
    return json_error(status, msg)


async def network_gate(request: web.Request) -> web.StreamResponse | None:
    """Lockout and network checks for device endpoints. None means allowed."""
    c = core(request)
    ip = request.remote or ""
    await c.reload_config()
    local = ip in LOCAL_ADDRS
    if not local:
        if c.throttle.is_locked(ip):
            if c.rl_log.allow(f"locked:{ip}"):
                log.warning("Richiesta da %s respinta: IP temporaneamente bloccato", ip)
            return await reject(request, 429, 4429, "Troppi tentativi, riprova più tardi")
        await c.network.refresh()
        if not in_local_subnet(ip, c.network.interfaces) or \
                not is_trusted((c.network.current or {}).get("mac"), c.config.get("trusted_networks", [])):
            if c.rl_log.allow(f"untrusted:{ip}"):
                log.warning("Richiesta da %s respinta: rete non fidata (%s)", ip,
                            (c.network.current or {}).get("mac") or "gateway sconosciuto")
            return await reject(request, 403, 4403, "Rete non fidata")
    return None


async def device_gate(request: web.Request) -> web.StreamResponse | None:
    """Network and token checks for device endpoints. None means allowed."""
    denied = await network_gate(request)
    if denied is not None:
        return denied
    c = core(request)
    path = request.path
    ip = request.remote or ""
    local = ip in LOCAL_ADDRS
    supplied = ""
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        supplied = auth[7:].strip()
    elif path == "/ws":
        supplied = request.query.get("token", "")
    if hmac.compare_digest(supplied.encode(), c.config["token"].encode()):
        if not local:
            c.throttle.record_success(ip)
        return None
    if not local:
        log.warning("Token non valido da %s (%s)", ip, short_ua(request))
        if c.throttle.record_failure(ip):
            log.warning("Troppi token errati da %s: IP bloccato per 10 minuti", ip)
    return await reject(request, 401, 4401, "Token mancante o non valido")


@web.middleware
async def security(request: web.Request, handler):
    path = request.path
    if path == "/admin" or path.startswith("/api/admin"):
        # Trust only the socket address; never X-Forwarded-For.
        if request.remote not in LOCAL_ADDRS:
            return json_error(403, "Accesso consentito solo da localhost")
        # Defend against DNS rebinding and cross-site requests to localhost.
        if host_only(request).lower() not in LOCAL_HOSTS:
            return json_error(403, "Host non consentito")
        origin = request.headers.get("Origin")
        if origin and urlparse(origin).netloc != request.host:
            return json_error(403, "Origine non consentita")
        if path.startswith("/api/admin"):  # the HTML page itself holds no secrets
            c = core(request)
            await c.reload_config()
            supplied = request.headers.get("X-Admin-Key", "")
            if not hmac.compare_digest(supplied.encode(), c.config["admin_key"].encode()):
                if c.rl_log.allow("admin-key"):
                    log.warning("Chiave admin mancante o non valida (%s)", path)
                return json_error(401, "Chiave admin mancante o non valida")
    elif path in PAIR_PATHS or path.startswith("/api/pair/"):
        denied = await network_gate(request)
        if denied is not None:
            return denied
    elif path in DEVICE_PATHS:
        denied = await device_gate(request)
        if denied is not None:
            return denied
    return await handler(request)


# ---------------------------------------------------------------- state

class Core:
    """Shared state: sessions, buttons, terminal backends, websocket clients."""

    FRONT_TTL = 2.0  # seconds the frontmost-app answer is reused

    def __init__(self, home: Path, config: dict, network: NetworkMonitor | None = None,
                 backends: list[Backend] | None = None) -> None:
        self.home = home
        self.config = config
        self.plat = platform.current()
        self.store = ButtonStore(home)
        self.tracker = SessionTracker()
        self.wake = asyncio.Event()
        self.backends = make_backends(self.plat, config, self._terminals_changed) if backends is None else backends
        self.by_name = {b.name: b for b in self.backends}
        if "tmux" in self.by_name:
            self.by_name["tmux"].raiser = self._raise_tmux_client
        self.bridge = BridgeServer(home, self.wake.set)
        self.network = network or NetworkMonitor()
        self.lan: LanController | None = None  # set by make_app
        self.throttle = AuthThrottle()
        self.rl_log = RateLimitedLog(60)
        self.pairing = Pairing()
        self._pair_hits: dict[str, collections.deque] = {}
        self._bg: set[asyncio.Task] = set()
        self.last_focused_id: str | None = None  # last tile focused via the device
        self.clients: set[web.WebSocketResponse] = set()
        self._client_ip: dict[web.WebSocketResponse, str] = {}
        self.active = asyncio.Event()  # set while at least one WebSocket client is connected
        self._last_state: str | None = None
        self._sig = None
        self._refresh_lock = asyncio.Lock()
        self._refreshed_at = -math.inf
        self._need_front = True
        self._front: str | None = None
        self._front_at = -math.inf
        self._cfg_mtime = config_mtime(home)

    @property
    def show_all_terminals(self) -> bool:
        """Terminal tiles for panes without Claude Code (the pre-0.4 key show_all_iterm is the fallback)."""
        value = self.config.get("show_all_terminals", self.config.get("show_all_iterm", True))
        return value is not False

    @property
    def iterm(self) -> Backend | None:
        return self.by_name.get("iterm")

    def _terminals_changed(self) -> None:
        self._front_at = -math.inf  # focus moved: don't trust the cached frontmost app
        self.wake.set()

    async def _raise_tmux_client(self, client: dict, chain: list, table: dict) -> None:
        """Bring the terminal window that hosts a tmux client to the front."""
        it = self.iterm
        if it is not None and it.available() and any("iTerm" in table[p][2] for p in chain if p in table):
            tty = client["tty"] or table.get(client["pid"], (0, "", ""))[1]
            if tty and tty != "?":
                try:
                    await it.focus_tty(tty)
                    return
                except ControlError:
                    pass
        await self.plat.raise_pids(chain, table)

    # --- pairing
    def pair_allowed(self, ip: str) -> bool:
        """At most PAIR_RATE[0] creations per IP per PAIR_RATE[1] seconds."""
        now, (limit, window) = time.monotonic(), PAIR_RATE
        for k in [k for k, d in self._pair_hits.items() if not d or now - d[-1] > window]:
            del self._pair_hits[k]
        d = self._pair_hits.setdefault(ip, collections.deque())
        while d and now - d[0] > window:
            d.popleft()
        if len(d) >= limit:
            return False
        d.append(now)
        return True

    def notify(self, title: str, text: str) -> None:
        """Fire-and-forget desktop notification."""
        async def go() -> None:
            try:
                await asyncio.wait_for(self.plat.notify(title, text), 6)
            except Exception:
                pass
        task = asyncio.ensure_future(go())
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)

    # --- clients
    def add_client(self, ws: web.WebSocketResponse, ip: str = "127.0.0.1") -> None:
        self.clients.add(ws)
        self._client_ip[ws] = ip
        for b in self.backends:
            b.want(math.inf)
        self.active.set()

    def remove_client(self, ws: web.WebSocketResponse) -> None:
        self.clients.discard(ws)
        self._client_ip.pop(ws, None)
        if not self.clients:
            self.active.clear()
            for b in self.backends:
                b.want(60)

    async def close_lan_clients(self) -> None:
        for ws in list(self.clients):
            if self._client_ip.get(ws) not in LOCAL_ADDRS:
                await ws.close(code=4403, message=b"untrusted")

    async def reload_config(self) -> None:
        """Pick up CLI edits of config.json (token, admin key, trusted networks)."""
        mtime = config_mtime(self.home)
        if mtime == self._cfg_mtime:
            return
        self._cfg_mtime = mtime
        try:  # an unreadable file must not make load_config regenerate the secrets
            if not isinstance(json.loads((self.home / "config.json").read_text(encoding="utf-8")), dict):
                return
        except (OSError, ValueError):
            return
        old_token = self.config.get("token")
        new = load_config(self.home)
        self.config.clear()
        self.config.update(new)
        log.info("config.json ricaricato")
        if self.lan:
            self.lan.poke.set()  # re-check the network now (e.g. after `neondeck trust`)
        if new["token"] != old_token:
            for ws in list(self.clients):
                await ws.close(code=4401, message=b"token")

    def state(self) -> dict:
        return {
            "type": "state",
            "sessions": self.tracker.sessions,
            "buttons": self.store.buttons,
            "iterm": bool(self.iterm and self.iterm.available()),
            "terminals": [{"id": b.name, "label": b.label} for b in self.backends if b.available()],
            "procs": self.tracker.procs,
        }

    async def broadcast(self, force: bool = False) -> None:
        msg = json.dumps(self.state(), ensure_ascii=False)
        if msg == self._last_state and not force:
            return
        self._last_state = msg
        for ws in list(self.clients):
            try:
                await ws.send_str(msg)
            except Exception:
                self.remove_client(ws)

    async def front_app(self) -> str | None:
        """Frontmost app, only when a terminal pane or VS Code session exists; reused for FRONT_TTL seconds."""
        if not self._need_front:
            return None
        now = time.monotonic()
        if now - self._front_at >= self.FRONT_TTL:
            self._front = await frontmost_app()
            self._front_at = now
        return self._front

    async def refresh(self, max_age: float = 0.0) -> None:
        """One full refresh (terminals, frontmost app, session files, bridge); skipped if newer than max_age."""
        async with self._refresh_lock:
            if max_age and time.monotonic() - self._refreshed_at < max_age:
                return
            for b in self.backends:
                b.want(math.inf if self.clients else 60)
            if not self.clients:  # on-demand call: give iTerm a moment to connect
                await asyncio.gather(*(b.wait_ready(1.5) for b in self.backends if b.name == "iterm"))
            await asyncio.gather(*(b.probe() for b in self.backends), return_exceptions=True)
            panes, connected = await self._collect()
            ctx = Context(panes=panes, connected=frozenset(connected), front_app=await self.front_app(),
                          front_known=self.plat.front_supported(), show_all=self.show_all_terminals,
                          bridge=self.bridge)
            sig = self.tracker.signature(ctx)
            if sig != self._sig:
                self._sig = sig
                await self.tracker.refresh(ctx)
                need = bool(panes) or any(x["host"] == "vscode" for x in self.tracker.sessions)
                if need and not self._need_front:
                    self.wake.set()  # a terminal session appeared: look at the frontmost app next
                self._need_front = need
                await self.broadcast()
            self._refreshed_at = time.monotonic()

    async def _collect(self) -> tuple[list[dict], set[str]]:
        """Panes of every available backend, and the names of the backends that answered."""
        live = [b for b in self.backends if b.available()]
        results = await asyncio.gather(*(b.snapshot(self.show_all_terminals) for b in live), return_exceptions=True)
        panes: list[dict] = []
        connected: set[str] = set()
        for b, res in zip(live, results):
            if isinstance(res, asyncio.CancelledError):
                raise res
            if isinstance(res, BaseException):
                log.debug("Scansione di %s fallita: %r", b.label, res)
                continue
            panes += res
            if b.available():
                connected.add(b.name)
        return panes, connected

    async def backend_report(self) -> list[dict]:
        """Status lines of every terminal backend plus the VS Code bridge (for admin info and `neondeck status`)."""
        rows = await reports(self.backends)
        n = len(self.bridge.windows)
        rows.append({"name": "vscode", "label": "VS Code bridge", "available": n > 0,
                     "text": f"VS Code bridge: {n} {'finestra' if n == 1 else 'finestre'}"})
        return rows

    async def watch(self) -> None:
        """Refresh loop; it blocks on `active` (no timers, no subprocesses) while nobody is connected."""
        while True:
            await self.active.wait()
            self.wake.clear()  # events that arrived while idle are covered by this refresh
            try:
                await self.refresh(max_age=0.5)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Errore nel ciclo di controllo")
            try:  # bridge and iTerm changes wake the loop early
                await asyncio.wait_for(self.wake.wait(), 1)
            except asyncio.TimeoutError:
                pass
            self.wake.clear()

    # --- actions
    def resolve(self, ref) -> dict | None:
        """Tile by stable id ("claude:<pid>", "tmux:%3", "iterm:<session>") or by Claude pid (compatibility)."""
        if isinstance(ref, str):
            return self.tracker.by_id(ref)
        if isinstance(ref, int) and not isinstance(ref, bool):
            return self.tracker.by_pid(ref)
        return None

    def _backend(self, name: str) -> Backend:
        b = self.by_name.get(name)
        if b is None or not b.available():
            raise ControlError("Terminale non raggiungibile")
        return b

    @staticmethod
    def _pane_of(s: dict) -> tuple[str, str] | None:
        """(backend, pane id) of a tile that lives in a terminal backend."""
        if s["kind"] == "terminal":
            return s["backend"], s["paneId"]
        return s.get("_pane")

    async def focus_pid(self, ref) -> None:
        s = self.resolve(ref)
        if s is None:
            raise ControlError("Sessione non trovata")
        pane = self._pane_of(s)
        if pane:
            await self._backend(pane[0]).focus(pane[1])
        elif s["host"] == "vscode":
            await self.bridge.focus(s)
        else:
            raise ControlError(UNSUPPORTED)
        self.last_focused_id = s["id"]

    async def send_text(self, text: str, ref=None) -> None:
        s = self.resolve(ref) if ref is not None else None
        if s is None:  # no (known) target: the session in the foreground, else the last one focused here
            s = self.tracker.focused() or self.tracker.by_id(self.last_focused_id)
            if s is None:
                raise ControlError("Nessuna sessione in primo piano")
        pane = self._pane_of(s)
        if pane:
            await self._backend(pane[0]).send_text(pane[1], text)
        elif s["host"] == "vscode":
            await self.bridge.send(s, text)
        else:
            raise ControlError(UNSUPPORTED)

    def find_session(self, match: str) -> dict | None:
        m = match.lower()
        return next(
            (s for s in self.tracker.sessions
             if m in s["name"].lower() or m in s["project"].lower() or m in (s.get("title") or "").lower()), None)

    async def do_focus(self, ref) -> tuple[bool, str]:
        try:
            await self.focus_pid(ref)
            return True, "Sessione in primo piano"
        except ControlError as exc:
            return False, str(exc)

    async def do_press(self, bid, pid, target_id=None) -> tuple[bool, str]:
        button = self.store.get(bid) if isinstance(bid, str) else None
        if button is None:
            return False, "Pulsante non trovato"
        if isinstance(target_id, str):
            ref = target_id
        else:
            ref = pid if isinstance(pid, int) and not isinstance(pid, bool) else None
        return await self.run(button, ref)

    async def run(self, button: dict, ref) -> tuple[bool, str]:
        return await run_button(
            button, ref, focus_pid=self.focus_pid,
            find_session=self.find_session, send_text=self.send_text)


def core(request: web.Request) -> Core:
    return request.app["core"]


async def body(request: web.Request) -> dict:
    try:
        data = await request.json()
    except Exception:
        raise web.HTTPBadRequest(text=json.dumps({"ok": False, "error": "JSON non valido"}),
                                 content_type="application/json")
    return data


# ---------------------------------------------------------------- device API

async def api_sessions(request):
    c = core(request)
    await c.refresh(max_age=1.0)  # no-op while the watch loop is running
    return web.json_response({"sessions": c.tracker.sessions, "iterm": bool(c.iterm and c.iterm.available()),
                              "terminals": [{"id": b.name, "label": b.label} for b in c.backends if b.available()],
                              "procs": c.tracker.procs})


async def api_buttons(request):
    return web.json_response({"buttons": core(request).store.buttons})


async def api_focus(request):
    data = await body(request)
    await core(request).refresh(max_age=1.0)
    ok, detail = await core(request).do_focus(data.get("id") or data.get("pid"))
    return web.json_response({"ok": ok, "detail": detail})


async def api_press(request):
    data = await body(request)
    await core(request).refresh(max_age=1.0)
    ok, detail = await core(request).do_press(data.get("id"), data.get("pid"), data.get("target_id"))
    return web.json_response({"ok": ok, "detail": detail})


async def ws_handler(request):
    c = core(request)
    ws = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(request)
    ip = request.remote
    c.add_client(ws, ip or "")
    log.info("Dispositivo connesso: %s (%s)", ip, short_ua(request))
    try:
        try:
            await c.refresh(max_age=0.5)  # first client after idle: bring the state up to date
        except Exception:
            log.exception("Errore nell'aggiornamento iniziale")
        await ws.send_str(json.dumps(c.state(), ensure_ascii=False))
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            try:
                m = json.loads(msg.data)
                kind = m.get("type")
            except (ValueError, AttributeError):
                continue
            if kind == "ping":
                await ws.send_json({"type": "pong"})
                continue
            if kind == "focus":
                ok, detail = await c.do_focus(m.get("id") or m.get("pid"))
            elif kind == "press":
                ok, detail = await c.do_press(m.get("id"), m.get("pid"), m.get("target_id"))
            else:
                continue
            await ws.send_json({"type": "result", "ok": ok, "detail": detail, "req": m.get("req")})
    finally:
        c.remove_client(ws)
        log.info("Dispositivo disconnesso: %s", ip)
    return ws


async def preview(request):
    # The page holds no secrets: the token is supplied by the user and checked on /ws.
    resp = web.FileResponse(STATIC / "preview.html")
    resp.headers["Referrer-Policy"] = "no-referrer"
    return resp


async def vendor_jsqr(request):
    # Public third-party library (Apache-2.0), no secrets: served without auth.
    resp = web.FileResponse(STATIC / "vendor" / "jsQR.js")
    resp.headers["Content-Type"] = "text/javascript; charset=utf-8"
    resp.headers["Cache-Control"] = "public, max-age=604800"
    return resp


async def manifest(request):
    # iOS "Add to Home Screen" uses start_url with storage separate from Safari: with a valid token
    # (throttled like device_gate, so this is no token oracle) the installed app starts already paired.
    start_url = "/preview"
    supplied = request.query.get("token", "")
    if supplied:
        c = core(request)
        await c.reload_config()
        ip = request.remote or ""
        local = ip in LOCAL_ADDRS
        if not local and c.throttle.is_locked(ip):
            if c.rl_log.allow(f"locked:{ip}"):
                log.warning("Richiesta da %s respinta: IP temporaneamente bloccato", ip)
        elif hmac.compare_digest(supplied.encode(), c.config["token"].encode()):
            start_url = "/preview?token=" + quote(supplied, safe="")
            if not local:
                c.throttle.record_success(ip)
        elif not local:
            log.warning("Token non valido da %s (%s)", ip, short_ua(request))
            if c.throttle.record_failure(ip):
                log.warning("Troppi token errati da %s: IP bloccato per 10 minuti", ip)
    data = {
        "id": "/preview", "name": "Neondeck", "short_name": "Neondeck", "lang": "it",
        "start_url": start_url, "scope": "/", "display": "standalone",
        "orientation": "any", "background_color": "#000000", "theme_color": "#000000",
        "icons": [
            {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png"},
            {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"},
        ],
    }
    return web.Response(text=json.dumps(data), content_type="application/manifest+json",
                        headers={"Cache-Control": "no-store"})


MAGENTA, UNLIT, GROUND = (255, 46, 136), (24, 21, 15), (5, 5, 5)
ICON_GRID = 28  # LED cells per side; the 16x14 mascot sits in the middle


def _dot_run(color: tuple, c: int, py: int) -> bytes:
    """One pixel row (c pixels wide) of a round LED of the given color on the ground, anti-aliased on the edge."""
    r, mid = c * 0.40, (c - 1) / 2
    out = bytearray()
    for px in range(c):
        cov = max(0.0, min(1.0, r - math.hypot(px - mid, py - mid) + 0.5))
        out += bytes(round(g + (k - g) * cov) for g, k in zip(GROUND, color))
    return bytes(out)


@functools.lru_cache(maxsize=8)
def make_icon(size: int) -> bytes:
    """PNG app icon: the mascot as round magenta LEDs over a faint unlit dot field, no dependencies."""
    c = max(2, size // ICON_GRID)
    ox = (size - ICON_GRID * c) // 2
    lit = {(x + (ICON_GRID - 16) // 2, y + (ICON_GRID - 14) // 2) for x, y in bitmap_cells(MASCOT)}
    runs = {(on, py): _dot_run(MAGENTA if on else UNLIT, c, py) for on in (True, False) for py in range(c)}
    margin = bytes(GROUND) * ox
    tail = bytes(GROUND) * (size - ox - ICON_GRID * c)
    blank = b"\x00" + bytes(GROUND) * size
    raw = bytearray()
    for y in range(size):
        gy, py = divmod(y - ox, c)
        if y < ox or gy >= ICON_GRID:
            raw += blank
            continue
        raw.append(0)
        raw += margin
        for gx in range(ICON_GRID):
            raw += runs[((gx, gy) in lit, py)]
        raw += tail

    def chunk(tag, data):
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + chunk(b"IEND", b""))


async def icon_png(request):
    size = {"icon-192.png": 192, "icon-512.png": 512, "apple-touch-icon.png": 180}[request.match_info["name"]]
    return web.Response(body=make_icon(size), content_type="image/png",
                        headers={"Cache-Control": "public, max-age=86400"})


# ---------------------------------------------------------------- icons

async def serve_icon(request):
    name = request.match_info["name"]
    base = (core(request).home / "icons").resolve()
    path = (base / name).resolve()
    if path.parent != base or path.suffix.lstrip(".").lower() not in ICON_EXT or not path.is_file():
        raise web.HTTPNotFound()
    resp = web.FileResponse(path)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    if path.suffix.lower() == ".svg":
        resp.headers["Content-Security-Policy"] = "default-src 'none'; style-src 'unsafe-inline'"
        resp.content_type = "image/svg+xml"
    return resp


MAGIC = {
    "png": (b"\x89PNG",), "jpg": (b"\xff\xd8\xff",), "jpeg": (b"\xff\xd8\xff",),
    "gif": (b"GIF8",), "webp": (b"RIFF",),
}


async def admin_upload(request):
    c = core(request)
    try:
        reader = await request.multipart()
        field = await reader.next()
        while field is not None and field.name != "file":
            field = await reader.next()
    except Exception:
        return json_error(400, "Upload non valido")
    if field is None:
        return json_error(400, "Nessun file ricevuto")
    ext = (field.filename or "").rsplit(".", 1)[-1].lower()
    if ext not in ICON_EXT:
        return json_error(400, "Formato non supportato (usa png, jpg, gif, webp o svg)")
    data = bytearray()
    while True:
        chunk = await field.read_chunk(64 * 1024)
        if not chunk:
            break
        data += chunk
        if len(data) > MAX_UPLOAD:
            return json_error(413, "File troppo grande (massimo 2 MB)")
    head = bytes(data[:512]).lstrip()
    if ext == "svg":
        if b"<svg" not in bytes(data[:4096]).lower():
            return json_error(400, "Il file non è un SVG valido")
    elif not any(head.startswith(m) for m in MAGIC[ext]):
        return json_error(400, "Il contenuto non corrisponde all'estensione")
    name = f"{uuid.uuid4().hex}.{ext}"
    (c.home / "icons" / name).write_bytes(bytes(data))
    return web.json_response({"path": f"icons/{name}"})


# ---------------------------------------------------------------- pairing (device side)

async def api_pair(request):
    c = core(request)
    ip = request.remote or ""
    if not c.pair_allowed(ip):
        return json_error(429, "Troppe richieste di abbinamento, riprova più tardi")
    rid, code = c.pairing.create(ip, short_ua(request))
    log.info("Richiesta di abbinamento da %s: codice %s", ip, code)
    c.notify("Neondeck", f"Codice {code} da {ip}: approva con neondeck pair o nel pannello admin")
    return web.json_response({"ok": True, "id": rid, "code": code, "expires_in": int(PAIR_TTL)})


async def api_pair_status(request):
    c = core(request)
    st = c.pairing.status(request.match_info["rid"])
    if st is None:
        ip = request.remote or ""
        if ip not in LOCAL_ADDRS:
            c.throttle.record_failure(ip)
        return web.json_response({"ok": False, "state": "expired"}, status=404)
    out = {"ok": True, "state": st["state"]}
    if st["state"] == "approved":
        out["token"] = c.config["token"]
    return web.json_response(out)


# ---------------------------------------------------------------- admin API

async def admin_page(request):
    return web.FileResponse(STATIC / "admin.html")


async def admin_list(request):
    return web.json_response({"buttons": core(request).store.buttons})


async def admin_replace(request):
    c = core(request)
    data = await body(request)
    items = data.get("buttons") if isinstance(data, dict) else data
    try:
        c.store.replace_all(items)
    except ValidationError as exc:
        return json_error(400, str(exc))
    await c.broadcast()
    return web.json_response({"buttons": c.store.buttons})


async def admin_create(request):
    c = core(request)
    try:
        b = c.store.create(await body(request))
    except ValidationError as exc:
        return json_error(400, str(exc))
    await c.broadcast()
    return web.json_response(b, status=201)


async def admin_update(request):
    c = core(request)
    try:
        b = c.store.update(request.match_info["id"], await body(request))
    except ValidationError as exc:
        return json_error(400, str(exc))
    if b is None:
        return json_error(404, "Pulsante non trovato")
    await c.broadcast()
    return web.json_response(b)


async def admin_delete(request):
    c = core(request)
    if not c.store.delete(request.match_info["id"]):
        return json_error(404, "Pulsante non trovato")
    await c.broadcast()
    return web.json_response({"ok": True})


async def admin_test(request):
    c = core(request)
    b = c.store.get(request.match_info["id"])
    if b is None:
        return json_error(404, "Pulsante non trovato")
    pid = None
    try:
        pid = (await request.json()).get("pid")
    except Exception:
        pass
    ok, detail = await c.run(b, pid if isinstance(pid, (int, str)) and not isinstance(pid, bool) else None)
    return web.json_response({"ok": ok, "detail": detail})


async def admin_info(request):
    c = core(request)
    cfg = c.config
    ips = lan_ips()
    lan = c.lan
    tls_info = {"enabled": bool(cfg.get("tls")), "setup_url": setup_url(cfg, ips),
                "https_url": preview_url({**cfg, "tls": True}, ips).split("?")[0]}
    if tls_info["enabled"]:
        tls_info.update({k: v for k, v in (await asyncio.to_thread(tls.status, c.home)).items() if k != "ca"})
    return web.json_response({
        "tls": tls_info,
        "token": cfg["token"], "port": cfg["port"], "lan_ips": ips,
        "preview_url": preview_url(cfg, ips), "preview_url_ip": preview_url(cfg, ips, by_name=False),
        "lan_listening": bool(lan and lan.listening_ip), "lan_ip": lan.listening_ip if lan else None,
        "network_trusted": bool(lan and lan.network_trusted), "network_name": lan.network_name if lan else None,
        "network_reason": lan.network_reason if lan else None, "network_checked": bool(lan and lan.checked),
        "show_all_terminals": core(request).show_all_terminals,
        "backends": await core(request).backend_report(),
        "bonjour": ("zc" in request.app) if lan and lan.listening_ip else None,
        "pairing": core(request).pairing.pending(),
    })


async def admin_pair_list(request):
    return web.json_response({"pending": core(request).pairing.pending()})


async def admin_pair_resolve(request, approve: bool):
    c = core(request)
    code = request.match_info["code"]
    if not (c.pairing.approve(code) if approve else c.pairing.deny(code)):
        return json_error(404, "Richiesta di abbinamento non trovata o scaduta")
    log.info("Abbinamento %s: codice %s", "approvato" if approve else "rifiutato", code)
    return web.json_response({"ok": True})


async def admin_pair_approve(request):
    return await admin_pair_resolve(request, True)


async def admin_pair_deny(request):
    return await admin_pair_resolve(request, False)


NETWORK_REASONS = {
    NO_ROUTE: "Nessuna rete attiva: collega il computer a una rete.",
    NO_ARP: "Il MAC del gateway non è leggibile.",
    LOCALNET: "macOS blocca la lettura della rete: serve il permesso Rete locale per l'interprete Python.",
}


def networks_state(request, net: dict | None, reason: str | None) -> dict:
    c = core(request)
    lan, nets = c.lan, c.config.get("trusted_networks", [])
    mac = (net or {}).get("mac")
    entry = next((n for n in nets if mac and normalize_mac(str(n.get("mac", ""))) == mac), None)
    current = {"mac": mac, "gateway": (net or {}).get("gateway"), "interface": (net or {}).get("interface"),
               "ip": (net or {}).get("ip"), "subnet": (net or {}).get("subnet"),
               "reason": None if net else NETWORK_REASONS.get(reason, "Rete non riconoscibile."),
               "trusted": entry is not None, "name": entry.get("name") if entry else None}
    listening = bool(lan and lan.listening_ip)
    return {"current": current,
            "trusted": [{**n, "index": i} for i, n in enumerate(nets, 1) if isinstance(n, dict)],
            "lan": {"listening": listening, "ip": lan.listening_ip if lan else None, "port": c.config["port"]},
            "bonjour": ("zc" in request.app) if listening else None}


async def networks_apply(request, status: int = 200) -> web.Response:
    """Persist the config just edited, re-check the network right away and return the new state."""
    c = core(request)
    save_config(c.home, c.config)
    c._cfg_mtime = config_mtime(c.home)
    if c.lan:
        await c.lan.check()
    net = c.network.current
    return web.json_response(networks_state(request, net, c.network.reason), status=status)


async def admin_networks(request):
    c = core(request)
    net = await c.network.refresh()
    return web.json_response(networks_state(request, net, c.network.reason))


async def admin_networks_trust(request):
    c = core(request)
    try:
        data = await request.json()
    except Exception:
        data = {}
    name = data.get("name") if isinstance(data, dict) else None
    if name is not None and (not isinstance(name, str) or len(name.strip()) > 64):
        return json_error(400, "Nome non valido")
    net = await c.network.refresh(force=True)  # only what the server itself detects: never a MAC from the client
    if not net:
        return json_error(409, NETWORK_REASONS.get(c.network.reason, "Rete non riconoscibile."))
    nets = c.config["trusted_networks"]
    if not any(normalize_mac(str(n.get("mac", ""))) == net["mac"] for n in nets if isinstance(n, dict)):
        nets.append(new_entry(net, (name or "").strip() or None))
    return await networks_apply(request, 201)


def find_network(c: Core, raw: str) -> dict | None:
    mac = normalize_mac(raw)
    return next((n for n in c.config["trusted_networks"]
                 if mac and isinstance(n, dict) and normalize_mac(str(n.get("mac", ""))) == mac), None)


async def admin_networks_rename(request):
    c = core(request)
    entry = find_network(c, request.match_info["mac"])
    if entry is None:
        return json_error(404, "Rete non trovata")
    data = await body(request)
    name = data.get("name") if isinstance(data, dict) else None
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > 64:
        return json_error(400, "Nome non valido")
    entry["name"] = name.strip()
    return await networks_apply(request)


async def admin_networks_delete(request):
    c = core(request)
    entry = find_network(c, request.match_info["mac"])
    if entry is None:
        return json_error(404, "Rete non trovata")
    c.config["trusted_networks"].remove(entry)
    return await networks_apply(request)


async def admin_settings(request):
    c = core(request)
    data = await body(request)
    value = data.get("show_all_terminals", data.get("show_all_iterm")) if isinstance(data, dict) else None
    if not isinstance(value, bool):
        return json_error(400, "Valore non valido")
    c.config["show_all_terminals"] = value
    c.config.pop("show_all_iterm", None)
    save_config(c.home, c.config)
    c._cfg_mtime = config_mtime(c.home)
    c.wake.set()
    c._refreshed_at = -math.inf
    await c.refresh()
    return web.json_response({"show_all_terminals": value})


# ---------------------------------------------------------------- lifecycle

class Listeners:
    """Local sites (always on) and the LAN site (only while the network is trusted) of one AppRunner."""

    def __init__(self, app: web.Application) -> None:
        self.app = app
        self.runner: web.AppRunner | None = None
        self.ready = asyncio.Event()
        self.lan_site: web.TCPSite | None = None
        self.setup_runner: web.AppRunner | None = None

    async def start_local(self) -> None:
        port = self.app["config"]["port"]
        await web.TCPSite(self.runner, "127.0.0.1", port).start()
        try:
            await web.TCPSite(self.runner, "::1", port).start()
        except OSError:
            pass  # no IPv6 loopback
        self.ready.set()

    async def start_lan(self, ip: str) -> None:
        cfg = self.app["config"]
        ssl_ctx = None
        if cfg.get("tls"):
            c = self.app["core"]
            try:
                # key generation is CPU-bound: keep it off the event loop
                ssl_ctx = await asyncio.to_thread(tls.server_context, c.home, tls.default_names(cfg), [ip])
            except Exception as exc:
                log.error("HTTPS non disponibile (%s): il listener LAN non viene avviato", exc)
                raise OSError(f"TLS non disponibile: {exc}") from exc
            setup = web.AppRunner(make_setup_app(c), access_log=None)
            await setup.setup()
            try:
                await web.TCPSite(setup, ip, cfg["tls_setup_port"]).start()
            except OSError:
                await setup.cleanup()
                raise
            self.setup_runner = setup
        site = web.TCPSite(self.runner, ip, cfg["port"], ssl_context=ssl_ctx)
        try:
            await site.start()
        except OSError:
            await self._stop_setup()
            raise
        self.lan_site = site
        await start_zeroconf(self.app, ip)

    async def _stop_setup(self) -> None:
        if self.setup_runner:
            runner, self.setup_runner = self.setup_runner, None
            await runner.cleanup()

    async def stop_lan(self) -> None:
        await stop_zeroconf(self.app)
        if self.lan_site:
            await self.lan_site.stop()
            self.lan_site = None
        await self._stop_setup()


def make_setup_app(c: Core) -> web.Application:
    """Plain-HTTP companion of the HTTPS listener: hands out the CA, redirects everything else to https."""
    cfg = c.config

    @web.middleware
    async def gate(request, handler):
        denied = await network_gate(request)
        return denied if denied is not None else await handler(request)

    async def to_setup(request):
        raise web.HTTPFound("/setup")

    async def setup_page(request):
        page = STATIC / "setup.html"
        try:
            text = await asyncio.to_thread(page.read_text, encoding="utf-8")
        except OSError:
            return web.Response(status=500, text="Pagina di setup mancante (static/setup.html)")
        https = f"https://{host_only(request)}:{cfg['port']}/preview"
        fp = await asyncio.to_thread(tls.ca_fingerprint, c.home) or ""
        text = text.replace("__HTTPS_URL__", html.escape(https)).replace("__CA_FP__", html.escape(fp))
        return web.Response(text=text, content_type="text/html", headers={"Cache-Control": "no-store"})

    async def mobileconfig(request):
        data = await asyncio.to_thread(tls.mobileconfig, c.home, socket.gethostname().split(".")[0])
        return web.Response(body=data, content_type="application/x-apple-aspen-config",
                            headers={"Content-Disposition": 'attachment; filename="neondeck-ca.mobileconfig"'})

    async def ca_download(request):
        data = await asyncio.to_thread(tls.ca_pem, c.home)
        return web.Response(body=data, content_type="application/x-pem-file",
                            headers={"Content-Disposition": 'attachment; filename="neondeck-ca.crt"'})

    async def to_https(request):
        raise web.HTTPPermanentRedirect(f"https://{host_only(request)}:{cfg['port']}{request.path_qs}")

    app = web.Application(middlewares=[gate])
    app["core"] = c
    app["config"] = cfg
    r = app.router
    r.add_get("/", to_setup)
    r.add_get("/setup", setup_page)
    r.add_get("/neondeck-ca.mobileconfig", mobileconfig)
    r.add_get("/neondeck-ca.pem", ca_download)
    r.add_get("/{tail:.*}", to_https)  # HEAD included
    return app


async def start_zeroconf(app: web.Application, ip: str) -> None:
    cfg = app["config"]
    try:
        from zeroconf import ServiceInfo
        from zeroconf.asyncio import AsyncZeroconf

        host = socket.gethostname().split(".")[0]
        info = ServiceInfo(
            "_neondeck._tcp.local.",
            f"Neondeck ({host})._neondeck._tcp.local.",
            addresses=[socket.inet_aton(ip)],
            port=cfg["port"], properties={"v": "1", "tls": "1" if cfg.get("tls") else "0"}, server=f"{cfg['lan_hostname']}.local.",
        )
        azc = AsyncZeroconf(interfaces=[ip])
        await azc.async_register_service(info)
        app["zc"] = (azc, info)
        log.info("Annunciato via Bonjour: _neondeck._tcp e %s.local su %s", cfg["lan_hostname"], ip)
    except Exception as exc:
        # Bonjour is optional (the Pi can always connect by IP): never block the LAN listener for it
        if _bonjour_warn.allow("bonjour"):
            log.warning("Bonjour non attivo: %s. Per l'auto-discovery: %s", exc, localnet_guidance())


async def stop_zeroconf(app: web.Application) -> None:
    zc = app.pop("zc", None)
    if zc:
        azc, info = zc
        try:
            await azc.async_unregister_service(info)
            await azc.async_close()
        except Exception:
            pass


async def lan_loop(app: web.Application):
    await app["listeners"].ready.wait()
    await app["core"].lan.run()


async def on_startup(app: web.Application):
    c: Core = app["core"]
    for b in c.backends:
        b.start()
    await c.bridge.start()
    app["watch"] = asyncio.create_task(c.watch())
    app["lan_task"] = asyncio.create_task(lan_loop(app))


async def on_cleanup(app: web.Application):
    app["watch"].cancel()
    app["lan_task"].cancel()
    for b in app["core"].backends:
        await b.stop()
    await app["core"].bridge.stop()
    await stop_zeroconf(app)


def make_app(home: Path, config: dict, network: NetworkMonitor | None = None) -> web.Application:
    app = web.Application(middlewares=[security], client_max_size=MAX_UPLOAD + 256 * 1024)
    app["config"] = config
    c = app["core"] = Core(home, config, network)
    app["listeners"] = ls = Listeners(app)
    c.lan = LanController(c.network, lambda: c.config, ls.start_lan, ls.stop_lan, c.close_lan_clients)
    r = app.router
    r.add_get("/api/sessions", api_sessions)
    r.add_get("/api/buttons", api_buttons)
    r.add_post("/api/focus", api_focus)
    r.add_post("/api/press", api_press)
    r.add_get("/ws", ws_handler)
    r.add_post("/api/pair", api_pair)
    r.add_get("/api/pair/{rid}", api_pair_status)
    r.add_get("/preview", preview)
    r.add_get("/vendor/jsQR.js", vendor_jsqr)
    r.add_get("/manifest.webmanifest", manifest)
    r.add_get(r"/{name:(icon-192|icon-512|apple-touch-icon)\.png}", icon_png)
    r.add_get("/icons/{name}", serve_icon)
    r.add_get("/admin", admin_page)
    r.add_get("/api/admin/buttons", admin_list)
    r.add_put("/api/admin/buttons", admin_replace)
    r.add_post("/api/admin/buttons", admin_create)
    r.add_put("/api/admin/buttons/{id}", admin_update)
    r.add_delete("/api/admin/buttons/{id}", admin_delete)
    r.add_post("/api/admin/buttons/{id}/test", admin_test)
    r.add_post("/api/admin/icons", admin_upload)
    r.add_get("/api/admin/info", admin_info)
    r.add_get("/api/admin/pair", admin_pair_list)
    r.add_post("/api/admin/pair/{code}/approve", admin_pair_approve)
    r.add_post("/api/admin/pair/{code}/deny", admin_pair_deny)
    r.add_put("/api/admin/settings", admin_settings)
    r.add_get("/api/admin/networks", admin_networks)
    r.add_post("/api/admin/networks", admin_networks_trust)
    r.add_patch("/api/admin/networks/{mac}", admin_networks_rename)
    r.add_delete("/api/admin/networks/{mac}", admin_networks_delete)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


def main() -> None:
    logging.basicConfig(
        stream=sys.stderr, level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:  # launchd gives agents a soft limit of 256 fds
        import resource
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        want = 4096 if hard == resource.RLIM_INFINITY else min(hard, 4096)
        if soft < want:
            resource.setrlimit(resource.RLIMIT_NOFILE, (want, hard))
    except (ImportError, ValueError, OSError):
        pass
    home = home_dir()
    config = load_config(home)
    log.info("Dati in %s", home)
    log.info("Pannello admin: neondeck admin (http://127.0.0.1:%d/admin)", config["port"])
    if not config["trusted_networks"]:
        log.warning("Nessuna rete fidata: i dispositivi in LAN sono respinti. Esegui `neondeck trust`")
    asyncio.run(serve(make_app(home, config), config["port"]))


async def serve(app: web.Application, port: int) -> None:
    runner = web.AppRunner(app, access_log=None)
    app["listeners"].runner = runner
    await runner.setup()
    try:
        await app["listeners"].start_local()  # the LAN site is started by the network monitor
        log.info("In ascolto in locale su 127.0.0.1:%d", port)
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()
