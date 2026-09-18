# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Continuous integration on GitHub Actions: the 229 tests run on Ubuntu and
  Windows, with Python 3.10 and 3.12. The window tests skip on the Linux runner,
  where there is no display, and a stale run is cancelled when a new commit
  arrives on the same branch.
- Issue templates for bug reports and feature requests, a contact list pointing at
  the verdict model, the known limits and the comparison page, and a pull-request
  template that asks about the four invariants of the project
  (`freeze_support()` first, `VERSION_MOTOR` bump, no Spanish in what is shipped,
  presentation layers stay thin).
- `COMPARISON.md`: how this project and FLAC Detective differ, both described on
  their own terms, with the commands that produce every number in the tables.

### Changed

- **The code is English now, not only the interface.** Every comment and every
  docstring in the repository was translated from Spanish, and `test_idioma.py`
  now fails if Spanish prose appears in any Python file — not just in the text
  the user can see. Internal identifiers (variable and function names, JSON
  protocol keys such as `tipo` or `veredicto`) keep their names: renaming them
  would break every consumer of the protocol.

### Fixed

- Three user-visible strings that the interface translation had missed:
  `MD5 verified:` in the interactive CLI, `Not found:` in the file-not-found
  alert of the GUI, and the console labels of the executable checker
  (`interface`, `engine`, `FAILED`, `RESULT:`).
- The `--seg` flag that the GUI still sent to the engine after the command-line
  flags were renamed to English, and the protocol status codes
  (`match`/`mismatch`, `far_cutoff`/`near_nyquist_cutoff`, `empty`/`partial`/
  `active`) that the CLI still compared against their old Spanish values.

### Planned

- Rename the remaining Spanish internal identifiers in the JSON protocol and the
  cache entries (field names such as `tipo`, `veredicto` or `desde_cache`, and
  the module-level constants) once that does not break external consumers.
- A trained classifier for the 320 kbps MP3 stored at 16 bits, which is at
  present a documented physical limit of the spectral analysis.

## [1.0.0] - 2026-09-16

First published version: a FLAC authenticity verifier that combines integrity
checks with spectral analysis and refuses to guess when the evidence is not
conclusive.

### Added

- **Real STREAMINFO MD5 verification.** The MD5 stored in the FLAC metadata is
  recomputed over the decoded audio and compared bit by bit. It is the only
  objective, total check of the format, and it works at 8, 16 and 24 bits. A
  mismatch is a hard integrity error: the file gets no verdict at all.
- **Spectrogram and per-album PDF report.** `flac_verifier_report.pdf` is written
  in the analysed folder with a cover and an executive summary by colour, one
  record per song (verdict with score, technical table, plain-language list of
  problems and the embedded spectrogram) and a final overall table. Each record
  is measured before it is written so that it always fits on its page, and no PDF
  failure can abort the batch.
- **Graphical interface** (`gui.py`, CustomTkinter): two-column window, folder
  drag-and-drop when `tkinterdnd2` is installed, per-file progress, colour-coded
  log, a batch summary card and a button that opens the generated PDF.
- **NDJSON protocol** for the engine, with one JSON line per event
  (`inicio`, `resultado`, `informe`, `aviso`, `fin`, `error`), strict JSON output
  with no `NaN` or `Infinity`, and `stderr` left free for diagnostics.
- **Per-file cache**, keyed by file identity (size, modification time and the
  STREAMINFO MD5), analysis parameters (`VERSION_MOTOR`, spectral mode, seconds)
  and the PNG render fingerprint.
- **Windows packaging:** PyInstaller `onedir` build with two executables
  (`FLAC_Verifier.exe` windowed and `flac_motor.exe` with a console), driven by
  `build.py` and checked by `comprobar_ejecutable.py`.

### Changed

- **Architecture split.** All the analysis logic lives in `motor_flac.py`;
  `verificar_flac.py`, `gui.py` and `informe_pdf.py` are presentation layers that
  must not reimplement it, and tests enforce that. `main.py` became the single
  entry point: a thin flag dispatcher towards the GUI, the CLI, the engine or the
  internal `--motor-cli` mode used by the windowed executable.
- **Coherent verdicts.** The verdict is now positive evidence minus confirmed
  defects, with a fixed threshold scale (`GENUINE LOSSLESS` ≥ 0.80,
  `PROBABLY LOSSLESS` ≥ 0.60, `SUSPICIOUS` ≥ 0.40, otherwise `PROBABLE UPSCALE`).
  Any blocking defect caps the verdict, so a file with a transcoding signature can
  never come out as genuine, and an inconclusive file is reported as
  `indeterminate` instead of being accused.
