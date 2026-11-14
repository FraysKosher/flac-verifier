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

import motor_flac as engine

KEYS_SPECTRAL = ("techo_hz", "techo_rel_nyquist", "clase_corte", "corte_detectado",
                      "frecuencia_corte", "distancia_nyquist", "corte_artificial",
                      "ratio", "ratio_aplica", "var_alta_db", "separacion_stereo",
                      "nyquist")


class test_streaming_equal_array(unittest.TestCase):
    """Oracle: the same data through both paths -> the same results."""

    @classmethod
    def setUpClass(cls):
        cls.fx = fx.Fixtures()

    @classmethod
    def tearDownClass(cls):
        cls.fx.close()

    def _compare(self, name, mode="center"):
        path = self.fx[name]
        res = engine.analyze_file_data(path, mode, None, False)
        self.assertNotIn("error", res, res.get("error"))

        data, sr = engine.read_audio(path)
        meta = engine.verify_metadata(path)

        self.assertEqual(res["clip"], engine.detect_clipping(data), "clipping")
        self.assertEqual(res["dr"], engine.calculate_dr(data, sr), "dynamic range")
        self.assertEqual(res["bdi"], engine.analyze_bit_depth(data, meta), "low bits")
        self.assertEqual(res["md5"], engine.verify_md5(data, meta), "MD5")

        spectrum = engine.analyze_spectrum(data, sr, mode, None, False, None)
        self.assertIsNone(spectrum.get("error"), spectrum.get("error"))
        for key in KEYS_SPECTRAL:
            with self.subTest(key=key):
                self.assertEqual(res["esp"][key], spectrum[key])

    def test_stereo_16_bits(self):
        self._compare("ok16")

    def test_stereo_24_bits(self):
        self._compare("ok24")

    def test_mono_no_reports_separation_stereo(self):
        """In mono there is no L-R separation: None in both paths."""
        self._compare("ok16_mono")
        res = engine.analyze_file_data(self.fx["ok16_mono"], "center", None, False)
        self.assertIsNone(res["esp"]["separacion_stereo"])

    def test_mode_full(self):
        self._compare("ok16", mode="full")


