"""Integrity, native-bit audit, clipping and dynamic range.

Extracted from the motor facade in Phase 4. Nothing here imports the spectral
or cache modules: this layer only needs NumPy, soundfile/mutagen and stdlib.
"""
from enum import Enum
import hashlib
import os
import time

import numpy as np


IO_RETRY_DELAYS = (0.25, 0.75)


def _is_transient_io_error(exc: BaseException) -> bool:
    """True when `exc` represents a transient OS/filesystem/NAS I/O error.

    Logical format errors (invalid FLAC header, bitstream sync loss, or
    unrecognised file format) fail immediately without retrying.
    """
    if isinstance(exc, OSError):
        return True
    cause = getattr(exc, "__cause__", None)
    if isinstance(cause, OSError):
        return True
    if type(exc).__name__ == "LibsndfileError" and getattr(exc, "code", None) == 2:
        return True
    return False


def retry_io(operation, delays=IO_RETRY_DELAYS, sleep_fn=None):
    """Runs `operation()` with short bounded backoff on transient I/O errors."""
    wait = time.sleep if sleep_fn is None else sleep_fn
    total_delays = tuple(delays)
    for attempt in range(len(total_delays) + 1):
        try:
            return operation()
        except Exception as exc:
            if attempt >= len(total_delays) or not _is_transient_io_error(exc):
                raise
            wait(total_delays[attempt])


class AccumulatorState(str, Enum):
    """Lifecycle phase for streaming forensic accumulators."""

    COLLECTING = "collecting"
    COMPLETED = "completed"
    FAILED = "failed"


class Md5Status(str, Enum):
    """STREAMINFO MD5 verification outcome."""

    MATCH = "match"
    MISMATCH = "mismatch"
    ABSENT = "absent"
    UNVERIFIABLE = "unverifiable"


class LsbStatus(str, Enum):
    """Effective LSB utilization status for >16-bit containers."""

    EMPTY = "empty"
    PARTIAL = "partial"
    ACTIVE = "active"
    INDETERMINATE = "indeterminate"


class BitStorageTier(str, Enum):
    """Memory tier for 24/32-bit native integer lattice bitmaps."""

    INACTIVE = "inactive"
    COMPACT_HI16 = "compact_hi16"
    FULL_24BIT = "full_24bit"


# DoP 1.0 alternating top-byte framing marker pairs (0x05/0xFA and 0x06/0xF9).
DOP_MARKER_PAIRS = ((0x05, 0xFA), (0x06, 0xF9))

SHORT_CLIP_RUN_MAX = 2

FRAMES_BY_BLOCK = 1 << 18            # 262 144 frames (~6 s at 44.1 kHz) per read

MAX_SAMPLES_BITS = 1 << 20            # sample for the LSB test

# Vector 4 (high-amplitude slices). A hyper-compressed master has no sample in
# the [-90, -48] dBFS window, so the comb is looked for around -18 and -12 dBFS
# instead: 1 050 000 / 8 388 608 is -18.05 dBFS and 2 100 000 is -12.03 dBFS.
# Each slice is one 16-bit grid step wide (32 768 integers) because that is the
# period of the comb left behind by a 16-bit source scaled into 24 bits.
SLICE_1_BASE = 1050000
SLICE_2_BASE = 2100000
SLICE_WIDTH  = 32768

def _normalize_path_fs(path):
    r"""Returns `ruta` in a form the Windows API can open even past MAX_PATH.

    On Windows the classic file APIs stop at 260 characters, so a classical album
    with long folder and track names can raise FileNotFoundError while the file is
    perfectly readable. The extended-length prefix (\\?\\) lifts that limit; UNC
    paths use \\?\UNC\ instead. Only long ABSOLUTE paths are rewritten (the prefix
    requires an absolute path), and on other systems the path is returned as is.
    """
    if os.name != "nt" or not isinstance(path, str) or not path:
        return path
    if path.startswith("\\\\?\\"):
        return path
    # ntpath.isabs() returns False for UNC paths without a drive (\\server\share),
    # so the two leading-separator forms are accepted explicitly.
    is_absolute = os.path.isabs(path) or path.startswith(("\\\\", "//"))
    if not is_absolute or len(path) <= 240:
        return path
    path = path.replace("/", "\\")
    if path.startswith("\\\\"):
        return "\\\\?\\UNC\\" + path[2:]
    return "\\\\?\\" + path

_WIDTH_BY_BITS = {8: 1, 16: 2, 24: 3, 32: 4}

_DTYPE_BY_WIDTH = {1: "<i1", 2: "<i2", 4: "<i4"}

_DEBRUIJN_TABLE = np.zeros(32, dtype=np.int64)
for _bit_i in range(32):
    _DEBRUIJN_TABLE[((1 << _bit_i) * 0x077CB531 & 0xFFFFFFFF) >> 27] = _bit_i


