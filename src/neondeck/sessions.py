"""Discovery of active Claude Code sessions from ~/.claude/sessions, merged with the terminal panes."""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from . import platform
from .platform.base import outer_app  # noqa: F401  (re-exported)
from .terminals.base import BACKEND_ORDER, loc_of  # noqa: F401  (re-exported)

SESSIONS_DIR = Path.home() / ".claude" / "sessions"
MAX_CHAIN = 64
EDITOR_EXES = ("code", "code-insiders", "codium", "cursor")  # Linux: all Electron processes share the exe
log = logging.getLogger("neondeck.sessions")


@dataclass
class Context:
    """Per-poll view of the terminals, gathered by the server."""
    panes: list = field(default_factory=list)  # panes of every available backend (see terminals.base)
    connected: frozenset = frozenset()  # names of the backends that answered
    front_app: str | None = None
    front_known: bool = True  # False when the platform cannot tell the frontmost app: pane focus is trusted
    show_all: bool = True  # terminal tiles for panes that run no Claude Code session
    bridge: object | None = None  # vscode.BridgeServer


def pane_focused(p: dict, front_app: str | None, front_known: bool = True) -> bool:
    """A pane is focused when its terminal marks it active and that app is in front."""
    if not p.get("active"):
        return False
    if not front_known:
        return True
    if not front_app:
        return False
    f = front_app.lower()
    # substring either way ("iTerm" vs "iTerm2"), but not for very short names such as "sh" or "zsh"
    return any(a == f or (len(a) >= 4 and (a in f or f in a)) for a in (x.lower() for x in p.get("apps") or []))


def terminal_tile(p: dict, focused: bool) -> dict:
    """Tile for a pane that is not a Claude Code session."""
    path = p.get("cwd") or ""
    title = p.get("job") or p.get("title") or "shell"
    return {
        "kind": "terminal",
        "id": f"{p['backend']}:{p['pane_id']}",
        "pid": None,
        "backend": p["backend"],
        "paneId": p["pane_id"],
        "sessionId": None,
        "name": title,
        "title": title,
        "cwd": path,
        "project": os.path.basename(path.rstrip("/")) or ("/" if path else ""),
        "status": "terminal",
        "statusSince": 0,
        "startedAt": None,
        "tty": p.get("tty"),
        "host": p["backend"],
        "loc": p.get("loc"),
        "controllable": True,
        "focused": focused,
    }


def _order(p: dict) -> tuple:
    b = p["backend"]
    return (BACKEND_ORDER.index(b) if b in BACKEND_ORDER else len(BACKEND_ORDER),
            p.get("window") or 0, p.get("tab") or 0, p.get("pane") or 0)


def match_pane(s: dict, by_tty: dict, by_pid: dict) -> dict | None:
    """The pane a Claude session runs in: by tty, else (kitty) by the shell pid among its ancestors."""
    pane = by_tty.get(s.get("tty")) if s.get("tty") else None
    if pane is None and by_pid:
        pane = next((by_pid[p] for p in [s.get("pid"), *s.get("_ancestors", [])] if p in by_pid), None)
    return pane


def attach_panes(claude: list[dict], panes: list[dict], front_app: str | None, front_known: bool = True) -> set:
    """Tag Claude tiles with the pane they run in (host, location, focus, controllable); returns the claimed panes."""
    by_tty = {p["tty"]: p for p in panes if p.get("tty")}
    by_pid = {p["pid"]: p for p in panes if p.get("pid")}
    claimed = set()
    for s in claude:
        pane = match_pane(s, by_tty, by_pid)
        if pane is None:
            s["loc"] = None
            continue
        claimed.add((pane["backend"], pane["pane_id"]))
        s.update(host=pane["backend"], loc=pane.get("loc"), controllable=True,
                 focused=pane_focused(pane, front_app, front_known),
                 _pane=(pane["backend"], pane["pane_id"]))
    return claimed


def assemble(claude: list[dict], panes: list[dict], claimed: set, front_app: str | None,
             front_known: bool = True, show_all: bool = True) -> list[dict]:
    """Claude tiles first, then terminal tiles in backend/window/tab/pane order; non-controllable Claude tiles last.

    A terminal tile exists only for panes that do not belong to a Claude Code session; iTerm2 sessions that
    are just a `tmux` client are hidden, the tmux panes behind them have their own tiles.
    """
    terminals = []
    if show_all:
        terminals = [terminal_tile(p, pane_focused(p, front_app, front_known)) for p in sorted(panes, key=_order)
                     if (p["backend"], p["pane_id"]) not in claimed
                     and not (p["backend"] == "iterm" and p.get("job") == "tmux")]
    return ([s for s in claude if s["controllable"]] + terminals
            + [s for s in claude if not s["controllable"]])


def merge_tiles(claude: list[dict], panes: list[dict], front_app: str | None, front_known: bool = True,
                show_all: bool = True) -> list[dict]:
    claimed = attach_panes(claude, panes, front_app, front_known)
    return assemble(claude, panes, claimed, front_app, front_known, show_all)


def host_of(comm: str) -> str | None:
    """Terminal host guessed from the name of an ancestor process."""
    if "iTerm" in comm:
        return "iterm"
    if ("Code Helper" in comm or "Visual Studio Code" in comm or comm.endswith("/Code")
            or "Cursor Helper" in comm or "/Cursor.app/" in comm or comm.endswith("/Cursor")):
        return "vscode"
    base = os.path.basename(comm.rstrip("/")).lower()
    if base in EDITOR_EXES:
        return "vscode"
    if base.startswith("tmux"):
        return "tmux"
    return None


