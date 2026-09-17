"""Protocolo JSON del motor: una línea por evento, siempre válida.

Cubre los fallos reproducidos en la auditoría:
  - una excepción en un archivo interrumpía el lote y NUNCA se emitía 'fin'.
  - separacion_stereo = NaN producía JSON inválido que JSON.parse rechaza.
"""
import json
import math
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fixtures as fx

import motor_flac as motor


class TestProtocolo(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.fx = fx.Fixtures()

    @classmethod
    def tearDownClass(cls):
        cls.fx.cerrar()

    def test_lote_con_archivos_rotos_emite_inicio_resultados_y_fin(self):
        p, eventos, crudas = fx.resultados(self.fx.dir)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(eventos[0]["tipo"], "inicio")
        self.assertEqual(eventos[-1]["tipo"], "fin")        # antes se perdía
        resultados = [e for e in eventos if e["tipo"] == "resultado"]
        self.assertEqual(len(resultados), eventos[0]["total"])
        self.assertEqual(eventos[0]["total"], len(crudas) - 2)
        # todos los índices presentes y correlativos
        self.assertEqual([r["indice"] for r in resultados],
                         list(range(1, len(resultados) + 1)))

    def test_todas_las_lineas_son_json_estricto(self):
        """JSON.parse de JavaScript rechaza literales NaN/Infinity."""
        _, _, crudas = fx.resultados(self.fx.dir)
        for linea in crudas:
            with self.subTest(linea=linea[:70]):
                json.loads(linea, parse_constant=fx.rechaza_constante)
                self.assertNotIn("NaN", linea)
                self.assertNotIn("Infinity", linea)

    def test_archivo_32k_estereo_no_produce_nan(self):
        """Nyquist 16 kHz -> máscaras de banda alta vacías (origen del NaN)."""
        res = motor.analizar_archivo_datos(self.fx["estereo32k"], "center", None, False)
        esp = res["esp"]
        self.assertIsNone(esp["separacion_stereo"])       # antes: nan
        self.assertEqual(esp["ratio"], 0.0)
        for clave, valor in esp.items():
            if isinstance(valor, float):
                with self.subTest(metrica=clave):
                    self.assertTrue(math.isfinite(valor), f"{clave} = {valor}")

    def test_sanear_convierte_no_finitos_en_null(self):
        linea = motor._json_linea({"esp": {"ratio": float("nan")},
                                   "lista": [float("inf"), float("-inf")],
                                   "ok": 1.5})
        self.assertNotIn("NaN", linea)
        self.assertNotIn("Infinity", linea)
        self.assertEqual(json.loads(linea)["esp"]["ratio"], None)
        self.assertEqual(json.loads(linea)["lista"], [None, None])
        self.assertEqual(json.loads(linea)["ok"], 1.5)

    def test_resultado_por_archivo_trae_su_veredicto_o_su_error(self):
        _, eventos, _ = fx.resultados(self.fx.dir)
        for evento in eventos:
            if evento["tipo"] != "resultado":
                continue
            with self.subTest(archivo=evento["archivo"]):
                tiene_error = "error" in evento
                tiene_veredicto = "veredicto" in evento
                self.assertTrue(tiene_error or tiene_veredicto)
                self.assertFalse(tiene_error and tiene_veredicto)
                if tiene_veredicto:
                    self.assertTrue(0.0 <= evento["score"] <= 1.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
