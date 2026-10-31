# FLAC Verifier — Design System and UI Refresh

Status: proposal for the `feat/ui-refresh` branch. No analysis, protocol, CLI or
PDF data changes are allowed by this document; it is presentation only.

## 1. Product and audience

FLAC Verifier is a desktop forensic tool. The user is an audio collector,
mastering engineer or archivist who runs a batch and needs a fast, trustworthy
answer: is this file genuinely lossless, or a lossy transcode dressed up as one?

The interface must feel like an **instrument**, not a SaaS dashboard: calm,
precise, legible at a glance, with the verdict and the evidence above the noise.
Trust comes from clarity, not decoration.

Operating context: Windows 10/11, 1120–1440 px wide windows, mouse and keyboard,
dark room, long batches, a log that can run for minutes, and a PDF report that
must look like the same product.

## 2. Design direction

**Precision instrument.** Dark, quiet surfaces; one accent; strong typographic
hierarchy; verdict colour used only where it means something; data in tabular
figures; borders instead of shadows; motion that explains state, never decorates.

References to borrow principles from, not to copy:

- **Linear** — density, hierarchy, restrained colour, crisp rows.
- **Raycast** — dark command surface, tight spacing, one accent.
- **Stripe Dashboard** — data tables and numeric alignment.
- **Roon / Audirvana** — audio context: dark, album art, calm.
- **Arc** — softness in the shell, not in the data.

## 3. Principles

1. **Verdict first.** The batch verdict and the selected file's verdict are the
   largest things on screen. Everything else is secondary.
2. **One accent.** Teal is the brand and the action colour. Verdict colours are
   semantic, never decorative.
3. **Borders, not shadows.** Tk cannot render real elevation; use surface steps
   and 1 px borders.
4. **8-point rhythm.** Every margin, gap and size is a multiple of 4, preferably 8.
5. **Tabular data.** Numbers are right-aligned, monospaced and never jitter.
6. **No emoji as icons.** Emoji render differently per Windows build and look
   amateur. Use a small line-icon set.
7. **Every state is designed.** Empty, loading, partial, error, cancelled, done.
8. **The PDF is the same product.** Same palette, type and verdict language.

## 4. Anti-patterns (do not ship)

- Default CustomTkinter blue, default fonts, default corner radii.
- Emoji icons (`📂`, `▶`, `■`, `📄`) in buttons or labels.
- Purple/blue gradients, glows, neon, gamer RGB.
- Cards inside cards; every section wrapped in a box.
- Pure black `#000` or pure grey `#808080`; always tint.
- Grey text on coloured backgrounds.
- Five font sizes and three weights in one panel.
- A big filled red "Cancel" button; cancel is a quiet action.
- Centring everything; data wants a left edge.
- Motion with bounce or elastic easing.
- More than one accent hue plus verdict colours.

## 5. Design tokens

### 5.1 Colour — dark (default)

| Token | Hex | Use |
|---|---|---|
| `bg.canvas` | `#0E1113` | Window background |
| `bg.surface` | `#151A1D` | Panels, cards |
| `bg.raised` | `#1B2226` | Inputs, menus, raised rows |
| `bg.hover` | `#222B30` | Row/button hover |
| `border.subtle` | `#2A353C` | Default 1 px border |
| `border.strong` | `#3A4852` | Focused/selected border |
| `text.primary` | `#E9EFF2` | Headings, values |
| `text.secondary` | `#A8B6BE` | Labels, body |
| `text.tertiary` | `#87949D` | Captions, timestamps (4.64:1 on `bg.hover`) |
| `accent` | `#63B7C2` | Primary action, selected state |
| `accent.hover` | `#79C8D2` | Primary hover |
| `accent.pressed` | `#4E9DA8` | Primary pressed |
| `on.accent` | `#06282C` | Text/icon on accent |
| `focus` | `#9BE3EA` | 2 px focus ring |
| `ok` | `#63D08D` | Genuine lossless |
| `ok.bg` | `#102A1B` | Verdict chip background |
| `ok.border` | `#1F5C38` | Verdict chip border |
| `warn` | `#E8BC63` | Probably lossless / suspicious |
| `warn.bg` | `#2E2410` | |
| `warn.border` | `#6B501C` | |
| `bad` | `#E28374` | Probable upscale |
| `bad.bg` | `#2E1614` | |
| `bad.border` | `#6B2C25` | |
| `unknown` | `#9AA7B0` | Indeterminate |
| `unknown.bg` | `#1D252A` | |
| `unknown.border` | `#38464F` | |
| `danger` | `#E06C6C` | Destructive text/border |
| `danger.bg` | `#2A1414` | Error banner background |

