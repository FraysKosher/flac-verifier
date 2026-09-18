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

    def test_verificar_metadatos_no_lanza(self):
        for nombre in self.fx.NOMBRES_ROTOS:      # includes 'truncado'
            with self.subTest(archivo=nombre):
                meta = motor.verificar_metadatos(self.fx[nombre])   # must not raise
                self.assertIsInstance(meta, dict)
                self.assertIn("valido", meta)

    def test_metadatos_ilegibles_se_marcan_como_invalidos(self):
        for nombre in ("basura_magic", "texto", "cero_bytes"):
            with self.subTest(archivo=nombre):
                self.assertFalse(motor.verificar_metadatos(self.fx[nombre])["valido"])

    def test_error_inesperado_trae_diagnostico(self):
        """With garbage after the 'fLaC' header, mutagen raised and killed the process."""
        meta = motor.verificar_metadatos(self.fx["basura_magic"])
        self.assertFalse(meta["valido"])
        self.assertIn("error", meta)

    def test_metadatos_legibles_no_significan_archivo_integro(self):
        """mutagen reads STREAMINFO from the header even when half the file is
        missing: 'valid metadata' is NOT an integrity check. The corruption is
        detected later, when decoding or verifying the MD5."""
        meta = motor.verificar_metadatos(self.fx["truncado"])
        self.assertTrue(meta["valido"])
        res = motor.analizar_archivo_datos(self.fx["truncado"], "center", None, False)
        self.assertIn("error", res)
        self.assertNotIn("veredicto", res)

    def test_verificar_metadatos_con_ruta_inexistente(self):
        meta = motor.verificar_metadatos(os.path.join(self.fx.dir, "no_existe.flac"))
        self.assertFalse(meta["valido"])
        self.assertIn("error", meta)

    def test_verificar_firma_no_lanza(self):
        self.assertTrue(motor.verificar_firma(self.fx["ok16"]))
        self.assertFalse(motor.verificar_firma(os.path.join(self.fx.dir, "no_existe.flac")))
        for nombre in self.fx.NOMBRES_ROTOS:
            with self.subTest(archivo=nombre):
                motor.verificar_firma(self.fx[nombre])          # must not raise

    def test_analizar_archivo_roto_devuelve_error_sin_veredicto(self):
        for nombre in self.fx.NOMBRES_ROTOS:
            with self.subTest(archivo=nombre):
                res = motor.analizar_archivo_datos(self.fx[nombre], "center", None, False)
                self.assertIn("error", res)
                self.assertNotIn("veredicto", res)
                self.assertNotIn("score", res)

    def test_leer_audio_no_lanza(self):
        for nombre in self.fx.NOMBRES_ROTOS:
            with self.subTest(archivo=nombre):
                data, sr = motor.leer_audio(self.fx[nombre])
                self.assertIsNone(data)

    def test_archivos_validos_siguen_analizandose(self):
        """The hardening must not break the happy path."""
        res = motor.analizar_archivo_datos(self.fx["ok16"], "center", None, False)
        self.assertNotIn("error", res)
        self.assertIn("veredicto", res)
        self.assertIn("esp", res)
        self.assertIn("score", res)


if __name__ == "__main__":
    unittest.main(verbosity=2)
