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

    def copia(self, name, source="ok16"):
        """A copy of its own per test: the cache lives next to the file."""
        destination = os.path.join(self.fx.dir, f"cache_{name}.flac")
        shutil.copy(self.fx[source], destination)
        return destination

    def test_second_pass_reuses_gives_same(self):
        path = self.copia("reuso")
        primera = engine.analyze_file_data(path, "center", None, False)
        self.assertFalse(primera.get("desde_cache"))
        second = engine.analyze_file_data(path, "center", None, False)
        self.assertTrue(second.get("desde_cache"))
        self.assertEqual(comparable(primera), comparable(second))

    def test_input_cache_is_json_with_fingerprint(self):
        path = self.copia("formato")
        engine.analyze_file_data(path, "center", None, False)
        path_cache = engine._cache_path(path, "center", None)
        self.assertTrue(os.path.exists(path_cache))
        with open(path_cache, encoding="utf-8") as f:
            input = json.load(f)
        self.assertEqual(sorted(input), ["clave", "identidad", "render", "resultado"])
        self.assertEqual(input["clave"]["motor"], engine.VERSION_MOTOR)
        self.assertEqual(input["clave"]["modo"], "center")
        self.assertEqual(input["identidad"]["tamano"], os.path.getsize(path))
        self.assertIn("resultado", input)

    def test_invalidates_if_changes_date_file(self):
        path = self.copia("fecha")
        engine.analyze_file_data(path, "center", None, False)
        info = os.stat(path)
        os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns + 1_000_000_000))
        otra = engine.analyze_file_data(path, "center", None, False)
        self.assertFalse(otra.get("desde_cache"), "the cache was not invalidated by a different date")

    def test_invalidates_if_changes_md5_although_match_size_date(self):
        """The hard case: same size and same date, different content."""
        path = self.copia("md5")
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
        path = self.copia("version")
        engine.analyze_file_data(path, "center", None, False)
        original = engine.VERSION_MOTOR
        engine.VERSION_MOTOR = original + "-test"
        try:
            otra = engine.analyze_file_data(path, "center", None, False)
        finally:
            engine.VERSION_MOTOR = original
        self.assertFalse(otra.get("desde_cache"))

    def test_invalidates_if_changes_mode(self):
        path = self.copia("modo")
        engine.analyze_file_data(path, "center", None, False)
        otra = engine.analyze_file_data(path, "full", None, False)
        self.assertFalse(otra.get("desde_cache"))
        # and repeating the original mode does reuse it
        self.assertTrue(engine.analyze_file_data(path, "center", None, False)
                        .get("desde_cache"))

    def test_without_cache_no_writes_nor_reuses(self):
        path = self.copia("sin_cache")
        primera = engine.analyze_file_data(path, "center", None, False, use_cache=False)
        self.assertNotIn("desde_cache", primera)
        self.assertFalse(os.path.exists(engine._cache_path(path, "center", None)))
        second = engine.analyze_file_data(path, "center", None, False, use_cache=False)
        self.assertNotIn("desde_cache", second)
        self.assertFalse(os.path.exists(engine._cache_path(path, "center", None)))

    def test_spectrogram_cached_reuses(self):
        path = self.copia("png")
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
        path = self.copia("png_borrado")
        primera = engine.analyze_file_data(path, "center", None, True)
        os.remove(primera["esp"]["espectrograma"])
        second = engine.analyze_file_data(path, "center", None, True)
        self.assertFalse(second.get("desde_cache"), "it reused a cache entry with no PNG")
        self.assertTrue(os.path.exists(second["esp"]["espectrograma"]))

    def test_without_png_no_reports_path_generated_before(self):
        path = self.copia("sin_png")
        with_png = engine.analyze_file_data(path, "center", None, True)
        self.assertIsNotNone(with_png["esp"]["espectrograma"])
        without_png = engine.analyze_file_data(path, "center", None, False)
        self.assertIsNone(without_png["esp"]["espectrograma"])


class TestParalelizacion(unittest.TestCase):

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


class TestStreamingProgresivo(unittest.TestCase):

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

    def _album(self, name, cantidad, position_candidate):
        folder = os.path.join(self.fx.dir, name)
        os.makedirs(folder, exist_ok=True)
        for index in range(cantidad):
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
        source = open(fx.ENGINE, encoding="utf-8").read()
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
