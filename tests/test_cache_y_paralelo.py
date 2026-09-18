"""Block 8: cache, parallelisation and the fast renderer.

What is pinned down here:
  - the second pass reuses the cache and gives EXACTLY the same result;
  - the cache is invalidated if the file changes (date, size or MD5 from
    STREAMINFO), if the mode changes or if the engine version changes;
  - with spectrograms, the cache only counts if the PNG is still there and is
    the right one;
  - the parallel analysis gives the same as the serial one and keeps the order;
  - an environment with no processes falls back to serial instead of failing;
  - the spectrogram is drawn with imshow (raster) and not with pcolormesh(gouraud).
"""
import json
import os
import re
import shutil
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fixtures as fx

import motor_flac as motor

PROCESOS = motor._sonda_procesos()
CAMPOS_VOLATILES = ("desde_cache", "tipo", "indice", "total")


def comparable(resultado):
    """The result without the fields that depend on the run."""
    return {k: v for k, v in resultado.items() if k not in CAMPOS_VOLATILES}


def eventos_final(salida):
    """Last event of the protocol."""
    return json.loads([l for l in salida.splitlines() if l.strip()][-1])


class TestCache(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.fx = fx.Fixtures()
        cls.carpeta = cls.fx.subcarpeta("cache", ("ok16", "ok24", "corte_16k"))

    @classmethod
    def tearDownClass(cls):
        cls.fx.cerrar()

    def copia(self, nombre, origen="ok16"):
        """A copy of its own per test: the cache lives next to the file."""
        destino = os.path.join(self.fx.dir, f"cache_{nombre}.flac")
        shutil.copy(self.fx[origen], destino)
        return destino

    def test_la_segunda_pasada_reaprovecha_y_da_lo_mismo(self):
        ruta = self.copia("reuso")
        primera = motor.analizar_archivo_datos(ruta, "center", None, False)
        self.assertFalse(primera.get("desde_cache"))
        segunda = motor.analizar_archivo_datos(ruta, "center", None, False)
        self.assertTrue(segunda.get("desde_cache"))
        self.assertEqual(comparable(primera), comparable(segunda))

    def test_la_entrada_de_cache_es_json_con_su_huella(self):
        ruta = self.copia("formato")
        motor.analizar_archivo_datos(ruta, "center", None, False)
        ruta_cache = motor._ruta_cache(ruta, "center", None)
        self.assertTrue(os.path.exists(ruta_cache))
        with open(ruta_cache, encoding="utf-8") as f:
            entrada = json.load(f)
        self.assertEqual(sorted(entrada), ["clave", "identidad", "render", "resultado"])
        self.assertEqual(entrada["clave"]["motor"], motor.VERSION_MOTOR)
        self.assertEqual(entrada["clave"]["modo"], "center")
        self.assertEqual(entrada["identidad"]["tamano"], os.path.getsize(ruta))
        self.assertIn("resultado", entrada)

    def test_se_invalida_si_cambia_la_fecha_del_archivo(self):
        ruta = self.copia("fecha")
        motor.analizar_archivo_datos(ruta, "center", None, False)
        info = os.stat(ruta)
        os.utime(ruta, ns=(info.st_atime_ns, info.st_mtime_ns + 1_000_000_000))
        otra = motor.analizar_archivo_datos(ruta, "center", None, False)
        self.assertFalse(otra.get("desde_cache"), "the cache was not invalidated by a different date")

    def test_se_invalida_si_cambia_el_md5_aunque_coincidan_tamano_y_fecha(self):
        """The hard case: same size and same date, different content."""
        ruta = self.copia("md5")
        primera = motor.analizar_archivo_datos(ruta, "center", None, False)
        self.assertEqual(primera["md5"]["estado"], "match")
        info = os.stat(ruta)

        with open(ruta, "r+b") as f:            # fakes the STREAMINFO MD5
            f.seek(26)
            f.write(b"\x00" * 16)
        os.utime(ruta, ns=(info.st_atime_ns, info.st_mtime_ns))   # same date

        segunda = motor.analizar_archivo_datos(ruta, "center", None, False)
        self.assertFalse(segunda.get("desde_cache"), "the cache did not look at the MD5")
        self.assertEqual(segunda["md5"]["estado"], "absent")

    def test_se_invalida_si_cambia_la_version_del_motor(self):
        ruta = self.copia("version")
        motor.analizar_archivo_datos(ruta, "center", None, False)
        original = motor.VERSION_MOTOR
        motor.VERSION_MOTOR = original + "-test"
        try:
            otra = motor.analizar_archivo_datos(ruta, "center", None, False)
        finally:
            motor.VERSION_MOTOR = original
        self.assertFalse(otra.get("desde_cache"))

    def test_se_invalida_si_cambia_el_modo(self):
        ruta = self.copia("modo")
        motor.analizar_archivo_datos(ruta, "center", None, False)
        otra = motor.analizar_archivo_datos(ruta, "full", None, False)
        self.assertFalse(otra.get("desde_cache"))
        # and repeating the original mode does reuse it
        self.assertTrue(motor.analizar_archivo_datos(ruta, "center", None, False)
                        .get("desde_cache"))

    def test_sin_cache_no_escribe_ni_reaprovecha(self):
        ruta = self.copia("sin_cache")
        primera = motor.analizar_archivo_datos(ruta, "center", None, False, usar_cache=False)
        self.assertNotIn("desde_cache", primera)
        self.assertFalse(os.path.exists(motor._ruta_cache(ruta, "center", None)))
        segunda = motor.analizar_archivo_datos(ruta, "center", None, False, usar_cache=False)
        self.assertNotIn("desde_cache", segunda)
        self.assertFalse(os.path.exists(motor._ruta_cache(ruta, "center", None)))

    def test_el_espectrograma_cacheado_se_reaprovecha(self):
        ruta = self.copia("png")
        primera = motor.analizar_archivo_datos(ruta, "center", None, True)
        ruta_png = primera["esp"]["espectrograma"]
        self.assertIsNotNone(ruta_png)
        sello = os.stat(ruta_png).st_mtime_ns

        segunda = motor.analizar_archivo_datos(ruta, "center", None, True)
        self.assertTrue(segunda.get("desde_cache"))
        self.assertEqual(segunda["esp"]["espectrograma"], ruta_png)
        self.assertEqual(os.stat(ruta_png).st_mtime_ns, sello,
                         "the spectrogram was drawn again instead of being reused")

    def test_si_falta_el_png_la_cache_no_vale_para_el_informe(self):
        ruta = self.copia("png_borrado")
        primera = motor.analizar_archivo_datos(ruta, "center", None, True)
        os.remove(primera["esp"]["espectrograma"])
        segunda = motor.analizar_archivo_datos(ruta, "center", None, True)
        self.assertFalse(segunda.get("desde_cache"), "it reused a cache entry with no PNG")
        self.assertTrue(os.path.exists(segunda["esp"]["espectrograma"]))

    def test_sin_png_no_se_reporta_una_ruta_generada_antes(self):
        ruta = self.copia("sin_png")
        con_png = motor.analizar_archivo_datos(ruta, "center", None, True)
        self.assertIsNotNone(con_png["esp"]["espectrograma"])
        sin_png = motor.analizar_archivo_datos(ruta, "center", None, False)
        self.assertIsNone(sin_png["esp"]["espectrograma"])


class TestParalelizacion(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.fx = fx.Fixtures()
        cls.carpeta = cls.fx.subcarpeta(
            "paralelo", ("ok16", "ok24", "corte_16k", "master_cd_20k"))

    @classmethod
    def tearDownClass(cls):
        cls.fx.cerrar()

    def _motor(self, *extra):
        # No cache: the point is to compare real analyses, not JSON reads.
        return fx.correr_motor(self.carpeta, extra=("--no-cache", *extra))

    def _resultados(self, salida):
        return [json.loads(l) for l in salida.splitlines()
                if l.strip() and json.loads(l)["tipo"] == "resultado"]

    def test_el_paralelo_da_exactamente_lo_mismo_que_el_secuencial(self):
        serie = self._resultados(self._motor("--workers", "1").stdout)
        paralelo = self._resultados(self._motor("--workers", "4").stdout)
        self.assertEqual(len(serie), 4)
        self.assertEqual(len(paralelo), 4)
        for uno, otro in zip(serie, paralelo):
            with self.subTest(archivo=uno["archivo"]):
                self.assertEqual(comparable(uno), comparable(otro))

    def test_se_conserva_el_orden_de_los_archivos(self):
        esperados = sorted(f for f in os.listdir(self.carpeta) if f.endswith(".flac"))
        resultados = self._resultados(self._motor("--workers", "4").stdout)
        self.assertEqual([r["archivo"] for r in resultados], esperados)
        self.assertEqual([r["indice"] for r in resultados], [1, 2, 3, 4])

    def test_el_evento_inicio_informa_de_los_procesos(self):
        eventos = [json.loads(l) for l in self._motor("--workers", "4").stdout.splitlines()
                   if l.strip()]
        inicio = eventos[0]
        self.assertEqual(inicio["tipo"], "inicio")
        self.assertEqual(inicio["total"], 4)
        self.assertIn("workers", inicio)
        self.assertIn("paralelo", inicio)
        self.assertFalse(inicio["cache"])          # --no-cache was requested
        self.assertEqual(eventos[-1]["tipo"], "fin")

    def test_el_pdf_sale_igual_con_paralelo(self):
        sonda = self.fx.subcarpeta("paralelo_pdf", ("ok16", "ok24", "corte_16k"))
        p = fx.correr_motor(sonda, extra=("--pdf", "--workers", "3"))
        eventos = [json.loads(l) for l in p.stdout.splitlines() if l.strip()]
        informe = [e for e in eventos if e["tipo"] == "informe"]
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertTrue(informe, "the report event was not emitted")
        self.assertEqual(informe[0]["paginas"], 5)          # cover + 3 + summary
        self.assertTrue(os.path.exists(informe[0]["ruta"]))
        self.assertGreater(os.path.getsize(informe[0]["ruta"]), 20 * 1024)
        self.assertEqual(informe[0]["desde_cache"], 0)      # first pass

    def test_un_archivo_roto_en_el_lote_no_tumba_el_paralelo(self):
        sonda = self.fx.subcarpeta("paralelo_roto", ("ok16", "ok24", "basura_magic"))
        p = fx.correr_motor(sonda, extra=("--workers", "3", "--no-cache"))
        resultados = self._resultados(p.stdout)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(len(resultados), 3)
        con_error = [r for r in resultados if r.get("error")]
        self.assertEqual(len(con_error), 1)
        self.assertEqual(con_error[0]["archivo"], "basura_magic.flac")
        con_veredicto = [r for r in resultados if r.get("veredicto")]
        self.assertEqual(len(con_veredicto), 2)
        self.assertEqual(eventos_final(p.stdout)["tipo"], "fin")

    def test_un_entorno_sin_procesos_degrada_a_secuencial(self):
        original = motor._sonda_procesos
        motor._sonda_procesos = lambda: False
        try:
            archivos = [os.path.join(self.carpeta, f)
                        for f in sorted(os.listdir(self.carpeta))]
            analisis, workers = motor.analizar_archivos(
                archivos, "center", None, False, workers=8, usar_cache=False)
            self.assertEqual(workers, 1)
            resultados = list(analisis)
            self.assertEqual(len(resultados), 4)
            self.assertTrue(all(r.get("archivo") for r in resultados))
        finally:
            motor._sonda_procesos = original

    @unittest.skipUnless(PROCESOS, "this environment does not allow creating processes")
    def test_con_procesos_disponibles_se_usa_el_pool(self):
        archivos = [os.path.join(self.carpeta, f)
                    for f in sorted(os.listdir(self.carpeta))]
        _, workers = motor.analizar_archivos(archivos, "center", None, False,
                                             workers=4, usar_cache=False)
        self.assertEqual(workers, 4)

    def test_la_cache_evita_arrancar_procesos(self):
        """With everything cached there is no work to spread: the pool is not needed."""
        sonda = self.fx.subcarpeta("cache_paralelo", ("ok16", "ok24", "corte_16k"))
        archivos = [os.path.join(sonda, f) for f in sorted(os.listdir(sonda))]
        for ruta in archivos:                      # first pass: fills the cache
            motor.analizar_archivo_datos(ruta, "center", None, False)
        analisis, workers = motor.analizar_archivos(archivos, "center", None, False,
                                                   workers=8, usar_cache=True)
        resultados = list(analisis)
        self.assertEqual(workers, 1, "it should not start processes when everything is cached")
        self.assertTrue(all(r.get("desde_cache") for r in resultados))


class TestRenderizador(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.fx = fx.Fixtures()

    @classmethod
    def tearDownClass(cls):
        cls.fx.cerrar()

    def test_el_espectrograma_se_pinta_con_imshow(self):
        """pcolormesh(shading='gouraud') with 2049x1500 cells took ~10 s per
        track; imshow paints a raster and is of the order of 10 times faster."""
        fuente = open(fx.MOTOR, encoding="utf-8").read()
        self.assertIn("ax.imshow(", fuente)
        self.assertNotIn("ax.pcolormesh(", fuente)      # the comment does mention it
        self.assertIn("shading=\"gouraud\"", fuente)    # only in the explanatory comment

    def test_huella_render_cambia_con_los_parametros(self):
        original_frames = motor.MAX_FRAMES_PNG
        original_dpi = motor.DPI_ESPECTROGRAMA
        base = motor.huella_render()
        try:
            motor.MAX_FRAMES_PNG = original_frames + 1
            self.assertNotEqual(motor.huella_render(), base)
            motor.MAX_FRAMES_PNG = original_frames
            self.assertEqual(motor.huella_render(), base)
            motor.DPI_ESPECTROGRAMA = original_dpi + 1
            self.assertNotEqual(motor.huella_render(), base)
        finally:
            motor.MAX_FRAMES_PNG = original_frames
            motor.DPI_ESPECTROGRAMA = original_dpi

    @unittest.skipUnless(motor.MATPLOTLIB_OK, "matplotlib not installed")
    def test_el_png_sigue_saliendo_con_un_tamano_razonable(self):
        ruta = self.fx["ok16"]
        resultado = motor.analizar_archivo_datos(ruta, "center", None, True,
                                                 usar_cache=False)
        ruta_png = resultado["esp"]["espectrograma"]
        self.assertIsNotNone(ruta_png, resultado["esp"].get("espectrograma_error"))
        self.assertGreater(os.path.getsize(ruta_png), 100 * 1024)
        with open(ruta_png, "rb") as f:
            self.assertEqual(f.read(8), b"\x89PNG\r\n\x1a\n")

    def test_un_fallo_al_pintar_se_informa(self):
        """The reason for the failure reaches the result (before, it was lost silently)."""
        original = motor.plt
        try:
            motor.plt = None                    # any use of plt will blow up
            resultado = motor.analizar_archivo_datos(self.fx["ok16"], "center", None,
                                                     True, usar_cache=False)
            self.assertIsNone(resultado["esp"]["espectrograma"])
            self.assertIn("espectrograma_error", resultado["esp"])
        finally:
            motor.plt = original


if __name__ == "__main__":
    unittest.main(verbosity=2)
