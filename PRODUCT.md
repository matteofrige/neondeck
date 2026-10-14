# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

Developers who run many Claude Code sessions in parallel across projects — in iTerm2 windows/tabs, tmux panes, WezTerm/kitty, and VS Code integrated terminals — on macOS or Linux. They use Neondeck as a physical or phone dashboard next to their main screen.

## Product Purpose

Neondeck shows every active Claude Code session (and optionally every terminal pane) as a grid of Stream Deck–style tiles, and lets the user bring any session to the front and send it keys or run custom actions without touching the keyboard. Two jobs carry equal weight:

- **Glance:** a passive status board on the desk — which sessions are working, which are idle or waiting for the user, which are stopped.
- **Act:** an active remote — tap a session to focus its window/tab/pane, then press Esc/Enter/Interrupt or a custom button.

Success: the user stops alt-tabbing through terminals to find the session that needs them.

## Positioning

A local agent reads Claude Code's own session registry plus the terminal hosts' APIs (iTerm2, tmux, WezTerm, kitty, a VS Code bridge extension), so it knows each session's real state and can focus the exact pane — not just an app launcher with hotkeys. Clients are a phone PWA and a dedicated Raspberry Pi touchscreen device.

## Operating Context

- Clients: Raspberry Pi Zero 2 W with an 800x480 touchscreen (Pimoroni HyperPixel 4.0), always on, glanced at from arm's length; phone (portrait and landscape) as a home-screen web app; desktop browser for the admin panel.
- The admin panel runs only on the computer (localhost) and is used occasionally: trusted networks, custom buttons (title, image/emoji, color, action), QR pairing, settings.
- Sessions have states: busy (working), idle, shell (running a command), plus non-controllable sessions and plain terminal panes.

## Capabilities and Constraints

- Everything inline and offline: no CDN, no external fonts, no network dependency in the UI.
- Must stay light enough for a Pi Zero 2 W (512 MB RAM) browser or a future native client mirroring the same design.
- UI language: Italian (README is English + Italian).
- Square Stream Deck tiles for both sessions and action buttons; large touch targets.
- `docs/demo.html` is generated from `src/neondeck/static/preview.html` by `scripts/build_demo.py`; demo mode `?demo=1`.
- App icons are generated in Python inside the server (no image files shipped for them).
- Neondeck is independent and not affiliated with Anthropic; no Anthropic logos or marks.

## Brand Commitments

- Name: Neondeck.
- Black background; cyberpunk character, modern and striking.
- An original pixel-art android mascot represents Claude sessions (may be reworked); a pixel monitor glyph represents plain terminals.

## Evidence on Hand

- Real UI: `src/neondeck/static/preview.html`, `src/neondeck/static/admin.html`; demo screenshots in `docs/assets/`; banner and logo in `docs/assets/`.
- No testimonials, user counts, or benchmarks exist; do not invent them. Measured footprint: ~57 MB RAM, ~0.6% CPU with one client connected.

## Product Principles

1. State before decoration: a session's status must read at arm's length in under a second.
2. One tap does one thing: focusing a session and pressing an action are never ambiguous.
3. Local and private by default: nothing leaves the user's network.
4. The device is a gadget people want on their desk; it should feel like a crafted object, not a web page.
