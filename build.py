"""Automatiza la construcción del ejecutable de FLAC VERIFIER.

    python build.py                # limpia, comprueba PyInstaller y compila
    python build.py --no-clean  # reutiliza build/ (compilación incremental)

Qué hace, en orden:
  1. Comprueba la versión de Python y que PyInstaller esté disponible (lo instala
     si falta).
  2. Borra build/ y dist/ para que no se mezclen restos de compilaciones previas.
  3. Compila con flac_verifier.spec (modo carpeta, dos ejecutables).
  4. Verifica el resultado: que existan los .exe, su tamaño, y que respondan a
     `--version` (sin abrir ninguna ventana).

No requiere permisos de administrador ni toca nada fuera de esta carpeta (salvo
la instalación de PyInstaller, si falta).
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time

AQUI = os.path.dirname(os.path.abspath(__file__))
SPEC = os.path.join(AQUI, "flac_verifier.spec")
DIST = os.path.join(AQUI, "dist")
BUILD = os.path.join(AQUI, "build")
CARPETA = os.path.join(DIST, "FLAC_Verifier")
EXE_GUI = os.path.join(CARPETA, "FLAC_Verifier.exe")
EXE_MOTOR = os.path.join(CARPETA, "flac_motor.exe")
PYTHON_MINIMO = (3, 8)


def _utf8() -> None:
    """UTF-8 en la salida: en Windows, con la salida redirigida a un archivo o
    tubería, cp1252 no puede con los marcas ✓/✗ y el script moriría él solo.

    Además se activa el vaciado por línea: con la salida por una tubería Python
    la retiene en bloques y no se vería avanzar la compilación hasta el final.
    """
    for nombre in ("stdout", "stderr"):
        flujo = getattr(sys, nombre, None)
        try:
            flujo.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
        except (AttributeError, ValueError, OSError):
            pass


def paso(numero: int, texto: str) -> None:
    print(f"\n[{numero}] {texto}")
    print("-" * 72)


def comprobar_python() -> None:
    if sys.version_info < PYTHON_MINIMO:
        raise SystemExit(f"Python {PYTHON_MINIMO[0]}.{PYTHON_MINIMO[1]} "
                         f"or newer is required (you have {sys.version.split()[0]}).")
    print(f"  Python {sys.version.split()[0]} in {sys.executable}")


def asegurar_pyinstaller() -> None:
    try:
        import PyInstaller
        print(f"  PyInstaller {PyInstaller.__version__} is already installed")
        return
    except ImportError:
        print("  PyInstaller is not installed; installing it…")
    resultado = subprocess.run([sys.executable, "-m", "pip", "install", "pyinstaller"],
                               text=True)
    if resultado.returncode != 0:
        raise SystemExit("Could not install PyInstaller. Install it manually:\n"
                         "    pip install pyinstaller")
    print("  PyInstaller installed")


def limpiar() -> None:
    for carpeta in (BUILD, DIST):
        if os.path.isdir(carpeta):
            shutil.rmtree(carpeta, ignore_errors=True)
            print(f"  removed {os.path.basename(carpeta)}/")
    for resto in ("__pycache__",):
        ruta = os.path.join(AQUI, resto)
        if os.path.isdir(ruta):
            shutil.rmtree(ruta, ignore_errors=True)
    if not os.path.exists(os.path.join(AQUI, "Logo", "icono_app.ico")):
        print("  warning: Logo/icono_app.ico is missing; building with the default icon")


def compilar() -> float:
    if not os.path.exists(SPEC):
        raise SystemExit(f"Cannot find {os.path.basename(SPEC)}")
    comando = [sys.executable, "-m", "PyInstaller", SPEC, "--noconfirm", "--clean",
               "--distpath", DIST, "--workpath", BUILD]
    print("  " + " ".join(comando[2:]))
    inicio = time.perf_counter()
    resultado = subprocess.run(comando, cwd=AQUI)
    if resultado.returncode != 0:
        raise SystemExit("The build failed. Check the PyInstaller message above.")
    return time.perf_counter() - inicio


def verificar() -> None:
    for etiqueta, ruta in (("graphical interface", EXE_GUI), ("engine", EXE_MOTOR)):
        if not os.path.exists(ruta):
            raise SystemExit(f"  ✗ missing {os.path.basename(ruta)}")
        print(f"  ✓ {os.path.basename(ruta):20s} {os.path.getsize(ruta) / 1e6:6.1f} MB "
              f"({etiqueta})")

    # Que respondan sin abrir ninguna ventana. El motor escribe la versión en
    # stdout; el de ventana no tiene consola, así que ahí basta con el código de
    # salida (que no reviente).
    for ruta in (EXE_GUI, EXE_MOTOR):
        proceso = subprocess.run([ruta, "--version"], capture_output=True, text=True,
                                 timeout=180,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        salida = (proceso.stdout or proceso.stderr or "").strip().splitlines()
        marca = "✓" if proceso.returncode == 0 else "✗"
        print(f"  {marca} {os.path.basename(ruta)} --version -> "
              f"exit code {proceso.returncode}, {salida[0] if salida else '(sin salida)'}")

    total = sum(os.path.getsize(os.path.join(raiz, nombre))
                for raiz, _, ficheros in os.walk(CARPETA)
                for nombre in ficheros)
    print(f"  total size of dist/FLAC_Verifier: {total / 1e6:.0f} MB")


def main() -> int:
    parser = argparse.ArgumentParser(description="Build FLAC_Verifier.exe")
    parser.add_argument("--no-clean", dest="sin_limpiar", action="store_true",
                        help="do not remove build/ or dist/ before building")
    args = parser.parse_args()

    _utf8()
    print("=" * 72)
    print("BUILDING FLAC VERIFIER 1.0.0")
    print("=" * 72)

    paso(1, "Checking the environment")
    comprobar_python()
    asegurar_pyinstaller()

    paso(2, "Removing leftovers from previous builds")
    if args.sin_limpiar:
        print("  (skipped by --no-clean)")
    else:
        limpiar()

    paso(3, "Building (this can take several minutes)")
    segundos = compilar()
    print(f"  finished in {segundos / 60:.1f} minutes")

    paso(4, "Verifying the result")
    verificar()

    print("\n" + "=" * 72)
    print("READY")
    print("=" * 72)
    print(f"  Distributable folder: {CARPETA}")
    print(f"  Executable          : {EXE_GUI}")
    print("\n  Copy that whole folder to the target machine (not just the .exe:")
    print("  the DLLs and the data live inside _internal) and run FLAC_Verifier.exe.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
