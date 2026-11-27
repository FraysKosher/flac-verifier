# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 FLAC VERIFIER Contributors
"""Unit tests for `scan_manager.py` and `spectrogram_bridge.py` (Phase 2)."""
from __future__ import annotations

import base64
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

import numpy as np
from mutagen.flac import FLAC, Picture
from PIL import Image
import soundfile as sf

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

import fixtures as fx
import informe_pdf
import library_db as ldb
import motor_flac
import scan_manager as sm
import spectrogram_bridge as sb


def _write_test_flac(
    path: str,
    *,
    sr: int = 44100,
    dur: float = 1.5,
    cutoff_hz: float | None = None,
    seed: int = 1,
    embed_cover: bool = False,
) -> str:
    audio = fx.full_band_noise(sr=sr, dur=dur, seed=seed)
    if cutoff_hz is not None:
        audio = fx.brickwall(audio, sr, cutoff_hz)
    sf.write(path, audio, sr, subtype="PCM_16")
    if embed_cover:
        img = Image.new("RGB", (360, 360), (42, 118, 129))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        mf = FLAC(path)
        pic = Picture()
        pic.type = 3
        pic.mime = "image/png"
        pic.data = buf.getvalue()
        mf.add_picture(pic)
        mf.save()
    return path


class _FakeProcess:
    """Deterministic fake subprocess emitting NDJSON events for pause/cancel tests."""

    def __init__(self, lines: list[str], *, delay: float = 0.0, block_event: threading.Event | None = None) -> None:
        self.pid = 99999
        self._lines = list(lines)
        self._delay = delay
        self._block_event = block_event
        self._done = False
        self._rc: int | None = None
        self.stderr = io.StringIO("")
        self.stdout = self._iter_lines()

    def _iter_lines(self):
        for line in self._lines:
            if self._block_event is not None:
                self._block_event.wait(timeout=5.0)
            if self._delay > 0:
                time.sleep(self._delay)
            yield line + "\n"
        self._done = True
        self._rc = 0

    def poll(self) -> int | None:
        return self._rc

    def wait(self, timeout: float | None = None) -> int:
        self._done = True
        if self._rc is None:
            self._rc = 0
        return self._rc

    def terminate(self) -> None:
        self._rc = -15
        if self._block_event is not None:
            self._block_event.set()

    def kill(self) -> None:
        self._rc = -9
        if self._block_event is not None:
            self._block_event.set()


class TestEngineCommandResolution(unittest.TestCase):

    def test_source_and_frozen_command_building(self) -> None:
        src_cmd = sm.resolve_engine_command(frozen=False, executable=sys.executable)
        self.assertEqual(src_cmd[0], sys.executable)
        self.assertEqual(src_cmd[1], "-u")
        self.assertTrue(src_cmd[2].endswith("motor_flac.py"))

        fake_gui = os.path.join(ROOT, "dist", "FLAC_Verifier", "FLAC_Verifier.exe")
        fake_eng = os.path.join(ROOT, "dist", "FLAC_Verifier", "flac_motor.exe")
        with mock.patch.object(sm.os.path, "exists", lambda p: p == fake_eng):
            frozen_cmd = sm.resolve_engine_command(frozen=True, executable=fake_gui)
        self.assertEqual(frozen_cmd, [fake_eng, "--motor-cli"])

        with mock.patch.object(sm.os.path, "exists", return_value=False):
            fallback_cmd = sm.resolve_engine_command(frozen=True, executable=fake_gui)
        self.assertEqual(fallback_cmd, [fake_gui, "--motor-cli"])

        full_cmd = sm.build_engine_command(
            "D:/Music/Album",
            mode="center",
            pdf=True,
            output_dir="D:/Reports",
            workers=4,
            no_cache=True,
            base_cmd=["flac_motor.exe", "--motor-cli"],
        )
        self.assertEqual(
            full_cmd,
            [
                "flac_motor.exe", "--motor-cli",
                "--path", "D:/Music/Album",
                "--mode", "center",
                "--pdf",
                "--output-dir", "D:/Reports",
                "--workers", "4",
                "--no-cache",
            ],
        )


