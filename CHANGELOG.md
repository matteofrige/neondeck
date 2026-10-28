# Changelog

All notable changes to this project. Versions before 0.3.0 were released under the name *Claude Deck*.

## 0.7.2

- Network log: every time the LAN listener goes down it is now logged, also when the same state was already seen before (a short network detection failure after waking from sleep used to close the listener silently). When no network is detected the log says why (`no_route`, `no_arp`, `localnet_permission`).

## 0.7.1

- The phone page is served with `Cache-Control: no-cache`, so the app installed on the iOS home screen picks up agent updates instead of keeping a stale copy.
- The phone page inlines its home-screen icon, so iOS shows it also over HTTPS with the local CA.

## 0.7.0

- Optional HTTPS with a local CA: `neondeck tls enable` creates a private CA (EC P-256, 10 years) and a server certificate for `neondeck.local` and the computer's Bonjour name. The CA is name-constrained (critical): it can only sign `.local` names and private/loopback IP ranges, so a stolen CA key cannot impersonate public sites. The default stays plain HTTP. Other commands: `neondeck tls [status|disable|rotate]`.
- The server certificate (397 days) is renewed automatically when it is close to expiry, when the LAN IP or the names change, or when the CA is rotated.
- Setup page on port 8786 (plain HTTP, same network checks): iOS configuration profile (`.mobileconfig`) and a `.crt` for Android; any other path on that port redirects to the HTTPS address. The Bonjour record and the admin info carry the TLS state.
- Live camera view for scanning the QR code when the phone page is served over HTTPS (browsers only allow `getUserMedia` on secure origins); the photo-based scan stays as a fallback.
- Phone layout: the side-by-side layout now applies in every landscape orientation (before, only from 560 px wide).
- New dependency: `cryptography`.

## 0.6.0

- Stable phone address `http://neondeck.local:8787`, announced over mDNS (configurable with `lan_hostname`). The installed PWA keeps its origin, and its token, across networks and IP changes. The QR code uses the name; the IP URL stays available as a fallback (`neondeck qr`, admin panel).
- The web manifest carries the token in `start_url` when requested with a valid one (throttled like the device endpoints), so iOS home-screen apps no longer ask for the token again.
- The phone token screen can scan the QR code from a photo (iOS camera via a file input, decoded locally with the bundled jsQR 1.4.0, Apache-2.0). A live camera view needs HTTPS, which the LAN page does not have.
- Pairing without typing the token: the phone token screen has an ABBINA button that shows a 6-digit code, which you approve on the computer in the admin panel, with `neondeck pair [code]`, or from the macOS notification that points to them. Requests expire after 2 minutes, only one is kept per IP (5 in total, 5 creations per IP per 10 minutes), and the network checks are unchanged. Pasting the token and the QR photo scan remain as alternatives.

## 0.5.2

- iTerm2: fixed a reconnection leak. After a disconnect the library's stale `App` singleton and the unclosed websockets exhausted the process file descriptors, which also broke network detection so phones saw "rete non fidata".
- The server raises its file descriptor soft limit to 4096 at startup (launchd defaults to 256).
- Phone layout: tiles no longer overlap, and landscape phones from 560 px wide use the side-by-side layout.

## 0.5.1

- macOS: `neondeck install` waits for launchd to finish unloading the previous agent before loading the new one, and retries once on `Bootstrap failed: 5: Input/output error`.

## 0.5.0

- **Visual redesign: dot-matrix LED.** Device/phone UI and admin now use an authored 5x7 dot-matrix face drawn as round LEDs over an unlit dot field (header counter "TOCCA A TE", amber only for sessions waiting for you, magenta for the brand and the selection). Sessions are sorted waiting first; long names scroll like a ticker; a tile whose state changes re-draws with a wipe; `prefers-reduced-motion` is respected.
- New icon type `{"type": "glyph", "value": "<name>"}` (14 authored 11x11 pictograms, picked in the admin editor under "Simbolo LED"). The default buttons use it; existing buttons that still carry the old default emoji are upgraded on load, other emoji and images are untouched.
- App icons (PNG) and `docs/assets/logo.svg`/`banner.svg` redrawn as LED dots; shared glyph table in `neondeck/ledfont.py` (embedded in both HTML files, kept identical by tests). README banner and screenshots regenerated from the demo; design system documented in `DESIGN.md`.

## 0.4.0

