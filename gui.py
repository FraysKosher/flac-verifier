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
import math
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time
import tkinter
from collections import OrderedDict, deque
from enum import Enum
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


class RunState(str, Enum):
    """Explicit lifecycle states for a batch execution in the GUI."""

    IDLE = "idle"
    RUNNING = "running"
    FINISHED = "finished"
    CANCELLED = "cancelled"
    ERROR = "error"


class SurfaceMode(str, Enum):
    """Workspace surface mode: onboarding placeholder vs live batch table."""

    EMPTY = "empty"
    RESULTS = "results"


class HeroMode(str, Enum):
    """Hero card mode: album summary overview vs single-track inspector."""

    ALBUM = "album"
    INSPECTOR = "inspector"


class SpectrogramState(str, Enum):
    """Visual state of the spectrogram preview and Open button."""

    NONE = "none"
    LOADING = "loading"
    MISSING = "missing"
    READY = "ready"


class VerdictFilter(str, Enum):
    """Quick verdict filter modes for the live results table."""

    ALL = "all"
    FLAGGED = "flagged"
    SUSPICIOUS = "suspicious"
    ERRORS = "errors"


LOG_LEVEL_TAGS: dict[str, str] = {
    "info": "info",
    "ok": "ok",
    "warn": "warn",
    "aviso": "warn",
    "error": "error",
}

RUN_STATE_TRANSITIONS: dict[RunState, frozenset[RunState]] = {
    RunState.IDLE: frozenset((RunState.RUNNING,)),
    RunState.RUNNING: frozenset((RunState.FINISHED, RunState.CANCELLED, RunState.ERROR)),
    RunState.FINISHED: frozenset((RunState.RUNNING, RunState.IDLE)),
    RunState.CANCELLED: frozenset((RunState.RUNNING, RunState.IDLE)),
    RunState.ERROR: frozenset((RunState.RUNNING, RunState.IDLE)),
}


def _coerce_run_state(value: RunState | str) -> RunState:
    if isinstance(value, RunState):
        return value
    return RunState(value)


def transition_run_state(current: RunState | str, target: RunState | str) -> RunState:
    """Validates and executes a run_state transition.

    While a batch is `running`, only terminal batch events (`finished`,
    `cancelled`, `error`) can leave `running`, preventing intermediate
    `informe` or `aviso` events from prematurely unlocking controls.
    """
    current_state = _coerce_run_state(current)
    target_state = _coerce_run_state(target)
    allowed = RUN_STATE_TRANSITIONS.get(
        current_state, frozenset((RunState.RUNNING, RunState.IDLE)))
    if target_state == current_state or target_state in allowed:
        return target_state
    return current_state


def event_logical_key(event: Event | None) -> tuple[Any, str] | None:
    """Logical identity of a track event across out-of-order insertions."""
    if not isinstance(event, dict):
        return None
    original_index = event.get("indice_original")
    path = str(event.get("ruta") or "")
    if not isinstance(original_index, int) and not path:
        return None
    return (original_index, path or str(event.get("archivo") or ""))


def same_logical_event(first: Event | None, second: Event | None) -> bool:
    """True when two result events refer to the same logical track."""
    if first is None or second is None:
        return False
    if first is second:
        return True
    key_a = event_logical_key(first)
    key_b = event_logical_key(second)
    return key_a is not None and key_a == key_b

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
WORKER_OPTIONS = ["Automatic", "1", "2", "4", "8"]

# ─── UI throughput and memory limits ─────────────────────────────────────────
BATCH_DRAINED = 200             # hard cap of engine events per UI tick
MAX_RESULTS_BY_TICK = 24   # redraw the progress bar once per batch
ACTIVE_INTERVAL_MS = 30       # reschedule fast while events are pending
IDLE_INTERVAL_MS = 100      # gentle polling while the engine works quietly
MAX_EVENTS_IN_MEMORY = 50000 # result events kept on screen / for the final summary
MAX_LINES_LOG = 3000          # log lines kept in the text box
MAX_QUEUE = 5000                # bounded queue: backpressure instead of unbounded growth
# Log level -> design token used to paint the line (DESIGN.md 7.10).
LOG_TAGS = {"info": "text.secondary", "ok": "ok", "warn": "warn", "error": "bad"}


# ─── PRESENTATION CONSTANTS ────────────────────────────────────────────

RAIL_WIDTH = 340
WINDOW_GEOMETRY = "1180x760"
WINDOW_MINIMUM = (1024, 680)


class _TokenColor(str):
    """A colour string that remembers the design token that produced it.

    CustomTkinter stores the same string object it is given, so after building a
    widget it is possible to ask it which token paints a property and replace
    that colour in place when the theme changes, without a registry of every
    widget and every property.
    """

    token = ""


def c(token: str) -> str:
    """Colour of a design token, so the widget code reads as the design brief."""
    value = _TokenColor(theme.color(token))
    value.token = token
    return value


# Colour properties of CustomTkinter widgets. When one of them holds a
# `_TokenColor`, the retheme can resolve it again for the new mode.
COLOUR_PROPERTIES = (
    "fg_color", "hover_color", "border_color", "text_color",
    "text_color_disabled", "button_color", "button_hover_color",
    "progress_color", "checkmark_color", "dropdown_fg_color",
    "dropdown_hover_color", "dropdown_text_color", "scrollbar_button_color",
    "scrollbar_button_hover_color", "placeholder_text_color",
)


def freeze_redraw(window: Any) -> list[int]:
    """Stops the window from painting while the colours change (Windows only).

    Returns the top-level handle that was frozen, or empty when unmapped, off
    Windows, or on failure."""
    if not sys.platform.startswith("win"):
        return []
    try:
        if not window.winfo_ismapped():
            return []
        import ctypes
        user32 = ctypes.windll.user32
        hwnd = int(window.winfo_id())
        if not hwnd:
            return []
        send = user32.SendMessageW
        send.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p,
                         ctypes.c_void_p]
        send(ctypes.c_void_p(hwnd), 0x000B, None, None)          # WM_SETREDRAW off
        return [hwnd]
    except Exception:
        return []


def thaw_redraw(window: Any, handles: list[int]) -> None:
    """Lets the frozen window paint again in one pass (Windows only)."""
    if not handles:
        return
    try:
        import ctypes
        user32 = ctypes.windll.user32
        send = user32.SendMessageW
        send.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p,
                         ctypes.c_void_p]
        for hwnd in handles:
            send(ctypes.c_void_p(hwnd), 0x000B, ctypes.c_void_p(1), None)
        redraw = user32.RedrawWindow
        redraw.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                           ctypes.c_uint]
        # RDW_ERASE | RDW_INVALIDATE | RDW_ALLCHILDREN | RDW_UPDATENOW
        redraw(ctypes.c_void_p(handles[0]), None, None,
               0x0004 | 0x0001 | 0x0080 | 0x0100)
    except Exception:
        pass


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

IGNORED_SCAN_DIRS = frozenset((
    "_spectrograms",
    "__pycache__",
    "system volume information",
    "@eadir",
    "#recycle",
))


def natural_sort_key(path_or_name: str) -> tuple[Any, ...]:
    """Natural numeric sort key for folders and tracks."""
    name = os.path.basename(str(path_or_name))
    parts = [
        (0, int(token), "") if token.isdigit() else (1, 0, token.casefold())
        for token in re.split(r"(\d+)", name)
        if token
    ]
    return (parts, name.casefold(), name)


def discover_album_queue(target_path: str) -> list[str]:
    """Builds an ordered album-by-album queue from a file or folder tree."""
    clean = str(target_path or "").strip().strip('"')
    if not clean or not os.path.exists(clean):
        return []
    if os.path.isfile(clean):
        return [os.path.abspath(clean)] if clean.lower().endswith(".flac") else []
    if not os.path.isdir(clean):
        return []
    batch_queue: list[str] = []
    root_abs = os.path.abspath(clean)
    for current_root, dirnames, filenames in os.walk(
        root_abs, topdown=True, followlinks=False
    ):
        dirnames[:] = sorted(
            (
                name for name in dirnames
                if not name.startswith((".", "$"))
                and name.lower() not in IGNORED_SCAN_DIRS
            ),
            key=natural_sort_key,
        )
        if any(name.lower().endswith(".flac") for name in filenames):
            batch_queue.append(current_root)
    return batch_queue


def relative_album_context(track_path: str, root_path: str) -> str:
    """Compact album folder context for multi-album rows and track inspector."""
    abs_track = os.path.abspath(track_path)
    album_dir = os.path.dirname(abs_track)
    abs_root = os.path.abspath(root_path) if root_path else album_dir
    if os.path.isfile(abs_root):
        abs_root = os.path.dirname(abs_root)
    try:
        rel = os.path.relpath(album_dir, abs_root).replace("\\", "/")
    except ValueError:
        rel = ""
    if not rel or rel == ".":
        return os.path.basename(os.path.normpath(album_dir)) or album_dir
    return rel


def matches_verdict_filter(
    event: Event, active_filter: VerdictFilter | str
) -> bool:
    """True when a `resultado` event belongs to the active table filter."""
    mode = (
        active_filter
        if isinstance(active_filter, VerdictFilter)
        else VerdictFilter(active_filter)
    )
    if mode is VerdictFilter.ALL:
        return True
    md5 = event.get("md5") if isinstance(event.get("md5"), dict) else {}
    failed = (
        bool(event.get("error"))
        or not event.get("veredicto")
        or md5.get("estado") == "mismatch"
    )
    if mode is VerdictFilter.ERRORS:
        return failed
    if failed:
        return False
    verdict = str(event.get("veredicto") or "")
    if mode is VerdictFilter.FLAGGED:
        return verdict == "PROBABLE UPSCALE"
    if mode is VerdictFilter.SUSPICIOUS:
        return verdict == "SUSPICIOUS"
    return True


def count_filter_buckets(events: Iterable[Event]) -> dict[str, int]:
    """Counts `resultado` events across all quick-filter buckets in one pass."""
    counts = {
        VerdictFilter.ALL.value: 0,
        VerdictFilter.FLAGGED.value: 0,
        VerdictFilter.SUSPICIOUS.value: 0,
        VerdictFilter.ERRORS.value: 0,
    }
    for event in events:
        if event.get("tipo") != "resultado":
            continue
        counts[VerdictFilter.ALL.value] += 1
        md5 = event.get("md5") if isinstance(event.get("md5"), dict) else {}
        failed = (
            bool(event.get("error"))
            or not event.get("veredicto")
            or md5.get("estado") == "mismatch"
        )
        if failed:
            counts[VerdictFilter.ERRORS.value] += 1
            continue
        verdict = str(event.get("veredicto") or "")
        if verdict == "PROBABLE UPSCALE":
            counts[VerdictFilter.FLAGGED.value] += 1
        elif verdict == "SUSPICIOUS":
            counts[VerdictFilter.SUSPICIOUS.value] += 1
    return counts


def build_command(path: str, mode: str = "full", seconds: int | None = None,
                      pdf: bool = True, workers: int = 0, without_cache: bool = False,
                      engine: str | None = None, png: bool = False,
                      output_dir: str | None = None) -> list[str]:
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
    elif png:
        command.append("--png")
    if output_dir is not None and str(output_dir).strip():
        command += ["--output-dir", str(output_dir).strip()]
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
    else:
        for pg_sig in (getattr(signal, "SIGTERM", None),
                       getattr(signal, "SIGKILL", None)):
            if pg_sig is None:
                continue
            try:
                pgid = os.getpgid(process.pid)
                os.killpg(pgid, pg_sig)
                process.wait(timeout=wait_time)
                return
            except Exception:
                continue
    for accion in (process.terminate, process.kill):
        try:
            accion()
            process.wait(timeout=wait_time)
            return
        except Exception:
            continue


def output_reader(process: "subprocess.Popen[str]", message_queue: "queue.Queue[Message]",
                  cancelled: Callable[[], bool] | None = None,
                  run_id: int | None = None) -> None:
    """Daemon thread: reads stdout and stderr and puts them into the queue.

    It always ends with a `__fin__` event so that the window knows the process
    finished, even if the read failed."""
    def stamp(event: Event) -> Event:
        if run_id is None:
            return event
        return {**event, "run_id": run_id}

    def read(stream: Any, channel: str) -> None:
        try:
            for line in stream:
                event = parse_event(line)
                if event is not None:
                    message_queue.put((channel, stamp(event)))
        except Exception as e:                       # broken pipe, process killed…
            message_queue.put((channel, stamp({"tipo": "__aviso_lectura__",
                              "mensaje": f"{type(e).__name__}: {e}"})))
        finally:
            try:
                stream.close()
            except Exception:
                pass

    code: int | None = None
    try:
        thread_error = threading.Thread(target=read, args=(process.stderr, "stderr"), daemon=True)
        thread_error.start()
        read(process.stdout, "stdout")
        thread_error.join(timeout=2.0)
        try:
            code = process.wait(timeout=5.0)
        except Exception:
            code = None
    finally:
        message_queue.put(("fin", stamp({"tipo": "__fin__", "codigo": code,
                          "cancelado": bool(cancelled and cancelled())})))


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
        self.run_id = 0

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

    def start(self, path: str, mode: str = "full", seconds: int | None = None,
                pdf: bool = True, workers: int = 0, without_cache: bool = False,
                png: bool = False, output_dir: str | None = None) -> list[str]:
        """Launches the analysis. Returns the command used (useful for the log)."""
        if self.active:
            raise RuntimeError("an analysis is already running")
        command = build_command(path, mode=mode, seconds=seconds, pdf=pdf,
                                    workers=workers, without_cache=without_cache,
                                    engine=self.engine, png=png,
                                    output_dir=output_dir)
        self.cancelled = False
        popen_kwargs: dict[str, Any] = {
            "cwd": DIRECTORY,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
            "bufsize": 1,
        }
        # CREATE_NO_WINDOW stops a console from flashing on Windows; a dedicated
        # session on POSIX lets cancellation signal the entire worker tree.
        if os.name == "nt":
            popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        else:
            popen_kwargs["start_new_session"] = True
        try:
            self.process = self._lanzador(command, **popen_kwargs)
        except TypeError:
            if self._lanzador is subprocess.Popen or "start_new_session" not in popen_kwargs:
                raise
            popen_kwargs.pop("start_new_session", None)
            self.process = self._lanzador(command, **popen_kwargs)
        self.run_id += 1
        current_run = self.run_id
        self.thread = threading.Thread(
            target=output_reader,
            args=(self.process, self.message_queue, lambda: self.cancelled, current_run),
            daemon=True, name="lector-motor")
        self.thread.start()
        return command

    def cancel(self) -> None:
        self.cancelled = True
        finish_process(self.process)
        if self.process is not None:
            for stream in (self.process.stdout, self.process.stderr):
                if stream is not None:
                    try:
                        stream.close()
                    except Exception:
                        pass

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
        widget.bind("<Enter>", self._schedule, add=True)
        widget.bind("<Leave>", self._hide, add=True)
        widget.bind("<ButtonPress-1>", self._hide, add=True)

    def _schedule(self, _event: Any = None) -> None:
        self._cancel()
        if not self.text:
            return
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
        if not self.text:
            return
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

    def retheme(self) -> None:
        """Re-resolves the canvas colours and its icons for the current mode."""
        tkinter.Canvas.configure(self, highlightbackground=c("bg.canvas"),
                                 highlightcolor=c("focus"))
        self._icon_resting = self.window._icon("folder-plus", 24,
                                               c("text.secondary"))
        self._icon_active = self.window._icon("folder-plus", 24, c("accent"))
        self.set_active(self.active)

    def set_active(self, active: bool) -> None:
        """Drag-over state: accent border and a 6 % accent tint (7.3)."""
        self.active = bool(active)
        tint = (theme.mix(c("bg.surface"), c("accent"), 0.06) if self.active
                else c("bg.surface"))
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

    def retheme(self) -> None:
        """Background and thumb after a palette change."""
        self.configure(background=c("bg.surface"))
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


