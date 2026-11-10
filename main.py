"""Single entry point of FLAC VERIFIER (and of the packaged executable).

    python main.py                     # graphical interface
    python main.py --gui [PATH]        # graphical interface, with the path already set
    python main.py --motor-cli ...     # engine in console mode (used by the GUI)
    python main.py --cli               # interactive CLI
    python main.py --version

When PyInstaller freezes the application, this file is the one that starts. TWO
executables are generated, sharing the folder:

    FLAC_Verifier.exe   no console: graphical interface (and engine with --motor-cli)
    flac_motor.exe      with console: the engine, for the GUI and for scripts

`flac_motor.exe` does not open windows: what it receives are engine options, so
`flac_motor.exe --path ALBUM --pdf` works as is. The GUI, in addition, always
passes it the `--motor-cli` flag so that the call does not depend on the name of
the binary (see `gui.engine_path`).

About `multiprocessing.freeze_support()`: it has to be the first thing that runs,
before even looking at the arguments. On Windows, `multiprocessing` starts each
child process by relaunching this same executable with internal arguments
(`--multiprocessing-fork`); `freeze_support()` intercepts them and runs the worker.
If it were called after evaluating the arguments —or not called at all— each of the
8 processes of the parallel analysis would start the whole application again: in the
windowed executable that means 8 new windows, and each one can hand out its own work
again, so the GUI multiplies without end.
"""
from __future__ import annotations

import multiprocessing
import os
import sys

# ─── BLAS THREADS (before numpy/scipy are imported) ──────────────────────────
# One BLAS thread per process: the parallel analysis already spreads the tracks
# across processes, so extra internal pools would only oversubscribe the CPU.
for _variable_blas in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS",
                       "MKL_NUM_THREADS", "VECLIB_MAXIMUM_THREADS",
                       "NUMEXPR_NUM_THREADS"):
    os.environ.setdefault(_variable_blas, "1")
del _variable_blas

VERSION = "2.5.0"
ENGINE_EXE_NAME = "flac_motor.exe"

HELP = """FLAC VERIFIER — FLAC authenticity verifier

Usage:
  FLAC_Verifier.exe                        Open the graphical interface
  FLAC_Verifier.exe --gui [PATH]           Graphical interface, path ready
  FLAC_Verifier.exe --motor-cli OPTIONS    Engine in console mode (used by the GUI)
  FLAC_Verifier.exe --cli                  Command-line interface
  FLAC_Verifier.exe --version              Show the version
  FLAC_Verifier.exe --help                 Show this help

  flac_motor.exe OPTIONS                   The engine, with no extra flags
  flac_motor.exe --path "D:/Music/album" --pdf

Engine options (--motor-cli and flac_motor.exe):
  --path PATH       Folder or .flac file (required)
  --mode MODE       center (default) | full | seconds
  --seconds N       Starting seconds when --mode seconds
  --png             Save the spectrograms as PNG
  --pdf             Generate the album PDF report
  --workers N       Parallel processes (0 = automatic, 1 = sequential)
  --no-cache        Ignore and do not write the results cache
"""


def is_engine_exe() -> bool:
    """Is `flac_motor.exe` (the engine executable) starting us?

    The engine lives in its own binary so that it can have a console: a windowed
    executable leaves `sys.stdout = None` and the NDJSON protocol would never reach
    the GUI. As it is the engine and not the application, its arguments are
    interpreted as engine options instead of opening the window.
    """
    if not getattr(sys, "frozen", False):
        return False
    return os.path.basename(sys.executable).lower() == ENGINE_EXE_NAME


def main(arguments: list[str] | None = None) -> int:
    """Routes the arguments towards the GUI, the engine or the interactive CLI."""
    arguments = list(sys.argv[1:] if arguments is None else arguments)

    if arguments and arguments[0] in ("--version", "-V"):
        print(f"FLAC VERIFIER {VERSION}")
        return 0
    if arguments and arguments[0] in ("--help", "-h", "/?"):
        print(HELP)
        return 0

    # Internal flag: the GUI asks the same executable for the engine, in console mode.
    if arguments and arguments[0] == "--motor-cli":
        import motor_flac
        return motor_flac.main_cli(arguments[1:])

    if arguments and arguments[0] == "--cli":
        import verificar_flac
        return verificar_flac.main(arguments[1:])

    # `flac_motor.exe` is the engine: unless the window is asked for on purpose,
    # what it receives are its options. With no arguments, the help (and not an error).
    if is_engine_exe() and not (arguments and arguments[0] in ("--gui", "-g")):
        if not arguments:
            print(HELP)
            return 0
        import motor_flac
        return motor_flac.main_cli(arguments)

    # By default, the graphical interface (with an optional path)
    path = None
    if arguments and arguments[0] in ("--gui", "-g"):
        arguments = arguments[1:]
    if arguments and not arguments[0].startswith("-"):
        path = arguments[0]
    try:
        from gui import main as main_grafico
    except Exception as e:                       # without customtkinter, without Tk…
        print(f"Could not load the graphical interface: {type(e).__name__}: {e}")
        print("You can use the CLI instead:  FLAC_Verifier.exe --cli")
        return 1
    return main_grafico(path)


if __name__ == "__main__":
    # ── FIRST STATEMENT, NO EXCEPTIONS ──────────────────────────────────────
    # See the explanation in the header: if it is moved from here, the parallel
    # analysis of the packaged executable starts one instance per worker.
    multiprocessing.freeze_support()
    raise SystemExit(main())
