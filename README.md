<p align="center">
  <img src="docs/assets/banner.png" alt="Neondeck: a Stream Deck for your Claude Code sessions" width="100%">
</p>

<p align="center">
  <b>A Stream Deck-style remote for your Claude Code sessions.</b><br>
  See every session at a glance, jump to it with one tap, and fire your own buttons from a phone or a Raspberry Pi.
</p>

<p align="center">
  <a href="https://github.com/matteofrige/neondeck/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/matteofrige/neondeck/actions/workflows/ci.yml/badge.svg"></a>
  <a href="LICENSE"><img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-ff3cc7?style=flat-square"></a>
  <img alt="Python 3.12+" src="https://img.shields.io/badge/python-3.12%2B-19f0ff?style=flat-square">
  <img alt="macOS and Linux" src="https://img.shields.io/badge/platform-macOS%20%7C%20Linux-000000?style=flat-square">
  <img alt="iTerm2, tmux, WezTerm, kitty" src="https://img.shields.io/badge/terminals-iTerm2%20%C2%B7%20tmux%20%C2%B7%20WezTerm%20%C2%B7%20kitty-1c1c1c?style=flat-square">
  <img alt="VS Code" src="https://img.shields.io/badge/VS%20Code-bridge-007acc?style=flat-square&logo=visualstudiocode&logoColor=white">
</p>

<p align="center">
  <a href="README.it.md">🇮🇹 Italiano</a> · <a href="https://matteofrige.github.io/neondeck/demo.html">Live demo</a> · <a href="CHANGELOG.md">Changelog</a>
</p>

---