### 5.2 Colour — light

| Token | Hex | Use |
|---|---|---|
| `bg.canvas` | `#F4F6F7` | Window background |
| `bg.surface` | `#FFFFFF` | Panels, cards, paper |
| `bg.raised` | `#F9FAFB` | Inputs, menus, raised rows |
| `bg.hover` | `#EEF2F3` | Row/button hover |
| `border.subtle` | `#DCE3E6` | Default 1 px border |
| `border.strong` | `#C3CFD4` | Focused/selected border |
| `text.primary` | `#132028` | Headings, values |
| `text.secondary` | `#53646D` | Labels, body |
| `text.tertiary` | `#5F6F78` | Captions, timestamps (4.62:1 on `bg.hover`) |
| `accent` | `#2F7F89` | Primary action, selected state |
| `accent.hover` | `#29707A` | Primary hover (white text keeps 5.69:1) |
| `accent.pressed` | `#235F68` | Primary pressed |
| `on.accent` | `#FFFFFF` | Text/icon on accent |
| `focus` | `#1D6F7A` | 2 px focus ring |
| `knob` | `#FFFFFF` | Switch knob |

Verdict colours are derived from the semantic hues of 5.1 instead of copied, so
the two palettes cannot drift apart:

| Token | Derivation | Result |
|---|---|---|
| `ok` | hue mixed 45 % towards `text.primary` | `#376F55` |
| `ok.bg` | hue at 12 % over `bg.surface` | `#ECF9F1` |
| `ok.border` | hue at 30 % over `bg.surface` | `#D0F1DD` |
| `warn` / `.bg` / `.border` | same rule | `#736643` / `#FCF7EC` / `#F8EBD0` |
| `bad` / `.bg` / `.border` | same rule | `#704D4A` / `#FCF0EE` / `#F6DAD5` |
| `unknown` / `.bg` / `.border` | same rule | `#505D65` / `#F3F4F6` / `#E1E5E7` |
| `danger` / `.bg` | same rule | `#6F4247` / `#FBEDED` |

Every text/surface and verdict pair reaches 4.5:1 in both modes; the tests
compute it from the palette, so a new token cannot ship below that.

### 5.3 Typography

| Role | Family | Size | Weight | Notes |
|---|---|---|---|---|
| Caption / section label | Segoe UI Variable Text, Segoe UI | 11 | 600 | Uppercase, +0.4 tracking, tertiary |
| Body small | same | 12 | 400 | Secondary |
| Body | same | 13 | 400 | Primary |
| Label / button | same | 13–14 | 500–600 | Sentence case |
| Section title | same | 16 | 600 | |
| Panel title | same | 20 | 600 | |
| Verdict | same | 28 | 700 | Uppercase only for the verdict word |
| Score | same | 40 | 600 | Mono, tabular |
| Data / log | Cascadia Mono, Consolas | 12 | 400 | Tabular figures |

Rules:

- One UI family, one mono family. No Inter, no Arial, no system default.
- Windows 10 fallback: Segoe UI; Windows 11: Segoe UI Variable.
- Tabular numbers for score, DR, sample rate, bit depth, MD5.
- No text below 11 px. No more than five sizes per screen.

### 5.4 Spacing

Scale: **4, 8, 12, 16, 20, 24, 32, 40**.

