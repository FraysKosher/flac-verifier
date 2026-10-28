"""Integrity, native-bit audit, clipping and dynamic range.

Extracted from the motor facade in Phase 4. Nothing here imports the spectral
or cache modules: this layer only needs NumPy, soundfile/mutagen and stdlib.
"""
import hashlib
import os

import numpy as np

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

def _pcm_bytes(block, bits_per_sample, width):
    """Little-endian interleaved PCM of a block, exactly as the format hashes it.

    float64 -> exact integer: the soundfile conversion is m / 2**(bits-1)."""
    scale = float(1 << (bits_per_sample - 1))
    dt = _DTYPE_BY_WIDTH.get(width)
    if dt is not None:
        return np.rint(block * scale).astype(dt).tobytes()
    # 24 bits (width 3): store as <i4 and keep the low 3 bytes, which is the same
    # little-endian layout the old <i8 path produced, at half the memory.
    q = np.rint(block * scale).astype("<i4")
    return q.view(np.uint8).reshape(-1, 4)[:, :width].tobytes()

def _md5_pcm(data, bits_per_sample, frames_by_block=1 << 20):
    """MD5 of the decoded PCM of a whole array. None if it is not verifiable."""
    width = _WIDTH_BY_BITS.get(bits_per_sample)
    if width is None:
        return None
    md5 = hashlib.md5()
    for i in range(0, len(data), frames_by_block):
        md5.update(_pcm_bytes(data[i : i + frames_by_block], bits_per_sample, width))
    return md5.hexdigest()

def _compare_md5(calculated, meta):
    """Compares an already computed MD5 with the one recorded in the STREAMINFO."""
    stored = meta.get("md5")
    if isinstance(stored, (bytes, bytearray)):
        stored = int.from_bytes(stored, "big")
    if not stored:
        return {"estado": "absent",
                "detalle": "STREAMINFO has no MD5 recorded (encoder built with --no-md5, or altered metadata)"}
    if calculated is None:
        return {"estado": "unverifiable",
                "detalle": f"bit depth {meta.get('bits_per_sample')} bits: not verifiable",
                "almacenado": f"{int(stored):032x}"}
    matches = int(calculated, 16) == int(stored)
    return {
        "estado":     "match" if matches else "mismatch",
        "calculado":  calculated,
        "almacenado": f"{int(stored):032x}",
    }

def verify_md5(data, meta):
    """Compares the recomputed MD5 of an array with the STREAMINFO one. A fact, not a vote."""
    return _compare_md5(_md5_pcm(data, meta.get("bits_per_sample")), meta)

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
            output = {"aplica": True, "lsbs": "indeterminate",
                      "conclusion": "indeterminate: too little signal to measure LSB utilization"}
            if forensic is not None:
                output["forense_bits"] = forensic
            return output

        # 1) Are the samples on the 16-bit grid? (the classic upscale test)
        upscaled = active_mask * 32768.0
        fraction = float(np.mean(np.abs(upscaled - np.round(upscaled)) < 0.05))

        # 2) Effective resolution: declared bits minus the mode of trailing zeros
        #    of the integer samples (16 bits inside 24 -> 8 zeros -> 16 bits).
        q = np.round(active_mask * float(1 << (bits - 1))).astype(np.int64)
        q = q[q != 0]
        if q.size:
            pow2     = np.abs(q) & -np.abs(q)                  # 2^(trailing zeros)
            zeros   = np.log2(pow2).round().astype(np.int64)
            mode    = int(np.bincount(zeros).argmax())
            effective = max(1, bits - mode)
        else:
            effective = bits

        pct = f"{fraction * 100:.1f}%"
        if fraction > 0.97:
            lsbs = "empty"
            conclusion = (f"the LSBs are empty ({pct} of samples on the 16-bit grid): "
                          f"effective resolution of {effective} bits in a {bits}-bit container "
                          f"— bit-depth upscale")
        elif fraction > 0.80:
            lsbs = "partial"
            conclusion = (f"LSBs partially inactive ({pct} on the 16-bit grid), "
                          f"effective resolution ~{effective} bits — upscale with later processing")
        else:
            lsbs = "active"
            conclusion = (f"LSBs active, effective resolution ~{effective} of {bits} bits: "
                          f"rules out grid upscaling, but does NOT prove the origin")
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
    except Exception as e:
        output = {"aplica": True, "lsbs": "indeterminate", "error": str(e)}
        if forensic is not None:
            output["forense_bits"] = forensic
        return output

