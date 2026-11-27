# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 FLAC VERIFIER Contributors
"""Offline album artwork extractor and WebP thumbnail cache for FLAC VERIFIER v3.

Extraction priority (100% offline, one extraction per album):
  1. Embedded FLAC `METADATA_BLOCK_PICTURE` (`type == 3` Front Cover first, then
     any other embedded image across the album's tracks).
  2. Folder sidecar images (`cover`, `folder`, `front`, `album`, `artwork` with
     `.jpg`, `.jpeg`, `.png`, `.webp`, or `.bmp` extension, case-insensitive).
  3. `None` (the UI renders a deterministic geometric SVG sleeve).

Resilience guarantees:
  - Corrupt, truncated, oversized, CMYK, palette, 16-bit, or transparent images
    are safely caught and either normalised to RGB or skipped to the next
    fallback without ever aborting an album scan.
  - Content-addressed SHA-256 hashing deduplicates identical artwork across
    tracks and multi-disc releases: an already cached `320x320` WebP thumbnail is
    reused immediately without re-decoding.
"""
from __future__ import annotations

import base64
import hashlib
import io
import os
import tempfile
from typing import Iterable
import warnings

from mutagen.flac import FLAC
from PIL import Image, ImageOps

from library_db import natural_sort_key, normalize_path, resolve_data_dir

COVER_SIZE: tuple[int, int] = (320, 320)
WEBP_QUALITY = 82
WEBP_METHOD = 4
COVERS_SUBDIR = "covers"

# Guard against corrupted headers or decompression bombs without blocking real scans.
MAX_IMAGE_BYTES = 25 * 1024 * 1024  # 25 MB raw image payload cap
MAX_IMAGE_PIXELS = 40_000_000       # 40 MP cap (~6300x6300)

# Neutral mineral dark canvas used when compositing transparent PNG/RGBA artwork.
ALPHA_BACKDROP_RGB: tuple[int, int, int] = (21, 25, 29)

SIDECAR_STEMS: tuple[str, ...] = ("cover", "folder", "front", "album", "artwork")
SIDECAR_EXTS: tuple[str, ...] = (".jpg", ".jpeg", ".png", ".webp", ".bmp")

_FRONT_COVER_TYPE = 3  # FLAC METADATA_BLOCK_PICTURE type 3 = Cover (front)


def default_covers_dir(
    base_dir: str | None = None,
    *,
    executable_path: str | None = None,
) -> str:
    """Returns `%APPDATA%\\FLAC Verifier\\covers` (or portable `<app>\\data\\covers`)."""
    return os.path.join(
        resolve_data_dir(base_dir, executable_path=executable_path),
        COVERS_SUBDIR,
    )


def cover_hash_for_bytes(raw_bytes: bytes) -> str:
    """Short content-addressed SHA-256 identifier (16 lowercase hex chars)."""
    return hashlib.sha256(raw_bytes).hexdigest()[:16]


def cover_file_path(cover_hash: str | None, cache_dir: str | None = None) -> str | None:
    """Returns the cached `.webp` path if it exists on disk, else None."""
    if not cover_hash or not _is_safe_hash(cover_hash):
        return None
    root = normalize_path(cache_dir) if cache_dir else default_covers_dir()
    candidate = os.path.join(root, f"{cover_hash}.webp")
    return candidate if os.path.isfile(candidate) and os.path.getsize(candidate) > 0 else None


def cover_data_uri(cover_hash: str | None, cache_dir: str | None = None) -> str | None:
    """Returns a `data:image/webp;base64,...` URI for a cached cover hash."""
    path = cover_file_path(cover_hash, cache_dir=cache_dir)
    if not path:
        return None
    try:
        with open(path, "rb") as fh:
            encoded = base64.b64encode(fh.read()).decode("ascii")
        return f"data:image/webp;base64,{encoded}"
    except OSError:
        return None


def _is_safe_hash(value: str) -> bool:
    return bool(value) and len(value) <= 64 and all(c in "0123456789abcdef" for c in value.lower())


def _normalize_to_rgb(img: Image.Image) -> Image.Image:
    """Converts any Pillow mode (CMYK, RGBA, grayscale-alpha, palette, L, 1, I) into RGB."""
    try:
        img = ImageOps.exif_transpose(img)
    except Exception:
        pass

    mode = img.mode
    if mode == "RGB":
        return img
    if mode == "P":
        img = img.convert("RGBA") if "transparency" in img.info else img.convert("RGB")
        mode = img.mode
    if mode.upper().endswith("A") and mode != "RGBA" or mode == "RGBA":
        rgba = img.convert("RGBA")
        canvas = Image.new("RGB", rgba.size, ALPHA_BACKDROP_RGB)
        canvas.paste(rgba, mask=rgba.getchannel("A"))
        return canvas
    if mode == "CMYK":
        return img.convert("RGB")
    return img.convert("RGB")


