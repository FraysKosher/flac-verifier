# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 FLAC VERIFIER Contributors
from enum import Enum
import hashlib
import heapq
import json
import math
import os
import re
import sys
import threading
import time
from typing import NamedTuple
import weakref

# ─── BLAS THREADS (before numpy/scipy are imported) ──────────────────────────
# The album is analysed by a pool of processes; if every process opened its own
# BLAS pool, the machine would run dozens of threads competing for the same
# cores (thread oversubscription) and the analysis would get slower, not faster.
# Each worker is pinned to one BLAS thread; parallelism comes from the pool.
for _variable_blas in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS",
                       "MKL_NUM_THREADS", "VECLIB_MAXIMUM_THREADS",
                       "NUMEXPR_NUM_THREADS"):
    os.environ.setdefault(_variable_blas, "1")
del _variable_blas

import numpy as np
import soundfile as sf
from mutagen.flac import FLAC, FLACNoHeaderError

try:
    from PIL import Image, ImageDraw, ImageFont
    PIL_OK = True
except ImportError:                    # the report and the PNGs are unavailable
    Image = ImageDraw = ImageFont = None
    PIL_OK = False

# ─── FACADE ──────────────────────────────────────────────────────────────────
# The implementation lives in four private modules; everything the tests, the
# CLI, the GUI and the report import from `motor_flac` is re-exported here.
# The module objects stay reachable as attributes (motor._motor_espectral...):
# the existing fault-injection tests patch `motor.Image`, and the renderer
# callback below is what makes that patch effective.
import _motor_bits
import _motor_cache
import _motor_espectral
import _motor_veredicto
from _motor_espectral import Image

from _motor_bits import (
    AccumulatorState, Md5Status, LsbStatus, BitStorageTier, DOP_MARKER_PAIRS,
    IO_RETRY_DELAYS, _is_transient_io_error, retry_io,
    FRAMES_BY_BLOCK, MAX_SAMPLES_BITS, _WIDTH_BY_BITS, _DTYPE_BY_WIDTH,
    _normalize_path_fs, _Int24Packer, _pack_int24_le, _pcm_bytes, _md5_pcm, _compare_md5, verify_md5,
    _StreamingBitDepthAccumulator, _ClippingAccumulator,
    analyze_bit_depth, _streaks, detect_clipping, _classify_streaks,
    _classify_clipping_nature, _dr_from_rms, calculate_dr,
    ForensicBitsAccumulator, _analyze_forensic_bits,
)
from _motor_espectral import (
    CutoffClass, CodecLatticeFamily, CutoffAssessment, PsychoacousticSummary,
    FftBackendKind, DsdProfile, HiresMorphology, HiresAssessment,
    WIDTH_PNG, HEIGHT_PNG, PNG_SPECTROGRAM_FPS, SPECTRO_SECONDS,
    MAX_FRAMES_PNG, ACTIVE_RMS_THRESHOLD_DB, _LUT_SPECTROGRAM, _mean_safe, _power_db,
    _spectrum_dbfs, _white_noise_gain, _stft_complex,
    _power_from_fft, _needed_active_blocks, _activity_score,
    _analyze_mid_side_asymmetry, _assess_hires_authenticity,
    _diagnose_hires_authenticity,
    _spectral_metrics, analyze_spectrum, render_spectrogram_fast,
    PsychoacousticSTFTAccumulator, _SpectralAccumulator,
    _detect_lapped_codec_grid, _detect_sbr_qmf_replication,
    _get_kbd_2048, _get_sine_2048, _get_vorbis_2048, _get_pqmf_fast,
    _robust_z_peak, _select_continuous_subwindow, _select_hf_subwindow_candidates,
    _save_spectrogram as _spectrogram_save,
)
from _motor_veredicto import (
    LIMIT_CD_HZ, ALBUM_COHERENCE_NOTE, Verdict, EvidenceTier, EvidenceFamily,
    FindingCode, VerdictCap, AlbumCoherenceAction, AlbumQuorumKind,
    AlbumQuorumDecision, ScoreEvaluation, _ScoreLedger, calculate_score,
    _deserves_album_coherence, _deserves_album_lossy_coherence,
    _is_clean_quorum_track, _evaluate_album_quorum,
    apply_album_coherence, forensic_evidence,
)
from _motor_cache import (
    PNG_FOLDER_NAME, PNG_MAGIC, CACHE_ENV_VAR, cache_root, _album_bucket_hash,
    CacheLookupStatus, SpectralMode, _png_folder, _png_path, _cache_path, _identity,
    _analysis_key, _replace_with_retries, _sanitize,
    read_cache_entry as _cache_read_entry,
    read_cache as _cache_read, write_cache as _cache_write,
)

# ─── BASIC CHECKS ─────────────────────────────────────────────────────────────

