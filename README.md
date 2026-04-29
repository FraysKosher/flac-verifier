<div align="center">

# 🎵 FLAC Verifier

[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](https://www.gnu.org/licenses/gpl-3.0)
[![Platform: Windows](https://img.shields.io/badge/platform-Windows-lightgrey.svg)]()
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)]()
[![Release](https://img.shields.io/github/v/release/FraysKosher/flac-verifier)](https://github.com/FraysKosher/flac-verifier/releases/latest)
[![Downloads](https://img.shields.io/github/downloads/FraysKosher/flac-verifier/total)](https://github.com/FraysKosher/flac-verifier/releases)
[![Español 🇪🇸](https://img.shields.io/badge/README-ES-a22c35)](README.es.md)

**Automatically detect upscaled or fake lossless FLAC files** — for audiophiles, archivists, and anyone who wants to know what's actually in their music collection.

[**⬇ Download**](#installation) · [**How it works**](#how-it-works) · [**Build from source**](#option-b--run-from-source)

</div>

---

## Why FLAC Verifier?

A large portion of "24-bit hi-res FLAC" files distributed online are fake:

- **MP3 or AAC re-encoded to FLAC** — lossy source inside a lossless container
- **16-bit upsampled to 24-bit** — zero-padded, no real extra resolution
- **Clipped and renormalized** — audio damaged beyond 0 dBFS and repackaged

FLAC Verifier performs a **full spectral, bit-depth, and dynamic range analysis** on every file in a folder, then assigns each one a scored verdict: **Lossless Genuine**, **Probably Lossless**, **Doubtful**, or **Probable Upscale**.

> No account required. No internet connection. Fully local.

---

## Screenshot

<p align="center">
  <img src="screenshots/app-main.png" alt="FLAC Verifier screenshot" width="900">
</p>

---

## Features

- 📊 **Full spectral analysis** — analyses the entire FLAC file from start to finish, no duration cap
- 🖼️ **Mandatory spectrograms** — every file gets a spectrogram PNG, always, no option to skip
- 🏷️ **Four-tier verdicts** — scored 0–100% with clear labels: `LOSSLESS GENUINE` / `PROBABLY LOSSLESS` / `DOUBTFUL` / `PROBABLE UPSCALE`
- 📄 **PDF + CSV reports** — per-folder report files and a cumulative master history log in `%APPDATA%\flac-verifier\`
- 🔍 **Filter bar** — one-click filtering of results by verdict (All / Genuine / Fake / Inconclusive)
- 🌗 **Dark / Light theme** — toggle in the nav bar, preference persisted across sessions
- 🌐 **EN / ES interface** — full English and Spanish localisation, including backend-generated issue strings
- 🔔 **Desktop notifications** — Windows system toast when analysis completes
- 🔄 **Auto-updater** — silent background check on startup; prompts only when a new version is available

---

## How It Works

```
Your FLAC file
     │
     ▼
┌──────────────────────────────┐
│  1. Metadata extraction      │  Sample rate, bit depth, channels
│  2. Full spectral analysis   │  FFT → frequency rolloff detection
│  3. Bit depth analysis       │  Effective vs. declared depth
│  4. Dynamic range check      │  DR score + clipping detection
│  5. Artifact fingerprinting  │  Encoder patterns (LAME, AAC, etc.)
└──────────────────────────────┘
     │
     ▼
  Score (0–100) + Verdict + Problem list
```

---

## Accuracy

| Case | Expected accuracy |
|---|---|
| 16→24 bit zero-padding | ~97% |
| MP3 128–192 kbps → FLAC | ~95% |
| AAC 256 kbps → FLAC | ~88% |
| LAME 320 kbps → FLAC | ~75% |

> When in doubt, trust the spectrogram. High-quality lossy encodes are intentionally hard to detect.
> False positives are rare but possible on very short files, long silent passages, or heavily limited masters.

---

## Requirements

| Requirement | Minimum version |
|---|---|
| **OS** | Windows 10 / 11 (x64) |
| **Python** | 3.10 |
| **Node.js** | 18 (only needed to run from source) |
| **Rust** | 1.77 (only needed to run from source) |

---

## Installation

### Option A — Pre-built installer (recommended)

1. Go to the [latest release](https://github.com/FraysKosher/flac-verifier/releases/latest)
2. Download `FLAC.Verifier_<version>_x64-setup.exe`
3. Run the installer — Python dependencies are bundled

### Option B — Run from source

```bash
# 1. Clone the repository
git clone https://github.com/FraysKosher/flac-verifier.git
cd flac-verifier

# 2. Install Python dependencies
pip install -r requirements.txt

# 3. Install Node dependencies and launch
cd flac-verifier
npm install
npm run tauri dev
```

---

## Usage

Launch the app via the Start Menu shortcut (installed) or via `npm run tauri dev` (source).

1. **Select a folder** — drag and drop a folder containing `.flac` files onto the drop zone, or use the Browse button
2. **Click Analyze** — results stream in card by card as each file is processed
3. **Review verdicts** — use the filter bar to show only Genuine, Fake, or Inconclusive results
4. **Export** — enable the PDF checkbox before analyzing to generate a formatted report alongside the source files

### CLI (Python engine directly)

```bash
python motor_flac.py --ruta "C:/Music/MyAlbum" --pdf
```

| Argument | Description |
|---|---|
| `--ruta` | Path to a folder or single `.flac` file (required) |
| `--pdf` | Generate a PDF report and CSV alongside the analyzed folder |

---

## A note on analysis time

FLAC Verifier analyses **the entire file** — there is no duration cap. This gives the most accurate verdict, especially for spectral ceiling and dynamic range measurements, but it means analysis time scales with total album duration.

| Album size | Approximate time |
|---|---|
| 10 files × 4 min | ~30–60 seconds |
| 15 files × 8 min | ~2–4 minutes |
| 30 files × 10 min | ~5–10 minutes |

A progress bar and per-file streaming results are shown throughout. A desktop notification fires when the run is complete.

---

## Project structure

```
flac-verifier/          ← Tauri + React frontend
  src/                  ← React components, hooks, i18n
  src-tauri/            ← Rust shell, capabilities, icons
motor_flac.py           ← Python analysis engine
docs/                   ← GitHub Pages landing page
requirements.txt        ← Python dependencies
```

---

## Roadmap

- [x] Spectral + bit-depth + clipping detection
- [x] Auto-updater + PDF reports
- [x] EN/ES bilingual interface
- [ ] Noise floor analysis (NFH)
- [ ] Pre-echo detection for high-quality transcodes
- [ ] Encoder fingerprinting (LAME, AAC, Opus)
- [ ] CSV/JSON export of full scan history

---

## License

This project is licensed under the **GNU General Public License v3.0**.  
See the [LICENSE](LICENSE) file for the full text.
