# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller specification for FLAC VERIFIER 2.0.0 (Windows 10/11 x64).

The build uses FOLDER mode (`--onedir`): this is the recommended choice for
applications with NumPy/SciPy/Matplotlib because it starts instantly and does not
unpack hundreds of megabytes into %TEMP% on every run (nor does it block when
several processes open at once, which is exactly what the parallel analysis
does).

TWO executables are generated and they share the same folder:

    FLAC_Verifier.exe   the graphical interface (no console: --noconsole)
    flac_motor.exe      the console engine, which the GUI launches as a subprocess

The reason for separating them: a windowed executable has no console, so
PyInstaller leaves `sys.stdout = None` and `print()` writes nothing: the NDJSON
protocol would arrive empty and the GUI would hang waiting for events. Since the
engine is a console build, it has a real stdout and the pipe works. The GUI
launches it with CREATE_NO_WINDOW, so no black window appears.

Build:
    pyinstaller flac_verifier.spec --noconfirm --clean       (or python build.py)
"""
from PyInstaller.utils.hooks import (collect_all, collect_data_files,
                                     collect_dynamic_libs, collect_submodules)

# ─── Collection of data, binaries and hidden imports ────────────────────────
datas: list = []
binaries: list = []
hiddenimports: list = []

# Packages that PyInstaller does not see in full on its own: the JSON themes and
# fonts of customtkinter, the TCL/TK and the tkdnd DLLs, the libsndfile of
# soundfile, and the fonts/data of reportlab and PIL.
for paquete in ("customtkinter", "tkinterdnd2", "soundfile", "reportlab", "PIL"):
    d, b, h = collect_all(paquete)
    datas += d
    binaries += b
    hiddenimports += h

# Matplotlib is collected ONLY by its data (fonts, mpl-data, stylelib…). With
# `collect_all` its submodules would enter as well, and among them matplotlib.testing
# drags in pandas, pyarrow, cryptography and lxml: 107 MB of libraries that this
# project does not use at all (the Agg backend and pyplot are added by
# PyInstaller's own hook, which also detects the `matplotlib.use("Agg")` of the engine).
datas += collect_data_files("matplotlib")

# The native libsndfile DLL, in case the hook does not classify it as a binary.
binaries += collect_dynamic_libs("soundfile")
binaries += collect_dynamic_libs("_soundfile_data")

# Submodules that PyInstaller usually omits in scipy/numpy and that this project
# uses (scipy.signal.stft/welch for the spectrum, scipy.fft and numpy.fft underneath).
hiddenimports += [
    "numpy.fft",
    "numpy.fft.helper",
    "scipy.fft",
    "scipy.fft._pocketfft",
    "scipy.fftpack",
    "scipy.signal",
    "scipy.signal.windows",
    "scipy.signal._spectral_py",
    "scipy.special",
    "scipy.linalg",
    "scipy.sparse",
    "scipy._lib",
    "scipy.io",
]
# The submodules are collected, but without the test packages: they never run
# and are only dead weight (and noise in the build report).
hiddenimports += [m for m in collect_submodules("scipy.signal") if ".tests" not in m]
hiddenimports += [m for m in collect_submodules("scipy.fft") if ".tests" not in m]

# Own modules that are only imported inside functions (lazy import): if they are
# not declared, PyInstaller does not find them and the .exe fails when using them.
hiddenimports += [
    "motor_flac",
    "informe_pdf",
    "verificar_flac",
    "gui",
    "argparse",
    "multiprocessing",
    "multiprocessing.spawn",
    "multiprocessing.pool",
    "concurrent.futures",
    "concurrent.futures.process",
    "hashlib",
    "mutagen.flac",
    "soundfile",
]

# ─── Own data: the visual identity ─────────────────────────────────────────
# They go to the bundle folder, so gui.py finds them by their relative path.
datas += [
    ("Logo/logo_flac_verifier.png", "Logo"),
    ("Logo/logo_flac_verifier_transparente.png", "Logo"),
    ("Logo/logo_flac_verifier_128.png", "Logo"),
    ("Logo/logo_flac_verifier.svg", "Logo"),
    ("Logo/icono_app.ico", "Logo"),
]

ICONO = "Logo/icono_app.ico"
VERSION = "version_info.txt"
NOMBRE_GUI = "FLAC_Verifier"
NOMBRE_MOTOR = "flac_motor"

# ─── Analysis ──────────────────────────────────────────────────────────────
a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # Heavy tools that are not used at runtime are excluded.
    excludes=[
        "pytest", "setuptools", "pip", "wheel",
        "IPython", "jupyter", "notebook", "nbformat",
        "matplotlib.tests", "numpy.tests", "scipy.tests",
        "PyQt5", "PyQt6", "PySide2", "PySide6", "wx",
        # They entered through matplotlib.testing (which is no longer collected)
        # and add up to more than 100 MB: nothing this program does needs them,
        # and keeping them in the list stops a future hook dragging them in again.
        "pandas", "pyarrow", "cryptography", "lxml",
        "openpyxl", "xlrd", "sqlalchemy", "ipykernel", "sphinx",
    ],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

# ─── The graphical interface (no console) ──────────────────────────────────
exe_gui = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=NOMBRE_GUI,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                 # UPX triggers antivirus false positives
    console=False,             # --noconsole: no black window behind it
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=ICONO,
    version=VERSION,
)

# ─── The console engine (launched by the GUI) ──────────────────────────────
exe_motor = EXE(
    pyz,
    a.scripts,                 # same entry point: main.py dispatches by flags
    [],
    exclude_binaries=True,
    name=NOMBRE_MOTOR,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,              # real console: stdout is a valid stream
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=ICONO,
    version=VERSION,
)

# ─── Distribution folder ───────────────────────────────────────────────────
coll = COLLECT(
    exe_gui,
    exe_motor,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="FLAC_Verifier",      # -> dist/FLAC_Verifier/
)
