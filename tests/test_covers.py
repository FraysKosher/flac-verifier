# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 FLAC VERIFIER Contributors
"""Unit tests for the offline album cover extractor (`covers.py`)."""
from __future__ import annotations

import io
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

import numpy as np
from mutagen.flac import FLAC, Picture
from PIL import Image
import soundfile as sf

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import covers


def _make_flac(path: str) -> str:
    audio = np.zeros((2048, 2), dtype=np.float32)
    sf.write(path, audio, 44100, subtype="PCM_16")
    return path


def _image_bytes(
    size: tuple[int, int] = (400, 400),
    mode: str = "RGB",
    color: tuple = (40, 120, 160),
    fmt: str = "PNG",
) -> bytes:
    img = Image.new(mode, size, color)
    buf = io.BytesIO()
    img.save(buf, format=fmt)
    return buf.getvalue()


def _attach_picture(
    flac_path: str,
    raw_bytes: bytes,
    *,
    pic_type: int = 3,
    mime: str = "image/png",
) -> None:
    audio = FLAC(flac_path)
    pic = Picture()
    pic.type = pic_type
    pic.mime = mime
    pic.data = raw_bytes
    audio.add_picture(pic)
    audio.save()


class TestCovers(unittest.TestCase):

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="flac_v3_covers_")
        self.cache_dir = os.path.join(self.tmp, "covers_cache")
        self.album_dir = os.path.join(self.tmp, "Album")
        os.makedirs(self.album_dir, exist_ok=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_embedded_front_cover_prioritized_over_other_types_and_sidecar(self) -> None:
        track = _make_flac(os.path.join(self.album_dir, "01.flac"))
        back_bytes = _image_bytes((300, 300), "RGB", (200, 30, 30), "PNG")
        front_bytes = _image_bytes((500, 500), "RGB", (30, 90, 210), "PNG")
        _attach_picture(track, back_bytes, pic_type=4)   # Cover (back)
        _attach_picture(track, front_bytes, pic_type=3)  # Cover (front)

        # Also add a sidecar that should NOT override embedded front cover.
        with open(os.path.join(self.album_dir, "cover.jpg"), "wb") as fh:
            fh.write(_image_bytes((300, 300), "RGB", (20, 200, 40), "JPEG"))

        digest = covers.extract_album_cover(self.album_dir, cache_dir=self.cache_dir)
        self.assertEqual(digest, covers.cover_hash_for_bytes(front_bytes))

        cached_path = covers.cover_file_path(digest, cache_dir=self.cache_dir)
        self.assertIsNotNone(cached_path)
        with Image.open(cached_path) as out:
            self.assertEqual(out.format, "WEBP")
            self.assertEqual(out.size, covers.COVER_SIZE)
            self.assertEqual(out.mode, "RGB")

    def test_sidecar_priority_and_case_insensitivity(self) -> None:
        _make_flac(os.path.join(self.album_dir, "01.flac"))
        folder_bytes = _image_bytes((240, 240), "RGB", (90, 90, 90), "PNG")
        cover_bytes = _image_bytes((240, 240), "RGB", (180, 120, 40), "JPEG")
        with open(os.path.join(self.album_dir, "FOLDER.PNG"), "wb") as fh:
            fh.write(folder_bytes)
        with open(os.path.join(self.album_dir, "Cover.JPG"), "wb") as fh:
            fh.write(cover_bytes)

        digest = covers.extract_album_cover(self.album_dir, cache_dir=self.cache_dir)
        self.assertEqual(digest, covers.cover_hash_for_bytes(cover_bytes))

    def test_fallback_returns_none_when_no_artwork(self) -> None:
        _make_flac(os.path.join(self.album_dir, "01.flac"))
        self.assertIsNone(covers.extract_album_cover(self.album_dir, cache_dir=self.cache_dir))

    def test_corrupt_embedded_falls_back_to_valid_sidecar(self) -> None:
        track = _make_flac(os.path.join(self.album_dir, "01.flac"))
        _attach_picture(track, b"\xff\xd8\xff\xe0corrupt-truncated-jpeg", pic_type=3, mime="image/jpeg")

        # Put a corrupt cover.jpg AND a valid folder.png.
        with open(os.path.join(self.album_dir, "cover.jpg"), "wb") as fh:
            fh.write(b"not an image at all")
        valid_png = _image_bytes((360, 200), "RGB", (60, 150, 120), "PNG")
        with open(os.path.join(self.album_dir, "folder.png"), "wb") as fh:
            fh.write(valid_png)

        digest = covers.extract_album_cover(self.album_dir, cache_dir=self.cache_dir)
        self.assertEqual(digest, covers.cover_hash_for_bytes(valid_png))

    def test_corrupt_all_sources_returns_none_safely(self) -> None:
        bad_flac = os.path.join(self.album_dir, "01.flac")
        with open(bad_flac, "wb") as fh:
            fh.write(b"garbage-flac-bytes")
        with open(os.path.join(self.album_dir, "cover.jpg"), "wb") as fh:
            fh.write(b"\xff\xd8truncated")
        self.assertIsNone(covers.extract_album_cover(self.album_dir, cache_dir=self.cache_dir))

    def test_cmyk_rgba_palette_and_grayscale_convert_to_rgb_webp(self) -> None:
        variants = [
            ("CMYK", (10, 80, 90, 20), "JPEG"),
            ("RGBA", (80, 160, 200, 128), "PNG"),
            ("LA", (180, 100), "PNG"),
            ("L", 140, "PNG"),
            ("P", 7, "PNG"),
        ]
        for mode, color, fmt in variants:
            with self.subTest(mode=mode):
                raw = _image_bytes((480, 300), mode, color, fmt)
                digest = covers.cache_cover_bytes(raw, cache_dir=self.cache_dir)
                self.assertIsNotNone(digest)
                path = covers.cover_file_path(digest, cache_dir=self.cache_dir)
                self.assertIsNotNone(path)
                with Image.open(path) as img:
                    self.assertEqual(img.format, "WEBP")
                    self.assertEqual(img.size, covers.COVER_SIZE)
                    self.assertEqual(img.mode, "RGB")

    def test_oversized_bytes_and_pixel_bomb_rejected_safely(self) -> None:
        with mock.patch.object(covers, "MAX_IMAGE_BYTES", 64):
            raw = _image_bytes((200, 200), "RGB", (50, 50, 50), "PNG")
            self.assertGreater(len(raw), 64)
            self.assertIsNone(covers.cache_cover_bytes(raw, cache_dir=self.cache_dir))

        with mock.patch.object(covers, "MAX_IMAGE_PIXELS", 10_000):
            raw = _image_bytes((200, 200), "RGB", (50, 50, 50), "PNG")
            self.assertIsNone(covers.cache_cover_bytes(raw, cache_dir=self.cache_dir))

    def test_deduplication_encodes_only_once_across_tracks_and_rescans(self) -> None:
        art = _image_bytes((350, 350), "RGB", (45, 115, 130), "PNG")
        paths = []
        for idx in range(1, 5):
            p = _make_flac(os.path.join(self.album_dir, f"{idx:02d}.flac"))
            _attach_picture(p, art, pic_type=3)
            paths.append(p)

        with mock.patch.object(covers, "_encode_cover_bytes", wraps=covers._encode_cover_bytes) as spy:
            h1 = covers.extract_album_cover(self.album_dir, paths, cache_dir=self.cache_dir)
            h2 = covers.extract_album_cover(self.album_dir, paths, cache_dir=self.cache_dir)
            self.assertEqual(h1, h2)
            self.assertEqual(spy.call_count, 1)

    def test_data_uri_and_prune_unused_covers(self) -> None:
        raw_keep = _image_bytes((200, 200), "RGB", (10, 20, 30), "PNG")
        raw_drop = _image_bytes((200, 200), "RGB", (200, 100, 50), "PNG")
        h_keep = covers.cache_cover_bytes(raw_keep, cache_dir=self.cache_dir)
        h_drop = covers.cache_cover_bytes(raw_drop, cache_dir=self.cache_dir)
        self.assertIsNotNone(h_keep)
        self.assertIsNotNone(h_drop)

        uri = covers.cover_data_uri(h_keep, cache_dir=self.cache_dir)
        self.assertTrue(uri and uri.startswith("data:image/webp;base64,"))

        removed = covers.prune_unused_covers({h_keep}, cache_dir=self.cache_dir)
        self.assertEqual(removed, 1)
        self.assertIsNotNone(covers.cover_file_path(h_keep, cache_dir=self.cache_dir))
        self.assertIsNone(covers.cover_file_path(h_drop, cache_dir=self.cache_dir))


if __name__ == "__main__":
    unittest.main(verbosity=2)
