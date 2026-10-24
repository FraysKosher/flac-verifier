"""Spectral analysis, spectrogram rendering and forensic spectral detectors.

Extracted from the motor facade in Phase 4. The STFT is float32 with
sliding_window_view; the PNG is NumPy + Pillow (no matplotlib).
"""
import heapq
import math
import os

import numpy as np

try:
    from PIL import Image, ImageDraw, ImageFont
    PIL_OK = True
except ImportError:
    Image = ImageDraw = ImageFont = None
    PIL_OK = False

from _motor_bits import FRAMES_POR_BLOQUE, _clasificar_rachas

ANCHO_PNG = 2100                    # total canvas width (14 in at 150 dpi)

ALTO_PNG  = 750                      # total canvas height (5 in at 150 dpi)

FPS_ESPECTROGRAMA_PNG = 20          # display time resolution (metrics use every frame)

SEGUNDOS_ESPECTRO = 45                 # minimum seconds of active audio selected for the STFT

MAX_FRAMES_PNG    = 1500

def _media_segura(valores):
    """Mean of a possibly empty array. Avoids NaN (mean of an empty slice),
    which broke the JSON protocol when serialised as the invalid literal NaN."""
    if valores.size == 0:
        return 0.0
    m = float(np.mean(valores))
    return m if math.isfinite(m) else 0.0

def _crear_lut_inferno_256():
    """256-level RGB lookup table (Inferno/Magma style), pure NumPy."""
    nodos_pos = np.array([0.0, 0.20, 0.45, 0.70, 0.90, 1.0], dtype=np.float32)
    nodos_rgb = np.array([
        [4, 4, 20],       # -120 dBFS: deep blue-black
        [48, 12, 84],     # -96 dBFS:  dark violet
        [132, 30, 108],   # -72 dBFS:  purple / magenta
        [212, 72, 66],    # -48 dBFS:  orange-red
        [250, 158, 22],   # -24 dBFS:  bright amber
        [252, 250, 164],  #    0 dBFS:  incandescent yellow
    ], dtype=np.float32)
    x = np.linspace(0.0, 1.0, 256, dtype=np.float32)
    lut = np.empty((256, 3), dtype=np.uint8)
    for canal in range(3):
        lut[:, canal] = np.clip(
            np.interp(x, nodos_pos, nodos_rgb[:, canal]), 0, 255).astype(np.uint8)
    return lut

_LUT_ESPECTROGRAMA = _crear_lut_inferno_256()

