"""Graphical interface for FLAC VERIFIER (CustomTkinter).

Two ways to launch it:

    python gui.py
    python verificar_flac.py --gui

Architecture (designed so that the interface never freezes on Windows):

    - The analysis runs in a separate PROCESS (`subprocess.Popen` with `-u`),
      using the same interpreter that runs the GUI.
    - A daemon THREAD reads stdout line by line (the engine's NDJSON protocol)
      and puts the events into a `queue.Queue`.
    - The window drains that queue with `self.after(100, ...)`: no widget is
      touched from the reader thread, so there are no races.

The logic (command, parsing, formatting, process lifecycle) is kept apart from
the widgets so that it can be tested without opening the window.

Optional dependencies:
    customtkinter   the interface itself (without it, this module only reports)
    tkinterdnd2     drag and drop of folders (if missing, everything still works)
"""
from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Iterable, Sequence

try:
    import customtkinter as ctk
    CTK_OK = True
except ImportError:                      # the GUI is optional
    ctk = None                           # type: ignore[assignment]
    CTK_OK = False

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    DND_OK = True
except ImportError:                      # optional: without this there is no drag and drop
    DND_FILES = None                     # type: ignore[assignment]
    TkinterDnD = None                    # type: ignore[assignment]
    DND_OK = False

Evento = dict[str, Any]
Mensaje = tuple[str, Evento]             # (channel, event) as it travels through the queue

DIRECTORIO = os.path.dirname(os.path.abspath(__file__))
MOTOR = os.path.join(DIRECTORIO, "motor_flac.py")
NOMBRE_INFORME = "flac_verifier_report.pdf"
# Engine executable packaged next to the GUI (see flac_verifier.spec).
NOMBRE_MOTOR_EXE = "flac_motor.exe"
BANDERA_MOTOR_CLI = "--motor-cli"


def empaquetado() -> bool:
    """Are we running inside a PyInstaller executable?"""
    return bool(getattr(sys, "frozen", False))


def ruta_motor() -> list[str]:
    """How the engine must be invoked, depending on how this is being run.

    - Packaged: the engine executable sitting next to it is preferred (it has its
      own console, so its stdout is a real stream and the NDJSON protocol
      arrives through the pipe). If it is missing, the same .exe is used with the
      internal `--motor-cli` flag.
    - From the source code: the interpreter with `-u` and the engine script.

    The `--motor-cli` flag is ALWAYS present in packaged mode, also with the
    engine executable: it is what keeps the call from depending on guessing which
    binary this is. (Without it, `FLAC_Verifier.exe` would open its window instead
    of analyzing: the GUI would sit waiting for events that never arrive.)
    """
    if empaquetado():
        carpeta = os.path.dirname(os.path.abspath(sys.executable))
        motor_exe = os.path.join(carpeta, NOMBRE_MOTOR_EXE)
        if os.path.exists(motor_exe):
            return [motor_exe, BANDERA_MOTOR_CLI]
        return [sys.executable, BANDERA_MOTOR_CLI]
    return [sys.executable, "-u", MOTOR]

# Visual identity: the assets are generated with Logo/generar_logo.py from the
# design in splashscreen.html. If they are missing, the window still works (it only warns).
LOGO_CARPETA = os.path.join(DIRECTORIO, "Logo")
LOGO_ICONO   = os.path.join(LOGO_CARPETA, "icono_app.ico")           # window and .exe
LOGO_ICONPHOTO = os.path.join(LOGO_CARPETA, "logo_flac_verifier_128.png")  # outside Windows
LOGO_MARCA   = os.path.join(LOGO_CARPETA, "logo_flac_verifier.png")  # header and README
COLOR_MARCA  = "#4f98a3"          # the teal of the splash

# Short labels for the log, and colours per verdict (the same as in the PDF).
ETIQUETA_VEREDICTO = {
    "GENUINE LOSSLESS":       "OK",
    "PROBABLY LOSSLESS": "OK?",
    "SUSPICIOUS":                 "SUSPICIOUS",
    "PROBABLE UPSCALE":       "UPSCALE",
    "indeterminate":          "?",
}
COLORES = {
    "verde":    ("#E3F4E7", "#14632C"),
    "amarillo": ("#FBF3D5", "#8A6D00"),
    "naranja":  ("#FDE8D5", "#A85B00"),
    "rojo":     ("#FBE0E0", "#A11616"),
    "gris":     ("#EDEDED", "#444444"),
}
COLOR_VEREDICTO = {
    "GENUINE LOSSLESS":       "verde",
    "PROBABLY LOSSLESS": "amarillo",
    "SUSPICIOUS":                 "naranja",
    "PROBABLE UPSCALE":       "rojo",
    "indeterminate":          "gris",
}
MODOS = ["center", "full", "seconds"]
OPCIONES_WORKERS = ["Automatic", "1", "2", "4", "8"]