- Window padding: 24.
- Panel padding: 20.
- Card padding: 16–20.
- Gap between sections: 24; between controls: 12; inside a control: 8.
- Table row height: 44; header: 36; button height: 40 (primary 44).

### 5.5 Radius and borders

| Token | Value |
|---|---|
| `radius.sm` | 6 (chips, small inputs) |
| `radius.md` | 8 (buttons, inputs) |
| `radius.lg` | 12 (cards, panels) |
| `radius.xl` | 16 (dialogs) |
| `radius.pill` | 999 (status pills) |
| Border | 1 px `border.subtle` |
| Focus ring | 2 px `focus`, offset 2 |

### 5.6 Motion

- Hover/press: 120 ms.
- Panel/expand: 180 ms.
- Progress: 1.2 s linear loop; no bounce.
- No animation on data rows; streaming rows appear instantly.
- Reduced motion: keep colour changes, drop movement.

## 6. App shell and layout

Target window: **1180 × 760**, minimum **1024 × 680**. Left rail fixed **340 px**;
main area flexible; activity log collapsible at the bottom of the main area.

```
┌────────────────────────────────────────────────────────────────────┐
│ [logo] FLAC VERIFIER              [Idle · 0/12]      [PDF] [⋯]     │ 56
├──────────────────┬─────────────────────────────────────────────────┤
│ SOURCE           │  Results                          [Open report]  │
│ ┌──────────────┐ │  ┌─────────────────────────────────────────────┐ │
│ │  drop zone   │ │  │  OK          Score 92       album/01.flac   │ │
│ │  or browse   │ │  │  GENUINE LOSSLESS · 16/44.1 · DR 12 · MD5 ✓ │ │
│ └──────────────┘ │  └─────────────────────────────────────────────┘ │
│ [ path input   ] │  ┌─────────────────────────────────────────────┐ │
│ [ Browse ]       │  │ File        Verdict      Score  Depth  Rate │ │
│                  │  │ 01.flac     ● OK            92   16/44.1    │ │
│ ANALYSIS         │  │ 02.flac     ● OK?           78   24/96      │ │
│ [ Spectral mode ]│  │ 03.flac     ● SUSPICIOUS    41   16/44.1    │ │
│ [ Seconds  15  ] │  └─────────────────────────────────────────────┘ │
│ [x] PDF report   │                                                  │
│ [x] Parallel    │  Activity                                    [⌃]  │
│ [ Workers   8  ] │  ┌─────────────────────────────────────────────┐ │
│ [x] Ignore cache │  │ 12:04:11  Analyzing 03.flac…                │ │
│                  │  └─────────────────────────────────────────────┘ │
│ [   Analyze    ] │                                                  │
└──────────────────┴─────────────────────────────────────────────────┘
```

Behaviour:

- The left rail scrolls independently if the window is short; the primary
  **Analyze** button stays pinned at the bottom of the rail.
- While running, Analyze becomes **Cancel** (ghost danger, 40 px) and the
  progress line sits under the top bar.
- The results table streams rows as `resultado` events arrive.
- Selecting a row opens the detail panel (phase 3) without leaving the table.
- The activity log is collapsed by default to 28 px; expanding shows 140–200 px.

## 7. Components

### 7.1 Buttons

The button set is the most visible part of the refresh. All buttons use
`CTkButton`, explicit colours, 1 px borders where specified, `cursor="hand2"`,
and `takefocus=True`.

| Variant | Height | Radius | Fill | Border | Text | Use |
|---|---|---|---|---|---|---|
| Primary | 44 | 10 | `accent` | none | `on.accent`, 14/600 | Analyze, start |
| Secondary | 40 | 8 | `bg.raised` | `border.subtle` | `text.primary`, 13/500 | Browse, Open report |
| Ghost | 36 | 8 | transparent | none | `text.secondary`, 13/500 | Toolbar, log toggle |
| Danger | 40 | 8 | transparent | `danger` | `danger`, 13/600 | Cancel, clear |
| Icon | 32×32 | 8 | transparent | none | icon 18 | Settings, theme, close |

