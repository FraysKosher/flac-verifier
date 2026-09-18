"""Blocks 2, 4 and 5: verdict coherence, honest bit depth and cut-off with
distance to Nyquist.

Acceptance criteria (fixtures from _audit_flac\\realista):
  - a 320 kbps MP3 repackaged into 24 bits must NOT be "PROBABLY LOSSLESS";
  - a CD master with an anti-alias filter at 20 kHz must NOT be accused of
    transcoding;
  - digital silence receives no verdict;
  - LSB utilisation never rewards (it only penalises).
"""
import os
import shutil
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fixtures as fx

import motor_flac as motor

VEREDICTOS_BUENOS = ("GENUINE LOSSLESS", "PROBABLY LOSSLESS")

ESP_PERFECTO = {"error": None, "techo_hz": 22050, "techo_rel_nyquist": 1.0,
                "clase_corte": "no_cutoff", "corte_detectado": False,
                "frecuencia_corte": None, "distancia_nyquist": None,
                "corte_artificial": False, "ratio": 1.0, "ratio_aplica": True,
                "var_alta_db": 50.0, "separacion_stereo": 1.0, "nyquist": 22050,
                "espectrograma": None}
META_CD = {"sample_rate": 44100, "bits_per_sample": 16, "canales": 2,
           "duracion": 10.0, "md5": 1}
SIN_BITDEPTH = {"aplica": False}