Neondeck is a small agent for macOS and Linux plus a touch-friendly web UI. If you run many [Claude Code](https://claude.com/claude-code) sessions at once, spread over iTerm2, tmux, WezTerm or kitty panes and VS Code integrated terminals, it turns them into a grid of tiles on your phone or on a small Raspberry Pi screen: which ones are working, which ones are waiting for you, which one is which. Tap a tile and that exact terminal comes to the front on your computer. Then use a row of customizable buttons (send keys, run a shell command, open a URL, jump to a session) to answer, interrupt, switch mode or deploy without touching the keyboard.

<p align="center">
  <img src="docs/assets/screenshot-landscape.png" alt="Neondeck on an 800x480 device" height="300">
  &nbsp;&nbsp;
  <img src="docs/assets/screenshot-phone.png" alt="Neondeck on a phone" height="300">
</p>

> **Not affiliated with Anthropic.** Neondeck is an independent project. It reads the local session files that Claude Code writes and controls your terminals; it does not talk to any Anthropic service. See [License and disclaimer](#license-and-disclaimer).

## Features

- 🟢 **Live session grid.** One tile per session with its status: working (busy), idle or waiting for you, or a plain shell. Tiles update as things happen.
- 👆 **Tap to focus.** Brings the right window, tab *and* split pane to the front in iTerm2, tmux, WezTerm or kitty, or the right terminal in VS Code.
- 🖥️ **Every terminal pane, not only Claude.** `zsh`, `vim`, `ssh`, `htop`... each pane gets a tile (can be switched off).
- 🐧 **macOS and Linux.** The same agent runs as a LaunchAgent on macOS and as a systemd user service on Linux. tmux works on both and also covers terminals without an API of their own (Terminal.app, Ghostty, Alacritty, GNOME Terminal, Konsole...).
- 🧩 **VS Code integrated terminals** through a tiny bridge extension (Cursor works best-effort).
- 🎛️ **Custom Stream Deck buttons.** Send keys, run a shell command, open a URL or jump to a session by name, with emoji or uploaded images and optional colors.
- 📱 **Phone PWA with QR pairing.** Scan once, add it to your Home Screen, done.
- 🥧 **Raspberry Pi device.** An 800x480 touch screen layout, designed for a Pi Zero 2 W (see [Building the Raspberry Pi device](#building-the-raspberry-pi-device)).
- 🔒 **Security by default.** The network listener only exists on networks you trust, an admin key protects the settings, failed logins are throttled, and a device can only press buttons you defined (it can never send arbitrary text or commands). Details in [Security](#security).
- 🪶 **Tiny footprint.** About 57 MB of RAM, essentially zero CPU when nobody is connected, and it never keeps your computer awake.

## How it works

```mermaid
flowchart LR
    subgraph Mac["Your Mac or Linux box"]
        S["~/.claude/sessions/*.json<br/>+ process table"] --> A(("Neondeck<br/>agent"))
        A <-->|"iTerm2 Python API (macOS)"| IT["iTerm2"]
        A <-->|"CLI / remote control"| TM["tmux · WezTerm · kitty"]
        A <-->|"Unix socket (JSON lines)"| VS["VS Code / Cursor<br/>bridge extension"]
        A -->|"localhost only"| AD["Admin panel"]
    end
    A <-->|"WebSocket over LAN<br/>trusted networks only"| P["Phone PWA"]
    A <-->|"WebSocket over LAN<br/>trusted networks only"| PI["Raspberry Pi"]
```

The agent reads the session files that Claude Code keeps in `~/.claude/sessions`, walks the process tree (`ps` on macOS, `/proc` on Linux, once per cycle at most) to find which terminal hosts each session, and asks the terminal (iTerm2 Python API, `tmux`, `wezterm cli`, `kitty @`) or the VS Code bridge (Unix socket) to focus a pane or type into it. Claude sessions are matched to panes by tty. Devices connect over a WebSocket, receive the full state on every change and send back two kinds of requests: *focus this tile* and *press this button*.

## Requirements and platform support

- macOS or Linux (Python 3.12+). On macOS the agent uses `launchd`, `scutil`, `lsappinfo` and `open`; on Linux it reads `/proc` and uses `ip` (iproute2), `systemd --user` and `xdg-open`. Other systems exit at startup with "Piattaforma non supportata" (Windows is a possible future target).
- [uv](https://docs.astral.sh/uv/) to install it.
- At least one supported terminal (see below). iTerm2 needs the **Python API enabled**: *iTerm2 › Settings › General › Magic › Enable Python API*; iTerm2 asks for permission the first time the agent connects.
- Optional: VS Code and/or Cursor for the integrated terminal bridge; `xdotool` on Linux/X11 to raise windows.
- A phone or a Raspberry Pi on the same Wi-Fi network.

| Terminal | macOS | Linux | How it is driven |
|---|:-:|:-:|---|
| iTerm2 | yes | n/a | Python API |
| tmux | yes | yes | `tmux` CLI on the default server (any terminal emulator can host it) |
| WezTerm | yes | yes | `wezterm cli` |
| kitty | yes | yes | `kitty @` remote control (must be enabled, see [kitty](#kitty)) |
| VS Code / Cursor | yes | yes | bridge extension |
| Terminal.app, Ghostty, Alacritty | via tmux | via tmux (Ghostty, Alacritty) | run `claude` inside tmux |
| GNOME Terminal, Konsole, others | n/a | via tmux | run `claude` inside tmux |

Linux support is new in 0.4.0. The test suite (including a real tmux server) runs on Ubuntu and macOS in CI; raising desktop windows is best effort (X11 with `xdotool`; Wayland cannot do it, see [Linux notes](#linux-notes)).

## Quick start

```bash
uv tool install git+https://github.com/matteofrige/neondeck
neondeck install
neondeck qr
```

`neondeck install` creates the background service (a LaunchAgent on macOS, log in `~/Library/Logs/neondeck.log`; a systemd user service on Linux, see [Linux notes](#linux-notes)), installs the extension into the VS Code / Cursor builds it finds, marks the current network as trusted if you have none yet, and prints the phone address with a QR code. Scan it with your phone while on the same Wi-Fi.

No camera, or no wish to type the token? Open the address on the phone and tap **ABBINA**: the phone shows a 6-digit code, and you approve it on the computer in the admin panel, with `neondeck pair`, or from the macOS notification that points to them. Requests expire after 2 minutes and the usual network checks apply; pasting the token and the QR photo scan stay available.

On Linux:

```bash
uv tool install git+https://github.com/matteofrige/neondeck.git
neondeck install      # writes ~/.config/systemd/user/neondeck.service and enables it
journalctl --user -u neondeck -f
```

> 💡 **Add to Home Screen.** On iPhone: Share › *Add to Home Screen* (on Android: menu › *Install app*). It opens full screen like a native app. The QR code points at `http://neondeck.local:8787` (announced over mDNS, name configurable with `lan_hostname` in `config.json`), so the installed app keeps working when the Mac's IP changes; if the name does not resolve, use the IP URL printed by `neondeck qr`. The app's storage is separate from Safari, but the manifest carries the token, so it normally starts already paired; if it still asks, paste the output of `neondeck token`.

Try it without installing anything: the [live demo](https://matteofrige.github.io/neondeck/demo.html) runs the UI with fake data.

<details>
<summary><b>Update, uninstall, upgrading from "Claude Deck"</b></summary>

```bash
uv tool upgrade neondeck && neondeck install      # update
neondeck uninstall                                # keeps your settings
neondeck uninstall --purge                        # also deletes ~/.config/neondeck
uv tool uninstall neondeck
```

Neondeck used to be called *Claude Deck*. On first run, if `~/.config/neondeck` does not exist but `~/.config/claude-deck` does, the settings (`config.json`, `buttons.json`, `icons/`) are copied over with the same permissions and the old directory is left in place. `neondeck install` also removes the old LaunchAgent and the old VS Code extension (macOS). The `CLAUDE_DECK_HOME` environment variable is still honored as a fallback for `NEONDECK_HOME`. Phones paired before the rename keep working; Bonjour clients must now look for `_neondeck._tcp`.

</details>

## CLI reference

Run `neondeck` with no arguments to start the server in the foreground (same as `neondeck run`).

| Command | Description |
|---|---|
| `neondeck` / `neondeck run` | Run the agent in the foreground. |
| `neondeck install [--no-vscode] [--no-service]` | Install the background service (LaunchAgent on macOS, systemd user unit on Linux) and the VS Code / Cursor extension, trust the current network if none is trusted yet, show the QR code. `--no-launchd` is kept as an alias of `--no-service`. On macOS it also removes the legacy *Claude Deck* agent and extension. |
| `neondeck uninstall [--purge]` | Remove the service and the extension. `--purge` also deletes the data directory. |
| `neondeck status` | Service, server, LAN listener, Bonjour, network trust, detected terminals (`tmux: attivo (3 sessioni, 7 pannelli)`, `WezTerm: non trovato`, `kitty: remote control disattivato`, `iTerm2: ...`, `VS Code bridge: n finestre`), bridge socket and extension status. |
| `neondeck restart` | Restart the agent (`launchctl kickstart -k` or `systemctl --user restart`). |
| `neondeck qr` | Print the phone address and a QR code. |
| `neondeck pair [CODE]` | Approve a phone pairing request: lists the pending ones and asks for each, or approves `CODE` directly. |
| `neondeck token [--rotate] [--admin]` | Print the device token (or the admin key with `--admin`); `--rotate` generates a new one. |
| `neondeck trust [--name NAME]` | Mark the current network as trusted. |
| `neondeck networks` | List trusted networks and mark the current one. |
| `neondeck untrust <MAC\|NAME\|N>` | Remove a trusted network by MAC address, name or list number. |
| `neondeck admin` | Open the admin panel in your browser (`open` / `xdg-open`; the URL is printed if neither exists). The key is passed in the URL fragment. |
| `neondeck vsix [-o PATH]` | Build the `.vsix` package of the VS Code extension. |
| `neondeck --version` | Print the version. |

Commands that edit `config.json` (`trust`, `untrust`, `token --rotate`) are picked up by the running agent without a restart.

## Configuration

Everything lives in `~/.config/neondeck/` (override with `NEONDECK_HOME`; on Linux `XDG_CONFIG_HOME` is honored). The directory is created with mode 700 and `config.json` with mode 600.

<details>
<summary><b><code>config.json</code> fields</b></summary>

| Field | Default | Meaning |
|---|---|---|
| `port` | `8787` | HTTP / WebSocket port. |
| `token` | generated | Secret used by devices (header `Authorization: Bearer`, or `?token=` on the WebSocket). |
| `admin_key` | generated | Secret for the admin panel (`X-Admin-Key`). |
| `trusted_networks` | `[]` | Networks where the LAN listener is allowed: `{name, mac, subnet, added}`, identified by the gateway's MAC address. Manage with `neondeck trust` / `untrust` or from the admin panel. |
| `bind` | unset | Set to `127.0.0.1` for local-only mode: the LAN listener never starts. |
| `show_all_terminals` | `true` | Show every terminal pane (iTerm2, tmux, WezTerm, kitty) as a tile, not only Claude Code ones (also a toggle in the admin panel). The pre-0.4 key `show_all_iterm` is still read when the new one is absent. |
| `tmux_socket` | unset | Path of the tmux server socket to use instead of the default `$TMUX_TMPDIR/tmux-<uid>/default`. Needed when your shell sets `TMUX_TMPDIR` or you use `tmux -L`. Read at startup. |
| `kitty_socket` | unset | kitty `--to` address (for example `unix:/tmp/kitty-1234`) instead of auto-discovery of `/tmp/kitty*`. Read at startup. |

</details>

<details>
<summary><b><code>buttons.json</code> schema and examples</b></summary>

An array of buttons. Every button has `id`, `title` (1 to 40 characters), `icon` (`{"type": "glyph", "value": "rocket"}` for a dot-matrix LED symbol (`esc`, `enter`, `stop`, `mode`, `tab`, `up`, `down`, `rocket`, `book`, `bolt`, `terminal`, `refresh`, `play`, `check`), `{"type": "emoji", "value": "🚀"}` or `{"type": "image", "value": "icons/name.png"}`), an optional `color` (`#rrggbb`) and an `action`. The easiest way to edit them is the admin panel (`neondeck admin`), which also uploads images (png, jpg, gif, webp or svg, up to 2 MB) into `icons/`.

The first run creates four buttons: Esc, Enter, Interrupt (Ctrl+C) and Switch mode (Shift+Tab).

```json
[
  {
    "id": "enter",
    "title": "Invio",
    "icon": { "type": "glyph", "value": "enter" },
    "action": { "type": "send_keys", "text": "\r", "target": "current" }
  },
  {
    "id": "deploy",
    "title": "Deploy",
    "icon": { "type": "emoji", "value": "🚀" },
    "color": "#ff3cc7",
    "action": { "type": "shell", "command": "./deploy.sh", "timeout": 60 }
  },
  {
    "id": "docs",
    "title": "Docs",
    "icon": { "type": "image", "value": "icons/docs.png" },
    "action": { "type": "open_url", "url": "https://example.com/docs" }
  },
  {
    "id": "jump-infra",
    "title": "Infra",
    "icon": { "type": "emoji", "value": "🏗️" },
    "action": { "type": "focus_session", "match": "infra" }
  }
]
```

| Action | Fields | Behavior |
|---|---|---|
| `send_keys` | `text`, `target` = `current` or `selected` | Types the text into a session, without pressing Enter unless `text` contains `\r`. `current` is the session in front (the active pane of the frontmost terminal, or the active terminal of the focused VS Code window), falling back to the last tile focused from a device; `selected` is the tile selected on the device. |
| `shell` | `command`, `timeout` (1 to 600 s, default 10) | Runs `/bin/zsh -lc <command>` (macOS) or `/bin/bash -lc <command>` (Linux) on the computer and reports the exit code and the last 500 characters of output. |
| `open_url` | `url` (needs a scheme) | Runs `open <url>` (macOS) or `xdg-open <url>` (Linux). |
| `focus_session` | `match` | Focuses the first session whose name or project contains the text. Works on terminal tiles too. |

</details>

## Session tiles

<details open>
<summary><b>Reading the grid</b></summary>

- **Who needs you.** Amber is reserved for one meaning: a Claude Code session waiting for you ("TOCCA A TE", its status word blinking like a departing train). The header shows how many there are as one large amber number, and those tiles are sorted first.
- **Other states.** White LEDs at reduced intensity: *in corso* (working, with three chasing dots), *comando* (a shell command, marked with `$`), terminal panes (a monitor glyph and the running program's name). View-only sessions are dimmed; empty slots show unlit modules. A tile whose state changes redraws with a left-to-right LED wipe.
- **Outlines.** A thin white outline marks the session currently in front on the computer; a magenta outline marks the tile you selected on the device (the target of `selected` buttons).
- **Host code.** Bottom right of each tile: `IT2` = iTerm2, `TMX` = tmux, `WZT` = WezTerm, `KTY` = kitty, `VSC` = VS Code / Cursor.
- **Location label.** iTerm2: `F1·T2` = window 1, tab 2; `F1·T2·P2` adds pane 2 when the tab is split (windows follow iTerm2's own order). tmux: `tmux work:2.1` = session `work`, window 2, pane 1. WezTerm and kitty: `wezterm 1:2.1` / `kitty 1:2.1` = window 1, tab 2, pane 1.
- **Subtitle.** Time in the current status for Claude tiles ("3 min"); the current folder or location for terminals.
- **Non-controllable tiles.** Sessions running in a terminal the agent cannot drive (Terminal.app or Ghostty without tmux, a VS Code window without the bridge, ...) are dimmed, show "non comandabile" and cannot be selected.
- **Order.** Controllable Claude sessions first, then terminals by window / tab / pane, non-controllable sessions last. If a pane runs a Claude Code session, only the Claude tile is shown. iTerm2 sessions that just host `tmux -CC` are hidden: the tmux panes behind them have their own tiles.

</details>

## Terminals

The agent polls the terminals only while a device is connected, and only those that are available.

### tmux

tmux works on macOS and Linux and is the way to control terminal emulators that have no API of their own (Terminal.app, Ghostty, Alacritty, GNOME Terminal, Konsole...).

- **Run `claude` inside tmux** (`tmux new -s work`, then `claude`). Every pane becomes a tile; a Claude session is matched to its pane by tty.
- The agent talks to the default server, `$TMUX_TMPDIR/tmux-<uid>/default` (`/tmp` when unset). A service does not see `TMUX_TMPDIR` from your shell profile and cannot guess `tmux -L name` sockets: set `tmux_socket` in `config.json` for those. With no server socket the agent does not even spawn `tmux`.
- **Tap a tile** and the agent selects the window and pane, switches the most recently active tmux client to that session (`switch-client`), and then raises the terminal window that hosts that client: through the iTerm2 API, `open -a` on macOS, `xdotool` on Linux/X11 (Wayland: the pane is selected but the window does not move). With no client attached the pane is still selected and the UI reports "Nessun client tmux collegato".
- Keys are typed as raw bytes (`send-keys -H`, tmux 3.0 or newer), so Esc, Ctrl+C and Shift+Tab buttons work. Older tmux falls back to literal keys with a warning.
- **iTerm2 `tmux -CC`:** the iTerm2 session that runs the control client is hidden and the tmux panes get the tiles.

### WezTerm

Needs `wezterm` in `PATH` and a running WezTerm window. The agent uses `wezterm cli list`, `activate-pane` and `send-text --no-paste`; the focused pane comes from `wezterm cli list-clients`. If the agent runs as a service it finds the GUI through WezTerm's default socket; if `wezterm cli list` works in a terminal but not for the agent, run the agent from that terminal with `WEZTERM_UNIX_SOCKET` set.

### kitty

kitty only answers `kitty @` when remote control is on. Add to `kitty.conf` and restart kitty:

```conf
allow_remote_control socket-only
listen_on unix:/tmp/kitty
```

kitty may append its process id to the socket name (for example `/tmp/kitty-1234`): the agent tries `$KITTY_LISTEN_ON` and every socket matching `/tmp/kitty*`, or the address in `kitty_socket`. Until remote control works, `neondeck status` shows "kitty: remote control disattivato". kitty does not expose a tty, so Claude sessions are matched to windows by the shell's process id. Keys are sent as is (`send-text --stdin`).

## Linux notes

- **Service.** `neondeck install` writes `~/.config/systemd/user/neondeck.service` (`ExecStart=<neondeck> run`, `Restart=on-failure`, `PATH` including `~/.local/bin`, `NEONDECK_HOME` when set), then runs `systemctl --user daemon-reload` and `enable --now`. Logs: `journalctl --user -u neondeck -f`. The user service runs only while you are logged in; to run it without a login session (a headless box) use `sudo loginctl enable-linger $USER`. Without a systemd user instance (containers, some WSL setups) `neondeck install` explains it and you run `neondeck run` yourself.
- **Firewall.** Devices connect to the port in `config.json` (8787). If `ufw` is active, allow your LAN only: `sudo ufw allow from 192.168.1.0/24 to any port 8787 proto tcp` (use the subnet `neondeck networks` shows). Neondeck still refuses requests from outside the trusted network's subnet.
- **Network detection** uses `ip -j route`/`neigh`/`addr` (iproute2) and falls back to `/proc/net/route` and `/proc/net/arp`. Trusted networks are identified by the gateway MAC, in the same format as on macOS.
- **Windows and focus.** On X11 install `xdotool`: the agent uses it to tell which app is in front and to raise a terminal window. Under Wayland there is no portable way to do either, so Neondeck skips them silently: the "focused" outline then follows what each terminal reports (tmux, WezTerm and kitty know their active pane), and tapping a tile selects the pane without moving the window. If the service does not see your display, import it once: `systemctl --user import-environment DISPLAY XAUTHORITY`.
- **Sessions.** `ps` is not used; the process table comes from `/proc`.

## VS Code bridge

VS Code has no external API for its integrated terminal, so `neondeck install` installs a small extension (plain JavaScript, no dependencies, no build step; `src/neondeck/vscode_bridge/`). It reports every terminal's shell PID over a Unix socket at `<NEONDECK_HOME>/bridge.sock` (mode 600 inside a mode 700 directory: no network, no token). The agent matches a Claude Code session to a terminal when that terminal's shell is among the session's parent processes, and then asks the extension to show the terminal or type into it. Requests time out after 3 seconds.

After installing, run **Developer: Reload Window** in each editor window. The extension logs to the **Neondeck** Output channel, and the **Neondeck: riconnetti** command forces a reconnect (it also retries by itself every 1 to 15 seconds). Without a CLI, run `neondeck vsix` and use *Extensions: Install from VSIX...*.

Limitations:

- The extension API cannot raise a window, so after focusing the terminal the agent runs the editor's CLI on the workspace folder (or `open -a` when there is none, macOS). On Linux it looks for `code`, `code-insiders`, `codium` or `cursor` in `PATH`, `/usr/share/code/bin/code` and `/snap/bin/code`; a window manager that refuses focus stealing may leave the window in the background. With several windows on the same folder, the wrong one may come to the front. Set `NEONDECK_NO_RAISE=1` to skip this step.
- Cursor is best-effort (same extension, `cursor` CLI).
- Remote windows (SSH, containers, WSL) are not supported.

## Security

Neondeck lets a device press keys in your terminals, so it is built to be reachable by as few things as possible.

| Who | What they can try | Mitigation |
|---|---|---|
| Someone on **your trusted Wi-Fi** | Sniff or guess the token, then list sessions and press your buttons | Token is 24 random bytes; 5 wrong tokens in 10 minutes block the IP for 10 minutes (429 / close code 4429); the device can only press predefined buttons, never send free text or commands; shell buttons can only be defined from the admin panel. Rotate with `neondeck token --rotate`. |
| Someone on **public or untrusted Wi-Fi** | Reach the agent | There is nothing to reach: the agent only listens on loopback, and opens the LAN listener while the computer is on a trusted network (identified by the gateway MAC). When the network changes the socket is closed, clients get close code 4403 and Bonjour is withdrawn. Requests are also refused if the gateway MAC cannot be determined (fail closed) or the client IP is outside the computer's subnet. |
| A **local process or user** on your computer | Read the token, talk to the bridge | `config.json` is mode 600 in a mode 700 directory; the bridge socket is only reachable by your user. Anything running as you can read your files anyway. |
| A **malicious website** in your browser | Hit `localhost:8787` (CSRF, DNS rebinding) to use the admin panel | `/api/admin/*` needs the `X-Admin-Key` header (never sent by a simple cross-site request), the admin is localhost-only judged on the socket address (never `X-Forwarded-For`), and `Host` / `Origin` are checked against DNS rebinding. |

Other details: icons are limited to 2 MB and to image types, and SVGs are served with `Content-Security-Policy: default-src 'none'`. Connections, wrong tokens, blocks and refused networks are logged.

**Be honest about the limits.**

- Traffic between the device and the computer is **plain HTTP** on your LAN. Anyone who can sniff that network can read the token. That is why the listener only runs on networks you explicitly trust.
- A button that sends **Enter can approve a Claude Code permission prompt**. Anybody holding the token can do that. Treat the token like a password.

Recommendations:

- Rotate the token if it was shared or seen: `neondeck token --rotate`.
- Never trust public networks (cafes, hotels, coworkings, shared hotspots).
- On a trusted network that is not really yours, turn on the macOS firewall *stealth mode* (System Settings › Network › Firewall › Options).
- On Linux, allow the port only from your LAN subnet in the firewall (see [Linux notes](#linux-notes)).
- For preview-only use on the computer, set `"bind": "127.0.0.1"` in `config.json`.

## macOS notes

- **Local Network permission** is only needed for **Bonjour** announcements (auto-discovery by the Pi). Network detection (gateway MAC, IP, subnet) reads configd through `scutil`, which macOS does not filter, so `neondeck trust` works even with uv's managed Python. Only if configd gives no gateway MAC does the agent fall back to `route` / `arp`, which recent macOS versions filter for binaries without the permission (`neondeck status` then shows `localnet_permission`). Without the permission the LAN listener still starts and devices connect by IP.
  To enable Bonjour: System Settings › Privacy & Security › **Local Network** › enable the Python binary named in the log message, then `neondeck restart`. After a uv Python upgrade the binary path changes and the permission must be granted again.
- **Sandboxed launches.** If you run Neondeck from inside Claude Code's `!` sandbox, `ps` is blocked: the agent cannot read processes and the UI shows "Processi non leggibili". Run it from a normal terminal or through `neondeck install`.
- The agent never prevents sleep and runs fine as a LaunchAgent in your login session. tmux, WezTerm and kitty work on macOS too; for Terminal.app and Ghostty run `claude` inside tmux.

## Resource usage

Measured: about **57 MB RSS** and about **0.6% CPU** with one client connected, and about 0 when idle. With no client connected the agent does nothing: no polling, no process scans, no `lsappinfo`, no `tmux` / `wezterm` / `kitty` calls, no iTerm2 scans; it refreshes immediately when the first client connects. The only always-on timer is the network check every 30 seconds. With clients connected, the frontmost app is read at most every 2 seconds (`lsappinfo`, or `xdotool` on Linux) and only if a terminal pane or VS Code session exists; iTerm2 details are read once per new session and then only when iTerm2 notifies a change; tmux costs one `tmux` call per second and WezTerm / kitty one or two calls per second, and only when they are running. It does not hold a power assertion, so it does not keep the computer awake.

## Building the Raspberry Pi device

The layout in `/preview` is designed for an 800x480 screen. Suggested hardware:

- **Raspberry Pi Zero 2 W**, small, cheap, Wi-Fi onboard.
- **Pimoroni HyperPixel 4.0 Touch** (800x480, capacitive touch, plugs on the GPIO header). The Zero has no DSI connector, which is why an official DSI display is not an option here.
- **PiSugar 3 Plus 5000 mAh** battery board.
- A microSD card.

If you prefer DSI displays, a Compute Module 4 on a suitable carrier is an alternative.

**Status: the native client is work in progress.** Until then, either run Chromium in kiosk mode on a Pi 4 or 5 pointed at `http://<agent-ip>:8787/preview?token=...&frame=1`, or just use your phone. `frame=1` forces the fixed 800x480 layout.

## WebSocket protocol reference

All device endpoints require a trusted network and the token (`Authorization: Bearer <token>`, or `?token=` on the WebSocket), except the two pairing endpoints, which need only the network.

<details>
<summary><b>HTTP endpoints</b></summary>

| Method | Path | Description |
|---|---|---|
| GET | `/api/sessions` | `{"sessions": [...], "iterm": bool, "terminals": [{"id", "label"}, ...]}` |
| GET | `/api/buttons` | `{"buttons": [...]}` |
| POST | `/api/focus` `{"id"}` or `{"pid"}` | Focus a tile (`id` is `claude:<pid>`, or `<backend>:<pane id>` for terminal tiles such as `iterm:<session>` or `tmux:%3`; `pid` only for Claude sessions). |
| POST | `/api/press` `{"id", "target_id"?, "pid"?}` | Press a button; `target_id` is the tile selected on the device. Returns `{"ok", "detail"}`. |
| POST | `/api/pair` | Pairing, no token (trusted network only): `{"ok", "id", "code", "expires_in"}`. Limited to 5 per IP per 10 minutes. |
| GET | `/api/pair/<id>` | Pairing status `pending` / `denied` / `approved` (with `token`); 404 once expired. |
| WS | `/ws?token=...` | See below. |
| GET | `/preview` | Remote / simulator web app (`?frame=1` fixed 800x480, `?demo=1` fake data). |
| GET | `/icons/<file>` | Button icons (no token). |

</details>

<details>
<summary><b>WebSocket messages</b></summary>

Server to client:

- `{"type": "state", "sessions": [...], "buttons": [...], "iterm": bool, "terminals": [{"id", "label"}, ...]}`: sent on connect and on every change; `terminals` lists the available backends.
- `{"type": "result", "ok": bool, "detail": str, "req": any}`: reply to `focus` / `press`; `req` is echoed back unchanged.
- `{"type": "pong"}`.

Client to server:

- `{"type": "focus", "id": "claude:123"}` (or `"pid"`).
- `{"type": "press", "id": "<button id>", "target_id"?: "<tile id>", "pid"?: n}`.
- `{"type": "ping"}`.

Application close codes: **4401** wrong token, **4403** untrusted network, **4429** too many attempts.

A session tile has: `kind` (`claude` or `terminal`), `id`, `pid` (null for terminals), `sessionId`, `name`, `cwd`, `project`, `status` (`busy`, `idle`, `shell`, `terminal`, ...), `statusSince` (ms), `startedAt`, `tty`, `host` (`iterm`, `tmux`, `wezterm`, `kitty`, `vscode`, `other`), `loc`, `controllable`, `focused`; terminal tiles also have `title`, `backend` and `paneId`.

Discovery: Bonjour service `_neondeck._tcp.local.` on the agent's port, TXT `v=1` (the token is never announced).

</details>

## Development

```bash
uv sync
uv run neondeck run          # run the agent from the checkout (no background service)
uv run pytest                # tests (the tmux integration test needs tmux and is skipped without it)
uv run neondeck vsix -o .    # build the VS Code extension package
```

- **Demo mode.** Open `/preview?demo=1` on a running agent, or `docs/demo.html` straight from disk: the UI shows fake sessions and buttons, with no WebSocket and no token. `docs/demo.html` is generated from `src/neondeck/static/preview.html` by `python scripts/build_demo.py` (a test checks that it is up to date) and can be hosted statically, for example with GitHub Pages.
- **Linux smoke test.** `scripts/linux-smoke.sh` builds `tests/docker/Dockerfile.linux` (Python 3.12 + tmux + iproute2), runs the whole test suite in it and then starts the agent against a real tmux, checking that a fake Claude session becomes a controllable `tmux` tile and that the other panes become terminal tiles. It prints PASS or FAIL for each check. CI (`.github/workflows/ci.yml`) runs the suite on Ubuntu and macOS.
- **Assets.** `python scripts/build_assets.py` regenerates `docs/assets/logo.svg` and `banner.svg`. The screenshots (`screenshot-landscape.png` 800x480, `screenshot-phone.png` 390x844) are taken from the demo with headless Chrome.

## Roadmap

- Native Raspberry Pi client (LVGL or pygame) instead of a browser
- Direct Terminal.app and Ghostty control (today: through tmux)
- Windows support
- Per-device tokens
- Homebrew tap
- Menu bar app
- Translated UI (it is currently Italian)

## Troubleshooting

<details>
<summary><b>A tile is grey and says "non comandabile"</b></summary>

The session runs in a terminal the agent cannot drive (anything other than iTerm2, tmux, WezTerm, kitty or VS Code / Cursor: run `claude` inside tmux), in a tmux server the agent cannot see (set `tmux_socket`), or in a VS Code window where the bridge is not connected. For VS Code: run *Developer: Reload Window*, then check the **Neondeck** Output channel and `neondeck status` (bridge socket and extension). Remote VS Code windows are not supported.

</details>

<details>
<summary><b>"Processi non leggibili" in the header</b></summary>

The agent could not read the process table (`ps` on macOS, `/proc` on Linux). This happens when it is started from inside a sandbox (for example Claude Code's `!` sandbox). Start it with `neondeck install` or from a normal terminal.

</details>

<details>
<summary><b>The phone cannot connect</b></summary>

Check in this order: the phone and the computer are on the same Wi-Fi; that network is trusted (`neondeck networks`, then `neondeck trust` from that network); the agent runs (`neondeck status`); the token is current (`neondeck token`). A "Rete non fidata" screen means close code 4403; "Troppi tentativi" means 5 wrong tokens and a 10-minute block.

</details>

<details>
<summary><b>tmux, WezTerm or kitty are not detected</b></summary>

`neondeck status` lists every terminal with its state. tmux: "nessun server in esecuzione" means there is no socket at the default path, so start tmux first or set `tmux_socket`. WezTerm: "non in esecuzione" means `wezterm cli list` fails for the agent. kitty: "remote control disattivato" means `allow_remote_control` / `listen_on` are missing from `kitty.conf` (see [kitty](#kitty)). Availability of WezTerm and kitty is re-checked every 30 seconds.

</details>

<details>
<summary><b>On Linux, tapping a tile selects the pane but the window does not come to the front</b></summary>

Raising windows needs X11 and `xdotool`. Under Wayland it is not possible; the pane is still selected and typing works. Under X11 check `which xdotool` and that the service sees `DISPLAY` (`systemctl --user import-environment DISPLAY XAUTHORITY`).

</details>

<details>
<summary><b>The phone cannot reach the agent on Linux</b></summary>

Besides the checks above, make sure the firewall lets your LAN reach the port (`sudo ufw status`, then `sudo ufw allow from <your-subnet> to any port 8787 proto tcp`).

</details>

<details>
<summary><b>iTerm2 shows as unavailable</b></summary>

(macOS only.) Enable *iTerm2 › Settings › General › Magic › Enable Python API* and accept the authorization prompt. The agent reconnects by itself; the session list keeps working in the meantime.

</details>

<details>
<summary><b>Bonjour is not active</b></summary>

Grant the Local Network permission to the Python binary (see [macOS notes](#macos-notes)). It is optional: devices can always connect by IP.

</details>

## License and disclaimer

[MIT](LICENSE) © 2026 Matteo Frigé.

The phone page bundles [jsQR](https://github.com/cozmo/jsQR) 1.4.0 (Apache-2.0, see `src/neondeck/static/vendor/jsQR.LICENSE`) to read the pairing QR code from a photo.

Neondeck is an independent project, not affiliated with or endorsed by Anthropic. Claude and Claude Code are trademarks of Anthropic. The mascot and all artwork in this repository are original.
