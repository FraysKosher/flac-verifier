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

VERSION = "2.9.5"
ENGINE_EXE_NAME = "flac_motor.exe"
ENGINE_OPTION_FLAGS = frozenset((
    "--path",
    "--mode",
    "--seconds",
    "--png",
    "--pdf",
    "--workers",
    "--no-cache",
))

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


def resolve_cli_dispatch(
    arguments: list[str],
    frozen_engine: bool = False,
) -> tuple[str, list[str] | str | None]:
    """Pure argument dispatcher returning (target, payload)."""
    args = list(arguments)
    if args and args[0] in ("--version", "-V"):
        return ("version", None)
    if args and args[0] in ("--help", "-h", "/?"):
        return ("help", None)
    if args and args[0] == "--motor-cli":
        return ("engine", args[1:])
    if args and args[0] == "--cli":
        return ("cli", args[1:])

    explicit_gui = bool(args and args[0] in ("--gui", "-g"))
    if frozen_engine and not explicit_gui:
        if not args:
            return ("help", None)
        return ("engine", args)

    if args and not explicit_gui and any(
        token.split("=", 1)[0] in ENGINE_OPTION_FLAGS for token in args
    ):
        return ("engine", args)

    rest = args[1:] if explicit_gui else args
    path = rest[0] if (rest and not rest[0].startswith("-")) else None
    return ("gui", path)


def main(arguments: list[str] | None = None) -> int:
    """Routes the arguments towards the GUI, the engine or the interactive CLI."""
    raw_args = list(sys.argv[1:] if arguments is None else arguments)
    target, payload = resolve_cli_dispatch(raw_args, is_engine_exe())

    if target == "version":
        print(f"FLAC VERIFIER {VERSION}")
        return 0
    if target == "help":
        print(HELP)
        return 0
    if target == "engine":
        import motor_flac
        return motor_flac.main_cli(list(payload) if isinstance(payload, list) else [])
    if target == "cli":
        import verificar_flac
        return verificar_flac.main(list(payload) if isinstance(payload, list) else [])

    path = payload if isinstance(payload, str) else None
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