States:

- **Hover**: primary `accent.hover`; secondary `bg.hover`; ghost `bg.hover`.
- **Pressed**: primary `accent.pressed`; secondary `border.strong`; ghost
  `bg.raised`.
- **Focus**: 2 px `focus` ring, 2 px offset; always visible via keyboard.
- **Disabled**: 35 % opacity, no hover, `cursor="arrow"`.
- **Loading**: label stays, a 14 px spinner or three-dot pulse replaces the icon;
  button is disabled but keeps its width.
- **Content**: icon 16–18 px + 8 px gap + label. No emoji. Sentence case.
- **Padding**: 20 px horizontal for primary, 16 for secondary, 12 for ghost.
- **No scale on hover**: scaling changes layout in Tk; change colour only.

Icon-only buttons need a tooltip (300 ms delay) and an accessible label.

### 7.2 Inputs

- Height 40, radius 8, `bg.raised`, 1 px `border.subtle`.
- Placeholder `text.tertiary`; value `text.primary` 13.
- Focus: border `accent`, 2 px focus ring.
- Error: border `danger`, helper text 12 `danger` below.
- Path input includes a trailing folder icon button (ghost, 28 px) that opens
  the same dialog as Browse.

### 7.3 Drop zone

- 120 px tall, radius 12, 1 px dashed `border.strong`, `bg.surface`.
- Centred icon 24 (`folder-plus`), headline 14/600, subline 12 secondary.
- Drag-over: border `accent`, background 6 % accent tint, icon `accent`.
- Whole area clickable; keyboard focusable; Enter opens browse.
- After a path is chosen, the zone collapses to a compact row with the path,
  a change link and a clear icon.

### 7.4 Select, slider, switch, checkbox

- **Select**: same box as input, chevron 16 right, menu `bg.raised`, radius 8,
  hover row `bg.hover`, selected row `accent` 12 % tint.
- **Slider**: 4 px track `border.subtle`, filled `accent`, thumb 14 px
  `accent` with 2 px `bg.canvas` ring; value in mono 12 right.
- **Switch**: 36×20, track `border.strong` off / `accent` on, knob 16 white;
  label 13.
- **Checkbox**: 18×18, radius 4, 1 px `border.strong`; checked `accent` with
  `on.accent` check; label 13.

### 7.5 Chips and badges

- Height 24, radius 6, 11/600, 8 px horizontal padding, dot 6 px + 6 px gap.
- Verdict chips: `ok`, `warn`, `bad`, `unknown` with their tinted backgrounds
  and borders. Text is the verdict label, never colour alone.
- Count chips in the header: neutral surface, number in mono.

### 7.6 Cards

- Radius 12, `bg.surface`, 1 px `border.subtle`, 16–20 padding.
- No card inside a card. A section that needs separation uses a border-top
  and 24 px spacing instead.
- Card header: 11 uppercase tertiary label + optional action on the right.

### 7.7 Table

- Header 36 px: 11/600 uppercase tertiary; sort arrow 12 on the active column.
- Row 44 px: 13 primary; 1 px separator `border.subtle`; no zebra.
- Hover `bg.hover`; selected row 8 % accent tint + 2 px left accent bar.
- Columns: File (flex), Verdict (chip), Score (mono, right), Depth (mono),
  Rate (mono), DR (mono), MD5 (icon check/cross).
- Long names truncate with an ellipsis and a tooltip; never wrap a row.
- Empty state fills the table body, not a separate card.

### 7.8 Verdict hero

- Height 120–140 for the album summary, 200–280 for the track inspector;
  radius 12, `bg.surface`, 1 px border.
