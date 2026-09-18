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

import motor_flac as motor


class TestMD5(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.fx = fx.Fixtures()

    @classmethod
    def tearDownClass(cls):
        cls.fx.cerrar()

    def test_coincide_en_8_16_y_24_bits(self):
        for nombre in ("ok8", "ok16", "ok24"):
            with self.subTest(archivo=nombre):
                res = motor.analizar_archivo_datos(self.fx[nombre], "center", None, False)
                self.assertNotIn("error", res)
                self.assertEqual(res["md5"]["estado"], "match")
                self.assertEqual(res["md5"]["calculado"], res["md5"]["almacenado"])

    def test_md5_falseado_es_error_duro(self):
        res = motor.analizar_archivo_datos(self.fx["md5_obsoleto"], "center", None, False)
        self.assertEqual(res["md5"]["estado"], "mismatch")
        self.assertIn("error", res)
        # it can receive neither a lossless verdict nor a score
        self.assertNotIn("veredicto", res)
        self.assertNotIn("score", res)
        # it does not even spend time analysing the spectrum of a corrupt file
        self.assertNotIn("esp", res)
        self.assertNotEqual(res["md5"]["calculado"], res["md5"]["almacenado"])

    def test_md5_ausente_no_es_error(self):
        res = motor.analizar_archivo_datos(self.fx["md5_ausente"], "center", None, False)
        self.assertEqual(res["md5"]["estado"], "absent")
        self.assertNotIn("error", res)
        self.assertIn("veredicto", res)
        self.assertIn("score", res)

    def test_el_md5_no_puntua(self):
        """Same audio with MD5 present and with MD5 absent -> identical score."""
        con_md5    = motor.analizar_archivo_datos(self.fx["ok16"], "center", None, False)
        sin_md5    = motor.analizar_archivo_datos(self.fx["md5_ausente"], "center", None, False)
        self.assertEqual(con_md5["md5"]["estado"], "match")
        self.assertEqual(sin_md5["md5"]["estado"], "absent")
        self.assertEqual(con_md5["score"], sin_md5["score"])
        self.assertEqual(con_md5["veredicto"], sin_md5["veredicto"])
        self.assertEqual(con_md5["problemas"], sin_md5["problemas"])

    def test_md5_pcm_coincide_con_mutagen(self):
        """The recomputed hash is the same as the one mutagen reports from STREAMINFO."""
        from mutagen.flac import FLAC
        for nombre in ("ok16", "ok24"):
            with self.subTest(archivo=nombre):
                data, _ = motor.leer_audio(self.fx[nombre])
                almacenado = int(FLAC(self.fx[nombre]).info.md5_signature)
                calculado = motor._md5_pcm(data, 16 if nombre == "ok16" else 24)
                self.assertEqual(int(calculado, 16), almacenado)

    def test_profundidad_no_verificable_devuelve_none(self):
        """An odd bit depth must not falsely accuse the file of corruption."""
        self.assertIsNone(motor._md5_pcm([[0.0]], 20))
        self.assertIsNone(motor._md5_pcm([[0.0]], 12))


if __name__ == "__main__":
    unittest.main(verbosity=2)