def _encode_cover_bytes(raw_bytes: bytes, dest_path: str) -> bool:
    """Decodes raw artwork bytes, normalises to 320x320 RGB WebP, writes atomically."""
    if not raw_bytes or len(raw_bytes) > MAX_IMAGE_BYTES:
        return False

    prev_limit = Image.MAX_IMAGE_PIXELS
    Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS
    tmp_path = ""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw_bytes)) as opened:
                w, h = opened.size
                if w <= 0 or h <= 0 or (w * h) > MAX_IMAGE_PIXELS:
                    return False
                opened.load()
                rgb = _normalize_to_rgb(opened)

        resample = getattr(getattr(Image, "Resampling", Image), "LANCZOS", Image.BICUBIC)
        thumb = ImageOps.fit(rgb, COVER_SIZE, method=resample, centering=(0.5, 0.5))

        folder = os.path.dirname(dest_path)
        os.makedirs(folder, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(prefix=".cover-", suffix=".webp", dir=folder)
        os.close(fd)
        thumb.save(tmp_path, format="WEBP", quality=WEBP_QUALITY, method=WEBP_METHOD)
        if os.path.getsize(tmp_path) <= 0:
            return False
        os.replace(tmp_path, dest_path)
        tmp_path = ""
        return True
    except Exception:
        return False
    finally:
        Image.MAX_IMAGE_PIXELS = prev_limit
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def cache_cover_bytes(raw_bytes: bytes, cache_dir: str | None = None) -> str | None:
    """Caches raw image bytes as a content-addressed 320x320 WebP thumbnail.

    Returns the 16-hex cover hash on success, or `None` if the image is invalid.
    """
    if not raw_bytes or len(raw_bytes) > MAX_IMAGE_BYTES:
        return None
    digest = cover_hash_for_bytes(raw_bytes)
    root = normalize_path(cache_dir) if cache_dir else default_covers_dir()
    dest = os.path.join(root, f"{digest}.webp")
    if os.path.isfile(dest) and os.path.getsize(dest) > 0:
        return digest
    if _encode_cover_bytes(raw_bytes, dest):
        return digest
    return None


def _iter_embedded_pictures(flac_path: str) -> Iterable[bytes]:
    """Yields raw bytes of embedded FLAC pictures, Front Cover (type 3) first."""
    try:
        audio = FLAC(flac_path)
        pictures = list(audio.pictures or [])
    except Exception:
        return
    if not pictures:
        return
    pictures.sort(key=lambda pic: (0 if getattr(pic, "type", None) == _FRONT_COVER_TYPE else 1))
    for pic in pictures:
        data = getattr(pic, "data", None)
        if isinstance(data, (bytes, bytearray)) and data:
            yield bytes(data)


def _find_sidecar_candidates(album_folder: str) -> list[str]:
    """Lists matching sidecar image files in priority order."""
    if not album_folder or not os.path.isdir(album_folder):
        return []
    try:
        entries = os.listdir(album_folder)
    except OSError:
        return []

    by_stem_ext: dict[tuple[str, str], str] = {}
    for entry in entries:
        full = os.path.join(album_folder, entry)
        if not os.path.isfile(full):
            continue
        stem, ext = os.path.splitext(entry)
        stem_low = stem.lower()
        ext_low = ext.lower()
        if stem_low in SIDECAR_STEMS and ext_low in SIDECAR_EXTS:
            by_stem_ext.setdefault((stem_low, ext_low), full)

    ordered: list[str] = []
    for stem in SIDECAR_STEMS:
        for ext in SIDECAR_EXTS:
            hit = by_stem_ext.get((stem, ext))
            if hit:
                ordered.append(hit)
    return ordered


def _read_sidecar_bytes(path: str) -> bytes | None:
    try:
        size = os.path.getsize(path)
        if size <= 0 or size > MAX_IMAGE_BYTES:
            return None
        with open(path, "rb") as fh:
            return fh.read(MAX_IMAGE_BYTES + 1)
    except OSError:
        return None


def extract_album_cover(
    album_folder: str,
    flac_paths: Iterable[str] | None = None,
    *,
    cache_dir: str | None = None,
) -> str | None:
    """Extracts and caches a single 320x320 WebP cover for an album folder.

    Tries embedded FLAC artwork first (Front Cover `type=3` prioritised), then
    folder sidecar images (`cover/folder/front/album/artwork`), and returns
    `None` if no valid artwork exists. Never raises on corrupt media.
    """
    folder_norm = normalize_path(album_folder)
    files: list[str]
    if flac_paths is not None:
        files = [normalize_path(p) for p in flac_paths]
    elif os.path.isdir(folder_norm):
        try:
            entries = sorted(
                (n for n in os.listdir(folder_norm) if n.lower().endswith(".flac")),
                key=natural_sort_key,
            )
            files = [os.path.join(folder_norm, n) for n in entries]
        except OSError:
            files = []
    else:
        files = []

    # 1. Embedded FLAC METADATA_BLOCK_PICTURE (stops at first valid decodable cover).
    seen_digests: set[str] = set()
    for flac_path in files:
        for raw in _iter_embedded_pictures(flac_path):
            if len(raw) > MAX_IMAGE_BYTES:
                continue
            digest = cover_hash_for_bytes(raw)
            if digest in seen_digests:
                continue
            seen_digests.add(digest)
            cached = cache_cover_bytes(raw, cache_dir=cache_dir)
            if cached:
                return cached

    # 2. Sidecar files in the album directory.
    for sidecar in _find_sidecar_candidates(folder_norm):
        raw = _read_sidecar_bytes(sidecar)
        if not raw:
            continue
        cached = cache_cover_bytes(raw, cache_dir=cache_dir)
        if cached:
            return cached

    # 3. Clean fallback: frontend renders deterministic geometric sleeve.
    return None


def prune_unused_covers(
    active_hashes: set[str],
    cache_dir: str | None = None,
) -> int:
    """Deletes cached `.webp` thumbnails no longer referenced by any album."""
    root = normalize_path(cache_dir) if cache_dir else default_covers_dir()
    if not os.path.isdir(root):
        return 0
    removed = 0
    try:
        entries = os.listdir(root)
    except OSError:
        return 0
    for name in entries:
        if not name.lower().endswith(".webp"):
            continue
        stem = os.path.splitext(name)[0]
        if stem not in active_hashes:
            try:
                os.unlink(os.path.join(root, name))
                removed += 1
            except OSError:
                pass
    return removed