class _FlatRowFrame(tkinter.Frame):
    """Zero-canvas flat frame for 0-radius table rows, bars and separators.

    Translates `fg_color` in `configure()` and `cget()` while avoiding per-row
    CustomTkinter canvas widgets and global tracker callbacks.
    """

    def __init__(self, master: Any, fg_color: str = "", width: int = 0,
                 height: int = 0, **kwargs: Any) -> None:
        self._fg_color = fg_color
        self._logical_width = width
        self._logical_height = height
        init_kw: dict[str, Any] = {"bd": 0, "highlightthickness": 0}
        if fg_color:
            init_kw["background"] = str(fg_color)
        if width:
            init_kw["width"] = width
        if height:
            init_kw["height"] = height
        init_kw.update(kwargs)
        super().__init__(master, **init_kw)

    def configure(self, cnf: Any = None, **kwargs: Any) -> Any:
        if "fg_color" in kwargs:
            fg = kwargs.pop("fg_color")
            self._fg_color = fg
            kwargs["background"] = str(fg)
        if "width" in kwargs:
            self._logical_width = int(kwargs["width"])
        if "height" in kwargs:
            self._logical_height = int(kwargs["height"])
        return super().configure(cnf, **kwargs)

    config = configure

    def cget(self, key: str) -> Any:
        if key == "fg_color":
            return self._fg_color
        if key == "height":
            return self._logical_height if self._logical_height else int(super().cget("height"))
        if key == "width":
            return self._logical_width if self._logical_width else int(super().cget("width"))
        return super().cget(key)


class _FlatRowLabel(tkinter.Label):
    """Zero-canvas flat label for monospace table cells and MD5 status icons."""

    def __init__(self, master: Any, fg_color: str = "", text_color: str = "",
                 image: Any = None, **kwargs: Any) -> None:
        self._fg_color = fg_color
        self._text_color = text_color
        self._image_ref = image
        init_kw: dict[str, Any] = {
            "bd": 0, "highlightthickness": 0, "padx": 0, "pady": 0,
        }
        if fg_color:
            init_kw["background"] = str(fg_color)
        if text_color:
            init_kw["foreground"] = str(text_color)
        if image is not None:
            init_kw["image"] = image
        init_kw.update(kwargs)
        super().__init__(master, **init_kw)

    def configure(self, cnf: Any = None, **kwargs: Any) -> Any:
        if "fg_color" in kwargs:
            fg = kwargs.pop("fg_color")
            self._fg_color = fg
            kwargs["background"] = str(fg)
        if "text_color" in kwargs:
            tc = kwargs.pop("text_color")
            self._text_color = tc
            kwargs["foreground"] = str(tc)
        if "image" in kwargs:
            img = kwargs["image"]
            self._image_ref = img
            if img is None:
                kwargs["image"] = ""
        return super().configure(cnf, **kwargs)

    config = configure

    def cget(self, key: str) -> Any:
        if key == "fg_color":
            return self._fg_color
        if key == "text_color":
            return self._text_color
        if key == "image":
            return self._image_ref
        return super().cget(key)


class _VerdictChip(_FlatRowLabel):
    """Single-widget table verdict pill backed by a cached anti-aliased PhotoImage.

    Replaces three CustomTkinter canvas widgets (`chip`, `dot`, `label`) per row
    while exposing `cget("fg_color")` for theme and state assertions.
    """

    def __init__(self, master: Any, text: str, tone: str, pill_fg: str,
                 row_bg: str, image: Any = None) -> None:
        self.chip_text = text
        self.chip_tone = tone
        self._pill_fg = pill_fg
        super().__init__(master, text="" if image is not None else text,
                         fg_color=row_bg, image=image)

    def set_pill_visual(self, pill_fg: str, row_bg: str, image: Any) -> None:
        self._pill_fg = pill_fg
        if image is not None:
            super().configure(fg_color=row_bg, image=image, text="")
        else:
            super().configure(fg_color=row_bg, text=self.chip_text)

    def cget(self, key: str) -> Any:
        if key == "fg_color":
            return self._pill_fg
        return super().cget(key)


_CHIP_FONT_CANDIDATES = (
    "C:/Windows/Fonts/segoeuib.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
)
_PIL_CHIP_FONTS: dict[int, Any] = {}


def _get_pil_chip_font(px_size: int) -> Any:
    cached = _PIL_CHIP_FONTS.get(px_size)
    if cached is not None:
        return cached
    from PIL import ImageFont
    loaded: Any = None
    for candidate in _CHIP_FONT_CANDIDATES:
        if os.path.exists(candidate):
            try:
                loaded = ImageFont.truetype(candidate, px_size)
                break
            except Exception:
                continue
    if loaded is None:
        loaded = ImageFont.load_default()
    _PIL_CHIP_FONTS[px_size] = loaded
    return loaded


