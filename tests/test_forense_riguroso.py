"""Deterministic forensic regression suite (Phase 5).

Ten synthetic cases with fixed seeds. They do not try to imitate a specific
commercial record: they reproduce the physical signatures the engine must tell
apart (analogue roll-off, sinc/aliasing upscales, joint-stereo and Opus
transcodes, 24-bit fakes, digital silence and a long quiet intro).
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy import signal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import motor_flac

VEREDICTOS_BUENOS = ("GENUINE LOSSLESS", "PROBABLY LOSSLESS")
VEREDICTOS_MALOS = ("SUSPICIOUS", "PROBABLE UPSCALE", "TRANSCODE")


def _crear_pista_referencia(sr: int, duracion: float = 8.0, seed: int = 2026) -> np.ndarray:
    """Stereo reference with decorrelated pink noise, harmonics and transients."""
    rng = np.random.default_rng(seed)
    n = int(sr * duracion)
    t = np.linspace(0.0, duracion, n, endpoint=False)

    b, a = ([0.049922, -0.095993, 0.050612, -0.004408],
            [1.0, -2.494956, 2.017265, -0.522189])
    rosa_l = signal.lfilter(b, a, rng.standard_normal(n))
    rosa_r = signal.lfilter(b, a, rng.standard_normal(n))
    env_percusiva = 0.12 + 0.88 * (np.sin(2.0 * np.pi * 2.5 * t) ** 10)
    base_tonal_l = 0.25 * np.sin(2.0 * np.pi * 110.0 * t) + 0.15 * np.sin(2.0 * np.pi * 440.0 * t)
    base_tonal_r = 0.25 * np.sin(2.0 * np.pi * 110.0 * t) + 0.15 * np.cos(2.0 * np.pi * 440.0 * t)

    est = np.column_stack([
        base_tonal_l + rosa_l * env_percusiva * 0.40,
        base_tonal_r + rosa_r * env_percusiva * 0.40,
    ])
    est /= np.max(np.abs(est)) * 1.12
    return est


def _dsd_like(sr: int, duracion: float, seed: int) -> np.ndarray:
    """Music below 20 kHz plus an independent rising noise floor in 24-40 kHz."""
    rng = np.random.default_rng(seed)
    n = int(sr * duracion)
    rng_freq = np.fft.rfftfreq(n, 1.0 / sr)
    musica = (rng.standard_normal((n // 2 + 1, 2))
              + 1j * rng.standard_normal((n // 2 + 1, 2)))
    musica[rng_freq > 20000.0] = 0.0
    musica = np.fft.irfft(musica, n, axis=0)
    musica /= np.max(np.abs(musica)) * 4.0

    ruido = (rng.standard_normal((n // 2 + 1, 2))
             + 1j * rng.standard_normal((n // 2 + 1, 2)))
    rampa = np.clip((rng_freq - 24000.0) / 16000.0, 0.0, 1.0)
    ruido *= (((rng_freq >= 24000.0) & (rng_freq <= 40000.0))[:, None]
              * (1.0 + 9.0 * rampa)[:, None])
    ruido = np.fft.irfft(ruido, n, axis=0)
    ruido /= np.max(np.abs(ruido)) * 3.0

    mezcla = musica + ruido
    return mezcla / (np.max(np.abs(mezcla)) * 1.05)


class TestForenseRiguroso(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base_path = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_01_hires_analog_rolloff_no_es_falso_positivo(self):
        """24/96 analogue master: smooth 18 kHz roll-off plus -104 dBFS dither."""
        sr = 96000
        rng = np.random.default_rng(2026)
        sig = _crear_pista_referencia(sr, duracion=8.0, seed=101)
        sos_analog = signal.butter(2, 18000.0, btype="low", fs=sr, output="sos")
        sig_suave = signal.sosfilt(sos_analog, sig, axis=0)
        piso_termico = rng.normal(0.0, 10 ** (-104.0 / 20.0), size=sig.shape)
        ruta = self.base_path / "queen_analog_master_24_96.flac"
        sf.write(ruta, np.clip(sig_suave + piso_termico, -0.98, 0.98), sr,
                 subtype="PCM_24")

        res = motor_flac.analizar_archivo_datos(str(ruta), usar_cache=False)
        self.assertIn(res["veredicto"], VEREDICTOS_BUENOS)

    def test_02_upscale_brickwall_44_a_96_es_detectado(self):
        """CD 16/44.1 -> 24/96 with a sinc resampler: brickwall at 22.05 kHz."""
        sig_44 = _crear_pista_referencia(44100, duracion=8.0, seed=102)
        sig_96 = signal.resample_poly(sig_44, up=320, down=147, axis=0)
        ruta = self.base_path / "upscale_sinc_24_96.flac"
        sf.write(ruta, np.clip(sig_96, -0.98, 0.98), 96000, subtype="PCM_24")

        res = motor_flac.analizar_archivo_datos(str(ruta), usar_cache=False)
        self.assertIn(res["veredicto"], ("PROBABLE UPSCALE", "SUSPICIOUS"))

    def test_03_upscale_aliasing_espejo_sin_filtro_es_detectado(self):
        """44.1 -> 88.2 linear interpolation: the image mirrors around 22.05 kHz."""
        sig_44 = _crear_pista_referencia(44100, duracion=8.0, seed=103)
        n = sig_44.shape[0]
        sig_88 = np.empty((n * 2, 2), dtype=np.float64)
        sig_88[0::2] = sig_44
        sig_88[1:-1:2] = 0.5 * (sig_44[:-1] + sig_44[1:])
        sig_88[-1] = sig_44[-1]

        ruta = self.base_path / "upscale_linear_mirror_88k.flac"
        sf.write(ruta, np.clip(sig_88, -0.98, 0.98), 88200, subtype="PCM_24")

        res = motor_flac.analizar_archivo_datos(str(ruta), usar_cache=False)
        self.assertNotEqual(res["veredicto"], "GENUINE LOSSLESS")

    def test_04_falso_24bit_con_padding_de_16bit(self):
        """16-bit samples stored in a 24-bit container (8 LSBs always zero)."""
        sig = _crear_pista_referencia(44100, duracion=6.0, seed=104)
        sig_q16 = np.round(sig * 32767.0) / 32768.0
        ruta = self.base_path / "padded_16_in_24.flac"
        sf.write(ruta, np.clip(sig_q16, -0.99, 0.99), 44100, subtype="PCM_24")

        res = motor_flac.analizar_archivo_datos(str(ruta), usar_cache=False)
        self.assertNotEqual(res["veredicto"], "GENUINE LOSSLESS")

    def test_05_transcode_joint_stereo_colapso_side(self):
        """Mid reaches 20.1 kHz while Side is cut at 15.8 kHz (MP3 320 joint)."""
        sr = 44100
        sig = _crear_pista_referencia(sr, duracion=8.0, seed=105)
        mid = 0.5 * (sig[:, 0] + sig[:, 1])
        side = 0.5 * (sig[:, 0] - sig[:, 1])

        sos_mid = signal.ellip(10, 0.1, 80.0, 20100.0, btype="low", fs=sr, output="sos")
        sos_side = signal.ellip(10, 0.1, 80.0, 15800.0, btype="low", fs=sr, output="sos")
        mid_f = signal.sosfilt(sos_mid, mid)
        side_f = signal.sosfilt(sos_side, side)

        est_js = np.column_stack([mid_f + side_f, mid_f - side_f])
        ruta = self.base_path / "fake_mp3_320_joint_stereo.flac"
        sf.write(ruta, np.clip(est_js, -0.98, 0.98), sr, subtype="PCM_16")

        res = motor_flac.analizar_archivo_datos(str(ruta), usar_cache=False)
        self.assertIn(res["veredicto"], VEREDICTOS_MALOS)

    def test_06_intro_larga_suave_no_engana_al_motor(self):
        """50 s of quiet low-frequency intro followed by 12 s of full band."""
        sr = 44100
        n_intro = sr * 50
        t_intro = np.linspace(0.0, 50.0, n_intro, endpoint=False)
        intro_grave = 0.2 * np.sin(2.0 * np.pi * 180.0 * t_intro)
        intro_est = np.column_stack([intro_grave, intro_grave * 0.95])

        cuerpo_activo = _crear_pista_referencia(sr, duracion=12.0, seed=106)
        pista_completa = np.vstack([intro_est, cuerpo_activo])

        ruta = self.base_path / "prog_rock_long_intro.flac"
        sf.write(ruta, np.clip(pista_completa, -0.98, 0.98), sr, subtype="PCM_16")

        res = motor_flac.analizar_archivo_datos(str(ruta), usar_cache=False)
        self.assertIn(res["veredicto"], VEREDICTOS_BUENOS)

    def test_07_pista_silencio_digital_no_produce_nan_ni_crash(self):
        """100% zeros: no verdict, no NaN/Inf, no exception."""
        sr = 44100
        ceros = np.zeros((sr * 4, 2), dtype=np.float32)
        ruta = self.base_path / "digital_silence.flac"
        sf.write(ruta, ceros, sr, subtype="PCM_16")

        res = motor_flac.analizar_archivo_datos(str(ruta), usar_cache=False)
        self._assert_finito(res, "resultado")

    def _assert_finito(self, valor, ruta):
        if isinstance(valor, float):
            self.assertFalse(np.isnan(valor), f"{ruta} is NaN")
            self.assertFalse(np.isinf(valor), f"{ruta} is Inf")
        elif isinstance(valor, dict):
            for clave, hijo in valor.items():
                self._assert_finito(hijo, f"{ruta}.{clave}")
        elif isinstance(valor, (list, tuple)):
            for indice, hijo in enumerate(valor):
                self._assert_finito(hijo, f"{ruta}[{indice}]")

    def test_08_transferencia_dsd_detectada_y_no_penalizada(self):
        """DSD64-like 88.2 kHz: music <20 kHz plus an ultrasonic noise ramp."""
        sr = 88200
        sig = _dsd_like(sr, duracion=4.0, seed=108)
        ruta = self.base_path / "dsd_transfer_88k.flac"
        sf.write(ruta, np.clip(sig, -0.98, 0.98), sr, subtype="PCM_24")

        res = motor_flac.analizar_archivo_datos(str(ruta), usar_cache=False)
        self.assertTrue(res["esp"]["es_transferencia_dsd"])
        self.assertIn(res["veredicto"], VEREDICTOS_BUENOS)

    def test_09_transcode_opus_20k_es_detectado(self):
        """libopus fullband 20.00 kHz brickwall with a jittering cut-off."""
        sr = 44100
        sig = _crear_pista_referencia(sr, duracion=6.0, seed=109)
        espectro = np.fft.rfft(sig, axis=0)
        freqs = np.fft.rfftfreq(len(sig), 1.0 / sr)
        espectro[freqs > 20000.0] = 0.0
        opus = np.fft.irfft(espectro, len(sig), axis=0)
        ruta = self.base_path / "opus_fullband_20k.flac"
        sf.write(ruta, np.clip(opus, -0.98, 0.98), sr, subtype="PCM_16")

        res = motor_flac.analizar_archivo_datos(str(ruta), usar_cache=False)
        self.assertTrue(res["esp"]["sospecha_corte_opus_20k"])
        self.assertEqual(res["veredicto"], "SUSPICIOUS")

    def test_10_falso_24bit_escalado_en_peine_es_detectado(self):
        """16-bit grid scaled by a gain into 24 bits: LSB quantisation comb."""
        sig = _crear_pista_referencia(44100, duracion=6.0, seed=110)
        q16 = np.round(sig * 32767.0) / 32768.0
        peine = np.clip(q16 * 0.96605, -1.0, 1.0)
        ruta = self.base_path / "fake_24bit_gain_comb.flac"
        sf.write(ruta, peine, 44100, subtype="PCM_24")

        res = motor_flac.analizar_archivo_datos(str(ruta), usar_cache=False)
        self.assertTrue(res["bdi"]["forense_bits"]["es_24bit_escalado_en_peine"])
        self.assertIn(res["veredicto"], ("SUSPICIOUS", "PROBABLE UPSCALE"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
