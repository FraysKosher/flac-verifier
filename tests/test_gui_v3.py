# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 FLAC VERIFIER Contributors
"""Headless unit tests for the v3 desktop shell & Python-JS API (`gui_v3.py`)."""
from __future__ import annotations

import base64
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from PIL import Image
import soundfile as sf

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

import covers
import fixtures as fx
import gui_v3
import library_db as ldb
import motor_flac
import scan_manager as sm


def _wsgi_get(app, path: str) -> tuple[str, dict[str, str], bytes]:
    captured: list = []

    def start_response(status, headers, _exc=None):
        captured.append((status, {k.lower(): v for k, v in headers}))

    environ = {
        "REQUEST_METHOD": "GET",
        "PATH_INFO": path,
        "QUERY_STRING": "",
        "SERVER_NAME": "127.0.0.1",
        "SERVER_PORT": "80",
        "wsgi.version": (1, 0),
        "wsgi.url_scheme": "http",
        "wsgi.input": io.BytesIO(b""),
        "wsgi.errors": io.StringIO(),
        "wsgi.multithread": False,
        "wsgi.multiprocess": False,
        "wsgi.run_once": False,
    }
    result = app(environ, start_response)
    try:
        body = b"".join(result)
    finally:
        if hasattr(result, "close"):
            result.close()
    status, headers = captured[0]
    return status, headers, body


