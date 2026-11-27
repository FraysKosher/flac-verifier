# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 FLAC VERIFIER Contributors
"""Unit tests for the persistent SQLite library store (`library_db.py`)."""
from __future__ import annotations

import csv
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

import numpy as np
import soundfile as sf
from mutagen.flac import FLAC

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import library_db as ldb


def _make_result(
    folder: str,
    filename: str,
    *,
    verdict: str = "GENUINE LOSSLESS",
    score: float = 0.96,
    bits: int = 16,
    sr: int = 44100,
    dr: float = 11.0,
    ceiling: float = 21200.0,
    md5_state: str = "match",
    spectrum_extra: dict | None = None,
    bdi_extra: dict | None = None,
    problems: list[str] | None = None,
    notes: list[str] | None = None,
    error: str | None = None,
) -> dict:
    path = os.path.join(folder, filename)
    spectrum = {"techo_hz": ceiling, "nyquist": sr // 2}
    if spectrum_extra:
        spectrum.update(spectrum_extra)
    bdi = {"aplica": bits > 16}
    if bdi_extra:
        bdi.update(bdi_extra)
    res: dict = {
        "archivo": filename,
        "ruta": path,
        "meta": {
            "sample_rate": sr,
            "bits_per_sample": bits,
            "canales": 2,
            "duracion": 120.0,
        },
        "md5": {"estado": md5_state},
        "dr": dr,
        "bdi": bdi,
        "esp": spectrum,
        "score": score,
        "veredicto": verdict,
        "problemas": list(problems or []),
        "notes": list(notes or []),
    }
    if error is not None:
        res["error"] = error
        res.pop("veredicto", None)
        res.pop("score", None)
    return res


class TestPathsAndPortableMode(unittest.TestCase):

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="flac_v3_paths_")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_normalize_and_case_insensitive_key(self) -> None:
        a = os.path.join(self.tmp, "Music", "Album_One")
        b = a.replace(os.sep, "/") + "/"
        self.assertEqual(ldb.normalize_path(a), ldb.normalize_path(b))
        self.assertEqual(
            ldb.path_key(a, case_insensitive=True),
            ldb.path_key(b.swapcase(), case_insensitive=True),
        )
        if os.name == "nt":
            drive_lower = "d:\\Music\\Album"
            drive_upper = "D:/music/album/"
            self.assertTrue(ldb.normalize_path(drive_lower).startswith("D:\\"))
            self.assertEqual(ldb.path_key(drive_lower), ldb.path_key(drive_upper))

    def test_portable_flag_switches_data_dir(self) -> None:
        fake_exe = os.path.join(self.tmp, "FLAC_Verifier.exe")
        with mock.patch.dict(os.environ, {ldb.DATA_DIR_ENV_VAR: ""}, clear=False):
            self.assertFalse(ldb.is_portable_mode(fake_exe))
            standard = ldb.resolve_data_dir(executable_path=fake_exe)
            self.assertNotEqual(standard, os.path.join(self.tmp, "data"))

            with open(os.path.join(self.tmp, ldb.PORTABLE_FLAG_NAME), "w", encoding="utf-8") as fh:
                fh.write("portable\n")
            self.assertTrue(ldb.is_portable_mode(fake_exe))
            self.assertEqual(
                ldb.resolve_data_dir(executable_path=fake_exe),
                os.path.join(self.tmp, "data"),
            )

    def test_env_override_takes_precedence_over_portable(self) -> None:
        custom = os.path.join(self.tmp, "custom_data")
        with mock.patch.dict(os.environ, {ldb.DATA_DIR_ENV_VAR: custom}, clear=False):
            self.assertEqual(ldb.resolve_data_dir(), ldb.normalize_path(custom))


