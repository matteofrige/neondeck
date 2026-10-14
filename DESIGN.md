---
name: Neondeck
description: A night departure board of LED dot-matrix tiles for Claude Code sessions, on a matte black ground.
colors:
  ground: "#050505"
  panel: "#0c0b09"
  unlit: "#1d1a14"
  rule: "#1a1814"
  tile-edge: "#16140f"
  field-edge: "#2a261e"
  amber: "#ffb000"
  amber-dim: "#6b4a00"
  led: "#e8eefc"
  led-mid: "rgba(232,238,252,.62)"
  led-low: "rgba(232,238,252,.32)"
  magenta: "#ff2e88"
  ok: "#36e07a"
  err: "#ff4033"
  prose: "#cfcabf"
  prose-2: "#8f8a80"
typography:
  display:
    fontFamily: "Neondeck LED 5x7 (authored dot-matrix bitmap, rendered as SVG)"
    fontSize: "42px"
    lineHeight: 1
  headline:
    fontFamily: "Neondeck LED 5x7 (authored dot-matrix bitmap, rendered as SVG)"
    fontSize: "28px"
    lineHeight: 1
  title:
    fontFamily: "Neondeck LED 5x7 (authored dot-matrix bitmap, rendered as SVG)"
    fontSize: "14px"
    lineHeight: 1
  label:
    fontFamily: "Neondeck LED 5x7 (authored dot-matrix bitmap, rendered as SVG)"
    fontSize: "11cqw"
    lineHeight: 1
  body:
    fontFamily: "ui-sans-serif, -apple-system, Segoe UI, Roboto, sans-serif"
    fontSize: "15px"
    fontWeight: 400
    lineHeight: 1.5
  body-small:
    fontFamily: "ui-sans-serif, -apple-system, Segoe UI, Roboto, sans-serif"
    fontSize: "13px"
    fontWeight: 400
    lineHeight: 1.35
  mono:
    fontFamily: "ui-monospace, Menlo, Consolas, monospace"
    fontSize: "13px"
    fontWeight: 400
rounded:
  hairline: "3px"
  module: "6px"
  lamp: "50%"
spacing:
  tile-gap: "8px"
  grid-pad: "10px"
  bar-x: "14px"
  board-gap: "16px"
  board-pad: "28px 24px"
  section: "28px"
  column-gap: "56px"
  header-height: "52px"
  touch-min: "56px"
components:
  tile-session:
    backgroundColor: "{colors.panel}"
    textColor: "{colors.led}"
    rounded: "{rounded.module}"
    size: "1:1 square, 1/4 of the sessions pane"
  tile-session-needs:
    backgroundColor: "{colors.panel}"
    textColor: "{colors.amber}"
    rounded: "{rounded.module}"
  tile-session-dim:
    backgroundColor: "{colors.panel}"
    textColor: "{colors.led-low}"
    rounded: "{rounded.module}"
  tile-action:
    backgroundColor: "{colors.panel}"
    textColor: "{colors.led}"
    rounded: "{rounded.module}"
    size: "1:1 square, 1/3 of the actions pane"
  button-primary:
    backgroundColor: "{colors.led}"
    textColor: "{colors.ground}"
    rounded: "{rounded.module}"
    height: "56px"
  button-secondary:
    backgroundColor: "transparent"
    textColor: "{colors.prose}"
    rounded: "{rounded.module}"
    padding: "7px 13px"
  button-danger:
    backgroundColor: "transparent"
    textColor: "{colors.err}"
    rounded: "{rounded.module}"
    padding: "7px 13px"
  input-gate:
    backgroundColor: "#000000"
    textColor: "{colors.prose}"
    rounded: "{rounded.module}"
    height: "56px"
    padding: "0 14px"
  input-admin:
    backgroundColor: "#000000"
    textColor: "{colors.prose}"
    rounded: "{rounded.module}"
    padding: "9px 11px"
  board:
    backgroundColor: "{colors.panel}"
    textColor: "{colors.prose}"
    rounded: "{rounded.module}"
    padding: "28px 24px"
    width: "min(440px, 100%)"
  toast:
    backgroundColor: "{colors.panel}"
    textColor: "{colors.prose}"
    typography: "{typography.body-small}"
    rounded: "{rounded.module}"
    padding: "8px 14px"
---

# Design System: Neondeck

## Overview

