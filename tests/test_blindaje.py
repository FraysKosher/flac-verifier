"""Block 1 (hardening): no file may abort the program.

Covers the failures reproduced in the audit:
  - mutagen raised on garbage after the 'fLaC' header and killed the process.
  - verificar_metadatos raised MutagenError on a path that does not exist.
  - an unexpected exception escaped from analizar_archivo_datos.
"""
import builtins
import os
import sys
import unittest
from unittest import mock

import soundfile as sf
from mutagen import MutagenError

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fixtures as fx

import motor_flac as engine


class test_files_broken(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.fx = fx.Fixtures()

    @classmethod
    def tearDownClass(cls):
        cls.fx.close()

    def test_verify_metadata_no_raises(self):
        for name in self.fx.NAMES_BROKEN:      # includes 'truncado'
            with self.subTest(file=name):
                meta = engine.verify_metadata(self.fx[name])   # must not raise
                self.assertIsInstance(meta, dict)
                self.assertIn("valido", meta)

    def test_metadata_unreadable_mark_as_invalid(self):
        for name in ("basura_magic", "texto", "cero_bytes"):
            with self.subTest(file=name):
                self.assertFalse(engine.verify_metadata(self.fx[name])["valido"])

    def test_error_unexpected_brings_diagnostic(self):
        """With garbage after the 'fLaC' header, mutagen raised and killed the process."""
        meta = engine.verify_metadata(self.fx["basura_magic"])
        self.assertFalse(meta["valido"])
        self.assertIn("error", meta)

    def test_metadata_readable_no_mean_file_intact(self):
        """mutagen reads STREAMINFO from the header even when half the file is
        missing: 'valid metadata' is NOT an integrity check. The corruption is
        detected later, when decoding or verifying the MD5."""
        meta = engine.verify_metadata(self.fx["truncado"])
        self.assertTrue(meta["valido"])
        res = engine.analyze_file_data(self.fx["truncado"], "center", None, False)
        self.assertIn("error", res)
        self.assertNotIn("veredicto", res)

    def test_verify_metadata_with_path_nonexistent(self):
        meta = engine.verify_metadata(os.path.join(self.fx.dir, "no_existe.flac"))
        self.assertFalse(meta["valido"])
        self.assertIn("error", meta)

    def test_verify_signature_no_raises(self):
        self.assertTrue(engine.verify_signature(self.fx["ok16"]))
        self.assertFalse(engine.verify_signature(os.path.join(self.fx.dir, "no_existe.flac")))
        for name in self.fx.NAMES_BROKEN:
            with self.subTest(file=name):
                engine.verify_signature(self.fx[name])          # must not raise

    def test_analyze_file_broken_returns_error_without_verdict(self):
        for name in self.fx.NAMES_BROKEN:
            with self.subTest(file=name):
                res = engine.analyze_file_data(self.fx[name], "center", None, False)
                self.assertIn("error", res)
                self.assertNotIn("veredicto", res)
                self.assertNotIn("score", res)

    def test_read_audio_no_raises(self):
        for name in self.fx.NAMES_BROKEN:
            with self.subTest(file=name):
                data, sr = engine.read_audio(self.fx[name])
                self.assertIsNone(data)

    def test_files_valid_follow_being_analyzed(self):
        """The hardening must not break the happy path."""
        res = engine.analyze_file_data(self.fx["ok16"], "center", None, False)
        self.assertNotIn("error", res)
        self.assertIn("veredicto", res)
        self.assertIn("esp", res)
        self.assertIn("score", res)


class TestTransientIoRetry(unittest.TestCase):
    """Bounded retry on transient NAS/SMB spin-down OS errors."""

    @classmethod
    def setUpClass(cls):
        cls.fx = fx.Fixtures()

    @classmethod
    def tearDownClass(cls):
        cls.fx.close()

    def test_retry_io_recovers_on_second_attempt_and_stops_after_budget(self):
        waits = []
        calls = 0

        def _flaky_once():
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError("NAS spin-up")
            return "ready"

        self.assertEqual(engine.retry_io(_flaky_once, sleep_fn=waits.append), "ready")
        self.assertEqual(calls, 2)
        self.assertEqual(waits, [0.25])

        waits_exhausted = []
        fail_calls = 0

        def _always_fails():
            nonlocal fail_calls
            fail_calls += 1
            raise OSError("network down")

        with self.assertRaises(OSError):
            engine.retry_io(_always_fails, sleep_fn=waits_exhausted.append)
        self.assertEqual(fail_calls, 3)
        self.assertEqual(waits_exhausted, [0.25, 0.75])

    def test_retry_io_does_not_retry_logical_format_errors(self):
        for exc in (
            ValueError("bad value"),
            engine.FLACNoHeaderError("not flac"),
            sf.LibsndfileError(158, "unrecognised format"),
        ):
            with self.subTest(exc=type(exc).__name__):
                waits = []
                calls = 0

                def _bad_format(err=exc):
                    nonlocal calls
                    calls += 1
                    raise err

                with self.assertRaises(type(exc)):
                    engine.retry_io(_bad_format, sleep_fn=waits.append)
                self.assertEqual(calls, 1)
                self.assertEqual(waits, [])

        io_waits = []
        io_calls = 0

        def _system_io_err():
            nonlocal io_calls
            io_calls += 1
            raise sf.LibsndfileError(2, "system I/O")

        with self.assertRaises(sf.LibsndfileError):
            engine.retry_io(_system_io_err, sleep_fn=io_waits.append)
        self.assertEqual(io_calls, 3)
        self.assertEqual(io_waits, [0.25, 0.75])

        real_open = builtins.open
        open_calls = 0
        target = os.path.abspath(self.fx["texto"])

        def _counting_open(path, *args, **kwargs):
            nonlocal open_calls
            if isinstance(path, str) and os.path.abspath(path.lstrip("\\\\?\\")) == target:
                open_calls += 1
            return real_open(path, *args, **kwargs)

        with mock.patch("builtins.open", side_effect=_counting_open), \
             mock.patch("time.sleep") as mock_sleep:
            self.assertFalse(engine.verify_signature(self.fx["texto"]))
        self.assertEqual(open_calls, 1)
        mock_sleep.assert_not_called()

    def test_metadata_and_signature_recover_from_transient_os_error(self):
        ok_path = self.fx["ok16"]
        target = os.path.abspath(ok_path)
        real_open = builtins.open
        open_attempts = 0

        def _flaky_open(path, *args, **kwargs):
            nonlocal open_attempts
            if (isinstance(path, str)
                    and os.path.abspath(path.lstrip("\\\\?\\")) == target):
                open_attempts += 1
                if open_attempts == 1:
                    raise OSError(5, "Input/output error")
            return real_open(path, *args, **kwargs)

        with mock.patch("builtins.open", side_effect=_flaky_open), \
             mock.patch("time.sleep") as mock_sleep:
            self.assertTrue(engine.verify_signature(ok_path))
        self.assertEqual(open_attempts, 2)
        mock_sleep.assert_called_once_with(0.25)

        real_flac = engine.FLAC
        flac_attempts = 0

        def _flaky_flac(path):
            nonlocal flac_attempts
            flac_attempts += 1
            if flac_attempts == 1:
                err = MutagenError("read failed")
                err.__cause__ = OSError("SMB wake")
                raise err
            return real_flac(path)

        with mock.patch.object(engine, "FLAC", side_effect=_flaky_flac), \
             mock.patch("time.sleep") as mock_sleep_meta:
            meta = engine.verify_metadata(ok_path)
        self.assertTrue(meta["valido"])
        self.assertEqual(flac_attempts, 2)
        mock_sleep_meta.assert_called_once_with(0.25)

    def test_streaming_block_read_recovers_mid_file_with_bit_exact_md5(self):
        path = self.fx["ok24"]
        expected = engine.analyze_file_data(path, "center", None, False, use_cache=False)
        self.assertEqual(expected["md5"]["estado"], "match")

        real_soundfile = sf.SoundFile
        block_size = 16384

        for fail_seek in (False, True):
            with self.subTest(reopen=fail_seek):
                state = {"tripped": False, "seek_failed": False}

                class _FlakySoundFileProxy:
                    def __init__(self, inner_path):
                        self._sf = real_soundfile(inner_path)

                    def __getattr__(self, name):
                        return getattr(self._sf, name)

                    def seek(self, offset):
                        if fail_seek and state["tripped"] and not state["seek_failed"]:
                            state["seek_failed"] = True
                            raise OSError("stale file handle")
                        return self._sf.seek(offset)

                    def read(self, *args, **kwargs):
                        if not state["tripped"] and self._sf.tell() == block_size:
                            state["tripped"] = True
                            raise sf.LibsndfileError(2, "transient SMB read")
                        return self._sf.read(*args, **kwargs)

                    def close(self):
                        return self._sf.close()

                with mock.patch.object(engine, "FRAMES_BY_BLOCK", block_size), \
                     mock.patch.object(engine.sf, "SoundFile", side_effect=_FlakySoundFileProxy), \
                     mock.patch("time.sleep") as mock_sleep:
                    recovered = engine.analyze_file_data(
                        path, "center", None, False, use_cache=False
                    )
                self.assertEqual(recovered["md5"]["estado"], "match")
                self.assertEqual(
                    recovered["md5"]["calculado"], expected["md5"]["calculado"]
                )
                self.assertEqual(recovered["veredicto"], expected["veredicto"])
                self.assertTrue(state["tripped"])
                if fail_seek:
                    self.assertTrue(state["seek_failed"])
                mock_sleep.assert_called()

    def test_retry_does_not_seek_closed_handle_after_reopen_failure(self):
        """After seek() fails and closes the handle, a transient reopen failure
        must not call seek() on the closed handle on the next retry."""
        path = self.fx["ok24"]
        expected = engine.analyze_file_data(path, "center", None, False, use_cache=False)
        self.assertEqual(expected["md5"]["estado"], engine.Md5Status.MATCH.value)

        real_soundfile = sf.SoundFile
        block_size = 16384
        tracker = {
            "read_failed": False,
            "seek_failed": False,
            "reopen_failed": False,
            "seek_after_close": 0,
        }

        class _StrictHandleProxy:
            def __init__(self, inner_path):
                self._sf = real_soundfile(inner_path)
                self._closed = False

            def __getattr__(self, name):
                return getattr(self._sf, name)

            def seek(self, offset):
                if self._closed:
                    tracker["seek_after_close"] += 1
                    raise RuntimeError("seek called on closed handle")
                if tracker["read_failed"] and not tracker["seek_failed"]:
                    tracker["seek_failed"] = True
                    raise OSError("stale handle on seek")
                return self._sf.seek(offset)

            def read(self, *args, **kwargs):
                if not tracker["read_failed"] and self._sf.tell() == block_size:
                    tracker["read_failed"] = True
                    raise sf.LibsndfileError(2, "transient SMB read")
                return self._sf.read(*args, **kwargs)

            def close(self):
                self._closed = True
                return self._sf.close()

        def _open_soundfile(inner_path):
            if tracker["seek_failed"] and not tracker["reopen_failed"]:
                tracker["reopen_failed"] = True
                raise OSError("transient reopen error")
            return _StrictHandleProxy(inner_path)

        with mock.patch.object(engine, "FRAMES_BY_BLOCK", block_size), \
             mock.patch.object(engine.sf, "SoundFile", side_effect=_open_soundfile), \
             mock.patch("time.sleep") as mock_sleep:
            recovered = engine.analyze_file_data(
                path, "center", None, False, use_cache=False
            )

        self.assertEqual(tracker["seek_after_close"], 0)
        self.assertTrue(tracker["read_failed"])
        self.assertTrue(tracker["seek_failed"])
        self.assertTrue(tracker["reopen_failed"])
        self.assertEqual(recovered["md5"]["estado"], engine.Md5Status.MATCH.value)
        self.assertEqual(
            recovered["md5"]["calculado"], expected["md5"]["calculado"]
        )
        self.assertEqual(recovered["veredicto"], expected["veredicto"])
        self.assertEqual(mock_sleep.call_count, 2)

    def test_late_exception_after_verdict_preserves_error_xor_verdict(self):
        """Exceptions raised after verdict/score assignment must still return
        a pure error payload with no verdict or score keys."""
        path = self.fx["ok16"]
        for hook_name in ("forensic_evidence", "write_cache"):
            with self.subTest(hook=hook_name):
                with mock.patch.object(
                    engine, hook_name, side_effect=RuntimeError("late failure")
                ):
                    res = engine.analyze_file_data(
                        path, "center", None, False, use_cache=True
                    )
                self.assertIn("error", res)
                self.assertNotIn("veredicto", res)
                self.assertNotIn("score", res)
                self.assertIn("late failure", res["error"])
                self.assertIn("meta", res)
                self.assertIn("md5", res)


if __name__ == "__main__":
    unittest.main(verbosity=2)
