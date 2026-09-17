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
    """Dónde guarda matplotlib su caché de fuentes (y por qué hay que decidirlo).

    Matplotlib construye esa caché la primera vez que dibuja y la reutiliza
    después. Si el directorio por defecto (`~/.matplotlib`) no se puede escribir
    —una instalación en una carpeta protegida, un perfil de usuario restringido, un
    entorno sin permiso de escritura— matplotlib avisa por stderr y vuelve a
    construirla en cada ejecución. Ese aviso es inocuo, pero aquí molesta: el
    motor habla con la GUI por stdout con líneas JSON, y un aviso suelto acaba en
    el registro de la ventana como línea ilegible.

    Se le da una carpeta propia en los datos del usuario y, si tampoco se puede,
    en el directorio temporal; si nada se puede crear, se deja el valor por
    defecto (`~/.matplotlib`), que es lo que haría matplotlib sin nosotros. Nunca
    es un error fatal: devuelve la carpeta elegida o None si no se tocó nada.
    """
    if os.environ.get("MPLCONFIGDIR"):
        return None                    # si el usuario lo configuró, manda él
    candidatas = []
    if os.environ.get("LOCALAPPDATA"):
        candidatas.append(os.path.join(os.environ["LOCALAPPDATA"], "FLAC_VERIFIER"))
    candidatas.append(os.path.join(tempfile.gettempdir(), "FLAC_VERIFIER"))
    for raiz in candidatas:
        carpeta = os.path.join(raiz, "matplotlib")
        try:
            os.makedirs(carpeta, exist_ok=True)
        except OSError:
            continue                   # sin permiso ahí: se prueba la siguiente
        os.environ["MPLCONFIGDIR"] = carpeta
        return carpeta
    return None


# Tiene que decidirse ANTES de importar matplotlib: la variable se lee al cargar.
preparar_cache_matplotlib()

try:
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    MATPLOTLIB_OK = True
except ImportError:
    MATPLOTLIB_OK = False

# ─── VERIFICACIONES BÁSICAS ───────────────────────────────────────────────────

# Súbela cuando cambie cualquier cálculo del análisis: invalida las cachés.
# 8.1: la banda alta de un hi-res se juzga contra el límite del CD y no contra
#      una proporción del Nyquist (corregía un falso "PROBABLE UPSCALE" en
#      masters 96/24 con roll-off analógico).
VERSION_MOTOR = "9.0"

# Presentación del espectrograma (afecta solo al PNG, nunca a las métricas).
FIGURA_ESPECTROGRAMA = (14, 5)      # pulgadas
DPI_ESPECTROGRAMA    = 150

def huella_render():
    """Parámetros que definen el PNG. Si cambia alguno, la caché del espectrograma
    deja de ser válida (el análisis sí se puede reaprovechar)."""
    return (f"v{VERSION_MOTOR}-f{MAX_FRAMES_PNG}-p{SEGUNDOS_ESPECTRO}-"
            f"{FIGURA_ESPECTROGRAMA[0]}x{FIGURA_ESPECTROGRAMA[1]}@{DPI_ESPECTROGRAMA}-imshow")

def forzar_utf8_salida():
    """Fuerza UTF-8 en stdout/stderr.

    En Windows, al redirigir la salida a un archivo o tubería, Python usa la
    codificación local (cp1252) y cualquier carácter no representable (los
    marcos '═', los emojis) provoca UnicodeEncodeError y mata el proceso."""
    for flujo in (sys.stdout, sys.stderr):
        try:
            flujo.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass          # flujo sin reconfigure (p. ej. sustituido en tests)

def _asegurar_flujos():
    """Deja stdout/stderr utilizables dentro de un ejecutable empaquetado.

    Un .exe de ventana (--noconsole) no tiene consola: PyInstaller deja
    `sys.stdout = None` y entonces `print` no escribe NADA, así que el protocolo
    NDJSON se perdería sin ningún error visible. Si el proceso sí tiene un
    descriptor válido (porque la GUI lo lanzó con una tubería), se reabre; si no,
    se manda a un agujero negro para que imprimir nunca sea un problema."""
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
    """Media de un array posiblemente vacío. Evita NaN (media de slice vacío),
    que rompía el protocolo JSON al serializarse como el literal inválido NaN."""
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
        # mutagen puede lanzar error/MutagenError/struct.error con archivos
        # truncados o corruptos. Antes escapaban y abortaban el lote entero.
        return {"valido": False, "error": f"{type(e).__name__}: {e}"}