**Creative North Star: "The Night Departure Board"**

Neondeck is a station board hung in a dark city: every Claude Code session is a service listed on it, and the services waiting for the user are the ones boarding. Everything that carries state is drawn in one authored 5x7 LED dot-matrix face, lit dots over a faint field of unlit ones, on a matte black ground. The board is an object on the desk, read from a metre away; it is not a web dashboard.

Colour is spent like electricity. White LED at reduced intensity is the working light of the board; amber is lit only for sessions that need the user; magenta is the brand and the hand that marks a selection; green and red flash when something has just succeeded or failed. Everything else stays dark. Depth comes from the dot field and thin inset rings, never from shadows or gloss.

Motion is LED switching. Words blink, dots chase, rows wipe and tickers advance one dot column at a time, all on `steps()` timing with no easing. Explanatory prose (admin copy, gate messages, toasts) is the only place the system sans appears, and it stays quiet and warm-grey beside the LED headings.

**Key Characteristics:**
- One authored dot-matrix face for every display string, brand mark and pictogram; no web fonts, no glyph fonts.
- Square LED modules (tiles) in a strict grid, each carrying its own unlit dot field.
- State encoded by both hue and form: amber plus blink for waiting, a chasing dot row for working, dimmed ink for inactive.
- Matte, flat, dark: depth by dot field and 1px inset rings only.
- Stepped motion everywhere; reduced motion swaps each animation for a static mark.

## Colors

A blackout palette: near-black grounds with a warm, faintly brown cast, one cold white LED in three intensities, and three saturated signal colours each with a single job.

### Primary
- **Boarding Amber** (`amber`): the only colour for "tocca a te". Waiting tiles' status word, mascot and project name; the header counter and its numeral; the static waiting dot under reduced motion. Nothing else on the board is ever amber.
- **Amber Ember** (`amber-dim`): the 1px inset ring around a waiting tile, so the tile's edge glows faintly without competing with its lit text.

### Secondary
- **Neon Magenta** (`magenta`): the brand and the selection hand. The NEONDECK wordmark, the app icon and README mascot, the 2px inset ring and 3x3 corner notch on the selected session, focus outlines, text selection (at 30% alpha) and admin checkbox accents.

### Tertiary
- **Signal Green** (`ok`): a press that succeeded. The 1px ring and "OK" that replace an action tile's label for 600ms, the OK toast edge, admin test results.
- **Fault Red** (`err`): a press that failed, plus faults the user must resolve: the "RETE NON FIDATA" / "TROPPI TENTATIVI" gate headline, the PRC process lamp, admin destructive buttons and error text.

### Neutral
- **Board White** (`led`): full-intensity LED. Project names, the focused terminal's monitor, gate headlines, the filled primary button.
- **Working White** (`led-mid`): LED at 62%. Status words of working, shell and terminal sessions, the clock, lamp codes, age and host codes, the focused-session ring.
- **Off White** (`led-low`): LED at 32%. Inactive and view-only sessions, admin secondary button outlines.
- **Matte Ground** (`ground`): the page and device background.
- **Module Black** (`panel`): tile, board and toast surfaces, a step above the ground.
- **Unlit Dot** (`unlit`): the dead LEDs behind every display string and the dot field inside tiles (at 40% inside tiles, full on gate boards); also the off state of lamps.
- **Rule Line** (`rule`): 1px dividers between header, panes, admin sections and list rows; board and toast borders.
- **Tile Edge** (`tile-edge`): the 1px outer border of every tile.
- **Field Edge** (`field-edge`): the 1px stroke of admin inputs, selects and the colour well.
- **Prose Grey** (`prose`) and **Hint Grey** (`prose-2`): sans prose and secondary prose (labels, hints, empty-state text).

User-chosen action colours (set per button in the admin) tint a tile's ground at 12% over Module Black and become its LED ink. They are content, not system colours.

### Named Rules
**The Boarding Rule.** Amber means exactly one thing: this session is waiting for you. If an element is amber and is not a waiting session or the count of them, it is wrong.

**The Dark Board Rule.** Most of any screen is unlit. White LED runs at 62% for routine state; full white is reserved for names and the item in hand.

**The Flash Rule.** Green and red appear as results and faults, not as decoration or category colour.

## Typography

