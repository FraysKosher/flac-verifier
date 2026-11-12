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


def _create_track_reference(sr: int, duration: float = 8.0, seed: int = 2026) -> np.ndarray:
    """Stereo reference with decorrelated pink noise, harmonics and transients."""
    rng = np.random.default_rng(seed)
    n = int(sr * duration)
    t = np.linspace(0.0, duration, n, endpoint=False)

    b, a = ([0.049922, -0.095993, 0.050612, -0.004408],
            [1.0, -2.494956, 2.017265, -0.522189])
    rosa_l = signal.lfilter(b, a, rng.standard_normal(n))
    rosa_r = signal.lfilter(b, a, rng.standard_normal(n))
    env_percusiva = 0.12 + 0.88 * (np.sin(2.0 * np.pi * 2.5 * t) ** 10)
    base_tonal_l = 0.25 * np.sin(2.0 * np.pi * 110.0 * t) + 0.15 * np.sin(2.0 * np.pi * 440.0 * t)
    base_tonal_r = 0.25 * np.sin(2.0 * np.pi * 110.0 * t) + 0.15 * np.cos(2.0 * np.pi * 440.0 * t)

    estimate = np.column_stack([
        base_tonal_l + rosa_l * env_percusiva * 0.40,
        base_tonal_r + rosa_r * env_percusiva * 0.40,
    ])
    estimate /= np.max(np.abs(estimate)) * 1.12
    return estimate


