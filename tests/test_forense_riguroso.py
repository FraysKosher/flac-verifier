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
BAD_VERDICTS = ("SUSPICIOUS", "PROBABLE UPSCALE")


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
        self.assertEqual(res["problemas"], [])
        self.assertIn("solo acoustic/low-centroid", " ".join(res["notes"]))

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

        low_with, _, prob_low_with, notes_low_with = motor_flac.calculate_score(
            meta, _spectrum_with_ceiling(13000, True), bdi, include_notes=True)
        low_without, _, prob_low_without = motor_flac.calculate_score(
            meta, _spectrum_with_ceiling(13000, False), bdi)
        self.assertGreater(low_with, low_without)
        self.assertEqual(prob_low_with, [])
        self.assertIn("solo acoustic/low-centroid", " ".join(notes_low_with))
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
        _, verdict, prob, notes = motor_flac.calculate_score(
            meta_24, spectrum_13000, bdi_24_clean, include_notes=True)
        self.assertIn("solo acoustic/low-centroid", " ".join(notes))
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

        from _motor_veredicto import _is_clean_quorum_track

        no_md5_low_ceil = _make_track(
            99,
            "PROBABLY LOSSLESS",
            0.70,
            rel_ceil=0.65,
            centroid=1450.0,
            decay=True,
            problems=["spectral ceiling at 14.3 kHz (65% of Nyquist)"],
        )
        no_md5_low_ceil["md5"] = {"estado": "absent"}
        self.assertFalse(_is_clean_quorum_track(no_md5_low_ceil))

    def test_39_opus_hf_subwindow_dynamic_ms_spared_and_near_nyquist_coherence(self):
        """Phase 13.4: HF multi-subwindow Opus detection, dynamic M/S guard and 0.60/near-Nyquist coherence."""
        from _motor_espectral import _fast_mdct_f32, _get_celt_1920
        from _motor_veredicto import _deserves_album_coherence

        # 1. Sparse-cymbal Opus CELT-960 at 44.1 kHz inside a full 262,144-sample block
        sr_opus = 48000
        sr = 44100
        rng = np.random.default_rng(3901)
        w_celt = _get_celt_1920()
        c_frames = rng.standard_normal((28, 1920)).astype(np.float32)
        c_mdct = _fast_mdct_f32(c_frames, w_celt)
        c_mdct[:, 800:] = 0.0
        pre_sig = self._imdct_overlap_add(c_mdct, w_celt)
        opus_48 = signal.lfilter([1.0], [1.0, -0.85], pre_sig).astype(np.float32)[53:]
        opus_44 = signal.resample_poly(opus_48, 147, 160, window=("kaiser", 8.6)).astype(np.float32)
        opus_44 /= float(np.max(np.abs(opus_44)) * 1.15)
        burst = opus_44[:24000]

        n_blk = motor_flac.FRAMES_BY_BLOCK
        t = np.arange(n_blk, dtype=np.float64) / sr
        low_mid = (
            0.40 * np.sin(2.0 * np.pi * 220.0 * t)
            + 0.25 * np.sin(2.0 * np.pi * 880.0 * t)
        ).astype(np.float32)
        # Boost central broadband energy so single-window p20 picks center instead of the burst
        low_mid[90000:170000] *= np.float32(1.45)
        mono_mix = low_mid.copy()
        mono_mix[210000:234000] = (
            np.float32(0.20) * low_mid[210000:234000] + np.float32(0.75) * burst
        )
        det = motor_flac._detect_lapped_codec_grid(mono_mix, sr, ceiling_hz=20000.0)
        self.assertIs(det["mdct_lattice_detected"], True)
        self.assertEqual(det["mdct_lattice_family"], "CELT-960 (Opus)")

        # 2. Dynamic M/S guard when Side reaches 20.5 kHz with no HF cut-off asymmetry
        n_ms = sr * 5
        t_ms = np.arange(n_ms, dtype=np.float64) / sr
        mid_sig = signal.sosfilt(
            signal.butter(4, [200.0, 20500.0], btype="band", fs=sr, output="sos"),
            rng.standard_normal(n_ms),
        )
        side_mid_noise = signal.sosfilt(
            signal.butter(4, [3000.0, 8000.0], btype="band", fs=sr, output="sos"),
            rng.standard_normal(n_ms),
        )
        # Intermittent 3-8 kHz Side bursts drive frame-level excess P95 to ~21-23 dB
        burst_env = np.where((np.sin(2.0 * np.pi * 1.2 * t_ms) > 0.15), 2.8, 0.25)
        side_mid = side_mid_noise * burst_env
        side_hi = signal.sosfilt(
            signal.butter(4, [13000.0, 20500.0], btype="band", fs=sr, output="sos"),
            rng.standard_normal(n_ms),
        ) * 0.26
        side_sig = side_mid + side_hi
        left = (mid_sig + side_sig) * 0.5
        right = (mid_sig - side_sig) * 0.5
        stereo_ms = np.column_stack([left, right])
        stereo_ms /= np.max(np.abs(stereo_ms)) * 1.15

        acc = motor_flac._SpectralAccumulator(sr, 2)
        acc.add(stereo_ms, 0)
        spec_ms = acc.result()
        self.assertIs(acc._psycho._ms_dynamic_collapse, True)
        self.assertGreater(acc._psycho._ms_p95_db, 20.0)
        self.assertLess(acc._psycho._ms_p95_db, 24.5)
        self.assertGreaterEqual(spec_ms["caida_side_agudos_db"], 6.0)
        self.assertLess(spec_ms["delta_corte_ms_hz"], 800.0)
        self.assertIs(spec_ms["sospecha_joint_stereo"], False)

        # 3. Album coherence: 0.60 clean full-band quorum + near_nyquist_cutoff closer
        def _album_item(idx, verdict, score, rel_ceil=0.92, cutoff=False,
                        artificial=False, cutoff_cls="no_cutoff",
                        cutoff_iqr=0.0, problems=None):
            return {
                "archivo": f"{idx:02d}_track.flac",
                "ruta": str(self.base_path / "v294_album" / f"{idx:02d}_track.flac"),
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
                    "corte_detectado": cutoff,
                    "corte_artificial": artificial,
                    "clase_corte": cutoff_cls,
                    "iqr_corte_hz": cutoff_iqr,
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
                    "familias_evidencia_lossy": [],
                },
            }

        album = [
            _album_item(1, "GENUINE LOSSLESS", 0.90),
            _album_item(2, "GENUINE LOSSLESS", 1.00),
        ]
        for idx in range(3, 8):
            album.append(_album_item(idx, "PROBABLY LOSSLESS", 0.70))
        for idx in range(8, 16):
            album.append(_album_item(idx, "PROBABLY LOSSLESS", 0.60))
        closer = _album_item(
            16,
            "SUSPICIOUS",
            0.35,
            rel_ceil=0.80,
            cutoff=True,
            artificial=False,
            cutoff_cls="near_nyquist_cutoff",
            cutoff_iqr=120.0,
            problems=[
                "spectral ceiling 17.7 kHz (80% of Nyquist of 22.0 kHz): reduced high band",
                "band cut at 20.1 kHz (91% of Nyquist): consistent with the anti-alias filter of a legitimate master AND with a high-rate codec — inconclusive",
            ],
        )
        album.append(closer)

        raised = motor_flac.apply_album_coherence(album)
        self.assertEqual(raised, 1)
        self.assertEqual(closer["veredicto"], "PROBABLY LOSSLESS")
        self.assertEqual(closer["score"], 0.60)
        self.assertIs(closer["coherencia_album"], True)

        far_fake = _album_item(
            17,
            "SUSPICIOUS",
            0.35,
            rel_ceil=0.72,
            cutoff=True,
            artificial=True,
            cutoff_cls="far_cutoff",
            cutoff_iqr=120.0,
            problems=[
                "band cut at 16.0 kHz (72% of Nyquist): typical of lossy codecs"
            ],
        )
        self.assertFalse(_deserves_album_coherence(far_fake))

    def test_40_hires_70s_analogue_rolloff_coherence_and_48k_mp3_v0_pqmf_jitter(self):
        """Phase 13.5: 1970s Hi-Res analogue roll-off, 75% Hi-Res coherence, 48 kHz PQMF-576 and extreme jitter cap."""
        from _motor_veredicto import _deserves_album_coherence

        rng = np.random.default_rng(4001)

        # Part 1: 24-bit/96 kHz 1970s analogue tape/console roll-off vs sinc upscale + 24-bit TPDF dither
        sr_hi = 96000
        n_hi = sr_hi * 4
        freqs_hi = np.fft.rfftfreq(n_hi, 1.0 / sr_hi)
        pink_spec = np.fft.rfft(rng.standard_normal((n_hi, 2)), axis=0)
        pink_spec *= (1.0 / np.maximum(freqs_hi, 100.0) ** 0.35)[:, None]
        music_hi = np.fft.irfft(pink_spec, n_hi, axis=0)
        music_hi /= np.max(np.abs(music_hi)) * 1.15
        sos_rolloff = signal.butter(3, 16200.0, btype="low", fs=sr_hi, output="sos")
        analog_hi = signal.sosfilt(sos_rolloff, music_hi, axis=0)
        analog_hi += rng.normal(0.0, 10.0 ** (-108.0 / 20.0), size=analog_hi.shape)

        path_analog = self.base_path / "queen_1970s_analog_24_96.flac"
        sf.write(path_analog, np.clip(analog_hi, -0.98, 0.98), sr_hi, subtype="PCM_24")
        res_analog = motor_flac.analyze_file_data(str(path_analog), use_cache=False)
        self.assertGreater(res_analog["esp"]["salto_ultrasonico_db"], 14.5)
        self.assertLess(res_analog["esp"]["salto_ultrasonico_db"], 22.5)
        self.assertLess(res_analog["esp"]["gradiente_nyquist_db_khz"], 12.0)
        self.assertIs(res_analog["esp"]["rolloff_analogico_genuino"], True)
        self.assertEqual(res_analog["veredicto"], "GENUINE LOSSLESS")

        n_44 = 44100 * 4
        t_44 = np.arange(n_44, dtype=np.float64) / 44100.0
        hi_44 = signal.sosfilt(
            signal.butter(2, [4000.0, 21000.0], btype="band", fs=44100, output="sos"),
            rng.standard_normal((n_44, 2)),
            axis=0,
        )
        sig_44 = (0.25 * np.sin(2.0 * np.pi * 440.0 * t_44)[:, None]) + 0.18 * hi_44
        sig_44 /= np.max(np.abs(sig_44)) * 1.15
        sig_up = signal.resample_poly(sig_44, up=320, down=147, axis=0)
        dither_24 = (rng.random(sig_up.shape) - rng.random(sig_up.shape)) / 8388608.0
        path_upscale = self.base_path / "upscale_sinc_dither_24_96.flac"
        sf.write(path_upscale, np.clip(sig_up + dither_24, -0.98, 0.98), sr_hi, subtype="PCM_24")
        res_upscale = motor_flac.analyze_file_data(str(path_upscale), use_cache=False)
        self.assertGreater(res_upscale["esp"]["salto_ultrasonico_db"], 40.0)
        self.assertIs(res_upscale["esp"]["muro_brickwall_nyquist"], True)
        self.assertIs(res_upscale["esp"]["rolloff_analogico_genuino"], False)
        self.assertIn(res_upscale["veredicto"], BAD_VERDICTS)

        # Part 2: 17-track 24/96 batch with 13/17 (76.5%) GENUINE quorum rescuing 4 low-ceiling tracks
        def _hires_item(idx, verdict, score, ceiling_hz=26000, sr_item=96000,
                        lsbs="active", brickwall=False, problems=None):
            rel_ceil = round(ceiling_hz / (sr_item * 0.5), 4)
            return {
                "archivo": f"{idx:02d}_hires.flac",
                "ruta": str(self.base_path / "v295_hires_album" / f"{idx:02d}_hires.flac"),
                "meta": {"sample_rate": sr_item, "bits_per_sample": 24, "canales": 2},
                "veredicto": verdict,
                "score": score,
                "problemas": list(problems or []),
                "evidencias_forenses": [],
                "coherencia_album": False,
                "md5": {"estado": "match"},
                "bdi": {
                    "aplica": True,
                    "lsbs": lsbs,
                    "forense_bits": {
                        "es_padded_16_en_24": False,
                        "es_24bit_escalado_en_peine": False,
                        "es_16bit_dither_en_24": False,
                    },
                },
                "esp": {
                    "techo_hz": ceiling_hz,
                    "techo_rel_nyquist": rel_ceil,
                    "corte_detectado": False,
                    "corte_artificial": False,
                    "clase_corte": "no_cutoff",
                    "iqr_corte_hz": 0.0,
                    "muro_brickwall_nyquist": brickwall,
                    "espejo_aliasing_detectado": False,
                    "rolloff_analogico_genuino": False,
                    "es_transferencia_dsd": False,
                    "sospecha_joint_stereo": False,
                    "sospecha_corte_opus_20k": False,
                    "clipping_reconstruccion_lossy": False,
                    "islas_mdct_sfb21": False,
                    "sospecha_excitador_armonico": False,
                    "sbr_qmf_detectado": False,
                    "modulacion_psicoacustica_20k": False,
                    "mdct_lattice_detected": False,
                    "ratio_huecos_mdct": 0.0,
                    "familias_evidencia_lossy": [],
                },
            }

        batch_17 = [_hires_item(i, "GENUINE LOSSLESS", 0.92) for i in range(1, 14)]
        rescued_specs = (
            (14, 17200, ["claims 96.0 kHz but the ceiling is 17.2 kHz, below the CD band: upscaled from 44.1/48 kHz",
                         "spectral ceiling 17.2 kHz: there is no content above the CD limit (22.05 kHz), so the band of 96.0 kHz is not backed by content"]),
            (15, 18700, ["partial hi-res content: ceiling of 18.7 kHz (39% of Nyquist of 48.0 kHz)",
                         "spectral ceiling 18.7 kHz: there is no content above the CD limit (22.05 kHz), so the band of 96.0 kHz is not backed by content"]),
            (16, 20500, ["partial hi-res content: ceiling of 20.5 kHz (43% of Nyquist of 48.0 kHz)",
                         "the band of 96.0 kHz is not backed by content"]),
            (17, 21800, ["partial hi-res content: ceiling of 21.8 kHz (45% of Nyquist of 48.0 kHz)",
                         "spectral ceiling 21.8 kHz: there is no content above the CD limit (22.05 kHz), so the band of 96.0 kHz is not backed by content"]),
        )
        for idx, ceil_hz, probs in rescued_specs:
            batch_17.append(_hires_item(idx, "SUSPICIOUS", 0.45, ceiling_hz=ceil_hz, problems=probs))

        raised = motor_flac.apply_album_coherence(batch_17)
        self.assertEqual(raised, 4)
        for track in batch_17[13:]:
            self.assertEqual(track["veredicto"], "PROBABLY LOSSLESS")
            self.assertIs(track["coherencia_album"], True)

        fake_bw = _hires_item(
            18, "PROBABLE UPSCALE", 0.15, ceiling_hz=21800, brickwall=True,
            problems=["spectral ceiling 21.8 kHz: so the band of 96.0 kHz is not backed by content"],
        )
        fake_partial = _hires_item(
            19, "SUSPICIOUS", 0.45, ceiling_hz=20500, lsbs="partial",
            problems=["partial hi-res content: ceiling of 20.5 kHz"],
        )
        fake_rule5 = _hires_item(
            20, "SUSPICIOUS", 0.50, ceiling_hz=20500, sr_item=44100, lsbs="active",
            problems=["declares 24 bits at 44.1 kHz but its high band is cut at 20.5 kHz: "
                      "the declared resolution is not backed by the content "
                      "(consistent with a repackaged high-rate transcode)"],
        )
        self.assertFalse(_deserves_album_coherence(fake_bw))
        self.assertFalse(_deserves_album_coherence(fake_partial))
        self.assertFalse(_deserves_album_coherence(fake_rule5))

        # Part 3: 48 kHz PQMF-576 active in subbands 19..25 (14.25-19.5 kHz), 26..29 empty
        sr_48 = 48000
        m_tap = np.arange(512, dtype=np.float64) - 256.0
        win_kaiser = np.concatenate(([0.0], np.kaiser(511, 9.0)))
        h_raw = (1.0 / 64.0) * np.sinc(m_tap / 64.0) * win_kaiser
        h_proto = (h_raw / np.sum(h_raw)).astype(np.float32)
        n_idx = np.arange(512, dtype=np.float32)
        win36 = np.sin(np.pi / 36.0 * (np.arange(36, dtype=np.float32) + 0.5)).astype(np.float32)

        n_gran = 160
        m_sub = (n_gran + 1) * 18
        n_pcm = m_sub * 32 + 1024
        low_48 = signal.sosfilt(
            signal.butter(6, 13800.0, btype="low", fs=sr_48, output="sos"),
            rng.standard_normal(n_pcm),
        ).astype(np.float32)
        high_48 = np.zeros(n_pcm, dtype=np.float32)
        for sb in range(19, 26):
            coeffs = rng.standard_normal((n_gran, 18)).astype(np.float32)
            coeffs = np.where(rng.random(coeffs.shape) < 0.50, 0.0, coeffs * np.sqrt(2.0))
            sub_sig = self._imdct_overlap_add(coeffs, win36)[:m_sub]
            if sb % 2 == 1:
                sub_sig[1::2] *= -1.0
            upsampled = np.zeros(m_sub * 32, dtype=np.float32)
            upsampled[::32] = sub_sig * 32.0
            synth_f = (h_proto * np.cos(np.pi / 64.0 * (2 * sb + 1) * (n_idx - 16)).astype(np.float32))[::-1]
            recon_s = np.convolve(upsampled, synth_f, mode="full")
            high_48[: recon_s.size] += recon_s
        mp3_48 = (low_48 * 0.4 + high_48 * 0.6)[213:]
        mp3_48 /= float(np.max(np.abs(mp3_48)) * 1.15)

        det_mp3 = motor_flac._detect_lapped_codec_grid(mp3_48, sr_48, ceiling_hz=21350)
        self.assertIs(det_mp3["mdct_lattice_detected"], True)
        self.assertEqual(det_mp3["mdct_lattice_family"], "PQMF-576 (MP3)")

        gen_48 = signal.sosfilt(
            signal.butter(4, 21000.0, btype="low", fs=sr_48, output="sos"),
            rng.standard_normal(n_pcm),
        ).astype(np.float32)
        gen_48 /= float(np.max(np.abs(gen_48)) * 1.15)
        det_gen = motor_flac._detect_lapped_codec_grid(gen_48, sr_48, ceiling_hz=21350)
        self.assertIs(det_gen["mdct_lattice_detected"], False)

        # Part 4: Extreme cut-off jitter (> 1800 Hz) capped at SUSPICIOUS while 700 Hz stays advisory
        meta_48 = {"sample_rate": 48000, "bits_per_sample": 16, "canales": 2}
        base_spec_48 = {
            "techo_hz": 22000,
            "techo_rel_nyquist": 0.92,
            "corte_detectado": False,
            "corte_artificial": False,
            "clase_corte": "no_cutoff",
            "frecuencia_corte": None,
            "distancia_nyquist": 0.0,
            "ratio_aplica": True,
            "ratio": 0.15,
            "var_alta_db": 6.0,
            "separacion_stereo": 0.45,
            "es_dual_mono": False,
            "es_estacionario": False,
            "rolloff_analogico_genuino": False,
            "es_transferencia_dsd": False,
            "ratio_huecos_mdct": 0.0,
        }
        spec_extreme = dict(base_spec_48, iqr_corte_hz=2650.0)
        score_ext, verd_ext, _ = motor_flac.calculate_score(meta_48, spec_extreme, {"aplica": False})
        self.assertEqual(verd_ext, "SUSPICIOUS")
        self.assertNotIn(verd_ext, ("GENUINE LOSSLESS", "PROBABLY LOSSLESS"))
        self.assertGreaterEqual(score_ext, 0.50)

        spec_ext_low = dict(spec_extreme, ratio=0.005)
        score_ext_low, verd_ext_low, _ = motor_flac.calculate_score(meta_48, spec_ext_low, {"aplica": False})
        self.assertEqual(verd_ext_low, "SUSPICIOUS")
        self.assertEqual(score_ext_low, 0.50)

        spec_mod = dict(base_spec_48, iqr_corte_hz=700.0)
        score_mod, verd_mod, _ = motor_flac.calculate_score(meta_48, spec_mod, {"aplica": False})
        self.assertEqual(verd_mod, "GENUINE LOSSLESS")
        self.assertAlmostEqual(score_mod, 0.80, places=4)

    def test_41_polyphase_skirt_brickwall_hot_master_vs_steep_genuine_hires(self):
        """Phase 13.6: polyphase transition-skirt brickwall on hot 0 dBFS upscales vs steep 24/96 controls."""
        rng = np.random.default_rng(4101)

        # Case A: 32-tap-per-phase 44.1 -> 96 kHz upscale of a hot bright master whose
        # skirt extends past 22.05 kHz (worse_slope > -28 dB/kHz) and inter-sample
        # clipping lifts the ultrasonic floor above -125 dBFS.
        sr_44, sr_hi = 44100, 96000
        dur = 4.0
        n_44 = int(sr_44 * dur)
        t_44 = np.arange(n_44, dtype=np.float64) / float(sr_44)
        hf_44 = signal.sosfilt(
            signal.butter(2, 4000.0, btype="high", fs=sr_44, output="sos"),
            rng.standard_normal((n_44, 2)),
            axis=0,
        )
        tonal_44 = (0.35 * np.sin(2.0 * np.pi * 220.0 * t_44)
                    + 0.25 * np.cos(2.0 * np.pi * 880.0 * t_44))[:, None]
        sig_44 = tonal_44 + 0.30 * hf_44
        sig_44 /= np.max(np.abs(sig_44)) * 1.02

        h_poly = signal.firwin(32 * 320 + 1, 1.01 / 320.0, window=("kaiser", 9.5)) * 320.0
        up_raw = signal.upfirdn(h_poly, sig_44, up=320, down=147, axis=0)
        delay = (len(h_poly) - 1) // (2 * 147)
        n_hi = int(sr_hi * dur)
        sig_up44 = up_raw[delay : delay + n_hi]
        sig_up44 = np.clip(sig_up44 * 1.005, -1.0, 8388607.0 / 8388608.0)
        dither_24 = (rng.random(sig_up44.shape) - rng.random(sig_up44.shape)) / 8388608.0
        path_up44 = self.base_path / "upscale_32tap_hot_44_96.flac"
        sf.write(path_up44, np.clip(sig_up44 + dither_24, -1.0, 8388607.0 / 8388608.0),
                 sr_hi, subtype="PCM_24")
        res_44 = motor_flac.analyze_file_data(str(path_up44), use_cache=False)

        self.assertGreaterEqual(res_44["esp"]["techo_hz"], 22050)
        self.assertIs(res_44["esp"]["muro_brickwall_nyquist"], True)
        self.assertGreaterEqual(res_44["esp"]["gradiente_nyquist_db_khz"], 20.0)
        self.assertGreaterEqual(res_44["esp"]["salto_ultrasonico_db"], 40.0)
        self.assertIn(res_44["veredicto"], BAD_VERDICTS)

        # Case B: 48 -> 96 kHz half-band resample_poly upscale of a hot master
        src_48 = np.clip(_create_track_reference(48000, duration=dur, seed=4102) * 1.08,
                         -0.999, 0.999)
        sig_up48 = signal.resample_poly(src_48, 2, 1, axis=0)
        sig_up48 = np.clip(sig_up48, -1.0, 8388607.0 / 8388608.0)
        dither_48 = (rng.random(sig_up48.shape) - rng.random(sig_up48.shape)) / 8388608.0
        path_up48 = self.base_path / "upscale_halfband_hot_48_96.flac"
        sf.write(path_up48, np.clip(sig_up48 + dither_48, -1.0, 8388607.0 / 8388608.0),
                 sr_hi, subtype="PCM_24")
        res_48 = motor_flac.analyze_file_data(str(path_up48), use_cache=False)

        self.assertGreaterEqual(res_48["esp"]["techo_hz"], 22050)
        self.assertIs(res_48["esp"]["muro_brickwall_nyquist"], True)
        self.assertIn(res_48["veredicto"], BAD_VERDICTS)

        # Case C1: Genuine 24/96 controls with steep analogue/anti-alias roll-offs (24 & 36 dB/oct)
        base_steep = _create_track_reference(sr_hi, duration=dur, seed=4103)
        steep_24 = signal.sosfilt(
            signal.butter(4, 21000.0, btype="low", fs=sr_hi, output="sos"),
            base_steep,
            axis=0,
        )
        steep_24 += rng.normal(0.0, 10.0 ** (-104.0 / 20.0), size=steep_24.shape)
        path_steep24 = self.base_path / "genuine_steep_order4_21k_24_96.flac"
        sf.write(path_steep24, np.clip(steep_24 / (np.max(np.abs(steep_24)) * 1.08), -0.99, 0.99),
                 sr_hi, subtype="PCM_24")
        res_steep24 = motor_flac.analyze_file_data(str(path_steep24), use_cache=False)

        self.assertIs(res_steep24["esp"]["muro_brickwall_nyquist"], False)
        self.assertIs(res_steep24["esp"]["rolloff_analogico_genuino"], True)
        self.assertIn(res_steep24["veredicto"], GOOD_VERDICTS)

        steep_36 = signal.sosfilt(
            signal.butter(6, 22500.0, btype="low", fs=sr_hi, output="sos"),
            base_steep,
            axis=0,
        )
        steep_36 += rng.normal(0.0, 10.0 ** (-105.0 / 20.0), size=steep_36.shape)
        path_steep36 = self.base_path / "genuine_steep_order6_22k5_24_96.flac"
        sf.write(path_steep36, np.clip(steep_36 / (np.max(np.abs(steep_36)) * 1.08), -0.99, 0.99),
                 sr_hi, subtype="PCM_24")
        res_steep36 = motor_flac.analyze_file_data(str(path_steep36), use_cache=False)

        self.assertIs(res_steep36["esp"]["muro_brickwall_nyquist"], False)
        self.assertIn(res_steep36["veredicto"], GOOD_VERDICTS)

        # Case C2: Genuine bright 24/96 studio control (4th-order 24.5 kHz + -112 dBFS floor)
        base_bright = _create_track_reference(sr_hi, duration=dur, seed=4104)
        bright = signal.sosfilt(
            signal.butter(4, 24500.0, btype="low", fs=sr_hi, output="sos"),
            base_bright,
            axis=0,
        )
        bright += rng.normal(0.0, 10.0 ** (-112.0 / 20.0), size=bright.shape)
        path_bright = self.base_path / "genuine_bright_order4_24k5_24_96.flac"
        sf.write(path_bright, np.clip(bright / (np.max(np.abs(bright)) * 1.08), -0.99, 0.99),
                 sr_hi, subtype="PCM_24")
        res_bright = motor_flac.analyze_file_data(str(path_bright), use_cache=False)

        self.assertGreaterEqual(res_bright["esp"]["techo_hz"], 23000)
        self.assertIs(res_bright["esp"]["muro_brickwall_nyquist"], False)
        self.assertEqual(res_bright["veredicto"], "GENUINE LOSSLESS")

        # Case C3: Genuine 24/96 studio master with ~29.5 kHz ultrasonic mastering filter
        # (Blu-ray / Hi-Res mastering DAC low-pass dropping into -144 dBFS 24-bit TPDF).
        base_ultra = _create_track_reference(sr_hi, duration=dur, seed=4105)
        ultra_master = signal.sosfilt(
            signal.butter(10, 29500.0, btype="low", fs=sr_hi, output="sos"),
            base_ultra,
            axis=0,
        )
        ultra_master = ultra_master / (np.max(np.abs(ultra_master)) * 1.08)
        dither_u30 = (rng.random(ultra_master.shape) - rng.random(ultra_master.shape)) / 8388608.0
        path_u30 = self.base_path / "genuine_ultrasonic_30k_master_24_96.flac"
        sf.write(
            path_u30,
            np.clip(ultra_master + dither_u30, -1.0, 8388607.0 / 8388608.0),
            sr_hi,
            subtype="PCM_24",
        )
        res_u30 = motor_flac.analyze_file_data(str(path_u30), use_cache=False)

        self.assertGreaterEqual(res_u30["esp"]["techo_hz"], 25000)
        self.assertIs(res_u30["esp"]["muro_brickwall_nyquist"], False)
        self.assertEqual(res_u30["veredicto"], "GENUINE LOSSLESS")

    def test_42_quiet_hires_30k_master_and_centered_vocal_ms_guard(self):
        """Phase 13.7: quiet 24/96 tracks with centered vocals, low sub-bass
        relative ceiling (< 22.05 kHz), and ~29.5 kHz studio LPFs must verify as
        GENUINE LOSSLESS without flagging muro_brickwall_nyquist or joint-stereo
        M/S collapse, while quiet 44.1->96 upscales, 48->96 stems over an analog
        floor, and true joint-stereo collapses remain detected."""
        sr_hi = 96000
        sr_44 = 44100
        dur = 6.0
        n_hi = int(sr_hi * dur)
        t_hi = np.arange(n_hi, dtype=np.float64) / float(sr_hi)
        rng = np.random.default_rng(4201)

        # Case A: Intimate genuine 24/96 track — wide stereo 2-6 kHz acoustic bed,
        # strictly centered mono 9-27 kHz vocal harmonics, sub-bass weighting
        # pushing the -60 dB ceiling below 22.05 kHz, gentle studio DAC roll-off
        # + 29.5 kHz ultrasonic LPF into 24-bit TPDF dither.
        wide_l = signal.sosfilt(
            signal.butter(2, [300.0, 5800.0], btype="band", fs=sr_hi, output="sos"),
            rng.standard_normal(n_hi),
        )
        wide_r = signal.sosfilt(
            signal.butter(2, [300.0, 5800.0], btype="band", fs=sr_hi, output="sos"),
            rng.standard_normal(n_hi),
        )
        centered_hf = signal.sosfilt(
            signal.butter(2, 7500.0, btype="high", fs=sr_hi, output="sos"),
            rng.standard_normal(n_hi),
        )
        # Mild amplitude modulation + sub-bass tone so relative -60 dB sits < 22 kHz.
        env = 0.25 + 0.75 * (np.sin(2.0 * np.pi * 1.8 * t_hi) ** 4)
        sub_bass = 0.22 * np.sin(2.0 * np.pi * 65.0 * t_hi)
        raw_genuine = np.column_stack([
            sub_bass + 0.03 * wide_l * env + 0.014 * centered_hf * env,
            sub_bass + 0.03 * wide_r * env + 0.014 * centered_hf * env,
        ])
        # Studio DAC pre-emphasis attenuation + 29.5 kHz ultrasonic anti-image filter.
        rolled = signal.sosfilt(
            signal.butter(2, 16500.0, btype="low", fs=sr_hi, output="sos"),
            raw_genuine,
            axis=0,
        )
        rolled = signal.sosfilt(
            signal.butter(10, 29500.0, btype="low", fs=sr_hi, output="sos"),
            rolled,
            axis=0,
        )
        dither_a = (rng.random(rolled.shape) - rng.random(rolled.shape)) / 8388608.0
        path_a = self.base_path / "genuine_quiet_30k_centered_vocal_24_96.flac"
        sf.write(
            path_a,
            np.clip(rolled + dither_a, -1.0, 8388607.0 / 8388608.0),
            sr_hi,
            subtype="PCM_24",
        )
        res_a = motor_flac.analyze_file_data(str(path_a), use_cache=False)

        self.assertIs(res_a["esp"]["muro_brickwall_nyquist"], False)
        self.assertIs(res_a["esp"]["rolloff_analogico_genuino"], True)
        self.assertIs(res_a["esp"]["sospecha_joint_stereo"], False)
        self.assertEqual(res_a["veredicto"], "GENUINE LOSSLESS")

        # Case B1: Quiet 44.1 -> 96 Sinc upscale -> must still be flagged.
        n_44 = int(sr_44 * dur)
        sig_44 = signal.resample_poly(raw_genuine, 147, 320, axis=0)[:n_44]
        up_sinc = signal.resample_poly(sig_44, 320, 147, axis=0)[:n_hi]
        dither_b1 = (rng.random(up_sinc.shape) - rng.random(up_sinc.shape)) / 8388608.0
        path_b1 = self.base_path / "upscale_quiet_44_96_sinc.flac"
        sf.write(
            path_b1,
            np.clip(up_sinc + dither_b1, -1.0, 8388607.0 / 8388608.0),
            sr_hi,
            subtype="PCM_24",
        )
        res_b1 = motor_flac.analyze_file_data(str(path_b1), use_cache=False)

        self.assertIs(res_b1["esp"]["muro_brickwall_nyquist"], True)
        self.assertIs(res_b1["esp"]["rolloff_analogico_genuino"], False)
        self.assertIn(res_b1["veredicto"], BAD_VERDICTS)

        # Case B2: Quiet 44.1 -> 96 32-tap polyphase upscale -> must still be flagged.
        h_poly = signal.firwin(32 * 320 + 1, 1.01 / 320.0, window=("kaiser", 9.5)) * 320.0
        up_poly_raw = signal.upfirdn(h_poly, sig_44 * 2.5, up=320, down=147, axis=0)
        delay = (len(h_poly) - 1) // (2 * 147)
        up_poly = up_poly_raw[delay : delay + n_hi]
        dither_b2 = (rng.random(up_poly.shape) - rng.random(up_poly.shape)) / 8388608.0
        path_b2 = self.base_path / "upscale_quiet_44_96_poly32.flac"
        sf.write(
            path_b2,
            np.clip(up_poly + dither_b2, -1.0, 8388607.0 / 8388608.0),
            sr_hi,
            subtype="PCM_24",
        )
        res_b2 = motor_flac.analyze_file_data(str(path_b2), use_cache=False)

        self.assertIs(res_b2["esp"]["muro_brickwall_nyquist"], True)
        self.assertIs(res_b2["esp"]["rolloff_analogico_genuino"], False)
        self.assertIn(res_b2["veredicto"], BAD_VERDICTS)

        # Case C: 48 kHz mix stem embedded in 24/96 over an analog noise floor.
        hf_48 = signal.sosfilt(
            signal.butter(2, 12000.0, btype="high", fs=48000, output="sos"),
            rng.standard_normal((int(48000 * dur), 2)),
            axis=0,
        )
        src_48 = _create_track_reference(48000, duration=dur, seed=4203) + 0.25 * hf_48
        h_48 = signal.firwin(161, 23200.0 / 48000.0, window=("kaiser", 10.0)) * 2.0
        up_48 = signal.upfirdn(h_48, src_48, up=2, down=1, axis=0)[40 : 40 + n_hi]
        analog_floor = rng.normal(0.0, 10.0 ** (-72.0 / 20.0), size=up_48.shape)
        path_c = self.base_path / "stem_48k_in_96k_analog_floor.flac"
        sf.write(
            path_c,
            np.clip(up_48 * 0.65 + analog_floor, -0.99, 0.99),
            sr_hi,
            subtype="PCM_24",
        )
        res_c = motor_flac.analyze_file_data(str(path_c), use_cache=False)

        self.assertIs(res_c["esp"]["muro_brickwall_nyquist"], True)
        self.assertIs(res_c["esp"]["rolloff_analogico_genuino"], False)
        self.assertIn(res_c["veredicto"], BAD_VERDICTS)

        # Case D: True joint-stereo HF collapse (Mid full to 20 kHz, Side cut at 15.5 kHz)
        # must still trigger sospecha_joint_stereo.
        src_cd = _create_track_reference(sr_44, duration=dur, seed=4204)
        mid_cd = 0.5 * (src_cd[:, 0] + src_cd[:, 1])
        side_cd = 0.5 * (src_cd[:, 0] - src_cd[:, 1])
        side_lp = signal.sosfilt(
            signal.butter(12, 15500.0, btype="low", fs=sr_44, output="sos"),
            side_cd,
        )
        joint_cd = np.column_stack([mid_cd + side_lp, mid_cd - side_lp])
        path_d = self.base_path / "true_joint_stereo_collapse.flac"
        sf.write(path_d, np.clip(joint_cd * 0.9, -0.99, 0.99), sr_44, subtype="PCM_16")
        res_d = motor_flac.analyze_file_data(str(path_d), use_cache=False)

        self.assertIs(res_d["esp"]["sospecha_joint_stereo"], True)

    def test_43_steep_24_96_rolloff_hires_ms_guards_and_opus_batch_quorum(self):
        """Phase 14.0: steep continuous 24/96 roll-off guard, genuine Hi-Res
        ultrasonic-stereo & dark-LPF M/S guards, and confirmed lapped-codec
        batch quorum for low-ceiling Opus intros."""
        sr_hi = 96000
        dur = 4.0
        n_hi = int(sr_hi * dur)
        t_hi = np.arange(n_hi, dtype=np.float64) / float(sr_hi)
        rng = np.random.default_rng(4301)

        # Part A1: Steep continuous 24/96 studio roll-off (~15.5 kHz multi-pole
        # decay that keeps decaying through 24.5-42 kHz above a -96 dBFS floor).
        base_hi = _create_track_reference(sr_hi, duration=dur, seed=4302)
        steep_cont = signal.sosfilt(
            signal.butter(5, 15500.0, btype="low", fs=sr_hi, output="sos"),
            base_hi,
            axis=0,
        )
        steep_cont += rng.normal(0.0, 10.0 ** (-94.0 / 20.0), size=steep_cont.shape)
        path_a1 = self.base_path / "genuine_steep_continuous_24_96.flac"
        sf.write(
            path_a1,
            np.clip(steep_cont / (np.max(np.abs(steep_cont)) * 1.08), -0.99, 0.99),
            sr_hi,
            subtype="PCM_24",
        )
        res_a1 = motor_flac.analyze_file_data(str(path_a1), use_cache=False)
        self.assertIs(res_a1["esp"]["muro_brickwall_nyquist"], False)
        self.assertIs(res_a1["esp"]["rolloff_analogico_genuino"], True)
        self.assertIn(res_a1["veredicto"], GOOD_VERDICTS)

        # Part A2: Adversarial 44.1 kHz stem with a flat -84 dBFS hiss floor
        # added on top (zero ultrasonic decay between 24.5-28 kHz and 34-42 kHz)
        # must NOT qualify for continuous_steep_rolloff.
        sig_44 = signal.resample_poly(base_hi, 147, 320, axis=0)
        up_flat = signal.resample_poly(sig_44, 320, 147, axis=0)[:n_hi]
        up_flat = signal.sosfilt(
            signal.butter(3, 18500.0, btype="low", fs=sr_hi, output="sos"),
            up_flat,
            axis=0,
        )
        flat_hiss = rng.normal(0.0, 10.0 ** (-78.0 / 20.0), size=up_flat.shape)
        path_a2 = self.base_path / "upscale_44k_flat_hiss_floor_24_96.flac"
        sf.write(
            path_a2,
            np.clip(up_flat * 0.8 + flat_hiss, -0.99, 0.99),
            sr_hi,
            subtype="PCM_24",
        )
        res_a2 = motor_flac.analyze_file_data(str(path_a2), use_cache=False)
        self.assertIs(res_a2["esp"]["rolloff_analogico_genuino"], False)
        self.assertIn(res_a2["veredicto"], BAD_VERDICTS)

        # Part B1: Genuine 24/96 dark studio LPF (< 13.5 kHz ceiling) with
        # centered transient bursts must not trigger M/S collapse or IQR penalty.
        dark_src = np.column_stack([
            0.30 * np.sin(2.0 * np.pi * 110.0 * t_hi) + 0.08 * rng.standard_normal(n_hi),
            0.30 * np.sin(2.0 * np.pi * 110.0 * t_hi) + 0.08 * rng.standard_normal(n_hi),
        ])
        burst_env = (np.sin(2.0 * np.pi * 2.5 * t_hi) > 0.65).astype(np.float64)
        mono_burst = 0.22 * burst_env * rng.standard_normal(n_hi)
        dark_mix = np.column_stack([dark_src[:, 0] + mono_burst, dark_src[:, 1] + mono_burst])
        dark_lp = signal.sosfilt(
            signal.butter(7, 5000.0, btype="low", fs=sr_hi, output="sos"),
            dark_mix,
            axis=0,
        )
        dark_lp += rng.normal(0.0, 10.0 ** (-102.0 / 20.0), size=dark_lp.shape)
        path_b1 = self.base_path / "genuine_dark_lpf_24_96.flac"
        sf.write(path_b1, np.clip(dark_lp, -0.99, 0.99), sr_hi, subtype="PCM_24")
        res_b1 = motor_flac.analyze_file_data(str(path_b1), use_cache=False)
        self.assertLess(res_b1["esp"]["techo_hz"], 13500)
        self.assertLessEqual(res_b1["esp"]["iqr_corte_hz"], 350.0)
        self.assertIs(res_b1["esp"]["sospecha_joint_stereo"], False)
        self.assertIn(res_b1["veredicto"], GOOD_VERDICTS)

        # Part B2: Genuine 24/96 full-bandwidth stereo synth with strong 24-36 kHz
        # Side energy AND intermittent centered 10-20 kHz pulses must keep
        # sospecha_joint_stereo False via hires_ultrasonic_stereo.
        us_stereo = signal.sosfilt(
            signal.butter(2, 4000.0, btype="high", fs=sr_hi, output="sos"),
            rng.standard_normal((n_hi, 2)),
            axis=0,
        ) * 0.06
        mid_pulse_band = signal.sosfilt(
            signal.butter(3, [9500.0, 19500.0], btype="band", fs=sr_hi, output="sos"),
            rng.standard_normal(n_hi),
        ) * (0.35 * burst_env)
        full_stereo = np.column_stack([
            0.25 * np.sin(2.0 * np.pi * 165.0 * t_hi) + us_stereo[:, 0] + mid_pulse_band,
            0.25 * np.sin(2.0 * np.pi * 165.0 * t_hi) + us_stereo[:, 1] + mid_pulse_band,
        ])
        path_b2 = self.base_path / "genuine_ultrasonic_stereo_24_96.flac"
        sf.write(path_b2, np.clip(full_stereo, -0.99, 0.99), sr_hi, subtype="PCM_24")
        res_b2 = motor_flac.analyze_file_data(str(path_b2), use_cache=False)
        self.assertIs(res_b2["esp"]["sospecha_joint_stereo"], False)
        self.assertIn(res_b2["veredicto"], GOOD_VERDICTS)

        # Part C: Album-level lapped-codec quorum for dark/low-ceiling intros.
        folder_opus = str(self.base_path / "opus_batch")
        dark_intro = {
            "archivo": "01_dark_intro.flac",
            "ruta": os.path.join(folder_opus, "01_dark_intro.flac"),
            "veredicto": "PROBABLY LOSSLESS",
            "score": 0.72,
            "problemas": [],
            "evidencias_forenses": [],
            "coherencia_album": False,
            "meta": {"sample_rate": 48000, "bits_per_sample": 24, "canales": 2},
            "esp": {
                "techo_hz": 13500,
                "techo_rel_nyquist": 0.5625,
                "mdct_lattice_detected": False,
                "mdct_lattice_snr": 4.25,
                "familias_evidencia_lossy": [],
            },
            "bdi": {"aplica": True, "lsbs": "active", "forense_bits": {}},
        }
        self.assertEqual(
            motor_flac.classify_batch_disposition(dark_intro),
            motor_flac.BatchDisposition.DEFERRED,
        )

        # In a genuine 48 kHz album (0% lattice), dark_intro stays untouched.
        genuine_siblings = [
            {
                "archivo": f"0{i}_genuine.flac",
                "ruta": os.path.join(folder_opus, f"0{i}_genuine.flac"),
                "veredicto": "GENUINE LOSSLESS",
                "score": 0.95,
                "problemas": [],
                "evidencias_forenses": [],
                "coherencia_album": False,
                "meta": {"sample_rate": 48000, "bits_per_sample": 24, "canales": 2},
                "esp": {
                    "techo_hz": 22500,
                    "techo_rel_nyquist": 0.9375,
                    "mdct_lattice_detected": False,
                    "mdct_lattice_snr": 1.1,
                    "familias_evidencia_lossy": [],
                },
                "bdi": {"aplica": True, "lsbs": "active", "forense_bits": {}},
            }
            for i in range(2, 6)
        ]
        intro_gen_copy = dict(dark_intro, problemas=[], evidencias_forenses=[])
        motor_flac.apply_album_coherence([intro_gen_copy] + genuine_siblings)
        self.assertIn(intro_gen_copy["veredicto"], GOOD_VERDICTS)

        # In an 80% confirmed CELT-960 (Opus) album batch, dark_intro is caught.
        opus_siblings = [
            {
                "archivo": f"0{i}_opus.flac",
                "ruta": os.path.join(folder_opus, f"0{i}_opus.flac"),
                "veredicto": "PROBABLE UPSCALE",
                "score": 0.15,
                "problemas": ["Lapped transform quantization lattice (CELT-960 (Opus))"],
                "evidencias_forenses": [],
                "coherencia_album": False,
                "meta": {"sample_rate": 48000, "bits_per_sample": 24, "canales": 2},
                "esp": {
                    "techo_hz": 20050,
                    "techo_rel_nyquist": 0.835,
                    "mdct_lattice_detected": True,
                    "mdct_lattice_family": "CELT-960 (Opus)",
                    "mdct_lattice_snr": 11.4,
                    "familias_evidencia_lossy": ["CELT_GRID"],
                },
                "bdi": {"aplica": True, "lsbs": "active", "forense_bits": {}},
            }
            for i in range(2, 6)
        ]
        intro_opus_copy = dict(dark_intro, problemas=[], evidencias_forenses=[])
        changed = motor_flac.apply_album_coherence([intro_opus_copy] + opus_siblings)
        self.assertEqual(changed, 1)
        self.assertEqual(intro_opus_copy["veredicto"], "PROBABLE UPSCALE")
        self.assertLessEqual(intro_opus_copy["score"], 0.35)
        self.assertIs(intro_opus_copy["coherencia_album"], True)
        ev = motor_flac.forensic_evidence(intro_opus_copy)
        self.assertTrue(any("CELT-960 (Opus)" in item for item in ev))

        # Also caught when 48 kHz siblings have sharp 20 kHz Opus brickwalls
        # (techo_rel_nyquist ~ 0.834, corte_artificial=True) without running lattice.
        cliff_siblings = [
            {
                "archivo": f"0{i}_opus48_cliff.flac",
                "ruta": os.path.join(folder_opus, f"0{i}_opus48_cliff.flac"),
                "veredicto": "PROBABLE UPSCALE",
                "score": 0.0,
                "problemas": ["artificial spectral cut-off"],
                "evidencias_forenses": [],
                "coherencia_album": False,
                "meta": {"sample_rate": 48000, "bits_per_sample": 24, "canales": 2},
                "esp": {
                    "techo_hz": 20020,
                    "techo_rel_nyquist": 0.8342,
                    "corte_artificial": True,
                    "mdct_lattice_detected": False,
                    "mdct_lattice_snr": 0.0,
                    "familias_evidencia_lossy": ["spectral_cutoff"],
                },
                "bdi": {"aplica": True, "lsbs": "active", "forense_bits": {}},
            }
            for i in range(2, 6)
        ]
        intro_cliff_copy = dict(dark_intro, problemas=[], evidencias_forenses=[])
        changed_cliff = motor_flac.apply_album_coherence([intro_cliff_copy] + cliff_siblings)
        self.assertEqual(changed_cliff, 1)
        self.assertEqual(intro_cliff_copy["veredicto"], "PROBABLE UPSCALE")
        self.assertIs(intro_cliff_copy["coherencia_album"], True)

    def test_44_v298_hires_clipped_skirt_celt960_plantasia_mod20k_and_vhs_coherence(self):
        """Phase 14.1: clipped 44.1->96k skirts, 44.1 kHz CELT-960 & sub-lattice
        20 kHz Opus corroboration, 24/44.1 low-centroid archival masters, static
        LTI/dither mod20k clamp, narrow-midrange M/S guard, and mixed VHS coherence."""
        from _motor_espectral import _fast_mdct_f32, _get_celt_1920
        import _motor_espectral
        import _motor_veredicto

        rng = np.random.default_rng(4401)

        # Part A: Clipped 44.1 -> 96 kHz upscale with -76 dBFS stopband vs 3-pole analog 24/96
        sr_hi = 96000
        dur = 4.0
        n_hi = int(sr_hi * dur)
        sig_44 = _create_track_reference(44100, duration=dur, seed=4402)
        sig_44 = np.clip(sig_44 * 1.35, -0.98, 0.98)
        sig_44 = np.round(sig_44 * 32767.0) / 32767.0
        up_96 = signal.resample_poly(sig_44, 320, 147, axis=0)[:n_hi]
        up_96 += rng.normal(0.0, 10.0 ** (-76.0 / 20.0), size=up_96.shape)
        path_clip_up = self.base_path / "clipped_44_to_96_upscale.flac"
        sf.write(path_clip_up, np.clip(up_96 * 0.98, -0.99, 0.99), sr_hi, subtype="PCM_24")
        res_clip_up = motor_flac.analyze_file_data(str(path_clip_up), use_cache=False)
        self.assertIs(res_clip_up["esp"]["muro_brickwall_nyquist"], True)
        self.assertNotIn(res_clip_up["veredicto"], GOOD_VERDICTS)

        # Control: genuine 3-pole analog 24/96 profile with jump ~ 22.6 dB (< 23.2 dB)
        freqs = np.linspace(10000.0, 46000.0, 1801)
        curve = np.where(
            freqs <= 21000.0,
            -35.0 - 1.2 * (freqs - 14000.0) / 1000.0,
            np.where(
                freqs <= 24000.0,
                -43.4 - 6.2 * (freqs - 21000.0) / 1000.0,
                -62.0 - 0.35 * (freqs - 24000.0) / 1000.0,
            ),
        )
        diag_analog = _motor_espectral._diagnose_hires_authenticity(
            freqs, curve, 96000, ceiling_hz=23200.0
        )
        self.assertIs(diag_analog["rolloff_analogico_genuino"], True)
        self.assertIs(diag_analog["muro_brickwall_nyquist"], False)

        # Part B: 44.1 kHz CELT-960 lapped lattice at ~15.5 kHz ceiling (>= 14.5 kHz gate)
        # + sub-threshold 20 kHz Opus corroboration
        sr_cd = 44100
        w_celt = _get_celt_1920()
        c_frames = rng.standard_normal((160, 1920)).astype(np.float32)
        c_mdct = _fast_mdct_f32(c_frames, w_celt)
        c_mdct[:, 620:800] *= np.float32(0.08)
        c_mdct[:, 800:] = 0.0
        pre_celt = self._imdct_overlap_add(c_mdct, w_celt)
        opus_48 = signal.lfilter([1.0], [1.0, -0.85], pre_celt).astype(np.float32)[53:]
        celt_44 = signal.resample_poly(opus_48, 147, 160, window=("kaiser", 8.6)).astype(np.float32)
        celt_44 /= float(np.max(np.abs(celt_44)) * 1.15)
        det_celt = motor_flac._detect_lapped_codec_grid(celt_44, sr_cd, ceiling_hz=15500.0)
        self.assertIs(det_celt["mdct_lattice_detected"], True)
        self.assertIn("CELT-960", str(det_celt["mdct_lattice_family"]))

        opus_dark_meta = {"sample_rate": 48000, "bits_per_sample": 16, "canales": 2}
        # A 14.5 dB 20 kHz shelf sits below the 18.5/25 dB step cut-off detector,
        # so cutoff assessment stays NO_CUTOFF while sospecha_corte_opus_20k +
        # sub-threshold lattice SNR (4.2 >= 3.8) condemns the track on its own.
        opus_dark_spec = {
            "techo_hz": 20010,
            "techo_rel_nyquist": 0.834,
            **motor_flac.CutoffAssessment.none().to_protocol_fields(),
            "ratio_aplica": True,
            "ratio": 0.002,
            "var_alta_db": 12.0,
            "sospecha_corte_opus_20k": True,
            "caida_opus_20k_db": 14.5,
            "mdct_lattice_detected": False,
            "mdct_lattice_snr": 4.2,
            "iqr_corte_hz": 180.0,
            "ratio_huecos_mdct": 0.01,
        }
        sc, verd, probs = motor_flac.calculate_score(
            opus_dark_meta, opus_dark_spec, {"aplica": False}
        )
        self.assertIn(verd, ("SUSPICIOUS", "PROBABLE UPSCALE"))
        self.assertIs(
            _motor_veredicto._is_lossy_codec_quorum_track(
                {"meta": opus_dark_meta, "esp": opus_dark_spec, "veredicto": verd}
            ),
            True,
        )

        # Part C: 24/44.1 low-centroid Moog / archival master vs padded 16-in-24 control
        t_cd = np.arange(int(sr_cd * 4.0), dtype=np.float64) / float(sr_cd)
        moog = np.zeros((t_cd.size, 2), dtype=np.float64)
        for harm in range(1, 9):
            freq = 130.81 * harm
            moog[:, 0] += (0.5 / harm) * np.sin(2.0 * np.pi * freq * t_cd)
            moog[:, 1] += (0.5 / harm) * np.sin(2.0 * np.pi * (freq * 1.003) * t_cd + 0.4)
        moog_noise = signal.sosfilt(
            signal.butter(3, 12500.0, btype="low", fs=sr_cd, output="sos"),
            rng.standard_normal((t_cd.size, 2)) * 0.03,
            axis=0,
        )
        moog_24 = np.clip(moog * 0.25 + moog_noise + rng.normal(0.0, 2e-5, size=moog.shape), -0.99, 0.99)
        moog_ok_dir = self.base_path / "moog_ok_album"
        moog_bad_dir = self.base_path / "moog_bad_album"
        moog_ok_dir.mkdir(parents=True, exist_ok=True)
        moog_bad_dir.mkdir(parents=True, exist_ok=True)

        path_moog = moog_ok_dir / "01_plantasia_24_44_archival.flac"
        sf.write(path_moog, moog_24, sr_cd, subtype="PCM_24")
        res_moog = motor_flac.analyze_file_data(str(path_moog), use_cache=False)
        self.assertIn(res_moog["veredicto"], ("SUSPICIOUS", "PROBABLE UPSCALE"))

        moog_sibs_ok = [
            {
                "archivo": f"0{i}_moog_sib.flac",
                "ruta": str(moog_ok_dir / f"0{i}_moog_sib.flac"),
                "veredicto": "PROBABLY LOSSLESS",
                "score": 0.70,
                "problemas": ["band cut at 21.2 kHz (96% of Nyquist): inconclusive"],
                "evidencias_forenses": [],
                "coherencia_album": False,
                "md5": {"estado": "absent"},
                "meta": {"sample_rate": sr_cd, "bits_per_sample": 24, "canales": 2},
                "esp": {
                    "techo_hz": 21200,
                    "techo_rel_nyquist": 0.96,
                    "clase_corte": "near_nyquist_cutoff",
                    "distancia_nyquist": 0.96,
                    "familias_evidencia_lossy": [],
                },
                "bdi": {"aplica": True, "lsbs": "active", "forense_bits": {}},
            }
            for i in range(2, 7)
        ]
        self.assertEqual(motor_flac.apply_album_coherence([res_moog] + moog_sibs_ok), 1)
        self.assertEqual(res_moog["veredicto"], "PROBABLY LOSSLESS")
        self.assertIs(res_moog["coherencia_album"], True)

        moog_16_pad = np.round(moog_24 * 32767.0) / 32767.0
        path_moog_pad = moog_bad_dir / "01_plantasia_16in24_padded.flac"
        sf.write(path_moog_pad, moog_16_pad, sr_cd, subtype="PCM_24")
        res_moog_pad = motor_flac.analyze_file_data(str(path_moog_pad), use_cache=False)
        self.assertNotIn(res_moog_pad["veredicto"], GOOD_VERDICTS)
        moog_sibs_bad = [
            dict(s, ruta=str(moog_bad_dir / s["archivo"]), coherencia_album=False)
            for s in moog_sibs_ok
        ]
        self.assertEqual(motor_flac.apply_album_coherence([res_moog_pad] + moog_sibs_bad), 0)
        self.assertNotIn(res_moog_pad["veredicto"], GOOD_VERDICTS)

        # Narrow-midrange genuine stereo mix vs HF joint-stereo collapse
        n_ms = sr_cd * 4
        mid_narrow = signal.sosfilt(
            signal.butter(4, [150.0, 20000.0], btype="band", fs=sr_cd, output="sos"),
            rng.standard_normal(n_ms),
        )
        side_narrow = signal.sosfilt(
            signal.butter(4, [150.0, 900.0], btype="band", fs=sr_cd, output="sos"),
            rng.standard_normal(n_ms),
        ) * 0.40 + rng.normal(0.0, 0.015, size=n_ms)
        acc_narrow = motor_flac._SpectralAccumulator(sr_cd, 2)
        acc_narrow.add(np.column_stack([(mid_narrow + side_narrow) * 0.4, (mid_narrow - side_narrow) * 0.4]), 0)
        self.assertIs(acc_narrow.result()["sospecha_joint_stereo"], False)

        # Part D: Static LTI filtered pink noise + gentle TPDF dither stays non-modulated
        pink_b, pink_a = ([0.049922, -0.095993, 0.050612, -0.004408], [1.0, -2.494956, 2.017265, -0.522189])
        static_noise = signal.lfilter(pink_b, pink_a, rng.standard_normal((sr_cd * 5, 2)), axis=0)
        static_noise = signal.sosfilt(
            signal.butter(4, 11800.0, btype="low", fs=sr_cd, output="sos"),
            static_noise,
            axis=0,
        )
        tpdf = (rng.uniform(-1.0, 1.0, size=static_noise.shape) + rng.uniform(-1.0, 1.0, size=static_noise.shape)) * (10.0 ** (-72.0 / 20.0))
        acc_lti = motor_flac._SpectralAccumulator(sr_cd, 2)
        acc_lti.add(np.clip(static_noise * 0.4 + tpdf, -0.99, 0.99), 0)
        res_lti = acc_lti.result()
        self.assertIs(res_lti["modulacion_psicoacustica_20k"], False)
        self.assertLessEqual(res_lti["iqr_corte_hz"], 350.0)

        # Part E: Mixed VHS/archival batch fallback (40..74% support) + tainted control
        folder_clean = os.path.join(str(self.base_path), "vhs_clean_album")
        folder_tainted = os.path.join(str(self.base_path), "vhs_tainted_album")
        os.makedirs(folder_clean, exist_ok=True)
        os.makedirs(folder_tainted, exist_ok=True)
        clean_refs = [
            {
                "archivo": f"0{i}_ref.flac",
                "ruta": os.path.join(folder_clean, f"0{i}_ref.flac"),
                "veredicto": "GENUINE LOSSLESS",
                "score": 0.92,
                "problemas": [],
                "evidencias_forenses": [],
                "coherencia_album": False,
                "md5": {"estado": "absent"},
                "meta": {"sample_rate": 44100, "bits_per_sample": 16, "canales": 2},
                "esp": {
                    "techo_hz": 21200,
                    "techo_rel_nyquist": 0.96,
                    "clase_corte": "no_cutoff",
                    "familias_evidencia_lossy": [],
                },
                "bdi": {"aplica": False},
            }
            for i in range(1, 5)
        ]
        dark_vhs = [
            {
                "archivo": f"0{i}_dark_vhs.flac",
                "ruta": os.path.join(folder_clean, f"0{i}_dark_vhs.flac"),
                "veredicto": "SUSPICIOUS",
                "score": 0.50,
                "problemas": ["low spectral ceiling"],
                "evidencias_forenses": [],
                "coherencia_album": False,
                "md5": {"estado": "absent"},
                "meta": {"sample_rate": 44100, "bits_per_sample": 16, "canales": 2},
                "esp": {
                    "techo_hz": 11500,
                    "techo_rel_nyquist": 0.52,
                    "clase_corte": "no_cutoff",
                    "decaimiento_acustico_suave": True,
                    "iqr_corte_hz": 120.0,
                    "ratio_huecos_mdct": 0.0,
                    "familias_evidencia_lossy": [],
                },
                "bdi": {"aplica": False},
            }
            for i in range(5, 9)
        ]
        rescued = motor_flac.apply_album_coherence(clean_refs + dark_vhs)
        self.assertEqual(rescued, 4)
        self.assertTrue(all(t["veredicto"] == "PROBABLY LOSSLESS" and t["coherencia_album"] for t in dark_vhs))

        tainted_refs = [dict(t, ruta=os.path.join(folder_tainted, t["archivo"]), coherencia_album=False) for t in clean_refs]
        tainted_dark = [
            dict(
                t,
                ruta=os.path.join(folder_tainted, t["archivo"]),
                veredicto="SUSPICIOUS",
                score=0.50,
                problemas=["low spectral ceiling"],
                evidencias_forenses=[],
                coherencia_album=False,
                esp=dict(t["esp"]),
            )
            for t in dark_vhs
        ]
        tainted_dark[0]["esp"]["mdct_lattice_detected"] = True
        tainted_dark[0]["esp"]["mdct_lattice_family"] = "MDCT-1024 (AAC/MP3/Vorbis)"
        self.assertEqual(motor_flac.apply_album_coherence(tainted_refs + tainted_dark), 0)

        # Part F: 20.00 kHz Opus cliff corroborated by sub-threshold CELT/MDCT lattice (Z >= 3.8)
        opus_sub_meta = {"sample_rate": 48000, "bits_per_sample": 16, "canales": 2}
        opus_sub_spec = {
            "techo_hz": 20060,
            "techo_rel_nyquist": 0.8358,
            "clase_corte": "no_cutoff",
            "corte_detectado": False,
            "corte_artificial": False,
            "ratio_aplica": True,
            "ratio": 0.025,
            "var_alta_db": 18.0,
            "separacion_stereo": 0.45,
            "sospecha_corte_opus_20k": True,
            "caida_opus_20k_db": 28.0,
            "modulacion_psicoacustica_20k": False,
            "iqr_corte_hz": 180.0,
            "ratio_huecos_mdct": 0.02,
            "mdct_lattice_detected": False,
            "mdct_lattice_snr": 4.15,
        }
        _, verd_opus_sub, _ = motor_flac.calculate_score(
            opus_sub_meta, opus_sub_spec, {"aplica": False}
        )
        self.assertNotIn(verd_opus_sub, GOOD_VERDICTS)

        # Part G: 24/44.1 near-Nyquist ADC transition right at >= 94% ceiling / >= 96% cutoff with low IQR
        adc_24_meta = {"sample_rate": 44100, "bits_per_sample": 24, "canales": 2}
        adc_24_spec = {
            "techo_hz": 20930,
            "techo_rel_nyquist": 0.9492,
            "clase_corte": "near_nyquist_cutoff",
            "corte_detectado": True,
            "frecuencia_corte": 21275,
            "distancia_nyquist": 0.9648,
            "corte_artificial": False,
            "ratio_aplica": True,
            "ratio": 0.12,
            "var_alta_db": 20.0,
            "separacion_stereo": 0.55,
            "gradiente_borde_db_khz": 46.7,
            "iqr_corte_hz": 96.9,
            "ratio_huecos_mdct": 0.0,
            "mdct_lattice_detected": False,
            "mdct_lattice_snr": 4.0,
        }
        adc_24_bdi = {"aplica": True, "lsbs": "active", "forense_bits": {}}
        _, verd_adc24, _ = motor_flac.calculate_score(
            adc_24_meta, adc_24_spec, adc_24_bdi
        )
        self.assertEqual(verd_adc24, "PROBABLY LOSSLESS")

        # Part H: Batch Opus lossy coherence catches 20 kHz cliff + high mod spread outliers
        opus_batch_dir = str(self.base_path / "opus_20k_spread_batch")
        os.makedirs(opus_batch_dir, exist_ok=True)
        opus_sibs = [
            {
                "archivo": f"0{i}_opus.flac",
                "ruta": os.path.join(opus_batch_dir, f"0{i}_opus.flac"),
                "veredicto": "PROBABLE UPSCALE",
                "score": 0.10,
                "problemas": ["CELT-960"],
                "evidencias_forenses": [],
                "coherencia_album": False,
                "md5": {"estado": "match"},
                "meta": {"sample_rate": 44100, "bits_per_sample": 16, "canales": 2},
                "esp": {
                    "techo_hz": 20020,
                    "techo_rel_nyquist": 0.908,
                    "mdct_lattice_detected": True,
                    "mdct_lattice_family": "CELT-960 (Opus)",
                    "mdct_lattice_snr": 9.5,
                },
                "bdi": {"aplica": False},
            }
            for i in range(1, 5)
        ]
        opus_outlier = {
            "archivo": "05_opus_cliff_only.flac",
            "ruta": os.path.join(opus_batch_dir, "05_opus_cliff_only.flac"),
            "veredicto": "GENUINE LOSSLESS",
            "score": 0.90,
            "problemas": [],
            "evidencias_forenses": [],
            "coherencia_album": False,
            "md5": {"estado": "match"},
            "meta": {"sample_rate": 44100, "bits_per_sample": 16, "canales": 2},
            "esp": {
                "techo_hz": 20015,
                "techo_rel_nyquist": 0.9077,
                "sospecha_corte_opus_20k": True,
                "brecha_modulacion_20k_db": 43.8,
                "mdct_lattice_detected": False,
                "mdct_lattice_snr": 3.18,
            },
            "bdi": {"aplica": False},
        }
        self.assertEqual(motor_flac.apply_album_coherence(opus_sibs + [opus_outlier]), 1)
        self.assertEqual(opus_outlier["veredicto"], "PROBABLE UPSCALE")
        self.assertIn("20.00 kHz shelf track", opus_outlier["album_coherence_note"])

        # Part I: Non-Opus filterbank (e.g. LAME 320 PQMF-576) + 20.00 kHz shelf
        # must report a generic codec low-pass shelf rather than claiming libopus,
        # while CELT-960 (Opus) keeps the Opus (libopus) attribution.
        mp3_20k_meta = {"sample_rate": 44100, "bits_per_sample": 16, "canales": 2}
        mp3_20k_spec = {
            "techo_hz": 20010,
            "techo_rel_nyquist": 0.9075,
            "clase_corte": "near_nyquist_cutoff",
            "corte_detectado": True,
            "frecuencia_corte": 19900,
            "distancia_nyquist": 0.9025,
            "corte_artificial": False,
            "ratio_aplica": True,
            "ratio": 0.08,
            "var_alta_db": 14.0,
            "separacion_stereo": 0.35,
            "sospecha_corte_opus_20k": True,
            "caida_opus_20k_db": 43.0,
            "mdct_lattice_detected": True,
            "mdct_lattice_family": "PQMF-576 (MP3)",
            "mdct_lattice_snr": 70.5,
        }
        sc_mp3_20k, verd_mp3_20k, probs_mp3_20k = motor_flac.calculate_score(
            mp3_20k_meta, mp3_20k_spec, {"aplica": False}
        )
        ev_mp3_20k = motor_flac.forensic_evidence(
            {"meta": mp3_20k_meta, "esp": mp3_20k_spec, "bdi": {"aplica": False}}
        )
        self.assertEqual(verd_mp3_20k, "PROBABLE UPSCALE")
        self.assertTrue(any("PQMF-576 (MP3)" in p for p in probs_mp3_20k))
        self.assertTrue(any("Steep 20.00 kHz codec low-pass shelf" in p for p in probs_mp3_20k))
        self.assertTrue(any("steep codec low-pass shelf" in e for e in ev_mp3_20k))
        self.assertNotIn("opus", " ".join(probs_mp3_20k + ev_mp3_20k).lower())

        opus_20k_spec = dict(mp3_20k_spec, mdct_lattice_family="CELT-960 (Opus)", mdct_lattice_snr=12.4)
        sc_opus_20k, verd_opus_20k, probs_opus_20k = motor_flac.calculate_score(
            mp3_20k_meta, opus_20k_spec, {"aplica": False}
        )
        ev_opus_20k = motor_flac.forensic_evidence(
            {"meta": mp3_20k_meta, "esp": opus_20k_spec, "bdi": {"aplica": False}}
        )
        self.assertAlmostEqual(sc_mp3_20k, sc_opus_20k)
        self.assertEqual(verd_opus_20k, "PROBABLE UPSCALE")
        self.assertTrue(any("Opus (libopus) fullband transcode signature" in p for p in probs_opus_20k))
        self.assertTrue(any("libopus fullband signature" in e for e in ev_opus_20k))

        # Part J: 16-in-24 boundary micro-dither caps effective bits at 16 and
        # emits a single consolidated message without claiming 24 effective bits;
        # 48 kHz far_cutoff omits the inconclusive 20.00 kHz note; Hi-Res
        # brickwall upscales attribute far_cutoff to a lower-rate source.
        import _motor_bits
        dither_bdi = _motor_bits._format_bit_depth_result(
            24, 10000, 10000, np.array([10000] + [0] * 31, dtype=np.int64),
            forensic={"es_16bit_dither_en_24": True, "lsb8_ocupacion_interior": 0.0},
        )
        self.assertEqual(dither_bdi["resolucion_efectiva_bits"], 16)
        _, verd_dith, probs_dith = motor_flac.calculate_score(
            {"sample_rate": 44100, "bits_per_sample": 24, "canales": 2},
            mp3_20k_spec,
            dither_bdi,
        )
        self.assertEqual(verd_dith, "PROBABLE UPSCALE")
        self.assertEqual(sum("micro-dither" in p for p in probs_dith), 1)
        self.assertFalse(any("effective resolution 24 bits" in p for p in probs_dith))

        opus48_spec = {
            "techo_hz": 20010,
            "techo_rel_nyquist": 20010 / 24000.0,
            "clase_corte": "far_cutoff",
            "corte_detectado": True,
            "frecuencia_corte": 19980,
            "distancia_nyquist": 19980 / 24000.0,
            "corte_artificial": True,
            "ratio_aplica": True,
            "ratio": 0.06,
            "var_alta_db": 12.0,
            "separacion_stereo": 0.30,
            "sospecha_corte_opus_20k": True,
            "caida_opus_20k_db": 48.0,
        }
        _, _, probs_op48, notes_op48 = motor_flac.calculate_score(
            {"sample_rate": 48000, "bits_per_sample": 16, "canales": 2},
            opus48_spec,
            {"aplica": False},
            include_notes=True,
        )
        self.assertTrue(any("artificial cut-off" in p for p in probs_op48))
        self.assertFalse(any("Inconclusive 20.00 kHz shelf" in n for n in notes_op48))

        up96_spec = dict(
            opus48_spec,
            techo_rel_nyquist=20010 / 48000.0,
            distancia_nyquist=19980 / 48000.0,
            muro_brickwall_nyquist=True,
            gradiente_nyquist_db_khz=53.4,
            salto_ultrasonico_db=85.1,
            sospecha_corte_opus_20k=False,
        )
        _, _, probs_up96 = motor_flac.calculate_score(
            {"sample_rate": 96000, "bits_per_sample": 24, "canales": 2},
            up96_spec,
            {"aplica": True, "lsbs": "active", "forense_bits": {}},
        )
        self.assertTrue(any("Upscaled lower-rate source (artificial cut-off" in p for p in probs_up96))

    def test_45_high_dr_24bit_converter_tail_exemption(self):
        """Genuine high-DR 24/48 warm studio masters pass Rule 5 via sub-ceiling
        converter tail while 16/44.1 -> 24/48 upscales and band-limited 24-bit
        repackages remain caught."""
        sr = 48000
        n = sr * 7
        rng = np.random.default_rng(2029)
        t = np.arange(n, dtype=np.float64) / sr

        # Warm dynamic mix (centroid < 1800 Hz, -60 dB ceiling around 14-18 kHz)
        # recorded through a genuine 24-bit / 48 kHz studio chain: body + subtle
        # room/cymbal overtones rolling through a 22.8 kHz converter filter into
        # a realistic -95 dBFS analog preamp floor.
        white = rng.standard_normal((n, 2))
        body = signal.sosfilt(
            signal.butter(2, 2200.0, btype="low", fs=sr, output="sos"),
            white * 0.14,
            axis=0,
        )
        strum_env = (0.25 + 0.75 * (np.sin(2.0 * np.pi * 1.8 * t) ** 2))[:, None]
        warm_air = signal.sosfilt(
            signal.butter(3, 14500.0, btype="low", fs=sr, output="sos"),
            rng.standard_normal((n, 2)) * 0.0045,
            axis=0,
        ) * strum_env
        converter_tail = signal.sosfilt(
            signal.butter(3, 21200.0, btype="low", fs=sr, output="sos"),
            rng.standard_normal((n, 2)) * 0.0012,
            axis=0,
        ) * strum_env
        preamp_floor = signal.sosfilt(
            signal.butter(2, 23200.0, btype="low", fs=sr, output="sos"),
            rng.standard_normal((n, 2)) * 0.000045,
            axis=0,
        )
        chords = sum(
            0.055 * np.sin(2.0 * np.pi * f0 * t)[:, None]
            for f0 in (164.81, 246.94, 329.63, 493.88)
        )
        master_24_48 = np.clip(
            body + chords + warm_air + converter_tail + preamp_floor,
            -0.85,
            0.85,
        )
        path_gen = self.base_path / "genuine_high_dr_dark_24_48.flac"
        sf.write(path_gen, master_24_48, sr, subtype="PCM_24")

        res_gen = motor_flac.analyze_file_data(str(path_gen), use_cache=False)
        self.assertIs(res_gen["esp"]["decaimiento_acustico_suave"], True)
        self.assertIs(res_gen["esp"]["native_24bit_hf_tail"], True)
        self.assertEqual(res_gen["bdi"]["lsbs"], "active")
        self.assertLess(res_gen["esp"]["techo_rel_nyquist"], 0.88)
        self.assertIn(res_gen["veredicto"], GOOD_VERDICTS)
        self.assertEqual(res_gen["problemas"], [])
        self.assertTrue(
            any("high-headroom master" in note for note in res_gen["notes"]),
            res_gen["notes"],
        )

        # Counterexample 1: 16/44.1 CD downsample upscaled back to 24/48 + TPDF
        cd_44 = signal.resample_poly(master_24_48, 147, 160, axis=0)
        cd_16 = np.round(np.clip(cd_44, -0.99, 0.99) * 32768.0) / 32768.0
        up_48 = signal.resample_poly(cd_16, 160, 147, axis=0)
        lsb24 = 1.0 / float(1 << 23)
        tpdf = (rng.random(up_48.shape) - rng.random(up_48.shape)) * lsb24
        path_up = self.base_path / "cd44_upscaled_to_24_48.flac"
        sf.write(path_up, np.clip(up_48 + tpdf, -0.99, 0.99), sr, subtype="PCM_24")

        res_up = motor_flac.analyze_file_data(str(path_up), use_cache=False)
        self.assertIs(res_up["esp"]["native_24bit_hf_tail"], False)
        self.assertNotIn(res_up["veredicto"], GOOD_VERDICTS)
        self.assertTrue(
            any("not backed by the content" in p for p in res_up["problemas"]),
            res_up["problemas"],
        )

        # Counterexample 2: band-limited signal + digital 24-bit TPDF silence floor
        bandlim = signal.sosfilt(
            signal.butter(5, 14000.0, btype="low", fs=sr, output="sos"),
            body + chords + warm_air,
            axis=0,
        )
        tpdf2 = (rng.random(bandlim.shape) - rng.random(bandlim.shape)) * lsb24
        path_bl = self.base_path / "bandlimited_redither_24_48.flac"
        sf.write(path_bl, np.clip(bandlim + tpdf2, -0.99, 0.99), sr, subtype="PCM_24")

        res_bl = motor_flac.analyze_file_data(str(path_bl), use_cache=False)
        self.assertIs(res_bl["esp"]["native_24bit_hf_tail"], False)
        self.assertNotIn(res_bl["veredicto"], GOOD_VERDICTS)
        self.assertTrue(
            any("not backed by the content" in p for p in res_bl["problemas"]),
            res_bl["problemas"],
        )

    def test_46_dark_ambient_analog_tape_subceiling_decay_and_uncut_crest_guard(self):
        """Phase 14.7: dark ambient / analog tape masters with a sub-10.5 kHz
        relative ceiling pass when calibrated dBFS tape hiss tapers smoothly to
        Nyquist, while digital-floor upscales and upper-band lossy transcodes
        stay caught; also spares uncut full-band analog crests from Gibbs."""
        from _motor_bits import _classify_from_clip_stats
        from _motor_espectral import _fast_mdct_f32, _get_vorbis_2048

        sr = 44100
        dur = 6.0
        n = int(sr * dur)
        t = np.arange(n, dtype=np.float64) / float(sr)
        rng = np.random.default_rng(4601)

        # 1. Genuine 16/44.1 dark ambient bowed-string / 2-inch analog tape drone:
        # strong low/mid harmonic body (centroid ~500-800 Hz) so -60 dB crosses
        # around 8.5-9.8 kHz, plus gentle tapering analog tape hiss (~-61..-68
        # dBFS/bin) extending smoothly all the way to 22.05 kHz.
        drone = np.zeros((n, 2), dtype=np.float64)
        for idx_f, f0 in enumerate((65.41, 98.00, 146.83, 220.00, 293.66)):
            amp = 0.22 / (idx_f + 1.0)
            drone[:, 0] += amp * np.sin(2.0 * np.pi * f0 * t + 0.1 * idx_f)
            drone[:, 1] += amp * np.sin(2.0 * np.pi * (f0 * 1.002) * t + 0.4 + 0.1 * idx_f)
        low_pad = signal.sosfilt(
            signal.butter(2, [80.0, 600.0], btype="band", fs=sr, output="sos"),
            rng.standard_normal((n, 2)),
            axis=0,
        ) * 0.25
        drone = drone + low_pad
        swells = 0.45 + 0.55 * (np.sin(2.0 * np.pi * 0.4 * t) ** 2)
        drone *= swells[:, None]

        # Mid/presence bowed texture rolling softly across 4-9.5 kHz
        bow_noise = signal.sosfilt(
            signal.butter(4, 3900.0, btype="low", fs=sr, output="sos"),
            rng.standard_normal((n, 2)),
            axis=0,
        ) * 0.035

        # Gentle analog tape hiss floor tapering ~6-7 dB from 12 kHz to 21.5 kHz
        f_axis = np.fft.rfftfreq(n, 1.0 / sr)
        hiss_spec = (
            rng.standard_normal((f_axis.size, 2))
            + 1j * rng.standard_normal((f_axis.size, 2))
        )
        tilt_db = np.where(f_axis > 6000.0, -0.65 * (f_axis - 6000.0) / 1000.0, 0.0)
        hiss_spec *= (10.0 ** (tilt_db / 20.0))[:, None]
        hiss = np.fft.irfft(hiss_spec, n, axis=0)
        hiss *= 0.00028 / max(float(np.std(hiss)), 1e-12)

        tape_mix = drone + bow_noise + hiss
        tape_mix /= np.max(np.abs(tape_mix)) * 1.15
        path_tape = self.base_path / "dark_ambient_analog_tape_16_44.flac"
        sf.write(path_tape, np.clip(tape_mix, -0.99, 0.99), sr, subtype="PCM_16")

        res_tape = motor_flac.analyze_file_data(str(path_tape), mode="full", use_cache=False)
        spectrum_tape = res_tape["esp"]
        self.assertGreaterEqual(spectrum_tape["techo_hz"], 7500)
        self.assertLess(spectrum_tape["techo_hz"], 10000)
        self.assertLess(spectrum_tape["centroide_hz"], 1200.0)
        self.assertIs(spectrum_tape["decaimiento_acustico_suave"], True)
        self.assertIs(spectrum_tape["mdct_lattice_detected"], False)
        self.assertEqual(res_tape["veredicto"], "PROBABLY LOSSLESS")
        self.assertEqual(res_tape["problemas"], [])

        # 2. Counterexample A: same low-centroid track band-limited at 9.5 kHz
        # with only 16-bit TPDF dither floor (no analog HF tape hiss) -> caught.
        dark_cut = signal.sosfilt(
            signal.ellip(6, 0.2, 85.0, 9500.0, btype="low", fs=sr, output="sos"),
            drone + bow_noise,
            axis=0,
        )
        dark_cut /= np.max(np.abs(dark_cut)) * 1.15
        lsb16 = 1.0 / 32768.0
        tpdf16 = (rng.random(dark_cut.shape) - rng.random(dark_cut.shape)) * lsb16
        path_cut = self.base_path / "dark_bandlimited_tpdf_fake.flac"
        sf.write(path_cut, np.clip(dark_cut + tpdf16, -0.99, 0.99), sr, subtype="PCM_16")

        res_cut = motor_flac.analyze_file_data(str(path_cut), mode="full", use_cache=False)
        self.assertLess(res_cut["esp"]["techo_hz"], 10000)
        self.assertIs(res_cut["esp"]["decaimiento_acustico_suave"], False)
        self.assertIn(res_cut["veredicto"], BAD_VERDICTS)

        # 3. Counterexample B: genuine dark ambient tape track quantized in the
        # upper band (12-20.5 kHz) with MDCT-1024 lapped lattice -> caught even
        # though its -60 dB ceiling sits < 10 kHz, and blocked from coherence.
        w_vorb = _get_vorbis_2048()
        k_lo = int(12000.0 / (sr / 2.0) * 1024)
        k_hi = int(20500.0 / (sr / 2.0) * 1024)
        trans_ch = []
        for ch in range(2):
            padded = np.pad(tape_mix[:, ch].astype(np.float32), (2048, 2048))
            n_fr = (padded.size - 2048) // 1024
            frames = np.lib.stride_tricks.sliding_window_view(padded, 2048)[::1024][:n_fr]
            c = _fast_mdct_f32(frames, w_vorb)
            z_mask = rng.random((n_fr, k_hi - k_lo)) < 0.45
            c[:, k_lo:k_hi] = np.where(z_mask, 0.0, c[:, k_lo:k_hi] * np.sqrt(1.0 / 0.55))
            rec = self._imdct_overlap_add(c, w_vorb)
            trans_ch.append(rec[2048 + 31 : 2048 + 31 + n])
        fake_mdct = np.column_stack(trans_ch).astype(np.float64)
        fake_mdct /= np.max(np.abs(fake_mdct)) * 1.15
        path_mdct = self.base_path / "dark_ambient_mdct_upper_band.flac"
        sf.write(path_mdct, np.clip(fake_mdct, -0.99, 0.99), sr, subtype="PCM_16")

        res_mdct = motor_flac.analyze_file_data(str(path_mdct), mode="full", use_cache=False)
        self.assertLess(res_mdct["esp"]["techo_hz"], 10000)
        self.assertIs(res_mdct["esp"]["mdct_lattice_detected"], True)
        self.assertEqual(res_mdct["veredicto"], "PROBABLE UPSCALE")
        self.assertFalse(motor_flac._deserves_album_coherence(res_mdct))

        # 4. Uncut full-band analog transient crest guard:
        # 15 single-sample crests on a gentle-rolloff full-band signal (>= 88%
        # Nyquist, no cutoff, edge < 18 dB/kHz, zero codec signature) must stay
        # clean, while the same crests on a cut or steep-edge stream stay Gibbs.
        stats_15_short = (15, 15, 15, 1, 0)
        uncut_ok = _classify_from_clip_stats(
            stats_15_short,
            ceiling_hz=20026,
            frequency_cutoff=None,
            nyquist=22050,
            edge_gradient_db_khz=11.5,
            has_codec_signature=False,
        )
        self.assertIs(uncut_ok["clipping_reconstruccion_lossy"], False)

        steep_bad = _classify_from_clip_stats(
            stats_15_short,
            ceiling_hz=19800,
            frequency_cutoff=19950,
            nyquist=22050,
            edge_gradient_db_khz=45.0,
            has_codec_signature=True,
        )
        self.assertIs(steep_bad["clipping_reconstruccion_lossy"], True)

        # Full end-to-end streaming check: uncut full-band track with 16 single-
        # sample full-scale transient crests stays GENUINE LOSSLESS.
        full_sig = _create_track_reference(sr, duration=6.0, seed=4609) * 0.85
        for idx_hit in range(16):
            full_sig[int(sr * 0.25) + idx_hit * int(sr * 0.30), 0] = 1.0
        path_crests = self.base_path / "fullband_analog_crests_uncut.flac"
        sf.write(path_crests, full_sig, sr, subtype="PCM_16")
        res_crests = motor_flac.analyze_file_data(str(path_crests), mode="full", use_cache=False)
        self.assertIs(res_crests["esp"]["clipping_reconstruccion_lossy"], False)
        self.assertEqual(res_crests["veredicto"], "GENUINE LOSSLESS")

    def test_47_clean_digital_solo_piano_vs_lowband_fakes_and_schema_270(self):
        """Phase 14.8 / schema 2.7.0: clean digital solo piano (7.5-10.5 kHz,
        silent or noise-shaped floor) passes on its own, while low-band sinc
        resamples, 16-in-24 padding and low-bitrate codecs stay caught, and
        isolated LOW_SPECTRAL_CEILING caps at SUSPICIOUS."""
        import shutil
        import subprocess
        from unittest import mock
        import _motor_espectral as spectral

        sr = 44100
        dur = 6.0
        n = int(sr * dur)
        t = np.arange(n, dtype=np.float64) / float(sr)

        def _make_clean_solo_piano(seed, dither_shaped=False):
            rng_p = np.random.default_rng(seed)
            piano_sig = np.zeros((n, 2), dtype=np.float64)
            events = [
                (0.05, 130.81, 0.24), (0.05, 196.00, 0.18), (0.05, 246.94, 0.15), (0.05, 329.63, 0.14),
                (1.20, 146.83, 0.22), (1.20, 220.00, 0.17), (1.20, 293.66, 0.15), (1.20, 392.00, 0.13),
                (2.40, 164.81, 0.23), (2.40, 246.94, 0.17), (2.40, 329.63, 0.16), (2.40, 493.88, 0.12),
                (3.60, 110.00, 0.25), (3.60, 220.00, 0.18), (3.60, 261.63, 0.16), (3.60, 392.00, 0.13),
                (4.70, 130.81, 0.22), (4.70, 196.00, 0.16), (4.70, 329.63, 0.14), (4.70, 440.00, 0.12),
            ]
            for t0, f0, vel in events:
                dt = t - t0
                mask = dt >= 0.0
                dtm = dt[mask]
                for k in range(1, 30):
                    fk = f0 * k * (1.0 + 0.00015 * k * k)
                    if fk >= 6500.0:
                        break
                    amp_k = vel * (1.0 / (k ** 0.9)) * (
                        10.0 ** (-5.5 * max(0.0, fk - 500.0) / 1000.0 / 20.0)
                    )
                    tau = max(0.12, 1.8 / (1.0 + fk / 600.0))
                    env = (1.0 - np.exp(-dtm / 0.004)) * np.exp(-dtm / tau)
                    ph0 = rng_p.uniform(0.0, 2.0 * np.pi)
                    ph1 = ph0 + rng_p.uniform(-0.35, 0.35)
                    piano_sig[mask, 0] += amp_k * env * np.sin(2.0 * np.pi * fk * dtm + ph0)
                    piano_sig[mask, 1] += amp_k * env * np.sin(2.0 * np.pi * fk * dtm + ph1)
            f_ax = np.fft.rfftfreq(n, 1.0 / sr)
            air_spec = (
                rng_p.standard_normal((f_ax.size, 2))
                + 1j * rng_p.standard_normal((f_ax.size, 2))
            )
            air_db = np.where(
                f_ax < 300.0, -40.0, -4.6 * np.maximum(0.0, f_ax - 1000.0) / 1000.0
            )
            air_spec *= (10.0 ** (air_db / 20.0))[:, None]
            air = np.fft.irfft(air_spec, n, axis=0)
            air *= 0.085 / max(float(np.std(air)), 1e-12)
            swells = 0.40 + 0.60 * (np.sin(2.0 * np.pi * 0.8 * t) ** 2)
            mix = piano_sig + air * swells[:, None]
            mix /= np.max(np.abs(mix)) * 1.18
            lsb16 = 1.0 / 32768.0
            if dither_shaped:
                d_raw = rng_p.standard_normal(mix.shape)
                d_hp = signal.sosfilt(
                    signal.butter(3, 17500.0, btype="high", fs=sr, output="sos"),
                    d_raw,
                    axis=0,
                )
                mix = mix + d_hp * (2.5 * lsb16)
            else:
                tpdf = (rng_p.random(mix.shape) - rng_p.random(mix.shape)) * lsb16
                mix = mix + tpdf
            return np.clip(mix, -0.99, 0.99)

        # 1. Clean digital solo piano (16/44.1, silent digital floor)
        piano_clean = _make_clean_solo_piano(4701, dither_shaped=False)
        p_clean = self.base_path / "ecm_solo_piano_clean_16_44.flac"
        sf.write(p_clean, piano_clean, sr, subtype="PCM_16")
        res_clean = motor_flac.analyze_file_data(str(p_clean), mode="full", use_cache=False)
        self.assertGreaterEqual(res_clean["esp"]["techo_hz"], 7500)
        self.assertLess(res_clean["esp"]["techo_hz"], 10500)
        self.assertLess(res_clean["esp"]["centroide_hz"], 1100.0)
        self.assertEqual(res_clean["esp"]["clase_corte"], motor_flac.CutoffClass.NO_CUTOFF.value)
        self.assertIs(res_clean["esp"]["decaimiento_acustico_suave"], True)
        self.assertIn(res_clean["veredicto"], GOOD_VERDICTS)
        self.assertEqual(res_clean["problemas"], [])
        self.assertNotIn("psycho_detector_error", res_clean["esp"])

        # 2. Clean digital solo piano with high-frequency noise-shaped dither
        piano_ns = _make_clean_solo_piano(4702, dither_shaped=True)
        p_ns = self.base_path / "ecm_solo_piano_nshaped_16_44.flac"
        sf.write(p_ns, piano_ns, sr, subtype="PCM_16")
        res_ns = motor_flac.analyze_file_data(str(p_ns), mode="full", use_cache=False)
        self.assertGreaterEqual(res_ns["esp"]["techo_hz"], 7500)
        self.assertLess(res_ns["esp"]["techo_hz"], 10500)
        self.assertIs(res_ns["esp"]["decaimiento_acustico_suave"], True)
        self.assertIn(res_ns["veredicto"], GOOD_VERDICTS)
        self.assertEqual(res_ns["problemas"], [])

        # 3. Full solo-piano album (4 dark tracks, zero full-band quorum) passes standalone
        album_dir = self.base_path / "ecm_solo_piano_album"
        album_dir.mkdir(parents=True, exist_ok=True)
        album_variants = [
            piano_clean,
            piano_ns,
            piano_clean[:, ::-1] * 0.92,
            -piano_ns[:, ::-1] * 0.90,
        ]
        album_files = []
        for idx, var_sig in enumerate(album_variants):
            fp = album_dir / f"{idx + 1:02d}_piano_solo.flac"
            sf.write(fp, var_sig, sr, subtype="PCM_16")
            album_files.append(str(fp))
        batch_gen, _ = motor_flac.analyze_files(album_files, "full", None, False, workers=1, use_cache=False)
        batch_res = list(batch_gen)
        self.assertEqual(len(batch_res), 4)
        for item in batch_res:
            self.assertIn(item["veredicto"], GOOD_VERDICTS, item["archivo"])
            self.assertEqual(item["problemas"], [], item["archivo"])

        # 4. Adversarial: 22.05 kHz -> 44.1 kHz sinc resample (brickwall ~11.0 kHz)
        p_22_sig = signal.resample(signal.resample(piano_clean, n // 2, axis=0), n, axis=0)
        p_22 = self.base_path / "piano_22k_sinc_upscale.flac"
        sf.write(p_22, np.clip(p_22_sig, -0.99, 0.99), sr, subtype="PCM_16")
        res_22 = motor_flac.analyze_file_data(str(p_22), mode="full", use_cache=False)
        self.assertEqual(res_22["esp"]["clase_corte"], motor_flac.CutoffClass.FAR_CUTOFF.value)
        self.assertEqual(res_22["veredicto"], motor_flac.Verdict.PROBABLE_UPSCALE.value)

        # 5. Adversarial: 16 kHz -> 44.1 kHz sinc resample (brickwall ~8.0 kHz)
        n_16 = int(round(n * 16000 / sr))
        p_16_sig = signal.resample(signal.resample(piano_clean, n_16, axis=0), n, axis=0)
        p_16 = self.base_path / "piano_16k_sinc_upscale.flac"
        sf.write(p_16, np.clip(p_16_sig, -0.99, 0.99), sr, subtype="PCM_16")
        res_16 = motor_flac.analyze_file_data(str(p_16), mode="full", use_cache=False)
        self.assertEqual(res_16["esp"]["clase_corte"], motor_flac.CutoffClass.FAR_CUTOFF.value)
        self.assertEqual(res_16["veredicto"], motor_flac.Verdict.PROBABLE_UPSCALE.value)

        # 6. Adversarial: 16-in-24 zero-padded solo piano -> blocked and condemned
        piano_16q = np.round(piano_clean * 32768.0) / 32768.0
        p_pad = self.base_path / "piano_16in24_padded.flac"
        sf.write(p_pad, np.clip(piano_16q, -0.99, 0.99), sr, subtype="PCM_24")
        res_pad = motor_flac.analyze_file_data(str(p_pad), mode="full", use_cache=False)
        self.assertEqual(res_pad["veredicto"], motor_flac.Verdict.PROBABLE_UPSCALE.value)
        self.assertIn(
            motor_flac.FindingCode.PADDED_16_IN_24.value,
            res_pad["esp"].get("finding_codes") or [],
        )

        # 7. Adversarial: real ffmpeg low-bitrate transcodes of the same solo piano
        ff_bin = shutil.which("ffmpeg")
        if ff_bin:
            src_wav = self.base_path / "piano_clean_src.wav"
            sf.write(src_wav, piano_clean, sr, subtype="PCM_16")
            codec_cases = [
                ("mp3_64k", ["-c:a", "libmp3lame", "-b:a", "64k"], ".mp3", (motor_flac.Verdict.PROBABLE_UPSCALE.value,)),
                ("mp3_96k", ["-c:a", "libmp3lame", "-b:a", "96k"], ".mp3", (motor_flac.Verdict.PROBABLE_UPSCALE.value,)),
                ("aac_64k", ["-c:a", "aac", "-b:a", "64k"], ".m4a", (motor_flac.Verdict.PROBABLE_UPSCALE.value,)),
                ("aac_96k", ["-c:a", "aac", "-b:a", "96k"], ".m4a", (motor_flac.Verdict.PROBABLE_UPSCALE.value,)),
                ("vorbis_q1", ["-c:a", "libvorbis", "-q:a", "1"], ".ogg", BAD_VERDICTS),
            ]
            for tag, enc_args, ext, allowed_verdicts in codec_cases:
                with self.subTest(codec=tag):
                    enc_p = self.base_path / f"piano_{tag}{ext}"
                    dec_p = self.base_path / f"piano_{tag}.flac"
                    subprocess.run([ff_bin, "-y", "-v", "error", "-i", str(src_wav), *enc_args, str(enc_p)], check=True)
                    subprocess.run([ff_bin, "-y", "-v", "error", "-i", str(enc_p), "-c:a", "flac", str(dec_p)], check=True)
                    r_cod = motor_flac.analyze_file_data(str(dec_p), mode="full", use_cache=False)
                    self.assertIn(r_cod["veredicto"], allowed_verdicts, r_cod["esp"])

        # 8. Invariant 2.7.0: isolated LOW_SPECTRAL_CEILING -> SUSPICIOUS (score >= 0.50)
        iso_spec = {
            "techo_hz": 12000,
            "techo_rel_nyquist": round(12000 / 22050.0, 4),
            **motor_flac.CutoffAssessment.none().to_protocol_fields(),
            "ratio_aplica": False,
            "ratio": 0.0,
            "var_alta_db": 0.0,
            "separacion_stereo": None,
            "decaimiento_acustico_suave": False,
            "rolloff_banda_limitada_genuina": False,
        }
        sc_iso, verd_iso, probs_iso = motor_flac.calculate_score(
            {"sample_rate": 44100, "bits_per_sample": 16, "canales": 2},
            iso_spec,
            {"aplica": False},
        )
        self.assertEqual(verd_iso, motor_flac.Verdict.SUSPICIOUS.value)
        self.assertGreaterEqual(sc_iso, 0.50)
        self.assertEqual(iso_spec["finding_codes"], [motor_flac.FindingCode.LOW_SPECTRAL_CEILING.value])
        self.assertEqual(len(probs_iso), 1)

        # 9. Invariant 2.7.0: ALBUM_LOSSY_CONSISTENCY + psycho_detector_error
        self.assertEqual(len(list(motor_flac.FindingCode)), 24)
        lossy_dir = self.base_path / "schema270_lossy_album"
        lossy_dir.mkdir(parents=True, exist_ok=True)
        lossy_batch = [
            {
                "archivo": f"{i + 1:02d}_mp3.flac",
                "ruta": str(lossy_dir / f"{i + 1:02d}_mp3.flac"),
                "meta": {"sample_rate": 44100, "bits_per_sample": 16, "canales": 2},
                "md5": {"estado": motor_flac.Md5Status.MATCH.value},
                "bdi": {"aplica": False},
                "veredicto": motor_flac.Verdict.PROBABLE_UPSCALE.value,
                "score": 0.20,
                "problemas": ["Lapped lattice"],
                "notes": [],
                "evidencias_forenses": [],
                "esp": {
                    "techo_hz": 20500,
                    "techo_rel_nyquist": 0.93,
                    "mdct_lattice_detected": True,
                    "mdct_lattice_snr": 15.0,
                    "mdct_lattice_family": motor_flac.CodecLatticeFamily.PQMF_576.value,
                    "finding_codes": [motor_flac.FindingCode.LAPPED_CODEC_LATTICE.value],
                },
            }
            for i in range(6)
        ]
        orig_spec = {
            "techo_hz": 14000,
            "techo_rel_nyquist": 0.635,
            "mdct_lattice_detected": False,
            "mdct_lattice_snr": 4.2,
            "finding_codes": [],
        }
        cand_lossy = {
            "archivo": "07_dark_intro.flac",
            "ruta": str(lossy_dir / "07_dark_intro.flac"),
            "meta": {"sample_rate": 44100, "bits_per_sample": 16, "canales": 2},
            "md5": {"estado": motor_flac.Md5Status.MATCH.value},
            "bdi": {"aplica": False},
            "veredicto": motor_flac.Verdict.PROBABLY_LOSSLESS.value,
            "score": 0.72,
            "problemas": [],
            "notes": [],
            "evidencias_forenses": [],
            "esp": orig_spec,
        }
        lossy_batch.append(cand_lossy)
        self.assertEqual(motor_flac.apply_album_coherence(lossy_batch), 1)
        self.assertIsNot(cand_lossy["esp"], orig_spec)
        self.assertEqual(orig_spec["finding_codes"], [])
        self.assertEqual(
            cand_lossy["esp"]["finding_codes"],
            [motor_flac.FindingCode.ALBUM_LOSSY_CONSISTENCY.value],
        )
        self.assertEqual(len(cand_lossy["esp"]["finding_codes"]), len(cand_lossy["problemas"]))

        acc_err = spectral._SpectralAccumulator(44100, is_stereo=True)
        acc_err.add(piano_clean[:16384].astype(np.float32), 0)
        with mock.patch.object(
            acc_err._psycho, "_get_cached_slices", side_effect=RuntimeError("psycho-boom")
        ):
            spec_psy_err = acc_err.result()
        self.assertEqual(spec_psy_err.get("psycho_detector_error"), "psycho-boom")



class TestMultichannelAndPhaseResilience(unittest.TestCase):
    """Regressions for v2.10.1 multichannel (>2ch), zero-energy & L=-R fixes."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_multichannel_3ch_and_51_silent_lr_analyzes_active_centre(self):
        import _motor_espectral as spectral

        sr = 44100
        dur = 3.5
        n = int(sr * dur)
        rng = np.random.default_rng(9101)
        sig = signal.sosfilt(
            signal.ellip(10, 0.1, 80.0, 16000.0, btype="low", fs=sr, output="sos"),
            rng.standard_normal(n) * 0.25,
        )
        # 3-channel (L, R, C): Front L/R digitally silent, only Centre has audio.
        data_3ch = np.zeros((n, 3), dtype=np.float64)
        data_3ch[:, 2] = np.clip(sig, -0.95, 0.95)
        p_3ch = self.tmp_path / "silent_lr_3ch_16k.flac"
        sf.write(p_3ch, data_3ch, sr, subtype="PCM_16", format="FLAC")

        res_3ch = motor_flac.analyze_file_data(str(p_3ch), use_cache=False, save_png=True)
        self.assertNotIn("error", res_3ch, res_3ch.get("error"))
        self.assertLess(res_3ch["esp"]["techo_hz"], 18000)
        self.assertEqual(res_3ch["veredicto"], "PROBABLE UPSCALE")
        png_3ch = res_3ch["esp"].get("espectrograma")
        self.assertTrue(png_3ch and os.path.isfile(png_3ch))

        # Full-band genuine 5.1 (silent Front L/R + silent LFE, wideband audio in Centre).
        sig_wb = rng.standard_normal(n) * 0.25
        data_51 = np.zeros((n, 6), dtype=np.float64)
        data_51[:, 2] = np.clip(sig_wb, -0.95, 0.95)
        p_51 = self.tmp_path / "silent_lr_51_genuine.flac"
        sf.write(p_51, data_51, sr, subtype="PCM_16", format="FLAC")

        res_51 = motor_flac.analyze_file_data(str(p_51), use_cache=False)
        self.assertNotIn("error", res_51, res_51.get("error"))
        self.assertGreater(res_51["esp"]["techo_hz"], 20000)
        self.assertEqual(res_51["veredicto"], "GENUINE LOSSLESS")

        # render_png_only also renders active Centre channel instead of failing/blank.
        meta_3ch = motor_flac.verify_metadata(str(p_3ch))
        acc = spectral._SpectralAccumulator(
            sr, is_stereo=True, save_png=True, path=str(p_3ch),
            mode="center", seconds=None,
        )
        for blk in motor_flac._read_blocks(str(p_3ch)):
            acc.add(blk, 0)
        png_only = acc.render_png_only(
            ceiling_hz=res_3ch["esp"]["techo_hz"],
            cutoff_hz=res_3ch["esp"]["frecuencia_corte"],
        )
        self.assertTrue(
            png_only.get("espectrograma") and os.path.isfile(png_only["espectrograma"])
        )
        self.assertEqual(meta_3ch["canales"], 3)

    def test_zero_energy_spectral_guard_rejects_flat_floor(self):
        import _motor_espectral as spectral

        freqs = np.fft.rfftfreq(4096, 1.0 / 44100)
        floor_pow = np.full_like(freqs, float(spectral._FLOOR_F32), dtype=np.float32)
        out = spectral._spectral_metrics(
            freqs, floor_pow, floor_pow, 44100, is_stereo=True,
            power=floor_pow, power_mid=floor_pow, power_side=floor_pow, gain_noise=34.0,
        )
        self.assertIsNotNone(out.get("error"))

        acc = spectral._SpectralAccumulator(44100, is_stereo=True)
        acc.add(np.zeros((8192, 2), dtype=np.float32), 0)
        acc_out = acc.result()
        self.assertIsNotNone(acc_out.get("error"))

    def test_antiphase_stereo_activity_and_spectrum_rescue(self):
        import math
        import _motor_espectral as spectral

        sr = 44100
        n = sr * 3
        rng = np.random.default_rng(9102)
        mono_src = rng.standard_normal(n) * 0.2
        anti = np.column_stack([mono_src, -mono_src]).astype(np.float32)

        rms_db, e_diff = spectral._activity_score(anti[:16384])
        self.assertTrue(math.isfinite(rms_db))
        self.assertGreater(rms_db, spectral.ACTIVE_RMS_THRESHOLD_DB)
        self.assertGreater(e_diff, 0.0)

        p_anti = self.tmp_path / "antiphase_hires_96.flac"
        sig_96 = rng.standard_normal(96000 * 3) * 0.15
        anti_96 = np.column_stack([sig_96, -sig_96])
        sf.write(p_anti, np.clip(anti_96, -0.95, 0.95), 96000, subtype="PCM_24")
        res_anti = motor_flac.analyze_file_data(str(p_anti), use_cache=False)
        self.assertNotIn("error", res_anti, res_anti.get("error"))
        self.assertGreater(res_anti["esp"]["techo_hz"], 40000)
        self.assertEqual(res_anti["veredicto"], "GENUINE LOSSLESS")

    def test_album_coherence_normalizes_relative_and_mixed_paths(self):
        album_dir = self.tmp_path / "MixedAlbum"
        sub_dir = album_dir / "nested"
        sub_dir.mkdir(parents=True)
        abs_album = str(album_dir)
        rel_via_dotdot = os.path.join(str(sub_dir), "..")
        batch = []
        for idx in range(7):
            batch.append({
                "archivo": f"g_{idx}.flac",
                "ruta": os.path.join(abs_album, f"g_{idx}.flac"),
                "veredicto": "GENUINE LOSSLESS",
                "score": 0.9,
                "meta": {"sample_rate": 44100, "bits_per_sample": 16},
                "md5": {"estado": "match"},
                "bdi": {"lsbs": "active"},
                "esp": {
                    "techo_hz": 22050,
                    "techo_rel_nyquist": 1.0,
                    "corte_detectado": False,
                    "corte_artificial": False,
                    "clase_corte": "no_cutoff",
                    "iqr_corte_hz": 120.0,
                    "familias_evidencia_lossy": [],
                },
                "problemas": [],
                "notes": [],
            })
        target = {
            "archivo": "acoustic_suspect.flac",
            "ruta": os.path.join(rel_via_dotdot, "acoustic_suspect.flac"),
            "veredicto": "SUSPICIOUS",
            "score": 0.55,
            "meta": {"sample_rate": 44100, "bits_per_sample": 16},
            "md5": {"estado": "match"},
            "bdi": {"lsbs": "active"},
            "esp": {
                "techo_hz": 17000,
                "techo_rel_nyquist": 0.771,
                "clase_corte": "no_cutoff",
                "corte_detectado": False,
                "corte_artificial": False,
                "decaimiento_acustico_suave": True,
                "familias_evidencia_lossy": [],
            },
            "problemas": ["Low spectral ceiling (17000 Hz)"],
            "notes": [],
        }
        batch.append(target)
        elevated = motor_flac.apply_album_coherence(batch)
        self.assertEqual(elevated, 1)
        self.assertTrue(target.get("coherencia_album"))
        self.assertEqual(target["veredicto"], "PROBABLY LOSSLESS")


class TestFindingCodesAndDiagnostics(unittest.TestCase):
    """Typed FindingCode ledger, detector error diagnostics, and real-corpus validator."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_finding_code_emission_across_calculate_score(self):
        """Every calculate_score penalty emits its canonical FindingCode while notes emit none."""
        fc = motor_flac.FindingCode
        self.assertEqual(len(list(fc)), 25)

        # Clean track with notes (low high/mid ratio, dual-mono, mastering limiter) emits zero codes.
        spec_clean = {
            "techo_hz": 22050,
            "techo_rel_nyquist": 1.0,
            "clase_corte": "no_cutoff",
            "frecuencia_corte": None,
            "distancia_nyquist": None,
            "ratio_aplica": True,
            "ratio": 0.002,
            "var_alta_db": 10.0,
            "separacion_stereo": 0.0,
            "es_dual_mono": True,
            "clipping_mastering_limitador": True,
        }
        _, _, probs_clean, notes_clean = motor_flac.calculate_score(
            {"sample_rate": 44100, "bits_per_sample": 16, "canales": 2},
            spec_clean,
            {"aplica": False},
            include_notes=True,
        )
        self.assertEqual(probs_clean, [])
        self.assertGreaterEqual(len(notes_clean), 3)
        self.assertEqual(spec_clean["finding_codes"], [])

        # CD 24-bit track triggering spectral, psychoacoustic, container, and bit-depth codes.
        spec_cd = {
            "techo_hz": 12000,
            "techo_rel_nyquist": 12000 / 22050.0,
            "clase_corte": "far_cutoff",
            "frecuencia_corte": 15000,
            "distancia_nyquist": 15000 / 22050.0,
            "ratio_aplica": True,
            "ratio": 0.02,
            "var_alta_db": 5.0,
            "separacion_stereo": 0.2,
            "sospecha_joint_stereo": True,
            "iqr_corte_hz": 900.0,
            "ratio_huecos_mdct": 0.25,
            "islas_mdct_sfb21": True,
            "sospecha_excitador_armonico": True,
            "modulacion_psicoacustica_20k": True,
            "mdct_lattice_detected": True,
            "mdct_lattice_family": "PQMF-576 (MP3)",
            "mdct_lattice_snr": 18.0,
            "sospecha_corte_opus_20k": True,
            "caida_opus_20k_db": 50.0,
            "clipping_reconstruccion_lossy": True,
        }
        bdi_comb = {
            "aplica": True,
            "lsbs": "partial",
            "forense_bits": {
                "es_padded_16_en_24": True,
                "es_24bit_escalado_en_peine": True,
                "es_16bit_dither_en_24": True,
            },
        }
        _, _, probs_cd = motor_flac.calculate_score(
            {"sample_rate": 44100, "bits_per_sample": 24, "canales": 2},
            spec_cd,
            bdi_comb,
        )
        self.assertEqual(len(spec_cd["finding_codes"]), len(probs_cd))
        expected_cd_subset = {
            fc.LOW_SPECTRAL_CEILING.value,
            fc.ARTIFICIAL_CUTOFF.value,
            fc.JOINT_STEREO_COLLAPSE.value,
            fc.MDCT_SPECTRAL_HOLES.value,
            fc.MP3_SFB21_ISLANDS.value,
            fc.EXCITER_RECTIFICATION.value,
            fc.LAPPED_CODEC_LATTICE.value,
            fc.OPUS_20K_CUTOFF.value,
            fc.GIBBS_LOSSY_CLIPPING.value,
            fc.CONTAINER_NOT_BACKED.value,
            fc.PARTIAL_LSBS.value,
            fc.PADDED_16_IN_24.value,
            fc.GAIN_COMB_24.value,
            fc.DITHER_16_IN_24.value,
        }
        self.assertTrue(expected_cd_subset.issubset(set(spec_cd["finding_codes"])))

        # Hi-Res track covering HIRES_BELOW_CD, HIRES_NO_ULTRASONIC, NYQUIST_BRICKWALL,
        # ALIASING_MIRROR, HIRES_UPSCALE_CUTOFF, CUTOFF_JITTER, SBR_REPLICA,
        # PSYCHOACOUSTIC_MOD_20K, and EMPTY_LSBS.
        spec_hr = {
            "techo_hz": 16000,
            "techo_rel_nyquist": 16000 / 48000.0,
            "clase_corte": "far_cutoff",
            "frecuencia_corte": 22050,
            "distancia_nyquist": 22050 / 48000.0,
            "muro_brickwall_nyquist": True,
            "espejo_aliasing_detectado": True,
            "iqr_corte_hz": 2000.0,
            "sbr_qmf_detectado": True,
            "modulacion_psicoacustica_20k": True,
        }
        _, _, probs_hr = motor_flac.calculate_score(
            {"sample_rate": 96000, "bits_per_sample": 24, "canales": 2},
            spec_hr,
            {"aplica": True, "lsbs": "empty", "forense_bits": {}},
        )
        self.assertEqual(len(spec_hr["finding_codes"]), len(probs_hr))
        expected_hr_subset = {
            fc.HIRES_BELOW_CD.value,
            fc.HIRES_NO_ULTRASONIC.value,
            fc.NYQUIST_BRICKWALL.value,
            fc.ALIASING_MIRROR.value,
            fc.HIRES_UPSCALE_CUTOFF.value,
            fc.CUTOFF_JITTER.value,
            fc.SBR_REPLICA.value,
            fc.PSYCHOACOUSTIC_MOD_20K.value,
            fc.EMPTY_LSBS.value,
        }
        self.assertTrue(expected_hr_subset.issubset(set(spec_hr["finding_codes"])))

    def test_typed_album_coherence_clearing_and_legacy_fallback(self):
        """Typed FindingCode gates rescue, clears codes on shallow copy, and preserves legacy fallback."""
        fc = motor_flac.FindingCode
        album_dir = self.tmp_path / "typed_coherence"
        album_dir.mkdir(parents=True, exist_ok=True)

        def _base_track(sr=44100, bits=16, ceiling=14000, md5_state="match"):
            nyq = sr / 2.0
            return {
                "archivo": "candidate.flac",
                "ruta": str(album_dir / "candidate.flac"),
                "veredicto": "SUSPICIOUS",
                "score": 0.50,
                "meta": {"sample_rate": sr, "bits_per_sample": bits, "canales": 2},
                "md5": {"estado": md5_state},
                "bdi": {"aplica": bits > 16, "lsbs": "active", "forense_bits": {}},
                "esp": {
                    "error": None,
                    "techo_hz": ceiling,
                    "techo_rel_nyquist": round(ceiling / nyq, 4),
                    "clase_corte": "no_cutoff",
                    "corte_artificial": False,
                    "centroide_hz": 400.0,
                    "iqr_corte_hz": 200.0,
                    "ratio_huecos_mdct": 0.0,
                    "gradiente_borde_db_khz": 12.0,
                    "mdct_lattice_snr": 1.5,
                    "finding_codes": [fc.LOW_SPECTRAL_CEILING.value],
                    "familias_evidencia_lossy": ["spectral_cutoff"],
                },
                "problemas": ["unrelated custom text"],
                "notes": [],
            }

        # Typed LOW_SPECTRAL_CEILING rescues even when prose text does not match legacy substrings.
        t_low = _base_track()
        self.assertTrue(motor_flac._deserves_album_coherence(t_low))

        # Non-rescuable typed code is rejected even when prose text looks like a legacy allowed string.
        t_bad = _base_track()
        t_bad["problemas"] = ["low spectral ceiling"]
        t_bad["esp"]["finding_codes"] = [fc.ARTIFICIAL_CUTOFF.value]
        self.assertFalse(motor_flac._deserves_album_coherence(t_bad))

        # Conditional CONTAINER_NOT_BACKED at 24/44.1 requires dark centroid (< 1800 Hz).
        t_24 = _base_track(sr=44100, bits=24, ceiling=15000)
        t_24["esp"]["finding_codes"] = [fc.CONTAINER_NOT_BACKED.value]
        self.assertTrue(motor_flac._deserves_album_coherence(t_24))
        t_24_bright = _base_track(sr=44100, bits=24, ceiling=15000)
        t_24_bright["esp"]["centroide_hz"] = 2400.0
        t_24_bright["esp"]["finding_codes"] = [fc.CONTAINER_NOT_BACKED.value]
        self.assertFalse(motor_flac._deserves_album_coherence(t_24_bright))

        # Conditional CUTOFF_JITTER requires dark_sparse_iqr_ok INCLUDING md5_state == "match".
        t_iqr = _base_track(sr=44100, bits=16, ceiling=11500, md5_state="match")
        t_iqr["esp"]["iqr_corte_hz"] = 780.0
        t_iqr["esp"]["finding_codes"] = [fc.CUTOFF_JITTER.value]
        self.assertTrue(motor_flac._deserves_album_coherence(t_iqr))
        t_iqr_no_md5 = _base_track(sr=44100, bits=16, ceiling=11500, md5_state="absent")
        t_iqr_no_md5["esp"]["iqr_corte_hz"] = 780.0
        t_iqr_no_md5["esp"]["finding_codes"] = [fc.CUTOFF_JITTER.value]
        self.assertFalse(motor_flac._deserves_album_coherence(t_iqr_no_md5))

        # Conditional Hi-Res codes rescue on active LSBs at 96 kHz.
        t_hr = _base_track(sr=96000, bits=24, ceiling=16000)
        t_hr["problemas"] = ["below cd", "no ultrasonic"]
        t_hr["esp"]["finding_codes"] = [fc.HIRES_BELOW_CD.value, fc.HIRES_NO_ULTRASONIC.value]
        self.assertTrue(motor_flac._deserves_album_coherence(t_hr))

        # Legacy substring fallback still works when finding_codes is absent.
        t_legacy = _base_track()
        del t_legacy["esp"]["finding_codes"]
        t_legacy["problemas"] = ["Low spectral ceiling (14.0 kHz)"]
        self.assertTrue(motor_flac._deserves_album_coherence(t_legacy))

        # apply_album_coherence clears finding_codes on a shallow copy without mutating the original spectrum dict.
        original_spectrum = t_low["esp"]
        siblings = [
            {
                "archivo": f"0{i}_clean.flac",
                "ruta": str(album_dir / f"0{i}_clean.flac"),
                "veredicto": "GENUINE LOSSLESS",
                "score": 0.95,
                "meta": {"sample_rate": 44100, "bits_per_sample": 16, "canales": 2},
                "md5": {"estado": "match"},
                "bdi": {"aplica": False},
                "esp": {
                    "techo_hz": 22050,
                    "techo_rel_nyquist": 1.0,
                    "clase_corte": "no_cutoff",
                    "familias_evidencia_lossy": [],
                    "finding_codes": [],
                },
                "problemas": [],
                "notes": [],
            }
            for i in range(1, 5)
        ]
        rescued = motor_flac.apply_album_coherence([t_low] + siblings)
        self.assertEqual(rescued, 1)
        self.assertEqual(t_low["problemas"], [])
        self.assertEqual(t_low["esp"]["finding_codes"], [])
        self.assertIsNot(t_low["esp"], original_spectrum)
        self.assertEqual(original_spectrum["finding_codes"], [fc.LOW_SPECTRAL_CEILING.value])

    def test_detector_error_diagnostics_and_fallback_propagation(self):
        """Detectors never crash on runtime errors and attach explicit error keys when failing."""
        from unittest import mock
        import _motor_espectral as spectral

        rng = np.random.default_rng(7701)
        sig = rng.standard_normal(44100 * 2).astype(np.float32) * 0.25

        # Clean runs must omit error keys.
        clean_sbr = spectral._detect_sbr_qmf_replication(sig, 44100)
        self.assertNotIn("sbr_detector_error", clean_sbr)
        clean_mdct = spectral._eval_lapped_codec_grid(sig, 44100)
        self.assertNotIn("mdct_detector_error", clean_mdct)

        # Injected exception inside SBR detector.
        with mock.patch.object(spectral, "_rfft_f32", side_effect=RuntimeError("boom-sbr")):
            err_sbr = spectral._detect_sbr_qmf_replication(sig, 44100)
        self.assertIs(err_sbr["sbr_qmf_detectado"], False)
        self.assertEqual(err_sbr.get("sbr_detector_error"), "RuntimeError: boom-sbr")

        # Injected exception inside lapped-codec grid detector.
        with mock.patch.object(
            spectral, "_select_continuous_subwindow", side_effect=RuntimeError("boom-mdct")
        ):
            err_mdct = spectral._eval_lapped_codec_grid(sig, 44100)
        self.assertIs(err_mdct["mdct_lattice_detected"], False)
        self.assertEqual(err_mdct.get("mdct_detector_error"), "RuntimeError: boom-mdct")

        # Fallback propagation when 7.0 kHz <= ceiling_hz < 17.5 kHz and fullband probe errors.
        def _fake_eval(mono_in, sr_in, ceiling_hz=None):
            if ceiling_hz is not None:
                return {
                    "mdct_lattice_detected": False,
                    "mdct_lattice_snr": 1.2,
                    "mdct_lattice_family": None,
                }
            return {
                "mdct_lattice_detected": False,
                "mdct_lattice_snr": 0.0,
                "mdct_lattice_family": None,
                "mdct_detector_error": "RuntimeError: fallback-boom",
            }

        with mock.patch.object(spectral, "_eval_lapped_codec_grid", side_effect=_fake_eval):
            prop_res = spectral._detect_lapped_codec_grid(sig, 44100, ceiling_hz=12000.0)
        self.assertIs(prop_res["mdct_lattice_detected"], False)
        self.assertEqual(prop_res.get("mdct_detector_error"), "RuntimeError: fallback-boom")

    def test_real_corpus_manifest_pass_fail_and_missing_skip(self):
        """--real-corpus validates passing albums, flags mismatches with exit 1, and skips missing paths."""
        import contextlib
        import io
        import json
        import tests.benchmark_validacion as bench

        sr = 44100
        album_dir = self.tmp_path / "synthetic_album"
        album_dir.mkdir(parents=True, exist_ok=True)
        for idx in (1, 2):
            sig = _create_track_reference(sr, duration=3.5, seed=8800 + idx)
            sf.write(album_dir / f"0{idx}_clean.flac", sig, sr, subtype="PCM_16")
        cut_sig = signal.sosfilt(
            signal.ellip(10, 0.1, 80.0, 15500.0, btype="low", fs=sr, output="sos"),
            _create_track_reference(sr, duration=3.5, seed=8803),
            axis=0,
        )
        sf.write(album_dir / "03_cut.flac", np.clip(cut_sig, -0.98, 0.98), sr, subtype="PCM_16")

        manifest_pass = self.tmp_path / "manifest_pass.json"
        manifest_pass.write_text(
            json.dumps({
                "album_checks": [
                    {
                        "label": "Synthetic Album",
                        "path": str(album_dir),
                        "expected_clean": 2,
                        "expected_flagged": {"03_cut.flac": "PROBABLE UPSCALE"},
                    },
                    {
                        "label": "Unmounted Drive Album",
                        "path": str(self.tmp_path / "nonexistent_album"),
                        "expected_clean": 99,
                        "expected_flagged": {},
                    },
                ],
                "info_globs": [str(self.tmp_path / "no_match_*")],
            }),
            encoding="utf-8",
        )
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc_pass = bench.main(["--real-corpus", str(manifest_pass)])
        self.assertEqual(rc_pass, 0, buf.getvalue())
        self.assertIn("[PASS] Synthetic Album", buf.getvalue())
        self.assertIn("[SKIP] Unmounted Drive Album", buf.getvalue())

        manifest_fail = self.tmp_path / "manifest_fail.json"
        manifest_fail.write_text(
            json.dumps({
                "album_checks": [
                    {
                        "label": "Synthetic Album Mismatch",
                        "path": str(album_dir),
                        "expected_clean": 3,
                        "expected_flagged": {},
                    }
                ],
                "info_globs": [],
            }),
            encoding="utf-8",
        )
        buf_fail = io.StringIO()
        with contextlib.redirect_stdout(buf_fail):
            rc_fail = bench.main(["--real-corpus", str(manifest_fail)])
        self.assertEqual(rc_fail, 1, buf_fail.getvalue())
        self.assertIn("[FAIL] Synthetic Album Mismatch", buf_fail.getvalue())

        buf_missing = io.StringIO()
        with contextlib.redirect_stdout(buf_missing):
            rc_missing = bench.main(["--real-corpus", str(self.tmp_path / "missing.json")])
        self.assertEqual(rc_missing, 0, buf_missing.getvalue())
        self.assertIn("SKIP", buf_missing.getvalue())

    def test_cutoff_assessment_verdict_and_pure_score_ledger_invariants(self):
        """CutoffAssessment, Verdict <-> ENGINE_VERDICTS, and pure _ScoreLedger.evaluate()."""
        none_cut = motor_flac.CutoffAssessment.none()
        self.assertIs(none_cut.cutoff_class, motor_flac.CutoffClass.NO_CUTOFF)
        self.assertFalse(none_cut.detected)
        self.assertFalse(none_cut.is_artificial)
        self.assertEqual(
            none_cut.to_protocol_fields(),
            {
                "clase_corte": "no_cutoff",
                "corte_detectado": False,
                "frecuencia_corte": None,
                "distancia_nyquist": None,
                "corte_artificial": False,
            },
        )

        near_cut = motor_flac.CutoffAssessment.from_cutoff(20000.0, 22050.0)
        self.assertIs(near_cut.cutoff_class, motor_flac.CutoffClass.NEAR_NYQUIST)
        self.assertTrue(near_cut.detected)
        self.assertFalse(near_cut.is_artificial)
        self.assertEqual(near_cut.to_protocol_fields()["frecuencia_corte"], 20000)
        self.assertAlmostEqual(near_cut.to_protocol_fields()["distancia_nyquist"], 0.907, places=3)

        far_cut = motor_flac.CutoffAssessment.from_cutoff(16000.0, 22050.0)
        self.assertIs(far_cut.cutoff_class, motor_flac.CutoffClass.FAR_CUTOFF)
        self.assertTrue(far_cut.detected)
        self.assertTrue(far_cut.is_artificial)
        self.assertTrue(far_cut.to_protocol_fields()["corte_artificial"])

        self.assertEqual(
            tuple(v.value for v in motor_flac.Verdict),
            motor_flac.ENGINE_VERDICTS,
        )

        ledger = motor_flac._ScoreLedger()
        ledger.add_votes(3.0, 6.0)
        ledger.note("neutral note")
        ledger.penalize(
            "artificial cutoff",
            0.40,
            family=motor_flac.EvidenceFamily.SPECTRAL_CUTOFF,
            code=motor_flac.FindingCode.ARTIFICIAL_CUTOFF,
        )
        self.assertTrue(ledger.is_blocked)
        spec_probe = {"techo_hz": 16000}
        spec_snapshot = dict(spec_probe)
        evaluation = ledger.evaluate()
        self.assertIsInstance(evaluation, motor_flac.ScoreEvaluation)
        self.assertEqual(spec_probe, spec_snapshot)  # evaluate() must not mutate spectrum
        self.assertIs(evaluation.verdict, motor_flac.Verdict.PROBABLE_UPSCALE)
        self.assertTrue(evaluation.corroborated)
        self.assertEqual(evaluation.finding_codes, ("artificial_cutoff",))

        score, verdict, problems, notes = ledger.finalize(spec_probe, include_notes=True)
        self.assertAlmostEqual(score, evaluation.score)
        self.assertEqual(verdict, evaluation.verdict.value)
        self.assertEqual(problems, list(evaluation.problems))
        self.assertEqual(notes, list(evaluation.notes))
        self.assertEqual(spec_probe["finding_codes"], ["artificial_cutoff"])
        self.assertTrue(spec_probe["corroboracion_multifamilia"])

    def test_album_coherence_is_idempotent_for_rescue_and_lossy_conviction(self):
        """Calling apply_album_coherence twice on the same batch returns 0 on the
        second call and never duplicates notes, problems, or evidence."""
        # 1. Lossless rescue branch
        rescue_dir = self.tmp_path / "idem_rescue"
        rescue_dir.mkdir(parents=True, exist_ok=True)
        rescue_batch = []
        for idx in range(7):
            rescue_batch.append({
                "archivo": f"{idx + 1:02d}_clean.flac",
                "ruta": str(rescue_dir / f"{idx + 1:02d}_clean.flac"),
                "meta": {"sample_rate": 44100, "bits_per_sample": 16, "canales": 2},
                "md5": {"estado": motor_flac.Md5Status.MATCH.value},
                "bdi": {"aplica": False},
                "veredicto": motor_flac.Verdict.GENUINE_LOSSLESS.value,
                "score": 0.95,
                "problemas": [],
                "notes": [],
                "evidencias_forenses": [],
                "esp": {
                    "techo_hz": 21500,
                    "techo_rel_nyquist": 0.975,
                    **motor_flac.CutoffAssessment.none().to_protocol_fields(),
                    "familias_evidencia_lossy": [],
                    "finding_codes": [],
                },
            })
        dark_track = {
            "archivo": "08_acoustic.flac",
            "ruta": str(rescue_dir / "08_acoustic.flac"),
            "meta": {"sample_rate": 44100, "bits_per_sample": 16, "canales": 2},
            "md5": {"estado": motor_flac.Md5Status.MATCH.value},
            "bdi": {"aplica": False},
            "veredicto": motor_flac.Verdict.SUSPICIOUS.value,
            "score": 0.50,
            "problemas": ["Severely truncated high frequencies (spectral ceiling 12.0 kHz)"],
            "notes": [],
            "evidencias_forenses": ["Spectral ceiling 12.0 kHz"],
            "esp": {
                "techo_hz": 12000,
                "techo_rel_nyquist": 0.544,
                **motor_flac.CutoffAssessment.none().to_protocol_fields(),
                "iqr_corte_hz": 120.0,
                "ratio_huecos_mdct": 0.0,
                "familias_evidencia_lossy": [motor_flac.EvidenceFamily.SPECTRAL_CUTOFF.value],
                "finding_codes": [motor_flac.FindingCode.LOW_SPECTRAL_CEILING.value],
            },
        }
        rescue_batch.append(dark_track)

        first_rescue = motor_flac.apply_album_coherence(rescue_batch)
        self.assertEqual(first_rescue, 1)
        self.assertEqual(dark_track["veredicto"], motor_flac.Verdict.PROBABLY_LOSSLESS.value)
        self.assertTrue(dark_track["coherencia_album"])
        notes_after_first = list(dark_track["notes"])
        evid_after_first = list(dark_track["evidencias_forenses"])

        second_rescue = motor_flac.apply_album_coherence(rescue_batch)
        self.assertEqual(second_rescue, 0)
        self.assertEqual(dark_track["notes"], notes_after_first)
        self.assertEqual(dark_track["evidencias_forenses"], evid_after_first)
        self.assertEqual(dark_track["problemas"], [])

        # 2. Lossy conviction branch
        lossy_dir = self.tmp_path / "idem_lossy"
        lossy_dir.mkdir(parents=True, exist_ok=True)
        lossy_batch = []
        for idx in range(7):
            lossy_batch.append({
                "archivo": f"{idx + 1:02d}_mp3.flac",
                "ruta": str(lossy_dir / f"{idx + 1:02d}_mp3.flac"),
                "meta": {"sample_rate": 44100, "bits_per_sample": 16, "canales": 2},
                "md5": {"estado": motor_flac.Md5Status.MATCH.value},
                "bdi": {"aplica": False},
                "veredicto": motor_flac.Verdict.PROBABLE_UPSCALE.value,
                "score": 0.20,
                "problemas": ["Lapped lattice"],
                "notes": [],
                "evidencias_forenses": [],
                "esp": {
                    "techo_hz": 20500,
                    "techo_rel_nyquist": 0.93,
                    "mdct_lattice_detected": True,
                    "mdct_lattice_snr": 15.0,
                    "mdct_lattice_family": motor_flac.CodecLatticeFamily.PQMF_576.value,
                },
            })
        hidden_lossy = {
            "archivo": "08_intro.flac",
            "ruta": str(lossy_dir / "08_intro.flac"),
            "meta": {"sample_rate": 44100, "bits_per_sample": 16, "canales": 2},
            "md5": {"estado": motor_flac.Md5Status.MATCH.value},
            "bdi": {"aplica": False},
            "veredicto": motor_flac.Verdict.PROBABLY_LOSSLESS.value,
            "score": 0.72,
            "problemas": [],
            "notes": [],
            "evidencias_forenses": [],
            "esp": {
                "techo_hz": 14000,
                "techo_rel_nyquist": 0.635,
                "mdct_lattice_detected": False,
                "mdct_lattice_snr": 4.1,
            },
        }
        lossy_batch.append(hidden_lossy)

        first_convict = motor_flac.apply_album_coherence(lossy_batch)
        self.assertEqual(first_convict, 1)
        self.assertEqual(hidden_lossy["veredicto"], motor_flac.Verdict.PROBABLE_UPSCALE.value)
        probs_after_first = list(hidden_lossy["problemas"])
        evid_lossy_after_first = list(hidden_lossy["evidencias_forenses"])

        second_convict = motor_flac.apply_album_coherence(lossy_batch)
        self.assertEqual(second_convict, 0)
        self.assertEqual(hidden_lossy["problemas"], probs_after_first)
        self.assertEqual(hidden_lossy["evidencias_forenses"], evid_lossy_after_first)

    def test_48_hires_assessment_mutual_exclusion_and_lattice_error_propagation(self):
        """HiresAssessment enforces mutually exclusive morphologies and
        _SpectralAccumulator preserves fallback mdct_detector_error when snr == 0.0."""
        import _motor_espectral
        from unittest import mock

        sr = 96000
        freqs = np.fft.rfftfreq(4096, 1.0 / sr)
        # Construct a 96 kHz spectrum with strong DSD64 noise shaping (smooth
        # rising ramp across 24-40 kHz, jump_db < 25 dB) AND a sharp 22 kHz step
        # plus symmetric sidebands around 22.05 kHz: DSD classification must stay
        # mutually exclusive with NYQUIST_BRICKWALL and ALIASING_MIRROR.
        power_db = np.full_like(freqs, -50.0, dtype=np.float64)
        power_db[(freqs >= 15000) & (freqs <= 21200)] = -52.0
        power_db[(freqs > 21200) & (freqs < 24000)] = -72.0
        mask_ramp = (freqs >= 24000) & (freqs <= 40000)
        power_db[mask_ramp] = -70.0 + 22.0 * ((freqs[mask_ramp] - 24000.0) / 16000.0)
        power_db[freqs > 40000] = -48.0

        assessment = _motor_espectral._assess_hires_authenticity(
            freqs, power_db, sr
        )
        self.assertIsInstance(assessment, motor_flac.HiresAssessment)
        self.assertIs(assessment.morphology, motor_flac.HiresMorphology.DSD_TRANSFER)
        proto = assessment.to_protocol_fields()
        self.assertTrue(proto["es_transferencia_dsd"])
        self.assertFalse(proto["muro_brickwall_nyquist"])
        self.assertFalse(proto["espejo_aliasing_detectado"])
        self.assertFalse(proto["rolloff_analogico_genuino"])

        # Fallback probe error propagation when mdct_lattice_snr == 0.0
        rng = np.random.default_rng(4848)
        n = 32768
        left = rng.uniform(-0.3, 0.3, n).astype(np.float32)
        right = (left * 0.1).astype(np.float32)
        stereo = np.column_stack([left, right])
        acc = _motor_espectral._SpectralAccumulator(44100, is_stereo=True)
        acc.add(stereo, 0)

        call_idx = {"n": 0}

        def _fake_lattice_probe(_blk, _sr, ceiling_hz=None):
            call_idx["n"] += 1
            if call_idx["n"] == 2:
                return {
                    "mdct_lattice_detected": False,
                    "mdct_lattice_snr": 0.0,
                    "mdct_lattice_family": None,
                    "mdct_detector_error": "fallback boom",
                }
            return {
                "mdct_lattice_detected": False,
                "mdct_lattice_snr": 0.0,
                "mdct_lattice_family": None,
            }

        with mock.patch.object(
            _motor_espectral, "_detect_lapped_codec_grid", side_effect=_fake_lattice_probe
        ):
            res = acc.result()
        self.assertEqual(res.get("mdct_detector_error"), "fallback boom")
        self.assertGreaterEqual(call_idx["n"], 2)

    def test_49_verdict_cap_all_levels_and_album_coherence_actions(self):
        """_ScoreLedger.evaluate enforces all 4 VerdictCap levels and
        apply_album_coherence stamps explicit AlbumCoherenceAction values."""
        # 1. VerdictCap.GENUINE_LOSSLESS allows GENUINE_LOSSLESS
        l_gen = motor_flac._ScoreLedger()
        l_gen.add_votes(5.0, 5.0)
        self.assertIs(l_gen.evaluate().verdict, motor_flac.Verdict.GENUINE_LOSSLESS)

        # 2. VerdictCap.PROBABLY_LOSSLESS clamps GENUINE_LOSSLESS -> PROBABLY_LOSSLESS
        l_prob = motor_flac._ScoreLedger()
        l_prob.add_votes(5.0, 5.0)
        l_prob.lower_cap(motor_flac.VerdictCap.PROBABLY_LOSSLESS)
        self.assertIs(l_prob.evaluate().verdict, motor_flac.Verdict.PROBABLY_LOSSLESS)

        # 3. VerdictCap.SUSPICIOUS caps high score at SUSPICIOUS (or PROBABLE_UPSCALE if low & corroborated)
        l_susp = motor_flac._ScoreLedger()
        l_susp.add_votes(5.0, 5.0)
        l_susp.lower_cap(motor_flac.VerdictCap.SUSPICIOUS)
        self.assertIs(l_susp.evaluate().verdict, motor_flac.Verdict.SUSPICIOUS)

        # 4. VerdictCap.PROBABLE_UPSCALE forces PROBABLE_UPSCALE when corroborated, else SUSPICIOUS
        l_up_uncorr = motor_flac._ScoreLedger()
        l_up_uncorr.add_votes(5.0, 5.0)
        l_up_uncorr.lower_cap(motor_flac.VerdictCap.PROBABLE_UPSCALE)
        self.assertIs(l_up_uncorr.evaluate().verdict, motor_flac.Verdict.SUSPICIOUS)

        l_up_corr = motor_flac._ScoreLedger()
        l_up_corr.add_votes(5.0, 5.0)
        l_up_corr.has_hard_proof = True
        l_up_corr.lower_cap(motor_flac.VerdictCap.PROBABLE_UPSCALE)
        self.assertIs(l_up_corr.evaluate().verdict, motor_flac.Verdict.PROBABLE_UPSCALE)

        # 5. AlbumCoherenceAction stamped on rescued and convicted tracks
        rescue_dir = self.tmp_path / "action_rescue"
        rescue_dir.mkdir(parents=True, exist_ok=True)
        rescue_batch = [
            {
                "archivo": f"{i:02d}_ok.flac",
                "ruta": str(rescue_dir / f"{i:02d}_ok.flac"),
                "meta": {"sample_rate": 44100, "bits_per_sample": 16, "canales": 2},
                "md5": {"estado": motor_flac.Md5Status.MATCH.value},
                "bdi": {"aplica": False},
                "veredicto": motor_flac.Verdict.GENUINE_LOSSLESS.value,
                "score": 1.0,
                "problemas": [],
                "notes": [],
                "evidencias_forenses": [],
                "esp": {"techo_hz": 21500, "techo_rel_nyquist": 0.975, "familias_evidencia_lossy": []},
            }
            for i in range(1, 5)
        ]
        rescued = {
            "archivo": "05_dark.flac",
            "ruta": str(rescue_dir / "05_dark.flac"),
            "meta": {"sample_rate": 44100, "bits_per_sample": 16, "canales": 2},
            "md5": {"estado": motor_flac.Md5Status.MATCH.value},
            "bdi": {"aplica": False},
            "veredicto": motor_flac.Verdict.SUSPICIOUS.value,
            "score": 0.50,
            "problemas": ["Low ceiling"],
            "notes": [],
            "evidencias_forenses": [],
            "esp": {
                "techo_hz": 12000,
                "techo_rel_nyquist": 0.544,
                **motor_flac.CutoffAssessment.none().to_protocol_fields(),
                "iqr_corte_hz": 100.0,
                "ratio_huecos_mdct": 0.0,
                "familias_evidencia_lossy": [motor_flac.EvidenceFamily.SPECTRAL_CUTOFF.value],
                "finding_codes": [motor_flac.FindingCode.LOW_SPECTRAL_CEILING.value],
            },
        }
        rescue_batch.append(rescued)
        self.assertEqual(motor_flac.apply_album_coherence(rescue_batch), 1)
        self.assertTrue(rescued["coherencia_album"])
        self.assertEqual(
            rescued["album_coherence_action"],
            motor_flac.AlbumCoherenceAction.RESCUED_LOSSLESS.value,
        )

        lossy_dir = self.tmp_path / "action_lossy"
        lossy_dir.mkdir(parents=True, exist_ok=True)
        lossy_batch = [
            {
                "archivo": f"{i:02d}_mp3.flac",
                "ruta": str(lossy_dir / f"{i:02d}_mp3.flac"),
                "meta": {"sample_rate": 44100, "bits_per_sample": 16, "canales": 2},
                "md5": {"estado": motor_flac.Md5Status.MATCH.value},
                "bdi": {"aplica": False},
                "veredicto": motor_flac.Verdict.PROBABLE_UPSCALE.value,
                "score": 0.2,
                "problemas": ["Lattice"],
                "notes": [],
                "evidencias_forenses": [],
                "esp": {
                    "techo_hz": 20500,
                    "techo_rel_nyquist": 0.93,
                    "mdct_lattice_detected": True,
                    "mdct_lattice_snr": 15.0,
                    "mdct_lattice_family": motor_flac.CodecLatticeFamily.PQMF_576.value,
                },
            }
            for i in range(1, 5)
        ]
        convicted = {
            "archivo": "05_quiet.flac",
            "ruta": str(lossy_dir / "05_quiet.flac"),
            "meta": {"sample_rate": 44100, "bits_per_sample": 16, "canales": 2},
            "md5": {"estado": motor_flac.Md5Status.MATCH.value},
            "bdi": {"aplica": False},
            "veredicto": motor_flac.Verdict.PROBABLY_LOSSLESS.value,
            "score": 0.72,
            "problemas": [],
            "notes": [],
            "evidencias_forenses": [],
            "esp": {
                "techo_hz": 14000,
                "techo_rel_nyquist": 0.635,
                "mdct_lattice_detected": False,
                "mdct_lattice_snr": 4.2,
            },
        }
        lossy_batch.append(convicted)
        self.assertEqual(motor_flac.apply_album_coherence(lossy_batch), 1)
        self.assertTrue(convicted["coherencia_album"])
        self.assertEqual(
            convicted["album_coherence_action"],
            motor_flac.AlbumCoherenceAction.CONVICTED_LOSSY.value,
        )

    def test_50_mdct_512_ac3_eac3_lapped_lattice_detected(self):
        """Engine 15.0: MDCT-512 (KBD alpha=5) catches real ffmpeg AC-3 and
        E-AC-3 transcodes packaged as 16-bit FLAC at 44.1/48 kHz without
        relying on bit-depth penalties or brickwall cut-offs."""
        import shutil
        import subprocess

        ffmpeg_bin = shutil.which("ffmpeg")
        if not ffmpeg_bin:
            self.skipTest("ffmpeg is not available on PATH")

        # Verify pure-NumPy KBD-512 window symmetry & energy normalization
        w512 = motor_flac._get_kbd_512_ac3()
        self.assertEqual(w512.shape, (512,))
        self.assertEqual(w512.dtype, np.float32)
        np.testing.assert_allclose(w512[:256], w512[256:][::-1], atol=1e-6)
        np.testing.assert_allclose(w512[:256] ** 2 + w512[256:] ** 2, 1.0, atol=1e-5)

        def _make_rich_stereo(sr, seed):
            base = _create_track_reference(sr, duration=6.0, seed=seed)
            n = len(base)
            t = np.arange(n, dtype=np.float64) / float(sr)
            rng = np.random.default_rng(seed + 100)
            mix = base.copy()
            for f0 in (220.0, 329.63, 440.0, 587.33):
                for k in range(1, 24):
                    fk = f0 * k
                    if fk >= min(21500.0, sr * 0.46):
                        break
                    amp = 0.08 / (k ** 0.8)
                    mix[:, 0] += amp * np.sin(2.0 * np.pi * fk * t + rng.uniform(0, 6.28))
                    mix[:, 1] += amp * np.sin(2.0 * np.pi * fk * t + rng.uniform(0, 6.28))
            return mix / (np.max(np.abs(mix)) * 1.15)

        wav44 = self.tmp_path / "ac3_ref44.wav"
        wav48 = self.tmp_path / "ac3_ref48.wav"
        sig44 = _make_rich_stereo(44100, 1501)
        sig48 = _make_rich_stereo(48000, 1502)
        sf.write(wav44, sig44, 44100, subtype="PCM_16")
        sf.write(wav48, sig48, 48000, subtype="PCM_16")

        # Clean controls stay well below the MDCT-512 detection threshold.
        for lbl, sig_c, sr_c in (("ctrl_44k", sig44, 44100), ("ctrl_48k", sig48, 48000)):
            p_ctrl = self.tmp_path / f"{lbl}.flac"
            sf.write(p_ctrl, sig_c, sr_c, subtype="PCM_16")
            r_ctrl = motor_flac.analyze_file_data(str(p_ctrl), use_cache=False)
            self.assertIn(r_ctrl["veredicto"], GOOD_VERDICTS, lbl)
            self.assertIs(r_ctrl["esp"]["mdct_lattice_detected"], False, lbl)
            self.assertLess(r_ctrl["esp"]["mdct_lattice_snr"], 6.0, lbl)

        # Real ffmpeg AC-3 and E-AC-3 packaged as 16-bit FLAC (no 24-bit penalty).
        cases = [
            ("ac3_44_192", wav44, ".ac3", ["-c:a", "ac3", "-b:a", "192k"], True),
            ("ac3_44_384", wav44, ".ac3", ["-c:a", "ac3", "-b:a", "384k"], True),
            ("ac3_44_640", wav44, ".ac3", ["-c:a", "ac3", "-b:a", "640k"], True),
            ("eac3_44_192", wav44, ".eac3", ["-c:a", "eac3", "-b:a", "192k"], True),
            ("eac3_44_384", wav44, ".eac3", ["-c:a", "eac3", "-b:a", "384k"], True),
            ("eac3_44_640", wav44, ".eac3", ["-c:a", "eac3", "-b:a", "640k"], True),
            ("ac3_48_192", wav48, ".ac3", ["-c:a", "ac3", "-b:a", "192k"], True),
            ("ac3_48_384", wav48, ".ac3", ["-c:a", "ac3", "-b:a", "384k"], True),
            ("ac3_48_640_fb", wav48, ".ac3", ["-c:a", "ac3", "-b:a", "640k", "-cutoff", "23500"], True),
            ("eac3_48_192", wav48, ".eac3", ["-c:a", "eac3", "-b:a", "192k"], True),
            ("eac3_48_384", wav48, ".eac3", ["-c:a", "eac3", "-b:a", "384k"], True),
            ("eac3_48_640_fb", wav48, ".eac3", ["-c:a", "eac3", "-b:a", "640k", "-cutoff", "23500"], True),
        ]
        for name, src_wav, ext, codec_args, require_full_lattice in cases:
            with self.subTest(case=name):
                tmp_lossy = self.tmp_path / f"{name}{ext}"
                out_flac = self.tmp_path / f"{name}.flac"
                subprocess.run(
                    [ffmpeg_bin, "-y", "-v", "error", "-i", str(src_wav)]
                    + codec_args
                    + [str(tmp_lossy)],
                    check=True,
                )
                subprocess.run(
                    [ffmpeg_bin, "-y", "-v", "error", "-i", str(tmp_lossy),
                     "-sample_fmt", "s16", str(out_flac)],
                    check=True,
                )
                data_dec, sr_dec = sf.read(out_flac, dtype="float32", always_2d=True)
                direct = motor_flac._detect_lapped_codec_grid(data_dec[:, 0], sr_dec)
                self.assertIs(direct["mdct_lattice_detected"], True, direct)
                self.assertEqual(
                    direct["mdct_lattice_family"],
                    motor_flac.CodecLatticeFamily.MDCT_512_AC3.value,
                    direct,
                )
                self.assertGreaterEqual(direct["mdct_lattice_snr"], 10.0, direct)

                res = motor_flac.analyze_file_data(str(out_flac), use_cache=False)
                self.assertEqual(res["meta"]["bits_per_sample"], 16)
                self.assertEqual(res["veredicto"], "PROBABLE UPSCALE", res["problemas"])
                if require_full_lattice:
                    self.assertIs(res["esp"]["mdct_lattice_detected"], True, res["esp"])
                    self.assertEqual(
                        res["esp"]["mdct_lattice_family"],
                        motor_flac.CodecLatticeFamily.MDCT_512_AC3.value,
                    )

    def test_51_hires_decimation_bridge_and_far_cutoff_attribution(self):
        """Engine 15.0: pure-NumPy Hi-Res decimation bridge catches ultrasonic-masked
        96 kHz transcodes (MP3, AAC, Vorbis, Opus) with zero FP on clean 24/96,
        24/192, and DSD64/128 controls, and attributes codec family under far_cutoff."""
        import shutil
        import subprocess

        ffmpeg_bin = shutil.which("ffmpeg")
        if not ffmpeg_bin:
            self.skipTest("ffmpeg is not available on PATH")

        rng = np.random.default_rng(1510)
        sig44 = _create_track_reference(44100, duration=6.0, seed=1511)
        sig48 = _create_track_reference(48000, duration=6.0, seed=1512)
        wav44 = self.tmp_path / "hr_ref44.wav"
        wav48 = self.tmp_path / "hr_ref48.wav"
        sf.write(wav44, sig44, 44100, subtype="PCM_24")
        sf.write(wav48, sig48, 48000, subtype="PCM_24")

        # 1. Ultrasonic-masked 96 kHz transcodes: muro_brickwall_nyquist is
        # defeated by gentle ultrasonic hiss, yet the decimation bridge catches
        # the underlying lapped codec lattice.
        hires_fakes = [
            ("mp3_44_96", wav44, ".mp3", ["-c:a", "libmp3lame", "-b:a", "320k", "-cutoff", "22050"],
             320, 147, 22200.0, motor_flac.CodecLatticeFamily.PQMF_576.value, True),
            ("aac_44_96", wav44, ".m4a", ["-c:a", "aac", "-b:a", "320k", "-cutoff", "22000"],
             320, 147, 22200.0, motor_flac.CodecLatticeFamily.MDCT_1024.value, True),
            ("vorbis_44_96", wav44, ".ogg", ["-c:a", "libvorbis", "-q:a", "9"],
             320, 147, 22200.0, motor_flac.CodecLatticeFamily.MDCT_1024.value, True),
            ("opus_48_96", wav48, ".opus", ["-c:a", "libopus", "-b:a", "256k"],
             2, 1, 23500.0, motor_flac.CodecLatticeFamily.CELT_960.value, False),
        ]
        for name, src_wav, ext, codec_args, up, dn, u_lo, exp_fam, expect_no_brick in hires_fakes:
            with self.subTest(case=name):
                tmp_l = self.tmp_path / f"{name}{ext}"
                tmp_w = self.tmp_path / f"{name}.wav"
                subprocess.run(
                    [ffmpeg_bin, "-y", "-v", "error", "-i", str(src_wav)]
                    + codec_args + [str(tmp_l)],
                    check=True,
                )
                subprocess.run(
                    [ffmpeg_bin, "-y", "-v", "error", "-i", str(tmp_l),
                     "-c:a", "pcm_s24le", str(tmp_w)],
                    check=True,
                )
                x_dec, _ = sf.read(tmp_w, dtype="float64", always_2d=True)
                x96 = signal.resample_poly(x_dec, up, dn, axis=0)
                u_order = 8 if u_lo >= 23000.0 else 6
                u_hiss = signal.sosfilt(
                    signal.butter(u_order, [u_lo, 40000.0], btype="band", fs=96000, output="sos"),
                    rng.standard_normal(x96.shape),
                    axis=0,
                )
                u_hiss *= (10.0 ** (-56.0 / 20.0)) / (np.std(u_hiss) + 1e-12)
                p96 = self.tmp_path / f"{name}.flac"
                sf.write(p96, np.clip(x96 + u_hiss, -0.99, 0.99), 96000, subtype="PCM_24")

                res = motor_flac.analyze_file_data(str(p96), use_cache=False)
                spec = res["esp"]
                self.assertEqual(res["veredicto"], "PROBABLE UPSCALE", spec)
                if expect_no_brick:
                    self.assertIs(spec["muro_brickwall_nyquist"], False, spec)
                self.assertIs(spec["mdct_lattice_detected"], True, spec)
                self.assertEqual(spec["mdct_lattice_family"], exp_fam, spec)

        # 2. far_cutoff attribution unlocked: 128 kbps MP3 gets PQMF-576 family.
        mp3_128 = self.tmp_path / "mp3_128_far.mp3"
        mp3_128_flac = self.tmp_path / "mp3_128_far.flac"
        subprocess.run(
            [ffmpeg_bin, "-y", "-v", "error", "-i", str(wav44),
             "-c:a", "libmp3lame", "-b:a", "128k", str(mp3_128)],
            check=True,
        )
        subprocess.run(
            [ffmpeg_bin, "-y", "-v", "error", "-i", str(mp3_128),
             "-sample_fmt", "s16", str(mp3_128_flac)],
            check=True,
        )
        r_far = motor_flac.analyze_file_data(str(mp3_128_flac), use_cache=False)
        self.assertEqual(r_far["esp"]["clase_corte"], "far_cutoff")
        self.assertIs(r_far["esp"]["mdct_lattice_detected"], True, r_far["esp"])
        self.assertEqual(
            r_far["esp"]["mdct_lattice_family"],
            motor_flac.CodecLatticeFamily.PQMF_576.value,
        )

        # 3. Genuine 24/96, 24/192, and DSD64 controls must yield 0 false positives.
        g96 = _create_track_reference(96000, duration=5.0, seed=1515)
        g96 += rng.normal(0.0, 10.0 ** (-108.0 / 20.0), g96.shape)
        p_g96 = self.tmp_path / "gen_hires_96.flac"
        sf.write(p_g96, np.clip(g96, -0.99, 0.99), 96000, subtype="PCM_24")

        g192 = _create_track_reference(192000, duration=3.5, seed=1516)
        g192 += rng.normal(0.0, 10.0 ** (-110.0 / 20.0), g192.shape)
        p_g192 = self.tmp_path / "gen_hires_192.flac"
        sf.write(p_g192, np.clip(g192, -0.99, 0.99), 192000, subtype="PCM_24")

        g_dsd = _dsd_like(88200, duration=4.5, seed=1517)
        p_dsd = self.tmp_path / "gen_dsd64.flac"
        sf.write(p_dsd, np.clip(g_dsd, -0.99, 0.99), 88200, subtype="PCM_24")

        for lbl, p_ctrl in (("gen_96", p_g96), ("gen_192", p_g192), ("gen_dsd64", p_dsd)):
            r_c = motor_flac.analyze_file_data(str(p_ctrl), use_cache=False)
            self.assertIn(r_c["veredicto"], GOOD_VERDICTS, lbl)
            self.assertIs(r_c["esp"]["mdct_lattice_detected"], False, f"{lbl}: {r_c['esp']}")

    def test_52_native_24_44_tail_and_noise_shaped_16_in_24_guard(self):
        """Engine 15.0: genuine 24/44.1 high-DR warm masters with <= -105 dBFS
        ADC stopband pass standalone while 16-bit TPDF repackages stay caught,
        and rising noise-shaped 16-in-24 dither triggers SOFT_BLOCKING without
        false positives on analogue tape (-70/-82/-92 dBFS)."""
        sr = 44100
        rng = np.random.default_rng(1520)

        # 1. Genuine 24/44.1 warm/acoustic master with ADC stopband <= -115 dBFS.
        n = sr * 7
        t = np.arange(n, dtype=np.float64) / float(sr)
        body = signal.sosfilt(
            signal.butter(2, 2400.0, btype="low", fs=sr, output="sos"),
            rng.standard_normal((n, 2)) * 0.16,
            axis=0,
        )
        strum = (0.30 + 0.70 * (np.sin(2.0 * np.pi * 1.4 * t) ** 2))[:, None]
        warm_air = signal.sosfilt(
            signal.butter(3, 13500.0, btype="low", fs=sr, output="sos"),
            rng.standard_normal((n, 2)) * 0.005,
            axis=0,
        ) * strum
        adc_tail = signal.sosfilt(
            signal.butter(6, 20900.0, btype="low", fs=sr, output="sos"),
            rng.standard_normal((n, 2)) * 0.0006,
            axis=0,
        )
        chords = sum(
            0.055 * np.sin(2.0 * np.pi * f0 * t)[:, None]
            for f0 in (164.81, 246.94, 329.63, 493.88)
        )
        warm_24_44 = np.clip(
            body + chords + warm_air + adc_tail
            + rng.normal(0.0, 10.0 ** (-118.0 / 20.0), (n, 2)),
            -0.88,
            0.88,
        )
        p_gen = self.tmp_path / "gen_warm_24_44.flac"
        sf.write(p_gen, warm_24_44, sr, subtype="PCM_24")
        r_gen = motor_flac.analyze_file_data(str(p_gen), use_cache=False)
        self.assertIs(r_gen["esp"]["native_24bit_hf_tail"], True, r_gen["esp"])
        self.assertIs(r_gen["esp"]["noise_shaped_16_in_24"], False)
        self.assertIn(r_gen["veredicto"], GOOD_VERDICTS, r_gen["problemas"])

        # Counterexample: same warm signal quantized to 16-bit + full 16-bit TPDF
        # cannot exploit native_24bit_hf_tail because stopband sits near -96 dBFS.
        lsb16 = 1.0 / 32768.0
        warm_16_tpdf = (
            np.round(warm_24_44 * 32768.0) / 32768.0
            + (rng.random(warm_24_44.shape) - rng.random(warm_24_44.shape)) * lsb16
        )
        p_fake_tpdf = self.tmp_path / "fake_warm_cd_16tpdf_24_44.flac"
        sf.write(p_fake_tpdf, np.clip(warm_16_tpdf, -0.99, 0.99), sr, subtype="PCM_24")
        r_fake_tpdf = motor_flac.analyze_file_data(str(p_fake_tpdf), use_cache=False)
        self.assertIs(r_fake_tpdf["esp"]["native_24bit_hf_tail"], False)
        self.assertNotIn(r_fake_tpdf["veredicto"], GOOD_VERDICTS)

        # 2. Noise-shaped 16-in-24 dither on a track with a quiet intro block
        # (RMS in [-92, -40] dBFS) triggers SOFT_BLOCKING (SUSPICIOUS alone).
        n_q = sr * 6
        t_q = np.linspace(0.0, 6.0, n_q, endpoint=False)
        fade = np.where(t_q < 3.5, 1e-5, ((t_q - 3.5) / 2.5) ** 2 * 0.015)
        quiet_part = rng.standard_normal((n_q, 2)) * fade[:, None]
        loud_part = _create_track_reference(sr, duration=6.0, seed=1521)
        full_sig = np.vstack([quiet_part, loud_part])

        d_ns = (rng.random(full_sig.shape) - rng.random(full_sig.shape)) * lsb16
        ns_out = np.empty_like(full_sig)
        for ch in range(2):
            e1 = 0.0
            e2 = 0.0
            src_c = full_sig[:, ch]
            d_c = d_ns[:, ch]
            dst_c = ns_out[:, ch]
            for idx_i in range(len(src_c)):
                val = src_c[idx_i] + 1.5 * e1 - 0.6 * e2
                q_val = round((val + d_c[idx_i]) * 32768.0) / 32768.0
                q_val = max(-0.99, min(0.99, q_val))
                e2 = e1
                e1 = val - q_val
                dst_c[idx_i] = q_val
        ns_16in24 = ns_out + (rng.random(full_sig.shape) - rng.random(full_sig.shape)) * lsb16
        p_ns = self.tmp_path / "fake_ns_16in24.flac"
        sf.write(p_ns, np.clip(ns_16in24, -0.99, 0.99), sr, subtype="PCM_24")
        r_ns = motor_flac.analyze_file_data(str(p_ns), use_cache=False)
        self.assertIs(r_ns["esp"]["noise_shaped_16_in_24"], True, r_ns["esp"])
        self.assertGreaterEqual(r_ns["esp"]["quiet_hf_slope_db"], 3.5)
        self.assertGreaterEqual(r_ns["esp"]["quiet_hf_flatness"], 0.90)
        self.assertGreaterEqual(r_ns["esp"]["quiet_hf_floor_dbfs"], -96.0)
        self.assertEqual(r_ns["veredicto"], "SUSPICIOUS")
        self.assertIn(
            motor_flac.FindingCode.NOISE_SHAPED_16_IN_24.value,
            r_ns["esp"]["finding_codes"],
        )

        # 3. Analogue tape (-70 / -82 / -92 dBFS) and flat thermal floors must
        # never trigger noise_shaped_16_in_24.
        for floor_db in (-70.0, -82.0, -92.0):
            with self.subTest(tape_floor_db=floor_db):
                hiss = signal.sosfilt(
                    signal.butter(1, 17500.0, btype="low", fs=sr, output="sos"),
                    rng.standard_normal(full_sig.shape),
                    axis=0,
                )
                hiss *= (10.0 ** (floor_db / 20.0)) / (np.std(hiss) + 1e-12)
                p_tape = self.tmp_path / f"gen_tape_{int(abs(floor_db))}.flac"
                sf.write(p_tape, np.clip(full_sig + hiss, -0.99, 0.99), sr, subtype="PCM_24")
                r_tape = motor_flac.analyze_file_data(str(p_tape), use_cache=False)
                self.assertIs(r_tape["esp"]["noise_shaped_16_in_24"], False, r_tape["esp"])
                self.assertIn(r_tape["veredicto"], GOOD_VERDICTS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
