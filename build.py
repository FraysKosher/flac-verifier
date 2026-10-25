"""Automates the build of the FLAC VERIFIER executable.

    python build.py                # clean, check PyInstaller and build
    python build.py --no-clean  # reuse build/ (incremental build)

What it does, in order:
  1. Checks the Python version and that PyInstaller is available (it installs it
     if missing).
  2. Removes build/ and dist/ so that leftovers from previous builds do not mix in.
  3. Builds with flac_verifier.spec (folder mode, two executables) and, on
     Windows, normalises the mandatory integrity label of the result.
  4. Verifies the result: that the .exe files exist, their size, and that they
     respond to `--version` (without opening any window).

It requires no administrator rights and touches nothing outside this folder
(except the PyInstaller installation, if missing).
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
FOLDER = os.path.join(DIST, "FLAC_Verifier")
EXE_GUI = os.path.join(FOLDER, "FLAC_Verifier.exe")
EXE_ENGINE = os.path.join(FOLDER, "flac_motor.exe")
MINIMUM_PYTHON = (3, 8)


def project_version() -> str:
    """The version the build announces, read from the single place that defines it.

    It used to be a literal in the banner, which is how the tag, the release and
    the executable ended up saying different numbers. `main.py` owns `VERSION`, so
    the build reads it from there.
    """
    import main
    return main.VERSION


def _utf8() -> None:
    """UTF-8 on the output: on Windows, with the output redirected to a file or a
    pipe, cp1252 cannot cope with the ✓/✗ marks and the script would die on its own.

    Line flushing is also enabled: with the output through a pipe Python holds it
    back in blocks and the build would not be seen advancing until the end.
    """
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        try:
            stream.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
        except (AttributeError, ValueError, OSError):
            pass


def step_size(number: int, text: str) -> None:
    print(f"\n[{number}] {text}")
    print("-" * 72)


def check_python() -> None:
    if sys.version_info < MINIMUM_PYTHON:
        raise SystemExit(f"Python {MINIMUM_PYTHON[0]}.{MINIMUM_PYTHON[1]} "
                         f"or newer is required (you have {sys.version.split()[0]}).")
    print(f"  Python {sys.version.split()[0]} in {sys.executable}")


def ensure_pyinstaller() -> None:
    try:
        import PyInstaller
        print(f"  PyInstaller {PyInstaller.__version__} is already installed")
        return
    except ImportError:
        print("  PyInstaller is not installed; installing it…")
    result = subprocess.run([sys.executable, "-m", "pip", "install", "pyinstaller"],
                               text=True)
    if result.returncode != 0:
        raise SystemExit("Could not install PyInstaller. Install it manually:\n"
                         "    pip install pyinstaller")
    print("  PyInstaller installed")


def clean() -> None:
    for folder in (BUILD, DIST):
        if os.path.isdir(folder):
            shutil.rmtree(folder, ignore_errors=True)
            print(f"  removed {os.path.basename(folder)}/")
    for resto in ("__pycache__",):
        path = os.path.join(AQUI, resto)
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
    if not os.path.exists(os.path.join(AQUI, "Logo", "icono_app.ico")):
        print("  warning: Logo/icono_app.ico is missing; building with the default icon")


def normalize_integrity_windows(folder: str) -> None:
    """Sets the Medium mandatory integrity label on everything under `carpeta`.

    A virtual environment or a workspace that was created from a low-integrity
    context carries an inheritable `Low` label, and PyInstaller copies that label
    into every file it writes. The packaged executables would then run Low and
    could not write into a normal (Medium) folder, so the PDF step would fail
    with PermissionError for reasons that have nothing to do with the code. The
    label is normalised after building, which is the only moment when the folder
    exists and the inherited label has already been copied.
    """
    if os.name != "nt" or not os.path.isdir(folder):
        return
    command = ["icacls", folder, "/setintegritylevel", "(OI)(CI)M", "/T", "/Q"]
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=120,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"  note: could not normalise the integrity label "
              f"({type(exc).__name__}: {exc})")
        return
    if result.returncode != 0:
        print(f"  note: icacls returned {result.returncode} while normalising "
              f"the integrity label; check the folder by hand")
        return
    print(f"  ✓ {os.path.basename(folder)}/ now carries the Medium integrity label")


def compilar() -> float:
    if not os.path.exists(SPEC):
        raise SystemExit(f"Cannot find {os.path.basename(SPEC)}")
    command = [sys.executable, "-m", "PyInstaller", SPEC, "--noconfirm", "--clean",
               "--distpath", DIST, "--workpath", BUILD]
    print("  " + " ".join(command[2:]))
    start = time.perf_counter()
    result = subprocess.run(command, cwd=AQUI)
    if result.returncode != 0:
        raise SystemExit("The build failed. Check the PyInstaller message above.")
    normalize_integrity_windows(FOLDER)
    return time.perf_counter() - start


def verify() -> None:
    for label, path in (("graphical interface", EXE_GUI), ("engine", EXE_ENGINE)):
        if not os.path.exists(path):
            raise SystemExit(f"  ✗ missing {os.path.basename(path)}")
        print(f"  ✓ {os.path.basename(path):20s} {os.path.getsize(path) / 1e6:6.1f} MB "
              f"({label})")

    # That they respond without opening any window. The engine writes the version
    # to stdout; the windowed one has no console, so there the exit code is enough
    # (that it does not blow up).
    for path in (EXE_GUI, EXE_ENGINE):
        process = subprocess.run([path, "--version"], capture_output=True, text=True,
                                 timeout=180,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        output = (process.stdout or process.stderr or "").strip().splitlines()
        brand = "✓" if process.returncode == 0 else "✗"
        print(f"  {brand} {os.path.basename(path)} --version -> "
              f"exit code {process.returncode}, {output[0] if output else '(sin salida)'}")

    total = sum(os.path.getsize(os.path.join(root, name))
                for root, _, files in os.walk(FOLDER)
                for name in files)
    print(f"  total size of dist/FLAC_Verifier: {total / 1e6:.0f} MB")


def main() -> int:
    parser = argparse.ArgumentParser(description="Build FLAC_Verifier.exe")
    parser.add_argument("--no-clean", dest="without_clean", action="store_true",
                        help="do not remove build/ or dist/ before building")
    args = parser.parse_args()

    _utf8()
    print("=" * 72)
    print("BUILDING FLAC VERIFIER " + project_version())
    print("=" * 72)

    step_size(1, "Checking the environment")
    check_python()
    ensure_pyinstaller()

    step_size(2, "Removing leftovers from previous builds")
    if args.without_clean:
        print("  (skipped by --no-clean)")
    else:
        clean()

    step_size(3, "Building (this can take several minutes)")
    seconds = compilar()
    print(f"  finished in {seconds / 60:.1f} minutes")

    step_size(4, "Verifying the result")
    verify()

    print("\n" + "=" * 72)
    print("READY")
    print("=" * 72)
    print(f"  Distributable folder: {FOLDER}")
    print(f"  Executable          : {EXE_GUI}")
    print("\n  Copy that whole folder to the target machine (not just the .exe:")
    print("  the DLLs and the data live inside _internal) and run FLAC_Verifier.exe.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