class TestVeredicto(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.fx = fx.Fixtures()

    @classmethod
    def tearDownClass(cls):
        cls.fx.cerrar()

    def analizar(self, nombre):
        res = motor.analizar_archivo_datos(self.fx[nombre], "center", None, False)
        self.assertNotIn("error", res, res.get("error"))
        return res

    # ── Block 2: silence guard ──────────────────────────────────────────────
    def test_el_silencio_digital_no_recibe_veredicto(self):
        res = motor.analizar_archivo_datos(self.fx["silence"], "center", None, False)
        self.assertIn("error", res)
        self.assertIn("silence", res["error"].lower())
        self.assertNotIn("veredicto", res)
        self.assertNotIn("score", res)

    # ── Block 5: CD master with a 20 kHz LPF ────────────────────────────────
    def test_master_de_cd_con_lpf_no_es_acusado_de_transcodificacion(self):
        res = self.analizar("master_cd_20k")
        esp = res["esp"]
        # The step is detected as a fact...
        self.assertTrue(esp["corte_detectado"])
        # ...but it is at less than 12% of Nyquist: the anti-alias filter zone.
        self.assertGreaterEqual(esp["distancia_nyquist"], 0.88)
        self.assertEqual(esp["clase_corte"], "near_nyquist_cutoff")
        self.assertFalse(esp["corte_artificial"])          # it is no longer an accusation
        # No message may accuse it of transcoding.
        texto = " ".join(res["problemas"]).lower()
        for acusacion in ("codec signature", "codec signature", "transcode",
                          "lossy", "artificial cut-off"):
            self.assertNotIn(acusacion, texto, f"unwarranted accusation: {acusacion}")
        self.assertNotIn(res["veredicto"], ("PROBABLE UPSCALE",))

    def test_corte_muy_por_debajo_del_nyquist_si_se_acusa(self):
        res = self.analizar("corte_16k")
        esp = res["esp"]
        self.assertLess(esp["distancia_nyquist"], 0.88)
        self.assertEqual(esp["clase_corte"], "far_cutoff")
        self.assertTrue(esp["corte_artificial"])
        self.assertTrue(any("artificial cut-off" in p for p in res["problemas"]))
        self.assertNotIn(res["veredicto"], VEREDICTOS_BUENOS)

    def test_la_frecuencia_del_corte_usa_redondeo_real(self):
        """It used to be printed with integer division: 19 821 Hz -> '19.0 kHz'."""
        res = self.analizar("corte_16k")
        fc = res["esp"]["frecuencia_corte"]
        esperado = f"{fc/1000:.1f} kHz"
        self.assertTrue(any(esperado in p for p in res["problemas"]),
                        f"no problem mentions {esperado}")

    def test_un_corte_lejano_bloquea_el_veredicto_maximo(self):
        """With all the rest of the evidence perfect, a confirmed defect cannot
        coexist with 'GENUINE LOSSLESS'."""
        esp = dict(ESP_PERFECTO, clase_corte="far_cutoff", corte_detectado=True,
                   frecuencia_corte=16000, distancia_nyquist=0.73,
                   corte_artificial=True)
        score, veredicto, problemas = motor.calcular_score(META_CD, esp, SIN_BITDEPTH)
        self.assertNotIn(veredicto, VEREDICTOS_BUENOS)
        self.assertTrue(any("artificial cut-off" in p for p in problemas))

    def test_un_techo_cerca_del_nyquist_no_regala_puntos(self):
        """The ceiling at 44.1 kHz no longer scores: it is identical in a master and
        in a 320 kbps file.

        Invariant: an ambiguous cut-off (hugging Nyquist) costs its whole block,
        so with all the rest of the evidence perfect it does NOT reach the maximum
        verdict — but neither is anything accused."""
        cerca = dict(ESP_PERFECTO, techo_hz=20000, techo_rel_nyquist=0.907,
                     clase_corte="near_nyquist_cutoff", corte_detectado=True,
                     frecuencia_corte=19800, distancia_nyquist=0.898,
                     corte_artificial=False)
        sin_corte = dict(cerca, clase_corte="no_cutoff", corte_detectado=False,
                         frecuencia_corte=None, distancia_nyquist=None)

        s_cerca, v_cerca, probs_cerca = motor.calcular_score(META_CD, cerca, SIN_BITDEPTH)
        s_sin, v_sin, _               = motor.calcular_score(META_CD, sin_corte, SIN_BITDEPTH)

        # the ambiguous one cannot reach the maximum verdict...
        self.assertEqual(v_sin, "GENUINE LOSSLESS")
        self.assertNotEqual(v_cerca, "GENUINE LOSSLESS")
        self.assertLess(s_cerca, s_sin)
        # ...and nothing is accused
        texto = " ".join(probs_cerca).lower()
        self.assertNotIn("artificial cut-off", texto)
        self.assertNotIn("codec signature", texto)
        self.assertNotIn("codec signature", texto)
        self.assertTrue(any("inconclusive" in p for p in probs_cerca))

    # ── Block 4: bit depth only penalises ───────────────────────────────────
    def test_la_utilizacion_de_lsb_no_premia(self):
        """'active' bdi must score EXACTLY the same as bdi not applicable."""
        activos = {"aplica": True, "lsbs": "active", "resolucion_efectiva_bits": 24,
                   "fraccion_16bit_grid": 9.8, "conclusion": "..."}
        s_sin, v_sin, _ = motor.calcular_score(META_CD, ESP_PERFECTO, SIN_BITDEPTH)
        s_act, v_act, _ = motor.calcular_score(META_CD, ESP_PERFECTO, activos)
        self.assertEqual(s_sin, s_act)
        self.assertEqual(v_sin, v_act)

    def test_lsb_vacios_penalizan_y_bloquean(self):
        vacios = {"aplica": True, "lsbs": "empty", "resolucion_efectiva_bits": 16,
                  "fraccion_16bit_grid": 100.0, "conclusion": "..."}
        s_act, _, _ = motor.calcular_score(META_CD, ESP_PERFECTO, SIN_BITDEPTH)
        s_vac, v_vac, problemas = motor.calcular_score(META_CD, ESP_PERFECTO, vacios)
        self.assertLess(s_vac, s_act)
        self.assertNotIn(v_vac, VEREDICTOS_BUENOS)
        self.assertTrue(any("LSBs are empty" in p for p in problemas))

    def test_la_resolucion_efectiva_detecta_la_rejilla_de_16_bits(self):
        res = self.analizar("upscale16a24")
        bdi = res["bdi"]
        self.assertEqual(bdi["lsbs"], "empty")
        self.assertEqual(bdi["resolucion_efectiva_bits"], 16)
        self.assertAlmostEqual(bdi["fraccion_16bit_grid"], 100.0, delta=1.0)
        # And it is no longer labelled as genuine
        self.assertNotIn("genuine_24", bdi)
        self.assertNotIn("genuine", bdi["conclusion"].lower())

    def test_el_bit_depth_no_etiqueta_como_genuino_un_transcodigo(self):
        """A lossy file in 24 bits fills the LSB: the test cannot confirm anything."""
        activos = {"aplica": True, "lsbs": "active", "resolucion_efectiva_bits": 24,
                   "fraccion_16bit_grid": 9.8, "conclusion": "..."}
        res = motor.analizar_archivo_datos(self.fx["ok24"], "center", None, False)
        self.assertNotEqual(res["bdi"].get("lsbs"), "empty")
        self.assertNotIn("genuine", res["bdi"]["conclusion"].lower())


class TestHiRes(unittest.TestCase):
    """The high band of a hi-res file is NOT measured in proportion to Nyquist.

    At 96 kHz, 70 % of Nyquist is 33.6 kHz, a frequency where real acoustic music
    has no energy: a 96/24 master with the analogue roll-off of its converters at
    30 kHz has its ceiling at ~26 kHz and is legitimate. The correct reference is
    the CD limit (22.05 kHz).
    """

    @classmethod
    def setUpClass(cls):
        cls.fx = fx.Fixtures()

    @classmethod
    def tearDownClass(cls):
        cls.fx.cerrar()

    def analizar(self, nombre):
        res = motor.analizar_archivo_datos(self.fx[nombre], "center", None, False,
                                           usar_cache=False)
        self.assertNotIn("error", res, res.get("error"))
        return res

    def _cuenta(self, meta, techo, **extra):
        """calcular_score with a synthetic spectrum at the given ceiling."""
        nyquist = meta["sample_rate"] // 2
        esp = dict(ESP_PERFECTO, techo_hz=techo, techo_rel_nyquist=techo / nyquist,
                   **extra)
        return motor.calcular_score(meta, esp, SIN_BITDEPTH)

    def test_un_master_hires_con_rolloff_a_30_khz_no_se_penaliza(self):
        """Real case: Steven Wilson, 'The Raven that Refused to Sing' (96/24)."""
        res = self.analizar("hires96_rolloff30k")
        self.assertGreaterEqual(res["esp"]["techo_hz"], motor.LIMITE_CD_HZ)
        texto = " ".join(res["problemas"]).lower()
        self.assertNotIn("destroyed", texto)
        self.assertNotIn("not backed by", texto)
        self.assertIn(res["veredicto"], VEREDICTOS_BUENOS)

    def test_un_hires_genuino_de_banda_completa_es_genuino(self):
        res = self.analizar("hires96")
        self.assertEqual(res["veredicto"], "GENUINE LOSSLESS")

    def test_un_techo_de_25_khz_en_hires_no_se_penaliza(self):
        """The specific case in the report: 25 kHz with a 48 kHz Nyquist."""
        meta = {"sample_rate": 96000, "bits_per_sample": 24, "canales": 2,
                "duracion": 100.0, "md5": 1}
        score, veredicto, problemas = self._cuenta(meta, 25000)
        texto = " ".join(problemas).lower()
        self.assertNotIn("destroyed", texto)
        self.assertNotIn("not backed by", texto)
        self.assertIn(veredicto, VEREDICTOS_BUENOS)
        self.assertGreater(score, 0.80)

    def test_el_limite_es_el_del_cd_y_no_una_proporcion_del_nyquist(self):
        """Just above the CD limit there is no penalty; just below it, there is.
        With the previous rule (70 % of 48 kHz = 33.6 kHz) both cases were
        penalised."""
        meta = {"sample_rate": 96000, "bits_per_sample": 24, "canales": 2,
                "duracion": 100.0, "md5": 1}
        _, _, encima = self._cuenta(meta, motor.LIMITE_CD_HZ)
        _, _, debajo = self._cuenta(meta, motor.LIMITE_CD_HZ - 1)
        self.assertFalse([p for p in encima if "not backed by" in p])
        self.assertTrue([p for p in debajo if "not backed by" in p])

    def test_un_upscale_de_cd_a_96_khz_sigue_detectandose(self):
        res = self.analizar("upscale96")
        self.assertLess(res["esp"]["techo_hz"], motor.LIMITE_CD_HZ)
        self.assertNotIn(res["veredicto"], VEREDICTOS_BUENOS)
        self.assertTrue(any("not backed by" in p for p in res["problemas"]))

    def test_la_regla_de_cd_no_cambia(self):
        """Below 48 kHz the measurement is still in proportion to Nyquist."""
        meta = {"sample_rate": 44100, "bits_per_sample": 16, "canales": 2,
                "duracion": 100.0, "md5": 1}
        # 0.907 of Nyquist: full band, no penalty
        _, _, alto = self._cuenta(meta, 20000)
        self.assertFalse([p for p in alto if "destroyed" in p])
        # 0.635 of Nyquist: destroyed band
        _, _, bajo = self._cuenta(meta, 14000)
        self.assertTrue([p for p in bajo if "destroyed" in p])

    def test_un_hires_sin_contenido_alto_sigue_penalizado(self):
        """The fix does not open the door to a blatant upscale."""
        res = self.analizar("upscale96")
        self.assertNotIn(res["veredicto"], VEREDICTOS_BUENOS)


@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg is not on the PATH")
class TestMP3Real(unittest.TestCase):
    """Main acceptance criterion: a real 320k MP3 upsampled to 24 bits."""

    @classmethod
    def setUpClass(cls):
        cls.fx = fx.Fixtures()
        cls.ruta = fx.transcodificar_mp3_a_flac(cls.fx.dir, cls.fx["ok16"], 320)

    @classmethod
    def tearDownClass(cls):
        cls.fx.cerrar()

    def test_mp3_320_a_24_bits_no_es_probablemente_lossless(self):
        res = motor.analizar_archivo_datos(self.ruta, "center", None, False)
        self.assertNotIn("error", res, res.get("error"))
        self.assertNotIn(res["veredicto"], VEREDICTOS_BUENOS)
        self.assertLess(res["score"], 0.60)

    def test_la_caida_viene_de_la_resolucion_declarada_sin_respaldo(self):
        res = motor.analizar_archivo_datos(self.ruta, "center", None, False)
        self.assertEqual(res["meta"]["bits_per_sample"], 24)
        self.assertTrue(any("not backed by the content" in p
                            for p in res["problemas"]))

    def test_mp3_128_tampoco_pasa(self):
        ruta = fx.transcodificar_mp3_a_flac(self.fx.dir, self.fx["ok16"], 128)
        res = motor.analizar_archivo_datos(ruta, "center", None, False)
        self.assertNotIn(res["veredicto"], VEREDICTOS_BUENOS)
        self.assertLessEqual(res["esp"]["distancia_nyquist"], 0.88)


if __name__ == "__main__":
    unittest.main(verbosity=2)