class TestLibraryDB(unittest.TestCase):

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="flac_v3_db_")
        self.db_path = os.path.join(self.tmp, "library.db")
        self.db = ldb.LibraryDB(self.db_path)

    def tearDown(self) -> None:
        self.db.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_wal_mode_and_schema_version(self) -> None:
        self.assertEqual(self.db.schema_version(), ldb.SCHEMA_VERSION)
        conn = sqlite3.connect(self.db_path)
        try:
            mode = conn.execute("PRAGMA journal_mode;").fetchone()[0]
            self.assertEqual(str(mode).lower(), "wal")
        finally:
            conn.close()

    def test_roots_deduplicated_on_windows_casing(self) -> None:
        root_dir = os.path.join(self.tmp, "MusicRoot")
        os.makedirs(root_dir, exist_ok=True)
        r1 = self.db.add_root(root_dir, label="Primary")
        variant = root_dir.lower() if os.name == "nt" else root_dir
        r2 = self.db.add_root(variant + os.sep, label="Primary Updated")
        self.assertEqual(r1["id"], r2["id"])
        roots = self.db.list_roots()
        self.assertEqual(len(roots), 1)
        self.assertEqual(roots[0]["label"], "Primary Updated")
        self.assertTrue(roots[0]["online"])

        self.db.touch_root_scanned(r1["id"])
        self.assertIsNotNone(self.db.list_roots()[0]["last_scanned_at"])
        self.assertTrue(self.db.remove_root(r1["id"]))
        self.assertEqual(self.db.list_roots(), [])

    def test_discover_albums_recursive_and_natural_order(self) -> None:
        root = os.path.join(self.tmp, "Collection")
        alb1 = os.path.join(root, "Artist A", "Album 1")
        alb2 = os.path.join(root, "Artist A", "Album 2")
        ignored = os.path.join(alb1, "_spectrograms")
        hidden = os.path.join(root, ".hidden")
        for folder in (alb1, alb2, ignored, hidden):
            os.makedirs(folder, exist_ok=True)
        for name in ("10.flac", "2.flac", "1.flac"):
            open(os.path.join(alb1, name), "wb").close()
        open(os.path.join(alb2, "01.flac"), "wb").close()
        open(os.path.join(ignored, "fake.flac"), "wb").close()
        open(os.path.join(hidden, "secret.flac"), "wb").close()

        found = ldb.discover_albums(root)
        self.assertEqual(len(found), 2)
        names_alb1 = [os.path.basename(p) for p in found[0]["flac_files"]]
        self.assertEqual(names_alb1, ["1.flac", "2.flac", "10.flac"])

    def test_incremental_scan_and_pruning(self) -> None:
        alb_dir = os.path.join(self.tmp, "AlbumInc")
        os.makedirs(alb_dir, exist_ok=True)
        f1 = os.path.join(alb_dir, "01 - First.flac")
        f2 = os.path.join(alb_dir, "02 - Second.flac")
        for path, payload in ((f1, b"abcd"), (f2, b"efghij")):
            with open(path, "wb") as fh:
                fh.write(payload)

        plan0 = self.db.plan_incremental_scan(alb_dir, [f1, f2], "17.2")
        self.assertTrue(plan0["album_needs_scan"])
        self.assertEqual(len(plan0["needs_scan"]), 2)

        self.db.upsert_album_results(
            alb_dir,
            [
                _make_result(alb_dir, "01 - First.flac"),
                _make_result(alb_dir, "02 - Second.flac"),
            ],
            "17.2",
            read_tags=False,
        )

        plan1 = self.db.plan_incremental_scan(alb_dir, [f1, f2], "17.2")
        self.assertFalse(plan1["album_needs_scan"])
        self.assertEqual(plan1["needs_scan"], [])
        self.assertEqual(len(plan1["up_to_date"]), 2)

        # Modify file 2 size & mtime.
        with open(f2, "wb") as fh:
            fh.write(b"modified-payload-12345")
        plan2 = self.db.plan_incremental_scan(alb_dir, [f1, f2], "17.2")
        self.assertTrue(plan2["album_needs_scan"])
        self.assertEqual(plan2["needs_scan"], [ldb.normalize_path(f2)])

        # Remove file 2 from disk and re-upsert only file 1.
        os.unlink(f2)
        plan3 = self.db.plan_incremental_scan(alb_dir, [f1], "17.2")
        self.assertEqual(plan3["removed_keys"], [ldb.path_key(f2)])
        alb = self.db.upsert_album_results(
            alb_dir,
            [_make_result(alb_dir, "01 - First.flac")],
            "17.2",
            prune_missing=True,
            read_tags=False,
        )
        self.assertEqual(alb["track_count"], 1)

    def test_engine_upgrade_preserves_reviewed_unless_now_genuine(self) -> None:
        alb_dir = os.path.join(self.tmp, "AlbumUpgrade")
        os.makedirs(alb_dir, exist_ok=True)
        res_v172 = [
            _make_result(
                alb_dir, "01 - Stem.flac",
                verdict="PROBABLE UPSCALE", score=0.25,
                spectrum_extra={"mdct_lattice_detected": True, "mdct_lattice_family": "PQMF-576 (MP3)"},
            ),
            _make_result(
                alb_dir, "02 - Acoustic.flac",
                verdict="SUSPICIOUS", score=0.52,
                spectrum_extra={"iqr_corte_hz": 1200.0},
            ),
        ]
        alb = self.db.upsert_album_results(alb_dir, res_v172, "17.2", read_tags=False)
        tracks = self.db.query_tracks(album_id=alb["id"], sort_by="num")
        self.assertEqual(len(tracks), 2)

        # User marks both tracks reviewed with custom notes.
        self.db.set_track_reviewed(tracks[0]["id"], True, "Known CD MP3 stem")
        self.db.set_track_reviewed(tracks[1]["id"], True, "Soft solo piano")

        outdated = self.db.count_outdated_albums("17.3")
        self.assertEqual(outdated["total_outdated"], 1)
        self.assertEqual(outdated["versions"], ["17.2"])

        # Re-verify under Engine 17.3: track 1 still flagged, track 2 now GENUINE LOSSLESS.
        res_v173 = [
            _make_result(
                alb_dir, "01 - Stem.flac",
                verdict="PROBABLE UPSCALE", score=0.22,
                spectrum_extra={"mdct_lattice_detected": True, "mdct_lattice_family": "PQMF-576 (MP3)"},
            ),
            _make_result(
                alb_dir, "02 - Acoustic.flac",
                verdict="GENUINE LOSSLESS", score=0.92,
            ),
        ]
        self.db.upsert_album_results(alb_dir, res_v173, "17.3", read_tags=False)

        updated = self.db.query_tracks(album_id=alb["id"], sort_by="num")
        self.assertTrue(updated[0]["reviewed"])
        self.assertEqual(updated[0]["reviewed_note"], "Known CD MP3 stem")
        self.assertEqual(updated[0]["engine_version"], "17.3")

        # Track 2 became GENUINE LOSSLESS -> review flag automatically cleared.
        self.assertFalse(updated[1]["reviewed"])
        self.assertEqual(updated[1]["reviewed_note"], "")
        self.assertEqual(self.db.count_outdated_albums("17.3")["total_outdated"], 0)

    def test_forensic_filters_counts_search_and_exports(self) -> None:
        root = self.db.add_root(os.path.join(self.tmp, "Vault"), label="Vault")
        alb_dir = os.path.join(self.tmp, "Vault", "AuditMix")
        os.makedirs(alb_dir, exist_ok=True)
        results = [
            _make_result(alb_dir, "01 - Clean.flac", verdict="GENUINE LOSSLESS", score=0.99),
            _make_result(
                alb_dir, "02 - MP3.flac", verdict="PROBABLE UPSCALE", score=0.14,
                spectrum_extra={"mdct_lattice_detected": True, "mdct_lattice_family": "PQMF-576 (MP3)"},
            ),
            _make_result(
                alb_dir, "03 - Fake96.flac", verdict="PROBABLE UPSCALE", score=0.20,
                bits=24, sr=96000, ceiling=24000.0,
                spectrum_extra={"muro_brickwall_nyquist": True},
            ),
            _make_result(
                alb_dir, "04 - Dither24.flac", verdict="PROBABLE UPSCALE", score=0.18,
                bits=24, sr=44100,
                bdi_extra={"forense_bits": {"es_16bit_dither_en_24": True}},
            ),
            _make_result(
                alb_dir, "05 - EnCodec.flac", verdict="PROBABLE UPSCALE", score=0.10,
                sr=48000,
                spectrum_extra={"neural_vocoder_detected": True, "vocoder_comb_spacing_hz": 1200.0},
            ),
            _make_result(
                alb_dir, "06 - Corrupt.flac", md5_state="mismatch",
                error="STREAMINFO MD5 does not match",
            ),
            _make_result(
                alb_dir, "10 - Suspicious.flac", verdict="SUSPICIOUS", score=0.55,
                spectrum_extra={"sospecha_joint_stereo": True},
            ),
        ]
        alb = self.db.upsert_album_results(
            alb_dir, results, "17.2", root_id=root["id"], read_tags=False
        )

        counts = self.db.get_sidebar_counts()
        self.assertEqual(counts["albums"], 1)
        self.assertEqual(counts["tracks"], 7)
        self.assertEqual(counts["genuine"], 1)
        self.assertEqual(counts["flagged"], 5)
        self.assertEqual(counts["suspicious"], 1)
        self.assertEqual(counts["needs_review"], 6)
        self.assertEqual(counts["lossy"], 2)          # MP3 + Suspicious joint-stereo
        self.assertEqual(counts["hires_upscaled"], 1)
        self.assertEqual(counts["fake_24bit"], 1)
        self.assertEqual(counts["ai_neural"], 1)
        self.assertEqual(counts["corrupt_md5"], 1)

        self.assertEqual(len(self.db.query_tracks(view="lossy")), 2)
        self.assertEqual(len(self.db.query_tracks(view="hires_upscaled")), 1)
        self.assertEqual(len(self.db.query_tracks(view="fake_24bit")), 1)
        self.assertEqual(len(self.db.query_tracks(view="ai_neural")), 1)
        self.assertEqual(len(self.db.query_tracks(view="corrupt_md5")), 1)
        self.assertEqual(len(self.db.query_tracks(view="needs_review")), 6)

        # Natural track number sort places 02 before 10.
        ordered = self.db.query_tracks(album_id=alb["id"], sort_by="num")
        self.assertEqual(
            [t["track_number"] for t in ordered],
            ["01", "02", "03", "04", "05", "06", "10"],
        )

        # Search by finding/title.
        hits = self.db.query_tracks(search="PQMF-576")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["filename"], "02 - MP3.flac")

        # Export CSV & JSON.
        csv_out = os.path.join(self.tmp, "export.csv")
        json_out = os.path.join(self.tmp, "export.json")
        self.assertEqual(self.db.export_tracks_csv(csv_out, view="needs_review"), 6)
        self.assertEqual(self.db.export_tracks_json(json_out, view="ai_neural"), 1)
        with open(csv_out, encoding="utf-8", newline="") as fh:
            rows = list(csv.DictReader(fh))
        self.assertEqual(len(rows), 6)
        with open(json_out, encoding="utf-8") as fh:
            payload = json.load(fh)
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["tracks"][0]["filename"], "05 - EnCodec.flac")

    def test_quick_drop_history_does_not_pollute_library(self) -> None:
        item = self.db.record_quick_drop(
            os.path.join(self.tmp, "OneOffAlbum"),
            track_count=10,
            flagged_count=2,
            engine_version="17.2",
            summary={"pdf": "report.pdf"},
        )
        self.assertEqual(item["track_count"], 10)
        self.assertEqual(self.db.get_sidebar_counts()["albums"], 0)
        recent = self.db.list_recent_quick_drops()
        self.assertEqual(len(recent), 1)
        self.assertEqual(recent[0]["summary"]["pdf"], "report.pdf")

    def test_real_flac_vorbis_tags_and_corrupt_fallback(self) -> None:
        alb_dir = os.path.join(self.tmp, "RealTagAlbum")
        os.makedirs(alb_dir, exist_ok=True)
        flac_path = os.path.join(alb_dir, "03_raw_name.flac")
        audio = np.zeros((4410, 2), dtype=np.float32)
        sf.write(flac_path, audio, 44100, subtype="PCM_16")
        mf = FLAC(flac_path)
        mf["title"] = "Luminol"
        mf["artist"] = "Steven Wilson"
        mf["albumartist"] = "Steven Wilson"
        mf["album"] = "The Raven That Refused to Sing"
        mf["date"] = "2013-02-25"
        mf["tracknumber"] = "1/6"
        mf["discnumber"] = "1/1"
        mf.save()

        tags = ldb.read_flac_tags(flac_path)
        self.assertEqual(tags["title"], "Luminol")
        self.assertEqual(tags["album"], "The Raven That Refused to Sing")
        self.assertEqual(tags["year"], "2013")
        self.assertEqual(tags["track_number"], "01")
        self.assertEqual(tags["disc_number"], "01")

        # Corrupt FLAC falls back cleanly without raising.
        bad_flac = os.path.join(alb_dir, "07 - Broken Track.flac")
        with open(bad_flac, "wb") as fh:
            fh.write(b"not a real flac")
        bad_tags = ldb.read_flac_tags(bad_flac)
        self.assertEqual(bad_tags["title"], "Broken Track")
        self.assertEqual(bad_tags["track_number"], "07")

    def test_bulk_transaction_speed_and_concurrent_reads(self) -> None:
        alb_dir = os.path.join(self.tmp, "BulkAlbum")
        os.makedirs(alb_dir, exist_ok=True)
        results = [
            _make_result(alb_dir, f"{i:03d} - Track {i}.flac")
            for i in range(1, 501)
        ]
        read_errors: list[Exception] = []
        stop_event = threading.Event()

        def reader() -> None:
            reader_db = ldb.LibraryDB(self.db_path)
            try:
                while not stop_event.is_set():
                    reader_db.get_sidebar_counts()
                    time.sleep(0.005)
            except Exception as exc:
                read_errors.append(exc)
            finally:
                reader_db.close()

        t = threading.Thread(target=reader, daemon=True)
        t.start()
        t0 = time.perf_counter()
        alb = self.db.upsert_album_results(alb_dir, results, "17.2", read_tags=False)
        elapsed = time.perf_counter() - t0
        stop_event.set()
        t.join(timeout=5.0)

        self.assertEqual(read_errors, [])
        self.assertEqual(alb["track_count"], 500)
        self.assertLess(elapsed, 5.0)

    def test_album_sorting_modes_and_neutral_evidence_dots(self) -> None:
        a1 = os.path.join(self.tmp, "SortA")
        a2 = os.path.join(self.tmp, "SortB")
        a3 = os.path.join(self.tmp, "SortC")
        for d in (a1, a2, a3):
            os.makedirs(d, exist_ok=True)
        self.db.upsert_album_results(
            a1,
            [_make_result(a1, "01.flac", sr=44100, bits=16, verdict="GENUINE LOSSLESS", score=0.98)],
            "17.2",
            album_meta={"artist": "Miles Davis", "title": "Kind of Blue", "year": "1959"},
            read_tags=False,
        )
        r_dday1 = _make_result(a2, "01.flac", sr=96000, bits=24, verdict="PROBABLE UPSCALE", score=0.10)
        r_dday1["problemas"] = ["Nyquist brickwall"]
        r_dday1["notes"] = ["Partial high-res content: 21.4 kHz"]
        self.db.upsert_album_results(
            a2,
            [
                r_dday1,
                _make_result(a2, "02.flac", sr=96000, bits=24, verdict="PROBABLE UPSCALE", score=0.12),
            ],
            "17.2",
            album_meta={"artist": "Agust D", "title": "D-DAY", "year": "2023"},
            read_tags=False,
        )
        self.db.upsert_album_results(
            a3,
            [_make_result(a3, "01.flac", sr=192000, bits=24, verdict="GENUINE LOSSLESS", score=0.99)],
            "17.2",
            album_meta={"artist": "Pink Floyd", "title": "Animals", "year": "1977"},
            read_tags=False,
        )

        # Default Artist A-Z
        by_artist = [a["artist"] for a in self.db.query_albums(sort_by="artist", sort_desc=False)]
        self.assertEqual(by_artist, ["Agust D", "Miles Davis", "Pink Floyd"])

        # Album title A-Z
        by_title = [a["title"] for a in self.db.query_albums(sort_by="title", sort_desc=False)]
        self.assertEqual(by_title, ["Animals", "D-DAY", "Kind of Blue"])

        # Year descending (newest first)
        by_year = [a["year"] for a in self.db.query_albums(sort_by="year", sort_desc=True)]
        self.assertEqual(by_year, ["2023", "1977", "1959"])

        # Format descending (192k -> 96k -> 44.1k)
        by_fmt = [a["sample_rate"] for a in self.db.query_albums(sort_by="format", sort_desc=True)]
        self.assertEqual(by_fmt, [192000, 96000, 44100])

        # Flagged first
        by_flag = [a["title"] for a in self.db.query_albums(sort_by="flagged", sort_desc=True)]
        self.assertEqual(by_flag[0], "D-DAY")

        # Cautionary note on flagged track MUST be level="neutral", never green "ok".
        tracks = self.db.query_tracks(view="all")
        dday_track = next(t for t in tracks if t["sample_rate"] == 96000 and t["score"] == 10.0)
        note_ev = next(e for e in dday_track["evidence"] if "Partial high-res" in e["text"])
        self.assertEqual(note_ev["level"], "neutral")

    def test_engine173_finding_codes_hires_attribution_and_coherence_tags(self) -> None:
        """Engine 17.3: read esp.finding_codes & bdi.lsbs, separate Hi-Res source
        Nyquist upscales from lossy transcodes, and handle album_coherence_action."""
        alb_dir = os.path.join(self.tmp, "HiRes_48_in_96")
        os.makedirs(alb_dir, exist_ok=True)
        f1 = os.path.join(alb_dir, "01.flac")
        f2 = os.path.join(alb_dir, "02.flac")
        for f in (f1, f2):
            open(f, "wb").close()

        r1 = _make_result(alb_dir, "01.flac", sr=96000, bits=24, verdict="PROBABLE UPSCALE", score=0.10)
        r1["esp"].update({
            "techo_hz": 23930,
            "corte_artificial": True,
            "frecuencia_corte": 23930,
            "muro_brickwall_nyquist": True,
            "finding_codes": ["nyquist_brickwall", "hires_upscale_cutoff"],
        })
        r1["bdi"] = {"aplica": True, "lsbs": "empty", "forense_bits": {}}
        r1["problemas"] = [
            "Upscaled from a 44.1/48 kHz lossless master (no lossy codec signature detected)",
            "Source sample-rate Nyquist limit (artificial cut-off at 23.9 kHz)",
        ]
        r1["notas_tecnicas"] = [
            "Ambiguous near-Nyquist transition — inconclusive",
            "Dual-mono track (identical L and R channels)",
        ]

        r2 = _make_result(alb_dir, "02.flac", sr=96000, bits=24, verdict="SUSPICIOUS", score=0.45)
        r2["album_coherence_action"] = "convicted_hires_upscale"
        r2["problemas"] = ["Album coherence: flagged as Hi-Res upscale"]

        self.db.upsert_album_results(alb_dir, [r1, r2], "17.3", read_tags=False)
        tracks = self.db.query_tracks(view="all")
        t1 = next(t for t in tracks if t["filename"] == "01.flac")
        t2 = next(t for t in tracks if t["filename"] == "02.flac")

        self.assertIn("hires_upscale_cutoff", t1["finding_codes"])
        self.assertIn("empty_lsbs", t1["finding_codes"])
        self.assertNotIn("artificial_cutoff", t1["finding_codes"])
        self.assertIn("hires_upscaled", t1["smart_tags"])
        self.assertIn("fake_24bit", t1["smart_tags"])
        self.assertNotIn("lossy", t1["smart_tags"])
        self.assertEqual(t1["primary_finding"], "Brickwall 23.9 kHz")
        for ev in t1["evidence"]:
            if "inconclusive" in ev["text"] or "Dual-mono" in ev["text"]:
                self.assertEqual(ev["level"], "neutral", ev)

        self.assertIn("hires_upscaled", t2["smart_tags"])
        self.assertNotIn("lossy", t2["smart_tags"])
        self.assertEqual(t2["primary_finding"], "Album Hi-Res upscale")

        alb = self.db.query_albums()[0]
        self.assertIn("Upscaled Hi-Res album quorum", alb["coherence_summary"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
