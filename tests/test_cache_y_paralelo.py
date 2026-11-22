"""Block 8: cache, parallelisation and the fast renderer.

What is pinned down here:
  - the second pass reuses the cache and gives EXACTLY the same result;
  - the cache is invalidated if the file changes (date, size or MD5 from
    STREAMINFO), if the mode changes or if the engine version changes;
  - with spectrograms, the cache only counts if the PNG is still there and is
    the right one;
  - the parallel analysis gives the same as the serial one and keeps the order;
  - an environment with no processes falls back to serial instead of failing;
  - the spectrogram is drawn as a NumPy+Pillow raster (the LUT), without matplotlib.
"""
import json
import os
import re
import shutil
import sys
import tempfile

import numpy as np
import soundfile as sf
from scipy import signal
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fixtures as fx

import motor_flac as engine
import informe_pdf

PROCESSES = engine._probe_processes()
VOLATILE_FIELDS = ("desde_cache", "tipo", "indice", "total")


def comparable(result):
    """The result without the fields that depend on the run."""
    return {k: v for k, v in result.items() if k not in VOLATILE_FIELDS}


def events_end(output):
    """Last event of the protocol."""
    return json.loads([l for l in output.splitlines() if l.strip()][-1])


class test_cache(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.fx = fx.Fixtures()
        cls.folder = cls.fx.subfolder("cache", ("ok16", "ok24", "corte_16k"))

    @classmethod
    def tearDownClass(cls):
        cls.fx.close()

    def copy(self, name, source="ok16"):
        """A copy of its own per test: the cache lives next to the file."""
        destination = os.path.join(self.fx.dir, f"cache_{name}.flac")
        shutil.copy(self.fx[source], destination)
        return destination

    def test_second_pass_reuses_gives_same(self):
        path = self.copy("reuso")
        primera = engine.analyze_file_data(path, "center", None, False)
        self.assertFalse(primera.get("desde_cache"))
        second = engine.analyze_file_data(path, "center", None, False)
        self.assertTrue(second.get("desde_cache"))
        self.assertEqual(comparable(primera), comparable(second))

    def test_input_cache_is_json_with_fingerprint(self):
        path = self.copy("formato")
        engine.analyze_file_data(path, "center", None, False)
        path_cache = engine._cache_path(path, "center", None)
        self.assertTrue(os.path.exists(path_cache))
        with open(path_cache, encoding="utf-8") as f:
            cache_entry = json.load(f)
        self.assertEqual(sorted(cache_entry), ["clave", "identidad", "render", "resultado"])
        self.assertEqual(cache_entry["clave"]["motor"], engine.ENGINE_VERSION)
        self.assertEqual(cache_entry["clave"]["modo"], "center")
        self.assertEqual(cache_entry["identidad"]["tamano"], os.path.getsize(path))
        self.assertIn("resultado", cache_entry)

    def test_invalidates_if_changes_date_file(self):
        path = self.copy("fecha")
        engine.analyze_file_data(path, "center", None, False)
        info = os.stat(path)
        os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns + 1_000_000_000))
        otra = engine.analyze_file_data(path, "center", None, False)
        self.assertFalse(otra.get("desde_cache"), "the cache was not invalidated by a different date")

    def test_invalidates_if_changes_md5_although_match_size_date(self):
        """The hard case: same size and same date, different content."""
        path = self.copy("md5")
        primera = engine.analyze_file_data(path, "center", None, False)
        self.assertEqual(primera["md5"]["estado"], "match")
        info = os.stat(path)

        with open(path, "r+b") as f:            # fakes the STREAMINFO MD5
            f.seek(26)
            f.write(b"\x00" * 16)
        os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))   # same date

        second = engine.analyze_file_data(path, "center", None, False)
        self.assertFalse(second.get("desde_cache"), "the cache did not look at the MD5")
        self.assertEqual(second["md5"]["estado"], "absent")

    def test_invalidates_if_changes_version_engine(self):
        path = self.copy("version")
        engine.analyze_file_data(path, "center", None, False)
        original = engine.ENGINE_VERSION
        engine.ENGINE_VERSION = original + "-test"
        try:
            otra = engine.analyze_file_data(path, "center", None, False)
        finally:
            engine.ENGINE_VERSION = original
        self.assertFalse(otra.get("desde_cache"))

    def test_invalidates_if_changes_mode(self):
        path = self.copy("modo")
        engine.analyze_file_data(path, "center", None, False)
        otra = engine.analyze_file_data(path, "full", None, False)
        self.assertFalse(otra.get("desde_cache"))
        # and repeating the original mode does reuse it
        self.assertTrue(engine.analyze_file_data(path, "center", None, False)
                        .get("desde_cache"))

    def test_without_cache_no_writes_nor_reuses(self):
        path = self.copy("sin_cache")
        primera = engine.analyze_file_data(path, "center", None, False, use_cache=False)
        self.assertNotIn("desde_cache", primera)
        self.assertFalse(os.path.exists(engine._cache_path(path, "center", None)))
        second = engine.analyze_file_data(path, "center", None, False, use_cache=False)
        self.assertNotIn("desde_cache", second)
        self.assertFalse(os.path.exists(engine._cache_path(path, "center", None)))

    def test_spectrogram_cached_reuses(self):
        path = self.copy("png")
        primera = engine.analyze_file_data(path, "center", None, True)
        png_path = primera["esp"]["espectrograma"]
        self.assertIsNotNone(png_path)
        sello = os.stat(png_path).st_mtime_ns

        second = engine.analyze_file_data(path, "center", None, True)
        self.assertTrue(second.get("desde_cache"))
        self.assertEqual(second["esp"]["espectrograma"], png_path)
        self.assertEqual(os.stat(png_path).st_mtime_ns, sello,
                         "the spectrogram was drawn again instead of being reused")

    def test_if_missing_png_cache_no_works_for_report(self):
        path = self.copy("png_borrado")
        primera = engine.analyze_file_data(path, "center", None, True)
        os.remove(primera["esp"]["espectrograma"])
        second = engine.analyze_file_data(path, "center", None, True)
        self.assertFalse(second.get("desde_cache"), "it reused a cache entry with no PNG")
        self.assertTrue(os.path.exists(second["esp"]["espectrograma"]))

    def test_without_png_no_reports_path_generated_before(self):
        path = self.copy("sin_png")
        with_png = engine.analyze_file_data(path, "center", None, True)
        self.assertIsNotNone(with_png["esp"]["espectrograma"])
        without_png = engine.analyze_file_data(path, "center", None, False)
        self.assertIsNone(without_png["esp"]["espectrograma"])

    @unittest.skipUnless(engine.PIL_OK, "Pillow not installed")
    def test_metrics_hit_needs_png_hydrates_without_bit_audit(self):
        path = self.copy("hydrate_png", source="ok24")
        first = engine.analyze_file_data(path, "center", None, True)
        png_path = first["esp"]["espectrograma"]
        self.assertTrue(png_path and os.path.exists(png_path))
        os.remove(png_path)

        meta = engine.verify_metadata(path)
        status, cached = engine.read_cache_entry(
            path, meta, "center", None, requires_png=True
        )
        self.assertIs(status, engine.CacheLookupStatus.METRICS_HIT_NEEDS_PNG)
        self.assertIsNotNone(cached)

        with mock.patch.object(engine, "ForensicBitsAccumulator") as bit_acc:
            second = engine.analyze_file_data(path, "center", None, True)
        bit_acc.assert_not_called()
        self.assertFalse(second.get("desde_cache"))
        self.assertTrue(os.path.exists(second["esp"]["espectrograma"]))
        self.assertEqual(comparable(first), comparable(second))