def leer_audio(ruta):
    try:
        data, sr = sf.read(ruta, dtype="float64", always_2d=True)
        return data, sr
    except Exception:
        return None, None

# ─── VERIFICACIÓN DE INTEGRIDAD (MD5 del STREAMINFO) ─────────────────────────
# El MD5 no es una opinión: es el único chequeo objetivo del formato. Se calcula
# sobre el PCM decodificado (enteros con signo, intercalados por canal, en
# little-endian, ceil(bits/8) bytes por muestra) y se compara con el registrado.
# Se reporta como HECHO propio ("md5"), nunca como voto de scoring.

_ANCHO_POR_BITS = {8: 1, 16: 2, 24: 3, 32: 4}

def _pcm_bytes(bloque, bits_per_sample, ancho):
    """PCM intercalado little-endian de un bloque, tal como lo hashea el formato.

    float64 -> entero exacto: la conversión de soundfile es m / 2**(bits-1)."""
    escala = float(1 << (bits_per_sample - 1))
    q = np.round(bloque * escala).astype("<i8")
    return q.view(np.uint8).reshape(-1, 8)[:, :ancho].tobytes()

def _md5_pcm(data, bits_per_sample, frames_por_bloque=1 << 20):
    """MD5 del PCM decodificado de un array completo. None si no es verificable."""
    ancho = _ANCHO_POR_BITS.get(bits_per_sample)
    if ancho is None:
        return None
    md5 = hashlib.md5()
    for i in range(0, len(data), frames_por_bloque):
        md5.update(_pcm_bytes(data[i : i + frames_por_bloque], bits_per_sample, ancho))
    return md5.hexdigest()

def _comparar_md5(calculado, meta):
    """Compara un MD5 ya calculado con el registrado en el STREAMINFO."""
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
    """Compara el MD5 recalculado de un array con el del STREAMINFO. Hecho, no voto."""
    return _comparar_md5(_md5_pcm(data, meta.get("bits_per_sample")), meta)

# ─── ANÁLISIS ────────────────────────────────────────────────────────────────

