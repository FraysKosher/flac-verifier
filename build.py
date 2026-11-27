# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 FLAC VERIFIER Contributors
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
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
SPEC = os.path.join(HERE, "flac_verifier.spec")
ISS_SCRIPT = os.path.join(HERE, "installer", "flac_verifier.iss")
DIST = os.path.join(HERE, "dist")
BUILD = os.path.join(HERE, "build")
FOLDER = os.path.join(DIST, "FLAC_Verifier")
EXE_GUI = os.path.join(FOLDER, "FLAC_Verifier.exe")
EXE_ENGINE = os.path.join(FOLDER, "flac_motor.exe")
MINIMUM_PYTHON = (3, 10)


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


def check_assets() -> None:
    """The interface artwork must exist and stay inside its size budget.

    The icons are data of the executable: if they are missing, the window still
    opens but every button loses its icon, so the build says it out loud instead
    of shipping a quieter interface.
    """
    folder = os.path.join(HERE, "Logo", "icons")
    if not os.path.isdir(folder):
        print("  warning: Logo/icons is missing; the buttons will show text only")
        return
    files = [name for name in os.listdir(folder) if name.endswith(".png")]
    total = sum(os.path.getsize(os.path.join(folder, name)) for name in files)
    print(f"  ✓ Logo/icons: {len(files)} icons, {total / 1024:.1f} kB")
    if total >= 100_000:
        print("  warning: the icon set is over the 100 kB budget of DESIGN.md")
    ui_folder = os.path.join(HERE, "ui_v3")
    if os.path.isdir(ui_folder):
        ui_files = [n for n in os.listdir(ui_folder) if os.path.isfile(os.path.join(ui_folder, n))]
        ui_bytes = sum(os.path.getsize(os.path.join(ui_folder, n)) for n in ui_files)
        print(f"  ✓ ui_v3: {len(ui_files)} assets, {ui_bytes / 1024:.1f} kB")
    else:
        print("  warning: ui_v3/ is missing; the v3 desktop shell cannot load")


def clean() -> None:
    for folder in (BUILD, DIST):
        if os.path.isdir(folder):
            shutil.rmtree(folder, ignore_errors=True)
            print(f"  removed {os.path.basename(folder)}/")
    for resto in ("__pycache__",):
        path = os.path.join(HERE, resto)
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
    if not os.path.exists(os.path.join(HERE, "Logo", "icono_app.ico")):
        print("  warning: Logo/icono_app.ico is missing; building with the default icon")