def _streaks(en, previous_count=0):
    """Lengths of the runs of True, vectorised and without losing the ones that
    cross the boundary between blocks. Returns (rachas_cerradas, cuenta_abierta_final)."""
    if not en.any():
        return ([previous_count] if previous_count else []), 0
    flags   = en.astype(np.int8)
    edges  = np.diff(np.concatenate(([0], flags, [0])))
    starts = np.flatnonzero(edges == 1)
    lengths  = (np.flatnonzero(edges == -1) - starts).tolist()
    if starts[0] == 0 and previous_count:
        lengths[0] += previous_count          # continues the run of the previous block
    if flags[-1]:
        return lengths[:-1], lengths[-1]      # the last one is still open
    return lengths, 0

def detect_clipping(data):
    """Real clipping = 3+ CONSECUTIVE samples at the top (>= 0.9999). A clipping
    run is counted once; isolated inter-sample peaks are not clipping."""
    try:
        amp     = np.max(np.abs(data), axis=1) if data.ndim > 1 else np.abs(data)
        streaks, opened = _streaks(amp >= 0.9999)
        if opened:
            streaks.append(opened)
        runs = sum(1 for long in streaks if long >= 3)
        return {"runs_clip": runs, "hay_clipping": runs > 0}
    except (ValueError, TypeError) as e:
        print(f"[clipping] {e}")
        return None

def _classify_streaks(run_lengths, ceiling_hz=None, frequency_cutoff=None, nyquist=None):
    """Vector 6 classification from the lengths of the saturation runs.

    * Lossy reconstruction (Gibbs overshoot): most events are 1-2 isolated
      samples and the spectral cut-off sits below 20.5 kHz.
    * Mastering limiter: runs of 4+ consecutive samples on average while the
      spectrum still reaches Nyquist. It is an observation, never a defect."""
    lengths = [int(l) for l in run_lengths if int(l) > 0]
    events = len(lengths)
    if not events:
        return {
            "clipping_eventos": 0,
            "clipping_fraccion_cortos": 0.0,
            "clipping_longitud_media": 0.0,
            "clipping_racha_maxima": 0,
            "clipping_reconstruccion_lossy": False,
            "clipping_mastering_limitador": False,
        }
    short = sum(1 for long in lengths if long <= 2)
    fraction = short / float(events)
    mean = sum(lengths) / float(events)
    cutoff_effective = (frequency_cutoff if frequency_cutoff is not None
                      else ceiling_hz)
    # Lossy reconstruction needs a pattern, not a few isolated events: a vinyl
    # rip with three clicks and a 19 kHz ceiling used to be accused of Gibbs
    # overshoot. At least 10 events, 8 of them short, are required.
    lossy = bool(
        events >= 10
        and short >= 8
        and fraction > 0.75
        and cutoff_effective is not None
        and cutoff_effective < 20500.0
    )
    master = bool(mean >= 4.0 and ceiling_hz is not None and nyquist
                  and ceiling_hz >= 0.97 * nyquist)
    return {
        "clipping_eventos":            events,
        "clipping_fraccion_cortos":    round(fraction, 4),
        "clipping_longitud_media":     round(mean, 2),
        "clipping_racha_maxima":       max(lengths),
        "clipping_reconstruccion_lossy": lossy,
        "clipping_mastering_limitador":  master,
    }

