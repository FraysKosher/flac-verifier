# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 FLAC VERIFIER Contributors
"""Compact interactive spectrogram grid builder and disk cache for v3 Canvas UI.

Produces a lightweight `256 x 160` uint8 dBFS grid (`[-130, 0]` dBFS) on demand
when a track is inspected, reusing `_motor_espectral._rfft_f32` (compiled C++
`pypocketfft`, zero `scipy` Python imports) with bounded memory and zlib disk
caching (~15-30 KB per track under `<data_dir>/specs/`).

Design invariants:
  - Presentation-only: computes zero forensic verdicts, cut-offs, or scores.
    All forensic overlay markers are read strictly from the already-computed
    engine result dictionary.
  - Zero impact on Engine 17.3 analysis speed or NDJSON payload size: mass
    library scans never serialize grids into NDJSON or SQLite.
"""
from __future__ import annotations

import base64
import hashlib
import os
import struct
import tempfile
import threading
from typing import Any
import zlib

import numpy as np
import soundfile as sf

import _motor_espectral as spectral_mod
from library_db import normalize_path, path_key, resolve_data_dir

SPECS_SUBDIR = "specs"
GRID_WIDTH = 256
GRID_HEIGHT = 160
MIN_DB = -130.0
MAX_DB = 0.0
_CACHE_MAGIC = b"FVSP01"
_HEADER_FMT = "<6sHHIdff"  # magic, width, height, sample_rate, duration_s, min_db, max_db
_HEADER_SIZE = struct.calcsize(_HEADER_FMT)
_STREAM_BLOCK = 262144


def default_specs_dir(
    base_dir: str | None = None,
    *,
    executable_path: str | None = None,
) -> str:
    """Returns `%APPDATA%\\FLAC Verifier\\specs` (or portable `<app>\\data\\specs`)."""
    return os.path.join(
        resolve_data_dir(base_dir, executable_path=executable_path),
        SPECS_SUBDIR,
    )


def _grid_cache_key(
    file_path: str,
    file_size: int,
    file_mtime_ns: int,
    width: int,
    height: int,
) -> str:
    token = f"{path_key(file_path)}|{file_size}|{file_mtime_ns}|{width}x{height}|v1"
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:20]


def _read_cached_grid(cache_path: str) -> dict[str, Any] | None:
    if not os.path.isfile(cache_path):
        return None
    try:
        with open(cache_path, "rb") as fh:
            raw = fh.read()
        if len(raw) <= _HEADER_SIZE:
            return None
        magic, width, height, sr, duration_s, min_db, max_db = struct.unpack(
            _HEADER_FMT, raw[:_HEADER_SIZE]
        )
        if magic != _CACHE_MAGIC or width <= 0 or height <= 0 or sr <= 0:
            return None
        grid_bytes = zlib.decompress(raw[_HEADER_SIZE:])
        if len(grid_bytes) != width * height:
            return None
        return {
            "width": int(width),
            "height": int(height),
            "sample_rate": int(sr),
            "nyquist_hz": float(sr) / 2.0,
            "duration_s": round(float(duration_s), 3),
            "min_db": float(min_db),
            "max_db": float(max_db),
            "grid_bytes": grid_bytes,
            "from_cache": True,
            "cache_path": cache_path,
        }
    except Exception:
        return None