def _dsd_like(sr: int, duration: float, seed: int) -> np.ndarray:
    """Music below 20 kHz plus an independent rising noise floor in 24-40 kHz."""
    rng = np.random.default_rng(seed)
    n = int(sr * duration)
    rng_freq = np.fft.rfftfreq(n, 1.0 / sr)
    musica = (rng.standard_normal((n // 2 + 1, 2))
              + 1j * rng.standard_normal((n // 2 + 1, 2)))
    musica[rng_freq > 20000.0] = 0.0
    musica = np.fft.irfft(musica, n, axis=0)
    musica /= np.max(np.abs(musica)) * 4.0

    noise = (rng.standard_normal((n // 2 + 1, 2))
             + 1j * rng.standard_normal((n // 2 + 1, 2)))
    ramp = np.clip((rng_freq - 24000.0) / 16000.0, 0.0, 1.0)
    noise *= (((rng_freq >= 24000.0) & (rng_freq <= 40000.0))[:, None]
              * (1.0 + 9.0 * ramp)[:, None])
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
        sig = _create_track_reference(sr, duration=8.0, seed=101)
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
        sig_44 = _create_track_reference(44100, duration=8.0, seed=102)
        sig_96 = signal.resample_poly(sig_44, up=320, down=147, axis=0)
        path = self.base_path / "upscale_sinc_24_96.flac"
        sf.write(path, np.clip(sig_96, -0.98, 0.98), 96000, subtype="PCM_24")

        res = motor_flac.analyze_file_data(str(path), use_cache=False)
        self.assertIn(res["veredicto"], ("PROBABLE UPSCALE", "SUSPICIOUS"))

    def test_03_upscale_aliasing_mirror_without_filter_is_detected(self):
        """44.1 -> 88.2 linear interpolation: the image mirrors around 22.05 kHz."""
        sig_44 = _create_track_reference(44100, duration=8.0, seed=103)
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
        sig = _create_track_reference(44100, duration=6.0, seed=104)
        sig_q16 = np.round(sig * 32767.0) / 32768.0
        path = self.base_path / "padded_16_in_24.flac"
        sf.write(path, np.clip(sig_q16, -0.99, 0.99), 44100, subtype="PCM_24")

        res = motor_flac.analyze_file_data(str(path), use_cache=False)
        self.assertNotEqual(res["veredicto"], "GENUINE LOSSLESS")

    def test_05_transcode_joint_stereo_collapse_side(self):
        """Mid reaches 20.1 kHz while Side is cut at 15.8 kHz (MP3 320 joint)."""
        sr = 44100
        sig = _create_track_reference(sr, duration=8.0, seed=105)
        mid = 0.5 * (sig[:, 0] + sig[:, 1])
        side = 0.5 * (sig[:, 0] - sig[:, 1])

        sos_mid = signal.ellip(10, 0.1, 80.0, 20100.0, btype="low", fs=sr, output="sos")
        sos_side = signal.ellip(10, 0.1, 80.0, 15800.0, btype="low", fs=sr, output="sos")
        mid_f = signal.sosfilt(sos_mid, mid)
        side_f = signal.sosfilt(sos_side, side)

        estimate_js = np.column_stack([mid_f + side_f, mid_f - side_f])
        path = self.base_path / "fake_mp3_320_joint_stereo.flac"
        sf.write(path, np.clip(estimate_js, -0.98, 0.98), sr, subtype="PCM_16")

        res = motor_flac.analyze_file_data(str(path), use_cache=False)
        self.assertIn(res["veredicto"], BAD_VERDICTS)

    def test_06_intro_long_smooth_no_fools_engine(self):
        """50 s of quiet low-frequency intro followed by 12 s of full band."""
        sr = 44100
        n_intro = sr * 50
        t_intro = np.linspace(0.0, 50.0, n_intro, endpoint=False)
        intro_bass = 0.2 * np.sin(2.0 * np.pi * 180.0 * t_intro)
        intro_estimate = np.column_stack([intro_bass, intro_bass * 0.95])

        body_active = _create_track_reference(sr, duration=12.0, seed=106)
        track_full = np.vstack([intro_estimate, body_active])

        path = self.base_path / "prog_rock_long_intro.flac"
        sf.write(path, np.clip(track_full, -0.98, 0.98), sr, subtype="PCM_16")

        res = motor_flac.analyze_file_data(str(path), use_cache=False)
        self.assertIn(res["veredicto"], GOOD_VERDICTS)

    def test_07_track_silence_digital_no_produces_nan_nor_crash(self):
        """100% zeros: no verdict, no NaN/Inf, no exception."""
        sr = 44100
        zeros = np.zeros((sr * 4, 2), dtype=np.float32)
        path = self.base_path / "digital_silence.flac"
        sf.write(path, zeros, sr, subtype="PCM_16")

        res = motor_flac.analyze_file_data(str(path), use_cache=False)
        self._assert_finite(res, "resultado")

    def _assert_finite(self, value, path):
        if isinstance(value, float):
            self.assertFalse(np.isnan(value), f"{path} is NaN")
            self.assertFalse(np.isinf(value), f"{path} is Inf")
        elif isinstance(value, dict):
            for key, hijo in value.items():
                self._assert_finite(hijo, f"{path}.{key}")
        elif isinstance(value, (list, tuple)):
            for index, hijo in enumerate(value):
                self._assert_finite(hijo, f"{path}[{index}]")

    def test_08_transfer_dsd_detected_no_penalized(self):
        """DSD64-like 88.2 kHz: music <20 kHz plus an ultrasonic noise ramp."""
        sr = 88200
        sig = _dsd_like(sr, duration=4.0, seed=108)
        path = self.base_path / "dsd_transfer_88k.flac"
        sf.write(path, np.clip(sig, -0.98, 0.98), sr, subtype="PCM_24")

        res = motor_flac.analyze_file_data(str(path), use_cache=False)
        self.assertTrue(res["esp"]["es_transferencia_dsd"])
        self.assertIn(res["veredicto"], GOOD_VERDICTS)

    def test_09_transcode_opus_20k_is_detected(self):
        """Realistic libopus fullband: 20.00 kHz cliff + frame-varying cut-off."""
        sr = 44100
        sig = _create_track_reference(sr, duration=6.0, seed=109)
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
        sig = _create_track_reference(44100, duration=6.0, seed=110)
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
        sig = _create_track_reference(sr, duration=6.0, seed=112)
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
        base = _create_track_reference(sr, duration=6.0, seed=114)
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
        sig = _create_track_reference(sr, duration=6.0, seed=115)
        peak = float(np.max(np.abs(sig)))

        def _compress(floor_db, ceiling_db):
            """Loudness-war squash: keep the waveform shape, fill the window."""
            floor, ceiling = 10 ** (floor_db / 20.0), 10 ** (ceiling_db / 20.0)
            return np.sign(sig) * (floor + (np.abs(sig) / peak) * (ceiling - floor))

        def _false(compressed):
            """16-bit master with a -0.3 dB digital gain, written into 24 bits."""
            q16 = np.round(compressed * 32768.0)
            return np.clip(q16 * 0.96605 / 32768.0, -1.0, 1.0)

        path = self.base_path / "fake_24bit_loud_no_quiet_samples.flac"
        sf.write(path, _false(_compress(-18.0, -1.0)), sr, subtype="PCM_24")
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
            sf.write(path, _false(_compress(floor_db, -1.0)), sr, subtype="PCM_24")
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
        genuine = np.clip(_compress(-6.0, -1.0) + dither, -0.999, 0.999)
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
        base = _create_track_reference(sr, duration=6.0, seed=116)
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

        spectrum_ok = res_ok["esp"]
        self.assertTrue(0.88 <= spectrum_ok["techo_rel_nyquist"] < 0.95,
                        f"ceiling {spectrum_ok['techo_hz']} Hz outside the test window")
        self.assertEqual(spectrum_ok["clase_corte"], "no_cutoff")
        self.assertLess(spectrum_ok["gradiente_borde_db_khz"], 35.0)
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
        marker = np.where(np.arange(n) % 2 == 0, 0x05, 0xFA).astype(np.int64)
        load = rng.integers(0, 1 << 16, size=n, dtype=np.int64)
        raw = (marker << 16) | load
        raw = np.where(raw >= (1 << 23), raw - (1 << 24), raw)
        block = (raw.astype(np.float64) / float(1 << 23)).reshape(-1, 1)

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

        def _raw_stereo(skew_right):
            """Two-channel DoP stream, split in two blocks to test the join."""
            def _channel(markers):
                load = rng.integers(0, 1 << 16, size=n, dtype=np.int64)
                values = (markers << 16) | load
                return np.where(values >= (1 << 23), values - (1 << 24), values)

            left = _channel(np.where(np.arange(n) % 2 == 0, 0x05, 0xFA))
            right = _channel(np.where(np.arange(n) % 2 == skew_right,
                                      0x05, 0xFA))
            raw = np.column_stack([left, right]).astype(np.float64)
            return raw / float(1 << 23)

        # Both channels in phase: ravel() would interleave 0x05, 0x05, 0xFA, 0xFA
        # and see no pair at all, so every marker pair must be counted per channel.
        stereo = _raw_stereo(skew_right=0)
        accumulator = motor_flac.ForensicBitsAccumulator(24, sample_rate=sr)
        accumulator.add(stereo[: n // 2])
        accumulator.add(stereo[n // 2 :])
        res = accumulator.result()
        self.assertTrue(res["es_flujo_dop"])
        self.assertGreaterEqual(res["dop_ratio"], 0.90)

        # Out of phase: the left channel starts at 0x05 and the right one at 0xFA.
        stereo = _raw_stereo(skew_right=1)
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
        duration = 10.0
        n = int(sr * duration)
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
        duration = 6.0
        t = np.arange(int(sr * duration)) / float(sr)
        track = _create_track_reference(sr, duration=duration, seed=117) * 0.12
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

        def _spectrum(peaks):
            """Flat 150-6000 Hz programme band, -0.004 dB/Hz above it, + peaks."""
            power_db = np.full(freqs.shape, -130.0)
            band = (freqs >= 150.0) & (freqs <= 6000.0)
            power_db[band] = 0.0
            highs = freqs > 6000.0
            power_db[highs] = -0.004 * (freqs[highs] - 6000.0)
            for frequency, level_db in peaks:
                idx = int(np.argmin(np.abs(freqs - frequency)))
                power_db[idx] = max(power_db[idx], level_db)
            return 10.0 ** (power_db / 10.0)

        def _ceiling_with_anchor(power, anchor_db):
            """Last bin above -60 dB relative to the given reference level."""
            power_db_n = 10.0 * np.log10(np.maximum(power, 1e-28)) - anchor_db
            mask = power_db_n > -60.0
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
        duration = 6.0
        t = np.arange(int(sr * duration)) / float(sr)
        mono = _create_track_reference(sr, duration=duration, seed=118)[:, 0]
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
        duration = 6.0
        t = np.arange(int(sr * duration)) / float(sr)
        shimmer = 0.02 * (np.sin(2.0 * np.pi * 19500.0 * t)
                          + 0.6 * np.sin(2.0 * np.pi * 20750.0 * t))
        reference = _create_track_reference(sr, duration=duration, seed=118)
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
        result_estimate = motor_flac.analyze_file_data(str(path), use_cache=False)
        self.assertIs(result_estimate["esp"]["es_dual_mono"], False)

    def test_19_dsd_96khz_ramp_direct(self):
        """A DSD transfer stored at 96 kHz is DSD: the profile is universal.

        The DSD detector used to require a sample rate that is a multiple of
        44.1 kHz, so a 96 kHz transfer fell through to the mirror/brickwall
        rules even though its 24-40 kHz noise-shaping ramp is unmistakable.
        """
        sr = 96000
        sig = _dsd_like(sr, duration=4.0, seed=119)

        path = self.base_path / "dsd_transfer_96k.flac"
        sf.write(path, np.clip(sig, -0.98, 0.98), sr, subtype="PCM_24")
        res = motor_flac.analyze_file_data(str(path), use_cache=False)

        self.assertTrue(res["esp"]["es_transferencia_dsd"])
        self.assertEqual(res["esp"]["perfil_dsd"], "direct_24_40k")
        self.assertEqual(res["veredicto"], "GENUINE LOSSLESS")

        # The same direct transfer at 192 kHz: same profile, same verdict.
        sr = 192000
        sig = _dsd_like(sr, duration=4.0, seed=119)

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
    def _dsd128_192k(sr=192000, duration=4.0, seed=119):
        """DSD128 transfer: music below 20 kHz and a shaped floor above it.

        The floor sits about 22 dB below the 18-20 kHz music band (roughly
        -53 dBFS per bin) and climbs another 20 dB between 40 and 86 kHz, which
        is how a DSD128 transfer looks. The ramp is carried past 80 kHz because
        the 1 kHz smoothing of the engine reaches into the band edge; the level
        is high enough that the file is not mistaken for shaped dither on a
        lossy transcode (a cliff of more than 25 dB would be)."""
        n = int(sr * duration)
        freqs = np.fft.rfftfreq(n, 1.0 / sr)

        musica = _create_track_reference(sr, duration=duration, seed=seed)
        spectrum = np.fft.rfft(musica, axis=0)
        spectrum[freqs > 20000.0] = 0.0
        musica = np.fft.irfft(spectrum, n, axis=0)
        musica /= np.max(np.abs(musica)) * 1.05

        level_db = np.full(freqs.shape, -400.0)
        level_db[(freqs >= 20000.0) & (freqs <= 40000.0)] = 0.0
        ramp = (freqs > 40000.0) & (freqs <= 86000.0)
        level_db[ramp] = 20.0 * (freqs[ramp] - 40000.0) / 40000.0

        rng = np.random.default_rng(2026)
        noise = (rng.standard_normal((freqs.size, 2))
                 + 1j * rng.standard_normal((freqs.size, 2)))
        noise *= (10.0 ** (level_db / 20.0))[:, None]
        noise = np.fft.irfft(noise, n, axis=0)
        noise *= 0.035 / np.max(np.abs(noise))

        lsb = 2.0 ** -23
        dither = (rng.random((n, 2)) - rng.random((n, 2))) * lsb
        return np.clip(musica + noise + dither, -0.999, 0.999)

    def test_19b_dsd_scarlet_book_30k_isolated(self):
        """Scarlet Book: the 30 kHz hump survives the SACD protective filter.

        Profile A needs a ramp that rises more than 12 dB from 24 to 40 kHz. The
        30 kHz protective filter of a SACD master cuts that ramp, so the file
        only keeps the noise-shaping hump between 23 and 31 kHz. The fixture is
        built so that Profile A cannot fire (a 1.5 dB total rise and 41 % of
        rising steps), and the reported profile pins which profile fired.
        """
        duration = 4.0
        sr = 96000
        n = int(sr * duration)
        freqs = np.fft.rfftfreq(n, 1.0 / sr)
        level_db, mix = self._scarlet_book(sr, duration)

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
        _, mix = self._scarlet_book(sr, duration)
        path = self.base_path / "dsd_scarlet_book_30k_24_192.flac"
        sf.write(path, mix, sr, subtype="PCM_24")
        res = motor_flac.analyze_file_data(str(path), use_cache=False)

        self.assertTrue(res["esp"]["es_transferencia_dsd"])
        self.assertEqual(res["esp"]["perfil_dsd"], "scarlet_30k")
        self.assertEqual(res["veredicto"], "GENUINE LOSSLESS")

    @staticmethod
    def _scarlet_book(sr, duration=4.0, seed=121):
        """SACD master: music to 20 kHz, 30 kHz hump and protective filter.

        Returns the designed per-bin envelope in dB and the stereo waveform."""
        n = int(sr * duration)
        freqs = np.fft.rfftfreq(n, 1.0 / sr)

        musica = _create_track_reference(sr, duration=duration, seed=seed)
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
            sig = _create_track_reference(sr, duration=4.0, seed=120)
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
        duration = 4.0
        n = int(sr * duration)
        freqs = np.fft.rfftfreq(n, 1.0 / sr)
        base = _create_track_reference(sr, duration=duration, seed=124)
        sos = signal.ellip(12, 0.1, 80.0, 20000.0, btype="low", fs=sr, output="sos")
        clipped = signal.sosfilt(sos, base, axis=0)
        clipped /= np.max(np.abs(clipped)) * 1.05

        level_db = np.full(freqs.shape, -400.0)
        high = (freqs >= 20000.0) & (freqs <= 40000.0)
        level_db[high] = 6.0 * (freqs[high] - 20000.0) / 20000.0
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
        duration = 6.0
        base = _create_track_reference(sr, duration=duration, seed=122)
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
        duration = 6.0
        base = _create_track_reference(sr, duration=duration, seed=123)
        sos = signal.butter(6, 19000.0, btype="low", fs=sr, output="sos")
        clean = signal.sosfilt(sos, base, axis=0)
        clean = clean / (np.max(np.abs(clean)) * 1.05)

        def _with_events(quantity, short):
            """`cortos` 1-sample pops followed by 4-sample plateaus."""
            y = clean.copy()
            for k in range(quantity):
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
        duration = 6.0
        n = int(sr * duration)
        t = np.arange(n) / float(sr)
        freqs = np.fft.rfftfreq(n, 1.0 / sr)

        base = _create_track_reference(sr, duration=duration, seed=125)
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

    def test_21b_harmonic_exciter_notch_sbr(self):
        """SBR / harmonic exciter: correlated envelopes plus a deep notch.

        The band is cut at 15.2 kHz and 16.2-20.5 kHz is regenerated from the
        8-14 kHz envelope (a single-sideband shift keeps the envelope, so the
        two measured envelopes correlate almost perfectly) while the cut leaves
        a deep notch where the original band ended.
        """
        sr = 44100
        duration = 6.0
        n = int(sr * duration)
        t = np.arange(n) / float(sr)
        freqs = np.fft.rfftfreq(n, 1.0 / sr)

        base = _create_track_reference(sr, duration=duration, seed=126)
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
        ramp = np.clip((freqs - 15800.0) / (16200.0 - 15800.0), 0.0, 1.0)
        gain = 10.0 ** (22.0 * (0.5 - 0.5 * np.cos(np.pi * ramp)) / 20.0)
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
        duration = 6.0
        n = int(sr * duration)
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
        spectrum = res["esp"]

        self.assertGreater(spectrum["centroide_hz"], 1250.0)
        self.assertLess(spectrum["centroide_hz"], 1800.0)
        self.assertGreaterEqual(spectrum["techo_hz"], 11500)
        self.assertLessEqual(spectrum["techo_hz"], 15000)
        self.assertLess(spectrum["techo_rel_nyquist"], 0.70)
        self.assertIs(spectrum["corte_detectado"], False)
        self.assertLess(spectrum["gradiente_borde_db_khz"], 20.0)
        self.assertIs(spectrum["decaimiento_acustico_suave"], True)
        self.assertEqual(res["veredicto"], "PROBABLY LOSSLESS")
        self.assertIn("solo acoustic/low-centroid", " ".join(res["problemas"]))

        # Rule 2, checked on the verdict tree directly. In the 0.70-0.88 band the
        # exemption must not add a single extra vote; below 0.70 it replaces the
        # 0.40 penalty with 1.5 votes.
        meta = {"sample_rate": 44100, "bits_per_sample": 16, "canales": 2,
                "duracion": 100.0}
        bdi = {"aplica": False}

        def _spectrum_with_ceiling(ceiling_hz, acoustic_smooth):
            return {"techo_hz": ceiling_hz,
                    "techo_rel_nyquist": round(ceiling_hz / 22050.0, 4),
                    "clase_corte": "no_cutoff", "frecuencia_corte": None,
                    "distancia_nyquist": None, "corte_detectado": False,
                    "ratio": 1.0, "ratio_aplica": True, "var_alta_db": 0.0,
                    "separacion_stereo": 1.0,
                    "decaimiento_acustico_suave": acoustic_smooth}

        mid_with, _, _ = motor_flac.calculate_score(meta, _spectrum_with_ceiling(16500, True), bdi)
        mid_without, _, _ = motor_flac.calculate_score(meta, _spectrum_with_ceiling(16500, False), bdi)
        self.assertEqual(mid_with, mid_without)
        self.assertLess(mid_with, 0.80)          # 1.5 votes, never 3.0

        low_with, _, prob_low_with = motor_flac.calculate_score(
            meta, _spectrum_with_ceiling(13000, True), bdi)
        low_without, _, prob_low_without = motor_flac.calculate_score(
            meta, _spectrum_with_ceiling(13000, False), bdi)
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
        spectrum_13000 = _spectrum_with_ceiling(13000, True)
        _, verdict, prob = motor_flac.calculate_score(
            meta_24, spectrum_13000, bdi_24_clean)
        self.assertIn("solo acoustic/low-centroid", " ".join(prob))
        self.assertFalse(any("destroyed" in p for p in prob))
        # Rule 5 is untouched by the exemption: 24 bits below 88 % of Nyquist
        # is not backed by the content and blocks the maximum verdict.
        self.assertTrue(any("not backed by the content" in p for p in prob))
        self.assertNotEqual(verdict, "GENUINE LOSSLESS")

        for state in ("partial", "indeterminate", None):
            bdi_24 = {"aplica": True, "lsbs": state, "forense_bits": {}}

            _, _, prob = motor_flac.calculate_score(
                meta_24, _spectrum_with_ceiling(13000, True), bdi_24)
            self.assertTrue(any("destroyed" in p for p in prob), state)
            self.assertNotIn("solo acoustic/low-centroid", " ".join(prob), state)

            spectrum_075 = _spectrum_with_ceiling(16538, True)
            self.assertGreaterEqual(spectrum_075["techo_rel_nyquist"], 0.70)
            self.assertLess(spectrum_075["techo_rel_nyquist"], 0.88)
            _, verdict, prob = motor_flac.calculate_score(meta_24, spectrum_075, bdi_24)
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

    def test_22_16bit_with_boundary_micro_dither_in_24(self):
        """A 16-bit source in 24-bit with +-2 LSB micro-dither is caught by interior LSB8 occupancy."""
        sr = 44100
        n = sr * 5
        rng = np.random.default_rng(2201)
        raw = rng.standard_normal((n, 2))
        b, a = signal.butter(2, 16000.0 / (sr * 0.5), btype="low")
        sig = signal.lfilter(b, a, raw, axis=0)
        sig /= np.max(np.abs(sig)) * 1.15

        grid16_i24 = np.round(sig * 32767.0).astype(np.int32) * 256
        micro_dither = rng.integers(-2, 3, size=grid16_i24.shape, dtype=np.int32)
        forged_i24 = np.clip(grid16_i24 + micro_dither, -8388608, 8388607)

        path_fake = self.base_path / "falso_16bit_micro_dither_en_24.flac"
        sf.write(path_fake, forged_i24 << 8, sr, subtype="PCM_24")
        res_fake = motor_flac.analyze_file_data(str(path_fake), use_cache=False)
        fb_fake = res_fake["bdi"]["forense_bits"]

        self.assertIs(fb_fake["es_padded_16_en_24"], False)
        self.assertLess(fb_fake["lsb8_ocupacion_interior"], 0.05)
        self.assertIs(fb_fake["es_16bit_dither_en_24"], True)
        self.assertNotEqual(res_fake["veredicto"], "GENUINE LOSSLESS")
        self.assertIn("boundary micro-dither", " ".join(res_fake["problemas"]))

        true24_i24 = np.clip(np.round(sig * 8388607.0).astype(np.int32), -8388608, 8388607)
        path_ok = self.base_path / "control_24bit_real.flac"
        sf.write(path_ok, true24_i24 << 8, sr, subtype="PCM_24")
        res_ok = motor_flac.analyze_file_data(str(path_ok), use_cache=False)
        fb_ok = res_ok["bdi"]["forense_bits"]

        self.assertGreater(fb_ok["lsb8_ocupacion_interior"], 0.80)
        self.assertIs(fb_ok["es_16bit_dither_en_24"], False)
        self.assertEqual(res_ok["veredicto"], "GENUINE LOSSLESS")

    def test_23_upscale_48k_to_96k_brickwall_24k(self):
        """A 48 kHz master upsampled to 96 kHz has a brickwall at 24.0 kHz and is caught."""
        sr_src = 48000
        sr_dst = 96000
        n_src = sr_src * 5
        rng = np.random.default_rng(2301)
        src = rng.standard_normal((n_src, 2))
        b, a = signal.butter(2, 18000.0 / (sr_src * 0.5), btype="low")
        src = signal.lfilter(b, a, src, axis=0)
        src /= np.max(np.abs(src)) * 1.15

        upscaled = signal.resample_poly(src, 2, 1, axis=0)
        dither = (rng.random(upscaled.shape) - rng.random(upscaled.shape)) / 8388608.0
        upscaled = np.clip(upscaled + dither, -0.99, 0.99)

        path = self.base_path / "falso_48k_a_96k_brickwall_24k.flac"
        sf.write(path, upscaled, sr_dst, subtype="PCM_24")
        res = motor_flac.analyze_file_data(str(path), use_cache=False)

        self.assertGreaterEqual(res["esp"]["techo_hz"], 22050)
        self.assertIs(res["esp"]["muro_brickwall_nyquist"], True)
        self.assertNotEqual(res["veredicto"], "GENUINE LOSSLESS")

    def test_24_hires_ultrasonic_transients_no_false_jitter(self):
        """A genuine 96 kHz master with natural ultrasonic decay does not trigger cut-off jitter."""
        sr = 96000
        n = sr * 6
        t = np.arange(n) / float(sr)
        rng = np.random.default_rng(2401)
        white = rng.standard_normal((n, 2))
        sos_body = signal.butter(3, 18000.0, btype="low", fs=sr, output="sos")
        body = signal.sosfilt(sos_body, white, axis=0)
        sos_ultra = signal.butter(2, [18000.0, 42000.0], btype="band", fs=sr, output="sos")
        ultra = signal.sosfilt(sos_ultra, rng.standard_normal((n, 2)), axis=0)
        burst_env = (0.15 + 0.85 * (np.sin(2.0 * np.pi * 1.7 * t) ** 4))[:, None]
        mix = body + 0.12 * ultra * burst_env
        mix /= np.max(np.abs(mix)) * 1.15

        path = self.base_path / "genuino_96k_transitorios_ultrasonicos.flac"
        sf.write(path, mix, sr, subtype="PCM_24")
        res = motor_flac.analyze_file_data(str(path), use_cache=False)

        self.assertLessEqual(res["esp"]["iqr_corte_hz"], 550.0)
        self.assertEqual(res["veredicto"], "GENUINE LOSSLESS")

    def test_25_upper_band_psychoacoustic_modulation_20k(self):
        """Frame-adaptive starvation above 20 kHz is caught while a static LTI filter is spared."""
        sr = 44100
        n = sr * 6
        t = np.arange(n) / float(sr)
        rng = np.random.default_rng(2501)
        white = rng.standard_normal((n, 2))
        sos_lo = signal.ellip(10, 0.2, 85.0, 19800.0, btype="low", fs=sr, output="sos")
        base_band = signal.sosfilt(sos_lo, white, axis=0)
        sos_hi = signal.butter(4, [20100.0, 21800.0], btype="band", fs=sr, output="sos")
        top_band = signal.sosfilt(sos_hi, rng.standard_normal((n, 2)), axis=0)

        gate = (np.sin(2.0 * np.pi * 2.3 * t) > 0.35).astype(np.float64)[:, None]
        modulated = base_band * 0.25 + top_band * (0.22 * gate + 0.0002 * (1.0 - gate))
        modulated /= np.max(np.abs(modulated)) * 1.10

        path_mod = self.base_path / "falso_modulacion_psicoacustica_20k.flac"
        sf.write(path_mod, modulated, sr, subtype="PCM_16")
        res_mod = motor_flac.analyze_file_data(str(path_mod), use_cache=False)

        self.assertIs(res_mod["esp"]["modulacion_psicoacustica_20k"], True)
        self.assertGreaterEqual(res_mod["esp"]["brecha_modulacion_20k_db"], 10.0)
        self.assertNotEqual(res_mod["veredicto"], "GENUINE LOSSLESS")

        static_lti = base_band / (np.max(np.abs(base_band)) * 1.10)
        path_lti = self.base_path / "control_lti_estatico_19k8.flac"
        sf.write(path_lti, static_lti, sr, subtype="PCM_16")
        res_lti = motor_flac.analyze_file_data(str(path_lti), use_cache=False)
        self.assertIs(res_lti["esp"]["modulacion_psicoacustica_20k"], False)

    @staticmethod
    def _imdct_overlap_add(coeffs, window):
        """Exact TDAC synthesis inverse of `_motor_espectral._fast_mdct_f32`."""
        from scipy import fft as _sp_fft
        n_frames, n = coeffs.shape
        n2 = n // 2
        two_n = 2 * n
        u = _sp_fft.dct(coeffs.astype(np.float32), type=4, axis=-1, norm="ortho", workers=1)
        y = np.empty((n_frames, two_n), dtype=np.float32)
        y[:, :n2] = u[:, n2:]
        y[:, n2:n] = -u[:, -1 : n2 - 1 : -1]
        y[:, n : n + n2] = -u[:, n2 - 1 :: -1]
        y[:, n + n2 :] = -u[:, :n2]
        y *= window
        out = np.zeros((n_frames + 1) * n, dtype=np.float32)
        for m in range(n_frames):
            out[m * n : m * n + two_n] += y[m]
        return out

    def test_26_mdct_1024_lapped_lattice_vorbis_aac_detected(self):
        """Phase 11: full-bandwidth MDCT-1024 lapped quantization lattice is caught."""
        from _motor_espectral import _fast_mdct_f32, _get_vorbis_2048

        sr = 44100
        rng = np.random.default_rng(2601)
        w = _get_vorbis_2048()
        n_frames = 190
        raw_pcm = rng.standard_normal((2, (n_frames + 2) * 1024)).astype(np.float32)
        k_lo = int(12000.0 / (sr / 2.0) * 1024)
        k_hi = int(20500.0 / (sr / 2.0) * 1024)
        # Shared psychoacoustic masking lattice across stereo channels, shifted by phi_0 = 37
        mask_zero = rng.random((n_frames, k_hi - k_lo)) < 0.45
        transcoded_ch = []
        for ch in range(2):
            frames = np.lib.stride_tricks.sliding_window_view(raw_pcm[ch], 2048)[::1024][:n_frames]
            c = _fast_mdct_f32(frames, w)
            c[:, k_lo:k_hi] = np.where(mask_zero, 0.0, c[:, k_lo:k_hi] * np.sqrt(1.0 / 0.55))
            sig_ch = self._imdct_overlap_add(c, w)
            transcoded_ch.append(sig_ch[1024 + 37 : 1024 + 37 + sr * 4])

        fake_sig = np.column_stack(transcoded_ch).astype(np.float64)
        fake_sig /= np.max(np.abs(fake_sig)) * 1.15
        path_fake = self.base_path / "falso_mdct_1024_vorbis_aac_fullband.flac"
        sf.write(path_fake, fake_sig, sr, subtype="PCM_16")
        res_fake = motor_flac.analyze_file_data(str(path_fake), use_cache=False)

        self.assertIs(res_fake["esp"]["mdct_lattice_detected"], True)
        self.assertEqual(res_fake["esp"]["mdct_lattice_family"], "MDCT-1024 (Vorbis/AAC)")
        self.assertGreaterEqual(res_fake["esp"]["mdct_lattice_snr"], 10.0)
        self.assertEqual(res_fake["veredicto"], "PROBABLE UPSCALE")
        self.assertIn("lapped transform quantization lattice", " ".join(res_fake["problemas"]))

        gen_sig = rng.standard_normal((sr * 4, 2))
        gen_sig /= np.max(np.abs(gen_sig)) * 1.15
        path_gen = self.base_path / "control_genuino_sin_reticula_mdct.flac"
        sf.write(path_gen, gen_sig, sr, subtype="PCM_16")
        res_gen = motor_flac.analyze_file_data(str(path_gen), use_cache=False)
        self.assertIs(res_gen["esp"]["mdct_lattice_detected"], False)

    def test_27_pqmf_576_hybrid_lattice_mp3_detected(self):
        """Phase 11: MP3 32-band PQMF + 18-point MDCT hybrid granule lattice is caught."""
        sr = 44100
        rng = np.random.default_rng(2701)
        h = signal.firwin(512, 1.0 / 64.0, window=("kaiser", 9.0)).astype(np.float32)
        n_idx = np.arange(512, dtype=np.float32)
        win36 = np.sin(np.pi / 36.0 * (np.arange(36, dtype=np.float32) + 0.5)).astype(np.float32)

        n_granules = 160
        m_sub = (n_granules + 1) * 18
        n_pcm = m_sub * 32 + 1024
        # Base low-band (<14 kHz) plus PQMF-synthesized upper subbands (22..29)
        sos_low = signal.butter(6, 13800.0, btype="low", fs=sr, output="sos")
        low_band = signal.sosfilt(sos_low, rng.standard_normal(n_pcm)).astype(np.float32)
        high_band = np.zeros(n_pcm, dtype=np.float32)

        for s in range(22, 30):
            coeffs = rng.standard_normal((n_granules, 18)).astype(np.float32)
            mask_z = rng.random(coeffs.shape) < 0.50
            coeffs = np.where(mask_z, 0.0, coeffs * np.sqrt(2.0))
            sub_sig = self._imdct_overlap_add(coeffs, win36)[:m_sub]
            if s % 2 == 1:
                sub_sig[1::2] *= -1.0
            upsampled = np.zeros(m_sub * 32, dtype=np.float32)
            upsampled[::32] = sub_sig * 32.0
            synth_filter = (h * np.cos(np.pi / 64.0 * (2 * s + 1) * (n_idx - 16)).astype(np.float32))[::-1]
            recon_s = np.convolve(upsampled, synth_filter, mode="full")
            high_band[: recon_s.size] += recon_s

        mono_mp3 = low_band * 0.4 + high_band * 0.6
        # Shift by an arbitrary granule phase phi_0 = 213 samples
        mono_mp3 = mono_mp3[213:]
        mono_mp3 /= np.max(np.abs(mono_mp3)) * 1.15

        det = motor_flac._detect_lapped_codec_grid(mono_mp3, sr)
        self.assertIs(det["mdct_lattice_detected"], True)
        self.assertEqual(det["mdct_lattice_family"], "PQMF-576 (MP3)")
        self.assertGreaterEqual(det["mdct_lattice_snr"], 12.0)

    def test_28_celt_960_opus_tdac_stopband_detected_vs_lti_brickwall(self):
        """Phase 11: Opus CELT-960 20 kHz TDAC stopband cancellation is caught while static LTI 20 kHz is spared."""
        from _motor_espectral import _fast_mdct_f32, _get_celt_1920

        sr = 48000
        rng = np.random.default_rng(2801)
        w_celt = _get_celt_1920()
        n_frames = 60
        raw_frames = rng.standard_normal((n_frames, 1920)).astype(np.float32)
        c = _fast_mdct_f32(raw_frames, w_celt)
        # CELT fullband stops allocating PVQ pulses at bin 800 (20.00 kHz at 48 kHz)
        c[:, 800:] = 0.0
        pre_sig = self._imdct_overlap_add(c, w_celt)
        # CELT synthesis deemphasis filter H(z) = 1 / (1 - 0.85 z^-1), shifted by phi_0 = 53
        opus_mono = signal.lfilter([1.0], [1.0, -0.85], pre_sig).astype(np.float32)[53:]
        opus_mono /= np.max(np.abs(opus_mono)) * 1.15

        det_opus = motor_flac._detect_lapped_codec_grid(opus_mono, sr)
        self.assertIs(det_opus["mdct_lattice_detected"], True)
        self.assertEqual(det_opus["mdct_lattice_family"], "CELT-960 (Opus)")
        self.assertGreaterEqual(det_opus["mdct_lattice_snr"], 6.5)

        # Control: static LTI brickwall filter at the exact same 20.00 kHz cut-off
        white = rng.standard_normal(opus_mono.size).astype(np.float32)
        spec = np.fft.rfft(white)
        freqs = np.fft.rfftfreq(white.size, 1.0 / sr)
        spec[freqs > 20000.0] *= 1e-4
        lti_mono = np.fft.irfft(spec, n=white.size).astype(np.float32)
        det_lti = motor_flac._detect_lapped_codec_grid(lti_mono, sr)
        self.assertIs(det_lti["mdct_lattice_detected"], False)

    def test_29_seamless_sbr_qmf_replication_44k_and_96k_detected_vs_harmonic_synth(self):
        """Phase 12: seamless QMF subband transposition without a boundary notch is caught at 44.1 and 96 kHz."""
        sr = 44100
        rng = np.random.default_rng(2901)
        n = sr * 6
        t = np.arange(n, dtype=np.float64) / sr

        # Musical master with chord modulation and percussive transients
        base = rng.standard_normal((n, 2)) * 0.15
        for f0 in (110.0, 164.81, 220.0, 329.63, 440.0, 587.33):
            for k in range(1, 34):
                fk = f0 * k
                if fk >= 19500.0:
                    break
                amp = (1.0 / (k ** 0.85)) * (0.5 + 0.5 * np.sin(2.0 * np.pi * (0.6 + 0.15 * (k % 5)) * t))
                base[:, 0] += amp * np.sin(2.0 * np.pi * fk * t + rng.uniform(0.0, 6.28))
                base[:, 1] += amp * np.sin(2.0 * np.pi * fk * t + rng.uniform(0.0, 6.28))
        master = base / (np.max(np.abs(base)) * 1.15)

        # Seamless SBR: copy 5.625-14.875 kHz into 11.25-20.5 kHz with matched boundary gain (zero notch)
        n_fft, hop = 2048, 1024
        df = sr / float(n_fft)
        k_cross = int(11250.0 / df)
        k_top = int(20500.0 / df)
        patch_w = k_top - k_cross
        k_src = k_cross // 2
        sbr_44 = np.zeros_like(master)
        for ch in range(2):
            _, _, z_stft = signal.stft(master[:, ch], fs=sr, nperseg=n_fft, noverlap=n_fft - hop)
            z_sbr = z_stft.copy()
            z_sbr[k_cross:, :] *= 1e-4
            src_patch = z_stft[k_src : k_src + patch_w, :]
            e_below = np.sqrt(np.mean(np.abs(z_stft[k_cross - 3 : k_cross, :]) ** 2, axis=0, keepdims=True)) + 1e-12
            e_start = np.sqrt(np.mean(np.abs(src_patch[:3, :]) ** 2, axis=0, keepdims=True)) + 1e-12
            tilt = 10.0 ** (np.linspace(-0.5, -9.0, patch_w)[:, None] / 20.0)
            noise_c = (rng.standard_normal(src_patch.shape) + 1j * rng.standard_normal(src_patch.shape)) * np.abs(src_patch) * 0.22
            z_sbr[k_cross : k_cross + patch_w, :] = (src_patch * 0.94 + noise_c) * (e_below / e_start) * tilt
            _, rec = signal.istft(z_sbr, fs=sr, nperseg=n_fft, noverlap=n_fft - hop)
            sbr_44[:, ch] = rec[:n]
        sbr_44 /= np.max(np.abs(sbr_44)) * 1.15

        path_sbr = self.base_path / "falso_sbr_qmf_sin_muesca_44k.flac"
        sf.write(path_sbr, sbr_44, sr, subtype="PCM_16")
        res_sbr = motor_flac.analyze_file_data(str(path_sbr), use_cache=False)
        self.assertIs(res_sbr["esp"]["sospecha_excitador_armonico"], False)
        self.assertIs(res_sbr["esp"]["sbr_qmf_detectado"], True)
        self.assertGreaterEqual(res_sbr["esp"]["sbr_qmf_z"], 12.0)
        self.assertEqual(res_sbr["veredicto"], "PROBABLE UPSCALE")

        # Upsampled to 96 kHz with ultrasonic dither masking: QMF replication still triggers
        sbr_96_mono = signal.resample_poly(0.5 * (sbr_44[:, 0] + sbr_44[:, 1]), 320, 147).astype(np.float32)
        sbr_96_mono += (rng.standard_normal(sbr_96_mono.size) * 1e-4).astype(np.float32)
        det_96 = motor_flac._detect_sbr_qmf_replication(sbr_96_mono, 96000)
        self.assertIs(det_96["sbr_qmf_detectado"], True)

        # Control: dense polyphonic sawtooth chord (multiplicative harmonics f -> k*f0, not f + delta_f)
        saw = np.zeros(n, dtype=np.float64)
        for f0 in (110.0, 164.81, 220.0, 277.18, 329.63, 440.0, 659.25):
            for k in range(1, int(21500 // f0)):
                saw += (1.0 / k) * np.sin(2.0 * np.pi * k * f0 * t)
        saw *= 0.2 + 0.8 * (np.sin(2.0 * np.pi * 3.1 * t) ** 8)
        saw = (saw / (np.max(np.abs(saw)) * 1.15)).astype(np.float32)
        det_saw = motor_flac._detect_sbr_qmf_replication(saw, sr)
        self.assertIs(det_saw["sbr_qmf_detectado"], False)

    def test_30_musepack_pqmf32_pure_subband_frame_lattice_detected(self):
        """Phase 12: Musepack PQMF-32 pure subband quantization lattice is caught without false positives."""
        sr = 44100
        rng = np.random.default_rng(3001)
        n = sr * 5
        h = signal.firwin(512, 1.0 / 64.0, window=("kaiser", 9.0)).astype(np.float64)
        n_idx = np.arange(512, dtype=np.float64)
        src = rng.standard_normal(n) * 0.25
        recon = np.zeros(n + 1024, dtype=np.float64)
        for s in range(31):
            ana = h * np.cos(np.pi / 64.0 * (2 * s + 1) * (n_idx - 16))
            syn = ana[::-1]
            sb = np.convolve(src, ana, mode="full")[511 : 511 + n]
            dec = sb[::32].copy()
            if s >= 18:
                n_blk = len(dec) // 12
                for b_i in range(n_blk):
                    sl = dec[b_i * 12 : (b_i + 1) * 12]
                    if s >= 22 and rng.random() < 0.35:
                        dec[b_i * 12 : (b_i + 1) * 12] = 0.0
                    else:
                        levels = 7.0 if s >= 24 else 15.0
                        step = (np.max(np.abs(sl)) + 1e-12) / levels
                        dec[b_i * 12 : (b_i + 1) * 12] = np.round(sl / step) * step
            up = np.zeros(len(dec) * 32, dtype=np.float64)
            up[::32] = dec * 32.0
            r_s = np.convolve(up, syn, mode="full")
            recon[: len(r_s)] += r_s
        mpc_mono = (recon[19 : 19 + n] / (np.max(np.abs(recon)) * 1.15)).astype(np.float32)

        det_mpc = motor_flac._detect_lapped_codec_grid(mpc_mono, sr)
        self.assertIs(det_mpc["mdct_lattice_detected"], True)
        self.assertEqual(det_mpc["mdct_lattice_family"], "PQMF-32 (Musepack)")
        self.assertGreaterEqual(det_mpc["mdct_lattice_snr"], 6.5)

        # Controls: sloping Butterworth low-pass and 384-sample hard-gated staccato bursts
        sloped = signal.sosfilt(
            signal.butter(4, 15000.0, btype="low", fs=sr, output="sos"),
            rng.standard_normal(n),
        ).astype(np.float32)
        self.assertIs(motor_flac._detect_lapped_codec_grid(sloped, sr)["mdct_lattice_detected"], False)

        staccato = rng.standard_normal(n).astype(np.float32) * 0.3
        for idx in range(0, n - 384, 384):
            if rng.random() < 0.5:
                staccato[idx : idx + 384] = 0.0
        self.assertIs(motor_flac._detect_lapped_codec_grid(staccato, sr)["mdct_lattice_detected"], False)

    def test_31_vintage_archival_band_limited_master_spared_while_mdct_transcode_caught(self):
        """Phase 12: bright vintage master (<15 kHz ceiling, >2 kHz centroid) is spared unless transcoded."""
        from _motor_espectral import _fast_mdct_f32, _get_vorbis_2048

        sr = 44100
        rng = np.random.default_rng(3101)
        n = sr * 5
        raw = rng.standard_normal((n, 2))
        bright = signal.sosfilt(
            signal.butter(2, [1400.0, 5200.0], btype="band", fs=sr, output="sos"),
            raw, axis=0,
        ) * 2.0 + raw * 0.25
        vintage = signal.sosfilt(
            signal.butter(4, 6500.0, btype="low", fs=sr, output="sos"),
            bright, axis=0,
        )
        tape_hiss = rng.standard_normal((n, 2)) * (10.0 ** (-82.0 / 20.0))
        vintage = (vintage + tape_hiss) / (np.max(np.abs(vintage)) * 1.15)

        path_gen = self.base_path / "genuino_archivo_analogico_1950s.flac"
        sf.write(path_gen, vintage, sr, subtype="PCM_16")
        res_gen = motor_flac.analyze_file_data(str(path_gen), use_cache=False)

        self.assertGreater(res_gen["esp"]["centroide_hz"], 2000.0)
        self.assertLess(res_gen["esp"]["techo_rel_nyquist"], 0.70)
        self.assertIs(res_gen["esp"]["decaimiento_acustico_suave"], False)
        self.assertIs(res_gen["esp"]["rolloff_banda_limitada_genuina"], True)
        self.assertIs(res_gen["esp"]["mdct_lattice_detected"], False)
        self.assertEqual(res_gen["veredicto"], "PROBABLY LOSSLESS")

        # Transcode that exact vintage recording through MDCT-1024 passband quantization
        w = _get_vorbis_2048()
        nyq = sr / 2.0
        k_lo, k_hi = int(6000.0 / nyq * 1024), int(11500.0 / nyq * 1024)
        trans_ch = []
        for ch in range(2):
            padded = np.pad(vintage[:, ch].astype(np.float32), (2048, 2048))
            n_fr = (padded.size - 2048) // 1024
            frames = np.lib.stride_tricks.sliding_window_view(padded, 2048)[::1024][:n_fr]
            c = _fast_mdct_f32(frames, w)
            z_mask = rng.random((n_fr, k_hi - k_lo)) < 0.42
            c[:, k_lo:k_hi] = np.where(z_mask, 0.0, c[:, k_lo:k_hi] * np.sqrt(1.0 / 0.58))
            rec = self._imdct_overlap_add(c, w)
            trans_ch.append(rec[2048 + 41 : 2048 + 41 + n])
        fake_vintage = np.column_stack(trans_ch).astype(np.float64)
        fake_vintage /= np.max(np.abs(fake_vintage)) * 1.15

        path_fake = self.base_path / "falso_archivo_1950s_transcodificado_mdct.flac"
        sf.write(path_fake, fake_vintage, sr, subtype="PCM_16")
        res_fake = motor_flac.analyze_file_data(str(path_fake), use_cache=False)

        self.assertIs(res_fake["esp"]["mdct_lattice_detected"], True)
        self.assertEqual(res_fake["veredicto"], "PROBABLE UPSCALE")

    def test_32_aac_kbd_window_and_asymmetric_stereo_lapped_lattice_detected(self):
        """Phase 13: MPEG-4 AAC KBD (alpha=4) window and R-channel asymmetric lattice are caught."""
        from _motor_espectral import _fast_mdct_f32, _get_kbd_2048

        w_kbd = _get_kbd_2048()
        self.assertEqual(w_kbd.shape, (2048,))
        tdac_err = float(np.max(np.abs(w_kbd[:1024] ** 2 + w_kbd[1023::-1] ** 2 - 1.0)))
        self.assertLess(tdac_err, 1e-6)

        sr = 44100
        rng = np.random.default_rng(3201)
        n = sr * 5
        master = _create_track_reference(sr, duration=5.0, seed=3201)

        # Left channel is gentle low-passed ambience; Right channel carries AAC KBD-1024 quantized programme
        left_muffled = signal.sosfilt(
            signal.butter(4, 11000.0, btype="low", fs=sr, output="sos"),
            master[:, 0],
        )
        nyq = sr / 2.0
        k_lo, k_hi = int(12500.0 / nyq * 1024), int(20200.0 / nyq * 1024)
        padded_r = np.pad(master[:, 1].astype(np.float32), (2048, 2048))
        n_fr = (padded_r.size - 2048) // 1024
        frames_r = np.lib.stride_tricks.sliding_window_view(padded_r, 2048)[::1024][:n_fr]
        c_r = _fast_mdct_f32(frames_r, w_kbd)
        mask_r = rng.random((n_fr, k_hi - k_lo)) < 0.45
        c_r[:, k_lo:k_hi] = np.where(mask_r, 0.0, c_r[:, k_lo:k_hi] * np.sqrt(1.0 / 0.55))
        rec_r = self._imdct_overlap_add(c_r, w_kbd)[2048 + 73 : 2048 + 73 + n]

        stereo_asym = np.column_stack([left_muffled * 0.35, rec_r.astype(np.float64)])
        stereo_asym /= np.max(np.abs(stereo_asym)) * 1.15

        path_aac = self.base_path / "falso_aac_kbd_asimetrico_r.flac"
        sf.write(path_aac, stereo_asym, sr, subtype="PCM_16")
        res = motor_flac.analyze_file_data(str(path_aac), use_cache=False)

        self.assertIs(res["esp"]["mdct_lattice_detected"], True)
        self.assertIn("MDCT-1024", res["esp"]["mdct_lattice_family"])
        self.assertEqual(res["veredicto"], "PROBABLE UPSCALE")

    def test_33_stationary_synth_drone_protected_and_single_soft_rule_capped_at_suspicious(self):
        """Phase 13: stationary electronic synth drones are protected and isolated soft rules cap at SUSPICIOUS."""
        sr = 44100
        duration = 5.0
        n = int(sr * duration)
        t = np.arange(n, dtype=np.float64) / sr
        rng = np.random.default_rng(3301)

        # Sustained modular sawtooth/pulse synth drone with discrete overtones up to 21 kHz
        drone = np.zeros((n, 2), dtype=np.float64)
        for f0 in (660.0, 990.0):
            for k in range(1, int(21200.0 // f0) + 1):
                fk = f0 * k
                amp = 1.0 / (k ** 0.75)
                drone[:, 0] += amp * np.sin(2.0 * np.pi * fk * t + 0.3 * k)
                drone[:, 1] += amp * np.cos(2.0 * np.pi * fk * t + 0.5 * k)
        # Subtle analogue dither floor (-84 dBFS)
        drone += rng.standard_normal((n, 2)) * (10.0 ** (-84.0 / 20.0))
        drone /= np.max(np.abs(drone)) * 1.15

        path_drone = self.base_path / "genuino_synth_drone_estacionario.flac"
        sf.write(path_drone, drone, sr, subtype="PCM_16")
        res_drone = motor_flac.analyze_file_data(str(path_drone), use_cache=False)

        self.assertIs(res_drone["esp"]["es_estacionario"], True)
        self.assertIs(res_drone["esp"]["islas_mdct_sfb21"], False)
        self.assertIs(res_drone["esp"]["modulacion_psicoacustica_20k"], False)
        self.assertEqual(res_drone["veredicto"], "GENUINE LOSSLESS")

        # Multi-family corroboration check: a single isolated soft heuristic is capped at SUSPICIOUS
        meta_cd = {"sample_rate": 44100, "bits_per_sample": 16, "canales": 2}
        spec_single = dict(res_drone["esp"])
        spec_single["es_estacionario"] = False
        spec_single["islas_mdct_sfb21"] = True
        spec_single["brecha_p90_p10_agudos_db"] = 48.0
        score_1, verd_1, _ = motor_flac.calculate_score(meta_cd, spec_single, {"aplica": False})
        self.assertEqual(spec_single["familias_evidencia_lossy"], ["temporal_modulation"])
        self.assertIs(spec_single["corroboracion_multifamilia"], False)
        self.assertEqual(verd_1, "SUSPICIOUS")
        self.assertGreaterEqual(score_1, 0.50)

        # Adding a second independent family (stereo_collapse) corroborates and triggers PROBABLE UPSCALE
        spec_two = dict(spec_single)
        spec_two["sospecha_joint_stereo"] = True
        spec_two["caida_side_agudos_db"] = 24.0
        spec_two["delta_corte_ms_hz"] = 2800.0
        score_2, verd_2, _ = motor_flac.calculate_score(meta_cd, spec_two, {"aplica": False})
        self.assertIs(spec_two["corroboracion_multifamilia"], True)
        self.assertEqual(verd_2, "PROBABLE UPSCALE")
        self.assertLess(score_2, 0.50)

    def test_34_mode_seconds_and_32bit_flac_streaming_audit(self):
        """Phase 13: --mode seconds restricts the spectral window and 32-bit integer FLAC is audited."""
        import shutil
        import subprocess

        sr = 44100
        # 12-second track (2 blocks of ~6s): first 4s clean broadband, second 6s louder 14.5 kHz brickwall
        clean_part = _create_track_reference(sr, duration=6.0, seed=3401) * 0.35
        loud_raw = _create_track_reference(sr, duration=6.0, seed=3402) * 0.95
        sos_14k = signal.ellip(10, 0.1, 80.0, 14500.0, btype="low", fs=sr, output="sos")
        cut_part = signal.sosfilt(sos_14k, loud_raw, axis=0)
        combined = np.vstack([clean_part, cut_part])
        combined = np.clip(combined, -0.98, 0.98)

        path_mode = self.base_path / "pista_hibrida_modo_seconds.flac"
        sf.write(path_mode, combined, sr, subtype="PCM_16")

        res_sec = motor_flac.analyze_file_data(
            str(path_mode), mode="seconds", seconds=4, use_cache=False
        )
        res_full = motor_flac.analyze_file_data(
            str(path_mode), mode="full", use_cache=False
        )
        self.assertEqual(res_sec["veredicto"], "GENUINE LOSSLESS")
        self.assertEqual(res_full["veredicto"], "PROBABLE UPSCALE")

        # 32-bit integer FLAC streaming + bit-depth audit (requires ffmpeg)
        if not shutil.which("ffmpeg"):
            self.skipTest("ffmpeg is required to encode 32-bit integer FLAC fixtures")

        wav_16 = self.base_path / "tmp_16.wav"
        flac_32_pad = self.base_path / "falso_16_en_32bit.flac"
        sf.write(wav_16, clean_part[: sr * 4], sr, subtype="PCM_16")
        subprocess.run(
            [
                "ffmpeg", "-y", "-v", "error", "-i", str(wav_16),
                "-strict", "experimental", "-c:a", "flac", "-sample_fmt", "s32",
                str(flac_32_pad),
            ],
            check=True,
        )
        res_32 = motor_flac.analyze_file_data(str(flac_32_pad), use_cache=False)
        self.assertIsNone(res_32.get("error"))
        self.assertEqual(res_32["meta"]["bits_per_sample"], 32)
        self.assertEqual(res_32["md5"]["estado"], "match")
        self.assertEqual(res_32["bdi"]["lsbs"], "empty")
        self.assertEqual(res_32["bdi"]["resolucion_efectiva_bits"], 16)
        self.assertIs(res_32["bdi"]["forense_bits"]["es_padded_16_en_24"], True)
        self.assertIn(res_32["veredicto"], BAD_VERDICTS)

        # Genuine 24-bit audio inside a 32-bit FLAC container (8 trailing zeros, not 16)
        wav_24 = self.base_path / "tmp_24.wav"
        flac_24_in_32 = self.base_path / "genuino_24_en_32bit.flac"
        rng_24 = np.random.default_rng(3409)
        sig_24 = np.clip(
            clean_part[: sr * 4] * 2.5 + rng_24.normal(0.0, 10.0 ** (-115.0 / 20.0), (sr * 4, 2)),
            -0.98,
            0.98,
        )
        sig_24_q = np.rint(sig_24 * 8388608.0) / 8388608.0
        sf.write(wav_24, sig_24_q, sr, subtype="PCM_32")
        subprocess.run(
            [
                "ffmpeg", "-y", "-v", "error", "-i", str(wav_24),
                "-strict", "experimental", "-c:a", "flac", "-sample_fmt", "s32",
                str(flac_24_in_32),
            ],
            check=True,
        )
        res_24_in_32 = motor_flac.analyze_file_data(str(flac_24_in_32), use_cache=False)
        self.assertIsNone(res_24_in_32.get("error"))
        self.assertEqual(res_24_in_32["md5"]["estado"], "match")
        self.assertEqual(res_24_in_32["bdi"]["lsbs"], "active")
        self.assertEqual(res_24_in_32["bdi"]["resolucion_efectiva_bits"], 24)
        self.assertEqual(res_24_in_32["bdi"]["forense_bits"]["trailing_zeros_lsb"], 8)
        self.assertIs(res_24_in_32["bdi"]["forense_bits"]["es_padded_16_en_24"], False)
        self.assertEqual(res_24_in_32["veredicto"], "GENUINE LOSSLESS")

    def test_35_aac_sine_window_and_unbiased_right_channel_lattice_detected(self):
        """Phase 13.2: MPEG-2/4 AAC Sine window, unbiased R-channel choice and active subwindow selection."""
        from _motor_espectral import _fast_mdct_f32, _get_sine_2048

        w_sine = _get_sine_2048()
        self.assertEqual(w_sine.shape, (2048,))
        tdac_err = float(np.max(np.abs(w_sine[:1024] ** 2 + w_sine[1023::-1] ** 2 - 1.0)))
        self.assertLess(tdac_err, 1e-6)

        sr = 44100
        rng = np.random.default_rng(3501)
        n = motor_flac.FRAMES_BY_BLOCK
        master = _create_track_reference(sr, duration=n / float(sr), seed=3501)[:n]

        # Right channel has AAC Sine-2048 MDCT lattice in the first 70 % and a quiet gap in the center/end,
        # while Left channel has ~75 % of Right channel energy (el < er, but el >= 0.5 * er).
        nyq = sr / 2.0
        k_lo, k_hi = int(12500.0 / nyq * 1024), int(20200.0 / nyq * 1024)
        padded_r = np.pad(master[:, 1].astype(np.float32), (2048, 2048))
        n_fr = (padded_r.size - 2048) // 1024
        frames_r = np.lib.stride_tricks.sliding_window_view(padded_r, 2048)[::1024][:n_fr]
        c_r = _fast_mdct_f32(frames_r, w_sine)
        mask_r = rng.random((n_fr, k_hi - k_lo)) < 0.48
        c_r[:, k_lo:k_hi] = np.where(mask_r, 0.0, c_r[:, k_lo:k_hi] * np.sqrt(1.0 / 0.52))
        rec_r = self._imdct_overlap_add(c_r, w_sine)[2048 + 59 : 2048 + 59 + n].astype(np.float64)

        # Introduce a silence gap in the middle-to-late segment so center-slicing would dilute SNR,
        # while the leading 196608-sample subwindow stays active.
        rec_r[200000:] *= 0.01
        clean_l = master[:, 0].astype(np.float64)
        clean_l[200000:] *= 0.01
        scale_l = np.sqrt(0.65 * np.mean(rec_r ** 2) / max(np.mean(clean_l ** 2), 1e-12))
        stereo = np.column_stack([clean_l * scale_l, rec_r])
        stereo /= np.max(np.abs(stereo)) * 1.10

        acc = motor_flac._SpectralAccumulator(sr, 2)
        acc.add(stereo, 0)
        spec = acc.result()
        self.assertIs(spec["mdct_lattice_detected"], True)
        self.assertEqual(spec["mdct_lattice_family"], "MDCT-1024 (Vorbis/AAC)")

    def test_36_pqmf_kaiser_exact_symmetry_and_mp3_not_misattributed_as_musepack(self):
        """Phase 13.2: PQMF 512-tap prototype is symmetric around tap 256 and MP3 stays PQMF-576."""
        bank_fft, win36 = motor_flac._get_pqmf_fast(4096)
        self.assertEqual(bank_fft.shape, (11, 2049))
        self.assertEqual(win36.shape, (36,))
        bank_time = np.fft.irfft(bank_fft, n=4096, axis=1)[:, :512]
        self.assertLess(float(np.max(np.abs(bank_time[:, 0]))), 1e-6)

        m_tap = np.arange(512, dtype=np.float64) - 256.0
        win_kaiser = np.concatenate(([0.0], np.kaiser(511, 9.0)))
        h_raw = (1.0 / 64.0) * np.sinc(m_tap / 64.0) * win_kaiser
        h_proto = (h_raw / np.sum(h_raw)).astype(np.float64)
        self.assertEqual(float(h_proto[0]), 0.0)
        sym_err = float(np.max(np.abs(h_proto[1:] - h_proto[1:][::-1])))
        self.assertLess(sym_err, 1e-12)

        # Synthesize an MP3-like 576-sample granule lattice (18-point MDCT per subband)
        # with alternating granule energy so 36-sample (2-granule) periodicity is also present,
        # verifying that it stays classified as PQMF-576 (MP3) rather than PQMF-32 (Musepack).
        sr = 44100
        rng = np.random.default_rng(3601)
        h = h_proto.astype(np.float32)
        n_idx = np.arange(512, dtype=np.float32)
        n_granules = 160
        m_sub = (n_granules + 1) * 18
        n_pcm = m_sub * 32 + 1024
        sos_low = signal.butter(6, 13800.0, btype="low", fs=sr, output="sos")
        low_band = signal.sosfilt(sos_low, rng.standard_normal(n_pcm)).astype(np.float32)
        high_band = np.zeros(n_pcm, dtype=np.float32)
        for s in range(19, 30):
            coeffs = rng.standard_normal((n_granules, 18)).astype(np.float32)
            if s >= 22:
                mask_z = rng.random(coeffs.shape) < 0.50
                coeffs = np.where(mask_z, 0.0, coeffs * np.sqrt(2.0))
                coeffs[1::2] *= 0.15
            sub_sig = self._imdct_overlap_add(coeffs, win36)[:m_sub]
            if s % 2 == 1:
                sub_sig[1::2] *= -1.0
            upsampled = np.zeros(m_sub * 32, dtype=np.float32)
            upsampled[::32] = sub_sig * 32.0
            synth_filter = (h * np.cos(np.pi / 64.0 * (2 * s + 1) * (n_idx - 16)).astype(np.float32))[::-1]
            recon_s = np.convolve(upsampled, synth_filter, mode="full")
            high_band[: recon_s.size] += recon_s
        mp3_mono = (low_band * 0.4 + high_band * 0.6)[213:]
        mp3_mono /= float(np.max(np.abs(mp3_mono)) * 1.15)

        det = motor_flac._detect_lapped_codec_grid(mp3_mono, sr)
        self.assertIs(det["mdct_lattice_detected"], True)
        self.assertEqual(det["mdct_lattice_family"], "PQMF-576 (MP3)")

    def test_37_celt960_44100_polyphase_resampler_and_gated_chord_sbr_immunity(self):
        """Phase 13.2: 44.1 kHz brickwall master avoids CELT-960 ringing and gated chords avoid false SBR."""
        sr = 44100
        rng = np.random.default_rng(3701)
        n = motor_flac.FRAMES_BY_BLOCK
        t = np.arange(n, dtype=np.float64) / sr

        # 1. 44.1 kHz broadband master with a brickwall low-pass at 20.0 kHz
        raw = rng.standard_normal(n)
        sos_20k = signal.ellip(12, 0.1, 85.0, 20000.0, btype="low", fs=sr, output="sos")
        brick_44k = signal.sosfilt(sos_20k, raw).astype(np.float32)
        brick_44k /= float(np.max(np.abs(brick_44k)) * 1.10)
        det_celt = motor_flac._detect_lapped_codec_grid(brick_44k, sr, ceiling_hz=20000.0)
        self.assertIs(det_celt["mdct_lattice_detected"], False)

        # 2. Sidechain-pumped harmonic sawtooth synth chord across two non-adjacent blocks
        chord = np.zeros(n, dtype=np.float64)
        for f0 in (110.0, 164.81, 220.0, 277.18, 329.63, 440.0, 659.25):
            for k in range(1, int(21500.0 // f0)):
                chord += (1.0 / k) * np.sin(2.0 * np.pi * (f0 * k) * t)
        # Trance gate modulating all harmonics simultaneously (strong common-mode envelope)
        gate = 0.2 + 0.8 * (np.sin(2.0 * np.pi * 3.1 * t) ** 8)
        gated_chord = (chord * gate).astype(np.float32)
        gated_chord /= float(np.max(np.abs(gated_chord)) * 1.15)

        sbr_res = motor_flac._detect_sbr_qmf_replication([gated_chord, gated_chord[::-1].copy()], sr)
        self.assertIs(sbr_res["sbr_qmf_detectado"], False)

    def test_38_wide_midrange_synth_centered_hf_drums_spared_and_album_coherence_quorum(self):
        """Phase 13.2: wide midrange with centered HF drums is spared Joint-Stereo and album quorum scales."""
        sr = 44100
        rng = np.random.default_rng(3801)
        n = sr * 5

        # Wide stereo midrange (4-14 kHz strong in Side) + centered HF percussion up to 20.5 kHz in both M and S
        mid_sig = signal.sosfilt(
            signal.butter(4, [200.0, 20500.0], btype="band", fs=sr, output="sos"),
            rng.standard_normal(n),
        )
        side_mid = signal.sosfilt(
            signal.butter(4, [4000.0, 14000.0], btype="band", fs=sr, output="sos"),
            rng.standard_normal(n),
        ) * 1.8
        side_hf = signal.sosfilt(
            signal.butter(4, [14000.0, 20500.0], btype="band", fs=sr, output="sos"),
            rng.standard_normal(n),
        ) * 0.12
        side_sig = side_mid + side_hf
        left = (mid_sig + side_sig) * 0.5
        right = (mid_sig - side_sig) * 0.5
        stereo_mix = np.column_stack([left, right])
        stereo_mix /= np.max(np.abs(stereo_mix)) * 1.15

        acc = motor_flac._SpectralAccumulator(sr, 2)
        acc.add(stereo_mix, 0)
        spec_mix = acc.result()
        self.assertLess(spec_mix["delta_corte_ms_hz"], 900.0)
        self.assertGreater(spec_mix["caida_side_agudos_db"], 16.5)
        self.assertIs(spec_mix["sospecha_joint_stereo"], False)

        # Album coherence quorum: 18-track album where quorum is formed by GENUINE LOSSLESS + clean PROBABLY LOSSLESS
        # and 3 acoustic candidates (one carrying a benign "mastering brickwall limiter" note) are all rescued.
        def _make_track(idx, verdict, score, rel_ceil=0.95, centroid=3200.0, decay=False, problems=None):
            return {
                "archivo": f"{idx:02d}_track.flac",
                "ruta": str(self.base_path / f"{idx:02d}_track.flac"),
                "meta": {"sample_rate": 44100, "bits_per_sample": 16, "canales": 2},
                "veredicto": verdict,
                "score": score,
                "problemas": list(problems or []),
                "evidencias_forenses": [],
                "coherencia_album": False,
                "md5": {"estado": "match"},
                "bdi": {
                    "lsbs": "active",
                    "forense_bits": {
                        "es_padded_16_en_24": False,
                        "es_24bit_escalado_en_peine": False,
                        "es_16bit_dither_en_24": False,
                    },
                },
                "esp": {
                    "corte_detectado": False,
                    "techo_rel_nyquist": rel_ceil,
                    "muro_brickwall_nyquist": False,
                    "espejo_aliasing_detectado": False,
                    "sospecha_joint_stereo": False,
                    "sospecha_corte_opus_20k": False,
                    "clipping_reconstruccion_lossy": False,
                    "islas_mdct_sfb21": False,
                    "sospecha_excitador_armonico": False,
                    "sbr_qmf_detectado": False,
                    "modulacion_psicoacustica_20k": False,
                    "mdct_lattice_detected": False,
                    "ratio_huecos_mdct": 0.0,
                    "centroide_hz": centroid,
                    "decaimiento_acustico_suave": decay,
                    "rolloff_banda_limitada_genuina": False,
                    "familias_evidencia_lossy": [],
                },
            }

        album = []
        for idx in range(1, 10):
            album.append(_make_track(idx, "GENUINE LOSSLESS", 0.95))
        for idx in range(10, 16):
            album.append(_make_track(idx, "PROBABLY LOSSLESS", 0.75))
        # 3 acoustic tracks (indices 16, 17, 18) — max_candidates = max(2, 18 // 6) = 3
        album.append(
            _make_track(
                16,
                "PROBABLY LOSSLESS",
                0.70,
                rel_ceil=0.65,
                centroid=1400.0,
                decay=True,
                problems=[
                    "spectral ceiling at 14.2 kHz (65% of Nyquist)",
                    "clipping runs of 4+ consecutive samples: mastering brickwall limiter",
                ],
            )
        )
        album.append(
            _make_track(
                17,
                "SUSPICIOUS",
                0.55,
                rel_ceil=0.62,
                centroid=1350.0,
                decay=True,
                problems=["spectral ceiling at 13.8 kHz (62% of Nyquist)"],
            )
        )
        album.append(
            _make_track(
                18,
                "PROBABLY LOSSLESS",
                0.70,
                rel_ceil=0.66,
                centroid=1500.0,
                decay=True,
                problems=["spectral ceiling at 14.5 kHz (66% of Nyquist)"],
            )
        )

        motor_flac.apply_album_coherence(album)
        for idx in (15, 16, 17):
            self.assertIs(album[idx]["coherencia_album"], True)
            self.assertEqual(album[idx]["veredicto"], "PROBABLY LOSSLESS")


if __name__ == "__main__":
    unittest.main(verbosity=2)