# Raised whenever any analysis calculation changes: it invalidates the caches.
# 8.1: the high band of a hi-res file is judged against the CD limit and not
#      against a proportion of Nyquist (this fixed a false "PROBABLE UPSCALE"
#      on 96/24 masters with an analogue roll-off).
ENGINE_VERSION = "14.8"
# 14.8: Recognise clean digital solo-instrument masters (e.g. modern ECM solo
#       piano at 7.5-10.5 kHz with smooth pre/post-ceiling decay and silent or
#       noise-shaped digital floor), add a dedicated 6.5-12.5 kHz low-band
#       brickwall detector (lowering edge-gradient clamp to 5.5 kHz) so
#       16/22.05 -> 44.1 kHz upscales and low-bitrate cutoffs remain HARD_PROOF
#       `far_cutoff`, downgrade isolated `LOW_SPECTRAL_CEILING` to SOFT_BLOCKING
#       (`SUSPICIOUS`), emit `FindingCode.ALBUM_LOSSY_CONSISTENCY`, and expose
#       `psycho_detector_error` on psychoacoustic runtime failures.
# 14.7: Recognise dark ambient / analog tape masters whose -60 dB musical contour
#       crosses early (7.5-10.5 kHz) while calibrated dBFS high-frequency tape
#       hiss tapers smoothly into Nyquist, probe upper-band lapped-codec grids on
#       7.0-17.5 kHz low-ceiling tracks so high-rate transcodes cannot hide below
#       the ceiling, and spare isolated 0 dBFS analog transient crests on uncut
#       full-band gentle-rolloff masters from false Gibbs overshoot flags.
# 14.6: Handle >=3-channel FLACs without discarding Centre/Surround channels,
#       add anti-phase (L = -R) fallback and a zero-energy spectral guard, track
#       clipping runs per channel in bounded O(channels) memory, scope PNG
#       spectrogram filenames by mode with atomic writes and PNG magic checks,
#       harden malformed cache JSON / stale paths / mid-stream 32-bit fallback,
#       and short-circuit spectral/PNG work on digital silence or MD5 mismatch.
# 14.5: Exempt genuine high-headroom 24-bit <= 48 kHz studio masters from Rule 5
#       ("not backed by the content") when smooth acoustic/band-limited decay is
#       backed by clean active 24-bit LSBs, zero lossy signatures, and a
#       continuous sub-ceiling converter roll-off into the Nyquist stopband
#       (`native_24bit_hf_tail`), preventing false flags on dynamic 24/48 indie/
#       acoustic recordings while still catching 44.1 -> 48 kHz upscales and
#       dithered lossy repackages.
# 14.4: Cap effective resolution at 16 bits when >97% of active samples sit on
#       the 16-bit grid (`es_16bit_dither_en_24`), consolidate boundary
#       micro-dither wording, avoid contradictory 48 kHz `far_cutoff` 20 kHz
#       notes and convicted-track stationarity notes, and close parallel worker
#       generators deterministically before album coherence.
# 14.3: Attribute 20.00 kHz brickwall shelves generically as codec low-pass
#       shelves whenever a non-Opus filterbank/SBR/MP3 signature (e.g. PQMF-576
#       on LAME 320 kbps) is already identified, avoiding contradictory Opus
#       labels while preserving scores and verdicts.
# 14.2: Split neutral/positive observations (`notes`) from actionable defects
#       (`problemas`), rewrite findings plain-English-first with physical metrics
#       and zero internal Vector jargon, and default spectral scans to `full`.
# 14.1: Hot 0 dBFS clipped 44.1->96 kHz polyphase skirt upscale detector,
#       16-bit TPDF CELT-960 + flat-stopband 20.00 kHz libopus/WMA shelf
#       corroboration, static LPF dither-floor clamp guard for mod20k,
#       narrow-midrange stereo & post-ceiling smooth acoustic decay guards,
#       24/44.1 active-LSB acoustic/ADC exemption with --no-md5 coherence, and
#       mixed VHS/archival album coherence for zero-codec albums.
# 14.0: Steep continuous 24/96 studio roll-off guard (24.5-28 kHz vs 34-42 kHz
#       ultrasonic decay >= 7.5 dB), genuine Hi-Res ultrasonic stereo & low-LPF
#       M/S + IQR guards, and confirmed lapped-codec batch quorum (>= 75%) for
#       dark/low-ceiling intros in 44.1/48 kHz Opus/MDCT folders.
# 13.9: L2-tiled zero-allocation stereo STFT with exact Parseval Mid/Side power,
#       direct pypocketfft C++ bindings, vectorized MDCT subband matrix, and
#       fused 24-bit integer MD5/abs/clipping streaming passes.
# 13.8: Streaming native int16/int24 decode without full-block float64 copies,
#       O(1)-RAM streaming bit-depth accumulator, PQMF-576 36x18 MDCT basis,
#       and pure-NumPy 160/147 polyphase resampler removing scipy.signal startup.
# 13.7: Phase 13.7 quiet 24/96 studio roll-off vs ultrasonic stopband and
#       centered upper-mid vocal M/S guard
# 13.6: Phase 13.6 polyphase transition-skirt and ultrasonic stopband brickwall
#       calibration for hot 0 dBFS 44.1/48 kHz -> Hi-Res upscales:
#       - Added `skirt_44k_brickwall` (19.8-26.2 kHz skirt slope < -20.0 dB/kHz,
#         < 2.5 * slope_base) and `skirt_48k_brickwall` (22.5-28.5 kHz skirt
#         slope < -20.0 dB/kHz, < 2.5 * slope_base) paired with a > 40.0 dB drop
#         to a 28.5-38.5 kHz stopband below -90.0 dBFS in `_diagnose_hires_authenticity`.
#       - Computed reported `gradiente_nyquist_db_khz` / `salto_ultrasonico_db`
#         after the DSD guard while keeping raw passband-transition metrics
#         unmodified for DSD and analogue roll-off rules.
# 13.5: Phase 13.5 1970s Hi-Res analogue roll-off calibration, 48 kHz PQMF-576 subband
#       alignment, extreme cut-off jitter cap, and complementary 75% album coherence:
#       - Added multi-pole 1970s analogue tape/console decay path (`14.5 <= jump_db < 22.5`,
#         `worse_slope > -12.0`, `level_after_db > -122.0`, `fall_14_17k_db > 10.0`,
#         `level_14_17k - level_before_db >= 1.5`) in `_diagnose_hires_authenticity`.
#       - Added physical-frequency aligned 8-band `PQMF-576` fallback for 48 kHz /
#         reduced-ceiling fullband tracks (`14.25-20.25 kHz` at 48 kHz, skipping redundant
#         Musepack re-evaluation), active-subband stopband slicing when 4..7 subbands are
#         active, and lowered `_SpectralAccumulator` opposite-channel / Mid fallback
#         trigger to `mdct_lattice_snr >= 3.2`.
#       - Added soft-blocking extreme cut-off jitter tier (`iqr_corte_hz > 1800 Hz`, -0.30,
#         `blocks_max_verdict=True`) guarded by stationarity, analogue roll-off and DSD.
#       - Lowered `apply_album_coherence` quorum to 75% with complementary `N // 4` candidate
#         cap, required `lsbs == "active"` for 24-bit Hi-Res rescue, and allowed Hi-Res
#         Rule 1/2 strings (`"below the cd band"`, `"not backed by content"`) at >= 88.2 kHz
#         while keeping Rule 5 `"not backed by the content"` blocked at <= 48 kHz.
# 13.4: Phase 13.4 high-frequency multi-subwindow Opus detection, dynamic M/S guard
#       and album coherence calibration:
#       - Added `_select_hf_subwindow_candidates` (75th-percentile second-order
#         difference energy ranking over up to 3 non-overlapping windows) for
#         `CELT-960 (Opus)` so sparse-cymbal 44.1 kHz transcodes are caught even when
#         low/midrange energy dominates the block center, plus opposite-channel lattice
#         fallback in `_SpectralAccumulator.result()` when primary SNR >= 4.5.
#       - Calibrated `_ms_dynamic_collapse` fusion in `_SpectralAccumulator.result()`
#         to require `delta_corte_ms_hz >= 800.0` or (`caida_side_agudos_db >= 6.0`
#         and `_ms_p95_db >= 25.0`), sparing genuine wide-synth/centered-drum mixes.
#       - Lowered `_is_clean_quorum_track` clean full-band floor to `score >= 0.60`
#         and allowed `near_nyquist_cutoff` tracks (`iqr_corte_hz <= 550.0`,
#         `corte_artificial == False`) into `_deserves_album_coherence`.
# 13.3: no calculation changed. The frozen executable of 13.2 could not import
#       scipy.signal (missing importlib.resources), so its lapped-codec lattice
#       detector silently returned "nothing found" and it cached those results
#       under 13.2; raising the version invalidates them.
# 13.2: Phase 13.2 filterbank lattice, soft heuristic and album quorum calibration:
#       - Added MPEG-2/4 AAC Sine window (`_get_sine_2048`), unbiased `el >= er`
#         channel selection, opposite-channel fallback on asymmetric stereo, and
#         energy-guided continuous subwindow selection (`_select_continuous_subwindow`).
#       - Centered PQMF-576 / PQMF-32 512-tap Kaiser prototype around tap 256 and
#         disambiguated MP3 vs Musepack 36-sample / 18-sample periodicity.
#       - Replaced whole-buffer FFT resampling at 44.1 kHz in CELT-960 with a
#         Kaiser-windowed polyphase resampler (`resample_poly(160, 147)`).
#       - Restricted SBR-QMF analysis to single continuous blocks with per-frame
#         common-mode subtraction and vectorized lag sweep.
#       - Added scale-floored `_robust_z_peak`, refined static Joint-Stereo and
#         20 kHz modulation guards, reclassified `sospecha_excitador_armonico` to
#         `SPECTRAL_ENVELOPE`, allowed >= 2 occupied modulo-256 LSB8 bins in global
#         micro-dither detection, and expanded album coherence quorum to include clean
#         `PROBABLY LOSSLESS` tracks (`score >= 0.70`) with scalable candidate limit
#         `max(2, N // 6)`.
# 13.1: Phase 13.1 surgical calibration:
#       - Fixed 24-in-32 container zero-padding threshold (`min_zeros_16 =
#         max(8, bits - 16)`) so genuine 24-bit audio inside a 32-bit FLAC
#         container (8 trailing zeros) is never mistaken for 16-bit padding.
#       - Calibrated `modulacion_psicoacustica_20k`, `_ms_dynamic_collapse`
#         and `es_estacionario` so real commercial tracks with centered snare/
#         vocal transients or rhythmic synth overtones are not capped at
#         SUSPICIOUS (50 %) by false soft triggers.
#       - Bounded `--mode full` active STFT budget to `SPECTRO_SECONDS + 15`
#         (60 s across 100 % of the track timeline) to preserve full-track
#         rigor without quadrupling CPU time.
# 13.0: Phase 13 forensic precision & architecture hardening:
#       - Multi-family corroboration gate (`familias_evidencia_lossy` &
#         `corroboracion_multifamilia`): condemning a track as PROBABLE UPSCALE
#         requires either >= 2 independent evidence families or a hard
#         arithmetic/structural proof (filterbank lattice, spectral cut-off, or
#         container bit-depth padding/comb); a single uncorroborated soft
#         spectral heuristic is capped at SUSPICIOUS.
#       - Spectral stationarity guard (`es_estacionario`) protecting sustained
#         electronic synth drones and harmonic pads from false subband-hole or
#         modulation triggers between discrete overtones.
#       - MPEG-4 AAC Kaiser-Bessel Derived (KBD, alpha=4) window in MDCT-1024
#         plus stereo channel (L/R/Mid) and 2nd-block lapped transform selection.
#       - Active `--mode` (center / full / seconds) and `--seconds N` spectral
#         windowing, plus native 32-bit integer FLAC (PCM_32) streaming and
#         bit-depth audit.
# 12.0: Phase 12 forensic closure: seamless QMF subband replication detector
#       for HE-AAC v1/v2 SBR without a boundary notch (CD and Hi-Res rates),
#       PQMF-32 pure subband 36-sample frame lattice for Musepack SV7/SV8, and
#       genuine band-limited / vintage / analogue roll-off protection paired
#       with adaptive passband + ATH-tail lapped transform inversion.
# 11.0: Phase 11 orthogonal lapped transform inversion: sample-aligned
#       quantization lattice detection for MDCT-1024 (Vorbis / AAC with
#       128-sample block-switching subgrids), PQMF-576 (MP3 32-band polyphase
#       filterbank + 18-point MDCT), and CELT-960 (Opus 20 ms low-overlap MDCT
#       stopband TDAC boundary cancellation with 1 - 0.85*z^-1 pre-emphasis).
# 10.0: Phase 9/10 forensic suite: 24-bit LSB8 interior occupancy audit
#       (catches 16-bit + boundary micro-dither in 24-bit), 24.0 kHz DAT/broadcast
#       Nyquist brickwall detector for 96/192 kHz containers, 22.05 kHz upper cap
#       on instantaneous cut-off jitter (prevents false jitter penalties on
#       genuine 24/96 and 24/192 ultrasonic masters), multi-scale spectral step
#       detector, frame-level dynamic Mid/Side stereo collapse, and upper-band
#       psychoacoustic modulation (>20 kHz) for high-bitrate lossy codecs.
# 9.8: Phase 8 detectors (24-bit lattice, DSD/Scarlet/DSD128, sfb21 islands, exciter, acoustic centroid).
#      Album coherence now also covers the Phase 8D acoustic exemption: a
#      low-centroid acoustic track that already reached PROBABLY LOSSLESS on its
#      own (smooth decay + clean bits, no lossy signature) is still held back by
#      `analyze_files` and, inside a directory that is >= 80% GENUINE LOSSLESS,
#      `apply_album_coherence` marks it with `coherencia_album = True` and
#      ALBUM_COHERENCE_NOTE. The elevation lives only in the batch result: the
#      individual on-disk cache keeps the track's own verdict.
# 9.7: cache invalidation for the final Phase 7 detectors.
# 9.6: Phase 5 forensic suite and validation benchmark (mirror threshold and
#      Opus scoring refined); results invalidate older caches.
# 9.5: modular facade; each result carries the additive `evidencias_forenses`
#      list shown by the GUI and the PDF.
# 9.4: Vector 5 (libopus 20.00 kHz cliff) and Vector 6 (clipping nature and
#      album-level coherence).
# 9.3: Vector 3 (Hi-Res morphology: brickwall, spectral mirror, DSD and
#      analogue roll-off) and Vector 4 (native-bit padding/comb audit).
# 9.2: Vector 1 (Mid/Side high-band asymmetry) and Vector 2 (temporal cut-off
#      jitter + MDCT spectral holes) added to the spectral result and the score.
# 9.1: the spectrum is measured on the K most active blocks of the whole file
#      (energy-guided selector) with a float32 sliding_window_view STFT and
#      Mid/Side separation, instead of a fixed 45 s window.