class test_reader_by_blocks(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.fx = fx.Fixtures()

    @classmethod
    def tearDownClass(cls):
        cls.fx.close()

    def test_no_reads_more_block_rebuilds_file(self):
        path = self.fx["ok24"]
        blocks = list(engine._read_blocks(path))
        self.assertGreater(len(blocks), 0)
        for block in blocks:
            self.assertLessEqual(block.shape[0], engine.FRAMES_BY_BLOCK)
        data, _ = engine.read_audio(path)
        self.assertTrue(np.array_equal(np.concatenate(blocks), data))

    def test_md5_incremental_matches_with_only_array(self):
        """The block hash must give the same result as hashing the whole array."""
        path = self.fx["ok24"]
        data, _ = engine.read_audio(path)
        meta = engine.verify_metadata(path)
        streaming = engine._analyze_streaming(path, meta, "full", None, False)
        self.assertEqual(streaming["md5"], engine.verify_md5(data, meta))
        self.assertEqual(streaming["md5"]["estado"], "match")
        self.assertEqual(streaming["md5"]["calculado"],
                         engine._md5_pcm(data, meta["bits_per_sample"]))


class TestVectorization(unittest.TestCase):
    """The vectorisation must count exactly the same as the naive loop."""

    @staticmethod
    def _naive_clipping(data):
        """Original implementation, sample by sample (reference)."""
        amp = np.max(np.abs(data), axis=1) if data.ndim > 1 else np.abs(data)
        en_clip = (amp >= 0.9999).astype(np.int8)
        runs = 0
        count = 0
        for v in en_clip:
            if v:
                count += 1
            else:
                if count >= 3:
                    runs += 1
                count = 0
        if count >= 3:
            runs += 1
        return {"runs_clip": runs, "hay_clipping": runs > 0}

    def test_streaks_edges_length_2_3(self):
        x = np.zeros((50, 2))
        x[0:3, 0]   = 1.0        # run of 3 at the start -> counts
        x[10:12, 1] = -1.0       # run of 2 -> does not count
        x[20:24, 0] = 1.0        # run of 4 -> counts
        x[47:50, 1] = 1.0        # run of 3 right at the end -> counts
        # the whole file: both agree and the result is the expected one
        self.assertEqual(engine.detect_clipping(x), self._naive_clipping(x))
        self.assertEqual(engine.detect_clipping(x)["runs_clip"], 3)

    def test_equivalence_with_data_random_clipping_real(self):
        rng = np.random.default_rng(5)
        cases = [rng.random((100000, 2)) * 2 - 1]
        with_clip = rng.random((50000, 2)) * 0.5
        with_clip[100:105] = 1.0                  # run of 5
        with_clip[49995:]  = -1.0                 # run of 5 at the end
        with_clip[20000:20002] = 1.0              # run of 2
        cases.append(with_clip)
        for data in cases:
            with self.subTest(shape=data.shape):
                self.assertEqual(engine.detect_clipping(data), self._naive_clipping(data))

    def test_streak_split_between_blocks_counts_alone_time(self):
        data = np.zeros((10, 1))
        data[3:9] = 1.0                          # a single run of 6
        for cutoff in range(1, 10):
            with self.subTest(cutoff=cutoff):
                count, runs = 0, 0
                for chunk in (data[:cutoff], data[cutoff:]):
                    if not len(chunk):
                        continue
                    streaks, count = engine._streaks(chunk[:, 0] >= 0.9999, count)
                    runs += sum(1 for long in streaks if long >= 3)
                if count >= 3:
                    runs += 1
                self.assertEqual(runs, 1)

    def test_streak_split_three_blocks(self):
        data = np.ones((9, 1))                   # run of 9
        count, runs = 0, 0
        for chunk in (data[:3], data[3:6], data[6:]):
            streaks, count = engine._streaks(chunk[:, 0] >= 0.9999, count)
            runs += sum(1 for long in streaks if long >= 3)
        if count >= 3:
            runs += 1
        self.assertEqual(runs, 1)

    def test_accumulator_counts_equal_via_array(self):
        """It includes the run that ends exactly with the file (it went uncounted before)."""
        rng = np.random.default_rng(11)
        cases = []
        x = rng.random((50000, 2)) * 0.5
        x[100:105] = 1.0                        # run in the middle
        x[-3:] = -1.0                           # run that ends with the file
        cases.append(x)
        y = rng.random((30000, 2)) * 2 - 1
        y[:4] = 1.0                             # run at the start
        cases.append(y)
        z = np.zeros((1000, 1)); z[:3] = 1.0    # run of 3 at the start, in mono
        cases.append(z)
        for i, data in enumerate(cases):
            with self.subTest(caso=i, shape=data.shape):
                sr = 8000
                meta = {"sample_rate": sr, "bits_per_sample": 16,
                        "canales": data.shape[1], "duracion": 1.0, "md5": None}
                accumulator = engine._BlockAnalysis(meta, sr, len(data), "full", None)
                for start in range(0, len(data), 7777):     # irregular blocks
                    accumulator.add(data[start : start + 7777], start)
                self.assertEqual(accumulator.result()["clip"],
                                 engine.detect_clipping(data))

    def test_dr_by_blocks_equal_via_array(self):
        """The DR accumulated over irregular blocks must be identical."""
        sr = 8000
        rng = np.random.default_rng(9)
        x = rng.standard_normal((sr * 20, 2)) * 0.2
        x[sr * 6 : sr * 9] *= 5.0                # one clearly louder block
        expected = engine.calculate_dr(x, sr)
        self.assertIsNotNone(expected)

        meta = {"sample_rate": sr, "bits_per_sample": 16, "canales": 2,
                "duracion": 20.0, "md5": 1}
        accumulator = engine._BlockAnalysis(meta, sr, len(x), "full", None)
        offset = 0
        for cutoff in (37, 1, 100003, 4096, 99991, 10 ** 9):
            chunk = x[offset : offset + cutoff]
            if not len(chunk):
                continue
            accumulator.add(chunk, offset)
            offset += len(chunk)
        self.assertEqual(offset, len(x))
        self.assertEqual(accumulator.result()["dr"], expected)

    def test_dr_file_short_still_being_none(self):
        """Less than 6 seconds = less than two 3 s blocks."""
        self.assertIsNone(engine.calculate_dr(np.zeros((8000 * 5, 2)), 8000))

    def test_result_idempotent_on_eof_clipping(self):
        """Calling acc.result() repeatedly when EOF ends in an open clipping streak is idempotent."""
        sr = 8000
        rng = np.random.default_rng(77)
        x = np.clip(rng.standard_normal((sr * 2, 2)) * 0.15, -0.5, 0.5)
        x[100:104, 0] = 1.0
        x[-5:, 1] = -1.0
        meta = {"sample_rate": sr, "bits_per_sample": 16, "canales": 2,
                "duracion": 2.0, "md5": None}
        acc = engine._BlockAnalysis(meta, sr, len(x), "full", None)
        acc.add(x, 0)
        first = acc.result()
        second = acc.result()
        self.assertEqual(first, second)
        self.assertEqual(first["clip"]["runs_clip"], 2)
        self.assertEqual(first["esp"]["clipping_eventos"], 2)


class TestNativeFastPathEquivalence(unittest.TestCase):
    """Bit-exact and numerical equivalence of native integer & pure-NumPy paths."""

    def test_native_int16_and_int24_streaming_bit_exact_oracle(self):
        import tempfile
        import soundfile as sf

        sr = 44100
        dur = 2.0
        audio = fx.full_band_noise(sr=sr, dur=dur, seed=11)
        # Inject an explicit clipping streak in the middle to verify integer gates.
        audio[12000:12006, 0] = 1.0
        audio[24000:24005, 1] = -1.0

        with tempfile.TemporaryDirectory() as tmpdir:
            p16 = os.path.join(tmpdir, "clip_16.flac")
            p24 = os.path.join(tmpdir, "clip_24.flac")
            sf.write(p16, audio, sr, subtype="PCM_16")
            sf.write(p24, audio, sr, subtype="PCM_24")
            for path in (p16, p24):
                with self.subTest(path=os.path.basename(path)):
                    meta = engine.verify_metadata(path)
                    fast_out = engine._analyze_streaming(
                        path, meta, "full", None, False
                    )
                    ref_acc = engine._BlockAnalysis(
                        meta, sr, sf.info(path).frames, "full", None, False, path
                    )
                    offset = 0
                    for blk in engine._read_blocks(
                        path, frames_by_block=engine.FRAMES_BY_BLOCK
                    ):
                        if blk.shape[0] == 0:
                            continue
                        ref_acc.add(blk, offset)
                        offset += blk.shape[0]
                    ref_out = ref_acc.result()

                    self.assertEqual(fast_out["md5"], ref_out["md5"])
                    self.assertAlmostEqual(fast_out["pico"], ref_out["pico"], places=12)
                    self.assertEqual(fast_out["clip"], ref_out["clip"])
                    self.assertEqual(fast_out["dr"], ref_out["dr"])
                    self.assertEqual(fast_out["bdi"], ref_out["bdi"])
                    self.assertEqual(fast_out["esp"], ref_out["esp"])

    def test_polyphase_resampler_and_mdct36_basis_equivalence(self):
        import subprocess
        from scipy import signal as sp_signal
        import _motor_espectral as spectral

        # 1) Clean subprocess check: _detect_lapped_codec_grid never pulls scipy.signal.
        probe_code = (
            "import sys, numpy as np, _motor_espectral as spectral; "
            "x = np.random.default_rng(1).standard_normal(44100 * 3).astype(np.float32); "
            "spectral._detect_lapped_codec_grid(x, 44100, ceiling_hz=20000); "
            "bad = [m for m in ('scipy.signal', 'scipy.stats', 'scipy.optimize') if m in sys.modules]; "
            "assert not bad, f'unexpected imports: {bad}'"
        )
        subprocess.run([sys.executable, "-c", probe_code], check=True)

        # 2) Polyphase 160/147 slice resampler vs scipy.signal.resample_poly.
        rng = np.random.default_rng(42)
        seg = rng.standard_normal(44100 * 3).astype(np.float32)
        ref = sp_signal.resample_poly(
            seg, 160, 147, window=("kaiser", 8.6)
        ).astype(np.float32)[320 : 320 + 15360]
        got = spectral._resample_celt_44k_to_48k_slice(seg, 320, 15360)
        self.assertEqual(got.shape, ref.shape)
        self.assertLess(float(np.max(np.abs(got - ref))), 5e-6)

        # 3) PQMF-576 36x18 basis matmul vs full MDCT-36 + L2-tiled MDCT-2048.
        grouped = rng.standard_normal((16, 8, 4, 18, 36)).astype(np.float32)
        _, win36 = spectral._get_pqmf_fast(4096)
        ref_36 = np.abs(spectral._fast_mdct_f32(grouped, win36))
        got_36 = np.abs(grouped @ spectral._get_mdct36_basis())
        self.assertLess(float(np.max(np.abs(got_36 - ref_36))), 5e-6)

        frames_big = rng.standard_normal((100, 2048)).astype(np.float32)
        win2048 = spectral._get_vorbis_2048()
        got_big = spectral._fast_mdct_f32(frames_big, win2048)
        ref_big = np.vstack([
            spectral._fast_mdct_f32(frames_big[i : i + 32], win2048)
            for i in range(0, frames_big.shape[0], 32)
        ])
        self.assertLess(float(np.max(np.abs(got_big - ref_big))), 5e-6)

    def test_int24_packer_and_pocketfft_direct_isolation(self):
        import importlib.util
        import subprocess
        from scipy import fft as sp_fft
        import _motor_bits as bits
        import _motor_espectral as spectral

        # 1) Reusable _Int24Packer bit-exactness across boundary values and sizes.
        packer = bits._Int24Packer()
        rng = np.random.default_rng(139)
        edge = np.array(
            [-8388608, -8388607, -32768, -256, -1, 0, 1, 255, 32767, 8388606, 8388607],
            dtype=np.int32,
        )
        for chunk in (
            edge,
            rng.integers(-8388608, 8388608, size=(17, 2), dtype=np.int32),
            rng.integers(-8388608, 8388608, size=(8192, 2), dtype=np.int32),
        ):
            flat = chunk.reshape(-1)
            expected = b"".join(int(v).to_bytes(4, "little", signed=True)[:3] for v in flat)
            got = bytes(bits._pack_int24_le(chunk, packer=packer))
            self.assertEqual(got, expected)

        # 2) Direct pypocketfft wrappers vs scipy.fft reference.
        sig = rng.standard_normal((12, 4096)).astype(np.float32)
        ref_rfft = sp_fft.rfft(sig, axis=-1, workers=1)
        got_rfft = spectral._rfft_f32(sig, axis=-1)
        self.assertLess(float(np.max(np.abs(got_rfft - ref_rfft))), 1e-4)

        ref_irfft = sp_fft.irfft(ref_rfft, n=4096, axis=-1, workers=1)
        got_irfft = spectral._irfft_f32(got_rfft, 4096, axis=-1)
        self.assertLess(float(np.max(np.abs(got_irfft - ref_irfft))), 1e-5)

        dct_in = rng.standard_normal((8, 1024)).astype(np.float32)
        ref_dct = sp_fft.dct(dct_in, type=4, norm="ortho", axis=-1, workers=1)
        got_dct = spectral._dct4_ortho_f32(dct_in, axis=-1)
        self.assertLess(float(np.max(np.abs(got_dct - ref_dct))), 1e-4)

        # 3) Frozen-mode isolation: _MEIPASS direct pypocketfft load pulls zero scipy subpackages.
        scipy_root = os.path.dirname(
            os.path.dirname(importlib.util.find_spec("scipy").origin)
        )
        probe = (
            "import sys, numpy as np; "
            f"sys._MEIPASS = {scipy_root!r}; "
            "import _motor_espectral as spectral; "
            "acc = spectral._SpectralAccumulator(96000, 'full', None); "
            "x = np.random.default_rng(7).standard_normal((96000, 2)).astype(np.float32) * 0.1; "
            "acc.add(x, 0); res = acc.result(); "
            "assert res['error'] is None; "
            "leaked = [m for m in sys.modules if m == 'scipy' or m.startswith('scipy.')]; "
            "assert not leaked, f'scipy leaked in frozen mode: {leaked}'"
        )
        subprocess.run([sys.executable, "-c", probe], check=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