- Left 3 px verdict bar or a chip; verdict 28/700; score 40/600 mono.
- File name 13 secondary, truncating from the left if long.
- Fact row: bit depth, sample rate, DR, MD5 as label/value pairs, 12 px.
- Issue messages are wrapped lines with a severity dot, never truncated chips.
  More than three are folded behind **Show all (N)**, which expands in place;
  the full text is always reachable without a tooltip.
- Actions on the right: **Open report** (secondary), **Reveal** (ghost).

### 7.9 Progress and status

- Top bar status: state 13/600, count `3 / 12` mono 12, current file 12
  secondary, truncating.
- Progress line: 4 px, `bg.raised` track, `accent` fill, no radius on the fill;
  indeterminate: a 30 % segment sliding 1.2 s.
- Never a modal progress dialog.

### 7.10 Activity log

- Collapsed 28 px: "Activity" + last line + chevron.
- Expanded 140–200 px: mono 12, line height 20, timestamp tertiary.
- Levels: `info` secondary, `ok` green, `warn` amber, `error` red; dot 6 px.
- Auto-scroll toggle; pause on manual scroll; clear button (ghost).
- Max 300 lines visible; the engine still keeps its own buffer.

### 7.11 States

- **Empty**: icon 40, headline 16/600, body 13 secondary, primary action.
- **Analyzing**: skeleton rows (three 44 px bars at 6 % text tint) + progress.
- **Partial**: rows appear as they arrive; header count updates.
- **Error**: inline banner 40 px, `danger.bg`, 1 px `danger` border, icon,
  message, dismiss; no modal.
- **Cancelled**: banner neutral, "Cancelled after N files", secondary action
  "Analyze again".
- **Done**: verdict hero + summary chips; the log collapses automatically.

## 8. Iconography

- Line icons, 1.5 px stroke, 16/20/24 px, rounded caps.
- Set: folder, folder-plus, play, stop, file-text, external-link, settings,
  sun, moon, chevron-down, chevron-right, check, x, alert-triangle, info,
  refresh, trash, search, music.
- PNG at 1× and 2× in `Logo/icons/`; `CTkImage` with explicit size.
- No emoji, no Unicode arrows as icons, no mixed stroke widths.
- Icons are decorative unless they are the only label; then add a tooltip and
  an accessible name.

## 9. Accessibility

- Text contrast ≥ 4.5:1; large text ≥ 3:1. Verify every token pair.
- Focus ring visible on every interactive control; logical tab order.
- Hit targets ≥ 32 px; primary actions ≥ 40 px.
- Never encode verdict by colour alone: label + dot + text.
- Keyboard: Enter/Space activate; Escape closes menus/dialogs; arrow keys move
  in the table.
- Test at 100 %, 125 % and 150 % Windows scaling; no clipped labels.

## 10. PDF alignment

- Same palette, type scale and verdict chips as the GUI.
- Header: logo + wordmark + file/album; footer: page number + engine version.
- Verdict band at the top of the first page; table header 10 uppercase.
- Spectrogram framed by a 1 px border, no heavy shadows.
- No pure black text; use `text.primary`.

## 11. CustomTkinter implementation notes

- Add `theme.py` (tokens + `font()`/`mono()` helpers); no logic in it.
- Set `ctk.set_appearance_mode("dark")`; do not rely on the default theme.
- Cache `CTkFont` instances; do not create one per widget.
- Use `grid` with 8-point `padx/pady`; avoid `place`.
- `CTkButton` supports `fg_color`, `hover_color`, `border_color`,
  `border_width`, `corner_radius`, `text_color`, `font`, `image`,
  `compound`, `cursor`, `height`. There is no shadow.
- `CTkImage(light_image=..., dark_image=..., size=(w, h))` for icons.
- Add `Logo/icons/` to the PyInstaller spec `datas` and to `build.py` checks.
- No new runtime dependencies: use `customtkinter`, `Pillow`, stdlib.
- Guard the UI at import time as today: if `customtkinter` is missing, the
  engine still works.
- The theme is saved in `%APPDATA%\FLAC Verifier\settings.json` (atomic write,
  home folder as fallback) and read once, before the first window is built.
