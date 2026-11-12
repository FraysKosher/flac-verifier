"""Block 6 (performance): the optimisation must not change any result.

The array path (load everything and analyse) is used as the ORACLE of the
streaming path, which is the one the program uses. The invariants of the block
reader and the equivalence of the vectorisation with the naive implementation
are pinned down as well.
"""
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fixtures as fx

import motor_flac as engine

KEYS_SPECTRAL = ("techo_hz", "techo_rel_nyquist", "clase_corte", "corte_detectado",
                      "frecuencia_corte", "distancia_nyquist", "corte_artificial",
                      "ratio", "ratio_aplica", "var_alta_db", "separacion_stereo",
                      "nyquist")


class test_streaming_equal_array(unittest.TestCase):
    """Oracle: the same data through both paths -> the same results."""

    @classmethod
    def setUpClass(cls):
        cls.fx = fx.Fixtures()

    @classmethod
    def tearDownClass(cls):
        cls.fx.close()

    def _compare(self, name, mode="center"):
        path = self.fx[name]
        res = engine.analyze_file_data(path, mode, None, False)
        self.assertNotIn("error", res, res.get("error"))

        data, sr = engine.read_audio(path)
        meta = engine.verify_metadata(path)

        self.assertEqual(res["clip"], engine.detect_clipping(data), "clipping")
        self.assertEqual(res["dr"], engine.calculate_dr(data, sr), "dynamic range")
        self.assertEqual(res["bdi"], engine.analyze_bit_depth(data, meta), "low bits")
        self.assertEqual(res["md5"], engine.verify_md5(data, meta), "MD5")

        spectrum = engine.analyze_spectrum(data, sr, mode, None, False, None)
        self.assertIsNone(spectrum.get("error"), spectrum.get("error"))
        for key in KEYS_SPECTRAL:
            with self.subTest(key=key):
                self.assertEqual(res["esp"][key], spectrum[key])

    def test_stereo_16_bits(self):
        self._compare("ok16")

    def test_stereo_24_bits(self):
        self._compare("ok24")

    def test_mono_no_reports_separation_stereo(self):
        """In mono there is no L-R separation: None in both paths."""
        self._compare("ok16_mono")
        res = engine.analyze_file_data(self.fx["ok16_mono"], "center", None, False)
        self.assertIsNone(res["esp"]["separacion_stereo"])

    def test_mode_full(self):
        self._compare("ok16", mode="full")