class TestParallelization(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.fx = fx.Fixtures()
        cls.folder = cls.fx.subfolder(
            "paralelo", ("ok16", "ok24", "corte_16k", "master_cd_20k"))

    @classmethod
    def tearDownClass(cls):
        cls.fx.close()

    def _engine(self, *extra):
        # No cache: the point is to compare real analyses, not JSON reads.
        return fx.run_engine(self.folder, extra=("--no-cache", *extra))

    def _results(self, output):
        return [json.loads(l) for l in output.splitlines()
                if l.strip() and json.loads(l)["tipo"] == "resultado"]

    def test_parallel_gives_exactly_same_sequential(self):
        series = self._results(self._engine("--workers", "1").stdout)
        parallel = self._results(self._engine("--workers", "4").stdout)
        self.assertEqual(len(series), 4)
        self.assertEqual(len(parallel), 4)
        for one, otro in zip(series, parallel):
            with self.subTest(file=one["archivo"]):
                self.assertEqual(comparable(one), comparable(otro))

    def test_preserves_order_files(self):
        expected = sorted(f for f in os.listdir(self.folder) if f.endswith(".flac"))
        results = self._results(self._engine("--workers", "4").stdout)
        self.assertEqual([r["archivo"] for r in results], expected)
        self.assertEqual([r["indice"] for r in results], [1, 2, 3, 4])

    def test_event_start_informs_processes(self):
        events = [json.loads(l) for l in self._engine("--workers", "4").stdout.splitlines()
                   if l.strip()]
        start = events[0]
        self.assertEqual(start["tipo"], "inicio")
        self.assertEqual(start["total"], 4)
        self.assertIn("workers", start)
        self.assertIn("paralelo", start)
        self.assertFalse(start["cache"])          # --no-cache was requested
        self.assertEqual(events[-1]["tipo"], "fin")

    def test_pdf_exits_equal_with_parallel(self):
        probe = self.fx.subfolder("paralelo_pdf", ("ok16", "ok24", "corte_16k"))
        p = fx.run_engine(probe, extra=("--pdf", "--workers", "3"))
        events = [json.loads(l) for l in p.stdout.splitlines() if l.strip()]
        report = [e for e in events if e["tipo"] == "informe"]
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertTrue(report, "the report event was not emitted")
        self.assertEqual(report[0]["paginas"], 5)          # cover + 3 + summary
        self.assertTrue(os.path.exists(report[0]["ruta"]))
        self.assertGreater(os.path.getsize(report[0]["ruta"]), 20 * 1024)
        self.assertEqual(report[0]["desde_cache"], 0)      # first pass

    def test_file_broken_batch_no_knocks_down_parallel(self):
        probe = self.fx.subfolder("paralelo_roto", ("ok16", "ok24", "basura_magic"))
        p = fx.run_engine(probe, extra=("--workers", "3", "--no-cache"))
        results = self._results(p.stdout)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(len(results), 3)
        with_error = [r for r in results if r.get("error")]
        self.assertEqual(len(with_error), 1)
        self.assertEqual(with_error[0]["archivo"], "basura_magic.flac")
        with_verdict = [r for r in results if r.get("veredicto")]
        self.assertEqual(len(with_verdict), 2)
        self.assertEqual(events_end(p.stdout)["tipo"], "fin")

    def test_environment_without_processes_degrades_sequential(self):
        original = engine._probe_processes
        engine._probe_processes = lambda: False
        try:
            files = [os.path.join(self.folder, f)
                        for f in sorted(os.listdir(self.folder))]
            analysis, workers = engine.analyze_files(
                files, "center", None, False, workers=8, use_cache=False)
            self.assertEqual(workers, 1)
            results = list(analysis)
            self.assertEqual(len(results), 4)
            self.assertTrue(all(r.get("archivo") for r in results))
        finally:
            engine._probe_processes = original

    @unittest.skipUnless(PROCESSES, "this environment does not allow creating processes")
    def test_with_processes_available_uses_pool(self):
        files = [os.path.join(self.folder, f)
                    for f in sorted(os.listdir(self.folder))]
        _, workers = engine.analyze_files(files, "center", None, False,
                                             workers=4, use_cache=False)
        self.assertEqual(workers, 4)

    def test_cache_avoids_start_processes(self):
        """With everything cached there is no work to spread: the pool is not needed."""
        probe = self.fx.subfolder("cache_paralelo", ("ok16", "ok24", "corte_16k"))
        files = [os.path.join(probe, f) for f in sorted(os.listdir(probe))]
        for path in files:                      # first pass: fills the cache
            engine.analyze_file_data(path, "center", None, False)
        analysis, workers = engine.analyze_files(files, "center", None, False,
                                                   workers=8, use_cache=True)
        results = list(analysis)
        self.assertEqual(workers, 1, "it should not start processes when everything is cached")
        self.assertTrue(all(r.get("desde_cache") for r in results))


class TestProgressiveStreaming(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.fx = fx.Fixtures()
        cls.folder = cls.fx.subfolder("streaming", ("ok16", "ok24", "ok16_mono"))

    @classmethod
    def tearDownClass(cls):
        cls.fx.close()

    def test_results_arrive_before_finish_batch(self):
        """The generator must stream non-candidate tracks, not buffer them all."""
        files = [os.path.join(self.folder, name)
                    for name in sorted(os.listdir(self.folder))
                    if name.endswith(".flac")]
        original = engine.analyze_file_data

        def slow(*args, **kwargs):
            time.sleep(0.25)
            return original(*args, **kwargs)

        with mock.patch.object(engine, "analyze_file_data", side_effect=slow):
            analysis, workers = engine.analyze_files(
                files, "center", None, False, workers=1, use_cache=False)
            start = time.perf_counter()
            llegadas = []
            results = []
            for result in analysis:
                llegadas.append(time.perf_counter() - start)
                results.append(result)
            end = time.perf_counter() - start

        self.assertEqual(workers, 1)
        self.assertEqual(len(results), 3)
        self.assertEqual(len(llegadas), 3)
        # Old buffered implementation yielded all three at `fin`: the first
        # arrival would be (almost) the same instant as the end.
        self.assertGreater(llegadas[0], 0.15)
        self.assertGreater(end - llegadas[0], 0.15)
        self.assertGreater(llegadas[-1] - llegadas[0], 0.15)


class test_cache_and_order_with_coherence(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.fx = fx.Fixtures()

    @classmethod
    def tearDownClass(cls):
        cls.fx.close()

    @staticmethod
    def _acoustic(path):
        sr = 44100
        x = fx.full_band_noise(sr=sr, dur=4.0, seed=7)
        y = signal.sosfilt(signal.butter(4, 15000.0, btype="low", fs=sr,
                                          output="sos"), x, axis=0)
        y /= max(1.0, np.max(np.abs(y)))
        sf.write(path, y, sr, subtype="PCM_16")

    def _album(self, name, quantity, position_candidate):
        folder = os.path.join(self.fx.dir, name)
        os.makedirs(folder, exist_ok=True)
        for index in range(quantity):
            destination = os.path.join(folder, f"{index + 1:02d}_track.flac")
            if index == position_candidate:
                self._acoustic(destination)
            else:
                shutil.copy(self.fx["ok16"], destination)
        return folder

    def test_cache_individual_no_inherits_coherence_batch(self):
        folder = self._album("cache_coherencia", 9, 0)
        files = [os.path.join(folder, name)
                    for name in sorted(os.listdir(folder))]
        batch = list(engine.analyze_files(
            files, "center", None, False, workers=1, use_cache=True)[0])
        rescued = next(r for r in batch if r["archivo"].startswith("01_"))
        self.assertTrue(rescued.get("coherencia_album"))
        self.assertEqual(rescued["veredicto"], "PROBABLY LOSSLESS")

        alone = engine.analyze_file_data(
            rescued["ruta"], "center", None, False, use_cache=True)
        self.assertFalse(alone.get("coherencia_album"))
        self.assertIn(alone["veredicto"], ("SUSPICIOUS", "PROBABLE UPSCALE"))

    def test_pdf_restores_order_original_after_retention(self):
        folder = self._album("orden_coherencia", 5, 1)
        files = [os.path.join(folder, name)
                    for name in sorted(os.listdir(folder))]
        results = list(engine.analyze_files(
            files, "center", None, False, workers=1, use_cache=False)[0])
        self.assertEqual(len(results), 5)
        self.assertEqual([r["indice_original"] for r in results],
                         [0, 2, 3, 4, 1])   # the retained one arrives last
        self.assertTrue(results[-1].get("coherencia_album"))

        with mock.patch("informe_pdf._elements",
                        side_effect=lambda *a, **k: iter(())) as elements:
            report = engine.generate_pdf_report(folder, results)
        self.assertTrue(report["ok"], report.get("error"))
        enviados = elements.call_args[0][1]
        self.assertEqual([r["indice_original"] for r in enviados],
                         [0, 1, 2, 3, 4])

        # CLI must also pair each header with its own result when a track is
        # held back for album coherence and yielded at the end of the batch.
        import io
        import contextlib
        import verificar_flac as cli
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            cli.execute_analysis(folder, mode="center", seconds=None,
                                 save_png=False, save_pdf=False)
        sections = [sec for sec in output.getvalue().split("📄 ") if sec.strip()][1:]
        self.assertEqual(len(sections), 5)
        self.assertEqual([sec.splitlines()[0].strip() for sec in sections],
                         [f"{i:02d}_track.flac" for i in range(1, 6)])
        retained_sec = next(sec for sec in sections if "02_" in sec.splitlines()[0])
        self.assertIn("PROBABLY LOSSLESS", retained_sec)
        for sec in sections:
            if "02_" not in sec.splitlines()[0]:
                self.assertIn("GENUINE LOSSLESS", sec)

        sample_names = [
            "1 - First.flac", "10 - Tenth.flac", "100 - Hundredth.flac",
            "136 - Last.flac", "14 - Fourteenth.flac", "2 - Second.flac",
        ]
        self.assertEqual(
            sorted(sample_names, key=engine.natural_sort_key),
            [
                "1 - First.flac", "2 - Second.flac", "10 - Tenth.flac",
                "14 - Fourteenth.flac", "100 - Hundredth.flac",
                "136 - Last.flac",
            ],
        )

    def test_natural_sort_key_handles_non_decimal_unicode_digits(self):
        names = (
            "Album²",
            "Album2²",
            "Album10",
            "Album2",
            "²",
            "①01 - Intro.flac",
            "０１",
            "2",
        )
        self.assertEqual(
            sorted(names, key=engine.natural_sort_key),
            ["０１", "2", "Album2", "Album2²", "Album10", "Album²", "²", "①01 - Intro.flac"],
        )


class test_renderer(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.fx = fx.Fixtures()

    @classmethod
    def tearDownClass(cls):
        cls.fx.close()

    def test_spectrogram_paints_with_pillow(self):
        """The PNG comes from the 256-colour NumPy LUT and Image.fromarray:
        no figure object, no Agg canvas, no matplotlib import."""
        with open(fx.ENGINE, encoding="utf-8") as f:
            source = f.read()
        self.assertIn("_LUT_SPECTROGRAM", source)
        self.assertIn("Image.fromarray(", source)
        self.assertIn("render_spectrogram_fast", source)
        self.assertNotIn("import matplotlib", source)
        self.assertNotIn("ax.imshow(", source)

    def test_fingerprint_render_changes_with_params(self):
        original_frames = engine.MAX_FRAMES_PNG
        original_width = engine.WIDTH_PNG
        base = engine.render_fingerprint()
        try:
            engine.MAX_FRAMES_PNG = original_frames + 1
            self.assertNotEqual(engine.render_fingerprint(), base)
            engine.MAX_FRAMES_PNG = original_frames
            self.assertEqual(engine.render_fingerprint(), base)
            engine.WIDTH_PNG = original_width + 1
            self.assertNotEqual(engine.render_fingerprint(), base)
        finally:
            engine.MAX_FRAMES_PNG = original_frames
            engine.WIDTH_PNG = original_width

    @unittest.skipUnless(engine.PIL_OK, "Pillow not installed")
    def test_png_still_exiting_with_size_reasonable(self):
        path = self.fx["ok16"]
        result = engine.analyze_file_data(path, "center", None, True,
                                                 use_cache=False)
        png_path = result["esp"]["espectrograma"]
        self.assertIsNotNone(png_path, result["esp"].get("espectrograma_error"))
        self.assertGreater(os.path.getsize(png_path), 100 * 1024)
        with open(png_path, "rb") as f:
            self.assertEqual(f.read(8), b"\x89PNG\r\n\x1a\n")

    def test_failure_paint_informs(self):
        """The reason for the failure reaches the result (before, it was lost silently)."""
        original = engine.Image
        try:
            engine.Image = None                  # Image.fromarray() will blow up
            result = engine.analyze_file_data(self.fx["ok16"], "center", None,
                                                     True, use_cache=False)
            self.assertIsNone(result["esp"]["espectrograma"])
            self.assertIn("espectrograma_error", result["esp"])
        finally:
            engine.Image = original


class TestStateMachines(unittest.TestCase):
    """Explicit cache-planning, batch-disposition, and accumulator states."""

    def test_plan_batch_cache_and_accumulator_state_guards(self):
        import tempfile
        import _motor_bits
        import _motor_espectral
        from _motor_cache import CacheLookupStatus

        with tempfile.TemporaryDirectory() as tmpdir:
            flac_path = os.path.join(tmpdir, "sample.flac")
            rng = np.random.default_rng(7)
            audio = rng.uniform(-0.25, 0.25, (8192, 2))
            sf.write(flac_path, audio, 44100, subtype="PCM_16", format="FLAC")

            plan_miss = engine.plan_batch_cache([flac_path], "center", 30, save_png=True, use_cache=True)
            self.assertEqual(plan_miss[0][2], CacheLookupStatus.MISS)

            gen1, _ = engine.analyze_files([flac_path], mode="center", seconds=30, save_png=False, workers=1, use_cache=True)
            list(gen1)
            plan_needs_png = engine.plan_batch_cache([flac_path], "center", 30, save_png=True, use_cache=True)
            self.assertEqual(plan_needs_png[0][2], CacheLookupStatus.METRICS_HIT_NEEDS_PNG)

            gen2, _ = engine.analyze_files([flac_path], mode="center", seconds=30, save_png=True, workers=1, use_cache=True)
            list(gen2)
            plan_full = engine.plan_batch_cache([flac_path], "center", 30, save_png=True, use_cache=True)
            self.assertEqual(plan_full[0][2], CacheLookupStatus.FULL_HIT)

            self.assertEqual(
                engine.classify_batch_disposition({"veredicto": "ORIGINAL"}),
                engine.BatchDisposition.IMMEDIATE,
            )
            with mock.patch.object(engine, "_deserves_album_coherence", return_value=True):
                self.assertEqual(
                    engine.classify_batch_disposition({"veredicto": "SUSPICIOUS"}),
                    engine.BatchDisposition.DEFERRED,
                )

            bits_acc = _motor_bits.ForensicBitsAccumulator(16, 44100)
            self.assertEqual(bits_acc.state, engine.AccumulatorState.COLLECTING)
            bits_acc.add(audio)
            bits_first = bits_acc.result()
            self.assertEqual(bits_acc.state, engine.AccumulatorState.COMPLETED)
            self.assertEqual(bits_acc.result(), bits_first)
            with self.assertRaises(RuntimeError):
                bits_acc.add(audio)

            spec_acc = _motor_espectral._SpectralAccumulator(44100, is_stereo=True)
            self.assertEqual(spec_acc.state, engine.AccumulatorState.COLLECTING)
            spec_acc.add(audio, 0)
            spec_first = spec_acc.result()
            self.assertEqual(spec_acc.state, engine.AccumulatorState.COMPLETED)
            self.assertEqual(spec_acc.result(), spec_first)
            with self.assertRaises(RuntimeError):
                spec_acc.add(audio, 0)

            meta = engine.verify_metadata(flac_path)
            block_acc = engine._BlockAnalysis(meta, 44100, len(audio), "center", 30, save_png=False, path=flac_path)
            self.assertEqual(block_acc.state, engine.AccumulatorState.COLLECTING)
            block_acc.add(audio, 0)
            block_first = block_acc.result()
            self.assertEqual(block_acc.state, engine.AccumulatorState.COMPLETED)
            self.assertEqual(block_acc.result(), block_first)
            with self.assertRaises(RuntimeError):
                block_acc.add(audio, 0)


class TestCentralizedCacheIsolation(unittest.TestCase):
    """OS user cache root, album-bucket isolation, and zero-write guarantees."""

    @classmethod
    def setUpClass(cls):
        cls.fx = fx.Fixtures()

    @classmethod
    def tearDownClass(cls):
        cls.fx.close()

    def test_cache_root_platform_defaults_and_env_override(self):
        with tempfile.TemporaryDirectory() as custom_dir:
            with mock.patch.dict(
                os.environ, {"FLAC_VERIFIER_CACHE_DIR": custom_dir}, clear=False
            ):
                self.assertEqual(engine.cache_root(), os.path.abspath(custom_dir))

        home = os.path.expanduser("~")
        with mock.patch.object(engine._motor_cache.sys, "platform", "win32"):
            with mock.patch.dict(
                os.environ,
                {
                    "FLAC_VERIFIER_CACHE_DIR": "",
                    "LOCALAPPDATA": "C:/Users/TestUser/AppData/Local",
                },
                clear=False,
            ):
                self.assertEqual(
                    engine.cache_root(),
                    os.path.join("C:/Users/TestUser/AppData/Local", "FLAC_Verifier", "cache"),
                )

        with mock.patch.object(engine._motor_cache.sys, "platform", "darwin"):
            with mock.patch.dict(
                os.environ, {"FLAC_VERIFIER_CACHE_DIR": ""}, clear=False
            ):
                self.assertEqual(
                    engine.cache_root(),
                    os.path.join(home, "Library", "Caches", "FLAC_Verifier", "cache"),
                )

        with mock.patch.object(engine._motor_cache.sys, "platform", "linux"):
            with mock.patch.dict(
                os.environ,
                {"FLAC_VERIFIER_CACHE_DIR": "", "XDG_CACHE_HOME": "/tmp/custom_xdg"},
                clear=False,
            ):
                self.assertEqual(
                    engine.cache_root(),
                    os.path.join("/tmp/custom_xdg", "flac-verifier", "cache"),
                )
            with mock.patch.dict(
                os.environ,
                {"FLAC_VERIFIER_CACHE_DIR": "", "XDG_CACHE_HOME": ""},
                clear=False,
            ):
                self.assertEqual(
                    engine.cache_root(),
                    os.path.join(home, ".cache", "flac-verifier", "cache"),
                )

    def test_bucket_hash_isolates_colliding_track_names_across_folders(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            isolated_cache = os.path.join(tmpdir, "cache_store")
            folder_a = os.path.join(tmpdir, "Album_A")
            folder_b = os.path.join(tmpdir, "Album_B")
            os.makedirs(folder_a)
            os.makedirs(folder_b)
            track_a = os.path.join(folder_a, "01 - Intro.flac")
            track_b = os.path.join(folder_b, "01 - Intro.flac")
            shutil.copy(self.fx["ok16"], track_a)
            shutil.copy(self.fx["corte_16k"], track_b)

            with mock.patch.dict(
                os.environ, {"FLAC_VERIFIER_CACHE_DIR": isolated_cache}, clear=False
            ):
                self.assertNotEqual(
                    engine._png_folder(track_a), engine._png_folder(track_b)
                )
                self.assertNotEqual(
                    engine._cache_path(track_a, "center", None),
                    engine._cache_path(track_b, "center", None),
                )
                first_a = engine.analyze_file_data(
                    track_a, "center", None, True, use_cache=True
                )
                first_b = engine.analyze_file_data(
                    track_b, "center", None, True, use_cache=True
                )
                self.assertEqual(first_a["veredicto"], "GENUINE LOSSLESS")
                self.assertEqual(first_b["veredicto"], "PROBABLE UPSCALE")
                png_a = first_a["esp"]["espectrograma"]
                png_b = first_b["esp"]["espectrograma"]
                self.assertTrue(png_a and os.path.isfile(png_a))
                self.assertTrue(png_b and os.path.isfile(png_b))
                self.assertNotEqual(png_a, png_b)

                second_a = engine.analyze_file_data(
                    track_a, "center", None, True, use_cache=True
                )
                second_b = engine.analyze_file_data(
                    track_b, "center", None, True, use_cache=True
                )
                self.assertTrue(second_a.get("desde_cache"))
                self.assertTrue(second_b.get("desde_cache"))
                self.assertEqual(second_a["veredicto"], "GENUINE LOSSLESS")
                self.assertEqual(second_b["veredicto"], "PROBABLE UPSCALE")
                self.assertEqual(second_a["esp"]["espectrograma"], png_a)
                self.assertEqual(second_b["esp"]["espectrograma"], png_b)

    def test_music_folder_stays_completely_untouched_with_cache_and_png(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            isolated_cache = os.path.join(tmpdir, "cache_store")
            album_dir = os.path.join(tmpdir, "ReadOnlyCandidate")
            os.makedirs(album_dir)
            shutil.copy(self.fx["ok16"], os.path.join(album_dir, "01_ok16.flac"))
            shutil.copy(self.fx["ok24"], os.path.join(album_dir, "02_ok24.flac"))
            before_list = sorted(os.listdir(album_dir))

            with mock.patch.dict(
                os.environ, {"FLAC_VERIFIER_CACHE_DIR": isolated_cache}, clear=False
            ):
                proc = fx.run_engine(
                    album_dir, mode="full", extra=("--png", "--workers", "1")
                )
                self.assertEqual(proc.returncode, 0, proc.stderr)
                events = [
                    json.loads(line)
                    for line in proc.stdout.splitlines()
                    if line.strip()
                ]
                result_events = [e for e in events if e.get("tipo") == "resultado"]
                self.assertEqual(len(result_events), 2)
                for ev in result_events:
                    png_path = (ev.get("esp") or {}).get("espectrograma")
                    self.assertTrue(png_path and os.path.isfile(png_path))
                    self.assertTrue(
                        os.path.abspath(png_path).startswith(os.path.abspath(isolated_cache))
                    )
                    cache_file = engine._cache_path(ev["ruta"], "full", None)
                    self.assertTrue(os.path.isfile(cache_file))

            self.assertEqual(sorted(os.listdir(album_dir)), before_list)
            self.assertFalse(os.path.exists(os.path.join(album_dir, "_spectrograms")))

    def test_schema_version_invalidation(self):
        self.assertEqual(engine.CACHE_SCHEMA_VERSION, "2.6.0")
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "schema_probe.flac")
            shutil.copy(self.fx["ok16"], path)
            first_result = engine.analyze_file_data(path, "center", None, False)
            self.assertFalse(first_result.get("desde_cache"))
            original_schema = engine.CACHE_SCHEMA_VERSION
            engine.CACHE_SCHEMA_VERSION = original_schema + "-test"
            try:
                second_result = engine.analyze_file_data(path, "center", None, False)
            finally:
                engine.CACHE_SCHEMA_VERSION = original_schema
            self.assertFalse(second_result.get("desde_cache"))


class TestCacheHardeningAndStreams(unittest.TestCase):
    """Regressions for v2.10.1 malformed cache JSON, PNG scoping & stream safety."""

    @classmethod
    def setUpClass(cls):
        cls.fx = fx.Fixtures()

    @classmethod
    def tearDownClass(cls):
        cls.fx.close()

    def test_malformed_json_roots_and_nested_types_degrade_to_miss(self):
        from _motor_cache import CacheLookupStatus

        with tempfile.TemporaryDirectory() as tmpdir:
            isolated_cache = os.path.join(tmpdir, "cache")
            flac_path = os.path.join(tmpdir, "ok.flac")
            shutil.copy(self.fx["ok16"], flac_path)
            with mock.patch.dict(
                os.environ, {"FLAC_VERIFIER_CACHE_DIR": isolated_cache}, clear=False
            ):
                base = engine.analyze_file_data(flac_path, "center", None, False, use_cache=True)
                self.assertEqual(base["veredicto"], "GENUINE LOSSLESS")
                cache_file = engine._cache_path(flac_path, "center", None)
                meta = engine.verify_metadata(flac_path)
                with open(cache_file, encoding="utf-8") as fh:
                    valid_payload = json.load(fh)

                bad_payloads = [
                    "[]",
                    "null",
                    "123",
                    '"broken"',
                    "{not json",
                    json.dumps({**valid_payload, "identidad": 42}),
                    json.dumps({**valid_payload, "clave": "not-a-dict"}),
                    json.dumps({**valid_payload, "resultado": ["not", "a", "dict"]}),
                    json.dumps({
                        **valid_payload,
                        "resultado": {**valid_payload["resultado"], "esp": []},
                    }),
                    json.dumps({
                        **valid_payload,
                        "resultado": {**valid_payload["resultado"], "problemas": "str"},
                    }),
                ]
                for raw in bad_payloads:
                    with self.subTest(raw=raw[:40]):
                        with open(cache_file, "w", encoding="utf-8") as fh:
                            fh.write(raw)
                        status, entry = engine.read_cache_entry(
                            flac_path, meta, "center", None, requires_png=False
                        )
                        self.assertIs(status, CacheLookupStatus.MISS)
                        self.assertIsNone(entry)
                        plan = engine.plan_batch_cache(
                            [flac_path], "center", None, save_png=False, use_cache=True
                        )
                        self.assertIs(plan[0][2], CacheLookupStatus.MISS)

    @unittest.skipUnless(engine.PIL_OK, "Pillow not installed")
    def test_mode_scoped_png_atomic_write_and_magic_validation(self):
        from _motor_cache import CacheLookupStatus

        with tempfile.TemporaryDirectory() as tmpdir:
            isolated_cache = os.path.join(tmpdir, "cache")
            flac_path = os.path.join(tmpdir, "track.flac")
            shutil.copy(self.fx["ok16"], flac_path)
            with mock.patch.dict(
                os.environ, {"FLAC_VERIFIER_CACHE_DIR": isolated_cache}, clear=False
            ):
                r_full = engine.analyze_file_data(flac_path, "full", None, True, use_cache=True)
                r_sec = engine.analyze_file_data(flac_path, "seconds", 1, True, use_cache=True)
                r_center = engine.analyze_file_data(flac_path, "center", None, True, use_cache=True)
                p_full = r_full["esp"]["espectrograma"]
                p_sec = r_sec["esp"]["espectrograma"]
                p_center = r_center["esp"]["espectrograma"]

                self.assertEqual(len({p_full, p_sec, p_center}), 3)
                for p_png in (p_full, p_sec, p_center):
                    self.assertTrue(os.path.isfile(p_png))
                    with open(p_png, "rb") as fh:
                        self.assertEqual(fh.read(8), engine.PNG_MAGIC)
                    self.assertFalse(
                        any(f.endswith(".tmp") for f in os.listdir(os.path.dirname(p_png)))
                    )

                # Corrupt PNG header -> METRICS_HIT_NEEDS_PNG triggers clean regeneration.
                with open(p_full, "wb") as fh:
                    fh.write(b"NOT_A_PNG_FILE_12345")
                meta = engine.verify_metadata(flac_path)
                status, _ = engine.read_cache_entry(
                    flac_path, meta, "full", None, requires_png=True
                )
                self.assertIs(status, CacheLookupStatus.METRICS_HIT_NEEDS_PNG)
                regen = engine.analyze_file_data(flac_path, "full", None, True, use_cache=True)
                self.assertFalse(regen.get("desde_cache"))
                with open(regen["esp"]["espectrograma"], "rb") as fh:
                    self.assertEqual(fh.read(8), engine.PNG_MAGIC)

    def test_cache_hits_refresh_caller_path_across_relative_invocations(self):
        from _motor_cache import CacheLookupStatus

        with tempfile.TemporaryDirectory() as tmpdir:
            isolated_cache = os.path.join(tmpdir, "cache")
            album = os.path.join(tmpdir, "Album")
            sub = os.path.join(album, "sub")
            os.makedirs(sub)
            flac_abs = os.path.join(album, "song.flac")
            shutil.copy(self.fx["ok16"], flac_abs)

            with mock.patch.dict(
                os.environ, {"FLAC_VERIFIER_CACHE_DIR": isolated_cache}, clear=False
            ):
                engine.analyze_file_data(flac_abs, "center", None, False, use_cache=True)
                rel_path = os.path.join(".", "song.flac")
                prev_cwd = os.getcwd()
                try:
                    os.chdir(album)
                    meta = engine.verify_metadata(rel_path)
                    st_hit, entry_hit = engine.read_cache_entry(
                        rel_path, meta, "center", None, requires_png=False
                    )
                    self.assertIs(st_hit, CacheLookupStatus.FULL_HIT)
                    self.assertEqual(entry_hit["ruta"], rel_path)
                    self.assertEqual(entry_hit["archivo"], "song.flac")

                    res_hit = engine.analyze_file_data(
                        rel_path, "center", None, False, use_cache=True
                    )
                    self.assertTrue(res_hit.get("desde_cache"))
                    self.assertEqual(res_hit["ruta"], rel_path)
                finally:
                    os.chdir(prev_cwd)

    def test_midstream_read_error_and_truncated_hydration_fail_cleanly(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            isolated_cache = os.path.join(tmpdir, "cache")
            flac_path = os.path.join(tmpdir, "truncated_hydrate.flac")
            shutil.copy(self.fx["ok24"], flac_path)

            class _MidFailSF:
                def __init__(self, _path):
                    self.calls = 0

                def read(self, frames, dtype="float64", always_2d=True):
                    self.calls += 1
                    if self.calls == 1:
                        return np.zeros((1024, 2), dtype=np.float64)
                    raise RuntimeError("midstream decode error")

                def close(self):
                    pass

            with mock.patch.object(engine.sf, "SoundFile", side_effect=_MidFailSF):
                with mock.patch.object(engine, "_iter_blocks_s32_flac") as s32_mock:
                    with self.assertRaises(RuntimeError):
                        list(engine._read_blocks(flac_path))
                    s32_mock.assert_not_called()

            with mock.patch.dict(
                os.environ, {"FLAC_VERIFIER_CACHE_DIR": isolated_cache}, clear=False
            ):
                first = engine.analyze_file_data(flac_path, "center", None, False, use_cache=True)
                self.assertEqual(first["veredicto"], "GENUINE LOSSLESS")
                with mock.patch.object(
                    engine, "_read_blocks",
                    return_value=iter([np.zeros((512, 2), dtype=np.float64)]),
                ):
                    bad = engine.analyze_file_data(flac_path, "center", None, True, use_cache=True)
                self.assertIn("Could not decode the audio", bad.get("error", ""))


if __name__ == "__main__":
    unittest.main(verbosity=2)
