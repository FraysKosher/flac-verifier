# Two designs for the same problem

Both **FLAC VERIFIER** and **[FLAC Detective](https://github.com/Guillain-RDCDE/FLAC_Detective)**
answer the same question — *is this lossless file genuinely lossless, or a
lossy transcode / inflated container in disguise?* — from two different
engineering philosophies. This page compares **FLAC VERIFIER `v2.9.1`** (`engine
13.1`) and **FLAC Detective `v2.6.0`**, with the exact commands used to obtain
every figure, so you can choose the one that fits your workflow (or use both).

- **FLAC Detective** is a Python package, CLI and library scanner meant to be
  installed via `pip`/`pipx`/Docker, scripted across a multi-format music
  library, or integrated into `beets`, with an optional PySide6 GUI and an
  optional stereo CNN.
- **FLAC VERIFIER** is a self-contained Windows desktop application (plus CLI
  and NDJSON engine) that requires no Python installation on the target machine,
  verifies bit-exact `STREAMINFO MD5` integrity alongside spectral,
  lapped-transform (`MDCT-1024` Vorbis + AAC `KBD`, `PQMF-576`, `PQMF-32`,
  `CELT-960`), seamless HE-AAC QMF SBR, multi-family corroboration, and
  24/32-bit algebraic forensics in a single streaming pass, and produces an
  archival per-album PDF report with embedded spectrograms.

---

## Side by side

| Dimension | FLAC Detective (`v2.6.0`) | FLAC VERIFIER (`v2.9.1`, engine `13.1`) |
|---|---|---|
| **Primary shape** | Python library + CLI + `beets` plugin + optional PySide6 GUI | Standalone Windows desktop app (`FLAC_Verifier.exe` + `flac_motor.exe`), interactive CLI & NDJSON engine |
| **Installation** | `pip install flac-detective` (`[gui]`, `[ml]`, `[beets]`), Docker (`amd64`/`arm64`), built-in `--update` | Portable Windows ZIP (extract and double-click `FLAC_Verifier.exe`; no Python required) or `pip install -r requirements.txt` |
| **OS shell integration** | Windows Explorer right-click menu (`--install-context-menu`) | Drag-and-drop folder/file target (`tkinterdnd2`), dark/light live in-place theme switch |
| **Input formats** | FLAC (8/16/24/32-bit), WAV, ALAC (`.m4a`), APE (`.ape`), Musepack | FLAC (8/16/24/32-bit, CD and Hi-Res up to 192 kHz) |
| **Bit-exact integrity (`STREAMINFO MD5`)** | Not checked (`0` matches in `src/`); offers a separate FLAC stream repair tool (`flac_detective.repair`) | Reconstructs decoded PCM bytes and verifies the 128-bit `STREAMINFO MD5` in the same pass; mismatch is a hard error outside scoring |
| **Transcode forensics** | 18 rules across independent evidence families: spectral cliff, MP3/Vorbis/Opus MDCT frame-alignment grids (including Vorbis block switching & CELT de-emphasis undo), HE-AAC QMF phase coherence (`Rule 17`), Side-channel step (`Rule 18`), plus optional stereo CNN (`Rule 12`) | Orthogonal lapped transform & subband inversion (`MDCT-1024` Vorbis + MPEG-4 AAC `KBD` $\alpha=4$ 128-subgrid across `L`/`R`/`Mid`, `PQMF-576` MP3 32-band polyphase + 18-MDCT, `PQMF-32` Musepack SV7/SV8 subband frame grid, `CELT-960` Opus TDAC boundary cancellation, plus adaptive passband/ATH-tail inversion for vintage masters) + seamless QMF subband transposition (`sbr_qmf_detectado` at 44.1–192 kHz) + stationarity guard (`es_estacionario`) + energy-guided full-track STFT |
| **24-bit & Hi-Res upscale forensics** | Separate `hires_verdict` axis (`GENUINE_HIRES`, `UPSAMPLED`, `PADDED_DEPTH`): trailing-zero bit check across 8 windows + Welch cliff at `22.05/24/44.1/48 kHz` above a `-70 dB` void | Integrated into the forensic model: full-stream modulo-256 LSB8 & 32-bit audit (`es_padded_16_en_24`, `es_16bit_dither_en_24` for $\pm 1\dots 3\text{ LSB}_{24}$ micro-dither, `es_24bit_escalado_en_peine` gain comb, DoP markers), `22.05/24.0 kHz` Nyquist brickwall, `23–29 kHz` aliasing mirror, DSD64/Scarlet/DSD128 ultrasonic ramp recognition, and smooth vintage/analogue roll-off protection (`rolloff_banda_limitada_genuina`) |
| **Album-level context** | Per-file independent evaluation | Album-level master coherence (`coherencia_album`) across the batch, plus natural numeric track ordering (`1, 2, …, 10, …, 100`) |
| **Verdict model** | Two axes: transcode (`AUTHENTIC` / `WARNING` / `SUSPICIOUS` / `FAKE_CERTAIN`, 0–150 pts, conviction requires 2 evidence families) + `hires_verdict` | Single unified verdict (`GENUINE LOSSLESS` / `PROBABLY LOSSLESS` / `SUSPICIOUS` / `PROBABLE UPSCALE` / `indeterminate`), positive evidence minus confirmed defects; `PROBABLE UPSCALE` requires multi-family corroboration (`corroboracion_multifamilia`) or a deterministic filterbank/cutoff/bit-depth proof |
| **Reports** | Text, CSV, JSON and self-contained HTML triage report with spectrum curves | Per-album PDF report (`flac_verifier_report.pdf`: executive cover, one page per song with embedded Pillow spectrogram and detected issues, overall summary table) + NDJSON stream |
| **Parallelism & caching** | Process pool (`cli/pool.py`) and in-run audio/window cache | Process pool (up to 8 workers) + persistent on-disk per-track JSON cache keyed by size, `mtime_ns`, `STREAMINFO MD5`, `ENGINE_VERSION` and render fingerprint |
| **Codebase size** | 89 Python files, 18,873 lines in `src/` | 10 application modules (8,780 lines), or 12 modules (9,580 lines) including build/verification scripts |
| **Automated tests** | 772 test functions across 82 files (`pytest`) | 338 test functions across 13 files (`unittest`, zero external audio files) + 260-track real-encoder benchmark + 11-check frozen `.exe` gate |
| **Version & commit checked** | `v2.6.0` (`52c51a5`, 2026-11-02) | `v2.9.1` (`ENGINE_VERSION = "13.1"`, 2026-11-11) |
| **Licence** | MIT | MIT |

---

## Where FLAC Detective excels

FLAC Detective is the natural choice when you want a **library-wide scanner and
Python ecosystem tool**:

1. **Multi-container & 32-bit support:** It scans FLAC, WAV, ALAC (`.m4a`), APE
   and Musepack in one pass, and decodes 32-bit integer FLACs via `ffmpeg`.
2. **HE-AAC SBR subband phase coherence (`Rule 17`):** Beyond MDCT frame grids,
   it inspects QMF subband phase coherence for HE-AAC SBR streams—including
   when subsequently upsampled to 96/192 kHz.
3. **Two-family corroboration & pre-registered empirical audits:** A file is
   only convicted (`FAKE_CERTAIN`) when at least two independent evidence
   families agree, backed by an optional stereo CNN (`flac-detective[ml]`) and
   extensive pre-registered evaluations in `ml/exchange/` across thousands of
   attested tracks.
4. **Workflow integrations:** `pip install`, self-updating CLI/GUI (`--update`),
   Windows Explorer context menu (`--install-context-menu`), `beets` plugin for
   automatic tagging on import, and non-destructive repair of unreadable FLAC
   streams (`--repair-in-place`).

---

## Where FLAC VERIFIER excels

FLAC VERIFIER is the natural choice when you want a **zero-setup Windows desktop
auditor, orthogonal lapped-transform & 24-bit/Hi-Res/DSD bit-lattice forensics,
and an archival album PDF**:

1. **Zero-install Windows distribution verified against source:** Released as a
   portable folder (`FLAC_Verifier.exe` + `flac_motor.exe`) that runs on any
   64-bit Windows 10/11 machine without Python or `ffmpeg`. `comprobar_ejecutable.py`
   runs 11 checks on the compiled executables and verifies that the frozen build
   matches the Python source field by field.
2. **Bit-exact `STREAMINFO MD5` verification:** Decodes the audio in a single
   streaming pass and hashes the raw little-endian PCM bytes against the
   128-bit MD5 stored in `STREAMINFO`. Corrupt or truncated files are caught
   deterministically and never assigned a speculative authenticity score.
3. **Orthogonal lapped transform & subband inversion (`MDCT-1024`, `PQMF-576`, `PQMF-32`, `CELT-960`) + seamless QMF SBR:**
   Projects the decoded waveform onto forward codec filterbanks across all
   sample-level phase offsets (`128` Vorbis/AAC block-switching offsets, `576`
   MP3 32-band PQMF × 18-MDCT granule phases, `32 × 36` Musepack `PQMF-32`
   subband frame phases, and `120` Opus CELT short-block offsets after inverting
   CELT post-emphasis) and sweeps local-whitened additive QMF shifts (`3.5–9.5
   kHz`) for seamless HE-AAC SBR. When a recording's musical ceiling sits in
   `9.5–17.5 kHz`, `MDCT-1024` and `PQMF-576` adapt to the passband and ATH
   roll-off tail so genuine 1950s–1970s archival masters are spared while
   transcoded vintage masters are still caught.
4. **Full-track 24-bit algebraic lattice & micro-dither detection:** Rather than
   only checking whether trailing bits are all zero, `_motor_bits.py` builds a
   modulo-256 LSB8 histogram across the track to catch:
   - zero-padded 16-in-24 containers (`es_padded_16_en_24`),
   - **dithered 16-in-24 fakes** (`es_16bit_dither_en_24`, where $\pm 1\dots 3\text{ LSB}_{24}$
     boundary dither leaves interior modulo-256 bins `16..239` below 5 % occupancy),
   - 16-bit audio scaled by a fixed digital gain in a 24-bit container
     (`es_24bit_escalado_en_peine`), and
   - DSD-over-PCM (`es_flujo_dop`) markers.
5. **Upper-band 20 kHz psychoacoustic modulation & DSD/Hi-Res profiling:**
   Detects frame-by-frame psychoacoustic energy drop and modulation in
   `20.2–21.8 kHz` (`modulacion_psicoacustica_20k`), `22.05/24.0 kHz` Nyquist
   brickwalls, `23–29 kHz` aliasing mirrors, and genuine SACD/DSD ultrasonic
   noise-shaping ramps (`direct_24_40k`, `scarlet_30k`, `dsd128_40_80k`).
6. **Persistent per-track disk cache & archival PDF report:** Re-verifying an
   already analysed album takes seconds via `_spectrograms/_cache/`, and `--pdf`
   produces a print-ready `flac_verifier_report.pdf` with one page per track
   (in natural track order `1, 2, …, 10, …, 100`) and its full-resolution
   spectrogram.

---

## How these numbers were measured

Both repositories were measured on **2026-11-10** with the same commands:

```bash
git clone --depth 5 https://github.com/Guillain-RDCDE/FLAC_Detective
cd FLAC_Detective
git log -1 --pretty="%h %ad" --date=short      # 52c51a5 2026-11-02 (v2.6.0)

# Capability greps over FLAC_Detective/src:
grep -ril 'md5\|streaminfo' src --include='*.py' | wc -l                         # 0
grep -ril '\bpdf\b'         src --include='*.py' | wc -l                         # 0
grep -ril 'dsd\|scarlet'    src --include='*.py' | wc -l                         # 0
grep -ril '\blsb\b\|low.bits\|trailing.zero\|padded_depth\|bit_depth' src --include='*.py' | wc -l  # 17

# Code and test counts in FLAC_Detective:
find src -name '*.py' | wc -l                                                    # 89
find src -name '*.py' -exec cat {} + | wc -l                                     # 18873
find tests -name 'test_*.py' | wc -l                                             # 82
grep -rE '^\s*def test_' tests | wc -l                                           # 772
```

Both projects are MIT licensed.

<!-- BENCHMARK:START -->
## Validation benchmark (real `ffmpeg` codecs + adversarial DSP, n = 260)

Generated by `tests/benchmark_validacion.py` (fixed seed `2026`, engine `13.1`):
- **110 authentic controls** across 11 families: CD 16/44.1, Studio 24/48, Hi-Res analogue 24/96, Hi-Res ultrasonic 24/192, DSD64 24/88.2, DSD128 24/192, Acoustic 18 kHz, Solo low-centroid (<15 kHz), Vintage 1950s archival tape (<14 kHz), Vinyl / Cassette / Dense polyphonic synth transfers, and Stationary electronic synth drones & genuine 32-bit `PCM_32` masters.
- **150 fake & adversarial tracks** across 21 families: MP3 (`libmp3lame` 128k / V0 / 320k fullband `-cutoff 22050`, `libshine` 320k), MP2 384k, AAC-LC (128k / 320k fullband `-cutoff 22000`, MediaFoundation 192k), Vorbis (`-q4`, `-q9` 320k fullband, plus 2 unconstrained `-q10` ~500k boundary probes), Opus (96k / 256k fullband `CELT-960`), WMAv2 192k, Seamless HE-AAC QMF SBR (0 dB notch at 44.1 & 96 kHz), Musepack SV7/SV8 `PQMF-32`, Vintage 1950s->MP3 transcodes, Sinc/Aliasing upscales, 24-bit zero-pad / gain-comb / boundary micro-dither, and MPEG-4 AAC `KBD-1024` asymmetric stereo & 16-in-32 container bloat.

| Ground truth | Detected as fake | Detected as authentic |
|---|---:|---:|
| Fake (n = 150) | **TP = 149** | FN = 1 |
| Authentic (n = 110) | FP = 0 | **TN = 110** |

| Metric | Value | Wilson 95% CI |
|---|---:|---:|
| Sensitivity (TPR) | **99.3%** | 96.3% – 99.9% |
| Specificity (TNR) | **100.0%** | 96.6% – 100.0% |

Acceptance criterion: sensitivity >= 95% and specificity >= 95% — **PASS**.

### Album coherence (batch, addition A)

A second batch runs through `analyze_files`: 8 homogeneous GENUINE tracks and
1 acoustic low-ceiling track in the same directory. The acoustic track must be
raised to PROBABLY LOSSLESS with `coherencia_album = true`:

**PASS**

<!-- BENCHMARK:END -->
