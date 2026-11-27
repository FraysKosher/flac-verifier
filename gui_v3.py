# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 FLAC VERIFIER Contributors
"""Modern desktop interface for FLAC VERIFIER v3 (Edge WebView2 via pywebview).

Architecture:
  - Presentation shell only: zero audio decoding or forensic scoring happens
    here. All analysis is executed by `motor_flac.py` / `flac_motor.exe` via
    `scan_manager.ScanManager`.
  - Serves `ui_v3/` (HTML5, CSS3, Vanilla ECMAScript modules) and cached `320x320`
    WebP album covers over a local Bottle WSGI app bound to pywebview's loopback
    server, enabling fast native `<img>` decoding and clean module imports.
  - Exposes `FlacVerifierApi` to JavaScript (`window.pywebview.api`) and pushes
    throttled live scan events back via `window.__flacOnEvent(...)`.
  - Hooks pywebview's native DOM drop event (`pywebviewFullPath`) so dragging
    `.flac` files or multi-album root folders from File Explorer passes real
    filesystem paths into Quick Drop or the Library.
  - Optional dependency guard: importing this module never requires `pywebview`
    or a graphical display, so CLI mode and headless CI tests stay green.
"""
from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from typing import Any, Callable

import covers as covers_mod
import library_db as ldb
import main as app_main
import motor_flac
import scan_manager as sm
import spectrogram_bridge as sb
import theme as theme_mod
import updater as updater_mod

try:
    import webview
    WEBVIEW_OK = True
except ImportError:
    webview = None  # type: ignore[assignment]
    WEBVIEW_OK = False

try:
    import bottle
    BOTTLE_OK = True
except ImportError:
    bottle = None  # type: ignore[assignment]
    BOTTLE_OK = False

DIRECTORY = os.path.dirname(os.path.abspath(__file__))
UI_DIR = os.path.join(
    getattr(sys, "_MEIPASS", DIRECTORY),
    "ui_v3",
)
if not os.path.isdir(UI_DIR):
    UI_DIR = os.path.join(DIRECTORY, "ui_v3")

WINDOW_TITLE = "FLAC Verifier"
DEFAULT_WIDTH = 1280
DEFAULT_HEIGHT = 800
MIN_WIDTH = 980
MIN_HEIGHT = 640

DWMWA_USE_IMMERSIVE_DARK_MODE = 20
DWMWA_USE_IMMERSIVE_DARK_MODE_OLD = 19
DWMWA_CAPTION_COLOR = 35
DARK_BG_HEX = "#0d0f11"
LIGHT_BG_HEX = "#ffffff"


def _colorref(hex_rgb: str) -> int:
    clean = hex_rgb.lstrip("#")
    r = int(clean[0:2], 16)
    g = int(clean[2:4], 16)
    b = int(clean[4:6], 16)
    return r | (g << 8) | (b << 16)


def _find_window_hwnd(pid: int, title_prefix: str = WINDOW_TITLE) -> int:
    """Locates the native Win32 HWND belonging to `pid`."""
    if os.name != "nt":
        return 0
    try:
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        proto = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        found: list[int] = []

        def visit(hwnd, _lp):
            win_pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(win_pid))
            if win_pid.value != pid:
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            if length > 0:
                buf = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buf, length + 1)
                if title_prefix.lower() in buf.value.lower():
                    found.append(int(hwnd))
                    return False
            return True

        user32.EnumWindows(proto(visit), 0)
        return found[0] if found else 0
    except Exception:
        return 0


def apply_dwm_titlebar(mode: str, hwnd: int | None = None) -> bool:
    """Synchronises Windows 10/11 native title bar theme and caption colour."""
    if os.name != "nt":
        return False
    target_hwnd = hwnd if hwnd else _find_window_hwnd(os.getpid())
    if not target_hwnd:
        return False
    dark = str(mode).lower() != "light"
    caption_hex = DARK_BG_HEX if dark else LIGHT_BG_HEX
    try:
        dwmapi = ctypes.windll.dwmapi
        dwmapi.DwmSetWindowAttribute.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint,
            ctypes.c_void_p,
            ctypes.c_uint,
        ]
        dwmapi.DwmSetWindowAttribute.restype = ctypes.c_long
        val = ctypes.c_int(1 if dark else 0)
        hr = dwmapi.DwmSetWindowAttribute(
            ctypes.c_void_p(target_hwnd),
            DWMWA_USE_IMMERSIVE_DARK_MODE,
            ctypes.byref(val),
            ctypes.sizeof(val),
        )
        if hr != 0:
            dwmapi.DwmSetWindowAttribute(
                ctypes.c_void_p(target_hwnd),
                DWMWA_USE_IMMERSIVE_DARK_MODE_OLD,
                ctypes.byref(val),
                ctypes.sizeof(val),
            )
        cap = ctypes.c_uint(_colorref(caption_hex))
        dwmapi.DwmSetWindowAttribute(
            ctypes.c_void_p(target_hwnd),
            DWMWA_CAPTION_COLOR,
            ctypes.byref(cap),
            ctypes.sizeof(cap),
        )
        return True
    except Exception:
        return False


