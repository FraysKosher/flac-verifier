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

GOOD_VERDICTS = ("GENUINE LOSSLESS", "PROBABLY LOSSLESS")
BAD_VERDICTS = ("SUSPICIOUS", "PROBABLE UPSCALE", "TRANSCODE")


def _create_track_reference(sr: int, duracion: float = 8.0, seed: int = 2026) -> np.ndarray:
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

    noise = (rng.standard_normal((n // 2 + 1, 2))
             + 1j * rng.standard_normal((n // 2 + 1, 2)))
    rampa = np.clip((rng_freq - 24000.0) / 16000.0, 0.0, 1.0)
    noise *= (((rng_freq >= 24000.0) & (rng_freq <= 40000.0))[:, None]
              * (1.0 + 9.0 * rampa)[:, None])
    noise = np.fft.irfft(noise, n, axis=0)
    noise /= np.max(np.abs(noise)) * 3.0

    mix = musica + noise
    return mix / (np.max(np.abs(mix)) * 1.05)


def _opus_fullband(x: np.ndarray, sr: int) -> np.ndarray:
    """Simulates libopus fullband: 20.00 kHz cliff plus frame-varying cut-off.

    The codec does not apply one ideal low-pass to the whole song: its cut-off
    moves between frames. Every sixth 200 ms segment is cut at 19.6 kHz while the
    rest stay at 20.0 kHz, which produces the >550 Hz IQR the detector looks for
    without faking an ideal brickwall.
    """
    n = len(x)
    spectrum = np.fft.rfft(x, axis=0)
    freqs = np.fft.rfftfreq(n, 1.0 / sr)
    spectrum[freqs > 20000.0] = 0.0
    y = np.fft.irfft(spectrum, n, axis=0)

    segmento = max(512, int(sr * 0.2))
    for index, start in enumerate(range(0, n, segmento)):
        end = min(n, start + segmento)
        if end - start < 512:
            continue
        block = y[start:end].copy()
        if index % 6 == 0:
            transformada = np.fft.rfft(block, axis=0)
            f_block = np.fft.rfftfreq(end - start, 1.0 / sr)
            transformada[f_block > 19600.0] = 0.0
            block = np.fft.irfft(transformada, end - start, axis=0)
        y[start:end] = block
    y /= max(1.0, np.max(np.abs(y))) * 1.1
    return y


class test_forensic_rigorous(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base_path = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_01_hires_analog_rolloff_no_is_false_positive(self):
        """24/96 analogue master: smooth 18 kHz roll-off plus -104 dBFS dither."""
        sr = 96000
        rng = np.random.default_rng(2026)
        sig = _create_track_reference(sr, duracion=8.0, seed=101)
        sos_analog = signal.butter(2, 18000.0, btype="low", fs=sr, output="sos")
        sig_smooth = signal.sosfilt(sos_analog, sig, axis=0)
        piso_termico = rng.normal(0.0, 10 ** (-104.0 / 20.0), size=sig.shape)
        path = self.base_path / "queen_analog_master_24_96.flac"
        sf.write(path, np.clip(sig_smooth + piso_termico, -0.98, 0.98), sr,
                 subtype="PCM_24")

        res = motor_flac.analyze_file_data(str(path), use_cache=False)
        self.assertIn(res["veredicto"], GOOD_VERDICTS)

    def test_02_upscale_brickwall_44_96_is_detected(self):
        """CD 16/44.1 -> 24/96 with a sinc resampler: brickwall at 22.05 kHz."""
        sig_44 = _create_track_reference(44100, duracion=8.0, seed=102)
        sig_96 = signal.resample_poly(sig_44, up=320, down=147, axis=0)
        path = self.base_path / "upscale_sinc_24_96.flac"
        sf.write(path, np.clip(sig_96, -0.98, 0.98), 96000, subtype="PCM_24")

        res = motor_flac.analyze_file_data(str(path), use_cache=False)
        self.assertIn(res["veredicto"], ("PROBABLE UPSCALE", "SUSPICIOUS"))

    def test_03_upscale_aliasing_mirror_without_filter_is_detected(self):
        """44.1 -> 88.2 linear interpolation: the image mirrors around 22.05 kHz."""
        sig_44 = _create_track_reference(44100, duracion=8.0, seed=103)
        n = sig_44.shape[0]
        sig_88 = np.empty((n * 2, 2), dtype=np.float64)
        sig_88[0::2] = sig_44
        sig_88[1:-1:2] = 0.5 * (sig_44[:-1] + sig_44[1:])
        sig_88[-1] = sig_44[-1]

        path = self.base_path / "upscale_linear_mirror_88k.flac"
        sf.write(path, np.clip(sig_88, -0.98, 0.98), 88200, subtype="PCM_24")

        res = motor_flac.analyze_file_data(str(path), use_cache=False)
        self.assertNotEqual(res["veredicto"], "GENUINE LOSSLESS")

    def test_04_false_24bit_with_padding_16bit(self):
        """16-bit samples stored in a 24-bit container (8 LSBs always zero)."""
        sig = _create_track_reference(44100, duracion=6.0, seed=104)
        sig_q16 = np.round(sig * 32767.0) / 32768.0
        path = self.base_path / "padded_16_in_24.flac"
        sf.write(path, np.clip(sig_q16, -0.99, 0.99), 44100, subtype="PCM_24")

        res = motor_flac.analyze_file_data(str(path), use_cache=False)
        self.assertNotEqual(res["veredicto"], "GENUINE LOSSLESS")

    def test_05_transcode_joint_stereo_colapso_side(self):
        """Mid reaches 20.1 kHz while Side is cut at 15.8 kHz (MP3 320 joint)."""
        sr = 44100
        sig = _create_track_reference(sr, duracion=8.0, seed=105)
        mid = 0.5 * (sig[:, 0] + sig[:, 1])
        side = 0.5 * (sig[:, 0] - sig[:, 1])

        sos_mid = signal.ellip(10, 0.1, 80.0, 20100.0, btype="low", fs=sr, output="sos")
        sos_side = signal.ellip(10, 0.1, 80.0, 15800.0, btype="low", fs=sr, output="sos")
        mid_f = signal.sosfilt(sos_mid, mid)
        side_f = signal.sosfilt(sos_side, side)

        est_js = np.column_stack([mid_f + side_f, mid_f - side_f])
        path = self.base_path / "fake_mp3_320_joint_stereo.flac"
        sf.write(path, np.clip(est_js, -0.98, 0.98), sr, subtype="PCM_16")

        res = motor_flac.analyze_file_data(str(path), use_cache=False)
        self.assertIn(res["veredicto"], BAD_VERDICTS)

    def test_06_intro_long_smooth_no_fools_engine(self):
        """50 s of quiet low-frequency intro followed by 12 s of full band."""
        sr = 44100
        n_intro = sr * 50
        t_intro = np.linspace(0.0, 50.0, n_intro, endpoint=False)
        intro_bass = 0.2 * np.sin(2.0 * np.pi * 180.0 * t_intro)
        intro_est = np.column_stack([intro_bass, intro_bass * 0.95])

        body_active = _create_track_reference(sr, duracion=12.0, seed=106)
        track_full = np.vstack([intro_est, body_active])

        path = self.base_path / "prog_rock_long_intro.flac"
        sf.write(path, np.clip(track_full, -0.98, 0.98), sr, subtype="PCM_16")

        res = motor_flac.analyze_file_data(str(path), use_cache=False)
        self.assertIn(res["veredicto"], GOOD_VERDICTS)

    def test_07_track_silence_digital_no_produces_nan_nor_crash(self):
        """100% zeros: no verdict, no NaN/Inf, no exception."""
        sr = 44100
        ceros = np.zeros((sr * 4, 2), dtype=np.float32)
        path = self.base_path / "digital_silence.flac"
        sf.write(path, ceros, sr, subtype="PCM_16")

        res = motor_flac.analyze_file_data(str(path), use_cache=False)
        self._assert_finito(res, "resultado")

    def _assert_finito(self, value, path):
        if isinstance(value, float):
            self.assertFalse(np.isnan(value), f"{path} is NaN")
            self.assertFalse(np.isinf(value), f"{path} is Inf")
        elif isinstance(value, dict):
            for key, hijo in value.items():
                self._assert_finito(hijo, f"{path}.{key}")
        elif isinstance(value, (list, tuple)):
            for index, hijo in enumerate(value):
                self._assert_finito(hijo, f"{path}[{index}]")

    def test_08_transfer_dsd_detected_no_penalized(self):
        """DSD64-like 88.2 kHz: music <20 kHz plus an ultrasonic noise ramp."""
        sr = 88200
        sig = _dsd_like(sr, duracion=4.0, seed=108)
        path = self.base_path / "dsd_transfer_88k.flac"
        sf.write(path, np.clip(sig, -0.98, 0.98), sr, subtype="PCM_24")

        res = motor_flac.analyze_file_data(str(path), use_cache=False)
        self.assertTrue(res["esp"]["es_transferencia_dsd"])
        self.assertIn(res["veredicto"], GOOD_VERDICTS)

    def test_09_transcode_opus_20k_is_detected(self):
        """Realistic libopus fullband: 20.00 kHz cliff + frame-varying cut-off."""
        sr = 44100
        sig = _create_track_reference(sr, duracion=6.0, seed=109)
        opus = _opus_fullband(sig, sr)
        path = self.base_path / "opus_fullband_20k.flac"
        sf.write(path, np.clip(opus, -0.98, 0.98), sr, subtype="PCM_16")

        res = motor_flac.analyze_file_data(str(path), use_cache=False)
        self.assertTrue(res["esp"]["sospecha_corte_opus_20k"])
        self.assertGreater(res["esp"]["iqr_corte_hz"], 550.0)
        self.assertEqual(res["veredicto"], "SUSPICIOUS")

    def test_11_low_strong_with_filter_static_no_triggers_jitter(self):
        """Strong bass plus a static 20 kHz low-pass must not fake cut-off jitter.

        The instantaneous threshold is referenced to the 4-11 kHz band energy of
        each frame, not to its peak: a loud bass line cannot drag the reference.
        """
        sr = 44100
        rng = np.random.default_rng(2026)
        n = sr * 6
        t = np.arange(n) / float(sr)
        noise = rng.standard_normal((n, 2)) * 0.25
        low = 0.9 * np.sin(2.0 * np.pi * 55.0 * t)[:, None]
        x = noise + low
        x /= max(1.0, np.max(np.abs(x))) * 1.05
        spectrum = np.fft.rfft(x, axis=0)
        freqs = np.fft.rfftfreq(n, 1.0 / sr)
        spectrum[freqs > 20000.0] = 0.0
        y = np.fft.irfft(spectrum, n, axis=0)

        path = self.base_path / "strong_bass_static_20k.flac"
        sf.write(path, np.clip(y, -0.99, 0.99), sr, subtype="PCM_16")
        res = motor_flac.analyze_file_data(str(path), use_cache=False)

        self.assertLessEqual(res["esp"]["iqr_corte_hz"], 550.0)
        self.assertIn(res["veredicto"], GOOD_VERDICTS)
        self.assertFalse(any("jitter" in p.lower() for p in res["problemas"]))

    def test_10_false_24bit_upscaled_comb_is_detected(self):
        """16-bit grid scaled by a gain into 24 bits: LSB quantisation comb."""
        sig = _create_track_reference(44100, duracion=6.0, seed=110)
        q16 = np.round(sig * 32767.0) / 32768.0
        comb = np.clip(q16 * 0.96605, -1.0, 1.0)
        path = self.base_path / "fake_24bit_gain_comb.flac"
        sf.write(path, comb, 44100, subtype="PCM_24")

        res = motor_flac.analyze_file_data(str(path), use_cache=False)
        self.assertTrue(res["bdi"]["forense_bits"]["es_24bit_escalado_en_peine"])
        self.assertIn(res["veredicto"], ("SUSPICIOUS", "PROBABLE UPSCALE"))


    def test_12_transcode_lossy_with_dither_96k_no_is_rolloff_analog(self):
        """A lossy source cut at 15.8 kHz, re-sampled to 24/96 with flat dither.

        The dither floor is continuous up to Nyquist, but there is a prior abrupt
        cut and no 14-17 kHz roll-off toward the ultrasonic floor. It must not be
        accepted as an analogue master or escape the far-cutoff penalty.
        """
        sr = 96000
        rng = np.random.default_rng(2026)
        sig = _create_track_reference(sr, duracion=6.0, seed=112)
        spectrum = np.fft.rfft(sig, axis=0)
        freqs = np.fft.rfftfreq(len(sig), 1.0 / sr)
        spectrum[freqs > 15800.0] = 0.0
        lossy = np.fft.irfft(spectrum, len(sig), axis=0)
        dither = rng.normal(0.0, 10 ** (-105.0 / 20.0), size=lossy.shape)
        y = lossy + dither

        path = self.base_path / "lossy_16k_dither_24_96.flac"
        sf.write(path, np.clip(y, -0.98, 0.98), sr, subtype="PCM_24")
        res = motor_flac.analyze_file_data(str(path), use_cache=False)

        self.assertFalse(res["esp"]["rolloff_analogico_genuino"])
        self.assertIn(res["veredicto"], BAD_VERDICTS)


    def test_13_track_24bit_few_samples_low_no_triggers_false_comb(self):
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
        low_samples = rng.uniform(1e-4, 0.003, size=500)
        low_samples *= rng.choice([-1.0, 1.0], size=500)
        aplanado[indices] = low_samples
        plano = aplanado.reshape(n, 2)

        # TPDF dither of +/-1 LSB at 24 bits: genuine low bits, not padding.
        lsb = 2.0 ** -23
        dither = (rng.random(plano.shape) - rng.random(plano.shape)) * lsb
        y = np.clip(plano + dither, -0.98, 0.98)

        path = self.base_path / "genuine_24bit_few_low_samples.flac"
        sf.write(path, y, sr, subtype="PCM_24")
        res = motor_flac.analyze_file_data(str(path), use_cache=False)
        forensic = res["bdi"]["forense_bits"]

        self.assertGreater(forensic["muestras_pasaje_bajo"], 0)
        self.assertLess(forensic["muestras_pasaje_bajo"], 2048)
        self.assertFalse(forensic["es_24bit_escalado_en_peine"])
        self.assertFalse(any("quantisation comb" in p.lower()
                             for p in res["problemas"]))


    def test_14_voices_without_cymbals_no_trigger_jitter_artificial(self):
        """Voice/guitar frames without cymbals have no >14 kHz intermittency."""
        sr = 44100
        base = _create_track_reference(sr, duracion=6.0, seed=114)
        vocal = signal.sosfilt(signal.butter(6, 9000.0, btype="low", fs=sr,
                                             output="sos"), base, axis=0)
        t = np.arange(len(vocal)) / float(sr)
        envolvente = 0.35 + 0.65 * (np.sin(2.0 * np.pi * 0.7 * t) ** 2)
        y = vocal * envolvente[:, None]
        y /= max(1.0, np.max(np.abs(y))) * 1.05

        path = self.base_path / "voice_without_cymbals.flac"
        sf.write(path, np.clip(y, -0.98, 0.98), sr, subtype="PCM_16")
        res = motor_flac.analyze_file_data(str(path), use_cache=False)

        self.assertLessEqual(res["esp"]["iqr_corte_hz"], 550.0)
        self.assertFalse(any("jitter" in p.lower() for p in res["problemas"]))

    def test_15_false_24bit_hypercompressed_dr3_without_samples_low_is_detected(self):
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
        sig = _create_track_reference(sr, duracion=6.0, seed=115)
        peak = float(np.max(np.abs(sig)))

        def _comprimir(floor_db, ceiling_db):
            """Loudness-war squash: keep the waveform shape, fill the window."""
            floor, ceiling = 10 ** (floor_db / 20.0), 10 ** (ceiling_db / 20.0)
            return np.sign(sig) * (floor + (np.abs(sig) / peak) * (ceiling - floor))

        def _false(comprimido):
            """16-bit master with a -0.3 dB digital gain, written into 24 bits."""
            q16 = np.round(comprimido * 32768.0)
            return np.clip(q16 * 0.96605 / 32768.0, -1.0, 1.0)

        path = self.base_path / "fake_24bit_loud_no_quiet_samples.flac"
        sf.write(path, _false(_comprimir(-18.0, -1.0)), sr, subtype="PCM_24")
        res = motor_flac.analyze_file_data(str(path), use_cache=False)
        forensic = res["bdi"]["forense_bits"]

        self.assertEqual(forensic["muestras_pasaje_bajo"], 0)
        self.assertTrue(forensic["es_24bit_escalado_en_peine"])
        self.assertGreater(forensic["paso_reticula_mediano"], 200.0)
        self.assertIsNotNone(forensic["lsb8_entropia_bits"])
        self.assertIn(res["veredicto"], BAD_VERDICTS)

        for floor_db in (-10.0, -6.0):
            label = f"{abs(int(floor_db))}db"
            path = self.base_path / f"fake_24bit_loud_{label}_no_quiet_samples.flac"
            sf.write(path, _false(_comprimir(floor_db, -1.0)), sr, subtype="PCM_24")
            res = motor_flac.analyze_file_data(str(path), use_cache=False)
            r = res["bdi"]["forense_bits"]

            self.assertEqual(r["muestras_pasaje_bajo"], 0)
            self.assertTrue(r["es_24bit_escalado_en_peine"])
            self.assertGreater(r["paso_reticula_mediano"], 100.0)
            self.assertIsNotNone(r["lsb8_entropia_bits"])
            self.assertGreater(r["lsb8_cajones_ocupados"], 0)
            # A loud comb caught by the global lattice occupies all 256 bins, so
            # quoting "256/256 bins occupied" next to "quantisation comb" would
            # contradict itself: the lattice step is the evidence in that case.
            text = " ".join(res["problemas"] + res["evidencias_forenses"])
            self.assertNotIn("256/256", text)
            self.assertIn("lattice step", " ".join(res["problemas"]))

        # The same -6 dBFS window, but with genuine 24-bit dither: not a comb.
        rng = np.random.default_rng(2026)
        lsb = 2.0 ** -23
        dither = (rng.random(sig.shape) - rng.random(sig.shape)) * lsb
        genuine = np.clip(_comprimir(-6.0, -1.0) + dither, -0.999, 0.999)
        path = self.base_path / "genuine_24bit_loud_dither.flac"
        sf.write(path, genuine, sr, subtype="PCM_24")
        res = motor_flac.analyze_file_data(str(path), use_cache=False)
        self.assertFalse(res["bdi"]["forense_bits"]["es_24bit_escalado_en_peine"])

    def test_16_master_studio_24_48_ceiling_21k_smooth_is_genuine_but_mp3_320_dither_no(self):
        """A 24/48 master whose anti-alias filter is gentle is not a transcode.

        Both files end up with a ceiling around 21 kHz (88-95 % of Nyquist), which
        the old rule accused blindly. The genuine one keeps a clean 24-bit lattice
        and a slow transition band; the other is a 20 kHz brickwall (order 12) of
        the kind a resampler or a 320 kbps MP3 leaves behind, re-dithered to 24
        bits, and it must stay penalised.
        """
        sr = 48000
        lsb = 2.0 ** -23
        base = _create_track_reference(sr, duracion=6.0, seed=116)
        noise = np.random.default_rng(7)
        base = base + 0.05 * noise.standard_normal(base.shape)
        base /= max(1.0, float(np.max(np.abs(base)))) * 1.02

        smooth = signal.sosfilt(
            signal.butter(2, 20500.0, btype="low", fs=sr, output="sos"),
            base, axis=0)
        dither = (noise.random(smooth.shape) - noise.random(smooth.shape)) * lsb
        path_ok = self.base_path / "master_24_48_soft_aa_filter.flac"
        sf.write(path_ok, np.clip(smooth + dither, -0.99, 0.99), sr, subtype="PCM_24")
        res_ok = motor_flac.analyze_file_data(str(path_ok), use_cache=False)

        esp_ok = res_ok["esp"]
        self.assertTrue(0.88 <= esp_ok["techo_rel_nyquist"] < 0.95,
                        f"ceiling {esp_ok['techo_hz']} Hz outside the test window")
        self.assertEqual(esp_ok["clase_corte"], "no_cutoff")
        self.assertLess(esp_ok["gradiente_borde_db_khz"], 35.0)
        self.assertFalse(res_ok["bdi"]["forense_bits"]["es_24bit_escalado_en_peine"])
        self.assertEqual(res_ok["veredicto"], "GENUINE LOSSLESS")

        hard = signal.sosfilt(
            signal.butter(12, 20000.0, btype="low", fs=sr, output="sos"),
            base, axis=0)
        dither = (noise.random(hard.shape) - noise.random(hard.shape)) * lsb
        path_bad = self.base_path / "upstream_brickwall_20k_redither_24.flac"
        sf.write(path_bad, np.clip(hard + dither, -0.99, 0.99), sr, subtype="PCM_24")
        res_bad = motor_flac.analyze_file_data(str(path_bad), use_cache=False)

        self.assertNotEqual(res_bad["veredicto"], "GENUINE LOSSLESS")
        self.assertTrue(any("not backed by the content" in p
                            for p in res_bad["problemas"]))

    def test_dop_detection_marker_24bit_176k(self):
        """DoP 1.0 framing: alternating 0x05 / 0xFA bytes in the top 8 bits.

        It is a histogram of the native integers, so ordinary 24-bit audio at the
        same rate (or the same markers at 44.1 kHz) must not be reported as DoP.
        """
        sr = 176400
        n = 16384
        rng = np.random.default_rng(2026)
        marcador = np.where(np.arange(n) % 2 == 0, 0x05, 0xFA).astype(np.int64)
        load = rng.integers(0, 1 << 16, size=n, dtype=np.int64)
        crudo = (marcador << 16) | load
        crudo = np.where(crudo >= (1 << 23), crudo - (1 << 24), crudo)
        block = (crudo.astype(np.float64) / float(1 << 23)).reshape(-1, 1)

        accumulator = motor_flac.ForensicBitsAccumulator(24, sample_rate=sr)
        accumulator.add(block)
        self.assertTrue(accumulator.result()["es_flujo_dop"])

        ordinario = rng.uniform(-0.5, 0.5, size=(n, 1))
        accumulator = motor_flac.ForensicBitsAccumulator(24, sample_rate=sr)
        accumulator.add(ordinario)
        self.assertFalse(accumulator.result()["es_flujo_dop"])

        # Same bytes at a CD sample rate: not a DoP stream, no DoP flag.
        accumulator = motor_flac.ForensicBitsAccumulator(24, sample_rate=44100)
        accumulator.add(block)
        self.assertFalse(accumulator.result()["es_flujo_dop"])

        def _crudo_stereo(skew_right):
            """Two-channel DoP stream, split in two blocks to test the join."""
            def _channel(marcadores):
                load = rng.integers(0, 1 << 16, size=n, dtype=np.int64)
                values = (marcadores << 16) | load
                return np.where(values >= (1 << 23), values - (1 << 24), values)

            left = _channel(np.where(np.arange(n) % 2 == 0, 0x05, 0xFA))
            right = _channel(np.where(np.arange(n) % 2 == skew_right,
                                      0x05, 0xFA))
            crudo = np.column_stack([left, right]).astype(np.float64)
            return crudo / float(1 << 23)

        # Both channels in phase: ravel() would interleave 0x05, 0x05, 0xFA, 0xFA
        # and see no pair at all, so every marker pair must be counted per channel.
        stereo = _crudo_stereo(skew_right=0)
        accumulator = motor_flac.ForensicBitsAccumulator(24, sample_rate=sr)
        accumulator.add(stereo[: n // 2])
        accumulator.add(stereo[n // 2 :])
        res = accumulator.result()
        self.assertTrue(res["es_flujo_dop"])
        self.assertGreaterEqual(res["dop_ratio"], 0.90)

        # Out of phase: the left channel starts at 0x05 and the right one at 0xFA.
        stereo = _crudo_stereo(skew_right=1)
        accumulator = motor_flac.ForensicBitsAccumulator(24, sample_rate=sr)
        accumulator.add(stereo[: n // 2])
        accumulator.add(stereo[n // 2 :])
        res = accumulator.result()
        self.assertTrue(res["es_flujo_dop"])
        self.assertGreaterEqual(res["dop_ratio"], 0.90)

    def test_tone_24bit_sustained_no_is_comb_grid_gains(self):
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

        def _forensic(samples):
            accumulator = motor_flac.ForensicBitsAccumulator(24, sample_rate=sr)
            accumulator.add(samples.reshape(-1, 1))
            return accumulator.result()

        rng = np.random.default_rng(2027)
        for frequency in (440.0, 1000.0, 3000.0):
            tone = amplitud * envolvente * np.sin(2.0 * np.pi * frequency * t)

            # A mathematically pure sine (no tremolo) touches only a few hundred
            # integers, so its lattice is sparse: it must never be called a comb.
            tone_pure = amplitud * np.sin(2.0 * np.pi * frequency * t)
            dither_pure = (rng.random(n) - rng.random(n)) * lsb
            r_pure = _forensic(np.clip(tone_pure + dither_pure, -0.999, 0.999))
            self.assertFalse(r_pure["es_24bit_escalado_en_peine"])

            dither = (rng.random(n) - rng.random(n)) * lsb
            r = _forensic(np.clip(tone + dither, -0.999, 0.999))
            self.assertFalse(r["es_24bit_escalado_en_peine"])

            q16 = np.round(tone * 32768.0) / 32768.0
            for gain in (0.5, 0.7071, 0.85):
                r = _forensic(np.clip(q16 * gain, -0.999, 0.999))
                self.assertTrue(r["es_24bit_escalado_en_peine"])
                self.assertGreater(r["paso_reticula_mediano"], 100.0)

    def test_17_subbass_808_massive_no_blinds_ceiling_spectral(self):
        """A dominant 50 Hz line must not hide the ceiling of the rest of the band.

        With the global peak as reference level every other band sits more than
        60 dB below the sub-bass and the -60 dB activity window stops before the
        real content ends, so the file was flagged as a low-ceiling upscale. The
        sub-bass re-anchor keeps the ceiling measurable.
        """
        sr = 44100
        duracion = 6.0
        t = np.arange(int(sr * duracion)) / float(sr)
        track = _create_track_reference(sr, duracion=duracion, seed=117) * 0.12
        # 0.85 against a 0.12 backing track: the 50 Hz line beats the whole
        # 150-6000 Hz band by more than 20 dB.
        subbass = 0.85 * np.sin(2.0 * np.pi * 50.0 * t)
        mix = track + subbass[:, None]
        mix = np.clip(mix, -0.98, 0.98)

        path = self.base_path / "808_subbass_16bit.flac"
        sf.write(path, mix, sr, subtype="PCM_16")
        res = motor_flac.analyze_file_data(str(path), use_cache=False)

        self.assertGreaterEqual(res["esp"]["techo_hz"], 20000)
        self.assertEqual(res["veredicto"], "GENUINE LOSSLESS")

    def test_17b_low_moderate_no_changes_anchor_global(self):
        """Normal bass keeps the global-maximum normalization, bin for bin.

        The re-anchor must only fire on a sub-bass peak that takes over the whole
        spectrum (> 6 dB over the 150-6000 Hz band). `_metricas_espectrales` is
        the shared core of `analizar_espectro` and of the streaming path, so the
        comparison is made on a synthetic spectrum where the expected ceiling can
        be computed exactly.
        """
        sr = 44100
        freqs = np.arange(0.0, sr / 2.0 + 5.0, 5.0)

        def _spectrum(picos):
            """Flat 150-6000 Hz programme band, -0.004 dB/Hz above it, + peaks."""
            pot_db = np.full(freqs.shape, -130.0)
            band = (freqs >= 150.0) & (freqs <= 6000.0)
            pot_db[band] = 0.0
            highs = freqs > 6000.0
            pot_db[highs] = -0.004 * (freqs[highs] - 6000.0)
            for frequency, level_db in picos:
                idx = int(np.argmin(np.abs(freqs - frequency)))
                pot_db[idx] = max(pot_db[idx], level_db)
            return 10.0 ** (pot_db / 10.0)

        def _ceiling_with_anchor(power, anchor_db):
            """Last bin above -60 dB relative to the given reference level."""
            pot_db_n = 10.0 * np.log10(np.maximum(power, 1e-28)) - anchor_db
            mask = pot_db_n > -60.0
            idx = len(freqs) - 1 - int(np.argmax(mask[::-1]))
            return round(float(freqs[idx]))

        def _ceiling_measured(power):
            metrics = motor_flac._spectral_metrics(
                freqs, power, power, sr, is_stereo=False)
            return metrics["techo_hz"]

        # +3 dB of sub-bass: below the 6 dB takeover margin, no re-anchor.
        power = _spectrum([(60.0, 3.0)])
        self.assertEqual(_ceiling_measured(power), _ceiling_with_anchor(power, 3.0))

        # Peak already at 1000 Hz: the global maximum is the anchor as well.
        power = _spectrum([(60.0, -10.0), (1000.0, 0.0)])
        self.assertEqual(_ceiling_measured(power), _ceiling_with_anchor(power, 0.0))

        # Control: +20 dB of sub-bass does re-anchor, so the comparison above is
        # meaningful and the ceiling is reported from the programme band.
        power = _spectrum([(60.0, 20.0)])
        self.assertEqual(_ceiling_measured(power), _ceiling_with_anchor(power, 0.0))
        self.assertGreater(_ceiling_measured(power), _ceiling_with_anchor(power, 20.0))

    def test_18_dual_mono_digital_no_loses_vote_stereo(self):
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
        mono = _create_track_reference(sr, duracion=duracion, seed=118)[:, 0]
        shimmer = 0.02 * (np.sin(2.0 * np.pi * 19500.0 * t)
                          + 0.6 * np.sin(2.0 * np.pi * 20750.0 * t))
        mono = mono + shimmer
        stereo = np.column_stack([mono, mono])

        path = self.base_path / "dual_mono_digital_16bit.flac"
        sf.write(path, np.clip(stereo, -0.98, 0.98), sr, subtype="PCM_16")
        res = motor_flac.analyze_file_data(str(path), use_cache=False)

        self.assertIs(res["esp"]["es_dual_mono"], True)
        self.assertEqual(res["score"], 1.0)
        self.assertEqual(res["veredicto"], "GENUINE LOSSLESS")

    def test_18b_dual_mono_analog_spectral_24_20_db_control_stereo(self):
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
        reference = _create_track_reference(sr, duracion=duracion, seed=118)
        mono = reference[:, 0] + shimmer
        rng = np.random.default_rng(2028)
        noise = rng.normal(0.0, 10 ** (-62.0 / 20.0), size=mono.shape)
        # 0.94 is a ~-30 dB Side/Mid offset: analogue, never digital dual mono.
        analog = np.column_stack([mono, mono * 0.94 + noise])

        path = self.base_path / "dual_mono_analogue_16bit.flac"
        sf.write(path, np.clip(analog, -0.98, 0.98), sr, subtype="PCM_16")
        res = motor_flac.analyze_file_data(str(path), use_cache=False)

        # Far from the 45 dB digital rule, yet the > 24 / > 20 dB spectral rule
        # catches it and no joint-stereo accusation follows.
        self.assertLess(res["esp"]["caida_side_agudos_db"], 45.0)
        self.assertIs(res["esp"]["sospecha_joint_stereo"], False)
        self.assertIs(res["esp"]["es_dual_mono"], True)
        self.assertEqual(res["score"], 1.0)
        self.assertEqual(res["veredicto"], "GENUINE LOSSLESS")

        control = np.column_stack([reference[:, 0] + shimmer,
                                   reference[:, 1] + shimmer])
        path = self.base_path / "stereo_control_16bit.flac"
        sf.write(path, np.clip(control, -0.98, 0.98), sr, subtype="PCM_16")
        res_est = motor_flac.analyze_file_data(str(path), use_cache=False)
        self.assertIs(res_est["esp"]["es_dual_mono"], False)

    def test_19_dsd_96khz_rampa_directa(self):
        """A DSD transfer stored at 96 kHz is DSD: the profile is universal.

        The DSD detector used to require a sample rate that is a multiple of
        44.1 kHz, so a 96 kHz transfer fell through to the mirror/brickwall
        rules even though its 24-40 kHz noise-shaping ramp is unmistakable.
        """
        sr = 96000
        sig = _dsd_like(sr, duracion=4.0, seed=119)

        path = self.base_path / "dsd_transfer_96k.flac"
        sf.write(path, np.clip(sig, -0.98, 0.98), sr, subtype="PCM_24")
        res = motor_flac.analyze_file_data(str(path), use_cache=False)

        self.assertTrue(res["esp"]["es_transferencia_dsd"])
        self.assertEqual(res["esp"]["perfil_dsd"], "direct_24_40k")
        self.assertEqual(res["veredicto"], "GENUINE LOSSLESS")

        # The same direct transfer at 192 kHz: same profile, same verdict.
        sr = 192000
        sig = _dsd_like(sr, duracion=4.0, seed=119)

        path = self.base_path / "dsd_transfer_192k.flac"
        sf.write(path, np.clip(sig, -0.98, 0.98), sr, subtype="PCM_24")
        res = motor_flac.analyze_file_data(str(path), use_cache=False)

        self.assertTrue(res["esp"]["es_transferencia_dsd"])
        self.assertEqual(res["esp"]["perfil_dsd"], "direct_24_40k")
        self.assertEqual(res["veredicto"], "GENUINE LOSSLESS")

        # DSD128 at 192 kHz: the noise-shaping ramp sits one octave higher, so
        # neither Profile A (24-40 kHz) nor Profile B (30 kHz hump) applies.
        path = self.base_path / "dsd128_transfer_192k.flac"
        sf.write(path, self._dsd128_192k(), sr, subtype="PCM_24")
        res = motor_flac.analyze_file_data(str(path), use_cache=False)

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

        musica = _create_track_reference(sr, duracion=duracion, seed=seed)
        spectrum = np.fft.rfft(musica, axis=0)
        spectrum[freqs > 20000.0] = 0.0
        musica = np.fft.irfft(spectrum, n, axis=0)
        musica /= np.max(np.abs(musica)) * 1.05

        level_db = np.full(freqs.shape, -400.0)
        level_db[(freqs >= 20000.0) & (freqs <= 40000.0)] = 0.0
        rampa = (freqs > 40000.0) & (freqs <= 86000.0)
        level_db[rampa] = 20.0 * (freqs[rampa] - 40000.0) / 40000.0

        rng = np.random.default_rng(2026)
        noise = (rng.standard_normal((freqs.size, 2))
                 + 1j * rng.standard_normal((freqs.size, 2)))
        noise *= (10.0 ** (level_db / 20.0))[:, None]
        noise = np.fft.irfft(noise, n, axis=0)
        noise *= 0.035 / np.max(np.abs(noise))

        lsb = 2.0 ** -23
        dither = (rng.random((n, 2)) - rng.random((n, 2))) * lsb
        return np.clip(musica + noise + dither, -0.999, 0.999)

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
        level_db, mix = self._scarlet_book(sr, duracion)

        # Profile A is ruled out by construction on this fixture.
        mask_a = (freqs >= 24000.0) & (freqs <= 40000.0)
        y_a = level_db[mask_a]
        self.assertLessEqual(float(y_a[-1] - y_a[0]), 12.0)
        self.assertLess(float(np.mean(np.diff(y_a) >= 0.0)), 0.60)
        # Profile B conditions do hold: rise, slope, monotony and post-peak drop.
        mask_b = (freqs >= 23000.0) & (freqs <= 31000.0)
        y_b = level_db[mask_b]
        self.assertGreaterEqual(float(np.max(y_b) - y_b[0]), 8.0)
        self.assertGreater(float(np.polyfit(freqs[mask_b], y_b, 1)[0] * 1000.0), 0.8)
        self.assertGreaterEqual(float(np.mean(np.diff(y_b) >= 0.0)), 0.60)
        fall_post_peak = float(np.max(y_b) - level_db[(freqs >= 31000.0)
                                                      & (freqs <= 40000.0)][-1])
        self.assertGreaterEqual(fall_post_peak, 6.0)
        self.assertLess(fall_post_peak, 10.0)

        path = self.base_path / "dsd_scarlet_book_30k_24_96.flac"
        sf.write(path, mix, sr, subtype="PCM_24")
        res = motor_flac.analyze_file_data(str(path), use_cache=False)

        self.assertTrue(res["esp"]["es_transferencia_dsd"])
        self.assertEqual(res["esp"]["perfil_dsd"], "scarlet_30k")
        self.assertEqual(res["veredicto"], "GENUINE LOSSLESS")

        # The same SACD master stored at 192 kHz: the profile does not depend on
        # the sample rate.
        sr = 192000
        _, mix = self._scarlet_book(sr, duracion)
        path = self.base_path / "dsd_scarlet_book_30k_24_192.flac"
        sf.write(path, mix, sr, subtype="PCM_24")
        res = motor_flac.analyze_file_data(str(path), use_cache=False)

        self.assertTrue(res["esp"]["es_transferencia_dsd"])
        self.assertEqual(res["esp"]["perfil_dsd"], "scarlet_30k")
        self.assertEqual(res["veredicto"], "GENUINE LOSSLESS")

    @staticmethod
    def _scarlet_book(sr, duracion=4.0, seed=121):
        """SACD master: music to 20 kHz, 30 kHz hump and protective filter.

        Returns the designed per-bin envelope in dB and the stereo waveform."""
        n = int(sr * duracion)
        freqs = np.fft.rfftfreq(n, 1.0 / sr)

        musica = _create_track_reference(sr, duracion=duracion, seed=seed)
        spectrum = np.fft.rfft(musica, axis=0)
        spectrum[freqs > 20000.0] = 0.0                  # SACD passband
        musica = np.fft.irfft(spectrum, n, axis=0)
        musica /= np.max(np.abs(musica)) * 1.05

        # Noise-shaping hump: +11 dB from 23 to 30.5 kHz, then the protective
        # filter takes 8 dB away between 30.5 and 40 kHz.
        level_db = np.full(freqs.shape, -400.0)
        subida = (freqs >= 23000.0) & (freqs <= 30500.0)
        level_db[subida] = 11.0 * (freqs[subida] - 23000.0) / 7500.0
        fall = (freqs > 30500.0) & (freqs <= 40000.0)
        level_db[fall] = 11.0 - 8.0 * (freqs[fall] - 30500.0) / 9500.0

        rng = np.random.default_rng(2026)
        noise = (rng.standard_normal((freqs.size, 2))
                 + 1j * rng.standard_normal((freqs.size, 2)))
        noise *= (10.0 ** (level_db / 20.0))[:, None]
        noise = np.fft.irfft(noise, n, axis=0)
        noise *= 0.03 / np.max(np.abs(noise))

        lsb = 2.0 ** -23
        dither = (rng.random((n, 2)) - rng.random((n, 2))) * lsb
        return level_db, np.clip(musica + noise + dither, -0.999, 0.999)

    def test_19c_hires_24_96_no_dsd_control(self):
        """A plain studio master is not DSD, and shaped dither is not a ramp.

        Three controls: a 24/96 and a 24/192 studio reference (no profile at
        all), and a lossy 20 kHz brickwall re-dithered with noise-shaped dither
        above 20 kHz (a rising floor, but 32 dB below the music band and after a
        cliff), which must stay accused.
        """
        for sr, name in ((96000, "hires_24_96_studio_control"),
                           (192000, "hires_24_192_studio_control")):
            sig = _create_track_reference(sr, duracion=4.0, seed=120)
            path = self.base_path / f"{name}.flac"
            sf.write(path, np.clip(sig, -0.98, 0.98), sr, subtype="PCM_24")
            res = motor_flac.analyze_file_data(str(path), use_cache=False)

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
        base = _create_track_reference(sr, duracion=duracion, seed=124)
        sos = signal.ellip(12, 0.1, 80.0, 20000.0, btype="low", fs=sr, output="sos")
        clipped = signal.sosfilt(sos, base, axis=0)
        clipped /= np.max(np.abs(clipped)) * 1.05

        level_db = np.full(freqs.shape, -400.0)
        alta = (freqs >= 20000.0) & (freqs <= 40000.0)
        level_db[alta] = 6.0 * (freqs[alta] - 20000.0) / 20000.0
        rng = np.random.default_rng(2027)
        noise = (rng.standard_normal((freqs.size, 2))
                 + 1j * rng.standard_normal((freqs.size, 2)))
        noise *= (10.0 ** (level_db / 20.0))[:, None]
        noise = np.fft.irfft(noise, n, axis=0)
        noise *= 0.0012 / np.max(np.abs(noise))

        path = self.base_path / "lossy_20k_shaped_dither_24_96.flac"
        sf.write(path, np.clip(clipped + noise, -0.999, 0.999), sr, subtype="PCM_24")
        res = motor_flac.analyze_file_data(str(path), use_cache=False)

        self.assertFalse(res["esp"]["es_transferencia_dsd"])
        self.assertIsNone(res["esp"]["perfil_dsd"])
        self.assertGreater(res["esp"]["salto_ultrasonico_db"], 25.0)
        self.assertIs(res["esp"]["corte_detectado"], True)
        self.assertIn(res["veredicto"], BAD_VERDICTS)

    def test_20_vinyl_with_3_clicks_isolated_no_triggers_gibbs_lossy(self):
        """Three vinyl clicks are not a lossy reconstruction pattern.

        Isolated 1-sample events with a band limit below 20.5 kHz used to be
        enough to accuse the file of Gibbs overshoot. A handful of clicks is what
        a vinyl transfer sounds like, not what a codec leaves behind.
        """
        sr = 44100
        duracion = 6.0
        base = _create_track_reference(sr, duracion=duracion, seed=122)
        sos = signal.butter(6, 19000.0, btype="low", fs=sr, output="sos")
        y = signal.sosfilt(sos, base, axis=0)
        y = y / (np.max(np.abs(y)) * 1.05)
        for index in (int(sr * 0.5), int(sr * 1.5), int(sr * 2.5)):
            y[index, 0] = 1.0                      # full-scale pop, one sample

        path = self.base_path / "vinyl_3_clicks_19k.flac"
        sf.write(path, y, sr, subtype="PCM_16")
        res = motor_flac.analyze_file_data(str(path), use_cache=False)

        self.assertLess(res["esp"]["techo_hz"], 20500)
        self.assertEqual(res["esp"]["clipping_eventos"], 3)
        self.assertFalse(res["esp"]["clipping_reconstruccion_lossy"])

    def test_20b_gibbs_positive_10_events_8_short(self):
        """The Gibbs rule needs a pattern: 10 events and 8 short ones.

        Nine events or only seven short ones in ten must not be enough, on the
        classifier and on a real signal alike.
        """
        classify = motor_flac._classify_streaks

        lossy = classify([1] * 8 + [4, 4], None, 20000.0, 22050)
        self.assertIs(lossy["clipping_reconstruccion_lossy"], True)
        self.assertEqual(lossy["clipping_eventos"], 10)

        pocos = classify([1] * 9, None, 20000.0, 22050)
        self.assertIs(pocos["clipping_reconstruccion_lossy"], False)
        self.assertEqual(pocos["clipping_eventos"], 9)

        lengths = classify([1] * 7 + [4, 4, 4], None, 20000.0, 22050)
        self.assertIs(lengths["clipping_reconstruccion_lossy"], False)
        self.assertEqual(lengths["clipping_fraccion_cortos"], 0.7)

        # A band limit of 20.5 kHz or more is not a lossy reconstruction.
        without_band = classify([1] * 8 + [4, 4], None, 20500.0, 22050)
        self.assertIs(without_band["clipping_reconstruccion_lossy"], False)

        sr = 44100
        duracion = 6.0
        base = _create_track_reference(sr, duracion=duracion, seed=123)
        sos = signal.butter(6, 19000.0, btype="low", fs=sr, output="sos")
        clean = signal.sosfilt(sos, base, axis=0)
        clean = clean / (np.max(np.abs(clean)) * 1.05)

        def _with_events(cantidad, short):
            """`cortos` 1-sample pops followed by 4-sample plateaus."""
            y = clean.copy()
            for k in range(cantidad):
                start = int(sr * 0.3) + k * int(sr * 0.35)
                if k < short:
                    y[start, 0] = 1.0
                else:
                    y[start : start + 4, 0] = 1.0
            return y

        path = self.base_path / "gibbs_10_eventos_8_cortos.flac"
        sf.write(path, _with_events(10, 8), sr, subtype="PCM_16")
        res = motor_flac.analyze_file_data(str(path), use_cache=False)
        self.assertEqual(res["esp"]["clipping_eventos"], 10)
        self.assertIs(res["esp"]["clipping_reconstruccion_lossy"], True)

        for name, events, short in (("9_eventos", 9, 9),
                                        ("10_eventos_7_cortos", 10, 7)):
            path = self.base_path / f"gibbs_{name}.flac"
            sf.write(path, _with_events(events, short), sr, subtype="PCM_16")
            res = motor_flac.analyze_file_data(str(path), use_cache=False)
            self.assertEqual(res["esp"]["clipping_eventos"], events)
            self.assertIs(res["esp"]["clipping_reconstruccion_lossy"], False)

    def test_21_islands_mdct_sfb21_without_lowpass(self):
        """A codec that disables its low-pass zeroes sfb21 only in some frames.

        The top scalefactor band (16-20.5 kHz) switches on and off frame by
        frame, so the level of that band relative to 4-11 kHz has a huge
        P90-P10 gap while the file still reaches ~21 kHz and has no cut-off.
        The presence band is boosted so the 4-11 kHz reference sits at a
        realistic level: on this synthetic source the band floor is only about
        30 dB below an unboosted reference, which is not enough to read a
        zeroed sfb21 as -55 dB.
        """
        sr = 44100
        duracion = 6.0
        n = int(sr * duracion)
        t = np.arange(n) / float(sr)
        freqs = np.fft.rfftfreq(n, 1.0 / sr)

        base = _create_track_reference(sr, duracion=duracion, seed=125)
        spectrum = np.fft.rfft(base, axis=0)
        spectrum[(freqs >= 4000.0) & (freqs <= 11000.0)] *= 10.0 ** (30.0 / 20.0)
        base = np.fft.irfft(spectrum, n, axis=0)
        base /= np.max(np.abs(base)) * 1.05

        spectrum = np.fft.rfft(base, axis=0)
        sfb21 = spectrum.copy()
        sfb21[(freqs < 16000.0) | (freqs > 20500.0)] = 0.0
        resto = np.fft.irfft(spectrum - sfb21, n, axis=0)
        sfb21_t = np.fft.irfft(sfb21, n, axis=0)

        # 2.5 Hz gate, 12 ms raised-cosine edges: half of the time the band is
        # gone, without the broadband splatter a hard splice would create.
        bruto = (np.sin(2.0 * np.pi * 2.5 * t) > 0.0).astype(np.float64)
        k = int(sr * 0.012)
        nucleo = np.hanning(k) / np.hanning(k).sum()
        puerta = np.clip(np.convolve(bruto, nucleo, mode="same") * 3.0, 0.0, 1.0)
        y = resto + sfb21_t * puerta[:, None]
        y /= np.max(np.abs(y)) * 1.02

        path = self.base_path / "lame_v0_sin_lowpass_16bit.flac"
        sf.write(path, y, sr, subtype="PCM_16")
        res = motor_flac.analyze_file_data(str(path), use_cache=False)

        self.assertGreaterEqual(res["esp"]["techo_hz"], 20500)
        self.assertIs(res["esp"]["corte_detectado"], False)
        self.assertGreater(res["esp"]["brecha_p90_p10_agudos_db"], 28.0)
        self.assertGreaterEqual(res["esp"]["ratio_apagado_sfb21"], 0.25)
        self.assertLessEqual(res["esp"]["ratio_apagado_sfb21"], 0.85)
        self.assertIs(res["esp"]["islas_mdct_sfb21"], True)
        self.assertIn(res["veredicto"], BAD_VERDICTS)

        # Regression guard for the 0.20 -> 0.25 lower bound: the same gated
        # band, but on ~78 % of the time, leaves a shut-off ratio between 0.20
        # and 0.25 with the same P90-P10 gap. The 0.20 bound fired here; with
        # 0.25 the gap alone is not enough evidence of sfb21 islands.
        bruto = (np.sin(2.0 * np.pi * 2.5 * t) > -0.40).astype(np.float64)
        puerta = np.clip(np.convolve(bruto, nucleo, mode="same") * 3.0, 0.0, 1.0)
        partial = resto + sfb21_t * puerta[:, None]
        partial /= np.max(np.abs(partial)) * 1.02

        path = self.base_path / "control_sfb21_apagado_bajo_25.flac"
        sf.write(path, partial, sr, subtype="PCM_16")
        res = motor_flac.analyze_file_data(str(path), use_cache=False)

        self.assertGreater(res["esp"]["brecha_p90_p10_agudos_db"], 28.0)
        self.assertGreaterEqual(res["esp"]["ratio_apagado_sfb21"], 0.20)
        self.assertLess(res["esp"]["ratio_apagado_sfb21"], 0.25)
        self.assertIs(res["esp"]["islas_mdct_sfb21"], False)

        # Control: strong bass modulated at 3 Hz plus continuous highs. The
        # 4-11 kHz reference does not follow the bass, so the band never looks
        # intermittent (a 1-8 kHz reference would have fired here).
        rng = np.random.default_rng(2033)
        noise_hf = (rng.standard_normal((freqs.size, 2))
                    + 1j * rng.standard_normal((freqs.size, 2)))
        noise_hf *= np.where(freqs >= 12000.0, 1.0, 0.0)[:, None]
        noise_hf = np.fft.irfft(noise_hf, n, axis=0)
        noise_hf *= 0.02 / np.max(np.abs(noise_hf))
        low = 0.45 * np.sin(2.0 * np.pi * 2000.0 * t) * (
            0.5 + 0.5 * np.sin(2.0 * np.pi * 3.0 * t))
        control = base + low[:, None] + noise_hf
        control /= np.max(np.abs(control)) * 1.02

        path = self.base_path / "control_bajo_modulado_agudos_continuos.flac"
        sf.write(path, control, sr, subtype="PCM_16")
        res = motor_flac.analyze_file_data(str(path), use_cache=False)

        self.assertGreaterEqual(res["esp"]["techo_hz"], 20500)
        self.assertIs(res["esp"]["islas_mdct_sfb21"], False)

    def test_21b_excitador_armonico_notch_sbr(self):
        """SBR / harmonic exciter: correlated envelopes plus a deep notch.

        The band is cut at 15.2 kHz and 16.2-20.5 kHz is regenerated from the
        8-14 kHz envelope (a single-sideband shift keeps the envelope, so the
        two measured envelopes correlate almost perfectly) while the cut leaves
        a deep notch where the original band ended.
        """
        sr = 44100
        duracion = 6.0
        n = int(sr * duracion)
        t = np.arange(n) / float(sr)
        freqs = np.fft.rfftfreq(n, 1.0 / sr)

        base = _create_track_reference(sr, duracion=duracion, seed=126)
        rng = np.random.default_rng(2031)
        white = rng.standard_normal((n, 2))
        env = (0.10 + 0.90 * (np.sin(2.0 * np.pi * 2.2 * t) ** 8))[:, None]
        strong = signal.sosfilt(
            signal.butter(6, [8000.0, 14000.0], btype="band", fs=sr, output="sos"),
            white * env, axis=0)
        strong *= 0.35 / np.max(np.abs(strong))
        source = base * 0.5 + strong
        source /= np.max(np.abs(source)) * 1.02

        cutoff = signal.sosfilt(
            signal.ellip(8, 0.5, 80.0, 15200.0, btype="low", fs=sr, output="sos"),
            source, axis=0)
        band = signal.sosfilt(
            signal.butter(6, [8000.0, 14000.0], btype="band", fs=sr, output="sos"),
            source, axis=0)
        desplazada = np.empty_like(band)
        for channel in range(2):
            analitica = signal.hilbert(band[:, channel])
            desplazada[:, channel] = np.real(analitica
                                           * np.exp(2j * np.pi * 8000.0 * t))
        regenerada = desplazada * (np.sqrt(np.mean(band ** 2))
                                   / np.sqrt(np.mean(desplazada ** 2)))

        y = cutoff + regenerada
        y /= np.max(np.abs(y)) * 1.02
        path = self.base_path / "excitador_sbr_notch_16bit.flac"
        sf.write(path, y, sr, subtype="PCM_16")
        res = motor_flac.analyze_file_data(str(path), use_cache=False)

        self.assertGreater(res["esp"]["corr_env_excitador"], 0.92)
        self.assertGreaterEqual(res["esp"]["muesca_excitador_db"], 15.0)
        self.assertIs(res["esp"]["sospecha_excitador_armonico"], True)
        self.assertIn(res["veredicto"], BAD_VERDICTS)

        # Control: the same correlation with the notch filled, so the depth is
        # the only difference left.
        relleno = signal.sosfilt(
            signal.butter(6, [14500.0, 16500.0], btype="band", fs=sr, output="sos"),
            white * env, axis=0)
        relleno *= 0.35 / np.max(np.abs(relleno))
        control = cutoff + regenerada + relleno * 0.4
        control /= np.max(np.abs(control)) * 1.02

        path = self.base_path / "control_notch_superficial_16bit.flac"
        sf.write(path, control, sr, subtype="PCM_16")
        res = motor_flac.analyze_file_data(str(path), use_cache=False)

        self.assertGreater(res["esp"]["corr_env_excitador"], 0.92)
        self.assertLess(res["esp"]["muesca_excitador_db"], 10.0)
        self.assertIs(res["esp"]["sospecha_excitador_armonico"], False)

        # Control: an ASCENDING step. The high band rises +22 dB from 15.8 kHz
        # while the 16-20.5 kHz envelope still tracks the 8-14 kHz one. The
        # unsigned distance between the two flanks is huge, but the sign says
        # "rising": there is no notch, so the exciter detector must stay quiet.
        step_band = signal.sosfilt(
            signal.butter(6, [13000.0, 20500.0], btype="band", fs=sr, output="sos"),
            white * env, axis=0)
        step_band *= 0.35 / np.max(np.abs(step_band))
        musica = signal.sosfilt(
            signal.ellip(8, 0.5, 80.0, 13000.0, btype="low", fs=sr, output="sos"),
            source, axis=0)
        rampa = np.clip((freqs - 15800.0) / (16200.0 - 15800.0), 0.0, 1.0)
        gain = 10.0 ** (22.0 * (0.5 - 0.5 * np.cos(np.pi * rampa)) / 20.0)
        step = np.fft.irfft(
            np.fft.rfft(step_band, axis=0) * gain[:, None], n, axis=0)
        subida = musica + step
        subida /= np.max(np.abs(subida)) * 1.02

        # The step itself, measured on the file: mean(16.2-17.5 kHz) minus
        # mean(14.5-15.8 kHz) is at least +20 dB.
        curva = 20.0 * np.log10(np.maximum(
            np.abs(np.fft.rfft(subida, axis=0)).mean(axis=1), 1e-12))
        pre = float(np.mean(curva[(freqs >= 14500.0) & (freqs <= 15800.0)]))
        post = float(np.mean(curva[(freqs >= 16200.0) & (freqs <= 17500.0)]))
        self.assertGreaterEqual(post - pre, 20.0)

        path = self.base_path / "control_escalon_ascendente_16bit.flac"
        sf.write(path, subida, sr, subtype="PCM_16")
        res = motor_flac.analyze_file_data(str(path), use_cache=False)

        self.assertGreater(res["esp"]["corr_env_excitador"], 0.92)
        self.assertLess(res["esp"]["muesca_excitador_db"], 15.0)
        self.assertIs(res["esp"]["sospecha_excitador_armonico"], False)

    def test_21c_acoustic_low_centroid_without_break_rule_cd(self):
        """A solo acoustic mix ends its band early and is not an upscale.

        The centroid sits around 1400 Hz (a rule with a 1200 Hz threshold would
        miss it), the band decays smoothly from 8 kHz with no cut-off, and the
        bits are clean: the low ceiling is the instrumentation, not a defect.
        """
        sr = 44100
        duracion = 6.0
        n = int(sr * duracion)
        t = np.arange(n) / float(sr)
        freqs = np.fft.rfftfreq(n, 1.0 / sr)

        rng = np.random.default_rng(2032)
        noise = (rng.standard_normal((freqs.size, 2))
                 + 1j * rng.standard_normal((freqs.size, 2)))
        level_db = np.full(freqs.shape, -400.0)
        rosa = (freqs >= 45.0) & (freqs <= 8000.0)
        level_db[rosa] = -11.0 * np.log10(freqs[rosa] / 45.0)
        level_8k = -11.0 * np.log10(8000.0 / 45.0)
        fall = (freqs > 8000.0) & (freqs <= 15000.0)
        level_db[fall] = level_8k - 6.0 * (freqs[fall] - 8000.0) / 1000.0
        level_db[freqs > 15000.0] = level_8k - 6.0 * 7.0
        noise *= (10.0 ** (level_db / 20.0))[:, None]
        acoustic = np.fft.irfft(noise, n, axis=0)
        acoustic /= np.max(np.abs(acoustic)) * 1.05
        acoustic *= (0.35 + 0.65
                     * (np.sin(2.0 * np.pi * 0.8 * t) ** 2))[:, None]
        acoustic /= np.max(np.abs(acoustic)) * 1.02

        path = self.base_path / "solo_acustico_centroide_bajo.flac"
        sf.write(path, acoustic, sr, subtype="PCM_16")
        res = motor_flac.analyze_file_data(str(path), use_cache=False)
        esp = res["esp"]

        self.assertGreater(esp["centroide_hz"], 1250.0)
        self.assertLess(esp["centroide_hz"], 1800.0)
        self.assertGreaterEqual(esp["techo_hz"], 11500)
        self.assertLessEqual(esp["techo_hz"], 15000)
        self.assertLess(esp["techo_rel_nyquist"], 0.70)
        self.assertIs(esp["corte_detectado"], False)
        self.assertLess(esp["gradiente_borde_db_khz"], 20.0)
        self.assertIs(esp["decaimiento_acustico_suave"], True)
        self.assertEqual(res["veredicto"], "PROBABLY LOSSLESS")
        self.assertIn("solo acoustic/low-centroid", " ".join(res["problemas"]))

        # Rule 2, checked on the verdict tree directly. In the 0.70-0.88 band the
        # exemption must not add a single extra vote; below 0.70 it replaces the
        # 0.40 penalty with 1.5 votes.
        meta = {"sample_rate": 44100, "bits_per_sample": 16, "canales": 2,
                "duracion": 100.0}
        bdi = {"aplica": False}

        def _esp_with_ceiling(ceiling_hz, acoustic_smooth):
            return {"techo_hz": ceiling_hz,
                    "techo_rel_nyquist": round(ceiling_hz / 22050.0, 4),
                    "clase_corte": "no_cutoff", "frecuencia_corte": None,
                    "distancia_nyquist": None, "corte_detectado": False,
                    "ratio": 1.0, "ratio_aplica": True, "var_alta_db": 0.0,
                    "separacion_stereo": 1.0,
                    "decaimiento_acustico_suave": acoustic_smooth}

        mid_with, _, _ = motor_flac.calculate_score(meta, _esp_with_ceiling(16500, True), bdi)
        mid_without, _, _ = motor_flac.calculate_score(meta, _esp_with_ceiling(16500, False), bdi)
        self.assertEqual(mid_with, mid_without)
        self.assertLess(mid_with, 0.80)          # 1.5 votes, never 3.0

        low_with, _, prob_low_with = motor_flac.calculate_score(
            meta, _esp_with_ceiling(13000, True), bdi)
        low_without, _, prob_low_without = motor_flac.calculate_score(
            meta, _esp_with_ceiling(13000, False), bdi)
        self.assertGreater(low_with, low_without)
        self.assertIn("solo acoustic/low-centroid", " ".join(prob_low_with))
        self.assertFalse(any("destroyed" in p for p in prob_low_with))
        self.assertTrue(any("destroyed" in p for p in prob_low_without))

        # 24-bit: the acoustic exemption is fail-closed. Only a fully active
        # LSB lattice earns it; "partial", "indeterminate" and None all mean
        # "unverified", not "clean".
        meta_24 = {"sample_rate": 44100, "bits_per_sample": 24, "canales": 2,
                   "duracion": 100.0}
        bdi_24_clean = {"aplica": True, "lsbs": "active",
                         "forense_bits": {"es_padded_16_en_24": False,
                                          "es_24bit_escalado_en_peine": False}}
        esp_13000 = _esp_with_ceiling(13000, True)
        _, verdict, prob = motor_flac.calculate_score(
            meta_24, esp_13000, bdi_24_clean)
        self.assertIn("solo acoustic/low-centroid", " ".join(prob))
        self.assertFalse(any("destroyed" in p for p in prob))
        # Rule 5 is untouched by the exemption: 24 bits below 88 % of Nyquist
        # is not backed by the content and blocks the maximum verdict.
        self.assertTrue(any("not backed by the content" in p for p in prob))
        self.assertNotEqual(verdict, "GENUINE LOSSLESS")

        for state in ("partial", "indeterminate", None):
            bdi_24 = {"aplica": True, "lsbs": state, "forense_bits": {}}

            _, _, prob = motor_flac.calculate_score(
                meta_24, _esp_with_ceiling(13000, True), bdi_24)
            self.assertTrue(any("destroyed" in p for p in prob), state)
            self.assertNotIn("solo acoustic/low-centroid", " ".join(prob), state)

            esp_075 = _esp_with_ceiling(16538, True)
            self.assertGreaterEqual(esp_075["techo_rel_nyquist"], 0.70)
            self.assertLess(esp_075["techo_rel_nyquist"], 0.88)
            _, verdict, prob = motor_flac.calculate_score(meta_24, esp_075, bdi_24)
            self.assertTrue(any("not backed by the content" in p for p in prob),
                            state)
            self.assertNotEqual(verdict, "GENUINE LOSSLESS", state)
            # A blocked score never reaches the two lossless labels, so the
            # unverified LSB states cannot be rescued as "probably lossless"
            # either.
            if state in ("indeterminate", None):
                self.assertEqual(verdict, "SUSPICIOUS", state)

        # A synthetic 24-bit upscale of the same low-centroid mix: clean active
        # LSBs earn the Rule 2 exemption, but Rule 5 blocks (the first file),
        # and a 16-bit grid inside a 24-bit container is caught by the comb.
        for name, payload in (
                ("solo_acustico_24bit_upscale.flac", acoustic),
                ("solo_acustico_16bit_en_24.flac",
                 np.round(acoustic * 32767.0) / 32767.0)):
            path = self.base_path / name
            sf.write(path, payload, sr, subtype="PCM_24")
            res = motor_flac.analyze_file_data(str(path), use_cache=False)
            self.assertEqual(res["meta"]["bits_per_sample"], 24)
            self.assertIs(res["bdi"]["aplica"], True)
            self.assertLess(res["esp"]["techo_rel_nyquist"], 0.88)
            self.assertNotEqual(res["veredicto"], "GENUINE LOSSLESS", name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