**Display Font:** Neondeck LED, the authored 5x7 dot-matrix face in `src/neondeck/ledfont.py` (no fallback; rendered as SVG dots)
**Body Font:** system UI sans (`ui-sans-serif, -apple-system, Segoe UI, Roboto, sans-serif`)
**Label/Mono Font:** system monospace (`ui-monospace, Menlo, Consolas, monospace`) for tokens, commands and addresses only

**Character:** an engineered bitmap face that reads like a station board from across the room, set beside an invisible system sans that only explains.

The LED face is uppercase only: 5x7 capitals and digits with a one-dot gap, Italian accented capitals rising two rows above the cap line (9 rows), a hollow box for unknown characters, eleven-by-eleven pictograms for actions, and two 16x14 bitmaps (the android mascot for Claude sessions, the monitor for terminals). Each lit dot is a circle of radius 0.43 cell; unlit dots sit behind the string. Size is set by cell size, never by a font-size; frontmatter sizes are cap heights (7 cells).

### Hierarchy
- **Display** (6px cell, 42px cap): the header waiting count only; three times the cell of its "TOCCA A TE" label.
- **Headline** (4px cell, scales to fit): gate headlines ("TOKEN RICHIESTO", "RETE NON FIDATA"); 3px for the empty-state "NESSUNA SESSIONE".
- **Title** (2 to 3px cell): header wordmark (2.2px), counter label and clock (2px), admin brand (3px) and admin section headings (2px), primary button labels (2.4px; 1.9px small).
- **Label** (container-relative, about 1.56cqw cell): tile status words and the age/host row; project names and action labels at 2cqw. Shrink-to-fit stops at 72% of the base cell; past that the line becomes a ticker.
- **Lamp code** (1.6px cell): connection and terminal-host codes beside their lamps.
- **Body** (sans 400, 15px, 1.5): admin copy, gate messages, empty-state hint.
- **Body small** (sans 400, 13px, 1.35): labels, hints, toasts, secondary admin rows.

On narrow phones (520px and below) every header cell scales by 0.72.

### Named Rules
**The One Face Rule.** Every status word, name, count, heading, brand mark and action label is drawn in the LED face. Sans is for sentences that explain; it never names a state.

**The Ticker Rule.** A line too long for its module scrolls like a ticker, one dot column per step, after a hold. It never wraps, truncates with an ellipsis, or drops below 72% of its base cell.

## Layout

The device and phone are a board of square modules. In the wide layout (the fixed 800x480 Pi screen, or any landscape viewport at least 700px wide) a 52px header band sits over two panes split 60/40 by a 1px rule: sessions on the left in four columns, actions on the right in three. The header is a three-column grid: wordmark left, waiting count centred, lamps and clock right.

In portrait and narrow viewports the sessions grid fills the top and scrolls; the actions grid is pinned to the bottom (at most 46% of the height) in four columns. At 520px and below the sessions grid drops to three columns and terminal lamps leave the header.

Grids use an 8px gap and 10px padding; empty slots are filled with blank tiles so the board always reads as a full panel. Inside a tile everything is measured in dots: the dot pitch is a fiftieth of the tile width, padding is three dots and row gaps two, so a tile scales as one object.

Sessions sort waiting first (oldest wait first), then working, shell, other, terminals, and view-only last.

The admin is a single centred column (max 1100px, 28px top padding) that becomes two columns with a 56px gutter from 1000px. Sections open with an LED heading over a 1px rule; list rows are separated by 1px rules.

## Elevation & Depth

Flat. There are no drop shadows anywhere. Depth is the dot field: every LED string carries its unlit dots, every tile its faint field, and gate boards a full-strength 6px field. State on a module is drawn as a 1px or 2px inset ring on a pseudo-layer, so the tile's footprint never changes.

### Shadow Vocabulary
- **Waiting ring** (`box-shadow: inset 0 0 0 1px` Amber Ember): waiting sessions.
- **Focus ring** (`box-shadow: inset 0 0 0 1px` Working White): the session currently in front on the computer.
- **Selection ring** (`box-shadow: inset 0 0 0 2px` Neon Magenta): the session that actions will target.
- **Result ring** (`box-shadow: inset 0 0 0 1px` Signal Green or Fault Red): an action tile's press result, for 600ms.

### Named Rules
**The Inset Ring Rule.** State lives on the inside edge of a module. Nothing lifts, glows or casts.

## Shapes

