# -*- mode: python ; coding: utf-8 -*-
"""Especificación de PyInstaller para FLAC VERIFIER 1.0.0 (Windows 10/11 x64).

Se construye en modo CARPETA (`--onedir`): es lo recomendado para aplicaciones
con NumPy/SciPy/Matplotlib porque arranca al instante y no descomprime cientos de
megas en %TEMP% en cada ejecución (ni bloquea al abrir varios procesos a la vez,
que es justo lo que hace el análisis en paralelo).

Se generan DOS ejecutables que comparten la misma carpeta:

    FLAC_Verifier.exe   la interfaz gráfica (sin consola: --noconsole)
    flac_motor.exe      el motor en consola, que la GUI lanza como subproceso

El motivo de separarlos: un ejecutable de ventana no tiene consola, así que
PyInstaller deja `sys.stdout = None` y `print()` no escribe nada: el protocolo
NDJSON llegaría vacío y la GUI se quedaría colgada esperando eventos. El motor,
al ser de consola, tiene stdout real y la tubería funciona. La GUI lo lanza con
CREATE_NO_WINDOW, así que no aparece ninguna ventana negra.

Construcción:
    pyinstaller flac_verifier.spec --noconfirm --clean       (o python build.py)
"""
from PyInstaller.utils.hooks import (collect_all, collect_data_files,
                                     collect_dynamic_libs, collect_submodules)

# ─── Recolección de datos, binarios e importaciones ocultas ─────────────────
datas: list = []
binaries: list = []
hiddenimports: list = []

# Paquetes que PyInstaller no ve entero por sí solo: temas JSON y fuentes de
# customtkinter, los TCL/TK y las DLL de tkdnd, la libsndfile de soundfile, y
# las fuentes/datos de reportlab y PIL.
for paquete in ("customtkinter", "tkinterdnd2", "soundfile", "reportlab", "PIL"):
    d, b, h = collect_all(paquete)
    datas += d
    binaries += b
    hiddenimports += h

# Matplotlib se recoge SOLO por sus datos (fuentes, mpl-data, stylelib…). Con
# `collect_all` entrarían también sus submódulos, y entre ellos matplotlib.testing
# arrastra pandas, pyarrow, cryptography y lxml: 107 MB de bibliotecas que este
# proyecto no usa para nada (el backend Agg y pyplot los añade el hook propio de
# PyInstaller, que además detecta el `matplotlib.use("Agg")` del motor).
datas += collect_data_files("matplotlib")

# La DLL nativa de libsndfile, por si el hook no la clasifica como binario.
binaries += collect_dynamic_libs("soundfile")
binaries += collect_dynamic_libs("_soundfile_data")

# Submódulos que PyInstaller suele omitir en scipy/numpy y que este proyecto usa
# (scipy.signal.stft/welch para el espectro, scipy.fft y numpy.fft por debajo).
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
# Se recogen los submódulos, pero sin los paquetes de tests: no se ejecutan nunca
# y solo hacen bulto (y ruido en el informe de la compilación).
hiddenimports += [m for m in collect_submodules("scipy.signal") if ".tests" not in m]
hiddenimports += [m for m in collect_submodules("scipy.fft") if ".tests" not in m]

# Módulos propios que solo se importan dentro de funciones (import perezoso): si
# no se declaran, PyInstaller no los encuentra y el .exe falla al usarlos.
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

# ─── Datos propios: la identidad visual ────────────────────────────────────
# Van a la carpeta del bundle, así que gui.py los encuentra por su ruta relativa.
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

# ─── Análisis ──────────────────────────────────────────────────────────────
a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # Se excluyen herramientas pesadas que no se usan en ejecución.
    excludes=[
        "pytest", "setuptools", "pip", "wheel",
        "IPython", "jupyter", "notebook", "nbformat",
        "matplotlib.tests", "numpy.tests", "scipy.tests",
        "PyQt5", "PyQt6", "PySide2", "PySide6", "wx",
        # Entraban por matplotlib.testing (que ya no se recoge) y suman más de
        # 100 MB: nada de lo que hace este programa los necesita, y dejarlos en
        # la lista evita que un hook futuro los arrastre otra vez.
        "pandas", "pyarrow", "cryptography", "lxml",
        "openpyxl", "xlrd", "sqlalchemy", "ipykernel", "sphinx",
    ],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

# ─── La interfaz gráfica (sin consola) ─────────────────────────────────────
exe_gui = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=NOMBRE_GUI,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                 # UPX dispara los falsos positivos de los antivirus
    console=False,             # --noconsole: sin ventana negra de fondo
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=ICONO,
    version=VERSION,
)

# ─── El motor en consola (lo lanza la GUI) ─────────────────────────────────
exe_motor = EXE(
    pyz,
    a.scripts,                 # mismo punto de entrada: main.py reparte por banderas
    [],
    exclude_binaries=True,
    name=NOMBRE_MOTOR,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,              # consola real: stdout es un flujo válido
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=ICONO,
    version=VERSION,
)

# ─── Carpeta de distribución ───────────────────────────────────────────────
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