def _fuente_png(tam=14):
    """TrueType font with support for the em dash; bitmap fallback otherwise."""
    for nombre in ("arial.ttf", "segoeui.ttf", "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(nombre, tam)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=tam)
    except TypeError:                  # very old Pillow: no size parameter
        return ImageFont.load_default()

def _texto_vertical(lienzo, xy, texto, fuente, color):
    """Draws rotated (90 CCW) text for the axis/colorbar labels."""
    caja = ImageDraw.Draw(lienzo).textbbox((0, 0), texto, font=fuente)
    ancho, alto = caja[2] - caja[0], caja[3] - caja[1]
    capa = Image.new("RGBA", (ancho + 4, alto + 4), (0, 0, 0, 0))
    ImageDraw.Draw(capa).text((2 - caja[0], 2 - caja[1]), texto,
                              font=fuente, fill=color)
    capa = capa.rotate(90, expand=True)
    lienzo.paste(capa, xy, capa)

def _linea_discontinua(dibujo, x0, x1, y, color, ancho=1, salto=10, hueco=7):
    x = x0
    while x < x1:
        dibujo.line([(x, y), (min(x + salto, x1), y)], fill=color, width=ancho)
        x += salto + hueco

def _paso_tiempo(duracion):
    """A readable time-tick step: 25 s on normal tracks, finer on short clips."""
    if duracion <= 10.0:
        return 1.0
    if duracion <= 30.0:
        return 5.0
    if duracion <= 60.0:
        return 10.0
    return 25.0

def _eje_tiempo(tiempos):
    """Positions, duration and label for the X axis.

    The energy selector keeps blocks spread over the whole song, so the recovered
    frames can have real gaps. Treating `0..t1` as one continuous span would put
    ticks over audio that is not in the image. When a gap is detected (more than
    three median frame steps, or more than one second), the positions accumulate
    only the frames actually represented and the axis says so.
    """
    tiempos = np.asarray(tiempos, dtype=np.float64).reshape(-1)
    if tiempos.size < 2:
        return tiempos - (tiempos[0] if tiempos.size else 0.0), 0.0, "Time (s)"
    diferencias = np.diff(tiempos)
    positivas = diferencias[diferencias > 0]
    paso = float(np.median(positivas)) if positivas.size else 0.0
    discontinuo = bool(paso > 0.0 and np.any(
        (diferencias > 3.0 * paso) | (diferencias > 1.0)))
    if discontinuo:
        posiciones = np.arange(tiempos.size, dtype=np.float64) * paso
        return posiciones, float(posiciones[-1]), "Selected active blocks (s)"
    return tiempos - tiempos[0], float(max(0.0, tiempos[-1] - tiempos[0])), "Time (s)"


def _dibujar_colorbar(lienzo, dibujo, x0, y0, ancho, alto, db_min, db_max, fuente):
    """Vertical LUT gradient from db_min (bottom) to db_max (top), with ticks."""
    indices = np.linspace(255, 0, int(alto), dtype=np.uint8).reshape(-1, 1)
    gradiente = _LUT_ESPECTROGRAMA[indices]              # (alto, 1, 3)
    barra = Image.fromarray(gradiente, mode="RGB").resize((ancho, int(alto)))
    lienzo.paste(barra, (x0, y0))
    dibujo.rectangle([x0, y0, x0 + ancho, y0 + alto], outline=(120, 120, 135), width=1)

    for valor in np.arange(db_min, db_max + 1e-9, 10.0):
        frac = (float(valor) - db_min) / max(db_max - db_min, 1.0)
        y = y0 + int(round((1.0 - frac) * alto))
        dibujo.line([(x0 + ancho, y), (x0 + ancho + 6, y)],
                    fill=(190, 190, 200), width=1)
        dibujo.text((x0 + ancho + 10, y), f"{valor:.0f}",
                    fill=(215, 215, 225), font=fuente, anchor="lm")

def _dibujar_leyenda(lienzo, dibujo, x1, y0, entradas, fuente):
    """Boxed upper-right legend with a dashed sample line per entry."""
    if not entradas:
        return
    anchos = [dibujo.textbbox((0, 0), texto, font=fuente)[2] for _, _, texto in entradas]
    ancho = max(anchos) + 70
    alto = 14 + 23 * len(entradas)
    x0 = x1 - ancho
    capa = Image.new("RGBA", lienzo.size, (0, 0, 0, 0))
    lapiz = ImageDraw.Draw(capa)
    lapiz.rounded_rectangle([x0, y0, x1, y0 + alto], radius=5,
                            fill=(15, 15, 22, 215), outline=(140, 140, 160, 255),
                            width=1)
    lienzo.alpha_composite(capa)
    for indice, (color, _, texto) in enumerate(entradas):
        y = y0 + 12 + indice * 23
        _linea_discontinua(dibujo, x0 + 10, x0 + 48, y, color, salto=8, hueco=5)
        dibujo.text((x0 + 56, y - 8), texto, fill=(230, 230, 235), font=fuente)

def renderizar_espectrograma_rapido(matriz_db, sample_rate, ruta_salida_png,
                                    db_min=-80.0, db_max=0.0,
                                    ancho_px=ANCHO_PNG, alto_px=ALTO_PNG,
                                    titulo=None, techo_hz=None,
                                    frecuencia_corte=None, times=None):
    """Renders a (frequency, time) dB matrix to a 2100 x 750 PNG (Pillow only).

    The layout follows the old matplotlib figure: central raster, numeric axes,
    vertical dB colorbar, and a boxed upper-right legend for the ceiling,
    cut-off and CD-Nyquist reference lines. No matplotlib is imported.
    """
    if not PIL_OK:
        raise RuntimeError("Pillow is not available")
    matriz = np.asarray(matriz_db, dtype=np.float32)
    if matriz.ndim != 2 or matriz.shape[0] == 0 or matriz.shape[1] == 0:
        raise ValueError("empty spectrogram matrix")

    ancho_total, alto_total = int(ancho_px), int(alto_px)
    margen_izq, margen_der, margen_sup, margen_inf = 130, 160, 55, 75
    ancho = max(80, ancho_total - margen_izq - margen_der)
    alto = max(80, alto_total - margen_sup - margen_inf)
    x_fin, y_fin = margen_izq + ancho, margen_sup + alto

    # `_guardar_espectrograma` passes (bins, frames): axis 0 is frequency and
    # axis 1 is time. Transpose only if a caller passed the old (frames, bins)
    # layout (recognisable because `times` then matches axis 0).
    if (times is not None and len(times) == matriz.shape[0]
            and matriz.shape[0] != matriz.shape[1]):
        matriz = matriz.T
    spec = matriz[::-1, :]             # high frequencies on top (image Y=0)
    norm = (spec - float(db_min)) / max(float(db_max - db_min), 1.0)
    idx = np.clip(norm * 255.0, 0, 255).astype(np.uint8)
    rgb = _LUT_ESPECTROGRAMA[idx]
    img_spec = Image.fromarray(rgb, mode="RGB").resize(
        (ancho, alto), resample=Image.Resampling.BILINEAR)

    lienzo = Image.new("RGBA", (ancho_total, alto_total), (18, 18, 24, 255))
    lienzo.paste(img_spec, (margen_izq, margen_sup))
    dibujo = ImageDraw.Draw(lienzo)
    fuente = _fuente_png(19)
    fuente_eje = _fuente_png(16)
    fuente_tick = _fuente_png(14)
    color_eje = (185, 185, 198)
    color_texto = (225, 225, 232)

    nyquist_khz = (float(sample_rate) / 2.0) / 1000.0

    def y_de(freq_hz):
        frac = 1.0 - (float(freq_hz) / (nyquist_khz * 1000.0)
                      if nyquist_khz else 0.0)
        return margen_sup + int(round(frac * alto))

    # Y axis: 0, 2.5, 5.0 ... kHz
    for valor in np.arange(0.0, nyquist_khz + 1e-9, 2.5):
        y = y_de(valor * 1000.0)
        dibujo.line([(margen_izq - 7, y), (margen_izq, y)], fill=color_eje, width=1)
        dibujo.text((margen_izq - 12, y), f"{valor:.1f}",
                    fill=color_texto, font=fuente_tick, anchor="rm")
    _texto_vertical(lienzo, (8, margen_sup + alto // 2 - 70),
                    "Frequency (kHz)", fuente_eje, color_texto)

    # X axis: real times, 25 s steps (finer for short clips)
    tiempos = (np.asarray(times, dtype=np.float64).reshape(-1)
               if times is not None else np.array([]))
    if tiempos.size != matriz.shape[1] or not np.all(np.isfinite(tiempos)):
        tiempos = np.arange(matriz.shape[1], dtype=np.float64)
    posiciones, duracion, etiqueta_tiempo = _eje_tiempo(tiempos)
    paso = _paso_tiempo(duracion)
    if duracion > 0.0:
        for valor in np.arange(0.0, duracion + 1e-9, paso):
            x = margen_izq + int(round((valor / duracion) * ancho))
            dibujo.line([(x, y_fin), (x, y_fin + 7)], fill=color_eje, width=1)
            dibujo.text((x, y_fin + 12), f"{valor:.0f}",
                        fill=color_texto, font=fuente_tick, anchor="ma")
    dibujo.text(((margen_izq + x_fin) // 2, y_fin + 44), etiqueta_tiempo,
                fill=color_texto, font=fuente_eje, anchor="ma")

    # Reference lines
    entradas = []
    if nyquist_khz > 24.0:
        _linea_discontinua(dibujo, margen_izq, x_fin, y_de(22050.0),
                           (0, 220, 255), salto=8, hueco=6)
        entradas.append(((0, 220, 255), True, "22.05 kHz (CD Nyquist)"))
    if techo_hz:
        _linea_discontinua(dibujo, margen_izq, x_fin, y_de(float(techo_hz)),
                           (0, 220, 255), ancho=2, salto=10, hueco=6)
        entradas.append(((0, 220, 255), True,
                         f"Ceiling: {float(techo_hz)/1000:.1f} kHz"))
    if frecuencia_corte:
        _linea_discontinua(dibujo, margen_izq, x_fin, y_de(float(frecuencia_corte)),
                           (235, 70, 70), ancho=2, salto=10, hueco=6)
        entradas.append(((235, 70, 70), True,
                         f"Cut-off: {float(frecuencia_corte)/1000:.1f} kHz"))

    dibujo.rectangle([margen_izq, margen_sup, x_fin, y_fin],
                     outline=(90, 90, 110), width=1)
    _dibujar_leyenda(lienzo, dibujo, x_fin - 12, margen_sup + 12,
                     entradas, _fuente_png(14))

    # Colorbar and title
    _dibujar_colorbar(lienzo, dibujo, x_fin + 32, margen_sup, 26, alto,
                      float(db_min), float(db_max), fuente_tick)
    _texto_vertical(lienzo, (ancho_total - 24, margen_sup + alto // 2 - 65),
                    "dB (relative to peak)", fuente_eje, color_texto)

    dibujo.text((margen_izq, 14), (titulo or "Spectrogram")[:85],
                fill=color_texto, font=fuente)
    lienzo.convert("RGB").save(ruta_salida_png, format="PNG", compress_level=3)

def _guardar_espectrograma(freqs, times, mezcla, samplerate, ruta_audio, techo_hz, frecuencia_corte):
    """Draws the spectrogram. Returns (ruta_png, error): it never raises, and the
    reason for the failure is kept so that it can be reported."""
    try:
        if not PIL_OK:
            return None, "Pillow is not available: PNG spectrograms are disabled"
        carpeta        = os.path.dirname(ruta_audio)
        nombre         = os.path.splitext(os.path.basename(ruta_audio))[0]
        directorio_png = os.path.join(carpeta, "_spectrograms")
        os.makedirs(directorio_png, exist_ok=True)   # race-safe across processes

        ruta_png = os.path.join(directorio_png, nombre + "_espectrograma.png")

        # Decimate before the transcendental step: log10 runs on far fewer cells
        # when the window has more frames than the PNG can show.
        mezcla_arr = np.asarray(mezcla, dtype=np.float32)
        tiempos = np.asarray(times, dtype=np.float64).reshape(-1)
        if mezcla_arr.shape[1] > MAX_FRAMES_PNG:
            step       = max(1, mezcla_arr.shape[1] // MAX_FRAMES_PNG)
            mezcla_arr = mezcla_arr[:, ::step]
            tiempos    = tiempos[::step]

        db = np.maximum(mezcla_arr, 1e-28)
        np.log10(db, out=db)
        db *= 10.0
        db -= float(np.max(db))

        renderizar_espectrograma_rapido(
            db, samplerate, ruta_png, db_min=-80.0, db_max=0.0,
            titulo=f"Spectrogram — {os.path.basename(ruta_audio)}",
            techo_hz=techo_hz, frecuencia_corte=frecuencia_corte,
            times=tiempos)
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

def _diagnosticar_autenticidad_hires(freqs_hz, espectro_mid_db, sample_rate,
                                     corte_hz=None):
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
    # look correlated with the musical band after detrending. The profile does not
    # depend on the sample rate being a multiple of 44.1 kHz: DSD transfers are
    # also stored at 96 and 192 kHz.
    es_dsd = False
    mask_dsd = (freqs >= 24000.0) & (freqs <= 40000.0)
    if np.count_nonzero(mask_dsd) > 8:
        y = curva[mask_dsd]
        x = freqs[mask_dsd]
        pendiente = float(np.polyfit(x, y, 1)[0] * 1000.0)
        subida = float(y[-1] - y[0])
        monotona = float(np.mean(np.diff(y) >= 0.0)) >= 0.6
        es_dsd = bool(pendiente > 0.8 and subida > 12.0 and monotona)

    # Profile B: a Scarlet Book (SACD) master keeps the noise-shaping hump around
    # 30 kHz but the protective filter cuts the ramp Profile A needs: the band
    # rises from 23 to 31 kHz and then falls into the filter. Both shapes are
    # independent evidence, so either one marks the transfer as DSD.
    if not es_dsd:
        mask_sb_subida = (freqs >= 23000.0) & (freqs <= 31000.0)
        mask_sb_caida = ((freqs >= 31000.0)
                         & (freqs <= min(40000.0, sample_rate * 0.45)))
        if (np.count_nonzero(mask_sb_subida) >= 5
                and np.count_nonzero(mask_sb_caida) >= 5):
            y_up = curva[mask_sb_subida]
            x_up = freqs[mask_sb_subida]
            y_dn = curva[mask_sb_caida]
            pend_up = float(np.polyfit(x_up, y_up, 1)[0] * 1000.0)
            sub_up = float(np.max(y_up) - y_up[0])
            mono_up = float(np.mean(np.diff(y_up) >= 0.0)) >= 0.60
            caida_post_pico = float(np.max(y_up) - y_dn[-1])
            if (pend_up > 0.8 and sub_up >= 8.0
                    and mono_up and caida_post_pico >= 6.0):
                es_dsd = True

    if es_dsd and muro_brickwall:
        # A DSD transfer always has ultrasonic content: never a brickwall.
        muro_brickwall = False

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
    if (np.std(banda_inf) > 0.5 and np.std(banda_sup) > 0.5
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

    # A genuine analogue roll-off falls from the mid-high band into the
    # ultrasonic floor. A lossy source cut at 15-16 kHz and then re-sampled with
    # a flat dither floor has no such 14-17 kHz drop and, crucially, a prior
    # abrupt cut-off: it must never be excused as an analogue master.
    mask_14_17k = (freqs >= 14000.0) & (freqs <= 17000.0)
    nivel_14_17k = (float(np.median(curva[mask_14_17k]))
                    if np.any(mask_14_17k) else -120.0)
    caida_14_17k_db = nivel_14_17k - nivel_despues_db
    corte_previo = corte_hz is not None and corte_hz < 22050.0

    rolloff_analogico = bool(
        not muro_brickwall
        and not espejo_aliasing
        and not es_dsd
        and peor_pendiente > -17.5
        and salto_db < 14.5
        and nivel_despues_db > -124.0
        and not corte_previo
        and caida_14_17k_db > 8.0
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
    es_dual_mono. Dual mono has two forms, and both are detected: digital (L and R
    identical, more than 45 dB of Mid-Side offset) and analogue (the same mono
    programme through two converters, 20-32 dB of Mid-Side offset over the whole
    band). Pure mono is not dual-mono: there is no Side channel at all."""
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
    # Analogue dual mono: the Side channel is not digitally empty, but a dual-mono
    # master sent through two converters already sits 20-32 dB below Mid over the
    # whole band. Without this, only the > 45 dB digital case was protected.
    if (math.isfinite(diff_media_db) and math.isfinite(diff_aguda_db)
            and diff_media_db > 24.0 and diff_aguda_db > 20.0):
        salida["es_dual_mono"] = True
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

def _gradiente_borde_db_khz(freqs_hz, pot_db, techo_hz, nyquist):
    """Steepest smoothed descent (dB/kHz) in the transition band around the ceiling.

    The band covers 1.5 kHz below the ceiling and 0.5 kHz above it, clamped to
    12 kHz and Nyquist. A 500 Hz moving average removes the bin-to-bin variance
    of real noise, which otherwise looks like a cliff on its own. A studio
    anti-alias filter leaves a gentle slope (well under 35 dB/kHz), while a
    brickwall or a resampler exceeds it.

    Returns a positive number for a descent, 0.0 when the band cannot be
    measured."""
    freqs = np.asarray(freqs_hz, dtype=np.float64)
    pot_db = np.asarray(pot_db, dtype=np.float64)
    if freqs.size < 8 or pot_db.shape != freqs.shape or not techo_hz:
        return 0.0
    f_min = max(12000.0, float(techo_hz) - 1500.0)
    f_max = min(float(nyquist), float(techo_hz) + 500.0)
    if f_max <= f_min:
        return 0.0
    mascara = (freqs >= f_min) & (freqs <= f_max)
    if np.count_nonzero(mascara) < 8:
        return 0.0
    f = freqs[mascara]
    d = pot_db[mascara]
    if not np.all(np.isfinite(d)):
        return 0.0
    paso_hz = float(f[1] - f[0])
    if paso_hz <= 0.0:
        return 0.0

    ventana = max(1, int(round(500.0 / paso_hz)))
    if 1 < ventana <= d.size:
        # 'valid' keeps the window inside the band: with 'same' the convolution
        # zero-pads the ends and invents a cliff right there.
        nucleo = np.ones(ventana, dtype=np.float64) / ventana
        d = np.convolve(d, nucleo, mode="valid")
        f = f[(ventana - 1) // 2 : (ventana - 1) // 2 + d.size]
    if d.size < 2:
        return 0.0

    pendiente = -np.diff(d) / np.diff(f) * 1000.0       # dB/kHz
    gradiente = float(np.max(pendiente))
    if not math.isfinite(gradiente):
        return 0.0
    return round(max(0.0, gradiente), 2)

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
    # The reference level is usually the global peak, but a huge sub-bass line
    # (an 808 or a kick) would drag every other band below the -60 dB activity
    # window and hide the real ceiling. The sub-bass peak is used as the anchor
    # only when it takes over the whole spectrum: it must sit below 150 Hz AND
    # exceed the [150, 6000] Hz peak by more than 6 dB. Ordinary bass never does.
    idx_max = int(np.argmax(pot_db))
    max_global = float(pot_db[idx_max])
    freq_pico = float(freqs[idx_max]) if freqs.size else 0.0
    mask_sin_subbajo = (freqs >= 150.0) & (freqs <= 6000.0)
    # The DC bin is not a sub-bass programme peak: a 1/f tilt puts the maximum at
    # 0 Hz, and re-anchoring on it would push a low-passed master back above the
    # 88% of Nyquist window and hide its ceiling.
    pico_grave = 0.0 < freq_pico < 150.0
    if pico_grave and np.any(mask_sin_subbajo):
        ref_sin_subbajo = float(np.max(pot_db[mask_sin_subbajo]))
        ref_techo_db = (ref_sin_subbajo if (max_global - ref_sin_subbajo) > 6.0
                        else max_global)
    else:
        ref_techo_db = max_global
    pot_db_n  = pot_db - ref_techo_db

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
    hires = _diagnosticar_autenticidad_hires(freqs, espectro_mid_db, samplerate,
                                             corte_hz=corte_hz)

    # A DSD transfer closes its musical band at the SACD passband (20 kHz) while
    # the noise-shaping ramp continues above it. That edge is the passband of the
    # format, not a codec band limit and not a Nyquist-relative step, so it is not
    # reported as a cut-off at all: the ramp itself rules the truncation out.
    if hires["es_transferencia_dsd"] and corte_hz is not None:
        corte_hz = None
        clase_corte = "no_cutoff"
        distancia = None

    # Transition-band slope around the ceiling: distinguishes a studio
    # anti-aliasing filter (gentle) from a brickwall or a resampler (a cliff).
    gradiente_borde = _gradiente_borde_db_khz(freqs, pot_db_n, techo_hz, nyquist)

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
        # Transition band around the ceiling (dB/kHz of steepest descent).
        "gradiente_borde_db_khz":   gradiente_borde,
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
            guardar_png=bool(guardar_png) and PIL_OK and bool(ruta),
            ruta=ruta)
        for inicio in range(0, len(data), FRAMES_POR_BLOQUE):
            acumulador.anadir(data[inicio : inicio + FRAMES_POR_BLOQUE], inicio)
        return acumulador.resultado()
    except Exception as e:
        return {"error": str(e)}

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
        self._suma_pot = None
        self._freqs = None
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
            self._freqs = freqs
            suma = pot.sum(axis=0, dtype=np.float64)
            self._suma_pot = (suma if self._suma_pot is None
                              else self._suma_pot + suma)

            # Instantaneous cut-off: last bin above -60 dB relative to the
            # frame's 4-11 kHz mean power. Referencing the frame peak made a
            # strong bass sweep the reference and produced fake cut-off jitter.
            pot_db = 10.0 * np.log10(np.maximum(pot, 1e-28))
            ref_db = 10.0 * np.log10(np.maximum(
                pot[:, mask_banda].mean(axis=1), np.float32(1e-28)))
            umbral = (ref_db - np.float32(60.0))[:, None]
            sobre = pot_db > umbral

            # A cut-off is only meaningful when the frame also has energy in
            # 10-14 kHz; otherwise a voice/guitar frame with no cymbals looks
            # like a codec cutting and its on/off nature fakes jitter.
            mask_presencia = (freqs >= 10000.0) & (freqs <= 14000.0)
            mask_hf = freqs >= 11000.0
            if not np.any(mask_presencia) or not np.any(mask_hf):
                # Low sample rates (Nyquist <= 12 kHz) have no 10-14 kHz band
                # or no bins above 11 kHz: no measurable cut-off.
                activas_presencia = np.zeros(pot.shape[0], dtype=bool)
                con_corte = np.empty(0, dtype=int)
            else:
                presencia_db = 10.0 * np.log10(np.maximum(
                    pot[:, mask_presencia].mean(axis=1), np.float32(1e-28)))
                activas_presencia = presencia_db > (ref_db - np.float32(25.0))
                sobre_hf = sobre[:, mask_hf]
                con_corte = np.flatnonzero(activas_presencia
                                           & np.any(sobre_hf, axis=1))
            if con_corte.size:
                ultimo = (sobre_hf.shape[1] - 1
                          - np.argmax(sobre_hf[con_corte, ::-1], axis=1))
                self._fc.extend(freqs[mask_hf][ultimo].tolist())

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

        # Vector 5: libopus fullband applies a deterministic 20.00 kHz low-pass
        # (a >45 dB cliff between 19.85 and 20.15 kHz) while 18-19.7 kHz stays
        # flat. A mastering anti-alias filter can look the same in isolation, so
        # this is reported as a fact and only scored with corroborating evidence.
        caida_opus = 0.0
        sospecha_opus = False
        if (self._suma_pot is not None and self._freqs is not None
                and self._tramas >= self.MIN_TRAMAS_FC and self.sr >= 41000):
            freqs = self._freqs
            media = self._suma_pot / float(self._tramas)

            def nivel_db(lo, hi):
                mask = (freqs >= lo) & (freqs < hi)
                if not np.any(mask):
                    return -300.0
                return 10.0 * math.log10(max(float(np.mean(media[mask])), 1e-28))

            bajo = nivel_db(18000.0, 19700.0)
            antes = nivel_db(19700.0, 19850.0)
            despues = nivel_db(20150.0, 20300.0)
            caida_opus = antes - despues
            mask_plano = (freqs >= 18000.0) & (freqs <= 19700.0)
            plano = False
            if np.count_nonzero(mask_plano) > 8:
                curva = 10.0 * np.log10(np.maximum(media[mask_plano], 1e-28))
                plano = bool(np.percentile(curva, 95)
                             - np.percentile(curva, 5) < 6.0)
            sospecha_opus = bool(caida_opus > 45.0 and plano
                                 and despues < bajo - 40.0)

        return {
            "iqr_corte_hz":      round(iqr, 1) if math.isfinite(iqr) else 0.0,
            "ratio_huecos_mdct": round(ratio, 4) if math.isfinite(ratio) else 0.0,
            "caida_opus_20k_db": (round(caida_opus, 2)
                                  if math.isfinite(caida_opus) else 0.0),
            "sospecha_corte_opus_20k": sospecha_opus,
        }

class _AcumuladorEspectral:
    """Keeps the K most active blocks in memory and runs the STFT on them at the
    end. Shared by the streaming path and the array path so both give the same
    metrics. Blocks are never concatenated: a frame must not cross the splice
    between two non-adjacent blocks, which would invent high-frequency content."""

    def __init__(self, samplerate, es_stereo=None, guardar_png=False, ruta=None,
                 renderer=None):
        self.sr = int(samplerate)
        self.es_stereo = es_stereo
        self.guardar_png = bool(guardar_png)
        self.ruta = ruta
        self._renderer = renderer or _guardar_espectrograma
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
            ruta_png, error = self._renderer(
                freqs, np.concatenate(png_times),
                np.concatenate(png_pot, axis=0).T,
                self.sr, self.ruta, esp["techo_hz"], esp["frecuencia_corte"])
            esp["espectrograma"] = ruta_png
            if error:
                esp["espectrograma_error"] = error
        return esp
