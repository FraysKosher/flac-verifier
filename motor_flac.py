import hashlib
import json
import math
import os
import sys
import tempfile
import numpy as np
import soundfile as sf
from scipy.signal import stft
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
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    MATPLOTLIB_OK = True
except ImportError:
    MATPLOTLIB_OK = False

# ─── BASIC CHECKS ─────────────────────────────────────────────────────────────

# Raised whenever any analysis calculation changes: it invalidates the caches.
# 8.1: the high band of a hi-res file is judged against the CD limit and not
#      against a proportion of Nyquist (this fixed a false "PROBABLE UPSCALE"
#      on 96/24 masters with an analogue roll-off).
VERSION_MOTOR = "9.0"

# Spectrogram presentation (affects only the PNG, never the metrics).
FIGURA_ESPECTROGRAMA = (14, 5)      # inches
DPI_ESPECTROGRAMA    = 150

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

def verificar_firma(ruta):
    try:
        with open(ruta, "rb") as f:
            return f.read(4) == b"fLaC"
    except (FileNotFoundError, PermissionError) as e:
        return False

def verificar_metadatos(ruta):
    try:
        audio = FLAC(ruta)
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
        data, sr = sf.read(ruta, dtype="float64", always_2d=True)
        return data, sr
    except Exception:
        return None, None

# ─── INTEGRITY CHECK (STREAMINFO MD5) ────────────────────────────────────────
# The MD5 is not an opinion: it is the only objective check of the format. It is
# computed over the decoded PCM (signed integers, interleaved by channel, in
# little-endian, ceil(bits/8) bytes per sample) and compared with the stored one.
# It is reported as a FACT of its own ("md5"), never as a scoring vote.

_ANCHO_POR_BITS = {8: 1, 16: 2, 24: 3, 32: 4}

def _pcm_bytes(bloque, bits_per_sample, ancho):
    """Little-endian interleaved PCM of a block, exactly as the format hashes it.

    float64 -> exact integer: the soundfile conversion is m / 2**(bits-1)."""
    escala = float(1 << (bits_per_sample - 1))
    q = np.round(bloque * escala).astype("<i8")
    return q.view(np.uint8).reshape(-1, 8)[:, :ancho].tobytes()

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

def analizar_bit_depth(data, meta):
    """LSB utilisation of the container (Block 4).

    This analysis CAN ONLY REFUTE: empty low bits, or low bits aligned to the
    16-bit grid, betray a container deeper than its content. It never proves
    authenticity — a lossy transcode stored in 24 bits also fills the LSBs, and a
    renormalisation breaks the alignment. That is why the result is reported as a
    fact and can only penalise in the scoring.
    """
    bits = meta["bits_per_sample"]
    if bits <= 16:
        return {"aplica": False}
    try:
        # All channels, not just the left one: a silent channel or a file with
        # misaligned channels biased the previous test.
        muestras = np.ascontiguousarray(data).reshape(-1)
        activas  = muestras[np.abs(muestras) > 0.001]
        if activas.size < 1000:
            return {"aplica": True, "lsbs": "indeterminate",
                    "conclusion": "indeterminate: too little signal to measure LSB utilization"}

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
        return {
            "aplica":                   True,
            "fraccion_16bit_grid":      round(fraccion * 100, 1),
            "resolucion_efectiva_bits": int(efectiva),
            "lsbs":                     lsbs,
            "conclusion":               conclusion,
        }
    except Exception as e:
        return {"aplica": True, "lsbs": "indeterminate", "error": str(e)}

