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

import motor_flac as engine

GOOD_VERDICTS = ("GENUINE LOSSLESS", "PROBABLY LOSSLESS")

ESP_PERFECTO = {"error": None, "techo_hz": 22050, "techo_rel_nyquist": 1.0,
                "clase_corte": "no_cutoff", "corte_detectado": False,
                "frecuencia_corte": None, "distancia_nyquist": None,
                "corte_artificial": False, "ratio": 1.0, "ratio_aplica": True,
                "var_alta_db": 50.0, "separacion_stereo": 1.0, "nyquist": 22050,
                "espectrograma": None}
CD_META = {"sample_rate": 44100, "bits_per_sample": 16, "canales": 2,
           "duracion": 10.0, "md5": 1}
NO_BITDEPTH = {"aplica": False}


class test_verdict(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.fx = fx.Fixtures()

    @classmethod
    def tearDownClass(cls):
        cls.fx.close()

    def analyze(self, name):
        res = engine.analyze_file_data(self.fx[name], "center", None, False)
        self.assertNotIn("error", res, res.get("error"))
        return res

    # ── Block 2: silence guard ──────────────────────────────────────────────
    def test_silence_digital_no_receives_verdict(self):
        res = engine.analyze_file_data(self.fx["silence"], "center", None, False)
        self.assertIn("error", res)
        self.assertIn("silence", res["error"].lower())
        self.assertNotIn("veredicto", res)
        self.assertNotIn("score", res)

    # ── Block 5: CD master with a 20 kHz LPF ────────────────────────────────
    def test_master_cd_with_lpf_no_is_accused_transcoding(self):
        res = self.analyze("master_cd_20k")
        esp = res["esp"]
        # The step is detected as a fact...
        self.assertTrue(esp["corte_detectado"])
        # ...but it is at less than 12% of Nyquist: the anti-alias filter zone.
        self.assertGreaterEqual(esp["distancia_nyquist"], 0.88)
        self.assertEqual(esp["clase_corte"], "near_nyquist_cutoff")
        self.assertFalse(esp["corte_artificial"])          # it is no longer an accusation
        # No message may accuse it of transcoding.
        text = " ".join(res["problemas"]).lower()
        for acusacion in ("codec signature", "codec signature", "transcode",
                          "lossy", "artificial cut-off"):
            self.assertNotIn(acusacion, text, f"unwarranted accusation: {acusacion}")
        self.assertNotIn(res["veredicto"], ("PROBABLE UPSCALE",))

    def test_cutoff_very_by_below_nyquist_if_accuses(self):
        res = self.analyze("corte_16k")
        esp = res["esp"]
        self.assertLess(esp["distancia_nyquist"], 0.88)
        self.assertEqual(esp["clase_corte"], "far_cutoff")
        self.assertTrue(esp["corte_artificial"])
        self.assertTrue(any("artificial cut-off" in p for p in res["problemas"]))
        self.assertNotIn(res["veredicto"], GOOD_VERDICTS)

    def test_frequency_cutoff_uses_rounding_real(self):
        """It used to be printed with integer division: 19 821 Hz -> '19.0 kHz'."""
        res = self.analyze("corte_16k")
        fc = res["esp"]["frecuencia_corte"]
        expected = f"{fc/1000:.1f} kHz"
        self.assertTrue(any(expected in p for p in res["problemas"]),
                        f"no problem mentions {expected}")

    def test_cutoff_distant_blocks_verdict_max(self):
        """With all the rest of the evidence perfect, a confirmed defect cannot
        coexist with 'GENUINE LOSSLESS'."""
        esp = dict(ESP_PERFECTO, clase_corte="far_cutoff", corte_detectado=True,
                   frecuencia_corte=16000, distancia_nyquist=0.73,
                   corte_artificial=True)
        score, verdict, problems = engine.calculate_score(CD_META, esp, NO_BITDEPTH)
        self.assertNotIn(verdict, GOOD_VERDICTS)
        self.assertTrue(any("artificial cut-off" in p for p in problems))

    def test_ceiling_near_nyquist_no_gives_away_points(self):
        """The ceiling at 44.1 kHz no longer scores: it is identical in a master and
        in a 320 kbps file.

        Invariant: an ambiguous cut-off (hugging Nyquist) costs its whole block,
        so with all the rest of the evidence perfect it does NOT reach the maximum
        verdict — but neither is anything accused."""
        near = dict(ESP_PERFECTO, techo_hz=20000, techo_rel_nyquist=0.907,
                     clase_corte="near_nyquist_cutoff", corte_detectado=True,
                     frecuencia_corte=19800, distancia_nyquist=0.898,
                     corte_artificial=False)
        without_cutoff = dict(near, clase_corte="no_cutoff", corte_detectado=False,
                         frecuencia_corte=None, distancia_nyquist=None)

        s_near, v_near, probs_near = engine.calculate_score(CD_META, near, NO_BITDEPTH)
        s_without, v_without, _               = engine.calculate_score(CD_META, without_cutoff, NO_BITDEPTH)

        # the ambiguous one cannot reach the maximum verdict...
        self.assertEqual(v_without, "GENUINE LOSSLESS")
        self.assertNotEqual(v_near, "GENUINE LOSSLESS")
        self.assertLess(s_near, s_without)
        # ...and nothing is accused
        text = " ".join(probs_near).lower()
        self.assertNotIn("artificial cut-off", text)
        self.assertNotIn("codec signature", text)
        self.assertNotIn("codec signature", text)
        self.assertTrue(any("inconclusive" in p for p in probs_near))

    # ── Block 4: bit depth only penalises ───────────────────────────────────
    def test_lsb_usage_does_not_reward(self):
        """'active' bdi must score EXACTLY the same as bdi not applicable."""
        active = {"aplica": True, "lsbs": "active", "resolucion_efectiva_bits": 24,
                   "fraccion_16bit_grid": 9.8, "conclusion": "..."}
        s_without, v_without, _ = engine.calculate_score(CD_META, ESP_PERFECTO, NO_BITDEPTH)
        s_act, v_act, _ = engine.calculate_score(CD_META, ESP_PERFECTO, active)
        self.assertEqual(s_without, s_act)
        self.assertEqual(v_without, v_act)

    def test_empty_lsb_penalize_and_block(self):
        empty_lsb = {"aplica": True, "lsbs": "empty", "resolucion_efectiva_bits": 16,
                  "fraccion_16bit_grid": 100.0, "conclusion": "..."}
        s_act, _, _ = engine.calculate_score(CD_META, ESP_PERFECTO, NO_BITDEPTH)
        s_vac, v_vac, problems = engine.calculate_score(CD_META, ESP_PERFECTO, empty_lsb)
        self.assertLess(s_vac, s_act)
        self.assertNotIn(v_vac, GOOD_VERDICTS)
        self.assertTrue(any("LSBs are empty" in p for p in problems))

    def test_resolution_effective_detects_grid_16_bits(self):
        res = self.analyze("upscale16a24")
        bdi = res["bdi"]
        self.assertEqual(bdi["lsbs"], "empty")
        self.assertEqual(bdi["resolucion_efectiva_bits"], 16)
        self.assertAlmostEqual(bdi["fraccion_16bit_grid"], 100.0, delta=1.0)
        # And it is no longer labelled as genuine
        self.assertNotIn("genuine_24", bdi)
        self.assertNotIn("genuine", bdi["conclusion"].lower())

    def test_bit_depth_no_label_as_genuine_transcode(self):
        """A lossy file in 24 bits fills the LSB: the test cannot confirm anything."""
        active = {"aplica": True, "lsbs": "active", "resolucion_efectiva_bits": 24,
                   "fraccion_16bit_grid": 9.8, "conclusion": "..."}
        res = engine.analyze_file_data(self.fx["ok24"], "center", None, False)
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
        cls.fx.close()

    def analyze(self, name):
        res = engine.analyze_file_data(self.fx[name], "center", None, False,
                                           use_cache=False)
        self.assertNotIn("error", res, res.get("error"))
        return res

    def _counts(self, meta, ceiling, **extra):
        """calcular_score with a synthetic spectrum at the given ceiling."""
        nyquist = meta["sample_rate"] // 2
        esp = dict(ESP_PERFECTO, techo_hz=ceiling, techo_rel_nyquist=ceiling / nyquist,
                   **extra)
        return engine.calculate_score(meta, esp, NO_BITDEPTH)

    def test_master_hires_with_rolloff_30_khz_no_penalizes(self):
        """Real case: Steven Wilson, 'The Raven that Refused to Sing' (96/24)."""
        res = self.analyze("hires96_rolloff30k")
        self.assertGreaterEqual(res["esp"]["techo_hz"], engine.LIMIT_CD_HZ)
        text = " ".join(res["problemas"]).lower()
        self.assertNotIn("destroyed", text)
        self.assertNotIn("not backed by", text)
        self.assertIn(res["veredicto"], GOOD_VERDICTS)

    def test_hires_genuine_band_full_is_genuine(self):
        res = self.analyze("hires96")
        self.assertEqual(res["veredicto"], "GENUINE LOSSLESS")

    def test_ceiling_25_khz_hires_no_penalizes(self):
        """The specific case in the report: 25 kHz with a 48 kHz Nyquist."""
        meta = {"sample_rate": 96000, "bits_per_sample": 24, "canales": 2,
                "duracion": 100.0, "md5": 1}
        score, verdict, problems = self._counts(meta, 25000)
        text = " ".join(problems).lower()
        self.assertNotIn("destroyed", text)
        self.assertNotIn("not backed by", text)
        self.assertIn(verdict, GOOD_VERDICTS)
        self.assertGreater(score, 0.80)

    def test_limit_is_cd_no_proportion_nyquist(self):
        """Just above the CD limit there is no penalty; just below it, there is.
        With the previous rule (70 % of 48 kHz = 33.6 kHz) both cases were
        penalised."""
        meta = {"sample_rate": 96000, "bits_per_sample": 24, "canales": 2,
                "duracion": 100.0, "md5": 1}
        _, _, encima = self._counts(meta, engine.LIMIT_CD_HZ)
        _, _, below = self._counts(meta, engine.LIMIT_CD_HZ - 1)
        self.assertFalse([p for p in encima if "not backed by" in p])
        self.assertTrue([p for p in below if "not backed by" in p])

    def test_upscale_cd_96_khz_still_being_detected(self):
        res = self.analyze("upscale96")
        self.assertLess(res["esp"]["techo_hz"], engine.LIMIT_CD_HZ)
        self.assertNotIn(res["veredicto"], GOOD_VERDICTS)
        self.assertTrue(any("not backed by" in p for p in res["problemas"]))

    def test_rule_cd_no_changes(self):
        """Below 48 kHz the measurement is still in proportion to Nyquist."""
        meta = {"sample_rate": 44100, "bits_per_sample": 16, "canales": 2,
                "duracion": 100.0, "md5": 1}
        # 0.907 of Nyquist: full band, no penalty
        _, _, height = self._counts(meta, 20000)
        self.assertFalse([p for p in height if "destroyed" in p])
        # 0.635 of Nyquist: destroyed band
        _, _, low = self._counts(meta, 14000)
        self.assertTrue([p for p in low if "destroyed" in p])

    def test_hires_without_content_high_still_penalized(self):
        """The fix does not open the door to a blatant upscale."""
        res = self.analyze("upscale96")
        self.assertNotIn(res["veredicto"], GOOD_VERDICTS)


@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg is not on the PATH")
class TestMP3Real(unittest.TestCase):
    """Main acceptance criterion: a real 320k MP3 upsampled to 24 bits."""

    @classmethod
    def setUpClass(cls):
        cls.fx = fx.Fixtures()
        cls.path = fx.transcode_mp3_to_flac(cls.fx.dir, cls.fx["ok16"], 320)

    @classmethod
    def tearDownClass(cls):
        cls.fx.close()

    def test_mp3_320_24_bits_no_is_probably_lossless(self):
        res = engine.analyze_file_data(self.path, "center", None, False)
        self.assertNotIn("error", res, res.get("error"))
        self.assertNotIn(res["veredicto"], GOOD_VERDICTS)
        self.assertLess(res["score"], 0.60)

    def test_fall_comes_resolution_declared_without_backup(self):
        res = engine.analyze_file_data(self.path, "center", None, False)
        self.assertEqual(res["meta"]["bits_per_sample"], 24)
        self.assertTrue(any("not backed by the content" in p
                            for p in res["problemas"]))

    def test_mp3_128_neither_passes(self):
        path = fx.transcode_mp3_to_flac(self.fx.dir, self.fx["ok16"], 128)
        res = engine.analyze_file_data(path, "center", None, False)
        self.assertNotIn(res["veredicto"], GOOD_VERDICTS)
        self.assertLessEqual(res["esp"]["distancia_nyquist"], 0.88)


if __name__ == "__main__":
    unittest.main(verbosity=2)
