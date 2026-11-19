import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.dirname(HERE)
ROOT = os.path.dirname(WEB)
for entry in (WEB, os.path.join(ROOT, "tests"), ROOT):
    if entry not in sys.path:
        sys.path.insert(0, entry)

import engine_host  # noqa: E402
import motor_flac  # noqa: E402
from fixtures import Fixtures  # noqa: E402

KEYS = ("veredicto", "score", "problemas", "md5")


class EngineHostTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fx = Fixtures()
        cls.cache = tempfile.mkdtemp(prefix="web_cache_")
        engine_host.setup(cls.cache)

    @classmethod
    def tearDownClass(cls):
        cls.fx.close()
        shutil.rmtree(cls.cache, True)

    def test_setup_returns_engine_version(self):
        self.assertEqual(engine_host.setup(self.cache), motor_flac.ENGINE_VERSION)

    def test_analyze_matches_engine(self):
        p = self.fx["ok16"]
        got = json.loads(engine_host.analyze(p, "full", None)[0])
        want = json.loads(motor_flac._json_line(
            motor_flac.analyze_file_data(p, "full", None, False, False)))
        for k in KEYS:
            self.assertEqual(got.get(k), want.get(k), k)

    def test_analyze_returns_png_and_cleans_up(self):
        line, png = engine_host.analyze(self.fx["ok16"], "full", None)
        self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")
        self.assertFalse(os.path.exists(json.loads(line)["esp"]["espectrograma"]))

    def test_seconds_only_passed_in_seconds_mode(self):
        p = self.fx["ok16"]
        a = json.loads(engine_host.analyze(p, "full", 15)[0])
        b = json.loads(engine_host.analyze(p, "full", None)[0])
        for k in KEYS:
            self.assertEqual(a.get(k), b.get(k), k)

    def test_corrupt_file_is_error_not_exception(self):
        p = os.path.join(self.fx.dir, "x.flac")
        with open(p, "wb") as f:
            f.write(os.urandom(4096))
        self.assertIn("error", json.loads(engine_host.analyze(p, "full", None)[0]))

    def test_s32_raw_reader_matches_original(self):
        import numpy as np
        src = os.path.join(WEB, ".fixtures", "flac32.flac")
        if not os.path.exists(src) or not shutil.which("ffmpeg"):
            self.skipTest("fixture or ffmpeg missing")
        self.assertTrue(engine_host.needs_s32(src))
        self.assertFalse(engine_host.needs_s32(self.fx["ok16"]))
        raw = os.path.join(self.fx.dir, "w32.raw")
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", src,
                        "-f", "s32le", raw], check=True)
        engine_host.set_s32_source(src, raw)
        try:
            for fb in (motor_flac.FRAMES_BY_BLOCK, 4096, 1000):
                want = list(motor_flac._iter_blocks_s32_flac(src, None, fb))
                got = list(engine_host._iter_blocks_s32_raw(src, None, fb))
                self.assertEqual(len(want), len(got))
                for w, g in zip(want, got):
                    self.assertEqual(w.shape, g.shape)
                    self.assertEqual(g.dtype, np.float64)
                    self.assertTrue(np.array_equal(w, g))
            engine_host.install_s32_reader()
            try:
                line, _ = engine_host.analyze(src, "full", None)
            finally:
                engine_host.restore_s32_reader()
            self.assertNotIn("error", json.loads(line))
        finally:
            engine_host.set_s32_source(src, None)

    def test_album_coherence_does_not_mutate_input(self):
        results = [json.loads(engine_host.analyze(self.fx[n], "full", None)[0])
                   for n in ("ok16", "ok24")]
        text = json.dumps(results)
        out = json.loads(engine_host.album_coherence(text))
        self.assertEqual(text, json.dumps(results))
        self.assertEqual(len(out), len(results))

    def test_disposition_matches_engine(self):
        for name in ("ok16", "upscale96", "corte_16k", "hires96_rolloff30k"):
            result = json.loads(engine_host.analyze(self.fx[name], "full", None)[0])
            deferred = (motor_flac.classify_batch_disposition(result)
                        == motor_flac.BatchDisposition.DEFERRED)
            self.assertEqual(engine_host.disposition(json.dumps(result)),
                             "deferred" if deferred else "immediate", name)

    def test_sort_names_natural(self):
        self.assertEqual(
            json.loads(engine_host.sort_names('["10.flac","2.flac","1.flac"]')),
            ["1.flac", "2.flac", "10.flac"])


if __name__ == "__main__":
    unittest.main()
