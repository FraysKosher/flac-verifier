# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 FLAC VERIFIER Contributors
"""Background job orchestrator for FLAC VERIFIER v3 (Library & Quick Drop).

Runs `motor_flac.py` / `flac_motor.exe` as an isolated subprocess speaking the
NDJSON protocol so analysis remains 100% inside the forensic engine while the
desktop shell stays responsive at all times.

Two independent job lanes:
  1. **Library queue**: processes album folders one by one so album-level
     coherence (`apply_album_coherence`) is preserved, extracts each album's
     cover once, commits each finished album atomically to `library.db`, and
     supports pause, resume, and clean cancellation without orphaned workers.
  2. **Quick Drop queue**: full parity with v2.13.2 — accepts a single `.flac`,
     an album folder, or a multi-album root folder; supports PDF generation in
     each album folder (`flac_verifier_report.pdf`) or a custom destination
     folder (`<Album>_flac_verifier_report.pdf`), worker count, and `--no-cache`,
     without polluting `library.db` unless explicitly promoted.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import json
import os
import signal
import subprocess
import sys
import threading
import time
from typing import Any, Callable

import covers as covers_mod
import library_db as ldb

DIRECTORY = os.path.dirname(os.path.abspath(__file__))
ENGINE_SCRIPT = os.path.join(DIRECTORY, "motor_flac.py")
ENGINE_EXE_NAME = "flac_motor.exe"
FLAG_ENGINE_CLI = "--motor-cli"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

EventCallback = Callable[[dict[str, Any]], None]


def is_frozen() -> bool:
    """True when running inside a PyInstaller executable."""
    return bool(getattr(sys, "frozen", False))


def resolve_engine_command(
    *,
    frozen: bool | None = None,
    executable: str | None = None,
) -> list[str]:
    """Resolves the base command to invoke the forensic engine subprocess."""
    packaged = is_frozen() if frozen is None else bool(frozen)
    exe_path = sys.executable if executable is None else executable
    if packaged:
        folder = os.path.dirname(os.path.abspath(exe_path))
        engine_exe = os.path.join(folder, ENGINE_EXE_NAME)
        if os.path.exists(engine_exe):
            return [engine_exe, FLAG_ENGINE_CLI]
        return [exe_path, FLAG_ENGINE_CLI]
    return [exe_path, "-u", ENGINE_SCRIPT]


def build_engine_command(
    target_path: str,
    *,
    mode: str = "full",
    seconds: int | None = None,
    pdf: bool = False,
    png: bool = False,
    output_dir: str | None = None,
    workers: int = 0,
    no_cache: bool = False,
    base_cmd: list[str] | None = None,
) -> list[str]:
    """Builds the exact CLI invocation for `motor_flac.py` / `flac_motor.exe`."""
    cmd = list(base_cmd) if base_cmd is not None else resolve_engine_command()
    cmd += ["--path", target_path]
    if mode and mode != "full":
        cmd += ["--mode", mode]
        if mode == "seconds" and seconds:
            cmd += ["--seconds", str(int(seconds))]
    if pdf:
        cmd.append("--pdf")
    elif png:
        cmd.append("--png")
    if output_dir is not None and str(output_dir).strip():
        cmd += ["--output-dir", str(output_dir).strip()]
    if workers and int(workers) > 0:
        cmd += ["--workers", str(int(workers))]
    if no_cache:
        cmd.append("--no-cache")
    return cmd


def parse_ndjson_line(line: str) -> dict[str, Any] | None:
    """Parses one NDJSON protocol line safely without crashing the reader."""
    text = (line or "").strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except ValueError:
        return {"tipo": "__unreadable__", "mensaje": text[:300]}
    if not isinstance(parsed, dict):
        return {"tipo": "__unreadable__", "mensaje": text[:300]}
    return parsed


def terminate_process_tree(
    process: "subprocess.Popen[str] | None",
    *,
    wait_timeout: float = 4.0,
) -> None:
    """Terminates an engine subprocess and all spawned child workers cleanly.

    On Windows uses `taskkill /PID <pid> /T /F` so `ProcessPoolExecutor` child
    workers cannot survive as orphans after cancellation or app shutdown.
    """
    if process is None:
        return
    try:
        if process.poll() is not None:
            return
    except Exception:
        return

    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=15,
                creationflags=NO_WINDOW,
            )
            process.wait(timeout=wait_timeout)
            return
        except Exception:
            pass
    else:
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                pgid = os.getpgid(process.pid)
                os.killpg(pgid, sig)
                process.wait(timeout=wait_timeout)
                return
            except Exception:
                continue

    for action in (process.terminate, process.kill):
        try:
            action()
            process.wait(timeout=wait_timeout)
            return
        except Exception:
            continue


def _close_pipes(process: "subprocess.Popen[str] | None") -> None:
    if process is None:
        return
    for stream in (process.stdout, process.stderr):
        if stream is not None:
            try:
                stream.close()
            except Exception:
                pass


@dataclass
class LibraryAlbumTask:
    """Single album queued for background library indexing."""

    folder_path: str
    flac_files: list[str]
    root_id: int | None = None
    force: bool = False
    workers: int = 0
    mode: str = "full"
    no_cache: bool = False
    pdf: bool = False
    pdf_dest_mode: str = "album"
    custom_pdf_dir: str | None = None


@dataclass
class QuickDropJob:
    """One-off Quick Drop audit request (file, album, or multi-album root)."""

    target_path: str
    album_targets: list[str]
    pdf: bool = False
    pdf_dest_mode: str = "album"  # "album" | "custom"
    custom_pdf_dir: str | None = None
    workers: int = 0
    no_cache: bool = False
    mode: str = "full"


@dataclass
class _SubprocessRunResult:
    results: list[dict[str, Any]] = field(default_factory=list)
    pdf_paths: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    cancelled: bool = False
    returncode: int | None = None


class ScanManager:
    """Coordinates background library indexing and Quick Drop audits."""

    def __init__(
        self,
        db: ldb.LibraryDB,
        *,
        engine_version: str,
        covers_dir: str | None = None,
        on_event: EventCallback | None = None,
        base_cmd: list[str] | None = None,
        popen_factory: Callable[..., Any] = subprocess.Popen,
    ) -> None:
        self.db = db
        self.engine_version = str(engine_version)
        self.covers_dir = covers_dir
        self._on_event = on_event
        self._base_cmd = base_cmd
        self._popen = popen_factory

        # Library lane state
        self._lib_lock = threading.RLock()
        self._lib_queue: deque[LibraryAlbumTask] = deque()
        self._lib_thread: threading.Thread | None = None
        self._lib_proc: "subprocess.Popen[str] | None" = None
        self._lib_pause_gate = threading.Event()
        self._lib_pause_gate.set()  # set = running, cleared = paused
        self._lib_cancel = threading.Event()
        self._lib_state = "idle"    # idle | scanning | pausing | paused
        self._lib_total_albums = 0
        self._lib_done_albums = 0
        self._lib_skipped_albums = 0
        self._lib_current_album = ""
        self._lib_current_album_path = ""
        self._lib_current_track_file = ""
        self._lib_current_track_idx = 0
        self._lib_current_track_total = 0
        self._lib_active_workers = 0
        self._lib_touched_roots: set[int] = set()

        # Quick Drop lane state
        self._drop_lock = threading.RLock()
        self._drop_thread: threading.Thread | None = None
        self._drop_proc: "subprocess.Popen[str] | None" = None
        self._drop_cancel = threading.Event()
        self._drop_state = "idle"   # idle | running | finished | cancelled | error
        self._drop_last_results: dict[str, list[dict[str, Any]]] = {}
        self._drop_last_pdfs: list[str] = []

    def _emit(self, payload: dict[str, Any]) -> None:
        cb = self._on_event
        if cb is not None:
            try:
                cb(payload)
            except Exception:
                pass

    # ── Status snapshots ─────────────────────────────────────────────────────

    def library_status(self) -> dict[str, Any]:
        with self._lib_lock:
            up_next = [
                {
                    "title": os.path.basename(t.folder_path) or t.folder_path,
                    "folder_path": t.folder_path,
                    "track_count": len(t.flac_files),
                }
                for t in list(self._lib_queue)[:25]
            ]
            return {
                "state": self._lib_state,
                "paused": not self._lib_pause_gate.is_set(),
                "queued_albums": len(self._lib_queue),
                "total_albums": self._lib_total_albums,
                "done_albums": self._lib_done_albums,
                "skipped_albums": self._lib_skipped_albums,
                "current_album": self._lib_current_album,
                "current_album_path": self._lib_current_album_path,
                "current_track_file": self._lib_current_track_file,
                "current_track_index": self._lib_current_track_idx,
                "current_track_total": self._lib_current_track_total,
                "active_workers": self._lib_active_workers,
                "up_next": up_next,
            }

    def quick_drop_status(self) -> dict[str, Any]:
        with self._drop_lock:
            return {
                "state": self._drop_state,
                "pdf_paths": list(self._drop_last_pdfs),
                "album_count": len(self._drop_last_results),
            }

    # ── Subprocess execution core ────────────────────────────────────────────

    def _run_engine_subprocess(
        self,
        command: list[str],
        *,
        cancel_event: threading.Event,
        store_proc: Callable[["subprocess.Popen[str] | None"], None],
        on_track_event: Callable[[dict[str, Any]], None] | None = None,
        on_start_event: Callable[[dict[str, Any]], None] | None = None,
        on_report_event: Callable[[dict[str, Any]], None] | None = None,
        on_warning_event: Callable[[dict[str, Any]], None] | None = None,
    ) -> _SubprocessRunResult:
        outcome = _SubprocessRunResult()
        popen_kwargs: dict[str, Any] = {
            "cwd": DIRECTORY,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
            "bufsize": 1,
        }
        if os.name == "nt":
            popen_kwargs["creationflags"] = NO_WINDOW
        else:
            popen_kwargs["start_new_session"] = True

        try:
            proc = self._popen(command, **popen_kwargs)
        except TypeError:
            popen_kwargs.pop("start_new_session", None)
            proc = self._popen(command, **popen_kwargs)

        store_proc(proc)
        stderr_lines: list[str] = []

        def _drain_stderr() -> None:
            if proc.stderr is None:
                return
            try:
                for line in proc.stderr:
                    txt = line.strip()
                    if txt:
                        stderr_lines.append(txt[:300])
            except Exception:
                pass

        err_thread = threading.Thread(target=_drain_stderr, daemon=True)
        err_thread.start()

        try:
            if proc.stdout is not None:
                for raw_line in proc.stdout:
                    if cancel_event.is_set():
                        outcome.cancelled = True
                        break
                    ev = parse_ndjson_line(raw_line)
                    if ev is None:
                        continue
                    kind = ev.get("tipo")
                    if kind == "inicio":
                        if on_start_event:
                            on_start_event(ev)
                    elif kind == "resultado":
                        outcome.results.append(ev)
                        if on_track_event:
                            on_track_event(ev)
                    elif kind == "informe":
                        pdf_path = str(ev.get("ruta") or "")
                        if pdf_path:
                            outcome.pdf_paths.append(pdf_path)
                        if on_report_event:
                            on_report_event(ev)
                    elif kind == "aviso":
                        msg = str(ev.get("mensaje") or "")
                        if msg:
                            outcome.warnings.append(msg)
                        if on_warning_event:
                            on_warning_event(ev)
                    elif kind == "error":
                        msg = str(ev.get("mensaje") or "Engine error")
                        outcome.errors.append(msg)
        except Exception as exc:
            if not cancel_event.is_set():
                outcome.errors.append(f"Reader error: {type(exc).__name__}: {exc}")
        finally:
            if cancel_event.is_set():
                outcome.cancelled = True
                terminate_process_tree(proc)
            _close_pipes(proc)
            err_thread.join(timeout=2.0)
            try:
                outcome.returncode = proc.wait(timeout=5.0)
            except Exception:
                terminate_process_tree(proc)
                outcome.returncode = proc.poll()
            store_proc(None)

        if not outcome.cancelled and outcome.returncode not in (0, None) and not outcome.errors:
            detail = stderr_lines[-1] if stderr_lines else f"exit code {outcome.returncode}"
            outcome.errors.append(f"Engine exited abnormally ({detail})")
        return outcome

    # ── Lane 1: Library Background Queue ─────────────────────────────────────

    def enqueue_root_scan(
        self,
        root_id: int,
        *,
        force: bool = False,
        workers: int = 0,
        mode: str = "full",
        pdf: bool = False,
        pdf_dest_mode: str = "album",
        custom_pdf_dir: str | None = None,
        no_cache: bool = False,
    ) -> dict[str, Any]:
        """Discovers all albums under a configured root folder and queues updates."""
        roots = self.db.list_roots()
        root = next((r for r in roots if int(r["id"]) == int(root_id)), None)
        if root is None:
            raise ValueError(f"Unknown library root id: {root_id}")

        discovered = ldb.discover_albums(root["path"])
        tasks: list[LibraryAlbumTask] = []
        skipped = 0
        effective_force = bool(force or no_cache)
        norm_pdf_dir = ldb.normalize_path(custom_pdf_dir) if custom_pdf_dir else None
        for item in discovered:
            plan = self.db.plan_incremental_scan(
                item["folder_path"],
                item["flac_files"],
                self.engine_version,
                force=effective_force,
            )
            if not plan["album_needs_scan"] and not pdf:
                skipped += 1
                continue
            tasks.append(
                LibraryAlbumTask(
                    folder_path=item["folder_path"],
                    flac_files=item["flac_files"],
                    root_id=int(root_id),
                    force=effective_force,
                    workers=int(workers or 0),
                    mode=mode,
                    no_cache=bool(no_cache),
                    pdf=bool(pdf),
                    pdf_dest_mode="custom" if pdf_dest_mode == "custom" else "album",
                    custom_pdf_dir=norm_pdf_dir,
                )
            )

        if not tasks:
            self.db.touch_root_scanned(int(root_id))
            self._emit({
                "lane": "library",
                "event": "up_to_date",
                "root_id": int(root_id),
                "discovered": len(discovered),
                "skipped": skipped,
            })
            return {"queued": 0, "skipped": skipped, "discovered": len(discovered)}

        self._enqueue_library_tasks(tasks, skipped=skipped, root_id=int(root_id))
        return {"queued": len(tasks), "skipped": skipped, "discovered": len(discovered)}

    def enqueue_all_roots_scan(
        self,
        *,
        force: bool = False,
        workers: int = 0,
        mode: str = "full",
        pdf: bool = False,
        pdf_dest_mode: str = "album",
        custom_pdf_dir: str | None = None,
        no_cache: bool = False,
    ) -> dict[str, int]:
        """Scans every online root folder incrementally."""
        total_queued = 0
        total_skipped = 0
        total_discovered = 0
        for root in self.db.list_roots():
            if not root.get("enabled") or not root.get("online"):
                continue
            info = self.enqueue_root_scan(
                int(root["id"]),
                force=force,
                workers=workers,
                mode=mode,
                pdf=pdf,
                pdf_dest_mode=pdf_dest_mode,
                custom_pdf_dir=custom_pdf_dir,
                no_cache=no_cache,
            )
            total_queued += info["queued"]
            total_skipped += info["skipped"]
            total_discovered += info["discovered"]
        return {
            "queued": total_queued,
            "skipped": total_skipped,
            "discovered": total_discovered,
        }

    def enqueue_albums(
        self,
        album_folders: list[str],
        *,
        root_id: int | None = None,
        force: bool = True,
        workers: int = 0,
        mode: str = "full",
        no_cache: bool = False,
        pdf: bool = False,
        pdf_dest_mode: str = "album",
        custom_pdf_dir: str | None = None,
    ) -> int:
        """Queues explicit album folders (for re-verify album or engine upgrades)."""
        tasks: list[LibraryAlbumTask] = []
        norm_pdf_dir = ldb.normalize_path(custom_pdf_dir) if custom_pdf_dir else None
        for folder in album_folders:
            discovered = ldb.discover_albums(folder)
            for item in discovered:
                tasks.append(
                    LibraryAlbumTask(
                        folder_path=item["folder_path"],
                        flac_files=item["flac_files"],
                        root_id=root_id,
                        force=force,
                        workers=int(workers or 0),
                        mode=mode,
                        no_cache=bool(no_cache),
                        pdf=bool(pdf),
                        pdf_dest_mode="custom" if pdf_dest_mode == "custom" else "album",
                        custom_pdf_dir=norm_pdf_dir,
                    )
                )
        if tasks:
            self._enqueue_library_tasks(tasks, skipped=0, root_id=root_id)
        return len(tasks)

    def _enqueue_library_tasks(
        self,
        tasks: list[LibraryAlbumTask],
        *,
        skipped: int = 0,
        root_id: int | None = None,
    ) -> None:
        with self._lib_lock:
            is_running = self._lib_thread is not None and self._lib_thread.is_alive()
            if not is_running:
                self._lib_total_albums = 0
                self._lib_done_albums = 0
                self._lib_skipped_albums = 0
                self._lib_touched_roots.clear()
                self._lib_cancel.clear()
                self._lib_pause_gate.set()

            existing_keys = {ldb.path_key(t.folder_path) for t in self._lib_queue}
            added = 0
            for task in tasks:
                tkey = ldb.path_key(task.folder_path)
                if tkey not in existing_keys:
                    self._lib_queue.append(task)
                    existing_keys.add(tkey)
                    added += 1

            self._lib_total_albums += added
            self._lib_skipped_albums += skipped
            if root_id is not None:
                self._lib_touched_roots.add(int(root_id))

            if not is_running and self._lib_queue:
                self._lib_state = "scanning"
                self._lib_thread = threading.Thread(
                    target=self._library_worker,
                    name="flac-library-scanner",
                    daemon=True,
                )
                self._lib_thread.start()

    def pause_library(self) -> None:
        """Pauses the library queue after the active album finishes."""
        with self._lib_lock:
            if self._lib_state == "scanning":
                self._lib_pause_gate.clear()
                self._lib_state = "pausing" if self._lib_proc is not None else "paused"
                self._emit({"lane": "library", "event": self._lib_state, **self.library_status()})

    def resume_library(self) -> None:
        """Resumes a paused library queue."""
        with self._lib_lock:
            if self._lib_state in ("pausing", "paused"):
                self._lib_pause_gate.set()
                self._lib_state = "scanning"
                self._emit({"lane": "library", "event": "resumed", **self.library_status()})

    def cancel_library(self) -> None:
        """Cancels the library queue immediately, terminating any active engine."""
        with self._lib_lock:
            self._lib_cancel.set()
            self._lib_pause_gate.set()
            self._lib_queue.clear()
            proc = self._lib_proc
        terminate_process_tree(proc)
        _close_pipes(proc)

    def wait_library(self, timeout: float = 30.0) -> bool:
        """Blocks until the library worker thread exits (for tests/shutdown)."""
        thread = self._lib_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
        return thread is None or not thread.is_alive()

    def _store_lib_proc(self, proc: "subprocess.Popen[str] | None") -> None:
        with self._lib_lock:
            self._lib_proc = proc

    def _library_worker(self) -> None:
        self._emit({"lane": "library", "event": "started", **self.library_status()})
        cancelled = False
        try:
            while True:
                if self._lib_cancel.is_set():
                    cancelled = True
                    break

                if not self._lib_pause_gate.is_set():
                    with self._lib_lock:
                        self._lib_state = "paused"
                    self._emit({"lane": "library", "event": "paused", **self.library_status()})
                    self._lib_pause_gate.wait()
                    if self._lib_cancel.is_set():
                        cancelled = True
                        break
                    with self._lib_lock:
                        self._lib_state = "scanning"

                with self._lib_lock:
                    if not self._lib_queue:
                        break
                    task = self._lib_queue.popleft()
                    self._lib_current_album = os.path.basename(task.folder_path) or task.folder_path
                    self._lib_current_album_path = task.folder_path
                    self._lib_current_track_file = ""
                    self._lib_current_track_idx = 0
                    self._lib_current_track_total = len(task.flac_files)
                    self._lib_active_workers = (
                        int(task.workers)
                        if int(task.workers or 0) > 0
                        else min(8, os.cpu_count() or 4)
                    )

                self._emit({
                    "lane": "library",
                    "event": "album_start",
                    "folder_path": task.folder_path,
                    **self.library_status(),
                })

                pdf_out = (
                    task.custom_pdf_dir
                    if (task.pdf and task.pdf_dest_mode == "custom" and task.custom_pdf_dir)
                    else None
                )
                cmd = build_engine_command(
                    task.folder_path,
                    mode=task.mode,
                    pdf=bool(task.pdf),
                    png=False,
                    output_dir=pdf_out,
                    workers=task.workers,
                    no_cache=task.no_cache,
                    base_cmd=self._base_cmd,
                )

                def _on_start(ev: dict[str, Any]) -> None:
                    with self._lib_lock:
                        self._lib_current_track_total = int(
                            ev.get("total") or self._lib_current_track_total
                        )
                        if ev.get("workers"):
                            self._lib_active_workers = int(ev.get("workers") or self._lib_active_workers)

                def _on_track(ev: dict[str, Any]) -> None:
                    with self._lib_lock:
                        self._lib_current_track_idx += 1
                        self._lib_current_track_file = str(ev.get("archivo") or "")
                        status = self.library_status()
                    self._emit({
                        "lane": "library",
                        "event": "track_progress",
                        "file": ev.get("archivo"),
                        **status,
                    })

                run_res = self._run_engine_subprocess(
                    cmd,
                    cancel_event=self._lib_cancel,
                    store_proc=self._store_lib_proc,
                    on_start_event=_on_start,
                    on_track_event=_on_track,
                )

                if run_res.cancelled or self._lib_cancel.is_set():
                    cancelled = True
                    break

                if run_res.results:
                    cover_hash = covers_mod.extract_album_cover(
                        task.folder_path,
                        task.flac_files,
                        cache_dir=self.covers_dir,
                    )
                    album_row = self.db.upsert_album_results(
                        task.folder_path,
                        run_res.results,
                        self.engine_version,
                        root_id=task.root_id,
                        cover_hash=cover_hash,
                        prune_missing=True,
                    )
                else:
                    album_row = None

                with self._lib_lock:
                    self._lib_done_albums += 1
                    status = self.library_status()
                self._emit({
                    "lane": "library",
                    "event": "album_done",
                    "folder_path": task.folder_path,
                    "album": album_row,
                    "pdf_paths": run_res.pdf_paths,
                    "errors": run_res.errors,
                    **status,
                })

            if not cancelled:
                with self._lib_lock:
                    touched = list(self._lib_touched_roots)
                for rid in touched:
                    try:
                        self.db.touch_root_scanned(rid)
                    except Exception:
                        pass
        finally:
            with self._lib_lock:
                self._lib_state = "idle"
                self._lib_current_album = ""
                self._lib_current_album_path = ""
                self._lib_current_track_file = ""
                self._lib_current_track_idx = 0
                self._lib_current_track_total = 0
                self._lib_active_workers = 0
                final_status = self.library_status()
            self._emit({
                "lane": "library",
                "event": "cancelled" if cancelled else "finished",
                **final_status,
            })

    # ── Lane 2: Quick Drop (100% parity with v2.13.2) ────────────────────────

    def start_quick_drop(
        self,
        target_path: str,
        *,
        pdf: bool = False,
        pdf_dest_mode: str = "album",
        custom_pdf_dir: str | None = None,
        workers: int = 0,
        no_cache: bool = False,
        mode: str = "full",
    ) -> dict[str, Any]:
        """Starts a one-off Quick Drop scan (single file, album, or root folder)."""
        norm_target = ldb.normalize_path(target_path)
        if not norm_target or not os.path.exists(norm_target):
            raise ValueError("Selected path does not exist")

        if os.path.isfile(norm_target):
            if not norm_target.lower().endswith(".flac"):
                raise ValueError("Selected file is not a .flac audio file")
            album_targets = [norm_target]
        else:
            discovered = ldb.discover_albums(norm_target)
            album_targets = [item["folder_path"] for item in discovered]
            if not album_targets:
                raise ValueError("No .flac files found in selected folder")

        if pdf and pdf_dest_mode == "custom":
            clean_out = ldb.normalize_path(custom_pdf_dir or "")
            if not clean_out:
                raise ValueError("Select a custom destination folder for the PDF report")
        else:
            clean_out = None

        with self._drop_lock:
            if self._drop_thread is not None and self._drop_thread.is_alive():
                raise RuntimeError("A Quick Drop scan is already running")
            self._drop_cancel.clear()
            self._drop_state = "running"
            self._drop_last_results = {}
            self._drop_last_pdfs = []
            job = QuickDropJob(
                target_path=norm_target,
                album_targets=album_targets,
                pdf=bool(pdf),
                pdf_dest_mode="custom" if pdf_dest_mode == "custom" else "album",
                custom_pdf_dir=clean_out,
                workers=int(workers or 0),
                no_cache=bool(no_cache),
                mode=mode,
            )
            self._drop_thread = threading.Thread(
                target=self._quick_drop_worker,
                args=(job,),
                name="flac-quick-drop",
                daemon=True,
            )
            self._drop_thread.start()

        return {
            "target_path": norm_target,
            "album_count": len(album_targets),
            "pdf": job.pdf,
            "pdf_dest_mode": job.pdf_dest_mode,
        }

    def cancel_quick_drop(self) -> None:
        """Cancels an active Quick Drop run immediately."""
        with self._drop_lock:
            self._drop_cancel.set()
            proc = self._drop_proc
        terminate_process_tree(proc)
        _close_pipes(proc)

    def wait_quick_drop(self, timeout: float = 30.0) -> bool:
        thread = self._drop_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
        return thread is None or not thread.is_alive()

    def promote_last_quick_drop_to_library(self) -> list[dict[str, Any]]:
        """Persists the most recent Quick Drop results into `library.db`."""
        with self._drop_lock:
            snapshot = {k: list(v) for k, v in self._drop_last_results.items()}
        saved: list[dict[str, Any]] = []
        for folder_path, results in snapshot.items():
            if not results:
                continue
            flac_paths = [
                ldb.normalize_path(str(r.get("ruta") or ""))
                for r in results
                if r.get("ruta")
            ]
            cover_hash = covers_mod.extract_album_cover(
                folder_path,
                flac_paths or None,
                cache_dir=self.covers_dir,
            )
            alb = self.db.upsert_album_results(
                folder_path,
                results,
                self.engine_version,
                cover_hash=cover_hash,
                prune_missing=False,
            )
            saved.append(alb)
        return saved

    def _store_drop_proc(self, proc: "subprocess.Popen[str] | None") -> None:
        with self._drop_lock:
            self._drop_proc = proc

    def _quick_drop_worker(self, job: QuickDropJob) -> None:
        total_albums = len(job.album_targets)
        total_tracks = 0
        flagged_tracks = 0
        all_pdfs: list[str] = []
        all_errors: list[str] = []
        cancelled = False

        self._emit({
            "lane": "quick_drop",
            "event": "started",
            "target_path": job.target_path,
            "total_albums": total_albums,
        })

        try:
            for idx, target in enumerate(job.album_targets, start=1):
                if self._drop_cancel.is_set():
                    cancelled = True
                    break

                album_folder = (
                    os.path.dirname(target) if os.path.isfile(target) else target
                )
                output_dir = (
                    job.custom_pdf_dir
                    if (job.pdf and job.pdf_dest_mode == "custom")
                    else None
                )
                cmd = build_engine_command(
                    target,
                    mode=job.mode,
                    pdf=job.pdf,
                    png=False,
                    output_dir=output_dir,
                    workers=job.workers,
                    no_cache=job.no_cache,
                    base_cmd=self._base_cmd,
                )

                self._emit({
                    "lane": "quick_drop",
                    "event": "album_start",
                    "album_index": idx,
                    "total_albums": total_albums,
                    "target": target,
                })

                def _on_track(ev: dict[str, Any]) -> None:
                    self._emit({
                        "lane": "quick_drop",
                        "event": "track_result",
                        "album_index": idx,
                        "total_albums": total_albums,
                        "album_folder": album_folder,
                        "result": ev,
                    })

                def _on_report(ev: dict[str, Any]) -> None:
                    self._emit({
                        "lane": "quick_drop",
                        "event": "pdf_report",
                        "album_index": idx,
                        "ruta": ev.get("ruta"),
                        "paginas": ev.get("paginas"),
                    })

                def _on_warning(ev: dict[str, Any]) -> None:
                    self._emit({
                        "lane": "quick_drop",
                        "event": "warning",
                        "mensaje": ev.get("mensaje"),
                    })

                run_res = self._run_engine_subprocess(
                    cmd,
                    cancel_event=self._drop_cancel,
                    store_proc=self._store_drop_proc,
                    on_track_event=_on_track,
                    on_report_event=_on_report,
                    on_warning_event=_on_warning,
                )

                if run_res.cancelled or self._drop_cancel.is_set():
                    cancelled = True
                    break

                if run_res.results:
                    with self._drop_lock:
                        self._drop_last_results.setdefault(album_folder, []).extend(
                            run_res.results
                        )
                    total_tracks += len(run_res.results)
                    for r in run_res.results:
                        verdict = str(r.get("veredicto") or "")
                        if r.get("error") or verdict in ("PROBABLE UPSCALE", "SUSPICIOUS"):
                            flagged_tracks += 1

                if run_res.pdf_paths:
                    all_pdfs.extend(run_res.pdf_paths)
                    with self._drop_lock:
                        self._drop_last_pdfs = list(all_pdfs)
                if run_res.errors:
                    all_errors.extend(run_res.errors)

            if not cancelled and total_tracks > 0:
                self.db.record_quick_drop(
                    job.target_path,
                    track_count=total_tracks,
                    flagged_count=flagged_tracks,
                    engine_version=self.engine_version,
                    summary={
                        "album_count": total_albums,
                        "pdf_paths": all_pdfs,
                        "errors": all_errors,
                    },
                )
        finally:
            final_state = (
                "cancelled"
                if cancelled
                else ("error" if (all_errors and total_tracks == 0) else "finished")
            )
            with self._drop_lock:
                self._drop_state = final_state
            self._emit({
                "lane": "quick_drop",
                "event": final_state,
                "target_path": job.target_path,
                "track_count": total_tracks,
                "flagged_count": flagged_tracks,
                "pdf_paths": all_pdfs,
                "errors": all_errors,
            })

    # ── On-demand PDF report for an indexed library album ────────────────────

    def generate_album_pdf(
        self,
        album_folder: str,
        *,
        output_dir: str | None = None,
        workers: int = 0,
        mode: str = "full",
    ) -> dict[str, Any]:
        """Generates a PDF report for an existing album folder via the engine."""
        norm = ldb.normalize_path(album_folder)
        if not norm or not os.path.exists(norm):
            return {"ok": False, "error": "Album folder not found"}
        clean_out = ldb.normalize_path(output_dir) if output_dir else None
        cmd = build_engine_command(
            norm,
            mode=mode,
            pdf=True,
            output_dir=clean_out,
            workers=workers,
            no_cache=False,
            base_cmd=self._base_cmd,
        )
        dummy_cancel = threading.Event()
        res = self._run_engine_subprocess(
            cmd,
            cancel_event=dummy_cancel,
            store_proc=lambda _p: None,
        )
        if res.pdf_paths:
            return {"ok": True, "pdf_path": res.pdf_paths[0], "warnings": res.warnings}
        err = res.warnings[0] if res.warnings else (res.errors[0] if res.errors else "PDF report was not generated")
        return {"ok": False, "error": err}

    # ── Clean shutdown ───────────────────────────────────────────────────────

    def shutdown(self, timeout: float = 10.0) -> None:
        """Cancels all active jobs and reaps subprocesses before window exit."""
        self.cancel_library()
        self.cancel_quick_drop()
        deadline = time.monotonic() + timeout
        for wait_fn in (self.wait_library, self.wait_quick_drop):
            remaining = max(0.1, deadline - time.monotonic())
            wait_fn(timeout=remaining)
