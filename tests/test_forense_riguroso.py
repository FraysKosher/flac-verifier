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

        Every sample stays inside the loud window, so the [-90, -48] dBFS bitmap
        that used to carry the whole comb detector is empty. The unique-level
        lattice must still see the 16-bit grid: 0.96605 is a -0.3 dB digital
        gain, so the comb step is 0.96605 x 256 = 247 integers and not the 256
        that a +0.2 dB gain would leave.

        The real loud-window cases are covered too: a DR3/DR5 master squashed to
        [-10, -1] or [-6, -1] dBFS has no sample in the low range and none in the
        -18 / -12 dBFS slices either, so only the global lattice can rule.
        """
        sr = 44100
        sig = _crear_pista_referencia(sr, duracion=6.0, seed=115)
        pico = float(np.max(np.abs(sig)))

        def _comprimir(suelo_db, techo_db):
            """Loudness-war squash: keep the waveform shape, fill the window."""
            suelo, techo = 10 ** (suelo_db / 20.0), 10 ** (techo_db / 20.0)
            return np.sign(sig) * (suelo + (np.abs(sig) / pico) * (techo - suelo))

        def _falso(comprimido):
            """16-bit master with a -0.3 dB digital gain, written into 24 bits."""
            q16 = np.round(comprimido * 32768.0)
            return np.clip(q16 * 0.96605 / 32768.0, -1.0, 1.0)

        ruta = self.base_path / "fake_24bit_loud_no_quiet_samples.flac"
        sf.write(ruta, _falso(_comprimir(-18.0, -1.0)), sr, subtype="PCM_24")
        res = motor_flac.analizar_archivo_datos(str(ruta), usar_cache=False)
        forense = res["bdi"]["forense_bits"]

        self.assertEqual(forense["muestras_pasaje_bajo"], 0)
        self.assertTrue(forense["es_24bit_escalado_en_peine"])
        self.assertGreater(forense["paso_reticula_mediano"], 200.0)
        self.assertIsNotNone(forense["lsb8_entropia_bits"])
        self.assertIn(res["veredicto"], VEREDICTOS_MALOS)

        for suelo_db in (-10.0, -6.0):
            etiqueta = f"{abs(int(suelo_db))}db"
            ruta = self.base_path / f"fake_24bit_loud_{etiqueta}_no_quiet_samples.flac"
            sf.write(ruta, _falso(_comprimir(suelo_db, -1.0)), sr, subtype="PCM_24")
            res = motor_flac.analizar_archivo_datos(str(ruta), usar_cache=False)
            r = res["bdi"]["forense_bits"]

            self.assertEqual(r["muestras_pasaje_bajo"], 0)
            self.assertTrue(r["es_24bit_escalado_en_peine"])
            self.assertGreater(r["paso_reticula_mediano"], 100.0)
            self.assertIsNotNone(r["lsb8_entropia_bits"])
            self.assertGreater(r["lsb8_cajones_ocupados"], 0)
            # A loud comb caught by the global lattice occupies all 256 bins, so
            # quoting "256/256 bins occupied" next to "quantisation comb" would
            # contradict itself: the lattice step is the evidence in that case.
            texto = " ".join(res["problemas"] + res["evidencias_forenses"])
            self.assertNotIn("256/256", texto)
            self.assertIn("lattice step", " ".join(res["problemas"]))

        # The same -6 dBFS window, but with genuine 24-bit dither: not a comb.
        rng = np.random.default_rng(2026)
        lsb = 2.0 ** -23
        dither = (rng.random(sig.shape) - rng.random(sig.shape)) * lsb
        genuino = np.clip(_comprimir(-6.0, -1.0) + dither, -0.999, 0.999)
        ruta = self.base_path / "genuine_24bit_loud_dither.flac"
        sf.write(ruta, genuino, sr, subtype="PCM_24")
        res = motor_flac.analizar_archivo_datos(str(ruta), usar_cache=False)
        self.assertFalse(res["bdi"]["forense_bits"]["es_24bit_escalado_en_peine"])

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

        def _crudo_stereo(desfase_derecha):
            """Two-channel DoP stream, split in two blocks to test the join."""
            def _canal(marcadores):
                carga = rng.integers(0, 1 << 16, size=n, dtype=np.int64)
                valores = (marcadores << 16) | carga
                return np.where(valores >= (1 << 23), valores - (1 << 24), valores)

            izquierda = _canal(np.where(np.arange(n) % 2 == 0, 0x05, 0xFA))
            derecha = _canal(np.where(np.arange(n) % 2 == desfase_derecha,
                                      0x05, 0xFA))
            crudo = np.column_stack([izquierda, derecha]).astype(np.float64)
            return crudo / float(1 << 23)

        # Both channels in phase: ravel() would interleave 0x05, 0x05, 0xFA, 0xFA
        # and see no pair at all, so every marker pair must be counted per channel.
        estereo = _crudo_stereo(desfase_derecha=0)
        acumulador = motor_flac.AcumuladorForenseBits(24, sample_rate=sr)
        acumulador.anadir(estereo[: n // 2])
        acumulador.anadir(estereo[n // 2 :])
        res = acumulador.resultado()
        self.assertTrue(res["es_flujo_dop"])
        self.assertGreaterEqual(res["dop_ratio"], 0.90)

        # Out of phase: the left channel starts at 0x05 and the right one at 0xFA.
        estereo = _crudo_stereo(desfase_derecha=1)
        acumulador = motor_flac.AcumuladorForenseBits(24, sample_rate=sr)
        acumulador.anadir(estereo[: n // 2])
        acumulador.anadir(estereo[n // 2 :])
        res = acumulador.resultado()
        self.assertTrue(res["es_flujo_dop"])
        self.assertGreaterEqual(res["dop_ratio"], 0.90)

    def test_tono_24bit_sostenido_no_es_peine_y_reticula_ganancias(self):
        """A sustained 24-bit tone is not a comb, but a scaled 16-bit one is.

        Tonal material is the worst case for a unique-level lattice: a
        mathematically pure sine touches only a few hundred integers and leaves
        the lattice test unevaluated. The generator therefore adds a 0.25 Hz,
        +/-1 % tremolo, which is what any sustained note carries and what makes
        the occupied levels dense enough to judge.
        """
        sr = 48000
        duracion = 10.0
        n = int(sr * duracion)
        t = np.arange(n) / float(sr)
        envolvente = 1.0 + 0.01 * np.sin(2.0 * np.pi * 0.25 * t)
        envolvente /= float(np.max(np.abs(envolvente)))
        amplitud = 10 ** (-18.0 / 20.0)
        lsb = 2.0 ** -23

        def _forense(muestras):
            acumulador = motor_flac.AcumuladorForenseBits(24, sample_rate=sr)
            acumulador.anadir(muestras.reshape(-1, 1))
            return acumulador.resultado()

        rng = np.random.default_rng(2027)
        for frecuencia in (440.0, 1000.0, 3000.0):
            tono = amplitud * envolvente * np.sin(2.0 * np.pi * frecuencia * t)

            # A mathematically pure sine (no tremolo) touches only a few hundred
            # integers, so its lattice is sparse: it must never be called a comb.
            tono_puro = amplitud * np.sin(2.0 * np.pi * frecuencia * t)
            dither_puro = (rng.random(n) - rng.random(n)) * lsb
            r_puro = _forense(np.clip(tono_puro + dither_puro, -0.999, 0.999))
            self.assertFalse(r_puro["es_24bit_escalado_en_peine"])

            dither = (rng.random(n) - rng.random(n)) * lsb
            r = _forense(np.clip(tono + dither, -0.999, 0.999))
            self.assertFalse(r["es_24bit_escalado_en_peine"])

            q16 = np.round(tono * 32768.0) / 32768.0
            for ganancia in (0.5, 0.7071, 0.85):
                r = _forense(np.clip(q16 * ganancia, -0.999, 0.999))
                self.assertTrue(r["es_24bit_escalado_en_peine"])
                self.assertGreater(r["paso_reticula_mediano"], 100.0)

    def test_17_subbajo_808_masivo_no_ciega_techo_espectral(self):
        """A dominant 50 Hz line must not hide the ceiling of the rest of the band.

        With the global peak as reference level every other band sits more than
        60 dB below the sub-bass and the -60 dB activity window stops before the
        real content ends, so the file was flagged as a low-ceiling upscale. The
        sub-bass re-anchor keeps the ceiling measurable.
        """
        sr = 44100
        duracion = 6.0
        t = np.arange(int(sr * duracion)) / float(sr)
        pista = _crear_pista_referencia(sr, duracion=duracion, seed=117) * 0.12
        # 0.85 against a 0.12 backing track: the 50 Hz line beats the whole
        # 150-6000 Hz band by more than 20 dB.
        subbajo = 0.85 * np.sin(2.0 * np.pi * 50.0 * t)
        mezcla = pista + subbajo[:, None]
        mezcla = np.clip(mezcla, -0.98, 0.98)

        ruta = self.base_path / "808_subbass_16bit.flac"
        sf.write(ruta, mezcla, sr, subtype="PCM_16")
        res = motor_flac.analizar_archivo_datos(str(ruta), usar_cache=False)

        self.assertGreaterEqual(res["esp"]["techo_hz"], 20000)
        self.assertEqual(res["veredicto"], "GENUINE LOSSLESS")

    def test_17b_bajo_moderado_no_altera_ancla_global(self):
        """Normal bass keeps the global-maximum normalization, bin for bin.

        The re-anchor must only fire on a sub-bass peak that takes over the whole
        spectrum (> 6 dB over the 150-6000 Hz band). `_metricas_espectrales` is
        the shared core of `analizar_espectro` and of the streaming path, so the
        comparison is made on a synthetic spectrum where the expected ceiling can
        be computed exactly.
        """
        sr = 44100
        freqs = np.arange(0.0, sr / 2.0 + 5.0, 5.0)

        def _espectro(picos):
            """Flat 150-6000 Hz programme band, -0.004 dB/Hz above it, + peaks."""
            pot_db = np.full(freqs.shape, -130.0)
            banda = (freqs >= 150.0) & (freqs <= 6000.0)
            pot_db[banda] = 0.0
            agudos = freqs > 6000.0
            pot_db[agudos] = -0.004 * (freqs[agudos] - 6000.0)
            for frecuencia, nivel_db in picos:
                idx = int(np.argmin(np.abs(freqs - frecuencia)))
                pot_db[idx] = max(pot_db[idx], nivel_db)
            return 10.0 ** (pot_db / 10.0)

        def _techo_con_ancla(potencia, ancla_db):
            """Last bin above -60 dB relative to the given reference level."""
            pot_db_n = 10.0 * np.log10(np.maximum(potencia, 1e-28)) - ancla_db
            mask = pot_db_n > -60.0
            idx = len(freqs) - 1 - int(np.argmax(mask[::-1]))
            return round(float(freqs[idx]))

        def _techo_medido(potencia):
            metricas = motor_flac._metricas_espectrales(
                freqs, potencia, potencia, sr, es_stereo=False)
            return metricas["techo_hz"]

        # +3 dB of sub-bass: below the 6 dB takeover margin, no re-anchor.
        potencia = _espectro([(60.0, 3.0)])
        self.assertEqual(_techo_medido(potencia), _techo_con_ancla(potencia, 3.0))

        # Peak already at 1000 Hz: the global maximum is the anchor as well.
        potencia = _espectro([(60.0, -10.0), (1000.0, 0.0)])
        self.assertEqual(_techo_medido(potencia), _techo_con_ancla(potencia, 0.0))

        # Control: +20 dB of sub-bass does re-anchor, so the comparison above is
        # meaningful and the ceiling is reported from the programme band.
        potencia = _espectro([(60.0, 20.0)])
        self.assertEqual(_techo_medido(potencia), _techo_con_ancla(potencia, 0.0))
        self.assertGreater(_techo_medido(potencia), _techo_con_ancla(potencia, 20.0))

    def test_18_dual_mono_digital_no_pierde_voto_estereo(self):
        """L == R: stereo separation cannot vote, and the file stays genuine.

        The separation vote is no longer lost because the metric is not
        informative, which used to cap the score below 1.0. The quiet 19.5/20.75
        kHz shimmer only gives the high band the bin-to-bin structure that the
        high-band variability vote looks for: a purely smooth pink-noise track
        caps at 0.9 for reasons that have nothing to do with dual mono.
        """
        sr = 44100
        duracion = 6.0
        t = np.arange(int(sr * duracion)) / float(sr)
        mono = _crear_pista_referencia(sr, duracion=duracion, seed=118)[:, 0]
        shimmer = 0.02 * (np.sin(2.0 * np.pi * 19500.0 * t)
                          + 0.6 * np.sin(2.0 * np.pi * 20750.0 * t))
        mono = mono + shimmer
        estereo = np.column_stack([mono, mono])

        ruta = self.base_path / "dual_mono_digital_16bit.flac"
        sf.write(ruta, np.clip(estereo, -0.98, 0.98), sr, subtype="PCM_16")
        res = motor_flac.analizar_archivo_datos(str(ruta), usar_cache=False)

        self.assertIs(res["esp"]["es_dual_mono"], True)
        self.assertEqual(res["score"], 1.0)
        self.assertEqual(res["veredicto"], "GENUINE LOSSLESS")

    def test_18b_dual_mono_analogico_espectral_24_20_db_y_control_estereo(self):
        """Analogue dual mono sits 20-32 dB below Mid, not 45: it must be caught.

        Two converters fed with the same mono signal leave a Side channel that is
        far from digitally empty, so the old > 45 dB rule missed it and the track
        could be accused of a joint-stereo collapse. The stereo control keeps the
        detector honest.
        """
        sr = 44100
        duracion = 6.0
        t = np.arange(int(sr * duracion)) / float(sr)
        shimmer = 0.02 * (np.sin(2.0 * np.pi * 19500.0 * t)
                          + 0.6 * np.sin(2.0 * np.pi * 20750.0 * t))
        referencia = _crear_pista_referencia(sr, duracion=duracion, seed=118)
        mono = referencia[:, 0] + shimmer
        rng = np.random.default_rng(2028)
        ruido = rng.normal(0.0, 10 ** (-62.0 / 20.0), size=mono.shape)
        # 0.94 is a ~-30 dB Side/Mid offset: analogue, never digital dual mono.
        analogico = np.column_stack([mono, mono * 0.94 + ruido])

        ruta = self.base_path / "dual_mono_analogue_16bit.flac"
        sf.write(ruta, np.clip(analogico, -0.98, 0.98), sr, subtype="PCM_16")
        res = motor_flac.analizar_archivo_datos(str(ruta), usar_cache=False)

        # Far from the 45 dB digital rule, yet the > 24 / > 20 dB spectral rule
        # catches it and no joint-stereo accusation follows.
        self.assertLess(res["esp"]["caida_side_agudos_db"], 45.0)
        self.assertIs(res["esp"]["sospecha_joint_stereo"], False)
        self.assertIs(res["esp"]["es_dual_mono"], True)
        self.assertEqual(res["score"], 1.0)
        self.assertEqual(res["veredicto"], "GENUINE LOSSLESS")

        control = np.column_stack([referencia[:, 0] + shimmer,
                                   referencia[:, 1] + shimmer])
        ruta = self.base_path / "stereo_control_16bit.flac"
        sf.write(ruta, np.clip(control, -0.98, 0.98), sr, subtype="PCM_16")
        res_est = motor_flac.analizar_archivo_datos(str(ruta), usar_cache=False)
        self.assertIs(res_est["esp"]["es_dual_mono"], False)

    def test_19_dsd_96khz_rampa_directa(self):
        """A DSD transfer stored at 96 kHz is DSD: the profile is universal.

        The DSD detector used to require a sample rate that is a multiple of
        44.1 kHz, so a 96 kHz transfer fell through to the mirror/brickwall
        rules even though its 24-40 kHz noise-shaping ramp is unmistakable.
        """
        sr = 96000
        sig = _dsd_like(sr, duracion=4.0, seed=119)

        ruta = self.base_path / "dsd_transfer_96k.flac"
        sf.write(ruta, np.clip(sig, -0.98, 0.98), sr, subtype="PCM_24")
        res = motor_flac.analizar_archivo_datos(str(ruta), usar_cache=False)

        self.assertTrue(res["esp"]["es_transferencia_dsd"])
        self.assertEqual(res["esp"]["perfil_dsd"], "direct_24_40k")
        self.assertEqual(res["veredicto"], "GENUINE LOSSLESS")

        # The same direct transfer at 192 kHz: same profile, same verdict.
        sr = 192000
        sig = _dsd_like(sr, duracion=4.0, seed=119)

        ruta = self.base_path / "dsd_transfer_192k.flac"
        sf.write(ruta, np.clip(sig, -0.98, 0.98), sr, subtype="PCM_24")
        res = motor_flac.analizar_archivo_datos(str(ruta), usar_cache=False)

        self.assertTrue(res["esp"]["es_transferencia_dsd"])
        self.assertEqual(res["esp"]["perfil_dsd"], "direct_24_40k")
        self.assertEqual(res["veredicto"], "GENUINE LOSSLESS")

        # DSD128 at 192 kHz: the noise-shaping ramp sits one octave higher, so
        # neither Profile A (24-40 kHz) nor Profile B (30 kHz hump) applies.
        ruta = self.base_path / "dsd128_transfer_192k.flac"
        sf.write(ruta, self._dsd128_192k(), sr, subtype="PCM_24")
        res = motor_flac.analizar_archivo_datos(str(ruta), usar_cache=False)

        self.assertTrue(res["esp"]["es_transferencia_dsd"])
        self.assertEqual(res["esp"]["perfil_dsd"], "dsd128_40_80k")
        self.assertEqual(res["veredicto"], "GENUINE LOSSLESS")

    @staticmethod
    def _dsd128_192k(sr=192000, duracion=4.0, seed=119):
        """DSD128 transfer: music below 20 kHz and a shaped floor above it.

        The floor sits about 22 dB below the 18-20 kHz music band (roughly
        -53 dBFS per bin) and climbs another 20 dB between 40 and 86 kHz, which
        is how a DSD128 transfer looks. The ramp is carried past 80 kHz because
        the 1 kHz smoothing of the engine reaches into the band edge; the level
        is high enough that the file is not mistaken for shaped dither on a
        lossy transcode (a cliff of more than 25 dB would be)."""
        n = int(sr * duracion)
        freqs = np.fft.rfftfreq(n, 1.0 / sr)

        musica = _crear_pista_referencia(sr, duracion=duracion, seed=seed)
        espectro = np.fft.rfft(musica, axis=0)
        espectro[freqs > 20000.0] = 0.0
        musica = np.fft.irfft(espectro, n, axis=0)
        musica /= np.max(np.abs(musica)) * 1.05

        nivel_db = np.full(freqs.shape, -400.0)
        nivel_db[(freqs >= 20000.0) & (freqs <= 40000.0)] = 0.0
        rampa = (freqs > 40000.0) & (freqs <= 86000.0)
        nivel_db[rampa] = 20.0 * (freqs[rampa] - 40000.0) / 40000.0

        rng = np.random.default_rng(2026)
        ruido = (rng.standard_normal((freqs.size, 2))
                 + 1j * rng.standard_normal((freqs.size, 2)))
        ruido *= (10.0 ** (nivel_db / 20.0))[:, None]
        ruido = np.fft.irfft(ruido, n, axis=0)
        ruido *= 0.035 / np.max(np.abs(ruido))

        lsb = 2.0 ** -23
        dither = (rng.random((n, 2)) - rng.random((n, 2))) * lsb
        return np.clip(musica + ruido + dither, -0.999, 0.999)

    def test_19b_dsd_scarlet_book_30k_aislado(self):
        """Scarlet Book: the 30 kHz hump survives the SACD protective filter.

        Profile A needs a ramp that rises more than 12 dB from 24 to 40 kHz. The
        30 kHz protective filter of a SACD master cuts that ramp, so the file
        only keeps the noise-shaping hump between 23 and 31 kHz. The fixture is
        built so that Profile A cannot fire (a 1.5 dB total rise and 41 % of
        rising steps), and the reported profile pins which profile fired.
        """
        duracion = 4.0
        sr = 96000
        n = int(sr * duracion)
        freqs = np.fft.rfftfreq(n, 1.0 / sr)
        nivel_db, mezcla = self._scarlet_book(sr, duracion)

        # Profile A is ruled out by construction on this fixture.
        mask_a = (freqs >= 24000.0) & (freqs <= 40000.0)
        y_a = nivel_db[mask_a]
        self.assertLessEqual(float(y_a[-1] - y_a[0]), 12.0)
        self.assertLess(float(np.mean(np.diff(y_a) >= 0.0)), 0.60)
        # Profile B conditions do hold: rise, slope, monotony and post-peak drop.
        mask_b = (freqs >= 23000.0) & (freqs <= 31000.0)
        y_b = nivel_db[mask_b]
        self.assertGreaterEqual(float(np.max(y_b) - y_b[0]), 8.0)
        self.assertGreater(float(np.polyfit(freqs[mask_b], y_b, 1)[0] * 1000.0), 0.8)
        self.assertGreaterEqual(float(np.mean(np.diff(y_b) >= 0.0)), 0.60)
        caida_post_pico = float(np.max(y_b) - nivel_db[(freqs >= 31000.0)
                                                      & (freqs <= 40000.0)][-1])
        self.assertGreaterEqual(caida_post_pico, 6.0)
        self.assertLess(caida_post_pico, 10.0)

        ruta = self.base_path / "dsd_scarlet_book_30k_24_96.flac"
        sf.write(ruta, mezcla, sr, subtype="PCM_24")
        res = motor_flac.analizar_archivo_datos(str(ruta), usar_cache=False)

        self.assertTrue(res["esp"]["es_transferencia_dsd"])
        self.assertEqual(res["esp"]["perfil_dsd"], "scarlet_30k")
        self.assertEqual(res["veredicto"], "GENUINE LOSSLESS")

        # The same SACD master stored at 192 kHz: the profile does not depend on
        # the sample rate.
        sr = 192000
        _, mezcla = self._scarlet_book(sr, duracion)
        ruta = self.base_path / "dsd_scarlet_book_30k_24_192.flac"
        sf.write(ruta, mezcla, sr, subtype="PCM_24")
        res = motor_flac.analizar_archivo_datos(str(ruta), usar_cache=False)

        self.assertTrue(res["esp"]["es_transferencia_dsd"])
        self.assertEqual(res["esp"]["perfil_dsd"], "scarlet_30k")
        self.assertEqual(res["veredicto"], "GENUINE LOSSLESS")

    @staticmethod
    def _scarlet_book(sr, duracion=4.0, seed=121):
        """SACD master: music to 20 kHz, 30 kHz hump and protective filter.

        Returns the designed per-bin envelope in dB and the stereo waveform."""
        n = int(sr * duracion)
        freqs = np.fft.rfftfreq(n, 1.0 / sr)

        musica = _crear_pista_referencia(sr, duracion=duracion, seed=seed)
        espectro = np.fft.rfft(musica, axis=0)
        espectro[freqs > 20000.0] = 0.0                  # SACD passband
        musica = np.fft.irfft(espectro, n, axis=0)
        musica /= np.max(np.abs(musica)) * 1.05

        # Noise-shaping hump: +11 dB from 23 to 30.5 kHz, then the protective
        # filter takes 8 dB away between 30.5 and 40 kHz.
        nivel_db = np.full(freqs.shape, -400.0)
        subida = (freqs >= 23000.0) & (freqs <= 30500.0)
        nivel_db[subida] = 11.0 * (freqs[subida] - 23000.0) / 7500.0
        caida = (freqs > 30500.0) & (freqs <= 40000.0)
        nivel_db[caida] = 11.0 - 8.0 * (freqs[caida] - 30500.0) / 9500.0

        rng = np.random.default_rng(2026)
        ruido = (rng.standard_normal((freqs.size, 2))
                 + 1j * rng.standard_normal((freqs.size, 2)))
        ruido *= (10.0 ** (nivel_db / 20.0))[:, None]
        ruido = np.fft.irfft(ruido, n, axis=0)
        ruido *= 0.03 / np.max(np.abs(ruido))

        lsb = 2.0 ** -23
        dither = (rng.random((n, 2)) - rng.random((n, 2))) * lsb
        return nivel_db, np.clip(musica + ruido + dither, -0.999, 0.999)

    def test_19c_hires_24_96_no_dsd_control(self):
        """A plain studio master is not DSD, and shaped dither is not a ramp.

        Three controls: a 24/96 and a 24/192 studio reference (no profile at
        all), and a lossy 20 kHz brickwall re-dithered with noise-shaped dither
        above 20 kHz (a rising floor, but 32 dB below the music band and after a
        cliff), which must stay accused.
        """
        for sr, nombre in ((96000, "hires_24_96_studio_control"),
                           (192000, "hires_24_192_studio_control")):
            sig = _crear_pista_referencia(sr, duracion=4.0, seed=120)
            ruta = self.base_path / f"{nombre}.flac"
            sf.write(ruta, np.clip(sig, -0.98, 0.98), sr, subtype="PCM_24")
            res = motor_flac.analizar_archivo_datos(str(ruta), usar_cache=False)

            self.assertFalse(res["esp"]["es_transferencia_dsd"])
            self.assertIsNone(res["esp"]["perfil_dsd"])

        # Lossy 20 kHz brickwall + noise-shaped dither: the floor rises above
        # 20 kHz, so the level guard alone would let it through (about -82 dBFS
        # per bin). The 32 dB cliff is what keeps it out of the DSD profiles,
        # and the cut keeps its codec penalty.
        sr = 96000
        duracion = 4.0
        n = int(sr * duracion)
        freqs = np.fft.rfftfreq(n, 1.0 / sr)
        base = _crear_pista_referencia(sr, duracion=duracion, seed=124)
        sos = signal.ellip(12, 0.1, 80.0, 20000.0, btype="low", fs=sr, output="sos")
        cortado = signal.sosfilt(sos, base, axis=0)
        cortado /= np.max(np.abs(cortado)) * 1.05

        nivel_db = np.full(freqs.shape, -400.0)
        alta = (freqs >= 20000.0) & (freqs <= 40000.0)
        nivel_db[alta] = 6.0 * (freqs[alta] - 20000.0) / 20000.0
        rng = np.random.default_rng(2027)
        ruido = (rng.standard_normal((freqs.size, 2))
                 + 1j * rng.standard_normal((freqs.size, 2)))
        ruido *= (10.0 ** (nivel_db / 20.0))[:, None]
        ruido = np.fft.irfft(ruido, n, axis=0)
        ruido *= 0.0012 / np.max(np.abs(ruido))

        ruta = self.base_path / "lossy_20k_shaped_dither_24_96.flac"
        sf.write(ruta, np.clip(cortado + ruido, -0.999, 0.999), sr, subtype="PCM_24")
        res = motor_flac.analizar_archivo_datos(str(ruta), usar_cache=False)

        self.assertFalse(res["esp"]["es_transferencia_dsd"])
        self.assertIsNone(res["esp"]["perfil_dsd"])
        self.assertGreater(res["esp"]["salto_ultrasonico_db"], 25.0)
        self.assertIs(res["esp"]["corte_detectado"], True)
        self.assertIn(res["veredicto"], VEREDICTOS_MALOS)

    def test_20_vinilo_con_3_chasquidos_aislados_no_dispara_gibbs_lossy(self):
        """Three vinyl clicks are not a lossy reconstruction pattern.

        Isolated 1-sample events with a band limit below 20.5 kHz used to be
        enough to accuse the file of Gibbs overshoot. A handful of clicks is what
        a vinyl transfer sounds like, not what a codec leaves behind.
        """
        sr = 44100
        duracion = 6.0
        base = _crear_pista_referencia(sr, duracion=duracion, seed=122)
        sos = signal.butter(6, 19000.0, btype="low", fs=sr, output="sos")
        y = signal.sosfilt(sos, base, axis=0)
        y = y / (np.max(np.abs(y)) * 1.05)
        for indice in (int(sr * 0.5), int(sr * 1.5), int(sr * 2.5)):
            y[indice, 0] = 1.0                      # full-scale pop, one sample

        ruta = self.base_path / "vinyl_3_clicks_19k.flac"
        sf.write(ruta, y, sr, subtype="PCM_16")
        res = motor_flac.analizar_archivo_datos(str(ruta), usar_cache=False)

        self.assertLess(res["esp"]["techo_hz"], 20500)
        self.assertEqual(res["esp"]["clipping_eventos"], 3)
        self.assertFalse(res["esp"]["clipping_reconstruccion_lossy"])

    def test_20b_gibbs_positivo_10_eventos_8_cortos(self):
        """The Gibbs rule needs a pattern: 10 events and 8 short ones.

        Nine events or only seven short ones in ten must not be enough, on the
        classifier and on a real signal alike.
        """
        clasificar = motor_flac._clasificar_rachas

        lossy = clasificar([1] * 8 + [4, 4], None, 20000.0, 22050)
        self.assertIs(lossy["clipping_reconstruccion_lossy"], True)
        self.assertEqual(lossy["clipping_eventos"], 10)

        pocos = clasificar([1] * 9, None, 20000.0, 22050)
        self.assertIs(pocos["clipping_reconstruccion_lossy"], False)
        self.assertEqual(pocos["clipping_eventos"], 9)

        largos = clasificar([1] * 7 + [4, 4, 4], None, 20000.0, 22050)
        self.assertIs(largos["clipping_reconstruccion_lossy"], False)
        self.assertEqual(largos["clipping_fraccion_cortos"], 0.7)

        # A band limit of 20.5 kHz or more is not a lossy reconstruction.
        sin_banda = clasificar([1] * 8 + [4, 4], None, 20500.0, 22050)
        self.assertIs(sin_banda["clipping_reconstruccion_lossy"], False)

        sr = 44100
        duracion = 6.0
        base = _crear_pista_referencia(sr, duracion=duracion, seed=123)
        sos = signal.butter(6, 19000.0, btype="low", fs=sr, output="sos")
        limpio = signal.sosfilt(sos, base, axis=0)
        limpio = limpio / (np.max(np.abs(limpio)) * 1.05)

        def _con_eventos(cantidad, cortos):
            """`cortos` 1-sample pops followed by 4-sample plateaus."""
            y = limpio.copy()
            for k in range(cantidad):
                inicio = int(sr * 0.3) + k * int(sr * 0.35)
                if k < cortos:
                    y[inicio, 0] = 1.0
                else:
                    y[inicio : inicio + 4, 0] = 1.0
            return y

        ruta = self.base_path / "gibbs_10_eventos_8_cortos.flac"
        sf.write(ruta, _con_eventos(10, 8), sr, subtype="PCM_16")
        res = motor_flac.analizar_archivo_datos(str(ruta), usar_cache=False)
        self.assertEqual(res["esp"]["clipping_eventos"], 10)
        self.assertIs(res["esp"]["clipping_reconstruccion_lossy"], True)

        for nombre, eventos, cortos in (("9_eventos", 9, 9),
                                        ("10_eventos_7_cortos", 10, 7)):
            ruta = self.base_path / f"gibbs_{nombre}.flac"
            sf.write(ruta, _con_eventos(eventos, cortos), sr, subtype="PCM_16")
            res = motor_flac.analizar_archivo_datos(str(ruta), usar_cache=False)
            self.assertEqual(res["esp"]["clipping_eventos"], eventos)
            self.assertIs(res["esp"]["clipping_reconstruccion_lossy"], False)


if __name__ == "__main__":
    unittest.main(verbosity=2)
