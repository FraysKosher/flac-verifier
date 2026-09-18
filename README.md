<p align="center">
  <img src="Logo/logo_flac_verifier.png" alt="FLAC VERIFIER" width="120">
</p>

# FLAC VERIFIER

[![CI](https://github.com/FraysKosher/flac-verifier/actions/workflows/ci.yml/badge.svg)](https://github.com/FraysKosher/flac-verifier/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Tests: 229](https://img.shields.io/badge/tests-229-brightgreen)](README.md#tests)

A tool for deciding whether a FLAC file is **genuinely lossless** or fake
lossless: a transcode from a lossy format (MP3, AAC…) or a bit-depth upgrade
(16 → 24).

It does not trust the spectral ceiling alone: it verifies **integrity** (the
STREAMINFO MD5), measures the **actual utilisation of the low bits**, analyses
the spectrum using the distance to Nyquist and, when the evidence does not allow
a decision, **it says so** instead of inventing a verdict.

> **Language.** The user interface (GUI, CLI and PDF report), the values of the
> engine's JSON protocol, the command-line flags, the code comments, the
> docstrings and all the documentation are in English, because the project is
> shared with the international audiophile community. `tests/test_idioma.py`
> enforces it. Internal identifiers (variable and function names, JSON protocol
> keys) keep their original names, as renaming them would break every consumer of
> the protocol.

```
FLAC VERIFIER/
├── main.py               single entry point (GUI, CLI or engine)
├── motor_flac.py         analysis engine + CLI with the JSON protocol
├── verificar_flac.py     command-line interface (presentation layer)
├── gui.py                graphical interface (CustomTkinter, optional)
├── informe_pdf.py        per-album PDF report (optional dependency: reportlab)
├── flac_verifier.spec    PyInstaller recipe for the .exe
├── version_info.txt      executable version resources
├── build.py              builds and verifies the executable
├── comprobar_ejecutable.py   full check of the .exe once built
├── requirements.txt      dependencies
└── tests/                229 tests (unittest, no extra dependencies)
```

All the logic lives in `motor_flac.py`. `verificar_flac.py` only formats the
output, `gui.py` only paints the window and `informe_pdf.py` only draws the PDF:
none of them reimplements analysis (there are tests watching over that).
`main.py` is only a flag dispatcher towards those four modules.

Analysing an album is spread across **processes** (up to 8) and keeps a
**per-track cache**, so repeating a verification is almost instantaneous.

---

## Installation

```
pip install -r requirements.txt
```

Verified with Python 3.10.11, numpy 2.2.6, scipy 1.15.3, soundfile 0.13.1,
mutagen 1.47.0, matplotlib 3.10.3, reportlab 5.0.1 and customtkinter 5.2.2.

Four dependencies are **optional** and the program works without them:

| Dependency | What it is for | If missing |
|---|---|---|
| `matplotlib` | PNG spectrograms (`--png`) | PNG output is disabled and a warning is shown |
| `reportlab` | Per-album PDF report (`--pdf`) | A warning is shown and the analysis continues unchanged |
| `customtkinter` | Graphical interface (`--gui`) | A warning is shown and the CLI is left |
| `tkinterdnd2` | Dragging folders onto the window | The window still works: the "Browse" button is used |

---

## Usage

### Graphical interface

```
python gui.py
python verificar_flac.py --gui
python verificar_flac.py --gui "D:/Music/Album"      # with the path already filled in
```

Two-column window: controls on the left, progress and log on the right. The
**folder can be dragged** onto the window (if `tkinterdnd2` is installed) or
chosen with "Browse".

| Control | What it does |
|---|---|
| Path + Browse | The album folder, or a single `.flac` file |
| Spectral mode | `center` (default), `full` or `seconds` with a slider from 5 to 120 s |
| Generate PDF report | Checked by default |
| Parallel processing | Automatic (up to 8 processes) or a fixed number; off = sequential |
| Ignore cache | Forces an analysis from scratch |
| Analyze / Cancel | Enabled and disabled according to the process life cycle |
| Open PDF report | Enabled when finished; opens the file with the system viewer |

While it is analysing: a per-file progress bar, a log with timestamps and a
colour per verdict, and when it finishes a status card with the batch summary
(green, yellow, orange or red). Engine errors and warnings appear in the log and
in a native dialog, without closing the application.

**How it works inside** (so that it does not freeze on Windows):

1. The engine is launched in a **separate process** with
   `subprocess.Popen([sys.executable, "-u", "motor_flac.py", …])` and
   `CREATE_NO_WINDOW`. The `-u` flag is what makes the NDJSON events arrive **in
   real time** instead of at the end; without it, the stdout buffer would hold
   them back.
2. A **daemon thread** reads stdout and stderr and puts them in a
   `queue.Queue`; no widget is touched from that thread.
3. The window drains the queue with `self.after(100, …)`, always on the
   interface thread, and keeps draining until nothing is left (if the engine
   finishes right after a pass, the final event would not be lost).
4. "Cancel" kills the process tree with `taskkill /F /T` on Windows (the engine
   spreads the work across children: killing only the parent would leave
   orphans) and leaves `terminate()`/`kill()` as a fallback. Closing the window
   cancels too.

### Command-line interface

```
python verificar_flac.py
```

It asks for the spectral analysis mode, whether PNG files are wanted, whether the
PDF report is wanted, and then the path of a `.flac` file or of a folder. It
analyses every `.flac` file at the first level.

### Engine (for scripts or a GUI)

```
python motor_flac.py --path "C:/Music/album" --mode center
python motor_flac.py --path "track.flac" --mode seconds --seconds 20 --png
python motor_flac.py --path "C:/Music/album" --mode center --pdf
python motor_flac.py --path "C:/Music/album" --pdf --png     # report + PNG
python motor_flac.py --path "C:/Music/album" --pdf --workers 1   # no parallelism
python motor_flac.py --path "C:/Music/album" --pdf --no-cache   # clean analysis
```

| Option | Values | Description |
|---|---|---|
| `--path` | path | `.flac` file or folder (required) |
| `--mode` | `seconds` \| `center` \| `full` | Spectral analysis window (default `center`) |
| `--seconds` | integer | Starting seconds when `--mode seconds` (default 15) |
| `--png` | — | Saves one spectrogram per track in `<folder>/_spectrograms/` |
| `--pdf` | — | Generates `flac_verifier_report.pdf` in the album folder (implies generating the spectrograms; requires `reportlab`) |
| `--workers` | integer | Parallel processes. `0` (default) = automatic up to 8; `1` = sequential |
| `--no-cache` | — | Ignores the cache and does not write it (to force an analysis from scratch) |

`--pdf` and `--png` are independent and can be combined. `--pdf` generates the
spectrograms on its own even if `--png` is not passed, because the report embeds
them.

---

## Distribution: a single `.exe` for Windows with no Python

To hand it to somebody who does not have Python installed, it is built with
PyInstaller. One command:

```powershell
cd "mis_proyectos_python\FLAC VERIFIER"
python -m pip install pyinstaller      # once
python build.py                        # cleans, builds and verifies (~1 min)
```

`build.py` does three things and fails with a clear message if something does not
add up: it checks the interpreter and PyInstaller, deletes `build/` and `dist/`,
builds the `.spec`, and **verifies the result** by launching the compiled
executables (version, a real analysis and the presence of the engine). With
`--no-clean` it reuses what has already been built.

`build/` is the PyInstaller scaffolding (about 66 MB): you can delete it whenever
you like, `build.py` recreates it on the next build.

### What is left in `dist/FLAC_Verifier/`

```
dist/FLAC_Verifier/
├── FLAC_Verifier.exe     the application (no console: double click and done)
├── flac_motor.exe        the engine in a console, which the GUI launches as a subprocess
└── _internal/            Python, numpy, scipy, matplotlib, reportlab, tk…
```

The whole folder is copied or compressed into a ZIP: **the `.exe` alone does not
work**, it needs its `_internal/` next to it. That is about 202 MB on disk
(88 MB in a ZIP), because it includes NumPy, SciPy, Matplotlib and the Python
interpreter. The first launch takes a few seconds (that is normal: there is
nothing to unpack in `%TEMP%`, which is exactly the reason for using folder mode
instead of a single `.exe`).

Where those 202 MB come from: `scipy` with its DLLs (82 MB), `numpy` (27 MB),
`matplotlib` (18 MB), `PIL` (14 MB, used by reportlab for the images),
`reportlab` (4 MB) and the Python interpreter with Tcl/Tk.

Collecting matplotlib with `collect_all` also pulled in `pandas`, `pyarrow`,
`cryptography` and `lxml` —107 MB— because `matplotlib.testing` imports them:
they are for matplotlib's own tests, not for this program. That is why the
matplotlib `.spec` collects only its data (fonts, `mpl-data`) and lets
PyInstaller's own hook add the `Agg` backend, and those four libraries are in
`excludes`. The result: from 340 MB to 202 MB, and the `.exe` files from 19.0 to
12.9 MB. If the `.spec` is ever touched, it is worth looking at the size of
`dist/`: a jump of 100 MB means they have come back in.

Use from a machine with no Python:

```powershell
.\FLAC_Verifier.exe                          # opens the window
.\FLAC_Verifier.exe --gui "D:\Music\Album"   # the window with the path already filled in
.\FLAC_Verifier.exe --cli                    # guided console mode
.\flac_motor.exe --path "D:\Music\Album" --pdf     # the engine on its own, for scripts
.\FLAC_Verifier.exe --version                # version
```

### Why there are two executables

A windowed executable (`--noconsole`) **has no console**, so PyInstaller leaves
`sys.stdout = None` and `print()` writes nothing. The engine talks to the GUI
through an NDJSON protocol over the stdout pipe: with a null stdout, the GUI would
sit waiting for events that never arrive. That is why `flac_motor.exe` is packaged
separately, **with** a console, which does have a real stdout.

The GUI looks for it next to itself and, if it does not find it (for example if
somebody copies only the windowed `.exe`), it uses the same binary with the
internal flag `--motor-cli`, which rebuilds the output streams before printing.
That is a fallback, not the normal path.

The `--motor-cli` flag **always** travels in packaged mode, also when
`flac_motor.exe` is used: that way the call does not depend on guessing which
binary it is. And the other way round, `flac_motor.exe` interprets whatever it
receives as engine options without needing any flags
(`flac_motor.exe --path ALBUM --pdf`), because its whole reason to exist is to be
the engine. The two rules together cover the renaming case: if somebody renames
the engine executable, the flag still decides.

### `multiprocessing.freeze_support()`: the line that cannot be moved

`main.py` starts its `__main__` block with `multiprocessing.freeze_support()`
**as the first statement**. In a frozen executable, every child process of the
parallel analysis starts the whole program again; without that call, instead of a
worker it starts a **complete copy of the application**: in the windowed `.exe`,
a new window per process, in a loop. There is a test
(`tests/test_empaquetado.py`) that checks it is still the first line.

### Warning: antivirus software complains (false positive)

It is common for Windows Defender, Avast or Kaspersky to flag a freshly compiled
PyInstaller `.exe` as suspicious. **It is not a virus**: antivirus software
distrusts unsigned binaries that carry an embedded Python interpreter, because
that is a pattern also used by some malicious programs. The project already takes
measures to reduce it:

- **No UPX** (`upx=False`): compressing the executable triggers far more alerts.
- **Version resources** (`version_info.txt`): the `.exe` presents itself with a
  name, version, description and copyright instead of being an anonymous binary.
- **Its own icon** on both executables.

If the alert appears, the normal thing to do is add an exclusion instead of
disabling protection:

**Windows Defender**

1. Start → "Windows Security" → *Virus & threat protection*.
2. *Virus & threat protection settings* → *Add or remove exclusions*.
3. *Add an exclusion* → **Folder** → the whole `FLAC_Verifier` folder (or the
   unzipped ZIP). The folder is enough: it covers both `.exe` files and
   `_internal/`.
4. If the file was already quarantined: *Protection history* → allow the item.

**Avast / AVG**

1. Menu → *Settings* → *Protection* → *Core Shields* → *Antivirus*.
2. *Exclusions* → *Add exclusion* → **Folder** → the program folder.
3. If Avast already blocked it, first restore it from *Quarantine* (Menu →
   *Quarantine*) and then add the exclusion.

**When handing it over**, warn the user about this in advance: it is what
surprises people the most and what usually makes somebody delete a legitimate
application. If the project is going to be distributed seriously, the real
solution is **signing the executable** with a (paid) code signing certificate and
submitting it to Microsoft for analysis.

### How to check that the executable came out right

`build.py` already runs a basic verification; for a complete check (the one worth
running on a machine **without Python**, which is where a missing module really
shows) there is a separate script:

```powershell
python comprobar_ejecutable.py                       # the 11 checks
python comprobar_ejecutable.py --quick               # only the version and a short analysis (steps 1-3)
python comprobar_ejecutable.py --album "D:\Music\Album"   # with your own album
```

The script runs **11 checks** grouped into 7 steps, and each step has its own time
limit, so a hang is detected and reported instead of staying there:

| Step | What it checks | Why it matters |
|---|---|---|
| 1 | `--version` on both `.exe` files | That they start and that the version resources are there |
| 2 | `flac_motor.exe --path … --workers 1` | That the engine works frozen, invoked the way a user would (no extra flags) |
| 3 | The same command with `--workers 8` | That `freeze_support()` is in place and the workers do not reopen the app |
| 4 | PDF report with spectrograms | That matplotlib and reportlab made it into the bundle |
| 5 | The exact command the GUI builds (it asks `gui.ruta_motor()`) and `FLAC_Verifier.exe --motor-cli` | That the subprocess the window launches is the right one and that the fallback flag exists |
| 6 | Opening and closing the window | That the GUI starts without leaving hung processes or windows |
| 7 | The frozen executable against the source code, on the same album | That packaging did not change the analysis: the records must match field by field (verdict, score, MD5, DR, problems…) |

Step 3 relies on the system allowing processes to be created. If it is run inside
an environment that blocks them (a sandbox, a security policy or a container
without permissions), `_sonda_procesos()` in the engine degrades to serial **on
purpose** and the check fails: it is not a failure of the executable, it is that
the parallel path could not be verified there. In a normal Windows console it
prints `paralelo: true`.

The output of the executables is redirected to a **file**, never to a pipe: when
the engine launches child processes, they inherit the pipe and the end of file
(`EOF`) does not arrive until the last one dies, so a read through a pipe waits
forever.

### What does NOT change when packaging

The analysis is exactly the same: the same verdicts, the same thresholds and the
same cache. Freezing only changes *where* Python lives, not *what* it computes.
The 229 tests pass in source mode before building, and afterwards the real
executable is checked: the demonstration album gives the same verdicts in both
cases.

---

## Visual identity

The logo design **is not new**: it is taken from `Logo/splashscreen.html`, the
application splash screen, and `Logo/generar_logo.py` reproduces it in the formats
each place needs (five white bars on a `#0f0f0d` background with a teal check
`#4f98a3`).

| File | What it is for |
|---|---|
| `Logo/logo_flac_verifier.png` | 1024×1024. Square icon: GUI header and the logo of this README |
| `Logo/logo_flac_verifier_transparente.png` | 1024×1024 with no background, to place it over any colour |
| `Logo/logo_flac_verifier_128.png` | 128×128 for `iconphoto` (Linux/macOS) |
| `Logo/icono_app.ico` | Multi-resolution (16, 24, 32, 48, 64, 128 and 256 px): window icon on Windows and icon of the future `.exe` |
| `Logo/logo_flac_verifier.svg` | Vector, for the web and the repository preview |
| `Logo/generar_logo.py` | Regenerates all of the above from the design: `python Logo/generar_logo.py` |

In the window, the header composes the 46 px brand mark next to the name and the
subtitle in the same teal as the splash screen, so a rasterised horizontal version
with the text is not needed.

> `Logo/vite.svg` and `Logo/tauri.svg` are the **Vite** and **Tauri** logos that
> came with the project scaffolding: they are third-party assets and are not used
> as the application's identity.

---

## JSON protocol contract

The engine writes **one JSON line per event** to stdout and always ends with
`fin`. `stderr` is left free for diagnostics. It is guaranteed that **all lines
are strict JSON**: no `NaN` and no `Infinity` (a non-finite float is emitted as
`null`).

### Events

| `tipo` | When | Fields |
|---|---|---|
| `inicio` | Once, before analysing | `total` (number of files), `workers` (processes), `paralelo` (bool), `cache` (bool) |
| `resultado` | Once per file | See below |
| `informe` | Only with `--pdf`, when the PDF was generated successfully | `ruta`, `paginas`, `total`, `desde_cache` |
| `aviso` | Only with `--pdf`, if the PDF could not be generated | `mensaje` |
| `fin` | Once, when finished | — |
| `error` | Only if the path is not valid or there are no `.flac` files | `mensaje` |

No file can abort the batch: an unreadable file produces its own `resultado` with
`error` and the process carries on until it emits `fin`.

### `resultado` — successful analysis

```json
{
  "tipo": "resultado", "indice": 1, "total": 3,
  "archivo": "track.flac", "ruta": "C:/Music/track.flac",
  "meta": { "valido": true, "sample_rate": 44100, "bits_per_sample": 24,
            "canales": 2, "duracion": 245.3, "md5": 12345 },
  "md5":  { "estado": "match", "calculado": "76a6…", "almacenado": "76a6…" },
  "clip": { "runs_clip": 0, "hay_clipping": false },
  "dr": 13.1,
  "bdi": { "aplica": true, "lsbs": "active",
           "resolucion_efectiva_bits": 24, "fraccion_16bit_grid": 9.8,
           "conclusion": "…" },
  "esp": { "error": null, "techo_hz": 20004, "techo_rel_nyquist": 0.9072,
           "clase_corte": "near_nyquist_cutoff", "corte_detectado": true,
           "frecuencia_corte": 19821, "distancia_nyquist": 0.8989,
           "corte_artificial": false, "ratio": 0.42439, "ratio_aplica": true,
           "var_alta_db": 683.6, "separacion_stereo": 0.438, "nyquist": 22050,
           "espectrograma": null },
  "score": 0.7, "veredicto": "PROBABLY LOSSLESS",
  "problemas": ["…"]
}
```

#### Facts (`meta`, `md5`, `clip`, `dr`, `bdi`, `esp`)

| Field | Meaning |
|---|---|
| `meta.md5` | Value of the STREAMINFO MD5 (128-bit integer), or `null` |
| `md5.estado` | `match` · `mismatch` · `absent` · `unverifiable` |
| `md5.calculado` / `almacenado` | Hashes as 32-character hex strings |
| `clip.runs_clip` | Runs of 3 or more consecutive samples at the ceiling (≥ 0.9999) |
| `dr` | **Estimated** dynamic range in dB, or `null` if the file is shorter than 6 s |
| `bdi.aplica` | `false` on 16-bit files or lower (the test makes no sense there) |
| `bdi.lsbs` | `empty` · `partial` · `active` · `indeterminate` |
| `bdi.resolucion_efectiva_bits` | Bits actually used (16 inside a 24-bit container → `16`) |
| `bdi.fraccion_16bit_grid` | % of samples aligned with the 16-bit grid |
| `esp.techo_hz` | Highest frequency with energy above −60 dB |
| `esp.techo_rel_nyquist` | That ceiling as a fraction of Nyquist |
| `esp.clase_corte` | `no_cutoff` · `near_nyquist_cutoff` (≥ 88 % of Nyquist) · `far_cutoff` (< 88 %) |
| `esp.frecuencia_corte` / `distancia_nyquist` | Where the step is and how far it is from Nyquist |
| `esp.corte_artificial` | `true` **only** if the cut-off is far away (confirmed accusation) |
| `esp.ratio` / `ratio_aplica` | Energy in the band above 18 kHz versus 15–18 kHz; `false` if that band does not exist |
| `esp.var_alta_db` | Variance of the spectrum above 18 kHz |
| `esp.separacion_stereo` | L−R difference in the high band versus the mid band; `null` in mono |
| `esp.espectrograma` | Path of the PNG, or `null` |

#### Interpretation (`score`, `veredicto`, `problemas`)

With the cache enabled, every `resultado` also carries **`desde_cache`** (`true` if
the file has not changed since the last analysis and the result was reused).

`veredicto` is one of: `GENUINE LOSSLESS`, `PROBABLY LOSSLESS`, `SUSPICIOUS`,
`PROBABLE UPSCALE`, `indeterminate`. `problemas` is the list of reasons, in plain
language; each line explains why score was subtracted.

### `resultado` — rejected file

The same identification fields plus `error`, and **without** `veredicto` or
`score`. A verdict is never emitted for a file that could not be analysed
completely. Cases:

| `error` (prefix) | Cause |
|---|---|
| `Not a valid FLAC signature` | It does not start with `fLaC`, or it is empty |
| `Could not read FLAC metadata (…)` | STREAMINFO unreadable or corrupt |
| `Could not decode the audio (…)` | Truncated or corrupt (the number of decoded samples is checked against the declared one) |
| `digital silence: …` | Peak below 1 LSB: there is no signal to analyse |
| `STREAMINFO MD5 does not match …` | Hard integrity failure |

---

## How the verdict is decided

The score is **positive evidence minus confirmed defects**. It is not an average
of everything measured: only what really distinguishes scores points.

**Positive evidence** (blocks that are added only if they are passed):

| Block | Weight | Condition |
|---|---|---|
| Full high band | 3 | Up to 48 kHz: ceiling ≥ 88 % of Nyquist (1.5 if ≥ 70 %). Above 48 kHz: ceiling ≥ 22.05 kHz, the CD limit |
| No anomalous step | 3 | `clase_corte == no_cutoff` |
| Hi-res content | 3 | Only if `sample_rate ≥ 88.2 kHz`: ceiling ≥ 22.05 kHz (1.5 if ≥ 18 kHz) |
| High/mid ratio | 2 | > 0.05 (1 if > 0.01) |
| High-band variance | 1 | > 3 dB² |
| Stereo separation | 1 | > 0.1 (not applicable in mono) |

**Confirmed defects** (they subtract and, except the last one, they block the
maximum verdict):

| Defect | Subtracts | Condition |
|---|---|---|
| Far artificial cut-off | 0.40 | Step below 88 % of Nyquist |
| Destroyed high band | 0.40 | Up to 48 kHz: ceiling < 70 % of Nyquist. Above 48 kHz: ceiling < 22.05 kHz |
| Hi-res with no high content | 0.40 | ≥ 88.2 kHz and ceiling < 18 kHz |
| Empty LSBs | 0.40 | > 97 % of samples on the 16-bit grid |
| Partially inactive LSBs | 0.15 | 80–97 % (does not block) |
| Declared resolution with no support | 0.25 | Claims > 16 bits at CD rate with no content above the CD band |

### Why hi-res is not measured in proportion to Nyquist

Up to 48 kHz the natural reference is Nyquist: a healthy CD has its ceiling at
88-100 % of it. **Above 48 kHz that proportion means nothing**: 70 % of 48 kHz is
33.6 kHz, a frequency where real acoustic music has no energy. A reference 96/24
master —recorded with converters whose analogue filter rolls off at 30 kHz— has
its ceiling at ~26 kHz and is perfectly legitimate, yet the proportional rule
accused it of a destroyed band.

That is why, from 48 kHz upwards, the reference is the **CD limit (22.05 kHz)**:
content above it is real ultrasonic content that a CD cannot have, and it is not
penalised. Below that, an upscale from 44.1/48 kHz to 96 kHz is still detected,
since by definition it cannot have anything above 22.05 kHz.

**Thresholds:** ≥ 0.80 `GENUINE LOSSLESS` · ≥ 0.60 `PROBABLY LOSSLESS` ·
≥ 0.40 `SUSPICIOUS` · the rest `PROBABLE UPSCALE`. If there is any blocking
defect, the verdict is limited to `SUSPICIOUS` (score ≥ 0.50) or `PROBABLE
UPSCALE`: a file with a transcoding signature **cannot** come out as genuine.

### What no longer scores (and why)

- **The spectral ceiling at 44.1/48 kHz.** Measured: a real 320 kbps MP3 and a CD
  master with an anti-alias filter at 20 kHz are spectrally identical (ceiling
  91 % of Nyquist, ratio 0.42, variance ~700 dB² **in both**). No ceiling
  threshold can separate them, so a cut-off glued to Nyquist is reported as
  **inconclusive** instead of accusing a legitimate master.
- **Active LSBs.** They only prove that the container uses its depth, not where
  the audio comes from: a lossy transcode stored at 24 bits also fills the LSBs.
  The test can only disprove, never confirm.
- **The presence of the MD5.** It is a separate fact. That the field exists says
  nothing; that it **matches** does, and if it does not match the file is corrupt
  and receives no verdict.

---

## Per-album PDF report

With `--pdf` (or by answering "y" in the interactive menu),
**`flac_verifier_report.pdf`** is generated, with this content:

| Page | Content |
|---|---|
| **Cover** | Title, path of the analysed album, date and time of the analysis, spectral mode used and **executive summary**: how many files are in each verdict, with colour (green genuine · yellow probably lossless · orange suspicious · red upscale or error) and their percentage |
| **One per song** | File name, **verdict with score** in a coloured band, table of technical data (format, duration, size, MD5 integrity, clipping, dynamic range, low bits and effective resolution, spectral ceiling, cut-off and its distance to Nyquist, ratio, variance, L-R separation), **list of problems in plain language** and the **embedded spectrogram** |
| **Final** | Overall table with all the songs: number, file, verdict, score and number of problems |

Behaviour details:

- **Location**: in the folder of the analysed album. If a single file is analysed,
  next to that file.
- **Spectrograms**: they are generated automatically for the report, without
  needing to pass `--png`. They are also left in `<folder>/_spectrograms/`.
- **A song's record always fits on its page**: before writing it, the space taken
  by the verdict, the table and the problems is measured, and the spectrogram is
  fitted to the remaining space (between 34 and 62 mm) keeping its proportions.
  That way it cannot overflow onto the next page and be left loose, separated from
  its title. If the list of problems is so long that not even the minimum fits,
  the title and the image move together to the next page (`KeepTogether`).
- **Without spectrograms** (for example with no `matplotlib`) the report is still
  generated and the note `(no image: …)` appears instead.
- **Files with an error** (corrupt, digital silence…) take up their page with the
  reason, marked in red and with no verdict: one is never invented for them.
- **It never aborts the analysis**: if the PDF cannot be written (permissions, no
  `reportlab`, full disk), a clear message is shown
  (`⚠️ Could not generate the PDF report: …`) and the rest of the work is already
  done and safe. In the JSON protocol that is emitted as an `aviso` event instead
  of `informe`.

---

## Known limits

They are physical limits, not bugs. They are measured and documented on purpose.

1. **A 320 kbps MP3 stored at 16 bits is not detected.** Its cut-off (~20.3 kHz)
   is indistinguishable from the anti-alias filter of a CD master. The 320 kbps
   case that is detected is the one in a 24-bit container, because of the
   incoherence between the declared resolution and the content. Detecting the
   16-bit one would require a trained classifier (auCDtect style), outside the
   scope of this tool.
2. **A CD master with a filter at 20 kHz comes out as `PROBABLY LOSSLESS`**, not
   as GENUINE: a cut-off glued to Nyquist costs its whole block instead of
   accusing. That is deliberate.
3. **A genuine 24-bit file at 44.1/48 kHz whose band ends below 95 % of Nyquist**
   receives the "declared resolution with no support" penalty and is blocked. It
   is the price of the rule that detects case 1.
4. **The step detector requires a drop of more than 25 dB over ~500 Hz.** A
   gradual band loss is detected by the ceiling and ratio blocks, not by the
   cut-off detector.
5. **`dr` is not the DR of the reference meter** (3 s blocks with their own
   calibration and gating). It is computed on a mono mix, so it is not comparable
   with published values; it is informative and does not score.
6. **Spectral analysis at 44.1/48 kHz is structurally unable to certify
   provenance.** That is why the MD5 is verified separately: it is the only
   objective and total check of the format.
7. **A hi-res file whose content does not go past the CD limit (22.05 kHz)
   receives the band penalty.** That is deliberate: it means its 96 kHz band is
   not supported by the content, which happens in an upscale from 44.1/48 kHz and
   also in a high-rate master that has been trimmed. A 96/24 master with an
   analogue roll-off at 30 kHz (ceiling ~26 kHz) does not fall into that case.
8. **The system environment can leave a matplotlib warning in the log.** The
   first time it draws, matplotlib writes a font cache; the engine gives it its
   own writable folder (`%LOCALAPPDATA%\FLAC_VERIFIER`, or the temporary
   directory if that one cannot be used), but there are systems that also block
   the lock file matplotlib creates next to the cache. When that happens,
   matplotlib warns on `stderr` and the GUI log shows that line loose among the
   JSON events. The analysis comes out identical: only the font cache is rebuilt
   on each run (a couple of seconds the first time spectrograms are requested).

---

## Performance

The analysis makes **a single streaming pass** with bounded memory. Measured with
`--mode full` (whole track) over 10 minutes of audio:

| Fixture | Path | Time | Python peak | Working set peak |
|---|---|---|---|---|
| 10 min 16/44.1 (423 MB in float64) | before | 5.4 s | 1818 MB | 1931 MB |
| | now | **4.3 s** | **151 MB** | **264 MB** |
| | | ×1.3 | ×12 | ×7.3 |
| 10 min 24/96 (922 MB in float64) | before | 18.4 s | 6142 MB | 6255 MB |
| | now | **10.3 s** | **296 MB** | **409 MB** |
| | | ×1.7 | ×20.7 | ×15.3 |

Per phase (same 10-minute signal):

| Phase | Before | Now | Note |
|---|---|---|---|
| Clipping | 2053 ms (per-sample loop) | 733 ms | ×2.8; memory 423 MB → 4 MB |
| Dynamic range | 378 ms | 381 ms | no time improvement: the benefit is not materialising the 212 MB mono array |
| Spectrum | 1.37 s / 1010 MB | 1.33 s / 128 MB | same time, ×7.9 less memory (45 s pieces) |
| MD5 | 0.84 s | 0.84 s | unavoidable work, but in the same pass |
| Mono FFT | 50 ms | 25 ms | reuses the single STFT |

Both paths produce **the same verdict, the same score and the same problems**;
there is only numerical drift in `ratio`, `var_alta_db` and `separacion_stereo`
of the order of 0.02–0.5 %, which does not change any decision.

### Full album with a PDF report

Measured with a real 17-track album in 24/96 (1.2 GB), `--mode full --pdf`,
16 cores:

| | Before | Now | Improvement |
|---|---|---|---|
| **First pass** (analysis + 17 spectrograms + PDF) | 339.7 s | **26.3 s** | **×12.9** |
| **Second pass** (cache) | 339.7 s | **2.7 s** | **×127** |

Where each improvement comes from:

| Change | Measured effect |
|---|---|
| Spectrogram with `imshow` instead of `pcolormesh(shading="gouraud")` | 339.7 s → 75.1 s (×4.5). The PNG cost 10.4 s per track, 84 % of the analysis; drawing 2049×1500 interpolated cells was the bottleneck |
| Parallel analysis (8 processes) | 75.1 s → 26.3 s |
| Per-track cache | 26.3 s → 2.7 s (and without starting processes: it is resolved by reading 17 JSON files) |

The new spectrogram is a **faithful raster** of the same power matrix: compared
with the previous one, same frame and same data, the correlation of the plot area
is 0.9979 and the mean difference 2.2/255. The 17 PNG files weigh 17.3 MB
(17.7 before) and the PDF 3.15 MB (3.54 before).

Tunable parameters in `motor_flac.py`: `FRAMES_POR_BLOQUE` (reading),
`SEGUNDOS_ESPECTRO` (STFT pieces), `MAX_MUESTRAS_BITS` (sample for the low-bit
test), `MAX_FRAMES_PNG`, `DPI_ESPECTROGRAMA`, `MAX_WORKERS` and
`MIN_ARCHIVOS_PARALELO`.

---

## Cache

Every analysed track leaves an entry in
`<folder>/_spectrograms/_cache/<file>.<mode>.json` with the complete result and
the fingerprint of what produced it. Repeating a verification neither decodes nor
draws anything again.

It is reused only if **all** of these match:

| Part of the key | What it includes |
|---|---|
| File identity | Size + modification date + **STREAMINFO MD5** |
| Analysis parameters | `VERSION_MOTOR`, spectral mode and seconds |
| Render fingerprint | PNG parameters (`huella_render()`), and that the PNG still exists |

That is: if the audio changes, even with the same size and the same date, the
STREAMINFO MD5 gives it away and it is analysed again. Each mode has its own
entry, so alternating `center` and `full` does not recompute.

It is disabled with `--no-cache`. When any analysis calculation changes,
`VERSION_MOTOR` in `motor_flac.py` must be raised: that invalidates all the old
caches. It currently reads `VERSION_MOTOR = "9.0"`.

---

## Tests

```
python -m unittest discover -s tests -v      # verbose
python -m unittest discover -s tests         # summary
python tests\test_md5.py                     # a single module
```

229 tests, ~60 s, **with no extra dependencies** (unittest from the standard
library). The FLAC fixtures are generated by themselves in a temporary directory.
The window tests need a display; to skip them (CI without a desktop):
`FLAC_VERIFIER_SIN_GUI=1 python -m unittest discover -s tests`.

GitHub Actions runs the same command on Ubuntu and Windows, with Python 3.10 and
3.12, on every push and pull request (`.github/workflows/ci.yml`). The badge at the
top of this file reports the last run.

Two tests are skipped depending on the environment, and in both cases the skip is
the right answer: the one that checks that the parallel path really uses processes
(if the system does not allow creating them) and the one that covers operation
**without** `tkinterdnd2` (if the library is installed).

| Module | What it pins down |
|---|---|
| `test_blindaje.py` | No broken file raises exceptions; the happy path works |
| `test_protocolo.py` | `inicio`/N results/`fin` always; strict JSON with no `NaN`; 32 kHz without `NaN` |
| `test_md5.py` | MD5 verified at 8/16/24 bits; `mismatch` is a hard error; the MD5 does not score |
| `test_veredictos.py` | Silence guard; CD master with no accusation; real MP3 (128/320 kbps) does not pass; bit depth only penalises; hi-res with a 30 kHz roll-off is not penalised, and the CD-to-96-kHz upscale is penalised |
| `test_rendimiento.py` | The streaming path gives the same as the array path (oracle); vectorisation identical to the naive loop; runs that cross blocks and that end with the file |
| `test_cache_y_paralelo.py` | The cache reuses and gives the same, and is invalidated by date, size, MD5, mode or engine version; the parallel path matches the sequential one and preserves order; with no processes it degrades to serial; the PNG is drawn with `imshow` |
| `test_informe_pdf.py` | The PDF is generated where it should be, has one page per song plus cover and summary, contains the expected text (its content is really read back), embeds the spectrograms, works without them, no record overflows its page and no PDF failure aborts the analysis |
| `test_gui.py` | Subprocess command with `-u` and `CREATE_NO_WINDOW`; NDJSON event parsing (including unreadable lines); formatting of results and of the colour summary; process life cycle and cancellation (including the `kill` fallback and the tree with `taskkill`); real-time events with a real subprocess; and the hidden window with its full flow |
| `test_estructura.py` | Importing executes nothing; the CLI does not duplicate the engine; UTF-8 with a redirected stdout; clean EOF |
| `test_empaquetado.py` | `freeze_support()` is the first statement of `main.py`; the flag dispatch of `main.py`; the engine command in source mode, packaged and without the engine `.exe`; the `.spec` collects everything (packages with data, soundfile DLLs, scipy submodules, own modules, two `EXE` with the correct console, icon, version, no UPX); `version_info.txt` is coherent; `build.py` builds |
| `test_idioma.py` | The language of the project: no text literal of the application and tool modules contains Spanish characters or Spanish words, and neither do any comment or docstring of any Python file in the repository (only the static parts of an f-string are inspected, so an interpolated variable name is not mistaken for prose; JSON protocol keys, reportlab style names and Spanish identifiers are treated as internal names and allowed). It also pins the shared verdict labels across engine, GUI and PDF, the English command-line flags, and the report and spectrogram folder names |

`tests/test_veredictos.py` generates real MP3 transcodes with `ffmpeg` if it is on
the `PATH` (otherwise those tests are skipped).

---

## Related projects

**[FLAC Detective](https://github.com/Guillain-RDCDE/FLAC_Detective)** answers the
same question from a different design: a Python library and CLI you install and
script — on PyPI, with Docker images, a beets plugin, CSV/HTML reports, wider
codec coverage and its own desktop GUI extra.

This project is the other design: a self-contained Windows application whose unit
of work is the album, with a per-album PDF report, an STREAMINFO MD5 integrity
check that sits outside the verdict scoring, and a build verified as a build.
`COMPARISON.md` compares the two approaches side by side, with the commands that
produce every number.

---

## Licence and authorship

Personal project. Use it and modify it freely. See `LICENSE` (MIT).