# ─── Settings persistence (backward-compatible with theme.settings_path) ─────

DEFAULT_SETTINGS: dict[str, Any] = {
    "theme": "dark",
    "workers": 0,
    "pdf_dest_mode": "album",
    "custom_pdf_dir": "",
    "offline_mode": False,
    "albums_view": "grid",
    "albums_sort": "artist",
    "albums_sort_desc": False,
    "library_pdf": False,
    "library_no_cache": False,
}

_VALID_ALBUM_SORTS = frozenset(("artist", "title", "flagged", "year", "format", "tracks", "recent"))


def v3_settings_path(data_dir: str | None = None) -> str:
    """Resolves `settings.json` inside the active data dir (or `%APPDATA%`)."""
    root = ldb.resolve_data_dir(data_dir)
    return os.path.join(root, theme_mod.SETTINGS_FILE)


def load_v3_settings(data_dir: str | None = None) -> dict[str, Any]:
    """Reads persisted UI settings safely, merging defaults."""
    settings = dict(DEFAULT_SETTINGS)
    path = v3_settings_path(data_dir)
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
        if isinstance(raw, dict):
            if raw.get("theme") in ("dark", "light"):
                settings["theme"] = raw["theme"]
            if isinstance(raw.get("workers"), int) and 0 <= raw["workers"] <= 32:
                settings["workers"] = raw["workers"]
            if raw.get("pdf_dest_mode") in ("album", "custom"):
                settings["pdf_dest_mode"] = raw["pdf_dest_mode"]
            if isinstance(raw.get("custom_pdf_dir"), str):
                settings["custom_pdf_dir"] = raw["custom_pdf_dir"]
            if isinstance(raw.get("offline_mode"), bool):
                settings["offline_mode"] = raw["offline_mode"]
            if raw.get("albums_view") in ("grid", "list"):
                settings["albums_view"] = raw["albums_view"]
            if raw.get("albums_sort") in _VALID_ALBUM_SORTS:
                settings["albums_sort"] = raw["albums_sort"]
            if isinstance(raw.get("albums_sort_desc"), bool):
                settings["albums_sort_desc"] = raw["albums_sort_desc"]
            if isinstance(raw.get("library_pdf"), bool):
                settings["library_pdf"] = raw["library_pdf"]
            if isinstance(raw.get("library_no_cache"), bool):
                settings["library_no_cache"] = raw["library_no_cache"]
    except Exception:
        pass
    return settings