def analizar_bit_depth(data, meta):
    """Utilización de los bits bajos del contenedor (Bloque 4).

    Este análisis SOLO PUEDE DESMENTIR: unos LSB vacíos o alineados a la rejilla
    de 16 bits delatan un contenedor más profundo que su contenido. Nunca
    demuestra autenticidad — un transcódigo con pérdida guardado en 24 bits
    también llena los LSB, y una renormalización rompe la alineación. Por eso el
    resultado se informa como hecho y en el scoring solo puede penalizar.
    """
    bits = meta["bits_per_sample"]
    if bits <= 16:
        return {"aplica": False}
    try:
        # Todos los canales, no solo el izquierdo: un canal silencioso o un
        # archivo con los canales desalineados sesgaban el test anterior.
        muestras = np.ascontiguousarray(data).reshape(-1)
        activas  = muestras[np.abs(muestras) > 0.001]
        if activas.size < 1000:
            return {"aplica": True, "lsbs": "indeterminate",
                    "conclusion": "indeterminate: too little signal to measure LSB utilization"}

        # 1) ¿Están las muestras en la rejilla de 16 bits? (test clásico de upscale)
        escalado = activas * 32768.0
        fraccion = float(np.mean(np.abs(escalado - np.round(escalado)) < 0.05))

        # 2) Resolución efectiva: bits declarados menos la moda de ceros finales
        #    de las muestras enteras (16 bits dentro de 24 -> 8 ceros -> 16 bits).
        q = np.round(activas * float(1 << (bits - 1))).astype(np.int64)
        q = q[q != 0]
        if q.size:
            pot     = np.abs(q) & -np.abs(q)                  # 2^(ceros finales)
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
    """Dibuja el espectrograma. Devuelve (ruta_png, error): nunca lanza, y el
    motivo del fallo se conserva para poder informarlo."""
    try:
        carpeta        = os.path.dirname(ruta_audio)
        nombre         = os.path.splitext(os.path.basename(ruta_audio))[0]
        directorio_png = os.path.join(carpeta, "_spectrograms")
        try:
            os.makedirs(directorio_png, exist_ok=True)
        except FileExistsError:
            pass                    # varios procesos pueden crearlo a la vez

        ruta_png = os.path.join(directorio_png, nombre + "_espectrograma.png")

        db  = 10 * np.log10(np.asarray(mezcla, dtype=np.float64) + 1e-30)
        db -= np.max(db)

        MAX_FRAMES = MAX_FRAMES_PNG
        if db.shape[1] > MAX_FRAMES:
            step  = max(1, db.shape[1] // MAX_FRAMES)
            db    = db[:, ::step]
            times = times[::step]

        fig, ax = plt.subplots(figsize=FIGURA_ESPECTROGRAMA, dpi=DPI_ESPECTROGRAMA)
        # imshow pinta un ráster: con una rejilla de ~2049x1500 celdas es ~10 veces
        # más rápido que pcolormesh(shading="gouraud") y más fiel (gouraud
        # interpolaba entre centros de celda, suavizando el resultado).
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
    """Núcleo del análisis espectral: todo se deriva del espectro de potencia
    medio en el tiempo (una entrada por bin). No depende de cómo se obtuvo.

    es_stereo se pasa explícito: en mono, pot_l y pot_r pueden ser arrays
    distintos con los mismos valores, así que la identidad no sirve de pista."""
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

    # Bloque 5: la distancia al Nyquist decide si el escalón es una firma de
    # códec o un filtro de masterización. Un anti-alias legítimo vive pegado al
    # Nyquist; solo un corte por debajo del 88% del Nyquist se acusa.
    nyquist = samplerate / 2.0
    if corte_hz is None:
        clase_corte = "no_cutoff"
        distancia   = None
    else:
        distancia   = corte_hz / nyquist
        clase_corte = ("near_nyquist_cutoff" if distancia >= 0.88 else "far_cutoff")

    mask_a = freqs > 18000
    mask_m = (freqs > 15000) & (freqs < 18000)
    # Guardas de máscara vacía: en 32 kHz (Nyquist 16 kHz) la banda alta no
    # existe y una media de slice vacío devolvía NaN -> JSON inválido.
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
        # Compatibilidad: "corte_artificial" ahora significa "acusación
        # confirmada" (corte claramente por debajo del Nyquist). Antes se
        # activaba con cualquier escalón, incluido el filtro de un master.
        "corte_artificial":   clase_corte == "far_cutoff",
        "ratio":              ratio,
        "ratio_aplica":       ratio_aplica,
        "var_alta_db":        var_alta,
        "separacion_stereo":  sep,
        "nyquist":            samplerate // 2,
        "espectrograma":      None,
    }

def analizar_espectro(data, samplerate, modo, segundos=None, guardar_png=False, ruta=None):
    """Análisis espectral sobre un array ya cargado (API directa y oráculo de los
    tests). El camino de producción usa _AnalisisPorBloques, que no carga la pista."""
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
            pot_r_z = pot_l_z                    # FFT única en mono

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
    """Longitudes de las rachas de True, vectorizado y sin perder las que cruzan
    la frontera entre bloques. Devuelve (rachas_cerradas, cuenta_abierta_final)."""
    if not en.any():
        return ([cuenta_previa] if cuenta_previa else []), 0
    flags   = en.astype(np.int8)
    bordes  = np.diff(np.concatenate(([0], flags, [0])))
    inicios = np.flatnonzero(bordes == 1)
    largos  = (np.flatnonzero(bordes == -1) - inicios).tolist()
    if inicios[0] == 0 and cuenta_previa:
        largos[0] += cuenta_previa          # continúa la racha del bloque anterior
    if flags[-1]:
        return largos[:-1], largos[-1]      # la última sigue abierta
    return largos, 0

