import hashlib
import heapq
import json
import math
import os
import sys
import time

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
    FRAMES_BY_BLOCK, MAX_SAMPLES_BITS, _WIDTH_BY_BITS, _DTYPE_BY_WIDTH,
    _normalize_path_fs, _pcm_bytes, _md5_pcm, _compare_md5, verify_md5,
    analyze_bit_depth, _streaks, detect_clipping, _classify_streaks,
    _classify_clipping_nature, _dr_from_rms, calculate_dr,
    ForensicBitsAccumulator, _analyze_forensic_bits,
)
from _motor_espectral import (
    WIDTH_PNG, HEIGHT_PNG, PNG_SPECTROGRAM_FPS, SPECTRO_SECONDS,
    MAX_FRAMES_PNG, ACTIVE_RMS_THRESHOLD_DB, _LUT_SPECTROGRAM, _mean_safe, _power_db,
    _spectrum_dbfs, _white_noise_gain, _stft_complejo,
    _power_from_fft, _needed_active_blocks, _activity_score,
    _analyze_mid_side_asymmetry, _diagnose_hires_authenticity,
    _spectral_metrics, analyze_spectrum, render_spectrogram_fast,
    PsychoacousticSTFTAccumulator, _SpectralAccumulator,
    _save_spectrogram as _spectrogram_save,
)
from _motor_veredicto import (
    LIMIT_CD_HZ, ALBUM_COHERENCE_NOTE, calculate_score,
    _deserves_album_coherence, apply_album_coherence, forensic_evidence,
)
from _motor_cache import (
    PNG_FOLDER_NAME, _png_folder, _cache_path, _identity, _analysis_key,
    _replace_with_retries, _sanitize,
    read_cache as _cache_read, write_cache as _cache_write,
)

# ─── BASIC CHECKS ─────────────────────────────────────────────────────────────

# Raised whenever any analysis calculation changes: it invalidates the caches.
# 8.1: the high band of a hi-res file is judged against the CD limit and not
#      against a proportion of Nyquist (this fixed a false "PROBABLE UPSCALE"
#      on 96/24 masters with an analogue roll-off).
VERSION_MOTOR = "9.8"
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
VERSION_ESQUEMA_CACHE = "2.3.0"

# Spectrogram presentation (affects only the PNG, never the metrics).

