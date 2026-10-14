"""tmux backend (macOS and Linux). One `tmux list-panes -a ; list-clients` call per snapshot."""
from __future__ import annotations

import logging
import os
import re
import shutil
import stat
from typing import Awaitable, Callable

from .. import platform
from ..errors import ControlError
from ..platform.base import ancestors_of
from .base import Backend, ordinals, report, run_cmd

log = logging.getLogger("neondeck.tmux")
TIMEOUT = 3.0
# Free-text fields go last: the line is split with a limit, so a stray tab cannot shift the others.
PANE_FIELDS = ("session_name", "session_id", "window_index", "window_active", "pane_index", "pane_id",
               "pane_tty", "pane_active", "session_attached", "pane_current_command", "window_name",
               "pane_current_path", "pane_title")
CLIENT_FIELDS = ("client_name", "client_tty", "client_session", "client_activity", "client_pid")
PANE_FMT = "P\t" + "\t".join(f"#{{{f}}}" for f in PANE_FIELDS)
CLIENT_FMT = "C\t" + "\t".join(f"#{{{f}}}" for f in CLIENT_FIELDS)

Runner = Callable[..., Awaitable[tuple[int, str, str]]]
RaiseHook = Callable[[dict, list, dict], Awaitable[None]]


class TmuxError(ControlError):
    pass


def default_socket(env=None, uid: int | None = None) -> str:
    """Path of the default server socket: $TMUX_TMPDIR (or /tmp)/tmux-<uid>/default."""
    env = os.environ if env is None else env
    return os.path.join(env.get("TMUX_TMPDIR") or "/tmp", f"tmux-{os.getuid() if uid is None else uid}", "default")


def parse_output(text: str) -> tuple[list[dict], list[dict]]:
    """Combined `list-panes` + `list-clients` output (lines tagged P / C) -> (panes, clients)."""
    panes, clients = [], []
    for line in text.splitlines():
        if line.startswith("P\t"):
            f = line[2:].split("\t", len(PANE_FIELDS) - 1)
            if len(f) == len(PANE_FIELDS):
                panes.append(dict(zip(PANE_FIELDS, f)))
        elif line.startswith("C\t"):
            f = line[2:].split("\t", len(CLIENT_FIELDS) - 1)
            if len(f) == len(CLIENT_FIELDS):
                c = dict(zip(CLIENT_FIELDS, f))
                c["activity"] = int(c["client_activity"]) if c["client_activity"].isdigit() else 0
                c["pid"] = int(c["client_pid"]) if c["client_pid"].isdigit() else 0
                c["name"], c["tty"], c["session"] = c["client_name"], c["client_tty"], c["client_session"]
                clients.append(c)
    return panes, clients


def pick_client(clients: list[dict]) -> dict | None:
    """The client the user is most likely looking at: the most recent activity (later in the list on a tie)."""
    best = None
    for c in clients:
        if best is None or c["activity"] >= best["activity"]:
            best = c
    return best


def hex_bytes(text: str) -> list[str]:
    """UTF-8 bytes of the text as two-digit hex words, for `send-keys -H`."""
    return [f"{b:02x}" for b in text.encode("utf-8")]


def parse_version(text: str) -> tuple[int, int] | None:
    """'tmux 3.4' / 'tmux next-3.5' / 'tmux 3.3a' -> (3, 4); None for 'tmux master'."""
    m = re.search(r"(\d+)\.(\d+)", text)
    return (int(m.group(1)), int(m.group(2))) if m else None


def build_panes(raw_panes: list[dict], clients: list[dict], apps: list[str]) -> list[dict]:
    """Normalized panes. `active` marks the single pane the most recent client is looking at."""
    client = pick_client(clients)
    sessions = ordinals(int(p["session_id"].lstrip("$")) for p in raw_panes if p["session_id"].lstrip("$").isdigit())
    per_window: dict[tuple, int] = {}
    for p in raw_panes:
        key = (p["session_id"], p["window_index"])
        per_window[key] = per_window.get(key, 0) + 1
    panes = []
    for p in raw_panes:
        sid = p["session_id"].lstrip("$")
        win, idx = int(p["window_index"]) if p["window_index"].isdigit() else 0, p["pane_index"]
        attached = bool(client) and client["session"] == p["session_name"]
        panes.append({
            "backend": "tmux", "pane_id": p["pane_id"], "tty": p["pane_tty"] or None,
            "title": p["pane_title"] or None, "job": p["pane_current_command"] or None,
            "cwd": p["pane_current_path"] or None,
            "loc": f"tmux {p['session_name']}:{p['window_index']}.{idx}",
            "active": attached and p["window_active"] == "1" and p["pane_active"] == "1",
            "apps": apps, "window": sessions.get(int(sid), 0) if sid.isdigit() else 0, "tab": win,
            "pane": int(idx) if idx.isdigit() else 0, "panes": per_window[(p["session_id"], p["window_index"])],
            "session": p["session_name"], "session_id": p["session_id"], "window_index": p["window_index"],
        })
    return panes


