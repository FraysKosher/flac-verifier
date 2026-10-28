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
import tkinter
from collections import deque
from typing import Any, Callable, Iterable

import informe_pdf
import theme

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
ICON_FOLDER  = os.path.join(LOGO_FOLDER, "icons")                 # line icons (DESIGN.md 8)
ICON_SIZES   = (16, 20, 24)                                       # the sizes shipped on disk

# Short labels for the log, and colours per verdict (the same as in the PDF).
LABEL_VERDICT = {
    "GENUINE LOSSLESS":       "OK",
    "PROBABLY LOSSLESS": "OK?",
    "SUSPICIOUS":                 "SUSPICIOUS",
    "PROBABLE UPSCALE":       "UPSCALE",
    "indeterminate":          "?",
}
# Batch summary -> design tone of the verdict chip. Five legacy buckets, four
# verdict colours: "naranja" is part of the warn tone.
SUMMARY_TONES = {
    "verde": "ok",
    "amarillo": "warn",
    "naranja": "warn",
    "rojo": "bad",
    "gris": "unknown",
}
# Design tone -> (text token, background token, border token) of a chip (7.5).
CHIP_STYLES = {
    "ok": ("ok", "ok.bg", "ok.border"),
    "warn": ("warn", "warn.bg", "warn.border"),
    "bad": ("bad", "bad.bg", "bad.border"),
    "unknown": ("unknown", "unknown.bg", "unknown.border"),
    "danger": ("danger", "danger.bg", "danger"),
}
# Verdict of the protocol -> tone. Only maps labels; the verdict itself is never
# computed here (test_estructura guards that rule).
VERDICT_TONES = {
    "GENUINE LOSSLESS": "ok",
    "PROBABLY LOSSLESS": "warn",
    "SUSPICIOUS": "warn",
    "PROBABLE UPSCALE": "bad",
    "indeterminate": "unknown",
}
# Order and short name of each verdict in the album summary and its chips.
SUMMARY_NAMES = (
    ("GENUINE LOSSLESS", "genuine"),
    ("PROBABLY LOSSLESS", "probable"),
    ("SUSPICIOUS", "suspicious"),
    ("PROBABLE UPSCALE", "upscale"),
    ("indeterminate", "indeterminate"),
)
# STREAMINFO MD5 state of the protocol -> short mark and icon.
MD5_LABELS = {
    "match": "verified",
    "mismatch": "mismatch",
    "absent": "absent",
    "unverifiable": "unverifiable",
}
MD5_ICONS = {
    "match": ("check", "ok"),
    "mismatch": ("x", "bad"),
    "absent": ("info", "unknown"),
    "unverifiable": ("info", "unknown"),
}
# Table columns of DESIGN.md 7.7: name, weight, minimum width, anchor.
TABLE_COLUMNS = (
    ("file", 1, 140, "w"),
    ("verdict", 0, 130, "w"),
    ("score", 0, 60, "e"),
    ("depth", 0, 70, "w"),
    ("rate", 0, 78, "w"),
    ("dr", 0, 44, "w"),
    ("md5", 0, 48, "w"),
)
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
# Log level -> design token used to paint the line (DESIGN.md 7.10).
LOG_TAGS = {"info": "text.secondary", "ok": "ok", "warn": "warn", "error": "bad"}


# ─── PRESENTATION CONSTANTS ────────────────────────────────────────────

RAIL_WIDTH = 340
WINDOW_GEOMETRY = "1180x760"
WINDOW_MINIMUM = (1024, 680)


def c(token: str) -> str:
    """Colour of a design token, so the widget code reads as the design brief."""
    return theme.color(token)


# Button variants of DESIGN.md 7.1. `border` is None when the variant has no
# visible border: the border band is still reserved (same colour as the fill or
# as the surface behind it) so the 2 px focus ring never moves the layout.
BUTTON_VARIANTS = {
    "primary":   {"height": 44, "radius": 10, "fill": "accent",
                  "hover": "accent.hover", "pressed": "accent.pressed",
                  "text": "on.accent", "border": None, "border_width": 2,
                  "size": 14, "weight": 600},
    "secondary": {"height": 40, "radius": 8, "fill": "bg.raised",
                  "hover": "bg.hover", "pressed": "border.strong",
                  "text": "text.primary", "border": "border.subtle",
                  "border_width": 1, "size": 13, "weight": 500},
    "ghost":     {"height": 36, "radius": 8, "fill": "transparent",
                  "hover": "bg.hover", "pressed": "bg.raised",
                  "text": "text.secondary", "border": None, "border_width": 2,
                  "size": 13, "weight": 500},
    "danger":    {"height": 40, "radius": 8, "fill": "transparent",
                  "hover": "danger.bg", "pressed": "danger.bg",
                  "text": "danger", "border": "danger", "border_width": 1,
                  "size": 13, "weight": 600},
    "icon":      {"height": 32, "radius": 8, "fill": "transparent",
                  "hover": "bg.hover", "pressed": "bg.raised",
                  "text": "text.secondary", "border": None, "border_width": 2,
                  "size": 13, "weight": 500},
}


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


def count_verdicts(events: Iterable[Event]) -> tuple[dict[str, int], int, int]:
    """(count by verdict, files with error, total files) of the result events.

    The only numbers the album summary and the hero show: they are counted from
    the protocol, never computed from the audio here."""
    counts: dict[str, int] = {}
    errors = 0
    total = 0
    for event in events:
        if event.get("tipo") != "resultado":
            continue
        total += 1
        if event.get("error") or "veredicto" not in event:
            errors += 1
        else:
            verdict = str(event["veredicto"])
            counts[verdict] = counts.get(verdict, 0) + 1
    return counts, errors, total


def format_summary(events: Iterable[Event]) -> tuple[str, str]:
    """Final summary of the batch: (colour_key, text) for the verdict hero."""
    counts, errors, total = count_verdicts(events)
    if total == 0:
        return "gris", "No results"

    labels = []
    for key, name in SUMMARY_NAMES:
        if counts.get(key):
            labels.append(f"{counts[key]} {name}")
    if errors:
        labels.append(f"{errors} with error")

    if errors or counts.get("PROBABLE UPSCALE"):
        color = "rojo"
    elif counts.get("SUSPICIOUS"):
        color = "naranja"
    elif counts.get("PROBABLY LOSSLESS") or counts.get("indeterminate"):
        color = "amarillo"
    else:
        color = "verde"
    files = f"{total} file" + ("s" if total != 1 else "")
    return color, f"{files}: " + " · ".join(labels)


def format_depth(bits: Any) -> str:
    """Bit depth as the table and the hero show it."""
    if isinstance(bits, (int, float)) and bits:
        return f"{int(bits)} bit"
    return "—"


def format_rate(sample_rate: Any) -> str:
    """Sample rate in kHz, without losing a non-integer rate."""
    if not isinstance(sample_rate, (int, float)) or sample_rate <= 0:
        return "—"
    if int(sample_rate) % 1000 == 0:
        return f"{int(sample_rate) // 1000} kHz"
    return f"{sample_rate / 1000:.1f} kHz"


def format_dr(dr: Any) -> str:
    """Dynamic range as the engine reports it (one decimal, or a whole number)."""
    if isinstance(dr, (int, float)):
        return f"{dr:g}"
    return "—"


def result_row(event: Event) -> dict[str, Any]:
    """Everything the table and the hero show for one `resultado` event.

    It only reads the protocol: no measurement, score or verdict is recomputed
    here (test_estructura guards that rule). A record from an older cache may
    lack a field; it degrades to a dash instead of raising."""
    meta = event.get("meta") if isinstance(event.get("meta"), dict) else {}
    md5 = event.get("md5") if isinstance(event.get("md5"), dict) else {}
    md5_state = str(md5.get("estado") or "")
    verdict = str(event.get("veredicto") or "")
    failed = bool(event.get("error")) or not verdict
    if failed:
        label, tone = "ERROR", "danger"
    else:
        label = LABEL_VERDICT.get(verdict, verdict)
        tone = VERDICT_TONES.get(verdict, "unknown")
    score = event.get("score")
    icon, icon_tone = MD5_ICONS.get(md5_state, ("info", "unknown"))
    return {
        "file": str(event.get("archivo") or "?"),
        "path": str(event.get("ruta") or ""),
        "verdict": "" if failed else verdict,
        "label": label,
        "tone": tone,
        "score": round(float(score) * 100) if isinstance(score, (int, float)) else None,
        "depth": format_depth(meta.get("bits_per_sample")),
        "rate": format_rate(meta.get("sample_rate")),
        "dr": format_dr(event.get("dr")),
        "md5": MD5_LABELS.get(md5_state, "—"),
        "md5_icon": icon,
        "md5_tone": icon_tone,
        "error": str(event.get("error") or ""),
        "cached": bool(event.get("desde_cache")),
    }


CUTOFF_LABELS = {
    "no_cutoff": "no cut-off",
    "near_nyquist_cutoff": "near Nyquist",
    "far_cutoff": "far cut-off",
}
LSBS_LABELS = {
    "active": "active",
    "partial": "partial",
    "empty": "empty",
    "indeterminate": "indeterminate",
}


def spectrogram_path(event: Event) -> str:
    """Path of the spectrogram PNG of a result, or "" when none was produced."""
    spectrum = event.get("esp")
    if not isinstance(spectrum, dict):
        return ""
    return str(spectrum.get("espectrograma") or "")


def cutoff_text(event: Event) -> str:
    """Cut-off category and frequency, from the spectrum block of the protocol."""
    spectrum = event.get("esp")
    if not isinstance(spectrum, dict) or not spectrum:
        return "—"
    if spectrum.get("error"):
        return "not analyzed"
    if not spectrum.get("corte_detectado"):
        return "no cut-off"
    category = CUTOFF_LABELS.get(str(spectrum.get("clase_corte")), "cut-off")
    frequency = spectrum.get("frecuencia_corte")
    if isinstance(frequency, (int, float)) and frequency:
        return f"{category} · {frequency / 1000:.1f} kHz"
    return category


def lsb_text(event: Event) -> str:
    """LSB utilization state of a 24-bit container (not applicable below that)."""
    bits = event.get("bdi")
    if not isinstance(bits, dict) or not bits:
        return "—"
    if not bits.get("aplica"):
        return "not applicable"
    return LSBS_LABELS.get(str(bits.get("lsbs")), "—")


def clipping_text(event: Event) -> str:
    """Full-scale clipping runs of the track."""
    clip = event.get("clip")
    if not isinstance(clip, dict) or not clip:
        return "—"
    runs = clip.get("runs_clip") or 0
    if clip.get("hay_clipping") or runs:
        return f"{runs} run(s) at full scale"
    return "no clipping"


def md5_detail(event: Event) -> str:
    """MD5 state plus the start of the recomputed stream hash."""
    md5 = event.get("md5")
    if not isinstance(md5, dict) or not md5:
        return "—"
    state = MD5_LABELS.get(str(md5.get("estado") or ""), "—")
    digest = str(md5.get("calculado") or "")
    return f"{state} · {digest[:8]}…" if digest else state


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

