"""HTTP/WebSocket server of the Neondeck agent."""
from __future__ import annotations

import asyncio
import functools
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
from urllib.parse import urlparse

from aiohttp import WSMsgType, web

from .buttons import ButtonStore, ValidationError, run_button
from .config import config_mtime, home_dir, lan_ips, load_config, preview_url, save_config
from .errors import ControlError
from .iterm import ItermClient
from .netguard import (LOCALNET, NO_ARP, NO_ROUTE, LOCALNET_WARN_EVERY, AuthThrottle, LanController, NetworkMonitor, RateLimitedLog,
                       in_local_subnet, is_trusted, localnet_guidance, new_entry, normalize_mac)
from .sessions import Context, SessionTracker, frontmost_app
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


async def device_gate(request: web.Request) -> web.StreamResponse | None:
    """Lockout, network and token checks for device endpoints. None means allowed."""
    c = core(request)
    path = request.path
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
    elif path in DEVICE_PATHS:
        denied = await device_gate(request)
        if denied is not None:
            return denied
    return await handler(request)


# ---------------------------------------------------------------- state

class Core:
    """Shared state: sessions, buttons, iTerm2, websocket clients."""

    FRONT_TTL = 2.0  # seconds lsappinfo's answer is reused

    def __init__(self, home: Path, config: dict, network: NetworkMonitor | None = None) -> None:
        self.home = home
        self.config = config
        self.store = ButtonStore(home)
        self.tracker = SessionTracker()
        self.wake = asyncio.Event()
        self.iterm = ItermClient(self._iterm_changed)
        self.bridge = BridgeServer(home, self.wake.set)
        self.network = network or NetworkMonitor()
        self.lan: LanController | None = None  # set by make_app
        self.throttle = AuthThrottle()
        self.rl_log = RateLimitedLog(60)
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
    def show_all_iterm(self) -> bool:
        return self.config.get("show_all_iterm", True) is not False

    def _iterm_changed(self) -> None:
        self._front_at = -math.inf  # focus moved: don't trust the cached frontmost app
        self.wake.set()

    # --- clients
    def add_client(self, ws: web.WebSocketResponse, ip: str = "127.0.0.1") -> None:
        self.clients.add(ws)
        self._client_ip[ws] = ip
        self.iterm.want()
        self.active.set()

    def remove_client(self, ws: web.WebSocketResponse) -> None:
        self.clients.discard(ws)
        self._client_ip.pop(ws, None)
        if not self.clients:
            self.active.clear()
            self.iterm.want(60)

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
            "iterm": self.iterm.connected,
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
        """Frontmost app, only when an iTerm/VS Code session exists; reused for FRONT_TTL seconds."""
        if not self._need_front:
            return None
        now = time.monotonic()
        if now - self._front_at >= self.FRONT_TTL:
            self._front = await frontmost_app()
            self._front_at = now
        return self._front

    async def refresh(self, max_age: float = 0.0) -> None:
        """One full refresh (iTerm, frontmost app, session files, bridge); skipped if newer than max_age."""
        async with self._refresh_lock:
            if max_age and time.monotonic() - self._refreshed_at < max_age:
                return
            self.iterm.want(math.inf if self.clients else 60)
            if not self.clients and not self.iterm.connected:  # on-demand call: give iTerm a moment
                await self.iterm.wait_connected(1.5)
            focus_tty, ttys, isessions = await self.iterm.snapshot(details=self.show_all_iterm)
            ctx = Context(iterm_connected=self.iterm.connected, iterm_ttys=ttys, iterm_focus_tty=focus_tty,
                          iterm_sessions=isessions, front_app=await self.front_app(), bridge=self.bridge)
            sig = self.tracker.signature(ctx)
            if sig != self._sig:
                self._sig = sig
                await self.tracker.refresh(ctx)
                need = any(x["host"] in ("iterm", "vscode") for x in self.tracker.sessions)
                if need and not self._need_front:
                    self.wake.set()  # a terminal session appeared: look at the frontmost app next
                self._need_front = need
                await self.broadcast()
            self._refreshed_at = time.monotonic()

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
        """Tile by stable id ("claude:<pid>", "iterm:<session>") or by Claude pid (compatibility)."""
        if isinstance(ref, str):
            return self.tracker.by_id(ref)
        if isinstance(ref, int) and not isinstance(ref, bool):
            return self.tracker.by_pid(ref)
        return None

    async def focus_pid(self, ref) -> None:
        s = self.resolve(ref)
        if s is None:
            raise ControlError("Sessione non trovata")
        if s["kind"] == "terminal":
            await self.iterm.focus(session_id=s["itermSession"])
        elif s["host"] == "iterm":
            await self.iterm.focus(s["tty"])
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
        if s["kind"] == "terminal":
            await self.iterm.send_text(text, session_id=s["itermSession"])
        elif s["host"] == "iterm":
            await self.iterm.send_text(text, s["tty"])
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
    return web.json_response({"sessions": c.tracker.sessions, "iterm": c.iterm.connected,
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


async def manifest(request):
    data = {
        "name": "Neondeck", "short_name": "Neondeck", "lang": "it",
        "start_url": "/preview", "scope": "/", "display": "standalone",
        "orientation": "any", "background_color": "#000000", "theme_color": "#000000",
        "icons": [
            {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png"},
            {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"},
        ],
    }
    return web.Response(text=json.dumps(data), content_type="application/manifest+json")


# Pixel-art mascot on a 16x14 grid (x, y, w, h), painted in this order: body, visor cut-out, eyes.
MASCOT_BODY = ((7, 0, 2, 2), (2, 2, 12, 9), (0, 5, 2, 3), (14, 5, 2, 3), (5, 11, 6, 1), (2, 12, 12, 2))
MASCOT_CUT = ((3, 5, 10, 3), (5, 9, 1, 1), (7, 9, 1, 1), (9, 9, 1, 1), (6, 12, 4, 1))
MASCOT_EYES = ((4, 6, 3, 1), (9, 6, 3, 1))


@functools.lru_cache(maxsize=8)
def make_icon(size: int) -> bytes:
    """PNG app icon: the neon mascot on black, nearest-neighbor scaled, with no dependencies."""
    body, eyes, black = (255, 60, 199), (25, 240, 255), (0, 0, 0)
    scale = max(1, int(size * 0.62) // 16)
    ox, oy = (size - 16 * scale) // 2, (size - 14 * scale) // 2
    grid = [[None] * 16 for _ in range(14)]
    for rects, color in ((MASCOT_BODY, body), (MASCOT_CUT, black), (MASCOT_EYES, eyes)):
        for x, y, w, h in rects:
            for yy in range(y, y + h):
                for xx in range(x, x + w):
                    grid[yy][xx] = color
    raw = bytearray()
    for y in range(size):
        raw.append(0)
        gy = (y - oy) // scale
        row = grid[gy] if 0 <= y - oy and 0 <= gy < 14 else None
        for x in range(size):
            gx = (x - ox) // scale
            px = row[gx] if row is not None and 0 <= x - ox and 0 <= gx < 16 else None
            px = px or black
            if y % 4 == 3:  # faint scanlines
                px = tuple(c * 3 // 4 for c in px)
            raw += bytes(px)

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
    cfg = core(request).config
    ips = lan_ips()
    lan = core(request).lan
    return web.json_response({
        "token": cfg["token"], "port": cfg["port"], "lan_ips": ips,
        "preview_url": preview_url(cfg, ips),
        "lan_listening": bool(lan and lan.listening_ip), "lan_ip": lan.listening_ip if lan else None,
        "network_trusted": bool(lan and lan.network_trusted), "network_name": lan.network_name if lan else None,
        "network_reason": lan.network_reason if lan else None, "network_checked": bool(lan and lan.checked),
        "show_all_iterm": core(request).show_all_iterm,
        "bonjour": ("zc" in request.app) if lan and lan.listening_ip else None,
    })


NETWORK_REASONS = {
    NO_ROUTE: "Nessuna rete attiva: collega il Mac a una rete.",
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
    value = data.get("show_all_iterm") if isinstance(data, dict) else None
    if not isinstance(value, bool):
        return json_error(400, "Valore non valido")
    c.config["show_all_iterm"] = value
    save_config(c.home, c.config)
    c._cfg_mtime = config_mtime(c.home)
    c.wake.set()
    c._refreshed_at = -math.inf
    await c.refresh()
    return web.json_response({"show_all_iterm": value})


# ---------------------------------------------------------------- lifecycle

class Listeners:
    """Local sites (always on) and the LAN site (only while the network is trusted) of one AppRunner."""

    def __init__(self, app: web.Application) -> None:
        self.app = app
        self.runner: web.AppRunner | None = None
        self.ready = asyncio.Event()
        self.lan_site: web.TCPSite | None = None

    async def start_local(self) -> None:
        port = self.app["config"]["port"]
        await web.TCPSite(self.runner, "127.0.0.1", port).start()
        try:
            await web.TCPSite(self.runner, "::1", port).start()
        except OSError:
            pass  # no IPv6 loopback
        self.ready.set()

    async def start_lan(self, ip: str) -> None:
        site = web.TCPSite(self.runner, ip, self.app["config"]["port"])
        await site.start()
        self.lan_site = site
        await start_zeroconf(self.app, ip)

    async def stop_lan(self) -> None:
        await stop_zeroconf(self.app)
        if self.lan_site:
            await self.lan_site.stop()
            self.lan_site = None


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
            port=cfg["port"], properties={"v": "1"}, server=f"{host}.local.",
        )
        azc = AsyncZeroconf(interfaces=[ip])
        await azc.async_register_service(info)
        app["zc"] = (azc, info)
        log.info("Annunciato via Bonjour: _neondeck._tcp su %s", ip)
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
    c.iterm.start()
    await c.bridge.start()
    app["watch"] = asyncio.create_task(c.watch())
    app["lan_task"] = asyncio.create_task(lan_loop(app))


async def on_cleanup(app: web.Application):
    app["watch"].cancel()
    app["lan_task"].cancel()
    await app["core"].iterm.stop()
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
    r.add_get("/preview", preview)
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
