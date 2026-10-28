"""Graphical interface for FLAC VERIFIER (CustomTkinter).

Two ways to launch it:

    python gui.py
    python verificar_flac.py --gui

Architecture (designed so that the interface never freezes on Windows):

    - The analysis runs in a separate PROCESS (`subprocess.Popen` with `-u`),
      using the same interpreter that runs the GUI.
    - A daemon THREAD reads stdout line by line (the engine's NDJSON protocol)
      and puts the events into a `queue.Queue`.
    - The window drains that queue in bounded batches (`BATCH_DRAINED` events per
      tick, rescheduled with a short interval while there is a backlog): no widget
      is touched from the reader thread, and a huge batch cannot freeze the window.

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
from collections import deque
from typing import Any, Callable, Iterable

import informe_pdf

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

Event = dict[str, Any]
Message = tuple[str, Event]             # (channel, event) as it travels through the queue

DIRECTORY = os.path.dirname(os.path.abspath(__file__))
ENGINE = os.path.join(DIRECTORY, "motor_flac.py")
# Single source: the report generator owns the file name and the GUI mirrors it,
# so the engine event and the "Open PDF" button can never disagree.
REPORT_NAME = informe_pdf.REPORT_NAME
# Engine executable packaged next to the GUI (see flac_verifier.spec).
ENGINE_EXE_NAME = "flac_motor.exe"
FLAG_ENGINE_CLI = "--motor-cli"


def packaging() -> bool:
    """Are we running inside a PyInstaller executable?"""
    return bool(getattr(sys, "frozen", False))


def engine_path() -> list[str]:
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
    if packaging():
        folder = os.path.dirname(os.path.abspath(sys.executable))
        engine_exe = os.path.join(folder, ENGINE_EXE_NAME)
        if os.path.exists(engine_exe):
            return [engine_exe, FLAG_ENGINE_CLI]
        return [sys.executable, FLAG_ENGINE_CLI]
    return [sys.executable, "-u", ENGINE]

# Visual identity: the assets are generated with Logo/generar_logo.py, which
# defines the design. If they are missing, the window still works (it only warns).
LOGO_FOLDER = os.path.join(DIRECTORY, "Logo")
LOGO_ICON   = os.path.join(LOGO_FOLDER, "icono_app.ico")           # window and .exe
LOGO_ICONPHOTO = os.path.join(LOGO_FOLDER, "logo_flac_verifier_128.png")  # outside Windows
LOGO_BRAND   = os.path.join(LOGO_FOLDER, "logo_flac_verifier.png")  # header and README
BRAND_COLOR  = "#4f98a3"          # the teal of the splash

# Short labels for the log, and colours per verdict (the same as in the PDF).
LABEL_VERDICT = {
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
MODES = ["center", "full", "seconds"]
WORKER_OPTIONS = ["Automatic", "1", "2", "4", "8"]

# ─── UI throughput and memory limits ─────────────────────────────────────────
BATCH_DRAINED = 200             # hard cap of engine events per UI tick
MAX_RESULTS_BY_TICK = 24   # redraw the progress bar once per batch
ACTIVE_INTERVAL_MS = 30       # reschedule fast while events are pending
IDLE_INTERVAL_MS = 100      # gentle polling while the engine works quietly
MAX_EVENTS_IN_MEMORY = 5000  # result events kept on screen / for the final summary
MAX_LINES_LOG = 3000          # log lines kept in the text box
MAX_QUEUE = 5000                # bounded queue: backpressure instead of unbounded growth
COLORES_LOG = {"error": "#E06C6C", "aviso": "#E0A96C", "ok": "#7FD18A", "info": "#9AA4B2"}


# ─── LOGIC (usable without a window) ─────────────────────────────────────────

def build_command(path: str, mode: str = "center", seconds: int | None = None,
                      pdf: bool = True, workers: int = 0, without_cache: bool = False,
                      engine: str | None = None) -> list[str]:
    """Engine command for the subprocess.

    From the source code `-u` is added (without it, the stdout buffer would hold
    the NDJSON events back instead of delivering them in real time). Packaged it
    is not needed: the engine is already packaged with unbuffered output and every
    line goes out with `flush=True`.
    """
    if engine:
        base = ([sys.executable, "-u", engine] if not packaging()
                else [engine, FLAG_ENGINE_CLI])
    else:
        base = engine_path()
    command = [*base, "--path", str(path), "--mode", mode]
    if mode == "seconds":
        command += ["--seconds", str(int(seconds or 15))]
    if pdf:
        command.append("--pdf")
    if workers and workers > 0:
        command += ["--workers", str(int(workers))]
    if without_cache:
        command.append("--no-cache")
    return command


def parse_event(line: str) -> Event | None:
    """Turns one NDJSON line into an event. None if the line is empty.

    An unreadable line cannot bring the reader down: it becomes an internal
    `__ilegible__` event so that there is a record of it in the log."""
    text = (line or "").strip()
    if not text:
        return None
    try:
        event = json.loads(text)
    except ValueError:
        return {"tipo": "__ilegible__", "mensaje": text[:300]}
    if not isinstance(event, dict):
        return {"tipo": "__ilegible__", "mensaje": text[:300]}
    if "tipo" not in event:
        event["tipo"] = "__desconocido__"
    return event


def format_result(event: Event) -> str:
    """Log line for a `resultado` event."""
    file = str(event.get("archivo", "?"))
    if event.get("error"):
        return f"[ERROR] {file}: {event['error']}"
    verdict = str(event.get("veredicto") or "indeterminate")
    label = LABEL_VERDICT.get(verdict, verdict)
    problems = len(event.get("problemas") or [])
    text = f"[{label}] {file}: {verdict}"
    score = event.get("score")
    if isinstance(score, (int, float)):
        text += f" · score {score * 100:.0f} %"
    text += f" · {problems} issue" + ("s" if problems != 1 else "")
    if event.get("desde_cache"):
        text += " · cache"
    return text


def format_summary(events: Iterable[Event]) -> tuple[str, str]:
    """Final summary of the batch: (colour_key, text) for the status card."""
    conteo: dict[str, int] = {}
    errors = 0
    total = 0
    for event in events:
        if event.get("tipo") != "resultado":
            continue
        total += 1
        if event.get("error") or "veredicto" not in event:
            errors += 1
        else:
            conteo[str(event["veredicto"])] = conteo.get(str(event["veredicto"]), 0) + 1
    if total == 0:
        return "gris", "No results"

    labels = []
    for key, name in (("GENUINE LOSSLESS", "genuine"),
                          ("PROBABLY LOSSLESS", "probable"),
                          ("SUSPICIOUS", "suspicious"),
                          ("PROBABLE UPSCALE", "upscale"),
                          ("indeterminate", "indeterminate")):
        if conteo.get(key):
            labels.append(f"{conteo[key]} {name}")
    if errors:
        labels.append(f"{errors} with error")

    if errors or conteo.get("PROBABLE UPSCALE"):
        color = "rojo"
    elif conteo.get("SUSPICIOUS"):
        color = "naranja"
    elif conteo.get("PROBABLY LOSSLESS") or conteo.get("indeterminate"):
        color = "amarillo"
    else:
        color = "verde"
    return color, f"{total} file(s): " + " · ".join(labels)


def dnd_paths(payload: str) -> list[str]:
    """Paths contained in a <<Drop>> event from tkinterdnd2.

    The widget delivers the paths with spaces between braces: `{C:/my music} {D:/other}`."""
    paths: list[str] = []
    current = ""
    inside = False
    for character in payload or "":
        if character == "{":
            inside = True
        elif character == "}":
            inside = False
            paths.append(current)
            current = ""
        elif character == " " and not inside:
            if current:
                paths.append(current)
                current = ""
        else:
            current += character
    if current:
        paths.append(current)
    return [r for r in paths if r]


def open_file(path: str) -> bool:
    """Opens a file with the system default viewer. Never raises."""
    try:
        if os.name == "nt":
            os.startfile(path)                       # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
        return True
    except Exception:
        return False


def finish_process(process: "subprocess.Popen[str] | None", wait_time: float = 3.0) -> None:
    """Terminates the process and ALL of its descendants. Never raises.

    The engine may spread the work across child processes (`--workers`), so killing
    only the parent would leave orphans behind: on Windows the tree is killed with
    taskkill and, whatever happens, the terminate()/kill() fallback remains."""
    if process is None or process.poll() is not None:
        return
    if os.name == "nt":
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(process.pid)],
                           capture_output=True, timeout=10,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except Exception:
            pass
    for accion in (process.terminate, process.kill):
        try:
            accion()
            process.wait(timeout=wait_time)
            return
        except Exception:
            continue


def output_reader(process: "subprocess.Popen[str]", message_queue: "queue.Queue[Message]",
                  cancelled: Callable[[], bool] | None = None) -> None:
    """Daemon thread: reads stdout and stderr and puts them into the queue.

    It always ends with a `__fin__` event so that the window knows the process
    finished, even if the read failed."""
    def read(stream: Any, channel: str) -> None:
        try:
            for line in stream:
                event = parse_event(line)
                if event is not None:
                    message_queue.put((channel, event))
        except Exception as e:                       # broken pipe, process killed…
            message_queue.put((channel, {"tipo": "__aviso_lectura__",
                              "mensaje": f"{type(e).__name__}: {e}"}))
        finally:
            try:
                stream.close()
            except Exception:
                pass

    thread_error = threading.Thread(target=read, args=(process.stderr, "stderr"), daemon=True)
    thread_error.start()
    read(process.stdout, "stdout")
    thread_error.join(timeout=2.0)
    try:
        code = process.wait(timeout=5.0)
    except Exception:
        code = None
    message_queue.put(("fin", {"tipo": "__fin__", "codigo": code,
                      "cancelado": bool(cancelled and cancelled())}))


class ProcessManager:
    """Lifecycle of the engine subprocess: start, read and cancel."""

    def __init__(self, message_queue: "queue.Queue[Message]", engine: str | None = None,
                 lanzador: Callable[..., Any] = subprocess.Popen) -> None:
        self.message_queue = message_queue
        self.engine = engine
        self._lanzador = lanzador                 # injectable for the tests
        self.process: "subprocess.Popen[str] | None" = None
        self.thread: threading.Thread | None = None
        self.cancelled = False

    @property
    def active(self) -> bool:
        return self.process is not None and self.process.poll() is None

    @property
    def active_thread(self) -> bool:
        """Is the reader thread still alive?

        The reader queues `__fin__` only AFTER the process is reaped, so `active`
        can be False while the final event has not reached the queue yet. The
        window must keep draining until the thread ends, or the last summary is
        lost."""
        return self.thread is not None and self.thread.is_alive()

    def start(self, path: str, mode: str = "center", seconds: int | None = None,
                pdf: bool = True, workers: int = 0, without_cache: bool = False) -> list[str]:
        """Launches the analysis. Returns the command used (useful for the log)."""
        if self.active:
            raise RuntimeError("an analysis is already running")
        command = build_command(path, mode=mode, seconds=seconds, pdf=pdf,
                                    workers=workers, without_cache=without_cache,
                                    engine=self.engine)
        self.cancelled = False
        # CREATE_NO_WINDOW stops a console from flashing when the engine starts.
        self.process = self._lanzador(
            command, cwd=DIRECTORY,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self.thread = threading.Thread(
            target=output_reader, args=(self.process, self.message_queue, lambda: self.cancelled),
            daemon=True, name="lector-motor")
        self.thread.start()
        return command

    def cancel(self) -> None:
        self.cancelled = True
        finish_process(self.process)

    def wait(self, duration: float = 5.0) -> None:
        if self.thread is not None:
            self.thread.join(timeout=duration)


# ─── WINDOW ─────────────────────────────────────────────────────────────────

if CTK_OK:

    class _BaseWindow(ctk.CTk, TkinterDnD.DnDWrapper if DND_OK else object):  # type: ignore[misc]
        """Base that adds drag and drop only if tkinterdnd2 is installed."""

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            if DND_OK:
                try:
                    self.tkdnd_version = TkinterDnD._require(self)
                except Exception:
                    pass

    class FlacVerifierWindow(_BaseWindow):
        """Main window: controls on the left, telemetry on the right."""

        def __init__(self, initial_path: str | None = None, engine: str | None = None) -> None:
            super().__init__()
            self.title("FLAC VERIFIER — FLAC authenticity verification")
            self.geometry("1120x740")
            self.minsize(980, 640)

            self.message_queue: "queue.Queue[Message]" = queue.Queue(maxsize=MAX_QUEUE)
            self.manager = ProcessManager(self.message_queue, engine=engine)
            # Bounded: a very long batch must not grow the window's memory forever.
            self.events: "deque[Evento]" = deque(maxlen=MAX_EVENTS_IN_MEMORY)
            self.total = 0
            self.recibidos = 0
            self.pdf_path: str | None = None

            self.grid_columnconfigure(0, weight=0)
            self.grid_columnconfigure(1, weight=1)
            self.grid_rowconfigure(0, weight=1)

            self._build_controls()
            self._build_telemetry()
            self._load_identity()
            self._update_state()
            self.protocol("WM_DELETE_WINDOW", self._on_close)
            self._register_dnd()

            if initial_path:
                self.var_path.set(initial_path)
                self._log(f"Path ready: {initial_path}")
            self._log("Ready. Choose a folder or a .flac file and press Analyze.")
            if not DND_OK:
                self._log("Drag and drop is not available "
                          "(pip install tkinterdnd2 to enable it).", "aviso")
            if not self.icon_loaded:
                self._log("Project icon not found "
                          "(Logo/icono_app.ico); the window uses the default icon.",
                          "aviso")

        # ── widget construction ─────────────────────────────────────────────
        def _brand_image(self, side: int) -> Any:
            """The logo mark, scaled. None if the asset is missing."""
            if not os.path.exists(LOGO_BRAND):
                return None
            try:
                from PIL import Image as ImagenPIL
                with ImagenPIL.open(LOGO_BRAND) as original:
                    recorte = original.convert("RGBA").copy()
                return ctk.CTkImage(light_image=recorte, dark_image=recorte,
                                    size=(side, side))
            except Exception as e:
                self._log(f"Could not load the logo: {type(e).__name__}: {e}", "aviso")
                return None

        def _load_identity(self) -> None:
            """Window icon (and that of the future .exe) with the project logo.

            On Windows the multi-resolution .ico is used, which is what feeds the
            taskbar; elsewhere, iconphoto with a PNG. If the asset is missing, the
            window still opens."""
            self.icon_loaded = False
            if os.name == "nt" and os.path.exists(LOGO_ICON):
                try:
                    self.iconbitmap(LOGO_ICON)
                    self.icon_loaded = True
                except Exception:
                    self.icon_loaded = False
            if not self.icon_loaded and os.path.exists(LOGO_ICONPHOTO):
                try:
                    import tkinter
                    # Keep a reference: if the PhotoImage is collected by the GC
                    # the window icon silently disappears (only on non-Windows).
                    self._icon_window = tkinter.PhotoImage(file=LOGO_ICONPHOTO)
                    self.iconphoto(True, self._icon_window)
                    self.icon_loaded = True
                except Exception:
                    self.icon_loaded = False

        def _build_controls(self) -> None:
            panel = ctk.CTkFrame(self, width=330, corner_radius=0)
            panel.grid(row=0, column=0, sticky="nsw")
            panel.grid_propagate(False)
            panel.grid_columnconfigure(0, weight=1)

            # Header: the logo mark next to the name, as in the splash
            header = ctk.CTkFrame(panel, fg_color="transparent")
            header.grid(row=0, column=0, padx=16, pady=(18, 14), sticky="ew")
            brand = self._brand_image(46)
            text_column = 1 if brand is not None else 0
            if brand is not None:
                ctk.CTkLabel(header, text="", image=brand).grid(
                    row=0, column=0, rowspan=2, padx=(0, 12))
            ctk.CTkLabel(header, text="FLAC VERIFIER",
                         font=ctk.CTkFont(size=21, weight="bold")).grid(
                row=0, column=text_column, sticky="w")
            ctk.CTkLabel(header, text="FAKE LOSSLESS DETECTION",
                         font=ctk.CTkFont(size=9), text_color=BRAND_COLOR).grid(
                row=1, column=text_column, sticky="w")

            ctk.CTkLabel(panel, text="Folder or .flac file",
                         font=ctk.CTkFont(size=12, weight="bold")).grid(
                row=1, column=0, padx=16, pady=(4, 2), sticky="w")
            self.var_path = ctk.StringVar(value="")
            self.input = ctk.CTkEntry(panel, textvariable=self.var_path,
                                        placeholder_text="C:/Music/Album")
            self.input.grid(row=2, column=0, padx=16, sticky="ew")
            self.btn_browse = ctk.CTkButton(panel, text="📂  Browse folder…",
                                              height=36, command=self._choose_folder)
            self.btn_browse.grid(row=3, column=0, padx=16, pady=(6, 14), sticky="ew")

            ctk.CTkLabel(panel, text="Spectral mode",
                         font=ctk.CTkFont(size=12, weight="bold")).grid(
                row=4, column=0, padx=16, pady=(4, 2), sticky="w")
            self.var_mode = ctk.StringVar(value="center")
            self.option_mode = ctk.CTkOptionMenu(panel, values=MODES,
                                                 variable=self.var_mode,
                                                 command=lambda _: self._change_mode())
            self.option_mode.grid(row=5, column=0, padx=16, sticky="ew")

            # The seconds only make sense in "seconds" mode: the control is
            # removed from the grid (not disabled) so that it leaves no gap and
            # the controls below move up.
            self.seconds_row = ctk.CTkFrame(panel, fg_color="transparent")
            self.seconds_row.grid(row=6, column=0, padx=16, pady=(8, 0), sticky="ew")
            self.seconds_row.grid_columnconfigure(0, weight=1)
            self.label_seconds = ctk.CTkLabel(self.seconds_row, text="Seconds: 15")
            self.label_seconds.grid(row=0, column=0, sticky="w")
            self.seconds_slider = ctk.CTkSlider(self.seconds_row, from_=5, to=120,
                                                 number_of_steps=23,
                                                 command=self._change_seconds)
            self.seconds_slider.set(15)
            self.seconds_slider.grid(row=1, column=0, sticky="ew")

            self.var_pdf = ctk.BooleanVar(value=True)
            self.check_pdf = ctk.CTkCheckBox(panel, text="Generate PDF report",
                                             variable=self.var_pdf)
            self.check_pdf.grid(row=7, column=0, padx=16, pady=(14, 6), sticky="w")

            self.var_parallel = ctk.BooleanVar(value=True)
            self.parallel_switch = ctk.CTkSwitch(panel, text="Parallel processing",
                                                 variable=self.var_parallel,
                                                 command=self._change_parallel)
            self.parallel_switch.grid(row=8, column=0, padx=16, pady=(6, 2), sticky="w")
            self.option_workers = ctk.CTkOptionMenu(panel, values=WORKER_OPTIONS,
                                                    width=150)
            self.option_workers.set(WORKER_OPTIONS[0])
            self.option_workers.grid(row=9, column=0, padx=16, pady=(2, 6), sticky="w")

            self.var_without_cache = ctk.BooleanVar(value=False)
            self.check_cache = ctk.CTkCheckBox(panel, text="Ignore cache",
                                               variable=self.var_without_cache)
            self.check_cache.grid(row=10, column=0, padx=16, pady=(6, 14), sticky="w")

            self.btn_analyze = ctk.CTkButton(panel, text="▶  Analyze", height=44,
                                              font=ctk.CTkFont(size=15, weight="bold"),
                                              command=self._analyze)
            self.btn_analyze.grid(row=11, column=0, padx=16, sticky="ew")
            self.btn_cancel = ctk.CTkButton(panel, text="■  Cancel", height=36,
                                              fg_color="#8B1A1A",
                                              hover_color="#A11616",
                                              command=self._cancel)
            self.btn_cancel.grid(row=12, column=0, padx=16, pady=(6, 14), sticky="ew")

            self.card = ctk.CTkFrame(panel, corner_radius=10)
            self.card.grid(row=13, column=0, padx=16, pady=(0, 8), sticky="ew")
            self.label_summary = ctk.CTkLabel(self.card, text="Nothing analyzed yet",
                                            wraplength=280, justify="left")
            self.label_summary.pack(padx=12, pady=10)

        def _build_telemetry(self) -> None:
            panel = ctk.CTkFrame(self, corner_radius=0, fg_color="transparent")
            panel.grid(row=0, column=1, sticky="nsew", padx=(0, 0))
            panel.grid_columnconfigure(0, weight=1)
            panel.grid_rowconfigure(3, weight=1)

            header = ctk.CTkFrame(panel, fg_color="transparent")
            header.grid(row=0, column=0, sticky="ew", padx=16, pady=(18, 6))
            header.grid_columnconfigure(0, weight=1)
            self.label_state = ctk.CTkLabel(header, text="Idle",
                                           font=ctk.CTkFont(size=14, weight="bold"))
            self.label_state.grid(row=0, column=0, sticky="w")
            self.label_progress = ctk.CTkLabel(header, text="")
            self.label_progress.grid(row=0, column=1, sticky="e")

            self.barra = ctk.CTkProgressBar(panel, height=14)
            self.barra.set(0)
            self.barra.grid(row=1, column=0, sticky="ew", padx=16, pady=(0, 10))

            self.log = ctk.CTkTextbox(panel, wrap="word",
                                      font=ctk.CTkFont(family="Consolas", size=12))
            self.log.grid(row=3, column=0, sticky="nsew", padx=16, pady=(0, 10))
            self.log.configure(state="disabled")
            # Tag colours are configured ONCE here; _log only reuses them.
            for label, color in COLORES_LOG.items():
                self.log.tag_config(label, foreground=color)

            acciones = ctk.CTkFrame(panel, fg_color="transparent")
            acciones.grid(row=4, column=0, sticky="ew", padx=16, pady=(0, 16))
            acciones.grid_columnconfigure(0, weight=1)
            self.btn_open_pdf = ctk.CTkButton(acciones, text="📄  Open PDF report",
                                               height=38, command=self._open_pdf)
            self.btn_open_pdf.grid(row=0, column=0, sticky="w")

        # ── view helpers ────────────────────────────────────────────────────
        def _log(self, text: str, label: str = "info",
                 scroll: bool = True) -> None:
            brand = time.strftime("%H:%M:%S")
            self.log.configure(state="normal")
            self.log.insert("end", f"[{brand}] {text}\n")
            try:                                     # colouring if the widget supports it
                self.log.tag_add(label, "end-2l", "end-1c")
            except Exception:
                pass
            self._trim_log()
            self.log.configure(state="disabled")
            if scroll:
                self.log.see("end")

        def _trim_log(self) -> None:
            """Keeps the text box bounded: drops the oldest lines past the limit."""
            try:
                total = int(self.log.index("end-1c").split(".")[0])
                if total > MAX_LINES_LOG:
                    self.log.delete("1.0", f"{total - MAX_LINES_LOG + 1}.0")
            except Exception:
                pass

        def _register_dnd(self) -> bool:
            if not DND_OK:
                return False
            try:
                self.input.drop_target_register(DND_FILES)
                self.input.dnd_bind("<<Drop>>", self._drop)
                return True
            except Exception:
                return False

        def _drop(self, event: Any) -> None:
            paths = dnd_paths(getattr(event, "data", ""))
            if paths:
                self.var_path.set(paths[0])
                self._log(f"Dropped: {paths[0]}")

        def _choose_folder(self) -> None:
            from tkinter import filedialog
            initial = self.var_path.get() or os.path.expanduser("~")
            chosen = filedialog.askdirectory(title="Choose the album folder",
                                              initialdir=initial if os.path.isdir(initial) else None)
            if chosen:
                self.var_path.set(chosen)
                self._log(f"Folder selected: {chosen}")

        def _change_mode(self) -> None:
            """Shows or hides the seconds control depending on the mode.

            `grid_remove()`/`grid()` is used and not `configure(state=...)`: this
            way the control releases its space, the row collapses and everything
            below moves up, without leaving ghost gaps. The widget still exists,
            ready to come back."""
            if self.var_mode.get() == "seconds":
                self.seconds_row.grid()
                self.seconds_row.grid_configure(row=6, column=0, padx=16,
                                                  pady=(8, 0), sticky="ew")
            else:
                self.seconds_row.grid_remove()

        def _change_seconds(self, value: float) -> None:
            self.label_seconds.configure(text=f"Seconds: {int(value)}")

        def _change_parallel(self) -> None:
            active = bool(self.var_parallel.get())
            self.option_workers.configure(state="normal" if active else "disabled")

        def _workers(self) -> int:
            if not self.var_parallel.get():
                return 1                              # sequential
            chosen = self.option_workers.get()
            return int(chosen) if chosen.isdigit() else 0   # 0 = automatic

        def _update_state(self) -> None:
            active = self.manager.active
            self.btn_analyze.configure(state="disabled" if active else "normal")
            self.btn_cancel.configure(state="normal" if active else "disabled")
            for widget in (self.input, self.btn_browse, self.option_mode,
                           self.check_pdf, self.parallel_switch, self.check_cache,
                           self.seconds_slider):
                widget.configure(state="disabled" if active else "normal")
            if not active:
                self._change_mode()
                self._change_parallel()
            there_is_pdf = bool(self.pdf_path and os.path.exists(self.pdf_path))
            self.btn_open_pdf.configure(state="normal" if there_is_pdf else "disabled")

        def _show_summary(self, color: str, text: str) -> None:
            background, tinta = COLORES.get(color, COLORES["gris"])
            self.card.configure(fg_color=background)
            self.label_summary.configure(text=text, text_color=tinta)

        # ── analysis lifecycle ──────────────────────────────────────────────
        def _analyze(self) -> None:
            path = self.var_path.get().strip().strip('"')
            if not path:
                self._alert("Path missing", "Choose a folder or a .flac file.")
                return
            if not os.path.exists(path):
                self._alert("Path does not exist", f"Not found:\n{path}", error=True)
                return

            self.events.clear()
            self.total = 0
            self.recibidos = 0
            self.pdf_path = None
            self.barra.set(0)
            self.label_progress.configure(text="")
            self.label_state.configure(text="Analyzing…")
            self._show_summary("gris", "Analyzing…")
            try:
                command = self.manager.start(
                    path, mode=self.var_mode.get(),
                    seconds=int(self.seconds_slider.get()),
                    pdf=bool(self.var_pdf.get()), workers=self._workers(),
                    without_cache=bool(self.var_without_cache.get()))
            except Exception as e:
                self._log(f"Could not start the analysis: {e}", "error")
                self._alert("Could not start the engine", str(e), error=True)
                self._update_state()
                return
            self._log("Running: " + " ".join(command[1:]))
            self._update_state()
            self.after(100, self._drain_queue)

        def _cancel(self) -> None:
            if not self.manager.active:
                return
            self._log("Cancelling the analysis and its child processes…", "aviso")
            self.manager.cancel()
            self.label_state.configure(text="Cancelled")
            self._update_state()

        def _drain_queue(self) -> None:
            """Runs on the interface thread: here it is safe to touch widgets.

            It processes a BOUNDED batch per tick: a huge backlog can no longer
            freeze the window, because Tk regains control between batches and the
            Cancel button stays responsive. It is rescheduled while the engine is
            alive, while the reader thread is still alive (it may not have queued
            the final `__fin__` yet) or while a full batch was just processed."""
            processed = 0
            results = 0
            try:
                while (processed < BATCH_DRAINED
                       and results < MAX_RESULTS_BY_TICK):
                    channel, event = self.message_queue.get_nowait()
                    is_result = (channel == "stdout"
                                    and event.get("tipo") == "resultado")
                    try:
                        # Non-result events keep their immediate handling; the
                        # progress widgets are redrawn once per batch below.
                        self._process(channel, event,
                                       update_progress=not is_result)
                    except Exception as e:      # one bad event cannot stall the UI
                        self._log(f"Could not process an engine event: "
                                  f"{type(e).__name__}: {e}", "error")
                    processed += 1
                    if is_result:
                        results += 1
            except queue.Empty:
                pass

            if results:
                self._update_progress()

            try:
                if not self.winfo_exists():
                    return
            except Exception:
                return
            backlog = results >= MAX_RESULTS_BY_TICK or processed >= BATCH_DRAINED
            if backlog or self.manager.active or self.manager.active_thread:
                self.after(ACTIVE_INTERVAL_MS if backlog else IDLE_INTERVAL_MS,
                           self._drain_queue)

        def _update_progress(self) -> None:
            """One redraw of the bar and the counter per drained batch."""
            if not self.total:
                return
            self.barra.set(min(1.0, self.recibidos / self.total))
            self.label_progress.configure(text=f"{self.recibidos} of {self.total}")

        def _process(self, channel: str, event: Event,
                      update_progress: bool = True) -> None:
            kind = event.get("tipo")
            if channel == "stderr" or kind == "__stderr__":
                self._log(f"motor: {event.get('mensaje', '').strip()}", "aviso")
                return
            if kind == "inicio":
                self.total = int(event.get("total") or 0)
                self._log(f"Analyzing {self.total} file(s) · "
                          f"{event.get('workers', '?')} process(es)"
                          + ("" if event.get("cache", True) else " · no cache"))
                self.label_progress.configure(text=f"0 of {self.total}")
            elif kind == "resultado":
                self.events.append(event)
                self.recibidos += 1
                label = "ok"
                if event.get("error"):
                    label = "error"
                elif event.get("veredicto") in ("SUSPICIOUS", "PROBABLE UPSCALE"):
                    label = "aviso"
                self._log(format_result(event), label, scroll=False)
                for evidence_item in event.get("evidencias_forenses") or []:
                    self._log(f"    evidence: {evidence_item}", "info", scroll=False)
                # One layout/scroll per result block, not once per evidence line.
                try:
                    self.log.see("end")
                except Exception:
                    pass
                if update_progress:
                    self._update_progress()
            elif kind == "informe":
                self.pdf_path = event.get("ruta")
                self._log(f"PDF report: {self.pdf_path} "
                          f"({event.get('paginas', '?')} pages)", "ok")
                self._update_state()
            elif kind == "aviso":
                # Non-blocking: engine warnings go to the log. A modal dialog here
                # would freeze the Tk event loop on every warning.
                self._log(f"Engine warning: {event.get('mensaje')}", "aviso")
            elif kind == "error":
                # The blocking dialog is reserved for a fatal non-zero exit, shown
                # once in _finalizar, not for every engine error event.
                self._log(f"Engine error: {event.get('mensaje')}", "error")
            elif kind == "__fin__":
                self._finish(event)
            elif kind == "__ilegible__":
                self._log(f"Unreadable line from the engine: {event.get('mensaje')}", "aviso")
            elif kind == "__aviso_lectura__":
                self._log(f"Problem reading the output: {event.get('mensaje')}", "aviso")

        def _finish(self, event: Event) -> None:
            self.barra.set(1.0 if not event.get("cancelado") else self.barra.get())
            color, text = format_summary(self.events)
            if event.get("cancelado"):
                self.label_state.configure(text="Cancelled")
                self._log("Analysis cancelled.", "aviso")
            elif event.get("codigo") not in (0, None):
                self.label_state.configure(text=f"Finished with code {event['codigo']}")
                self._log(f"The engine finished with code {event['codigo']}.", "error")
                self._alert("The engine finished with an error",
                             f"Exit code: {event['codigo']}", error=True)
            else:
                self.label_state.configure(text="Finished")
                self._log("Analysis finished.", "ok")
            self._show_summary(color, text)
            self._update_state()

        def _open_pdf(self) -> None:
            if not self.pdf_path or not os.path.exists(self.pdf_path):
                self._alert("No report", "No PDF has been generated yet.")
                return
            if not open_file(self.pdf_path):
                self._alert("Could not open the PDF",
                             f"Open it manually:\n{self.pdf_path}", error=True)

        def _alert(self, title: str, message: str, error: bool = False) -> None:
            """Native alert. It is also written to the log, so it can be reviewed later."""
            self._log(f"{title}: {message}", "error" if error else "aviso")
            from tkinter import messagebox
            try:
                (messagebox.showerror if error else messagebox.showwarning)(title, message)
            except Exception:
                pass                      # without a window (tests) there is no dialog

        def _on_close(self) -> None:
            """Closing the window must not leave live processes behind."""
            if self.manager.active:
                self._log("Closing: cancelling the running analysis…", "aviso")
                self.manager.cancel()
                self.manager.wait(duration=3.0)
            self.destroy()


def main(initial_path: str | None = None) -> int:
    """Entry point of the graphical interface."""
    if not CTK_OK:
        print("⚠️  customtkinter is not installed, so there is no graphical interface.")
        print("   Install it with:  pip install customtkinter")
        print("   Meanwhile you can use the CLI:  python verificar_flac.py")
        return 1
    try:
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")
        window = FlacVerifierWindow(initial_path=initial_path)
        window.mainloop()
        return 0
    except Exception as e:                     # no display, no Tk, no permissions…
        print(f"Could not open the graphical interface: {type(e).__name__}: {e}")
        print("In an environment without a desktop use the CLI: python verificar_flac.py")
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else None))