def detectar_clipping(data):
    """Clipping real = 3+ muestras CONSECUTIVAS al tope (>= 0.9999). Un run se
    cuenta una sola vez; los picos inter-muestra aislados no son clipping."""
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
    """Fórmula del rango dinámico, compartida por la vía array y la de bloques."""
    if n_bloques < 2:                       # hace falta al menos 6 segundos
        return None
    validos = [r for r in rms_list if r > 1e-6]
    if not validos or pico <= 0:
        return None
    rms_top = float(np.mean(sorted(validos)[-max(1, len(validos) // 5):]))
    return round(20 * np.log10(pico / (rms_top + 1e-30)), 1)

def calcular_dr(data, sr):
    """DR estimado sobre un array ya cargado. No es el DR del medidor de
    referencia: promedia los canales a mono y no aplica su calibración ni su
    gating, así que no es comparable con valores publicados."""
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
# Modelo (Bloques 2/4/5): solo puntúa la evidencia POSITIVA de que no hay daño;
# todo defecto confirmado RESTA y BLOQUEA el veredicto máximo, porque un archivo
# con firma de transcodificación no puede ser "LOSSLESS GENUINO".

# Límite de la banda de un CD (Nyquist de 44.1 kHz). Es la referencia absoluta
# para juzgar el contenido de un archivo hi-res: por encima de esto hay contenido
# ultrasónico que un CD no puede tener, y por debajo solo hay banda de CD.
LIMITE_CD_HZ = 22050
#
# Lo que ya no regala puntos:
#   - El techo espectral a 44.1/48 kHz. Un MP3 de 320 kbps y un master de CD con
#     filtro anti-alias a 20 kHz son espectralmente idénticos (medido: techo 91%
#     del Nyquist, ratio 0.42, varianza ~700 dB² en ambos), así que el techo no
#     distingue nada: se informa, no se premia.
#   - La utilización de los LSB: solo puede desmentir (Bloque 4).
#   - La mera presencia del MD5: es un hecho aparte, no un voto.

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

        # 1. Contenido por encima de la banda de CD: solo se le puede exigir a un
        #    archivo que reclama hi-res. A 44.1/48 kHz es físicamente imposible.
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

        # 2. Banda alta. La referencia depende de la tasa de muestreo:
        #    - Hasta 48 kHz se mide en proporción al Nyquist: el techo de un CD
        #      sano está en el 88-100 % de él, y por debajo del 70 % la banda se
        #      da por destruida.
        #    - Por encima de 48 kHz una proporción del Nyquist no significa nada:
        #      el 70 % de 48 kHz son 33.6 kHz, donde la música acústica real no
        #      tiene energía. Un master 96/24 con el roll-off natural de sus
        #      convertidores a 30 kHz tiene el techo en ~26 kHz y es legítimo.
        #      La referencia pasa a ser el límite del CD: contenido por encima de
        #      22.05 kHz es contenido ultrasónico real y NO se penaliza.
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

        # 3. Corte: solo se acusa si está claramente por debajo del Nyquist.
        max_v += 3
        if clase == "far_cutoff":
            penaliza(f"artificial cut-off at {esp['frecuencia_corte']/1000:.1f} kHz "
                     f"({rel_corte*100:.0f}% of Nyquist of {nyq_txt}): lossy codec signature",
                     0.40)
        elif clase == "near_nyquist_cutoff":
            # No se acusa ni se premia: aquí también vive el filtro anti-alias de
            # un master legítimo. Se informa como no concluyente.
            problemas.append(f"band cut at {esp['frecuencia_corte']/1000:.1f} kHz "
                             f"({rel_corte*100:.0f}% of Nyquist): consistent with the anti-alias "
                             f"filter of a legitimate master AND with a high-rate codec "
                             f"— inconclusive")
        else:
            votos += 3

        # 4. Métricas de banda alta (no aplicables si el archivo no tiene banda alta).
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

        # 5. Coherencia formato/contenido: reclamar 24 bits a tasa de CD exige algo
        #    de contenido por encima de la banda de CD. Es la única señal que separa
        #    un MP3 de 320 kbps reempaquetado en 24 bits de un master real de 16 bits.
        if bits > 16 and sr <= 48000 and (rel_techo < 0.95 or clase == "far_cutoff"):
            corte_txt = (f"cut at {esp['frecuencia_corte']/1000:.1f} kHz"
                         if esp.get("frecuencia_corte") else
                         f"limited to {rel_techo*100:.0f}% of Nyquist "
                         f"({techo/1000:.1f} kHz)")
            penaliza(f"declara {bits} bits a {sr/1000:.1f} kHz but its high band is "
                     f"{corte_txt}: the declared resolution is not backed by the "
                     f"content (consistent with a repackaged high-rate transcode)",
                     0.25)

    # 6. Utilización de los bits bajos: SOLO penaliza (Bloque 4).
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

    # MD5: ya NO se puntúa. Su verificación se reporta como hecho propio en
    # resultado["md5"] (verificar_md5), y un MD5 que no coincide es error duro.

    if max_v == 0:
        return 0.0, "indeterminate", problemas

    score = max(0.0, votos / max_v - resta)

    if bloquea:
        # Bloque 2: un defecto confirmado no convive con un veredicto de "genuino".
        veredicto = "SUSPICIOUS" if score >= 0.50 else "PROBABLE UPSCALE"
    elif score >= 0.80:   veredicto = "GENUINE LOSSLESS"
    elif score >= 0.60:   veredicto = "PROBABLY LOSSLESS"
    elif score >= 0.40:   veredicto = "SUSPICIOUS"
    else:                 veredicto = "PROBABLE UPSCALE"
    return score, veredicto, problemas

# ─── PASADA ÚNICA EN STREAMING ───────────────────────────────────────────────
# Todo lo que necesita ver el archivo entero (MD5, clipping, rango dinámico,
# muestra para los bits bajos y espectro) se calcula en UNA sola lectura por
# bloques con memoria acotada. Antes se cargaba la pista completa en float64 y se
# construía la STFT entera: un 24/96 de 10 minutos pedía ~2.8 GB.

FRAMES_POR_BLOQUE = 1 << 18            # 262 144 tramas (~6 s a 44.1 kHz) de lectura
SEGUNDOS_ESPECTRO = 45                 # tamaño de las piezas de STFT (30-60 s)
MAX_MUESTRAS_BITS = 1 << 20            # muestra para el test de bits bajos
MAX_FRAMES_PNG    = 1500

def _leer_bloques(ruta, frames_por_bloque=FRAMES_POR_BLOQUE):
    """Generador de bloques: nunca carga la pista entera en memoria."""
    with sf.SoundFile(ruta) as f:
        while True:
            bloque = f.read(frames_por_bloque, dtype="float64", always_2d=True)
            if bloque.shape[0] == 0:
                return
            yield bloque

class _AnalisisPorBloques:
    """Acumula en una pasada los análisis que necesitan el archivo completo."""

    def __init__(self, meta, sr, total_frames, modo, segundos,
                 guardar_png=False, ruta=None):
        self.meta  = meta
        self.sr    = int(sr)
        self.bits  = meta["bits_per_sample"]
        self.total = int(total_frames) if total_frames and total_frames > 0 else (1 << 62)
        self.ruta  = ruta
        self.guardar_png = bool(guardar_png) and MATPLOTLIB_OK and bool(ruta)

        # MD5 (mismo PCM que verificar_md5, hasheado de forma incremental). Solo
        # se hashea si hay algo con lo que comparar: sin MD5 registrado o con una
        # profundidad no verificable, el resultado es "ausente"/"no_verificable"
        # y hashear sería un recorrido extra de todo el archivo para nada.
        self._ancho_md5 = _ANCHO_POR_BITS.get(self.bits)
        self._md5 = (hashlib.md5()
                     if self._ancho_md5 and meta.get("md5") else None)

        # clipping + pico global
        self._clip_runs   = 0
        self._clip_abierta = 0
        self._pico        = 0.0

        # rango dinámico (bloques de 3 s, arrastrando el bloque incompleto)
        self._dr_bloque   = self.sr * 3
        self._dr_sumsq    = []
        self._dr_pendiente = np.zeros(0)
        self._dr_pico     = 0.0

        # bits bajos: muestra repartida por todo el archivo
        self._muestras    = []
        self._muestras_n  = 0
        self._paso_muestreo = (max(1, int(round(self.total / MAX_MUESTRAS_BITS)))
                               if self.total < (1 << 62) else 1)

        # espectro
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
        # Las muestras de la ventana se juntan en piezas de SEGUNDOS_ESPECTRO
        # segundos antes de la STFT: menos llamadas y memoria acotada igual.
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
        return 0, self.total                       # "full" (o modo desconocido)

    def anadir(self, bloque, offset):
        self._integridad_y_clipping(bloque)
        self._rango_dinamico(bloque)
        self._muestra_bits(bloque, offset)
        self._espectro(bloque, offset)

    # ── por bloque ───────────────────────────────────────────────────────────
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
        """Junta las muestras de la ventana de análisis y las procesa por piezas."""
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
            return                              # trozo demasiado corto para la STFT
        inicio = self.ini + self._esp_consumidos
        self._esp_consumidos += pieza.shape[0]
        try:
            # Cada pieza es señal contigua: ninguna trama cruza la frontera entre
            # bloques, así que no se cuela ningún artefacto de empalme.
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
                pot_r = pot_l                   # FFT única en mono
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
        """Guarda tramas repartidas por toda la ventana (memoria acotada)."""
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

    # ── resultado ────────────────────────────────────────────────────────────
    def resultado(self):
        self._vaciar_espectro()                 # resto de la ventana que quedaba en espera
        if self._clip_abierta >= 3:
            self._clip_runs += 1                # racha que termina justo con el archivo
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
    """Una sola lectura del archivo, con memoria acotada por bloque."""
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
    """Sustituye por None cualquier float no finito antes de serializar.
    NaN/Infinity no son JSON válido: JSON.parse de JavaScript los rechaza."""
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: _sanear(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sanear(v) for v in obj]
    return obj

def _json_linea(obj):
    """Serializa una línea del protocolo garantizando JSON válido."""
    try:
        return json.dumps(_sanear(obj), default=str, allow_nan=False)
    except (TypeError, ValueError) as e:
        return json.dumps({"tipo": "error",
                           "mensaje": f"resultado no serializable: {type(e).__name__}: {e}"})

# ─── CACHÉ (Bloque 8) ────────────────────────────────────────────────────────
# Repetir el análisis de un álbum ya verificado no debería volver a decodificar
# ni a dibujar nada. Se guarda, por pista, un JSON junto a los espectrogramas con
# el resultado completo y la huella de lo que lo produjo:
#
#   identidad  = tamaño + fecha de modificación + MD5 del STREAMINFO
#   parámetros = versión del motor + modo + segundos
#   render     = huella de los parámetros del PNG
#
# Si la identidad o los parámetros cambian, la entrada no se usa: un archivo
# modificado se vuelve a analizar entero. Si solo cambia la huella del render,
# el resultado se reaprovecha pero el PNG se vuelve a dibujar.

NOMBRE_CARPETA_PNG = "_spectrograms"

def _carpeta_png(ruta_audio):
    return os.path.join(os.path.dirname(os.path.abspath(ruta_audio)), NOMBRE_CARPETA_PNG)

def _ruta_cache(ruta_audio, modo, segundos):
    """Una entrada por archivo Y por modo: analizar en 'centro' no borra la caché
    del análisis en 'completo', así se puede alternar sin recalcular."""
    clave = f"{modo}{segundos if segundos else ''}"
    return os.path.join(_carpeta_png(ruta_audio), "_cache",
                        f"{os.path.basename(ruta_audio)}.{clave}.json")

def _identidad(ruta, meta):
    """Huella barata y robusta del archivo: tamaño, fecha y MD5 del STREAMINFO."""
    try:
        info = os.stat(ruta)
        return {"tamano": info.st_size, "mtime_ns": info.st_mtime_ns,
                "md5": meta.get("md5") if isinstance(meta.get("md5"), int) else None}
    except OSError:
        return None

def _clave_analisis(modo, segundos):
    return {"motor": VERSION_MOTOR, "modo": modo, "seconds": segundos}

def leer_cache(ruta, meta, modo, segundos, requiere_png):
    """Devuelve el resultado cacheado, o None si no se puede reaprovechar."""
    try:
        with open(_ruta_cache(ruta, modo, segundos), encoding="utf-8") as f:
            entrada = json.load(f)
    except (OSError, ValueError):
        return None                    # sin caché o entrada ilegible

    if entrada.get("identidad") != _identidad(ruta, meta):
        return None
    if entrada.get("clave") != _clave_analisis(modo, segundos):
        return None
    resultado = entrada.get("resultado")
    if not isinstance(resultado, dict):
        return None

    if requiere_png:
        # El PNG debe existir y corresponder a los parámetros actuales.
        ruta_png = (resultado.get("esp") or {}).get("espectrograma")
        if entrada.get("render") != huella_render() or not ruta_png:
            return None
        if not os.path.exists(ruta_png) or os.path.getsize(ruta_png) == 0:
            return None
        return resultado

    # No se han pedido PNG: el llamador no debe ver una ruta que esta ejecución no
    # ha generado (la entrada de caché sí la conserva para la próxima vez).
    copia = dict(resultado)
    copia["esp"] = dict(resultado.get("esp") or {})
    copia["esp"]["espectrograma"] = None
    copia["esp"].pop("espectrograma_error", None)
    return copia

def escribir_cache(ruta, meta, modo, segundos, resultado):
    """Guarda el resultado. Un fallo al escribir no puede afectar al análisis."""
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
        os.replace(temporal, destino)      # atómico: nunca deja un JSON a medias
    except Exception:
        pass

# ─── PARALELIZACIÓN (Bloque 8) ───────────────────────────────────────────────
# Cada pista es independiente, así que se reparten entre procesos. Se usan
# PROCESOS y no hilos porque matplotlib no es thread-safe y porque numpy/scipy
# sueltan el GIL solo en parte del trabajo; cada proceso importa motor_flac por su
# cuenta y se queda con el backend Agg, sin compartir estado con los demás.

MIN_ARCHIVOS_PARALELO = 3              # por debajo, el coste de arrancar no compensa
MAX_WORKERS = 8                        # techo por defecto (memoria: ~0.3 GB/proceso)

def workers_por_defecto(n_archivos):
    nucleos = os.cpu_count() or 1
    return max(1, min(nucleos, MAX_WORKERS, n_archivos))

def _trabajo_analisis(tarea):
    """Trabajo de un archivo en un proceso hijo (debe ser de nivel de módulo)."""
    ruta, modo, segundos, guardar_png, usar_cache = tarea
    return analizar_archivo_datos(ruta, modo, segundos, guardar_png, usar_cache)

def _sonda_procesos():
    """¿Se pueden crear procesos en este entorno?

    Hay entornos que no lo permiten (sandboxes que bloquean las tuberías con
    nombre de Windows). En ese caso se analiza en serie: más lento, pero correcto.
    """
    try:
        from concurrent.futures import ProcessPoolExecutor
        with ProcessPoolExecutor(max_workers=1) as ejecutor:
            return ejecutor.submit(int, 1).result(timeout=120) == 1
    except Exception:
        return False

def analizar_archivos(archivos, modo, segundos, guardar_png, workers=None,
                      usar_cache=True):
    """Genera los resultados en orden. Devuelve (generador, workers_efectivos).

    Primero se resuelve de la caché lo que ya está calculado (leer un JSON es
    mucho más barato que analizar), y solo lo que falta se reparte entre
    procesos. Si un proceso hijo muere, ese archivo se reporta como error y el
    lote continúa."""
    # 1) Qué se puede reaprovechar sin analizar nada
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
        """Analiza en serie o en paralelo los archivos que no estaban en caché."""
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
                    # Un proceso caído (memoria, cuelgue) no puede tumbar el lote.
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

# ─── ANÁLISIS DE UN ARCHIVO (devuelve dict, no imprime) ──────────────────────

def analizar_archivo_datos(ruta, modo, segundos, guardar_png, usar_cache=True):
    """Analiza un archivo y devuelve un dict. Con `usar_cache`, si el archivo no
    ha cambiado desde el último análisis se reaprovecha el resultado guardado."""
    resultado = {"archivo": os.path.basename(ruta), "ruta": ruta}

    # Blindaje: cualquier fallo inesperado en un archivo se reporta como error de
    # ESE archivo. Antes una excepción escapaba, mataba el proceso y el consumidor
    # se quedaba sin la línea {"tipo":"fin"}.
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

        # Caché: un álbum ya verificado no se vuelve a decodificar ni a dibujar.
        if usar_cache:
            cacheado = leer_cache(ruta, meta, modo, segundos, requiere_png=guardar_png)
            if cacheado is not None:
                cacheado["desde_cache"] = True
                return cacheado

        # Una sola pasada, memoria acotada por bloque (Bloque 6). Un fallo de
        # decodificación aquí es un archivo truncado o corrupto.
        try:
            datos = _analizar_en_streaming(ruta, meta, modo, segundos, guardar_png)
        except Exception as e:
            resultado["error"] = f"Could not decode the audio ({type(e).__name__}: {e})"
            return resultado

        # Guarda de silencio (Bloque 2): sin señal no hay nada que analizar, y
        # cualquier veredicto sería inventado. Antes un archivo mudo salía
        # "PROBABLEMENTE LOSSLESS" porque su techo se medía como el Nyquist entero.
        if datos["pico"] < 1.0 / float(1 << max(0, meta["bits_per_sample"] - 1)):
            resultado["md5"] = datos["md5"]
            resultado["error"] = ("digital silence: the file contains no analyzable signal "
                                  "(no verdict is issued)")
            return resultado

        # Integridad: si el MD5 no cuadra el archivo está corrupto o su audio fue
        # alterado, así que no recibe veredicto ni score.
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
# Uso: python motor_flac.py --path "C:/Music/album" --mode center --png --pdf
# Protocolo: una línea JSON por evento
#   { tipo: "inicio"|"resultado"|"informe"|"aviso"|"fin"|"error" }
# (_sanear y _json_linea viven arriba, junto a la caché, que también los usa.)

def generar_informe_pdf(ruta_analizada, resultados, modo=None):
    """Genera el informe PDF sin poder abortar nada.

    Devuelve el mismo dict que informe_pdf.generar(), y si el módulo no está
    disponible (reportlab ausente) lo informa en vez de fallar."""
    try:
        import informe_pdf
    except Exception as e:
        return {"ok": False, "ruta": None, "paginas": 0,
                "error": f"could not load the report generator ({type(e).__name__}: {e})"}
    return informe_pdf.generar(ruta_analizada, resultados, modo=modo)

def cli_principal(argv: "list[str] | None" = None) -> int:
    """CLI del motor. Devuelve el código de salida (0 = bien).

    Está como función y no dentro del bloque `__main__` para que el ejecutable
    congelado pueda invocarla desde su punto de entrada (`main.py`) cuando la GUI
    lo lanza con `--motor-cli`."""
    import argparse

    forzar_utf8_salida()
    _asegurar_flujos()          # en un .exe de ventana stdout puede no existir

    parser = argparse.ArgumentParser(description="FLAC analysis engine")
    # `dest` mantiene los nombres internos (el código de dentro no se traduce):
    # lo que cambia es la interfaz, que va en inglés.
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
    # El informe necesita los espectrogramas, así que --pdf los genera aunque no
    # se haya pedido --png: el usuario no tiene que acordarse de combinar flags.
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

    # Línea 1: cuántos archivos, con los procesos y la caché que se van a usar
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

    # Informe del álbum: el fallo se informa, nunca aborta el lote.
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

    # Última línea: señal de fin
    print(_json_linea({"tipo": "fin"}), flush=True)
    return 0

if __name__ == "__main__":
    raise SystemExit(cli_principal())