- **Honest bit-depth reporting.** The low-bit test reports the effective
  resolution actually in use (`empty`, `partial`, `active`, `indeterminate`),
  distinguishes a 16-bit payload inside a 24-bit container, and can only
  disprove — never confirm — provenance. The bit-depth penalty never upgrades a
  verdict on its own.
- **Nyquist-distance cut-off rule.** A spectral step only counts as an accusation
  when it sits below 88 % of Nyquist; a cut-off glued to Nyquist (the anti-alias
  filter of a legitimate CD master) is reported as inconclusive rather than
  penalised. `corte_artificial` now means "confirmed accusation".
- **Hi-res correction above 48 kHz.** The proportional-to-Nyquist rule is replaced
  by the CD limit (22.05 kHz) as the reference, because 70 % of 48 kHz is
  33.6 kHz, a frequency where real acoustic music has no energy. A 96/24 master
  that rolls off at 30 kHz is no longer flagged as a destroyed band, while a
  CD-to-96-kHz upscale is still detected.
- **Robustness and validation hardening.** No broken file can raise an exception
  or abort a batch: invalid signatures, corrupt or truncated STREAMINFO, failed
  decodes, digital silence and MD5 mismatches all produce their own rejected
  record with a reason and no invented verdict. Empty-band guards prevent the
  `NaN` that used to produce invalid JSON at 32 kHz, and redirections to a file
  (never a pipe) keep the child processes of the parallel path from hanging.
- **Performance.** A single streaming pass replaced the array-based path:
  measured on 10 minutes of audio the peak memory drops from 1818 MB to 151 MB at
  16/44.1 and from 6142 MB to 296 MB at 24/96, with the same verdicts, the same
  score and the same problems. The changes behind it are the clipped vectorised
  run detector (423 MB → 4 MB), the block-wise spectrum (45 s STFT pieces,
  ×7.9 less memory), the `imshow` spectrogram renderer instead of
  `pcolormesh(shading="gouraud")` (339.7 s → 75.1 s on a 17-track album), the
  process pool (75.1 s → 26.3 s) and the per-track cache (26.3 s → 2.7 s).
- **Documentation** (`README.md`) rewritten in English with the measured numbers,
  the JSON protocol contract, the verdict model, the known limits and the
  antivirus false-positive guidance.
- **Internationalisation.** The user interface, the command-line flags, the JSON
  protocol values, the PDF report and all the documentation are now in English,
  because the project is published for an international audiophile community.
  The CLI flags became `--path`, `--mode`, `--seconds`, `--png`, `--pdf`,
  `--workers` and `--no-cache`; the spectral modes became `center` (default),
  `full` and `seconds`; the verdict labels became `GENUINE LOSSLESS`,
  `PROBABLY LOSSLESS`, `SUSPICIOUS`, `PROBABLE UPSCALE` and `indeterminate`; the
  report is `flac_verifier_report.pdf` and the spectrogram folder is
  `_spectrograms`. `build.py` now takes `--no-clean`, and
  `comprobar_ejecutable.py` takes `--folder`, `--album` and `--quick`. Code
  comments and docstrings are still in Spanish: a known pending task.

### Fixed

- A NaN could reach the JSON protocol for sample rates where the high band does
  not exist (32 kHz, Nyquist 16 kHz); non-finite floats are now emitted as `null`.
- A spectral ceiling measured in proportion to Nyquist wrongly flagged legitimate
  hi-res masters; the hi-res rule now uses the CD limit as reference.
- The spectrogram title and image could be split across pages when a song had a
  long list of problems; they are now kept together and the image is fitted to the
  remaining space.
- The packaged windowed executable needed the `--motor-cli` flag to deliver the
  NDJSON protocol at all, because a `--noconsole` build leaves
  `sys.stdout = None`; the engine is now also shipped as `flac_motor.exe`, with a
  real console, and the GUI always passes the flag.
- `multiprocessing.freeze_support()` is pinned as the first statement of
  `main.py`; without it every parallel worker relaunched the whole application
  (eight windows in a loop in the windowed executable).

### Security

- The build disables UPX and adds version resources and an icon to both
  executables, to reduce antivirus false positives. No UPX compression, no
  signature: the README documents how to add an exclusion and warns that code
  signing is the real solution for serious distribution.