class test_reader_by_blocks(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.fx = fx.Fixtures()

    @classmethod
    def tearDownClass(cls):
        cls.fx.close()

    def test_no_reads_more_block_rebuilds_file(self):
        path = self.fx["ok24"]
        blocks = list(engine._read_blocks(path))
        self.assertGreater(len(blocks), 0)
        for block in blocks:
            self.assertLessEqual(block.shape[0], engine.FRAMES_BY_BLOCK)
        data, _ = engine.read_audio(path)
        self.assertTrue(np.array_equal(np.concatenate(blocks), data))

    def test_md5_incremental_matches_with_only_array(self):
        """The block hash must give the same result as hashing the whole array."""
        path = self.fx["ok24"]
        data, _ = engine.read_audio(path)
        meta = engine.verify_metadata(path)
        streaming = engine._analyze_streaming(path, meta, "full", None, False)
        self.assertEqual(streaming["md5"], engine.verify_md5(data, meta))
        self.assertEqual(streaming["md5"]["estado"], "match")
        self.assertEqual(streaming["md5"]["calculado"],
                         engine._md5_pcm(data, meta["bits_per_sample"]))


class TestVectorization(unittest.TestCase):
    """The vectorisation must count exactly the same as the naive loop."""

    @staticmethod
    def _naive_clipping(data):
        """Original implementation, sample by sample (reference)."""
        amp = np.max(np.abs(data), axis=1) if data.ndim > 1 else np.abs(data)
        en_clip = (amp >= 0.9999).astype(np.int8)
        runs = 0
        count = 0
        for v in en_clip:
            if v:
                count += 1
            else:
                if count >= 3:
                    runs += 1
                count = 0
        if count >= 3:
            runs += 1
        return {"runs_clip": runs, "hay_clipping": runs > 0}

    def test_streaks_edges_length_2_3(self):
        x = np.zeros((50, 2))
        x[0:3, 0]   = 1.0        # run of 3 at the start -> counts
        x[10:12, 1] = -1.0       # run of 2 -> does not count
        x[20:24, 0] = 1.0        # run of 4 -> counts
        x[47:50, 1] = 1.0        # run of 3 right at the end -> counts
        # the whole file: both agree and the result is the expected one
        self.assertEqual(engine.detect_clipping(x), self._naive_clipping(x))
        self.assertEqual(engine.detect_clipping(x)["runs_clip"], 3)

    def test_equivalence_with_data_random_clipping_real(self):
        rng = np.random.default_rng(5)
        cases = [rng.random((100000, 2)) * 2 - 1]
        with_clip = rng.random((50000, 2)) * 0.5
        with_clip[100:105] = 1.0                  # run of 5
        with_clip[49995:]  = -1.0                 # run of 5 at the end
        with_clip[20000:20002] = 1.0              # run of 2
        cases.append(with_clip)
        for data in cases:
            with self.subTest(shape=data.shape):
                self.assertEqual(engine.detect_clipping(data), self._naive_clipping(data))

    def test_streak_split_between_blocks_counts_alone_time(self):
        data = np.zeros((10, 1))
        data[3:9] = 1.0                          # a single run of 6
        for cutoff in range(1, 10):
            with self.subTest(cutoff=cutoff):
                count, runs = 0, 0
                for chunk in (data[:cutoff], data[cutoff:]):
                    if not len(chunk):
                        continue
                    streaks, count = engine._streaks(chunk[:, 0] >= 0.9999, count)
                    runs += sum(1 for long in streaks if long >= 3)
                if count >= 3:
                    runs += 1
                self.assertEqual(runs, 1)

    def test_streak_split_three_blocks(self):
        data = np.ones((9, 1))                   # run of 9
        count, runs = 0, 0
        for chunk in (data[:3], data[3:6], data[6:]):
            streaks, count = engine._streaks(chunk[:, 0] >= 0.9999, count)
            runs += sum(1 for long in streaks if long >= 3)
        if count >= 3:
            runs += 1
        self.assertEqual(runs, 1)

    def test_accumulator_counts_equal_via_array(self):
        """It includes the run that ends exactly with the file (it went uncounted before)."""
        rng = np.random.default_rng(11)
        cases = []
        x = rng.random((50000, 2)) * 0.5
        x[100:105] = 1.0                        # run in the middle
        x[-3:] = -1.0                           # run that ends with the file
        cases.append(x)
        y = rng.random((30000, 2)) * 2 - 1
        y[:4] = 1.0                             # run at the start
        cases.append(y)
        z = np.zeros((1000, 1)); z[:3] = 1.0    # run of 3 at the start, in mono
        cases.append(z)
        for i, data in enumerate(cases):
            with self.subTest(caso=i, shape=data.shape):
                sr = 8000
                meta = {"sample_rate": sr, "bits_per_sample": 16,
                        "canales": data.shape[1], "duracion": 1.0, "md5": None}
                accumulator = engine._BlockAnalysis(meta, sr, len(data), "full", None)
                for start in range(0, len(data), 7777):     # irregular blocks
                    accumulator.add(data[start : start + 7777], start)
                self.assertEqual(accumulator.result()["clip"],
                                 engine.detect_clipping(data))

    def test_dr_by_blocks_equal_via_array(self):
        """The DR accumulated over irregular blocks must be identical."""
        sr = 8000
        rng = np.random.default_rng(9)
        x = rng.standard_normal((sr * 20, 2)) * 0.2
        x[sr * 6 : sr * 9] *= 5.0                # one clearly louder block
        expected = engine.calculate_dr(x, sr)
        self.assertIsNotNone(expected)

        meta = {"sample_rate": sr, "bits_per_sample": 16, "canales": 2,
                "duracion": 20.0, "md5": 1}
        accumulator = engine._BlockAnalysis(meta, sr, len(x), "full", None)
        offset = 0
        for cutoff in (37, 1, 100003, 4096, 99991, 10 ** 9):
            chunk = x[offset : offset + cutoff]
            if not len(chunk):
                continue
            accumulator.add(chunk, offset)
            offset += len(chunk)
        self.assertEqual(offset, len(x))
        self.assertEqual(accumulator.result()["dr"], expected)

    def test_dr_file_short_still_being_none(self):
        """Less than 6 seconds = less than two 3 s blocks."""
        self.assertIsNone(engine.calculate_dr(np.zeros((8000 * 5, 2)), 8000))

    def test_result_idempotent_on_eof_clipping(self):
        """Calling acc.result() repeatedly when EOF ends in an open clipping streak is idempotent."""
        sr = 8000
        rng = np.random.default_rng(77)
        x = np.clip(rng.standard_normal((sr * 2, 2)) * 0.15, -0.5, 0.5)
        x[100:104, 0] = 1.0
        x[-5:, 1] = -1.0
        meta = {"sample_rate": sr, "bits_per_sample": 16, "canales": 2,
                "duracion": 2.0, "md5": None}
        acc = engine._BlockAnalysis(meta, sr, len(x), "full", None)
        acc.add(x, 0)
        first = acc.result()
        second = acc.result()
        self.assertEqual(first, second)
        self.assertEqual(first["clip"]["runs_clip"], 2)
        self.assertEqual(first["esp"]["clipping_eventos"], 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