def render_fingerprint():
    """Parameters that define the PNG. If any of them changes, the spectrogram
    cache is no longer valid (the analysis itself can still be reused)."""
    return (f"v{VERSION_MOTOR}-f{MAX_FRAMES_PNG}-p{SPECTRO_SECONDS}-"
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



# Verdict labels are defined in _motor_veredicto.py; the facade repeats the
# literals so the source-level invariants (test_idioma) keep watching them.
ENGINE_VERDICTS = ("GENUINE LOSSLESS", "PROBABLY LOSSLESS", "SUSPICIOUS",
                    "PROBABLE UPSCALE", "indeterminate")

PNG_FOLDER_NAME = _motor_cache.PNG_FOLDER_NAME  # "_spectrograms"

def _rgb_to_image(rgb):
    """Pillow raster conversion (Image.fromarray) used by the LUT renderer."""
    return Image.fromarray(rgb, mode="RGB")

def _save_spectrogram(freqs, times, mix, samplerate, audio_path,
                          ceiling_hz, frequency_cutoff):
    """Facade seam: the tests inject renderer failures through `motor.Image`."""
    if Image is None:
        return None, "Image renderer is not available"
    return _spectrogram_save(freqs, times, mix, samplerate, audio_path,
                                      ceiling_hz, frequency_cutoff)

def read_cache(path, meta, mode, seconds, requiere_png):
    """Facade wrapper: the mutable VERSION_MOTOR/render fingerprint are passed
    explicitly, so changing `motor_flac.VERSION_MOTOR` still invalidates them."""
    return _cache_read(path, meta, mode, seconds, requiere_png,
                       VERSION_MOTOR, VERSION_ESQUEMA_CACHE, render_fingerprint())

def write_cache(path, meta, mode, seconds, result):
    return _cache_write(path, meta, mode, seconds, result,
                           VERSION_MOTOR, VERSION_ESQUEMA_CACHE, render_fingerprint())

def verify_signature(path):
    try:
        with open(_normalize_path_fs(path), "rb") as f:
            return f.read(4) == b"fLaC"
    except (FileNotFoundError, PermissionError):
        return False

def verify_metadata(path):
    try:
        audio = FLAC(_normalize_path_fs(path))
        info  = audio.info
        return {
            "valido":           True,
            "sample_rate":      info.sample_rate,
            "bits_per_sample":  info.bits_per_sample,
            "canales":          info.channels,
            "duracion":         round(info.length, 2),
            "md5":              getattr(info, "md5_signature", None),
        }
    except FLACNoHeaderError:
        return {"valido": False}
    except Exception as e:
        # mutagen can raise error/MutagenError/struct.error on truncated or
        # corrupt files. They used to escape and abort the whole batch.
        return {"valido": False, "error": f"{type(e).__name__}: {e}"}

def read_audio(path):
    try:
        data, sr = sf.read(_normalize_path_fs(path), dtype="float64", always_2d=True)
        return data, sr
    except Exception:
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


def _read_blocks(path, frames_by_block=FRAMES_BY_BLOCK):
    """Generator of blocks: it never loads the whole track into memory."""
    with sf.SoundFile(_normalize_path_fs(path)) as f:
        while True:
            block = f.read(frames_by_block, dtype="float64", always_2d=True)
            if block.shape[0] == 0:
                return
            yield block

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

        # clipping + global peak
        self._clip_runs   = 0
        self._clip_opened = 0
        self._clip_lengths = []      # Vector 6: all run lengths, including 1-2
        self._peak        = 0.0

        # dynamic range (3 s blocks, carrying the incomplete block over)
        self._dr_block   = self.sr * 3
        self._dr_sumsq    = []
        self._dr_slope = np.zeros(0)
        self._dr_peak     = 0.0

        # LSB: sample spread over the whole file
        self._samples    = []
        self._samples_n  = 0
        self._sampling_step = (max(1, int(round(self.total / MAX_SAMPLES_BITS)))
                               if self.total < (1 << 62) else 1)
        # Vector 4: native-integer audit over the WHOLE file (24-bit only).
        self._forensic_bits = (ForensicBitsAccumulator(self.bits, sample_rate=self.sr)
                              if self.bits == 24 else None)

        # spectrum: the whole file is scored, the K most active blocks are kept
        self._spectrum_acc = _SpectralAccumulator(
            self.sr, meta.get("canales", 0) >= 2 if meta.get("canales") else None,
            save_png=self.save_png, path=path,
            renderer=_save_spectrogram)

    def add(self, block, offset):
        self._integrity_and_clipping(block)
        self._dynamic_range(block)
        self._sample_bits(block, offset)
        if self._forensic_bits is not None:
            self._forensic_bits.add(block)
        self._spectrum_acc.add(block, offset)

    # ── per block ────────────────────────────────────────────────────────────
    def _integrity_and_clipping(self, block):
        if self._md5 is not None:
            self._md5.update(_pcm_bytes(block, self.bits, self._md5_width))
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
        if amp.size:
            peak = float(np.max(amp))
            if peak > self._peak:
                self._peak = peak
        streaks, self._clip_opened = _streaks(amp >= 0.9999, self._clip_opened)
        self._clip_lengths.extend(streaks)
        self._clip_runs += sum(1 for long in streaks if long >= 3)

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
        if mono.size:
            # max(|mono|) without materialising a full np.abs(mono) temporary.
            peak = max(float(np.max(mono)), -float(np.min(mono)))
            if peak > self._dr_peak:
                self._dr_peak = peak
        if self._dr_slope.size:
            mono = np.concatenate((self._dr_slope, mono))
        n = mono.size // self._dr_block
        if n:
            full = mono[: n * self._dr_block].reshape(n, self._dr_block)
            self._dr_sumsq.extend(np.einsum("ij,ij->i", full, full).tolist())
            mono = mono[n * self._dr_block :]
        self._dr_slope = mono

    def _sample_bits(self, block, offset):
        # Bits <= 16 never reach the LSB test (analyze_bit_depth returns
        # {"aplica": False}), so nothing is buffered for CD-depth tracks: this
        # saves holding up to 1<<20 float64 frames (~16 MB) per file.
        if self.bits <= 16 or self._samples_n >= MAX_SAMPLES_BITS:
            return
        if (offset // FRAMES_BY_BLOCK) % self._sampling_step:
            return
        self._samples.append(block)
        self._samples_n += block.shape[0]

    # ── result ───────────────────────────────────────────────────────────────
    def result(self):
        if self._clip_opened:
            self._clip_lengths.append(self._clip_opened)   # run open at EOF
        if self._clip_opened >= 3:
            self._clip_runs += 1                # run that ends exactly with the file
        output = {
            "pico": self._peak,
            "md5":  _compare_md5(self._md5.hexdigest() if self._md5 else None, self.meta),
            "clip": {"runs_clip": self._clip_runs, "hay_clipping": self._clip_runs > 0},
        }
        if self._dr_block and self._dr_sumsq:
            rms = np.sqrt(np.asarray(self._dr_sumsq, dtype=np.float64) / self._dr_block)
            output["dr"] = _dr_from_rms(rms, self._dr_peak, len(self._dr_sumsq))
        else:
            output["dr"] = None

        if self._samples:
            sample = np.concatenate(self._samples)
        else:
            sample = np.zeros((0, 1))
        bdi = analyze_bit_depth(sample, self.meta, include_forensic=False)
        if self._forensic_bits is not None:
            bdi["forense_bits"] = self._forensic_bits.result()
        elif bdi.get("aplica"):
            # Deeper than 16 bits but not 24: Vector 4 does not apply. The key
            # matches the array path so both give identical results.
            bdi["forense_bits"] = {"aplica": False}
        output["bdi"] = bdi
        output["esp"] = self._spectral_result()
        return output

    def _spectral_result(self):
        esp = self._spectrum_acc.result()
        if not esp.get("error"):
            # Vector 6: the nature of the clipping needs the spectrum (cut-off
            # below 20.5 kHz or content up to Nyquist).
            esp.update(_classify_streaks(self._clip_lengths,
                                          esp.get("techo_hz"),
                                          esp.get("frecuencia_corte"),
                                          self.sr // 2))
        return esp

def _analyze_streaming(path, meta, mode, seconds, save_png):
    """A single read of the file, with memory bounded per block."""
    with sf.SoundFile(_normalize_path_fs(path)) as f:
        sr    = f.samplerate
        total = f.frames
        accumulator = _BlockAnalysis(meta, sr, total, mode, seconds,
                                         save_png, path)
        offset = 0
        while True:
            block = f.read(FRAMES_BY_BLOCK, dtype="float64", always_2d=True)
            if block.shape[0] == 0:
                break
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
        return json.dumps({"tipo": "error",
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

def _work_analysis(tarea):
    """Work for one file in a child process (it must be at module level)."""
    path, mode, seconds, save_png, use_cache = tarea
    return analyze_file_data(path, mode, seconds, save_png, use_cache)

_PROBE_PROCESSES_CACHE = None

def _probe_processes():
    """Can processes be created in this environment?

    Some environments do not allow it (sandboxes that block Windows named pipes).
    In that case the analysis runs in series: slower, but correct. The answer is
    cached for the lifetime of the process: probing starts a whole child
    interpreter (numpy, scipy, soundfile, Pillow), which is wasteful to repeat
    on every batch.
    """
    global _PROBE_PROCESSES_CACHE
    if _PROBE_PROCESSES_CACHE is None:
        try:
            from concurrent.futures import ProcessPoolExecutor
            with ProcessPoolExecutor(max_workers=1) as ejecutor:
                _PROBE_PROCESSES_CACHE = (
                    ejecutor.submit(int, 1).result(timeout=120) == 1)
        except Exception:
            _PROBE_PROCESSES_CACHE = False
    return _PROBE_PROCESSES_CACHE




def analyze_files(files, mode, seconds, save_png, workers=None,
                      use_cache=True):
    """Yields the results in order. Returns (generator, workers_efectivos).

    First, whatever is already computed is resolved from the cache (reading a JSON
    is much cheaper than analysing), and only what is missing is spread across
    processes. If a child process dies, that file is reported as an error and the
    batch continues."""
    # 1) What can be reused without analysing anything
    plan = []
    for index, path in enumerate(files):
        cached = None
        if use_cache:
            meta = verify_metadata(path)
            if meta.get("valido"):
                cached = read_cache(path, meta, mode, seconds,
                                      requiere_png=save_png)
                if cached is not None:
                    cached["desde_cache"] = True
        plan.append((index, path, cached))

    pendientes = [path for _, path, cached in plan if cached is None]
    workers = workers if workers and workers > 0 else default_workers(len(files))
    parallel = (workers > 1 and len(pendientes) >= MIN_FILES_PARALLEL
                   and _probe_processes())
    if parallel:
        workers_efectivos = min(workers, len(pendientes))
    else:
        workers_efectivos = 1

    def calculate_pending():
        """Analyses in series or in parallel the files that were not in the cache."""
        tareas = [(path, mode, seconds, save_png, use_cache) for path in pendientes]
        if not parallel:
            for tarea in tareas:
                yield _work_analysis(tarea)
            return
        from concurrent.futures import ProcessPoolExecutor
        with ProcessPoolExecutor(max_workers=workers_efectivos) as ejecutor:
            futuros = [ejecutor.submit(_work_analysis, tarea) for tarea in tareas]
            for path, futuro in zip(pendientes, futuros):
                try:
                    yield futuro.result()
                except Exception as e:
                    # A crashed process (memory, hang) cannot bring the batch down.
                    yield {"archivo": os.path.basename(path), "ruta": path,
                           "error": f"the analysis process failed "
                                    f"({type(e).__name__}: {e})"}

    def in_order():
        calculated = calculate_pending() if pendientes else None
        batch = []
        retenidas = []
        for index, path, cached in plan:
            if cached is not None:
                result = cached
            else:
                result = next(calculated, None) if calculated is not None else None
                if result is None:
                    result = {"archivo": os.path.basename(path), "ruta": path,
                                 "error": "could not get the analysis result"}
            result["indice_original"] = index
            batch.append(result)
            # Everything that cannot be rescued by album context is streamed
            # immediately. Only tracks that `_deserves_album_coherence` could
            # elevate are held back until the whole batch is known: a low
            # ceiling is suspicious (SUSPICIOUS/PROBABLE UPSCALE) or already
            # excused on its own by the acoustic exemption (PROBABLY LOSSLESS).
            candidate = (result.get("veredicto") in
                         ("SUSPICIOUS", "PROBABLE UPSCALE", "PROBABLY LOSSLESS")
                         and _deserves_album_coherence(result))
            if candidate:
                retenidas.append((path, result))
            else:
                yield result

        # Album-level consistency needs the whole directory before deciding.
        apply_album_coherence(batch)
        for _path, result in retenidas:
            # The rescue belongs to this batch context only: the individual
            # on-disk cache keeps the suspicious verdict, so analysing the file
            # alone later does not inherit an album-level elevation.
            yield result

    return in_order(), workers_efectivos

# ─── ANALYSIS OF ONE FILE (returns a dict, does not print) ───────────────────

def analyze_file_data(path, mode="center", seconds=None, save_png=False,
                           use_cache=True):
    """Analyses one file and returns a dict. With `use_cache`, if the file has not
    changed since the last analysis the stored result is reused."""
    result = {"archivo": os.path.basename(path), "ruta": path}

    # Hardening: any unexpected failure in one file is reported as an error of
    # THAT file. Previously an exception escaped, killed the process and the
    # consumer was left without the {"tipo":"fin"} line.
    try:
        if not verify_signature(path):
            result["error"] = "Not a valid FLAC signature"
            return result

        meta = verify_metadata(path)
        if not meta["valido"]:
            detail = meta.get("error")
            result["error"] = ("Could not read FLAC metadata"
                                  + (f" ({detail})" if detail else ""))
            return result
        result["meta"] = meta

        # Cache: an already verified album is not decoded or drawn again.
        if use_cache:
            cached = read_cache(path, meta, mode, seconds, requiere_png=save_png)
            if cached is not None:
                cached["desde_cache"] = True
                return cached

        # A single pass, memory bounded per block (Block 6). A decoding failure
        # here is a truncated or corrupt file.
        try:
            payload = _analyze_streaming(path, meta, mode, seconds, save_png)
        except Exception as e:
            result["error"] = f"Could not decode the audio ({type(e).__name__}: {e})"
            return result

        # Silence guard (Block 2): with no signal there is nothing to analyse, and
        # any verdict would be invented. Previously a silent file came out as
        # "PROBABLY LOSSLESS" because its ceiling was measured as the whole Nyquist.
        if payload["pico"] < 1.0 / float(1 << max(0, meta["bits_per_sample"] - 1)):
            result["md5"] = payload["md5"]
            result["error"] = ("digital silence: the file contains no analyzable signal "
                                  "(no verdict is issued)")
            return result

        # Integrity: if the MD5 does not match, the file is corrupt or its audio
        # was altered, so it gets neither verdict nor score.
        result["md5"] = payload["md5"]
        if result["md5"]["estado"] == "mismatch":
            result["error"] = ("STREAMINFO MD5 does not match the decoded "
                                  "audio — file corrupt or modified")
            return result

        result["clip"] = payload["clip"]
        result["dr"]   = payload["dr"]
        result["bdi"]  = payload["bdi"]
        result["esp"]  = payload["esp"]

        score, verdict, problems = calculate_score(meta, result["esp"], result["bdi"])
        result["score"]     = round(score, 4)
        result["veredicto"] = verdict
        result["problemas"] = problems
        result["evidencias_forenses"] = forensic_evidence(result)
        if use_cache:
            result["desde_cache"] = False
            write_cache(path, meta, mode, seconds, result)
        return result
    except Exception as e:
        result["error"] = f"Unexpected failure in the analysis: {type(e).__name__}: {e}"
        return result

# ─── CLI ─────────────────────────────────────────────────────────────────────
# Usage: python motor_flac.py --path "C:/Music/album" --mode center --png --pdf
# Protocol: one JSON line per event
#   { tipo: "inicio"|"resultado"|"informe"|"aviso"|"fin"|"error" }
# (_sanitize and _json_line live above, next to the cache, which also uses them.)

def generate_pdf_report(analyzed_path, results, mode=None):
    """Generates the PDF report and is unable to abort anything.

    It returns the same dict as informe_pdf.generate(), and if the module is not
    available (reportlab missing) it reports that instead of failing."""
    try:
        import informe_pdf
    except Exception as e:
        return {"ok": False, "ruta": None, "paginas": 0,
                "error": f"could not load the report generator ({type(e).__name__}: {e})"}
    return informe_pdf.generate(analyzed_path, results, mode=mode)

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
    parser.add_argument("--mode",  dest="mode", default="center",
                        choices=["seconds", "center", "full"],
                        help="Spectral window: center (default), full or seconds")
    parser.add_argument("--seconds", dest="seconds", type=int, default=15,
                        help="Seconds when mode=seconds")
    parser.add_argument("--png",   action="store_true", help="Save PNG spectrograms")
    parser.add_argument("--pdf",   action="store_true",
                        help="Generate an album PDF report (implies generating the "
                             "spectrograms; requires reportlab)")
    parser.add_argument("--workers", type=int, default=0,
                        help=f"Parallel processes. 0 = automatic "
                             f"(up to {MAX_WORKERS}), 1 = sequential")
    parser.add_argument("--no-cache", dest="without_cache", action="store_true",
                        help="Ignore and do not write the results cache")
    args = parser.parse_args(argv)

    path     = args.path.strip().strip('"')
    seconds = args.seconds if args.mode == "seconds" else None
    # The report needs the spectrograms, so --pdf generates them even when --png
    # was not requested: the user does not have to remember to combine flags.
    save_png = args.png or args.pdf
    use_cache  = not args.without_cache

    if os.path.isfile(path) and path.lower().endswith(".flac"):
        files = [path]
    elif os.path.isdir(path):
        files = [
            os.path.join(path, f)
            for f in sorted(os.listdir(path))
            if f.lower().endswith(".flac")
        ]
    else:
        print(_json_line({"tipo": "error", "mensaje": "Invalid path"}), flush=True)
        return 1

    total = len(files)
    if total == 0:
        print(_json_line({"tipo": "error", "mensaje": "No .flac files found"}), flush=True)
        return 1

    analysis, workers = analyze_files(files, args.mode, seconds, save_png,
                                          workers=args.workers, use_cache=use_cache)

    # Line 1: how many files, with the processes and the cache that will be used
    print(_json_line({"tipo": "inicio", "total": total, "workers": workers,
                       "paralelo": workers > 1, "cache": use_cache}), flush=True)

    results = []
    from_cache = 0
    for i, result in enumerate(analysis):
        result["tipo"]   = "resultado"
        result["indice"] = i + 1
        result["total"]  = total
        results.append(result)
        from_cache += 1 if result.get("desde_cache") else 0
        print(_json_line(result), flush=True)

    # Album report: the failure is reported, it never aborts the batch.
    if args.pdf:
        report = generate_pdf_report(path, results, mode=args.mode)
        if report["ok"]:
            print(_json_line({"tipo": "informe", "ruta": report["ruta"],
                               "paginas": report["paginas"], "total": total,
                               "desde_cache": from_cache}), flush=True)
        else:
            print(_json_line({"tipo": "aviso",
                               "mensaje": "could not generate the PDF report: "
                                          + str(report["error"])}), flush=True)

    # Last line: end signal
    print(_json_line({"tipo": "fin"}), flush=True)
    return 0

if __name__ == "__main__":
    raise SystemExit(main_cli())