def _write_cached_grid(
    cache_path: str,
    *,
    width: int,
    height: int,
    sample_rate: int,
    duration_s: float,
    min_db: float,
    max_db: float,
    grid_bytes: bytes,
) -> bool:
    folder = os.path.dirname(cache_path)
    tmp_path = ""
    try:
        os.makedirs(folder, exist_ok=True)
        header = struct.pack(
            _HEADER_FMT,
            _CACHE_MAGIC,
            int(width),
            int(height),
            int(sample_rate),
            float(duration_s),
            float(min_db),
            float(max_db),
        )
        compressed = zlib.compress(grid_bytes, level=6)
        fd, tmp_path = tempfile.mkstemp(prefix=".spec-", suffix=".specz", dir=folder)
        with os.fdopen(fd, "wb") as fh:
            fh.write(header)
            fh.write(compressed)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, cache_path)
        tmp_path = ""
        return True
    except Exception:
        return False
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def _bin_spectrum_to_rows(power_bins: np.ndarray, height: int) -> np.ndarray:
    """Maps FFT frequency bins (`0..Nyquist`) onto `height` vertical rows via max-pooling.

    Max-pooling keeps narrow forensic landmarks (EnCodec harmonic combs, MP3
    sfb21 islands, Nyquist cliffs) crisp at compact Canvas resolution.
    Row 0 corresponds to 0 Hz; row `height - 1` corresponds to Nyquist.
    """
    n_bins = int(power_bins.shape[0])
    if n_bins <= 0:
        return np.zeros(height, dtype=np.float32)
    edges = np.linspace(0, n_bins, height + 1, dtype=np.int64)
    if n_bins >= height:
        # Vectorized C-level max-pooling across strictly increasing slice starts.
        return np.maximum.reduceat(power_bins, edges[:-1]).astype(np.float32, copy=False)
    out = np.empty(height, dtype=np.float32)
    for row in range(height):
        lo = int(edges[row])
        hi = max(lo + 1, int(edges[row + 1]))
        if lo >= n_bins:
            lo = n_bins - 1
            hi = n_bins
        out[row] = float(np.max(power_bins[lo:hi]))
    return out


def _quantize_db_to_u8(power_grid: np.ndarray, min_db: float, max_db: float) -> bytes:
    """Converts linear power `[height, width]` to quantized uint8 dBFS bytes."""
    eps = np.float32(1e-16)
    db = np.float32(10.0) * np.log10(np.maximum(power_grid, eps))
    span = float(max_db - min_db)
    norm = np.clip((db - np.float32(min_db)) / np.float32(span), 0.0, 1.0)
    u8 = np.rint(norm * np.float32(255.0)).astype(np.uint8)
    return u8.tobytes()


