# Accessibility Statement

FLAC VERIFIER aims to present forensic audio analysis clearly and accessibly
across its desktop GUI, interactive CLI, NDJSON engine, and PDF reports.

## What Is Supported

- **Verified text contrast (WCAG AA):** Every text/surface pair, verdict tone
  (on its chip tint and on surfaces), and primary accent button pair in both the
  dark and light palettes meets at least a **4.5:1** contrast ratio, enforced
  automatically by `tests/test_theme.py` (which also guards against hard-coded
  hex colors in `gui.py`). PDF report verdict chips and all normal/small text
  styles (table cells, labels, headers, and footers) are automatically verified
  at **4.5:1** against paper in `tests/test_informe_pdf.py` (with **3:1** for
  large bold headings `>= 14 pt`, and no pure `#000000` text).
- **Dark and light themes:** The desktop interface supports live in-place
  switching between dark and light palettes without losing results, scroll
  position, or focus, and persists the preference atomically across sessions
  (`%APPDATA%\FLAC Verifier\settings.json`, home folder fallback).
- **Never color alone:** Verdicts (`GENUINE LOSSLESS`, `PROBABLY LOSSLESS`,
  `SUSPICIOUS`, `PROBABLE UPSCALE`, `indeterminate`) are always paired with
  explicit text labels, status indicators, and full plain-English issue/note
  descriptions rather than relying on color alone.
- **Keyboard access and focus visibility (controls):** Core interactive controls
  in `gui.py` (buttons, quick-filter buttons, drop zone, path/output-folder text
  inputs, checkboxes, the parallel-processing switch, and the worker dropdown)
  participate in `Tab` focus order and render a **2 px** high-contrast focus
  ring (`#9BE3EA` dark / `#1D6F7A` light). `Enter` and `Space` activate focused
  buttons, filter buttons, and the drop zone; `Space` toggles focused checkboxes
  and switches; and `Escape` closes the track detail inspector to return to the
  album summary.
- **Typography, target sizes, and DPI scaling:** `theme.py` resolves `Segoe UI
  Variable Text` (`Segoe UI` fallback) for UI text (minimum 11 px labels, 12–13
  px body, 28 px verdict, 40 px score) and `Cascadia Mono` (`Consolas` fallback)
  for tabular numeric data and logs. Main action buttons use 44 px primary and
  36–40 px secondary/ghost/danger heights, icon buttons are 32 px, and table
  rows are 44 px tall (with compact 28 px quick-filter pills). `gui.py` tracks
  CustomTkinter display scaling (`ScalingTracker.get_widget_scaling`) to scale
  fonts, Pillow-rendered verdict chips, and 1x/2x line icons cleanly across
  Windows DPI settings (100%, 125%, 150%) inside a resizable window (`1180x760`
  default, `1024x680` minimum).
- **Restrained motion:** Streaming table rows appear directly without animation,
  progress is shown via a quiet 4 px bar without modal dialogs, and no bouncing
  or flashing transitions are used.
- **Text-first and screen-reader-friendly CLI alternative:** Both the interactive
  CLI (`python verificar_flac.py`) and the machine-readable NDJSON engine
  (`python motor_flac.py --path ...`) expose 100% of the analysis, verdicts,
  measurements, and plain-English findings without requiring the graphical
  interface.

## Known Limitations

- **Desktop GUI screen-reader coverage:** The graphical interface is built on
  Tkinter / CustomTkinter and renders large result batches inside a custom
  virtualized `tk.Canvas` row pool with Pillow-rendered verdict chips. Because
  Tkinter has limited native Windows UI Automation / MSAA / AT-SPI exposure,
  screen-reader navigation inside the GUI table and track inspector is currently
  incomplete. Screen-reader users are encouraged to use the interactive CLI
  (`verificar_flac.py`) or NDJSON output (`motor_flac.py`), which deliver the
  complete forensic results as plain text or structured JSON.
- **Partial keyboard coverage in the GUI:** Arrow-key (`Up`/`Down`) navigation
  and keyboard row selection inside the virtualized results table are not yet
  implemented (selecting a track row and toggling the collapsible Activity log
  header currently require a pointer click). In addition, the two PDF
  destination radio buttons rely on default CustomTkinter styling rather than
  the custom 2 px focus ring wrapper.
- **Visual spectrograms:** Embedded PNG spectrograms are visual plots. Every
  spectral finding derived from them (spectral ceiling, cut-off frequency,
  filterbank lattice family, SBR replication, Hi-Res/DSD morphology, and
  plain-English issue explanations) is also reported in text and JSON fields so
  visual inspection of the image is never required to interpret the verdict.

## Reporting Accessibility Barriers

If you run into a contrast issue, keyboard trap, scaling glitch, or assistive
technology barrier, please open an issue at
[https://github.com/FraysKosher/flac-verifier/issues](https://github.com/FraysKosher/flac-verifier/issues)
and include:

- Operating system and display scaling percentage (e.g., Windows 11 at 150%)
- Interface used (GUI, CLI, PDF report) and app version (`python main.py --version`)
- Assistive technology or settings in use, if applicable
- A short description of what blocked you or behaved unexpectedly
