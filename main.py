"""Punto de entrada único de FLAC VERIFIER (y del ejecutable empaquetado).

    python main.py                     # interfaz gráfica
    python main.py --gui [PATH]        # interfaz gráfica, con la ruta ya puesta
    python main.py --motor-cli ...     # motor en modo consola (lo usa la GUI)
    python main.py --cli               # CLI interactivo
    python main.py --version

Cuando PyInstaller congela la aplicación, este archivo es el que arranca. Se
generan DOS ejecutables que comparten la carpeta:

    FLAC_Verifier.exe   sin consola: interfaz gráfica (y motor con --motor-cli)
    flac_motor.exe      con consola: el motor, para la GUI y para scripts

`flac_motor.exe` no abre ventanas: lo que recibe son opciones del motor, así que
`flac_motor.exe --path ÁLBUM --pdf` funciona tal cual. La GUI, además, le pasa
siempre la bandera `--motor-cli` para que la llamada no dependa del nombre del
binario (ver `gui.ruta_motor`).

Sobre `multiprocessing.freeze_support()`: tiene que ser lo primero que se ejecuta,
antes de mirar siquiera los argumentos. En Windows, `multiprocessing` arranca cada
proceso hijo volviendo a lanzar este mismo ejecutable con argumentos internos
(`--multiprocessing-fork`); `freeze_support()` los intercepta y ejecuta el worker.
Si se llamara después de evaluar los argumentos —o no se llamara— cada uno de los
8 procesos del análisis paralelo volvería a arrancar la aplicación entera: en el
ejecutable de ventana eso son 8 ventanas nuevas, y cada una puede volver a
repartir su propio trabajo, así que la GUI se multiplica sin fin.
"""
from __future__ import annotations

import multiprocessing
import os
import sys

VERSION = "1.0.0"
NOMBRE_MOTOR_EXE = "flac_motor.exe"

AYUDA = """FLAC VERIFIER — FLAC authenticity verifier

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


def es_motor_exe() -> bool:
    """¿Nos está arrancando `flac_motor.exe` (el ejecutable del motor)?

    El motor va en su propio binario para tener consola: un ejecutable de ventana
    deja `sys.stdout = None` y el protocolo NDJSON no llegaría a la GUI. Como es
    el motor y no la aplicación, sus argumentos se interpretan como opciones del
    motor en lugar de abrir la ventana.
    """
    if not getattr(sys, "frozen", False):
        return False
    return os.path.basename(sys.executable).lower() == NOMBRE_MOTOR_EXE


def principal(argumentos: list[str] | None = None) -> int:
    """Reparte los argumentos hacia la GUI, el motor o el CLI interactivo."""
    argumentos = list(sys.argv[1:] if argumentos is None else argumentos)

    if argumentos and argumentos[0] in ("--version", "-V"):
        print(f"FLAC VERIFIER {VERSION}")
        return 0
    if argumentos and argumentos[0] in ("--help", "-h", "/?"):
        print(AYUDA)
        return 0

    # Bandera interna: la GUI pide el motor al mismo ejecutable, en consola.
    if argumentos and argumentos[0] == "--motor-cli":
        import motor_flac
        return motor_flac.cli_principal(argumentos[1:])

    if argumentos and argumentos[0] == "--cli":
        import verificar_flac
        return verificar_flac.main(argumentos[1:])

    # `flac_motor.exe` es el motor: si no se pide la ventana a propósito, lo que
    # recibe son sus opciones. Sin argumentos, la ayuda (y no un error).
    if es_motor_exe() and not (argumentos and argumentos[0] in ("--gui", "-g")):
        if not argumentos:
            print(AYUDA)
            return 0
        import motor_flac
        return motor_flac.cli_principal(argumentos)

    # Por defecto, la interfaz gráfica (con ruta opcional)
    ruta = None
    if argumentos and argumentos[0] in ("--gui", "-g"):
        argumentos = argumentos[1:]
    if argumentos and not argumentos[0].startswith("-"):
        ruta = argumentos[0]
    try:
        from gui import main as main_grafico
    except Exception as e:                       # sin customtkinter, sin Tk…
        print(f"Could not load the graphical interface: {type(e).__name__}: {e}")
        print("You can use the CLI instead:  FLAC_Verifier.exe --cli")
        return 1
    return main_grafico(ruta)


if __name__ == "__main__":
    # ── PRIMERA INSTRUCCIÓN, SIN EXCEPCIONES ────────────────────────────────
    # Ver la explicación del encabezado: si se mueve de aquí, el análisis en
    # paralelo del ejecutable empaquetado arranca una instancia por worker.
    multiprocessing.freeze_support()
    raise SystemExit(principal())