- **Linux support.** The agent now runs on Linux as well as macOS. OS-specific code lives in `neondeck/platform/` (`macos.py`, `linux.py`), selected by `sys.platform`; other systems stop at startup with "Piattaforma non supportata".
  - Process table from `/proc` (tty from `tty_nr`, exe or comm), frontmost app and window raising through `xdotool` on X11 (Wayland: skipped, focus comes from the terminals), network detection with `ip -j` and `/proc/net` fallbacks (gateway MACs are cross-platform), `xdg-open`, `bash -lc` for shell buttons, `XDG_CONFIG_HOME`.
  - `neondeck install` writes a systemd user unit (`~/.config/systemd/user/neondeck.service`) and enables it; `status`, `restart` and `uninstall` use systemd. Without a systemd user instance it explains how to run `neondeck run`. Logs: `journalctl --user -u neondeck -f`.
  - VS Code / Cursor CLI discovery and window raising on Linux (`code`, `code-insiders`, `codium`, `cursor`, `/usr/share/code/bin/code`, `/snap/bin/code`).
- **Terminal backends** (`neondeck/terminals/`): iTerm2 (adapter around the existing client, imported lazily so Linux never loads `iterm2`), **tmux**, **WezTerm** and **kitty**, on macOS and Linux. Claude sessions are matched to panes by tty (kitty: by shell pid); tile ids are `<backend>:<pane id>`; host badges `iT`, `TM`, `WZ`, `KT`, `VS`.
  - tmux: one `list-panes` + `list-clients` call per poll, `switch-client` / `select-window` / `select-pane` on the most recently active client, window raising (iTerm2 API, `open -a`, `xdotool`), keys sent as raw bytes with `send-keys -H` (tmux 3.0+). iTerm2 `tmux -CC` gateway sessions are hidden in favor of the tmux panes. Optional `tmux_socket`.
  - WezTerm through `wezterm cli`, kitty through `kitty @` (remote control must be enabled; optional `kitty_socket`). Availability of both is re-checked every 30 seconds.
  - Backends are only polled while a device is connected.
- `show_all_iterm` is now `show_all_terminals` (the old key is still read); admin label "Mostra tutte le sessioni dei terminali, non solo Claude Code".
- `neondeck status` lists the detected terminals and the VS Code bridge windows; `neondeck install --no-service` (`--no-launchd` still works).
- API: `/api/sessions` and the WebSocket state carry `terminals`; terminal tiles have `backend` and `paneId` instead of `itermSession`.
- The `iterm2` dependency is installed only on macOS.
- Tests: Linux parsers, tmux (including an integration test against a private tmux server, marker `tmux`), WezTerm, kitty, systemd, platform dispatch. New `scripts/linux-smoke.sh` (Docker) and a CI workflow on Ubuntu and macOS.

## 0.3.0

- Renamed to **Neondeck**: package, command (`neondeck`), data directory (`~/.config/neondeck`), `NEONDECK_HOME` (`CLAUDE_DECK_HOME` still honored), LaunchAgent label `io.neondeck.agent`, log `~/Library/Logs/neondeck.log`, Bonjour type `_neondeck._tcp`.
- First run copies the old `~/.config/claude-deck` settings (config, buttons, icons) with the same permissions; the old directory is left in place.
- `neondeck install` removes the legacy LaunchAgent and the legacy VS Code extension; `uninstall` removes both too.
- VS Code extension is now `neondeck.neondeck-bridge` ("Neondeck Bridge"), MIT licensed.
- New app icon and manifest generated from the pixel-art mascot.
- Demo mode: `/preview?demo=1` shows fake sessions and buttons with no connection; `docs/demo.html` is a statically hostable build (`scripts/build_demo.py`).
- Logo and banner (`scripts/build_assets.py`), full English and Italian READMEs, MIT license.
- Test fixtures use neutral fake data.

## 0.2.2

- Fix: `Context` was built positionally and emptied the session list, causing a 500 on the `show_all_iterm` option.

## 0.2.1

- Network detection through `scutil` (configd): no Local Network permission needed for `trust`.
- Trusted networks can be managed from the admin panel.

## 0.2.0

- Idle without clients: no timers or process scans until a device connects.
- Admin key (`X-Admin-Key`) for the admin panel, trusted networks, Local Network permission guidance.
- All iTerm2 sessions shown as terminal tiles (window / tab / pane locations), optional via `show_all_iterm`.

## 0.1.1

- Cyberpunk android mascot replaces the previous tile artwork.

## 0.1.0

- First release: macOS agent, Stream Deck-style web UI, VS Code bridge extension and CLI (LaunchAgent install, token, QR code).
