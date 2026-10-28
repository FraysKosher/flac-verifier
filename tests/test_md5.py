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
        for name in ("ok8", "ok16", "ok24"):
            with self.subTest(file=name):
                res = engine.analyze_file_data(self.fx[name], "center", None, False)
                self.assertNotIn("error", res)
                self.assertEqual(res["md5"]["estado"], "match")
                self.assertEqual(res["md5"]["calculado"], res["md5"]["almacenado"])

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