class _Int24Packer:
    """Reusable scratch-buffer packer for little-endian 24-bit PCM words."""

    def __init__(self):
        self._u = None
        self._t0 = None
        self._t1 = None
        self._out = None

    def pack(self, q_i32):
        flat = np.asarray(q_i32, dtype="<i4").ravel()
        n = flat.size
        if n == 0:
            return np.empty(0, dtype=np.uint8)
        n4 = (n // 4) * 4
        if n4 == 0:
            return np.ascontiguousarray(flat.view(np.uint8).reshape(-1, 4)[:, :3]).ravel()
        q4 = n4 // 4
        if self._u is None or self._u.size < n4:
            self._u = np.empty(n4, dtype="<u4")
            self._t0 = np.empty(q4, dtype="<u4")
            self._t1 = np.empty(q4, dtype="<u4")
            self._out = np.empty(q4 * 3, dtype="<u4")
        u = self._u[:n4]
        np.bitwise_and(flat[:n4].view(np.uint32), np.uint32(0xFFFFFF), out=u)
        u2d = u.reshape(q4, 4)
        u0, u1, u2, u3 = u2d[:, 0], u2d[:, 1], u2d[:, 2], u2d[:, 3]
        t0, t1 = self._t0[:q4], self._t1[:q4]
        out3 = self._out[: q4 * 3].reshape(q4, 3)
        np.left_shift(u1, np.uint32(24), out=t0)
        np.bitwise_or(u0, t0, out=out3[:, 0])
        np.right_shift(u1, np.uint32(8), out=t0)
        np.left_shift(u2, np.uint32(16), out=t1)
        np.bitwise_or(t0, t1, out=out3[:, 1])
        np.right_shift(u2, np.uint32(16), out=t0)
        np.left_shift(u3, np.uint32(8), out=t1)
        np.bitwise_or(t0, t1, out=out3[:, 2])
        out_bytes = out3.reshape(-1).view(np.uint8)
        if n4 == n:
            return out_bytes
        tail = np.ascontiguousarray(flat[n4:].view(np.uint8).reshape(-1, 4)[:, :3]).ravel()
        return np.concatenate((out_bytes, tail))


def _pack_int24_le(q_i32, packer=None):
    """Packs signed int32 samples in [-8388608, 8388607] into little-endian
    24-bit PCM bytes (4 samples -> 3 uint32 words)."""
    if packer is not None:
        return packer.pack(q_i32)
    flat = np.asarray(q_i32, dtype="<i4").ravel()
    if flat.size == 0:
        return np.empty(0, dtype=np.uint8)
    u = flat.view(np.uint32) & np.uint32(0xFFFFFF)
    n4 = (u.size // 4) * 4
    if n4 == 0:
        return np.ascontiguousarray(flat.view(np.uint8).reshape(-1, 4)[:, :3]).ravel()
    out = np.empty((n4 // 4) * 3, dtype="<u4")
    u0 = u[0:n4:4]
    u1 = u[1:n4:4]
    u2 = u[2:n4:4]
    u3 = u[3:n4:4]
    out[0::3] = u0 | (u1 << np.uint32(24))
    out[1::3] = (u1 >> np.uint32(8)) | (u2 << np.uint32(16))
    out[2::3] = (u2 >> np.uint32(16)) | (u3 << np.uint32(8))
    out_bytes = out.view(np.uint8)
    if n4 == u.size:
        return out_bytes
    tail = np.ascontiguousarray(flat[n4:].view(np.uint8).reshape(-1, 4)[:, :3]).ravel()
    return np.concatenate((out_bytes, tail))


def _pcm_bytes(block, bits_per_sample, width, q_int=None, packer24=None):
    """Little-endian interleaved PCM of a block, exactly as the format hashes it.

    float64 -> exact integer: the soundfile conversion is m / 2**(bits-1)."""
    scale = float(1 << (bits_per_sample - 1))
    dt = _DTYPE_BY_WIDTH.get(width)
    if dt is not None:
        if q_int is not None and width == 2 and q_int.dtype == np.dtype("<i2") and q_int.flags.c_contiguous:
            return q_int
        if q_int is not None and width == 4:
            return np.ascontiguousarray(q_int.astype(dt, copy=False))
        if width == 4:
            clipped = np.clip(np.rint(block * scale), -2147483648.0, 2147483647.0)
            return np.ascontiguousarray(clipped.astype(dt))
        if q_int is None and width in (1, 2):
            clipped = np.clip(
                np.rint(block * scale),
                -(1 << (bits_per_sample - 1)),
                (1 << (bits_per_sample - 1)) - 1,
            )
            return np.ascontiguousarray(clipped.astype(dt))
        return np.ascontiguousarray(np.rint(block * scale).astype(dt))
    # 24 bits (width 3): store as <i4 and pack the low 3 bytes with SIMD shifts.
    q = (
        q_int
        if q_int is not None
        else np.clip(np.rint(block * scale), -8388608.0, 8388607.0).astype("<i4")
    )
    if width == 3:
        return _pack_int24_le(q, packer=packer24)
    return np.ascontiguousarray(q.view(np.uint8).reshape(-1, 4)[:, :width])

def _md5_pcm(data, bits_per_sample, frames_by_block=1 << 20):
    """MD5 of the decoded PCM of a whole array. None if it is not verifiable."""
    width = _WIDTH_BY_BITS.get(bits_per_sample)
    if width is None:
        return None
    md5 = hashlib.md5()
    packer = _Int24Packer() if width == 3 else None
    for i in range(0, len(data), frames_by_block):
        md5.update(_pcm_bytes(data[i : i + frames_by_block], bits_per_sample, width,
                              packer24=packer))
    return md5.hexdigest()

def _compare_md5(calculated, meta):
    """Compares an already computed MD5 with the one recorded in the STREAMINFO."""
    stored = meta.get("md5")
    if isinstance(stored, (bytes, bytearray)):
        stored = int.from_bytes(stored, "big")
    if not stored:
        return {"estado": Md5Status.ABSENT.value,
                "detalle": "STREAMINFO has no MD5 recorded (encoder built with --no-md5, or altered metadata)"}
    if calculated is None:
        return {"estado": Md5Status.UNVERIFIABLE.value,
                "detalle": f"bit depth {meta.get('bits_per_sample')} bits: not verifiable",
                "almacenado": f"{int(stored):032x}"}
    matches = int(calculated, 16) == int(stored)
    return {
        "estado":     Md5Status.MATCH.value if matches else Md5Status.MISMATCH.value,
        "calculado":  calculated,
        "almacenado": f"{int(stored):032x}",
    }

def verify_md5(data, meta):
    """Compares the recomputed MD5 of an array with the STREAMINFO one. A fact, not a vote."""
    return _compare_md5(_md5_pcm(data, meta.get("bits_per_sample")), meta)

def _format_bit_depth_result(bits, n_active, n_grid16, counts_idx, forensic=None):
    """Formats the LSB utilization metrics and conclusion from accumulated counts."""
    if n_active < 1000:
        output = {"aplica": True, "lsbs": LsbStatus.INDETERMINATE.value,
                  "conclusion": "indeterminate: too little signal to measure LSB utilization"}
        if forensic is not None:
            output["forense_bits"] = forensic
        return output

    fraction = float(n_grid16) / float(n_active)
    if int(counts_idx.sum()):
        counts_zeros = np.empty(32, dtype=np.int64)
        counts_zeros[_DEBRUIJN_TABLE] = counts_idx
        mode = int(counts_zeros.argmax())
        effective = max(1, bits - mode)
    else:
        effective = bits

    pct = f"{fraction * 100:.1f}%"
    if fraction > 0.97:
        effective = min(effective, 16)
        lsbs = LsbStatus.EMPTY.value
        conclusion = (f"16-bit content inside a {bits}-bit container: LSBs are empty "
                      f"({pct} of samples on the 16-bit grid; effective resolution {effective} bits)")
    elif fraction > 0.80:
        lsbs = LsbStatus.PARTIAL.value
        conclusion = (f"Bit-depth upscale with later processing: LSBs partially inactive "
                      f"({pct} on the 16-bit grid; effective resolution ~{effective} bits)")
    else:
        lsbs = LsbStatus.ACTIVE.value
        conclusion = (f"Active LSBs (~{effective} of {bits} effective bits): rules out "
                      f"grid upscaling, though bit activity alone does not prove lossless origin")
    output = {
        "aplica":                   True,
        "fraccion_16bit_grid":      round(fraction * 100, 1),
        "resolucion_efectiva_bits": int(effective),
        "lsbs":                     lsbs,
        "conclusion":               conclusion,
    }
    if forensic is not None:
        output["forense_bits"] = forensic
    return output


class _StreamingBitDepthAccumulator:
    """Streaming O(1)-memory LSB grid and effective-bit-depth accumulator."""

    def __init__(self, bits_per_sample, total_frames):
        self.bits = int(bits_per_sample)
        self._sampled_frames = 0
        self._sampling_step = (
            max(1, int(round(total_frames / MAX_SAMPLES_BITS)))
            if total_frames < (1 << 62)
            else 1
        )
        self._n_active = 0
        self._n_grid16 = 0
        self._counts_idx = np.zeros(32, dtype=np.int64)
        self._error = None
        self._state = AccumulatorState.COLLECTING
        self._finalized = False
        self._cached_result = None

    @property
    def state(self) -> AccumulatorState:
        """Current streaming lifecycle phase."""
        return self._state

    @property
    def _is_finalized(self) -> bool:
        return self._cached_result is not None

    def _finalize(self, output: dict, state: AccumulatorState) -> dict:
        self._state = state
        self._finalized = True
        self._cached_result = dict(output)
        return dict(output)

    def add_block(self, offset, block_f64=None, q_int=None, abs_q=None):
        if self._is_finalized:
            raise RuntimeError("cannot add audio blocks after completion")
        if self.bits <= 16 or self._sampled_frames >= MAX_SAMPLES_BITS or self._error:
            return
        if (offset // FRAMES_BY_BLOCK) % self._sampling_step:
            return
        try:
            if self.bits == 24 and q_int is not None:
                n_frames = q_int.shape[0]
                self._sampled_frames += n_frames
                flat = np.asarray(q_int).ravel()
                abs_flat = np.asarray(abs_q).ravel() if abs_q is not None else np.abs(flat)
                active_mask = abs_flat >= 8389
                if not np.any(active_mask):
                    return
                active = flat if np.all(active_mask) else flat[active_mask]
                self._n_active += int(active.size)
                self._n_grid16 += int(np.count_nonzero(((active + 12) & 0xFF) <= 24))
                u = active.view(np.uint32)
                pow2 = u & (np.bitwise_not(u) + np.uint32(1))
                idx = (pow2 * np.uint32(0x077CB531)) >> np.uint32(27)
                self._counts_idx += np.bincount(idx, minlength=32)
                return

            if block_f64 is None:
                return
            self._sampled_frames += block_f64.shape[0]
            samples = np.ascontiguousarray(block_f64).reshape(-1)
            active_mask = samples[np.abs(samples) > 0.001]
            if active_mask.size == 0:
                return
            self._n_active += int(active_mask.size)
            upscaled = active_mask * 32768.0
            self._n_grid16 += int(np.count_nonzero(np.abs(upscaled - np.rint(upscaled)) < 0.05))
            scale_b = float(1 << (self.bits - 1))
            if self.bits < 32:
                q = np.rint(active_mask * scale_b).astype(np.int32)
            else:
                q = np.clip(np.rint(active_mask * scale_b), -scale_b, scale_b - 1.0).astype(np.int32)
            q_nz = q if np.all(q) else q[q != 0]
            if q_nz.size:
                u = q_nz.view(np.uint32)
                pow2 = u & (np.bitwise_not(u) + np.uint32(1))
                idx = (pow2 * np.uint32(0x077CB531)) >> np.uint32(27)
                self._counts_idx += np.bincount(idx, minlength=32)
        except Exception as e:
            self._error = str(e)
            self._state = AccumulatorState.FAILED

    def result(self):
        if self._cached_result is not None:
            return dict(self._cached_result)
        if self.bits <= 16:
            return self._finalize({"aplica": False}, AccumulatorState.COMPLETED)
        if self._error or self._state is AccumulatorState.FAILED:
            output = {
                "aplica": True,
                "lsbs": LsbStatus.INDETERMINATE.value,
                "error": self._error or "bit-depth accumulator failed",
            }
            return self._finalize(output, AccumulatorState.FAILED)
        output = _format_bit_depth_result(
            self.bits, self._n_active, self._n_grid16, self._counts_idx
        )
        return self._finalize(output, AccumulatorState.COMPLETED)


def analyze_bit_depth(data, meta, include_forensic=True):
    """LSB utilisation of the container (Block 4).

    This analysis CAN ONLY REFUTE: empty low bits, or low bits aligned to the
    16-bit grid, betray a container deeper than its content. It never proves
    authenticity — a lossy transcode stored in 24 bits also fills the LSBs, and a
    renormalisation breaks the alignment. That is why the result is reported as a
    fact and can only penalise in the scoring.

    `forense_bits` (Vector 4) audits the native integers: zero padding and the
    quantisation comb of a 16-bit source scaled into 24 bits. The streaming path
    disables it here and attaches its own whole-file accumulator instead.
    """
    bits = meta["bits_per_sample"]
    if bits <= 16:
        return {"aplica": False}
    forensic = None
    try:
        if include_forensic:
            forensic = _analyze_forensic_bits(data, meta)

        # All channels, not just the left one: a silent channel or a file with
        # misaligned channels biased the previous test.
        samples = np.ascontiguousarray(data).reshape(-1)
        active_mask  = samples[np.abs(samples) > 0.001]
        if active_mask.size < 1000:
            return _format_bit_depth_result(
                bits, active_mask.size, 0, np.zeros(32, dtype=np.int64), forensic=forensic
            )

        # 1) Are the samples on the 16-bit grid? (the classic upscale test)
        upscaled = active_mask * 32768.0
        n_grid16 = int(np.count_nonzero(np.abs(upscaled - np.rint(upscaled)) < 0.05))

        # 2) Effective resolution: declared bits minus the mode of trailing zeros
        #    of the integer samples (16 bits inside 24 -> 8 zeros -> 16 bits).
        scale_b = float(1 << (bits - 1))
        if bits < 32:
            q = np.rint(active_mask * scale_b).astype(np.int32)
        else:
            q = np.clip(np.rint(active_mask * scale_b), -scale_b, scale_b - 1.0).astype(np.int32)
        q_nz = q if np.all(q) else q[q != 0]
        if q_nz.size:
            u = q_nz.view(np.uint32)
            pow2 = u & (np.bitwise_not(u) + np.uint32(1))
            idx = (pow2 * np.uint32(0x077CB531)) >> np.uint32(27)
            counts_idx = np.bincount(idx, minlength=32)
        else:
            counts_idx = np.zeros(32, dtype=np.int64)

        return _format_bit_depth_result(
            bits, int(active_mask.size), n_grid16, counts_idx, forensic=forensic
        )
    except Exception as e:
        output = {"aplica": True, "lsbs": LsbStatus.INDETERMINATE.value, "error": str(e)}
        if forensic is not None:
            output["forense_bits"] = forensic
        return output

def _streaks(en, previous_count=0):
    """Lengths of the runs of True, vectorised and without losing the ones that
    cross the boundary between blocks. Returns (closed_runs, open_trailing_count)."""
    if en.size == 0:
        return [], (int(previous_count) if previous_count else 0)
    if not en.any():
        return ([int(previous_count)] if previous_count else []), 0
    flags = en.astype(np.int8)
    edges = np.diff(np.concatenate(([0], flags, [0])))
    starts = np.flatnonzero(edges == 1)
    lengths = (np.flatnonzero(edges == -1) - starts).tolist()
    if previous_count:
        if starts[0] == 0:
            lengths[0] += int(previous_count)   # continues the run from the previous block
        else:
            lengths.insert(0, int(previous_count))  # previous run ended right at boundary
    if flags[-1]:
        return lengths[:-1], lengths[-1]        # trailing run stays open
    return lengths, 0


def _accumulate_clip_run(
    events: int,
    short_events: int,
    clip_runs: int,
    total_samples: int,
    max_streak: int,
    length: int,
) -> tuple[int, int, int, int, int]:
    """Single-source classifier for a single closed full-scale run."""
    run_len = int(length)
    if run_len <= 0:
        return events, short_events, clip_runs, total_samples, max_streak
    events += 1
    total_samples += run_len
    if run_len <= SHORT_CLIP_RUN_MAX:
        short_events += 1
    else:
        clip_runs += 1
    if run_len > max_streak:
        max_streak = run_len
    return events, short_events, clip_runs, total_samples, max_streak


class _ClippingAccumulator:
    """Bounded O(channels) per-channel clipping streak tracker.

    Tracks consecutive full-scale runs independently on each channel so
    alternating peaks across channels (L, R, L) never merge into false runs,
    while accumulating exact run counts, mean length and maximum streak in O(1)
    auxiliary memory regardless of signal duration.
    """

    def __init__(self, channels=2):
        n_ch = max(1, int(channels))
        self._open = [0] * n_ch
        self._events = 0
        self._short_events = 0
        self._clip_runs = 0
        self._total_samples = 0
        self._max_streak = 0
        self._state = AccumulatorState.COLLECTING

    @property
    def state(self) -> AccumulatorState:
        """Current streaming lifecycle phase."""
        return self._state

    def _require_collecting(self):
        if self._state is not AccumulatorState.COLLECTING:
            raise RuntimeError("cannot add audio blocks after completion")

    def finalize(self):
        """Freezes the accumulator against further block mutations."""
        if self._state is AccumulatorState.COLLECTING:
            self.flush_zeros()
            self._state = AccumulatorState.COMPLETED

    def _ensure_channels(self, n_ch):
        if n_ch > len(self._open):
            self._open.extend([0] * (n_ch - len(self._open)))

    def _close_run_len(self, length):
        (
            self._events,
            self._short_events,
            self._clip_runs,
            self._total_samples,
            self._max_streak,
        ) = _accumulate_clip_run(
            self._events,
            self._short_events,
            self._clip_runs,
            self._total_samples,
            self._max_streak,
            length,
        )

    def _close_channel_open(self, ch):
        prev = self._open[ch]
        if prev:
            self._close_run_len(prev)
            self._open[ch] = 0

    def flush_zeros(self):
        """Closes every currently open channel run (used on sub-threshold blocks)."""
        self._require_collecting()
        for ch, prev in enumerate(self._open):
            if prev:
                self._close_run_len(prev)
                self._open[ch] = 0

    def _record_channel(self, ch, en_1d):
        n = en_1d.shape[0]
        if n == 0:
            return
        if not en_1d.any():
            self._close_channel_open(ch)
            return
        prev = self._open[ch]
        if en_1d.all():
            self._open[ch] = prev + n
            return
        flags = np.asarray(en_1d, dtype=np.int8)
        padded = np.empty(n + 2, dtype=np.int8)
        padded[0] = 0
        padded[-1] = 0
        padded[1:-1] = flags
        diff = np.diff(padded)
        starts = np.flatnonzero(diff == 1)
        lengths = np.flatnonzero(diff == -1) - starts
        if prev:
            if starts[0] == 0:
                lengths[0] += prev
            else:
                self._close_run_len(prev)
        if flags[-1]:
            self._open[ch] = int(lengths[-1])
            closed = lengths[:-1]
        else:
            self._open[ch] = 0
            closed = lengths
        if closed.size:
            self._events += int(closed.size)
            n_short = int(np.count_nonzero(closed <= SHORT_CLIP_RUN_MAX))
            self._short_events += n_short
            self._clip_runs += int(closed.size) - n_short
            self._total_samples += int(closed.sum())
            blk_max = int(closed.max())
            if blk_max > self._max_streak:
                self._max_streak = blk_max

    def add_float_block(self, block, threshold=0.9999):
        self._require_collecting()
        arr = np.asarray(block)
        if arr.size == 0:
            return
        thr = float(threshold)
        if arr.ndim <= 1:
            self._ensure_channels(1)
            if float(np.max(arr)) < thr and float(np.min(arr)) > -thr:
                self._close_channel_open(0)
                return
            self._record_channel(0, (arr >= thr) | (arr <= -thr))
            return
        n_ch = arr.shape[1]
        self._ensure_channels(n_ch)
        for ch in range(n_ch):
            col = arr[:, ch]
            if float(np.max(col)) < thr and float(np.min(col)) > -thr:
                self._close_channel_open(ch)
            else:
                self._record_channel(ch, (col >= thr) | (col <= -thr))

    def add_int16_block(self, block_i16, threshold=32765):
        self._require_collecting()
        if block_i16.size == 0:
            return
        thr = int(threshold)
        if block_i16.ndim <= 1:
            self._ensure_channels(1)
            if int(np.max(block_i16)) < thr and int(np.min(block_i16)) > -thr:
                self._close_channel_open(0)
                return
            self._record_channel(0, (block_i16 >= thr) | (block_i16 <= -thr))
            return
        n_ch = block_i16.shape[1]
        self._ensure_channels(n_ch)
        for ch in range(n_ch):
            col = block_i16[:, ch]
            if int(np.max(col)) < thr and int(np.min(col)) > -thr:
                self._close_channel_open(ch)
            else:
                self._record_channel(ch, (col >= thr) | (col <= -thr))

    def add_int_threshold_block(self, block_int, threshold):
        """Validated integer full-scale block ingestion (signed >= thr or <= -thr)."""
        self._require_collecting()
        arr = np.asarray(block_int)
        if arr.size == 0:
            return
        thr = int(threshold)
        if arr.ndim <= 1:
            self._ensure_channels(1)
            if int(np.max(arr)) < thr and int(np.min(arr)) > -thr:
                self._close_channel_open(0)
                return
            self._record_channel(0, (arr >= thr) | (arr <= -thr))
            return
        n_ch = arr.shape[1]
        self._ensure_channels(n_ch)
        for ch in range(n_ch):
            col = arr[:, ch]
            if int(np.max(col)) < thr and int(np.min(col)) > -thr:
                self._close_channel_open(ch)
            else:
                self._record_channel(ch, (col >= thr) | (col <= -thr))

    def add_abs_int_block(self, abs_q, threshold=8387770):
        self._require_collecting()
        if abs_q.size == 0:
            return
        thr = int(threshold)
        if abs_q.ndim <= 1:
            self._ensure_channels(1)
            if int(np.max(abs_q)) < thr:
                self._close_channel_open(0)
                return
            self._record_channel(0, abs_q >= thr)
            return
        n_ch = abs_q.shape[1]
        self._ensure_channels(n_ch)
        for ch in range(n_ch):
            col = abs_q[:, ch]
            if int(np.max(col)) < thr:
                self._close_channel_open(ch)
            else:
                self._record_channel(ch, col >= thr)

    def snapshot(self):
        """Non-mutating summary (events, short_events, total_samples, max_streak, clip_runs)."""
        events = self._events
        short_events = self._short_events
        total_samples = self._total_samples
        max_streak = self._max_streak
        clip_runs = self._clip_runs
        for open_len in self._open:
            if open_len > 0:
                events, short_events, clip_runs, total_samples, max_streak = (
                    _accumulate_clip_run(
                        events, short_events, clip_runs, total_samples, max_streak, open_len
                    )
                )
        return events, short_events, total_samples, max_streak, clip_runs

    def clip_dict(self):
        _, _, _, _, clip_runs = self.snapshot()
        return {"runs_clip": clip_runs, "hay_clipping": clip_runs > 0}

    def classify(self, ceiling_hz=None, frequency_cutoff=None, nyquist=None,
                 edge_gradient_db_khz=None, has_codec_signature=False):
        return _classify_from_clip_stats(
            self.snapshot(),
            ceiling_hz=ceiling_hz,
            frequency_cutoff=frequency_cutoff,
            nyquist=nyquist,
            edge_gradient_db_khz=edge_gradient_db_khz,
            has_codec_signature=has_codec_signature,
        )


def _classify_from_clip_stats(stats, ceiling_hz=None, frequency_cutoff=None,
                              nyquist=None, edge_gradient_db_khz=None,
                              has_codec_signature=False):
    """Vector 6 classification from (events, short_events, total_samples, max_streak, clip_runs)."""
    events, short_events, total_samples, max_streak, _ = stats
    if not events:
        return {
            "clipping_eventos": 0,
            "clipping_fraccion_cortos": 0.0,
            "clipping_longitud_media": 0.0,
            "clipping_racha_maxima": 0,
            "clipping_reconstruccion_lossy": False,
            "clipping_mastering_limitador": False,
        }
    fraction = short_events / float(events)
    mean = total_samples / float(events)
    cutoff_effective = (frequency_cutoff if frequency_cutoff is not None
                        else ceiling_hz)
    # Lossy reconstruction needs a pattern, not a few isolated events: a vinyl
    # rip with three clicks and a 19 kHz ceiling used to be accused of Gibbs
    # overshoot. At least 10 events, 8 of them short, are required.
    # Also spare isolated 0 dBFS analog transient crests on uncut full-band
    # masters (>= 88% Nyquist, gentle edge < 18 dB/kHz, zero codec signatures).
    gentle_fullband_crests = bool(
        frequency_cutoff is None
        and ceiling_hz is not None
        and nyquist
        and float(ceiling_hz) >= 0.88 * float(nyquist)
        and edge_gradient_db_khz is not None
        and float(edge_gradient_db_khz) < 18.0
        and not has_codec_signature
    )
    lossy = bool(
        events >= 10
        and short_events >= 8
        and fraction > 0.75
        and cutoff_effective is not None
        and cutoff_effective < 20500.0
        and not gentle_fullband_crests
    )
    master = bool(mean >= 4.0 and ceiling_hz is not None and nyquist
                  and ceiling_hz >= 0.97 * nyquist)
    return {
        "clipping_eventos":            events,
        "clipping_fraccion_cortos":    round(fraction, 4),
        "clipping_longitud_media":     round(mean, 2),
        "clipping_racha_maxima":       max_streak,
        "clipping_reconstruccion_lossy": lossy,
        "clipping_mastering_limitador":  master,
    }


def detect_clipping(data):
    """Real clipping = 3+ CONSECUTIVE samples at the top (>= 0.9999) on a single
    channel. A clipping run is counted once; isolated inter-sample peaks are not
    clipping."""
    try:
        arr = np.asarray(data)
        if arr.size == 0:
            return {"runs_clip": 0, "hay_clipping": False}
        n_ch = arr.shape[1] if arr.ndim > 1 else 1
        acc = _ClippingAccumulator(n_ch)
        acc.add_float_block(arr)
        return acc.clip_dict()
    except (ValueError, TypeError) as e:
        print(f"[clipping] {e}")
        return None


def _classify_streaks(run_lengths, ceiling_hz=None, frequency_cutoff=None, nyquist=None):
    """Vector 6 classification from explicit run lengths."""
    lengths = [int(l) for l in run_lengths if int(l) > 0]
    if not lengths:
        return _classify_from_clip_stats((0, 0, 0, 0, 0), ceiling_hz, frequency_cutoff, nyquist)
    events = len(lengths)
    short = sum(1 for l in lengths if l <= 2)
    total_samples = sum(lengths)
    max_streak = max(lengths)
    clip_runs = events - short
    return _classify_from_clip_stats(
        (events, short, total_samples, max_streak, clip_runs),
        ceiling_hz=ceiling_hz,
        frequency_cutoff=frequency_cutoff,
        nyquist=nyquist,
    )


def _classify_clipping_nature(samples_int, max_val_integer,
                              ceiling_hz=None, frequency_cutoff=None,
                              nyquist=None):
    """Vector 6 from native integer samples (array/direct API path).

    `max_val_integer` is the positive full-scale integer (32767 for 16 bits,
    8388607 for 24 bits). Spectral arguments are optional: without them only the
    run statistics are produced, because both classifications need the spectrum."""
    try:
        arr = np.asarray(samples_int)
        if arr.size == 0:
            return _classify_from_clip_stats(
                (0, 0, 0, 0, 0), ceiling_hz, frequency_cutoff, nyquist
            )
        thr = int(max_val_integer)
        n_ch = arr.shape[1] if arr.ndim > 1 else 1
        acc = _ClippingAccumulator(n_ch)
        acc.add_int_threshold_block(arr, thr)
        return acc.classify(ceiling_hz=ceiling_hz,
                            frequency_cutoff=frequency_cutoff,
                            nyquist=nyquist)
    except Exception as e:
        return {"clipping_eventos": 0, "error": str(e)}

def _dr_from_rms(rms_list, peak, n_blocks):
    """Dynamic range formula in O(n) via np.partition, shared by the array path and
    the block path. It accepts a Python list or an ndarray alike."""
    if n_blocks < 2 or peak <= 0:          # at least 6 seconds are needed
        return None
    arr     = np.asarray(rms_list, dtype=np.float64)
    valid = arr[arr > 1e-6]
    if valid.size == 0:
        return None
    k       = max(1, valid.size // 5)
    rms_top = float(np.mean(np.partition(valid, -k)[-k:]))
    return round(20 * np.log10(peak / (rms_top + 1e-30)), 1)

def calculate_dr(data, sr):
    """Estimated DR over an already loaded array. It is not the DR of the
    reference meter: it averages the channels to mono and applies neither its
    calibration nor its gating, so it is not comparable with published values."""
    try:
        mono   = np.mean(data, axis=1) if data.ndim > 1 else np.asarray(data)
        block = sr * 3
        n      = len(mono) // block
        if n < 2:
            return None
        chunks = mono[: n * block].reshape(n, block)
        rms    = np.sqrt(np.einsum("ij,ij->i", chunks, chunks) / block)
        peak   = float(np.max(np.abs(mono)))
        return _dr_from_rms(rms.tolist(), peak, n)
    except (ValueError, TypeError) as e:
        print(f"[dr] {e}")
        return None

class ForensicBitsAccumulator:
    """Vector 4: 24-bit container audit on the native integer samples.

    Detects two different fakes:
      * zero padding (16-in-24): the eight low bits are always zero, so an OR of
        every |sample| has at least 8 trailing zeros;
      * 16 bits scaled into 24 (gain/ReplayGain/DSP): the low bits are no longer
        zero, but the distinct absolute values stay on a sparse grid, the
        quantisation comb left behind by a 16-bit source.

    The comb is measured on unique levels, never on sample counts, so the result
    does not depend on how loud or how dense the programme material is. Four
    boolean bitmaps of the whole file are kept, one bit per absolute integer:

      * low window (-90 to -48 dBFS), where a genuine 24-bit/dithered master
        spreads the levels uniformly;
      * two high-amplitude slices around -18 and -12 dBFS, one 16-bit grid step
        wide: a hyper-compressed master never leaves the loud range, so without
        them the detector would simply have nothing to look at;
      * one global bitmap of every active level, which works at any level and is
        the one that always sees the grid.

    Each bitmap is judged by the distances between consecutive occupied levels:
    a comb leaves steps close to 256 x gain, while genuine dither visits
    neighbouring integers. The tenth percentile of those distances is reported
    as the lattice step.

    A fifth, independent measurement looks for DoP 1.0 (DSD over PCM) framing:
    at 24 bits and 176.4 kHz or above, a DoP stream marks every sample with
    0x05 or 0xFA in its top byte, alternating. It is tracked per channel, so
    interleaved stereo keeps its L/R pairs instead of mixing them."""

    def __init__(self, bits_per_sample, sample_rate=0):
        self.bits = int(bits_per_sample)
        self.sr = int(sample_rate or 0)
        self.max_val = float(1 << (self.bits - 1))
        self.grid_max = float(1 << 23)
        self._or = 0
        self._samples = 0

        # Integer bounds of the low window [-90, -48] dBFS on the 24-bit grid.
        self.low_int = int(np.rint(self.grid_max * (10.0 ** (-90.0 / 20.0))))
        self.high_int = int(np.rint(self.grid_max * (10.0 ** (-48.0 / 20.0))))
        self.low_width = max(1, self.high_int - self.low_int + 1)

        if self.bits in (24, 32):
            self._bitmap_global = np.zeros(int(self.grid_max) + 1, dtype=bool)
            self._bitmap_hi16 = np.zeros((int(self.grid_max) >> 8) + 1, dtype=bool)
            self._storage_tier = BitStorageTier.COMPACT_HI16
            self._bitmap_low = self._bitmap_global[self.low_int : self.high_int + 1]
            self._bitmap_reb1 = self._bitmap_global[SLICE_1_BASE : SLICE_1_BASE + SLICE_WIDTH]
            self._bitmap_reb2 = self._bitmap_global[SLICE_2_BASE : SLICE_2_BASE + SLICE_WIDTH]
        else:
            self._bitmap_low = None
            self._bitmap_reb1 = None
            self._bitmap_reb2 = None
            self._bitmap_global = None
            self._bitmap_hi16 = None
            self._storage_tier = BitStorageTier.INACTIVE

        # Samples that fed each bitmap: they decide whether it is judged at all.
        self._samples_low = 0
        self._samples_reb1 = 0
        self._samples_reb2 = 0
        self._samples_active = 0

        # DoP 1.0 framing counters (0x05 / 0xFA marker bytes).
        self._dop_valid = 0
        self._dop_pares = 0
        self._dop_last = None
        self._state = AccumulatorState.COLLECTING
        self._cached_result = None

    @property
    def storage_tier(self) -> BitStorageTier:
        """Current bitmap storage tier for native 24/32-bit levels."""
        return self._storage_tier

    @property
    def _hi16_only(self) -> bool:
        return self._storage_tier is BitStorageTier.COMPACT_HI16

    def _flush_hi16_bitmap(self):
        """Materialises the compact upper-16-bit bitmap into the full 24-bit map."""
        if self._storage_tier is not BitStorageTier.COMPACT_HI16:
            return
        if self._bitmap_hi16 is not None:
            nz = np.flatnonzero(self._bitmap_hi16)
            if nz.size:
                self._bitmap_global[nz << 8] = True
            self._bitmap_hi16 = None
        self._bitmap_global[0] = False
        self._storage_tier = BitStorageTier.FULL_24BIT

    @property
    def state(self) -> AccumulatorState:
        """Current streaming lifecycle phase."""
        return self._state

    def __repr__(self) -> str:
        return (
            f"ForensicBitsAccumulator(bits={self.bits}, sr={self.sr}, "
            f"state={self._state.value!r}, samples={self._samples})"
        )

    def _follow_dop(self, q):
        """Counts DoP marker transitions in the top byte of consecutive samples.

        The marker lives in the most significant byte of the 24-bit integer, so
        the shift is applied to the SIGNED sample: 0xFA0000 is a negative value
        whose byte is 0xFA, which |q| would report as 0x06. The scan stays 2D,
        one column per channel: ravel() would interleave L and R and destroy
        every marker pair of a stereo stream."""
        vista = q[:, None] if q.ndim == 1 else q
        if vista.shape[0] == 0:
            return
        tops = ((vista >> 16) & 0xFF).astype(np.int16)
        if self._dop_last is not None:
            if self._dop_last.shape[0] == tops.shape[1]:
                tops = np.vstack((self._dop_last[None, :], tops))
            else:
                self._dop_last = None
        if tops.shape[0] >= 2:
            previous = tops[:-1, :]
            following = tops[1:, :]
            valid = np.zeros(previous.shape, dtype=bool)
            for first_marker, second_marker in DOP_MARKER_PAIRS:
                valid |= ((previous == first_marker) & (following == second_marker)) | (
                    (previous == second_marker) & (following == first_marker)
                )
            self._dop_pares += int(valid.size)
            self._dop_valid += int(np.count_nonzero(valid))
        self._dop_last = tops[-1, :].copy()

    def add(self, block, q_int=None, abs_q=None):
        if self._state in (AccumulatorState.COMPLETED, AccumulatorState.FAILED):
            raise RuntimeError("cannot add audio blocks after completion")
        try:
            if self.bits not in (24, 32) or block.shape[0] == 0:
                return
            if q_int is None:
                q = np.rint(np.asarray(block, dtype=np.float64) * self.max_val)
                q = np.clip(q, -self.max_val, self.max_val - 1.0).astype(np.int32)
            else:
                q = q_int
            self._samples += int(q.size)
            if self.bits == 32:
                if (self._or & 1) == 0:
                    self._or |= int(np.bitwise_or.reduce(q.view(np.uint32).ravel()))
                absq = np.minimum(np.abs(q.astype(np.int64)).ravel() >> 8, 8388608).astype(np.int32)
                absq_or = int(np.bitwise_or.reduce(absq)) if self._hi16_only else 0
            else:
                absq = np.asarray(abs_q).ravel() if abs_q is not None else np.abs(q).ravel()
                if self._hi16_only:
                    absq_or = int(np.bitwise_or.reduce(absq))
                    self._or |= absq_or
                elif (self._or & 1) == 0:
                    self._or |= int(np.bitwise_or.reduce(absq))

            if self._hi16_only:
                if (absq_or & 0xFF) == 0:
                    self._bitmap_hi16[absq >> 8] = True
                else:
                    self._flush_hi16_bitmap()
                    self._bitmap_global[absq] = True
                    self._bitmap_global[0] = False
            else:
                self._bitmap_global[absq] = True
                self._bitmap_global[0] = False

            u_abs = absq.view(np.uint32)
            self._samples_low += int(np.count_nonzero(
                (u_abs - np.uint32(self.low_int)) <= np.uint32(self.high_int - self.low_int)))
            if self._samples_reb1 < 2048:
                self._samples_reb1 += int(np.count_nonzero(
                    (u_abs - np.uint32(SLICE_1_BASE)) < np.uint32(SLICE_WIDTH)))
            if self._samples_reb2 < 2048:
                self._samples_reb2 += int(np.count_nonzero(
                    (u_abs - np.uint32(SLICE_2_BASE)) < np.uint32(SLICE_WIDTH)))
            if self._samples_active < 2048:
                self._samples_active += int(np.count_nonzero(absq))

            if self.bits == 24 and self.sr >= 176400:
                self._follow_dop(q)
        except Exception:
            self._state = AccumulatorState.FAILED
            raise

    @staticmethod
    def _evaluate_bitmap(bitmap, offset, n_samples):
        """Evaluates the unique-level lattice inside a boolean bitmap window.

        Only the occupied levels matter, never how many samples hit them, so a
        quiet window with few samples is reported as not evaluated instead of
        being mistaken for a comb. The comb test is `p10 > 16`, the distance
        that separates the 16-bit grid (256 x gain integers) from dither that
        visits neighbouring levels."""
        raw_indices = np.flatnonzero(bitmap)
        n_unique = int(raw_indices.size)
        if n_samples < 2048 or n_unique < 64:
            return {
                "evaluado": False,
                "es_peine": False,
                "p10": 1.0,
                "entropia": None,
                "cajones": n_unique,
                "ocupacion_interior_lsb8": 1.0,
            }
        offset_int = int(offset)
        if offset_int == 0 and bitmap.size >= 65536:
            n256 = (bitmap.size // 256) * 256
            hist_mod = bitmap[:n256].reshape(-1, 256).sum(axis=0, dtype=np.int64)
            if n256 < bitmap.size:
                rem = np.flatnonzero(bitmap[n256:]) + n256
                for idx_rem in rem:
                    hist_mod[idx_rem & 0xFF] += 1
        else:
            levels_unique = raw_indices + offset_int if offset_int else raw_indices
            hist_mod = np.bincount((levels_unique & 0xFF).astype(np.uint8), minlength=256)
        bins = int(np.count_nonzero(hist_mod))
        p = hist_mod[hist_mod > 0] / float(n_unique)
        entropy = float(-np.sum(p * np.log2(p)))
        inner_ratio = float(np.sum(hist_mod[16:240]) / float(n_unique))

        diffs = np.diff(raw_indices)
        np.minimum(diffs, 1025, out=diffs)
        counts = np.bincount(diffs, minlength=1026)[:1025]
        n_local = int(counts.sum())
        if n_local < 32:
            return {
                "evaluado": False,
                "es_peine": False,
                "p10": 1.0,
                "entropia": entropy,
                "cajones": bins,
                "ocupacion_interior_lsb8": inner_ratio,
            }
        cdf = np.cumsum(counts)
        pos10 = (n_local - 1) * 0.10
        lo10, hi10 = int(np.floor(pos10)), int(np.ceil(pos10))
        v_lo10 = int(np.searchsorted(cdf, lo10 + 1))
        v_hi10 = int(np.searchsorted(cdf, hi10 + 1))
        p10 = float(v_lo10 + (v_hi10 - v_lo10) * (pos10 - lo10))

        pos50 = (n_local - 1) * 0.50
        lo50, hi50 = int(np.floor(pos50)), int(np.ceil(pos50))
        v_lo50 = int(np.searchsorted(cdf, lo50 + 1))
        v_hi50 = int(np.searchsorted(cdf, hi50 + 1))
        med = float(v_lo50 + (v_hi50 - v_lo50) * (pos50 - lo50))

        vals = np.arange(1025, dtype=np.float64)
        in_band = int(counts[np.abs(vals - med) <= 2.0].sum())
        frac = float(in_band / n_local)
        is_comb = bool(p10 > 16.0 and frac >= 0.70)
        return {
            "evaluado": True,
            "es_peine": is_comb,
            "p10": p10,
            "entropia": entropy,
            "cajones": bins,
            "ocupacion_interior_lsb8": inner_ratio,
        }

    def result(self):
        if self._state is AccumulatorState.FAILED:
            raise RuntimeError("cannot finalize failed bit-depth accumulator")
        if self._cached_result is not None:
            return dict(self._cached_result)
        if self.bits not in (24, 32) or self._samples == 0:
            self._state = AccumulatorState.COMPLETED
            self._cached_result = {"aplica": False}
            return dict(self._cached_result)

        self._flush_hi16_bitmap()
        value_or = int(self._or)
        if value_or == 0:                      # digital silence: no grid to judge
            zeros = self.bits
        else:
            zeros = (value_or & -value_or).bit_length() - 1
        min_zeros_16 = max(8, self.bits - 16)
        padded = bool(value_or and zeros >= min_zeros_16)

        r_low = self._evaluate_bitmap(self._bitmap_low, self.low_int,
                                      self._samples_low)

        dithered_16_in_24 = False
        inner_rep = 1.0
        if not padded:
            r_reb1 = self._evaluate_bitmap(self._bitmap_reb1, SLICE_1_BASE,
                                           self._samples_reb1)
            r_reb2 = self._evaluate_bitmap(self._bitmap_reb2, SLICE_2_BASE,
                                           self._samples_reb2)
            r_glob = self._evaluate_bitmap(self._bitmap_global, 0, self._samples_active)
            windows = (r_low, r_reb1, r_reb2, r_glob)
            comb = any(r["es_peine"] for r in windows)
            # Representative window: the first one that saw the comb, otherwise
            # the first one that could be measured at all (the low window if
            # nothing could be measured).
            rep = None
            for r in windows:
                if r["es_peine"]:
                    rep = r
                    break
            if rep is None:
                for r in windows:
                    if r["evaluado"]:
                        rep = r
                        break
            if rep is None:
                rep = r_low
            entropy_rep = rep["entropia"]
            ocupados_rep = rep["cajones"]
            inner_rep = r_glob["ocupacion_interior_lsb8"] if r_glob["evaluado"] else rep["ocupacion_interior_lsb8"]
            if comb:
                step_grid = rep["p10"]
            else:
                step_grid = r_glob["p10"] if r_glob["evaluado"] else rep["p10"]
                if r_glob["evaluado"] and r_glob["cajones"] >= 2 and inner_rep < 0.05:
                    dithered_16_in_24 = True
        else:
            comb = False
            entropy_rep = r_low["entropia"]
            ocupados_rep = r_low["cajones"]
            inner_rep = 0.0
            step_grid = 1.0

        dop_pares = int(self._dop_pares)
        dop_ratio = (self._dop_valid / float(dop_pares)) if dop_pares else 0.0
        is_stream_dop = bool(self.bits == 24 and self.sr >= 176400
                            and dop_pares >= 100 and dop_ratio >= 0.90)

        output = {
            "aplica":                     True,
            "es_padded_16_en_24":         padded,
            "trailing_zeros_lsb":         int(zeros),
            "lsb8_entropia_bits":         round(entropy_rep, 3) if entropy_rep is not None else None,
            "lsb8_cajones_ocupados":      ocupados_rep,
            "lsb8_ocupacion_interior":    round(inner_rep, 4),
            "es_16bit_dither_en_24":      dithered_16_in_24,
            "muestras_pasaje_bajo":       int(self._samples_low),
            "paso_reticula_mediano":      round(step_grid, 2),
            "es_24bit_escalado_en_peine": comb,
            "es_flujo_dop":              is_stream_dop,
            "dop_pares_inspeccionados":   dop_pares,
            "dop_ratio":                  round(dop_ratio, 4),
        }
        self._state = AccumulatorState.COMPLETED
        self._cached_result = dict(output)
        return output

def _analyze_forensic_bits(data, meta):
    """Array-path counterpart of ForensicBitsAccumulator (used by the oracle and
    by direct API callers). It walks the same FRAMES_BY_BLOCK blocks."""
    bits = meta.get("bits_per_sample")
    if bits not in (24, 32):
        return {"aplica": False}
    accumulator = ForensicBitsAccumulator(bits,
                                       sample_rate=meta.get("sample_rate", 0))
    for start in range(0, len(data), FRAMES_BY_BLOCK):
        accumulator.add(data[start : start + FRAMES_BY_BLOCK])
    return accumulator.result()