def _guardar_espectrograma(freqs, times, mezcla, samplerate, ruta_audio, techo_hz, frecuencia_corte):
    """Draws the spectrogram. Returns (ruta_png, error): it never raises, and the
    reason for the failure is kept so that it can be reported."""
    try:
        carpeta        = os.path.dirname(ruta_audio)
        nombre         = os.path.splitext(os.path.basename(ruta_audio))[0]
        directorio_png = os.path.join(carpeta, "_spectrograms")
        try:
            os.makedirs(directorio_png, exist_ok=True)
        except FileExistsError:
            pass                    # several processes may create it at once

        ruta_png = os.path.join(directorio_png, nombre + "_espectrograma.png")

        db  = 10 * np.log10(np.asarray(mezcla, dtype=np.float64) + 1e-30)
        db -= np.max(db)

        MAX_FRAMES = MAX_FRAMES_PNG
        if db.shape[1] > MAX_FRAMES:
            step  = max(1, db.shape[1] // MAX_FRAMES)
            db    = db[:, ::step]
            times = times[::step]

        fig, ax = plt.subplots(figsize=FIGURA_ESPECTROGRAMA, dpi=DPI_ESPECTROGRAMA)
        # imshow paints a raster: with a grid of ~2049x1500 cells it is ~10 times
        # faster than pcolormesh(shading="gouraud") and more faithful (gouraud
        # interpolated between cell centres, smoothing the result).
        img = ax.imshow(db, aspect="auto", origin="lower", cmap="inferno",
                        vmin=-80, vmax=0, interpolation="auto",
                        extent=[times[0], times[-1], freqs[0] / 1000, freqs[-1] / 1000])
        plt.colorbar(img, ax=ax, label="dB (relative to peak)")
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
        plt.tight_layout()
        plt.savefig(ruta_png, dpi=DPI_ESPECTROGRAMA)
        plt.close(fig)
        return ruta_png, None
    except Exception as e:
        try:
            plt.close("all")
        except Exception:
            pass
        return None, f"{type(e).__name__}: {e}"

def _metricas_espectrales(freqs, pot_l, pot_r, samplerate, es_stereo=None):
    """Core of the spectral analysis: everything is derived from the
    time-averaged power spectrum (one entry per bin). It does not depend on how
    it was obtained.

    es_stereo is passed explicitly: in mono, pot_l and pot_r can be distinct
    arrays holding the same values, so identity is not a usable hint."""
    if es_stereo is None:
        es_stereo = pot_l is not pot_r
    potencia  = (pot_l + pot_r) / 2
    pot_db    = 10 * np.log10(potencia + 1e-30)
    pot_db_n  = pot_db - np.max(pot_db)

    freqs_act = freqs[pot_db_n > -60]
    techo_hz  = float(freqs_act[-1]) if len(freqs_act) > 0 else 0

    corte_hz = None
    mask_s = (freqs >= 12000) & (freqs <= 25000)
    if np.sum(mask_s) > 10:
        pr   = pot_db_n[mask_s]
        fr   = freqs[mask_s]
        paso = max(1, len(pr) // 20)
        for i in range(paso, len(pr) - paso):
            antes   = _media_segura(pr[i - paso : i])
            despues = _media_segura(pr[i : i + paso])
            if (antes - despues) > 25 and despues < -45:
                corte_hz = float(fr[i])
                break

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
    ratio_aplica = bool(np.sum(mask_a) > 0 and np.sum(mask_m) > 0)
    if ratio_aplica:
        ratio = _media_segura(potencia[mask_a]) / (_media_segura(potencia[mask_m]) + 1e-30)
    else:
        ratio = 0.0
    var_alta = float(np.var(pot_db_n[mask_a])) if np.sum(mask_a) > 0 else 0

    sep = None
    if es_stereo and ratio_aplica:
        da  = _media_segura(np.abs(pot_l[mask_a] - pot_r[mask_a]))
        dm  = _media_segura(np.abs(pot_l[mask_m] - pot_r[mask_m]))
        sep = float(da / (dm + 1e-30))

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
        "nyquist":            samplerate // 2,
        "espectrograma":      None,
    }

def analizar_espectro(data, samplerate, modo, segundos=None, guardar_png=False, ruta=None):
    """Spectral analysis over an already loaded array (direct API and oracle of
    the tests). The production path uses _AnalisisPorBloques, which does not load
    the track."""
    try:
        es_stereo = data.shape[1] >= 2
        canal_l   = data[:, 0]
        canal_r   = data[:, 1] if es_stereo else data[:, 0]

        if modo == "seconds" and segundos:
            canal_l = canal_l[: samplerate * segundos]
            canal_r = canal_r[: samplerate * segundos]
        elif modo == "center":
            mitad   = len(canal_l) // 2
            n       = samplerate * 30
            canal_l = canal_l[mitad : mitad + n]
            canal_r = canal_r[mitad : mitad + n]

        nperseg = max(256, min(4096, len(canal_l) // 8))
        hop     = nperseg // 2
        freqs, times, Zxx_l = stft(canal_l, samplerate, nperseg=nperseg, noverlap=hop)
        pot_l_z = np.abs(Zxx_l) ** 2
        del Zxx_l
        if es_stereo:
            _, _, Zxx_r = stft(canal_r, samplerate, nperseg=nperseg, noverlap=hop)
            pot_r_z = np.abs(Zxx_r) ** 2
            del Zxx_r
        else:
            pot_r_z = pot_l_z                    # single FFT in mono

        esp = _metricas_espectrales(freqs, np.mean(pot_l_z, axis=1),
                                    np.mean(pot_r_z, axis=1), samplerate,
                                    es_stereo=es_stereo)
        if guardar_png and MATPLOTLIB_OK and ruta:
            ruta_png, error = _guardar_espectrograma(
                freqs, times, (pot_l_z + pot_r_z) / 2, samplerate, ruta,
                esp["techo_hz"], esp["frecuencia_corte"])
            esp["espectrograma"] = ruta_png
            if error:
                esp["espectrograma_error"] = error
        return esp
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
    """Dynamic range formula, shared by the array path and the block path."""
    if n_bloques < 2:                       # at least 6 seconds are needed
        return None
    validos = [r for r in rms_list if r > 1e-6]
    if not validos or pico <= 0:
        return None
    rms_top = float(np.mean(sorted(validos)[-max(1, len(validos) // 5):]))
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
            if techo >= LIMITE_CD_HZ:
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

        # 3. Cut-off: it is only flagged if it is clearly below the Nyquist.
        max_v += 3
        if clase == "far_cutoff":
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
SEGUNDOS_ESPECTRO = 45                 # size of the STFT pieces (30-60 s)
MAX_MUESTRAS_BITS = 1 << 20            # sample for the LSB test
MAX_FRAMES_PNG    = 1500

def _leer_bloques(ruta, frames_por_bloque=FRAMES_POR_BLOQUE):
    """Generator of blocks: it never loads the whole track into memory."""
    with sf.SoundFile(ruta) as f:
        while True:
            bloque = f.read(frames_por_bloque, dtype="float64", always_2d=True)
            if bloque.shape[0] == 0:
                return
            yield bloque

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

        # spectrum
        self.ini, self.fin = self._ventana(modo, segundos)
        largo      = max(1, self.fin - self.ini)
        self._nperseg = max(256, min(4096, largo // 8))
        self._hop     = max(1, self._nperseg // 2)
        self._pot_l   = None
        self._pot_r   = None
        self._freqs   = None
        self._es_stereo = None
        self._esp_frames = 0
        self._error_espectro = None
        # The samples of the window are gathered into pieces of SEGUNDOS_ESPECTRO
        # seconds before the STFT: fewer calls and bounded memory all the same.
        self._largo_pieza = max(self._nperseg, self.sr * SEGUNDOS_ESPECTRO)
        self._buf_espera  = []
        self._buf_espera_n = 0
        self._esp_consumidos = 0
        frames_est    = max(1, (largo - self._nperseg) // self._hop + 1)
        self._paso_png = max(1, frames_est // MAX_FRAMES_PNG)
        self._png_pot   = []
        self._png_times = []

    def _ventana(self, modo, segundos):
        if modo == "seconds" and segundos:
            return 0, min(self.total, self.sr * int(segundos))
        if modo == "center":
            ini = (self.total // 2)
            return ini, min(self.total, ini + self.sr * 30)
        return 0, self.total                       # "full" (or an unknown mode)

    def anadir(self, bloque, offset):
        self._integridad_y_clipping(bloque)
        self._rango_dinamico(bloque)
        self._muestra_bits(bloque, offset)
        self._espectro(bloque, offset)

    # ── per block ────────────────────────────────────────────────────────────
    def _integridad_y_clipping(self, bloque):
        if self._md5 is not None:
            self._md5.update(_pcm_bytes(bloque, self.bits, self._ancho_md5))
        amp = np.max(np.abs(bloque), axis=1) if bloque.ndim > 1 else np.abs(bloque)
        if amp.size:
            pico = float(np.max(amp))
            if pico > self._pico:
                self._pico = pico
        rachas, self._clip_abierta = _rachas(amp >= 0.9999, self._clip_abierta)
        self._clip_runs += sum(1 for largo in rachas if largo >= 3)

    def _rango_dinamico(self, bloque):
        mono = bloque.mean(axis=1) if bloque.ndim > 1 else bloque
        if mono.size:
            pico = float(np.max(np.abs(mono)))
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
        if self._muestras_n >= MAX_MUESTRAS_BITS:
            return
        if (offset // FRAMES_POR_BLOQUE) % self._paso_muestreo:
            return
        self._muestras.append(bloque)
        self._muestras_n += bloque.shape[0]

    def _espectro(self, bloque, offset):
        """Gathers the samples of the analysis window and processes them in pieces."""
        if self._error_espectro or self.fin <= self.ini:
            return
        a = max(0, self.ini - offset)
        b = min(bloque.shape[0], self.fin - offset)
        if b <= a:
            return
        self._buf_espera.append(bloque[a:b])
        self._buf_espera_n += b - a
        if self._buf_espera_n >= self._largo_pieza:
            self._vaciar_espectro()

    def _vaciar_espectro(self):
        if not self._buf_espera:
            return
        pieza = (self._buf_espera[0] if len(self._buf_espera) == 1
                 else np.concatenate(self._buf_espera))
        self._buf_espera = []
        self._buf_espera_n = 0
        if pieza.shape[0] < self._nperseg:
            return                              # piece too short for the STFT
        inicio = self.ini + self._esp_consumidos
        self._esp_consumidos += pieza.shape[0]
        try:
            # Each piece is contiguous signal: no frame crosses the boundary
            # between blocks, so no splice artefact slips in.
            freqs, _, Zl = stft(pieza[:, 0], self.sr, nperseg=self._nperseg,
                                noverlap=self._hop)
            pot_l = np.abs(Zl) ** 2
            del Zl
            es_stereo = pieza.shape[1] >= 2
            if es_stereo:
                _, _, Zr = stft(pieza[:, 1], self.sr, nperseg=self._nperseg,
                                noverlap=self._hop)
                pot_r = np.abs(Zr) ** 2
                del Zr
            else:
                pot_r = pot_l                   # single FFT in mono
            self._es_stereo = es_stereo

            if self.guardar_png:
                self._guardar_frames(pot_l, pot_r, inicio)
            if self._pot_l is None:
                self._freqs = freqs
                self._pot_l = np.sum(pot_l, axis=1)
                self._pot_r = np.sum(pot_r, axis=1)
            else:
                self._pot_l += np.sum(pot_l, axis=1)
                self._pot_r += np.sum(pot_r, axis=1)
            self._esp_frames += pot_l.shape[1]
            del pot_l, pot_r, pieza
        except Exception as e:
            self._error_espectro = str(e)

    def _guardar_frames(self, pot_l, pot_r, inicio_muestras):
        """Stores frames spread over the whole window (bounded memory)."""
        mezcla = (pot_l + pot_r) / 2
        base   = self._esp_frames
        for i in range(mezcla.shape[1]):
            if (base + i) % self._paso_png:
                continue
            if len(self._png_times) >= MAX_FRAMES_PNG:
                return
            self._png_pot.append(mezcla[:, i].astype(np.float32))
            self._png_times.append(
                (inicio_muestras + i * self._hop) / self.sr - self.ini / self.sr)

    # ── result ───────────────────────────────────────────────────────────────
    def resultado(self):
        self._vaciar_espectro()                 # remainder of the window still waiting
        if self._clip_abierta >= 3:
            self._clip_runs += 1                # run that ends exactly with the file
        salida = {
            "pico": self._pico,
            "md5":  _comparar_md5(self._md5.hexdigest() if self._md5 else None, self.meta),
            "clip": {"runs_clip": self._clip_runs, "hay_clipping": self._clip_runs > 0},
        }
        if self._dr_bloque:
            rms = [math.sqrt(s / self._dr_bloque) for s in self._dr_sumsq]
            salida["dr"] = _dr_desde_rms(rms, self._dr_pico, len(self._dr_sumsq))
        else:
            salida["dr"] = None

        if self._muestras:
            muestra = np.concatenate(self._muestras)
        else:
            muestra = np.zeros((0, 1))
        salida["bdi"] = analizar_bit_depth(muestra, self.meta)
        salida["esp"] = self._resultado_espectral()
        return salida

    def _resultado_espectral(self):
        if self._error_espectro:
            return {"error": self._error_espectro}
        if self._pot_l is None or not self._esp_frames:
            return {"error": "could not analyze the spectrum"}
        esp = _metricas_espectrales(self._freqs,
                                    self._pot_l / self._esp_frames,
                                    self._pot_r / self._esp_frames,
                                    self.sr, es_stereo=getattr(self, "_es_stereo", None))
        if self.guardar_png and self._png_pot:
            ruta_png, error = _guardar_espectrograma(
                self._freqs, np.asarray(self._png_times),
                np.stack(self._png_pot, axis=1), self.sr, self.ruta,
                esp["techo_hz"], esp["frecuencia_corte"])
            esp["espectrograma"] = ruta_png
            if error:
                esp["espectrograma_error"] = error
        return esp

def _analizar_en_streaming(ruta, meta, modo, segundos, guardar_png):
    """A single read of the file, with memory bounded per block."""
    with sf.SoundFile(ruta) as f:
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
        info = os.stat(ruta)
        return {"tamano": info.st_size, "mtime_ns": info.st_mtime_ns,
                "md5": meta.get("md5") if isinstance(meta.get("md5"), int) else None}
    except OSError:
        return None

def _clave_analisis(modo, segundos):
    return {"motor": VERSION_MOTOR, "modo": modo, "seconds": segundos}

def leer_cache(ruta, meta, modo, segundos, requiere_png):
    """Returns the cached result, or None if it cannot be reused."""
    try:
        with open(_ruta_cache(ruta, modo, segundos), encoding="utf-8") as f:
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

def escribir_cache(ruta, meta, modo, segundos, resultado):
    """Stores the result. A failure while writing cannot affect the analysis."""
    try:
        carpeta = os.path.join(_carpeta_png(ruta), "_cache")
        os.makedirs(carpeta, exist_ok=True)
        entrada = {
            "identidad": _identidad(ruta, meta),
            "clave":     _clave_analisis(modo, segundos),
            "render":    huella_render(),
            "resultado": resultado,
        }
        destino = _ruta_cache(ruta, modo, segundos)
        temporal = destino + ".tmp"
        with open(temporal, "w", encoding="utf-8") as f:
            json.dump(_sanear(entrada), f, ensure_ascii=False)
        os.replace(temporal, destino)      # atomic: it never leaves a half-written JSON
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

def _sonda_procesos():
    """Can processes be created in this environment?

    Some environments do not allow it (sandboxes that block Windows named pipes).
    In that case the analysis runs in series: slower, but correct.
    """
    try:
        from concurrent.futures import ProcessPoolExecutor
        with ProcessPoolExecutor(max_workers=1) as ejecutor:
            return ejecutor.submit(int, 1).result(timeout=120) == 1
    except Exception:
        return False

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