One corner: modules, boards, inputs, buttons and toasts share a gently softened 6px radius; inline code takes 3px. Lamps and the reduced-motion waiting mark are true circles. Every tile is a 1:1 square. Inside the LED layer every lit or unlit pixel is a round dot, never a square pixel, and the selection notch is a 3x3 dot corner in the tile's top-left.

## Components

### Session Tile
A square LED module: status word on the first row, the mascot (or monitor, for terminals) in the middle, the project name below it, and age with a three-letter host code on the last row.
- **Waiting:** all three strings in Boarding Amber, the status word blinks, the Amber Ember ring is on.
- **Working:** status word in Working White over a row of three chasing dots.
- **Shell / terminal / other:** status in Working White (other at Off White); terminals show the window title as their status word and their location as the meta row.
- **View-only:** whole tile at 40% opacity, meta reads "SOLO VISTA", not pressable.
- **Selected / focused:** magenta 2px ring plus corner notch; Working White 1px ring.
- **Change of state:** the tile re-draws with a left-to-right wipe, one dot column per step (280ms).
- **Press:** content drops to 70% opacity while held.

### Action Tile
A square module with an 11x11 LED pictogram (or a user image or emoji) above a centred LED label. Default ink is Board White; a user colour becomes the ink and tints the ground at 12%. On press the label is replaced by a green "OK" with a green ring, or a red ring on failure, for 600ms; a toast follows.

### Header Band
52px, divided from the board by a 1px rule. Magenta wordmark left; centre reads "TOCCA A TE" plus the monumental count in amber, or "TUTTO IN CORSO" / "NESSUNA SESSIONE" in Working White; right holds lamps (6px dots: unlit, white when live, red on fault) with their codes, then the clock. The count wipes in when it changes.

### Buttons
- **Shape:** gently softened corners (6px).
- **Primary:** filled Board White with black LED ink and no unlit backing; 56px tall on the device gates, 36px in the admin.
- **Secondary (admin):** transparent with an Off White 1px outline and sans label (7px 13px); outline rises to Working White on hover.
- **Danger (admin):** Fault Red text and outline.
- **Icon (admin):** 32px square with an 18px stroked line icon.
- **Disabled:** 40% opacity. **Pressed:** 70% opacity. **Focus:** 2px magenta outline, 2px offset.

### Inputs / Fields
- **Gate input:** black, 56px tall, 1px Rule Line border, 16px sans; on focus the border turns Board White.
- **Admin input / select / textarea:** black, 1px Field Edge stroke, 9px 11px padding; on focus a 2px magenta outline. Textareas are monospace.

### Gate Board
A centred panel up to 440px wide on Module Black with a full unlit dot field, LED headline, sans message, then field and primary button; 16px gaps, 28px by 24px padding. Used for the token request and network faults.

### Toast
Bottom-centred Module Black chip with a 1px border that turns green or red, an LED "OK" / "ERRORE" and a sans message; shows for 2.2s.

### Admin Tabs
Text tabs on a 1px rule; the active tab gets a 2px Board White underline and Board White text.

## Do's and Don'ts

### Do:
- **Do** draw every state, name, count, heading and action label with the shared LED module (`ledText`, `ledLine`, `ledBitmap`) from `ledfont.py`; add new characters or pictograms to `ledfont.py` and keep the embedded copies identical.
- **Do** keep amber for waiting sessions and their count, and nothing else.
- **Do** time every animation with `steps()`: blink 1.2s, chase 1.2s staggered by 0.4s, wipe 280ms across 50 dot columns, ticker one dot column per step.
- **Do** give every animation a static reduced-motion form: the blink becomes an amber dot, the chase a still row, the ticker a 78% fade mask.
- **Do** mark state with inset rings (1px, 2px for selection) inside a 6px-radius square module.
- **Do** keep tiles square and fill empty grid slots with blank tiles.
- **Do** generate app icons and README art from the same bitmaps and dot geometry (mascot in magenta over the unlit field).

### Don't:
- **Don't** use drop shadows, glows, gradients or rounded-card dashboard styling; the board is matte.
- **Don't** set display text in the system sans or a web font; sans is for explanatory sentences only.
- **Don't** use green or red as category or decoration colours.
- **Don't** use eased transitions or opacity fades for state changes.
- **Don't** wrap or ellipsize LED lines; let them tick.
- **Don't** use Anthropic logos or marks; the mascot is Neondeck's own android.
