"""Discovery of active Claude Code sessions from ~/.claude/sessions."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

SESSIONS_DIR = Path.home() / ".claude" / "sessions"
MAX_CHAIN = 64
PS_WARN_EVERY = 300  # seconds between repeated `ps` warnings
log = logging.getLogger("neondeck.sessions")
_last_ps_warn = 0.0


@dataclass
class Context:
    """Per-poll view of the terminal hosts, gathered by the server."""
    iterm_connected: bool = False
    iterm_ttys: frozenset = frozenset()
    iterm_focus_tty: str | None = None
    iterm_sessions: list = field(default_factory=list)  # all iTerm2 sessions (see ItermClient.snapshot), if wanted
    front_app: str | None = None
    bridge: object | None = None  # vscode.BridgeServer


def loc_of(it: dict) -> str:
    """'F1 · T2' or 'F1 · T2 · P3' (pane only when the tab is split)."""
    loc = f"F{it['window']} · T{it['tab']}"
    return f"{loc} · P{it['pane']}" if it.get("panes", 1) > 1 else loc


def terminal_tile(it: dict, front_app: str | None) -> dict:
    """Tile for an iTerm2 session that is not a Claude Code session."""
    path = it.get("path") or ""
    title = it.get("job") or it.get("title") or "shell"
    return {
        "kind": "terminal",
        "id": f"iterm:{it['session_id']}",
        "pid": None,
        "itermSession": it["session_id"],
        "sessionId": None,
        "name": title,
        "title": title,
        "cwd": path,
        "project": os.path.basename(path.rstrip("/")) or ("/" if path else ""),
        "status": "terminal",
        "statusSince": 0,
        "startedAt": None,
        "tty": it.get("tty"),
        "host": "iterm",
        "loc": loc_of(it),
        "controllable": True,
        "focused": bool(it.get("current") and front_app == "iTerm2"),
    }


def merge_tiles(claude: list[dict], iterm: list[dict], front_app: str | None) -> list[dict]:
    """Claude tiles (tagged with their iTerm location) first, then terminal tiles in window/tab/pane order.

    Non-controllable Claude tiles stay last. A terminal tile exists only for iTerm sessions whose tty
    does not belong to a Claude Code session.
    """
    by_tty = {it["tty"]: it for it in iterm if it.get("tty")}
    claude_ttys = {s["tty"] for s in claude if s.get("tty")}
    for s in claude:
        it = by_tty.get(s.get("tty")) if s["host"] == "iterm" else None
        s["loc"] = loc_of(it) if it else None
    ordered = sorted(iterm, key=lambda i: (i["window"], i["tab"], i["pane"]))
    terminals = [terminal_tile(i, front_app) for i in ordered if not i.get("tty") or i["tty"] not in claude_ttys]
    return ([s for s in claude if s["controllable"]] + terminals
            + [s for s in claude if not s["controllable"]])


def host_of(comm: str) -> str | None:
    if "iTerm" in comm:
        return "iterm"
    if ("Code Helper" in comm or "Visual Studio Code" in comm or comm.endswith("/Code")
            or "Cursor Helper" in comm or "/Cursor.app/" in comm or comm.endswith("/Cursor")):
        return "vscode"
    return None


def outer_app(comm: str) -> str | None:
    i = comm.find(".app")
    return comm[:i + 4] if i > 0 else None


def _warn_ps(reason: str) -> None:
    global _last_ps_warn
    now = time.monotonic()
    if _last_ps_warn and now - _last_ps_warn < PS_WARN_EVERY:
        return
    _last_ps_warn = now
    log.warning("ps non disponibile (%s): impossibile riconoscere iTerm/VS Code, "
                "sessioni non comandabili. L'agent gira in una sandbox?", reason)


async def process_table() -> dict[int, tuple[int, str, str]]:
    """One `ps` snapshot: pid -> (ppid, tty, comm). Empty (with a warning) when ps is unusable."""
    table: dict[int, tuple[int, str, str]] = {}
    try:
        proc = await asyncio.create_subprocess_exec(
            "ps", "-axo", "pid=,ppid=,tty=,comm=",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await proc.communicate()
    except OSError as exc:
        _warn_ps(str(exc))
        return table
    if proc.returncode != 0:
        _warn_ps(err.decode(errors="replace").strip() or f"codice {proc.returncode}")
        return table
    for line in out.decode(errors="replace").splitlines():
        parts = line.split(None, 3)  # comm may contain spaces
        if len(parts) == 4 and parts[0].isdigit() and parts[1].isdigit():
            table[int(parts[0])] = (int(parts[1]), parts[2], parts[3])
    if not table:
        _warn_ps("nessun processo restituito")
    return table


async def frontmost_app() -> str | None:
    try:
        proc = await asyncio.create_subprocess_exec(
            "/bin/sh", "-c", 'lsappinfo info -only name "$(lsappinfo front)"',
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(proc.communicate(), 3)
    except (OSError, asyncio.TimeoutError):
        return None
    line = out.decode(errors="replace").splitlines()[:1]
    quoted = re.findall(r'"([^"]*)"', line[0]) if line else []
    return quoted[-1] if quoted else None  # `"iTerm2" ASN:...` or `"LSDisplayName"="iTerm2"`


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
        # pid -> (ancestors nearest first, host, app bundle path, tty)
        self._info: dict[int, tuple[list[int], str, str | None, str | None]] = {}
        self._sessions: list[dict] = []
        self._public: list[dict] = []
        self.procs = True  # False while the last `ps` snapshot was unusable

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
        iterm = tuple((i["session_id"], i["window"], i["tab"], i["pane"], i["panes"], i["tty"], i["title"],
                       i["job"], i["path"], i["current"]) for i in ctx.iterm_sessions)
        return (tuple(files), ctx.iterm_connected, ctx.iterm_ttys, ctx.iterm_focus_tty,
                ctx.front_app == "iTerm2", bridge.version if bridge else 0, iterm)

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
        # a single ps snapshot, only when some pid is not cached yet
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
            if host == "iterm":
                controllable = bool(ctx.iterm_connected and tty and tty in ctx.iterm_ttys)
                focused = bool(tty and tty == ctx.iterm_focus_tty and ctx.front_app == "iTerm2")
            elif host == "vscode" and bridge is not None:
                controllable = bridge.match(ancestors) is not None
                focused = bridge.is_focused(ancestors)
            else:
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
        result.sort(key=lambda s: (not s["controllable"], s["status"] != "busy", -(s["statusSince"] or 0)))
        live = {s["pid"] for s in result}
        result = merge_tiles(result, ctx.iterm_sessions, ctx.front_app)
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