def _classify_clipping_nature(samples_int, max_val_integer,
                                    ceiling_hz=None, frequency_cutoff=None,
                                    nyquist=None):
    """Vector 6 from native integer samples (array/direct API path).

    `max_val_integer` is the positive full-scale integer (32767 for 16 bits,
    8388607 for 24 bits). Spectral arguments are optional: without them only the
    run statistics are produced, because both classifications need the spectrum."""
    try:
        samples = np.asarray(samples_int).reshape(-1)
        en_clip = np.abs(samples) >= int(max_val_integer)
        streaks, opened = _streaks(en_clip)
        if opened:
            streaks.append(opened)
        return _classify_streaks(streaks, ceiling_hz, frequency_cutoff, nyquist)
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
        self._or = 0
        self._samples = 0

        # Integer bounds of the low window [-90, -48] dBFS.
        self.low_int = int(np.rint(self.max_val * (10.0 ** (-90.0 / 20.0))))
        self.high_int = int(np.rint(self.max_val * (10.0 ** (-48.0 / 20.0))))
        self.low_width = max(1, self.high_int - self.low_int + 1)

        if self.bits == 24:
            self._bitmap_low = np.zeros(self.low_width, dtype=bool)
            self._bitmap_reb1 = np.zeros(SLICE_WIDTH, dtype=bool)
            self._bitmap_reb2 = np.zeros(SLICE_WIDTH, dtype=bool)
            self._bitmap_global = np.zeros(int(self.max_val) + 1, dtype=bool)
        else:
            self._bitmap_low = None
            self._bitmap_reb1 = None
            self._bitmap_reb2 = None
            self._bitmap_global = None

        # Samples that fed each bitmap: they decide whether it is judged at all.
        self._samples_low = 0
        self._samples_reb1 = 0
        self._samples_reb2 = 0
        self._samples_active = 0

        # DoP 1.0 framing counters (0x05 / 0xFA marker bytes).
        self._dop_valid = 0
        self._dop_pares = 0
        self._dop_last = None

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
        if self._dop_last is not None and self._dop_last.shape[0] == tops.shape[1]:
            tops = np.vstack((self._dop_last[None, :], tops))
        if tops.shape[0] >= 2:
            previous = tops[:-1, :]
            next = tops[1:, :]
            valid = (((previous == 0x05) & (next == 0xFA))
                       | ((previous == 0xFA) & (next == 0x05))
                       | ((previous == 0x06) & (next == 0xF9))
                       | ((previous == 0xF9) & (next == 0x06)))
            self._dop_pares += int(valid.size)
            self._dop_valid += int(np.count_nonzero(valid))
        self._dop_last = tops[-1, :].copy()

    def add(self, block):
        if self.bits != 24 or block.shape[0] == 0:
            return
        q = np.rint(np.asarray(block, dtype=np.float64) * self.max_val)
        q = np.clip(q, -self.max_val, self.max_val - 1.0).astype(np.int64)
        self._samples += int(q.size)
        absq = np.abs(q).ravel()
        self._or |= int(np.bitwise_or.reduce(absq))

        mask_b = (absq >= self.low_int) & (absq <= self.high_int)
        if np.any(mask_b):
            self._bitmap_low[absq[mask_b] - self.low_int] = True
            self._samples_low += int(np.count_nonzero(mask_b))

        mask_1 = (absq >= SLICE_1_BASE) & (absq < SLICE_1_BASE + SLICE_WIDTH)
        if np.any(mask_1):
            self._bitmap_reb1[absq[mask_1] - SLICE_1_BASE] = True
            self._samples_reb1 += int(np.count_nonzero(mask_1))

        mask_2 = (absq >= SLICE_2_BASE) & (absq < SLICE_2_BASE + SLICE_WIDTH)
        if np.any(mask_2):
            self._bitmap_reb2[absq[mask_2] - SLICE_2_BASE] = True
            self._samples_reb2 += int(np.count_nonzero(mask_2))

        active = absq[absq > 0]
        if active.size:
            self._bitmap_global[active] = True
            self._samples_active += int(active.size)

        if self.bits == 24 and self.sr >= 176400:
            self._follow_dop(q)

    @staticmethod
    def _evaluate_bitmap(bitmap, offset, n_samples):
        """Evaluates the unique-level lattice inside a boolean bitmap window.

        Only the occupied levels matter, never how many samples hit them, so a
        quiet window with few samples is reported as not evaluated instead of
        being mistaken for a comb. The comb test is `p10 > 16`, the distance
        that separates the 16-bit grid (256 x gain integers) from dither that
        visits neighbouring levels."""
        levels_unique = np.flatnonzero(bitmap) + int(offset)
        n_unique = int(levels_unique.size)
        if n_samples < 2048 or n_unique < 64:
            return {
                "evaluado": False,
                "es_peine": False,
                "p10": 1.0,
                "entropia": None,
                "cajones": n_unique,
            }
        hist_mod = np.bincount((levels_unique & 0xFF).astype(np.uint8), minlength=256)
        bins = int(np.count_nonzero(hist_mod))
        p = hist_mod[hist_mod > 0] / float(n_unique)
        entropy = float(-np.sum(p * np.log2(p)))

        diffs = np.diff(levels_unique)
        diffs_locales = diffs[diffs <= 1024]
        if diffs_locales.size < 32:
            return {
                "evaluado": False,
                "es_peine": False,
                "p10": 1.0,
                "entropia": entropy,
                "cajones": bins,
            }
        p10 = float(np.percentile(diffs_locales, 10))
        med = float(np.median(diffs_locales))
        frac = float(np.mean(np.abs(diffs_locales - med) <= 2.0))
        is_comb = bool(p10 > 16.0 and frac >= 0.70)
        return {
            "evaluado": True,
            "es_peine": is_comb,
            "p10": p10,
            "entropia": entropy,
            "cajones": bins,
        }

    def result(self):
        if self.bits != 24 or self._samples == 0:
            return {"aplica": False}

        value_or = int(self._or)
        if value_or == 0:                      # digital silence: no grid to judge
            zeros = self.bits
        else:
            zeros = (value_or & -value_or).bit_length() - 1
        padded = bool(value_or and zeros >= 8)

        r_low = self._evaluate_bitmap(self._bitmap_low, self.low_int,
                                      self._samples_low)
        r_reb1 = self._evaluate_bitmap(self._bitmap_reb1, SLICE_1_BASE,
                                      self._samples_reb1)
        r_reb2 = self._evaluate_bitmap(self._bitmap_reb2, SLICE_2_BASE,
                                      self._samples_reb2)
        r_glob = self._evaluate_bitmap(self._bitmap_global, 0, self._samples_active)
        windows = (r_low, r_reb1, r_reb2, r_glob)

        if not padded:
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
            if comb:
                step_grid = rep["p10"]
            else:
                step_grid = r_glob["p10"] if r_glob["evaluado"] else rep["p10"]
        else:
            comb = False
            entropy_rep = r_low["entropia"]
            ocupados_rep = r_low["cajones"]
            step_grid = 1.0

        dop_pares = int(self._dop_pares)
        dop_ratio = (self._dop_valid / float(dop_pares)) if dop_pares else 0.0
        is_stream_dop = bool(self.bits == 24 and self.sr >= 176400
                            and dop_pares >= 100 and dop_ratio >= 0.90)

        return {
            "aplica":                     True,
            "es_padded_16_en_24":         padded,
            "trailing_zeros_lsb":         int(zeros),
            "lsb8_entropia_bits":         round(entropy_rep, 3) if entropy_rep is not None else None,
            "lsb8_cajones_ocupados":      ocupados_rep,
            "muestras_pasaje_bajo":       int(self._samples_low),
            "paso_reticula_mediano":      round(step_grid, 2),
            "es_24bit_escalado_en_peine": comb,
            "es_flujo_dop":              is_stream_dop,
            "dop_pares_inspeccionados":   dop_pares,
            "dop_ratio":                  round(dop_ratio, 4),
        }

def _analyze_forensic_bits(data, meta):
    """Array-path counterpart of ForensicBitsAccumulator (used by the oracle and
    by direct API callers). It walks the same FRAMES_BY_BLOCK blocks."""
    bits = meta.get("bits_per_sample")
    if bits != 24:
        return {"aplica": False}
    accumulator = ForensicBitsAccumulator(bits,
                                       sample_rate=meta.get("sample_rate", 0))
    for start in range(0, len(data), FRAMES_BY_BLOCK):
        accumulator.add(data[start : start + FRAMES_BY_BLOCK])
    return accumulator.result()
