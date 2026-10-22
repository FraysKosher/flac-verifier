import hashlib
import heapq
import json
import math
import os
import sys
import tempfile
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


def preparar_cache_matplotlib():
    """Where matplotlib keeps its font cache (and why it has to be decided).

    Matplotlib builds that cache the first time it draws and reuses it
    afterwards. If the default directory (`~/.matplotlib`) is not writable —an
    installation in a protected folder, a restricted user profile, an environment
    without write permission— matplotlib warns on stderr and rebuilds it on every
    run. That warning is harmless, but here it gets in the way: the engine talks
    to the GUI over stdout with JSON lines, and a stray warning ends up in the
    window log as an unreadable line.

    It is given a folder of its own under the user data directory and, if that is
    not possible either, under the temporary directory; if nothing can be created,
    the default value (`~/.matplotlib`) is left in place, which is what matplotlib
    would do on its own. It is never a fatal error: it returns the chosen folder,
    or None if nothing was touched.
    """
    if os.environ.get("MPLCONFIGDIR"):
        return None                    # if the user configured it, that value wins
    candidatas = []
    if os.environ.get("LOCALAPPDATA"):
        candidatas.append(os.path.join(os.environ["LOCALAPPDATA"], "FLAC_VERIFIER"))
    candidatas.append(os.path.join(tempfile.gettempdir(), "FLAC_VERIFIER"))
    for raiz in candidatas:
        carpeta = os.path.join(raiz, "matplotlib")
        try:
            os.makedirs(carpeta, exist_ok=True)
        except OSError:
            continue                   # no permission there: try the next one
        os.environ["MPLCONFIGDIR"] = carpeta
        return carpeta
    return None


# It has to be decided BEFORE importing matplotlib: the variable is read on load.
preparar_cache_matplotlib()

try:
    import matplotlib
    matplotlib.use('Agg')          # headless backend: safe in worker processes
    # pyplot is kept at module level because the spectrogram uses plt.Figure (the
    # OO Figure class, which does not touch pyplot's global state); the test suite
    # injects failures through `motor.plt`.
    import matplotlib.pyplot as plt
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    MATPLOTLIB_OK = True
except ImportError:
    plt = None
    FigureCanvasAgg = None
    MATPLOTLIB_OK = False

# ─── BASIC CHECKS ─────────────────────────────────────────────────────────────

# Raised whenever any analysis calculation changes: it invalidates the caches.
# 8.1: the high band of a hi-res file is judged against the CD limit and not
#      against a proportion of Nyquist (this fixed a false "PROBABLE UPSCALE"
#      on 96/24 masters with an analogue roll-off).
VERSION_MOTOR = "9.3"
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
VERSION_ESQUEMA_CACHE = "2.2.0"

# Spectrogram presentation (affects only the PNG, never the metrics).
FIGURA_ESPECTROGRAMA = (14, 5)      # inches
DPI_ESPECTROGRAMA    = 150
FPS_ESPECTROGRAMA_PNG = 20          # display time resolution (metrics use every frame)

def huella_render():
    """Parameters that define the PNG. If any of them changes, the spectrogram
    cache is no longer valid (the analysis itself can still be reused)."""
    return (f"v{VERSION_MOTOR}-f{MAX_FRAMES_PNG}-p{SEGUNDOS_ESPECTRO}-"
            f"{FIGURA_ESPECTROGRAMA[0]}x{FIGURA_ESPECTROGRAMA[1]}@{DPI_ESPECTROGRAMA}-imshow")

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

def _media_segura(valores):
    """Mean of a possibly empty array. Avoids NaN (mean of an empty slice),
    which broke the JSON protocol when serialised as the invalid literal NaN."""
    if valores.size == 0:
        return 0.0
    m = float(np.mean(valores))
    return m if math.isfinite(m) else 0.0

def _normalizar_ruta_fs(ruta):
    r"""Returns `ruta` in a form the Windows API can open even past MAX_PATH.

    On Windows the classic file APIs stop at 260 characters, so a classical album
    with long folder and track names can raise FileNotFoundError while the file is
    perfectly readable. The extended-length prefix (\\?\\) lifts that limit; UNC
    paths use \\?\UNC\ instead. Only long ABSOLUTE paths are rewritten (the prefix
    requires an absolute path), and on other systems the path is returned as is.
    """
    if os.name != "nt" or not isinstance(ruta, str) or not ruta:
        return ruta
    if ruta.startswith("\\\\?\\"):
        return ruta
    # ntpath.isabs() returns False for UNC paths without a drive (\\server\share),
    # so the two leading-separator forms are accepted explicitly.
    es_absoluta = os.path.isabs(ruta) or ruta.startswith(("\\\\", "//"))
    if not es_absoluta or len(ruta) <= 240:
        return ruta
    ruta = ruta.replace("/", "\\")
    if ruta.startswith("\\\\"):
        return "\\\\?\\UNC\\" + ruta[2:]
    return "\\\\?\\" + ruta

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

_ANCHO_POR_BITS = {8: 1, 16: 2, 24: 3, 32: 4}
# Direct dump width for 8/16/32 bits: the integer dtype already matches the format
# width, so the contiguous buffer is written as is (no <i8 round-trip, no
# non-contiguous view). 24 bits has no native dtype and falls back to <i4.
_DTYPE_POR_ANCHO = {1: "<i1", 2: "<i2", 4: "<i4"}

def _pcm_bytes(bloque, bits_per_sample, ancho):
    """Little-endian interleaved PCM of a block, exactly as the format hashes it.

    float64 -> exact integer: the soundfile conversion is m / 2**(bits-1)."""
    escala = float(1 << (bits_per_sample - 1))
    dt = _DTYPE_POR_ANCHO.get(ancho)
    if dt is not None:
        return np.rint(bloque * escala).astype(dt).tobytes()
    # 24 bits (width 3): store as <i4 and keep the low 3 bytes, which is the same
    # little-endian layout the old <i8 path produced, at half the memory.
    q = np.rint(bloque * escala).astype("<i4")
    return q.view(np.uint8).reshape(-1, 4)[:, :ancho].tobytes()

def _md5_pcm(data, bits_per_sample, frames_por_bloque=1 << 20):
    """MD5 of the decoded PCM of a whole array. None if it is not verifiable."""
    ancho = _ANCHO_POR_BITS.get(bits_per_sample)
    if ancho is None:
        return None
    md5 = hashlib.md5()
    for i in range(0, len(data), frames_por_bloque):
        md5.update(_pcm_bytes(data[i : i + frames_por_bloque], bits_per_sample, ancho))
    return md5.hexdigest()

def _comparar_md5(calculado, meta):
    """Compares an already computed MD5 with the one recorded in the STREAMINFO."""
    almacenado = meta.get("md5")
    if isinstance(almacenado, (bytes, bytearray)):
        almacenado = int.from_bytes(almacenado, "big")
    if not almacenado:
        return {"estado": "absent",
                "detalle": "STREAMINFO has no MD5 recorded (encoder built with --no-md5, or altered metadata)"}
    if calculado is None:
        return {"estado": "unverifiable",
                "detalle": f"bit depth {meta.get('bits_per_sample')} bits: not verifiable",
                "almacenado": f"{int(almacenado):032x}"}
    coincide = int(calculado, 16) == int(almacenado)
    return {
        "estado":     "match" if coincide else "mismatch",
        "calculado":  calculado,
        "almacenado": f"{int(almacenado):032x}",
    }

def verificar_md5(data, meta):
    """Compares the recomputed MD5 of an array with the STREAMINFO one. A fact, not a vote."""
    return _comparar_md5(_md5_pcm(data, meta.get("bits_per_sample")), meta)

# ─── ANALYSIS ────────────────────────────────────────────────────────────────

def analizar_bit_depth(data, meta, incluir_forense=True):
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
    forense = None
    try:
        if incluir_forense:
            forense = _analizar_forense_bits(data, meta)

        # All channels, not just the left one: a silent channel or a file with
        # misaligned channels biased the previous test.
        muestras = np.ascontiguousarray(data).reshape(-1)
        activas  = muestras[np.abs(muestras) > 0.001]
        if activas.size < 1000:
            salida = {"aplica": True, "lsbs": "indeterminate",
                      "conclusion": "indeterminate: too little signal to measure LSB utilization"}
            if forense is not None:
                salida["forense_bits"] = forense
            return salida

        # 1) Are the samples on the 16-bit grid? (the classic upscale test)
        escalado = activas * 32768.0
        fraccion = float(np.mean(np.abs(escalado - np.round(escalado)) < 0.05))

        # 2) Effective resolution: declared bits minus the mode of trailing zeros
        #    of the integer samples (16 bits inside 24 -> 8 zeros -> 16 bits).
        q = np.round(activas * float(1 << (bits - 1))).astype(np.int64)
        q = q[q != 0]
        if q.size:
            pot     = np.abs(q) & -np.abs(q)                  # 2^(trailing zeros)
            ceros   = np.log2(pot).round().astype(np.int64)
            moda    = int(np.bincount(ceros).argmax())
            efectiva = max(1, bits - moda)
        else:
            efectiva = bits

        pct = f"{fraccion * 100:.1f}%"
        if fraccion > 0.97:
            lsbs = "empty"
            conclusion = (f"the LSBs are empty ({pct} of samples on the 16-bit grid): "
                          f"effective resolution of {efectiva} bits in a {bits}-bit container "
                          f"— bit-depth upscale")
        elif fraccion > 0.80:
            lsbs = "partial"
            conclusion = (f"LSBs partially inactive ({pct} on the 16-bit grid), "
                          f"effective resolution ~{efectiva} bits — upscale with later processing")
        else:
            lsbs = "active"
            conclusion = (f"LSBs active, effective resolution ~{efectiva} of {bits} bits: "
                          f"rules out grid upscaling, but does NOT prove the origin")
        salida = {
            "aplica":                   True,
            "fraccion_16bit_grid":      round(fraccion * 100, 1),
            "resolucion_efectiva_bits": int(efectiva),
            "lsbs":                     lsbs,
            "conclusion":               conclusion,
        }
        if forense is not None:
            salida["forense_bits"] = forense
        return salida
    except Exception as e:
        salida = {"aplica": True, "lsbs": "indeterminate", "error": str(e)}
        if forense is not None:
            salida["forense_bits"] = forense
        return salida

