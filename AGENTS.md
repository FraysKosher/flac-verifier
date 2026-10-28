# AGENTS.md

Orientation for coding agents working in this repository. `README.md` and
`CONTRIBUTING.md` are the authoritative documentation; this file is the short version of
what an agent gets wrong when it starts cold.

Chat language is the user's (Spanish is fine). Every text that lands in the repository —
code, comments, docstrings, docs, commit messages — is **English**.

## Project

FLAC VERIFIER: a Python tool (engine + CLI + GUI + PDF report) that decides whether a FLAC
file is genuine lossless or a lossy transcode. Architecture is deliberately flat.

| Module | Responsibility |
|---|---|
| `motor_flac.py` | **All** analysis logic: decoding, MD5, clipping, DR, low-bit usage, spectral analysis, scoring, cache, process pool, JSON protocol |
| `verificar_flac.py` | CLI presentation only |
| `gui.py` | Window only: launches the engine as a subprocess and paints events |
| `informe_pdf.py` | PDF only |
| `main.py` | Thin flag dispatcher. Nothing else |
| `build.py`, `comprobar_ejecutable.py` | Packaging and post-build verification |

## Commands

```bash
.venv/Scripts/python.exe -m unittest discover -s tests        # ~322 tests, fast; run this first
.venv/Scripts/python.exe -m unittest tests.test_veredictos     # one module
FLAC_VERIFIER_SIN_GUI=1 python -m unittest discover -s tests   # no desktop (skips Tk tests)
python build.py                                                # package the .exe (slow)
python comprobar_ejecutable.py --album "<folder>"              # verify the built .exe
```

Tests need no extra dependencies (stdlib `unittest`); FLAC fixtures are generated at run
time. Python is 3.10.11 in `.venv`; CI also runs 3.12 on Ubuntu and Windows.

## Invariants (tests fail if you break these)

1. **Analysis logic lives only in `motor_flac.py`.** Never reimplement a measurement in
   the CLI, the GUI or the PDF. `tests/test_estructura.py` catches it.
2. `multiprocessing.freeze_support()` stays the **first statement** of the `__main__`
   block in `main.py` (`tests/test_empaquetado.py`).
3. **Raise `ENGINE_VERSION`** (currently `"9.8"`, line 76 of `motor_flac.py`) whenever any
   analysis calculation changes: it is what invalidates existing caches.
4. No new runtime dependencies without discussing it. `numpy`, `scipy`, `soundfile`,
   `mutagen`, `pillow` are required; `reportlab`, `customtkinter`, `tkinterdnd2` must stay
   optional and degrade with a clear message.
5. The analysis is a single streaming pass with bounded memory. Materialising a whole
   track again is a regression; the README performance tables are part of the contract.
6. Any change to scoring, thresholds or spectral analysis needs a measurement (fixture,
   real file, before/after table) in the same change.

## Tests

- Deterministic and fast: synthesise signals via `tests/fixtures.py`, never commit audio.
  Keep each test at a second or two.
- `self.skipTest("reason")` only when the case genuinely cannot run (no display, no
  `ffmpeg`, no process permissions), and say why. Never hide a failure with a skip.
- If you add a test module, update the tests table in `README.md` and the count.

## Style

- No formatter configured: match the surrounding code. Four spaces, `snake_case`,
  `UPPER_CASE` constants, type hints where they help.
- Module headers explain **why** the code is the way it is, not what it does line by line.
- **Python identifiers are English**: functions, classes, constants, variables,
  parameters and attributes. The only Spanish names left are the **JSON protocol keys**
  (`tipo`, `veredicto`, `archivo`, `coherencia_album`, `esp`, `bdi`, …), the reportlab
  style names and a few argparse destinations; they are the public contract and must not
  be renamed. Spanish **prose** anywhere is not allowed; `tests/test_idioma.py`
  enforces it.
- Honest wording over marketing: if a change removes a documented limitation, justify it.

## Git

- Conventional Commits in English, with a scope when it helps:
  `feat(psychoacoustic): …`, `fix(verdict): …`, `docs(verdict): …`, `chore: …`.
- One topic per commit. Never commit `build/`, `dist/`, `__pycache__/`, `_spectrograms/`,
  PDFs or private audio.

## Known doc drift (trust the code, not the doc)

- The suite is **322 tests** and `ENGINE_VERSION = "9.8"`; `README.md` and
  `CONTRIBUTING.md` agree. Keep the number you touch in sync.