def _column_power_from_frames(
    frames: np.ndarray,
    window: np.ndarray,
    n_fft: int,
) -> np.ndarray:
    """Computes average power spectrum for 1..2 short mono/stereo sub-frames."""
    if frames.ndim == 2 and frames.shape[1] >= 2:
        mono = (frames[:, 0] + frames[:, 1]) * np.float32(0.5)
    elif frames.ndim == 2:
        mono = frames[:, 0]
    else:
        mono = frames
    mono = np.asarray(mono, dtype=np.float32)
    n = int(mono.shape[0])
    if n <= 0:
        return np.zeros(n_fft // 2 + 1, dtype=np.float32)
    if n < n_fft:
        padded = np.zeros(n_fft, dtype=np.float32)
        padded[:n] = mono
        z = spectral_mod._rfft_f32(padded * window, axis=0)
        return spectral_mod._power_from_fft(z)
    if n >= n_fft + (n_fft // 2):
        hop = n_fft // 2
        f1 = mono[:n_fft] * window
        f2 = mono[hop : hop + n_fft] * window
        z = spectral_mod._rfft_f32(np.stack((f1, f2), axis=0), axis=1)
        p = spectral_mod._power_from_fft(z)
        return np.maximum(p[0], p[1])
    z = spectral_mod._rfft_f32(mono[:n_fft] * window, axis=0)
    return spectral_mod._power_from_fft(z)


def _bin_spectra_batch_to_rows(power_batch: np.ndarray, height: int) -> np.ndarray:
    """Vectorized max-pooling of `[batch, n_bins]` power spectra onto `[height, batch]`."""
    n_bins = int(power_batch.shape[1])
    batch = int(power_batch.shape[0])
    if n_bins <= 0 or batch <= 0:
        return np.zeros((height, batch), dtype=np.float32)
    edges = np.linspace(0, n_bins, height + 1, dtype=np.int64)
    if n_bins >= height:
        # Single C-level max-pool across all columns in the block.
        return np.maximum.reduceat(power_batch, edges[:-1], axis=1).T.astype(np.float32, copy=False)
    out = np.empty((height, batch), dtype=np.float32)
    for row in range(height):
        lo = int(edges[row])
        hi = max(lo + 1, int(edges[row + 1]))
        if lo >= n_bins:
            lo = n_bins - 1
            hi = n_bins
        out[row, :] = np.max(power_batch[:, lo:hi], axis=1)
    return out


def _block_to_mono_f32(block: np.ndarray) -> np.ndarray:
    """Downmixes a SoundFile block to contiguous 1D float32 mono once per chunk."""
    if block.ndim == 2 and block.shape[1] >= 2:
        mono = block[:, 0] + block[:, 1]
        mono *= np.float32(0.5)
        return np.ascontiguousarray(mono, dtype=np.float32)
    if block.ndim == 2:
        return np.ascontiguousarray(block[:, 0], dtype=np.float32)
    return np.ascontiguousarray(block, dtype=np.float32)


def _compute_spectrogram_grid(
    file_path: str,
    width: int = GRID_WIDTH,
    height: int = GRID_HEIGHT,
    cancel_event: threading.Event | None = None,
) -> dict[str, Any]:
    """Computes a bounded-memory `[height, width]` uint8 dBFS grid in one streaming pass.

    Reads sequentially in large `_STREAM_BLOCK` (262,144-frame) chunks, converts
    each chunk to mono once, and evaluates all spectrogram columns inside a
    block with a single batched `_rfft_f32` call — avoiding both 256 random
    libFLAC seeks over SMB/Wi-Fi and per-column Python FFT loop overhead.
    """
    with sf.SoundFile(file_path) as handle:
        sr = int(handle.samplerate)
        total_frames = int(handle.frames)
        if sr <= 0:
            raise ValueError("Invalid sample rate in audio file")
        n_fft = 4096 if sr >= 88200 else 2048
        if 0 < total_frames < n_fft:
            n_fft = max(256, 1 << int(np.floor(np.log2(max(256, total_frames)))))

        win = np.hanning(n_fft + 1)[:-1].astype(np.float32)
        scale = np.float32(1.0 / max(1e-12, float(win.sum())))
        scaled_win = (win * scale).astype(np.float32)
        # Compensate single-sided real FFT energy so 0 dBFS sine aligns near 0 dBFS.
        scaled_win *= np.float32(2.0)

        grid = np.zeros((height, width), dtype=np.float32)
        hop = n_fft // 2
        read_len = n_fft + hop

        estimated_total = total_frames if total_frames > 0 else sr * 300
        max_start = max(0, estimated_total - n_fft)
        if width > 1 and max_start > 0:
            targets = np.linspace(0, max_start, width, dtype=np.int64)
        else:
            targets = np.zeros(width, dtype=np.int64)

        offset = 0
        next_col = 0
        filled = np.zeros(width, dtype=bool)
        remainder = np.zeros(0, dtype=np.float32)

        for block in handle.blocks(
            blocksize=_STREAM_BLOCK, dtype="float32", always_2d=True
        ):
            if cancel_event is not None and cancel_event.is_set():
                return {"ok": False, "cancelled": True, "error": "Spectrogram render cancelled"}
            if block.shape[0] == 0:
                break
            mono_block = _block_to_mono_f32(block)
            if remainder.shape[0] > 0:
                work = np.concatenate((remainder, mono_block))
                base_offset = offset - remainder.shape[0]
            else:
                work = mono_block
                base_offset = offset
            offset += mono_block.shape[0]

            n_avail = int(work.shape[0])
            if n_avail >= n_fft and next_col < width:
                max_target_in_buf = base_offset + n_avail - n_fft
                col_start = next_col
                while next_col < width and int(targets[next_col]) <= max_target_in_buf:
                    next_col += 1
                count = next_col - col_start
                if count > 0:
                    # Collect 1 or 2 overlapping windows per column and run ONE batched FFT.
                    frames_a = np.empty((count, n_fft), dtype=np.float32)
                    frames_b = np.empty((count, n_fft), dtype=np.float32)
                    has_b = False
                    b_mask = np.zeros(count, dtype=bool)
                    for i in range(count):
                        target = int(targets[col_start + i])
                        rel = max(0, min(n_avail - n_fft, target - base_offset))
                        frames_a[i] = work[rel : rel + n_fft]
                        if rel + read_len <= n_avail:
                            frames_b[i] = work[rel + hop : rel + read_len]
                            b_mask[i] = True
                            has_b = True
                    frames_a *= scaled_win
                    z_a = spectral_mod._rfft_f32(frames_a, axis=1)
                    power = spectral_mod._power_from_fft(z_a)
                    if has_b:
                        idx_b = np.flatnonzero(b_mask)
                        fb = frames_b[idx_b] * scaled_win
                        z_b = spectral_mod._rfft_f32(fb, axis=1)
                        p_b = spectral_mod._power_from_fft(z_b)
                        np.maximum(power[idx_b], p_b, out=power[idx_b])
                    grid[:, col_start:next_col] = _bin_spectra_batch_to_rows(power, height)
                    filled[col_start:next_col] = True

            keep = min(read_len, n_avail)
            remainder = work[-keep:].copy() if keep > 0 else np.zeros(0, dtype=np.float32)

        if not np.any(filled) and remainder.shape[0] > 0:
            power = _column_power_from_frames(remainder, scaled_win, n_fft)
            col_vals = _bin_spectrum_to_rows(power, height)
            grid[:, :] = col_vals[:, None]
        elif np.any(filled) and not np.all(filled):
            last_col = np.zeros(height, dtype=np.float32)
            for col in range(width):
                if filled[col]:
                    last_col = grid[:, col]
                else:
                    grid[:, col] = last_col
        duration_s = float(max(total_frames, offset)) / float(sr)

    grid_bytes = _quantize_db_to_u8(grid, MIN_DB, MAX_DB)
    return {
        "width": int(width),
        "height": int(height),
        "sample_rate": int(sr),
        "nyquist_hz": float(sr) / 2.0,
        "duration_s": round(float(duration_s), 3),
        "min_db": MIN_DB,
        "max_db": MAX_DB,
        "grid_bytes": grid_bytes,
        "from_cache": False,
        "cache_path": None,
    }


def extract_forensic_markers(result: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Extracts presentation-only frequency overlay markers from an engine result."""
    if not isinstance(result, dict):
        return []
    spectrum = result.get("esp") or {}
    meta = result.get("meta") or {}
    bdi = result.get("bdi") or {}
    forensic = bdi.get("forense_bits") or {}
    sr = int(meta.get("sample_rate") or 0)
    nyquist = sr / 2.0 if sr > 0 else 0.0
    markers: list[dict[str, Any]] = []

    if spectrum.get("muro_brickwall_nyquist"):
        ceiling = float(spectrum.get("techo_hz") or 22050.0)
        grad = float(spectrum.get("gradiente_nyquist_db_khz") or 0.0)
        label = f"Brickwall {ceiling/1000.0:.1f} kHz"
        if grad > 0:
            label += f" (-{grad:.1f} dB/kHz)"
        markers.append({"freq_hz": ceiling, "label": label, "kind": "brickwall"})

    if spectrum.get("mdct_lattice_detected"):
        family = str(spectrum.get("mdct_lattice_family") or "MDCT")
        snr = float(spectrum.get("mdct_lattice_snr") or 0.0)
        freq = float(spectrum.get("frecuencia_corte") or spectrum.get("techo_hz") or (nyquist * 0.8))
        label = f"{family} · {snr:.1f} σ" if snr > 0 else family
        markers.append({"freq_hz": freq, "label": label, "kind": "lattice"})
    elif spectrum.get("corte_artificial"):
        fc = float(spectrum.get("frecuencia_corte") or spectrum.get("techo_hz") or 0.0)
        has_lossy = bool(
            spectrum.get("sbr_qmf_detectado")
            or spectrum.get("islas_mdct_sfb21")
            or spectrum.get("sospecha_excitador_armonico")
            or spectrum.get("neural_vocoder_detected")
            or spectrum.get("spectral_folding_detected")
            or spectrum.get("sospecha_corte_opus_20k")
            or spectrum.get("modulacion_psicoacustica_20k")
        )
        if fc > 0 and not (
            spectrum.get("rolloff_analogico_genuino")
            or spectrum.get("es_transferencia_dsd")
        ):
            is_hires_src_nyq = sr >= 88200 and not has_lossy and (20500.0 <= fc <= 24800.0)
            lbl = (
                f"Source Nyquist cut-off {fc/1000.0:.2f} kHz"
                if is_hires_src_nyq
                else f"Codec cut-off {fc/1000.0:.2f} kHz"
            )
            markers.append({
                "freq_hz": fc,
                "label": lbl,
                "kind": "cutoff",
            })

    if spectrum.get("sospecha_corte_opus_20k"):
        drop = float(spectrum.get("caida_opus_20k_db") or 0.0)
        label = f"20.0 kHz shelf (-{drop:.0f} dB)" if drop > 0 else "20.0 kHz codec shelf"
        markers.append({"freq_hz": 20000.0, "label": label, "kind": "shelf"})

    if spectrum.get("neural_vocoder_detected"):
        spacing = float(spectrum.get("vocoder_comb_spacing_hz") or 1200.0)
        z_val = float(spectrum.get("vocoder_hf_comb_z") or 0.0)
        anchor = min(nyquist * 0.5, max(4000.0, spacing * 10.0)) if nyquist > 0 else 12000.0
        label = f"Neural comb {spacing:.0f} Hz"
        if z_val > 0:
            label += f" ({z_val:.1f} σ)"
        markers.append({"freq_hz": anchor, "label": label, "kind": "neural"})

    if spectrum.get("spectral_folding_detected"):
        pivot = float(spectrum.get("spectral_folding_pivot_hz") or 0.0)
        if pivot > 0:
            markers.append({
                "freq_hz": pivot,
                "label": f"Spectral folding pivot {pivot/1000.0:.2f} kHz",
                "kind": "folding",
            })

    if not markers and (
        forensic.get("es_padded_16_en_24")
        or forensic.get("es_16bit_dither_en_24")
        or forensic.get("es_24bit_escalado_en_peine")
        or spectrum.get("noise_shaped_16_in_24")
    ):
        ceiling = float(spectrum.get("techo_hz") or (nyquist * 0.9 if nyquist else 20000.0))
        markers.append({
            "freq_hz": ceiling,
            "label": "Inflated 24-bit container (16-bit core)",
            "kind": "bitdepth",
        })

    # Hi-Res ultrasonic ceiling & low-spectral-ceiling markers (e.g. hires_no_ultrasonic / hires_below_cd).
    raw_codes: set[str] = set()
    for src_codes in (
        result.get("hallazgos") or (),
        result.get("finding_codes") or (),
        spectrum.get("finding_codes") or (),
    ):
        for entry in src_codes:
            if isinstance(entry, dict) and entry.get("code"):
                raw_codes.add(str(entry["code"]))
            elif isinstance(entry, str):
                raw_codes.add(entry)
    ceiling_hz = float(spectrum.get("techo_hz") or 0.0)
    verdict = str(result.get("veredicto") or "")
    has_ceiling_marker = any(
        m.get("kind") in ("brickwall", "cutoff", "hires_ceiling", "ceiling")
        for m in markers
    )
    hires_verified_analog = bool(
        (spectrum.get("rolloff_analogico_genuino") or spectrum.get("es_transferencia_dsd"))
        and verdict in ("GENUINE LOSSLESS", "PROBABLY LOSSLESS")
    )
    if sr >= 88200 and ceiling_hz > 0:
        if not has_ceiling_marker and not hires_verified_analog and (
            ceiling_hz < 22050.0
            or "hires_no_ultrasonic" in raw_codes
            or "hires_below_cd" in raw_codes
            or (verdict in ("SUSPICIOUS", "PROBABLE UPSCALE") and ceiling_hz < 24500.0)
        ):
            if ceiling_hz < 22050.0:
                lbl = f"Ceiling {ceiling_hz/1000.0:.1f} kHz (below 22.05 kHz CD limit)"
            else:
                lbl = f"Ceiling {ceiling_hz/1000.0:.1f} kHz (no ultrasonic content)"
            markers.append({"freq_hz": ceiling_hz, "label": lbl, "kind": "hires_ceiling"})
        # Subtle 22.05 kHz CD Nyquist reference line on Hi-Res tracks unless a marker sits right at 22.05 kHz.
        near_cd_ref = any(abs(float(m.get("freq_hz") or 0.0) - 22050.0) < 250.0 for m in markers)
        if not near_cd_ref and nyquist > 26000.0:
            markers.append({
                "freq_hz": 22050.0,
                "label": "CD Nyquist (22.05 kHz)",
                "kind": "reference",
                "subtle": True,
            })
    elif not markers and ceiling_hz > 0 and (
        "low_spectral_ceiling" in raw_codes
        or (nyquist > 0 and ceiling_hz < nyquist * 0.72 and verdict in ("SUSPICIOUS", "PROBABLE UPSCALE"))
    ):
        markers.append({
            "freq_hz": ceiling_hz,
            "label": f"Low spectral ceiling ({ceiling_hz/1000.0:.1f} kHz)",
            "kind": "ceiling",
        })

    return markers


def get_interactive_spectrogram(
    file_path: str,
    *,
    result: dict[str, Any] | None = None,
    cache_dir: str | None = None,
    width: int = GRID_WIDTH,
    height: int = GRID_HEIGHT,
    use_cache: bool = True,
    cancel_event: threading.Event | None = None,
) -> dict[str, Any]:
    """Returns a Canvas-ready interactive spectrogram payload for `file_path`.

    Uses zlib-compressed disk cache under `<data_dir>/specs/<key>.specz`.
    """
    norm = normalize_path(file_path)
    if not norm or not os.path.isfile(norm):
        return {"ok": False, "error": "Audio file is offline or no longer accessible"}

    cache_path: str | None = None
    if use_cache:
        try:
            st = os.stat(norm)
            key = _grid_cache_key(norm, int(st.st_size), int(st.st_mtime_ns), width, height)
            root = normalize_path(cache_dir) if cache_dir else default_specs_dir()
            cache_path = os.path.join(root, f"{key}.specz")
            cached = _read_cached_grid(cache_path)
            if cached is not None:
                grid_bytes = cached.pop("grid_bytes")
                cached["ok"] = True
                cached["grid_b64"] = base64.b64encode(grid_bytes).decode("ascii")
                cached["markers"] = extract_forensic_markers(result)
                return cached
        except OSError:
            cache_path = None

    try:
        computed = _compute_spectrogram_grid(
            norm, width=width, height=height, cancel_event=cancel_event
        )
    except Exception as exc:
        return {"ok": False, "error": f"Could not render spectrogram ({type(exc).__name__}: {exc})"}

    if computed.get("cancelled"):
        return computed

    grid_bytes = computed.pop("grid_bytes")
    if cache_path:
        if _write_cached_grid(
            cache_path,
            width=computed["width"],
            height=computed["height"],
            sample_rate=computed["sample_rate"],
            duration_s=computed["duration_s"],
            min_db=computed["min_db"],
            max_db=computed["max_db"],
            grid_bytes=grid_bytes,
        ):
            computed["cache_path"] = cache_path

    computed["ok"] = True
    computed["grid_b64"] = base64.b64encode(grid_bytes).decode("ascii")
    computed["markers"] = extract_forensic_markers(result)
    return computed