# ─── LOGIC (usable without a window) ─────────────────────────────────────────

def construir_comando(ruta: str, modo: str = "center", segundos: int | None = None,
                      pdf: bool = True, workers: int = 0, sin_cache: bool = False,
                      motor: str | None = None) -> list[str]:
    """Engine command for the subprocess.

    From the source code `-u` is added (without it, the stdout buffer would hold
    the NDJSON events back instead of delivering them in real time). Packaged it
    is not needed: the engine is already packaged with unbuffered output and every
    line goes out with `flush=True`.
    """
    if motor:
        base = ([sys.executable, "-u", motor] if not empaquetado()
                else [motor, BANDERA_MOTOR_CLI])
    else:
        base = ruta_motor()
    comando = [*base, "--path", str(ruta), "--mode", modo]
    if modo == "seconds":
        comando += ["--seconds", str(int(segundos or 15))]
    if pdf:
        comando.append("--pdf")
    if workers and workers > 0:
        comando += ["--workers", str(int(workers))]
    if sin_cache:
        comando.append("--no-cache")
    return comando


def parsear_evento(linea: str) -> Evento | None:
    """Turns one NDJSON line into an event. None if the line is empty.

    An unreadable line cannot bring the reader down: it becomes an internal
    `__ilegible__` event so that there is a record of it in the log."""
    texto = (linea or "").strip()
    if not texto:
        return None
    try:
        evento = json.loads(texto)
    except ValueError:
        return {"tipo": "__ilegible__", "mensaje": texto[:300]}
    if not isinstance(evento, dict):
        return {"tipo": "__ilegible__", "mensaje": texto[:300]}
    if "tipo" not in evento:
        evento["tipo"] = "__desconocido__"
    return evento


def formatear_resultado(evento: Evento) -> str:
    """Log line for a `resultado` event."""
    archivo = str(evento.get("archivo", "?"))
    if evento.get("error"):
        return f"[ERROR] {archivo}: {evento['error']}"
    veredicto = str(evento.get("veredicto") or "indeterminate")
    etiqueta = ETIQUETA_VEREDICTO.get(veredicto, veredicto)
    problemas = len(evento.get("problemas") or [])
    texto = f"[{etiqueta}] {archivo}: {veredicto}"
    score = evento.get("score")
    if isinstance(score, (int, float)):
        texto += f" · score {score * 100:.0f} %"
    texto += f" · {problemas} issue" + ("s" if problemas != 1 else "")
    if evento.get("desde_cache"):
        texto += " · cache"
    return texto


def formatear_resumen(eventos: Iterable[Evento]) -> tuple[str, str]:
    """Final summary of the batch: (colour_key, text) for the status card."""
    conteo: dict[str, int] = {}
    errores = 0
    total = 0
    for evento in eventos:
        if evento.get("tipo") != "resultado":
            continue
        total += 1
        if evento.get("error") or "veredicto" not in evento:
            errores += 1
        else:
            conteo[str(evento["veredicto"])] = conteo.get(str(evento["veredicto"]), 0) + 1
    if total == 0:
        return "gris", "No results"

    etiquetas = []
    for clave, nombre in (("GENUINE LOSSLESS", "genuine"),
                          ("PROBABLY LOSSLESS", "probable"),
                          ("SUSPICIOUS", "suspicious"),
                          ("PROBABLE UPSCALE", "upscale"),
                          ("indeterminate", "indeterminate")):
        if conteo.get(clave):
            etiquetas.append(f"{conteo[clave]} {nombre}")
    if errores:
        etiquetas.append(f"{errores} with error")

    if errores or conteo.get("PROBABLE UPSCALE"):
        color = "rojo"
    elif conteo.get("SUSPICIOUS"):
        color = "naranja"
    elif conteo.get("PROBABLY LOSSLESS") or conteo.get("indeterminate"):
        color = "amarillo"
    else:
        color = "verde"
    return color, f"{total} file(s): " + " · ".join(etiquetas)


