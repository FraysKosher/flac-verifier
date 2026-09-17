"""Bloque 3 (integridad): el MD5 del STREAMINFO se verifica de verdad.

Reglas que fijan estos tests:
  - 'coincide'  -> el audio decodificado es exactamente el que registró el encoder.
  - 'ausente'   -> no hay MD5 registrado; no es un error, pero no se premia.
  - 'no_coincide' -> ERROR DURO: archivo corrupto o alterado, sin veredicto.
  - el MD5 no es un voto de scoring en ningún sentido.
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
        # no puede recibir veredicto de lossless ni score
        self.assertNotIn("veredicto", res)
        self.assertNotIn("score", res)
        # ni siquiera se gasta en analizar el espectro de un archivo corrupto
        self.assertNotIn("esp", res)
        self.assertNotEqual(res["md5"]["calculado"], res["md5"]["almacenado"])

    def test_md5_ausente_no_es_error(self):
        res = motor.analizar_archivo_datos(self.fx["md5_ausente"], "center", None, False)
        self.assertEqual(res["md5"]["estado"], "absent")
        self.assertNotIn("error", res)
        self.assertIn("veredicto", res)
        self.assertIn("score", res)

    def test_el_md5_no_puntua(self):
        """Mismo audio con MD5 presente y con MD5 ausente -> score idéntico."""
        con_md5    = motor.analizar_archivo_datos(self.fx["ok16"], "center", None, False)
        sin_md5    = motor.analizar_archivo_datos(self.fx["md5_ausente"], "center", None, False)
        self.assertEqual(con_md5["md5"]["estado"], "match")
        self.assertEqual(sin_md5["md5"]["estado"], "absent")
        self.assertEqual(con_md5["score"], sin_md5["score"])
        self.assertEqual(con_md5["veredicto"], sin_md5["veredicto"])
        self.assertEqual(con_md5["problemas"], sin_md5["problemas"])

    def test_md5_pcm_coincide_con_mutagen(self):
        """El hash recalculado es el mismo que mutagen reporta del STREAMINFO."""
        from mutagen.flac import FLAC
        for nombre in ("ok16", "ok24"):
            with self.subTest(archivo=nombre):
                data, _ = motor.leer_audio(self.fx[nombre])
                almacenado = int(FLAC(self.fx[nombre]).info.md5_signature)
                calculado = motor._md5_pcm(data, 16 if nombre == "ok16" else 24)
                self.assertEqual(int(calculado, 16), almacenado)

    def test_profundidad_no_verificable_devuelve_none(self):
        """Una profundidad rara no puede acusar en falso de corrupción."""
        self.assertIsNone(motor._md5_pcm([[0.0]], 20))
        self.assertIsNone(motor._md5_pcm([[0.0]], 12))


if __name__ == "__main__":
    unittest.main(verbosity=2)
