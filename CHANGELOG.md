# Changelog

All notable changes to this project. Versions before 0.3.0 were released under the name *Claude Deck*.

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
