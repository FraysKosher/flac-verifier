# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 FLAC VERIFIER Contributors
"""Unit tests for the GitHub Releases auto-updater (`updater.py`, Phase 4)."""
from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
import urllib.error

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import updater


class _FakeResponse:
    def __init__(self, payload: bytes, headers: dict[str, str] | None = None) -> None:
        self._buf = io.BytesIO(payload)
        self.headers = headers or {"Content-Length": str(len(payload))}

    def read(self, size: int = -1) -> bytes:
        return self._buf.read(size)

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self._buf.close()


class _FakeOpener:
    def __init__(self, responses: dict[str, bytes | Exception]) -> None:
        self._responses = responses

    def open(self, req, timeout: float = 6.0):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        item = self._responses.get(url)
        if item is None:
            raise urllib.error.URLError(f"No fake response for {url}")
        if isinstance(item, Exception):
            raise item
        return _FakeResponse(item)


class TestUpdater(unittest.TestCase):

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="flac_v3_updater_")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_semver_parsing_and_comparison(self) -> None:
        self.assertTrue(updater.is_newer_version("3.0.1", "3.0.0"))
        self.assertTrue(updater.is_newer_version("v3.1.0", "3.0.9"))
        self.assertTrue(updater.is_newer_version("3.0.0", "3.0.0-rc1"))
        self.assertFalse(updater.is_newer_version("3.0.0", "3.0.0"))
        self.assertFalse(updater.is_newer_version("2.13.2", "3.0.0"))
        self.assertFalse(updater.is_newer_version("not-a-version", "3.0.0"))

    def test_strict_https_and_host_allowlist(self) -> None:
        valid_urls = [
            "https://github.com/FraysKosher/flac-verifier/releases/latest/download/latest.json",
            "https://github.com/FraysKosher/flac-verifier/releases/download/v3.0.1/FLAC_Verifier_Setup_3.0.1-win64.exe",
            "https://objects.githubusercontent.com/github-production-release-asset/12345",
            "https://release-assets.githubusercontent.com/assets/installer.exe",
        ]
        for url in valid_urls:
            self.assertEqual(updater.validate_release_url(url), url)

        invalid_urls = [
            "http://github.com/FraysKosher/flac-verifier/releases/latest/download/latest.json",
            "https://github.com/other-user/flac-verifier/releases/latest/download/latest.json",
            "https://evil.example.com/FLAC_Verifier_Setup.exe",
            "file:///C:/Windows/System32/cmd.exe",
            "",
        ]
        for bad in invalid_urls:
            with self.subTest(url=bad):
                with self.assertRaises(ValueError):
                    updater.validate_release_url(bad)

    def test_offline_mode_and_timeout_handling(self) -> None:
        offline = updater.check_for_update("3.0.0", offline_mode=True, portable=False)
        self.assertTrue(offline["ok"])
        self.assertFalse(offline["available"])
        self.assertTrue(offline["skipped"])

        opener = _FakeOpener({updater.LATEST_JSON_URL: TimeoutError("timed out")})
        timed_out = updater.check_for_update("3.0.0", opener=opener, portable=False)
        self.assertFalse(timed_out["ok"])
        self.assertFalse(timed_out["available"])
        self.assertIn("timed out", timed_out["error"].lower())

    def test_latest_json_404_falls_back_to_github_api_cleanly(self) -> None:
        http_404 = urllib.error.HTTPError(
            updater.LATEST_JSON_URL, 404, "Not Found", hdrs=None, fp=None
        )
        # 1) Older release tag on GitHub API -> friendly up-to-date response (no raw 404 error).
        api_older = json.dumps({
            "tag_name": "v2.13.2",
            "html_url": "https://github.com/FraysKosher/flac-verifier/releases/tag/v2.13.2",
        }).encode("utf-8")
        opener_same = _FakeOpener({
            updater.LATEST_JSON_URL: http_404,
            updater.GITHUB_API_LATEST_URL: api_older,
        })
        res_same = updater.check_for_update("3.0.0", opener=opener_same, portable=False)
        self.assertTrue(res_same["ok"], res_same)
        self.assertFalse(res_same["available"])
        self.assertIn("up to date", res_same.get("message", "").lower())
        self.assertNotIn("HTTPError", json.dumps(res_same))

        # 2) Newer release tag without latest.json -> marks available as manual/portable release link.
        api_newer = json.dumps({
            "tag_name": "v3.1.0",
            "name": "FLAC VERIFIER 3.1.0",
            "html_url": "https://github.com/FraysKosher/flac-verifier/releases/tag/v3.1.0",
        }).encode("utf-8")
        opener_newer = _FakeOpener({
            updater.LATEST_JSON_URL: http_404,
            updater.GITHUB_API_LATEST_URL: api_newer,
        })
        res_newer = updater.check_for_update("3.0.0", opener=opener_newer, portable=False)
        self.assertTrue(res_newer["ok"], res_newer)
        self.assertTrue(res_newer["available"])
        self.assertEqual(res_newer["version"], "3.1.0")
        self.assertTrue(res_newer["is_portable"])
        self.assertIsNone(res_newer["installer"])

        # 3) Both latest.json and API return 404 -> friendly up-to-date message.
        api_404 = urllib.error.HTTPError(
            updater.GITHUB_API_LATEST_URL, 404, "Not Found", hdrs=None, fp=None
        )
        opener_none = _FakeOpener({
            updater.LATEST_JSON_URL: http_404,
            updater.GITHUB_API_LATEST_URL: api_404,
        })
        res_none = updater.check_for_update("3.0.0", opener=opener_none, portable=False)
        self.assertTrue(res_none["ok"], res_none)
        self.assertFalse(res_none["available"])
        self.assertIn("up to date", res_none.get("message", "").lower())

    def test_check_download_and_sha256_verification_pass_and_fail(self) -> None:
        installer_bytes = b"MZ-fake-inno-setup-installer-payload" * 100
        good_sha = hashlib.sha256(installer_bytes).hexdigest()
        bad_sha = "0" * 64
        dl_url = "https://github.com/FraysKosher/flac-verifier/releases/download/v3.1.0/FLAC_Verifier_Setup_3.1.0-win64.exe"
        zip_url = "https://github.com/FraysKosher/flac-verifier/releases/download/v3.1.0/FLAC_Verifier_3.1.0-win64-portable.zip"

        manifest_ok = json.dumps({
            "version": "3.1.0",
            "tag": "v3.1.0",
            "notes": "Faster library scanning",
            "installer": {
                "filename": "FLAC_Verifier_Setup_3.1.0-win64.exe",
                "url": dl_url,
                "sha256": good_sha,
                "size": len(installer_bytes),
            },
            "portable": {
                "filename": "FLAC_Verifier_3.1.0-win64-portable.zip",
                "url": zip_url,
                "sha256": good_sha,
                "size": len(installer_bytes),
            },
        }).encode("utf-8")

        opener = _FakeOpener({
            updater.LATEST_JSON_URL: manifest_ok,
            dl_url: installer_bytes,
        })

        # Installed mode detects available update.
        info = updater.check_for_update("3.0.0", portable=False, opener=opener)
        self.assertTrue(info["ok"])
        self.assertTrue(info["available"])
        self.assertFalse(info["is_portable"])
        self.assertEqual(info["version"], "3.1.0")

        # Portable mode also detects available update and marks is_portable=True.
        info_port = updater.check_for_update("3.0.0", portable=True, opener=opener)
        self.assertTrue(info_port["available"])
        self.assertTrue(info_port["is_portable"])

        # Valid SHA-256 download succeeds and invokes progress callback.
        progress_ticks: list[tuple[int, int]] = []
        dl_ok = updater.download_and_verify_installer(
            info,
            dest_dir=self.tmp,
            on_progress=lambda done, total: progress_ticks.append((done, total)),
            opener=opener,
        )
        self.assertTrue(dl_ok["ok"], dl_ok)
        self.assertTrue(os.path.isfile(dl_ok["installer_path"]))
        self.assertEqual(dl_ok["sha256"], good_sha)
        self.assertTrue(progress_ticks)

        # Launching verified installer passes silent Inno Setup flags.
        launched: list[list[str]] = []
        launch_res = updater.launch_installer(
            dl_ok["installer_path"],
            launcher=lambda cmd, **_kw: launched.append(cmd),
        )
        self.assertTrue(launch_res["ok"])
        self.assertIn("/VERYSILENT", launched[0])
        self.assertIn("/NORESTART", launched[0])

        # Tampered SHA-256 MUST fail and delete the downloaded file immediately.
        tampered = dict(info)
        tampered["installer"] = dict(info["installer"], sha256=bad_sha)
        bad_dir = os.path.join(self.tmp, "bad_check")
        dl_bad = updater.download_and_verify_installer(
            tampered, dest_dir=bad_dir, opener=opener
        )
        self.assertFalse(dl_bad["ok"])
        self.assertIn("SHA-256", dl_bad["error"])
        self.assertEqual(os.listdir(bad_dir), [])

    def test_release_metadata_generation_and_iss_script_present(self) -> None:
        setup_path = os.path.join(self.tmp, "FLAC_Verifier_Setup_3.0.0-win64.exe")
        zip_path = os.path.join(self.tmp, "FLAC_Verifier_3.0.0-win64-portable.zip")
        with open(setup_path, "wb") as fh:
            fh.write(b"setup-binary-test")
        with open(zip_path, "wb") as fh:
            fh.write(b"portable-zip-test")

        out = updater.write_release_metadata(
            self.tmp,
            "3.0.0",
            installer_path=setup_path,
            portable_zip_path=zip_path,
            notes="FLAC VERIFIER 3.0.0",
        )
        self.assertTrue(os.path.isfile(out["latest_json"]))
        self.assertTrue(os.path.isfile(out["sha256sums"]))

        with open(out["latest_json"], encoding="utf-8") as fh:
            parsed = updater.parse_manifest(fh.read())
        self.assertEqual(parsed["version"], "3.0.0")
        self.assertEqual(parsed["installer"]["sha256"], updater.compute_file_sha256(setup_path))
        self.assertEqual(parsed["portable"]["sha256"], updater.compute_file_sha256(zip_path))

        # Verify Inno Setup script exists and enforces per-user lowest privileges.
        iss_path = os.path.join(ROOT, "installer", "flac_verifier.iss")
        self.assertTrue(os.path.isfile(iss_path))
        with open(iss_path, encoding="utf-8") as fh:
            iss = fh.read()
        self.assertIn("PrivilegesRequired=lowest", iss)
        self.assertIn("{localappdata}\\Programs\\", iss)
        self.assertIn("portable.flag", iss)


if __name__ == "__main__":
    unittest.main(verbosity=2)