def save_v3_settings(updates: dict[str, Any], data_dir: str | None = None) -> dict[str, Any]:
    """Atomically merges and writes `settings.json`, preserving existing keys."""
    current = load_v3_settings(data_dir)
    for key in DEFAULT_SETTINGS:
        if key not in updates:
            continue
        val = updates[key]
        if key == "theme" and val in ("dark", "light"):
            current[key] = val
        elif key == "workers" and isinstance(val, int) and 0 <= val <= 32:
            current[key] = val
        elif key == "pdf_dest_mode" and val in ("album", "custom"):
            current[key] = val
        elif key == "custom_pdf_dir" and isinstance(val, str):
            current[key] = val.strip()
        elif key in ("offline_mode", "library_pdf", "library_no_cache", "albums_sort_desc") and isinstance(val, bool):
            current[key] = val
        elif key == "albums_view" and val in ("grid", "list"):
            current[key] = val
        elif key == "albums_sort" and val in _VALID_ALBUM_SORTS:
            current[key] = val

    path = v3_settings_path(data_dir)
    folder = os.path.dirname(path)
    tmp_path = ""
    try:
        os.makedirs(folder, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(prefix=".settings-", suffix=".json", dir=folder)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(current, fh, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
        tmp_path = ""
    except Exception:
        pass
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
    return current


# ─── Local WSGI asset server (UI + cached WebP covers) ───────────────────────

def create_ui_app(
    *,
    ui_dir: str = UI_DIR,
    covers_dir: str | None = None,
) -> Any:
    """Builds a local Bottle WSGI app serving `ui_v3/` and cached WebP covers."""
    if not BOTTLE_OK or bottle is None:
        raise RuntimeError("bottle is required to serve the v3 interface")
    app = bottle.Bottle()
    resolved_covers = covers_dir or covers_mod.default_covers_dir()

    @app.route("/")
    def _index():
        resp = bottle.static_file("index.html", root=ui_dir)
        resp.set_header("Cache-Control", "no-store")
        return resp

    @app.route("/covers/<cover_hash>.webp")
    def _cover(cover_hash: str):
        path = covers_mod.cover_file_path(cover_hash, cache_dir=resolved_covers)
        if not path:
            return bottle.HTTPError(404, "Cover not found")
        resp = bottle.static_file(
            os.path.basename(path),
            root=os.path.dirname(path),
            mimetype="image/webp",
        )
        resp.set_header("Cache-Control", "public, max-age=86400")
        return resp

    @app.route("/<filepath:path>")
    def _static(filepath: str):
        resp = bottle.static_file(filepath, root=ui_dir)
        resp.set_header("Cache-Control", "no-store")
        return resp

    return app


# ─── Python <-> JS Bridge API ────────────────────────────────────────────────

class FlacVerifierApi:
    """Public bridge exposed to `window.pywebview.api` (and testable headless)."""

    def __init__(
        self,
        db: ldb.LibraryDB,
        scanner: sm.ScanManager,
        *,
        data_dir: str | None = None,
        covers_dir: str | None = None,
        specs_dir: str | None = None,
        initial_path: str | None = None,
        event_sink: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.db = db
        self.scanner = scanner
        self.data_dir = data_dir
        self.covers_dir = covers_dir or covers_mod.default_covers_dir(data_dir)
        self.specs_dir = specs_dir or sb.default_specs_dir(data_dir)
        self.initial_path = ldb.normalize_path(initial_path) if initial_path else None
        self._window: Any = None
        self._event_sink = event_sink
        self._settings = load_v3_settings(data_dir)
        self._last_progress_push = 0.0
        self._push_lock = threading.Lock()
        self._spec_lock = threading.Lock()
        self._spec_cancel_event: threading.Event | None = None
        self._update_info: dict[str, Any] | None = None

    def attach_window(self, window: Any) -> None:
        self._window = window

    def set_update_info(self, info: dict[str, Any] | None) -> None:
        self._update_info = info
        if info:
            self.push_event({"lane": "updater", "event": "update_available", "info": info})

    def push_event(self, payload: dict[str, Any]) -> None:
        """Forwards backend events to the WebView2 page (or test sink)."""
        # Throttle high-frequency track_progress ticks so massive scans stay silky.
        if payload.get("event") == "track_progress":
            now = time.monotonic()
            with self._push_lock:
                if now - self._last_progress_push < 0.08:
                    return
                self._last_progress_push = now

        if self._event_sink is not None:
            try:
                self._event_sink(payload)
            except Exception:
                pass
        win = self._window
        if win is not None:
            try:
                encoded = json.dumps(payload, ensure_ascii=False, default=str)
                win.evaluate_js(
                    f"window.__flacOnEvent && window.__flacOnEvent({encoded});"
                )
            except Exception:
                pass

    # ── Bootstrap & settings ─────────────────────────────────────────────────

    def bootstrap(self) -> dict[str, Any]:
        """Initial state payload loaded once on frontend startup."""
        return {
            "ok": True,
            "app_version": app_main.VERSION,
            "engine_version": motor_flac.ENGINE_VERSION,
            "portable": ldb.is_portable_mode(),
            "theme": self._settings.get("theme", "dark"),
            "settings": dict(self._settings),
            "counts": self.db.get_sidebar_counts(),
            "roots": self.db.list_roots(),
            "outdated": self.db.count_outdated_albums(motor_flac.ENGINE_VERSION),
            "library_status": self.scanner.library_status(),
            "quick_drop_status": self.scanner.quick_drop_status(),
            "recent_quick_drops": self.db.list_recent_quick_drops(limit=10),
            "initial_path": self.initial_path,
            "cpu_count": os.cpu_count() or 4,
            "update_info": self._update_info,
        }

    def set_theme(self, mode: str) -> dict[str, Any]:
        clean = "light" if str(mode).lower() == "light" else "dark"
        self._settings = save_v3_settings({"theme": clean}, self.data_dir)
        apply_dwm_titlebar(clean)
        return {"ok": True, "theme": clean}

    def save_preferences(self, prefs: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(prefs, dict):
            return {"ok": False, "error": "Invalid preferences payload"}
        self._settings = save_v3_settings(prefs, self.data_dir)
        return {"ok": True, "settings": dict(self._settings)}

    # ── Library queries ──────────────────────────────────────────────────────

    def get_overview(self) -> dict[str, Any]:
        return {
            "ok": True,
            "counts": self.db.get_sidebar_counts(),
            "roots": self.db.list_roots(),
            "outdated": self.db.count_outdated_albums(motor_flac.ENGINE_VERSION),
            "library_status": self.scanner.library_status(),
        }

    def get_albums(
        self,
        root_id: int | None = None,
        verdict_pill: str = "all",
        search: str = "",
        sort_by: str | None = None,
        sort_desc: bool | None = None,
    ) -> dict[str, Any]:
        rid = int(root_id) if root_id is not None else None
        eff_sort = str(sort_by or self._settings.get("albums_sort") or "artist")
        eff_desc = (
            bool(self._settings.get("albums_sort_desc", False))
            if sort_desc is None
            else bool(sort_desc)
        )
        all_for_pills = self.db.query_albums(
            root_id=rid,
            verdict_pill="all",
            search=search,
            sort_by=eff_sort,
            sort_desc=eff_desc,
        )
        pill_counts = {
            "all": len(all_for_pills),
            "ok": sum(
                1 for a in all_for_pills
                if int(a.get("flagged_count") or 0) == 0
                and int(a.get("suspicious_count") or 0) == 0
            ),
            "warn": sum(
                1 for a in all_for_pills
                if int(a.get("flagged_count") or 0) == 0
                and int(a.get("suspicious_count") or 0) > 0
            ),
            "bad": sum(
                1 for a in all_for_pills if int(a.get("flagged_count") or 0) > 0
            ),
        }
        if verdict_pill == "all":
            albums = all_for_pills
        else:
            albums = self.db.query_albums(
                root_id=rid,
                verdict_pill=verdict_pill,
                search=search,
                sort_by=eff_sort,
                sort_desc=eff_desc,
            )
        return {"ok": True, "albums": albums, "pill_counts": pill_counts}

    def get_album_detail(
        self,
        album_id: int,
        verdict_pill: str = "all",
        search: str = "",
        sort_by: str = "num",
        sort_desc: bool = False,
    ) -> dict[str, Any]:
        aid = int(album_id)
        album = self.db.get_album(aid)
        if not album:
            return {"ok": False, "error": "Album not found"}
        all_tracks = self.db.query_tracks(
            album_id=aid, verdict_pill="all", search=search,
            sort_by=sort_by, sort_desc=bool(sort_desc),
        )
        pill_counts = _count_verdict_pills(all_tracks)
        tracks = (
            all_tracks
            if verdict_pill == "all"
            else [t for t in all_tracks if t.get("verdict_class") == verdict_pill]
        )
        return {
            "ok": True,
            "album": album,
            "tracks": tracks,
            "pill_counts": pill_counts,
        }

    def get_tracks(
        self,
        view: str = "all",
        root_id: int | None = None,
        verdict_pill: str = "all",
        search: str = "",
        sort_by: str = "score",
        sort_desc: bool = False,
        limit: int | None = None,
        offset: int = 0,
    ) -> dict[str, Any]:
        rid = int(root_id) if root_id is not None else None
        all_rows = self.db.query_tracks(
            root_id=rid,
            view=view,
            verdict_pill="all",
            search=search,
            sort_by=sort_by,
            sort_desc=bool(sort_desc),
        )
        pill_counts = _count_verdict_pills(all_rows)
        filtered = (
            all_rows
            if verdict_pill == "all"
            else [t for t in all_rows if t.get("verdict_class") == verdict_pill]
        )
        total = len(filtered)
        if limit is not None:
            start = max(0, int(offset))
            filtered = filtered[start : start + max(1, int(limit))]
        return {
            "ok": True,
            "tracks": filtered,
            "total": total,
            "pill_counts": pill_counts,
        }

    def get_track_detail(self, track_id: int) -> dict[str, Any]:
        track = self.db.get_track(int(track_id), include_raw=True)
        if not track:
            return {"ok": False, "error": "Track not found"}
        return {"ok": True, "track": track}

    def get_spectrogram(
        self,
        file_path: str,
        track_id: int | None = None,
        raw_result: dict[str, Any] | None = None,
        request_id: int | None = None,
    ) -> dict[str, Any]:
        # Cancel any obsolete in-flight spectrogram read so rapid track changes
        # over network shares never pile up queued SMB I/O.
        my_cancel = threading.Event()
        with self._spec_lock:
            if self._spec_cancel_event is not None:
                self._spec_cancel_event.set()
            self._spec_cancel_event = my_cancel

        result_payload = raw_result
        if result_payload is None and track_id is not None:
            track = self.db.get_track(int(track_id), include_raw=True)
            if track:
                result_payload = track.get("raw_result")
                if not file_path:
                    file_path = str(track.get("file_path") or "")

        payload = sb.get_interactive_spectrogram(
            file_path,
            result=result_payload,
            cache_dir=self.specs_dir,
            use_cache=True,
            cancel_event=my_cancel,
        )
        if request_id is not None:
            payload["request_id"] = int(request_id)
        return payload

    def prefetch_spectrogram(
        self,
        file_path: str,
        track_id: int | None = None,
    ) -> dict[str, Any]:
        """Silently warms `.specz` cache for the next track when idle."""
        lib_st = self.scanner.library_status()
        if lib_st.get("state") != "idle" or self.scanner.quick_drop_status().get("state") == "running":
            return {"ok": False, "skipped": "busy"}
        target = str(file_path or "")
        if not target and track_id is not None:
            tr = self.db.get_track(int(track_id), include_raw=False)
            if tr:
                target = str(tr.get("file_path") or "")
        if not target or not os.path.isfile(target):
            return {"ok": False, "skipped": "missing"}

        def _warm() -> None:
            try:
                sb.get_interactive_spectrogram(
                    target,
                    cache_dir=self.specs_dir,
                    use_cache=True,
                )
            except Exception:
                pass

        threading.Thread(target=_warm, name="flac-spec-prefetch", daemon=True).start()
        return {"ok": True, "queued": True}

    def set_track_reviewed(
        self,
        track_id: int,
        reviewed: bool,
        note: str = "",
    ) -> dict[str, Any]:
        updated = self.db.set_track_reviewed(int(track_id), bool(reviewed), str(note or ""))
        if not updated:
            return {"ok": False, "error": "Track not found"}
        return {
            "ok": True,
            "track": updated,
            "counts": self.db.get_sidebar_counts(),
        }

    # ── Native dialogs & shell helpers ───────────────────────────────────────

    def _dialog_open(self, kind: str, **kwargs: Any) -> list[str]:
        win = self._window
        if win is None or not WEBVIEW_OK or webview is None:
            return []
        dialog_enum = getattr(webview, "FileDialog", None)
        if dialog_enum is not None:
            type_map = {
                "folder": dialog_enum.FOLDER,
                "open": dialog_enum.OPEN,
                "save": dialog_enum.SAVE,
            }
        else:
            type_map = {
                "folder": getattr(webview, "FOLDER_DIALOG", 20),
                "open": getattr(webview, "OPEN_DIALOG", 10),
                "save": getattr(webview, "SAVE_DIALOG", 30),
            }
        chosen = win.create_file_dialog(type_map[kind], **kwargs)
        if not chosen:
            return []
        if isinstance(chosen, str):
            return [chosen]
        return [str(p) for p in chosen if p]

    def pick_folder(self, directory: str = "") -> dict[str, Any]:
        paths = self._dialog_open("folder", directory=directory or "")
        if not paths:
            return {"ok": False, "cancelled": True, "path": ""}
        return {"ok": True, "cancelled": False, "path": ldb.normalize_path(paths[0])}

    def pick_flac_file(self, directory: str = "") -> dict[str, Any]:
        paths = self._dialog_open(
            "open",
            directory=directory or "",
            allow_multiple=False,
            file_types=("FLAC audio (*.flac)", "All files (*.*)"),
        )
        if not paths:
            return {"ok": False, "cancelled": True, "path": ""}
        return {"ok": True, "cancelled": False, "path": ldb.normalize_path(paths[0])}

    def add_library_folder(
        self,
        folder_path: str | None = None,
        options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        target = ldb.normalize_path(folder_path) if folder_path else ""
        if not target:
            picked = self.pick_folder()
            if not picked.get("ok"):
                return {"ok": False, "cancelled": True}
            target = picked["path"]
        if not os.path.isdir(target):
            return {"ok": False, "error": "Selected library folder does not exist"}

        opts = options if isinstance(options, dict) else {}
        workers = int(opts.get("workers", self._settings.get("workers") or 0) or 0)
        pdf = bool(opts.get("pdf", False))
        pdf_dest_mode = (
            "custom"
            if str(opts.get("pdf_dest_mode", self._settings.get("pdf_dest_mode") or "album")) == "custom"
            else "album"
        )
        custom_pdf_dir = str(
            opts.get("custom_pdf_dir", self._settings.get("custom_pdf_dir") or "")
        ).strip()
        no_cache = bool(opts.get("no_cache", False))
        if pdf and pdf_dest_mode == "custom" and not custom_pdf_dir:
            return {"ok": False, "error": "Select a custom destination folder for PDF reports"}

        if opts:
            self._settings = save_v3_settings(
                {
                    "workers": max(0, min(32, workers)),
                    "pdf_dest_mode": pdf_dest_mode,
                    "custom_pdf_dir": custom_pdf_dir,
                    "library_pdf": pdf,
                    "library_no_cache": no_cache,
                },
                self.data_dir,
            )

        root = self.db.add_root(target)
        scan = self.scanner.enqueue_root_scan(
            int(root["id"]),
            force=bool(opts.get("force", False) or no_cache),
            workers=workers,
            pdf=pdf,
            pdf_dest_mode=pdf_dest_mode,
            custom_pdf_dir=custom_pdf_dir or None,
            no_cache=no_cache,
        )
        return {
            "ok": True,
            "root": root,
            "scan": scan,
            "roots": self.db.list_roots(),
            "settings": dict(self._settings),
            "library_status": self.scanner.library_status(),
        }

    def remove_library_folder(
        self,
        root_id: int,
        delete_albums: bool = True,
    ) -> dict[str, Any]:
        removed = self.db.remove_root(int(root_id), delete_albums=bool(delete_albums))
        if removed:
            try:
                covers_mod.prune_unused_covers(
                    self.db.active_cover_hashes(), cache_dir=self.covers_dir
                )
            except Exception:
                pass
        return {
            "ok": removed,
            "roots": self.db.list_roots(),
            "counts": self.db.get_sidebar_counts(),
            "outdated": self.db.count_outdated_albums(motor_flac.ENGINE_VERSION),
        }

    def rescan_library(
        self,
        root_id: int | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        workers = int(self._settings.get("workers") or 0)
        pdf = bool(self._settings.get("library_pdf", False))
        pdf_dest_mode = str(self._settings.get("pdf_dest_mode") or "album")
        custom_pdf_dir = str(self._settings.get("custom_pdf_dir") or "").strip() or None
        no_cache = bool(self._settings.get("library_no_cache", False))
        if root_id is not None:
            info = self.scanner.enqueue_root_scan(
                int(root_id),
                force=bool(force),
                workers=workers,
                pdf=pdf,
                pdf_dest_mode=pdf_dest_mode,
                custom_pdf_dir=custom_pdf_dir,
                no_cache=no_cache,
            )
        else:
            info = self.scanner.enqueue_all_roots_scan(
                force=bool(force),
                workers=workers,
                pdf=pdf,
                pdf_dest_mode=pdf_dest_mode,
                custom_pdf_dir=custom_pdf_dir,
                no_cache=no_cache,
            )
        return {"ok": True, "scan": info, "library_status": self.scanner.library_status()}

    def rescan_outdated(self, flagged_only: bool = False) -> dict[str, Any]:
        albums = self.db.list_outdated_albums(
            motor_flac.ENGINE_VERSION, flagged_only=bool(flagged_only)
        )
        folders = [a["folder_path"] for a in albums if os.path.isdir(a["folder_path"])]
        workers = int(self._settings.get("workers") or 0)
        queued = self.scanner.enqueue_albums(
            folders, force=True, workers=workers, no_cache=False
        )
        return {
            "ok": True,
            "queued": queued,
            "library_status": self.scanner.library_status(),
        }

    def reverify_album(self, album_id: int) -> dict[str, Any]:
        album = self.db.get_album(int(album_id))
        if not album:
            return {"ok": False, "error": "Album not found"}
        if not os.path.isdir(album["folder_path"]):
            return {"ok": False, "error": "Album folder is offline or missing"}
        workers = int(self._settings.get("workers") or 0)
        queued = self.scanner.enqueue_albums(
            [album["folder_path"]],
            root_id=album.get("root_id"),
            force=True,
            workers=workers,
            no_cache=True,
        )
        return {
            "ok": True,
            "queued": queued,
            "library_status": self.scanner.library_status(),
        }

    def pause_library_scan(self) -> dict[str, Any]:
        self.scanner.pause_library()
        return {"ok": True, "library_status": self.scanner.library_status()}

    def resume_library_scan(self) -> dict[str, Any]:
        self.scanner.resume_library()
        return {"ok": True, "library_status": self.scanner.library_status()}

    def cancel_library_scan(self) -> dict[str, Any]:
        self.scanner.cancel_library()
        return {"ok": True, "library_status": self.scanner.library_status()}

    # ── Quick Drop ───────────────────────────────────────────────────────────

    def start_quick_drop(
        self,
        target_path: str,
        pdf: bool = False,
        pdf_dest_mode: str = "album",
        custom_pdf_dir: str = "",
        workers: int = 0,
        no_cache: bool = False,
    ) -> dict[str, Any]:
        try:
            info = self.scanner.start_quick_drop(
                target_path,
                pdf=bool(pdf),
                pdf_dest_mode=pdf_dest_mode,
                custom_pdf_dir=custom_pdf_dir or None,
                workers=int(workers or 0),
                no_cache=bool(no_cache),
            )
            return {"ok": True, **info}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def cancel_quick_drop(self) -> dict[str, Any]:
        self.scanner.cancel_quick_drop()
        return {"ok": True, "quick_drop_status": self.scanner.quick_drop_status()}

    def promote_quick_drop(self) -> dict[str, Any]:
        saved = self.scanner.promote_last_quick_drop_to_library()
        return {
            "ok": True,
            "added_albums": len(saved),
            "counts": self.db.get_sidebar_counts(),
        }

    # ── Reports, export, OS integration ──────────────────────────────────────

    def generate_album_pdf(
        self,
        album_id: int,
        pdf_dest_mode: str = "album",
        custom_pdf_dir: str = "",
    ) -> dict[str, Any]:
        album = self.db.get_album(int(album_id))
        if not album:
            return {"ok": False, "error": "Album not found"}
        output_dir = None
        if pdf_dest_mode == "custom":
            if custom_pdf_dir:
                output_dir = custom_pdf_dir
            else:
                picked = self.pick_folder()
                if not picked.get("ok"):
                    return {"ok": False, "cancelled": True}
                output_dir = picked["path"]

        def _worker() -> None:
            res = self.scanner.generate_album_pdf(
                album["folder_path"],
                output_dir=output_dir,
                workers=int(self._settings.get("workers") or 0),
            )
            self.push_event({
                "lane": "pdf",
                "event": "pdf_done",
                "album_id": int(album_id),
                "album_title": album.get("title"),
                **res,
            })

        threading.Thread(target=_worker, name="flac-album-pdf", daemon=True).start()
        return {"ok": True, "started": True}

    def export_tracks(
        self,
        fmt: str = "csv",
        view: str = "all",
        album_id: int | None = None,
        root_id: int | None = None,
        verdict_pill: str = "all",
        search: str = "",
        dest_path: str | None = None,
    ) -> dict[str, Any]:
        ext = "json" if str(fmt).lower() == "json" else "csv"
        target = ldb.normalize_path(dest_path) if dest_path else ""
        if not target:
            file_types = (
                ("JSON files (*.json)", "All files (*.*)")
                if ext == "json"
                else ("CSV files (*.csv)", "All files (*.*)")
            )
            picked = self._dialog_open(
                "save",
                save_filename=f"flac_verifier_library.{ext}",
                file_types=file_types,
            )
            if not picked:
                return {"ok": False, "cancelled": True}
            target = ldb.normalize_path(picked[0])

        query_kw = {
            "album_id": int(album_id) if album_id is not None else None,
            "root_id": int(root_id) if root_id is not None else None,
            "view": view,
            "verdict_pill": verdict_pill,
            "search": search,
        }
        try:
            if ext == "json":
                count = self.db.export_tracks_json(target, **query_kw)
            else:
                count = self.db.export_tracks_csv(target, **query_kw)
            return {"ok": True, "path": target, "count": count}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def reveal_in_explorer(self, path: str) -> dict[str, Any]:
        norm = ldb.normalize_path(path)
        if not norm or not os.path.exists(norm):
            return {"ok": False, "error": "Path does not exist on disk"}
        try:
            if os.name == "nt":
                if os.path.isfile(norm):
                    subprocess.Popen(["explorer", "/select,", norm])
                else:
                    subprocess.Popen(["explorer", norm])
            elif sys.platform == "darwin":
                subprocess.Popen(["open", "-R", norm])
            else:
                folder = os.path.dirname(norm) if os.path.isfile(norm) else norm
                subprocess.Popen(["xdg-open", folder])
            return {"ok": True}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def open_external_path(self, path: str) -> dict[str, Any]:
        norm = ldb.normalize_path(path)
        if not norm or not os.path.exists(norm):
            return {"ok": False, "error": "File does not exist"}
        try:
            if os.name == "nt":
                os.startfile(norm)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", norm])
            else:
                subprocess.Popen(["xdg-open", norm])
            return {"ok": True}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def open_external_url(self, url: str) -> dict[str, Any]:
        clean = str(url or "").strip()
        if not clean.startswith(("https://", "http://")):
            return {"ok": False, "error": "Unsupported URL scheme"}
        webbrowser.open(clean)
        return {"ok": True}

    # ── Updates & offline mode ───────────────────────────────────────────────

    def set_offline_mode(self, enabled: bool) -> dict[str, Any]:
        flag = bool(enabled)
        self._settings = save_v3_settings({"offline_mode": flag}, self.data_dir)
        if flag:
            self._update_info = None
        return {"ok": True, "offline_mode": flag, "settings": dict(self._settings)}

    def check_updates(self, force: bool = False) -> dict[str, Any]:
        offline = bool(self._settings.get("offline_mode")) and not bool(force)
        info = updater_mod.check_for_update(
            app_main.VERSION,
            offline_mode=offline,
            portable=ldb.is_portable_mode(),
        )
        if info.get("available"):
            self.set_update_info(info)
        return info

    def install_update(self) -> dict[str, Any]:
        info = self._update_info
        if not info or not info.get("available"):
            return {"ok": False, "error": "No update is currently available"}

        if info.get("is_portable") or ldb.is_portable_mode():
            rel_url = str(info.get("release_url") or updater_mod.RELEASES_BASE_URL)
            try:
                updater_mod.validate_release_url(rel_url)
            except ValueError as exc:
                return {"ok": False, "error": str(exc)}
            webbrowser.open(rel_url)
            return {"ok": True, "mode": "portable", "opened_url": rel_url}

        def _worker() -> None:
            self.push_event({
                "lane": "updater",
                "event": "download_started",
                "version": info.get("version"),
            })

            def _progress(done: int, total: int) -> None:
                self.push_event({
                    "lane": "updater",
                    "event": "download_progress",
                    "downloaded": done,
                    "total": total,
                })

            dl = updater_mod.download_and_verify_installer(
                info,
                on_progress=_progress,
            )
            if not dl.get("ok"):
                self.push_event({
                    "lane": "updater",
                    "event": "download_error",
                    "error": dl.get("error") or "Update download failed",
                })
                return

            launched = updater_mod.launch_installer(dl["installer_path"])
            if not launched.get("ok"):
                self.push_event({
                    "lane": "updater",
                    "event": "download_error",
                    "error": launched.get("error") or "Could not start installer",
                })
                return

            self.push_event({
                "lane": "updater",
                "event": "installing",
                "version": info.get("version"),
            })
            # Give the frontend a moment to show status before closing cleanly.
            time.sleep(0.35)
            self.scanner.shutdown(timeout=5.0)
            self.db.close()
            win = self._window
            if win is not None:
                try:
                    win.destroy()
                except Exception:
                    pass

        threading.Thread(target=_worker, name="flac-updater", daemon=True).start()
        return {"ok": True, "mode": "installed", "started": True}


def _count_verdict_pills(tracks: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "all": len(tracks),
        "ok": sum(1 for t in tracks if t.get("verdict_class") == "ok"),
        "warn": sum(1 for t in tracks if t.get("verdict_class") == "warn"),
        "bad": sum(1 for t in tracks if t.get("verdict_class") == "bad"),
    }


def _extract_dropped_paths(event: dict[str, Any]) -> list[str]:
    """Extracts real filesystem paths from a pywebview native DOM drop event."""
    data_transfer = event.get("dataTransfer") if isinstance(event, dict) else None
    files = data_transfer.get("files") if isinstance(data_transfer, dict) else None
    if not isinstance(files, list):
        return []
    paths: list[str] = []
    seen: set[str] = set()
    for item in files:
        if not isinstance(item, dict):
            continue
        raw = item.get("pywebviewFullPath") or item.get("path") or ""
        norm = ldb.normalize_path(str(raw))
        if not norm or not os.path.exists(norm):
            continue
        key = ldb.path_key(norm)
        if key not in seen:
            seen.add(key)
            paths.append(norm)
    return paths


def _bind_native_dnd(window: Any, api: FlacVerifierApi) -> None:
    """Binds pywebview's DOM drop event so dropped Explorer items carry real paths."""
    try:
        from webview.dom import DOMEventHandler
    except Exception:
        DOMEventHandler = None  # type: ignore[assignment]

    def _on_drop(ev: dict[str, Any]) -> None:
        paths = _extract_dropped_paths(ev)
        if not paths:
            return
        try:
            payload = json.dumps(paths, ensure_ascii=False)
            window.evaluate_js(
                f"window.__flacOnNativeDrop && window.__flacOnNativeDrop({payload});"
            )
        except Exception:
            pass

    def _on_dragover(_ev: dict[str, Any]) -> None:
        pass

    try:
        doc = window.dom.document
        if DOMEventHandler is not None:
            doc.events.dragover += DOMEventHandler(_on_dragover, prevent_default=True, stop_propagation=False)
            doc.events.drop += DOMEventHandler(_on_drop, prevent_default=True, stop_propagation=False)
        else:
            doc.events.drop += _on_drop
    except Exception:
        pass


def main(
    initial_path: str | None = None,
    *,
    data_dir: str | None = None,
    width: int = DEFAULT_WIDTH,
    height: int = DEFAULT_HEIGHT,
    debug: bool = False,
) -> int:
    """Launches the FLAC VERIFIER v3 desktop window."""
    if not WEBVIEW_OK or webview is None or not BOTTLE_OK:
        print("Could not load the v3 desktop interface: pywebview is not installed.")
        print("Install it with:  pip install pywebview")
        print("You can still use the CLI:  FLAC_Verifier.exe --cli")
        return 1

    resolved_data = ldb.resolve_data_dir(data_dir)
    db_path = ldb.default_db_path(resolved_data)
    covers_dir = covers_mod.default_covers_dir(resolved_data)
    specs_dir = sb.default_specs_dir(resolved_data)
    settings = load_v3_settings(resolved_data)

    db = ldb.LibraryDB(db_path)
    api_holder: list[FlacVerifierApi] = []

    def _dispatch_event(ev: dict[str, Any]) -> None:
        if api_holder:
            api_holder[0].push_event(ev)

    scanner = sm.ScanManager(
        db,
        engine_version=motor_flac.ENGINE_VERSION,
        covers_dir=covers_dir,
        on_event=_dispatch_event,
    )
    api = FlacVerifierApi(
        db,
        scanner,
        data_dir=resolved_data,
        covers_dir=covers_dir,
        specs_dir=specs_dir,
        initial_path=initial_path,
    )
    api_holder.append(api)

    app = create_ui_app(ui_dir=UI_DIR, covers_dir=covers_dir)
    bg = DARK_BG_HEX if settings.get("theme", "dark") == "dark" else LIGHT_BG_HEX
    window = webview.create_window(
        WINDOW_TITLE,
        url=app,
        js_api=api,
        width=int(width),
        height=int(height),
        min_size=(MIN_WIDTH, MIN_HEIGHT),
        background_color=bg,
        text_select=False,
    )
    api.attach_window(window)

    def _on_loaded() -> None:
        apply_dwm_titlebar(settings.get("theme", "dark"))
        _bind_native_dnd(window, api)
        if not settings.get("offline_mode", False):
            def _bg_check() -> None:
                try:
                    api.check_updates(force=False)
                except Exception:
                    pass
            threading.Thread(target=_bg_check, name="flac-update-check", daemon=True).start()

    def _on_closing() -> None:
        scanner.shutdown(timeout=6.0)
        db.close()

    window.events.loaded += _on_loaded
    window.events.closing += _on_closing

    _patch_frozen_pywebview_runtimes()

    try:
        gui_backend = "edgechromium" if os.name == "nt" else None
        webview.start(gui=gui_backend, debug=debug)
    finally:
        scanner.shutdown(timeout=6.0)
        db.close()
    return 0


def _patch_frozen_pywebview_runtimes() -> None:
    """Guard pywebview 6.x edgechromium import against trimmed non-x64 loaders.

    PyInstaller drops x86 and arm64 DLLs from a win-x64 bundle, while
    ``webview.platforms.edgechromium`` queries ``interop_dll_path`` for all
    three architectures unconditionally at import time. Falling back to
    ``win-x64`` for the two unused architectures avoids a startup crash without
    shipping dead binaries.
    """
    if not getattr(sys, "frozen", False) or webview is None:
        return
    try:
        from webview import util as _wv_util
    except Exception:
        return
    orig = getattr(_wv_util, "interop_dll_path", None)
    if orig is None or getattr(orig, "_flac_verifier_patched", False):
        return

    def _safe_interop_dll_path(dll_name: str) -> str:
        try:
            return orig(dll_name)
        except FileNotFoundError:
            if dll_name in ("win-arm64", "win-x86"):
                return orig("win-x64")
            raise

    _safe_interop_dll_path._flac_verifier_patched = True  # type: ignore[attr-defined]
    _wv_util.interop_dll_path = _safe_interop_dll_path


if __name__ == "__main__":
    arg_path = sys.argv[1] if len(sys.argv) > 1 else None
    raise SystemExit(main(arg_path))
