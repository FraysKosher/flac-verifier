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


def _opus_fullband(x: np.ndarray, sr: int) -> np.ndarray:
    """Simulates libopus fullband: 20.00 kHz cliff plus frame-varying cut-off.

    The codec does not apply one ideal low-pass to the whole song: its cut-off
    moves between frames. Every sixth 200 ms segment is cut at 19.6 kHz while the
    rest stay at 20.0 kHz, which produces the >550 Hz IQR the detector looks for
    without faking an ideal brickwall.
    """
    n = len(x)
    espectro = np.fft.rfft(x, axis=0)
    freqs = np.fft.rfftfreq(n, 1.0 / sr)
    espectro[freqs > 20000.0] = 0.0
    y = np.fft.irfft(espectro, n, axis=0)

    segmento = max(512, int(sr * 0.2))
    for indice, inicio in enumerate(range(0, n, segmento)):
        fin = min(n, inicio + segmento)
        if fin - inicio < 512:
            continue
        bloque = y[inicio:fin].copy()
        if indice % 6 == 0:
            transformada = np.fft.rfft(bloque, axis=0)
            f_bloque = np.fft.rfftfreq(fin - inicio, 1.0 / sr)
            transformada[f_bloque > 19600.0] = 0.0
            bloque = np.fft.irfft(transformada, fin - inicio, axis=0)
        y[inicio:fin] = bloque
    y /= max(1.0, np.max(np.abs(y))) * 1.1
    return y


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
        """Realistic libopus fullband: 20.00 kHz cliff + frame-varying cut-off."""
        sr = 44100
        sig = _crear_pista_referencia(sr, duracion=6.0, seed=109)
        opus = _opus_fullband(sig, sr)
        ruta = self.base_path / "opus_fullband_20k.flac"
        sf.write(ruta, np.clip(opus, -0.98, 0.98), sr, subtype="PCM_16")

        res = motor_flac.analizar_archivo_datos(str(ruta), usar_cache=False)
        self.assertTrue(res["esp"]["sospecha_corte_opus_20k"])
        self.assertGreater(res["esp"]["iqr_corte_hz"], 550.0)
        self.assertEqual(res["veredicto"], "SUSPICIOUS")

    def test_11_bajo_fuerte_con_filtro_estatico_no_dispara_jitter(self):
        """Strong bass plus a static 20 kHz low-pass must not fake cut-off jitter.

        The instantaneous threshold is referenced to the 4-11 kHz band energy of
        each frame, not to its peak: a loud bass line cannot drag the reference.
        """
        sr = 44100
        rng = np.random.default_rng(2026)
        n = sr * 6
        t = np.arange(n) / float(sr)
        ruido = rng.standard_normal((n, 2)) * 0.25
        bajo = 0.9 * np.sin(2.0 * np.pi * 55.0 * t)[:, None]
        x = ruido + bajo
        x /= max(1.0, np.max(np.abs(x))) * 1.05
        espectro = np.fft.rfft(x, axis=0)
        freqs = np.fft.rfftfreq(n, 1.0 / sr)
        espectro[freqs > 20000.0] = 0.0
        y = np.fft.irfft(espectro, n, axis=0)

        ruta = self.base_path / "strong_bass_static_20k.flac"
        sf.write(ruta, np.clip(y, -0.99, 0.99), sr, subtype="PCM_16")
        res = motor_flac.analizar_archivo_datos(str(ruta), usar_cache=False)

        self.assertLessEqual(res["esp"]["iqr_corte_hz"], 550.0)
        self.assertIn(res["veredicto"], VEREDICTOS_BUENOS)
        self.assertFalse(any("jitter" in p.lower() for p in res["problemas"]))

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


    def test_12_transcode_lossy_con_dither_a_96k_no_es_rolloff_analogico(self):
        """A lossy source cut at 15.8 kHz, re-sampled to 24/96 with flat dither.

        The dither floor is continuous up to Nyquist, but there is a prior abrupt
        cut and no 14-17 kHz roll-off toward the ultrasonic floor. It must not be
        accepted as an analogue master or escape the far-cutoff penalty.
        """
        sr = 96000
        rng = np.random.default_rng(2026)
        sig = _crear_pista_referencia(sr, duracion=6.0, seed=112)
        espectro = np.fft.rfft(sig, axis=0)
        freqs = np.fft.rfftfreq(len(sig), 1.0 / sr)
        espectro[freqs > 15800.0] = 0.0
        lossy = np.fft.irfft(espectro, len(sig), axis=0)
        dither = rng.normal(0.0, 10 ** (-105.0 / 20.0), size=lossy.shape)
        y = lossy + dither

        ruta = self.base_path / "lossy_16k_dither_24_96.flac"
        sf.write(ruta, np.clip(y, -0.98, 0.98), sr, subtype="PCM_24")
        res = motor_flac.analizar_archivo_datos(str(ruta), usar_cache=False)

        self.assertFalse(res["esp"]["rolloff_analogico_genuino"])
        self.assertIn(res["veredicto"], VEREDICTOS_MALOS)


    def test_13_pista_24bit_pocas_muestras_bajas_no_dispara_falso_peine(self):
        """Only 500 samples in the low range: occupancy alone is not a comb.

        With 256 samples the 256-bin histogram is expected to fill only ~160
        bins, which used to look like a 16-bit quantisation comb. Genuine 24-bit
        dither with fewer than 2048 low samples must not trigger the penalty.
        """
        sr = 44100
        rng = np.random.default_rng(2026)
        n = sr * 4
        plano = rng.uniform(0.005, 0.3, size=(n, 2))
        plano *= rng.choice([-1.0, 1.0], size=(n, 2))
        aplanado = plano.reshape(-1)
        indices = rng.choice(aplanado.size, size=500, replace=False)
        bajas = rng.uniform(1e-4, 0.003, size=500)
        bajas *= rng.choice([-1.0, 1.0], size=500)
        aplanado[indices] = bajas
        plano = aplanado.reshape(n, 2)

        # TPDF dither of +/-1 LSB at 24 bits: genuine low bits, not padding.
        lsb = 2.0 ** -23
        dither = (rng.random(plano.shape) - rng.random(plano.shape)) * lsb
        y = np.clip(plano + dither, -0.98, 0.98)

        ruta = self.base_path / "genuine_24bit_few_low_samples.flac"
        sf.write(ruta, y, sr, subtype="PCM_24")
        res = motor_flac.analizar_archivo_datos(str(ruta), usar_cache=False)
        forense = res["bdi"]["forense_bits"]

        self.assertGreater(forense["muestras_pasaje_bajo"], 0)
        self.assertLess(forense["muestras_pasaje_bajo"], 2048)
        self.assertFalse(forense["es_24bit_escalado_en_peine"])
        self.assertFalse(any("quantisation comb" in p.lower()
                             for p in res["problemas"]))


    def test_14_voces_sin_platillos_no_disparan_jitter_artificial(self):
        """Voice/guitar frames without cymbals have no >14 kHz intermittency."""
        sr = 44100
        base = _crear_pista_referencia(sr, duracion=6.0, seed=114)
        vocal = signal.sosfilt(signal.butter(6, 9000.0, btype="low", fs=sr,
                                             output="sos"), base, axis=0)
        t = np.arange(len(vocal)) / float(sr)
        envolvente = 0.35 + 0.65 * (np.sin(2.0 * np.pi * 0.7 * t) ** 2)
        y = vocal * envolvente[:, None]
        y /= max(1.0, np.max(np.abs(y))) * 1.05

        ruta = self.base_path / "voice_without_cymbals.flac"
        sf.write(ruta, np.clip(y, -0.98, 0.98), sr, subtype="PCM_16")
        res = motor_flac.analizar_archivo_datos(str(ruta), usar_cache=False)

        self.assertLessEqual(res["esp"]["iqr_corte_hz"], 550.0)
        self.assertFalse(any("jitter" in p.lower() for p in res["problemas"]))

    def test_15_falso_24bit_hipercomprimido_dr3_sin_muestras_bajas_es_detectado(self):
        """Hyper-compressed 16-bit source scaled into 24 bits, with no quiet samples.

        Every sample stays between -18 and -1 dBFS, so the [-90, -48] dBFS
        histogram that used to carry the whole comb detector is empty. The
        lattice test must still see the 16-bit grid (a step near 247 integers).
        """
        sr = 44100
        sig = _crear_pista_referencia(sr, duracion=6.0, seed=115)
        pico = float(np.max(np.abs(sig)))
        suelo, techo = 10 ** (-18.0 / 20.0), 10 ** (-1.0 / 20.0)
        # Loudness-war squash: keep the waveform shape but fill the whole window.
        comprimido = np.sign(sig) * (suelo + (np.abs(sig) / pico) * (techo - suelo))
        q16 = np.round(comprimido * 32768.0)
        # +0.2 dB of digital gain, straight from a real 16-bit master.
        escalado = np.clip(q16 * 0.96605 / 32768.0, -1.0, 1.0)

        ruta = self.base_path / "fake_24bit_loud_no_quiet_samples.flac"
        sf.write(ruta, escalado, sr, subtype="PCM_24")
        res = motor_flac.analizar_archivo_datos(str(ruta), usar_cache=False)
        forense = res["bdi"]["forense_bits"]

        self.assertEqual(forense["muestras_pasaje_bajo"], 0)
        self.assertTrue(forense["es_24bit_escalado_en_peine"])
        self.assertIn(res["veredicto"], VEREDICTOS_MALOS)

    def test_16_master_estudio_24_48_techo_21k_suave_es_genuino_pero_mp3_320_dither_no(self):
        """A 24/48 master whose anti-alias filter is gentle is not a transcode.

        Both files end up with a ceiling around 21 kHz (88-95 % of Nyquist), which
        the old rule accused blindly. The genuine one keeps a clean 24-bit lattice
        and a slow transition band; the other is a 20 kHz brickwall (order 12) of
        the kind a resampler or a 320 kbps MP3 leaves behind, re-dithered to 24
        bits, and it must stay penalised.
        """
        sr = 48000
        lsb = 2.0 ** -23
        base = _crear_pista_referencia(sr, duracion=6.0, seed=116)
        ruido = np.random.default_rng(7)
        base = base + 0.05 * ruido.standard_normal(base.shape)
        base /= max(1.0, float(np.max(np.abs(base)))) * 1.02

        suave = signal.sosfilt(
            signal.butter(2, 20500.0, btype="low", fs=sr, output="sos"),
            base, axis=0)
        dither = (ruido.random(suave.shape) - ruido.random(suave.shape)) * lsb
        ruta_ok = self.base_path / "master_24_48_soft_aa_filter.flac"
        sf.write(ruta_ok, np.clip(suave + dither, -0.99, 0.99), sr, subtype="PCM_24")
        res_ok = motor_flac.analizar_archivo_datos(str(ruta_ok), usar_cache=False)

        esp_ok = res_ok["esp"]
        self.assertTrue(0.88 <= esp_ok["techo_rel_nyquist"] < 0.95,
                        f"ceiling {esp_ok['techo_hz']} Hz outside the test window")
        self.assertEqual(esp_ok["clase_corte"], "no_cutoff")
        self.assertLess(esp_ok["gradiente_borde_db_khz"], 35.0)
        self.assertFalse(res_ok["bdi"]["forense_bits"]["es_24bit_escalado_en_peine"])
        self.assertEqual(res_ok["veredicto"], "GENUINE LOSSLESS")

        duro = signal.sosfilt(
            signal.butter(12, 20000.0, btype="low", fs=sr, output="sos"),
            base, axis=0)
        dither = (ruido.random(duro.shape) - ruido.random(duro.shape)) * lsb
        ruta_mal = self.base_path / "upstream_brickwall_20k_redither_24.flac"
        sf.write(ruta_mal, np.clip(duro + dither, -0.99, 0.99), sr, subtype="PCM_24")
        res_mal = motor_flac.analizar_archivo_datos(str(ruta_mal), usar_cache=False)

        self.assertNotEqual(res_mal["veredicto"], "GENUINE LOSSLESS")
        self.assertTrue(any("not backed by the content" in p
                            for p in res_mal["problemas"]))

    def test_dop_detection_marker_24bit_176k(self):
        """DoP 1.0 framing: alternating 0x05 / 0xFA bytes in the top 8 bits.

        It is a histogram of the native integers, so ordinary 24-bit audio at the
        same rate (or the same markers at 44.1 kHz) must not be reported as DoP.
        """
        sr = 176400
        n = 16384
        rng = np.random.default_rng(2026)
        marcador = np.where(np.arange(n) % 2 == 0, 0x05, 0xFA).astype(np.int64)
        carga = rng.integers(0, 1 << 16, size=n, dtype=np.int64)
        crudo = (marcador << 16) | carga
        crudo = np.where(crudo >= (1 << 23), crudo - (1 << 24), crudo)
        bloque = (crudo.astype(np.float64) / float(1 << 23)).reshape(-1, 1)

        acumulador = motor_flac.AcumuladorForenseBits(24, sample_rate=sr)
        acumulador.anadir(bloque)
        self.assertTrue(acumulador.resultado()["es_flujo_dop"])

        ordinario = rng.uniform(-0.5, 0.5, size=(n, 1))
        acumulador = motor_flac.AcumuladorForenseBits(24, sample_rate=sr)
        acumulador.anadir(ordinario)
        self.assertFalse(acumulador.resultado()["es_flujo_dop"])

        # Same bytes at a CD sample rate: not a DoP stream, no DoP flag.
        acumulador = motor_flac.AcumuladorForenseBits(24, sample_rate=44100)
        acumulador.anadir(bloque)
        self.assertFalse(acumulador.resultado()["es_flujo_dop"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
