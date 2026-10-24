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
    FRAMES_POR_BLOQUE, MAX_MUESTRAS_BITS, _ANCHO_POR_BITS, _DTYPE_POR_ANCHO,
    _normalizar_ruta_fs, _pcm_bytes, _md5_pcm, _comparar_md5, verificar_md5,
    analizar_bit_depth, _rachas, detectar_clipping, _clasificar_rachas,
    _clasificar_naturaleza_clipping, _dr_desde_rms, calcular_dr,
    AcumuladorForenseBits, _analizar_forense_bits,
)
from _motor_espectral import (
    ANCHO_PNG, ALTO_PNG, FPS_ESPECTROGRAMA_PNG, SEGUNDOS_ESPECTRO,
    MAX_FRAMES_PNG, UMBRAL_RMS_ACTIVO_DB, _LUT_ESPECTROGRAMA, _media_segura, _potencia_db,
    _espectro_dbfs, _ganancia_ruido_blanco, _stft_complejo,
    _potencia_desde_fft, _bloques_activos_necesarios, _puntuacion_actividad,
    _analizar_asimetria_mid_side, _diagnosticar_autenticidad_hires,
    _metricas_espectrales, analizar_espectro, renderizar_espectrograma_rapido,
    AcumuladorPsicoacusticoSTFT, _AcumuladorEspectral,
    _guardar_espectrograma as _esp_guardar_espectrograma,
)
from _motor_veredicto import (
    LIMITE_CD_HZ, NOTA_COHERENCIA_ALBUM, calcular_score,
    _merece_coherencia_album, aplicar_coherencia_album, evidencias_forenses,
)
from _motor_cache import (
    NOMBRE_CARPETA_PNG, _carpeta_png, _ruta_cache, _identidad, _clave_analisis,
    _reemplazar_con_reintentos, _sanear,
    leer_cache as _cache_leer, escribir_cache as _cache_escribir,
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
#      `analizar_archivos` and, inside a directory that is >= 80% GENUINE LOSSLESS,
#      `aplicar_coherencia_album` marks it with `coherencia_album = True` and
#      NOTA_COHERENCIA_ALBUM. The elevation lives only in the batch result: the
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

def huella_render():
    """Parameters that define the PNG. If any of them changes, the spectrogram
    cache is no longer valid (the analysis itself can still be reused)."""
    return (f"v{VERSION_MOTOR}-f{MAX_FRAMES_PNG}-p{SEGUNDOS_ESPECTRO}-"
            f"{ANCHO_PNG}x{ALTO_PNG}-pil-lut")

def forzar_utf8_salida():
    """Forces UTF-8 on stdout/stderr.

    On Windows, when the output is redirected to a file or a pipe, Python uses the
    local encoding (cp1252) and any character it cannot represent (the '═' frames,
    emoji) raises UnicodeEncodeError and kills the process."""
    for flujo in (sys.stdout, sys.stderr):
        try:
            flujo.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass          # stream without reconfigure (e.g. replaced in tests)

def _asegurar_flujos():
    """Keeps stdout/stderr usable inside a packaged executable.

    A windowed .exe (--noconsole) has no console: PyInstaller leaves
    `sys.stdout = None` and then `print` writes NOTHING, so the NDJSON protocol
    would be lost with no visible error. If the process does have a valid
    descriptor (because the GUI launched it with a pipe), it is reopened; if not,
    it is sent to a black hole so that printing is never a problem."""
    for nombre in ("stdout", "stderr"):
        if getattr(sys, nombre, None) is not None:
            continue
        flujo = None
        try:
            descriptor = 1 if nombre == "stdout" else 2
            flujo = open(descriptor, "w", encoding="utf-8", errors="replace",
                         buffering=1, closefd=False)
            flujo.write("")
        except Exception:
            try:
                flujo = open(os.devnull, "w", encoding="utf-8")
            except Exception:
                flujo = None
        if flujo is not None:
            setattr(sys, nombre, flujo)



# Verdict labels are defined in _motor_veredicto.py; the facade repeats the
# literals so the source-level invariants (test_idioma) keep watching them.
VEREDICTOS_MOTOR = ("GENUINE LOSSLESS", "PROBABLY LOSSLESS", "SUSPICIOUS",
                    "PROBABLE UPSCALE", "indeterminate")

NOMBRE_CARPETA_PNG = _motor_cache.NOMBRE_CARPETA_PNG  # "_spectrograms"

def _rgb_a_imagen(rgb):
    """Pillow raster conversion (Image.fromarray) used by the LUT renderer."""
    return Image.fromarray(rgb, mode="RGB")

def _guardar_espectrograma(freqs, times, mezcla, samplerate, ruta_audio,
                          techo_hz, frecuencia_corte):
    """Facade seam: the tests inject renderer failures through `motor.Image`."""
    if Image is None:
        return None, "Image renderer is not available"
    return _esp_guardar_espectrograma(freqs, times, mezcla, samplerate, ruta_audio,
                                      techo_hz, frecuencia_corte)

def leer_cache(ruta, meta, modo, segundos, requiere_png):
    """Facade wrapper: the mutable VERSION_MOTOR/render fingerprint are passed
    explicitly, so changing `motor_flac.VERSION_MOTOR` still invalidates them."""
    return _cache_leer(ruta, meta, modo, segundos, requiere_png,
                       VERSION_MOTOR, VERSION_ESQUEMA_CACHE, huella_render())

def escribir_cache(ruta, meta, modo, segundos, resultado):
    return _cache_escribir(ruta, meta, modo, segundos, resultado,
                           VERSION_MOTOR, VERSION_ESQUEMA_CACHE, huella_render())

def verificar_firma(ruta):
    try:
        with open(_normalizar_ruta_fs(ruta), "rb") as f:
            return f.read(4) == b"fLaC"
    except (FileNotFoundError, PermissionError):
        return False

def verificar_metadatos(ruta):
    try:
        audio = FLAC(_normalizar_ruta_fs(ruta))
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

def leer_audio(ruta):
    try:
        data, sr = sf.read(_normalizar_ruta_fs(ruta), dtype="float64", always_2d=True)
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


def _leer_bloques(ruta, frames_por_bloque=FRAMES_POR_BLOQUE):
    """Generator of blocks: it never loads the whole track into memory."""
    with sf.SoundFile(_normalizar_ruta_fs(ruta)) as f:
        while True:
            bloque = f.read(frames_por_bloque, dtype="float64", always_2d=True)
            if bloque.shape[0] == 0:
                return
            yield bloque

# ─── ENERGY-GUIDED SPECTRAL SELECTOR ────────────────────────────────────────
# The spectrum used to be measured on the first/central 45 s, which is exactly
# the wrong window when a track opens with 90 s of soft analogue synth or an
# acoustic intro: the ceiling came out artificially low and a genuine file was
# penalised. The whole file is already being read for the MD5, so every block is
# scored by its transient/high-frequency energy and only the K most active ones
# are transformed. That keeps the STFT work bounded (~45-60 s of audio) while
# guaranteeing that the sections with drums and cymbals are the ones measured.










class _AnalisisPorBloques:
    """Accumulates in a single pass the analyses that need the whole file."""

    def __init__(self, meta, sr, total_frames, modo, segundos,
                 guardar_png=False, ruta=None):
        self.meta  = meta
        self.sr    = int(sr)
        self.bits  = meta["bits_per_sample"]
        self.total = int(total_frames) if total_frames and total_frames > 0 else (1 << 62)
        self.ruta  = ruta
        self.guardar_png = bool(guardar_png) and PIL_OK and bool(ruta)

        # MD5 (the same PCM as verificar_md5, hashed incrementally). It is only
        # hashed if there is something to compare with: with no recorded MD5 or
        # with an unverifiable bit depth, the result is "absent"/"unverifiable"
        # and hashing would be an extra pass over the whole file for nothing.
        self._ancho_md5 = _ANCHO_POR_BITS.get(self.bits)
        self._md5 = (hashlib.md5()
                     if self._ancho_md5 and meta.get("md5") else None)

        # clipping + global peak
        self._clip_runs   = 0
        self._clip_abierta = 0
        self._clip_longitudes = []      # Vector 6: all run lengths, including 1-2
        self._pico        = 0.0

        # dynamic range (3 s blocks, carrying the incomplete block over)
        self._dr_bloque   = self.sr * 3
        self._dr_sumsq    = []
        self._dr_pendiente = np.zeros(0)
        self._dr_pico     = 0.0

        # LSB: sample spread over the whole file
        self._muestras    = []
        self._muestras_n  = 0
        self._paso_muestreo = (max(1, int(round(self.total / MAX_MUESTRAS_BITS)))
                               if self.total < (1 << 62) else 1)
        # Vector 4: native-integer audit over the WHOLE file (24-bit only).
        self._forense_bits = (AcumuladorForenseBits(self.bits, sample_rate=self.sr)
                              if self.bits == 24 else None)

        # spectrum: the whole file is scored, the K most active blocks are kept
        self._espectro_acc = _AcumuladorEspectral(
            self.sr, meta.get("canales", 0) >= 2 if meta.get("canales") else None,
            guardar_png=self.guardar_png, ruta=ruta,
            renderer=_guardar_espectrograma)

    def anadir(self, bloque, offset):
        self._integridad_y_clipping(bloque)
        self._rango_dinamico(bloque)
        self._muestra_bits(bloque, offset)
        if self._forense_bits is not None:
            self._forense_bits.anadir(bloque)
        self._espectro_acc.anadir(bloque, offset)

    # ── per block ────────────────────────────────────────────────────────────
    def _integridad_y_clipping(self, bloque):
        if self._md5 is not None:
            self._md5.update(_pcm_bytes(bloque, self.bits, self._ancho_md5))
        if bloque.ndim > 1:
            n_ch = bloque.shape[1]
            if n_ch == 2:
                # Operating on the two channel columns avoids the stride overhead
                # of np.max(..., axis=1) on a tiny axis.
                amp = np.maximum(np.abs(bloque[:, 0]), np.abs(bloque[:, 1]))
            elif n_ch == 1:
                amp = np.abs(bloque[:, 0])
            else:
                amp = np.max(np.abs(bloque), axis=1)
        else:
            amp = np.abs(bloque)
        if amp.size:
            pico = float(np.max(amp))
            if pico > self._pico:
                self._pico = pico
        rachas, self._clip_abierta = _rachas(amp >= 0.9999, self._clip_abierta)
        self._clip_longitudes.extend(rachas)
        self._clip_runs += sum(1 for largo in rachas if largo >= 3)

    def _rango_dinamico(self, bloque):
        if bloque.ndim > 1:
            n_ch = bloque.shape[1]
            if n_ch == 2:
                mono = (bloque[:, 0] + bloque[:, 1]) * 0.5
            elif n_ch == 1:
                mono = bloque[:, 0]
            else:
                mono = bloque.mean(axis=1)
        else:
            mono = bloque
        if mono.size:
            # max(|mono|) without materialising a full np.abs(mono) temporary.
            pico = max(float(np.max(mono)), -float(np.min(mono)))
            if pico > self._dr_pico:
                self._dr_pico = pico
        if self._dr_pendiente.size:
            mono = np.concatenate((self._dr_pendiente, mono))
        n = mono.size // self._dr_bloque
        if n:
            completo = mono[: n * self._dr_bloque].reshape(n, self._dr_bloque)
            self._dr_sumsq.extend(np.einsum("ij,ij->i", completo, completo).tolist())
            mono = mono[n * self._dr_bloque :]
        self._dr_pendiente = mono

    def _muestra_bits(self, bloque, offset):
        # Bits <= 16 never reach the LSB test (analizar_bit_depth returns
        # {"aplica": False}), so nothing is buffered for CD-depth tracks: this
        # saves holding up to 1<<20 float64 frames (~16 MB) per file.
        if self.bits <= 16 or self._muestras_n >= MAX_MUESTRAS_BITS:
            return
        if (offset // FRAMES_POR_BLOQUE) % self._paso_muestreo:
            return
        self._muestras.append(bloque)
        self._muestras_n += bloque.shape[0]

    # ── result ───────────────────────────────────────────────────────────────
    def resultado(self):
        if self._clip_abierta:
            self._clip_longitudes.append(self._clip_abierta)   # run open at EOF
        if self._clip_abierta >= 3:
            self._clip_runs += 1                # run that ends exactly with the file
        salida = {
            "pico": self._pico,
            "md5":  _comparar_md5(self._md5.hexdigest() if self._md5 else None, self.meta),
            "clip": {"runs_clip": self._clip_runs, "hay_clipping": self._clip_runs > 0},
        }
        if self._dr_bloque and self._dr_sumsq:
            rms = np.sqrt(np.asarray(self._dr_sumsq, dtype=np.float64) / self._dr_bloque)
            salida["dr"] = _dr_desde_rms(rms, self._dr_pico, len(self._dr_sumsq))
        else:
            salida["dr"] = None

        if self._muestras:
            muestra = np.concatenate(self._muestras)
        else:
            muestra = np.zeros((0, 1))
        bdi = analizar_bit_depth(muestra, self.meta, incluir_forense=False)
        if self._forense_bits is not None:
            bdi["forense_bits"] = self._forense_bits.resultado()
        elif bdi.get("aplica"):
            # Deeper than 16 bits but not 24: Vector 4 does not apply. The key
            # matches the array path so both give identical results.
            bdi["forense_bits"] = {"aplica": False}
        salida["bdi"] = bdi
        salida["esp"] = self._resultado_espectral()
        return salida

    def _resultado_espectral(self):
        esp = self._espectro_acc.resultado()
        if not esp.get("error"):
            # Vector 6: the nature of the clipping needs the spectrum (cut-off
            # below 20.5 kHz or content up to Nyquist).
            esp.update(_clasificar_rachas(self._clip_longitudes,
                                          esp.get("techo_hz"),
                                          esp.get("frecuencia_corte"),
                                          self.sr // 2))
        return esp

def _analizar_en_streaming(ruta, meta, modo, segundos, guardar_png):
    """A single read of the file, with memory bounded per block."""
    with sf.SoundFile(_normalizar_ruta_fs(ruta)) as f:
        sr    = f.samplerate
        total = f.frames
        acumulador = _AnalisisPorBloques(meta, sr, total, modo, segundos,
                                         guardar_png, ruta)
        offset = 0
        while True:
            bloque = f.read(FRAMES_POR_BLOQUE, dtype="float64", always_2d=True)
            if bloque.shape[0] == 0:
                break
            acumulador.anadir(bloque, offset)
            offset += bloque.shape[0]
    if total and offset != total:
        raise ValueError(f"decoded {offset} of the {total} declared samples")
    return acumulador.resultado()


def _json_linea(obj):
    """Serialises one line of the protocol, guaranteeing valid JSON."""
    try:
        return json.dumps(_sanear(obj), default=str, allow_nan=False)
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

MIN_ARCHIVOS_PARALELO = 3              # below this, the start-up cost does not pay off
MAX_WORKERS = 8                        # default ceiling (memory: ~0.3 GB/process)

def workers_por_defecto(n_archivos):
    nucleos = os.cpu_count() or 1
    return max(1, min(nucleos, MAX_WORKERS, n_archivos))

def _trabajo_analisis(tarea):
    """Work for one file in a child process (it must be at module level)."""
    ruta, modo, segundos, guardar_png, usar_cache = tarea
    return analizar_archivo_datos(ruta, modo, segundos, guardar_png, usar_cache)

_SONDA_PROCESOS_CACHE = None

def _sonda_procesos():
    """Can processes be created in this environment?

    Some environments do not allow it (sandboxes that block Windows named pipes).
    In that case the analysis runs in series: slower, but correct. The answer is
    cached for the lifetime of the process: probing starts a whole child
    interpreter (numpy, scipy, soundfile, Pillow), which is wasteful to repeat
    on every batch.
    """
    global _SONDA_PROCESOS_CACHE
    if _SONDA_PROCESOS_CACHE is None:
        try:
            from concurrent.futures import ProcessPoolExecutor
            with ProcessPoolExecutor(max_workers=1) as ejecutor:
                _SONDA_PROCESOS_CACHE = (
                    ejecutor.submit(int, 1).result(timeout=120) == 1)
        except Exception:
            _SONDA_PROCESOS_CACHE = False
    return _SONDA_PROCESOS_CACHE




def analizar_archivos(archivos, modo, segundos, guardar_png, workers=None,
                      usar_cache=True):
    """Yields the results in order. Returns (generator, workers_efectivos).

    First, whatever is already computed is resolved from the cache (reading a JSON
    is much cheaper than analysing), and only what is missing is spread across
    processes. If a child process dies, that file is reported as an error and the
    batch continues."""
    # 1) What can be reused without analysing anything
    plan = []
    for indice, ruta in enumerate(archivos):
        cacheado = None
        if usar_cache:
            meta = verificar_metadatos(ruta)
            if meta.get("valido"):
                cacheado = leer_cache(ruta, meta, modo, segundos,
                                      requiere_png=guardar_png)
                if cacheado is not None:
                    cacheado["desde_cache"] = True
        plan.append((indice, ruta, cacheado))

    pendientes = [ruta for _, ruta, cacheado in plan if cacheado is None]
    workers = workers if workers and workers > 0 else workers_por_defecto(len(archivos))
    en_paralelo = (workers > 1 and len(pendientes) >= MIN_ARCHIVOS_PARALELO
                   and _sonda_procesos())
    if en_paralelo:
        workers_efectivos = min(workers, len(pendientes))
    else:
        workers_efectivos = 1

    def calcular_pendientes():
        """Analyses in series or in parallel the files that were not in the cache."""
        tareas = [(ruta, modo, segundos, guardar_png, usar_cache) for ruta in pendientes]
        if not en_paralelo:
            for tarea in tareas:
                yield _trabajo_analisis(tarea)
            return
        from concurrent.futures import ProcessPoolExecutor
        with ProcessPoolExecutor(max_workers=workers_efectivos) as ejecutor:
            futuros = [ejecutor.submit(_trabajo_analisis, tarea) for tarea in tareas]
            for ruta, futuro in zip(pendientes, futuros):
                try:
                    yield futuro.result()
                except Exception as e:
                    # A crashed process (memory, hang) cannot bring the batch down.
                    yield {"archivo": os.path.basename(ruta), "ruta": ruta,
                           "error": f"the analysis process failed "
                                    f"({type(e).__name__}: {e})"}

    def en_orden():
        calculados = calcular_pendientes() if pendientes else None
        lote = []
        retenidas = []
        for indice, ruta, cacheado in plan:
            if cacheado is not None:
                resultado = cacheado
            else:
                resultado = next(calculados, None) if calculados is not None else None
                if resultado is None:
                    resultado = {"archivo": os.path.basename(ruta), "ruta": ruta,
                                 "error": "could not get the analysis result"}
            resultado["indice_original"] = indice
            lote.append(resultado)
            # Everything that cannot be rescued by album context is streamed
            # immediately. Only tracks that `_merece_coherencia_album` could
            # elevate are held back until the whole batch is known: a low
            # ceiling is suspicious (SUSPICIOUS/PROBABLE UPSCALE) or already
            # excused on its own by the acoustic exemption (PROBABLY LOSSLESS).
            candidata = (resultado.get("veredicto") in
                         ("SUSPICIOUS", "PROBABLE UPSCALE", "PROBABLY LOSSLESS")
                         and _merece_coherencia_album(resultado))
            if candidata:
                retenidas.append((ruta, resultado))
            else:
                yield resultado

        # Album-level consistency needs the whole directory before deciding.
        aplicar_coherencia_album(lote)
        for _ruta, resultado in retenidas:
            # The rescue belongs to this batch context only: the individual
            # on-disk cache keeps the suspicious verdict, so analysing the file
            # alone later does not inherit an album-level elevation.
            yield resultado

    return en_orden(), workers_efectivos

# ─── ANALYSIS OF ONE FILE (returns a dict, does not print) ───────────────────

def analizar_archivo_datos(ruta, modo="center", segundos=None, guardar_png=False,
                           usar_cache=True):
    """Analyses one file and returns a dict. With `usar_cache`, if the file has not
    changed since the last analysis the stored result is reused."""
    resultado = {"archivo": os.path.basename(ruta), "ruta": ruta}

    # Hardening: any unexpected failure in one file is reported as an error of
    # THAT file. Previously an exception escaped, killed the process and the
    # consumer was left without the {"tipo":"fin"} line.
    try:
        if not verificar_firma(ruta):
            resultado["error"] = "Not a valid FLAC signature"
            return resultado

        meta = verificar_metadatos(ruta)
        if not meta["valido"]:
            detalle = meta.get("error")
            resultado["error"] = ("Could not read FLAC metadata"
                                  + (f" ({detalle})" if detalle else ""))
            return resultado
        resultado["meta"] = meta

        # Cache: an already verified album is not decoded or drawn again.
        if usar_cache:
            cacheado = leer_cache(ruta, meta, modo, segundos, requiere_png=guardar_png)
            if cacheado is not None:
                cacheado["desde_cache"] = True
                return cacheado

        # A single pass, memory bounded per block (Block 6). A decoding failure
        # here is a truncated or corrupt file.
        try:
            datos = _analizar_en_streaming(ruta, meta, modo, segundos, guardar_png)
        except Exception as e:
            resultado["error"] = f"Could not decode the audio ({type(e).__name__}: {e})"
            return resultado

        # Silence guard (Block 2): with no signal there is nothing to analyse, and
        # any verdict would be invented. Previously a silent file came out as
        # "PROBABLY LOSSLESS" because its ceiling was measured as the whole Nyquist.
        if datos["pico"] < 1.0 / float(1 << max(0, meta["bits_per_sample"] - 1)):
            resultado["md5"] = datos["md5"]
            resultado["error"] = ("digital silence: the file contains no analyzable signal "
                                  "(no verdict is issued)")
            return resultado

        # Integrity: if the MD5 does not match, the file is corrupt or its audio
        # was altered, so it gets neither verdict nor score.
        resultado["md5"] = datos["md5"]
        if resultado["md5"]["estado"] == "mismatch":
            resultado["error"] = ("STREAMINFO MD5 does not match the decoded "
                                  "audio — file corrupt or modified")
            return resultado

        resultado["clip"] = datos["clip"]
        resultado["dr"]   = datos["dr"]
        resultado["bdi"]  = datos["bdi"]
        resultado["esp"]  = datos["esp"]

        score, veredicto, problemas = calcular_score(meta, resultado["esp"], resultado["bdi"])
        resultado["score"]     = round(score, 4)
        resultado["veredicto"] = veredicto
        resultado["problemas"] = problemas
        resultado["evidencias_forenses"] = evidencias_forenses(resultado)
        if usar_cache:
            resultado["desde_cache"] = False
            escribir_cache(ruta, meta, modo, segundos, resultado)
        return resultado
    except Exception as e:
        resultado["error"] = f"Unexpected failure in the analysis: {type(e).__name__}: {e}"
        return resultado

# ─── CLI ─────────────────────────────────────────────────────────────────────
# Usage: python motor_flac.py --path "C:/Music/album" --mode center --png --pdf
# Protocol: one JSON line per event
#   { tipo: "inicio"|"resultado"|"informe"|"aviso"|"fin"|"error" }
# (_sanear and _json_linea live above, next to the cache, which also uses them.)

def generar_informe_pdf(ruta_analizada, resultados, modo=None):
    """Generates the PDF report and is unable to abort anything.

    It returns the same dict as informe_pdf.generar(), and if the module is not
    available (reportlab missing) it reports that instead of failing."""
    try:
        import informe_pdf
    except Exception as e:
        return {"ok": False, "ruta": None, "paginas": 0,
                "error": f"could not load the report generator ({type(e).__name__}: {e})"}
    return informe_pdf.generar(ruta_analizada, resultados, modo=modo)

def cli_principal(argv: "list[str] | None" = None) -> int:
    """Engine CLI. Returns the exit code (0 = OK).

    It is a function and not code inside the `__main__` block so that the frozen
    executable can invoke it from its entry point (`main.py`) when the GUI launches
    it with `--motor-cli`."""
    import argparse

    forzar_utf8_salida()
    _asegurar_flujos()          # in a windowed .exe stdout may not exist

    parser = argparse.ArgumentParser(description="FLAC analysis engine")
    # `dest` keeps the internal names (the code inside is not translated):
    # what changes is the interface, which is in English.
    parser.add_argument("--path",  dest="ruta", required=True, help="Folder or .flac file")
    parser.add_argument("--mode",  dest="modo", default="center",
                        choices=["seconds", "center", "full"],
                        help="Spectral window: center (default), full or seconds")
    parser.add_argument("--seconds", dest="seg", type=int, default=15,
                        help="Seconds when mode=seconds")
    parser.add_argument("--png",   action="store_true", help="Save PNG spectrograms")
    parser.add_argument("--pdf",   action="store_true",
                        help="Generate an album PDF report (implies generating the "
                             "spectrograms; requires reportlab)")
    parser.add_argument("--workers", type=int, default=0,
                        help=f"Parallel processes. 0 = automatic "
                             f"(up to {MAX_WORKERS}), 1 = sequential")
    parser.add_argument("--no-cache", dest="sin_cache", action="store_true",
                        help="Ignore and do not write the results cache")
    args = parser.parse_args(argv)

    ruta     = args.ruta.strip().strip('"')
    segundos = args.seg if args.modo == "seconds" else None
    # The report needs the spectrograms, so --pdf generates them even when --png
    # was not requested: the user does not have to remember to combine flags.
    guardar_png = args.png or args.pdf
    usar_cache  = not args.sin_cache

    if os.path.isfile(ruta) and ruta.lower().endswith(".flac"):
        archivos = [ruta]
    elif os.path.isdir(ruta):
        archivos = [
            os.path.join(ruta, f)
            for f in sorted(os.listdir(ruta))
            if f.lower().endswith(".flac")
        ]
    else:
        print(_json_linea({"tipo": "error", "mensaje": "Invalid path"}), flush=True)
        return 1

    total = len(archivos)
    if total == 0:
        print(_json_linea({"tipo": "error", "mensaje": "No .flac files found"}), flush=True)
        return 1

    analisis, workers = analizar_archivos(archivos, args.modo, segundos, guardar_png,
                                          workers=args.workers, usar_cache=usar_cache)

    # Line 1: how many files, with the processes and the cache that will be used
    print(_json_linea({"tipo": "inicio", "total": total, "workers": workers,
                       "paralelo": workers > 1, "cache": usar_cache}), flush=True)

    resultados = []
    desde_cache = 0
    for i, resultado in enumerate(analisis):
        resultado["tipo"]   = "resultado"
        resultado["indice"] = i + 1
        resultado["total"]  = total
        resultados.append(resultado)
        desde_cache += 1 if resultado.get("desde_cache") else 0
        print(_json_linea(resultado), flush=True)

    # Album report: the failure is reported, it never aborts the batch.
    if args.pdf:
        informe = generar_informe_pdf(ruta, resultados, modo=args.modo)
        if informe["ok"]:
            print(_json_linea({"tipo": "informe", "ruta": informe["ruta"],
                               "paginas": informe["paginas"], "total": total,
                               "desde_cache": desde_cache}), flush=True)
        else:
            print(_json_linea({"tipo": "aviso",
                               "mensaje": "could not generate the PDF report: "
                                          + str(informe["error"])}), flush=True)

    # Last line: end signal
    print(_json_linea({"tipo": "fin"}), flush=True)
    return 0

if __name__ == "__main__":
    raise SystemExit(cli_principal())
