"""Bloque 7 (estructura): capa fina, guarda __main__, UTF-8 y sin duplicación.

Estos tests vigilan las invariantes estructurales del proyecto:
  - importar un módulo no puede ejecutar ningún CLI.
  - la lógica de análisis NO puede volver a duplicarse en el CLI.
  - la salida con stdout redirigido no puede morir por codificación.
  - la tabla de veredictos del CLI cubre las salidas reales del motor.
"""
import json
import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fixtures as fx

import motor_flac as motor
import verificar_flac as cli

# Funciones que deben vivir SOLO en el motor.
FUNCIONES_DEL_MOTOR = (
    "verificar_firma", "verificar_metadatos", "leer_audio", "_md5_pcm",
    "verificar_md5", "analizar_bit_depth", "analizar_espectro",
    "_guardar_espectrograma", "detectar_clipping", "calcular_dr",
    "calcular_score", "analizar_archivo_datos",
)


class TestSinEfectosSecundarios(unittest.TestCase):

    def test_importar_los_modulos_no_ejecuta_ningun_cli(self):
        """Antes, 'import verificar_flac' arrancaba el menú interactivo."""
        codigo = "import motor_flac, verificar_flac; print('IMPORT_OK')"
        p = subprocess.run([sys.executable, "-c", codigo], cwd=fx.RAIZ,
                           capture_output=True, text=True, encoding="utf-8", timeout=180)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout.strip(), "IMPORT_OK")
        self.assertEqual(p.stderr.strip(), "")


class TestCapaFina(unittest.TestCase):

    def test_el_cli_no_redefine_el_motor(self):
        for nombre in FUNCIONES_DEL_MOTOR:
            with self.subTest(funcion=nombre):
                self.assertNotIn(
                    nombre, vars(cli),
                    f"{nombre} vuelve a estar definido en verificar_flac.py: "
                    f"la lógica se ha duplicado en vez de usar motor_flac")

    def test_el_cli_importa_el_motor(self):
        self.assertIs(cli.motor, motor)

    def test_la_dependencia_del_pdf_queda_aislada(self):
        """Ni el motor ni el CLI importan reportlab: solo informe_pdf lo usa, y
        por eso ambos funcionan aunque reportlab no esté instalado."""
        for ruta in (fx.MOTOR, fx.CLI):
            with self.subTest(modulo=os.path.basename(ruta)):
                fuente = open(ruta, encoding="utf-8").read()
                self.assertNotRegex(fuente, r"(?m)^\s*(?:import|from)\s+reportlab")
                self.assertNotRegex(fuente, r"(?m)^\s*(?:import|from)\s+PIL")

    def test_el_informe_no_usa_reportlab_al_importar(self):
        """Un valor por defecto en la firma se evalúa al importar el módulo: usar
        ahí un nombre de reportlab (mm, A4, colors…) rompía la importación cuando
        la librería no está, y con ella el aviso de dependencia que falta."""
        fuente = open(os.path.join(fx.RAIZ, "informe_pdf.py"), encoding="utf-8").read()
        for linea in fuente.splitlines():
            if linea.lstrip().startswith("def "):
                with self.subTest(firma=linea.strip()[:70]):
                    self.assertNotRegex(linea, r"=\s*[^,()]*\b(mm|A4)\b")
                    self.assertNotRegex(linea, r"=\s*[^,()]*colors\.")

    def test_el_motor_y_el_informe_se_importan_sin_reportlab(self):
        codigo = ("import sys\n"
                  "for m in list(sys.modules):\n"
                  "    if m.startswith('reportlab'):\n"
                  "        del sys.modules[m]\n"
                  "sys.modules['reportlab'] = None\n"
                  "import motor_flac, informe_pdf\n"
                  "print('motor OK', informe_pdf.disponible())\n"
                  "print(informe_pdf.generar('.', [{'archivo': 'x'}])['error'])\n")
        # El hijo imprime un mensaje con acento: sin esto, un stdout redirigido en
        # Windows usa cp1252 y el propio test moriría con UnicodeEncodeError.
        entorno = {**os.environ, "PYTHONIOENCODING": "utf-8"}
        p = subprocess.run([sys.executable, "-c", codigo], cwd=fx.RAIZ, env=entorno,
                           capture_output=True, text=True, encoding="utf-8", timeout=180)
        self.assertEqual(p.returncode, 0, p.stderr)
        lineas = p.stdout.strip().splitlines()
        self.assertEqual(lineas[0], "motor OK False")
        self.assertIn("reportlab", lineas[1])

    def test_la_tabla_de_veredictos_cubre_las_salidas_del_motor(self):
        esp_ok = {"error": None, "techo_hz": 22050, "corte_artificial": False,
                  "frecuencia_corte": None, "ratio": 1.0, "var_alta_db": 50.0,
                  "separacion_stereo": 1.0, "nyquist": 22050, "espectrograma": None}
        meta = {"sample_rate": 44100, "bits_per_sample": 16, "canales": 2,
                "duracion": 10.0, "md5": 1}
        casos = [esp_ok,
                 dict(esp_ok, techo_hz=8000, corte_artificial=True,
                      frecuencia_corte=16000, ratio=0.0, var_alta_db=0.0),
                 dict(esp_ok, error="fallo simulado")]
        for esp in casos:
            with self.subTest(esp=esp.get("error") or esp["techo_hz"]):
                score, veredicto, problemas = motor.calcular_score(meta, esp, {"aplica": False})
                self.assertTrue(0.0 <= score <= 1.0)
                self.assertIn(veredicto, cli.VEREDICTOS)
                self.assertIsInstance(problemas, list)

    def test_etiqueta_veredicto_no_depende_de_subcadenas(self):
        for veredicto in cli.VEREDICTOS:
            with self.subTest(veredicto=veredicto):
                self.assertTrue(cli.etiqueta_veredicto(veredicto))
        # un veredicto desconocido no debe romper la presentación
        self.assertIn("nuevo", cli.etiqueta_veredicto("nuevo"))