def _decode_spectrogram_pil(path: str, width_limit: int,
                            height_limit: int) -> tuple[Any, tuple[int, int]] | None:
    """Decodes and downscales a spectrogram PNG using PIL only (thread-safe)."""
    if not path or not os.path.exists(path):
        return None
    try:
        from PIL import Image
        target_w = max(1, round(width_limit * 1.5))
        target_h = max(1, round(height_limit * 1.5))
        with Image.open(path) as source:
            src_w, src_h = source.size
            if src_w <= 0 or src_h <= 0:
                return None
            ratio = min(1.0, target_w / src_w, target_h / src_h)
            thumb_size = (max(1, round(src_w * ratio)),
                          max(1, round(src_h * ratio)))
            try:
                source.draft("RGB", thumb_size)
            except Exception:
                pass
            rgb = source.convert("RGB")
            if rgb.size != thumb_size:
                picture = rgb.resize(thumb_size, Image.Resampling.BILINEAR)
            else:
                picture = rgb.copy()
        width, height = picture.size
        scale = min(width_limit / width, height_limit / height)
        display = (max(1, round(width * scale)),
                   max(1, round(height * scale)))
        return picture, display
    except Exception:
        return None


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
        ROW_HEIGHT = 44
        VIRTUAL_POOL_SIZE = 28
        MAX_SPECTROGRAM_CACHE = 256
        MAX_PIL_SPECTROGRAM_CACHE = 512
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
            self.run_state: RunState = RunState.IDLE
            self.active_run_id = 0
            self._cancelled_run_ids: set[int] = set()
            self.surface_mode: SurfaceMode = SurfaceMode.EMPTY
            self.hero_mode: HeroMode = HeroMode.ALBUM
            self._detail_visible: bool | None = None
            self.spectrogram_state: SpectrogramState = SpectrogramState.NONE
            # Bounded: a very long batch must not grow the window's memory forever.
            self.events: "deque[Event]" = deque(maxlen=MAX_EVENTS_IN_MEMORY)
            self.total = 0
            self.recibidos = 0
            self.pdf_path: str | None = None
            self._batch_queue: deque[str] = deque()
            self._batch_root = ""
            self._batch_total_count = 1
            self._batch_current_index = 1
            self._batch_current_path = ""
            self._failed_batches: list[tuple[int, str, str]] = []
            self._album_total = 0
            self._album_received = 0
            self.active_filter: VerdictFilter = VerdictFilter.ALL
            self._filter_counts: dict[str, int] = {
                VerdictFilter.ALL.value: 0,
                VerdictFilter.FLAGGED.value: 0,
                VerdictFilter.SUSPICIOUS.value: 0,
                VerdictFilter.ERRORS.value: 0,
            }
            self._filter_dirty = False
            self._rendered_filter_style: tuple[str, str] | None = None
            self._prev_filter_mode: str = VerdictFilter.ALL.value
            self._rendered_filter_counts: dict[str, int] = {}
            self._rendered_filter_status = ""
            # CTkImage and PhotoImage references must stay alive: a collected
            # image disappears from its widget silently.
            self._icons: dict[tuple[str, int, str], Any] = {}
            self._tk_icons: dict[tuple[str, int, str], Any] = {}
            self._chip_images: dict[tuple[str, str, str, float, str], Any] = {}
            # Results surface: logical track records (`table_rows`) + viewport pool.
            self.selected_event: Event | None = None
            self._hovered_entry: dict[str, Any] | None = None
            self._problems_expanded = False
            self._no_anomaly_chip: Any = None
            self._no_anomaly_label: Any = None
            self._problem_rows_pool: list[dict[str, Any]] = []
            self._problem_toggle_btn: Any = None
            self._last_problem_wrap = 0
            self.table_rows: list[dict[str, Any]] = []
            self._filtered_rows: list[dict[str, Any]] = []
            self._row_slots: list[dict[str, Any]] = []
            self._virtual_start_index = 0
            self._active_pool_cap = 18
            self._syncing_viewport = False
            self._viewport_dirty = False
            self._viewport_flush_scheduled = False
            self._last_scrollregion: tuple[int, int, int, int] | None = None
            self._table_window_top_y = 0
            self._measures: dict[tuple[Any, ...], Any] = {}
            self._font_max_char: dict[tuple[Any, ...], int] = {}
            self._scaled_fonts: dict[tuple[Any, ...], tuple[Any, ...]] = {}
            self._eyebrow_full = ""
            self._headline_full = ""
            self._last_log_line = ""
            self._log_entries: list[tuple[str, str, str, Event | None]] = []
            self.activity_expanded = False
            self._retheming = False
            self._hero_width = 0
            self._preview_width = 0
            self._canvas_width = 0
            self._pending_canvas_width = 0
            self._canvas_resize_after: str | None = None
            # (widget, background token) of every frame focus ring.
            self._focus_rings: list[tuple[Any, str]] = []
            self._buttons: list[Any] = []
            self._scroll_scheduled = False
            self._batch_draining = False
            self._pending_log_chunks: list[str] = []
            self._pending_log_scroll = False
            self._hero_dirty = False
            self._spectrograms: OrderedDict[str, Any] = OrderedDict()
            self._pil_spectrograms: OrderedDict[str, tuple[Any, tuple[int, int]]] = OrderedDict()
            self._spec_inflight: set[str] = set()
            self._spec_queued: set[str] = set()
            self._spec_priority: set[str] = set()
            self._spec_bad_paths: set[str] = set()
            self._spec_deque: deque[str] = deque()
            self._spec_lock = threading.Lock()
            self._spec_cond = threading.Condition(self._spec_lock)
            self._spec_ready_queue: "queue.Queue[str]" = queue.Queue()
            self._spec_poll_scheduled = False
            self._spec_stop = False
            self._spec_worker: threading.Thread | None = None
            self._spectrogram_path_value = ""
            self._hero_layout_scheduled = False

            self.grid_columnconfigure(0, weight=1)
            self.grid_rowconfigure(0, weight=1)
            # One container holds the two columns. The theme switch recolours the
            # widgets in place (see _retheme); nothing is rebuilt.
            self.shell = self._new_shell()
            self.shell.grid(row=0, column=0, sticky="nsew")

            self._build_controls(self.shell)
            self._build_telemetry(self.shell)
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

        def _tk_icon(self, name: str, size: int = 16, color: str | None = None) -> Any:
            """Native PhotoImage icon for lightweight table cells, cached by DPI and tint."""
            tint = str(color or c("text.secondary"))
            scaling = ctk.ScalingTracker.get_widget_scaling(self)
            px = max(1, round(size * scaling))
            key = (name, px, tint)
            if key in self._tk_icons:
                return self._tk_icons[key]
            photo = None
            path = os.path.join(ICON_FOLDER, f"{name}_{size}.png")
            if not os.path.exists(path):
                closest = min(ICON_SIZES, key=lambda value: abs(value - size))
                path = os.path.join(ICON_FOLDER, f"{name}_{closest}.png")
            if os.path.exists(path):
                try:
                    from PIL import Image, ImageTk
                    with Image.open(path) as source:
                        alpha = source.convert("RGBA").getchannel("A")
                    if alpha.size != (px, px):
                        alpha = alpha.resize((px, px), Image.Resampling.BILINEAR)
                    coloured = Image.new("RGBA", (px, px), tint)
                    coloured.putalpha(alpha)
                    photo = ImageTk.PhotoImage(coloured, master=self)
                except Exception:
                    photo = None
            self._tk_icons[key] = photo
            return photo

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
            # The tokens are kept in `ui_spec`, not the resolved hex values: a
            # retheme repaints the same button from the new palette.
            behind = background or "bg.surface"
            if spec["border"]:
                border = c(spec["border"])
            elif spec["fill"] != "transparent":
                border = c(spec["fill"])
            else:
                border = c(behind)
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
            self._buttons.append(button)
            button.ui_spec = {
                "spec": spec, "parent": behind, "border": spec["border"],
                "focused": False,
                "states": {"normal": spec["fill"] if spec["fill"] != "transparent"
                                       else "transparent",
                           "hover": spec["hover"],
                           "pressed": spec["pressed"]},
                "icon": (icon, icon_size, spec["text"]) if icon else None,
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
            token = ui["states"][state]
            colour = "transparent" if token == "transparent" else c(token)
            button.configure(fg_color=colour)
            if ui["spec"]["border"] is None and ui["spec"]["fill"] != "transparent":
                button.configure(border_color=c("focus") if ui["focused"] else colour)

        def _focus_ring(self, button: Any, focused: bool) -> None:
            ui = button.ui_spec
            ui["focused"] = focused
            if focused:
                button.configure(border_width=2, border_color=c("focus"))
            else:
                if ui["border"]:
                    border = c(ui["border"])
                elif ui["states"]["normal"] != "transparent":
                    border = c(ui["states"]["normal"])
                else:
                    border = c(ui["parent"])
                button.configure(border_width=ui["spec"]["border_width"],
                                 border_color=border)

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
            self._focus_rings.append((widget, background))
            tkinter.Frame.configure(widget, takefocus=True, highlightthickness=2,
                                    highlightbackground=c(background),
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
                dimmed = theme.mix(c(ui["states"]["normal"]), c(ui["parent"]), 0.35)
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

        def _new_shell(self) -> Any:
            """Container of the rail and the main column, ready to be swapped."""
            shell = ctk.CTkFrame(self, fg_color="transparent")
            shell.grid_columnconfigure(0, weight=0, minsize=RAIL_WIDTH)
            shell.grid_columnconfigure(1, weight=1)
            shell.grid_rowconfigure(0, weight=1)
            return shell

        def _build_controls(self, parent: Any) -> None:
            rail = ctk.CTkFrame(parent, width=RAIL_WIDTH, corner_radius=0,
                                fg_color=c("bg.surface"))
            rail.grid(row=0, column=0, sticky="nsw")
            rail.grid_propagate(False)
            rail.grid_columnconfigure(0, weight=1)
            # The spacer row is what pins the action bar to the bottom of the rail.
            rail.grid_rowconfigure(9, weight=1)

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

            # ANALYSIS (always scans in `full` mode)
            self._section_label(rail, "Analysis", row=4)
            self.pdf_group = ctk.CTkFrame(rail, fg_color="transparent")
            self.pdf_group.grid(row=5, column=0, padx=theme.SPACE["lg"],
                                pady=(0, theme.SPACE["xs"]), sticky="ew")
            self.pdf_group.grid_columnconfigure(0, weight=1)

            self.var_pdf = ctk.BooleanVar(value=False)
            self.check_pdf = ctk.CTkCheckBox(
                self.pdf_group, text="Generate PDF report", variable=self.var_pdf,
                command=self._change_pdf,
                checkbox_width=18, checkbox_height=18,
                corner_radius=theme.RADIUS["xs"], border_width=1,
                border_color=c("border.strong"), fg_color=c("accent"),
                hover_color=c("accent.hover"), checkmark_color=c("on.accent"),
                text_color=c("text.primary"),
                text_color_disabled=c("text.tertiary"), font=theme.font(13))
            self.check_pdf.grid(row=0, column=0, sticky="w")
            self._ring_highlight(self.check_pdf, "bg.surface")
            self._toggle_on_space(self.check_pdf, self.var_pdf, self._change_pdf)

            self.pdf_output_row = ctk.CTkFrame(
                self.pdf_group, fg_color=c("bg.raised"),
                corner_radius=theme.RADIUS["md"],
                border_width=1, border_color=c("border.subtle"),
            )
            self.pdf_output_row.grid(row=1, column=0, pady=(theme.SPACE["xs"], 0),
                                     sticky="ew")
            self.pdf_output_row.grid_columnconfigure(0, weight=1)
            ctk.CTkLabel(
                self.pdf_output_row, text="PDF destination", anchor="w",
                font=theme.font(11, 600), text_color=c("text.secondary"),
            ).grid(
                row=0, column=0, sticky="ew",
                padx=theme.SPACE["sm"], pady=(theme.SPACE["xs"], theme.SPACE["2xs"]),
            )
            self.var_pdf_dest_mode = ctk.StringVar(value="album")
            self.radio_pdf_album = ctk.CTkRadioButton(
                self.pdf_output_row,
                text="Save in each album folder",
                variable=self.var_pdf_dest_mode,
                value="album",
                command=self._change_pdf_dest_mode,
                radiobutton_width=16,
                radiobutton_height=16,
                border_width_unchecked=2,
                border_width_checked=5,
                fg_color=c("accent"),
                hover_color=c("accent.hover"),
                border_color=c("border.strong"),
                text_color=c("text.primary"),
                text_color_disabled=c("text.tertiary"),
                font=theme.font(12),
            )
            self.radio_pdf_album.grid(
                row=1, column=0, sticky="w",
                padx=theme.SPACE["sm"], pady=(0, theme.SPACE["2xs"]),
            )
            self.radio_pdf_custom = ctk.CTkRadioButton(
                self.pdf_output_row,
                text="Save in custom folder...",
                variable=self.var_pdf_dest_mode,
                value="custom",
                command=self._change_pdf_dest_mode,
                radiobutton_width=16,
                radiobutton_height=16,
                border_width_unchecked=2,
                border_width_checked=5,
                fg_color=c("accent"),
                hover_color=c("accent.hover"),
                border_color=c("border.strong"),
                text_color=c("text.primary"),
                text_color_disabled=c("text.tertiary"),
                font=theme.font(12),
            )
            self.radio_pdf_custom.grid(
                row=2, column=0, sticky="w",
                padx=theme.SPACE["sm"], pady=(0, theme.SPACE["2xs"]),
            )
            self.pdf_custom_dir_row = ctk.CTkFrame(
                self.pdf_output_row, fg_color="transparent"
            )
            self.pdf_custom_dir_row.grid(
                row=3, column=0, sticky="ew",
                padx=theme.SPACE["sm"], pady=(theme.SPACE["2xs"], theme.SPACE["2xs"]),
            )
            self.pdf_custom_dir_row.grid_columnconfigure(0, weight=1)
            self.var_output_dir = ctk.StringVar(value="")
            self.input_output_dir = ctk.CTkEntry(
                self.pdf_custom_dir_row, textvariable=self.var_output_dir, height=32,
                corner_radius=theme.RADIUS["md"], border_width=1,
                border_color=c("border.subtle"), fg_color=c("bg.surface"),
                text_color=c("text.primary"),
                placeholder_text="Choose folder…",
                placeholder_text_color=c("text.tertiary"), font=theme.font(12))
            self.input_output_dir.grid(row=0, column=0, sticky="ew")
            self._ring_border(self.input_output_dir, 1, "border.subtle")

            self.btn_output_dir = self._button(
                self.pdf_custom_dir_row, "icon", "", self._choose_output_dir,
                icon="folder", icon_size=18, width=30, background="bg.raised")
            self.btn_output_dir.grid(row=0, column=1, padx=(theme.SPACE["2xs"], 0))
            self.output_dir_tooltip = _Tooltip(
                self.btn_output_dir, "Choose PDF output folder")

            self.btn_clear_output_dir = self._button(
                self.pdf_custom_dir_row, "icon", "", self._clear_output_dir,
                icon="x", icon_size=16, width=26, background="bg.raised")
            self.btn_clear_output_dir.grid(row=0, column=2,
                                           padx=(theme.SPACE["2xs"], 0))
            self.clear_output_dir_tooltip = _Tooltip(
                self.btn_clear_output_dir, "Save in each album folder")
            self.label_pdf_dest_hint = ctk.CTkLabel(
                self.pdf_output_row,
                text="Writes flac_verifier_report.pdf inside each album",
                anchor="w",
                font=theme.font(11),
                text_color=c("text.tertiary"),
            )
            self.label_pdf_dest_hint.grid(
                row=4, column=0, sticky="ew",
                padx=theme.SPACE["sm"], pady=(0, theme.SPACE["xs"]),
            )
            self.var_output_dir.trace_add("write", self._on_output_dir_var_changed)
            self.var_pdf.trace_add("write", lambda *_args: self._change_pdf())
            self._change_pdf()

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
            self.parallel_switch.grid(row=6, column=0, padx=theme.SPACE["lg"],
                                      pady=(theme.SPACE["xs"], theme.SPACE["2xs"]),
                                      sticky="w")
            self._ring_highlight(self.parallel_switch, "bg.surface")
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
            self.option_workers.grid(row=7, column=0, padx=theme.SPACE["lg"],
                                     pady=(theme.SPACE["2xs"], theme.SPACE["xs"]),
                                     sticky="w")
            self._ring_highlight(self.option_workers, "bg.surface")

            self.var_without_cache = ctk.BooleanVar(value=False)
            self.check_cache = ctk.CTkCheckBox(
                rail, text="Ignore cache", variable=self.var_without_cache,
                checkbox_width=18, checkbox_height=18,
                corner_radius=theme.RADIUS["xs"], border_width=1,
                border_color=c("border.strong"), fg_color=c("accent"),
                hover_color=c("accent.hover"), checkmark_color=c("on.accent"),
                text_color=c("text.primary"),
                text_color_disabled=c("text.tertiary"), font=theme.font(13))
            self.check_cache.grid(row=8, column=0, padx=theme.SPACE["lg"],
                                  pady=(theme.SPACE["xs"], 0), sticky="w")
            self._ring_highlight(self.check_cache, "bg.surface")
            self._toggle_on_space(self.check_cache, self.var_without_cache)

            # Pinned action bar: the primary action, always visible at the
            # bottom of the rail (DESIGN.md 6).
            footer = ctk.CTkFrame(rail, fg_color="transparent")
            footer.grid(row=10, column=0, padx=theme.SPACE["lg"],
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
        def _build_telemetry(self, parent: Any) -> None:
            panel = ctk.CTkFrame(parent, corner_radius=0, fg_color="transparent")
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
            # window is recoloured in place and the choice is saved for the next
            # start (see _retheme).
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
            self.hero_problems.grid_columnconfigure(0, weight=1)
            self._no_anomaly_chip = self._chip(self.hero_problems, "", "ok")
            self._no_anomaly_label = self._no_anomaly_chip._chip_parts[1]
            self._no_anomaly_chip.grid_remove()
            for _ in range(10):
                self._problem_rows_pool.append(self._create_problem_pool_entry())
            self._problem_toggle_btn = ctk.CTkButton(
                self.hero_problems, text="", height=24, width=120,
                fg_color="transparent", hover_color=c("bg.hover"),
                text_color=c("accent"), font=theme.font(11, 600),
                border_width=0, anchor="w", command=self._toggle_problems)
            self._problem_toggle_btn.grid_remove()

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
            self.spectrogram_icon = ctk.CTkLabel(
                self.spectrogram_placeholder, text="",
                image=self._icon("music", 24, c("text.tertiary")))
            self.spectrogram_icon.grid(row=0, column=0,
                                       pady=(theme.SPACE["sm"], theme.SPACE["2xs"]))
            self._placeholder_icon_spec = ("music", 24, "text.tertiary")
            self.spectrogram_note = ctk.CTkLabel(
                self.spectrogram_placeholder, text="No spectrogram",
                font=theme.font(12), text_color=c("text.secondary"))
            self.spectrogram_note.grid(row=1, column=0)
            self.spectrogram_hint = ctk.CTkLabel(
                self.spectrogram_placeholder, text="Spectrogram not available",
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
                                             background="bg.surface")
            self.btn_open_pdf.grid(row=0, column=0)
            self.btn_reveal = self._button(actions, "ghost", "Reveal", self._reveal,
                                           icon="folder",
                                           background="bg.surface")
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

        FILTER_BUTTON_SPECS = (
            (VerdictFilter.ALL.value, "All", "accent", "accent.hover", "accent", "on.accent"),
            (VerdictFilter.FLAGGED.value, "Flagged", "bad.bg", "bg.hover", "bad.border", "bad"),
            (VerdictFilter.SUSPICIOUS.value, "Suspicious", "warn.bg", "bg.hover", "warn.border", "warn"),
            (VerdictFilter.ERRORS.value, "Errors", "danger.bg", "bg.hover", "danger", "danger"),
        )

        def _build_table(self, parent: Any) -> None:
            """Filter bar, column header and streaming rows (DESIGN.md 7.7)."""
            self.table_panel = ctk.CTkFrame(parent, fg_color=c("bg.surface"),
                                            corner_radius=theme.RADIUS["lg"],
                                            border_width=1,
                                            border_color=c("border.subtle"))
            self.table_panel.grid(row=1, column=0, sticky="nsew",
                                  padx=theme.SPACE["xl"],
                                  pady=(0, theme.SPACE["md"]))
            self.table_panel.grid_columnconfigure(0, weight=1)
            self.table_panel.grid_rowconfigure(2, weight=1)

            self.filter_bar = ctk.CTkFrame(self.table_panel, fg_color="transparent",
                                           height=38, corner_radius=0)
            self.filter_bar.grid(row=0, column=0, columnspan=2, sticky="ew",
                                 padx=theme.SPACE["sm"],
                                 pady=(theme.SPACE["xs"], theme.SPACE["2xs"]))
            self.filter_bar.grid_columnconfigure(len(self.FILTER_BUTTON_SPECS), weight=1)
            self.filter_buttons: dict[str, Any] = {}
            for col_idx, (key, title, _fill, _hov, _bd, _ink) in enumerate(self.FILTER_BUTTON_SPECS):
                button = ctk.CTkButton(
                    self.filter_bar,
                    text=f"{title} (0)",
                    height=28,
                    width=88,
                    corner_radius=theme.RADIUS["sm"],
                    border_width=1,
                    border_color=c("border.subtle"),
                    fg_color=c("bg.raised"),
                    hover_color=c("bg.hover"),
                    text_color=c("text.secondary"),
                    font=theme.font(12, 600),
                    cursor="hand2",
                    command=lambda mode_key=key: self.set_verdict_filter(mode_key),
                )
                button.grid(row=0, column=col_idx, sticky="w",
                            padx=(0, theme.SPACE["xs"]))
                tkinter.Frame.configure(button, takefocus=True)
                tkinter.Frame.bind(
                    button, "<FocusIn>",
                    lambda _ev, btn=button: btn.configure(
                        border_width=2, border_color=c("focus")),
                )
                tkinter.Frame.bind(
                    button, "<FocusOut>",
                    lambda _ev, mode_key=key: self._style_filter_button(mode_key),
                )
                tkinter.Frame.bind(
                    button, "<Return>",
                    lambda _ev, mode_key=key: self._activate_filter_key(mode_key),
                )
                tkinter.Frame.bind(
                    button, "<space>",
                    lambda _ev, mode_key=key: self._activate_filter_key(mode_key),
                )
                self.filter_buttons[key] = button

            self.label_filter_status = ctk.CTkLabel(
                self.filter_bar, text="", anchor="e",
                font=theme.font(11), text_color=c("text.tertiary"),
            )
            self.label_filter_status.grid(
                row=0, column=len(self.FILTER_BUTTON_SPECS), sticky="e",
                padx=(theme.SPACE["xs"], 0),
            )
            self._refresh_filter_bar(force=True)

            self.table_header = ctk.CTkFrame(self.table_panel, fg_color="transparent",
                                             height=36, corner_radius=0)
            self.table_header.grid(row=1, column=0, sticky="ew")
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
                                               highlightthickness=0, bd=0,
                                               yscrollincrement=self.ROW_HEIGHT // 2)
            self.table_canvas.grid(row=2, column=0, sticky="nsew")
            self.table_scroll = _TableScrollbar(
                self.table_panel, command=self._on_scrollbar_command,
                background=c("bg.surface"))
            self.table_scroll.grid(row=2, column=1, sticky="ns")
            self.table_canvas.configure(yscrollcommand=self._on_canvas_yscroll)

            self.table_body = _FlatRowFrame(self.table_canvas, height=1,
                                            fg_color=c("bg.surface"))
            self._table_window = self.table_canvas.create_window(
                (0, 0), window=self.table_body, anchor="nw")
            self.table_body.bind("<Configure>", self._on_table_body)
            self.table_canvas.bind("<Configure>", self._on_table_canvas)
            self.table_canvas.bind("<Leave>", self._on_table_canvas_leave, add=True)
            self.table_canvas.bind("<MouseWheel>", self._on_table_wheel)
            self.table_body.bind("<MouseWheel>", self._on_table_wheel)

        def _configure_table_columns(self, frame: Any) -> None:
            """Same column grid for the header and for every row, so they align."""
            frame.grid_columnconfigure(0, weight=0, minsize=2)
            for index, (_, weight, minsize, _) in enumerate(TABLE_COLUMNS, start=1):
                frame.grid_columnconfigure(index, weight=weight, minsize=minsize)

        def _activate_filter_key(self, mode_key: str) -> str:
            self.set_verdict_filter(mode_key)
            return "break"

        def _style_filter_button(self, mode_key: str) -> None:
            button = self.filter_buttons.get(mode_key)
            if button is None:
                return
            is_active = mode_key == self.active_filter.value
            if is_active:
                _, _, fill_t, hover_t, border_t, text_t = next(
                    spec for spec in self.FILTER_BUTTON_SPECS if spec[0] == mode_key
                )
            else:
                fill_t, hover_t, border_t, text_t = (
                    "bg.raised", "bg.hover", "border.subtle", "text.secondary"
                )
            button.configure(
                fg_color=c(fill_t),
                hover_color=c(hover_t),
                border_color=c(border_t),
                border_width=1,
                text_color=c(text_t),
            )

        def _reset_filter_counts(self) -> None:
            for key in self._filter_counts:
                self._filter_counts[key] = 0
            self._filter_dirty = False
            self._rendered_filter_style = None
            self._rendered_filter_counts.clear()
            self._rendered_filter_status = ""
            if hasattr(self, "filter_buttons"):
                self._refresh_filter_bar(force=True)

        @staticmethod
        def _event_filter_bucket(event: Event) -> str:
            md5 = event.get("md5") if isinstance(event.get("md5"), dict) else {}
            if (
                bool(event.get("error"))
                or not event.get("veredicto")
                or md5.get("estado") == "mismatch"
            ):
                return VerdictFilter.ERRORS.value
            verdict = str(event.get("veredicto") or "")
            if verdict == "PROBABLE UPSCALE":
                return VerdictFilter.FLAGGED.value
            if verdict == "SUSPICIOUS":
                return VerdictFilter.SUSPICIOUS.value
            return "ok"

        def _record_filter_event(self, event: Event, bucket: str | None = None) -> None:
            self._filter_counts[VerdictFilter.ALL.value] += 1
            resolved = bucket or self._event_filter_bucket(event)
            if resolved in self._filter_counts:
                self._filter_counts[resolved] += 1

        def _refresh_filter_bar(self, force: bool = False) -> None:
            if not hasattr(self, "filter_buttons"):
                return
            active_val = self.active_filter.value
            style_sig = (theme.MODE, active_val)
            style_dirty = force or (self._rendered_filter_style != style_sig)
            theme_changed = (
                self._rendered_filter_style is None
                or self._rendered_filter_style[0] != theme.MODE
            )
            prev_active = self._prev_filter_mode
            if style_dirty:
                self._rendered_filter_style = style_sig
                self._prev_filter_mode = active_val
            for mode_key, title, *_ in self.FILTER_BUTTON_SPECS:
                count = self._filter_counts.get(mode_key, 0)
                button = self.filter_buttons[mode_key]
                if force or self._rendered_filter_counts.get(mode_key) != count:
                    self._rendered_filter_counts[mode_key] = count
                    button.configure(text=f"{title} ({count})")
                if force or theme_changed or (
                    style_dirty and mode_key in (prev_active, active_val)
                ):
                    self._style_filter_button(mode_key)
            total_all = self._filter_counts.get(VerdictFilter.ALL.value, 0)
            if self.active_filter is VerdictFilter.ALL:
                status_text = ""
            else:
                shown = self._filter_counts.get(active_val, 0)
                status_text = f"Showing {shown} of {total_all}"
            if force or self._rendered_filter_status != status_text:
                self._rendered_filter_status = status_text
                self.label_filter_status.configure(text=status_text)

        def set_verdict_filter(self, filter_mode: VerdictFilter | str) -> None:
            """Applies a live verdict filter across existing rows in O(visible) viewport."""
            mode = (
                filter_mode
                if isinstance(filter_mode, VerdictFilter)
                else VerdictFilter(filter_mode)
            )
            self.active_filter = mode
            if mode is VerdictFilter.ALL:
                for entry in self.table_rows:
                    entry["visible"] = True
                self._filtered_rows = list(self.table_rows)
            else:
                target_bucket = mode.value
                filtered: list[dict[str, Any]] = []
                for entry in self.table_rows:
                    want = entry["filter_bucket"] == target_bucket
                    entry["visible"] = want
                    if want:
                        filtered.append(entry)
                self._filtered_rows = filtered
            self._virtual_start_index = 0
            self._syncing_viewport = True
            try:
                if self.table_canvas.yview()[0] != 0.0:
                    self.table_canvas.yview_moveto(0.0)
            finally:
                self._syncing_viewport = False
            self._sync_virtual_viewport(force=False)
            self._clear_hover()
            self._refresh_filter_bar(force=False)

        def _display_file_name(self, event: Event, values: dict[str, Any]) -> str:
            if self._batch_total_count > 1 and values.get("path"):
                ctx = relative_album_context(values["path"], self._batch_root)
                if ctx:
                    return f"{ctx} / {values['file']}"
            return values["file"]

        # ── table rows (virtualized viewport pool) ──────────────────────────
        def _on_scrollbar_command(self, *args: Any) -> None:
            self.table_canvas.yview(*args)
            self._sync_virtual_viewport()
            self._clear_hover()

        def _on_canvas_yscroll(self, first: Any, last: Any) -> None:
            self.table_scroll.set(first, last)
            if not self._syncing_viewport and not self._retheming:
                self._sync_virtual_viewport()

        def _set_scrollregion(self, region: tuple[int, int, int, int]) -> None:
            if self._last_scrollregion != region:
                self._last_scrollregion = region
                self.table_canvas.configure(scrollregion=region)

        def _create_row_slot(self) -> dict[str, Any]:
            """Creates one reusable 44 px viewport row slot inside `table_body`."""
            mono = self._scaled_font(theme.mono(12))
            surface_bg = c("bg.surface")
            row = _FlatRowFrame(self.table_body, height=self.ROW_HEIGHT,
                                fg_color=surface_bg)
            row.grid_rowconfigure(0, minsize=self.ROW_HEIGHT - 1)
            row.grid_rowconfigure(1, minsize=1)
            self._configure_table_columns(row)

            bar = _FlatRowFrame(row, width=2, height=1, fg_color=surface_bg)
            bar.grid(row=0, column=0, sticky="ns")

            file_font = theme.font(13)
            file_label = tkinter.Label(
                row, text="", anchor="w", width=1, bd=0, highlightthickness=0,
                background=surface_bg, foreground=c("text.primary"),
                font=self._scaled_font(file_font),
            )
            file_label.grid(row=0, column=1, sticky="ew",
                            padx=(theme.SPACE["sm"], theme.SPACE["xs"]))
            file_label.full_text = ""
            file_label.ui_font = file_font
            file_label.tooltip = _Tooltip(file_label, "")
            file_label.fitted_width = 0
            file_label.bind(
                "<Configure>",
                lambda configure, label=file_label: self._refit_file_name(label, configure),
            )

            chip = self._row_chip(row, "OK", "ok", surface_bg)
            chip.grid(row=0, column=2, sticky="w",
                      padx=(theme.SPACE["xs"], theme.SPACE["xs"]))

            plain: list[Any] = []
            text_sec = c("text.secondary")
            for column, text, anchor in (
                (3, "—", "e"),
                (4, "—", "w"),
                (5, "—", "w"),
                (6, "—", "w"),
            ):
                label = _FlatRowLabel(row, text=text, anchor=anchor, font=mono,
                                      fg_color=surface_bg, text_color=text_sec)
                label.grid(row=0, column=column, sticky="ew",
                           padx=(theme.SPACE["xs"], theme.SPACE["xs"]))
                plain.append(label)

            mark = _FlatRowLabel(row, text="—", font=mono, fg_color=surface_bg,
                                 text_color=c("text.tertiary"))
            mark.grid(row=0, column=7, sticky="w",
                      padx=(theme.SPACE["xs"], theme.SPACE["sm"]))
            plain.append(mark)
            mark_tooltip = _Tooltip(mark, "")

            separator = _FlatRowFrame(row, height=1, fg_color=c("border.subtle"))
            separator.grid(row=1, column=0, columnspan=len(TABLE_COLUMNS) + 1,
                           sticky="ew")

            slot: dict[str, Any] = {
                "entry": None,
                "packed": False,
                "frame": row,
                "bar": bar,
                "separator": separator,
                "cells": {
                    "file": file_label,
                    "verdict": chip,
                    "score": plain[0],
                    "depth": plain[1],
                    "rate": plain[2],
                    "dr": plain[3],
                    "md5": mark,
                },
                "file_label": file_label,
                "plain": plain,
                "mark_tooltip": mark_tooltip,
                "_file_text": "",
                "_plain_texts": ["—", "—", "—", "—"],
                "_md5_state": ("empty", None),
                "_chip_sig": None,
                "_row_bg_sig": None,
            }
            file_label._row_slot = slot
            self._bind_slot(row, slot)
            return slot

        def _bind_slot(self, widget: Any, slot: dict[str, Any]) -> None:
            widget._row_slot = slot
            widget.bind(
                "<Button-1>",
                lambda _event, sl=slot: sl["entry"] is not None and self._select(sl["entry"]["event"]),
                add=True,
            )
            widget.bind(
                "<Enter>",
                lambda _event, sl=slot: sl["entry"] is not None and self._hover_row(sl["entry"], True),
                add=True,
            )
            widget.bind(
                "<Leave>",
                lambda event, sl=slot: sl["entry"] is not None and self._row_leave(sl["entry"], event),
                add=True,
            )
            widget.bind("<MouseWheel>", self._on_table_wheel, add=True)
            for child in widget.winfo_children():
                self._bind_slot(child, slot)

        def _unbind_slot(self, slot: dict[str, Any]) -> None:
            old = slot.get("entry")
            if old is not None and old.get("slot") is slot:
                if self._hovered_entry is old:
                    self._hovered_entry = None
                old["hover"] = False
                old["slot"] = None
                old["frame"] = None
                old["bar"] = None
                old["separator"] = None
                old["cells"] = None
                old["file_label"] = None
                old["plain"] = None
                old["_was_selected"] = False
                old["_paint_state"] = None
            slot["entry"] = None

        def _bind_slot_to_entry(
            self, slot: dict[str, Any], entry: dict[str, Any], force: bool = False
        ) -> None:
            current_entry = slot.get("entry")
            if current_entry is entry and not force:
                return
            if current_entry is not entry:
                self._unbind_slot(slot)
                prev_slot = entry.get("slot")
                if prev_slot is not None and prev_slot is not slot:
                    self._unbind_slot(prev_slot)
                slot["entry"] = entry
                entry["slot"] = slot
                entry["frame"] = slot["frame"]
                entry["bar"] = slot["bar"]
                entry["separator"] = slot["separator"]
                entry["cells"] = slot["cells"]
                entry["file_label"] = slot["file_label"]
                entry["plain"] = slot["plain"]

            values = entry["values"]
            display_name = values["display_file"]
            file_lbl = slot["file_label"]
            file_lbl.full_text = display_name
            fit_w = file_lbl.fitted_width
            if fit_w > 8:
                cached_fit = entry.get("_fitted")
                if cached_fit is not None and cached_fit[0] == fit_w:
                    shown = cached_fit[1]
                else:
                    shown = self._fit(display_name, file_lbl.ui_font, fit_w - 4)
                    entry["_fitted"] = (fit_w, shown)
                file_lbl.tooltip.text = display_name if shown != display_name else ""
            else:
                shown = display_name
                file_lbl.tooltip.text = ""
            tk_call = self.tk.call
            if force or slot["_file_text"] != shown:
                slot["_file_text"] = shown
                tk_call(file_lbl._w, "configure", "-text", shown)

            score_text = "—" if values["score"] is None else str(values["score"])
            plain_vals = (score_text, values["depth"], values["rate"], values["dr"])
            prev_texts = slot["_plain_texts"]
            for idx, new_txt in enumerate(plain_vals):
                if force or prev_texts[idx] != new_txt:
                    prev_texts[idx] = new_txt
                    tk_call(slot["plain"][idx]._w, "configure", "-text", new_txt)

            icon_spec = entry.get("icon_spec")
            mark_lbl = slot["cells"]["md5"]
            if icon_spec is None:
                md5_key = ("dash", None)
                if force or slot["_md5_state"] != md5_key:
                    slot["_md5_state"] = md5_key
                    mark_lbl._image_ref = None
                    tk_call(
                        mark_lbl._w, "configure",
                        "-image", "", "-text", "—",
                        "-foreground", str(c("text.tertiary")),
                    )
                    slot["mark_tooltip"].text = ""
            else:
                md5_key = ("icon", icon_spec)
                if force or slot["_md5_state"] != md5_key:
                    slot["_md5_state"] = md5_key
                    photo = self._tk_icon(icon_spec[0], icon_spec[1], c(icon_spec[2]))
                    mark_lbl._image_ref = photo
                    tk_call(
                        mark_lbl._w, "configure",
                        "-text", "", "-image", "" if photo is None else photo,
                    )
                slot["mark_tooltip"].text = f"MD5 {values['md5']}"

            verdict_cell = slot["cells"]["verdict"]
            verdict_cell.chip_text = values["label"]
            verdict_cell.chip_tone = values["tone"]
            entry["_paint_state"] = None
            self._paint_row(entry, force=force)

        def _viewport_pool_size(self) -> int:
            canvas_h = (
                self.table_canvas.winfo_height()
                if hasattr(self, "table_canvas")
                else 0
            )
            if canvas_h <= 1:
                return 18
            return max(12, min(self.VIRTUAL_POOL_SIZE, (canvas_h // self.ROW_HEIGHT) + 4))

        def _sync_virtual_viewport(self, force: bool = False) -> None:
            if self._syncing_viewport:
                return
            self._syncing_viewport = True
            self._viewport_dirty = False
            try:
                n_visible = len(self._filtered_rows)
                pool_cap = self._viewport_pool_size()
                self._active_pool_cap = pool_cap
                target_slots = min(n_visible, pool_cap)
                while len(self._row_slots) < target_slots:
                    self._row_slots.append(self._create_row_slot())

                for idx, slot in enumerate(self._row_slots):
                    if idx < target_slots:
                        if not slot["packed"]:
                            slot["frame"].pack(fill="x")
                            slot["packed"] = True
                    else:
                        self._unbind_slot(slot)
                        if slot["packed"]:
                            slot["frame"].pack_forget()
                            slot["packed"] = False

                cw = max(1, self._canvas_width or self.table_canvas.winfo_width())
                if n_visible == 0:
                    self._virtual_start_index = 0
                    if self._table_window_top_y != 0:
                        self.table_canvas.coords(self._table_window, 0, 0)
                        self._table_window_top_y = 0
                    tkinter.Frame.configure(self.table_body, height=1)
                    self._set_scrollregion((0, 0, cw, 1))
                    self.table_scroll.set(0.0, 1.0)
                    return

                if n_visible <= pool_cap:
                    self._virtual_start_index = 0
                    for idx in range(n_visible):
                        self._bind_slot_to_entry(
                            self._row_slots[idx], self._filtered_rows[idx], force=force
                        )
                    if self._table_window_top_y != 0:
                        self.table_canvas.coords(self._table_window, 0, 0)
                        self._table_window_top_y = 0
                    req_h = max(1, int(self.table_body.winfo_reqheight()) or (n_visible * self.ROW_HEIGHT))
                    if self.table_body.winfo_height() > req_h:
                        tkinter.Frame.configure(self.table_body, height=req_h)
                    self._set_scrollregion((0, 0, cw, req_h))
                    return

                total_height = n_visible * self.ROW_HEIGHT
                self._set_scrollregion((0, 0, cw, total_height))
                first_frac, _ = self.table_canvas.yview()
                first_row = int((first_frac * total_height) // self.ROW_HEIGHT)
                start_index = max(0, min(n_visible - pool_cap, first_row - 2))
                self._virtual_start_index = start_index
                top_y = start_index * self.ROW_HEIGHT
                if self._table_window_top_y != top_y:
                    self.table_canvas.coords(self._table_window, 0, top_y)
                    self._table_window_top_y = top_y
                for slot_idx in range(pool_cap):
                    self._bind_slot_to_entry(
                        self._row_slots[slot_idx],
                        self._filtered_rows[start_index + slot_idx],
                        force=force,
                    )
            finally:
                self._syncing_viewport = False

        def _schedule_viewport_flush(self) -> None:
            if self._viewport_flush_scheduled:
                return
            self._viewport_flush_scheduled = True
            self.after_idle(self._flush_viewport)

        def _flush_viewport(self) -> None:
            self._viewport_flush_scheduled = False
            if self._viewport_dirty:
                self._sync_virtual_viewport()

        def _add_row(self, event: Event) -> dict[str, Any]:
            """Records a result row and mounts it only when inside the visible viewport."""
            follow = self._table_at_bottom()
            values = result_row(event)
            display_name = self._display_file_name(event, values)
            values["display_file"] = display_name
            batch_idx = self._batch_current_index
            insert_pos: int | None = None
            original_index = event.get("indice_original")
            if isinstance(original_index, int) and self.table_rows:
                idx = len(self.table_rows) - 1
                while idx >= 0:
                    existing = self.table_rows[idx]
                    if existing.get("batch_index", 1) != batch_idx:
                        break
                    existing_index = existing["event"].get("indice_original")
                    if (isinstance(existing_index, int)
                            and existing_index > original_index):
                        insert_pos = idx
                        idx -= 1
                        continue
                    break

            bucket = self._event_filter_bucket(event)
            want_visible = (
                self.active_filter is VerdictFilter.ALL
                or bucket == self.active_filter.value
            )
            icon_spec = (
                None if values["md5"] == "—"
                else (values["md5_icon"], 16, values["md5_tone"])
            )
            entry: dict[str, Any] = {
                "event": event,
                "values": values,
                "slot": None,
                "frame": None,
                "bar": None,
                "separator": None,
                "cells": None,
                "file_label": None,
                "plain": None,
                "icon_spec": icon_spec,
                "filter_bucket": bucket,
                "hover": False,
                "visible": want_visible,
                "batch_index": batch_idx,
                "_was_selected": False,
                "_paint_state": None,
            }
            if insert_pos is not None:
                self.table_rows.insert(insert_pos, entry)
            else:
                self.table_rows.append(entry)

            filt_pos: int | None = None
            if want_visible:
                if insert_pos is not None and self._filtered_rows:
                    fidx = len(self._filtered_rows) - 1
                    while fidx >= 0:
                        fexisting = self._filtered_rows[fidx]
                        if fexisting.get("batch_index", 1) != batch_idx:
                            break
                        f_orig = fexisting["event"].get("indice_original")
                        if isinstance(f_orig, int) and f_orig > original_index:
                            filt_pos = fidx
                            fidx -= 1
                            continue
                        break
                if filt_pos is not None:
                    self._filtered_rows.insert(filt_pos, entry)
                else:
                    self._filtered_rows.append(entry)

            self._record_filter_event(event, bucket=bucket)
            if self._batch_draining:
                self._filter_dirty = True
                self._viewport_dirty = True
            else:
                self._refresh_filter_bar()
                if (len(self._filtered_rows) <= self._active_pool_cap
                        or (filt_pos is not None
                            and filt_pos < self._virtual_start_index + self._active_pool_cap)):
                    self._sync_virtual_viewport()
                else:
                    self._viewport_dirty = True
                    self._schedule_viewport_flush()

            if (self.selected_event is not None
                    and same_logical_event(event, self.selected_event)):
                self.selected_event = event
                if self._batch_draining:
                    self._hero_dirty = True
                else:
                    self._render_hero()
                    self._update_reveal()
            if follow and want_visible:
                self._schedule_table_scroll()
            return entry

        def _clear_hover(self) -> None:
            prev = self._hovered_entry
            self._hovered_entry = None
            if prev is not None and prev.get("hover"):
                prev["hover"] = False
                if prev.get("frame") is not None:
                    self._paint_row(prev)

        def _sync_hover_to_pointer(self) -> None:
            try:
                if not self.winfo_ismapped():
                    self._clear_hover()
                    return
                px, py = self.winfo_pointerxy()
                target = self.winfo_containing(px, py)
            except Exception:
                self._clear_hover()
                return
            slot = getattr(target, "_row_slot", None) if target is not None else None
            entry = slot.get("entry") if slot is not None else None
            if entry is not None:
                self._hover_row(entry, True)
            else:
                self._clear_hover()

        def _on_table_canvas_leave(self, event: Any) -> None:
            try:
                target = self.table_canvas.winfo_containing(event.x_root, event.y_root)
                if target is not None:
                    path = str(target)
                    canvas_path = str(self.table_canvas)
                    if path == canvas_path or path.startswith(canvas_path + "."):
                        return
            except Exception:
                pass
            self._clear_hover()

        def _hover_row(self, entry: dict[str, Any], hovering: bool) -> None:
            if hovering:
                prev = self._hovered_entry
                if prev is not None and prev is not entry:
                    prev["hover"] = False
                    self._hovered_entry = None
                    if prev.get("frame") is not None:
                        self._paint_row(prev)
                self._hovered_entry = entry
                if entry["hover"]:
                    return
                entry["hover"] = True
                if entry.get("frame") is not None:
                    self._paint_row(entry)
                return
            if self._hovered_entry is entry:
                self._hovered_entry = None
            if not entry["hover"]:
                return
            entry["hover"] = False
            if entry.get("frame") is not None:
                self._paint_row(entry)

        def _row_leave(self, entry: dict[str, Any], event: Any) -> None:
            frame = entry.get("frame")
            if frame is not None:
                target = frame.winfo_containing(event.x_root, event.y_root)
                if target is not None:
                    path = str(target)
                    if path == str(frame) or path.startswith(str(frame) + "."):
                        return
            self._hover_row(entry, False)

        def _paint_row(self, entry: dict[str, Any], force: bool = False) -> None:
            slot = entry.get("slot")
            if slot is None or entry.get("frame") is None:
                return
            selected = same_logical_event(entry["event"], self.selected_event)
            entry["_was_selected"] = selected
            if selected:
                background = theme.mix(c("accent"), c("bg.surface"), 0.08)
                bar = c("accent")
            elif entry["hover"]:
                background, bar = c("bg.hover"), c("bg.hover")
            else:
                background, bar = c("bg.surface"), c("bg.surface")
            bg_str = str(background)
            paint_state = (theme.MODE, bg_str, str(bar))
            bg_changed = force or (slot.get("_row_bg_sig") != paint_state)
            entry["_paint_state"] = paint_state
            tk_call = self.tk.call
            if bg_changed:
                slot["_row_bg_sig"] = paint_state
                bar_str = str(bar)
                text_pri = str(c("text.primary"))
                entry["frame"]._fg_color = background
                tk_call(entry["frame"]._w, "configure", "-background", bg_str)
                entry["bar"]._fg_color = bar
                tk_call(entry["bar"]._w, "configure", "-background", bar_str)
                tk_call(entry["file_label"]._w, "configure",
                        "-background", bg_str, "-foreground", text_pri)
                for label in entry["plain"]:
                    label._fg_color = background
                    tk_call(label._w, "configure", "-background", bg_str)
            verdict_cell = entry["cells"]["verdict"]
            chip_sig = (
                verdict_cell.chip_text,
                verdict_cell.chip_tone,
                bg_str,
                theme.MODE,
            )
            if force or slot.get("_chip_sig") != chip_sig:
                slot["_chip_sig"] = chip_sig
                if isinstance(verdict_cell, _VerdictChip):
                    ink_t, bg_t, bd_t = CHIP_STYLES.get(
                        verdict_cell.chip_tone, CHIP_STYLES["unknown"]
                    )
                    pill_fg = c(bg_t)
                    img = self._chip_photo(
                        verdict_cell.chip_text, verdict_cell.chip_tone,
                        c(ink_t), pill_fg, c(bd_t), bg_str,
                    )
                    verdict_cell.set_pill_visual(pill_fg, background, img)
                else:
                    try:
                        verdict_cell.configure(bg_color=bg_str)
                    except Exception:
                        pass

        def _clear_table(self) -> None:
            self._hovered_entry = None
            for slot in self._row_slots:
                self._unbind_slot(slot)
                if slot["packed"]:
                    slot["frame"].pack_forget()
                    slot["packed"] = False
            self.table_rows.clear()
            self._filtered_rows.clear()
            self._virtual_start_index = 0
            self._reset_filter_counts()
            self._sync_virtual_viewport()
            self.table_canvas.yview_moveto(0.0)

        def _select(self, event: Event) -> None:
            if event is self.selected_event:
                self._clear_selection()
                return
            self.selected_event = event
            self._problems_expanded = False
            if self._viewport_dirty:
                self._sync_virtual_viewport()
            for slot in self._row_slots:
                entry = slot["entry"]
                if entry is not None and (
                    entry.get("_was_selected")
                    or same_logical_event(entry["event"], event)
                ):
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
            for slot in self._row_slots:
                entry = slot["entry"]
                if entry is not None and entry.get("_was_selected"):
                    self._paint_row(entry)
            self._render_hero()
            self._update_reveal()

        def _on_table_body(self, _event: Any = None) -> None:
            req_height = max(1, int(self.table_body.winfo_reqheight()))
            if self.table_body.winfo_height() > req_height:
                tkinter.Frame.configure(self.table_body, height=req_height)
            if len(self._filtered_rows) > self._active_pool_cap:
                cw = max(1, self._canvas_width or self.table_canvas.winfo_width())
                self._set_scrollregion(
                    (0, 0, cw, len(self._filtered_rows) * self.ROW_HEIGHT)
                )
                return
            bbox = self.table_canvas.bbox("all")
            if bbox:
                self._set_scrollregion(
                    (int(bbox[0]), int(bbox[1]), int(bbox[2]), int(min(bbox[3], req_height)))
                )

        def _on_table_canvas(self, event: Any) -> None:
            width = int(getattr(event, "width", 0) or 0)
            height = int(getattr(event, "height", 0) or 0)
            need_viewport = (
                height > 1
                and self._filtered_rows
                and self._viewport_pool_size() != self._active_pool_cap
            )
            if width <= 1 or width == self._canvas_width:
                if need_viewport:
                    self._sync_virtual_viewport()
                return
            if self._canvas_width == 0:
                self._canvas_width = width
                self.table_canvas.itemconfigure(self._table_window, width=width)
                if need_viewport:
                    self._sync_virtual_viewport()
                else:
                    self._on_table_body()
                return
            self._pending_canvas_width = width
            if self._canvas_resize_after is None:
                self._canvas_resize_after = self.after(16, self._apply_canvas_width)

        def _apply_canvas_width(self) -> None:
            self._canvas_resize_after = None
            width = self._pending_canvas_width
            if width <= 1:
                return
            changed_w = width != self._canvas_width
            if changed_w:
                self._canvas_width = width
            try:
                if changed_w:
                    self.table_canvas.itemconfigure(self._table_window, width=width)
                if (self._filtered_rows
                        and self._viewport_pool_size() != self._active_pool_cap):
                    self._sync_virtual_viewport()
                elif changed_w:
                    self._on_table_body()
            except Exception:
                pass

        def _on_table_wheel(self, event: Any) -> str:
            if self.table_canvas.yview() != (0.0, 1.0):
                step = -1 if getattr(event, "delta", 0) > 0 else 1
                self.table_canvas.yview_scroll(step * 3, "units")
                self._sync_virtual_viewport()
                self._sync_hover_to_pointer()
            return "break"

        def _table_at_bottom(self) -> bool:
            try:
                return self.table_canvas.yview()[1] >= 0.999
            except Exception:
                return True

        def _schedule_table_scroll(self) -> None:
            """One pending scroll per batch, never one per row."""
            if self._scroll_scheduled:
                return
            self._scroll_scheduled = True
            self.after_idle(self._scroll_table_to_end)

        def _scroll_table_to_end(self) -> None:
            if not self._scroll_scheduled:
                return
            self._scroll_scheduled = False
            try:
                if self._viewport_dirty:
                    self._sync_virtual_viewport()
                else:
                    self._on_table_body()
                self.table_canvas.yview_moveto(1.0)
                self._sync_virtual_viewport()
            except Exception:
                pass

        # ── chips ───────────────────────────────────────────────────────────
        def _chip_photo(self, text: str, tone: str, ink_col: str,
                        pill_bg: str, pill_border: str, row_bg: str) -> Any:
            """Renders and caches a 3x-supersampled anti-aliased verdict pill."""
            scaling = ctk.ScalingTracker.get_widget_scaling(self)
            key = (text, tone, str(row_bg), scaling, theme.MODE)
            if key in self._chip_images:
                return self._chip_images[key]
            photo = None
            try:
                from PIL import Image, ImageDraw, ImageTk
                ss = 3
                pad_l = max(1, round(theme.SPACE["xs"] * scaling))
                pad_r = pad_l
                gap = max(1, round(6 * scaling))
                dot_d = max(2, round(6 * scaling))
                height = max(12, round(24 * scaling))
                font_px = max(8, round(11 * scaling * ss))
                pil_font = _get_pil_chip_font(font_px)
                dummy = ImageDraw.Draw(Image.new("RGB", (4, 4)))
                bbox = dummy.textbbox((0, 0), text, font=pil_font)
                text_w = max(1, math.ceil((bbox[2] - bbox[0]) / ss)) if "math" in globals() else max(1, (bbox[2] - bbox[0] + ss - 1) // ss)
                width = pad_l + dot_d + gap + text_w + pad_r
                hi_w, hi_h = width * ss, height * ss
                canvas = Image.new("RGB", (hi_w, hi_h), str(row_bg))
                draw = ImageDraw.Draw(canvas)
                radius = max(2, round(theme.RADIUS["sm"] * scaling * ss))
                border_w = max(1, round(1 * scaling * ss))
                draw.rounded_rectangle(
                    (0, 0, hi_w - 1, hi_h - 1),
                    radius=radius, fill=str(pill_bg),
                    outline=str(pill_border), width=border_w,
                )
                dot_x = pad_l * ss
                dot_size = dot_d * ss
                dot_y = (hi_h - dot_size) // 2
                draw.ellipse(
                    (dot_x, dot_y, dot_x + dot_size, dot_y + dot_size),
                    fill=str(ink_col),
                )
                text_x = (pad_l + dot_d + gap) * ss
                text_h = bbox[3] - bbox[1]
                text_y = (hi_h - text_h) // 2 - bbox[1]
                draw.text((text_x, text_y), text, fill=str(ink_col), font=pil_font)
                smooth = canvas.resize((width, height), Image.Resampling.LANCZOS)
                photo = ImageTk.PhotoImage(smooth, master=self)
            except Exception:
                photo = None
            self._chip_images[key] = photo
            return photo

        def _row_chip(self, parent: Any, text: str, tone: str,
                      row_bg: str) -> _VerdictChip:
            """Lightweight single-widget verdict chip for streaming table rows."""
            ink_t, bg_t, bd_t = CHIP_STYLES.get(tone, CHIP_STYLES["unknown"])
            pill_fg = c(bg_t)
            photo = self._chip_photo(
                text, tone, c(ink_t), pill_fg, c(bd_t), str(row_bg),
            )
            return _VerdictChip(parent, text, tone, pill_fg, row_bg, image=photo)

        def _chip(self, parent: Any, text: str, tone: str) -> Any:
            """24 px chip of DESIGN.md 7.5: dot + label, never colour alone."""
            ink, background, border = CHIP_STYLES.get(tone, CHIP_STYLES["unknown"])
            chip = ctk.CTkFrame(parent, fg_color=c(background),
                                corner_radius=theme.RADIUS["sm"],
                                border_width=1, border_color=c(border))
            dot = ctk.CTkFrame(chip, width=6, height=6, corner_radius=3,
                               fg_color=c(ink))
            dot.grid(row=0, column=0, padx=(theme.SPACE["xs"], 6), pady=9)
            label = ctk.CTkLabel(chip, text=text, font=theme.font(11, 600),
                                 text_color=c(ink))
            label.grid(row=0, column=1, padx=(0, theme.SPACE["xs"]))
            chip._chip_parts = (dot, label, tone)
            return chip

        def _recolour_chip(self, chip: Any, row_bg: str,
                           palette: dict[str, tuple[Any, Any, Any]]) -> None:
            """Fast direct retheme of a verdict chip without tree introspection."""
            if isinstance(chip, _VerdictChip):
                colours = palette.get(chip.chip_tone)
                if colours is None:
                    ink_t, bg_t, bd_t = CHIP_STYLES.get(
                        chip.chip_tone, CHIP_STYLES["unknown"]
                    )
                    colours = (c(ink_t), c(bg_t), c(bd_t))
                    palette[chip.chip_tone] = colours
                ink_col, bg_col, bd_col = colours
                img = self._chip_photo(
                    chip.chip_text, chip.chip_tone,
                    ink_col, bg_col, bd_col, str(row_bg),
                )
                chip.set_pill_visual(bg_col, row_bg, img)
                return
            parts = getattr(chip, "_chip_parts", None)
            if parts is None:
                self._recolour_widget(chip)
                return
            dot, label, tone = parts
            colours = palette.get(tone)
            if colours is None:
                ink_t, bg_t, bd_t = CHIP_STYLES.get(tone, CHIP_STYLES["unknown"])
                colours = (c(ink_t), c(bg_t), c(bd_t))
                palette[tone] = colours
            ink_col, bg_col, bd_col = colours
            bg_hex = str(bg_col)
            chip.configure(fg_color=bg_col, border_color=bd_col, bg_color=str(row_bg))
            dot.configure(fg_color=ink_col, bg_color=bg_hex)
            label.configure(text_color=ink_col, bg_color=bg_hex)

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
            if self._batch_total_count > 1 and total > 0:
                album_num = self._batch_current_index
                text = (
                    f"{album_num} album" + ("" if album_num == 1 else "s")
                    + f" · {text}"
                )
            kinds = [verdict for verdict in counts if counts[verdict]]
            if total == 0:
                headline = "ANALYZING" if self._is_busy() else "NO RESULTS"
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
            eyebrow = self._display_file_name(event, values)
            self._show_detail(True)
            self._set_hero_headline(
                eyebrow,
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
            self._show_spectrogram(spectrogram_path(event), async_load=True)
            self._schedule_hero_layout()

        def _show_detail(self, visible: bool) -> None:
            """Shows or hides the inspector parts of the hero (7.8, phase 3)."""
            target_mode = HeroMode.INSPECTOR if visible else HeroMode.ALBUM
            if self._detail_visible is visible and self.hero_mode is target_mode:
                return
            self._detail_visible = visible
            self.hero_mode = target_mode
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

        def _create_problem_pool_entry(self) -> dict[str, Any]:
            """Pre-allocated reusable issue/note row inside `hero_problems`."""
            row = ctk.CTkFrame(self.hero_problems, fg_color="transparent")
            row.grid_columnconfigure(1, weight=1)
            dot = ctk.CTkFrame(row, width=6, height=6, corner_radius=3,
                               fg_color=c("warn"))
            dot.grid(row=0, column=0, padx=(0, 8), pady=(7, 0), sticky="nw")
            label = ctk.CTkLabel(
                row, text="", font=theme.font(11),
                text_color=c("text.secondary"), wraplength=396,
                justify="left", anchor="w",
            )
            label.grid(row=0, column=1, sticky="ew")
            row.grid_remove()
            return {
                "frame": row,
                "dot": dot,
                "label": label,
                "visible": False,
                "row_idx": -1,
                "tone": "warn",
                "text": "",
                "wrap": 396,
            }

        def _ensure_problem_pool(self, needed: int) -> None:
            while len(self._problem_rows_pool) < needed:
                self._problem_rows_pool.append(self._create_problem_pool_entry())

        def _render_problems(self, event: Event | None) -> None:
            """Reuses pooled issue/note rows in `hero_problems` without destroying widgets."""
            available = max(220, self._usable_width(self.hero_problems, 420))
            wrap_px = max(160, available - 24)
            self._last_problem_wrap = wrap_px
            if event is None:
                if self._no_anomaly_label is not None:
                    self._no_anomaly_label.configure(text="")
                    self._no_anomaly_chip.grid_remove()
                for pool_item in self._problem_rows_pool:
                    if pool_item["visible"]:
                        pool_item["visible"] = False
                        pool_item["row_idx"] = -1
                        pool_item["text"] = ""
                        pool_item["label"].configure(text="")
                        pool_item["frame"].grid_remove()
                if self._problem_toggle_btn is not None:
                    self._problem_toggle_btn.configure(text="")
                    self._problem_toggle_btn.grid_remove()
                return

            problems = [str(item) for item in (event.get("problemas") or [])]
            notes = [str(item) for item in (event.get("notes") or [])]
            if event.get("error"):
                problems = [str(event["error"])]
                notes = []
            expanded = self._problems_expanded
            if not problems:
                if self._no_anomaly_label.cget("text") != "No anomalies detected":
                    self._no_anomaly_label.configure(text="No anomalies detected")
                self._no_anomaly_chip.grid(
                    row=0, column=0, sticky="w", pady=(0, 4 if notes else 0)
                )
                items = [(note, "accent") for note in notes]
                row_offset = 1
            else:
                if self._no_anomaly_label.cget("text"):
                    self._no_anomaly_label.configure(text="")
                self._no_anomaly_chip.grid_remove()
                tone = ("danger" if event.get("error")
                        else "bad" if event.get("veredicto") == "PROBABLE UPSCALE"
                        else "warn")
                items = [(item, tone) for item in problems] + [
                    (item, "accent") for item in notes
                ]
                row_offset = 0

            visible = items if expanded else items[:self.MAX_VISIBLE_PROBLEMS]
            self._ensure_problem_pool(len(visible))
            for idx, (item_text, item_tone) in enumerate(visible):
                pool_item = self._problem_rows_pool[idx]
                target_row = idx + row_offset
                if not pool_item["visible"] or pool_item["row_idx"] != target_row:
                    pool_item["frame"].grid(
                        row=target_row, column=0, sticky="ew", pady=(0, 4)
                    )
                    pool_item["visible"] = True
                    pool_item["row_idx"] = target_row
                if pool_item["tone"] != item_tone:
                    pool_item["tone"] = item_tone
                    pool_item["dot"].configure(fg_color=c(item_tone))
                lbl_updates: dict[str, Any] = {}
                if pool_item["text"] != item_text:
                    pool_item["text"] = item_text
                    lbl_updates["text"] = item_text
                if pool_item["wrap"] != wrap_px:
                    pool_item["wrap"] = wrap_px
                    lbl_updates["wraplength"] = wrap_px
                if lbl_updates:
                    pool_item["label"].configure(**lbl_updates)

            for pool_item in self._problem_rows_pool[len(visible):]:
                if pool_item["visible"]:
                    pool_item["visible"] = False
                    pool_item["row_idx"] = -1
                    pool_item["text"] = ""
                    pool_item["label"].configure(text="")
                    pool_item["frame"].grid_remove()

            if len(items) > self.MAX_VISIBLE_PROBLEMS:
                toggle_label = "Show less" if expanded else f"Show all ({len(items)})"
                toggle_row = len(visible) + row_offset
                padx_left = 14
                self._problem_toggle_btn.configure(text=toggle_label)
                self._problem_toggle_btn.grid(
                    row=toggle_row, column=0, sticky="w",
                    padx=(padx_left, 0), pady=(2, 0),
                )
            else:
                if self._problem_toggle_btn.cget("text"):
                    self._problem_toggle_btn.configure(text="")
                self._problem_toggle_btn.grid_remove()

        def _toggle_problems(self) -> None:
            self._problems_expanded = not self._problems_expanded
            if self.selected_event is not None:
                self._render_problems(self.selected_event)
                self._schedule_hero_layout()

        def _show_spectrogram(self, path: str, async_load: bool = False) -> None:
            """Image preview with RAM cache hit, async loading state, or missing note."""
            self._spectrogram_path_value = path
            if not path:
                self.spectrogram_state = SpectrogramState.NONE
                self.spectrogram_label.configure(image=None, text="")
                self.spectrogram_label.grid_remove()
                self.spectrogram_placeholder.grid()
                self.spectrogram_note.configure(text="No spectrogram")
                self.spectrogram_hint.configure(text="Spectrogram not available")
                self._set_enabled(self.btn_open_spectrogram, False)
                return

            image = self._spectrogram_image_cached(path)
            if image is not None:
                self.spectrogram_state = SpectrogramState.READY
                self.spectrogram_label.configure(image=image, text="")
                self.spectrogram_label.grid()
                self.spectrogram_placeholder.grid_remove()
                self._set_enabled(self.btn_open_spectrogram, True)
                return

            if path in self._spec_bad_paths or not os.path.exists(path):
                self.spectrogram_state = SpectrogramState.MISSING
                self.spectrogram_label.configure(image=None, text="")
                self.spectrogram_label.grid_remove()
                self.spectrogram_placeholder.grid()
                self.spectrogram_note.configure(text="Image not available")
                self.spectrogram_hint.configure(text="The file was moved")
                self._set_enabled(self.btn_open_spectrogram, False)
                return

            if async_load:
                self.spectrogram_state = SpectrogramState.LOADING
                self.spectrogram_label.configure(image=None, text="")
                self.spectrogram_label.grid_remove()
                self.spectrogram_placeholder.grid()
                self.spectrogram_note.configure(text="Loading spectrogram…")
                self.spectrogram_hint.configure(text="Rendering preview")
                self._set_enabled(self.btn_open_spectrogram, False)
                self._enqueue_spectrogram(path, priority=True)
                self._schedule_spec_poll()
                return

            decoded = _decode_spectrogram_pil(
                path, self.SPECTROGRAM_WIDTH, self.SPECTROGRAM_HEIGHT,
            )
            with self._spec_lock:
                if decoded is not None:
                    while len(self._pil_spectrograms) >= self.MAX_PIL_SPECTROGRAM_CACHE:
                        self._pil_spectrograms.popitem(last=False)
                    self._pil_spectrograms[path] = decoded
                else:
                    self._spec_bad_paths.add(path)
            image = self._spectrogram_image_cached(path)
            if image is not None:
                self.spectrogram_state = SpectrogramState.READY
                self.spectrogram_label.configure(image=image, text="")
                self.spectrogram_label.grid()
                self.spectrogram_placeholder.grid_remove()
                self._set_enabled(self.btn_open_spectrogram, True)
            else:
                self.spectrogram_state = SpectrogramState.MISSING
                self.spectrogram_label.configure(image=None, text="")
                self.spectrogram_label.grid_remove()
                self.spectrogram_placeholder.grid()
                self.spectrogram_note.configure(text="Image not available")
                self.spectrogram_hint.configure(text="The file was moved")
                self._set_enabled(self.btn_open_spectrogram, False)

        def _enqueue_spectrogram(self, path: str, priority: bool = False) -> None:
            """Queues PNG decoding onto the background worker (LIFO priority on click)."""
            if not path or path in self._spectrograms:
                return
            with self._spec_cond:
                if (self._spec_stop or path in self._pil_spectrograms
                        or path in self._spec_bad_paths):
                    return
                if priority:
                    self._spec_priority.add(path)
                    if path in self._spec_queued:
                        try:
                            self._spec_deque.remove(path)
                        except ValueError:
                            pass
                        self._spec_deque.appendleft(path)
                    elif path not in self._spec_inflight:
                        self._spec_deque.appendleft(path)
                        self._spec_queued.add(path)
                else:
                    if (path in self._spec_inflight or path in self._spec_queued
                            or len(self._spec_deque) >= self.MAX_PIL_SPECTROGRAM_CACHE):
                        return
                    self._spec_deque.append(path)
                    self._spec_queued.add(path)
                if self._spec_worker is None or not self._spec_worker.is_alive():
                    self._spec_worker = threading.Thread(
                        target=self._spectrogram_worker_loop,
                        daemon=True, name="spectrogram-prewarm",
                    )
                    self._spec_worker.start()
                self._spec_cond.notify()

        def _prewarm_spectrogram(self, path: str) -> None:
            """Queues background PIL decoding so row selection hits RAM instead of disk."""
            self._enqueue_spectrogram(path, priority=False)

        def _spectrogram_worker_loop(self) -> None:
            while True:
                with self._spec_cond:
                    while not self._spec_deque and not self._spec_stop:
                        self._spec_cond.wait()
                    if self._spec_stop:
                        return
                    item = self._spec_deque.popleft()
                    self._spec_queued.discard(item)
                    was_priority = item in self._spec_priority
                    self._spec_inflight.add(item)
                decoded = _decode_spectrogram_pil(
                    item, self.SPECTROGRAM_WIDTH, self.SPECTROGRAM_HEIGHT,
                )
                with self._spec_lock:
                    self._spec_inflight.discard(item)
                    notify_ui = was_priority or (item in self._spec_priority)
                    self._spec_priority.discard(item)
                    if decoded is not None:
                        while len(self._pil_spectrograms) >= self.MAX_PIL_SPECTROGRAM_CACHE:
                            self._pil_spectrograms.popitem(last=False)
                        self._pil_spectrograms[item] = decoded
                    else:
                        self._spec_bad_paths.add(item)
                if notify_ui and not self._spec_stop:
                    try:
                        self._spec_ready_queue.put_nowait(item)
                    except Exception:
                        pass

        def _schedule_spec_poll(self) -> None:
            """Schedules main-thread polling for async spectrogram completion."""
            if self._spec_poll_scheduled or self._spec_stop:
                return
            self._spec_poll_scheduled = True
            self.after(16, self._poll_async_spectrogram)

        def _poll_async_spectrogram(self) -> None:
            """Checks background decoded thumbnails on the main UI thread only."""
            self._spec_poll_scheduled = False
            try:
                if not self.winfo_exists():
                    return
            except Exception:
                return
            ready_paths: list[str] = []
            while True:
                try:
                    ready_paths.append(self._spec_ready_queue.get_nowait())
                except queue.Empty:
                    break
            if self.spectrogram_state is SpectrogramState.LOADING:
                current_path = spectrogram_path(self.selected_event) if self.selected_event else ""
                if not current_path:
                    return
                with self._spec_lock:
                    done = (
                        current_path in self._pil_spectrograms
                        or current_path in self._spec_bad_paths
                    )
                if done or current_path in ready_paths:
                    self._on_async_spectrogram_ready(current_path)
                else:
                    self._schedule_spec_poll()

        def _on_async_spectrogram_ready(self, path: str) -> None:
            """Paints the decoded thumbnail only if this track is still selected."""
            try:
                if not self.winfo_exists():
                    return
            except Exception:
                return
            if (self.selected_event is None
                    or spectrogram_path(self.selected_event) != path):
                return
            self._show_spectrogram(path, async_load=False)

        def _spectrogram_image_cached(self, path: str) -> Any:
            """RAM-only spectrogram lookup; never blocks the UI thread on disk I/O."""
            if not path:
                return None
            if path in self._spectrograms:
                self._spectrograms.move_to_end(path)
                return self._spectrograms[path]
            decoded = None
            with self._spec_lock:
                if path in self._pil_spectrograms:
                    self._pil_spectrograms.move_to_end(path)
                    decoded = self._pil_spectrograms[path]
            if decoded is None:
                return None
            image = None
            try:
                picture, display = decoded
                image = ctk.CTkImage(light_image=picture, dark_image=picture,
                                     size=display)
            except Exception:
                image = None
            if image is not None:
                while len(self._spectrograms) >= self.MAX_SPECTROGRAM_CACHE:
                    self._spectrograms.popitem(last=False)
                self._spectrograms[path] = image
            return image

        def _spectrogram_image(self, path: str) -> Any:
            """Compatibility helper returning a cached or decoded CTkImage."""
            image = self._spectrogram_image_cached(path)
            if image is not None or not path or not os.path.exists(path):
                return image
            decoded = _decode_spectrogram_pil(
                path, self.SPECTROGRAM_WIDTH, self.SPECTROGRAM_HEIGHT,
            )
            if decoded is None:
                return None
            with self._spec_lock:
                while len(self._pil_spectrograms) >= self.MAX_PIL_SPECTROGRAM_CACHE:
                    self._pil_spectrograms.popitem(last=False)
                self._pil_spectrograms[path] = decoded
            return self._spectrogram_image_cached(path)

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
            if self._retheming:
                return
            self._refit_hero_texts()
            width = self._usable_width(self.hero_left, 520) - 8
            self._reflow_facts(width)
            self._reflow_evidence(width)
            if self.selected_event is not None:
                new_wrap = max(
                    160,
                    max(220, self._usable_width(self.hero_problems, 420)) - 24,
                )
                if abs(new_wrap - self._last_problem_wrap) >= 4:
                    self._last_problem_wrap = new_wrap
                    for pool_item in self._problem_rows_pool:
                        if pool_item["visible"]:
                            pool_item["label"].configure(wraplength=new_wrap)
                            pool_item["wrap"] = new_wrap

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
            self._ring_highlight(self.switch_autoscroll, "bg.surface")
            self._toggle_on_space(self.switch_autoscroll, self.var_autoscroll)
            self.btn_clear_log = self._button(toolbar, "ghost", "Clear",
                                              self._clear_log, icon="trash",
                                              height=28,
                                              background="bg.surface")
            self.btn_clear_log.grid(row=0, column=2, padx=(theme.SPACE["xs"], 0))
            self.btn_activity_close = self._button(
                toolbar, "icon", "", self._toggle_activity,
                icon="chevron-down", height=24, width=24,
                background="bg.surface")
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
                self._scroll_log()
            else:
                self.activity_panel.grid_remove()
                self.activity_bar.grid()
                self.activity_tooltip.text = "Expand activity"

        def _toggle_activity(self) -> None:
            self._set_activity(not self.activity_expanded)

        def _clear_log(self) -> None:
            self._pending_log_chunks.clear()
            self._pending_log_scroll = False
            self.log.configure(state="normal")
            self.log.delete("1.0", "end")
            self.log.configure(state="disabled")
            self._log_entries.clear()
            self._last_log_line = ""
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
                self._font_max_char[key] = max(1, measure.measure("W"))
            return measure

        def _text_width(self, font: Any, text: str) -> int:
            """Width of `text` in on-screen pixels at the current scaling."""
            return self._measure_font(font).measure(text)

        def _scaled_font(self, font: Any) -> tuple[Any, ...]:
            """Tk font tuple at the current scaling, for plain Tk widgets."""
            scaling = ctk.ScalingTracker.get_widget_scaling(self)
            key = (font.cget("family"), int(font.cget("size")),
                   str(font.cget("weight")), scaling)
            cached = self._scaled_fonts.get(key)
            if cached is None:
                cached = font.create_scaled_tuple(scaling)
                self._scaled_fonts[key] = cached
            return cached

        def _fit(self, text: str, font: Any, pixels: int,
                 from_left: bool = False) -> str:
            """Truncates with an ellipsis so a row or the hero never overflows."""
            if not text or pixels <= 0:
                return text
            scaling = ctk.ScalingTracker.get_widget_scaling(self)
            key = (font.cget("family"), int(font.cget("size")),
                   str(font.cget("weight")), scaling)
            measure = self._measures.get(key)
            if measure is None:
                measure = self._measure_font(font)
            if len(text) * self._font_max_char.get(key, 24) <= pixels:
                return text
            if measure.measure(text) <= pixels:
                return text
            ellipsis = "…"
            if measure.measure(ellipsis) > pixels:
                return ""
            if from_left:
                low, high = 0, len(text)
                while low < high:
                    mid = (low + high) // 2
                    if measure.measure(ellipsis + text[mid:]) > pixels:
                        low = mid + 1
                    else:
                        high = mid
                return ellipsis + text[low:]
            low, high = 0, len(text)
            while low < high:
                mid = (low + high + 1) // 2
                if measure.measure(text[:mid] + ellipsis) > pixels:
                    high = mid - 1
                else:
                    low = mid
            return text[:low] + ellipsis

        def _refit_file_name(self, label: Any, event: Any) -> None:
            """Truncates with an ellipsis inside the file cell of one row."""
            if event.width <= 8:            # not mapped yet: keep the whole name
                return
            if abs(event.width - label.fitted_width) < 2:
                return
            label.fitted_width = event.width
            shown = self._fit(label.full_text, label.ui_font, event.width - 4)
            label.configure(text=shown)
            row_slot = getattr(label, "_row_slot", None)
            if row_slot is not None:
                row_slot["_file_text"] = shown
            if shown != label.full_text:
                if label.tooltip is None:
                    label.tooltip = _Tooltip(label, label.full_text)
                else:
                    label.tooltip.text = label.full_text
            elif label.tooltip is not None:
                label.tooltip.text = ""

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
            self.surface_mode = SurfaceMode.RESULTS if visible else SurfaceMode.EMPTY
            if visible:
                self.empty_state.grid_remove()
                self.results.grid()
            else:
                self.results.grid_remove()
                self.empty_state.grid()

        # ── view helpers ────────────────────────────────────────────────────
        def _bulk_insert_log(self, chunks: list[str]) -> None:
            """Inserts alternating (text, tag) pairs into `self.log` in one Tcl call."""
            if not chunks:
                return
            textbox = getattr(self.log, "_textbox", None)
            if textbox is not None:
                textbox.insert("end", *chunks)
            else:
                for idx in range(0, len(chunks), 2):
                    self.log.insert("end", chunks[idx], chunks[idx + 1])

        def _flush_pending_logs(self) -> None:
            """Flushes buffered batch log lines into the textbox once per UI tick."""
            if not self._pending_log_chunks:
                if self._pending_log_scroll:
                    self._pending_log_scroll = False
                    if self.activity_expanded:
                        self._scroll_log()
                return
            chunks = self._pending_log_chunks
            self._pending_log_chunks = []
            need_scroll = self._pending_log_scroll
            self._pending_log_scroll = False
            self.log.configure(state="normal")
            self._bulk_insert_log(chunks)
            self._trim_log()
            self.log.configure(state="disabled")
            self._refresh_activity_preview()
            if need_scroll and self.activity_expanded:
                self._scroll_log()

        def _log(self, text: str, label: str = "info",
                 scroll: bool = True,
                 result_event: Event | None = None) -> None:
            """One log line: timestamp in tertiary, text in the level colour."""
            stamp = time.strftime("%H:%M:%S")
            # "aviso" is the legacy engine callback label mapped to "warn"
            # so warning lines consistently pick up the amber tag colour.
            label = LOG_LEVEL_TAGS.get(label, label)
            self._log_entries.append((stamp, text, label, result_event))
            if len(self._log_entries) > MAX_LINES_LOG:
                del self._log_entries[:-MAX_LINES_LOG]
            self._last_log_line = text
            if self._batch_draining:
                self._pending_log_chunks.extend((f"{stamp}  ", "time", f"{text}\n", label))
                if scroll:
                    self._pending_log_scroll = True
                return
            self.log.configure(state="normal")
            self._bulk_insert_log([f"{stamp}  ", "time", f"{text}\n", label])
            self._trim_log()
            self.log.configure(state="disabled")
            self._refresh_activity_preview()
            if scroll:
                self._scroll_log()

        def _order_batch_results(self) -> None:
            """Ensures `self.events` and the activity log list tracks in album order."""
            self._flush_pending_logs()
            if len(self.table_rows) <= 1:
                return
            ordered_events = [row["event"] for row in self.table_rows]
            if list(self.events) != ordered_events:
                self.events.clear()
                self.events.extend(ordered_events)

            batch_idx = self._batch_current_index
            batch_events = [
                row["event"]
                for row in self.table_rows
                if row.get("batch_index", 1) == batch_idx
            ]
            batch_ids = {id(ev) for ev in batch_events}
            seen_order: list[int] = []
            blocks: dict[int, list[tuple[str, str, str, Event | None]]] = {}
            for entry in self._log_entries:
                ev = entry[3]
                if ev is not None and id(ev) in batch_ids:
                    ev_id = id(ev)
                    if ev_id not in blocks:
                        seen_order.append(ev_id)
                        blocks[ev_id] = []
                    blocks[ev_id].append(entry)

            target_order = [id(ev) for ev in batch_events if id(ev) in blocks]
            if len(target_order) <= 1 or seen_order == target_order:
                return

            ordered_result_lines: list[tuple[str, str, str, Event | None]] = []
            for ev_id in target_order:
                ordered_result_lines.extend(blocks[ev_id])

            rebuilt: list[tuple[str, str, str, Event | None]] = []
            inserted = False
            for entry in self._log_entries:
                ev = entry[3]
                if ev is not None and id(ev) in batch_ids:
                    if not inserted:
                        rebuilt.extend(ordered_result_lines)
                        inserted = True
                else:
                    rebuilt.append(entry)

            self._log_entries = rebuilt[-MAX_LINES_LOG:]
            flat_chunks: list[str] = []
            for stamp, line_text, tag_label, _ in self._log_entries:
                flat_chunks.extend((f"{stamp}  ", "time", f"{line_text}\n", tag_label))
            self.log.configure(state="normal")
            self.log.delete("1.0", "end")
            self._bulk_insert_log(flat_chunks)
            self.log.configure(state="disabled")
            if self._log_entries:
                self._last_log_line = self._log_entries[-1][1]
                self._refresh_activity_preview()

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

        def _choose_output_dir(self) -> None:
            from tkinter import filedialog
            initial = self._effective_output_dir() or self.var_path.get() or os.path.expanduser("~")
            chosen = filedialog.askdirectory(
                title="Choose PDF output folder",
                initialdir=initial if os.path.isdir(initial) else None,
            )
            if chosen:
                self.var_pdf_dest_mode.set("custom")
                self.var_output_dir.set(chosen)
                self._change_pdf_dest_mode()
                self._log(f"PDF output folder: {chosen}")

        def _clear_output_dir(self) -> None:
            self.var_output_dir.set("")
            self.var_pdf_dest_mode.set("album")
            self._change_pdf_dest_mode()
            self._log("PDF destination set to each album folder.")

        def _on_output_dir_var_changed(self, *_args: Any) -> None:
            if (self.var_output_dir.get().strip()
                    and self.var_pdf_dest_mode.get() != "custom"):
                self.var_pdf_dest_mode.set("custom")
                self._change_pdf_dest_mode()

        def _change_pdf_dest_mode(self) -> None:
            if not hasattr(self, "pdf_custom_dir_row"):
                return
            is_custom = self.var_pdf_dest_mode.get() == "custom"
            if is_custom:
                self.pdf_custom_dir_row.grid()
                self.label_pdf_dest_hint.configure(
                    text="Writes <Album>_flac_verifier_report.pdf in custom folder"
                )
            else:
                self.pdf_custom_dir_row.grid_remove()
                self.label_pdf_dest_hint.configure(
                    text="Writes flac_verifier_report.pdf inside each album"
                )
            allow_custom = (
                bool(self.var_pdf.get()) and is_custom and not self._is_busy()
            )
            self.input_output_dir.configure(
                state="normal" if allow_custom else "disabled"
            )
            self._set_enabled(self.btn_output_dir, allow_custom)
            self._set_enabled(self.btn_clear_output_dir, allow_custom)

        def _effective_output_dir(self) -> str | None:
            if getattr(self, "var_pdf_dest_mode", None) is not None:
                if self.var_pdf_dest_mode.get() != "custom":
                    return None
            raw = self.var_output_dir.get().strip().strip('"')
            return raw or None

        def _change_pdf(self) -> None:
            enabled = bool(self.var_pdf.get())
            if enabled:
                self.pdf_output_row.grid()
            else:
                self.pdf_output_row.grid_remove()
            allow_mode = enabled and not self._is_busy()
            radio_state = "normal" if allow_mode else "disabled"
            self.radio_pdf_album.configure(state=radio_state)
            self.radio_pdf_custom.configure(state=radio_state)
            self._change_pdf_dest_mode()

        def _change_parallel(self) -> None:
            active = bool(self.var_parallel.get())
            self.option_workers.configure(state="normal" if active else "disabled")

        def _workers(self) -> int:
            if not self.var_parallel.get():
                return 1                              # sequential
            chosen = self.option_workers.get()
            return int(chosen) if chosen.isdigit() else 0   # 0 = automatic

        def _set_run_state(self, target: RunState | str) -> RunState:
            """Applies a guarded transition to `self.run_state`."""
            self.run_state = transition_run_state(self.run_state, target)
            return self.run_state

        def _is_busy(self) -> bool:
            """True while an analysis batch is active or draining its final events."""
            return self.manager.active or self.run_state == RunState.RUNNING

        def ui_snapshot(self) -> dict[str, Any]:
            """Structured snapshot of the window's explicit UI and batch state."""
            selected = self.selected_event or {}
            return {
                "run_state": self.run_state.value,
                "active_run_id": self.active_run_id,
                "surface_mode": self.surface_mode.value,
                "hero_mode": self.hero_mode.value,
                "spectrogram_state": self.spectrogram_state.value,
                "busy": self._is_busy(),
                "total": self.total,
                "received": self.recibidos,
                "selected_file": selected.get("archivo"),
                "pdf_ready": bool(self.pdf_path and os.path.exists(self.pdf_path)),
                "queued_batches": len(self._batch_queue),
                "batch_total_count": self._batch_total_count,
                "batch_current_index": self._batch_current_index,
                "failed_batches": len(self._failed_batches),
                "active_filter": self.active_filter.value,
                "pdf_enabled": bool(self.var_pdf.get()),
                "pdf_dest_mode": self.var_pdf_dest_mode.get(),
            }

        def _update_state(self) -> None:
            """Paints the idle/running state: Analyze becomes Cancel."""
            active = self._is_busy()
            self._set_enabled(self.btn_analyze, not active)
            self._set_enabled(self.btn_cancel, active)
            self._show_action(self.btn_cancel if active else self.btn_analyze)
            for widget in (self.input, self.btn_browse,
                           self.check_pdf, self.parallel_switch, self.check_cache):
                widget.configure(state="disabled" if active else "normal")
            self._set_enabled(self.btn_folder, not active)
            self._set_enabled(self.btn_theme, not active)
            if active:
                self.radio_pdf_album.configure(state="disabled")
                self.radio_pdf_custom.configure(state="disabled")
                self.input_output_dir.configure(state="disabled")
                self._set_enabled(self.btn_output_dir, False)
                self._set_enabled(self.btn_clear_output_dir, False)
            else:
                self._change_pdf()
                self._change_parallel()
            there_is_pdf = bool(self.pdf_path and os.path.exists(self.pdf_path))
            self._set_enabled(self.btn_open_pdf, there_is_pdf)
            # Empty state until there is something to show (DESIGN.md 7.11).
            self._set_results_visible(active or bool(self.events))
            self._update_reveal()

        def _toggle_theme(self) -> None:
            """Switches the window between dark and light, live and in place."""
            if self._is_busy() or self._retheming:
                return
            target = "light" if theme.MODE == "dark" else "dark"
            saved = theme.save_preference(target)
            self._retheme(target)
            if not saved:
                self._log("Could not save the theme preference; "
                          "it will not be remembered.", "aviso")

        def _set_appearance(self, mode: str) -> None:
            """Updates the appearance mode without hiding or redrawing the window.

            On Windows `ctk.set_appearance_mode` withdraws the window to recolour
            the title bar (the hide/show is the theme flash) and notifies every
            existing widget (a redraw in the middle of the switch). The tracker is
            updated in place and the title bar is recoloured at the end of the
            retheme.
            """
            previous = self._deactivate_windows_window_header_manipulation
            self._deactivate_windows_window_header_manipulation = True
            try:
                tracker = getattr(ctk, "AppearanceModeTracker", None)
                if tracker is None:                 # older CustomTkinter
                    ctk.set_appearance_mode(mode)
                else:
                    tracker.appearance_mode_set_by = "user"
                    tracker.appearance_mode = 1 if mode == "dark" else 0
            finally:
                self._deactivate_windows_window_header_manipulation = previous

        def _apply_titlebar(self, mode: str) -> None:
            """Recolours the Windows title bar in place, without hide/show."""
            if not sys.platform.startswith("win"):
                return
            try:
                import ctypes
                hwnd = ctypes.windll.user32.GetParent(self.winfo_id())
                value = ctypes.c_int(1 if mode == "dark" else 0)
                for attribute in (20, 19):      # DWMWA_USE_IMMERSIVE_DARK_MODE
                    if ctypes.windll.dwmapi.DwmSetWindowAttribute(
                            hwnd, attribute, ctypes.byref(value),
                            ctypes.sizeof(value)) == 0:
                        break
                # SWP_FRAMECHANGED | NOMOVE | NOSIZE | NOZORDER
                ctypes.windll.user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0,
                                                  0x0020 | 0x0002 | 0x0001 | 0x0004)
            except Exception:
                pass

        def _retheme(self, mode: str) -> None:
            """Recolours every widget in place, without destroying a single one.

            Each CustomTkinter colour was given as a `_TokenColor`, so the
            widget itself remembers which token paints each property and the new
            value can be resolved from the new palette. Painting is frozen while
            the colours change and the whole window is redrawn once at the end,
            so the user sees the old theme and then the new one, never a mix. The
            plain Tk labels, canvases and text tags are recoloured explicitly.
            """
            if self._is_busy() or self._retheming:
                return
            self._retheming = True
            focused = self.focus_get()
            # The client area is covered by a snapshot for the duration of the
            # switch. It is a separate window, so the compositor shows either the
            # old snapshot or the already-painted new window, never a half-updated
            # surface (the tearing a screen capture can catch while the window is
            # being repainted).
            overlay = self._theme_overlay()
            frozen = freeze_redraw(self)
            try:
                theme.MODE = mode
                self._set_appearance(mode)
                self._icons.clear()
                self._tk_icons.clear()
                self._chip_images.clear()
                # The toggle is the same button: its icon and tooltip must show
                # the mode a click will switch to.
                name = "sun" if mode == "dark" else "moon"
                self.btn_theme.ui_spec["icon"] = (name, 16, "text.secondary")
                self.theme_tooltip.text = ("Switch to light theme" if mode == "dark"
                                           else "Switch to dark theme")
                self.configure(fg_color=c("bg.canvas"))

                # Plain Tk surfaces first: the CTk widgets painted over them
                # re-detect their background colour from the new values.
                surface_bg = c("bg.surface")
                self.table_canvas.configure(background=surface_bg)
                self.drop_zone.retheme()
                self.table_scroll.retheme()

                self._recolour_widget(self.shell)
                self._refresh_filter_bar(force=True)
                for widget, background in self._focus_rings:
                    tkinter.Frame.configure(widget,
                                            highlightbackground=c(background),
                                            highlightcolor=c("focus"))
                if self._viewport_dirty:
                    self._sync_virtual_viewport()
                self.table_body.configure(fg_color=surface_bg)
                sep_col = c("border.subtle")
                sep_str = str(sep_col)
                surf_str = str(surface_bg)
                text_pri = str(c("text.primary"))
                text_sec = c("text.secondary")
                text_ter = c("text.tertiary")
                tk_call = self.tk.call
                for slot in self._row_slots:
                    sep = slot["separator"]
                    sep._fg_color = sep_col
                    tk_call(sep._w, "configure", "-background", sep_str)
                    for plain_lbl in slot["plain"][:4]:
                        plain_lbl._text_color = c("text.secondary")
                        tk_call(plain_lbl._w, "configure", "-foreground", text_sec)
                    slot["_chip_sig"] = None
                    slot["_row_bg_sig"] = None
                    slot["_md5_state"] = ("retheme", None)
                    entry = slot.get("entry")
                    if entry is not None:
                        icon_spec = entry.get("icon_spec")
                        mark_lbl = slot["cells"]["md5"]
                        if icon_spec:
                            photo = self._tk_icon(
                                icon_spec[0], icon_spec[1], c(icon_spec[2])
                            )
                            mark_lbl._image_ref = photo
                            tk_call(
                                mark_lbl._w, "configure",
                                "-text", "", "-image", "" if photo is None else photo,
                            )
                            slot["_md5_state"] = ("icon", icon_spec)
                        else:
                            mark_lbl._text_color = c("text.tertiary")
                            tk_call(mark_lbl._w, "configure", "-foreground", text_ter)
                            slot["_md5_state"] = ("dash", None)
                        self._paint_row(entry, force=True)
                    else:
                        slot["frame"]._fg_color = surface_bg
                        tk_call(slot["frame"]._w, "configure", "-background", surf_str)
                        slot["bar"]._fg_color = surface_bg
                        tk_call(slot["bar"]._w, "configure", "-background", surf_str)
                        tk_call(slot["file_label"]._w, "configure",
                                "-background", surf_str, "-foreground", text_pri)
                        for plain_lbl in slot["plain"]:
                            plain_lbl._fg_color = surface_bg
                            tk_call(plain_lbl._w, "configure", "-background", surf_str)
                        slot["cells"]["md5"]._text_color = c("text.tertiary")
                        tk_call(slot["cells"]["md5"]._w, "configure",
                                "-foreground", text_ter)
                        verdict_cell = slot["cells"]["verdict"]
                        if isinstance(verdict_cell, _VerdictChip):
                            verdict_cell._fg_color = surface_bg
                            tk_call(verdict_cell._w, "configure", "-background", surf_str)
                if self._placeholder_icon_spec:
                    name, size, token = self._placeholder_icon_spec
                    self.spectrogram_icon.configure(
                        image=self._icon(name, size, c(token)))
                for level, token in LOG_TAGS.items():
                    self.log.tag_config(level, foreground=c(token))
                self.log.tag_config("time", foreground=c("text.tertiary"))
                # Enabled buttons already carry the new colours; the disabled
                # ones have a blended fill that has to be mixed again.
                for button in self._buttons:
                    if button.cget("state") == "disabled":
                        self._set_enabled(button, False)
                self.update_idletasks()
            finally:
                thaw_redraw(self, frozen)
                self._retheming = False
            # The title bar is recoloured after the redraw and before the
            # snapshot goes away, so the frame and the client change together.
            self._apply_titlebar(mode)
            self._flush_composition()
            self._drop_theme_overlay(overlay, focused)

        def _flush_composition(self) -> None:
            """Waits for the compositor to show the fully painted new frame.

            Without this the overlay can be removed while the window below is
            still being composed, and a screen capture catches a single frame
            with half of the old surface and half of the new one. `DwmFlush`
            makes the removal happen only after the new frame is on screen.
            """
            if not sys.platform.startswith("win"):
                return
            try:
                import ctypes
                ctypes.windll.dwmapi.DwmFlush()
            except Exception:
                pass

        def _theme_overlay(self) -> Any:
            """A snapshot of the client area that hides the recolouring.

            A screen grab of the current (old) theme is shown in a borderless
            topmost Toplevel exactly over the client area. The window is
            recoloured underneath; when the overlay disappears the compositor
            reveals the new window in one step."""
            if not sys.platform.startswith("win"):
                return None
            if not self.winfo_ismapped():
                return None
            try:
                from PIL import ImageGrab, ImageTk
                x, y = self.winfo_rootx(), self.winfo_rooty()
                width, height = self.winfo_width(), self.winfo_height()
                if width <= 1 or height <= 1:
                    return None
                snapshot = ImageGrab.grab(bbox=(x, y, x + width, y + height))
                overlay = tkinter.Toplevel(self)
                overlay.overrideredirect(True)
                overlay.attributes("-topmost", True)
                overlay.geometry(f"{width}x{height}+{x}+{y}")
                photo = ImageTk.PhotoImage(snapshot)
                label = tkinter.Label(overlay, image=photo, bd=0,
                                      highlightthickness=0)
                label.pack()
                overlay.deiconify()
                overlay.lift()
                overlay.update()
                self.update_idletasks()
                return (overlay, photo)
            except Exception:
                return None

        def _drop_theme_overlay(self, overlay: Any, focused: Any) -> None:
            """Removes the snapshot and gives the focus back to its widget."""
            if overlay is not None:
                try:
                    overlay[0].destroy()
                except Exception:
                    pass
            if focused is not None:
                try:
                    focused.tk.call("focus", "-force", focused._w)
                except Exception:
                    pass

        def _recolour_widget(self, widget: Any) -> None:
            """Resolves every stored `_TokenColor` of a widget and its children.

            All the properties of one widget are configured in a single call: a
            few CustomTkinter widgets run `update_idletasks` inside their draw,
            and one call per property multiplied that cost by five."""
            if widget is self.table_body:
                return
            updates: dict[str, Any] = {}
            # Plain Tk children (a CustomTkinter canvas or label) do not accept
            # tokens: asking them for sixteen colour options raises sixteen Tcl
            # errors per widget, so they are skipped as a block.
            if hasattr(widget, "_apply_appearance_mode"):
                for prop in COLOUR_PROPERTIES:
                    try:
                        value = widget.cget(prop)
                    except Exception:
                        continue
                    if (isinstance(value, _TokenColor)
                            and str(value) != theme.color(value.token)):
                        updates[prop] = c(value.token)
                if not isinstance(widget.master, ctk.CTkBaseClass):
                    try:
                        # Widgets hosted directly on a plain Tk surface store
                        # their master's colour on creation; re-detecting picks
                        # up the new one without quadratic child propagation.
                        widget.cget("bg_color")
                        updates["bg_color"] = "transparent"
                    except Exception:
                        pass
            if updates:
                try:
                    widget.configure(**updates)
                except Exception:
                    for prop, value in updates.items():
                        try:
                            widget.configure(**{prop: value})
                        except Exception:
                            pass
            if getattr(widget, "ui_spec", None) is not None:
                self._refresh_button_image(widget)
            for child in widget.winfo_children():
                self._recolour_widget(child)

        def _refresh_button_image(self, button: Any) -> None:
            spec = getattr(button, "ui_spec", None)
            if not spec or not spec.get("icon"):
                return
            name, size, token = spec["icon"]
            button.configure(image=self._icon(name, size, c(token)))

        def _refresh_row_icon(self, entry: dict[str, Any]) -> None:
            spec = entry.get("icon_spec")
            if spec:
                name, size, token = spec
                entry["cells"]["md5"].configure(image=self._tk_icon(name, size,
                                                                    c(token)))

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
            if (bool(self.var_pdf.get())
                    and self.var_pdf_dest_mode.get() == "custom"
                    and not self._effective_output_dir()):
                self._alert(
                    "PDF folder missing",
                    "Choose a custom PDF output folder, or select 'Save in each album folder'.",
                )
                return

            discovered = discover_album_queue(path)
            if not discovered:
                discovered = [path]

            self.events.clear()
            self._log_entries = [
                (stamp, text, label, None)
                for stamp, text, label, _ in self._log_entries
            ]
            self.total = 0
            self.recibidos = 0
            self.pdf_path = None
            self._batch_root = path
            self._batch_queue = deque(discovered)
            self._batch_total_count = len(discovered)
            self._batch_current_index = 0
            self._batch_current_path = ""
            self._failed_batches = []
            self._album_total = 0
            self._album_received = 0
            self.barra.set(0)
            self.label_progress.configure(text="")
            self.selected_event = None
            self._set_run_state(RunState.RUNNING)
            self._clear_table()
            self._render_hero()
            if self._batch_total_count > 1:
                self._log(
                    f"Queued {self._batch_total_count} albums from {path}"
                )
            if not self._start_next_batch():
                return
            self._update_state()
            self._render_hero()
            self.after(100, self._drain_queue)

        def _start_next_batch(self) -> bool:
            """Starts the next album batch in `self._batch_queue`."""
            while self._batch_queue:
                next_path = self._batch_queue.popleft()
                self._batch_current_index += 1
                self._batch_current_path = next_path
                self._album_total = 0
                self._album_received = 0
                # Ensure a finished fake or exited process does not trip start().
                if not self.manager.active:
                    self.manager.process = None
                pdf_enabled = bool(self.var_pdf.get())
                output_dir = self._effective_output_dir() if pdf_enabled else None
                try:
                    command = self.manager.start(
                        next_path,
                        mode="full",
                        pdf=pdf_enabled,
                        workers=self._workers(),
                        without_cache=bool(self.var_without_cache.get()),
                        png=True,
                        output_dir=output_dir,
                    )
                except Exception as e:
                    self._failed_batches.append(
                        (self._batch_current_index, next_path, str(e))
                    )
                    self._log(
                        f"Could not start album {self._batch_current_index} / "
                        f"{self._batch_total_count}: {e}",
                        "error",
                    )
                    if self._batch_queue:
                        continue
                    self._set_run_state(RunState.ERROR)
                    if (self._batch_total_count > 1
                            and len(self._failed_batches) < self._batch_current_index):
                        # At least one previous album completed; let _finish finalize once.
                        return False
                    self._alert("Could not start the engine", str(e), error=True)
                    self._update_state()
                    self._render_hero()
                    return False

                self.active_run_id = getattr(
                    self.manager, "run_id", self.active_run_id + 1
                )
                if self._batch_total_count > 1:
                    album_name = (
                        os.path.basename(os.path.normpath(next_path)) or next_path
                    )
                    self.label_state.configure(
                        text=f"Analyzing album {self._batch_current_index} / "
                             f"{self._batch_total_count} · {album_name}"
                    )
                    self._log(
                        f"Album {self._batch_current_index} / "
                        f"{self._batch_total_count}: {next_path}"
                    )
                else:
                    self.label_state.configure(text="Analyzing…")
                self._log("Running: " + " ".join(command[1:]))
                self._update_progress()
                return True
            return False

        def _cancel(self) -> None:
            self._batch_queue.clear()
            if (not self.manager.active
                    and not self.manager.active_thread
                    and self.run_state != RunState.RUNNING):
                return
            if (not self.manager.active
                    and not self.manager.active_thread
                    and not self.message_queue.empty()):
                self._drain_queue()
                if self.run_state != RunState.RUNNING:
                    return
            if self.active_run_id:
                self._cancelled_run_ids.add(self.active_run_id)
            self._log("Cancelling the analysis and its child processes…", "aviso")
            self.manager.cancel()
            self._set_run_state(RunState.CANCELLED)
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
            the final `__fin__` yet), while items remain in `message_queue`, or
            while a full batch was just processed."""
            if self._retheming:
                self.after(IDLE_INTERVAL_MS, self._drain_queue)
                return
            processed = 0
            results = 0
            self._batch_draining = True
            try:
                while (processed < BATCH_DRAINED
                       and results < MAX_RESULTS_BY_TICK):
                    channel, event = self.message_queue.get_nowait()
                    is_result = (channel == "stdout"
                                    and event.get("tipo") == "resultado")
                    try:
                        # Non-result events keep their immediate handling; the
                        # progress, hero and log widgets flush once per batch below.
                        self._process(channel, event,
                                       update_progress=not is_result,
                                       batch_mode=True)
                    except Exception as e:      # one bad event cannot stall the UI
                        self._log(f"Could not process an engine event: "
                                  f"{type(e).__name__}: {e}", "error")
                    processed += 1
                    if is_result:
                        results += 1
            except queue.Empty:
                pass
            finally:
                self._batch_draining = False
                self._flush_pending_logs()

            if self._viewport_dirty:
                self._sync_virtual_viewport()
            if self._filter_dirty:
                self._filter_dirty = False
                self._refresh_filter_bar()
            if self._hero_dirty:
                self._hero_dirty = False
                self._render_hero()
                self._update_reveal()
            if results:
                self._update_progress()

            try:
                if not self.winfo_exists():
                    return
            except Exception:
                return
            backlog = results >= MAX_RESULTS_BY_TICK or processed >= BATCH_DRAINED
            # Check `still_running` BEFORE `has_pending` so a reader thread that
            # pushes `__fin__` and exits during widget updates above is guaranteed
            # to trigger `has_pending == True`.
            still_running = self.manager.active or self.manager.active_thread
            has_pending = not self.message_queue.empty()
            if backlog or has_pending or still_running:
                self.after(
                    ACTIVE_INTERVAL_MS if (backlog or has_pending) else IDLE_INTERVAL_MS,
                    self._drain_queue,
                )

        def _update_progress(self) -> None:
            """One redraw of the bar and the counter per drained batch."""
            if self._batch_total_count > 1:
                album_frac = (
                    (self._album_received / self._album_total)
                    if self._album_total
                    else 0.0
                )
                overall = (
                    (max(0, self._batch_current_index - 1) + min(1.0, album_frac))
                    / float(self._batch_total_count)
                )
                self.barra.set(min(1.0, overall))
                self.label_progress.configure(
                    text=f"Album {self._batch_current_index} / "
                         f"{self._batch_total_count} · {self.recibidos} / {self.total}"
                )
                return
            if not self.total:
                return
            self.barra.set(min(1.0, self.recibidos / self.total))
            self.label_progress.configure(text=f"{self.recibidos} / {self.total}")

        def _process(self, channel: str, event: Event,
                      update_progress: bool = True,
                      batch_mode: bool = False) -> None:
            kind = event.get("tipo")
            event_run_id = event.get("run_id")
            if event_run_id is not None:
                if self.active_run_id and event_run_id != self.active_run_id:
                    return
                if event_run_id in self._cancelled_run_ids and kind != "__fin__":
                    return
            if channel == "stderr" or kind == "__stderr__":
                self._log(f"motor: {event.get('mensaje', '').strip()}", "aviso")
                return
            if kind == "inicio":
                self._log_entries = [
                    (stamp, text, label, None)
                    for stamp, text, label, _ in self._log_entries
                ]
                album_count = int(event.get("total") or 0)
                self._album_total = album_count
                self._album_received = 0
                if self._batch_total_count > 1:
                    self.total += album_count
                else:
                    self.total = album_count
                files = f"{album_count} file" + ("" if album_count == 1 else "s")
                workers = event.get("workers", "?")
                processes = ("process" if str(workers) == "1"
                             else "processes")
                self._log(f"Analyzing {files} · {workers} {processes}"
                          + ("" if event.get("cache", True) else " · no cache"))
                if self._batch_total_count > 1:
                    self._update_progress()
                else:
                    self.label_progress.configure(text=f"0 / {self.total}")
            elif kind == "resultado":
                self.events.append(event)
                self.recibidos += 1
                self._album_received += 1
                self._set_results_visible(True)
                label = "ok"
                if event.get("error"):
                    label = "error"
                elif event.get("veredicto") in ("SUSPICIOUS", "PROBABLE UPSCALE"):
                    label = "aviso"
                self._log(format_result(event), label, scroll=False,
                          result_event=event)
                for evidence_item in event.get("evidencias_forenses") or []:
                    self._log(f"    evidence: {evidence_item}", "info",
                              scroll=False, result_event=event)
                # A row per file, as the event arrives (DESIGN.md 7.7).
                self._add_row(event)
                spec_path = spectrogram_path(event)
                if spec_path:
                    self._prewarm_spectrogram(spec_path)
                if batch_mode:
                    self._pending_log_scroll = True
                    if self.selected_event is None:
                        self._hero_dirty = True
                else:
                    # One layout/scroll per result block, not once per evidence line.
                    self._scroll_log()
                    if self.selected_event is None:
                        self._render_hero()
                        self._update_reveal()
                if update_progress:
                    self._update_progress()
            elif kind == "informe":
                self._order_batch_results()
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
            self._hero_dirty = False
            self._order_batch_results()
            cancelled = bool(event.get("cancelado")) or (
                event.get("run_id") in self._cancelled_run_ids
            )
            if cancelled:
                self._batch_queue.clear()
            else:
                if event.get("codigo") not in (0, None):
                    self._failed_batches.append(
                        (
                            self._batch_current_index,
                            self._batch_current_path,
                            f"exit code {event['codigo']}",
                        )
                    )
                if self._batch_queue:
                    if event.get("codigo") not in (0, None):
                        self._log(
                            f"Album {self._batch_current_index} / "
                            f"{self._batch_total_count} finished with code "
                            f"{event['codigo']}.",
                            "error",
                        )
                    if self._start_next_batch():
                        self._update_state()
                        self._render_hero()
                        if not self._batch_draining:
                            self.after(ACTIVE_INTERVAL_MS, self._drain_queue)
                        return

            self.barra.set(1.0 if not event.get("cancelado") else self.barra.get())
            color, text = format_summary(self.events)
            if cancelled:
                self._set_run_state(RunState.CANCELLED)
                self.label_state.configure(text="Cancelled")
                self._log("Analysis cancelled.", "aviso")
            elif self._batch_total_count > 1 and self._failed_batches:
                failed_count = len(self._failed_batches)
                self._set_run_state(RunState.ERROR)
                self.label_state.configure(
                    text=(
                        f"Finished with errors ({failed_count}/"
                        f"{self._batch_total_count} albums failed)"
                    )
                )
                self._log(
                    f"Batch finished with errors: {failed_count} of "
                    f"{self._batch_total_count} albums failed.",
                    "error",
                )
                self._alert(
                    "Batch finished with errors",
                    f"{failed_count} of {self._batch_total_count} albums failed.",
                    error=True,
                )
            elif event.get("codigo") not in (0, None):
                self._set_run_state(RunState.ERROR)
                self.label_state.configure(text=f"Finished with code {event['codigo']}")
                self._log(f"The engine finished with code {event['codigo']}.", "error")
                self._alert("The engine finished with an error",
                             f"Exit code: {event['codigo']}", error=True)
            else:
                self._set_run_state(RunState.FINISHED)
                if self._batch_total_count > 1:
                    self.label_state.configure(
                        text=f"Finished ({self._batch_total_count} albums)"
                    )
                    self._log(
                        f"Finished {self._batch_total_count} albums.", "ok"
                    )
                else:
                    self.label_state.configure(text="Finished")
                    self._log("Analysis finished.", "ok")
            self._refresh_filter_bar(force=True)
            self._render_hero()
            self._set_activity(False)
            self._update_state()
            self._scroll_scheduled = False
            self._on_table_body()
            self.table_canvas.yview_moveto(0.0)
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
            self._batch_queue.clear()
            if self.manager.active:
                self._log("Closing: cancelling the running analysis…", "aviso")
                self.manager.cancel()
                self.manager.wait(duration=3.0)
            self.destroy()

        def destroy(self) -> None:
            """Stops background worker and cancels pending Tcl `after` callbacks."""
            with self._spec_cond:
                self._spec_stop = True
                self._spec_deque.clear()
                self._spec_queued.clear()
                self._spec_cond.notify_all()
            try:
                for after_id in self.tk.call("after", "info"):
                    try:
                        self.tk.call("after", "cancel", after_id)
                    except Exception:
                        pass
            except Exception:
                pass
            super().destroy()


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
