"""Block 3 (integrity): the STREAMINFO MD5 is really verified.

Rules these tests pin down:
  - 'match'    -> the decoded audio is exactly what the encoder recorded.
  - 'absent'   -> there is no registered MD5; it is not an error, but it earns
                  no credit.
  - 'mismatch' -> HARD ERROR: corrupt or altered file, with no verdict.
  - the MD5 is not a scoring vote in either direction.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fixtures as fx

import motor_flac as engine


class TestMD5(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.fx = fx.Fixtures()

    @classmethod
    def tearDownClass(cls):
        cls.fx.close()

    def test_matches_8_16_24_bits(self):
        import numpy as np
        from _motor_bits import _pcm_bytes

        for name in ("ok8", "ok16", "ok24"):
            with self.subTest(file=name):
                res = engine.analyze_file_data(self.fx[name], "center", None, False)
                self.assertNotIn("error", res)
                self.assertEqual(res["md5"]["estado"], "match")
                self.assertEqual(res["md5"]["calculado"], res["md5"]["almacenado"])

        edge = np.array([[1.0, -1.0]], dtype=np.float64)
        for bits, width in ((8, 1), (16, 2), (24, 3), (32, 4)):
            with self.subTest(clamp_bits=bits):
                raw = bytes(_pcm_bytes(edge, bits, width))
                pos = int.from_bytes(raw[:width], "little", signed=True)
                neg = int.from_bytes(raw[width : 2 * width], "little", signed=True)
                self.assertEqual(pos, (1 << (bits - 1)) - 1)
                self.assertEqual(neg, -(1 << (bits - 1)))

    def test_md5_faked_is_error_hard(self):
        res = engine.analyze_file_data(self.fx["md5_obsoleto"], "center", None, False)
        self.assertEqual(res["md5"]["estado"], "mismatch")
        self.assertIn("error", res)
        # it can receive neither a lossless verdict nor a score
        self.assertNotIn("veredicto", res)
        self.assertNotIn("score", res)
        # it does not even spend time analysing the spectrum of a corrupt file
        self.assertNotIn("esp", res)
        self.assertNotEqual(res["md5"]["calculado"], res["md5"]["almacenado"])

    def test_md5_absent_no_is_error(self):
        res = engine.analyze_file_data(self.fx["md5_ausente"], "center", None, False)
        self.assertEqual(res["md5"]["estado"], "absent")
        self.assertNotIn("error", res)
        self.assertIn("veredicto", res)
        self.assertIn("score", res)

    def test_md5_does_not_score(self):
        """Same audio with MD5 present and with MD5 absent -> identical score."""
        with_md5    = engine.analyze_file_data(self.fx["ok16"], "center", None, False)
        without_md5    = engine.analyze_file_data(self.fx["md5_ausente"], "center", None, False)
        self.assertEqual(with_md5["md5"]["estado"], "match")
        self.assertEqual(without_md5["md5"]["estado"], "absent")
        self.assertEqual(with_md5["score"], without_md5["score"])
        self.assertEqual(with_md5["veredicto"], without_md5["veredicto"])
        self.assertEqual(with_md5["problemas"], without_md5["problemas"])

    def test_md5_pcm_matches_with_mutagen(self):
        """The recomputed hash is the same as the one mutagen reports from STREAMINFO."""
        from mutagen.flac import FLAC
        for name in ("ok16", "ok24"):
            with self.subTest(file=name):
                data, _ = engine.read_audio(self.fx[name])
                stored = int(FLAC(self.fx[name]).info.md5_signature)
                calculated = engine._md5_pcm(data, 16 if name == "ok16" else 24)
                self.assertEqual(int(calculated, 16), stored)

    def test_depth_no_verifiable_returns_none(self):
        """An odd bit depth must not falsely accuse the file of corruption."""
        self.assertIsNone(engine._md5_pcm([[0.0]], 20))
        self.assertIsNone(engine._md5_pcm([[0.0]], 12))


class TestShortCircuitOnSilenceAndBadChecksum(unittest.TestCase):
    """Regressions for v2.10.1 short-circuit before STFT/PNG on invalid files."""

    def test_silence_and_bad_checksum_skip_stft_and_png(self):
        import os
        import tempfile
        from unittest import mock
        import numpy as np
        import soundfile as sf
        import motor_flac as engine

        with tempfile.TemporaryDirectory() as tmpdir:
            isolated_cache = os.path.join(tmpdir, "cache")
            silent_path = os.path.join(tmpdir, "silent.flac")
            sf.write(silent_path, np.zeros((16384, 2), dtype=np.float64),
                     44100, subtype="PCM_16")

            tone_path = os.path.join(tmpdir, "bad_md5.flac")
            t = np.arange(16384, dtype=np.float64) / 44100.0
            sig = np.column_stack([0.2 * np.sin(2 * np.pi * 1000.0 * t)] * 2)
            sf.write(tone_path, sig, 44100, subtype="PCM_16")

            with mock.patch.dict(
                os.environ, {"FLAC_VERIFIER_CACHE_DIR": isolated_cache}, clear=False
            ):
                with mock.patch.object(
                    engine._BlockAnalysis, "_spectral_result"
                ) as spec_res:
                    res_silent = engine.analyze_file_data(
                        silent_path, "full", None, save_png=True, use_cache=False
                    )
                    spec_res.assert_not_called()
                    self.assertIn("digital silence", res_silent.get("error", ""))

                    meta_bad = engine.verify_metadata(tone_path)
                    meta_bad["md5"] = meta_bad["md5"] ^ 1
                    res_md5 = engine.analyze_file_data(
                        tone_path, "full", None, save_png=True,
                        use_cache=False, _pre_meta=meta_bad,
                    )
                    spec_res.assert_not_called()
                    self.assertIn("MD5 does not match", res_md5.get("error", ""))

                # Neither short-circuited track should leave orphan PNGs in cache.
                orphan_pngs = [
                    name
                    for _, _, fnames in os.walk(isolated_cache)
                    for name in fnames
                    if name.lower().endswith(".png")
                ]
                self.assertEqual(orphan_pngs, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