def normalize_integrity_windows(folder: str) -> None:
    """Sets the Medium mandatory integrity label on everything under `folder`.

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


def compile_binary() -> float:
    if not os.path.exists(SPEC):
        raise SystemExit(f"Cannot find {os.path.basename(SPEC)}")
    command = [sys.executable, "-m", "PyInstaller", SPEC, "--noconfirm", "--clean",
               "--distpath", DIST, "--workpath", BUILD]
    print("  " + " ".join(command[2:]))
    start = time.perf_counter()
    result = subprocess.run(command, cwd=HERE)
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
    failed = []
    for path in (EXE_GUI, EXE_ENGINE):
        process = subprocess.run([path, "--version"], capture_output=True, text=True,
                                 timeout=180,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        output = (process.stdout or process.stderr or "").strip().splitlines()
        brand = "✓" if process.returncode == 0 else "✗"
        print(f"  {brand} {os.path.basename(path)} --version -> "
              f"exit code {process.returncode}, {output[0] if output else '(no output)'}")
        if process.returncode != 0:
            failed.append(f"{os.path.basename(path)} (exit code {process.returncode})")
    if failed:
        raise SystemExit("  ✗ executable check failed: " + "; ".join(failed))

    total = sum(os.path.getsize(os.path.join(root, name))
                for root, _, files in os.walk(FOLDER)
                for name in files)
    print(f"  total size of dist/FLAC_Verifier: {total / 1e6:.1f} MB")


def find_inno_setup() -> str | None:
    """Locates Inno Setup 6 (`ISCC.exe`) on PATH or standard Windows install paths."""
    found = shutil.which("ISCC") or shutil.which("iscc")
    if found:
        return found
    candidates = [
        os.path.join(os.environ.get("ProgramFiles(x86)", ""), "Inno Setup 6", "ISCC.exe"),
        os.path.join(os.environ.get("ProgramFiles", ""), "Inno Setup 6", "ISCC.exe"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Inno Setup 6", "ISCC.exe"),
    ]
    for path in candidates:
        if path and os.path.isfile(path):
            return path
    return None


def build_portable_zip(version: str) -> str:
    """Packages `dist/FLAC_Verifier/` into a portable ZIP containing `portable.flag`."""
    zip_name = f"FLAC_Verifier_{version}-win64-portable.zip"
    zip_path = os.path.join(DIST, zip_name)
    if os.path.exists(zip_path):
        os.unlink(zip_path)

    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for root, _, files in os.walk(FOLDER):
            for name in sorted(files):
                full = os.path.join(root, name)
                rel = os.path.relpath(full, DIST).replace("\\", "/")
                zf.write(full, arcname=rel)
        # Stamp portable.flag inside the portable archive so data lives in <folder>/data/.
        zf.writestr(
            "FLAC_Verifier/portable.flag",
            "Portable mode marker: library.db and covers live in ./data/\n",
        )
    size_mb = os.path.getsize(zip_path) / 1e6
    print(f"  ✓ {zip_name} ({size_mb:.1f} MB)")
    return zip_path


def build_installer(version: str) -> str | None:
    """Compiles the per-user Windows installer via Inno Setup 6 (`ISCC.exe`)."""
    if not os.path.isfile(ISS_SCRIPT):
        print(f"  warning: missing {ISS_SCRIPT}; skipping installer build")
        return None
    iscc = find_inno_setup()
    if not iscc:
        print("  note: Inno Setup 6 (ISCC.exe) not found locally; skipping installer build")
        return None
    cmd = [iscc, f"/DMyAppVersion={version}", ISS_SCRIPT]
    print("  " + " ".join(cmd))
    res = subprocess.run(cmd, cwd=HERE)
    if res.returncode != 0:
        raise SystemExit("Inno Setup compilation failed. Check the ISCC output above.")
    exe_name = f"FLAC_Verifier_Setup_{version}-win64.exe"
    exe_path = os.path.join(DIST, exe_name)
    if not os.path.isfile(exe_path):
        raise SystemExit(f"Expected installer not found after ISCC: {exe_path}")
    size_mb = os.path.getsize(exe_path) / 1e6
    print(f"  ✓ {exe_name} ({size_mb:.1f} MB)")
    return exe_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Build FLAC_Verifier.exe")
    parser.add_argument("--no-clean", dest="without_clean", action="store_true",
                        help="do not remove build/ or dist/ before building")
    parser.add_argument("--portable-zip", dest="portable_zip", action="store_true",
                        help="also build the portable .zip with portable.flag")
    parser.add_argument("--installer", dest="installer", action="store_true",
                        help="also compile the per-user Inno Setup installer")
    parser.add_argument("--release-assets", dest="release_assets", action="store_true",
                        help="build portable .zip, Inno Setup installer (if ISCC exists), SHA256SUMS and latest.json")
    args = parser.parse_args()

    _utf8()
    version = project_version()
    print("=" * 72)
    print("BUILDING FLAC VERIFIER " + version)
    print("=" * 72)

    step_size(1, "Checking the environment")
    check_python()
    check_assets()
    ensure_pyinstaller()

    step_size(2, "Removing leftovers from previous builds")
    if args.without_clean:
        print("  (skipped by --no-clean)")
    else:
        clean()

    step_size(3, "Building (this can take several minutes)")
    seconds = compile_binary()
    print(f"  finished in {seconds / 60:.1f} minutes")

    step_size(4, "Verifying the result")
    verify()

    want_zip = args.portable_zip or args.release_assets
    want_installer = args.installer or args.release_assets
    zip_path: str | None = None
    setup_path: str | None = None
    if want_zip or want_installer:
        step_size(5, "Packaging release deliverables")
        if want_zip:
            zip_path = build_portable_zip(version)
        if want_installer:
            setup_path = build_installer(version)
        if args.release_assets:
            import updater
            meta = updater.write_release_metadata(
                DIST,
                version,
                installer_path=setup_path,
                portable_zip_path=zip_path,
            )
            print(f"  ✓ {os.path.basename(meta['latest_json'])}")
            print(f"  ✓ {os.path.basename(meta['sha256sums'])}")

    print("\n" + "=" * 72)
    print("READY")
    print("=" * 72)
    print(f"  Distributable folder: {FOLDER}")
    print(f"  Executable          : {EXE_GUI}")
    if zip_path:
        print(f"  Portable archive    : {zip_path}")
    if setup_path:
        print(f"  Windows installer   : {setup_path}")
    print("\n  Copy that whole folder to the target machine (not just the .exe:")
    print("  the DLLs and the data live inside _internal) and run FLAC_Verifier.exe.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
