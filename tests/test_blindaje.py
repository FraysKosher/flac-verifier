"""Block 1 (hardening): no file may abort the program.

Covers the failures reproduced in the audit:
  - mutagen raised on garbage after the 'fLaC' header and killed the process.
  - verificar_metadatos raised MutagenError on a path that does not exist.
  - an unexpected exception escaped from analizar_archivo_datos.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fixtures as fx

import motor_flac as motor


class TestArchivosRotos(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.fx = fx.Fixtures()

    @classmethod
    def tearDownClass(cls):
        cls.fx.cerrar()

    def test_verify_metadata_no_raises(self):
        for nombre in self.fx.NOMBRES_ROTOS:      # includes 'truncado'
            with self.subTest(archivo=nombre):
                meta = motor.verify_metadata(self.fx[nombre])   # must not raise
                self.assertIsInstance(meta, dict)
                self.assertIn("valido", meta)

    def test_metadata_unreadable_mark_as_invalid(self):
        for nombre in ("basura_magic", "texto", "cero_bytes"):
            with self.subTest(archivo=nombre):
                self.assertFalse(motor.verify_metadata(self.fx[nombre])["valido"])

    def test_error_unexpected_brings_diagnostic(self):
        """With garbage after the 'fLaC' header, mutagen raised and killed the process."""
        meta = motor.verify_metadata(self.fx["basura_magic"])
        self.assertFalse(meta["valido"])
        self.assertIn("error", meta)

    def test_metadata_readable_no_mean_file_intact(self):
        """mutagen reads STREAMINFO from the header even when half the file is
        missing: 'valid metadata' is NOT an integrity check. The corruption is
        detected later, when decoding or verifying the MD5."""
        meta = motor.verify_metadata(self.fx["truncado"])
        self.assertTrue(meta["valido"])
        res = motor.analyze_file_data(self.fx["truncado"], "center", None, False)
        self.assertIn("error", res)
        self.assertNotIn("veredicto", res)

    def test_verify_metadata_with_path_nonexistent(self):
        meta = motor.verify_metadata(os.path.join(self.fx.dir, "no_existe.flac"))
        self.assertFalse(meta["valido"])
        self.assertIn("error", meta)

    def test_verify_signature_no_raises(self):
        self.assertTrue(motor.verify_signature(self.fx["ok16"]))
        self.assertFalse(motor.verify_signature(os.path.join(self.fx.dir, "no_existe.flac")))
        for nombre in self.fx.NOMBRES_ROTOS:
            with self.subTest(archivo=nombre):
                motor.verify_signature(self.fx[nombre])          # must not raise

    def test_analyze_file_broken_returns_error_without_verdict(self):
        for nombre in self.fx.NOMBRES_ROTOS:
            with self.subTest(archivo=nombre):
                res = motor.analyze_file_data(self.fx[nombre], "center", None, False)
                self.assertIn("error", res)
                self.assertNotIn("veredicto", res)
                self.assertNotIn("score", res)

    def test_read_audio_no_raises(self):
        for nombre in self.fx.NOMBRES_ROTOS:
            with self.subTest(archivo=nombre):
                data, sr = motor.read_audio(self.fx[nombre])
                self.assertIsNone(data)

    def test_files_valid_follow_being_analyzed(self):
        """The hardening must not break the happy path."""
        res = motor.analyze_file_data(self.fx["ok16"], "center", None, False)
        self.assertNotIn("error", res)
        self.assertIn("veredicto", res)
        self.assertIn("esp", res)
        self.assertIn("score", res)


if __name__ == "__main__":
    unittest.main(verbosity=2)