def rutas_de_dnd(datos: str) -> list[str]:
    """Paths contained in a <<Drop>> event from tkinterdnd2.

    The widget delivers the paths with spaces between braces: `{C:/my music} {D:/other}`."""
    rutas: list[str] = []
    actual = ""
    dentro = False
    for caracter in datos or "":
        if caracter == "{":
            dentro = True
        elif caracter == "}":
            dentro = False
            rutas.append(actual)
            actual = ""
        elif caracter == " " and not dentro:
            if actual:
                rutas.append(actual)
                actual = ""
        else:
            actual += caracter
    if actual:
        rutas.append(actual)
    return [r for r in rutas if r]


def abrir_archivo(ruta: str) -> bool:
    """Opens a file with the system default viewer. Never raises."""
    try:
        if os.name == "nt":
            os.startfile(ruta)                       # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", ruta])
        else:
            subprocess.Popen(["xdg-open", ruta])
        return True
    except Exception:
        return False


def terminar_proceso(proceso: "subprocess.Popen[str] | None", espera: float = 3.0) -> None:
    """Terminates the process and ALL of its descendants. Never raises.

    The engine may spread the work across child processes (`--workers`), so killing
    only the parent would leave orphans behind: on Windows the tree is killed with
    taskkill and, whatever happens, the terminate()/kill() fallback remains."""
    if proceso is None or proceso.poll() is not None:
        return
    if os.name == "nt":
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proceso.pid)],
                           capture_output=True, timeout=10,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except Exception:
            pass
    for accion in (proceso.terminate, proceso.kill):
        try:
            accion()
            proceso.wait(timeout=espera)
            return
        except Exception:
            continue


def lector_salida(proceso: "subprocess.Popen[str]", cola: "queue.Queue[Mensaje]",
                  cancelado: Callable[[], bool] | None = None) -> None:
    """Daemon thread: reads stdout and stderr and puts them into the queue.

    It always ends with a `__fin__` event so that the window knows the process
    finished, even if the read failed."""
    def leer(flujo: Any, canal: str) -> None:
        try:
            for linea in flujo:
                evento = parsear_evento(linea)
                if evento is not None:
                    cola.put((canal, evento))
        except Exception as e:                       # broken pipe, process killed…
            cola.put((canal, {"tipo": "__aviso_lectura__",
                              "mensaje": f"{type(e).__name__}: {e}"}))
        finally:
            try:
                flujo.close()
            except Exception:
                pass

    hilo_error = threading.Thread(target=leer, args=(proceso.stderr, "stderr"), daemon=True)
    hilo_error.start()
    leer(proceso.stdout, "stdout")
    hilo_error.join(timeout=2.0)
    try:
        codigo = proceso.wait(timeout=5.0)
    except Exception:
        codigo = None
    cola.put(("fin", {"tipo": "__fin__", "codigo": codigo,
                      "cancelado": bool(cancelado and cancelado())}))