class _Tooltip:
    """Delayed tooltip for icon-only buttons (DESIGN.md 7.1: 300 ms).

    A tooltip is also the accessible name of an icon button: without it the
    button carries no text at all."""

    DELAY_MS = 300

    def __init__(self, widget: Any, text: str) -> None:
        self.widget = widget
        self.text = text
        self._after: str | None = None
        self._window: Any = None
        widget.bind("<Enter>", self._schedule)
        widget.bind("<Leave>", self._hide)
        widget.bind("<ButtonPress-1>", self._hide)

    def _schedule(self, _event: Any = None) -> None:
        self._cancel()
        self._after = self.widget.after(self.DELAY_MS, self._show)

    def _cancel(self) -> None:
        if self._after is not None:
            try:
                self.widget.after_cancel(self._after)
            except Exception:
                pass
            self._after = None

    def _show(self) -> None:
        self._after = None
        try:
            x = self.widget.winfo_rootx()
            y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
            self._window = tkinter.Toplevel(self.widget)
            self._window.wm_overrideredirect(True)
            self._window.wm_geometry(f"+{x}+{y}")
            label = tkinter.Label(self._window, text=self.text,
                                  background=c("bg.raised"),
                                  foreground=c("text.secondary"),
                                  font=theme.font(12), padx=theme.SPACE["xs"],
                                  pady=theme.SPACE["2xs"], borderwidth=0,
                                  highlightthickness=1,
                                  highlightbackground=c("border.subtle"))
            label.pack()
        except Exception:
            self._window = None

    def _hide(self, _event: Any = None) -> None:
        self._cancel()
        if self._window is not None:
            try:
                self._window.destroy()
            except Exception:
                pass
            self._window = None


class _DropZone(tkinter.Canvas):
    """120 px drop target with a dashed border (DESIGN.md 7.3).

    Tk has no dashed frame and CustomTkinter has no dashed border, so the border
    is drawn on a canvas and redrawn on resize; the content is a child frame
    placed in the middle. The whole area is clickable and reachable with the
    keyboard, and it is the drop target of the window when tkinterdnd2 is
    installed.
    """

    HEIGHT = 120
    RADIUS = 12
    DASH = (4, 4)

    def __init__(self, master: Any, window: Any) -> None:
        super().__init__(master, height=self.HEIGHT, bd=0, highlightthickness=2,
                         highlightbackground=c("bg.canvas"),
                         highlightcolor=c("focus"), cursor="hand2",
                         background=c("bg.surface"))
        self.window = window
        self.active = False

        self.content = ctk.CTkFrame(self, fg_color=c("bg.surface"), corner_radius=0)
        self._icon_resting = window._icon("folder-plus", 24, c("text.secondary"))
        self._icon_active = window._icon("folder-plus", 24, c("accent"))
        self.icon_label = ctk.CTkLabel(self.content, text="",
                                       image=self._icon_resting)
        self.icon_label.grid(row=0, column=0, pady=(0, theme.SPACE["xs"]))
        self.headline = ctk.CTkLabel(self.content,
                                     text="Drop a folder or a .flac file",
                                     font=theme.font(14, 600),
                                     text_color=c("text.primary"))
        self.headline.grid(row=1, column=0)
        self.subline = ctk.CTkLabel(self.content,
                                    text="or browse to pick an album folder",
                                    font=theme.font(12),
                                    text_color=c("text.secondary"))
        self.subline.grid(row=2, column=0, pady=(theme.SPACE["2xs"], 0))
        self._content_id = self.create_window(0, 0, window=self.content,
                                              anchor="center")

        tkinter.Canvas.configure(self, takefocus=True)
        self.bind("<Configure>", self._on_configure)
        for widget in (self, self.content, self.icon_label, self.headline,
                       self.subline):
            widget.bind("<Button-1>", self._browse)
            widget.bind("<Return>", self._browse)
            widget.bind("<space>", self._browse)

    def _browse(self, _event: Any = None) -> str:
        self.window._choose_folder()
        return "break"

    def _on_configure(self, event: Any) -> None:
        self.coords(self._content_id, event.width / 2, event.height / 2)
        self._draw_border(event.width, event.height)

    def _draw_border(self, width: int, height: int) -> None:
        self.delete("border")
        inset, radius = 2, self.RADIUS
        right, bottom = width - inset, height - inset
        colour = c("accent") if self.active else c("border.strong")
        draw = {"fill": colour, "dash": self.DASH, "width": 1, "tags": "border"}
        self.create_line(inset + radius, inset, right - radius, inset, **draw)
        self.create_line(inset + radius, bottom, right - radius, bottom, **draw)
        self.create_line(inset, inset + radius, inset, bottom - radius, **draw)
        self.create_line(right, inset + radius, right, bottom - radius, **draw)
        corners = (
            (inset, inset, inset + 2 * radius, inset + 2 * radius, 90),
            (right - 2 * radius, inset, right, inset + 2 * radius, 0),
            (right - 2 * radius, bottom - 2 * radius, right, bottom, 270),
            (inset, bottom - 2 * radius, inset + 2 * radius, bottom, 180),
        )
        for left, top, corner_right, corner_bottom, start in corners:
            self.create_arc(left, top, corner_right, corner_bottom, start=start,
                            extent=90, style="arc", outline=colour,
                            dash=self.DASH, width=1, tags="border")

    def set_active(self, active: bool) -> None:
        """Drag-over state: accent border and a 6 % accent tint (7.3)."""
        self.active = bool(active)
        tint = theme.mix(c("bg.surface"), c("accent"), 0.06) if self.active             else c("bg.surface")
        self.configure(background=tint)
        self.content.configure(fg_color=tint)
        self.icon_label.configure(image=self._icon_active if self.active
                                  else self._icon_resting)
        self._draw_border(self.winfo_width(), self.winfo_height())

    def register_drop(self) -> bool:
        """Registers the whole zone as a drop target, if the library is there."""
        if not DND_OK:
            return False
        try:
            for target in (self, self.content, self.icon_label, self.headline,
                           self.subline):
                target.drop_target_register(DND_FILES)
                target.dnd_bind("<<DropEnter>>", self._drag_enter)
                target.dnd_bind("<<DropLeave>>", self._drag_leave)
                target.dnd_bind("<<Drop>>", self._dropped)
            return True
        except Exception:
            return False

    def _drag_enter(self, event: Any = None) -> Any:
        self.set_active(True)
        return getattr(event, "action", None)

    def _drag_leave(self, event: Any = None) -> Any:
        self.set_active(False)
        return getattr(event, "action", None)

    def _dropped(self, event: Any) -> Any:
        self.set_active(False)
        self.window._drop(event)
        return getattr(event, "action", None)


