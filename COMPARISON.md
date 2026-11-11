# Two designs for the same problem

Both **FLAC VERIFIER** and **[FLAC Detective](https://github.com/Guillain-RDCDE/FLAC_Detective)**
answer the same question — *is this lossless file genuinely lossless, or a
lossy transcode / inflated container in disguise?* — from two different
engineering philosophies. This page compares both tools at version **2.6.0**,
with the exact commands used to obtain every figure, so you can choose the one
that fits your workflow (or use both).

- **FLAC Detective** is a Python package, CLI and library scanner meant to be
  installed via `pip`/`pipx`/Docker, scripted across a multi-format music
  library, or integrated into `beets`, with an optional PySide6 GUI and an
  optional stereo CNN.
- **FLAC VERIFIER** is a self-contained Windows desktop application (plus CLI
  and NDJSON engine) that requires no Python installation on the target machine,
  verifies bit-exact `STREAMINFO MD5` integrity alongside spectral and 24-bit
  algebraic forensics in a single streaming pass, and produces an archival
  per-album PDF report with embedded spectrograms.

---

## Side by side (both at v2.6.0)

| Dimension | FLAC Detective (`v2.6.0`) | FLAC VERIFIER (`v2.6.0`, engine `10.0`) |
|---|---|---|
| **Primary shape** | Python library + CLI + `beets` plugin + optional PySide6 GUI | Standalone Windows desktop app (`FLAC_Verifier.exe` + `flac_motor.exe`), interactive CLI & NDJSON engine |
| **Installation** | `pip install flac-detective` (`[gui]`, `[ml]`, `[beets]`), Docker (`amd64`/`arm64`), built-in `--update` | Portable Windows ZIP (extract and double-click `FLAC_Verifier.exe`; no Python required) or `pip install -r requirements.txt` |
| **OS shell integration** | Windows Explorer right-click menu (`--install-context-menu`) | Drag-and-drop folder/file target (`tkinterdnd2`), dark/light live in-place theme switch |
| **Input formats** | FLAC (8/16/24/32-bit), WAV, ALAC (`.m4a`), APE (`.ape`), Musepack | FLAC (8/16/24-bit, CD and Hi-Res up to 192 kHz) |
| **Bit-exact integrity (`STREAMINFO MD5`)** | Not checked (`0` matches in `src/`); offers a separate FLAC stream repair tool (`flac_detective.repair`) | Reconstructs decoded PCM bytes and verifies the 128-bit `STREAMINFO MD5` in the same pass; mismatch is a hard error outside scoring |
| **Transcode forensics** | 18 rules across independent evidence families: spectral cliff, MP3/Vorbis/Opus MDCT frame-alignment grids (including Vorbis block switching & CELT de-emphasis undo), HE-AAC QMF phase coherence (`Rule 17`), Side-channel step (`Rule 18`), plus optional stereo CNN (`Rule 12`) | Energy-guided full-track STFT: multi-scale spectral step & distance to Nyquist, upper-band 20 kHz psychoacoustic modulation (`LAME V0/320`, `AAC 256/320`, `Vorbis`, `Opus`), `sfb21` MDCT islands, 600 Hz MDCT holes, dynamic Mid/Side collapse, Opus 20 kHz cliff, harmonic exciter/SBR & Gibbs reconstruction clipping |
| **24-bit & Hi-Res upscale forensics** | Separate `hires_verdict` axis (`GENUINE_HIRES`, `UPSAMPLED`, `PADDED_DEPTH`): trailing-zero bit check across 8 windows + Welch cliff at `22.05/24/44.1/48 kHz` above a `-70 dB` void | Integrated into the forensic model: full-stream modulo-256 LSB8 audit (`es_padded_16_en_24`, `es_16bit_dither_en_24` for $\pm 1\dots 3\text{ LSB}_{24}$ micro-dither, `es_24bit_escalado_en_peine` gain comb, DoP markers), `22.05/24.0 kHz` Nyquist brickwall, `23–29 kHz` aliasing mirror, and DSD64/Scarlet/DSD128 ultrasonic ramp recognition |
| **Album-level context** | Per-file independent evaluation | Album-level master coherence (`coherencia_album`) across the batch, plus natural numeric track ordering (`1, 2, …, 10, …, 100`) |
| **Verdict model** | Two axes: transcode (`AUTHENTIC` / `WARNING` / `SUSPICIOUS` / `FAKE_CERTAIN`, 0–150 pts, conviction requires 2 evidence families) + `hires_verdict` | Single unified verdict (`GENUINE LOSSLESS` / `PROBABLY LOSSLESS` / `SUSPICIOUS` / `PROBABLE UPSCALE` / `indeterminate`), positive evidence minus confirmed defects; blocking defects cap the maximum verdict |
| **Reports** | Text, CSV, JSON and self-contained HTML triage report with spectrum curves | Per-album PDF report (`flac_verifier_report.pdf`: executive cover, one page per song with embedded Pillow spectrogram and detected issues, overall summary table) + NDJSON stream |
| **Parallelism & caching** | Process pool (`cli/pool.py`) and in-run audio/window cache | Process pool (up to 8 workers) + persistent on-disk per-track JSON cache keyed by size, `mtime_ns`, `STREAMINFO MD5`, `ENGINE_VERSION` and render fingerprint |
| **Codebase size** | 89 Python files, 18,873 lines in `src/` | 10 application modules (8,245 lines), or 12 modules (9,046 lines) including build/verification scripts |
| **Automated tests** | 772 test functions across 82 files (`pytest`) | 329 test functions across 13 files (`unittest`, zero external audio files) + 11-check frozen `.exe` gate |
| **Version & commit checked** | `v2.6.0` (`52c51a5`, 2026-11-02) | `v2.6.0` (`ENGINE_VERSION = "10.0"`, 2026-11-10) |
| **Licence** | MIT | MIT |

---

## Where FLAC Detective excels

FLAC Detective is the natural choice when you want a **library-wide scanner and
Python ecosystem tool**:

1. **Multi-container & 32-bit support:** It scans FLAC, WAV, ALAC (`.m4a`), APE
   and Musepack in one pass, and decodes 32-bit integer FLACs via `ffmpeg`.
2. **Time-domain MDCT & QMF grid arithmetic:** Beyond spectral cut-offs, its
   `Rule 13` inspects MDCT frame boundaries in the time domain (MP3 576/1152
   samples, Vorbis 8-alignment block switching, and Opus 20 ms CELT frames after
   inverting de-emphasis), and `Rule 17` measures subband phase coherence for
   HE-AAC SBR—even when upsampled to 96/192 kHz.
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
auditor, deep 24-bit/Hi-Res/DSD bit-lattice forensics, and an archival album PDF**:

1. **Zero-install Windows distribution verified against source:** Released as a
   portable folder (`FLAC_Verifier.exe` + `flac_motor.exe`) that runs on any
   64-bit Windows 10/11 machine without Python or `ffmpeg`. `comprobar_ejecutable.py`
   runs 11 checks on the compiled executables and verifies that the frozen build
   matches the Python source field by field.
2. **Bit-exact `STREAMINFO MD5` verification:** Decodes the audio in a single
   streaming pass and hashes the raw little-endian PCM bytes against the
   128-bit MD5 stored in `STREAMINFO`. Corrupt or truncated files are caught
   deterministically and never assigned a speculative authenticity score.
3. **Full-track 24-bit algebraic lattice & micro-dither detection:** Rather than
   only checking whether trailing bits are all zero, `_motor_bits.py` builds a
   modulo-256 LSB8 histogram across the track to catch:
   - zero-padded 16-in-24 containers (`es_padded_16_en_24`),
   - **dithered 16-in-24 fakes** (`es_16bit_dither_en_24`, where $\pm 1\dots 3\text{ LSB}_{24}$
     boundary dither leaves interior modulo-256 bins `16..239` below 5 % occupancy),
   - 16-bit audio scaled by a fixed digital gain in a 24-bit container
     (`es_24bit_escalado_en_peine`), and
   - DSD-over-PCM (`es_flujo_dop`) markers.
4. **Upper-band 20 kHz psychoacoustic modulation & DSD/Hi-Res profiling:**
   Detects frame-by-frame psychoacoustic energy drop and modulation in
   `20.2–21.8 kHz` (`modulacion_psicoacustica_20k`), `22.05/24.0 kHz` Nyquist
   brickwalls, `23–29 kHz` aliasing mirrors, and genuine SACD/DSD ultrasonic
   noise-shaping ramps (`direct_24_40k`, `scarlet_30k`, `dsd128_40_80k`).
5. **Persistent per-track disk cache & archival PDF report:** Re-verifying an
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
