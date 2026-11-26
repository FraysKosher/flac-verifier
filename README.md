<p align="center">
  <img src="Logo/logo_flac_verifier.png" alt="FLAC VERIFIER" width="120">
</p>

# FLAC VERIFIER

[![CI](https://github.com/FraysKosher/flac-verifier/actions/workflows/ci.yml/badge.svg)](https://github.com/FraysKosher/flac-verifier/actions/workflows/ci.yml)
[![Version: 2.13.1](https://img.shields.io/badge/version-2.13.1-blue)](CHANGELOG.md)
[![License: AGPL v3+](https://img.shields.io/badge/license-AGPL_v3%2B-blue)](LICENSE)
[![Tests: 447](https://img.shields.io/badge/tests-447-brightgreen)](#tests)

A forensic audio verifier for deciding whether a FLAC file is **genuinely
lossless** or a fake: a lossy transcode (MP3, AAC, Vorbis, Opus, WMA…) or an
inflated bit-depth/sample-rate container (16-in-24/32 padding, dithered 16-in-24,
or 44.1/48 kHz upscaled to 96/192 kHz).

<p align="center">
  <img src="docs/screenshots/start.png" alt="FLAC Verifier start screen" width="900">
</p>

---

## What it checks

- **Bit-exact integrity (`STREAMINFO MD5` & offline AccurateRip v1/v2 CRCs):**
  decodes the PCM stream in a single bounded-memory pass (8/16/24/32-bit integer
  FLAC), verifies the `STREAMINFO MD5` outside the scoring model (mismatch is
  reported immediately as corruption), extracts informative FLAC `vendor`
  metadata, and computes offline Red Book (`16/44.1/2ch`) AccurateRip v1/v2 CRCs
  (`accuraterip`: middle/first/last positions + CD sector alignment).
- **24-bit & 32-bit algebraic lattice & micro-dither audit:** inspects the
  low bits across the whole track (`es_padded_16_en_24`, `es_16bit_dither_en_24`,
  `es_24bit_escalado_en_peine`, DoP markers) and quiet-block spectral floors
  (`noise_shaped_16_in_24`) to catch 16-bit audio stored in 24-bit or 32-bit
  containers—even when masked with $\pm 1\dots 3\text{ LSB}_{24}$ boundary
  dither or noise-shaped 16-bit dither, while sparing genuine `24/44.1` and
  `24/48` warm studio masters (`native_24bit_hf_tail`).
- **Psychoacoustic, spectral & lapped-transform forensics:** detects hard codec
  cut-offs, **orthogonal lapped transform & subband quantization lattices**
  (`MDCT-1024` with both Vorbis and MPEG-4 AAC `KBD` $\alpha=4$ windows across
  `L`/`R`/`Mid` channels for Vorbis `-q4..-q9` & AAC 320, `PQMF-576` for MP3
  320/V0 even with `--lowpass -1`, `MDCT-512` `KBD` $\alpha=5$ for AC-3 and
  E-AC-3, split-half phase-coherent `PQMF-32` for Musepack SV7/SV8, `CELT-960`
  for Opus 20 kHz TDAC boundary cancellation — with selective fractional-sample
  TDAC refinement for `+/-0.25/+0.50` sample delayed transcodes, sub-splice
  mid-band `8.5–14.8 kHz` `MDCT-1024` detection for AI-spliced lossy tracks,
  `88.2–192 kHz` Hi-Res pure-NumPy decimation bridge, and `far_cutoff` codec
  attribution), **neural super-resolution & AI bandwidth-extension forensics**
  (transposed-convolution vocoder high-frequency spectral-comb + phase-coherence
  detection and 2D Gram time-domain spectral-folding BWE detection across
  `44.1/48/96/192 kHz`), **seamless QMF subband replication** for HE-AAC v1/v2
  SBR (at 44.1–192 kHz including guarded low-ceiling `5.5–14.5 kHz` solo-piano
  tracks even without a boundary notch), upper-band 20 kHz psychoacoustic
  modulation,
  `sfb21` MDCT islands, dynamic Mid/Side stereo collapse, inter-channel
  `MSC`/`IPD` high-frequency stereo phase coupling (`stereo_phase_coupling`),
  diagnostic pre-echo (`pre_echo_ratio_db`) and high-frequency Modified Group
  Delay (`mgd_hf_dispersion`) metrics, harmonic exciters, and Gibbs
  reconstruction clipping.
- **Multi-family corroboration & spectral stationarity guard:** condemning a
  file as `PROBABLE UPSCALE` requires either `>= 2` independent evidence
  families (`corroboracion_multifamilia`) or a deterministic structural proof
  (filterbank lattice, spectral cut-off, or container bit-depth padding/comb),
  while `es_estacionario` protects sustained electronic synthesizer drones and
  modular harmonic pads from false subband-hole or modulation triggers.
- **Hi-Res, DSD & vintage/analogue authenticity:** distinguishes genuine 96/192
  kHz analogue roll-offs, SACD/DSD noise-shaping ramps (`direct_24_40k`,
  `scarlet_30k`, `dsd128_40_80k`), and 1950s–1970s archival tape/vinyl/cassette
  masters (`rolloff_banda_limitada_genuina`) from 22.05/24.0 kHz brickwall
  upscales, aliasing mirrors, and band-limited lossy transcodes.
- **Album-level coherence & ordered reports:** evaluates master consistency
  across the folder, sorts tracks in natural numeric order (`1, 2, …, 10, …,
  100`), and generates a per-album PDF report with embedded spectrograms.

---

## Installation

### Windows portable release (no Python required)

Download **[FLAC Verifier 2.13.1](https://github.com/FraysKosher/flac-verifier/releases/tag/v2.13.1)**,
extract the `FLAC_Verifier` folder, and run `FLAC_Verifier.exe`.

To verify the download with `SHA256SUMS_2.13.1.txt`:

```powershell
Get-FileHash .\FLAC_Verifier_2.13.1-win64.zip -Algorithm SHA256
```

### From source

```bash
# With uv (recommended for CLI/GUI tools) or pipx
uv tool install ".[all]"
flac-verifier

# Or traditional pip in a virtual environment
pip install -r requirements.txt
```

`reportlab` (PDF report), `customtkinter` (GUI) and `tkinterdnd2` (drag and
drop) are optional; the engine and CLI work without them.

### Web version

Open the [web version](https://frayskosher.github.io/flac-verifier/) and choose an album folder.

Supported browsers: desktop Chromium and Firefox.

- The first load downloads about 33 MB.

---

## Usage

### Graphical interface

```bash
flac-verifier                            # when installed via uv / pipx
python gui.py
python verificar_flac.py --gui "D:/Music/Album"
```

Drop a `.flac` file, a single album folder, or a whole artist/library tree onto
the window (or click **Browse folder**), choose your options on the left rail,
and press **Analyze**. Multi-album folder trees are queued album-by-album
automatically, leaving music directories untouched by default while storing
working cache and PNG spectrograms in your OS user cache directory. Filter the
results live (`All`, `Flagged`, `Suspicious`, `Errors`), click any row to open
the track inspector, and use the top-right sun/moon button to switch between
dark and light themes in place.

<p align="center">
  <img src="docs/screenshots/batch_inspector.png" alt="FLAC Verifier batch results and track inspector" width="900">
</p>

**Generate PDF report** starts unchecked by default to avoid unexpected writes
during bulk or read-only scans. When enabled, a **PDF destination** selector lets
you choose explicitly between **Save in each album folder** (default) or **Save in
custom folder...** (using `<Album>_flac_verifier_report.pdf` filenames so
multi-album runs never overwrite earlier reports):

<p align="center">
  <img src="docs/screenshots/pdf_report.png" alt="FLAC Verifier generated PDF verification report" width="900">
</p>

### Interactive CLI & Engine CLI

```bash
# Interactive guided console
python verificar_flac.py

# Non-interactive NDJSON engine (for scripts or automation)
python motor_flac.py --path "C:/Music/Album" --pdf
python motor_flac.py --path "C:/Music/Album" --pdf --output-dir "D:/Reports"
python motor_flac.py --path "track.flac" --png --no-cache
```

| Option | Values | Description |
|---|---|---|
| `--path` | path | `.flac` file or album folder (required) |
| `--png` | — | Save PNG spectrograms in the OS user cache directory (`FLAC_VERIFIER_CACHE_DIR` override) |
| `--pdf` | — | Generate the per-album PDF report (implies `--png`) |
| `--output-dir` | path | Destination folder for generated PDF reports (`<Album>_flac_verifier_report.pdf`) |
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
├── comprobar_ejecutable.py   12-check verification suite for the frozen .exe
├── pyproject.toml            PEP 621 metadata & uv / pipx console scripts
├── docs/                     technical reference, design system, validation & comparison
└── tests/                    447 unit tests (stdlib unittest, synthetic fixtures)
```

Visual identity assets live in `Logo/` (`logo_flac_verifier.png`,
`icono_app.ico`, `logo_flac_verifier.svg`, regenerated by `generar_logo.py`).

---

## Tests & building

```bash
# Run the full 447-test suite (~75 s, no external audio files needed)
python -m unittest discover -s tests

# Skip GUI tests in headless environments
FLAC_VERIFIER_SIN_GUI=1 python -m unittest discover -s tests

# Build the standalone Windows folder in dist/FLAC_Verifier/
python build.py
```

| Test module | Coverage |
|---|---|
| `test_blindaje.py`, `test_protocolo.py`, `test_md5.py` | Corrupt-file safety, strict NDJSON contract, 8/16/24/32-bit MD5 verification |
| `test_veredictos.py`, `test_forense_riguroso.py` | Phase 8–17 forensic detectors: 24/32-bit lattice, 16-in-24 micro-dither & noise-shaped dither guard, `24/44.1` studio converter tail, DSD/Hi-Res, 24 kHz brickwall, lapped transform & subband lattices (`MDCT-1024` Vorbis/KBD/Sine + mid-band sub-splice `8.5–14.8 kHz`, `PQMF-576`, `MDCT-512` AC-3/E-AC-3, `PQMF-32`, `CELT-960`, plus Hi-Res `88.2–192 kHz` decimation bridge), neural vocoder comb/phase & 2D spectral-folding BWE detectors, seamless QMF SBR (including guarded low-ceiling `5.5–14.5 kHz` tracks), inter-channel `MSC`/`IPD` stereo phase coupling, multi-family corroboration, synth stationarity guard, `sfb21` islands, 20 kHz modulation, harmonic exciter, vintage/acoustic exemptions & album coherence |
| `test_rendimiento.py`, `test_cache_y_paralelo.py` | Streaming equivalence, vectorised hot paths, cache invalidation & partial PNG hydration, parallel pool & natural track ordering |
| `test_gui.py`, `test_theme.py`, `test_informe_pdf.py` | GUI lifecycle, dark/light live retheming, WCAG contrast, PDF layout & spectrogram embedding |
| `test_estructura.py`, `test_empaquetado.py`, `test_idioma.py` | Architectural boundaries, PyInstaller spec invariants & English-only repository enforcement |

---

## Documentation

- **[`docs/REFERENCE.md`](docs/REFERENCE.md)** — Full NDJSON protocol contract, scoring weights, known physical limits, cache keys and Windows packaging internals.
- **[`CHANGELOG.md`](CHANGELOG.md)** — Release history (`current: 2.13.1`).
- **[`docs/VALIDATION.md`](docs/VALIDATION.md)** — 340-track real-encoder & adversarial benchmark report (41 families) with Wilson 95 % confidence intervals and SHA-256 manifest digest.
- **[`docs/COMPARISON.md`](docs/COMPARISON.md)** — Side-by-side architectural comparison with [FLAC Detective](https://github.com/Guillain-RDCDE/FLAC_Detective).
- **[`docs/DESIGN.md`](docs/DESIGN.md)** — UI/PDF design system and accessibility tokens.
- **[`CONTRIBUTING.md`](CONTRIBUTING.md)** — Development workflow and invariants.
- **[`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md)** — Community standards and reporting guidelines.
- **[`ACCESSIBILITY.md`](ACCESSIBILITY.md)** — Accessibility support, verified contrast, and known GUI limitations.

---

<!-- BENCHMARK:START -->
## Validation benchmark (real `ffmpeg` codecs + adversarial DSP, n = 340)

Generated by `tests/benchmark_validacion.py` (fixed seed `2026`, engine `17.1`):
- **140 authentic controls** across 14 families: CD 16/44.1, Studio 24/48, Hi-Res analogue 24/96, Hi-Res ultrasonic 24/192, DSD64 24/88.2, DSD128 24/192, Acoustic 18 kHz, Solo low-centroid (<15 kHz), Vintage 1950s archival tape (<14 kHz), Vinyl / Cassette / Dense polyphonic synth transfers, Stationary electronic synth drones & genuine 32-bit `PCM_32` masters, Genuine 24/44.1 warm high-DR studio masters, Clean solo piano (5.8-10.2 kHz discrete harmonics with TPDF & noise-shaped dither), and Percussive beat-locked studio mixes.
- **200 fake & adversarial tracks** across 27 families: MP3 (`libmp3lame` 128k / V0 / 320k fullband `-cutoff 22050`, `libshine` 320k), MP2 384k, AAC-LC (128k / 320k fullband `-cutoff 22000`, MediaFoundation 192k), Vorbis (`-q4`, `-q9` 320k fullband, plus 2 unconstrained `-q10` ~500k boundary probes), Opus (96k / 256k fullband `CELT-960`), WMAv2 192k, AC-3 & E-AC-3 16-bit (`MDCT-512`), Hi-Res 96 kHz transcodes masked with ultrasonic hiss, Fractional-delay (`+0.25`/`+0.50` sample) laundered AAC 320 & MP3 320 fullband (`P1-1`), Neural vocoder SR comb & AI-spliced `MDCT-1024` (44.1/48/96k), Time-domain spectral-folding BWE & dark solo-piano seamless SBR (44.1/48/96k), Seamless HE-AAC QMF SBR (0 dB notch at 44.1 & 96 kHz), Musepack SV7/SV8 `PQMF-32`, Vintage 1950s->MP3 transcodes, Sinc/Aliasing upscales, 24-bit zero-pad / gain-comb / boundary micro-dither / noise-shaped 16-in-24 dither, and MPEG-4 AAC `KBD-1024` asymmetric stereo & 16-in-32 container bloat.

| Ground truth | Detected as fake | Detected as authentic |
|---|---:|---:|
| Fake (n = 200) | **TP = 200** | FN = 0 |
| Authentic (n = 140) | FP = 0 | **TN = 140** |

| Metric | Value | Wilson 95% CI |
|---|---:|---:|
| Sensitivity (TPR) | **100.0%** | 98.1% – 100.0% |
| Specificity (TNR) | **100.0%** | 97.3% – 100.0% |

Acceptance criterion: sensitivity >= 95% and specificity >= 95% — **PASS**.

### Album coherence (batch, addition A)

A second batch runs through `analyze_files`: 8 homogeneous GENUINE tracks and
1 acoustic low-ceiling track in the same directory. The acoustic track must be
raised to PROBABLY LOSSLESS with `coherencia_album = true`:

**PASS**

<!-- BENCHMARK:END -->

## License

GNU Affero General Public License v3.0 or later (`AGPL-3.0-or-later`). See [`LICENSE`](LICENSE).