class _TableScrollbar(tkinter.Canvas):
    """Thin scrollbar for the results table (DESIGN.md 7.7).

    CustomTkinter's own scrollbar redraws with an internal `update_idletasks()`
    from its Configure handler, and inside a canvas whose height settles by a
    pixel that recursion never ends (it locked the window during the first
    streaming test). A canvas with a thumb is small, predictable and enough: the
    table scrolls with the thumb, a click on the track and the mouse wheel.
    """

    WIDTH = 12
    MINIMUM_THUMB = 24

    def __init__(self, master: Any, command: Callable[..., Any],
                 background: str) -> None:
        super().__init__(master, width=self.WIDTH, bd=0, highlightthickness=0,
                         background=background, cursor="arrow")
        self.command = command
        self.first = 0.0
        self.last = 1.0
        self.bind("<Configure>", lambda _event: self._draw())
        self.bind("<Button-1>", self._jump)
        self.bind("<B1-Motion>", self._jump)

    def set(self, first: Any, last: Any) -> None:
        """`yscrollcommand` callback: only stores the range and redraws."""
        self.first, self.last = float(first), float(last)
        self._draw()

    def _draw(self) -> None:
        self.delete("all")
        height = self.winfo_height()
        if height <= 1:
            return
        top = self.first * height
        bottom = max(self.last * height, top + self.MINIMUM_THUMB)
        bottom = min(bottom, height)
        if bottom - top >= height - 2:
            return                                  # nothing to scroll
        self.create_rectangle(2, top + 1, self.WIDTH - 2, bottom - 1,
                              fill=c("border.strong"), outline="")

    def _jump(self, event: Any) -> str:
        height = self.winfo_height()
        if height > 1:
            centre = event.y / height
            span = self.last - self.first
            self.command("moveto", max(0.0, min(1.0, centre - span / 2)))
        return "break"


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

        # Thumbnail box inside the spectrogram frame of the hero (DESIGN.md 7.8).
        SPECTROGRAM_WIDTH = 212
        SPECTROGRAM_HEIGHT = 96
        # More issue lines than this are folded behind "Show all"; the text of
        # each issue is always shown in full, wrapped, never truncated.
        MAX_VISIBLE_PROBLEMS = 3

        def __init__(self, initial_path: str | None = None, engine: str | None = None) -> None:
            super().__init__(fg_color=c("bg.canvas"))
            # Resolve the families after the Tk root exists: Windows 10 has no
            # Segoe UI Variable and falls back to Segoe UI.
            theme.resolve_fonts(self)
            self.title("FLAC VERIFIER — FLAC authenticity verification")
            self.geometry(WINDOW_GEOMETRY)
            self.minsize(*WINDOW_MINIMUM)
            self.configure(fg_color=c("bg.canvas"))

            self.message_queue: "queue.Queue[Message]" = queue.Queue(maxsize=MAX_QUEUE)
            self.manager = ProcessManager(self.message_queue, engine=engine)
            # Bounded: a very long batch must not grow the window's memory forever.
            self.events: "deque[Evento]" = deque(maxlen=MAX_EVENTS_IN_MEMORY)
            self.total = 0
            self.recibidos = 0
            self.pdf_path: str | None = None
            # CTkImage references must stay alive: a collected image disappears
            # from the button silently.
            self._icons: dict[tuple[str, int, str], Any] = {}
            # Results surface: the selected row, its widgets and the text metrics
            # used by the ellipsis.
            self.selected_event: Event | None = None
            self._problems_expanded = False
            self.table_rows: list[dict[str, Any]] = []
            self._measures: dict[tuple[Any, ...], Any] = {}
            self._eyebrow_full = ""
            self._headline_full = ""
            self._last_log_line = ""
            # Bounded history of (timestamp, text, level) so a rebuild can put
            # the log back exactly as it was, colours included.
            self._log_history: deque[tuple[str, str, str]] = deque(
                maxlen=MAX_LINES_LOG)
            self.activity_expanded = False
            self._rebuilding = False
            self._hero_width = 0
            self._preview_width = 0
            self._spectrograms: dict[str, Any] = {}
            self._spectrogram_path_value = ""
            self._hero_layout_scheduled = False

            self.grid_columnconfigure(0, weight=0, minsize=RAIL_WIDTH)
            self.grid_columnconfigure(1, weight=1)
            self.grid_rowconfigure(0, weight=1)

            self._build_controls()
            self._build_telemetry()
            self._load_identity()
            self._render_hero()
            self._update_state()
            self.protocol("WM_DELETE_WINDOW", self._on_close)
            self.bind("<Escape>", self._escape_detail)
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
            if not os.path.isdir(ICON_FOLDER):
                self._log("Icon set not found (Logo/icons); the buttons show text only.",
                          "aviso")

        # ── presentation helpers ────────────────────────────────────────────
        def _icon(self, name: str, size: int = 16, color: str | None = None) -> Any:
            """Line icon from Logo/icons, tinted, or None when the asset is missing.

            The PNGs carry a white stroke, so one file per size serves every
            colour; tinting keeps the icons in the palette without shipping a
            copy per token."""
            tint = color or c("text.secondary")
            key = (name, size, tint)
            if key in self._icons:
                return self._icons[key]
            image = None
            path = os.path.join(ICON_FOLDER, f"{name}_{size}.png")
            if not os.path.exists(path):
                # CTkImage scales anyway, so an unlisted size borrows the closest
                # asset instead of silently losing the icon.
                closest = min(ICON_SIZES, key=lambda value: abs(value - size))
                path = os.path.join(ICON_FOLDER, f"{name}_{closest}.png")
            if os.path.exists(path):
                try:
                    from PIL import Image
                    with Image.open(path) as source:
                        alpha = source.convert("RGBA").getchannel("A")
                    coloured = Image.new("RGBA", alpha.size, tint)
                    coloured.putalpha(alpha)
                    image = ctk.CTkImage(light_image=coloured, dark_image=coloured,
                                         size=(size, size))
                except Exception:
                    image = None
            self._icons[key] = image
            return image

        def _section_label(self, parent: Any, text: str, row: int) -> Any:
            """11 px uppercase section label (DESIGN.md 5.3)."""
            label = ctk.CTkLabel(parent, text=text.upper(), anchor="w",
                                 font=theme.font(11, 600),
                                 text_color=c("text.tertiary"))
            label.grid(row=row, column=0, padx=theme.SPACE["lg"],
                       pady=(theme.SPACE["sm"], theme.SPACE["xs"]), sticky="ew")
            return label

        def _button(self, parent: Any, variant: str, text: str, command: Any,
                    icon: str | None = None, icon_size: int = 16,
                    width: int | None = None, height: int | None = None,
                    background: str | None = None) -> Any:
            """Button of DESIGN.md 7.1 with hover, pressed, focus and disabled.

            Tk has no shadows and CustomTkinter has no per-widget opacity, so the
            states are colours: the fill changes for hover/press, the reserved
            border band turns into the 2 px focus ring, and the disabled fill is
            the normal fill blended with the surface at 35 %."""
            spec = BUTTON_VARIANTS[variant]
            behind = background or c("bg.surface")
            if spec["border"]:
                border = c(spec["border"])
            elif spec["fill"] != "transparent":
                border = c(spec["fill"])
            else:
                border = behind
            image = self._icon(icon, icon_size, c(spec["text"])) if icon else None
            button = ctk.CTkButton(
                parent, text=text, command=command, width=width or 140,
                height=height or spec["height"], corner_radius=spec["radius"],
                border_width=spec["border_width"], border_color=border,
                fg_color=(c(spec["fill"]) if spec["fill"] != "transparent"
                          else "transparent"),
                hover_color=c(spec["hover"]), text_color=c(spec["text"]),
                text_color_disabled=c("text.tertiary"),
                font=theme.font(spec["size"], spec["weight"]),
                image=image, compound="left", cursor="hand2")
            button.ui_spec = {
                "spec": spec, "parent": behind, "border": border, "focused": False,
                "states": {"normal": c(spec["fill"]) if spec["fill"] != "transparent"
                                       else "transparent",
                           "hover": c(spec["hover"]),
                           "pressed": c(spec["pressed"])},
            }
            # takefocus is not an option CustomTkinter forwards, so it is set on
            # the underlying Tk frame directly.
            tkinter.Frame.configure(button, takefocus=True)
            button.bind("<ButtonPress-1>",
                        lambda _event: self._paint(button, "pressed"), add=True)
            button.bind("<ButtonRelease-1>",
                        lambda _event: self._paint(button, "hover"), add=True)
            button.bind("<Leave>",
                        lambda _event: self._paint(button, "normal"), add=True)
            # Focus and keys live on the frame, not on the canvas that
            # CustomTkinter uses for its own bindings.
            tkinter.Frame.bind(button, "<FocusIn>",
                               lambda _event: self._focus_ring(button, True))
            tkinter.Frame.bind(button, "<FocusOut>",
                               lambda _event: self._focus_ring(button, False))
            tkinter.Frame.bind(button, "<Return>",
                               lambda _event: self._activate(button))
            tkinter.Frame.bind(button, "<space>",
                               lambda _event: self._activate(button))
            return button

        def _paint(self, button: Any, state: str) -> None:
            """Repaints a button for one of its states, if it is enabled."""
            if button.cget("state") == "disabled":
                return
            ui = button.ui_spec
            colour = ui["states"][state]
            button.configure(fg_color=colour)
            if ui["spec"]["border"] is None and ui["spec"]["fill"] != "transparent":
                button.configure(border_color=c("focus") if ui["focused"] else colour)

        def _focus_ring(self, button: Any, focused: bool) -> None:
            ui = button.ui_spec
            ui["focused"] = focused
            if focused:
                button.configure(border_width=2, border_color=c("focus"))
            else:
                button.configure(border_width=ui["spec"]["border_width"],
                                 border_color=ui["border"])

        def _activate(self, button: Any) -> str:
            """Enter/Space on a focused button (DESIGN.md 9)."""
            if button.cget("state") != "disabled":
                command = button.cget("command")
                if command is not None:
                    command()
            return "break"

        def _ring_border(self, widget: Any, width: int, token: str) -> None:
            """Focus ring for controls whose border is drawn inside the canvas.

            The entry and the slider paint their border on the canvas, so a
            thicker border changes neither the widget nor the layout."""
            widget.bind("<FocusIn>",
                        lambda _event: widget.configure(border_width=2,
                                                        border_color=c("focus")))
            widget.bind("<FocusOut>",
                        lambda _event: widget.configure(border_width=width,
                                                        border_color=c(token)))

        def _ring_highlight(self, widget: Any, background: str) -> None:
            """Focus ring for controls with no border of their own.

            Tk draws it in the frame's highlight band, which is always two pixels
            thick (invisible over the surface) so that focusing never shifts the
            layout; `highlightcolor` switches it to the focus colour on its own."""
            tkinter.Frame.configure(widget, takefocus=True, highlightthickness=2,
                                    highlightbackground=background,
                                    highlightcolor=c("focus"))

        def _toggle_on_space(self, widget: Any, variable: Any,
                             command: Callable[[], Any] | None = None) -> None:
            def toggle(_event: Any) -> str:
                variable.set(0 if variable.get() else 1)
                if command is not None:
                    command()
                return "break"
            tkinter.Frame.bind(widget, "<space>", toggle)

        def _set_enabled(self, button: Any, enabled: bool) -> None:
            """Enabled/disabled state, including the 35 % disabled fill."""
            ui = button.ui_spec
            if enabled:
                button.configure(state="normal")
                self._paint(button, "normal")
                return
            self._paint(button, "normal")
            button.configure(state="disabled")
            if ui["spec"]["fill"] != "transparent":
                dimmed = theme.mix(ui["states"]["normal"], ui["parent"], 0.35)
                button.configure(fg_color=dimmed)
                if ui["spec"]["border"] is None:
                    button.configure(border_color=dimmed)

        # ── widget construction ─────────────────────────────────────────────
        def _brand_image(self, side: int) -> Any:
            """The logo mark, scaled. None if the asset is missing."""
            if not os.path.exists(LOGO_BRAND):
                return None
            try:
                from PIL import Image
                with Image.open(LOGO_BRAND) as original:
                    mark = original.convert("RGBA").copy()
                return ctk.CTkImage(light_image=mark, dark_image=mark,
                                    size=(side, side))
            except Exception as error:
                if hasattr(self, "log"):
                    self._log(f"Could not load the logo: "
                              f"{type(error).__name__}: {error}", "aviso")
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
                    # Keep a reference: if the PhotoImage is collected by the
                    # GC the window icon silently disappears (only on non-Windows).
                    self._icon_window = tkinter.PhotoImage(file=LOGO_ICONPHOTO)
                    self.iconphoto(True, self._icon_window)
                    self.icon_loaded = True
                except Exception:
                    self.icon_loaded = False

        def _build_controls(self) -> None:
            rail = ctk.CTkFrame(self, width=RAIL_WIDTH, corner_radius=0,
                                fg_color=c("bg.surface"))
            rail.grid(row=0, column=0, sticky="nsw")
            rail.grid_propagate(False)
            rail.grid_columnconfigure(0, weight=1)
            # The spacer row is what pins the action bar to the bottom of the rail.
            rail.grid_rowconfigure(11, weight=1)

            # Header: logo mark, wordmark and caption (DESIGN.md 6).
            header = ctk.CTkFrame(rail, fg_color="transparent")
            header.grid(row=0, column=0, padx=theme.SPACE["lg"],
                        pady=(theme.SPACE["lg"], theme.SPACE["md"]), sticky="ew")
            brand = self._brand_image(32)
            text_column = 1 if brand is not None else 0
            if brand is not None:
                ctk.CTkLabel(header, text="", image=brand).grid(
                    row=0, column=0, rowspan=2, padx=(0, theme.SPACE["sm"]))
            ctk.CTkLabel(header, text="FLAC VERIFIER", anchor="w",
                         font=theme.font(16, 600),
                         text_color=c("text.primary")).grid(
                row=0, column=text_column, sticky="w")
            ctk.CTkLabel(header, text="FAKE LOSSLESS DETECTION", anchor="w",
                         font=theme.font(10, 600),
                         text_color=c("text.tertiary")).grid(
                row=1, column=text_column, sticky="w")

            # SOURCE
            self._section_label(rail, "Source", row=1)
            source_row = ctk.CTkFrame(rail, fg_color="transparent")
            source_row.grid(row=2, column=0, padx=theme.SPACE["lg"], sticky="ew")
            source_row.grid_columnconfigure(0, weight=1)

            self.var_path = ctk.StringVar(value="")
            self.input = ctk.CTkEntry(
                source_row, textvariable=self.var_path, height=40,
                corner_radius=theme.RADIUS["md"], border_width=1,
                border_color=c("border.subtle"), fg_color=c("bg.raised"),
                text_color=c("text.primary"), placeholder_text="C:/Music/Album",
                placeholder_text_color=c("text.tertiary"), font=theme.font(13))
            self.input.grid(row=0, column=0, sticky="ew")
            self._ring_border(self.input, 1, "border.subtle")

            self.btn_folder = self._button(source_row, "icon", "", self._choose_folder,
                                           icon="folder", icon_size=20, width=32)
            self.btn_folder.grid(row=0, column=1, padx=(theme.SPACE["xs"], 0))
            self.folder_tooltip = _Tooltip(self.btn_folder, "Browse folder")

            self.btn_browse = self._button(rail, "secondary", "Browse folder",
                                           self._choose_folder, icon="folder")
            self.btn_browse.grid(row=3, column=0, padx=theme.SPACE["lg"],
                                 pady=(theme.SPACE["xs"], theme.SPACE["md"]),
                                 sticky="ew")

            # ANALYSIS
            self._section_label(rail, "Analysis", row=4)
            self.var_mode = ctk.StringVar(value="center")
            self.option_mode = ctk.CTkOptionMenu(
                rail, values=MODES, variable=self.var_mode,
                command=lambda _: self._change_mode(), height=40,
                corner_radius=theme.RADIUS["md"], fg_color=c("bg.raised"),
                button_color=c("bg.raised"), button_hover_color=c("bg.hover"),
                text_color=c("text.primary"),
                text_color_disabled=c("text.tertiary"),
                dropdown_fg_color=c("bg.raised"),
                dropdown_hover_color=c("bg.hover"),
                dropdown_text_color=c("text.primary"), font=theme.font(13),
                dropdown_font=theme.font(13), anchor="w")
            self.option_mode.grid(row=5, column=0, padx=theme.SPACE["lg"],
                                  sticky="ew")
            self._ring_highlight(self.option_mode, c("bg.surface"))

            # The seconds only make sense in "seconds" mode: the control is
            # removed from the grid (not disabled) so that it leaves no gap and
            # the controls below move up.
            self.seconds_row = ctk.CTkFrame(rail, fg_color="transparent")
            self.seconds_row.grid(row=6, column=0, padx=theme.SPACE["lg"],
                                  pady=(theme.SPACE["xs"], 0), sticky="ew")
            self.seconds_row.grid_columnconfigure(0, weight=1)
            self.label_seconds = ctk.CTkLabel(self.seconds_row, text="Seconds: 15",
                                              anchor="w", font=theme.font(12),
                                              text_color=c("text.secondary"))
            self.label_seconds.grid(row=0, column=0, sticky="w")
            self.seconds_slider = ctk.CTkSlider(
                self.seconds_row, from_=5, to=120, number_of_steps=23,
                command=self._change_seconds, height=16, corner_radius=8,
                button_length=14, button_corner_radius=7,
                fg_color=c("border.subtle"), progress_color=c("accent"),
                button_color=c("accent"), button_hover_color=c("accent.hover"),
                border_width=2, border_color=c("border.subtle"))
            self.seconds_slider.set(15)
            self.seconds_slider.grid(row=1, column=0, sticky="ew",
                                     pady=(theme.SPACE["2xs"], 0))
            self._ring_border(self.seconds_slider, 2, "border.subtle")

            self.var_pdf = ctk.BooleanVar(value=True)
            self.check_pdf = ctk.CTkCheckBox(
                rail, text="Generate PDF report", variable=self.var_pdf,
                checkbox_width=18, checkbox_height=18,
                corner_radius=theme.RADIUS["xs"], border_width=1,
                border_color=c("border.strong"), fg_color=c("accent"),
                hover_color=c("accent.hover"), checkmark_color=c("on.accent"),
                text_color=c("text.primary"),
                text_color_disabled=c("text.tertiary"), font=theme.font(13))
            self.check_pdf.grid(row=7, column=0, padx=theme.SPACE["lg"],
                                pady=(theme.SPACE["sm"], theme.SPACE["xs"]),
                                sticky="w")
            self._ring_highlight(self.check_pdf, c("bg.surface"))
            self._toggle_on_space(self.check_pdf, self.var_pdf)

            self.var_parallel = ctk.BooleanVar(value=True)
            self.parallel_switch = ctk.CTkSwitch(
                rail, text="Parallel processing", variable=self.var_parallel,
                command=self._change_parallel, switch_width=36,
                switch_height=20, corner_radius=theme.RADIUS["pill"],
                fg_color=c("border.strong"), progress_color=c("accent"),
                button_color=c("knob"),
                button_hover_color=c("knob"),
                text_color=c("text.primary"),
                text_color_disabled=c("text.tertiary"), font=theme.font(13))
            self.parallel_switch.grid(row=8, column=0, padx=theme.SPACE["lg"],
                                      pady=(theme.SPACE["xs"], theme.SPACE["2xs"]),
                                      sticky="w")
            self._ring_highlight(self.parallel_switch, c("bg.surface"))
            self._toggle_on_space(self.parallel_switch, self.var_parallel,
                                  self._change_parallel)

            self.option_workers = ctk.CTkOptionMenu(
                rail, values=WORKER_OPTIONS, width=150, height=40,
                corner_radius=theme.RADIUS["md"], fg_color=c("bg.raised"),
                button_color=c("bg.raised"), button_hover_color=c("bg.hover"),
                text_color=c("text.primary"),
                text_color_disabled=c("text.tertiary"),
                dropdown_fg_color=c("bg.raised"),
                dropdown_hover_color=c("bg.hover"),
                dropdown_text_color=c("text.primary"), font=theme.font(13),
                dropdown_font=theme.font(13), anchor="w")
            self.option_workers.set(WORKER_OPTIONS[0])
            self.option_workers.grid(row=9, column=0, padx=theme.SPACE["lg"],
                                     pady=(theme.SPACE["2xs"], theme.SPACE["xs"]),
                                     sticky="w")
            self._ring_highlight(self.option_workers, c("bg.surface"))

            self.var_without_cache = ctk.BooleanVar(value=False)
            self.check_cache = ctk.CTkCheckBox(
                rail, text="Ignore cache", variable=self.var_without_cache,
                checkbox_width=18, checkbox_height=18,
                corner_radius=theme.RADIUS["xs"], border_width=1,
                border_color=c("border.strong"), fg_color=c("accent"),
                hover_color=c("accent.hover"), checkmark_color=c("on.accent"),
                text_color=c("text.primary"),
                text_color_disabled=c("text.tertiary"), font=theme.font(13))
            self.check_cache.grid(row=10, column=0, padx=theme.SPACE["lg"],
                                  pady=(theme.SPACE["xs"], 0), sticky="w")
            self._ring_highlight(self.check_cache, c("bg.surface"))
            self._toggle_on_space(self.check_cache, self.var_without_cache)

            # Pinned action bar: the primary action, always visible at the
            # bottom of the rail (DESIGN.md 6).
            footer = ctk.CTkFrame(rail, fg_color="transparent")
            footer.grid(row=12, column=0, padx=theme.SPACE["lg"],
                        pady=(theme.SPACE["sm"], theme.SPACE["lg"]),
                        sticky="sew")
            footer.grid_columnconfigure(0, weight=1)

            self.btn_analyze = self._button(footer, "primary", "Analyze",
                                            self._analyze, icon="play",
                                            icon_size=20)
            self.btn_analyze.grid(row=0, column=0, sticky="ew")
            self.btn_cancel = self._button(footer, "danger", "Cancel",
                                           self._cancel, icon="stop")
            self.btn_cancel.grid(row=0, column=0, sticky="ew")
            self.btn_cancel.grid_remove()

        # ── results, hero, table and activity (DESIGN.md 6, 7.3, 7.7–7.11) ──
        def _build_telemetry(self) -> None:
            panel = ctk.CTkFrame(self, corner_radius=0, fg_color="transparent")
            panel.grid(row=0, column=1, sticky="nsew")
            panel.grid_columnconfigure(0, weight=1)
            panel.grid_rowconfigure(2, weight=1)

            # Top bar: state and count.
            top = ctk.CTkFrame(panel, fg_color="transparent")
            top.grid(row=0, column=0, sticky="ew", padx=theme.SPACE["xl"],
                     pady=(theme.SPACE["md"], theme.SPACE["sm"]))
            top.grid_columnconfigure(2, weight=1)
            self.label_state = ctk.CTkLabel(top, text="Idle", anchor="w",
                                            font=theme.font(13, 600),
                                            text_color=c("text.primary"))
            self.label_state.grid(row=0, column=0, sticky="w")
            self.label_progress = ctk.CTkLabel(top, text="", anchor="w",
                                               font=theme.mono(12),
                                               text_color=c("text.tertiary"))
            self.label_progress.grid(row=0, column=1, sticky="w",
                                     padx=(theme.SPACE["sm"], 0))

            # Theme button: the icon shows what the click will switch to. The
            # change is saved here and applied on the next start, because this
            # window builds every colour once (see theme.py).
            self.btn_theme = self._button(
                top, "icon", "", self._toggle_theme,
                icon="sun" if theme.MODE == "dark" else "moon", width=32)
            self.btn_theme.grid(row=0, column=3, sticky="e")
            self.theme_tooltip = _Tooltip(
                self.btn_theme, "Switch to light theme"
                if theme.MODE == "dark" else "Switch to dark theme")

            # Progress line: 4 px, no radius (DESIGN.md 7.9).
            self.barra = ctk.CTkProgressBar(panel, height=4, corner_radius=0,
                                            border_width=0,
                                            fg_color=c("bg.raised"),
                                            progress_color=c("accent"))
            self.barra.set(0)
            self.barra.grid(row=1, column=0, sticky="ew", padx=theme.SPACE["xl"])

            self.content = ctk.CTkFrame(panel, fg_color="transparent")
            self.content.grid(row=2, column=0, sticky="nsew")
            self.content.grid_columnconfigure(0, weight=1)
            self.content.grid_rowconfigure(0, weight=1)
            self._build_empty_state(self.content)
            self._build_results(self.content)
            self._build_activity(panel)

        def _build_empty_state(self, parent: Any) -> None:
            """Empty state: the drop zone of DESIGN.md 7.3 and 7.11."""
            self.empty_state = ctk.CTkFrame(parent, fg_color="transparent")
            self.empty_state.grid(row=0, column=0, sticky="nsew",
                                  padx=theme.SPACE["xl"], pady=theme.SPACE["xs"])
            self.empty_state.grid_columnconfigure(0, weight=1)
            self.empty_state.grid_rowconfigure(0, weight=1)
            self.empty_state.grid_rowconfigure(2, weight=1)
            self.drop_zone = _DropZone(self.empty_state, self)
            self.drop_zone.grid(row=1, column=0, sticky="ew")

        def _build_results(self, parent: Any) -> None:
            self.results = ctk.CTkFrame(parent, fg_color="transparent")
            self.results.grid(row=0, column=0, sticky="nsew")
            self.results.grid_columnconfigure(0, weight=1)
            self.results.grid_rowconfigure(1, weight=1)
            self._build_hero(self.results)
            self._build_table(self.results)

        # ── verdict hero and track detail (DESIGN.md 7.8) ───────────────────
        def _build_hero(self, parent: Any) -> None:
            """Verdict hero: the album summary, or the inspector of one track."""
            self.hero = ctk.CTkFrame(parent, fg_color=c("bg.surface"),
                                     corner_radius=theme.RADIUS["lg"],
                                     border_width=1,
                                     border_color=c("border.subtle"))
            self.hero.grid(row=0, column=0, sticky="ew", padx=theme.SPACE["xl"],
                           pady=(theme.SPACE["xs"], theme.SPACE["md"]))
            self.hero.grid_columnconfigure(1, weight=1)

            self.hero_bar = ctk.CTkFrame(self.hero, width=3, height=1,
                                         corner_radius=0, fg_color=c("unknown"))
            self.hero_bar.grid(row=0, column=0, sticky="ns",
                               padx=(theme.SPACE["md"], theme.SPACE["sm"]),
                               pady=theme.SPACE["sm"])

            self.hero_left = ctk.CTkFrame(self.hero, fg_color="transparent")
            self.hero_left.grid(row=0, column=1, sticky="nsew",
                                pady=theme.SPACE["sm"])
            self.hero_left.grid_columnconfigure(0, weight=1)

            # Eyebrow: the album summary or the file name, plus the close button.
            eyebrow_row = ctk.CTkFrame(self.hero_left, fg_color="transparent")
            eyebrow_row.grid(row=0, column=0, sticky="ew")
            eyebrow_row.grid_columnconfigure(0, weight=1)
            self.hero_eyebrow = ctk.CTkLabel(eyebrow_row, text="No results",
                                             anchor="w", height=20,
                                             font=theme.font(13),
                                             text_color=c("text.secondary"))
            self.hero_eyebrow.grid(row=0, column=0, sticky="ew")
            self.label_summary = self.hero_eyebrow
            self.btn_close_detail = self._button(eyebrow_row, "icon", "",
                                                 self._clear_selection, icon="x",
                                                 height=22, width=22)
            self.btn_close_detail.grid(row=0, column=1, padx=(theme.SPACE["xs"], 0))
            self.close_tooltip = _Tooltip(self.btn_close_detail,
                                          "Back to album summary")

            # Verdict and score share the row: the verdict is the biggest thing.
            headline_row = ctk.CTkFrame(self.hero_left, fg_color="transparent")
            headline_row.grid(row=1, column=0, sticky="ew")
            headline_row.grid_columnconfigure(0, weight=1)
            self.hero_headline = ctk.CTkLabel(headline_row, text="NO RESULTS",
                                              anchor="w", font=theme.font(28, 700),
                                              text_color=c("text.primary"))
            self.hero_headline.grid(row=0, column=0, sticky="ew")
            score_block = ctk.CTkFrame(headline_row, fg_color="transparent")
            score_block.grid(row=0, column=1, sticky="e", padx=(theme.SPACE["sm"], 0))
            self.hero_number = ctk.CTkLabel(score_block, text="—", anchor="e",
                                            height=46, font=theme.mono(40, 600),
                                            text_color=c("text.primary"))
            self.hero_number.grid(row=0, column=0, sticky="e")
            self.hero_number_caption = ctk.CTkLabel(score_block, text="SCORE",
                                                    anchor="e", height=15,
                                                    font=theme.font(11, 600),
                                                    text_color=c("text.tertiary"))
            self.hero_number_caption.grid(row=1, column=0, sticky="e")

            self.hero_facts = ctk.CTkFrame(self.hero_left, fg_color="transparent")
            self.hero_facts.grid(row=2, column=0, sticky="w", pady=(6, 0))
            self.fact_pairs = [self._fact_pair(self.hero_facts, column)
                               for column in range(4)]
            self._fact_columns = 4

            self.hero_evidence = ctk.CTkFrame(self.hero_left, fg_color="transparent")
            self.hero_evidence.grid(row=3, column=0, sticky="ew", pady=(6, 0))
            self.evidence_pairs = [self._evidence_pair(self.hero_evidence)
                                   for _ in range(4)]
            self._evidence_columns = 2
            self._evidence_values: tuple[tuple[str, str], ...] = ()

            self.hero_problems = ctk.CTkFrame(self.hero_left, fg_color="transparent")
            self.hero_problems.grid(row=4, column=0, sticky="ew", pady=(8, 0))

            # Right column: spectrogram, its action, and the report actions. It
            # stays in place in the album state (only the actions are shown), so
            # the hero does not jump when a row is selected.
            self.hero_right = ctk.CTkFrame(self.hero, fg_color="transparent")
            self.hero_right.grid(row=0, column=2, sticky="nsew",
                                 padx=(theme.SPACE["sm"], theme.SPACE["lg"]),
                                 pady=theme.SPACE["sm"])
            self.hero_right.grid_columnconfigure(0, weight=1)
            self.hero_right.grid_rowconfigure(0, weight=1)

            self.spectrogram_frame = ctk.CTkFrame(
                self.hero_right, fg_color=c("bg.raised"),
                corner_radius=theme.RADIUS["sm"], border_width=1,
                border_color=c("border.subtle"), height=104)
            self.spectrogram_frame.grid(row=0, column=0, sticky="nsew")
            self.spectrogram_frame.grid_columnconfigure(0, weight=1)
            self.spectrogram_frame.grid_rowconfigure(0, weight=1)
            self.spectrogram_label = ctk.CTkLabel(self.spectrogram_frame, text="")
            self.spectrogram_label.grid(row=0, column=0, sticky="nsew")
            self.spectrogram_placeholder = ctk.CTkFrame(
                self.spectrogram_frame, fg_color="transparent")
            self.spectrogram_placeholder.grid(row=0, column=0, sticky="nsew")
            self.spectrogram_placeholder.grid_columnconfigure(0, weight=1)
            ctk.CTkLabel(self.spectrogram_placeholder, text="",
                         image=self._icon("music", 24, c("text.tertiary"))).grid(
                row=0, column=0, pady=(theme.SPACE["sm"], theme.SPACE["2xs"]))
            self.spectrogram_note = ctk.CTkLabel(
                self.spectrogram_placeholder, text="No spectrogram",
                font=theme.font(12), text_color=c("text.secondary"))
            self.spectrogram_note.grid(row=1, column=0)
            self.spectrogram_hint = ctk.CTkLabel(
                self.spectrogram_placeholder, text="Enable the PDF report",
                font=theme.font(11), text_color=c("text.tertiary"))
            self.spectrogram_hint.grid(row=2, column=0,
                                       pady=(0, theme.SPACE["xs"]))

            self.btn_open_spectrogram = self._button(
                self.hero_right, "ghost", "Open spectrogram",
                self._open_spectrogram, icon="external-link", height=28)
            self.btn_open_spectrogram.grid(row=1, column=0, sticky="ew",
                                           pady=(theme.SPACE["xs"], 0))

            actions = ctk.CTkFrame(self.hero_right, fg_color="transparent")
            actions.grid(row=2, column=0, sticky="se", pady=(theme.SPACE["xs"], 0))
            self.btn_open_pdf = self._button(actions, "secondary", "Open report",
                                             self._open_pdf, icon="external-link",
                                             background=c("bg.surface"))
            self.btn_open_pdf.grid(row=0, column=0)
            self.btn_reveal = self._button(actions, "ghost", "Reveal", self._reveal,
                                           icon="folder",
                                           background=c("bg.surface"))
            self.btn_reveal.grid(row=0, column=1, padx=(theme.SPACE["xs"], 0))

            self.hero.bind("<Configure>", self._on_hero_configure)

        def _fact_pair(self, parent: Any, column: int) -> tuple[Any, Any]:
            """One label/value pair of the fact row (12 px, DESIGN.md 7.8)."""
            caption = ctk.CTkLabel(parent, text="", anchor="w", height=15,
                                   font=theme.font(11, 600),
                                   text_color=c("text.tertiary"))
            caption.grid(row=0, column=column, sticky="w",
                         padx=(0, theme.SPACE["lg"]))
            value = ctk.CTkLabel(parent, text="", anchor="w", height=17,
                                 font=theme.mono(12), text_color=c("text.primary"))
            value.grid(row=1, column=column, sticky="w",
                       padx=(0, theme.SPACE["lg"]))
            return caption, value

        def _evidence_pair(self, parent: Any) -> tuple[Any, Any]:
            """Inline label/value of the evidence grid (2 x 2, DESIGN.md 7.8)."""
            caption = ctk.CTkLabel(parent, text="", anchor="w", height=18,
                                   font=theme.font(11, 600),
                                   text_color=c("text.tertiary"))
            value = ctk.CTkLabel(parent, text="", anchor="w", height=18,
                                 font=theme.mono(12), text_color=c("text.primary"))
            return caption, value

        def _build_table(self, parent: Any) -> None:
            """Header and streaming rows (DESIGN.md 7.7)."""
            self.table_panel = ctk.CTkFrame(parent, fg_color=c("bg.surface"),
                                            corner_radius=theme.RADIUS["lg"],
                                            border_width=1,
                                            border_color=c("border.subtle"))
            self.table_panel.grid(row=1, column=0, sticky="nsew",
                                  padx=theme.SPACE["xl"],
                                  pady=(0, theme.SPACE["md"]))
            self.table_panel.grid_columnconfigure(0, weight=1)
            self.table_panel.grid_rowconfigure(1, weight=1)

            self.table_header = ctk.CTkFrame(self.table_panel, fg_color="transparent",
                                             height=36, corner_radius=0)
            self.table_header.grid(row=0, column=0, sticky="ew")
            self.table_header.grid_rowconfigure(0, minsize=36)
            self._configure_table_columns(self.table_header)
            for index, (name, _, _, anchor) in enumerate(TABLE_COLUMNS, start=1):
                left_pad = theme.SPACE["sm"] if name == "file" else theme.SPACE["xs"]
                right_pad = theme.SPACE["sm"] if name == "md5" else theme.SPACE["xs"]
                ctk.CTkLabel(self.table_header, text=name.upper(), anchor=anchor,
                             font=theme.font(11, 600),
                             text_color=c("text.tertiary")).grid(
                    row=0, column=index, sticky="ew", padx=(left_pad, right_pad))

            self.table_canvas = tkinter.Canvas(self.table_panel,
                                               background=c("bg.surface"),
                                               highlightthickness=0, bd=0)
            self.table_canvas.grid(row=1, column=0, sticky="nsew")
            self.table_scroll = _TableScrollbar(
                self.table_panel, command=self.table_canvas.yview,
                background=c("bg.surface"))
            self.table_scroll.grid(row=1, column=1, sticky="ns")
            self.table_canvas.configure(yscrollcommand=self.table_scroll.set)

            self.table_body = ctk.CTkFrame(self.table_canvas,
                                           fg_color=c("bg.surface"), corner_radius=0)
            self._table_window = self.table_canvas.create_window(
                (0, 0), window=self.table_body, anchor="nw")
            self.table_body.bind("<Configure>", self._on_table_body)
            self.table_canvas.bind("<Configure>", self._on_table_canvas)
            self.table_canvas.bind("<MouseWheel>", self._on_table_wheel)
            self.table_body.bind("<MouseWheel>", self._on_table_wheel)

        def _configure_table_columns(self, frame: Any) -> None:
            """Same column grid for the header and for every row, so they align."""
            frame.grid_columnconfigure(0, weight=0, minsize=2)
            for index, (_, weight, minsize, _) in enumerate(TABLE_COLUMNS, start=1):
                frame.grid_columnconfigure(index, weight=weight, minsize=minsize)

        # ── table rows ──────────────────────────────────────────────────────
        def _add_row(self, event: Event) -> dict[str, Any]:
            """One 44 px row, appended as the event arrives."""
            follow = self._table_at_bottom()
            values = result_row(event)
            mono = theme.mono(12)
            row = ctk.CTkFrame(self.table_body, fg_color=c("bg.surface"),
                               corner_radius=0)
            row.pack(fill="x")
            # The row is 44 px: 43 for the content and 1 for the separator. The
            # height comes from the grid rows, not from `pack_propagate` (which
            # only governs packed children, not gridded ones).
            row.grid_rowconfigure(0, minsize=43)
            row.grid_rowconfigure(1, minsize=1)
            self._configure_table_columns(row)

            bar = ctk.CTkFrame(row, width=2, height=1, corner_radius=0,
                               fg_color=c("bg.surface"))
            bar.grid(row=0, column=0, sticky="ns")

            plain: list[Any] = []
            # A plain Tk label with width=1: its requested width does not depend
            # on the file name, so a long name can never widen the column or
            # feed a Configure loop; sticky="ew" stretches it to the cell and the
            # ellipsis is applied on its own <Configure>.
            file_label = tkinter.Label(row, text=values["file"], anchor="w",
                                       width=1, bd=0, highlightthickness=0,
                                       background=c("bg.surface"),
                                       foreground=c("text.primary"),
                                       font=self._scaled_font(theme.font(13)))
            file_label.grid(row=0, column=1, sticky="ew",
                            padx=(theme.SPACE["sm"], theme.SPACE["xs"]))
            file_label.full_text = values["file"]
            file_label.ui_font = theme.font(13)
            file_label.tooltip = None
            file_label.fitted_width = 0
            file_label.bind("<Configure>",
                            lambda configure, label=file_label:
                            self._refit_file_name(label, configure))

            chip = self._chip(row, values["label"], values["tone"])
            chip.grid(row=0, column=2, sticky="w",
                      padx=(theme.SPACE["xs"], theme.SPACE["xs"]))

            score_text = "—" if values["score"] is None else str(values["score"])
            for column, key, text, anchor in (
                    (3, "score", score_text, "e"),
                    (4, "depth", values["depth"], "w"),
                    (5, "rate", values["rate"], "w"),
                    (6, "dr", values["dr"], "w")):
                label = ctk.CTkLabel(row, text=text, anchor=anchor, font=mono,
                                     fg_color=c("bg.surface"), corner_radius=0,
                                     text_color=c("text.secondary"))
                label.grid(row=0, column=column, sticky="ew",
                           padx=(theme.SPACE["xs"], theme.SPACE["xs"]))
                plain.append(label)

            if values["md5"] == "—":
                mark = ctk.CTkLabel(row, text="—", font=mono,
                                    fg_color=c("bg.surface"), corner_radius=0,
                                    text_color=c("text.tertiary"))
            else:
                mark = ctk.CTkLabel(row, text="", image=self._icon(
                    values["md5_icon"], 16, c(values["md5_tone"])),
                    fg_color=c("bg.surface"), corner_radius=0)
                _Tooltip(mark, f"MD5 {values['md5']}")
            mark.grid(row=0, column=7, sticky="w",
                      padx=(theme.SPACE["xs"], theme.SPACE["sm"]))
            plain.append(mark)

            separator = ctk.CTkFrame(row, height=1, corner_radius=0,
                                     fg_color=c("border.subtle"))
            separator.grid(row=1, column=0, columnspan=len(TABLE_COLUMNS) + 1,
                           sticky="ew")

            entry = {"event": event, "values": values, "frame": row, "bar": bar,
                     "cells": {"file": file_label, "verdict": chip,
                               "score": plain[0], "depth": plain[1],
                               "rate": plain[2], "dr": plain[3], "md5": mark},
                     "file_label": file_label, "plain": plain, "hover": False}
            self.table_rows.append(entry)
            self._bind_row(row, entry)
            self._paint_row(entry)
            if follow:
                self.after_idle(self._scroll_table_to_end)
            return entry

        def _bind_row(self, widget: Any, entry: dict[str, Any]) -> None:
            """Click selects; hover paints; the wheel scrolls the table."""
            widget.bind("<Button-1>", lambda _event: self._select(entry["event"]))
            widget.bind("<Enter>", lambda _event: self._hover_row(entry, True))
            widget.bind("<Leave>", lambda event: self._row_leave(entry, event))
            widget.bind("<MouseWheel>", self._on_table_wheel)
            for child in widget.winfo_children():
                self._bind_row(child, entry)

        def _hover_row(self, entry: dict[str, Any], hovering: bool) -> None:
            entry["hover"] = hovering
            self._paint_row(entry)

        def _row_leave(self, entry: dict[str, Any], event: Any) -> None:
            target = entry["frame"].winfo_containing(event.x_root, event.y_root)
            if target is not None:
                path = str(target)
                if path == str(entry["frame"]) or path.startswith(str(entry["frame"]) + "."):
                    return
            self._hover_row(entry, False)

        def _paint_row(self, entry: dict[str, Any]) -> None:
            selected = entry["event"] is self.selected_event
            if selected:
                background = theme.mix(c("accent"), c("bg.surface"), 0.08)
                bar = c("accent")
            elif entry["hover"]:
                background, bar = c("bg.hover"), c("bg.hover")
            else:
                background, bar = c("bg.surface"), c("bg.surface")
            entry["frame"].configure(fg_color=background)
            entry["bar"].configure(fg_color=bar)
            entry["file_label"].configure(background=background)
            for label in entry["plain"]:
                label.configure(fg_color=background)

        def _clear_table(self) -> None:
            for entry in self.table_rows:
                entry["frame"].destroy()
            self.table_rows.clear()
            self.table_canvas.yview_moveto(0.0)

        def _select(self, event: Event) -> None:
            if event is self.selected_event:
                self._clear_selection()
                return
            self.selected_event = event
            self._problems_expanded = False
            for entry in self.table_rows:
                self._paint_row(entry)
            self._render_hero()
            self._update_reveal()

        def _escape_detail(self, _event: Any = None) -> None:
            """Escape closes the detail inspector (DESIGN.md 9)."""
            if self.selected_event is not None:
                self._clear_selection()

        def _clear_selection(self) -> None:
            self.selected_event = None
            self._problems_expanded = False
            for entry in self.table_rows:
                self._paint_row(entry)
            self._render_hero()
            self._update_reveal()

        def _on_table_body(self, _event: Any = None) -> None:
            self.table_canvas.configure(scrollregion=self.table_canvas.bbox("all"))

        def _on_table_canvas(self, event: Any) -> None:
            self.table_canvas.itemconfigure(self._table_window, width=event.width)
            self._on_table_body()

        def _on_table_wheel(self, event: Any) -> str:
            if self.table_canvas.yview() != (0.0, 1.0):
                step = -1 if getattr(event, "delta", 0) > 0 else 1
                self.table_canvas.yview_scroll(step * 3, "units")
            return "break"

        def _table_at_bottom(self) -> bool:
            try:
                return self.table_canvas.yview()[1] >= 0.999
            except Exception:
                return True

        def _scroll_table_to_end(self) -> None:
            try:
                self.table_canvas.update_idletasks()
                self.table_canvas.yview_moveto(1.0)
            except Exception:
                pass

        # ── chips ───────────────────────────────────────────────────────────
        def _chip(self, parent: Any, text: str, tone: str) -> Any:
            """24 px chip of DESIGN.md 7.5: dot + label, never colour alone."""
            ink, background, border = CHIP_STYLES.get(tone, CHIP_STYLES["unknown"])
            chip = ctk.CTkFrame(parent, fg_color=c(background),
                                corner_radius=theme.RADIUS["sm"],
                                border_width=1, border_color=c(border))
            dot = ctk.CTkFrame(chip, width=6, height=6, corner_radius=3,
                               fg_color=c(ink))
            dot.grid(row=0, column=0, padx=(theme.SPACE["xs"], 6), pady=9)
            ctk.CTkLabel(chip, text=text, font=theme.font(11, 600),
                         text_color=c(ink)).grid(row=0, column=1,
                                                 padx=(0, theme.SPACE["xs"]))
            return chip

        # ── hero content ────────────────────────────────────────────────────
        def _render_hero(self) -> None:
            if self.selected_event is not None:
                self._render_file_hero(self.selected_event)
            else:
                self._render_album_hero()

        def _render_album_hero(self) -> None:
            counts, errors, total = count_verdicts(self.events)
            cached = sum(1 for event in self.events if event.get("desde_cache"))
            colour, text = format_summary(self.events)
            kinds = [verdict for verdict in counts if counts[verdict]]
            if total == 0:
                headline = "ANALYZING" if self.manager.active else "NO RESULTS"
                tone = "unknown"
            elif errors == total:
                headline, tone = "ERRORS", "danger"
            elif not errors and len(kinds) == 1:
                headline = kinds[0].upper()
                tone = VERDICT_TONES.get(kinds[0], "unknown")
            else:
                headline, tone = "MIXED RESULTS", SUMMARY_TONES.get(colour, "unknown")
            self._show_detail(False)
            self._set_hero_headline(text, headline, tone)
            self._set_hero_number(str(total), "FILES")
            self._set_facts((("ERRORS", str(errors)),
                             ("CACHED", str(cached)),
                             ("TYPES", str(len(kinds)))))
            self._set_evidence(())
            self._show_spectrogram("")
            self._schedule_hero_layout()

        def _render_file_hero(self, event: Event) -> None:
            values = result_row(event)
            self._show_detail(True)
            self._set_hero_headline(
                values["file"],
                values["verdict"].upper() if values["verdict"] else "ERROR",
                values["tone"])
            self._set_hero_number(
                "—" if values["score"] is None else str(values["score"]), "SCORE")
            self._set_facts((("DEPTH", values["depth"]),
                             ("RATE", values["rate"]),
                             ("DR", values["dr"]),
                             ("MD5", values["md5"])))
            self._set_evidence((("CUT-OFF", cutoff_text(event)),
                                ("LSB", lsb_text(event)),
                                ("CLIPPING", clipping_text(event)),
                                ("MD5", md5_detail(event))))
            self._render_problems(event)
            self._show_spectrogram(spectrogram_path(event))
            self._schedule_hero_layout()

        def _show_detail(self, visible: bool) -> None:
            """Shows or hides the inspector parts of the hero (7.8, phase 3)."""
            widgets = (self.btn_close_detail, self.hero_evidence,
                       self.hero_problems, self.spectrogram_frame,
                       self.btn_open_spectrogram)
            for widget in widgets:
                if visible:
                    widget.grid()
                else:
                    widget.grid_remove()

        def _set_hero_headline(self, eyebrow: str, headline: str, tone: str) -> None:
            ink = CHIP_STYLES.get(tone, CHIP_STYLES["unknown"])[0]
            self._eyebrow_full = eyebrow
            self._headline_full = headline
            self.hero_eyebrow.configure(
                text=self._fit(eyebrow, theme.font(13),
                               self._usable_width(self.hero_eyebrow),
                               from_left=self.selected_event is not None))
            self.hero_headline.configure(
                text=self._fit(headline, theme.font(28, 700),
                               self._usable_width(self.hero_headline)),
                text_color=c(ink))
            self.hero_bar.configure(fg_color=c(ink))

        def _set_hero_number(self, text: str, caption: str) -> None:
            self.hero_number.configure(text=text)
            self.hero_number_caption.configure(text=caption)

        def _set_facts(self, pairs: tuple[tuple[str, str], ...]) -> None:
            for index, (caption, value) in enumerate(self.fact_pairs):
                if index < len(pairs):
                    label, text = pairs[index]
                    caption.configure(text=label)
                    value.configure(text=text)
                    caption.grid()
                    value.grid()
                else:
                    caption.grid_remove()
                    value.grid_remove()

        def _set_evidence(self, pairs: tuple[tuple[str, str], ...]) -> None:
            """Label/value pairs of the evidence block, below the facts."""
            self._evidence_values = pairs
            for index, (caption, value) in enumerate(self.evidence_pairs):
                if index < len(pairs):
                    label, text = pairs[index]
                    caption.configure(text=label)
                    value.configure(text=text)
            self._place_evidence()

        def _place_evidence(self) -> None:
            for index, (caption, value) in enumerate(self.evidence_pairs):
                if index >= len(self._evidence_values):
                    caption.grid_remove()
                    value.grid_remove()
                    continue
                row, column = divmod(index, self._evidence_columns)
                caption.grid(row=row, column=column * 2, sticky="w", padx=(0, 6))
                value.grid(row=row, column=column * 2 + 1, sticky="w",
                           padx=(0, theme.SPACE["lg"]))

        def _reflow_evidence(self, width: int) -> None:
            """Two pairs per row, or one when the hero is narrow (window minimum)."""
            columns = 2 if width >= 430 else 1
            if columns == self._evidence_columns:
                return
            self._evidence_columns = columns
            self._place_evidence()

        def _render_problems(self, event: Event | None) -> None:
            """Issue text lines (warn/bad), wrapped in full and never truncated.

            A chip is a label, not a sentence: cutting the only message and
            hiding the rest in a tooltip leaves the user without the reason the
            track was flagged. The text wraps; more than MAX_VISIBLE_PROBLEMS
            are folded behind "Show all (N)" instead of being dropped.
            """
            for child in self.hero_problems.winfo_children():
                child.destroy()
            if event is None:
                return
            problems = [str(item) for item in (event.get("problemas") or [])]
            if event.get("error"):
                problems = [str(event["error"])]
            if not problems:
                self._chip(self.hero_problems, "No anomalies detected",
                           "ok").grid(row=0, column=0, sticky="w")
                return
            tone = ("danger" if event.get("error")
                    else "bad" if event.get("veredicto") == "PROBABLE UPSCALE"
                    else "warn")
            available = max(220, self._usable_width(self.hero_problems, 420))
            expanded = self._problems_expanded
            visible = problems if expanded else problems[:self.MAX_VISIBLE_PROBLEMS]
            for index, problem in enumerate(visible):
                self._problem_line(index, problem, tone, available)
            if len(problems) > self.MAX_VISIBLE_PROBLEMS:
                label = "Show less" if expanded else f"Show all ({len(problems)})"
                link = ctk.CTkButton(
                    self.hero_problems, text=label, height=24, width=120,
                    fg_color="transparent", hover_color=c("bg.hover"),
                    text_color=c("accent"), font=theme.font(11, 600),
                    border_width=0, anchor="w", command=self._toggle_problems)
                link.grid(row=len(visible), column=0, sticky="w",
                          padx=(14, 0), pady=(2, 0))

        def _problem_line(self, index: int, text: str, tone: str,
                          available: int) -> None:
            """One wrapped issue line: a severity dot and the full message."""
            row = ctk.CTkFrame(self.hero_problems, fg_color="transparent")
            row.grid(row=index, column=0, sticky="ew", pady=(0, 4))
            row.grid_columnconfigure(1, weight=1)
            dot = ctk.CTkFrame(row, width=6, height=6, corner_radius=3,
                               fg_color=c(tone))
            dot.grid(row=0, column=0, padx=(0, 8), pady=(7, 0), sticky="nw")
            ctk.CTkLabel(row, text=text, font=theme.font(11),
                         text_color=c("text.secondary"),
                         wraplength=max(160, available - 24),
                         justify="left", anchor="w").grid(
                row=0, column=1, sticky="ew")

        def _toggle_problems(self) -> None:
            self._problems_expanded = not self._problems_expanded
            if self.selected_event is not None:
                self._render_problems(self.selected_event)
                self._schedule_hero_layout()

        def _show_spectrogram(self, path: str) -> None:
            """Image, or a clean note when the analysis produced no PNG (7.8)."""
            self._spectrogram_path_value = path
            image = self._spectrogram_image(path)
            if image is not None:
                self.spectrogram_label.configure(image=image, text="")
                self.spectrogram_label.grid()
                self.spectrogram_placeholder.grid_remove()
            else:
                self.spectrogram_label.grid_remove()
                self.spectrogram_placeholder.grid()
                self.spectrogram_note.configure(
                    text="No spectrogram" if not path else "Image not available")
                self.spectrogram_hint.configure(
                    text="Enable the PDF report" if not path else "The file was moved")
            self._set_enabled(self.btn_open_spectrogram,
                              bool(path and os.path.exists(path)))

        def _spectrogram_image(self, path: str) -> Any:
            """Thumbnail of the track spectrogram, cached by path."""
            if not path or not os.path.exists(path):
                return None
            if path in self._spectrograms:
                return self._spectrograms[path]
            image = None
            try:
                from PIL import Image
                with Image.open(path) as source:
                    picture = source.convert("RGB")
                picture.thumbnail((self.SPECTROGRAM_WIDTH * 2,
                                   self.SPECTROGRAM_HEIGHT * 2))
                width, height = picture.size
                scale = min(self.SPECTROGRAM_WIDTH / width,
                            self.SPECTROGRAM_HEIGHT / height)
                display = (max(1, round(width * scale)),
                           max(1, round(height * scale)))
                image = ctk.CTkImage(light_image=picture, dark_image=picture,
                                     size=display)
            except Exception:
                image = None
            self._spectrograms[path] = image
            return image

        def _open_spectrogram(self) -> None:
            path = self._spectrogram_path_value
            if not path or not os.path.exists(path):
                self._alert("No spectrogram",
                            "This track has no spectrogram image.")
                return
            if not open_file(path):
                self._alert("Could not open the spectrogram",
                            f"Open it manually:\n{path}", error=True)

        def _reflow_facts(self, width: int) -> None:
            """Four columns, or two when the hero is narrow (window minimum)."""
            columns = 4 if width >= 430 else 2
            if columns == self._fact_columns:
                return
            self._fact_columns = columns
            for index, (caption, value) in enumerate(self.fact_pairs):
                row, column = divmod(index, columns)
                caption.grid_configure(row=row * 2, column=column)
                value.grid_configure(row=row * 2 + 1, column=column)

        def _usable_width(self, widget: Any, fallback: int = 320) -> int:
            """Measured width, with a fallback while the window is not mapped yet."""
            width = widget.winfo_width()
            return width if width > 1 else fallback

        def _on_hero_configure(self, event: Any) -> None:
            """Refits the hero when the hero itself changes width.

            Listening to the hero and not to the labels is what keeps this from
            feeding back: truncating a label changes the label, not the width of
            the hero, so no second Configure can bounce between two texts. The
            work is deferred to idle because the inner columns are sized after
            the hero itself.
            """
            if event.width <= 40:
                return
            if abs(event.width - self._hero_width) < 2:
                return
            self._hero_width = event.width
            self._schedule_hero_layout()

        def _schedule_hero_layout(self) -> None:
            if self._hero_layout_scheduled:
                return
            self._hero_layout_scheduled = True
            self.after_idle(self._apply_hero_layout)

        def _apply_hero_layout(self) -> None:
            self._hero_layout_scheduled = False
            if self._rebuilding:
                return
            self._refit_hero_texts()
            width = self._usable_width(self.hero_left, 520) - 8
            self._reflow_facts(width)
            self._reflow_evidence(width)
            if self.selected_event is not None:
                self._render_problems(self.selected_event)

        def _refit_hero_texts(self) -> None:
            self.hero_eyebrow.configure(
                text=self._fit(getattr(self, "_eyebrow_full", ""),
                               theme.font(13), self._usable_width(self.hero_eyebrow),
                               from_left=self.selected_event is not None))
            self.hero_headline.configure(
                text=self._fit(getattr(self, "_headline_full", ""),
                               theme.font(28, 700),
                               self._usable_width(self.hero_headline)))

        # ── activity log (DESIGN.md 7.10) ───────────────────────────────────
        def _build_activity(self, parent: Any) -> None:
            self.activity = ctk.CTkFrame(parent, fg_color=c("bg.surface"),
                                         corner_radius=theme.RADIUS["lg"],
                                         border_width=1,
                                         border_color=c("border.subtle"))
            self.activity.grid(row=3, column=0, sticky="ew",
                               padx=theme.SPACE["xl"], pady=(0, theme.SPACE["lg"]))
            self.activity.grid_columnconfigure(0, weight=1)

            # Collapsed bar: 28 px with the last line and the chevron.
            self.activity_bar = ctk.CTkFrame(self.activity, fg_color="transparent",
                                             height=28, corner_radius=0,
                                             cursor="hand2")
            self.activity_bar.grid(row=0, column=0, sticky="ew")
            self.activity_bar.grid_propagate(False)
            self.activity_bar.bind("<Configure>", self._refit_activity_preview)
            self.activity_bar.grid_columnconfigure(1, weight=1)
            self.activity_title = ctk.CTkLabel(self.activity_bar, text="ACTIVITY",
                                               font=theme.font(11, 600),
                                               text_color=c("text.tertiary"))
            self.activity_title.grid(row=0, column=0, padx=(theme.SPACE["md"],
                                                           theme.SPACE["sm"]))
            self.label_activity_preview = ctk.CTkLabel(
                self.activity_bar, text="No activity yet", anchor="w",
                font=theme.font(12), text_color=c("text.secondary"))
            self.label_activity_preview.grid(row=0, column=1, sticky="ew")
            self.btn_activity = self._button(self.activity_bar, "icon", "",
                                             self._toggle_activity,
                                             icon="chevron-right", height=24,
                                             width=24)
            self.btn_activity.grid(row=0, column=2, padx=(theme.SPACE["xs"],
                                                          theme.SPACE["sm"]),
                                   pady=2)
            self.activity_tooltip = _Tooltip(self.btn_activity, "Expand activity")
            for widget in (self.activity_bar, self.activity_title,
                           self.label_activity_preview):
                widget.bind("<Button-1>", lambda _event: self._toggle_activity())

            # Expanded panel: toolbar (auto-scroll, clear) plus the log itself.
            self.activity_panel = ctk.CTkFrame(self.activity,
                                               fg_color="transparent")
            self.activity_panel.grid(row=1, column=0, sticky="nsew")
            self.activity_panel.grid_columnconfigure(0, weight=1)
            toolbar = ctk.CTkFrame(self.activity_panel, fg_color="transparent",
                                   height=36)
            toolbar.grid(row=0, column=0, sticky="ew")
            toolbar.grid_columnconfigure(1, weight=1)
            ctk.CTkLabel(toolbar, text="ACTIVITY", font=theme.font(11, 600),
                         text_color=c("text.tertiary")).grid(
                row=0, column=0, padx=(theme.SPACE["md"], theme.SPACE["sm"]))
            self.var_autoscroll = ctk.BooleanVar(value=True)
            self.switch_autoscroll = ctk.CTkSwitch(
                toolbar, text="Auto-scroll", variable=self.var_autoscroll,
                switch_width=36, switch_height=20,
                corner_radius=theme.RADIUS["pill"],
                fg_color=c("border.strong"), progress_color=c("accent"),
                button_color=c("knob"),
                button_hover_color=c("knob"),
                text_color=c("text.secondary"), font=theme.font(12))
            self.switch_autoscroll.grid(row=0, column=1, sticky="w")
            self._ring_highlight(self.switch_autoscroll, c("bg.surface"))
            self._toggle_on_space(self.switch_autoscroll, self.var_autoscroll)
            self.btn_clear_log = self._button(toolbar, "ghost", "Clear",
                                              self._clear_log, icon="trash",
                                              height=28,
                                              background=c("bg.surface"))
            self.btn_clear_log.grid(row=0, column=2, padx=(theme.SPACE["xs"], 0))
            self.btn_activity_close = self._button(
                toolbar, "icon", "", self._toggle_activity,
                icon="chevron-down", height=24, width=24,
                background=c("bg.surface"))
            self.btn_activity_close.grid(row=0, column=3,
                                         padx=(theme.SPACE["xs"],
                                               theme.SPACE["sm"]))

            self.log = ctk.CTkTextbox(
                self.activity_panel, wrap="word", font=theme.mono(12), height=140,
                corner_radius=theme.RADIUS["md"], border_width=1,
                border_color=c("border.subtle"), fg_color=c("bg.canvas"),
                text_color=c("text.secondary"),
                scrollbar_button_color=c("border.subtle"),
                scrollbar_button_hover_color=c("border.strong"))
            self.log.grid(row=1, column=0, sticky="nsew",
                          padx=theme.SPACE["sm"], pady=(0, theme.SPACE["sm"]))
            self.log.configure(state="disabled")
            # Tag colours are configured ONCE here; _log only reuses them.
            self.log.tag_config("time", foreground=c("text.tertiary"))
            for level, token in LOG_TAGS.items():
                self.log.tag_config(level, foreground=c(token))
            self._set_activity(False)

        def _set_activity(self, expanded: bool) -> None:
            self.activity_expanded = bool(expanded)
            if expanded:
                self.activity_bar.grid_remove()
                self.activity_panel.grid()
                self.activity_tooltip.text = "Collapse activity"
            else:
                self.activity_panel.grid_remove()
                self.activity_bar.grid()
                self.activity_tooltip.text = "Expand activity"

        def _toggle_activity(self) -> None:
            self._set_activity(not self.activity_expanded)

        def _clear_log(self) -> None:
            self.log.configure(state="normal")
            self.log.delete("1.0", "end")
            self.log.configure(state="disabled")
            self._last_log_line = ""
            self._log_history.clear()
            self.label_activity_preview.configure(text="No activity yet")

        def _refresh_activity_preview(self) -> None:
            text = self._last_log_line or "No activity yet"
            self.label_activity_preview.configure(
                text=self._fit(text, theme.font(12),
                               self._usable_width(self.label_activity_preview, 300)))

        def _refit_activity_preview(self, event: Any) -> None:
            if event.width <= 20 or abs(event.width - self._preview_width) < 2:
                return
            self._preview_width = event.width
            self._refresh_activity_preview()

        def _scroll_log(self) -> None:
            if self.var_autoscroll.get():
                try:
                    self.log.see("end")
                except Exception:
                    pass

        # ── text fitting ────────────────────────────────────────────────────
        def _measure_font(self, font: Any) -> Any:
            scaling = ctk.ScalingTracker.get_widget_scaling(self)
            key = (font.cget("family"), int(font.cget("size")),
                   str(font.cget("weight")), scaling)
            measure = self._measures.get(key)
            if measure is None:
                family, size, style = font.create_scaled_tuple(scaling)
                measure = tkinter.font.Font(root=self, family=family, size=size,
                                            weight=("bold" if "bold" in style
                                                    else "normal"))
                self._measures[key] = measure
            return measure

        def _text_width(self, font: Any, text: str) -> int:
            """Width of `text` in on-screen pixels at the current scaling."""
            return self._measure_font(font).measure(text)

        def _scaled_font(self, font: Any) -> tuple[Any, ...]:
            """Tk font tuple at the current scaling, for plain Tk widgets."""
            return font.create_scaled_tuple(ctk.ScalingTracker.get_widget_scaling(self))

        def _fit(self, text: str, font: Any, pixels: int,
                 from_left: bool = False) -> str:
            """Truncates with an ellipsis so a row or the hero never overflows."""
            if not text or pixels <= 0:
                return text
            measure = self._measure_font(font)
            if measure.measure(text) <= pixels:
                return text
            ellipsis = "…"
            if measure.measure(ellipsis) > pixels:
                return ""
            if from_left:
                start = 0
                while start < len(text) and measure.measure(ellipsis + text[start:]) > pixels:
                    start += 1
                return ellipsis + text[start:]
            end = len(text)
            while end > 0 and measure.measure(text[:end] + ellipsis) > pixels:
                end -= 1
            return text[:end] + ellipsis

        def _refit_file_name(self, label: Any, event: Any) -> None:
            """Truncates with an ellipsis inside the file cell of one row."""
            if event.width <= 8:            # not mapped yet: keep the whole name
                return
            if abs(event.width - label.fitted_width) < 2:
                return
            label.fitted_width = event.width
            shown = self._fit(label.full_text, label.ui_font, event.width - 4)
            label.configure(text=shown)
            if shown != label.full_text and label.tooltip is None:
                label.tooltip = _Tooltip(label, label.full_text)

        # ── hero actions ────────────────────────────────────────────────────
        def _reveal_target(self) -> str:
            if self.selected_event is not None:
                path = str(self.selected_event.get("ruta") or "")
                if path:
                    return path
            return self.var_path.get().strip().strip('"')

        def _update_reveal(self) -> None:
            target = self._reveal_target()
            self._set_enabled(self.btn_reveal, bool(target and os.path.exists(target)))

        def _reveal(self) -> None:
            target = self._reveal_target()
            if not target or not os.path.exists(target):
                self._alert("Nothing to reveal",
                            "Analyze an album or select a file first.")
                return
            folder = target if os.path.isdir(target) else os.path.dirname(target)
            if not open_file(folder):
                self._alert("Could not open the folder",
                            f"Open it manually:\n{folder}", error=True)

        def _set_results_visible(self, visible: bool) -> None:
            if visible:
                self.empty_state.grid_remove()
                self.results.grid()
            else:
                self.results.grid_remove()
                self.empty_state.grid()

        # ── view helpers ────────────────────────────────────────────────────
        def _log(self, text: str, label: str = "info",
                 scroll: bool = True) -> None:
            """One log line: timestamp in tertiary, text in the level colour."""
            stamp = time.strftime("%H:%M:%S")
            self.log.configure(state="normal")
            self.log.insert("end", f"{stamp}  ", "time")
            self.log.insert("end", f"{text}\n", label)
            self._trim_log()
            self.log.configure(state="disabled")
            self._log_history.append((stamp, text, label))
            self._last_log_line = text
            self._refresh_activity_preview()
            if scroll:
                self._scroll_log()

        def _trim_log(self) -> None:
            """Keeps the text box bounded: drops the oldest lines past the limit."""
            try:
                total = int(self.log.index("end-1c").split(".")[0])
                if total > MAX_LINES_LOG:
                    self.log.delete("1.0", f"{total - MAX_LINES_LOG + 1}.0")
            except Exception:
                pass

        def _register_dnd(self) -> bool:
            """The drop zone and the path entry both accept a dropped folder."""
            zone = (self.drop_zone.register_drop()
                    if hasattr(self, "drop_zone") else False)
            if not DND_OK:
                return zone
            try:
                self.input.drop_target_register(DND_FILES)
                self.input.dnd_bind("<<Drop>>", self._drop)
                return True
            except Exception:
                return zone

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
                self.seconds_row.grid_configure(row=6, column=0,
                                                padx=theme.SPACE["lg"],
                                                pady=(theme.SPACE["xs"], 0),
                                                sticky="ew")
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
            """Paints the idle/running state: Analyze becomes Cancel."""
            active = self.manager.active
            self._set_enabled(self.btn_analyze, not active)
            self._set_enabled(self.btn_cancel, active)
            self._show_action(self.btn_cancel if active else self.btn_analyze)
            for widget in (self.input, self.btn_browse, self.option_mode,
                           self.check_pdf, self.parallel_switch, self.check_cache,
                           self.seconds_slider):
                widget.configure(state="disabled" if active else "normal")
            self._set_enabled(self.btn_folder, not active)
            self._set_enabled(self.btn_theme, not active)
            if not active:
                self._change_mode()
                self._change_parallel()
            there_is_pdf = bool(self.pdf_path and os.path.exists(self.pdf_path))
            self._set_enabled(self.btn_open_pdf, there_is_pdf)
            # Empty state until there is something to show (DESIGN.md 7.11).
            self._set_results_visible(active or bool(self.events))
            self._update_reveal()

        def _toggle_theme(self) -> None:
            """Switches the whole window between dark and light, live.

            Mid-run the button is disabled and this method refuses to build a
            second time: the running analysis owns the widgets it is writing to.
            """
            if self.manager.active or self._rebuilding:
                return
            target = "light" if theme.MODE == "dark" else "dark"
            saved = theme.save_preference(target)
            self._rebuild_ui(target)
            if not saved:
                self._log("Could not save the theme preference; "
                          "it will not be remembered.", "aviso")

        def _rebuild_ui(self, mode: str) -> None:
            """Rebuilds every widget in `mode` without recreating the Tk root.

            The interface resolves its colours once, so switching live means
            building it again. Everything the user can see is saved first and put
            back afterwards: the source and analysis options, the results, the
            selected track, the progress, the state labels, the log (text and
            level) and whether the activity panel is open.
            """
            state = {
                "path": self.var_path.get(),
                "mode": self.var_mode.get(),
                "seconds": self.seconds_slider.get(),
                "pdf": bool(self.var_pdf.get()),
                "parallel": bool(self.var_parallel.get()),
                "workers": self.option_workers.get(),
                "without_cache": bool(self.var_without_cache.get()),
                "events": list(self.events),
                "selected": self.selected_event,
                "total": self.total,
                "received": self.recibidos,
                "pdf_path": self.pdf_path,
                "activity": self.activity_expanded,
                "problems_expanded": self._problems_expanded,
                "log": list(self._log_history),
                "barra": self.barra.get(),
                "state_text": self.label_state.cget("text"),
                "progress_text": self.label_progress.cget("text"),
            }
            self._rebuilding = True
            try:
                for child in list(self.winfo_children()):
                    child.destroy()
                self._icons.clear()
                self._measures.clear()
                self.table_rows.clear()
                self._hero_width = 0
                self._preview_width = 0
                self._hero_layout_scheduled = False

                theme.MODE = mode
                ctk.set_appearance_mode(mode)
                self.configure(fg_color=c("bg.canvas"))

                self._build_controls()
                self._build_telemetry()

                self.var_path.set(state["path"])
                self.var_mode.set(state["mode"])
                self.option_workers.set(state["workers"])
                self.var_pdf.set(state["pdf"])
                self.var_parallel.set(state["parallel"])
                self.var_without_cache.set(state["without_cache"])
                self.seconds_slider.set(state["seconds"])
                self._change_seconds(state["seconds"])
                self._change_mode()
                self._change_parallel()

                self.events = deque(state["events"], maxlen=MAX_EVENTS_IN_MEMORY)
                self.total = state["total"]
                self.recibidos = state["received"]
                self.pdf_path = state["pdf_path"]
                self.selected_event = state["selected"]
                self._problems_expanded = state["problems_expanded"]
                for event in state["events"]:
                    self._add_row(event)
                for entry in self.table_rows:
                    self._paint_row(entry)

                self._render_hero()
                self._update_state()
                self.barra.set(state["barra"])
                self.label_state.configure(text=state["state_text"])
                self.label_progress.configure(text=state["progress_text"])
                self._restore_log(state["log"])
                self._set_activity(state["activity"])
                self._register_dnd()
                self._schedule_hero_layout()
            finally:
                self._rebuilding = False

        def _restore_log(self, history: list[tuple[str, str, str]]) -> None:
            """Replays the saved log lines, keeping their timestamp and level."""
            self.log.configure(state="normal")
            for stamp, text, label in history:
                self.log.insert("end", f"{stamp}  ", "time")
                self.log.insert("end", f"{text}\n", label)
            self.log.configure(state="disabled")
            if history:
                self._last_log_line = history[-1][1]
            self._refresh_activity_preview()
            self._scroll_log()

        def _show_action(self, button: Any) -> None:
            """Only one primary action is visible: Analyze or Cancel."""
            for candidate in (self.btn_analyze, self.btn_cancel):
                if candidate is button:
                    candidate.grid()
                else:
                    candidate.grid_remove()

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
            self.selected_event = None
            self._clear_table()
            self._render_hero()
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
            self._render_hero()
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

            A theme rebuild destroys and recreates every widget, so a queued
            event must not touch a half-built window.

            It processes a BOUNDED batch per tick: a huge backlog can no longer
            freeze the window, because Tk regains control between batches and the
            Cancel button stays responsive. It is rescheduled while the engine is
            alive, while the reader thread is still alive (it may not have queued
            the final `__fin__` yet) or while a full batch was just processed."""
            if self._rebuilding:
                return
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
            self.label_progress.configure(text=f"{self.recibidos} / {self.total}")

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
                self.label_progress.configure(text=f"0 / {self.total}")
            elif kind == "resultado":
                self.events.append(event)
                self.recibidos += 1
                self._set_results_visible(True)
                label = "ok"
                if event.get("error"):
                    label = "error"
                elif event.get("veredicto") in ("SUSPICIOUS", "PROBABLE UPSCALE"):
                    label = "aviso"
                self._log(format_result(event), label, scroll=False)
                for evidence_item in event.get("evidencias_forenses") or []:
                    self._log(f"    evidence: {evidence_item}", "info", scroll=False)
                # A row per file, as the event arrives (DESIGN.md 7.7).
                self._add_row(event)
                # One layout/scroll per result block, not once per evidence line.
                self._scroll_log()
                if self.selected_event is None:
                    self._render_hero()
                    self._update_reveal()
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
            self._render_hero()
            self._set_activity(False)
            self._update_state()
            self._log("Activity collapsed; open it to review the batch.", "info")

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
        print("customtkinter is not installed, so there is no graphical interface.")
        print("   Install it with:  pip install customtkinter")
        print("   Meanwhile you can use the CLI:  python verificar_flac.py")
        return 1
    try:
        # The preference is read here, before the window exists: every colour is
        # resolved once while the widgets are built.
        theme.load_preference()
        ctk.set_appearance_mode("light" if theme.MODE == "light" else "dark")
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
