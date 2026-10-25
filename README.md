<p align="center">
  <img src="Logo/logo_flac_verifier.png" alt="FLAC VERIFIER" width="120">
</p>

# FLAC VERIFIER

[![CI](https://github.com/FraysKosher/flac-verifier/actions/workflows/ci.yml/badge.svg)](https://github.com/FraysKosher/flac-verifier/actions/workflows/ci.yml)
[![Version: 2.3.0](https://img.shields.io/badge/version-2.3.0-blue)](CHANGELOG.md)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Tests: 266](https://img.shields.io/badge/tests-266-brightgreen)](README.md#tests)

See [`CHANGELOG.md`](CHANGELOG.md) for the version history (current: **2.3.0**).

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
└── tests/                266 tests (unittest, no extra dependencies)
```

All the logic lives in `motor_flac.py`. `verificar_flac.py` only formats the
output, `gui.py` only paints the window and `informe_pdf.py` only draws the PDF:
none of them reimplements analysis (there are tests watching over that).
`main.py` is only a flag dispatcher towards those four modules.

Analysing an album is spread across **processes** (up to 8) and keeps a
**per-track cache**, so repeating a verification is almost instantaneous.

---

## Installation

Prebuilt Windows release:
[FLAC Verifier 2.3.0](https://github.com/FraysKosher/flac-verifier/releases/tag/v2.3.0).

From source:

```
pip install -r requirements.txt
```

### Verify the Windows download

The release ZIP includes a SHA-256 checksum. On Windows:

```powershell
Get-FileHash .\FLAC_Verifier_2.3.0-win64.zip -Algorithm SHA256
```

Compare the output with `SHA256SUMS_2.3.0.txt` from the same release. A matching
hash means the ZIP arrived intact; a mismatch means it was corrupted or replaced,
so download it again from the official release page.

Verified with Python 3.10.11, numpy 2.2.6, scipy 1.15.3, soundfile 0.13.1,
mutagen 1.47.0, pillow 12.3.0, reportlab 5.0.1 and customtkinter 5.2.2.

Three dependencies are **optional** and the program works without them:

| Dependency | What it is for | If missing |
|---|---|---|
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
| Spectral mode | `center` (default), `full` or `seconds` with a slider from 5 to 120 s. The analysis now measures the most active blocks of the whole track, so the mode only labels the cache entry |
| Generate PDF report | Checked by default |
| Parallel processing | Automatic (up to 8 processes) or a fixed number; off = sequential |
| Ignore cache | Forces an analysis from scratch |
| Analyze / Cancel | Enabled and disabled according to the process life cycle |
| Open PDF report | Enabled when finished; opens the file with the system viewer |

While it is analysing: a per-file progress bar, a log with timestamps and a
colour per verdict, and when it finishes a status card with the batch summary
(green, yellow, orange or red). Engine warnings and errors are written to the log
(only a fatal failure opens a native dialog), without closing the application.

**How it works inside** (so that it does not freeze on Windows):

1. The engine is launched in a **separate process** with
   `subprocess.Popen([sys.executable, "-u", "motor_flac.py", …])` and
   `CREATE_NO_WINDOW`. The `-u` flag is what makes the NDJSON events arrive **in
   real time** instead of at the end; without it, the stdout buffer would hold
   them back.
2. A **daemon thread** reads stdout and stderr and puts them in a
   `queue.Queue`; no widget is touched from that thread.
3. The window drains that queue in **bounded batches** (`LOTE_DRENADO` events per
   tick, always on the interface thread) and reschedules with a short interval
   while there is a backlog, so a huge batch cannot freeze the window and
   "Cancel" stays responsive. It keeps draining while the process is alive **or**
   while the reader thread still is, so the final `__fin__` event is never lost
   even if the engine finishes right after a pass.
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
| `--mode` | `seconds` \| `center` \| `full` | Spectral mode label (default `center`). The energy-guided selector always covers the whole track, so the value only identifies the cache entry |
| `--seconds` | integer | Starting-seconds label when `--mode seconds` (default 15); the selector still covers the whole track |
| `--png` | — | Saves one spectrogram per track in `<folder>/_spectrograms/` |
| `--pdf` | — | Generates `flac_verifier_report.pdf` in the album folder (implies generating the spectrograms; requires `reportlab`) |
| `--workers` | integer | Parallel processes. `0` (default) = automatic up to 8; `1` = sequential |
| `--no-cache` | — | Ignores the cache and does not write it (to force an analysis from scratch) |

`--pdf` and `--png` are independent and can be combined. `--pdf` generates the
spectrograms on its own even if `--png` is not passed, because the report embeds
them.

The GUI always passes the internal `--motor-cli` flag (`FLAC_Verifier.exe
--motor-cli …`, or `python main.py --motor-cli …` in source mode), so the call
does not depend on the executable name; `flac_motor.exe` accepts the same engine
options without that flag. `--motor-cli` is not an option of `motor_flac.py`; it
is the dispatcher flag that rebuilds the output streams in the windowed
executable.

---

## Distribution: a single `.exe` for Windows with no Python

To hand it to somebody who does not have Python installed, it is built with
PyInstaller. One command:

```powershell
cd "mis_proyectos_python\FLAC VERIFIER"
python -m pip install pyinstaller      # once
python build.py                        # cleans, builds and verifies (~1 min)
```

`build.py` does four things and fails with a clear message if something does not
add up: it checks the interpreter and PyInstaller, deletes `build/` and `dist/`,
builds the `.spec`, **normalises the Windows mandatory integrity label** of the
result (see MIC below) and **verifies the result** by launching the compiled
executables (version, a real analysis and the presence of the engine). With
`--no-clean` it reuses what has already been built.

`build/` is the PyInstaller scaffolding (about 43 MiB): you can delete it whenever
you like, `build.py` recreates it on the next build.

### What is left in `dist/FLAC_Verifier/`

```
dist/FLAC_Verifier/
├── FLAC_Verifier.exe     the application (no console: double click and done)
├── flac_motor.exe        the engine in a console, which the GUI launches as a subprocess
└── _internal/            Python, numpy, scipy, Pillow, reportlab, tk…
```

The whole folder is copied or compressed into a ZIP: **the `.exe` alone does not
work**, it needs its `_internal/` next to it. That is about **157 MiB (164 MB)**
on disk and **67 MiB (70 MB)** in `FLAC_Verifier_2.3.0-win64.zip`, because it
includes NumPy, SciPy and the Python interpreter; the current spec no longer
bundles matplotlib, so it is smaller. The first launch takes a few seconds (that is normal: there is
nothing to unpack in `%TEMP%`, which is exactly the reason for using folder mode
instead of a single `.exe`).

Where those 157 MiB come from: `scipy` with its DLLs (≈63 MiB), the OpenBLAS
libraries that numpy and scipy each ship (≈39 MB together), `PIL` (≈6 MB, used for
the spectrograms and the PDF images), `reportlab` (≈4 MB) and the Python
interpreter with Tcl/Tk and `libsndfile`.

The 2.3.0 build also trimmed what the program never loads: `PIL._avif` (an image
codec it never opens, ≈7.5 MB), `reportlab.graphics` (the chart stack `platypus`
never imports), **matplotlib** (the spectrogram is now rendered with Pillow) and
the non-Windows `tkinterdnd2` builds (only `win-x64` is kept). `collect_all()` was also called with `include_py_files=False`: by default
it copies every `.py` again as *data*, which bloated the bundle and defeated the
`excludes`.

Matplotlib used to drag in `pandas`, `pyarrow`, `cryptography` and `lxml`
—107 MB— through `matplotlib.testing`. The whole package is now in `excludes`, so
those libraries stay out as well. If the `.spec` is ever touched, it is worth
looking at the size of `dist/`: a jump of 100 MB means they have come back in.

> One exclusion is deliberately **not** made: `scipy.optimize._highspy`. The
> engine never solves a linear program, but `import scipy.signal` pulls in
> `scipy.optimize._linprog_highs`, which imports `_highspy` eagerly — removing it
> would break the engine at start-up. The `.spec` records this in a comment.

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

### Windows mandatory integrity label (MIC)

A Windows process carries a *mandatory integrity level* (Low, Medium, High) and
the kernel enforces **no-write-up**: a Low process cannot write into a Medium
folder. If the repository or the virtual environment was created inside a
Low-inherited workspace, every `.exe` built there inherits the Low label, and
the packaged PDF step fails with `PermissionError` on the album folder even
though the same `.exe` works when launched from a normal console.

Since 2.3.0, `build.py` normalises the label of `dist/FLAC_Verifier` to Medium
after building, and `comprobar_ejecutable.py` does the same on the folder it is
about to test. The check also reports the caller's token integrity if the PDF
step fails, so a Low-inherited environment is diagnosed instead of blamed on
`reportlab`. The release ZIP is built from the Medium-labelled folder.

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
| 4 | PDF report with spectrograms | That Pillow and reportlab made it into the bundle |
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
The 266 tests pass in source mode before building, and afterwards the real
executable is checked: the demonstration album gives the same verdicts in both
cases.

---

## Visual identity

`Logo/generar_logo.py` defines the brand mark and generates every format the
application needs (five white bars on a `#0f0f0d` background with a teal check
`#4f98a3`). The design is kept in that script, which is the single source of truth
for the proportions.

| File | What it is for |
|---|---|
| `Logo/logo_flac_verifier.png` | 1024×1024. Square icon: GUI header and the logo of this README |
| `Logo/logo_flac_verifier_transparente.png` | 1024×1024 with no background, to place it over any colour |
| `Logo/logo_flac_verifier_128.png` | 128×128 for `iconphoto` (Linux/macOS) |
| `Logo/icono_app.ico` | Multi-resolution (16, 24, 32, 48, 64, 128 and 256 px): window icon on Windows and icon of the future `.exe` |
| `Logo/logo_flac_verifier.svg` | Vector, for the web and the repository preview |
| `Logo/generar_logo.py` | Regenerates all of the above from the design: `python Logo/generar_logo.py` |

In the window, the header composes the 46 px brand mark next to the name and the
subtitle in the same teal as the brand, so a rasterised horizontal version with the
text is not needed.

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
           "forense_bits": { "es_padded_16_en_24": false, "trailing_zeros_lsb": 0,
                             "lsb8_entropia_bits": 7.2, "lsb8_cajones_ocupados": 250,
                             "paso_reticula_mediano": 1.0,
                             "es_24bit_escalado_en_peine": false },
           "conclusion": "…" },
  "esp": { "error": null, "techo_hz": 20004, "techo_rel_nyquist": 0.9072,
           "clase_corte": "near_nyquist_cutoff", "corte_detectado": true,
           "frecuencia_corte": 19821, "distancia_nyquist": 0.8989,
           "corte_artificial": false, "ratio": 0.42439, "ratio_aplica": true,
           "var_alta_db": 683.6, "separacion_stereo": 0.438, "nyquist": 22050,
           "perfil_dsd": null, "gradiente_borde_db_khz": 12.4,
           "centroide_hz": 1420.0, "decaimiento_acustico_suave": false,
           "brecha_p90_p10_agudos_db": 0.0, "ratio_apagado_sfb21": 0.0,
           "islas_mdct_sfb21": false, "corr_env_excitador": 0.0,
           "muesca_excitador_db": 0.0, "sospecha_excitador_armonico": false,
           "espectrograma": null },
  "score": 0.7, "veredicto": "PROBABLY LOSSLESS",
  "problemas": ["…"],
  "evidencias_forenses": ["Spectral ceiling 20.0 kHz (91% of Nyquist)",
                          "STREAMINFO MD5 verified (match)"]
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
| `bdi.forense_bits` | Native-integer audit (24-bit only): `es_padded_16_en_24`, `trailing_zeros_lsb`, `lsb8_entropia_bits`, `lsb8_cajones_ocupados`, `muestras_pasaje_bajo`, `paso_reticula_mediano`, `es_24bit_escalado_en_peine`, `es_flujo_dop`, `dop_pares_inspeccionados`, `dop_ratio` |
| `esp.techo_hz` | Highest frequency with energy above −60 dB |
| `esp.techo_rel_nyquist` | That ceiling as a fraction of Nyquist |
| `esp.clase_corte` | `no_cutoff` · `near_nyquist_cutoff` (≥ 88 % of Nyquist) · `far_cutoff` (< 88 %) |
| `esp.frecuencia_corte` / `distancia_nyquist` | Where the step is and how far it is from Nyquist |
| `esp.corte_artificial` | `true` **only** if the cut-off is far away (confirmed accusation) |
| `esp.ratio` / `ratio_aplica` | Energy in the band above 18 kHz versus 15–18 kHz; `false` if that band does not exist |
| `esp.var_alta_db` | Variance of the spectrum above 18 kHz |
| `esp.separacion_stereo` | L−R difference in the high band versus the mid band; `null` in mono |
| `esp.caida_side_agudos_db` | Extra Mid−Side drop in 15.5–19.8 kHz versus 2–8 kHz (joint-stereo collapse) |
| `esp.delta_corte_ms_hz` | Mid cut-off minus Side cut-off: a lossy joint-stereo encoder cuts Side first |
| `esp.sospecha_joint_stereo` | `true` when the Side channel collapses and cuts before Mid |
| `esp.es_dual_mono` | `true` when L and R are identical (Side more than 45 dB below Mid) |
| `esp.iqr_corte_hz` | Interquartile range of the per-frame cut-off in high-energy frames (codec jitter) |
| `esp.ratio_huecos_mdct` | Fraction of high-energy frames with a 600 Hz MDCT hole (> 22 dB below both neighbours) |
| `esp.muro_brickwall_nyquist` | `true` for a Hi-Res container with a cliff at 22.05/24 kHz (upscale with a sinc filter) |
| `esp.espejo_aliasing_detectado` | `true` when the 23–29 kHz band mirrors 15–21 kHz (unfiltered interpolator) |
| `esp.rolloff_analogico_genuino` | `true` for a smooth analogue/acoustic roll-off with a continuous ultrasonic floor |
| `esp.gradiente_nyquist_db_khz` | Steepest slope around the CD/DAT Nyquist, in dB/kHz |
| `esp.salto_ultrasonico_db` | Level step between 18–20.8 kHz and 23.5–36 kHz |
| `esp.correlacion_espejo_22k` | Detrended Pearson r of the mirrored bands |
| `esp.es_transferencia_dsd` | `true` for a DSD/SACD noise-shaping ramp in 24–40 kHz |
| `esp.perfil_dsd` | DSD profile: `direct_24_40k`, `scarlet_30k`, `dsd128_40_80k`, or `null` (no DSD ramp) |
| `esp.gradiente_borde_db_khz` | Steepest slope of the transition band around the ceiling, in dB/kHz |
| `esp.centroide_hz` | Spectral centroid: a low-centroid acoustic mix sits below 1800 Hz |
| `esp.decaimiento_acustico_suave` | `true` for a low-centroid, no-cutoff, ≥ 11 kHz ceiling with a gentle (< 20 dB/kHz) edge: the acoustic exemption candidate |
| `esp.brecha_p90_p10_agudos_db` | P90–P10 energy gap in 16–20.5 kHz: large values expose intermittent sfb21 islands |
| `esp.ratio_apagado_sfb21` | Fraction of high-energy frames whose sfb21 band is switched off |
| `esp.islas_mdct_sfb21` | `true` for intermittent 16–20.5 kHz islands (28 dB gap, 0.25–0.85 shut-off): lowpass-disabled codec signature |
| `esp.corr_env_excitador` | Correlation between the 8–14 kHz envelope and the high band |
| `esp.muesca_excitador_db` | Depth of the notch left where the original high band was cut |
| `esp.sospecha_excitador_armonico` | `true` when correlation > 0.92 **and** notch ≥ 15 dB: harmonic exciter / SBR signature |
| `esp.sospecha_corte_opus_20k` | `true` for a 20.00 kHz cliff (> 45 dB between 19.85 and 20.15 kHz) with a flat 18–19.7 kHz band |
| `esp.caida_opus_20k_db` | Depth of that cliff in dB |
| `esp.clipping_eventos` / `clipping_longitud_media` | Saturation runs and their mean length |
| `esp.clipping_fraccion_cortos` | Fraction of clipping events lasting only 1–2 samples |
| `esp.clipping_reconstruccion_lossy` | `true` for Gibbs overshoot (short events with a band limit below 20.5 kHz) |
| `esp.clipping_mastering_limitador` | `true` for 4+ sample plateaus with content up to Nyquist (observation, not a defect) |
| `esp.espectrograma` | Path of the PNG, or `null` |

#### Interpretation (`score`, `veredicto`, `problemas`)

With the cache enabled, every `resultado` also carries **`desde_cache`** (`true` if
the file has not changed since the last analysis and the result was reused).
When a batch is analysed as an album, a rescued acoustic/low-HF track also
carries **`coherencia_album: true`** and the note
`Acoustic/low-HF track validated by album-level master consistency`.

`veredicto` is one of: `GENUINE LOSSLESS`, `PROBABLY LOSSLESS`, `SUSPICIOUS`,
`PROBABLE UPSCALE`, `indeterminate`. `problemas` is the list of reasons, in plain
language; each line explains why score was subtracted. `evidencias_forenses` is
an additive list of short technical evidence lines (spectral ceiling, MD5 match,
DSD/sfb21/exciter signatures, 24-bit lattice…), so a consumer can explain the
verdict without re-deriving the detectors.

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
| Analogue/DSD morphology | 3 | `rolloff_analogico_genuino` or `es_transferencia_dsd`: the declared band is backed by a smooth roll-off / DSD ramp |
| Acoustic exemption | 1.5 | `decaimiento_acustico_suave` + active LSB lattice + no lossy signature: a low-centroid acoustic ceiling is explained, not destroyed |

**Confirmed defects** (they subtract; the blocking ones limit the maximum
verdict — see the note after the table):

| Defect | Subtracts | Condition |
|---|---|---|
| Far artificial cut-off | 0.40 | Step below 88 % of Nyquist |
| Destroyed high band | 0.40 | Up to 48 kHz: ceiling < 70 % of Nyquist. Above 48 kHz: ceiling < 22.05 kHz |
| Hi-res with no high content | 0.40 | ≥ 88.2 kHz and ceiling < 18 kHz |
| Empty LSBs | 0.40 | > 97 % of samples on the 16-bit grid |
| Partially inactive LSBs | 0.15 | 80–97 % (does not block) |
| Declared resolution with no support | 0.25 | Claims > 16 bits at CD rate with no content above the CD band (blocks) |
| Joint-stereo collapse | 0.30 | Side channel drops at HF (`sospecha_joint_stereo`) |
| Cut-off jitter | 0.20 | IQR > 550 Hz (frame-adaptive codec; does not block) |
| MDCT holes | 0.25 | > 10 % of high-energy frames with a 600 Hz hole (does not block) |
| sfb21 islands | 0.25 | Intermittent 16–20.5 kHz islands (blocks) |
| Harmonic exciter / SBR | 0.30 | Envelope correlation > 0.92 and notch ≥ 15 dB (blocks) |
| Opus 20 kHz cliff | 0.20 | 20.00 kHz cliff with other codec evidence (does not block) |
| Gibbs clipping | 0.25 | 1–2 sample clipping events with a band limit < 20.5 kHz (does not block) |
| 16-in-24 padding | 0.40 | Every sample has trailing zero bits (blocks) |
| 24-bit quantisation comb | 0.40 | Global lattice step in the 8 LSBs (blocks) |
| Brickwall / aliasing | 0.40 | Hi-Res cliff at 22.05/24 kHz or mirrored 23–29 kHz (blocks) |

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
The blocking defects are the 0.40 ones plus `islas_mdct_sfb21` (0.25), the
harmonic exciter (0.30) and the declared-resolution penalty (0.25); the jitter,
MDCT-hole, Opus and Gibbs penalties do not block on their own.

### What no longer scores (and why)

- **The spectral ceiling at 44.1/48 kHz.** Measured: a real 320 kbps MP3 and a CD
  master with an anti-alias filter at 20 kHz are spectrally identical (ceiling
  91 % of Nyquist, ratio 0.42, variance ~700 dB² **in both**). No ceiling
  threshold can separate them, so a cut-off glued to Nyquist is reported as
  **inconclusive** instead of accusing a legitimate master.
- **Active LSBs on their own.** They prove that the container uses its depth,
  not where the audio comes from: a lossy transcode stored at 24 bits also fills
  the LSBs. The 8D.1 rule is fail-closed, though: only an active lattice can
  earn the acoustic exemption or the 24-bit transition-band excuse, so `empty`,
  `partial`, `indeterminate` and missing LSBs block it instead of passing.
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
- **Without spectrograms** (for example with no `Pillow`) the report is still
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

1. **A 320 kbps MP3 stored at 16 bits is not detected when it has no
   joint-stereo collapse.** Its cut-off (~20.3 kHz) is indistinguishable from
   the anti-alias filter of a CD master. The 320 kbps case that *is* detected is
   the joint-stereo one (the benchmark's MP3 320 case) via the Mid/Side vector,
   and the 24-bit repackaged one via the declared-resolution rule. Detecting the
   plain 16-bit 320 would require a trained classifier (auCDtect style), outside
   the scope of this tool.
2. **A CD master with a filter well below Nyquist (below 70 %) comes out as
   `PROBABLY LOSSLESS` or `SUSPICIOUS`**, not as GENUINE: the high band is
   reduced, not destroyed. Between 70 % and 88 % the high-band block still
   gives 1.5 points, so a clean master can be `GENUINE`; a near-Nyquist filter
   is inconclusive. A low-centroid acoustic master can get the 8D exemption
   below 70 % when the LSB lattice is active and there is no codec signature.
3. **A 24-bit file at 44.1/48 kHz whose band ends below 88 % of Nyquist** is
   blocked by the declared-resolution rule, unless the LSB lattice is active,
   the track is an acoustic/low-centroid master (`decaimiento_acustico_suave`)
   and no lossy signature is present: then the 8D.1 fail-closed exemption can
   rescue it. Between 88 % and 95 % the Vector 5 rule excuses it only with a
   clean lattice and no codec/brickwall signature. That is the price of
   detecting case 1, and 8D.1 is deliberately conservative.
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
8. **The spectrogram renderer writes nothing to `stderr`.** Since Phase 3 it is a
   NumPy + Pillow raster, with no font cache, no lock file and no backend: the
   JSON lines the GUI reads are never mixed with library warnings.

---

## Performance (2.1.0/2.2.0 baselines)

The analysis makes **a single streaming pass** with bounded memory. The tables
below are the 2.1.0/2.2.0 baselines; the 2.3.0 Phase 8 detectors add a bounded
per-block cost (the full 2.3.0 numbers have not been re-measured). Measured with
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

Both paths produce **the same verdict, the same score and the same problems**.
`test_rendimiento.py` pins it exactly: the streaming path and the array path (the
oracle) must return identical values, field by field, including `ratio`,
`var_alta_db` and `separacion_stereo`.

### 2.1.0: vectorising the hot paths

The streaming pass was already memory-bounded; 2.1.0 made the arithmetic itself
faster, without changing any result (verified field by field):

| Hot spot | 2.1.0 change | Isolated gain |
|---|---|---|
| Spectral cut-off search (`_metricas_espectrales`) | Sliding-window loop `O(n·paso)` → `np.cumsum` `O(n)` | **×57** |
| `_pcm_bytes` (MD5 dump, 16-bit) | `<i8` round-trip → contiguous `<i1`/`<i2`/`<i4` | **×3.7** |
| Dynamic range (`_dr_desde_rms`) | `sorted()` `O(n log n)` → `np.partition` `O(n)` | **×13** |
| Spectrogram render | float64 `log10` → float32 in place, decimate first | **×1.35** |

On a real 21-track album (463 MB, 16/44.1 kHz, `--mode full`):

| Scenario | Before | After | Saving |
|---|---|---|---|
| Full album, no PNG | 29.46 s | **19.14 s** | **−35 %** |
| Full album, with PNG | 47.95 s | **32.49 s** | **−32 %** |
| Peak RAM, with PNG | 734 MB | **570 MB** | **−164 MB** |

The verdicts are identical before and after (`15 GENUINE LOSSLESS ·
5 PROBABLY LOSSLESS · 1 PROBABLE UPSCALE` on that album). The GUI got the same
treatment: it drains the engine queue in bounded batches, the `__fin__` race is
fixed, and the event list, the log and the queue are bounded.

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
caches. It currently reads `VERSION_MOTOR = "9.8"` and
`VERSION_ESQUEMA_CACHE = "2.3.0"`.

---

## Tests

```
python -m unittest discover -s tests -v      # verbose
python -m unittest discover -s tests         # summary
python tests\test_md5.py                     # a single module
```

266 tests, ~70 s in the reference `.venv` (the exact time depends on the
machine), **with no extra dependencies** (unittest from the standard library).
The FLAC fixtures are generated by themselves in a temporary directory.
The window tests need a display; to skip them (CI without a desktop):
`FLAC_VERIFIER_SIN_GUI=1 python -m unittest discover -s tests`.

GitHub Actions runs the same command on Ubuntu and Windows, with Python 3.10 and
3.12, on every push and pull request (`.github/workflows/ci.yml`). The badge at the
top of this file reports the last run.

A few tests skip themselves depending on the environment, and in every case the
skip is the right answer: the parallel path really uses processes (skipped if the
system does not allow creating them), operation **without** `tkinterdnd2`
(skipped if the library is installed), and the real-MP3 cases (skipped if `ffmpeg`
is not on the `PATH`).

| Module | What it pins down |
|---|---|
| `test_blindaje.py` | No broken file raises exceptions; the happy path works |
| `test_protocolo.py` | `inicio`/N results/`fin` always; strict JSON with no `NaN`; 32 kHz without `NaN` |
| `test_md5.py` | MD5 verified at 8/16/24 bits; `mismatch` is a hard error; the MD5 does not score |
| `test_veredictos.py` | Silence guard; CD master with no accusation; real MP3 (128/320 kbps) does not pass; bit depth only penalises; hi-res with a 30 kHz roll-off is not penalised, and the CD-to-96-kHz upscale is penalised |
| `test_rendimiento.py` | The streaming path gives the same as the array path (oracle); vectorisation identical to the naive loop; runs that cross blocks and that end with the file |
| `test_forense_riguroso.py` | The Phase 8 detectors: 24-bit lattice, DSD/Scarlet/DSD128 profiles, sfb21 islands, harmonic exciter, acoustic centroid, 8D.1 fail-closed and album coherence |
| `test_cache_y_paralelo.py` | The cache reuses and gives the same, and is invalidated by date, size, MD5, mode or engine version; the parallel path matches the sequential one and preserves order; with no processes it degrades to serial; the PNG is drawn with `imshow` |
| `test_informe_pdf.py` | The PDF is generated where it should be, has one page per song plus cover and summary, contains the expected text (its content is really read back), embeds the spectrograms, works without them, no record overflows its page and no PDF failure aborts the analysis |
| `test_gui.py` | Subprocess command with `-u` and `CREATE_NO_WINDOW`; NDJSON event parsing (including unreadable lines); formatting of results and of the colour summary; process life cycle and cancellation (including the `kill` fallback and the tree with `taskkill`, and the reader-thread liveness `hilo_activo` the window uses so as not to lose the final event); real-time events with a real subprocess; and the hidden window with its full flow |
| `test_estructura.py` | Importing executes nothing; the CLI does not duplicate the engine; UTF-8 with a redirected stdout; clean EOF |
| `test_empaquetado.py` | `freeze_support()` is the first statement of `main.py`; the flag dispatch of `main.py`; the engine command in source mode, packaged and without the engine `.exe`; the `.spec` collects what it needs (packages with data, soundfile DLLs, scipy submodules, own modules, two `EXE` with the correct console, icon, version, no UPX, `include_py_files=False` and the exclusion list); `version_info.txt` is coherent; `build.py` builds |
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

A second batch runs through `analizar_archivos`: 8 homogeneous GENUINE tracks and
1 acoustic low-ceiling track in the same directory. The acoustic track must be
raised to PROBABLY LOSSLESS with `coherencia_album = true`:

**PASS**

<!-- BENCHMARK:END -->