async def process_table() -> dict[int, tuple[int, str, str]]:
    """pid -> (ppid, tty, comm). Empty (with a warning) when the process table is unreadable."""
    return await platform.current().process_table()


async def frontmost_app() -> str | None:
    return await platform.current().frontmost_app()


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _json_files() -> list[os.DirEntry]:
    try:
        with os.scandir(SESSIONS_DIR) as it:
            return [e for e in it if e.name.endswith(".json") and e.is_file()]
    except OSError:
        return []


def _file_pid(name: str) -> int | None:
    head = name.split(".", 1)[0]
    return int(head) if head.isdigit() else None


class SessionTracker:
    def __init__(self) -> None:
        # pid -> (ancestors nearest first, host, app bundle path (macOS), tty)
        self._info: dict[int, tuple[list[int], str, str | None, str | None]] = {}
        self._sessions: list[dict] = []
        self._public: list[dict] = []
        self.procs = True  # False while the last process-table snapshot was unusable

    def signature(self, ctx: Context) -> tuple:
        files = []
        for e in _json_files():
            try:
                mtime = e.stat().st_mtime_ns
            except OSError:
                continue
            pid = _file_pid(e.name)
            alive = pid is not None and pid_alive(pid)
            files.append((e.name, mtime, alive))
        files.sort()
        bridge = ctx.bridge
        panes = tuple((p["backend"], p["pane_id"], p.get("tty"), p.get("pid"), p.get("loc"), p.get("title"),
                       p.get("job"), p.get("cwd"), p.get("window"), p.get("tab"), p.get("pane"),
                       pane_focused(p, ctx.front_app, ctx.front_known)) for p in ctx.panes)
        return (tuple(files), ctx.connected, panes, ctx.show_all, bridge.version if bridge else 0)

    def _resolve(self, pid: int, table: dict) -> tuple[list[int], str, str | None, str | None] | None:
        if pid in self._info:
            return self._info[pid]
        if pid not in table:
            return None
        ancestors: list[int] = []
        host, app = "other", None
        cur = table[pid][0]
        while cur > 1 and cur not in ancestors and len(ancestors) < MAX_CHAIN and cur in table:
            ancestors.append(cur)
            comm = table[cur][2]
            if host == "other" and (h := host_of(comm)):
                host, app = h, outer_app(comm)
            cur = table[cur][0]
        tty_raw = table[pid][1]
        tty = None if tty_raw in ("?", "??") else (tty_raw if tty_raw.startswith("/dev/") else "/dev/" + tty_raw)
        info = (ancestors, host, app, tty)
        if tty is not None:  # keep retrying while the tty is still unknown
            self._info[pid] = info
        return info

    async def refresh(self, ctx: Context) -> list[dict]:
        entries = []
        for e in _json_files():
            try:
                data = json.loads(Path(e.path).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(data, dict) or data.get("kind") != "interactive":
                continue
            pid = data.get("pid")
            if isinstance(pid, int) and pid_alive(pid):
                entries.append((pid, data))
        # a single process-table snapshot, only when some pid is not cached yet
        table: dict = {}
        if any(p not in self._info for p, _ in entries):
            table = await process_table()
            self.procs = bool(table)
        bridge = ctx.bridge
        result = []
        for pid, data in entries:
            ancestors, host, app, tty = self._resolve(pid, table) or ([], "other", None, None)
            cwd = str(data.get("cwd") or "")
            status_at = data.get("statusUpdatedAt") or data.get("updatedAt") or 0
            if host == "vscode" and bridge is not None:
                controllable = bridge.match(ancestors) is not None
                focused = bridge.is_focused(ancestors)
            else:  # terminal panes claim their sessions in attach_panes
                controllable = focused = False
            result.append({
                "kind": "claude",
                "id": f"claude:{pid}",
                "pid": pid,
                "sessionId": data.get("sessionId"),
                "name": data.get("name") or os.path.basename(cwd.rstrip("/")) or str(pid),
                "cwd": cwd,
                "project": os.path.basename(cwd.rstrip("/")) or cwd,
                "status": str(data.get("status") or "unknown"),
                "statusSince": status_at,
                "startedAt": data.get("startedAt"),
                "tty": tty,
                "host": host,
                "controllable": controllable,
                "focused": focused,
                "_ancestors": ancestors,
                "_app": app,
            })
        claimed = attach_panes(result, ctx.panes, ctx.front_app, ctx.front_known)
        result.sort(key=lambda s: (not s["controllable"], s["status"] != "busy", -(s["statusSince"] or 0)))
        live = {s["pid"] for s in result}
        result = assemble(result, ctx.panes, claimed, ctx.front_app, ctx.front_known, ctx.show_all)
        for pid in list(self._info):
            if pid not in live and not pid_alive(pid):
                del self._info[pid]
        self._sessions = result
        self._public = [{k: v for k, v in s.items() if not k.startswith("_")} for s in result]
        return result

    @property
    def sessions(self) -> list[dict]:
        """Public view (no internal fields), safe to serialize."""
        return self._public

    def by_pid(self, pid) -> dict | None:
        """Internal record (includes _ancestors/_app)."""
        return next((s for s in self._sessions if s["pid"] == pid and s["kind"] == "claude"), None) \
            if pid is not None else None

    def by_id(self, tile_id) -> dict | None:
        """Internal record of any tile (Claude or terminal) by its stable id."""
        return next((s for s in self._sessions if s["id"] == tile_id), None) if tile_id else None

    def focused(self) -> dict | None:
        return next((s for s in self._sessions if s["focused"]), None)