class TestScanManager(unittest.TestCase):

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="flac_v3_scan_")
        self.db_path = os.path.join(self.tmp, "library.db")
        self.covers_dir = os.path.join(self.tmp, "covers")
        self.db = ldb.LibraryDB(self.db_path)
        self.events: list[dict] = []

    def tearDown(self) -> None:
        self.db.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_library_root_scan_incremental_and_cover_extraction(self) -> None:
        root_dir = os.path.join(self.tmp, "MusicLibrary")
        alb1 = os.path.join(root_dir, "Album Alpha")
        alb2 = os.path.join(root_dir, "Album Beta")
        os.makedirs(alb1, exist_ok=True)
        os.makedirs(alb2, exist_ok=True)
        _write_test_flac(os.path.join(alb1, "01 - Track One.flac"), seed=1, embed_cover=True)
        _write_test_flac(os.path.join(alb2, "01 - Track Two.flac"), seed=2, embed_cover=False)

        root = self.db.add_root(root_dir, label="Test Lib")
        mgr = sm.ScanManager(
            self.db,
            engine_version=motor_flac.ENGINE_VERSION,
            covers_dir=self.covers_dir,
            on_event=self.events.append,
        )
        try:
            info1 = mgr.enqueue_root_scan(root["id"], workers=1, mode="center")
            self.assertEqual(info1["queued"], 2)
            self.assertTrue(mgr.wait_library(timeout=60.0))

            albums = self.db.query_albums(root_id=root["id"])
            self.assertEqual(len(albums), 2)
            alpha = next(a for a in albums if "Alpha" in a["folder_path"])
            self.assertIsNotNone(alpha["cover_hash"])
            self.assertEqual(alpha["engine_version"], motor_flac.ENGINE_VERSION)
            self.assertEqual(self.db.get_sidebar_counts()["tracks"], 2)

            # Second incremental scan skips both unchanged albums.
            info2 = mgr.enqueue_root_scan(root["id"], workers=1, mode="center")
            self.assertEqual(info2["queued"], 0)
            self.assertEqual(info2["skipped"], 2)
        finally:
            mgr.shutdown()

    def test_library_pause_resume_and_incremental_persistence(self) -> None:
        alb_dirs = [os.path.join(self.tmp, f"Alb{i}") for i in range(1, 4)]
        for folder in alb_dirs:
            os.makedirs(folder, exist_ok=True)
            open(os.path.join(folder, "01.flac"), "wb").close()

        gate = threading.Event()
        calls: list[str] = []

        def fake_popen(cmd, **_kwargs):
            path_idx = cmd.index("--path") + 1
            target = cmd[path_idx]
            calls.append(target)
            track_path = os.path.join(target, "01.flac")
            payload = {
                "tipo": "resultado",
                "archivo": "01.flac",
                "ruta": track_path,
                "veredicto": "GENUINE LOSSLESS",
                "score": 0.98,
                "meta": {"sample_rate": 44100, "bits_per_sample": 16, "canales": 2, "duracion": 10.0},
                "md5": {"estado": "match"},
                "dr": 11.0,
                "bdi": {"aplica": False},
                "esp": {"techo_hz": 21500.0, "nyquist": 22050},
                "problemas": [],
                "notes": [],
            }
            lines = [
                json.dumps({"tipo": "inicio", "total": 1}),
                json.dumps(payload),
                json.dumps({"tipo": "fin"}),
            ]
            # Block album 1 slightly so we can request pause while it runs.
            return _FakeProcess(lines, block_event=gate if len(calls) == 1 else None)

        paused_seen = threading.Event()

        def on_ev(ev: dict) -> None:
            self.events.append(ev)
            if ev.get("event") == "paused":
                paused_seen.set()

        mgr = sm.ScanManager(
            self.db,
            engine_version="17.2",
            covers_dir=self.covers_dir,
            on_event=on_ev,
            popen_factory=fake_popen,
        )
        try:
            mgr.enqueue_albums(alb_dirs, workers=1)
            # Wait briefly for album 1 to enter fake_popen, then request pause and release gate.
            deadline = time.monotonic() + 5.0
            while not calls and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(len(calls), 1)
            mgr.pause_library()
            gate.set()

            self.assertTrue(paused_seen.wait(timeout=5.0), "Library scanner did not pause")
            # Album 1 already persisted in SQLite; albums 2 and 3 have not started yet.
            self.assertEqual(len(self.db.query_albums()), 1)
            self.assertEqual(len(calls), 1)

            mgr.resume_library()
            self.assertTrue(mgr.wait_library(timeout=10.0))
            self.assertEqual(len(self.db.query_albums()), 3)
            self.assertEqual(len(calls), 3)
        finally:
            mgr.shutdown()

    def test_real_subprocess_cancellation_leaves_no_live_process(self) -> None:
        alb_dir = os.path.join(self.tmp, "CancelAlbum")
        os.makedirs(alb_dir, exist_ok=True)
        open(os.path.join(alb_dir, "01.flac"), "wb").close()

        # Spawn a real sleeper subprocess pretending to be the engine.
        sleeper_cmd = [sys.executable, "-u", "-c", "import time; print('{\"tipo\":\"inicio\",\"total\":1}', flush=True); time.sleep(60)"]
        started = threading.Event()
        spawned_procs: list[subprocess.Popen] = []

        def tracking_popen(_cmd, **kwargs):
            proc = subprocess.Popen(sleeper_cmd, **kwargs)
            spawned_procs.append(proc)
            started.set()
            return proc

        mgr = sm.ScanManager(
            self.db,
            engine_version="17.2",
            covers_dir=self.covers_dir,
            on_event=self.events.append,
            popen_factory=tracking_popen,
        )
        try:
            mgr.enqueue_albums([alb_dir], workers=1)
            self.assertTrue(started.wait(timeout=5.0))
            time.sleep(0.15)
            mgr.cancel_library()
            self.assertTrue(mgr.wait_library(timeout=10.0))
            self.assertEqual(len(spawned_procs), 1)
            self.assertIsNotNone(spawned_procs[0].poll(), "Cancelled subprocess is still alive")
            self.assertTrue(any(e.get("event") == "cancelled" for e in self.events))
        finally:
            mgr.shutdown()

    @unittest.skipUnless(informe_pdf.available(), "reportlab not installed")
    def test_quick_drop_pdf_album_vs_custom_folder_and_promote(self) -> None:
        root_dir = os.path.join(self.tmp, "DropMulti")
        alb1 = os.path.join(root_dir, "DropAlbumOne")
        alb2 = os.path.join(root_dir, "DropAlbumTwo")
        custom_out = os.path.join(self.tmp, "CustomReports")
        for folder, seed in ((alb1, 11), (alb2, 12)):
            os.makedirs(folder, exist_ok=True)
            _write_test_flac(os.path.join(folder, "01 - Song.flac"), seed=seed)

        mgr = sm.ScanManager(
            self.db,
            engine_version=motor_flac.ENGINE_VERSION,
            covers_dir=self.covers_dir,
            on_event=self.events.append,
        )
        try:
            # 1) Save PDF inside each album folder (flac_verifier_report.pdf).
            mgr.start_quick_drop(
                alb1,
                pdf=True,
                pdf_dest_mode="album",
                workers=1,
                mode="center",
            )
            self.assertTrue(mgr.wait_quick_drop(timeout=60.0))
            in_album_pdf = os.path.join(alb1, informe_pdf.REPORT_NAME)
            self.assertTrue(os.path.isfile(in_album_pdf))
            # Quick Drop must NOT add to library albums automatically.
            self.assertEqual(self.db.get_sidebar_counts()["albums"], 0)
            self.assertEqual(len(self.db.list_recent_quick_drops()), 1)

            # 2) Multi-album root with custom PDF folder (<Album>_flac_verifier_report.pdf).
            mgr.start_quick_drop(
                root_dir,
                pdf=True,
                pdf_dest_mode="custom",
                custom_pdf_dir=custom_out,
                workers=1,
                mode="center",
            )
            self.assertTrue(mgr.wait_quick_drop(timeout=90.0))
            self.assertTrue(
                os.path.isfile(os.path.join(custom_out, f"DropAlbumOne_{informe_pdf.REPORT_NAME}"))
            )
            self.assertTrue(
                os.path.isfile(os.path.join(custom_out, f"DropAlbumTwo_{informe_pdf.REPORT_NAME}"))
            )

            # 3) Explicit promote saves both albums into library.db without re-running engine.
            promoted = mgr.promote_last_quick_drop_to_library()
            self.assertEqual(len(promoted), 2)
            self.assertEqual(self.db.get_sidebar_counts()["albums"], 2)
        finally:
            mgr.shutdown()

    def test_library_scan_forwards_pdf_workers_and_no_cache_options(self) -> None:
        root_dir = os.path.join(self.tmp, "LibOptsRoot")
        alb_dir = os.path.join(root_dir, "AlbumA")
        custom_pdf = os.path.join(self.tmp, "LibCustomPDFs")
        os.makedirs(alb_dir, exist_ok=True)
        open(os.path.join(alb_dir, "01.flac"), "wb").close()
        root = self.db.add_root(root_dir)

        seen_cmds: list[list[str]] = []

        def fake_popen(cmd, **_kwargs):
            seen_cmds.append(list(cmd))
            lines = [
                json.dumps({"tipo": "inicio", "total": 1}),
                json.dumps({
                    "tipo": "resultado",
                    "archivo": "01.flac",
                    "ruta": os.path.join(alb_dir, "01.flac"),
                    "veredicto": "GENUINE LOSSLESS",
                    "score": 0.96,
                    "meta": {"sample_rate": 44100, "bits_per_sample": 16, "canales": 2, "duracion": 8.0},
                    "md5": {"estado": "match"},
                    "dr": 10.0,
                    "bdi": {"aplica": False},
                    "esp": {"techo_hz": 21200.0, "nyquist": 22050},
                    "problemas": [],
                    "notes": [],
                }),
                json.dumps({"tipo": "informe", "ruta": os.path.join(custom_pdf, "AlbumA_flac_verifier_report.pdf")}),
                json.dumps({"tipo": "fin"}),
            ]
            return _FakeProcess(lines)

        mgr = sm.ScanManager(
            self.db,
            engine_version="17.2",
            covers_dir=self.covers_dir,
            on_event=self.events.append,
            popen_factory=fake_popen,
        )
        try:
            info = mgr.enqueue_root_scan(
                int(root["id"]),
                workers=4,
                pdf=True,
                pdf_dest_mode="custom",
                custom_pdf_dir=custom_pdf,
                no_cache=True,
            )
            self.assertEqual(info["queued"], 1)
            self.assertTrue(mgr.wait_library(timeout=10.0))
            self.assertEqual(len(seen_cmds), 1)
            cmd = seen_cmds[0]
            self.assertIn("--pdf", cmd)
            self.assertIn("--no-cache", cmd)
            self.assertEqual(cmd[cmd.index("--workers") + 1], "4")
            self.assertEqual(cmd[cmd.index("--output-dir") + 1], ldb.normalize_path(custom_pdf))
            done_events = [e for e in self.events if e.get("event") == "album_done"]
            self.assertEqual(len(done_events), 1)
            self.assertEqual(len(done_events[0].get("pdf_paths") or []), 1)
        finally:
            mgr.shutdown()