class TestUTF8(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.fx = fx.Fixtures()
        cls.carpeta = cls.fx.subcarpeta("cli_lote", ("ok16", "basura_magic"))

    @classmethod
    def tearDownClass(cls):
        cls.fx.cerrar()

    def _correr_cli(self, carpeta, pdf=False):
        """CLI con stdin/stdout redirigidos y SIN PYTHONIOENCODING.

        Es exactamente el escenario donde antes moría con UnicodeEncodeError.
        El orden de respuestas es: modo, PNG, informe PDF, ruta, otra carpeta."""
        entorno = {k: v for k, v in os.environ.items() if k != "PYTHONIOENCODING"}
        entrada = f"2\nn\n{'y' if pdf else 'n'}\n{carpeta}\nn\n"
        return subprocess.run([sys.executable, fx.CLI], input=entrada, cwd=fx.RAIZ,
                              capture_output=True, text=True, encoding="utf-8",
                              timeout=900, env=entorno)

    def test_cli_con_stdout_redirigido_no_muere_por_codificacion(self):
        p = self._correr_cli(self.carpeta)
        self.assertEqual(p.returncode, 0, p.stderr[-2000:])
        self.assertNotIn("UnicodeEncodeError", p.stderr or "")
        self.assertNotIn("Traceback", p.stderr or "")
        self.assertIn("═", p.stdout)                 # el carácter que reventaba en cp1252
        self.assertIn("✅", p.stdout)

    def test_cli_analiza_el_lote_entero_incluido_el_archivo_roto(self):
        p = self._correr_cli(self.carpeta)
        self.assertIn("Analyzing 2 file(s)", p.stdout)
        self.assertIn("SUMMARY:", p.stdout)
        self.assertIn("1 ❌ WITH ERRORS", p.stdout)     # el lote no se aborta
        self.assertIn("ok16", p.stdout)
        self.assertIn("basura_magic", p.stdout)

    def test_cli_sale_limpio_sin_entrada(self):
        """EOF en stdin (stdin cerrado) debe terminar con elegancia."""
        p = subprocess.run([sys.executable, fx.CLI], input="", cwd=fx.RAIZ,
                           capture_output=True, text=True, encoding="utf-8", timeout=180)
        self.assertEqual(p.returncode, 0, p.stderr[-2000:])
        self.assertNotIn("Traceback", p.stderr or "")


class TestEspectrogramaPNG(unittest.TestCase):
    """El PNG lo genera la vía en streaming; su firma cambió en el Bloque 6."""

    @classmethod
    def setUpClass(cls):
        cls.fx = fx.Fixtures()

    @classmethod
    def tearDownClass(cls):
        cls.fx.cerrar()

    @unittest.skipUnless(motor.MATPLOTLIB_OK, "matplotlib no instalado")
    def test_se_genera_el_png_y_se_reporta_la_ruta(self):
        res = motor.analizar_archivo_datos(self.fx["ok16"], "center", None, True)
        self.assertNotIn("error", res, res.get("error"))
        ruta = res["esp"]["espectrograma"]
        self.assertIsNotNone(ruta, "no se generó el espectrograma")
        self.assertTrue(os.path.exists(ruta))
        self.assertGreater(os.path.getsize(ruta), 1000)

    def test_sin_png_no_se_reporta_ruta(self):
        res = motor.analizar_archivo_datos(self.fx["ok16"], "center", None, False)
        self.assertIsNone(res["esp"]["espectrograma"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