class TestGuiV3Headless(unittest.TestCase):

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="flac_v3_gui_")
        self.data_dir = os.path.join(self.tmp, "data")
        self.covers_dir = os.path.join(self.data_dir, "covers")
        self.specs_dir = os.path.join(self.data_dir, "specs")
        self.db = ldb.LibraryDB(os.path.join(self.data_dir, "library.db"))
        self.events: list[dict] = []
        self.scanner = sm.ScanManager(
            self.db,
            engine_version=motor_flac.ENGINE_VERSION,
            covers_dir=self.covers_dir,
        )
        self.api = gui_v3.FlacVerifierApi(
            self.db,
            self.scanner,
            data_dir=self.data_dir,
            covers_dir=self.covers_dir,
            specs_dir=self.specs_dir,
            event_sink=self.events.append,
        )

    def tearDown(self) -> None:
        self.scanner.shutdown()
        self.db.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_degrades_gracefully_without_pywebview(self) -> None:
        code = (
            "import sys\n"
            "sys.modules['webview'] = None\n"
            "import gui_v3\n"
            "assert gui_v3.WEBVIEW_OK is False\n"
            "rc = gui_v3.main()\n"
            "assert rc == 1, rc\n"
            "print('DEGRADED_OK')\n"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("pywebview", proc.stdout.lower())
        self.assertIn("DEGRADED_OK", proc.stdout)

    def test_no_engine_analysis_logic_redefined(self) -> None:
        forbidden = (
            "verify_signature", "verify_metadata", "verify_md5",
            "analyze_bit_depth", "analyze_spectrum", "detect_clipping",
            "calculate_dr", "calculate_score", "analyze_file_data",
        )
        for name in forbidden:
            self.assertNotIn(name, vars(gui_v3))

    def test_settings_persistence_and_theme_toggle(self) -> None:
        saved = gui_v3.save_v3_settings(
            {
                "theme": "light",
                "workers": 4,
                "pdf_dest_mode": "custom",
                "custom_pdf_dir": "D:\\Reports",
                "offline_mode": True,
            },
            self.data_dir,
        )
        self.assertEqual(saved["theme"], "light")
        self.assertEqual(saved["workers"], 4)
        self.assertEqual(saved["pdf_dest_mode"], "custom")
        self.assertTrue(saved["offline_mode"])

        reloaded = gui_v3.load_v3_settings(self.data_dir)
        self.assertEqual(reloaded, saved)

        with mock.patch.object(gui_v3, "apply_dwm_titlebar", return_value=True) as dwm:
            res = self.api.set_theme("dark")
        self.assertTrue(res["ok"])
        self.assertEqual(res["theme"], "dark")
        dwm.assert_called_once_with("dark")

    def test_api_bootstrap_queries_review_spectrogram_and_export(self) -> None:
        alb_dir = os.path.join(self.tmp, "Music", "TestAlbum")
        os.makedirs(alb_dir, exist_ok=True)
        flac1 = os.path.join(alb_dir, "01 - Clean.flac")
        flac2 = os.path.join(alb_dir, "02 - Upscaled.flac")
        sf.write(flac1, fx.full_band_noise(sr=44100, dur=1.2, seed=1), 44100, subtype="PCM_16")
        sf.write(
            flac2,
            fx.brickwall(fx.full_band_noise(sr=44100, dur=1.2, seed=2), 44100, 16000),
            44100,
            subtype="PCM_16",
        )

        root = self.db.add_root(os.path.join(self.tmp, "Music"), label="Music")
        alb = self.db.upsert_album_results(
            alb_dir,
            [
                {
                    "archivo": "01 - Clean.flac",
                    "ruta": flac1,
                    "meta": {"sample_rate": 44100, "bits_per_sample": 16, "canales": 2},
                    "md5": {"estado": "match"},
                    "dr": 12.0,
                    "bdi": {"aplica": False},
                    "esp": {"techo_hz": 21400.0, "nyquist": 22050},
                    "score": 0.98,
                    "veredicto": "GENUINE LOSSLESS",
                    "problemas": [],
                    "notes": ["Full Red Book bandwidth verified"],
                },
                {
                    "archivo": "02 - Upscaled.flac",
                    "ruta": flac2,
                    "meta": {"sample_rate": 44100, "bits_per_sample": 16, "canales": 2},
                    "md5": {"estado": "match"},
                    "dr": 8.0,
                    "bdi": {"aplica": False},
                    "esp": {
                        "techo_hz": 16000.0,
                        "nyquist": 22050,
                        "corte_artificial": True,
                        "frecuencia_corte": 16000.0,
                        "mdct_lattice_detected": True,
                        "mdct_lattice_family": "PQMF-576 (MP3)",
                        "mdct_lattice_snr": 22.1,
                    },
                    "score": 0.16,
                    "veredicto": "PROBABLE UPSCALE",
                    "problemas": ["PQMF-576 (MP3) quantization lattice detected"],
                    "notes": [],
                },
            ],
            "17.1",  # simulate older engine to test outdated banner payload
            root_id=root["id"],
            read_tags=False,
        )

        boot = self.api.bootstrap()
        self.assertTrue(boot["ok"])
        self.assertEqual(boot["counts"]["albums"], 1)
        self.assertEqual(boot["counts"]["needs_review"], 1)
        self.assertEqual(boot["outdated"]["total_outdated"], 1)

        albs = self.api.get_albums()
        self.assertEqual(len(albs["albums"]), 1)
        self.assertEqual(albs["pill_counts"]["bad"], 1)

        detail = self.api.get_album_detail(alb["id"])
        self.assertTrue(detail["ok"])
        self.assertEqual(len(detail["tracks"]), 2)
        flagged_id = detail["tracks"][1]["id"]

        # Real interactive Canvas spectrogram payload on demand
        spec = self.api.get_spectrogram(flac2, track_id=flagged_id)
        self.assertTrue(spec["ok"])
        self.assertEqual(len(base64.b64decode(spec["grid_b64"])), 256 * 160)
        self.assertTrue(any(m["kind"] == "lattice" for m in spec["markers"]))

        # Mark reviewed updates counts
        rev = self.api.set_track_reviewed(flagged_id, True, "Confirmed MP3 stem")
        self.assertTrue(rev["ok"])
        self.assertTrue(rev["track"]["reviewed"])
        self.assertEqual(rev["counts"]["needs_review"], 0)
        self.assertEqual(rev["counts"]["reviewed"], 1)

        # Export CSV
        csv_path = os.path.join(self.tmp, "out.csv")
        exp = self.api.export_tracks(fmt="csv", dest_path=csv_path)
        self.assertTrue(exp["ok"])
        self.assertEqual(exp["count"], 2)
        self.assertTrue(os.path.isfile(csv_path))

    def test_extract_dropped_paths_deduplicates_and_validates(self) -> None:
        existing = os.path.join(self.tmp, "dropped.flac")
        open(existing, "wb").close()
        event = {
            "dataTransfer": {
                "files": [
                    {"pywebviewFullPath": existing},
                    {"pywebviewFullPath": existing.lower() if os.name == "nt" else existing},
                    {"pywebviewFullPath": os.path.join(self.tmp, "missing.flac")},
                ]
            }
        }
        paths = gui_v3._extract_dropped_paths(event)
        self.assertEqual(paths, [ldb.normalize_path(existing)])

    @unittest.skipUnless(gui_v3.BOTTLE_OK, "bottle not installed")
    def test_local_wsgi_app_serves_ui_assets_and_webp_covers(self) -> None:
        buf = io.BytesIO()
        Image.new("RGB", (320, 320), (36, 118, 129)).save(buf, format="PNG")
        digest = covers.cache_cover_bytes(buf.getvalue(), cache_dir=self.covers_dir)
        self.assertIsNotNone(digest)

        app = gui_v3.create_ui_app(ui_dir=gui_v3.UI_DIR, covers_dir=self.covers_dir)

        for route in ("/", "/styles.css", "/app.js", "/covers.js", "/spectrogram.js"):
            status, _headers, body = _wsgi_get(app, route)
            self.assertTrue(status.startswith("200"), f"{route} -> {status}")
            self.assertGreater(len(body), 100)

        status, headers, body = _wsgi_get(app, f"/covers/{digest}.webp")
        self.assertTrue(status.startswith("200"), status)
        self.assertEqual(headers.get("content-type"), "image/webp")
        self.assertGreater(len(body), 50)

        # Unknown or traversal cover hash safely returns 404.
        status_404, _, _ = _wsgi_get(app, "/covers/deadbeef00000000.webp")
        self.assertTrue(status_404.startswith("404"))

    def test_add_library_folder_options_albums_view_and_spectrogram_request_id(self) -> None:
        root_dir = os.path.join(self.tmp, "ConfiguredRoot")
        alb_dir = os.path.join(root_dir, "AlbumOne")
        custom_pdf = os.path.join(self.tmp, "PdfOut")
        os.makedirs(alb_dir, exist_ok=True)
        flac = os.path.join(alb_dir, "01 - Track.flac")
        sf.write(flac, fx.full_band_noise(sr=44100, dur=1.0, seed=4), 44100, subtype="PCM_16")

        # Persisting albums_view ("list" / "grid") round-trips cleanly.
        pref = self.api.save_preferences({"albums_view": "list", "workers": 4})
        self.assertTrue(pref["ok"])
        self.assertEqual(pref["settings"]["albums_view"], "list")
        self.assertEqual(gui_v3.load_v3_settings(self.data_dir)["albums_view"], "list")

        # add_library_folder forwards modal options to scanner and persists them.
        with mock.patch.object(
            self.scanner,
            "enqueue_root_scan",
            return_value={"queued": 1, "skipped": 0, "discovered": 1},
        ) as enq:
            res = self.api.add_library_folder(
                root_dir,
                {
                    "workers": 2,
                    "pdf": True,
                    "pdf_dest_mode": "custom",
                    "custom_pdf_dir": custom_pdf,
                    "no_cache": True,
                },
            )
        self.assertTrue(res["ok"], res)
        enq.assert_called_once()
        _, kwargs = enq.call_args
        self.assertEqual(kwargs["workers"], 2)
        self.assertTrue(kwargs["pdf"])
        self.assertEqual(kwargs["pdf_dest_mode"], "custom")
        self.assertEqual(kwargs["custom_pdf_dir"], custom_pdf)
        self.assertTrue(kwargs["no_cache"])
        reloaded = gui_v3.load_v3_settings(self.data_dir)
        self.assertEqual(reloaded["workers"], 2)
        self.assertTrue(reloaded["library_pdf"])
        self.assertEqual(reloaded["pdf_dest_mode"], "custom")
        self.assertEqual(reloaded["custom_pdf_dir"], custom_pdf)

        # Spectrogram request returns matching request_id for stale-response discarding.
        spec = self.api.get_spectrogram(flac, request_id=42)
        self.assertTrue(spec["ok"])
        self.assertEqual(spec["request_id"], 42)

        # Albums sort preference round-trips and get_albums accepts sort_by/sort_desc.
        sp = self.api.save_preferences({"albums_sort": "format", "albums_sort_desc": True})
        self.assertTrue(sp["ok"])
        self.assertEqual(sp["settings"]["albums_sort"], "format")
        self.assertTrue(sp["settings"]["albums_sort_desc"])
        albs = self.api.get_albums(sort_by="format", sort_desc=True)
        self.assertTrue(albs["ok"])

        # Prefetch queues cleanly when idle.
        pf = self.api.prefetch_spectrogram(flac)
        self.assertTrue(pf["ok"])
        self.assertTrue(pf["queued"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
