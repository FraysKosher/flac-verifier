"""Bloque 6 (rendimiento): la optimización no puede cambiar ningún resultado.

La vía array (cargar todo y analizar) se usa como ORÁCULO de la vía en streaming,
que es la que usa el programa. Además se fijan las invariantes del lector por
bloques y la equivalencia de la vectorización con la implementación ingenua.
"""
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fixtures as fx

import motor_flac as motor

CLAVES_ESPECTRALES = ("techo_hz", "techo_rel_nyquist", "clase_corte", "corte_detectado",
                      "frecuencia_corte", "distancia_nyquist", "corte_artificial",
                      "ratio", "ratio_aplica", "var_alta_db", "separacion_stereo",
                      "nyquist")


class TestStreamingIgualQueArray(unittest.TestCase):
    """Oráculo: mismos datos por las dos vías -> mismos resultados."""

    @classmethod
    def setUpClass(cls):
        cls.fx = fx.Fixtures()

    @classmethod
    def tearDownClass(cls):
        cls.fx.cerrar()

    def _comparar(self, nombre, modo="center"):
        ruta = self.fx[nombre]
        res = motor.analizar_archivo_datos(ruta, modo, None, False)
        self.assertNotIn("error", res, res.get("error"))

        data, sr = motor.leer_audio(ruta)
        meta = motor.verificar_metadatos(ruta)

        self.assertEqual(res["clip"], motor.detectar_clipping(data), "clipping")
        self.assertEqual(res["dr"], motor.calcular_dr(data, sr), "rango dinámico")
        self.assertEqual(res["bdi"], motor.analizar_bit_depth(data, meta), "bits bajos")
        self.assertEqual(res["md5"], motor.verificar_md5(data, meta), "MD5")

        esp = motor.analizar_espectro(data, sr, modo, None, False, None)
        self.assertIsNone(esp.get("error"), esp.get("error"))
        for clave in CLAVES_ESPECTRALES:
            with self.subTest(clave=clave):
                self.assertEqual(res["esp"][clave], esp[clave])

    def test_estereo_16_bits(self):
        self._comparar("ok16")

    def test_estereo_24_bits(self):
        self._comparar("ok24")

    def test_mono_no_reporta_separacion_estereo(self):
        """En mono no hay separación L-R: None en las dos vías."""
        self._comparar("ok16_mono")
        res = motor.analizar_archivo_datos(self.fx["ok16_mono"], "center", None, False)
        self.assertIsNone(res["esp"]["separacion_stereo"])

    def test_modo_completo(self):
        self._comparar("ok16", modo="full")


