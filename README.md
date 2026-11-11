<p align="center">
  <img src="Logo/logo_flac_verifier.png" alt="FLAC VERIFIER" width="120">
</p>

# FLAC VERIFIER

[![CI](https://github.com/FraysKosher/flac-verifier/actions/workflows/ci.yml/badge.svg)](https://github.com/FraysKosher/flac-verifier/actions/workflows/ci.yml)
[![Version: 2.7.0](https://img.shields.io/badge/version-2.7.0-blue)](CHANGELOG.md)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Tests: 332](https://img.shields.io/badge/tests-332-brightgreen)](#tests)

A forensic audio verifier for deciding whether a FLAC file is **genuinely
lossless** or a fake: a lossy transcode (MP3, AAC, Vorbis, Opus, WMA…) or an
inflated bit-depth/sample-rate container (16-in-24 padding, dithered 16-in-24,
or 44.1/48 kHz upscaled to 96/192 kHz).

<p align="center">
  <img src="docs/screenshots/start.png" alt="FLAC Verifier start screen" width="900">
</p>

---

## What it checks

- **Bit-exact integrity (`STREAMINFO MD5`):** decodes the PCM stream in a
  single bounded-memory pass and verifies the MD5 signature outside the scoring
  model. A mismatch is reported immediately as corruption.
- **24-bit algebraic lattice & micro-dither audit:** inspects the bottom 8 bits
  across the whole track (`es_padded_16_en_24`, `es_16bit_dither_en_24`,
  `es_24bit_escalado_en_peine`, DoP markers) to catch 16-bit audio stored in
  24-bit containers—even when masked with $\pm 1\dots 3\text{ LSB}_{24}$
  boundary dither.
- **Psychoacoustic, spectral & lapped-transform forensics:** detects hard codec
  cut-offs, **orthogonal lapped transform quantization lattices** (`MDCT-1024`
  for Vorbis `-q8/-q10` & AAC 320, `PQMF-576` for MP3 320/V0 even with
  `--lowpass -1`, `CELT-960` for Opus 20 kHz TDAC boundary cancellation),
  upper-band 20 kHz psychoacoustic modulation, `sfb21` MDCT islands, dynamic
  Mid/Side stereo collapse, harmonic exciters/SBR, and Gibbs reconstruction
  clipping.
- **Hi-Res & DSD authenticity:** distinguishes genuine 96/192 kHz analogue
  roll-offs and SACD/DSD noise-shaping ramps (`direct_24_40k`, `scarlet_30k`,
  `dsd128_40_80k`) from 22.05/24.0 kHz brickwall upscales and aliasing mirrors.
- **Album-level coherence & ordered reports:** evaluates master consistency
  across the folder, sorts tracks in natural numeric order (`1, 2, …, 10, …,
  100`), and generates a per-album PDF report with embedded spectrograms.

---

## Installation

### Windows portable release (no Python required)

Download **[FLAC Verifier 2.7.0](https://github.com/FraysKosher/flac-verifier/releases/tag/v2.7.0)**,
extract the `FLAC_Verifier` folder, and run `FLAC_Verifier.exe`.

To verify the download with `SHA256SUMS_2.7.0.txt`:

```powershell
Get-FileHash .\FLAC_Verifier_2.7.0-win64.zip -Algorithm SHA256
```

### From source

```bash
pip install -r requirements.txt
```

`reportlab` (PDF report), `customtkinter` (GUI) and `tkinterdnd2` (drag and
drop) are optional; the engine and CLI work without them.

---

## Usage

### Graphical interface

```bash
python gui.py
python verificar_flac.py --gui "D:/Music/Album"
```

Drop a folder or `.flac` file onto the window (or click **Browse**), choose
your options on the left rail, and press **Analyze**. Click any row in the
results table to open the track inspector with its spectrogram, technical metrics
and detected issues. Use the top-right sun/moon button to switch between dark
and light themes in place.

### Interactive CLI & Engine CLI

```bash
# Interactive guided console
python verificar_flac.py

# Non-interactive NDJSON engine (for scripts or automation)
python motor_flac.py --path "C:/Music/Album" --mode center --pdf
python motor_flac.py --path "track.flac" --png --no-cache
```

| Option | Values | Description |
|---|---|---|
| `--path` | path | `.flac` file or album folder (required) |
| `--mode` | `center` \| `full` \| `seconds` | Analysis mode label (default `center`) |
| `--seconds` | integer | Seconds label when `--mode seconds` (default `15`) |
| `--png` | — | Save spectrograms in `<folder>/_spectrograms/` |
| `--pdf` | — | Generate `flac_verifier_report.pdf` in the album folder (implies `--png`) |
| `--workers` | integer | Parallel processes (`0` = auto up to 8, `1` = sequential) |
| `--no-cache` | — | Ignore and do not write the per-track result cache |

---

## Verdicts at a glance

| Verdict | Score | Meaning |
|---|---|---|
| `GENUINE LOSSLESS` | ≥ 80 % | Positive spectral and bit-depth evidence with no transcoding or upscale signatures |
| `PROBABLY LOSSLESS` | 60 – 79 % | Clean signal with a conservative or acoustic ceiling, or validated by album coherence |
| `SUSPICIOUS` | 40 – 59 % | Ambiguous high-frequency roll-off or capped by a blocking forensic anomaly |
| `PROBABLE UPSCALE` | < 40 % | Confirmed lossy transcode, Nyquist brickwall upscale, or padded/dithered 16-in-24 container |
| `indeterminate` | — | Unreadable, corrupt, digital silence, or `STREAMINFO MD5` mismatch (never given a fake score) |

For the full scoring weights, detector definitions and NDJSON event schema, see
[`docs/REFERENCE.md`](docs/REFERENCE.md).

---

## Project structure

```text
FLAC VERIFIER/
├── main.py                   single entry point (GUI, CLI or engine)
├── motor_flac.py             analysis engine facade, process pool, cache & CLI
├── _motor_bits.py            MD5, clipping, DR & 24-bit algebraic lattice audit
├── _motor_espectral.py       streaming STFT, psychoacoustic & Hi-Res/DSD detectors
├── _motor_veredicto.py       scoring, verdicts, forensic evidence & album coherence
├── _motor_cache.py           JSON sanitiser & per-track cache I/O
├── verificar_flac.py         interactive command-line interface
├── gui.py                    CustomTkinter desktop interface
├── theme.py                  dark & light design tokens shared by GUI and PDF
├── informe_pdf.py            per-album PDF report generator
├── build.py                  PyInstaller builder & post-build verifier
├── comprobar_ejecutable.py   11-check verification suite for the frozen .exe
└── tests/                    332 unit tests (stdlib unittest, synthetic fixtures)
```

Visual identity assets live in `Logo/` (`logo_flac_verifier.png`,
`icono_app.ico`, `logo_flac_verifier.svg`, regenerated by `generar_logo.py`).

---

## Tests & building

```bash
# Run the full 332-test suite (~90 s, no external audio files needed)
python -m unittest discover -s tests

# Skip GUI tests in headless environments
FLAC_VERIFIER_SIN_GUI=1 python -m unittest discover -s tests

# Build the standalone Windows folder in dist/FLAC_Verifier/
python build.py
```

| Test module | Coverage |
|---|---|
| `test_blindaje.py`, `test_protocolo.py`, `test_md5.py` | Corrupt-file safety, strict NDJSON contract, 8/16/24-bit MD5 verification |
| `test_veredictos.py`, `test_forense_riguroso.py` | Phase 8–11 forensic detectors: 24-bit lattice, 16-in-24 micro-dither, DSD/Hi-Res, 24 kHz brickwall, lapped transform lattices (`MDCT-1024`, `PQMF-576`, `CELT-960`), `sfb21` islands, 20 kHz modulation, harmonic exciter, acoustic exemption & album coherence |
| `test_rendimiento.py`, `test_cache_y_paralelo.py` | Streaming equivalence, vectorised hot paths, cache invalidation, parallel pool & natural track ordering |
| `test_gui.py`, `test_theme.py`, `test_informe_pdf.py` | GUI lifecycle, dark/light live retheming, WCAG contrast, PDF layout & spectrogram embedding |
| `test_estructura.py`, `test_empaquetado.py`, `test_idioma.py` | Architectural boundaries, PyInstaller spec invariants & English-only repository enforcement |

---

## Documentation

- **[`docs/REFERENCE.md`](docs/REFERENCE.md)** — Full NDJSON protocol contract, scoring weights, known physical limits, cache keys and Windows packaging internals.
- **[`CHANGELOG.md`](CHANGELOG.md)** — Release history (`current: 2.7.0`).
- **[`VALIDATION.md`](VALIDATION.md)** — Synthetic benchmark report and Wilson 95 % confidence intervals.
- **[`COMPARISON.md`](COMPARISON.md)** — Side-by-side architectural comparison with [FLAC Detective](https://github.com/Guillain-RDCDE/FLAC_Detective).
- **[`DESIGN.md`](DESIGN.md)** — UI/PDF design system and accessibility tokens.
- **[`CONTRIBUTING.md`](CONTRIBUTING.md)** — Development workflow and invariants.

---

<!-- BENCHMARK:START -->
## Validation benchmark (synthetic, n = 40)

Generated by `tests/benchmark_validacion.py` with the fixed seed `2026`:
20 authentic tracks (CD 16/44.1, Hi-Res analogue 24/96, DSD-like 24/88.2 and
acoustic 18 kHz roll-off) and 20 fakes (sinc upscale, aliasing mirror, MP3 320
joint stereo, Opus fullband 20 kHz, 16-in-24 padding and gain comb).

| Ground truth | Detected as fake | Detected as authentic |
|---|---:|---:|
| Fake (n = 20) | **TP = 20** | FN = 0 |
| Authentic (n = 20) | FP = 0 | **TN = 20** |

| Metric | Value | Wilson 95% CI |
|---|---:|---:|
| Sensitivity (TPR) | **100.0%** | 83.9% – 100.0% |
| Specificity (TNR) | **100.0%** | 83.9% – 100.0% |

Acceptance criterion: sensitivity >= 95% and specificity >= 95% — **PASS**.

### Album coherence (batch, addition A)

A second batch runs through `analyze_files`: 8 homogeneous GENUINE tracks and
1 acoustic low-ceiling track in the same directory. The acoustic track must be
raised to PROBABLY LOSSLESS with `coherencia_album = true`:

**PASS**

<!-- BENCHMARK:END -->

## License

MIT. See [`LICENSE`](LICENSE).