class GestorProceso:
    """Lifecycle of the engine subprocess: start, read and cancel."""

    def __init__(self, cola: "queue.Queue[Mensaje]", motor: str | None = None,
                 lanzador: Callable[..., Any] = subprocess.Popen) -> None:
        self.cola = cola
        self.motor = motor
        self._lanzador = lanzador                 # injectable for the tests
        self.proceso: "subprocess.Popen[str] | None" = None
        self.hilo: threading.Thread | None = None
        self.cancelado = False

    @property
    def activo(self) -> bool:
        return self.proceso is not None and self.proceso.poll() is None

    def iniciar(self, ruta: str, modo: str = "center", segundos: int | None = None,
                pdf: bool = True, workers: int = 0, sin_cache: bool = False) -> list[str]:
        """Launches the analysis. Returns the command used (useful for the log)."""
        if self.activo:
            raise RuntimeError("an analysis is already running")
        comando = construir_comando(ruta, modo=modo, segundos=segundos, pdf=pdf,
                                    workers=workers, sin_cache=sin_cache,
                                    motor=self.motor)
        self.cancelado = False
        # CREATE_NO_WINDOW stops a console from flashing when the engine starts.
        self.proceso = self._lanzador(
            comando, cwd=DIRECTORIO,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self.hilo = threading.Thread(
            target=lector_salida, args=(self.proceso, self.cola, lambda: self.cancelado),
            daemon=True, name="lector-motor")
        self.hilo.start()
        return comando

    def cancelar(self) -> None:
        self.cancelado = True
        terminar_proceso(self.proceso)

    def esperar(self, tiempo: float = 5.0) -> None:
        if self.hilo is not None:
            self.hilo.join(timeout=tiempo)


# ─── WINDOW ─────────────────────────────────────────────────────────────────

if CTK_OK:

    class _BaseVentana(ctk.CTk, TkinterDnD.DnDWrapper if DND_OK else object):  # type: ignore[misc]
        """Base that adds drag and drop only if tkinterdnd2 is installed."""

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            if DND_OK:
                try:
                    self.TkdndVersion = TkinterDnD._require(self)
                except Exception:
                    pass

    class VentanaFlacVerifier(_BaseVentana):
        """Main window: controls on the left, telemetry on the right."""

        def __init__(self, ruta_inicial: str | None = None, motor: str | None = None) -> None:
            super().__init__()
            self.title("FLAC VERIFIER — FLAC authenticity verification")
            self.geometry("1120x740")
            self.minsize(980, 640)

            self.cola: "queue.Queue[Mensaje]" = queue.Queue()
            self.gestor = GestorProceso(self.cola, motor=motor)
            self.eventos: list[Evento] = []
            self.total = 0
            self.recibidos = 0
            self.ruta_pdf: str | None = None

            self.grid_columnconfigure(0, weight=0)
            self.grid_columnconfigure(1, weight=1)
            self.grid_rowconfigure(0, weight=1)

            self._construir_controles()
            self._construir_telemetria()
            self._cargar_identidad()
            self._actualizar_estado()
            self.protocol("WM_DELETE_WINDOW", self._al_cerrar)
            self.dnd_activo = self._registrar_dnd()

            if ruta_inicial:
                self.var_ruta.set(ruta_inicial)
                self._log(f"Path ready: {ruta_inicial}")
            self._log("Ready. Choose a folder or a .flac file and press Analyze.")
            if not DND_OK:
                self._log("Drag and drop is not available "
                          "(pip install tkinterdnd2 to enable it).", "aviso")
            if not self.icono_cargado:
                self._log("Project icon not found "
                          "(Logo/icono_app.ico); the window uses the default icon.",
                          "aviso")

        # ── widget construction ─────────────────────────────────────────────
        def _imagen_marca(self, lado: int) -> Any:
            """The logo mark, scaled. None if the asset is missing."""
            if not os.path.exists(LOGO_MARCA):
                return None
            try:
                from PIL import Image as ImagenPIL
                with ImagenPIL.open(LOGO_MARCA) as original:
                    recorte = original.convert("RGBA").copy()
                return ctk.CTkImage(light_image=recorte, dark_image=recorte,
                                    size=(lado, lado))
            except Exception as e:
                self._log(f"Could not load the logo: {type(e).__name__}: {e}", "aviso")
                return None

        def _cargar_identidad(self) -> None:
            """Window icon (and that of the future .exe) with the project logo.

            On Windows the multi-resolution .ico is used, which is what feeds the
            taskbar; elsewhere, iconphoto with a PNG. If the asset is missing, the
            window still opens."""
            self.icono_cargado = False
            if os.name == "nt" and os.path.exists(LOGO_ICONO):
                try:
                    self.iconbitmap(LOGO_ICONO)
                    self.icono_cargado = True
                except Exception:
                    self.icono_cargado = False
            if not self.icono_cargado and os.path.exists(LOGO_ICONPHOTO):
                try:
                    import tkinter
                    self.iconphoto(True, tkinter.PhotoImage(file=LOGO_ICONPHOTO))
                    self.icono_cargado = True
                except Exception:
                    self.icono_cargado = False

        def _construir_controles(self) -> None:
            panel = ctk.CTkFrame(self, width=330, corner_radius=0)
            panel.grid(row=0, column=0, sticky="nsw")
            panel.grid_propagate(False)
            panel.grid_columnconfigure(0, weight=1)

            # Header: the logo mark next to the name, as in the splash
            cabecera = ctk.CTkFrame(panel, fg_color="transparent")
            cabecera.grid(row=0, column=0, padx=16, pady=(18, 14), sticky="ew")
            marca = self._imagen_marca(46)
            columna_texto = 1 if marca is not None else 0
            if marca is not None:
                ctk.CTkLabel(cabecera, text="", image=marca).grid(
                    row=0, column=0, rowspan=2, padx=(0, 12))
            ctk.CTkLabel(cabecera, text="FLAC VERIFIER",
                         font=ctk.CTkFont(size=21, weight="bold")).grid(
                row=0, column=columna_texto, sticky="w")
            ctk.CTkLabel(cabecera, text="FAKE LOSSLESS DETECTION",
                         font=ctk.CTkFont(size=9), text_color=COLOR_MARCA).grid(
                row=1, column=columna_texto, sticky="w")

            ctk.CTkLabel(panel, text="Folder or .flac file",
                         font=ctk.CTkFont(size=12, weight="bold")).grid(
                row=1, column=0, padx=16, pady=(4, 2), sticky="w")
            self.var_ruta = ctk.StringVar(value="")
            self.entrada = ctk.CTkEntry(panel, textvariable=self.var_ruta,
                                        placeholder_text="C:/Music/Album")
            self.entrada.grid(row=2, column=0, padx=16, sticky="ew")
            self.btn_examinar = ctk.CTkButton(panel, text="📂  Browse folder…",
                                              height=36, command=self._elegir_carpeta)
            self.btn_examinar.grid(row=3, column=0, padx=16, pady=(6, 14), sticky="ew")

            ctk.CTkLabel(panel, text="Spectral mode",
                         font=ctk.CTkFont(size=12, weight="bold")).grid(
                row=4, column=0, padx=16, pady=(4, 2), sticky="w")
            self.var_modo = ctk.StringVar(value="center")
            self.opcion_modo = ctk.CTkOptionMenu(panel, values=MODOS,
                                                 variable=self.var_modo,
                                                 command=lambda _: self._cambiar_modo())
            self.opcion_modo.grid(row=5, column=0, padx=16, sticky="ew")

            # The seconds only make sense in "seconds" mode: the control is
            # removed from the grid (not disabled) so that it leaves no gap and
            # the controls below move up.
            self.fila_segundos = ctk.CTkFrame(panel, fg_color="transparent")
            self.fila_segundos.grid(row=6, column=0, padx=16, pady=(8, 0), sticky="ew")
            self.fila_segundos.grid_columnconfigure(0, weight=1)
            self.lbl_segundos = ctk.CTkLabel(self.fila_segundos, text="Seconds: 15")
            self.lbl_segundos.grid(row=0, column=0, sticky="w")
            self.slider_segundos = ctk.CTkSlider(self.fila_segundos, from_=5, to=120,
                                                 number_of_steps=23,
                                                 command=self._cambiar_segundos)
            self.slider_segundos.set(15)
            self.slider_segundos.grid(row=1, column=0, sticky="ew")

            self.var_pdf = ctk.BooleanVar(value=True)
            self.check_pdf = ctk.CTkCheckBox(panel, text="Generate PDF report",
                                             variable=self.var_pdf)
            self.check_pdf.grid(row=7, column=0, padx=16, pady=(14, 6), sticky="w")

            self.var_paralelo = ctk.BooleanVar(value=True)
            self.switch_paralelo = ctk.CTkSwitch(panel, text="Parallel processing",
                                                 variable=self.var_paralelo,
                                                 command=self._cambiar_paralelo)
            self.switch_paralelo.grid(row=8, column=0, padx=16, pady=(6, 2), sticky="w")
            self.opcion_workers = ctk.CTkOptionMenu(panel, values=OPCIONES_WORKERS,
                                                    width=150)
            self.opcion_workers.set(OPCIONES_WORKERS[0])
            self.opcion_workers.grid(row=9, column=0, padx=16, pady=(2, 6), sticky="w")

            self.var_sin_cache = ctk.BooleanVar(value=False)
            self.check_cache = ctk.CTkCheckBox(panel, text="Ignore cache",
                                               variable=self.var_sin_cache)
            self.check_cache.grid(row=10, column=0, padx=16, pady=(6, 14), sticky="w")

            self.btn_analizar = ctk.CTkButton(panel, text="▶  Analyze", height=44,
                                              font=ctk.CTkFont(size=15, weight="bold"),
                                              command=self._analizar)
            self.btn_analizar.grid(row=11, column=0, padx=16, sticky="ew")
            self.btn_cancelar = ctk.CTkButton(panel, text="■  Cancel", height=36,
                                              fg_color="#8B1A1A",
                                              hover_color="#A11616",
                                              command=self._cancelar)
            self.btn_cancelar.grid(row=12, column=0, padx=16, pady=(6, 14), sticky="ew")

            self.tarjeta = ctk.CTkFrame(panel, corner_radius=10)
            self.tarjeta.grid(row=13, column=0, padx=16, pady=(0, 8), sticky="ew")
            self.lbl_resumen = ctk.CTkLabel(self.tarjeta, text="Nothing analyzed yet",
                                            wraplength=280, justify="left")
            self.lbl_resumen.pack(padx=12, pady=10)

        def _construir_telemetria(self) -> None:
            panel = ctk.CTkFrame(self, corner_radius=0, fg_color="transparent")
            panel.grid(row=0, column=1, sticky="nsew", padx=(0, 0))
            panel.grid_columnconfigure(0, weight=1)
            panel.grid_rowconfigure(3, weight=1)

            cabecera = ctk.CTkFrame(panel, fg_color="transparent")
            cabecera.grid(row=0, column=0, sticky="ew", padx=16, pady=(18, 6))
            cabecera.grid_columnconfigure(0, weight=1)
            self.lbl_estado = ctk.CTkLabel(cabecera, text="Idle",
                                           font=ctk.CTkFont(size=14, weight="bold"))
            self.lbl_estado.grid(row=0, column=0, sticky="w")
            self.lbl_progreso = ctk.CTkLabel(cabecera, text="")
            self.lbl_progreso.grid(row=0, column=1, sticky="e")

            self.barra = ctk.CTkProgressBar(panel, height=14)
            self.barra.set(0)
            self.barra.grid(row=1, column=0, sticky="ew", padx=16, pady=(0, 10))

            self.log = ctk.CTkTextbox(panel, wrap="word",
                                      font=ctk.CTkFont(family="Consolas", size=12))
            self.log.grid(row=3, column=0, sticky="nsew", padx=16, pady=(0, 10))
            self.log.configure(state="disabled")

            acciones = ctk.CTkFrame(panel, fg_color="transparent")
            acciones.grid(row=4, column=0, sticky="ew", padx=16, pady=(0, 16))
            acciones.grid_columnconfigure(0, weight=1)
            self.btn_abrir_pdf = ctk.CTkButton(acciones, text="📄  Open PDF report",
                                               height=38, command=self._abrir_pdf)
            self.btn_abrir_pdf.grid(row=0, column=0, sticky="w")

        # ── view helpers ────────────────────────────────────────────────────
        def _log(self, texto: str, etiqueta: str = "info") -> None:
            marca = time.strftime("%H:%M:%S")
            self.log.configure(state="normal")
            self.log.insert("end", f"[{marca}] {texto}\n")
            try:                                     # colouring if the widget supports it
                indice = self.log.index("end-2l")
                self.log.tag_add(etiqueta, indice, "end-1c")
                color = {"error": "#E06C6C", "aviso": "#E0A96C",
                         "ok": "#7FD18A", "info": "#9AA4B2"}.get(etiqueta, "#9AA4B2")
                self.log.tag_config(etiqueta, foreground=color)
            except Exception:
                pass
            self.log.configure(state="disabled")
            self.log.see("end")

        def _registrar_dnd(self) -> bool:
            if not DND_OK:
                return False
            try:
                self.entrada.drop_target_register(DND_FILES)
                self.entrada.dnd_bind("<<Drop>>", self._al_soltar)
                return True
            except Exception:
                return False

        def _al_soltar(self, evento: Any) -> None:
            rutas = rutas_de_dnd(getattr(evento, "data", ""))
            if rutas:
                self.var_ruta.set(rutas[0])
                self._log(f"Dropped: {rutas[0]}")

        def _elegir_carpeta(self) -> None:
            from tkinter import filedialog
            inicial = self.var_ruta.get() or os.path.expanduser("~")
            elegida = filedialog.askdirectory(title="Choose the album folder",
                                              initialdir=inicial if os.path.isdir(inicial) else None)
            if elegida:
                self.var_ruta.set(elegida)
                self._log(f"Folder selected: {elegida}")

        def _cambiar_modo(self) -> None:
            """Shows or hides the seconds control depending on the mode.

            `grid_remove()`/`grid()` is used and not `configure(state=...)`: this
            way the control releases its space, the row collapses and everything
            below moves up, without leaving ghost gaps. The widget still exists,
            ready to come back."""
            if self.var_modo.get() == "seconds":
                self.fila_segundos.grid()
                self.fila_segundos.grid_configure(row=6, column=0, padx=16,
                                                  pady=(8, 0), sticky="ew")
            else:
                self.fila_segundos.grid_remove()

        def _cambiar_segundos(self, valor: float) -> None:
            self.lbl_segundos.configure(text=f"Seconds: {int(valor)}")

        def _cambiar_paralelo(self) -> None:
            activo = bool(self.var_paralelo.get())
            self.opcion_workers.configure(state="normal" if activo else "disabled")

        def _workers(self) -> int:
            if not self.var_paralelo.get():
                return 1                              # sequential
            elegido = self.opcion_workers.get()
            return int(elegido) if elegido.isdigit() else 0   # 0 = automatic

        def _actualizar_estado(self) -> None:
            activo = self.gestor.activo
            self.btn_analizar.configure(state="disabled" if activo else "normal")
            self.btn_cancelar.configure(state="normal" if activo else "disabled")
            for widget in (self.entrada, self.btn_examinar, self.opcion_modo,
                           self.check_pdf, self.switch_paralelo, self.check_cache,
                           self.slider_segundos):
                widget.configure(state="disabled" if activo else "normal")
            if not activo:
                self._cambiar_modo()
                self._cambiar_paralelo()
            hay_pdf = bool(self.ruta_pdf and os.path.exists(self.ruta_pdf))
            self.btn_abrir_pdf.configure(state="normal" if hay_pdf else "disabled")

        def _mostrar_resumen(self, color: str, texto: str) -> None:
            fondo, tinta = COLORES.get(color, COLORES["gris"])
            self.tarjeta.configure(fg_color=fondo)
            self.lbl_resumen.configure(text=texto, text_color=tinta)

        # ── analysis lifecycle ──────────────────────────────────────────────
        def _analizar(self) -> None:
            ruta = self.var_ruta.get().strip().strip('"')
            if not ruta:
                self._alerta("Path missing", "Choose a folder or a .flac file.")
                return
            if not os.path.exists(ruta):
                self._alerta("Path does not exist", f"Not found:\n{ruta}", error=True)
                return

            self.eventos = []
            self.total = 0
            self.recibidos = 0
            self.ruta_pdf = None
            self.barra.set(0)
            self.lbl_progreso.configure(text="")
            self.lbl_estado.configure(text="Analyzing…")
            self._mostrar_resumen("gris", "Analyzing…")
            try:
                comando = self.gestor.iniciar(
                    ruta, modo=self.var_modo.get(),
                    segundos=int(self.slider_segundos.get()),
                    pdf=bool(self.var_pdf.get()), workers=self._workers(),
                    sin_cache=bool(self.var_sin_cache.get()))
            except Exception as e:
                self._log(f"Could not start the analysis: {e}", "error")
                self._alerta("Could not start the engine", str(e), error=True)
                self._actualizar_estado()
                return
            self._log("Running: " + " ".join(comando[1:]))
            self._actualizar_estado()
            self.after(100, self._drenar_cola)

        def _cancelar(self) -> None:
            if not self.gestor.activo:
                return
            self._log("Cancelling the analysis and its child processes…", "aviso")
            self.gestor.cancelar()
            self.lbl_estado.configure(text="Cancelled")
            self._actualizar_estado()

        def _drenar_cola(self) -> None:
            """Runs on the interface thread: here it is safe to touch widgets.

            It is rescheduled while the process is still alive **or** while the
            last drain took something out: if the engine finishes right after a
            pass, the `__fin__` event would stay in the queue and the window would
            never show the final summary."""
            if self.gestor.activo or self._vaciar_cola():
                self.after(100, self._drenar_cola)

        def _vaciar_cola(self) -> bool:
            """Processes everything in the queue. True if it processed something."""
            algo = False
            try:
                while True:
                    canal, evento = self.cola.get_nowait()
                    self._procesar(canal, evento)
                    algo = True
            except queue.Empty:
                return algo

        def _procesar(self, canal: str, evento: Evento) -> None:
            tipo = evento.get("tipo")
            if canal == "stderr" or tipo == "__stderr__":
                self._log(f"motor: {evento.get('mensaje', '').strip()}", "aviso")
                return
            if tipo == "inicio":
                self.total = int(evento.get("total") or 0)
                self._log(f"Analyzing {self.total} file(s) · "
                          f"{evento.get('workers', '?')} process(es)"
                          + ("" if evento.get("cache", True) else " · no cache"))
                self.lbl_progreso.configure(text=f"0 of {self.total}")
            elif tipo == "resultado":
                self.eventos.append(evento)
                self.recibidos += 1
                etiqueta = "ok"
                if evento.get("error"):
                    etiqueta = "error"
                elif evento.get("veredicto") in ("SUSPICIOUS", "PROBABLE UPSCALE"):
                    etiqueta = "aviso"
                self._log(formatear_resultado(evento), etiqueta)
                if self.total:
                    self.barra.set(min(1.0, self.recibidos / self.total))
                    self.lbl_progreso.configure(text=f"{self.recibidos} of {self.total}")
            elif tipo == "informe":
                self.ruta_pdf = evento.get("ruta")
                self._log(f"PDF report: {self.ruta_pdf} "
                          f"({evento.get('paginas', '?')} pages)", "ok")
                self._actualizar_estado()
            elif tipo == "aviso":
                self._alerta("Engine warning", str(evento.get("mensaje")), error=False)
            elif tipo == "error":
                self._alerta("Engine error", str(evento.get("mensaje")), error=True)
            elif tipo == "__fin__":
                self._finalizar(evento)
            elif tipo == "__ilegible__":
                self._log(f"Unreadable line from the engine: {evento.get('mensaje')}", "aviso")
            elif tipo == "__aviso_lectura__":
                self._log(f"Problem reading the output: {evento.get('mensaje')}", "aviso")

        def _finalizar(self, evento: Evento) -> None:
            self.barra.set(1.0 if not evento.get("cancelado") else self.barra.get())
            color, texto = formatear_resumen(self.eventos)
            if evento.get("cancelado"):
                self.lbl_estado.configure(text="Cancelled")
                self._log("Analysis cancelled.", "aviso")
            elif evento.get("codigo") not in (0, None):
                self.lbl_estado.configure(text=f"Finished with code {evento['codigo']}")
                self._log(f"The engine finished with code {evento['codigo']}.", "error")
                self._alerta("The engine finished with an error",
                             f"Exit code: {evento['codigo']}", error=True)
            else:
                self.lbl_estado.configure(text="Finished")
                self._log("Analysis finished.", "ok")
            self._mostrar_resumen(color, texto)
            self._actualizar_estado()

        def _abrir_pdf(self) -> None:
            if not self.ruta_pdf or not os.path.exists(self.ruta_pdf):
                self._alerta("No report", "No PDF has been generated yet.")
                return
            if not abrir_archivo(self.ruta_pdf):
                self._alerta("Could not open the PDF",
                             f"Open it manually:\n{self.ruta_pdf}", error=True)

        def _alerta(self, titulo: str, mensaje: str, error: bool = False) -> None:
            """Native alert. It is also written to the log, so it can be reviewed later."""
            self._log(f"{titulo}: {mensaje}", "error" if error else "aviso")
            from tkinter import messagebox
            try:
                (messagebox.showerror if error else messagebox.showwarning)(titulo, mensaje)
            except Exception:
                pass                      # without a window (tests) there is no dialog

        def _al_cerrar(self) -> None:
            """Closing the window must not leave live processes behind."""
            if self.gestor.activo:
                self._log("Closing: cancelling the running analysis…", "aviso")
                self.gestor.cancelar()
                self.gestor.esperar(tiempo=3.0)
            self.destroy()


def main(ruta_inicial: str | None = None) -> int:
    """Entry point of the graphical interface."""
    if not CTK_OK:
        print("⚠️  customtkinter is not installed, so there is no graphical interface.")
        print("   Install it with:  pip install customtkinter")
        print("   Meanwhile you can use the CLI:  python verificar_flac.py")
        return 1
    try:
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")
        ventana = VentanaFlacVerifier(ruta_inicial=ruta_inicial)
        ventana.mainloop()
        return 0
    except Exception as e:                     # no display, no Tk, no permissions…
        print(f"Could not open the graphical interface: {type(e).__name__}: {e}")
        print("In an environment without a desktop use the CLI: python verificar_flac.py")
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else None))
