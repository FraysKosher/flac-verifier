# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller specification for FLAC VERIFIER 2.3.0 (Windows 10/11 x64).

The build uses FOLDER mode (`--onedir`): this is the recommended choice for
applications with NumPy/SciPy/Pillow because it starts instantly and does not
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
import glob
import importlib.util
import os

from PyInstaller.utils.hooks import (collect_all, collect_dynamic_libs,
                                     collect_submodules)

# ─── Collection of data, binaries and hidden imports ────────────────────────
datas: list = []
binaries: list = []
hiddenimports: list = []

# Packages that PyInstaller does not see in full on its own: the JSON themes and
# fonts of customtkinter, the TCL/TK and the tkdnd DLLs, the libsndfile of
# soundfile, and the fonts/data of reportlab and PIL.
for paquete in ("customtkinter", "tkinterdnd2", "soundfile", "reportlab", "PIL"):
    # include_py_files=False: the source `.py` files are already compiled into the
    # PYZ; copying them again as *data* only bloats the bundle (and defeats the
    # `excludes` below, which drop unused modules but cannot drop data copies).
    d, b, h = collect_all(paquete, include_py_files=False)
    datas += d
    binaries += b
    hiddenimports += h

# The spectrogram is rendered with NumPy + Pillow (no matplotlib at all).

# The native libsndfile DLL, in case the hook does not classify it as a binary.
binaries += collect_dynamic_libs("soundfile")
binaries += collect_dynamic_libs("_soundfile_data")

# Ship only SciPy's compiled C++ pypocketfft extension (~1 MB) instead of the
# scipy Python package (~25 MB) and duplicate scipy.libs OpenBLAS (~19 MB).
# `_motor_espectral._get_pocketfft()` loads this extension directly via
# `importlib.util.spec_from_file_location` when frozen.
_scipy_spec = importlib.util.find_spec("scipy")
if _scipy_spec and _scipy_spec.submodule_search_locations:
    _pocketfft_dir = os.path.join(
        _scipy_spec.submodule_search_locations[0], "fft", "_pocketfft"
    )
    for _pattern in ("pypocketfft*.pyd", "pypocketfft*.so"):
        for _ext in glob.glob(os.path.join(_pocketfft_dir, _pattern)):
            binaries.append((_ext, "scipy/fft/_pocketfft"))

# ─── Trim what the app never loads ───────────────────────────────────────────
# Sources of dead weight that `collect_all` above pulls in:
#   * tkinterdnd2 ships one native tkdnd build per platform and ABI; this is a
#     Windows x64 build against Tcl/Tk 8.6, so only `win-x64` is ever loaded
#     (the Tcl 9, Linux, macOS, arm64 and x86 builds cannot run here).
#   * Pillow bundles optional codecs (AVIF, WebP…) the program never opens: it
#     only reads PNG (spectrograms, icons) and writes JPEG (for the PDF).
def _conservar(origen):
    ruta = str(origen).replace("\\", "/").lower()
    if "tkinterdnd2/tkdnd/" in ruta:
        return "tkinterdnd2/tkdnd/win-x64/" in ruta
    base = os.path.basename(ruta)
    return "_avif" not in base and "_webp" not in base

datas    = [(s, d) for (s, d) in datas if _conservar(s)]
binaries = [(s, d) for (s, d) in binaries if _conservar(s)]

# Submodules that PyInstaller may omit in numpy / importlib.resources.
hiddenimports += [
    "numpy.fft",
    "numpy.fft.helper",
    "importlib.resources",
]
# A single module on Python 3.10, a package on 3.12: collect whatever exists.
hiddenimports += collect_submodules("importlib.resources")

# Own modules that are only imported inside functions (lazy import): if they are
# not declared, PyInstaller does not find them and the .exe fails when using them.
hiddenimports += [
    "motor_flac",
    "_motor_bits",
    "_motor_espectral",
    "_motor_veredicto",
    "_motor_cache",
    "informe_pdf",
    "verificar_flac",
    "gui",
    "theme",
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
    ("LICENSE", "."),
    ("Logo/logo_flac_verifier.png", "Logo"),
    ("Logo/logo_flac_verifier_transparente.png", "Logo"),
    ("Logo/logo_flac_verifier_128.png", "Logo"),
    ("Logo/logo_flac_verifier.svg", "Logo"),
    ("Logo/icono_app.ico", "Logo"),
    # Line icons of the interface (DESIGN.md 8), whole folder in one entry.
    ("Logo/icons", "Logo/icons"),
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
        "matplotlib", "numpy.tests", "scipy.tests",
        "PyQt5", "PyQt6", "PySide2", "PySide6", "wx",
        # They used to enter through matplotlib.testing (now excluded): nothing
        # this program does needs them, and keeping them in the list stops a
        # future hook from dragging them in again.
        "pandas", "pyarrow", "cryptography", "lxml",
        "openpyxl", "xlrd", "sqlalchemy", "ipykernel", "sphinx",
        # Optional codecs the app never opens (only PNG in, JPEG out).
        "PIL._avif", "PIL._webp",
        # reportlab's chart/graphics stack: `platypus` never imports it (verified),
        # so the report renders fine without it.
        "reportlab.graphics",
        # Unused stdlib binary/dev stacks (offline desktop tool; hashlib MD5 uses
        # libcrypto via _hashlib, never TLS/SSL or SQLite).
        "ssl", "_ssl", "sqlite3", "_sqlite3",
        "unittest", "pydoc", "doctest", "turtle", "turtledemo",
        # Entire scipy Python package and duplicate OpenBLAS are excluded:
        # polyphase resampling is pure NumPy and STFT/MDCT load pypocketfft directly.
        "scipy", "scipy.fft", "scipy.special", "scipy.linalg", "scipy.sparse",
        "scipy.signal", "scipy.optimize", "scipy.spatial", "scipy.interpolate",
        "scipy.stats", "scipy.io", "scipy.integrate", "scipy.ndimage",
        "scipy.cluster", "scipy.odr", "scipy.datasets", "scipy.fftpack",
    ],
    noarchive=False,
    optimize=0,
)

# Post-Analysis TOC filter: strip Tcl timezone/locale catalogs (~700 files),
# duplicate OpenBLAS from scipy.libs, and unused TLS/SQLite binaries while
# keeping _tcl_data/encoding, core Tk scripts, pypocketfft, and libcrypto intact.
def _keep_toc_entry(entry):
    dest = str(entry[0]).replace("\\", "/").lower()
    src = str(entry[1]).replace("\\", "/").lower()
    if any(seg in dest for seg in ("_tcl_data/tzdata/", "_tcl_data/msgs/",
                                   "tcl/tzdata/", "tcl/msgs/", "tk/msgs/")):
        return False
    if ("scipy.libs" in dest or "scipy.libs" in src) and "numpy.libs" not in src:
        return False
    base = os.path.basename(dest)
    if base in ("_ssl.pyd", "_sqlite3.pyd", "sqlite3.dll") or base.startswith("libssl"):
        return False
    return True

a.datas = [entry for entry in a.datas if _keep_toc_entry(entry)]
a.binaries = [entry for entry in a.binaries if _keep_toc_entry(entry)]

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