# Version of the cache entry layout and of the metrics it stores. It is part of
# the cache key, so improving the detection formulas recomputes old entries
# instead of serving verdicts produced by an older engine.
CACHE_SCHEMA_VERSION = "2.7.0"

# Spectrogram presentation (affects only the PNG, never the metrics).

def render_fingerprint():
    """Parameters that define the PNG. If any of them changes, the spectrogram
    cache is no longer valid (the analysis itself can still be reused)."""
    return (f"v{ENGINE_VERSION}-f{MAX_FRAMES_PNG}-p{SPECTRO_SECONDS}-"
            f"{WIDTH_PNG}x{HEIGHT_PNG}-pil-lut")

def force_utf8_output():
    """Forces UTF-8 on stdout/stderr.

    On Windows, when the output is redirected to a file or a pipe, Python uses the
    local encoding (cp1252) and any character it cannot represent (the '═' frames,
    emoji) raises UnicodeEncodeError and kills the process."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass          # stream without reconfigure (e.g. replaced in tests)

def _ensure_streams():
    """Keeps stdout/stderr usable inside a packaged executable.

    A windowed .exe (--noconsole) has no console: PyInstaller leaves
    `sys.stdout = None` and then `print` writes NOTHING, so the NDJSON protocol
    would be lost with no visible error. If the process does have a valid
    descriptor (because the GUI launched it with a pipe), it is reopened; if not,
    it is sent to a black hole so that printing is never a problem."""
    for name in ("stdout", "stderr"):
        if getattr(sys, name, None) is not None:
            continue
        stream = None
        try:
            descriptor = 1 if name == "stdout" else 2
            stream = open(descriptor, "w", encoding="utf-8", errors="replace",
                         buffering=1, closefd=False)
            stream.write("")
        except Exception:
            try:
                stream = open(os.devnull, "w", encoding="utf-8")
            except Exception:
                stream = None
        if stream is not None:
            setattr(sys, name, stream)



class StreamEvent(str, Enum):
    """NDJSON event types emitted by `motor_flac.py --path ...`."""

    START = "inicio"
    RESULT = "resultado"
    REPORT = "informe"
    WARNING = "aviso"
    ERROR = "error"
    END = "fin"


class BatchDisposition(str, Enum):
    """Streaming disposition for a finished track inside a directory batch."""

    IMMEDIATE = "immediate"
    DEFERRED = "deferred"


# Verdict labels are defined in _motor_veredicto.py; the facade repeats the
# literals so the source-level invariants (test_idioma) keep watching them.
ENGINE_VERDICTS = ("GENUINE LOSSLESS", "PROBABLY LOSSLESS", "SUSPICIOUS",
                    "PROBABLE UPSCALE", "indeterminate")

PNG_FOLDER_NAME = _motor_cache.PNG_FOLDER_NAME  # "_spectrograms"

def _rgb_to_image(rgb):
    """Pillow raster conversion (Image.fromarray) used by the LUT renderer."""
    return Image.fromarray(rgb, mode="RGB")

def _save_spectrogram(freqs, times, mix, samplerate, audio_path,
                          ceiling_hz, frequency_cutoff,
                          mode="full", seconds=None):
    """Facade seam: the tests inject renderer failures through `motor.Image`."""
    if Image is None or not PIL_OK or not _motor_espectral.PIL_OK:
        return None, "Image renderer is not available"
    return _spectrogram_save(freqs, times, mix, samplerate, audio_path,
                             ceiling_hz, frequency_cutoff,
                             mode=mode, seconds=seconds)

def read_cache_entry(path, meta, mode, seconds, requires_png):
    """Facade wrapper returning (CacheLookupStatus, cached_result_or_None)."""
    return _cache_read_entry(
        path, meta, mode, seconds, requires_png,
        ENGINE_VERSION, CACHE_SCHEMA_VERSION, render_fingerprint()
    )

def read_cache(path, meta, mode, seconds, requires_png):
    """Facade wrapper: the mutable ENGINE_VERSION/render fingerprint are passed
    explicitly, so changing `motor_flac.ENGINE_VERSION` still invalidates them."""
    return _cache_read(path, meta, mode, seconds, requires_png,
                       ENGINE_VERSION, CACHE_SCHEMA_VERSION, render_fingerprint())

def write_cache(path, meta, mode, seconds, result):
    return _cache_write(path, meta, mode, seconds, result,
                           ENGINE_VERSION, CACHE_SCHEMA_VERSION, render_fingerprint())

def verify_signature(path):
    norm_path = _normalize_path_fs(path)

    def _read_magic():
        with open(norm_path, "rb") as f:
            return f.read(4)

    try:
        return retry_io(_read_magic) == b"fLaC"
    except OSError:
        return False

def verify_metadata(path):
    norm_path = _normalize_path_fs(path)
    try:
        audio = retry_io(lambda: FLAC(norm_path))
        info  = audio.info
        return {
            "valido":           True,
            "sample_rate":      info.sample_rate,
            "bits_per_sample":  info.bits_per_sample,
            "canales":          info.channels,
            "duracion":         round(info.length, 2),
            "total_samples":    int(getattr(info, "total_samples", 0) or 0),
            "md5":              getattr(info, "md5_signature", None),
        }
    except FLACNoHeaderError:
        return {"valido": False}
    except Exception as e:
        # mutagen can raise error/MutagenError/struct.error on truncated or
        # corrupt files. They used to escape and abort the whole batch.
        return {"valido": False, "error": f"{type(e).__name__}: {e}"}

def _iter_blocks_s32_flac(path, meta=None, frames_by_block=FRAMES_BY_BLOCK):
    """Streaming fallback for 32-bit integer FLAC (RFC 9639 PCM_32) when the
    bundled libsndfile cannot open 32-bit FLAC frames directly."""
    import shutil
    import subprocess
    import threading
    if meta is None:
        meta = verify_metadata(path)
    if not (meta and meta.get("valido") and meta.get("bits_per_sample") == 32):
        raise ValueError("not a 32-bit integer FLAC container")
    ffmpeg_bin = shutil.which("ffmpeg")
    if not ffmpeg_bin:
        raise RuntimeError("libsndfile does not support 32-bit FLAC and ffmpeg is not in PATH")
    ch = max(1, int(meta.get("canales") or 1))
    bytes_per_block = int(frames_by_block) * ch * 4
    cmd = [
        ffmpeg_bin, "-nostdin", "-v", "error",
        "-i", _normalize_path_fs(path),
        "-f", "s32le", "-acodec", "pcm_s32le", "-",
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    stderr_chunks = []

    def _drain_stderr(stream):
        try:
            while True:
                chunk = stream.read(4096)
                if not chunk:
                    break
                stderr_chunks.append(chunk)
        except Exception:
            pass

    stderr_thread = None
    if proc.stderr is not None:
        stderr_thread = threading.Thread(target=_drain_stderr, args=(proc.stderr,), daemon=True)
        stderr_thread.start()
    try:
        while True:
            raw = proc.stdout.read(bytes_per_block) if proc.stdout else b""
            if not raw:
                break
            n_int = (len(raw) // (4 * ch)) * ch
            if n_int <= 0:
                break
            arr = np.frombuffer(raw[: n_int * 4], dtype="<i4").reshape(-1, ch)
            yield arr.astype(np.float64) * (1.0 / 2147483648.0)
        if stderr_thread is not None:
            stderr_thread.join(timeout=5.0)
        stderr_bytes = b"".join(stderr_chunks)
        rc = proc.wait()
        if rc != 0:
            msg = stderr_bytes.decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"ffmpeg s32le decode failed (exit {rc}): {msg}")
    finally:
        if proc.stdout:
            proc.stdout.close()
        if proc.stderr:
            proc.stderr.close()
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        if stderr_thread is not None and stderr_thread.is_alive():
            stderr_thread.join(timeout=1.0)


def _should_fallback_s32(meta, offset=0) -> bool:
    """True only when opening a valid 32-bit integer FLAC failed before any block was read."""
    return bool(
        offset == 0
        and isinstance(meta, dict)
        and meta.get("valido")
        and meta.get("bits_per_sample") == 32
    )


def _silence_floor(bits_per_sample) -> float:
    """One LSB amplitude floor for the declared bit depth."""
    return 1.0 / float(1 << max(0, int(bits_per_sample) - 1))


def _make_error_result(path, message, *, meta=None, md5=None) -> dict:
    """Builds an error result dict guaranteeing the `error XOR veredicto` invariant."""
    out = {
        "archivo": os.path.basename(path),
        "ruta": path,
        "error": str(message),
    }
    if meta is not None:
        out["meta"] = meta
    if md5 is not None:
        out["md5"] = md5
    return out


def read_audio(path):
    norm_path = _normalize_path_fs(path)
    try:
        data, sr = retry_io(
            lambda: sf.read(norm_path, dtype="float64", always_2d=True)
        )
        return data, sr
    except Exception:
        try:
            meta = verify_metadata(path)
            if _should_fallback_s32(meta):
                blocks = list(_iter_blocks_s32_flac(path, meta=meta))
                if blocks:
                    return np.concatenate(blocks, axis=0), int(meta["sample_rate"])
        except Exception:
            pass
        return None, None

# ─── INTEGRITY CHECK (STREAMINFO MD5) ────────────────────────────────────────
# The MD5 is not an opinion: it is the only objective check of the format. It is
# computed over the decoded PCM (signed integers, interleaved by channel, in
# little-endian, ceil(bits/8) bytes per sample) and compared with the stored one.
# It is reported as a FACT of its own ("md5"), never as a scoring vote.

# Direct dump width for 8/16/32 bits: the integer dtype already matches the format
# width, so the contiguous buffer is written as is (no <i8 round-trip, no
# non-contiguous view). 24 bits has no native dtype and falls back to <i4.





# ─── ANALYSIS ────────────────────────────────────────────────────────────────





















# ─── SCORING ─────────────────────────────────────────────────────────────────
# Model (Blocks 2/4/5): only POSITIVE evidence that there is no damage scores;
# every confirmed defect SUBTRACTS and BLOCKS the maximum verdict, because a file
# with a transcoding signature cannot be "GENUINE LOSSLESS".

# Band limit of a CD (Nyquist of 44.1 kHz). It is the absolute reference for
# judging the content of a hi-res file: above this there is ultrasonic content
# that a CD cannot have, and below it there is only CD band.
#
# What no longer gives points away for free:
#   - The spectral ceiling at 44.1/48 kHz. A 320 kbps MP3 and a CD master with a
#     20 kHz anti-alias filter are spectrally identical (measured: ceiling 91%
#     of Nyquist, ratio 0.42, variance ~700 dB² in both), so the ceiling
#     distinguishes nothing: it is reported, it is not rewarded.
#   - The LSB utilisation: it can only refute (Block 4).
#   - The mere presence of the MD5: it is a fact of its own, not a vote.


# ─── SINGLE STREAMING PASS ───────────────────────────────────────────────────
# Everything that needs to see the whole file (MD5, clipping, dynamic range, the
# sample for the LSB test and the spectrum) is computed in ONE single read, in
# blocks and with bounded memory. Previously the whole track was loaded as
# float64 and the whole STFT was built: a 10-minute 24/96 asked for ~2.8 GB.


class _HandleState(Enum):
    """Explicit handle liveness state for bounded transient I/O retries."""

    ALIVE = "alive"
    STALE_CLOSED = "stale_closed"


def _close_handle_quietly(handle) -> None:
    if handle is None:
        return
    try:
        handle.close()
    except Exception:
        pass


def _read_sf_block_with_retry(
    sf_handle, norm_path, offset, read_call, delays=IO_RETRY_DELAYS, sleep_fn=None
):
    """Reads a block from an open SoundFile, retrying transient NAS/I/O failures."""
    handle = sf_handle
    try:
        return handle, read_call(handle)
    except Exception as exc:
        if not _is_transient_io_error(exc):
            raise
        wait = time.sleep if sleep_fn is None else sleep_fn
        last_exc = exc
        state = _HandleState.ALIVE if handle is not None else _HandleState.STALE_CLOSED
        for delay in tuple(delays):
            wait(delay)
            if state is _HandleState.ALIVE and handle is not None:
                try:
                    handle.seek(offset)
                except Exception as seek_exc:
                    _close_handle_quietly(handle)
                    handle = None
                    state = _HandleState.STALE_CLOSED
                    if not _is_transient_io_error(seek_exc):
                        raise
                    last_exc = seek_exc
                else:
                    try:
                        return handle, read_call(handle)
                    except Exception as read_exc:
                        _close_handle_quietly(handle)
                        handle = None
                        state = _HandleState.STALE_CLOSED
                        if not _is_transient_io_error(read_exc):
                            raise
                        last_exc = read_exc
                        continue
            if state is _HandleState.STALE_CLOSED:
                try:
                    handle = sf.SoundFile(norm_path)
                    state = _HandleState.ALIVE
                    if offset:
                        handle.seek(offset)
                    return handle, read_call(handle)
                except Exception as reopen_exc:
                    _close_handle_quietly(handle)
                    handle = None
                    state = _HandleState.STALE_CLOSED
                    if not _is_transient_io_error(reopen_exc):
                        raise
                    last_exc = reopen_exc
        raise last_exc


def _declared_total_samples(path, meta):
    """Returns declared STREAMINFO sample count if known, else 0."""
    declared = int((meta or {}).get("total_samples") or 0)
    if declared > 0:
        return declared
    try:
        return int(getattr(FLAC(_normalize_path_fs(path)).info, "total_samples", 0) or 0)
    except Exception:
        return 0


def _read_blocks(path, frames_by_block=FRAMES_BY_BLOCK):
    """Generator of blocks: it never loads the whole track into memory."""
    norm_path = _normalize_path_fs(path)
    offset = 0
    try:
        f = retry_io(lambda: sf.SoundFile(norm_path))
        try:
            while True:
                f, block = _read_sf_block_with_retry(
                    f, norm_path, offset,
                    lambda handle: handle.read(
                        frames_by_block, dtype="float64", always_2d=True
                    ),
                )
                if block.shape[0] == 0:
                    return
                offset += block.shape[0]
                yield block
        finally:
            try:
                f.close()
            except Exception:
                pass
    except Exception:
        if offset > 0:
            raise
        meta = verify_metadata(path)
        if _should_fallback_s32(meta, offset=offset):
            yield from _iter_blocks_s32_flac(path, meta=meta,
                                             frames_by_block=frames_by_block)
        else:
            raise

# ─── ENERGY-GUIDED SPECTRAL SELECTOR ────────────────────────────────────────
# The spectrum used to be measured on the first/central 45 s, which is exactly
# the wrong window when a track opens with 90 s of soft analogue synth or an
# acoustic intro: the ceiling came out artificially low and a genuine file was
# penalised. The whole file is already being read for the MD5, so every block is
# scored by its transient/high-frequency energy and only the K most active ones
# are transformed. That keeps the STFT work bounded (~45-60 s of audio) while
# guaranteeing that the sections with drums and cymbals are the ones measured.










class _BlockAnalysis:
    """Accumulates in a single pass the analyses that need the whole file."""

    def __init__(self, meta, sr, total_frames, mode, seconds,
                 save_png=False, path=None):
        self.meta  = meta
        self.sr    = int(sr)
        self.bits  = meta["bits_per_sample"]
        self.total = int(total_frames) if total_frames and total_frames > 0 else (1 << 62)
        self.path  = path
        self.save_png = bool(save_png) and PIL_OK and bool(path)

        # MD5 (the same PCM as verify_md5, hashed incrementally). It is only
        # hashed if there is something to compare with: with no recorded MD5 or
        # with an unverifiable bit depth, the result is "absent"/"unverifiable"
        # and hashing would be an extra pass over the whole file for nothing.
        self._md5_width = _WIDTH_BY_BITS.get(self.bits)
        self._md5 = (hashlib.md5()
                     if self._md5_width and meta.get("md5") else None)
        self._packer24 = (_Int24Packer()
                          if self._md5 is not None and self._md5_width == 3
                          else None)
        self._abs_i32_buf = None

        # clipping + global peak
        n_ch = int(meta.get("canales") or 2)
        self._clip_acc = _ClippingAccumulator(n_ch)
        self._peak     = 0.0

        # dynamic range (3 s blocks, carrying the incomplete block over)
        self._dr_block   = self.sr * 3
        self._dr_sumsq    = []
        self._dr_remainder = np.zeros(0)
        self._dr_peak     = 0.0

        # LSB: O(1)-memory sample spread over the whole file
        self._bdi_acc = _StreamingBitDepthAccumulator(self.bits, self.total)
        # Vector 4: native-integer audit over the WHOLE file (24-bit and 32-bit).
        self._forensic_bits = (ForensicBitsAccumulator(self.bits, sample_rate=self.sr)
                              if self.bits in (24, 32) else None)

        # spectrum: the whole file is scored, the K most active blocks are kept
        self._spectrum_acc = _SpectralAccumulator(
            self.sr, meta.get("canales", 0) >= 2 if meta.get("canales") else None,
            save_png=self.save_png, path=path,
            renderer=_save_spectrogram,
            mode=mode, seconds=seconds)
        self._state = AccumulatorState.COLLECTING
        self._cached_result = None

    @property
    def state(self) -> AccumulatorState:
        """Current streaming lifecycle phase."""
        return self._state

    def __repr__(self) -> str:
        return (
            f"_BlockAnalysis(sr={self.sr}, bits={self.bits}, "
            f"state={self._state.value!r}, path={self.path!r})"
        )

    def _require_collecting(self) -> None:
        if self._state is not AccumulatorState.COLLECTING:
            raise RuntimeError("cannot add audio blocks after completion")

    def add(self, block, offset):
        self._require_collecting()
        try:
            q_int = None
            if self.bits == 24 and (self._md5 is not None
                                    or self._forensic_bits is not None):
                q_int = np.clip(
                    np.rint(np.asarray(block, dtype=np.float64) * 8388608.0),
                    -8388608.0, 8388607.0).astype("<i4")
            elif self.bits == 32 and (self._md5 is not None
                                      or self._forensic_bits is not None):
                q_int = np.clip(
                    np.rint(np.asarray(block, dtype=np.float64) * 2147483648.0),
                    -2147483648.0, 2147483647.0).astype("<i4")
            self._integrity_and_clipping(block, q_int=q_int)
            mono = self._dynamic_range(block)
            self._sample_bits(block, offset)
            if self._forensic_bits is not None:
                self._forensic_bits.add(block, q_int=q_int)
            self._spectrum_acc.add(block, offset, mono=mono)
        except Exception:
            self._state = AccumulatorState.FAILED
            raise

    # ── per block ────────────────────────────────────────────────────────────
    def _integrity_and_clipping(self, block, q_int=None):
        if self._md5 is not None:
            self._md5.update(_pcm_bytes(block, self.bits, self._md5_width,
                                        q_int=q_int, packer24=self._packer24))
        if block.ndim > 1:
            n_ch = block.shape[1]
            if n_ch == 2:
                # Operating on the two channel columns avoids the stride overhead
                # of np.max(..., axis=1) on a tiny axis.
                amp = np.maximum(np.abs(block[:, 0]), np.abs(block[:, 1]))
            elif n_ch == 1:
                amp = np.abs(block[:, 0])
            else:
                amp = np.max(np.abs(block), axis=1)
        else:
            amp = np.abs(block)
        peak = 0.0
        if amp.size:
            peak = float(np.max(amp))
            if peak > self._peak:
                self._peak = peak
        if peak < 0.9999:
            self._clip_acc.flush_zeros()
            return
        self._clip_acc.add_float_block(block)

    def add_int16(self, block_i16, offset):
        """Streaming path for native little-endian int16 PCM blocks."""
        self._require_collecting()
        try:
            if block_i16.shape[0] == 0:
                return
            if self._md5 is not None:
                self._md5.update(_pcm_bytes(None, 16, self._md5_width, q_int=block_i16))
            max_abs = 0
            if block_i16.size:
                max_abs = max(int(np.max(block_i16)), -int(np.min(block_i16)))
                peak = max_abs * (1.0 / 32768.0)
                if peak > self._peak:
                    self._peak = peak
            n_ch = block_i16.shape[1]
            if n_ch == 2:
                mono = (
                    block_i16[:, 0].astype(np.int32) + block_i16[:, 1].astype(np.int32)
                ) * (0.5 / 32768.0)
            elif n_ch == 1:
                mono = block_i16[:, 0] * (1.0 / 32768.0)
            else:
                mono = block_i16.mean(axis=1, dtype=np.float64) * (1.0 / 32768.0)
            # |sample| / 32768.0 >= 0.9999 iff |sample| >= 32765.
            if max_abs < 32765:
                self._clip_acc.flush_zeros()
            else:
                self._clip_acc.add_int16_block(block_i16, threshold=32765)
            self._dynamic_range_from_mono(mono)
            self._spectrum_acc.add(block_i16, offset, mono=mono, int_scale=1.0 / 32768.0)
        except Exception:
            self._state = AccumulatorState.FAILED
            raise

    def add_int24_s32(self, q_i32, offset):
        """Streaming path for signed 24-bit integers stored in <i4 containers."""
        self._require_collecting()
        try:
            if q_i32.shape[0] == 0:
                return
            if self._md5 is not None:
                self._md5.update(_pcm_bytes(None, 24, self._md5_width, q_int=q_i32,
                                            packer24=self._packer24))
            if self._abs_i32_buf is None or self._abs_i32_buf.shape != q_i32.shape:
                self._abs_i32_buf = np.empty(q_i32.shape, dtype=np.int32)
            abs_view = self._abs_i32_buf
            np.abs(q_i32, out=abs_view)
            max_abs = int(np.max(abs_view)) if abs_view.size else 0
            peak = max_abs * (1.0 / 8388608.0)
            if peak > self._peak:
                self._peak = peak
            n_ch = q_i32.shape[1]
            if n_ch == 2:
                mono = (q_i32[:, 0] + q_i32[:, 1]) * (0.5 / 8388608.0)
            elif n_ch == 1:
                mono = q_i32[:, 0] * (1.0 / 8388608.0)
            else:
                mono = q_i32.mean(axis=1, dtype=np.float64) * (1.0 / 8388608.0)
            # |sample| / 8388608.0 >= 0.9999 iff |sample| >= 8387770.
            if max_abs < 8387770:
                self._clip_acc.flush_zeros()
            else:
                self._clip_acc.add_abs_int_block(abs_view, threshold=8387770)
            self._dynamic_range_from_mono(mono)
            self._bdi_acc.add_block(offset, q_int=q_i32, abs_q=abs_view)
            if self._forensic_bits is not None:
                self._forensic_bits.add(q_i32, q_int=q_i32, abs_q=abs_view)
            self._spectrum_acc.add(q_i32, offset, mono=mono, int_scale=1.0 / 8388608.0)
        except Exception:
            self._state = AccumulatorState.FAILED
            raise

    def _dynamic_range_from_mono(self, mono):
        block_mono = mono
        if mono.size:
            # max(|mono|) without materialising a full np.abs(mono) temporary.
            peak = max(float(np.max(mono)), -float(np.min(mono)))
            if peak > self._dr_peak:
                self._dr_peak = peak
        if self._dr_remainder.size:
            mono = np.concatenate((self._dr_remainder, mono))
        n = mono.size // self._dr_block
        if n:
            full = mono[: n * self._dr_block].reshape(n, self._dr_block)
            self._dr_sumsq.extend(np.einsum("ij,ij->i", full, full).tolist())
            mono = mono[n * self._dr_block :]
        self._dr_remainder = mono
        return block_mono

    def _dynamic_range(self, block):
        if block.ndim > 1:
            n_ch = block.shape[1]
            if n_ch == 2:
                mono = (block[:, 0] + block[:, 1]) * 0.5
            elif n_ch == 1:
                mono = block[:, 0]
            else:
                mono = block.mean(axis=1)
        else:
            mono = block
        return self._dynamic_range_from_mono(mono)

    def _sample_bits(self, block, offset):
        self._bdi_acc.add_block(offset, block_f64=block)

    # ── result ───────────────────────────────────────────────────────────────
    def result(self):
        if self._state is AccumulatorState.FAILED:
            raise RuntimeError("cannot finalize failed block analysis")
        if self._cached_result is not None:
            return dict(self._cached_result)
        self._clip_acc.finalize()
        md5_out = _compare_md5(self._md5.hexdigest() if self._md5 else None, self.meta)
        clip_out = self._clip_acc.clip_dict()
        if self._dr_block and self._dr_sumsq:
            rms = np.sqrt(np.asarray(self._dr_sumsq, dtype=np.float64) / self._dr_block)
            dr_out = _dr_from_rms(rms, self._dr_peak, len(self._dr_sumsq))
        else:
            dr_out = None
        silence_floor = _silence_floor(self.bits)
        if self._peak < silence_floor:
            output = {
                "pico": self._peak,
                "md5": md5_out,
                "clip": clip_out,
                "dr": dr_out,
                "bdi": {"aplica": False},
                "esp": {"error": "digital silence: insufficient signal energy"},
            }
            self._state = AccumulatorState.COMPLETED
            self._cached_result = dict(output)
            return output
        if md5_out.get("estado") == Md5Status.MISMATCH.value:
            output = {
                "pico": self._peak,
                "md5": md5_out,
                "clip": clip_out,
                "dr": dr_out,
                "bdi": {"aplica": False},
                "esp": {"error": "STREAMINFO MD5 mismatch"},
            }
            self._state = AccumulatorState.COMPLETED
            self._cached_result = dict(output)
            return output

        output = {
            "pico": self._peak,
            "md5": md5_out,
            "clip": clip_out,
            "dr": dr_out,
        }

        bdi = self._bdi_acc.result()
        if self._forensic_bits is not None:
            bdi["forense_bits"] = self._forensic_bits.result()
        elif bdi.get("aplica"):
            # Deeper than 16 bits but not 24: Vector 4 does not apply. The key
            # matches the array path so both give identical results.
            bdi["forense_bits"] = {"aplica": False}
        output["bdi"] = bdi
        output["esp"] = self._spectral_result()
        self._state = AccumulatorState.COMPLETED
        self._cached_result = dict(output)
        return output

    def _spectral_result(self):
        spectrum = self._spectrum_acc.result()
        if not spectrum.get("error"):
            # Vector 6: the nature of the clipping needs the spectrum (cut-off
            # below 20.5 kHz or content up to Nyquist) and edge/codec context.
            has_codec_sig = bool(
                spectrum.get("corte_artificial")
                or spectrum.get("sospecha_joint_stereo")
                or spectrum.get("sospecha_corte_opus_20k")
                or spectrum.get("islas_mdct_sfb21")
                or spectrum.get("sospecha_excitador_armonico")
                or spectrum.get("sbr_qmf_detectado")
                or spectrum.get("modulacion_psicoacustica_20k")
                or spectrum.get("mdct_lattice_detected")
                or (spectrum.get("iqr_corte_hz") or 0.0) > 550.0
                or (spectrum.get("ratio_huecos_mdct") or 0.0) > 0.10
            )
            spectrum.update(self._clip_acc.classify(
                ceiling_hz=spectrum.get("techo_hz"),
                frequency_cutoff=spectrum.get("frecuencia_corte"),
                nyquist=self.sr // 2,
                edge_gradient_db_khz=spectrum.get("gradiente_borde_db_khz"),
                has_codec_signature=has_codec_sig,
            ))
        return spectrum

def _analyze_streaming(path, meta, mode, seconds, save_png):
    """A single read of the file, with memory bounded per block."""
    norm_path = _normalize_path_fs(path)
    offset = 0
    try:
        f = retry_io(lambda: sf.SoundFile(norm_path))
        try:
            sr    = f.samplerate
            total = f.frames
            accumulator = _BlockAnalysis(meta, sr, total, mode, seconds,
                                             save_png, path)
            bits = meta.get("bits_per_sample")
            subtype = getattr(f, "subtype", None)
            if bits == 16 and subtype == "PCM_16":
                buf = np.empty((FRAMES_BY_BLOCK, f.channels), dtype="<i2")
                while True:
                    f, block = _read_sf_block_with_retry(
                        f, norm_path, offset, lambda handle: handle.read(out=buf)
                    )
                    if block.shape[0] == 0:
                        break
                    accumulator.add_int16(block, offset)
                    offset += block.shape[0]
            elif bits == 24 and subtype == "PCM_24":
                buf = np.empty((FRAMES_BY_BLOCK, f.channels), dtype="<i4")
                q_buf = np.empty((FRAMES_BY_BLOCK, f.channels), dtype="<i4")
                while True:
                    f, block = _read_sf_block_with_retry(
                        f, norm_path, offset, lambda handle: handle.read(out=buf)
                    )
                    if block.shape[0] == 0:
                        break
                    q_view = q_buf[: block.shape[0]]
                    np.right_shift(block, 8, out=q_view)
                    accumulator.add_int24_s32(q_view, offset)
                    offset += block.shape[0]
            else:
                while True:
                    f, block = _read_sf_block_with_retry(
                        f, norm_path, offset,
                        lambda handle: handle.read(
                            FRAMES_BY_BLOCK, dtype="float64", always_2d=True
                        ),
                    )
                    if block.shape[0] == 0:
                        break
                    accumulator.add(block, offset)
                    offset += block.shape[0]
        finally:
            try:
                f.close()
            except Exception:
                pass
    except Exception:
        if not _should_fallback_s32(meta, offset=offset):
            raise
        sr = int(meta["sample_rate"])
        total = _declared_total_samples(path, meta)
        accumulator = _BlockAnalysis(meta, sr, total, mode, seconds,
                                         save_png, path)
        offset = 0
        for block in _iter_blocks_s32_flac(path, meta=meta,
                                           frames_by_block=FRAMES_BY_BLOCK):
            if block.shape[0] == 0:
                continue
            accumulator.add(block, offset)
            offset += block.shape[0]
    if total and offset != total:
        raise ValueError(f"decoded {offset} of the {total} declared samples")
    return accumulator.result()


def _json_line(obj):
    """Serialises one line of the protocol, guaranteeing valid JSON."""
    try:
        return json.dumps(_sanitize(obj), default=str, allow_nan=False)
    except (TypeError, ValueError) as e:
        return json.dumps({"tipo": StreamEvent.ERROR.value,
                           "mensaje": f"result is not serialisable: {type(e).__name__}: {e}"})

# ─── CACHE (Block 8) ─────────────────────────────────────────────────────────
# Repeating the analysis of an already verified album should not decode or draw
# anything again. A JSON is stored per track, next to the spectrograms, holding
# the full result and the fingerprint of what produced it:
#
#   identidad  = size + modification date + STREAMINFO MD5
#   parameters = engine version + mode + seconds
#   render     = fingerprint of the PNG parameters
#
# If identidad or the parameters change, the entry is not used: a modified file is
# analysed in full again. If only the render fingerprint changes, the result is
# reused but the PNG is drawn again.









# ─── PARALLELISATION (Block 8) ───────────────────────────────────────────────
# Each track is independent, so the tracks are spread across processes. PROCESSES
# are used and not threads because the workers are CPU-bound and because
# numpy/scipy release the GIL only for part of the work; each process imports
# motor_flac on its own and keeps the Agg backend, sharing no state with the
# others.

MIN_FILES_PARALLEL = 3              # below this, the start-up cost does not pay off
MAX_WORKERS = 8                        # default ceiling (memory: ~0.3 GB/process)

def default_workers(n_files):
    nucleos = os.cpu_count() or 1
    return max(1, min(nucleos, MAX_WORKERS, n_files))

class BatchTask(NamedTuple):
    """Picklable unit of work for a single track in a batch run."""

    index: int
    path: str
    mode: str
    seconds: int | None
    save_png: bool
    use_cache: bool
    pre_meta: dict | None = None
    skip_cache_lookup: bool = False
    cached_entry: dict | None = None


def _work_analysis(task):
    """Work for one file in a child process (it must be at module level)."""
    if isinstance(task, BatchTask):
        return analyze_file_data(
            task.path, task.mode, task.seconds, task.save_png, task.use_cache,
            _pre_meta=task.pre_meta,
            _skip_cache_lookup=task.skip_cache_lookup,
            _pre_cached_entry=task.cached_entry,
        )
    if len(task) == 5:
        path, mode, seconds, save_png, use_cache = task
        return analyze_file_data(path, mode, seconds, save_png, use_cache)
    path, mode, seconds, save_png, use_cache, pre_meta, skip_cache_lookup = task
    return analyze_file_data(
        path, mode, seconds, save_png, use_cache,
        _pre_meta=pre_meta, _skip_cache_lookup=skip_cache_lookup,
    )

_PROBE_PROCESSES_LOCK = threading.Lock()
_PROBE_PROCESSES_CACHE = None


def _reset_process_probe_for_tests():
    """Resets the cached process-spawning probe result for deterministic tests."""
    global _PROBE_PROCESSES_CACHE
    with _PROBE_PROCESSES_LOCK:
        _PROBE_PROCESSES_CACHE = None


def _probe_processes():
    """Can processes be created in this environment?

    Some environments do not allow it (sandboxes that block Windows named pipes
    or SemLock creation). Check Queue allocation once without spawning a heavy
    child interpreter; ProcessPoolExecutor also falls back serially on error.
    """
    global _PROBE_PROCESSES_CACHE
    with _PROBE_PROCESSES_LOCK:
        if _PROBE_PROCESSES_CACHE is None:
            try:
                import multiprocessing
                ctx = multiprocessing.get_context()
                q = ctx.Queue()
                q.close()
                q.join_thread()
                _PROBE_PROCESSES_CACHE = True
            except Exception:
                _PROBE_PROCESSES_CACHE = False
        return _PROBE_PROCESSES_CACHE


def _plan_batch_cache_with_meta(files, mode, seconds, save_png, use_cache=True,
                                check_signature=False):
    """Internal cache planner that optionally preserves validated metadata."""
    plan = []
    for index, path in enumerate(files):
        status = CacheLookupStatus.MISS
        cached = None
        pre_meta = None
        if use_cache and (not check_signature or verify_signature(path)):
            meta = verify_metadata(path)
            if meta.get("valido"):
                pre_meta = meta if check_signature else None
                status, entry = read_cache_entry(
                    path, meta, mode, seconds, requires_png=save_png
                )
                if status is CacheLookupStatus.FULL_HIT and entry is not None:
                    entry = dict(entry)
                    entry["ruta"] = path
                    entry["archivo"] = os.path.basename(path)
                    entry["desde_cache"] = True
                    cached = entry
                elif (status is CacheLookupStatus.METRICS_HIT_NEEDS_PNG
                      and entry is not None):
                    entry = dict(entry)
                    entry["ruta"] = path
                    entry["archivo"] = os.path.basename(path)
                    cached = entry
        plan.append((index, path, status, cached, pre_meta))
    return plan


def plan_batch_cache(files, mode, seconds, save_png, use_cache=True):
    """Classifies each batch input against the cache lookup state machine.

    Returns a list of `(index, path, CacheLookupStatus, cached_or_none)` tuples:
    - `FULL_HIT`: returned immediately from JSON cache (`desde_cache=True`)
    - `METRICS_HIT_NEEDS_PNG`: scheduled for fast PNG hydration (no re-audit)
    - `MISS`: scheduled for full single-pass streaming analysis
    """
    return [
        (index, path, status, cached)
        for index, path, status, cached, _ in _plan_batch_cache_with_meta(
            files, mode, seconds, save_png, use_cache=use_cache, check_signature=False
        )
    ]


def classify_batch_disposition(result) -> BatchDisposition:
    """Determines whether a track result streams right away or awaits quorum."""
    if (result.get("veredicto") in (
                Verdict.SUSPICIOUS.value,
                Verdict.PROBABLE_UPSCALE.value,
                Verdict.PROBABLY_LOSSLESS.value,
            )
            and _deserves_album_coherence(result)):
        return BatchDisposition.DEFERRED
    if _deserves_album_lossy_coherence(result):
        return BatchDisposition.DEFERRED
    return BatchDisposition.IMMEDIATE


def _shutdown_unused_executor(pool_ref) -> None:
    executor = pool_ref[0]
    pool_ref[0] = None
    if executor is not None:
        try:
            executor.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass


def analyze_files(files, mode, seconds, save_png, workers=None,
                      use_cache=True):
    """Yields the results in order. Returns (generator, workers_efectivos).

    First, whatever is already computed is resolved from the cache (reading a JSON
    is much cheaper than analysing), and only what is missing is spread across
    processes. If a child process dies, that file is reported as an error and the
    batch continues."""
    # 1) What can be reused without analysing anything
    plan_with_meta = _plan_batch_cache_with_meta(
        files, mode, seconds, save_png, use_cache=use_cache, check_signature=True
    )
    pending_entries = [
        (index, path, status, cached, pre_meta)
        for index, path, status, cached, pre_meta in plan_with_meta
        if status is not CacheLookupStatus.FULL_HIT
    ]
    pendientes = [path for _, path, _, _, _ in pending_entries]
    workers = workers if workers and workers > 0 else default_workers(len(files))
    parallel = (workers > 1 and len(pendientes) >= MIN_FILES_PARALLEL
                   and _probe_processes())
    pool_ref = [None]
    if parallel:
        workers_efectivos = min(workers, len(pendientes))
        try:
            from concurrent.futures import ProcessPoolExecutor
            pool_ref[0] = ProcessPoolExecutor(max_workers=workers_efectivos)
        except (PermissionError, OSError):
            parallel = False
            workers_efectivos = 1
    else:
        workers_efectivos = 1

    def calculate_pending():
        """Analyses in series or in parallel the files that were not in the cache."""
        tasks = [
            BatchTask(
                index=orig_index,
                path=path,
                mode=mode,
                seconds=seconds,
                save_png=save_png,
                use_cache=use_cache,
                pre_meta=pre_meta,
                skip_cache_lookup=(
                    status is CacheLookupStatus.MISS and pre_meta is not None
                ),
                cached_entry=(
                    cached
                    if status is CacheLookupStatus.METRICS_HIT_NEEDS_PNG
                    else None
                ),
            )
            for orig_index, path, status, cached, pre_meta in pending_entries
        ]
        if not parallel:
            for task in tasks:
                yield task.index, _work_analysis(task)
            return
        from concurrent.futures import ProcessPoolExecutor

        def _file_size_key(idx):
            try:
                return -os.path.getsize(_normalize_path_fs(tasks[idx].path))
            except OSError:
                return 0

        lpt_order = sorted(range(len(tasks)), key=_file_size_key)
        executor = pool_ref[0]
        pool_ref[0] = None
        try:
            if executor is None:
                executor = ProcessPoolExecutor(max_workers=workers_efectivos)
            with executor:
                futures_by_index = {
                    idx: executor.submit(_work_analysis, tasks[idx])
                    for idx in lpt_order
                }
                for idx, task in enumerate(tasks):
                    futuro = futures_by_index[idx]
                    try:
                        yield task.index, futuro.result()
                    except Exception as e:
                        # A crashed process (memory, hang) cannot bring the batch down.
                        yield task.index, _make_error_result(
                            task.path,
                            f"the analysis process failed ({type(e).__name__}: {e})",
                        )
        except (PermissionError, OSError):
            for task in tasks:
                yield task.index, _work_analysis(task)

    def in_order():
        """Two-phase result stream:
        1. Immediate phase: tracks that cannot be rescued by album coherence
           are yielded as soon as their individual analysis finishes.
        2. Deferred coherence phase: tracks held in `retenidas` are yielded
           after `apply_album_coherence(batch)` evaluates the complete album.
        Consumers that associate results with input paths or display them in
        album order must read `result["ruta"]` and `result["indice_original"]`
        rather than assuming positional zip order."""
        calculated = calculate_pending() if pendientes else None
        batch = []
        retenidas = []
        try:
            for index, path, status, cached, _ in plan_with_meta:
                if status is CacheLookupStatus.FULL_HIT and cached is not None:
                    result = dict(cached)
                else:
                    calc_item = next(calculated, None) if calculated is not None else None
                    if calc_item is None:
                        result = _make_error_result(
                            path, "could not get the analysis result"
                        )
                    else:
                        calc_index, calc_result = calc_item
                        if calc_index != index:
                            raise RuntimeError(
                                f"batch result index mismatch: expected {index}, "
                                f"got {calc_index}"
                            )
                        result = dict(calc_result)
                result["indice_original"] = index
                batch.append(result)
                # Everything that cannot be rescued by album context is streamed
                # immediately. Only tracks that `_deserves_album_coherence` could
                # elevate are held back until the whole batch is known: a low
                # ceiling is suspicious (SUSPICIOUS/PROBABLE UPSCALE) or already
                # excused on its own by the acoustic exemption (PROBABLY LOSSLESS).
                if classify_batch_disposition(result) is BatchDisposition.DEFERRED:
                    retenidas.append((path, result))
                else:
                    yield result
        finally:
            if calculated is not None:
                calculated.close()
            _shutdown_unused_executor(pool_ref)

        # Album-level consistency needs the whole directory before deciding.
        apply_album_coherence(batch)
        for _path, result in retenidas:
            # The rescue belongs to this batch context only: the individual
            # on-disk cache keeps the suspicious verdict, so analysing the file
            # alone later does not inherit an album-level elevation.
            yield result

    gen = in_order()
    if pool_ref[0] is not None:
        weakref.finalize(gen, _shutdown_unused_executor, pool_ref)
    return gen, workers_efectivos

# ─── ANALYSIS OF ONE FILE (returns a dict, does not print) ───────────────────

def _hydrate_cached_png(path, meta, mode, seconds, cached, save_png=True):
    """Regenerates only the missing spectrogram PNG while reusing all cached
    forensic metrics, score, verdict and evidence."""
    result = dict(cached)
    result["ruta"] = path
    result["archivo"] = os.path.basename(path)
    spectrum = dict(result.get("esp") or {})
    result["esp"] = spectrum
    if bool(save_png) and PIL_OK and bool(path):
        sr = int(meta["sample_rate"])
        is_stereo = meta.get("canales", 0) >= 2 if meta.get("canales") else None
        expected_total = _declared_total_samples(path, meta)

        acc = _SpectralAccumulator(
            sr, is_stereo, save_png=True, path=path,
            renderer=_save_spectrogram, mode=mode, seconds=seconds
        )
        offset = 0
        for block in _read_blocks(path, frames_by_block=FRAMES_BY_BLOCK):
            if block.shape[0] == 0:
                continue
            acc.add(block, offset)
            offset += block.shape[0]
        if expected_total and offset != expected_total:
            raise ValueError(f"decoded {offset} of the {expected_total} declared samples")
        spectrum.update(acc.render_png_only(
            ceiling_hz=spectrum.get("techo_hz"),
            cutoff_hz=spectrum.get("frecuencia_corte"),
        ))
        result["desde_cache"] = False
        write_cache(path, meta, mode, seconds, result)
        return result
    spectrum["espectrograma"] = None
    spectrum.pop("espectrograma_error", None)
    result["desde_cache"] = False
    return result


def analyze_file_data(path, mode="full", seconds=None, save_png=False,
                           use_cache=True, _pre_meta=None,
                           _skip_cache_lookup=False,
                           _pre_cached_entry=None):
    """Analyses one file and returns a dict. With `use_cache`, if the file has not
    changed since the last analysis the stored result is reused."""
    result = {"archivo": os.path.basename(path), "ruta": path}
    meta = None
    md5_info = None

    # Hardening: any unexpected failure in one file is reported as an error of
    # THAT file. Previously an exception escaped, killed the process and the
    # consumer was left without the {"tipo":"fin"} line.
    try:
        if _pre_meta is not None:
            meta = _pre_meta
        else:
            if not verify_signature(path):
                return _make_error_result(path, "Not a valid FLAC signature")

            meta = verify_metadata(path)
            if not meta["valido"]:
                detail = meta.get("error")
                return _make_error_result(
                    path,
                    "Could not read FLAC metadata" + (f" ({detail})" if detail else ""),
                )
        result["meta"] = meta

        # Cache: an already verified album is not decoded or drawn again.
        if use_cache and not _skip_cache_lookup:
            if _pre_cached_entry is not None:
                try:
                    return _hydrate_cached_png(
                        path, meta, mode, seconds, _pre_cached_entry, save_png=save_png
                    )
                except Exception as e:
                    return _make_error_result(
                        path,
                        f"Could not decode the audio ({type(e).__name__}: {e})",
                        meta=meta,
                    )
            status, cached = read_cache_entry(
                path, meta, mode, seconds, requires_png=save_png
            )
            if status is CacheLookupStatus.FULL_HIT and cached is not None:
                cached["ruta"] = path
                cached["archivo"] = os.path.basename(path)
                cached["desde_cache"] = True
                return cached
            if status is CacheLookupStatus.METRICS_HIT_NEEDS_PNG and cached is not None:
                try:
                    return _hydrate_cached_png(
                        path, meta, mode, seconds, cached, save_png=save_png
                    )
                except Exception as e:
                    return _make_error_result(
                        path,
                        f"Could not decode the audio ({type(e).__name__}: {e})",
                        meta=meta,
                    )

        # A single pass, memory bounded per block (Block 6). A decoding failure
        # here is a truncated or corrupt file.
        try:
            payload = _analyze_streaming(path, meta, mode, seconds, save_png)
        except Exception as e:
            return _make_error_result(
                path,
                f"Could not decode the audio ({type(e).__name__}: {e})",
                meta=meta,
            )

        # Silence guard (Block 2): with no signal there is nothing to analyse, and
        # any verdict would be invented. Previously a silent file came out as
        # "PROBABLY LOSSLESS" because its ceiling was measured as the whole Nyquist.
        md5_info = payload["md5"]
        if payload["pico"] < _silence_floor(meta["bits_per_sample"]):
            return _make_error_result(
                path,
                "digital silence: the file contains no analyzable signal "
                "(no verdict is issued)",
                meta=meta,
                md5=md5_info,
            )

        # Integrity: if the MD5 does not match, the file is corrupt or its audio
        # was altered, so it gets neither verdict nor score.
        result["md5"] = md5_info
        if result["md5"]["estado"] == Md5Status.MISMATCH.value:
            return _make_error_result(
                path,
                "STREAMINFO MD5 does not match the decoded "
                "audio — file corrupt or modified",
                meta=meta,
                md5=md5_info,
            )

        result["clip"] = payload["clip"]
        result["dr"]   = payload["dr"]
        result["bdi"]  = payload["bdi"]
        result["esp"]  = payload["esp"]

        score, verdict, problems, notes = calculate_score(
            meta, result["esp"], result["bdi"], include_notes=True
        )
        forensic_bits = (result.get("bdi") or {}).get("forense_bits") or {}
        if forensic_bits.get("es_flujo_dop"):
            notes.append(
                "DSD over PCM (DoP 1.0) stream: 0x05/0xFA framing markers detected in top byte"
            )
        if (
            (result.get("esp") or {}).get("es_estacionario")
            and verdict in (
                Verdict.GENUINE_LOSSLESS.value,
                Verdict.PROBABLY_LOSSLESS.value,
            )
        ):
            notes.append(
                "Stationary harmonic/tonal programme: sustained drone/synth guard active"
            )
        result["score"]     = round(score, 4)
        result["veredicto"] = verdict
        result["problemas"] = problems
        result["notes"]     = notes
        result["evidencias_forenses"] = forensic_evidence(result)
        if use_cache:
            result["desde_cache"] = False
            write_cache(path, meta, mode, seconds, result)
        return result
    except Exception as e:
        return _make_error_result(
            path,
            f"Unexpected failure in the analysis: {type(e).__name__}: {e}",
            meta=meta,
            md5=md5_info,
        )

# ─── CLI ─────────────────────────────────────────────────────────────────────
# Usage: python motor_flac.py --path "C:/Music/album" --png --pdf
# Protocol: one JSON line per event
#   { tipo: "inicio"|"resultado"|"informe"|"aviso"|"fin"|"error" }
# (_sanitize and _json_line live above, next to the cache, which also uses them.)

def natural_sort_key(path_or_name: str) -> tuple:
    """Natural sort key so numbered tracks ('1', '2', ..., '10', ..., '100')
    appear in numeric order rather than ASCII lexicographical order."""
    name = os.path.basename(str(path_or_name))
    parts = [
        (0, int(token), "") if token.isdecimal() else (1, 0, token.casefold())
        for token in re.split(r"(\d+)", name)
        if token
    ]
    return (parts, name.casefold(), name)


def sort_results_by_original_index(results) -> list[dict]:
    """Returns results ordered by valid integer `indice_original` (excluding bool)."""
    def _sort_key(pair):
        pos, item = pair
        if isinstance(item, dict):
            orig = item.get("indice_original")
            if isinstance(orig, int) and not isinstance(orig, bool):
                return (0, orig, pos)
        return (1, pos, pos)

    return [item for _, item in sorted(enumerate(results), key=_sort_key)]


def generate_pdf_report(analyzed_path, results, mode=None, output_dir=None):
    """Generates the PDF report and is unable to abort anything.

    It returns the same dict as informe_pdf.generate(), and if the module is not
    available (reportlab missing) it reports that instead of failing."""
    try:
        import informe_pdf
    except Exception as e:
        return {"ok": False, "ruta": None, "paginas": 0,
                "error": f"could not load the report generator ({type(e).__name__}: {e})"}
    ordered = sort_results_by_original_index(results)
    return informe_pdf.generate(analyzed_path, ordered, mode=mode, output_dir=output_dir)

def main_cli(argv: "list[str] | None" = None) -> int:
    """Engine CLI. Returns the exit code (0 = OK).

    It is a function and not code inside the `__main__` block so that the frozen
    executable can invoke it from its entry point (`main.py`) when the GUI launches
    it with `--motor-cli`."""
    import argparse

    force_utf8_output()
    _ensure_streams()          # in a windowed .exe stdout may not exist

    parser = argparse.ArgumentParser(description="FLAC analysis engine")
    # `dest` keeps the internal names (the code inside is not translated):
    # what changes is the interface, which is in English.
    parser.add_argument("--path",  dest="path", required=True, help="Folder or .flac file")
    parser.add_argument("--mode",  dest="mode", default="full",
                        choices=["seconds", "center", "full"],
                        help=argparse.SUPPRESS)
    parser.add_argument("--seconds", dest="seconds", type=int, default=15,
                        help=argparse.SUPPRESS)
    parser.add_argument("--png",   action="store_true", help="Save PNG spectrograms")
    parser.add_argument("--pdf",   action="store_true",
                        help="Generate an album PDF report (implies generating the "
                             "spectrograms; requires reportlab)")
    parser.add_argument("--output-dir", dest="output_dir", default=None,
                        help="Destination folder for the generated PDF report")
    parser.add_argument("--workers", type=int, default=0,
                        help=f"Parallel processes. 0 = automatic "
                             f"(up to {MAX_WORKERS}), 1 = sequential")
    parser.add_argument("--no-cache", dest="without_cache", action="store_true",
                        help="Ignore and do not write the results cache")
    args = parser.parse_args(argv)

    path     = args.path.strip().strip('"')
    output_dir = args.output_dir.strip().strip('"') if args.output_dir else None
    output_dir = output_dir or None
    seconds = args.seconds if args.mode == "seconds" else None
    # The report needs the spectrograms, so --pdf generates them even when --png
    # was not requested: the user does not have to remember to combine flags.
    save_png = args.png or args.pdf
    use_cache  = not args.without_cache

    if os.path.isfile(path) and path.lower().endswith(".flac"):
        files = [path]
    elif os.path.isdir(path):
        try:
            entries = retry_io(lambda: os.listdir(_normalize_path_fs(path)))
        except OSError as exc:
            print(_json_line({"tipo": StreamEvent.ERROR.value,
                              "mensaje": f"Could not list folder ({type(exc).__name__}: {exc})"}),
                  flush=True)
            return 1
        files = [
            os.path.join(path, f)
            for f in sorted(entries, key=natural_sort_key)
            if f.lower().endswith(".flac")
        ]
    else:
        print(_json_line({"tipo": StreamEvent.ERROR.value, "mensaje": "Invalid path"}), flush=True)
        return 1

    total = len(files)
    if total == 0:
        print(_json_line({"tipo": StreamEvent.ERROR.value, "mensaje": "No .flac files found"}), flush=True)
        return 1

    analysis, workers = analyze_files(files, args.mode, seconds, save_png,
                                          workers=args.workers, use_cache=use_cache)

    # Line 1: how many files, with the processes and the cache that will be used
    print(_json_line({"tipo": StreamEvent.START.value, "total": total, "workers": workers,
                       "paralelo": workers > 1, "cache": use_cache}), flush=True)

    results = []
    from_cache = 0
    for i, result in enumerate(analysis):
        result["tipo"]   = StreamEvent.RESULT.value
        result["indice"] = i + 1
        result["total"]  = total
        results.append(result)
        from_cache += 1 if result.get("desde_cache") else 0
        print(_json_line(result), flush=True)

    # Album report: the failure is reported, it never aborts the batch.
    if args.pdf:
        report = generate_pdf_report(path, results, mode=args.mode,
                                     output_dir=output_dir)
        if report["ok"]:
            print(_json_line({"tipo": StreamEvent.REPORT.value, "ruta": report["ruta"],
                               "paginas": report["paginas"], "total": total,
                               "desde_cache": from_cache}), flush=True)
        else:
            print(_json_line({"tipo": StreamEvent.WARNING.value,
                               "mensaje": "could not generate the PDF report: "
                                          + str(report["error"])}), flush=True)

    # Last line: end signal
    print(_json_line({"tipo": StreamEvent.END.value}), flush=True)
    return 0

if __name__ == "__main__":
    raise SystemExit(main_cli())
