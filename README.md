# FLAC Verifier

[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](https://www.gnu.org/licenses/gpl-3.0)
[![Platform: Windows](https://img.shields.io/badge/platform-Windows-lightgrey.svg)]()
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)]()
[![Release](https://img.shields.io/github/v/release/FraysKosher/flac-verifier)](https://github.com/FraysKosher/flac-verifier/releases/latest)

**Automatically detect upscaled or fake lossless FLAC files** — for audiophiles, archivists, and anyone who wants to know what's actually in their music collection.

FLAC Verifier performs a full spectral, bit-depth, and dynamic range analysis on every file in a folder, then assigns each one a scored verdict: **Lossless Genuine**, **Probably Lossless**, **Doubtful**, or **Probable Upscale**.

---

<!-- add screenshot here -->

---

## Features

- 📊 **Full spectral analysis** — analyses the entire FLAC file from start to finish, no duration cap
- 🖼️ **Mandatory spectrograms** — every file gets a spectrogram PNG, always, no option to skip
- 🏷️ **Four-tier verdicts** — scored 0–100 % with clear labels: `LOSSLESS GENUINE` / `PROBABLY LOSSLESS` / `DOUBTFUL` / `PROBABLE UPSCALE`
- 📄 **PDF + CSV reports** — per-folder report files and a cumulative master history log in `%APPDATA%\flac-verifier\`
- 🔍 **Filter bar** — one-click filtering of results by verdict (All / Genuine / Fake / Inconclusive)
- 🌗 **Dark / Light theme** — toggle in the nav bar, preference persisted across sessions
- 🌐 **EN / ES interface** — full English and Spanish localisation, including backend-generated issue strings
- 🔔 **Desktop notifications** — Windows system toast when analysis completes
- 🔄 **Auto-updater** — silent background check on startup; prompts only when a new version is available

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
cd flac-verifier          # repo root — contains motor_flac.py

# 2. Install Python dependencies
pip install -r requirements.txt

# 3. Install Node dependencies and launch
cd flac-verifier          # Tauri frontend subfolder
npm install
npm run tauri dev
```

---

## Usage

Launch the app via the Start Menu shortcut (installed) or via `npm run tauri dev` (source).

1. **Select a folder** — drag and drop a folder containing `.flac` files onto the drop zone, or use the Browse buttons
2. **Click Analyze** — results stream in card by card as each file is processed
3. **Review verdicts** — use the filter bar to show only Genuine, Fake, or Inconclusive results
4. **Export** — enable the PDF checkbox before analyzing to generate a formatted report alongside the source files

### CLI (Python engine directly)

The analysis engine can also be invoked directly from the command line:

```bash
python motor_flac.py --ruta "C:/Music/MyAlbum" --pdf
```

| Argument | Description |
|---|---|
| `--ruta` | Path to a folder or single `.flac` file (required) |
| `--pdf` | Generate a PDF report and CSV alongside the analyzed folder |

---

## ⏱️ A note on analysis time

FLAC Verifier analyses **the entire file** — there is no duration cap. This gives the most accurate verdict, especially for spectral ceiling and dynamic range measurements, but it means analysis time scales with total album duration.

**Rough estimates on a modern CPU:**

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

## License

This project is licensed under the **GNU General Public License v3.0**.  
See the [LICENSE](LICENSE) file for the full text.