- The toggle switches live **in place**: `_retheme(mode)` recolours every widget
  without destroying or recreating any of them. Each colour was given as a
  `_TokenColor`, so the widget remembers its token and the new value is resolved
  from the new palette; plain Tk labels, canvases and text tags are recoloured
  explicitly. While the colours change the window painting is frozen
  (`WM_SETREDRAW(0)` on the client and its children, Windows) and a snapshot of
  the old client area covers the window in a borderless topmost Toplevel; a
  single `RedrawWindow` paints the complete new surface, the title bar is
  recoloured with DWM, and the snapshot is removed. Nothing is hidden with
  `withdraw`. The button is disabled while an analysis runs and the preference is
  saved on every click.

## 12. Phases and acceptance criteria

### Phase 1 — Tokens and controls (pure aesthetic)

- `theme.py`, palette, typography, spacing, radii.
- Header, buttons, inputs, select, slider, switch, checkbox, drop zone.
- Same widgets and behaviour; same layout skeleton.
- Acceptance: 267 tests green; `comprobar_ejecutable` [6] opens/closes;
  screenshots at 100/125/150 %; no emoji left.

### Phase 2 — Results surface

- App shell, verdict hero, results table, summary chips, empty/loading/error
  states, streaming rows.
- Uses existing `resultado` events only; no engine, protocol or CLI change.
- Acceptance: tests updated to semantic assertions (not pixel positions);
  267+ green; build 11/11.

### Phase 3 — Detail, activity and PDF

- Row selection → detail panel with spectrogram thumbnail, problems, evidence.
- Collapsible activity log; PDF restyled with the same tokens.
- Acceptance: PDF pages inspected; no overflow; build 11/11.

### Phase 4 — Light theme and polish (done in 2.4.0)

- Complete light palette: the core tokens of 5.2 plus the verdict text/tint/border
  pairs derived from the dark hues, `focus` and the switch `knob`.
- `text.tertiary` was darkened in both modes so the 11 px labels reach 4.5:1.
- Sun/moon toggle in the main header (icon-only, tooltip, focus ring) that
  recolours the window live through `_retheme(mode)`: the same widgets keep
  their identity and their state (album, results, selection, scroll, log,
  activity, focus), the painting is frozen during the recolour and the window is
  redrawn once, so there is no hybrid or empty frame. It is disabled while an
  analysis runs and saves the choice in
  `%APPDATA%\FLAC Verifier\settings.json`.
- Tests: contrast of every text/surface and verdict pair in both palettes, the
  preference file (default, round trip, malformed, unwritable), the in-place
  toggle with widget identity, scroll, focus, results and log preserved, a
  300-row retheme timing, a light window in every state, and a guard that
  `gui.py` contains no hard-coded colour.
- Acceptance: both themes build and read; 325 tests; build READY;
  `comprobar_ejecutable` 11/11; screenshots at 100/125/150 % in both modes.

## 13. Guardrails

- Presentation only: do not move analysis out of `motor_flac.py`.
- Do not change JSON keys, values, thresholds, scoring or verdicts.
- All new prose is English (`tests/test_idioma.py`).
- Update `tests/test_gui.py` for the new widgets, but keep behaviour tests.
- Keep `tests/test_estructura.py` green: no analysis in `gui.py`.
- `comprobar_ejecutable` [6] must keep opening and closing the window.
- New assets must be in the spec and in the zip; check the size.
- No new runtime dependencies without discussion.

## 14. Definition of done for the refresh

1. The window looks deliberate at first glance: one accent, clear hierarchy,
   no default CTk widgets, no emoji.
2. The verdict is the first thing the eye finds; the table is readable at
   1120 px and at 1440 px.
3. Every state (empty, loading, partial, error, cancelled, done) is designed.
4. Buttons have hover, pressed, focus and disabled states and feel consistent.
5. The PDF matches the GUI.
6. 267 tests, benchmark 20/20, `comprobar_ejecutable` 11/11.