def _guardar_espectrograma(freqs, times, mezcla, samplerate, ruta_audio, techo_hz, frecuencia_corte):
    """Draws the spectrogram. Returns (ruta_png, error): it never raises, and the
    reason for the failure is kept so that it can be reported."""
    try:
        carpeta        = os.path.dirname(ruta_audio)
        nombre         = os.path.splitext(os.path.basename(ruta_audio))[0]
        directorio_png = os.path.join(carpeta, "_spectrograms")
        os.makedirs(directorio_png, exist_ok=True)   # race-safe across processes

        ruta_png = os.path.join(directorio_png, nombre + "_espectrograma.png")

        # 1) Decimate BEFORE the transcendental step: log10 runs on far fewer
        #    cells when the window has more frames than the PNG can show.
        mezcla_arr = np.asarray(mezcla, dtype=np.float32)
        if mezcla_arr.shape[1] > MAX_FRAMES_PNG:
            step       = max(1, mezcla_arr.shape[1] // MAX_FRAMES_PNG)
            mezcla_arr = mezcla_arr[:, ::step]
            times      = times[::step]

        # 2) log10 in place, in float32 (half the memory bandwidth of float64).
        db = np.maximum(mezcla_arr, 1e-28)
        np.log10(db, out=db)
        db *= 10.0
        db -= float(np.max(db))

        # 3) OO API through plt.Figure: it returns the matplotlib.figure.Figure
        #    class directly, so pyplot's global state is not touched, yet the
        #    module-level `plt` stays as the fault-injection point the tests use.
        fig = plt.Figure(figsize=FIGURA_ESPECTROGRAMA, dpi=DPI_ESPECTROGRAMA)
        FigureCanvasAgg(fig)
        ax  = fig.add_subplot(111)

        # imshow paints a raster: with a grid of ~2049x1500 cells it is ~10 times
        # faster than pcolormesh(shading="gouraud") and more faithful (gouraud
        # interpolated between cell centres, smoothing the result).
        img = ax.imshow(db, aspect="auto", origin="lower", cmap="inferno",
                        vmin=-80, vmax=0, interpolation="auto",
                        extent=[times[0], times[-1], freqs[0] / 1000, freqs[-1] / 1000])
        fig.colorbar(img, ax=ax, label="dB (relative to peak)")
        ax.set_ylabel("Frequency (kHz)")
        ax.set_xlabel("Time (s)")
        ax.set_title(f"Spectrogram — {os.path.basename(ruta_audio)}")
        ax.set_ylim(0, samplerate / 2000)
        if techo_hz:
            ax.axhline(techo_hz / 1000, color="cyan",  lw=1.2, ls="--", alpha=0.85,
                       label=f"Ceiling: {techo_hz/1000:.1f} kHz")
        if frecuencia_corte:
            ax.axhline(frecuencia_corte / 1000, color="red", lw=1.5, ls="--", alpha=0.9,
                       label=f"Corte: {frecuencia_corte/1000:.1f} kHz")
        if techo_hz or frecuencia_corte:
            ax.legend(loc="upper right", fontsize=8)

        fig.tight_layout()
        fig.savefig(ruta_png, dpi=DPI_ESPECTROGRAMA)
        return ruta_png, None
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"

def _ganancia_ruido_blanco(nperseg, ventana):
    """Mean bin power of a unit-variance white noise through the normalised
    float32 STFT. Dividing a power spectrum by it turns each bin into dBFS: a
    broadband floor at -104 dBFS reads -104 dB, independently of nperseg."""
    n = int(nperseg)
    suma_w = float(np.sum(ventana, dtype=np.float64))
    if n <= 0 or suma_w <= 0.0:
        return 1.0
    suma_w2 = float(np.sum(np.asarray(ventana, dtype=np.float64) ** 2))
    return n * suma_w2 / (2.0 * suma_w * suma_w * (n / 2.0 + 1.0))

def _potencia_db(potencia):
    """Power spectrum in dB (floor 1e-28, no np.sqrt)."""
    return 10.0 * np.log10(np.maximum(np.asarray(potencia, dtype=np.float64), 1e-28))

def _espectro_dbfs(potencia, ganancia_ruido=1.0):
    """Power spectrum in dBFS/ bin using the white-noise gain calibration."""
    ganancia = ganancia_ruido if ganancia_ruido and ganancia_ruido > 0 else 1.0
    return 10.0 * np.log10(np.maximum(np.asarray(potencia, dtype=np.float64),
                                      1e-28) / ganancia)

def _diagnosticar_autenticidad_hires(freqs_hz, espectro_mid_db, sample_rate):
    """Vector 3: morphology of a Hi-Res spectrum (>= 88.2 kHz).

    Distinguishes three physically different cases that the old CD-limit rule
    mixed together:
      1. A brickwall at the CD/DAT Nyquist (22.05 / 24 kHz): upscale made with a
         sinc filter. A steep spectral slope plus a jump into the digital void.
      2. A spectral mirror around 22.05 kHz: upscale with an unfiltered
         interpolator (folded aliasing).
      3. A genuine analogue/acoustic roll-off (Queen case): no cliff, a smooth
         slope and a continuous thermal/tape/dither floor above the CD limit.
    A DSD/SACD transfer (88.2/176.4 kHz) adds a rising noise-shaping ramp in
    24-40 kHz.

    `espectro_mid_db` is calibrated to dBFS per bin (see _espectro_dbfs)."""
    salida = {
        "muro_brickwall_nyquist":  False,
        "espejo_aliasing_detectado": False,
        "rolloff_analogico_genuino": False,
        "gradiente_nyquist_db_khz": 0.0,
        "salto_ultrasonico_db":     0.0,
        "correlacion_espejo_22k":   0.0,
        "es_transferencia_dsd":     False,
    }
    freqs = np.asarray(freqs_hz, dtype=np.float64)
    espectro = np.asarray(espectro_mid_db, dtype=np.float64)
    if sample_rate < 88200 or freqs.size < 8 or espectro.shape != freqs.shape:
        return salida
    if not np.all(np.isfinite(espectro)):
        return salida

    df = float(freqs[1] - freqs[0]) if freqs.size > 1 else 1.0
    # 1 kHz smoothing: a 250 Hz kernel lets the bin-to-bin variance of real
    # noise look like a 90 dB/kHz cliff. The morphology of a brickwall or a
    # roll-off lives on a much wider scale, so smoothing does not hide it.
    bins_suavizado = max(3, int(round(1000.0 / df)))
    kernel = np.ones(bins_suavizado, dtype=np.float32) / bins_suavizado
    curva = np.convolve(espectro, kernel, mode="same")
    grad_db_khz = np.gradient(curva, df / 1000.0)

    mask_22k = (freqs >= 19800.0) & (freqs <= 22450.0)
    mask_24k = (freqs >= 22500.0) & (freqs <= 24400.0)
    mask_previa = (freqs >= 13500.0) & (freqs < 19200.0)
    pendiente_22k = float(np.min(grad_db_khz[mask_22k])) if np.any(mask_22k) else 0.0
    pendiente_24k = float(np.min(grad_db_khz[mask_24k])) if np.any(mask_24k) else 0.0
    peor_pendiente = min(pendiente_22k, pendiente_24k)
    pendiente_base = (float(np.percentile(grad_db_khz[mask_previa], 20))
                      if np.any(mask_previa) else -4.0)

    mask_antes = (freqs >= 18000.0) & (freqs <= 20800.0)
    mask_despues = ((freqs >= 23500.0)
                    & (freqs <= min(36000.0, sample_rate * 0.44)))
    nivel_antes_db = float(np.median(curva[mask_antes])) if np.any(mask_antes) else -120.0
    nivel_despues_db = (float(np.median(curva[mask_despues]))
                        if np.any(mask_despues) else -140.0)
    salto_db = nivel_antes_db - nivel_despues_db

    muro_brickwall = (
        (peor_pendiente < -28.0 and peor_pendiente < pendiente_base * 2.1
         and salto_db > 15.0)
        or (salto_db > 21.0 and nivel_despues_db < -125.0)
    )

    # DSD64/128 to PCM: rising noise-shaping ramp between 24 and 40 kHz. It is
    # checked before the mirror because an independent rising ultrasonic ramp can
    # look correlated with the musical band after detrending.
    es_dsd = False
    mask_dsd = (freqs >= 24000.0) & (freqs <= 40000.0)
    if (sample_rate % 44100 == 0 and np.count_nonzero(mask_dsd) > 8):
        y = curva[mask_dsd]
        x = freqs[mask_dsd]
        pendiente = float(np.polyfit(x, y, 1)[0] * 1000.0)
        subida = float(y[-1] - y[0])
        monotona = float(np.mean(np.diff(y) >= 0.0)) >= 0.6
        es_dsd = bool(pendiente > 0.8 and subida > 12.0 and monotona)

    # Spectral mirror around the CD Nyquist (unfiltered interpolation). The fine
    # 250 Hz curve keeps the texture the mirror copies; the coarse curve is only
    # for slopes and levels.
    bins_fino = max(3, int(round(250.0 / df)))
    curva_fina = np.convolve(espectro,
                             np.ones(bins_fino, dtype=np.float32) / bins_fino,
                             mode="same")
    deltas = np.linspace(1000.0, 7000.0, 48)
    banda_inf = np.interp(22050.0 - deltas, freqs, curva_fina)
    banda_sup = np.interp(22050.0 + deltas, freqs, curva_fina)
    if (np.std(banda_inf) > 1.5 and np.std(banda_sup) > 1.5
            and np.mean(banda_sup) > -115.0):
        # The interpolator's anti-imaging response tilts the mirror copy, so a
        # raw Pearson coefficient can be negative even for a perfect mirror;
        # the shape is what matters. Detrending both bands keeps r > 0.82 for a
        # mirror and low for genuine independent ultrasonic noise.
        def _sin_tendencia(valores):
            t = np.arange(valores.size, dtype=np.float64)
            return valores - np.polyval(np.polyfit(t, valores, 1), t)
        corr_espejo = float(np.corrcoef(_sin_tendencia(banda_inf),
                                        _sin_tendencia(banda_sup))[0, 1])
    else:
        corr_espejo = 0.0
    espejo_aliasing = bool(corr_espejo > 0.82 and salto_db < 18.0 and not es_dsd)

    rolloff_analogico = bool(
        not muro_brickwall
        and not espejo_aliasing
        and not es_dsd
        and peor_pendiente > -17.5
        and salto_db < 14.5
        and nivel_despues_db > -124.0
    )

    salida.update({
        "muro_brickwall_nyquist":   bool(muro_brickwall),
        "espejo_aliasing_detectado": espejo_aliasing,
        "rolloff_analogico_genuino": rolloff_analogico,
        "gradiente_nyquist_db_khz": round(abs(peor_pendiente), 2),
        "salto_ultrasonico_db":     round(salto_db, 2),
        "correlacion_espejo_22k":   round(corr_espejo, 3),
        "es_transferencia_dsd":     es_dsd,
    })
    return salida

def _analizar_asimetria_mid_side(espectro_mid_db, espectro_side_db, freqs_hz,
                                 sample_rate, es_mono_real):
    """Vector 1: spectral Mid/Side asymmetry (joint-stereo lossy detector).

    In a genuine lossless stereo recording, mastering filters act on L and R
    equally, so Mid (L+R) and Side (L-R) reach the same ceiling. A lossy codec in
    joint-stereo mode (MP3 320/V0, AAC, Vorbis) quantises the Side channel more
    aggressively above 15 kHz: Side collapses and cuts before Mid.

    Returns caida_side_agudos_db, delta_corte_ms_hz, sospecha_joint_stereo and
    es_dual_mono (L and R essentially identical, > 45 dB of Mid-Side offset).
    Pure mono is not dual-mono: there is no Side channel at all."""
    salida = {
        "caida_side_agudos_db": 0.0,
        "delta_corte_ms_hz":    0.0,
        "sospecha_joint_stereo": False,
        "es_dual_mono":         False,
    }
    if es_mono_real or sample_rate < 44100:
        return salida

    freqs = np.asarray(freqs_hz, dtype=np.float64)
    mid = np.asarray(espectro_mid_db, dtype=np.float64)
    side = np.asarray(espectro_side_db, dtype=np.float64)
    if freqs.size < 2 or mid.shape != freqs.shape or side.shape != freqs.shape:
        return salida
    if not (np.all(np.isfinite(mid)) and np.all(np.isfinite(side))):
        return salida

    # Dual mono: in a stereo container the Side channel is essentially empty.
    diferencia = mid - side
    if np.all(np.isfinite(diferencia)):
        salida["es_dual_mono"] = bool(float(np.median(diferencia)) > 45.0)

    mask_media = (freqs >= 2000.0) & (freqs <= 8000.0)
    mask_aguda = (freqs >= 15500.0) & (freqs <= 19800.0)
    if not np.any(mask_media) or not np.any(mask_aguda):
        return salida

    diff_media_db = float(np.median(diferencia[mask_media]))
    diff_aguda_db = float(np.median(diferencia[mask_aguda]))
    colapso = diff_aguda_db - diff_media_db

    mask_alta = (freqs >= 13000.0) & (freqs <= 21000.0)
    mask_pivote = (freqs >= 6000.0) & (freqs <= 10000.0)
    if not np.any(mask_alta) or not np.any(mask_pivote):
        return salida
    f_alta = freqs[mask_alta]

    umbral_mid = max(float(np.percentile(mid[mask_pivote], 75)) - 38.0, -92.0)
    umbral_side = max(float(np.percentile(side[mask_pivote], 75)) - 38.0, -95.0)
    activos_mid = np.flatnonzero(mid[mask_alta] > umbral_mid)
    activos_side = np.flatnonzero(side[mask_alta] > umbral_side)
    techo_mid_hz = float(f_alta[activos_mid[-1]]) if activos_mid.size else 13000.0
    techo_side_hz = float(f_alta[activos_side[-1]]) if activos_side.size else 13000.0
    delta_corte = max(0.0, techo_mid_hz - techo_side_hz)

    sospecha = (
        not salida["es_dual_mono"]
        and diff_media_db < 14.0
        and techo_mid_hz >= 17500.0
        and ((colapso > 11.5 and delta_corte > 1100.0) or colapso > 16.5)
    )

    salida["caida_side_agudos_db"] = round(colapso, 2) if math.isfinite(colapso) else 0.0
    salida["delta_corte_ms_hz"] = round(delta_corte, 1) if math.isfinite(delta_corte) else 0.0
    salida["sospecha_joint_stereo"] = bool(sospecha)
    return salida

def _metricas_espectrales(freqs, pot_l, pot_r, samplerate, es_stereo=None, potencia=None,
                          pot_mid=None, pot_side=None, ganancia_ruido=1.0):
    """Core of the spectral analysis: everything is derived from the
    time-averaged power spectrum (one entry per bin). It does not depend on how
    it was obtained.

    es_stereo is passed explicitly: in mono, pot_l and pot_r can be distinct
    arrays holding the same values, so identity is not a usable hint.
    potencia is the total power spectrum; the Mid/Side decomposition provides it
    directly (|M|^2 + |S|^2 = (|L|^2 + |R|^2) / 2)."""
    if es_stereo is None:
        es_stereo = pot_l is not pot_r
    if potencia is None:
        potencia = (pot_l + pot_r) / 2
    pot_db    = 10.0 * np.log10(np.maximum(potencia, 1e-28))
    pot_db_n  = pot_db - np.max(pot_db)

    mask_act = pot_db_n > -60
    if np.any(mask_act):
        # Last bin above -60 dB, located from the end in C without copying freqs.
        ultimo_idx = len(freqs) - 1 - int(np.argmax(mask_act[::-1]))
        techo_hz   = float(freqs[ultimo_idx])
    else:
        techo_hz   = 0.0

    corte_hz = None
    mask_s = (freqs >= 12000) & (freqs <= 25000)
    if np.count_nonzero(mask_s) > 10:
        pr   = pot_db_n[mask_s]
        fr   = freqs[mask_s]
        n_pr = len(pr)
        paso = max(1, n_pr // 20)
        if n_pr > 2 * paso:
            # O(n) equivalent of the sliding-window loop via cumulative sums: the
            # two means per index become two vectorised subtractions.
            cs = np.empty(n_pr + 1, dtype=np.float64)
            cs[0] = 0.0
            np.cumsum(pr, dtype=np.float64, out=cs[1:])
            inv_paso = 1.0 / paso
            antes   = (cs[paso : n_pr - paso] - cs[0 : n_pr - 2 * paso]) * inv_paso
            despues = (cs[2 * paso : n_pr]    - cs[paso : n_pr - paso]) * inv_paso
            cond    = ((antes - despues) > 25.0) & (despues < -45.0)
            if np.any(cond):
                corte_hz = float(fr[int(np.argmax(cond)) + paso])

    # Block 5: the distance to Nyquist decides whether the step is a codec
    # signature or a mastering filter. A legitimate anti-alias filter lives right
    # at the Nyquist; only a cut-off below 88% of Nyquist is flagged.
    nyquist = samplerate / 2.0
    if corte_hz is None:
        clase_corte = "no_cutoff"
        distancia   = None
    else:
        distancia   = corte_hz / nyquist
        clase_corte = ("near_nyquist_cutoff" if distancia >= 0.88 else "far_cutoff")

    mask_a = freqs > 18000
    mask_m = (freqs > 15000) & (freqs < 18000)
    # Empty-mask guards: at 32 kHz (Nyquist 16 kHz) the high band does not exist
    # and a mean of an empty slice returned NaN -> invalid JSON.
    hay_a = bool(np.any(mask_a))
    ratio_aplica = bool(hay_a and np.any(mask_m))
    if ratio_aplica:
        ratio = _media_segura(potencia[mask_a]) / (_media_segura(potencia[mask_m]) + 1e-30)
    else:
        ratio = 0.0
    var_alta = float(np.var(pot_db_n[mask_a])) if hay_a else 0

    sep = None
    if es_stereo and ratio_aplica:
        da  = _media_segura(np.abs(pot_l[mask_a] - pot_r[mask_a]))
        dm  = _media_segura(np.abs(pot_l[mask_m] - pot_r[mask_m]))
        sep = float(da / (dm + 1e-30))

    # Vector 1: Mid/Side asymmetry. Without the decomposition (direct API use)
    # the detector is not applicable: never guess it from L/R, which would flag
    # an ordinary stereo difference as joint-stereo collapse.
    if pot_mid is None or pot_side is None:
        asimetria = _analizar_asimetria_mid_side(
            _espectro_dbfs(pot_l, ganancia_ruido), _espectro_dbfs(pot_r, ganancia_ruido),
            freqs, samplerate, es_mono_real=True)
    else:
        mid_db = _espectro_dbfs(pot_mid, ganancia_ruido)
        side_db = _espectro_dbfs(pot_side, ganancia_ruido)
        asimetria = _analizar_asimetria_mid_side(
            mid_db, side_db, freqs, samplerate, es_mono_real=not es_stereo)

    # Vector 3: Hi-Res morphology, measured on the Mid spectrum calibrated to
    # dBFS per bin so the absolute thermal-floor thresholds are meaningful.
    if pot_mid is None or pot_side is None:
        espectro_mid_db = _espectro_dbfs(pot_mid if pot_mid is not None else pot_l,
                                         ganancia_ruido)
    else:
        espectro_mid_db = mid_db
    hires = _diagnosticar_autenticidad_hires(freqs, espectro_mid_db, samplerate)

    return {
        "error":              None,
        "techo_hz":           round(techo_hz),
        "techo_rel_nyquist":  round(techo_hz / nyquist, 4) if nyquist else 0.0,
        "clase_corte":        clase_corte,
        "corte_detectado":    corte_hz is not None,
        "frecuencia_corte":   round(corte_hz) if corte_hz else None,
        "distancia_nyquist":  round(distancia, 4) if distancia is not None else None,
        # Compatibility: "corte_artificial" now means "confirmed accusation"
        # (a cut-off clearly below the Nyquist). It used to be enabled by any
        # step, including the filter of a master.
        "corte_artificial":   clase_corte == "far_cutoff",
        "ratio":              ratio,
        "ratio_aplica":       ratio_aplica,
        "var_alta_db":        var_alta,
        "separacion_stereo":  sep,
        # Vector 1 (additive keys: old consumers ignore them).
        "caida_side_agudos_db": asimetria["caida_side_agudos_db"],
        "delta_corte_ms_hz":    asimetria["delta_corte_ms_hz"],
        "sospecha_joint_stereo": asimetria["sospecha_joint_stereo"],
        "es_dual_mono":         asimetria["es_dual_mono"],
        # Vector 3 (additive keys: old consumers ignore them).
        "muro_brickwall_nyquist":   hires["muro_brickwall_nyquist"],
        "espejo_aliasing_detectado": hires["espejo_aliasing_detectado"],
        "rolloff_analogico_genuino": hires["rolloff_analogico_genuino"],
        "gradiente_nyquist_db_khz": hires["gradiente_nyquist_db_khz"],
        "salto_ultrasonico_db":     hires["salto_ultrasonico_db"],
        "correlacion_espejo_22k":   hires["correlacion_espejo_22k"],
        "es_transferencia_dsd":     hires["es_transferencia_dsd"],
        "nyquist":            samplerate // 2,
        "espectrograma":      None,
    }

def analizar_espectro(data, samplerate, modo, segundos=None, guardar_png=False, ruta=None):
    """Spectral analysis over an already loaded array (direct API and oracle of
    the tests). The production path uses _AnalisisPorBloques, which does not load
    the track. Both paths share the selector and the float32 STFT, so they must
    give exactly the same metrics.

    `modo` and `segundos` are kept in the signature for API/cache compatibility;
    the selector already covers the whole file, so they do not narrow the window."""
    try:
        es_stereo = data.shape[1] >= 2
        acumulador = _AcumuladorEspectral(
            samplerate, es_stereo,
            guardar_png=bool(guardar_png) and MATPLOTLIB_OK and bool(ruta),
            ruta=ruta)
        for inicio in range(0, len(data), FRAMES_POR_BLOQUE):
            acumulador.anadir(data[inicio : inicio + FRAMES_POR_BLOQUE], inicio)
        return acumulador.resultado()
    except Exception as e:
        return {"error": str(e)}

def _rachas(en, cuenta_previa=0):
    """Lengths of the runs of True, vectorised and without losing the ones that
    cross the boundary between blocks. Returns (rachas_cerradas, cuenta_abierta_final)."""
    if not en.any():
        return ([cuenta_previa] if cuenta_previa else []), 0
    flags   = en.astype(np.int8)
    bordes  = np.diff(np.concatenate(([0], flags, [0])))
    inicios = np.flatnonzero(bordes == 1)
    largos  = (np.flatnonzero(bordes == -1) - inicios).tolist()
    if inicios[0] == 0 and cuenta_previa:
        largos[0] += cuenta_previa          # continues the run of the previous block
    if flags[-1]:
        return largos[:-1], largos[-1]      # the last one is still open
    return largos, 0

def detectar_clipping(data):
    """Real clipping = 3+ CONSECUTIVE samples at the top (>= 0.9999). A clipping
    run is counted once; isolated inter-sample peaks are not clipping."""
    try:
        amp     = np.max(np.abs(data), axis=1) if data.ndim > 1 else np.abs(data)
        rachas, abierta = _rachas(amp >= 0.9999)
        if abierta:
            rachas.append(abierta)
        runs = sum(1 for largo in rachas if largo >= 3)
        return {"runs_clip": runs, "hay_clipping": runs > 0}
    except (ValueError, TypeError) as e:
        print(f"[clipping] {e}")
        return None

def _dr_desde_rms(rms_list, pico, n_bloques):
    """Dynamic range formula in O(n) via np.partition, shared by the array path and
    the block path. It accepts a Python list or an ndarray alike."""
    if n_bloques < 2 or pico <= 0:          # at least 6 seconds are needed
        return None
    arr     = np.asarray(rms_list, dtype=np.float64)
    validos = arr[arr > 1e-6]
    if validos.size == 0:
        return None
    k       = max(1, validos.size // 5)
    rms_top = float(np.mean(np.partition(validos, -k)[-k:]))
    return round(20 * np.log10(pico / (rms_top + 1e-30)), 1)

def calcular_dr(data, sr):
    """Estimated DR over an already loaded array. It is not the DR of the
    reference meter: it averages the channels to mono and applies neither its
    calibration nor its gating, so it is not comparable with published values."""
    try:
        mono   = np.mean(data, axis=1) if data.ndim > 1 else np.asarray(data)
        bloque = sr * 3
        n      = len(mono) // bloque
        if n < 2:
            return None
        trozos = mono[: n * bloque].reshape(n, bloque)
        rms    = np.sqrt(np.einsum("ij,ij->i", trozos, trozos) / bloque)
        pico   = float(np.max(np.abs(mono)))
        return _dr_desde_rms(rms.tolist(), pico, n)
    except (ValueError, TypeError) as e:
        print(f"[dr] {e}")
        return None

# ─── SCORING ─────────────────────────────────────────────────────────────────
# Model (Blocks 2/4/5): only POSITIVE evidence that there is no damage scores;
# every confirmed defect SUBTRACTS and BLOCKS the maximum verdict, because a file
# with a transcoding signature cannot be "GENUINE LOSSLESS".

# Band limit of a CD (Nyquist of 44.1 kHz). It is the absolute reference for
# judging the content of a hi-res file: above this there is ultrasonic content
# that a CD cannot have, and below it there is only CD band.
LIMITE_CD_HZ = 22050
#
# What no longer gives points away for free:
#   - The spectral ceiling at 44.1/48 kHz. A 320 kbps MP3 and a CD master with a
#     20 kHz anti-alias filter are spectrally identical (measured: ceiling 91%
#     of Nyquist, ratio 0.42, variance ~700 dB² in both), so the ceiling
#     distinguishes nothing: it is reported, it is not rewarded.
#   - The LSB utilisation: it can only refute (Block 4).
#   - The mere presence of the MD5: it is a fact of its own, not a vote.

def calcular_score(meta, esp, bdi):
    votos   = 0.0
    max_v   = 0.0
    resta   = 0.0
    bloquea = False
    problemas = []

    sr      = meta["sample_rate"]
    bits    = meta["bits_per_sample"]
    nyquist = sr // 2
    es_hires = sr >= 88200

    def penaliza(texto, cuanto, bloquea_maximo=True):
        nonlocal resta, bloquea
        problemas.append(texto)
        resta += cuanto
        bloquea = bloquea or bloquea_maximo

    if not esp.get("error"):
        techo     = esp["techo_hz"]
        clase     = esp.get("clase_corte", "no_cutoff")
        rel_corte = esp.get("distancia_nyquist")
        rel_techo = esp.get("techo_rel_nyquist")
        if rel_techo is None:
            rel_techo = techo / nyquist if nyquist else 0.0
        nyq_txt = f"{nyquist/1000:.1f} kHz"

        # 1. Content above the CD band: it can only be demanded of a file that
        #    claims to be hi-res. At 44.1/48 kHz it is physically impossible.
        if es_hires:
            max_v += 3
            if esp.get("rolloff_analogico_genuino") or esp.get("es_transferencia_dsd"):
                votos += 3
                problemas.append("hi-res morphology consistent with an analogue/DSD "
                                 "master (smooth roll-off and continuous ultrasonic floor)")
            elif techo >= LIMITE_CD_HZ:
                votos += 3
            elif techo >= 18000:
                votos += 1.5
                problemas.append(f"partial hi-res content: ceiling of {techo/1000:.1f} kHz "
                                 f"({rel_techo*100:.0f}% of Nyquist of {nyq_txt})")
            else:
                penaliza(f"reclama {sr/1000:.1f} kHz but the ceiling is {techo/1000:.1f} kHz, "
                         f"below the CD band: upscaled from 44.1/48 kHz", 0.40)

        # 2. High band. The reference depends on the sample rate:
        #    - Up to 48 kHz it is measured as a proportion of Nyquist: the ceiling
        #      of a healthy CD sits at 88-100 % of it, and below 70 % the band is
        #      taken to be destroyed.
        #    - Above 48 kHz a proportion of Nyquist means nothing: 70 % of 48 kHz
        #      is 33.6 kHz, where real acoustic music has no energy. A 96/24 master
        #      with the natural roll-off of its converters at 30 kHz has its
        #      ceiling at ~26 kHz and is legitimate. The reference becomes the CD
        #      limit: content above 22.05 kHz is real ultrasonic content and is NOT
        #      penalised.
        max_v += 3
        if sr > 48000:
            if techo >= LIMITE_CD_HZ:
                votos += 3
            elif esp.get("rolloff_analogico_genuino") or esp.get("es_transferencia_dsd"):
                votos += 3
                problemas.append("hi-res ceiling explained by an analogue/DSD master: "
                                 "the declared band is backed by the morphology")
            else:
                penaliza(f"spectral ceiling {techo/1000:.1f} kHz: there is no content above "
                         f"the CD limit ({LIMITE_CD_HZ/1000:.2f} kHz), so "
                         f"the band of {sr/1000:.1f} kHz is not backed by content", 0.40)
        elif rel_techo >= 0.88:
            votos += 3
        elif rel_techo >= 0.70:
            votos += 1.5
            problemas.append(f"spectral ceiling {techo/1000:.1f} kHz "
                             f"({rel_techo*100:.0f}% of Nyquist of {nyq_txt}): reduced high band")
        else:
            penaliza(f"spectral ceiling {techo/1000:.1f} kHz ({rel_techo*100:.0f}% of Nyquist "
                     f"of {nyq_txt}): the high band is destroyed", 0.40)

        # 3b. Vector 3: morphology that no legitimate master produces.
        if esp.get("muro_brickwall_nyquist"):
            penaliza(f"Hi-Res container with a brickwall at the CD/DAT Nyquist "
                     f"({esp.get('gradiente_nyquist_db_khz', 0.0):.1f} dB/kHz, "
                     f"{esp.get('salto_ultrasonico_db', 0.0):.1f} dB into the digital void): "
                     f"upscaled from a 44.1/48 kHz source", 0.40)
        if esp.get("espejo_aliasing_detectado"):
            penaliza(f"ultrasonic band mirrors the content below 22.05 kHz "
                     f"(r = {esp.get('correlacion_espejo_22k', 0.0):.2f}): "
                     f"unfiltered-interpolator upscale (spectral aliasing)", 0.40)
        if esp.get("es_transferencia_dsd"):
            problemas.append("DSD/SACD noise-shaping profile detected in 24-40 kHz "
                             "(legitimate hi-res transfer)")

        # 3. Cut-off: it is only flagged if it is clearly below the Nyquist.
        max_v += 3
        if clase == "far_cutoff":
            if esp.get("es_transferencia_dsd") or esp.get("rolloff_analogico_genuino"):
                problemas.append(f"band edge at {esp['frecuencia_corte']/1000:.1f} kHz "
                                 f"({rel_corte*100:.0f}% of Nyquist): the musical band ends "
                                 f"there, consistent with an analogue/DSD master")
            else:
                penaliza(f"artificial cut-off at {esp['frecuencia_corte']/1000:.1f} kHz "
                         f"({rel_corte*100:.0f}% of Nyquist of {nyq_txt}): lossy codec signature",
                         0.40)
        elif clase == "near_nyquist_cutoff":
            # It is neither flagged nor rewarded: the anti-alias filter of a
            # legitimate master lives here too. It is reported as inconclusive.
            problemas.append(f"band cut at {esp['frecuencia_corte']/1000:.1f} kHz "
                             f"({rel_corte*100:.0f}% of Nyquist): consistent with the anti-alias "
                             f"filter of a legitimate master AND with a high-rate codec "
                             f"— inconclusive")
        else:
            votos += 3

        # 4. High-band metrics (not applicable if the file has no high band).
        if esp.get("ratio_aplica"):
            max_v += 2
            r = esp["ratio"]
            if r > 0.05:   votos += 2
            elif r > 0.01: votos += 1
            else:          problemas.append(f"high/mid ratio very low ({r:.5f})")

            max_v += 1
            if esp.get("var_alta_db", 0) > 3:
                votos += 1

        sep = esp.get("separacion_stereo")
        if sep is not None:
            max_v += 1
            if sep > 0.1:
                votos += 1

        # 4b. Vector 1 (Mid/Side) and Vector 2 (cut-off jitter + MDCT holes).
        # Only ADD penalties when the signature is present: existing keys, votes
        # and verdicts of files without it stay exactly as they were.
        if esp.get("es_dual_mono"):
            problemas.append("dual-mono (L and R identical): stereo separation is "
                             "not informative")
        if esp.get("sospecha_joint_stereo"):
            penaliza(f"the Side channel (L-R) collapses at high frequency "
                     f"(Mid-Side drop {esp.get('caida_side_agudos_db', 0.0):.1f} dB, "
                     f"cut-off delta {esp.get('delta_corte_ms_hz', 0.0):.0f} Hz): "
                     f"joint-stereo lossy codec signature", 0.30)
        iqr = esp.get("iqr_corte_hz") or 0.0
        if iqr > 550.0:
            penaliza(f"the instantaneous cut-off jitters (IQR {iqr:.0f} Hz): "
                     f"frame-adaptive codec signature", 0.20, bloquea_maximo=False)
        huecos = esp.get("ratio_huecos_mdct") or 0.0
        if huecos > 0.10:
            penaliza(f"MDCT spectral holes in {huecos*100:.0f}% of the high-energy "
                     f"frames: lossy codec signature", 0.25, bloquea_maximo=False)

        # 5. Format/content coherence: claiming 24 bits at a CD sample rate
        #    demands some content above the CD band. It is the only signal that
        #    separates a 320 kbps MP3 repackaged as 24 bits from a real 16-bit
        #    master.
        if bits > 16 and sr <= 48000 and (rel_techo < 0.95 or clase == "far_cutoff"):
            corte_txt = (f"cut at {esp['frecuencia_corte']/1000:.1f} kHz"
                         if esp.get("frecuencia_corte") else
                         f"limited to {rel_techo*100:.0f}% of Nyquist "
                         f"({techo/1000:.1f} kHz)")
            penaliza(f"declara {bits} bits a {sr/1000:.1f} kHz but its high band is "
                     f"{corte_txt}: the declared resolution is not backed by the "
                     f"content (consistent with a repackaged high-rate transcode)",
                     0.25)

    # 6. LSB utilisation: it ONLY penalises (Block 4).
    if bdi.get("aplica"):
        lsbs = bdi.get("lsbs")
        if lsbs == "empty":
            penaliza(f"the LSBs are empty ({bdi.get('fraccion_16bit_grid','?')}% of samples "
                     f"on the 16-bit grid): effective resolution of "
                     f"{bdi.get('resolucion_efectiva_bits','?')} bits in a "
                     f"{bits} bits — bit-depth upscale", 0.40)
        elif lsbs == "partial":
            penaliza(f"LSBs partially inactive ({bdi.get('fraccion_16bit_grid','?')}% on the "
                     f"16-bit grid): upscale with later processing", 0.15,
                     bloquea_maximo=False)
        elif lsbs == "indeterminate" and bdi.get("error"):
            problemas.append(f"could not evaluate the LSB utilization: {bdi['error']}")

        # Vector 4: native-integer audit (zero padding and quantisation comb).
        forense = bdi.get("forense_bits") or {}
        if forense.get("es_padded_16_en_24"):
            problemas.append(f"Vector 4: every sample has "
                             f"{forense.get('trailing_zeros_lsb', 0)} trailing zero bits "
                             f"(16-bit zero-padded into 24 bits)")
            if lsbs != "empty":
                penaliza(f"the 8 low bits are always zero in the whole file: "
                         f"16-bit content zero-padded into a {bits}-bit container", 0.40)
        if forense.get("es_24bit_escalado_en_peine"):
            penaliza(f"24-bit container with a quantisation comb in the 8 LSBs "
                     f"(entropy {forense.get('lsb8_entropia_bits')} bits, "
                     f"{forense.get('lsb8_cajones_ocupados')}/256 bins occupied): "
                     f"a 16-bit source scaled into 24 bits", 0.40)

    # MD5: it is NO longer scored. Its check is reported as a fact of its own in
    # resultado["md5"] (verificar_md5), and an MD5 that does not match is a hard error.

    if max_v == 0:
        return 0.0, "indeterminate", problemas

    score = max(0.0, votos / max_v - resta)

    if bloquea:
        # Block 2: a confirmed defect does not coexist with a "genuine" verdict.
        veredicto = "SUSPICIOUS" if score >= 0.50 else "PROBABLE UPSCALE"
    elif score >= 0.80:   veredicto = "GENUINE LOSSLESS"
    elif score >= 0.60:   veredicto = "PROBABLY LOSSLESS"
    elif score >= 0.40:   veredicto = "SUSPICIOUS"
    else:                 veredicto = "PROBABLE UPSCALE"
    return score, veredicto, problemas

# ─── SINGLE STREAMING PASS ───────────────────────────────────────────────────
# Everything that needs to see the whole file (MD5, clipping, dynamic range, the
# sample for the LSB test and the spectrum) is computed in ONE single read, in
# blocks and with bounded memory. Previously the whole track was loaded as
# float64 and the whole STFT was built: a 10-minute 24/96 asked for ~2.8 GB.

FRAMES_POR_BLOQUE = 1 << 18            # 262 144 frames (~6 s at 44.1 kHz) per read
SEGUNDOS_ESPECTRO = 45                 # minimum seconds of active audio selected for the STFT
MAX_MUESTRAS_BITS = 1 << 20            # sample for the LSB test
MAX_FRAMES_PNG    = 1500

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

UMBRAL_RMS_ACTIVO_DB = -65.0      # below this a block is silence for the selector

def _bloques_activos_necesarios(samplerate):
    """K blocks, chosen so that the selected audio is at least SEGUNDOS_ESPECTRO
    long. With FRAMES_POR_BLOQUE fixed, K grows with the sample rate."""
    return max(1, int(math.ceil(SEGUNDOS_ESPECTRO * float(samplerate) / FRAMES_POR_BLOQUE)))

def _puntuacion_actividad(bloque, ultima_muestra=None):
    """(RMS in dBFS, E_diff) of a block. E_diff = mean((x[t]-x[t-1])^2) is a
    cheap high-frequency/transient indicator: it is large on cymbals and attacks
    and almost zero on a sustained bass line. The last sample of the previous
    block is carried over so the difference is not lost at the boundary."""
    if bloque.ndim > 1:
        n_ch = bloque.shape[1]
        if n_ch == 1:
            mono = bloque[:, 0]
        else:
            mono = bloque.mean(axis=1)
    else:
        mono = bloque
    if mono.size == 0:
        return -math.inf, 0.0

    sumsq = float(np.dot(mono, mono))
    if sumsq > 0.0:
        rms_db = 10.0 * math.log10(sumsq / mono.size)
    else:
        rms_db = -math.inf

    if ultima_muestra is None:
        diferencias = np.diff(mono)
    else:
        diferencias = np.diff(np.concatenate(([ultima_muestra], mono)))
    if diferencias.size:
        e_diff = float(np.dot(diferencias, diferencias) / diferencias.size)
    else:
        e_diff = 0.0
    return rms_db, e_diff

def _stft_complejo(canal, nperseg, hop, ventana, escala):
    """float32 STFT with sliding_window_view: no copies beyond the windowed
    frames, and the result stays in single precision (half the RAM of float64)."""
    x = np.asarray(canal, dtype=np.float32)
    marcos = np.lib.stride_tricks.sliding_window_view(x, nperseg)[::hop]
    marcos = marcos * ventana
    espectro = np.fft.rfft(marcos, axis=1)
    espectro *= escala
    return espectro

def _potencia_desde_fft(espectro):
    """|Z|^2 without np.sqrt, with the same 1e-28 floor used for the dB scale."""
    potencia = espectro.real ** 2 + espectro.imag ** 2
    np.maximum(potencia, 1e-28, out=potencia)
    return potencia

class AcumuladorPsicoacusticoSTFT:
    """Vector 2: temporal jitter of the cut-off and MDCT spectral holes.

    An LTI mastering filter cuts at the same frequency in every frame; a lossy
    codec decides per frame, so its instantaneous cut-off wanders (high IQR) and
    it zeroes whole 600 Hz sub-bands that look like rectangular holes. Only
    frames with real energy in 4-11 kHz are judged: silence has no cut-off to
    measure.

    The power spectra come from `_stft_complejo`, already normalised by the
    window sum; the unit-variance white-noise gain is removed so the -52 dBFS
    gate is independent of nperseg."""

    UMBRAL_BANDA_DBFS = -52.0
    INICIO_SUBBANDA_HZ = 13500.0
    ANCHO_SUBBANDA_HZ = 600.0
    N_SUBBANDAS = 10
    CAIDA_HUECO_DB = 22.0
    MIN_TRAMAS_FC = 8

    def __init__(self, samplerate):
        self.sr = int(samplerate)
        self._fc = []
        self._tramas = 0
        self._tramas_con_hueco = 0
        self._error = None

    def anadir(self, potencia, freqs, ventana):
        """Consumes the (frames, bins) power of one selected block."""
        if self._error or potencia.shape[0] == 0:
            return
        try:
            n = ventana.size
            suma_w = float(np.sum(ventana, dtype=np.float64))
            if suma_w <= 0.0:
                return
            # Mean bin power of unit-variance white noise with this window/FFT.
            ganancia = _ganancia_ruido_blanco(n, ventana)
            mask_banda = (freqs >= 4000.0) & (freqs <= 11000.0)
            if not np.any(mask_banda) or ganancia <= 0.0:
                return

            banda = (potencia[:, mask_banda].mean(axis=1, dtype=np.float64)
                     / ganancia)
            activas = banda > 10.0 ** (self.UMBRAL_BANDA_DBFS / 10.0)
            if not np.any(activas):
                return
            pot = potencia[activas]
            self._tramas += int(pot.shape[0])

            # Instantaneous cut-off: last bin above -60 dB relative to the frame
            # peak (same criterion as the global ceiling).
            pot_db = 10.0 * np.log10(np.maximum(pot, 1e-28))
            pot_db -= pot_db.max(axis=1, keepdims=True)
            sobre = pot_db > -60.0
            ultimo = pot.shape[1] - 1 - np.argmax(sobre[:, ::-1], axis=1)
            self._fc.extend(freqs[ultimo].tolist())

            # MDCT holes: 10 x 600 Hz sub-bands in 13.5-19.5 kHz.
            if self.sr < 39000:
                return                     # the band does not exist
            subbandas = []
            for i in range(self.N_SUBBANDAS):
                lo = self.INICIO_SUBBANDA_HZ + i * self.ANCHO_SUBBANDA_HZ
                hi = lo + self.ANCHO_SUBBANDA_HZ
                mask = (freqs >= lo) & (freqs < hi)
                if not np.any(mask):
                    return
                subbandas.append(10.0 * np.log10(
                    np.maximum(pot[:, mask].mean(axis=1), 1e-28)))
            sb = np.column_stack(subbandas)
            hueco = np.zeros(sb.shape[0], dtype=bool)
            for i in range(1, self.N_SUBBANDAS - 1):
                hueco |= ((sb[:, i] < sb[:, i - 1] - self.CAIDA_HUECO_DB)
                          & (sb[:, i] < sb[:, i + 1] - self.CAIDA_HUECO_DB))
            self._tramas_con_hueco += int(np.count_nonzero(hueco))
        except Exception as e:             # never abort the analysis of a file
            self._error = str(e)

    def resultado(self):
        fc = np.asarray(self._fc, dtype=np.float64)
        if fc.size >= self.MIN_TRAMAS_FC and np.all(np.isfinite(fc)):
            iqr = float(np.percentile(fc, 75) - np.percentile(fc, 25))
        else:
            iqr = 0.0
        ratio = (self._tramas_con_hueco / float(self._tramas)
                 if self._tramas else 0.0)
        return {
            "iqr_corte_hz":      round(iqr, 1) if math.isfinite(iqr) else 0.0,
            "ratio_huecos_mdct": round(ratio, 4) if math.isfinite(ratio) else 0.0,
        }

class _AcumuladorEspectral:
    """Keeps the K most active blocks in memory and runs the STFT on them at the
    end. Shared by the streaming path and the array path so both give the same
    metrics. Blocks are never concatenated: a frame must not cross the splice
    between two non-adjacent blocks, which would invent high-frequency content."""

    def __init__(self, samplerate, es_stereo=None, guardar_png=False, ruta=None):
        self.sr = int(samplerate)
        self.es_stereo = es_stereo
        self.guardar_png = bool(guardar_png)
        self.ruta = ruta
        self._k = _bloques_activos_necesarios(self.sr)
        self._seleccion = []          # min-heap by (E_diff, -index)
        self._respaldo = None         # loudest block, used if none passes -65 dBFS
        self._indice = 0
        self._ultima = None
        self._error = None
        self._psico = AcumuladorPsicoacusticoSTFT(self.sr)

    def anadir(self, bloque, inicio=0):
        if self._error or bloque.shape[0] == 0:
            return
        try:
            rms_db, e_diff = _puntuacion_actividad(bloque, self._ultima)
            if bloque.ndim > 1:
                self._ultima = float(np.mean(bloque[-1]))
                if self.es_stereo is None:
                    self.es_stereo = bloque.shape[1] >= 2
            else:
                self._ultima = float(bloque[-1])
                self.es_stereo = False

            audio = np.asarray(bloque, dtype=np.float32)
            indice = self._indice
            self._indice += 1
            entrada = (e_diff, -indice, indice, int(inicio), audio)

            if self._respaldo is None or rms_db > self._respaldo[0]:
                self._respaldo = (rms_db, -indice, indice, int(inicio), audio)

            if rms_db <= UMBRAL_RMS_ACTIVO_DB:
                return                    # silence: never a candidate
            if len(self._seleccion) < self._k:
                heapq.heappush(self._seleccion, entrada)
            elif entrada[:2] > self._seleccion[0][:2]:
                heapq.heapreplace(self._seleccion, entrada)
        except Exception as e:            # never abort the analysis of the file
            self._error = str(e)

    def resultado(self):
        if self._error:
            return {"error": self._error}
        elegidos = self._seleccion or ([self._respaldo] if self._respaldo else [])
        if not elegidos:
            return {"error": "could not analyze the spectrum"}
        elegidos = sorted(elegidos, key=lambda entrada: entrada[2])   # chronological

        largos = [len(entrada[4]) for entrada in elegidos]
        total_muestras = sum(largos)
        nperseg = max(256, min(4096, total_muestras // 8))
        nperseg = min(nperseg, max(largos))
        nperseg = max(32, nperseg)
        hop = max(1, nperseg // 8)      # 87.5 % overlap: enough time detail for the PNG

        ventana = np.hanning(nperseg + 1)[:-1].astype(np.float32)   # periodic Hann
        escala = np.float32(1.0 / float(ventana.sum()))
        ganancia_ruido = _ganancia_ruido_blanco(nperseg, ventana)
        frames_estimados = sum((n - nperseg) // hop + 1
                               for n in largos if n >= nperseg)
        if frames_estimados <= 0:
            return {"error": "could not analyze the spectrum"}
        paso_png = max(1, frames_estimados // MAX_FRAMES_PNG)
        # Display-only cap: a 75-87 % overlap gives far more frames than the PNG
        # can show, and that per-column detail is random noise that JPEG cannot
        # compress (it used to bloat the PDF). One displayed frame per 1/25 s
        # keeps the image faithful and the PDF small; metrics still average all
        # frames, so no evidence is lost.
        paso_png = max(paso_png,
                       int(round(self.sr / (hop * float(FPS_ESPECTROGRAMA_PNG)))))

        suma_l = suma_r = suma_total = None
        suma_mid = suma_side = None
        freqs = np.fft.rfftfreq(nperseg, 1.0 / self.sr)
        frames = 0
        frames_global = 0
        png_pot, png_times, png_n = [], [], 0

        for _, _, _, inicio, audio in elegidos:
            if len(audio) < nperseg:
                continue                  # a very short trailing block
            zl = _stft_complejo(audio[:, 0], nperseg, hop, ventana, escala)
            pot_l = _potencia_desde_fft(zl)
            if self.es_stereo:
                zr = _stft_complejo(audio[:, 1], nperseg, hop, ventana, escala)
                pot_r = _potencia_desde_fft(zr)
                medio = np.float32(0.5)
                zm = (zl + zr) * medio
                zs = (zl - zr) * medio
                pot_mid = _potencia_desde_fft(zm)
                pot_side = _potencia_desde_fft(zs)
                pot_total = pot_mid + pot_side
                del zr, zm, zs
            else:
                pot_r = pot_l
                pot_total = pot_l
            del zl

            suma_l = (pot_l.sum(axis=0, dtype=np.float32) if suma_l is None
                      else suma_l + pot_l.sum(axis=0, dtype=np.float32))
            if self.es_stereo:
                suma_r = (pot_r.sum(axis=0, dtype=np.float32) if suma_r is None
                          else suma_r + pot_r.sum(axis=0, dtype=np.float32))
                suma_total = (pot_total.sum(axis=0, dtype=np.float32)
                              if suma_total is None
                              else suma_total + pot_total.sum(axis=0, dtype=np.float32))
                suma_mid = (pot_mid.sum(axis=0, dtype=np.float32) if suma_mid is None
                            else suma_mid + pot_mid.sum(axis=0, dtype=np.float32))
                suma_side = (pot_side.sum(axis=0, dtype=np.float32)
                             if suma_side is None
                             else suma_side + pot_side.sum(axis=0, dtype=np.float32))
            else:
                suma_r = suma_l
                suma_total = suma_l

            self._psico.anadir(pot_total, freqs, ventana)

            n_frames = pot_total.shape[0]
            if self.guardar_png:
                restantes = MAX_FRAMES_PNG - png_n
                if restantes > 0:
                    i0 = (-frames_global) % paso_png
                    idx = np.arange(i0, n_frames, paso_png)[:restantes]
                    if idx.size:
                        # pot_total is (frames, bins): keep the time rows here and
                        # transpose once, so the PNG gets (bins, frames).
                        png_pot.append(pot_total[idx])
                        png_times.append((inicio + idx * hop) / float(self.sr))
                        png_n += idx.size
            frames_global += n_frames
            frames += n_frames
            del pot_l, pot_r, pot_total
            if self.es_stereo:
                del pot_mid, pot_side

        if frames == 0 or suma_l is None:
            return {"error": "could not analyze the spectrum"}

        mean_l = suma_l / np.float32(frames)
        mean_r = mean_l if not self.es_stereo else suma_r / np.float32(frames)
        mean_total = suma_total / np.float32(frames)
        mean_mid = (suma_mid / np.float32(frames)) if self.es_stereo else None
        mean_side = (suma_side / np.float32(frames)) if self.es_stereo else None
        esp = _metricas_espectrales(freqs, mean_l, mean_r, self.sr,
                                    es_stereo=self.es_stereo,
                                    potencia=mean_total,
                                    pot_mid=mean_mid, pot_side=mean_side,
                                    ganancia_ruido=ganancia_ruido)
        esp.update(self._psico.resultado())
        if self.guardar_png and png_pot:
            ruta_png, error = _guardar_espectrograma(
                freqs, np.concatenate(png_times),
                np.concatenate(png_pot, axis=0).T,
                self.sr, self.ruta, esp["techo_hz"], esp["frecuencia_corte"])
            esp["espectrograma"] = ruta_png
            if error:
                esp["espectrograma_error"] = error
        return esp

class AcumuladorForenseBits:
    """Vector 4: 24-bit container audit on the native integer samples.

    Detects two different fakes:
      * zero padding (16-in-24): the eight low bits are always zero, so an OR of
        every |sample| has at least 8 trailing zeros;
      * 16 bits scaled into 24 (gain/ReplayGain/DSP): the low bits are no longer
        zero, but their histogram is a quantisation comb, with Shannon entropy
        below 7.45 bits or fewer than 235 of the 256 bins occupied.

    Only samples between -48 and -90 dBFS are used for the histogram: there the
    low bits carry no musical signal and a genuine 24-bit/dithered master spreads
    them uniformly."""

    MIN_MUESTRAS_BAJAS = 256

    def __init__(self, bits_per_sample):
        self.bits = int(bits_per_sample)
        self.max_val = float(1 << (self.bits - 1))
        self._or = 0
        self._hist = np.zeros(256, dtype=np.int64)
        self._muestras = 0
        self._muestras_bajas = 0

    def anadir(self, bloque):
        if self.bits != 24 or bloque.shape[0] == 0:
            return
        q = np.rint(np.asarray(bloque, dtype=np.float64) * self.max_val)
        q = np.clip(q, -self.max_val, self.max_val - 1.0).astype(np.int64)
        self._muestras += int(q.size)
        self._or |= int(np.bitwise_or.reduce(np.abs(q).ravel()))

        absq = np.abs(q)
        bajo = self.max_val * 10.0 ** (-90.0 / 20.0)
        alto = self.max_val * 10.0 ** (-48.0 / 20.0)
        seleccion = absq[(absq >= bajo) & (absq <= alto)]
        if seleccion.size:
            residuos = (seleccion & 0xFF).astype(np.uint8)
            self._hist += np.bincount(residuos, minlength=256)
            self._muestras_bajas += int(residuos.size)

    def resultado(self):
        if self.bits != 24 or self._muestras == 0:
            return {"aplica": False}

        valor_or = int(self._or)
        if valor_or == 0:                      # digital silence: no grid to judge
            ceros = self.bits
        else:
            ceros = (valor_or & -valor_or).bit_length() - 1
        padded = bool(valor_or and ceros >= 8)

        total = int(self._hist.sum())
        if total:
            p = self._hist[self._hist > 0] / float(total)
            entropia = float(-np.sum(p * np.log2(p)))
            ocupados = int(np.count_nonzero(self._hist))
        else:
            entropia, ocupados = None, 0

        peine = bool(
            not padded
            and total >= self.MIN_MUESTRAS_BAJAS
            and (entropia < 7.45 or ocupados < 235)
        )
        return {
            "aplica":                    True,
            "es_padded_16_en_24":        padded,
            "trailing_zeros_lsb":        int(ceros),
            "lsb8_entropia_bits":        round(entropia, 3) if entropia is not None else None,
            "lsb8_cajones_ocupados":     ocupados,
            "muestras_pasaje_bajo":      int(total),
            "es_24bit_escalado_en_peine": peine,
        }

def _analizar_forense_bits(data, meta):
    """Array-path counterpart of AcumuladorForenseBits (used by the oracle and
    by direct API callers). It walks the same FRAMES_POR_BLOQUE blocks."""
    bits = meta.get("bits_per_sample")
    if bits != 24:
        return {"aplica": False}
    acumulador = AcumuladorForenseBits(bits)
    for inicio in range(0, len(data), FRAMES_POR_BLOQUE):
        acumulador.anadir(data[inicio : inicio + FRAMES_POR_BLOQUE])
    return acumulador.resultado()

class _AnalisisPorBloques:
    """Accumulates in a single pass the analyses that need the whole file."""

    def __init__(self, meta, sr, total_frames, modo, segundos,
                 guardar_png=False, ruta=None):
        self.meta  = meta
        self.sr    = int(sr)
        self.bits  = meta["bits_per_sample"]
        self.total = int(total_frames) if total_frames and total_frames > 0 else (1 << 62)
        self.ruta  = ruta
        self.guardar_png = bool(guardar_png) and MATPLOTLIB_OK and bool(ruta)

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
        self._forense_bits = (AcumuladorForenseBits(self.bits)
                              if self.bits == 24 else None)

        # spectrum: the whole file is scored, the K most active blocks are kept
        self._espectro_acc = _AcumuladorEspectral(
            self.sr, meta.get("canales", 0) >= 2 if meta.get("canales") else None,
            guardar_png=self.guardar_png, ruta=ruta)

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
        return self._espectro_acc.resultado()

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

def _sanear(obj):
    """Replaces any non-finite float with None before serialising.
    NaN/Infinity are not valid JSON: JavaScript's JSON.parse rejects them."""
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: _sanear(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sanear(v) for v in obj]
    return obj

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

NOMBRE_CARPETA_PNG = "_spectrograms"

def _carpeta_png(ruta_audio):
    return os.path.join(os.path.dirname(os.path.abspath(ruta_audio)), NOMBRE_CARPETA_PNG)

def _ruta_cache(ruta_audio, modo, segundos):
    """One entry per file AND per mode: analysing in 'center' does not erase the
    cache of the analysis in 'full', so the two can be alternated without
    recomputing."""
    clave = f"{modo}{segundos if segundos else ''}"
    return os.path.join(_carpeta_png(ruta_audio), "_cache",
                        f"{os.path.basename(ruta_audio)}.{clave}.json")

def _identidad(ruta, meta):
    """Cheap and robust fingerprint of the file: size, date and STREAMINFO MD5."""
    try:
        info = os.stat(_normalizar_ruta_fs(ruta))
        return {"tamano": info.st_size, "mtime_ns": info.st_mtime_ns,
                "md5": meta.get("md5") if isinstance(meta.get("md5"), int) else None}
    except OSError:
        return None

def _clave_analisis(modo, segundos):
    return {"motor": VERSION_MOTOR, "esquema": VERSION_ESQUEMA_CACHE,
            "modo": modo, "seconds": segundos}

def leer_cache(ruta, meta, modo, segundos, requiere_png):
    """Returns the cached result, or None if it cannot be reused."""
    try:
        with open(_normalizar_ruta_fs(_ruta_cache(ruta, modo, segundos)),
                  encoding="utf-8") as f:
            entrada = json.load(f)
    except (OSError, ValueError):
        return None                    # no cache, or an unreadable entry

    if entrada.get("identidad") != _identidad(ruta, meta):
        return None
    if entrada.get("clave") != _clave_analisis(modo, segundos):
        return None
    resultado = entrada.get("resultado")
    if not isinstance(resultado, dict):
        return None

    if requiere_png:
        # The PNG must exist and match the current parameters.
        ruta_png = (resultado.get("esp") or {}).get("espectrograma")
        if entrada.get("render") != huella_render() or not ruta_png:
            return None
        if not os.path.exists(ruta_png) or os.path.getsize(ruta_png) == 0:
            return None
        return resultado

    # No PNG was requested: the caller must not see a path that this run has not
    # generated (the cache entry does keep it for next time).
    copia = dict(resultado)
    copia["esp"] = dict(resultado.get("esp") or {})
    copia["esp"]["espectrograma"] = None
    copia["esp"].pop("espectrograma_error", None)
    return copia

def _reemplazar_con_reintentos(origen, destino, intentos=3, espera=0.015):
    """os.replace with a short retry: on Windows, another worker or the antivirus
    indexer can hold the destination open for a few milliseconds and os.replace
    then raises PermissionError. The retry does not hide the failure: after the
    last attempt the exception is raised and the caller decides."""
    for intento in range(intentos):
        try:
            os.replace(origen, destino)
            return
        except PermissionError:
            if intento == intentos - 1:
                raise
            time.sleep(espera)

def escribir_cache(ruta, meta, modo, segundos, resultado):
    """Stores the result. A failure while writing cannot affect the analysis."""
    try:
        carpeta = _normalizar_ruta_fs(os.path.join(_carpeta_png(ruta), "_cache"))
        os.makedirs(carpeta, exist_ok=True)
        entrada = {
            "identidad": _identidad(ruta, meta),
            "clave":     _clave_analisis(modo, segundos),
            "render":    huella_render(),
            "resultado": resultado,
        }
        destino = _normalizar_ruta_fs(_ruta_cache(ruta, modo, segundos))
        temporal = destino + ".tmp"
        with open(temporal, "w", encoding="utf-8") as f:
            json.dump(_sanear(entrada), f, ensure_ascii=False)
        _reemplazar_con_reintentos(temporal, destino)  # atomic, with a short retry
    except Exception:
        pass

# ─── PARALLELISATION (Block 8) ───────────────────────────────────────────────
# Each track is independent, so the tracks are spread across processes. PROCESSES
# are used and not threads because matplotlib is not thread-safe and because
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
    interpreter (numpy, scipy, matplotlib, soundfile), which is wasteful to repeat
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
        for _, ruta, cacheado in plan:
            if cacheado is not None:
                yield cacheado
                continue
            resultado = next(calculados, None) if calculados is not None else None
            if resultado is None:
                yield {"archivo": os.path.basename(ruta), "ruta": ruta,
                       "error": "could not get the analysis result"}
            else:
                yield resultado

    return en_orden(), workers_efectivos

# ─── ANALYSIS OF ONE FILE (returns a dict, does not print) ───────────────────

def analizar_archivo_datos(ruta, modo, segundos, guardar_png, usar_cache=True):
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
