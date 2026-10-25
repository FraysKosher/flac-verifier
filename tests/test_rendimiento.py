"""Block 6 (performance): the optimisation must not change any result.

The array path (load everything and analyse) is used as the ORACLE of the
streaming path, which is the one the program uses. The invariants of the block
reader and the equivalence of the vectorisation with the naive implementation
are pinned down as well.
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
    """Oracle: the same data through both paths -> the same results."""

    @classmethod
    def setUpClass(cls):
        cls.fx = fx.Fixtures()

    @classmethod
    def tearDownClass(cls):
        cls.fx.cerrar()

    def _comparar(self, nombre, modo="center"):
        ruta = self.fx[nombre]
        res = motor.analyze_file_data(ruta, modo, None, False)
        self.assertNotIn("error", res, res.get("error"))

        data, sr = motor.read_audio(ruta)
        meta = motor.verify_metadata(ruta)

        self.assertEqual(res["clip"], motor.detect_clipping(data), "clipping")
        self.assertEqual(res["dr"], motor.calculate_dr(data, sr), "dynamic range")
        self.assertEqual(res["bdi"], motor.analyze_bit_depth(data, meta), "low bits")
        self.assertEqual(res["md5"], motor.verify_md5(data, meta), "MD5")

        esp = motor.analyze_spectrum(data, sr, modo, None, False, None)
        self.assertIsNone(esp.get("error"), esp.get("error"))
        for clave in CLAVES_ESPECTRALES:
            with self.subTest(clave=clave):
                self.assertEqual(res["esp"][clave], esp[clave])

    def test_stereo_16_bits(self):
        self._comparar("ok16")

    def test_stereo_24_bits(self):
        self._comparar("ok24")

    def test_mono_no_reports_separation_stereo(self):
        """In mono there is no L-R separation: None in both paths."""
        self._comparar("ok16_mono")
        res = motor.analyze_file_data(self.fx["ok16_mono"], "center", None, False)
        self.assertIsNone(res["esp"]["separacion_stereo"])

    def test_mode_full(self):
        self._comparar("ok16", modo="full")


class TestLectorPorBloques(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.fx = fx.Fixtures()

    @classmethod
    def tearDownClass(cls):
        cls.fx.cerrar()

    def test_no_reads_more_block_rebuilds_file(self):
        ruta = self.fx["ok24"]
        bloques = list(motor._read_blocks(ruta))
        self.assertGreater(len(bloques), 0)
        for bloque in bloques:
            self.assertLessEqual(bloque.shape[0], motor.FRAMES_BY_BLOCK)
        data, _ = motor.read_audio(ruta)
        self.assertTrue(np.array_equal(np.concatenate(bloques), data))

    def test_md5_incremental_matches_with_only_array(self):
        """The block hash must give the same result as hashing the whole array."""
        ruta = self.fx["ok24"]
        data, _ = motor.read_audio(ruta)
        meta = motor.verify_metadata(ruta)
        streaming = motor._analyze_streaming(ruta, meta, "full", None, False)
        self.assertEqual(streaming["md5"], motor.verify_md5(data, meta))
        self.assertEqual(streaming["md5"]["estado"], "match")
        self.assertEqual(streaming["md5"]["calculado"],
                         motor._md5_pcm(data, meta["bits_per_sample"]))


class TestVectorizacion(unittest.TestCase):
    """The vectorisation must count exactly the same as the naive loop."""

    @staticmethod
    def _clipping_ingenuo(data):
        """Original implementation, sample by sample (reference)."""
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

    def test_streaks_edges_length_2_3(self):
        x = np.zeros((50, 2))
        x[0:3, 0]   = 1.0        # run of 3 at the start -> counts
        x[10:12, 1] = -1.0       # run of 2 -> does not count
        x[20:24, 0] = 1.0        # run of 4 -> counts
        x[47:50, 1] = 1.0        # run of 3 right at the end -> counts
        # the whole file: both agree and the result is the expected one
        self.assertEqual(motor.detect_clipping(x), self._clipping_ingenuo(x))
        self.assertEqual(motor.detect_clipping(x)["runs_clip"], 3)

    def test_equivalence_with_data_random_clipping_real(self):
        rng = np.random.default_rng(5)
        casos = [rng.random((100000, 2)) * 2 - 1]
        con_clip = rng.random((50000, 2)) * 0.5
        con_clip[100:105] = 1.0                  # run of 5
        con_clip[49995:]  = -1.0                 # run of 5 at the end
        con_clip[20000:20002] = 1.0              # run of 2
        casos.append(con_clip)
        for data in casos:
            with self.subTest(shape=data.shape):
                self.assertEqual(motor.detect_clipping(data), self._clipping_ingenuo(data))

    def test_streak_split_between_blocks_counts_alone_time(self):
        data = np.zeros((10, 1))
        data[3:9] = 1.0                          # a single run of 6
        for corte in range(1, 10):
            with self.subTest(corte=corte):
                cuenta, runs = 0, 0
                for trozo in (data[:corte], data[corte:]):
                    if not len(trozo):
                        continue
                    rachas, cuenta = motor._streaks(trozo[:, 0] >= 0.9999, cuenta)
                    runs += sum(1 for largo in rachas if largo >= 3)
                if cuenta >= 3:
                    runs += 1
                self.assertEqual(runs, 1)

    def test_streak_split_three_blocks(self):
        data = np.ones((9, 1))                   # run of 9
        cuenta, runs = 0, 0
        for trozo in (data[:3], data[3:6], data[6:]):
            rachas, cuenta = motor._streaks(trozo[:, 0] >= 0.9999, cuenta)
            runs += sum(1 for largo in rachas if largo >= 3)
        if cuenta >= 3:
            runs += 1
        self.assertEqual(runs, 1)

    def test_accumulator_counts_equal_via_array(self):
        """It includes the run that ends exactly with the file (it went uncounted before)."""
        rng = np.random.default_rng(11)
        casos = []
        x = rng.random((50000, 2)) * 0.5
        x[100:105] = 1.0                        # run in the middle
        x[-3:] = -1.0                           # run that ends with the file
        casos.append(x)
        y = rng.random((30000, 2)) * 2 - 1
        y[:4] = 1.0                             # run at the start
        casos.append(y)
        z = np.zeros((1000, 1)); z[:3] = 1.0    # run of 3 at the start, in mono
        casos.append(z)
        for i, data in enumerate(casos):
            with self.subTest(caso=i, shape=data.shape):
                sr = 8000
                meta = {"sample_rate": sr, "bits_per_sample": 16,
                        "canales": data.shape[1], "duracion": 1.0, "md5": None}
                acumulador = motor._BlockAnalysis(meta, sr, len(data), "full", None)
                for inicio in range(0, len(data), 7777):     # irregular blocks
                    acumulador.add(data[inicio : inicio + 7777], inicio)
                self.assertEqual(acumulador.result()["clip"],
                                 motor.detect_clipping(data))

    def test_dr_by_blocks_equal_via_array(self):
        """The DR accumulated over irregular blocks must be identical."""
        sr = 8000
        rng = np.random.default_rng(9)
        x = rng.standard_normal((sr * 20, 2)) * 0.2
        x[sr * 6 : sr * 9] *= 5.0                # one clearly louder block
        esperado = motor.calculate_dr(x, sr)
        self.assertIsNotNone(esperado)

        meta = {"sample_rate": sr, "bits_per_sample": 16, "canales": 2,
                "duracion": 20.0, "md5": 1}
        acumulador = motor._BlockAnalysis(meta, sr, len(x), "full", None)
        offset = 0
        for corte in (37, 1, 100003, 4096, 99991, 10 ** 9):
            trozo = x[offset : offset + corte]
            if not len(trozo):
                continue
            acumulador.add(trozo, offset)
            offset += len(trozo)
        self.assertEqual(offset, len(x))
        self.assertEqual(acumulador.result()["dr"], esperado)

    def test_dr_file_short_still_being_none(self):
        """Less than 6 seconds = less than two 3 s blocks."""
        self.assertIsNone(motor.calculate_dr(np.zeros((8000 * 5, 2)), 8000))


if __name__ == "__main__":
    unittest.main(verbosity=2)