class TestSpectrogramBridge(unittest.TestCase):

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="flac_v3_spec_")
        self.specs_dir = os.path.join(self.tmp, "specs")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_compact_grid_caching_and_brickwall_energy_drop(self) -> None:
        bw_path = _write_test_flac(
            os.path.join(self.tmp, "brickwall_16k.flac"),
            sr=44100,
            dur=2.0,
            cutoff_hz=16000,
            seed=5,
        )
        res_stub = {
            "meta": {"sample_rate": 44100},
            "esp": {"corte_artificial": True, "frecuencia_corte": 16000.0, "techo_hz": 16000.0},
        }
        first = sb.get_interactive_spectrogram(
            bw_path, result=res_stub, cache_dir=self.specs_dir
        )
        self.assertTrue(first["ok"])
        self.assertFalse(first["from_cache"])
        self.assertEqual(first["width"], sb.GRID_WIDTH)
        self.assertEqual(first["height"], sb.GRID_HEIGHT)
        self.assertAlmostEqual(first["nyquist_hz"], 22050.0, places=1)
        self.assertTrue(first["cache_path"] and os.path.isfile(first["cache_path"]))
        # Compressed cache file must stay compact (< 40 KB).
        self.assertLess(os.path.getsize(first["cache_path"]), 40_000)

        raw = base64.b64decode(first["grid_b64"])
        self.assertEqual(len(raw), sb.GRID_WIDTH * sb.GRID_HEIGHT)
        grid = np.frombuffer(raw, dtype=np.uint8).reshape(sb.GRID_HEIGHT, sb.GRID_WIDTH)

        # Row 0 = 0 Hz, Row 159 = 22.05 kHz. Check energy below 14 kHz vs above 18 kHz.
        low_band = float(np.mean(grid[20:95, :]))
        dead_band = float(np.mean(grid[135:158, :]))
        self.assertGreater(low_band - dead_band, 40.0)

        # Second call hits disk cache with identical grid bytes.
        second = sb.get_interactive_spectrogram(
            bw_path, result=res_stub, cache_dir=self.specs_dir
        )
        self.assertTrue(second["ok"])
        self.assertTrue(second["from_cache"])
        self.assertEqual(second["grid_b64"], first["grid_b64"])
        self.assertEqual(len(second["markers"]), 1)
        self.assertEqual(second["markers"][0]["kind"], "cutoff")

    def test_forensic_markers_cover_all_detectors_and_missing_file(self) -> None:
        missing = sb.get_interactive_spectrogram(os.path.join(self.tmp, "nope.flac"))
        self.assertFalse(missing["ok"])

        markers = sb.extract_forensic_markers({
            "meta": {"sample_rate": 96000},
            "esp": {
                "muro_brickwall_nyquist": True,
                "techo_hz": 24000.0,
                "gradiente_nyquist_db_khz": 61.6,
                "mdct_lattice_detected": True,
                "mdct_lattice_family": "MDCT-2048 (WMA)",
                "mdct_lattice_snr": 110.7,
                "neural_vocoder_detected": True,
                "vocoder_comb_spacing_hz": 1200.0,
                "vocoder_hf_comb_z": 18.2,
                "spectral_folding_detected": True,
                "spectral_folding_pivot_hz": 12000.0,
            },
        })
        kinds = {m["kind"] for m in markers}
        self.assertEqual(kinds, {"brickwall", "lattice", "neural", "folding", "reference"})

        # Hi-Res track without ultrasonic content gets hires_ceiling + subtle 22.05 kHz CD reference.
        hires_markers = sb.extract_forensic_markers({
            "meta": {"sample_rate": 96000},
            "veredicto": "PROBABLE UPSCALE",
            "finding_codes": ["hires_no_ultrasonic"],
            "esp": {"techo_hz": 21400.0, "nyquist": 48000},
        })
        hires_kinds = {m["kind"] for m in hires_markers}
        self.assertIn("hires_ceiling", hires_kinds)
        self.assertIn("reference", hires_kinds)
        ref = next(m for m in hires_markers if m["kind"] == "reference")
        self.assertAlmostEqual(ref["freq_hz"], 22050.0)
        self.assertTrue(ref.get("subtle"))

        # Clean 16/44.1 track has zero markers (so UI disables Markers button).
        clean_markers = sb.extract_forensic_markers({
            "meta": {"sample_rate": 44100},
            "veredicto": "GENUINE LOSSLESS",
            "esp": {"techo_hz": 21600.0, "nyquist": 22050},
        })
        self.assertEqual(clean_markers, [])

    def test_sequential_spectrogram_zero_seeks_speed_and_cancel(self) -> None:
        """Long FLAC must stream sequentially with zero `seek()` calls and fast timing."""
        long_path = _write_test_flac(
            os.path.join(self.tmp, "long_brickwall_20s.flac"),
            sr=44100,
            dur=20.0,
            cutoff_hz=16000,
            seed=9,
        )
        seek_calls: list[tuple[int, int]] = []
        orig_sf = sf.SoundFile

        class _NoSeekSpySoundFile(orig_sf):
            def seek(self, frames, whence=0):
                seek_calls.append((int(frames), int(whence)))
                return super().seek(frames, whence)

        t0 = time.perf_counter()
        with unittest.mock.patch.object(sb.sf, "SoundFile", _NoSeekSpySoundFile):
            res = sb.get_interactive_spectrogram(
                long_path, cache_dir=self.specs_dir, use_cache=False
            )
        elapsed = time.perf_counter() - t0

        self.assertTrue(res["ok"], res)
        # soundfile's internal `_cdata_io` updates its read pointer once per
        # sequential `_STREAM_BLOCK` (~7 blocks for 20s at 44.1k), whereas the
        # old random-seek strategy issued 256+ seeks across compressed frames.
        # Verify strictly sequential block reads (<< 256 seeks) and no `.seek(`
        # calls in `_compute_spectrogram_grid`.
        random_seeks = [c for c in seek_calls if c[1] == sf.SEEK_SET and c[0] > 0]
        self.assertLessEqual(
            len(random_seeks),
            12,
            f"Expected sequential streaming blocks, got {len(random_seeks)} seeks",
        )
        import inspect
        self.assertNotIn(".seek(", inspect.getsource(sb._compute_spectrogram_grid))
        self.assertLess(elapsed, 4.0, f"Sequential 20s FLAC spectrogram took too long: {elapsed:.2f}s")
        self.assertAlmostEqual(res["duration_s"], 20.0, delta=0.2)

        raw = base64.b64decode(res["grid_b64"])
        grid = np.frombuffer(raw, dtype=np.uint8).reshape(sb.GRID_HEIGHT, sb.GRID_WIDTH)
        # Verify spectral parity across the full timeline (columns 0..255).
        low_band = float(np.mean(grid[20:95, :]))
        dead_band = float(np.mean(grid[135:158, :]))
        self.assertGreater(low_band - dead_band, 40.0)
        # Ensure first and last quarters both carry real passband energy (no truncated tail).
        self.assertGreater(float(np.mean(grid[20:95, :64])), 80.0)
        self.assertGreater(float(np.mean(grid[20:95, 192:])), 80.0)

        # Pre-set cancellation event must abort immediately without writing a grid.
        cancel = threading.Event()
        cancel.set()
        cancelled = sb.get_interactive_spectrogram(
            long_path, cache_dir=self.specs_dir, use_cache=False, cancel_event=cancel
        )
        self.assertFalse(cancelled["ok"])
        self.assertTrue(cancelled.get("cancelled"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
