# Changelog

All notable changes to this project. Versions before 0.3.0 were released under the name *Claude Deck*.

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