class TmuxBackend(Backend):
    name = "tmux"
    label = "tmux"

    def __init__(self, socket: str | None = None, run: Runner = run_cmd, plat=None, which=shutil.which) -> None:
        self.socket = socket  # explicit server socket (-S); None = the default server
        self._run = run
        self._plat = plat
        self._which = which
        self.raiser: RaiseHook | None = None  # set by the server: knows about iTerm2
        self._known: dict[str, dict] = {}  # pane id -> pane, from the last snapshot
        self._apps: dict[int, list[str]] = {}  # client pid -> names of its ancestors
        self._found = False
        self._hex_ok: bool | None = None
        self._warned = False

    @property
    def plat(self):
        return self._plat or platform.current()

    def _argv(self, *args: str) -> list[str]:
        return ["tmux", *(["-S", self.socket] if self.socket else []), *args]

    def _has_binary(self) -> bool:
        if not self._found:  # once found it stays found; a missing binary is looked up again
            self._found = bool(self._which("tmux"))
        return self._found

    def socket_path(self) -> str:
        return self.socket or default_socket()

    def available(self) -> bool:
        """A tmux binary and a server socket: no subprocess while there is no server."""
        if not self._has_binary():
            return False
        try:
            return stat.S_ISSOCK(os.stat(self.socket_path()).st_mode)
        except OSError:
            return False

    async def _tmux(self, *args: str, timeout: float = TIMEOUT) -> str:
        rc, out, err = await self._run(self._argv(*args), timeout)
        if rc != 0:
            raise TmuxError(err.strip().splitlines()[-1] if err.strip() else f"tmux ha risposto con codice {rc}")
        return out

    async def _list(self) -> tuple[list[dict], list[dict]]:
        out = await self._tmux("list-panes", "-a", "-F", PANE_FMT, ";", "list-clients", "-F", CLIENT_FMT)
        return parse_output(out)

    async def _client_apps(self, client: dict | None) -> list[str]:
        """Names (exe basenames and .app bundles) of the processes hosting a tmux client; cached per pid."""
        if not client or not client["pid"]:
            return []
        pid = client["pid"]
        if pid not in self._apps:
            table = await self.plat.process_table()
            names: list[str] = []
            for p in ancestors_of(pid, table):
                comm = table[p][2]
                names.append(os.path.basename(comm.rstrip("/")))
                i = comm.find(".app")
                if i > 0:
                    names.append(os.path.basename(comm[:i]))
            self._apps = {k: v for k, v in self._apps.items() if k == pid}  # keep the cache tiny
            self._apps[pid] = names
        return self._apps[pid]

    async def snapshot(self, details: bool = True) -> list[dict]:
        try:
            raw, clients = await self._list()
        except TmuxError as exc:
            log.debug("tmux non risponde: %s", exc)
            self._known = {}
            return []
        panes = build_panes(raw, clients, await self._client_apps(pick_client(clients)))
        self._known = {p["pane_id"]: p for p in panes}
        return panes

    async def _find(self, pane_id: str) -> dict:
        if pane_id not in self._known:
            await self.snapshot()
        if pane_id not in self._known:
            raise TmuxError("Pannello tmux non trovato")
        return self._known[pane_id]

    async def focus(self, pane_id: str) -> None:
        pane = await self._find(pane_id)
        _, clients = await self._list()
        client = pick_client(clients)
        if client:
            await self._tmux("switch-client", "-c", client["name"], "-t", pane["session_id"])
        await self._tmux("select-window", "-t", f"{pane['session_id']}:{pane['window_index']}")
        await self._tmux("select-pane", "-t", pane_id)
        if client is None:
            raise TmuxError("Nessun client tmux collegato")
        await self._raise(client)

    async def _raise(self, client: dict) -> None:
        if not client["pid"]:
            return
        table = await self.plat.process_table()
        chain = ancestors_of(client["pid"], table)
        if self.raiser:
            await self.raiser(client, chain, table)
        else:
            await self.plat.raise_pids(chain, table)

    async def _supports_hex(self) -> bool:
        if self._hex_ok is None:
            rc, out, _ = await self._run(["tmux", "-V"], TIMEOUT)
            ver = parse_version(out) if rc == 0 else None
            self._hex_ok = ver is None or ver >= (3, 0)
            if not self._hex_ok:
                log.warning("tmux %s è precedente alla 3.0: invio dei tasti in modalità letterale, "
                            "alcuni tasti speciali potrebbero non funzionare", out.strip())
        return self._hex_ok

    async def send_text(self, pane_id: str, text: str) -> None:
        if not text:
            return
        if await self._supports_hex():
            await self._tmux("send-keys", "-t", pane_id, "-H", *hex_bytes(text))
        else:
            await self._tmux("send-keys", "-t", pane_id, "-l", text)

    async def report(self) -> dict:
        if not self._has_binary():
            return report(self, False, "tmux: non trovato")
        if not self.available():
            return report(self, False, "tmux: nessun server in esecuzione")
        try:
            raw, _ = await self._list()
        except TmuxError as exc:
            return report(self, False, f"tmux: non risponde ({exc})")
        n = len({p["session_id"] for p in raw})
        return report(self, True, f"tmux: attivo ({n} {'sessione' if n == 1 else 'sessioni'}, "
                                  f"{len(raw)} {'pannello' if len(raw) == 1 else 'pannelli'})")