class TestLectorPorBloques(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.fx = fx.Fixtures()

    @classmethod
    def tearDownClass(cls):
        cls.fx.cerrar()

    def test_no_lee_mas_de_un_bloque_y_reconstruye_el_archivo(self):
        ruta = self.fx["ok24"]
        bloques = list(motor._leer_bloques(ruta))
        self.assertGreater(len(bloques), 0)
        for bloque in bloques:
            self.assertLessEqual(bloque.shape[0], motor.FRAMES_POR_BLOQUE)
        data, _ = motor.leer_audio(ruta)
        self.assertTrue(np.array_equal(np.concatenate(bloques), data))

    def test_el_md5_incremental_coincide_con_el_de_un_solo_array(self):
        """El hash por bloques debe dar el mismo resultado que el de todo el array."""
        ruta = self.fx["ok24"]
        data, _ = motor.leer_audio(ruta)
        meta = motor.verificar_metadatos(ruta)
        streaming = motor._analizar_en_streaming(ruta, meta, "full", None, False)
        self.assertEqual(streaming["md5"], motor.verificar_md5(data, meta))
        self.assertEqual(streaming["md5"]["estado"], "match")
        self.assertEqual(streaming["md5"]["calculado"],
                         motor._md5_pcm(data, meta["bits_per_sample"]))


class TestVectorizacion(unittest.TestCase):
    """La vectorización debe contar exactamente lo mismo que el bucle ingenuo."""

    @staticmethod
    def _clipping_ingenuo(data):
        """Implementación original, muestra a muestra (referencia)."""
        amp = np.max(np.abs(data), axis=1) if data.ndim > 1 else np.abs(data)
        en_clip = (amp >= 0.9999).astype(np.int8)
        runs = 0
        cuenta = 0
        for v in en_clip:
            if v:
                cuenta += 1
            else:
                if cuenta >= 3:
                    runs += 1
                cuenta = 0
        if cuenta >= 3:
            runs += 1
        return {"runs_clip": runs, "hay_clipping": runs > 0}

    def test_rachas_en_los_bordes_y_de_longitud_2_y_3(self):
        x = np.zeros((50, 2))
        x[0:3, 0]   = 1.0        # racha de 3 al principio -> cuenta
        x[10:12, 1] = -1.0       # racha de 2 -> no cuenta
        x[20:24, 0] = 1.0        # racha de 4 -> cuenta
        x[47:50, 1] = 1.0        # racha de 3 justo al final -> cuenta
        # el archivo entero: ambos coinciden y el resultado es el esperado
        self.assertEqual(motor.detectar_clipping(x), self._clipping_ingenuo(x))
        self.assertEqual(motor.detectar_clipping(x)["runs_clip"], 3)

    def test_equivalencia_con_datos_aleatorios_y_clipping_real(self):
        rng = np.random.default_rng(5)
        casos = [rng.random((100000, 2)) * 2 - 1]
        con_clip = rng.random((50000, 2)) * 0.5
        con_clip[100:105] = 1.0                  # racha de 5
        con_clip[49995:]  = -1.0                 # racha de 5 al final
        con_clip[20000:20002] = 1.0              # racha de 2
        casos.append(con_clip)
        for data in casos:
            with self.subTest(shape=data.shape):
                self.assertEqual(motor.detectar_clipping(data), self._clipping_ingenuo(data))

    def test_una_racha_partida_entre_bloques_cuenta_una_sola_vez(self):
        data = np.zeros((10, 1))
        data[3:9] = 1.0                          # una única racha de 6
        for corte in range(1, 10):
            with self.subTest(corte=corte):
                cuenta, runs = 0, 0
                for trozo in (data[:corte], data[corte:]):
                    if not len(trozo):
                        continue
                    rachas, cuenta = motor._rachas(trozo[:, 0] >= 0.9999, cuenta)
                    runs += sum(1 for largo in rachas if largo >= 3)
                if cuenta >= 3:
                    runs += 1
                self.assertEqual(runs, 1)

    def test_una_racha_repartida_en_tres_bloques(self):
        data = np.ones((9, 1))                   # racha de 9
        cuenta, runs = 0, 0
        for trozo in (data[:3], data[3:6], data[6:]):
            rachas, cuenta = motor._rachas(trozo[:, 0] >= 0.9999, cuenta)
            runs += sum(1 for largo in rachas if largo >= 3)
        if cuenta >= 3:
            runs += 1
        self.assertEqual(runs, 1)

    def test_el_acumulador_cuenta_igual_que_la_via_array(self):
        """Incluye la racha que termina justo con el archivo (se quedaba sin contar)."""
        rng = np.random.default_rng(11)
        casos = []
        x = rng.random((50000, 2)) * 0.5
        x[100:105] = 1.0                        # racha en medio
        x[-3:] = -1.0                           # racha que acaba con el archivo
        casos.append(x)
        y = rng.random((30000, 2)) * 2 - 1
        y[:4] = 1.0                             # racha al principio
        casos.append(y)
        z = np.zeros((1000, 1)); z[:3] = 1.0    # racha de 3 al principio, en mono
        casos.append(z)
        for i, data in enumerate(casos):
            with self.subTest(caso=i, shape=data.shape):
                sr = 8000
                meta = {"sample_rate": sr, "bits_per_sample": 16,
                        "canales": data.shape[1], "duracion": 1.0, "md5": None}
                acumulador = motor._AnalisisPorBloques(meta, sr, len(data), "full", None)
                for inicio in range(0, len(data), 7777):     # bloques irregulares
                    acumulador.anadir(data[inicio : inicio + 7777], inicio)
                self.assertEqual(acumulador.resultado()["clip"],
                                 motor.detectar_clipping(data))

    def test_dr_por_bloques_igual_que_la_via_array(self):
        """El DR acumulado en bloques irregulares debe ser idéntico."""
        sr = 8000
        rng = np.random.default_rng(9)
        x = rng.standard_normal((sr * 20, 2)) * 0.2
        x[sr * 6 : sr * 9] *= 5.0                # un bloque claramente más fuerte
        esperado = motor.calcular_dr(x, sr)
        self.assertIsNotNone(esperado)

        meta = {"sample_rate": sr, "bits_per_sample": 16, "canales": 2,
                "duracion": 20.0, "md5": 1}
        acumulador = motor._AnalisisPorBloques(meta, sr, len(x), "full", None)
        offset = 0
        for corte in (37, 1, 100003, 4096, 99991, 10 ** 9):
            trozo = x[offset : offset + corte]
            if not len(trozo):
                continue
            acumulador.anadir(trozo, offset)
            offset += len(trozo)
        self.assertEqual(offset, len(x))
        self.assertEqual(acumulador.resultado()["dr"], esperado)

    def test_dr_de_un_archivo_corto_sigue_siendo_none(self):
        """Menos de 6 segundos = menos de dos bloques de 3 s."""
        self.assertIsNone(motor.calcular_dr(np.zeros((8000 * 5, 2)), 8000))


if __name__ == "__main__":
    unittest.main(verbosity=2)
