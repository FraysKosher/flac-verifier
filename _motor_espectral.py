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

from _motor_bits import FRAMES_BY_BLOCK, _classify_streaks

WIDTH_PNG = 2100                    # total canvas width (14 in at 150 dpi)

HEIGHT_PNG  = 750                      # total canvas height (5 in at 150 dpi)

PNG_SPECTROGRAM_FPS = 20          # display time resolution (metrics use every frame)

SPECTRO_SECONDS = 45                 # minimum seconds of active audio selected for the STFT

MAX_FRAMES_PNG    = 1500

def _mean_safe(values):
    """Mean of a possibly empty array. Avoids NaN (mean of an empty slice),
    which broke the JSON protocol when serialised as the invalid literal NaN."""
    if values.size == 0:
        return 0.0
    m = float(np.mean(values))
    return m if math.isfinite(m) else 0.0

def _create_lut_inferno_256():
    """256-level RGB lookup table (Inferno/Magma style), pure NumPy."""
    nodes_pos = np.array([0.0, 0.20, 0.45, 0.70, 0.90, 1.0], dtype=np.float32)
    nodes_rgb = np.array([
        [4, 4, 20],       # -120 dBFS: deep blue-black
        [48, 12, 84],     # -96 dBFS:  dark violet
        [132, 30, 108],   # -72 dBFS:  purple / magenta
        [212, 72, 66],    # -48 dBFS:  orange-red
        [250, 158, 22],   # -24 dBFS:  bright amber
        [252, 250, 164],  #    0 dBFS:  incandescent yellow
    ], dtype=np.float32)
    x = np.linspace(0.0, 1.0, 256, dtype=np.float32)
    lut = np.empty((256, 3), dtype=np.uint8)
    for channel in range(3):
        lut[:, channel] = np.clip(
            np.interp(x, nodes_pos, nodes_rgb[:, channel]), 0, 255).astype(np.uint8)
    return lut

_LUT_SPECTROGRAM = _create_lut_inferno_256()
_FONT_CACHE = {}

def _source_png(font_size=14):
    """TrueType font with support for the em dash; bitmap fallback otherwise."""
    cached = _FONT_CACHE.get(font_size)
    if cached is not None:
        return cached
    loaded = None
    for name in ("arial.ttf", "segoeui.ttf", "DejaVuSans.ttf"):
        try:
            loaded = ImageFont.truetype(name, font_size)
            break
        except OSError:
            continue
    if loaded is None:
        try:
            loaded = ImageFont.load_default(size=font_size)
        except TypeError:                  # very old Pillow: no size parameter
            loaded = ImageFont.load_default()
    _FONT_CACHE[font_size] = loaded
    return loaded

def _text_vertical(canvas, xy, text, source, color):
    """Draws rotated (90 CCW) text for the axis/colorbar labels."""
    caja = ImageDraw.Draw(canvas).textbbox((0, 0), text, font=source)
    width, height = caja[2] - caja[0], caja[3] - caja[1]
    layer = Image.new("RGBA", (width + 4, height + 4), (0, 0, 0, 0))
    ImageDraw.Draw(layer).text((2 - caja[0], 2 - caja[1]), text,
                              font=source, fill=color)
    layer = layer.rotate(90, expand=True)
    canvas.paste(layer, xy, layer)

def _line_dashed(drawing, x0, x1, y, color, width=1, jump=10, gap=7):
    x = x0
    while x < x1:
        drawing.line([(x, y), (min(x + jump, x1), y)], fill=color, width=width)
        x += jump + gap

def _time_step(duration):
    """A readable time-tick step: 25 s on normal tracks, finer on short clips."""
    if duration <= 10.0:
        return 1.0
    if duration <= 30.0:
        return 5.0
    if duration <= 60.0:
        return 10.0
    return 25.0

def _time_axis(times):
    """Positions, duration and label for the X axis.

    The energy selector keeps blocks spread over the whole song, so the recovered
    frames can have real gaps. Treating `0..t1` as one continuous span would put
    ticks over audio that is not in the image. When a gap is detected (more than
    three median frame steps, or more than one second), the positions accumulate
    only the frames actually represented and the axis says so.
    """
    times = np.asarray(times, dtype=np.float64).reshape(-1)
    if times.size < 2:
        return times - (times[0] if times.size else 0.0), 0.0, "Time (s)"
    diferencias = np.diff(times)
    positivas = diferencias[diferencias > 0]
    step_size = float(np.median(positivas)) if positivas.size else 0.0
    discontinuo = bool(step_size > 0.0 and np.any(
        (diferencias > 3.0 * step_size) | (diferencias > 1.0)))
    if discontinuo:
        positions = np.arange(times.size, dtype=np.float64) * step_size
        return positions, float(positions[-1]), "Selected active blocks (s)"
    return times - times[0], float(max(0.0, times[-1] - times[0])), "Time (s)"


def _draw_colorbar(canvas, drawing, x0, y0, width, height, db_min, db_max, source):
    """Vertical LUT gradient from db_min (bottom) to db_max (top), with ticks."""
    indices = np.linspace(255, 0, int(height), dtype=np.uint8).reshape(-1, 1)
    gradient = _LUT_SPECTROGRAM[indices]              # (height, 1, 3)
    barra = Image.fromarray(gradient, mode="RGB").resize((width, int(height)))
    canvas.paste(barra, (x0, y0))
    drawing.rectangle([x0, y0, x0 + width, y0 + height], outline=(120, 120, 135), width=1)

    for value in np.arange(db_min, db_max + 1e-9, 10.0):
        frac = (float(value) - db_min) / max(db_max - db_min, 1.0)
        y = y0 + int(round((1.0 - frac) * height))
        drawing.line([(x0 + width, y), (x0 + width + 6, y)],
                    fill=(190, 190, 200), width=1)
        drawing.text((x0 + width + 10, y), f"{value:.0f}",
                    fill=(215, 215, 225), font=source, anchor="lm")

def _draw_legend(canvas, drawing, x1, y0, entries, source):
    """Boxed upper-right legend with a dashed sample line per entry."""
    if not entries:
        return
    widths = [drawing.textbbox((0, 0), text, font=source)[2] for _, _, text in entries]
    width = max(widths) + 70
    height = 14 + 23 * len(entries)
    x0 = x1 - width
    layer = Image.new("RGBA", (width + 2, height + 2), (0, 0, 0, 0))
    pen = ImageDraw.Draw(layer)
    pen.rounded_rectangle([0, 0, width, height], radius=5,
                          fill=(15, 15, 22, 215), outline=(140, 140, 160, 255),
                          width=1)
    canvas.alpha_composite(layer, (x0, y0))
    for index, (color, _, text) in enumerate(entries):
        y = y0 + 12 + index * 23
        _line_dashed(drawing, x0 + 10, x0 + 48, y, color, jump=8, gap=5)
        drawing.text((x0 + 56, y - 8), text, fill=(230, 230, 235), font=source)

def render_spectrogram_fast(matriz_db, sample_rate, output_png_path,
                                    db_min=-80.0, db_max=0.0,
                                    width_px=WIDTH_PNG, height_px=HEIGHT_PNG,
                                    title=None, ceiling_hz=None,
                                    frequency_cutoff=None, times=None):
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

    width_total, height_total = int(width_px), int(height_px)
    margin_left, margin_right, margin_top, margin_bottom = 130, 160, 55, 75
    width = max(80, width_total - margin_left - margin_right)
    height = max(80, height_total - margin_top - margin_bottom)
    x_end, y_end = margin_left + width, margin_top + height

    # `_save_spectrogram` passes (bins, frames): axis 0 is frequency and
    # axis 1 is time. Transpose only if a caller passed the old (frames, bins)
    # layout (recognisable because `times` then matches axis 0).
    if (times is not None and len(times) == matriz.shape[0]
            and matriz.shape[0] != matriz.shape[1]):
        matriz = matriz.T
    spec = matriz[::-1, :]             # high frequencies on top (image Y=0)
    norm = (spec - float(db_min)) / max(float(db_max - db_min), 1.0)
    idx = np.clip(norm * 255.0, 0, 255).astype(np.uint8)
    rgb = _LUT_SPECTROGRAM[idx]
    img_spec = Image.fromarray(rgb, mode="RGB").resize(
        (width, height), resample=Image.Resampling.BILINEAR)

    canvas = Image.new("RGBA", (width_total, height_total), (18, 18, 24, 255))
    canvas.paste(img_spec, (margin_left, margin_top))
    drawing = ImageDraw.Draw(canvas)
    source = _source_png(19)
    axis_font = _source_png(16)
    tick_font = _source_png(14)
    axis_color = (185, 185, 198)
    text_color = (225, 225, 232)

    nyquist_khz = (float(sample_rate) / 2.0) / 1000.0

    def y_for(freq_hz):
        frac = 1.0 - (float(freq_hz) / (nyquist_khz * 1000.0)
                      if nyquist_khz else 0.0)
        return margin_top + int(round(frac * height))

    # Y axis: 0, 2.5, 5.0 ... kHz
    for value in np.arange(0.0, nyquist_khz + 1e-9, 2.5):
        y = y_for(value * 1000.0)
        drawing.line([(margin_left - 7, y), (margin_left, y)], fill=axis_color, width=1)
        drawing.text((margin_left - 12, y), f"{value:.1f}",
                    fill=text_color, font=tick_font, anchor="rm")
    _text_vertical(canvas, (8, margin_top + height // 2 - 70),
                    "Frequency (kHz)", axis_font, text_color)

    # X axis: real times, 25 s steps (finer for short clips)
    times = (np.asarray(times, dtype=np.float64).reshape(-1)
               if times is not None else np.array([]))
    if times.size != matriz.shape[1] or not np.all(np.isfinite(times)):
        times = np.arange(matriz.shape[1], dtype=np.float64)
    positions, duration, time_label = _time_axis(times)
    step_size = _time_step(duration)
    if duration > 0.0:
        for value in np.arange(0.0, duration + 1e-9, step_size):
            x = margin_left + int(round((value / duration) * width))
            drawing.line([(x, y_end), (x, y_end + 7)], fill=axis_color, width=1)
            drawing.text((x, y_end + 12), f"{value:.0f}",
                        fill=text_color, font=tick_font, anchor="ma")
    drawing.text(((margin_left + x_end) // 2, y_end + 44), time_label,
                fill=text_color, font=axis_font, anchor="ma")

    # Reference lines
    entries = []
    if nyquist_khz > 24.0:
        _line_dashed(drawing, margin_left, x_end, y_for(22050.0),
                           (0, 220, 255), jump=8, gap=6)
        entries.append(((0, 220, 255), True, "22.05 kHz (CD Nyquist)"))
    if ceiling_hz:
        _line_dashed(drawing, margin_left, x_end, y_for(float(ceiling_hz)),
                           (0, 220, 255), width=2, jump=10, gap=6)
        entries.append(((0, 220, 255), True,
                         f"Ceiling: {float(ceiling_hz)/1000:.1f} kHz"))
    if frequency_cutoff:
        _line_dashed(drawing, margin_left, x_end, y_for(float(frequency_cutoff)),
                           (235, 70, 70), width=2, jump=10, gap=6)
        entries.append(((235, 70, 70), True,
                         f"Cut-off: {float(frequency_cutoff)/1000:.1f} kHz"))

    drawing.rectangle([margin_left, margin_top, x_end, y_end],
                     outline=(90, 90, 110), width=1)
    _draw_legend(canvas, drawing, x_end - 12, margin_top + 12,
                     entries, _source_png(14))

    # Colorbar and title
    _draw_colorbar(canvas, drawing, x_end + 32, margin_top, 26, height,
                      float(db_min), float(db_max), tick_font)
    _text_vertical(canvas, (width_total - 24, margin_top + height // 2 - 65),
                    "dB (relative to peak)", axis_font, text_color)

    drawing.text((margin_left, 14), (title or "Spectrogram")[:85],
                fill=text_color, font=source)
    canvas.convert("RGB").save(output_png_path, format="PNG", compress_level=3)

def _save_spectrogram(freqs, times, mix, samplerate, audio_path, ceiling_hz, frequency_cutoff):
    """Draws the spectrogram. Returns (png_path, error): it never raises, and the
    reason for the failure is kept so that it can be reported."""
    try:
        if not PIL_OK:
            return None, "Pillow is not available: PNG spectrograms are disabled"
        folder        = os.path.dirname(audio_path)
        name         = os.path.splitext(os.path.basename(audio_path))[0]
        png_directory = os.path.join(folder, "_spectrograms")
        os.makedirs(png_directory, exist_ok=True)   # race-safe across processes

        png_path = os.path.join(png_directory, name + "_espectrograma.png")

        # Decimate before the transcendental step: log10 runs on far fewer cells
        # when the window has more frames than the PNG can show.
        mix_arr = np.asarray(mix, dtype=np.float32)
        times = np.asarray(times, dtype=np.float64).reshape(-1)
        if mix_arr.shape[1] > MAX_FRAMES_PNG:
            step       = max(1, mix_arr.shape[1] // MAX_FRAMES_PNG)
            mix_arr = mix_arr[:, ::step]
            times    = times[::step]

        db = np.maximum(mix_arr, 1e-28)
        np.log10(db, out=db)
        db *= 10.0
        db -= float(np.max(db))

        render_spectrogram_fast(
            db, samplerate, png_path, db_min=-80.0, db_max=0.0,
            title=f"Spectrogram — {os.path.basename(audio_path)}",
            ceiling_hz=ceiling_hz, frequency_cutoff=frequency_cutoff,
            times=times)
        return png_path, None
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"

def _white_noise_gain(nperseg, window):
    """Mean bin power of a unit-variance white noise through the normalised
    float32 STFT. Dividing a power spectrum by it turns each bin into dBFS: a
    broadband floor at -104 dBFS reads -104 dB, independently of nperseg."""
    n = int(nperseg)
    sum_w = float(np.sum(window, dtype=np.float64))
    if n <= 0 or sum_w <= 0.0:
        return 1.0
    sum_w2 = float(np.sum(np.asarray(window, dtype=np.float64) ** 2))
    return n * sum_w2 / (2.0 * sum_w * sum_w * (n / 2.0 + 1.0))

def _power_db(power):
    """Power spectrum in dB (floor 1e-28, no np.sqrt)."""
    return 10.0 * np.log10(np.maximum(np.asarray(power, dtype=np.float64), 1e-28))

def _spectrum_dbfs(power, gain_noise=1.0):
    """Power spectrum in dBFS/ bin using the white-noise gain calibration."""
    gain = gain_noise if gain_noise and gain_noise > 0 else 1.0
    return 10.0 * np.log10(np.maximum(np.asarray(power, dtype=np.float64),
                                      1e-28) / gain)

def _diagnose_hires_authenticity(freqs_hz, spectrum_mid_db, sample_rate,
                                     cutoff_hz=None):
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

    `spectrum_mid_db` is calibrated to dBFS per bin (see _spectrum_dbfs)."""
    output = {
        "muro_brickwall_nyquist":  False,
        "espejo_aliasing_detectado": False,
        "rolloff_analogico_genuino": False,
        "gradiente_nyquist_db_khz": 0.0,
        "salto_ultrasonico_db":     0.0,
        "correlacion_espejo_22k":   0.0,
        "es_transferencia_dsd":     False,
        "perfil_dsd":               None,
    }
    freqs = np.asarray(freqs_hz, dtype=np.float64)
    spectrum = np.asarray(spectrum_mid_db, dtype=np.float64)
    if sample_rate < 88200 or freqs.size < 8 or spectrum.shape != freqs.shape:
        return output
    if not np.all(np.isfinite(spectrum)):
        return output

    df = float(freqs[1] - freqs[0]) if freqs.size > 1 else 1.0
    # 1 kHz smoothing: a 250 Hz kernel lets the bin-to-bin variance of real
    # noise look like a 90 dB/kHz cliff. The morphology of a brickwall or a
    # roll-off lives on a much wider scale, so smoothing does not hide it.
    bins_suavizado = max(3, int(round(1000.0 / df)))
    kernel = np.ones(bins_suavizado, dtype=np.float32) / bins_suavizado
    curva = np.convolve(spectrum, kernel, mode="same")
    grad_db_khz = np.gradient(curva, df / 1000.0)

    mask_22k = (freqs >= 19800.0) & (freqs <= 22450.0)
    mask_24k = (freqs >= 22500.0) & (freqs <= 24400.0)
    mask_previous = (freqs >= 13500.0) & (freqs < 19200.0)
    slope_22k = float(np.min(grad_db_khz[mask_22k])) if np.any(mask_22k) else 0.0
    slope_24k = float(np.min(grad_db_khz[mask_24k])) if np.any(mask_24k) else 0.0
    worse_slope = min(slope_22k, slope_24k)
    slope_base = (float(np.percentile(grad_db_khz[mask_previous], 20))
                      if np.any(mask_previous) else -4.0)

    mask_before = (freqs >= 18000.0) & (freqs <= 20800.0)
    mask_after = ((freqs >= 23500.0)
                    & (freqs <= min(36000.0, sample_rate * 0.44)))
    level_before_db = float(np.median(curva[mask_before])) if np.any(mask_before) else -120.0
    level_after_db = (float(np.median(curva[mask_after]))
                        if np.any(mask_after) else -140.0)
    jump_db = level_before_db - level_after_db

    # 48 kHz -> 96/192 kHz upscale: the passband reaches 22.5 kHz intact and
    # collapses across the 24.0 kHz Nyquist into the ultrasonic void.
    mask_pre_24k = (freqs >= 19500.0) & (freqs <= 22500.0)
    mask_post_24k = ((freqs >= 25000.0)
                     & (freqs <= min(38000.0, sample_rate * 0.44)))
    level_pre24_db = (float(np.median(curva[mask_pre_24k]))
                      if np.any(mask_pre_24k) else -120.0)
    level_post24_db = (float(np.median(curva[mask_post_24k]))
                       if np.any(mask_post_24k) else -140.0)
    jump_24k_db = level_pre24_db - level_post24_db
    muro_24k = (
        slope_24k < -20.0
        and slope_22k > -12.0
        and slope_24k < slope_base * 2.0
        and jump_24k_db > 22.0
    )
    if muro_24k:
        jump_db = max(jump_db, jump_24k_db)

    muro_brickwall = (
        (worse_slope < -28.0 and worse_slope < slope_base * 2.1
         and jump_db > 15.0)
        or (jump_db > 21.0 and level_after_db < -125.0)
        or muro_24k
    )

    # DSD64/128 to PCM: rising noise-shaping ramp between 24 and 40 kHz. It is
    # checked before the mirror because an independent rising ultrasonic ramp can
    # look correlated with the musical band after detrending. The profile does not
    # depend on the sample rate being a multiple of 44.1 kHz: DSD transfers are
    # also stored at 96 and 192 kHz.
    #
    # Guard: noise-shaped dither on a lossy transcode also rises above 24 kHz, but
    # it lives in the thermal floor (below -90 dBFS per bin) and only appears after
    # a massive cliff (more than 25 dB). A real delta-sigma transfer is loud and
    # continuous, so both conditions must hold for every profile.
    is_dsd = False
    profile_dsd = None
    mask_dsd = (freqs >= 24000.0) & (freqs <= 40000.0)
    if np.count_nonzero(mask_dsd) > 8:
        y = curva[mask_dsd]
        x = freqs[mask_dsd]
        slope = float(np.polyfit(x, y, 1)[0] * 1000.0)
        subida = float(y[-1] - y[0])
        monotona = float(np.mean(np.diff(y) >= 0.0)) >= 0.6
        if (slope > 0.8 and subida > 12.0 and monotona
                and float(np.max(y)) > -90.0 and jump_db < 25.0):
            is_dsd = True
            profile_dsd = "direct_24_40k"

    # Profile B: a Scarlet Book (SACD) master keeps the noise-shaping hump around
    # 30 kHz but the protective filter cuts the ramp Profile A needs: the band
    # rises from 23 to 31 kHz and then falls into the filter. Both shapes are
    # independent evidence, so either one marks the transfer as DSD.
    if not is_dsd:
        mask_sb_subida = (freqs >= 23000.0) & (freqs <= 31000.0)
        mask_sb_fall = ((freqs >= 31000.0)
                         & (freqs <= min(40000.0, sample_rate * 0.45)))
        if (np.count_nonzero(mask_sb_subida) >= 5
                and np.count_nonzero(mask_sb_fall) >= 5):
            y_up = curva[mask_sb_subida]
            x_up = freqs[mask_sb_subida]
            y_dn = curva[mask_sb_fall]
            pend_up = float(np.polyfit(x_up, y_up, 1)[0] * 1000.0)
            sub_up = float(np.max(y_up) - y_up[0])
            mono_up = float(np.mean(np.diff(y_up) >= 0.0)) >= 0.60
            fall_post_peak = float(np.max(y_up) - y_dn[-1])
            if (pend_up > 0.8 and sub_up >= 8.0
                    and mono_up and fall_post_peak >= 6.0
                    and float(np.max(y_up)) > -90.0 and jump_db < 25.0):
                is_dsd = True
                profile_dsd = "scarlet_30k"

    # Profile C: a DSD128 transfer at 176.4/192 kHz has its own noise-shaping ramp
    # one octave higher. The slope is gentler because the same power is spread
    # over twice the band, so the threshold is 0.35 dB/kHz instead of 0.8.
    if not is_dsd and sample_rate >= 176400:
        mask_c = ((freqs >= 40000.0)
                  & (freqs <= min(80000.0, sample_rate * 0.45)))
        if np.count_nonzero(mask_c) > 8:
            y_c = curva[mask_c]
            x_c = freqs[mask_c]
            pend_c = float(np.polyfit(x_c, y_c, 1)[0] * 1000.0)
            sub_c = float(y_c[-1] - y_c[0])
            mono_c = float(np.mean(np.diff(y_c) >= 0.0)) >= 0.60
            if (pend_c > 0.35 and sub_c > 12.0 and mono_c
                    and float(np.max(y_c)) > -90.0 and jump_db < 25.0):
                is_dsd = True
                profile_dsd = "dsd128_40_80k"

    if is_dsd and muro_brickwall:
        # A DSD transfer always has ultrasonic content: never a brickwall.
        muro_brickwall = False

    # Spectral mirror around the CD Nyquist (unfiltered interpolation). The fine
    # 250 Hz curve keeps the texture the mirror copies; the coarse curve is only
    # for slopes and levels.
    fine_bins = max(3, int(round(250.0 / df)))
    fine_curve = np.convolve(spectrum,
                             np.ones(fine_bins, dtype=np.float32) / fine_bins,
                             mode="same")
    deltas = np.linspace(1000.0, 7000.0, 48)
    band_lower = np.interp(22050.0 - deltas, freqs, fine_curve)
    band_upper = np.interp(22050.0 + deltas, freqs, fine_curve)
    if (np.std(band_lower) > 0.5 and np.std(band_upper) > 0.5
            and np.mean(band_upper) > -115.0):
        # The interpolator's anti-imaging response tilts the mirror copy, so a
        # raw Pearson coefficient can be negative even for a perfect mirror;
        # the shape is what matters. Detrending both bands keeps r > 0.82 for a
        # mirror and low for genuine independent ultrasonic noise.
        def _detrended(values):
            t = np.arange(values.size, dtype=np.float64)
            return values - np.polyval(np.polyfit(t, values, 1), t)
        mirror_correlation = float(np.corrcoef(_detrended(band_lower),
                                        _detrended(band_upper))[0, 1])
    else:
        mirror_correlation = 0.0
    mirror_aliasing = bool(mirror_correlation > 0.82 and jump_db < 18.0 and not is_dsd)

    # A genuine analogue roll-off falls from the mid-high band into the
    # ultrasonic floor. A lossy source cut at 15-16 kHz and then re-sampled with
    # a flat dither floor has no such 14-17 kHz drop and, crucially, a prior
    # abrupt cut-off: it must never be excused as an analogue master.
    mask_14_17k = (freqs >= 14000.0) & (freqs <= 17000.0)
    level_14_17k = (float(np.median(curva[mask_14_17k]))
                    if np.any(mask_14_17k) else -120.0)
    fall_14_17k_db = level_14_17k - level_after_db
    cutoff_previous = cutoff_hz is not None and cutoff_hz < 22050.0

    rolloff_analog = bool(
        not muro_brickwall
        and not mirror_aliasing
        and not is_dsd
        and worse_slope > -17.5
        and jump_db < 14.5
        and level_after_db > -124.0
        and not cutoff_previous
        and fall_14_17k_db > 8.0
    )

    output.update({
        "muro_brickwall_nyquist":   bool(muro_brickwall),
        "espejo_aliasing_detectado": mirror_aliasing,
        "rolloff_analogico_genuino": rolloff_analog,
        "gradiente_nyquist_db_khz": round(abs(worse_slope), 2),
        "salto_ultrasonico_db":     round(jump_db, 2),
        "correlacion_espejo_22k":   round(mirror_correlation, 3),
        "es_transferencia_dsd":     is_dsd,
        "perfil_dsd":               profile_dsd,
    })
    return output

def _analyze_mid_side_asymmetry(spectrum_mid_db, spectrum_side_db, freqs_hz,
                                 sample_rate, is_mono_real):
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
    output = {
        "caida_side_agudos_db": 0.0,
        "delta_corte_ms_hz":    0.0,
        "sospecha_joint_stereo": False,
        "es_dual_mono":         False,
    }
    if is_mono_real or sample_rate < 44100:
        return output

    freqs = np.asarray(freqs_hz, dtype=np.float64)
    mid = np.asarray(spectrum_mid_db, dtype=np.float64)
    side = np.asarray(spectrum_side_db, dtype=np.float64)
    if freqs.size < 2 or mid.shape != freqs.shape or side.shape != freqs.shape:
        return output
    if not (np.all(np.isfinite(mid)) and np.all(np.isfinite(side))):
        return output

    # Dual mono: in a stereo container the Side channel is essentially empty.
    diferencia = mid - side
    if np.all(np.isfinite(diferencia)):
        output["es_dual_mono"] = bool(float(np.median(diferencia)) > 45.0)

    mask_mean = (freqs >= 2000.0) & (freqs <= 8000.0)
    mask_high = (freqs >= 15500.0) & (freqs <= 19800.0)
    if not np.any(mask_mean) or not np.any(mask_high):
        return output

    diff_mean_db = float(np.median(diferencia[mask_mean]))
    diff_high_db = float(np.median(diferencia[mask_high]))
    # Analogue dual mono: the Side channel is not digitally empty, but a dual-mono
    # master sent through two converters already sits 20-32 dB below Mid over the
    # whole band. Without this, only the > 45 dB digital case was protected.
    if (math.isfinite(diff_mean_db) and math.isfinite(diff_high_db)
            and diff_mean_db > 24.0 and diff_high_db > 20.0):
        output["es_dual_mono"] = True
    collapse = diff_high_db - diff_mean_db

    high_mask = (freqs >= 13000.0) & (freqs <= 21000.0)
    mask_pivote = (freqs >= 6000.0) & (freqs <= 10000.0)
    if not np.any(high_mask) or not np.any(mask_pivote):
        return output
    f_high = freqs[high_mask]

    mid_threshold = max(float(np.percentile(mid[mask_pivote], 75)) - 38.0, -92.0)
    side_threshold = max(float(np.percentile(side[mask_pivote], 75)) - 38.0, -95.0)
    active_mid = np.flatnonzero(mid[high_mask] > mid_threshold)
    active_side = np.flatnonzero(side[high_mask] > side_threshold)
    mid_ceiling_hz = float(f_high[active_mid[-1]]) if active_mid.size else 13000.0
    side_ceiling_hz = float(f_high[active_side[-1]]) if active_side.size else 13000.0
    delta_cutoff = max(0.0, mid_ceiling_hz - side_ceiling_hz)

    suspicion = (
        not output["es_dual_mono"]
        and diff_mean_db < 14.0
        and mid_ceiling_hz >= 17500.0
        and ((collapse > 11.5 and delta_cutoff > 1100.0) or collapse > 16.5)
    )

    output["caida_side_agudos_db"] = round(collapse, 2) if math.isfinite(collapse) else 0.0
    output["delta_corte_ms_hz"] = round(delta_cutoff, 1) if math.isfinite(delta_cutoff) else 0.0
    output["sospecha_joint_stereo"] = bool(suspicion)
    return output

def _gradient_edge_db_khz(freqs_hz, power_db, ceiling_hz, nyquist):
    """Steepest smoothed descent (dB/kHz) in the transition band around the ceiling.

    The band covers 1.5 kHz below the ceiling and 0.5 kHz above it, clamped to
    12 kHz and Nyquist. A 500 Hz moving average removes the bin-to-bin variance
    of real noise, which otherwise looks like a cliff on its own. A studio
    anti-alias filter leaves a gentle slope (well under 35 dB/kHz), while a
    brickwall or a resampler exceeds it.

    Returns a positive number for a descent, 0.0 when the band cannot be
    measured."""
    freqs = np.asarray(freqs_hz, dtype=np.float64)
    power_db = np.asarray(power_db, dtype=np.float64)
    if freqs.size < 8 or power_db.shape != freqs.shape or not ceiling_hz:
        return 0.0
    f_min = max(12000.0, float(ceiling_hz) - 1500.0)
    f_max = min(float(nyquist), float(ceiling_hz) + 500.0)
    if f_max <= f_min:
        return 0.0
    mask = (freqs >= f_min) & (freqs <= f_max)
    if np.count_nonzero(mask) < 8:
        return 0.0
    f = freqs[mask]
    d = power_db[mask]
    if not np.all(np.isfinite(d)):
        return 0.0
    step_hz = float(f[1] - f[0])
    if step_hz <= 0.0:
        return 0.0

    window = max(1, int(round(500.0 / step_hz)))
    if 1 < window <= d.size:
        # 'valid' keeps the window inside the band: with 'same' the convolution
        # zero-pads the ends and invents a cliff right there.
        nucleo = np.ones(window, dtype=np.float64) / window
        d = np.convolve(d, nucleo, mode="valid")
        f = f[(window - 1) // 2 : (window - 1) // 2 + d.size]
    if d.size < 2:
        return 0.0

    slope = -np.diff(d) / np.diff(f) * 1000.0       # dB/kHz
    gradient = float(np.max(slope))
    if not math.isfinite(gradient):
        return 0.0
    return round(max(0.0, gradient), 2)

def _spectral_metrics(freqs, power_l, power_r, samplerate, is_stereo=None, power=None,
                          power_mid=None, power_side=None, gain_noise=1.0):
    """Core of the spectral analysis: everything is derived from the
    time-averaged power spectrum (one entry per bin). It does not depend on how
    it was obtained.

    is_stereo is passed explicitly: in mono, power_l and power_r can be distinct
    arrays holding the same values, so identity is not a usable hint.
    power is the total power spectrum; the Mid/Side decomposition provides it
    directly (|M|^2 + |S|^2 = (|L|^2 + |R|^2) / 2)."""
    if is_stereo is None:
        is_stereo = power_l is not power_r
    if power is None:
        power = (power_l + power_r) / 2
    power_db    = 10.0 * np.log10(np.maximum(power, 1e-28))
    # The reference level is usually the global peak, but a huge sub-bass line
    # (an 808 or a kick) would drag every other band below the -60 dB activity
    # window and hide the real ceiling. The sub-bass peak is used as the anchor
    # only when it takes over the whole spectrum: it must sit below 150 Hz AND
    # exceed the [150, 6000] Hz peak by more than 6 dB. Ordinary bass never does.
    idx_max = int(np.argmax(power_db))
    max_global = float(power_db[idx_max])
    peak_freq = float(freqs[idx_max]) if freqs.size else 0.0
    mask_without_subbass = (freqs >= 150.0) & (freqs <= 6000.0)
    # The DC bin is not a sub-bass programme peak: a 1/f tilt puts the maximum at
    # 0 Hz, and re-anchoring on it would push a low-passed master back above the
    # 88% of Nyquist window and hide its ceiling.
    peak_bass = 0.0 < peak_freq < 150.0
    if peak_bass and np.any(mask_without_subbass):
        ref_without_subbass = float(np.max(power_db[mask_without_subbass]))
        ref_ceiling_db = (ref_without_subbass if (max_global - ref_without_subbass) > 6.0
                        else max_global)
    else:
        ref_ceiling_db = max_global
    power_db_n  = power_db - ref_ceiling_db

    mask_act = power_db_n > -60
    if np.any(mask_act):
        # Last bin above -60 dB, located from the end in C without copying freqs.
        last_idx = len(freqs) - 1 - int(np.argmax(mask_act[::-1]))
        ceiling_hz   = float(freqs[last_idx])
    else:
        ceiling_hz   = 0.0

    cutoff_hz = None
    mask_s = (freqs >= 12000) & (freqs <= 25000)
    if np.count_nonzero(mask_s) > 10:
        pr   = power_db_n[mask_s]
        fr   = freqs[mask_s]
        n_pr = len(pr)
        step_size = max(1, n_pr // 20)
        if n_pr > 2 * step_size:
            # O(n) equivalent of the sliding-window loop via cumulative sums: the
            # two means per index become two vectorised subtractions.
            cs = np.empty(n_pr + 1, dtype=np.float64)
            cs[0] = 0.0
            np.cumsum(pr, dtype=np.float64, out=cs[1:])
            inv_step = 1.0 / step_size
            before   = (cs[step_size : n_pr - step_size] - cs[0 : n_pr - 2 * step_size]) * inv_step
            after = (cs[2 * step_size : n_pr]    - cs[step_size : n_pr - step_size]) * inv_step
            cond    = ((before - after) > 25.0) & (after < -45.0)
            if np.any(cond):
                cutoff_hz = float(fr[int(np.argmax(cond)) + step_size])
            else:
                step_med = max(1, n_pr // 14)
                if n_pr > 2 * step_med:
                    inv_med = 1.0 / step_med
                    bef_m = (cs[step_med : n_pr - step_med] - cs[0 : n_pr - 2 * step_med]) * inv_med
                    aft_m = (cs[2 * step_med : n_pr] - cs[step_med : n_pr - step_med]) * inv_med
                    cond_m = ((bef_m - aft_m) > 18.5) & (aft_m < -42.0) & (bef_m > -52.0)
                    if np.any(cond_m):
                        cand_hz = float(fr[int(np.argmax(cond_m)) + step_med])
                        if _gradient_edge_db_khz(freqs, power_db_n, cand_hz, samplerate / 2.0) > 38.0:
                            cutoff_hz = cand_hz

    # Block 5: the distance to Nyquist decides whether the step is a codec
    # signature or a mastering filter. A legitimate anti-alias filter lives right
    # at the Nyquist; only a cut-off below 88% of Nyquist is flagged.
    nyquist = samplerate / 2.0
    if cutoff_hz is None:
        category_cutoff = "no_cutoff"
        distance   = None
    else:
        distance   = cutoff_hz / nyquist
        category_cutoff = ("near_nyquist_cutoff" if distance >= 0.88 else "far_cutoff")

    mask_a = freqs > 18000
    mask_m = (freqs > 15000) & (freqs < 18000)
    # Empty-mask guards: at 32 kHz (Nyquist 16 kHz) the high band does not exist
    # and a mean of an empty slice returned NaN -> invalid JSON.
    has_a = bool(np.any(mask_a))
    ratio_applies = bool(has_a and np.any(mask_m))
    if ratio_applies:
        ratio = _mean_safe(power[mask_a]) / (_mean_safe(power[mask_m]) + 1e-30)
    else:
        ratio = 0.0
    var_high = float(np.var(power_db_n[mask_a])) if has_a else 0

    sep = None
    if is_stereo and ratio_applies:
        gives  = _mean_safe(np.abs(power_l[mask_a] - power_r[mask_a]))
        dm  = _mean_safe(np.abs(power_l[mask_m] - power_r[mask_m]))
        sep = float(gives / (dm + 1e-30))

    # Vector 1: Mid/Side asymmetry. Without the decomposition (direct API use)
    # the detector is not applicable: never guess it from L/R, which would flag
    # an ordinary stereo difference as joint-stereo collapse.
    if power_mid is None or power_side is None:
        asymmetry = _analyze_mid_side_asymmetry(
            _spectrum_dbfs(power_l, gain_noise), _spectrum_dbfs(power_r, gain_noise),
            freqs, samplerate, is_mono_real=True)
    else:
        mid_db = _spectrum_dbfs(power_mid, gain_noise)
        side_db = _spectrum_dbfs(power_side, gain_noise)
        asymmetry = _analyze_mid_side_asymmetry(
            mid_db, side_db, freqs, samplerate, is_mono_real=not is_stereo)

    # Vector 3: Hi-Res morphology, measured on the Mid spectrum calibrated to
    # dBFS per bin so the absolute thermal-floor thresholds are meaningful.
    if power_mid is None or power_side is None:
        spectrum_mid_db = _spectrum_dbfs(power_mid if power_mid is not None else power_l,
                                         gain_noise)
    else:
        spectrum_mid_db = mid_db
    hires = _diagnose_hires_authenticity(freqs, spectrum_mid_db, samplerate,
                                             cutoff_hz=cutoff_hz)

    # A DSD transfer closes its musical band at the SACD passband (20 kHz) while
    # the noise-shaping ramp continues above it. That edge is the passband of the
    # format, not a codec band limit and not a Nyquist-relative step, so it is not
    # reported as a cut-off at all: the ramp itself rules the truncation out.
    if hires["es_transferencia_dsd"] and cutoff_hz is not None:
        cutoff_hz = None
        category_cutoff = "no_cutoff"
        distance = None

    # Transition-band slope around the ceiling: distinguishes a studio
    # anti-aliasing filter (gentle) from a brickwall or a resampler (a cliff).
    gradient_edge = _gradient_edge_db_khz(freqs, power_db_n, ceiling_hz, nyquist)

    # Spectral centroid and acoustic decay: a solo instrument or a low-centroid
    # acoustic mix ends its band early without any codec signature, and that is
    # the only case where a low ceiling is not a defect. The centroid separates
    # it from a bass-heavy electronic mix (centroid far below 1800 Hz).
    sum_power = float(np.sum(power))
    centroid_hz = (float(np.sum(freqs * power) / sum_power)
                    if sum_power > 1e-30 else 0.0)
    smooth_acoustic_decay = bool(
        0.0 < centroid_hz < 1800.0
        and cutoff_hz is None
        and ceiling_hz >= 11000.0
        and gradient_edge < 20.0
    )

    return {
        "error":              None,
        "techo_hz":           round(ceiling_hz),
        "techo_rel_nyquist":  round(ceiling_hz / nyquist, 4) if nyquist else 0.0,
        "clase_corte":        category_cutoff,
        "corte_detectado":    cutoff_hz is not None,
        "frecuencia_corte":   round(cutoff_hz) if cutoff_hz else None,
        "distancia_nyquist":  round(distance, 4) if distance is not None else None,
        # Compatibility: "corte_artificial" now means "confirmed accusation"
        # (a cut-off clearly below the Nyquist). It used to be enabled by any
        # step, including the filter of a master.
        "corte_artificial":   category_cutoff == "far_cutoff",
        "ratio":              ratio,
        "ratio_aplica":       ratio_applies,
        "var_alta_db":        var_high,
        "separacion_stereo":  sep,
        # Vector 1 (additive keys: old consumers ignore them).
        "caida_side_agudos_db": asymmetry["caida_side_agudos_db"],
        "delta_corte_ms_hz":    asymmetry["delta_corte_ms_hz"],
        "sospecha_joint_stereo": asymmetry["sospecha_joint_stereo"],
        "es_dual_mono":         asymmetry["es_dual_mono"],
        # Vector 3 (additive keys: old consumers ignore them).
        "muro_brickwall_nyquist":   hires["muro_brickwall_nyquist"],
        "espejo_aliasing_detectado": hires["espejo_aliasing_detectado"],
        "rolloff_analogico_genuino": hires["rolloff_analogico_genuino"],
        "gradiente_nyquist_db_khz": hires["gradiente_nyquist_db_khz"],
        "salto_ultrasonico_db":     hires["salto_ultrasonico_db"],
        "correlacion_espejo_22k":   hires["correlacion_espejo_22k"],
        "es_transferencia_dsd":     hires["es_transferencia_dsd"],
        "perfil_dsd":               hires["perfil_dsd"],
        # Transition band around the ceiling (dB/kHz of steepest descent).
        "gradiente_borde_db_khz":   gradient_edge,
        "centroide_hz":             (round(centroid_hz, 1)
                                     if math.isfinite(centroid_hz) else 0.0),
        "decaimiento_acustico_suave": smooth_acoustic_decay,
        # Psychoacoustic metrics: the streaming path overwrites them with
        # _psycho.resultado(). Direct callers get the neutral default so every key
        # exists without an STFT-driven accumulator.
        "iqr_corte_hz":             0.0,
        "ratio_huecos_mdct":        0.0,
        "caida_opus_20k_db":        0.0,
        "sospecha_corte_opus_20k":  False,
        "brecha_p90_p10_agudos_db": 0.0,
        "ratio_apagado_sfb21":      0.0,
        "islas_mdct_sfb21":         False,
        "corr_env_excitador":       0.0,
        "muesca_excitador_db":      0.0,
        "sospecha_excitador_armonico": False,
        "caida_modulacion_20k_db":  0.0,
        "brecha_modulacion_20k_db": 0.0,
        "modulacion_psicoacustica_20k": False,
        "nyquist":            samplerate // 2,
        "espectrograma":      None,
    }

def analyze_spectrum(data, samplerate, mode, seconds=None, save_png=False, path=None):
    """Spectral analysis over an already loaded array (direct API and oracle of
    the tests). The production path uses _BlockAnalysis, which does not load
    the track. Both paths share the selector and the float32 STFT, so they must
    give exactly the same metrics.

    `mode` and `seconds` are kept in the signature for API/cache compatibility;
    the selector already covers the whole file, so they do not narrow the window."""
    try:
        is_stereo = data.shape[1] >= 2
        accumulator = _SpectralAccumulator(
            samplerate, is_stereo,
            save_png=bool(save_png) and PIL_OK and bool(path),
            path=path)
        for start in range(0, len(data), FRAMES_BY_BLOCK):
            accumulator.add(data[start : start + FRAMES_BY_BLOCK], start)
        return accumulator.result()
    except Exception as e:
        return {"error": str(e)}

ACTIVE_RMS_THRESHOLD_DB = -65.0      # below this a block is silence for the selector

def _needed_active_blocks(samplerate):
    """K blocks, chosen so that the selected audio is at least SPECTRO_SECONDS
    long. With FRAMES_BY_BLOCK fixed, K grows with the sample rate."""
    return max(1, int(math.ceil(SPECTRO_SECONDS * float(samplerate) / FRAMES_BY_BLOCK)))

def _activity_score(block, last_sample=None, mono=None):
    """(RMS in dBFS, E_diff) of a block. E_diff = mean((x[t]-x[t-1])^2) is a
    cheap high-frequency/transient indicator: it is large on cymbals and attacks
    and almost zero on a sustained bass line. The last sample of the previous
    block is carried over so the difference is not lost at the boundary."""
    if mono is None:
        if block.ndim > 1:
            n_ch = block.shape[1]
            if n_ch == 1:
                mono = block[:, 0]
            elif n_ch == 2:
                mono = (block[:, 0] + block[:, 1]) * 0.5
            else:
                mono = block.mean(axis=1)
        else:
            mono = block
    if mono.size == 0:
        return -math.inf, 0.0

    sumsq = float(np.dot(mono, mono))
    if sumsq > 0.0:
        rms_db = 10.0 * math.log10(sumsq / mono.size)
    else:
        rms_db = -math.inf

    if last_sample is None:
        diffs = np.diff(mono)
        if diffs.size:
            e_diff = float(np.dot(diffs, diffs) / diffs.size)
        else:
            e_diff = 0.0
    else:
        diffs = np.diff(mono)
        first_diff = float(mono[0]) - float(last_sample)
        total_sq = first_diff * first_diff + (float(np.dot(diffs, diffs)) if diffs.size else 0.0)
        e_diff = float(total_sq / mono.size)
    return rms_db, e_diff

def _stft_complex(channel, nperseg, hop, window, scale, frame_buf=None):
    """float32 STFT with sliding_window_view: no copies beyond the windowed
    frames, and the result stays in single precision (half the RAM of float64)."""
    x = np.ascontiguousarray(channel, dtype=np.float32)
    frames = np.lib.stride_tricks.sliding_window_view(x, nperseg)[::hop]
    if frame_buf is not None and frame_buf.shape == frames.shape:
        np.multiply(frames, window, out=frame_buf)
        spectrum = np.fft.rfft(frame_buf, axis=1)
    else:
        frames = frames * window
        spectrum = np.fft.rfft(frames, axis=1)
    spectrum *= scale
    return spectrum

_FLOOR_F32 = np.float32(1e-28)

def _power_from_fft(spectrum, out=None, tmp=None):
    """|Z|^2 without np.sqrt, with the same 1e-28 floor used for the dB scale."""
    if out is not None and tmp is not None and out.shape == spectrum.shape:
        np.multiply(spectrum.real, spectrum.real, out=out)
        np.multiply(spectrum.imag, spectrum.imag, out=tmp)
        np.add(out, tmp, out=out)
        np.maximum(out, _FLOOR_F32, out=out)
        return out
    power = spectrum.real ** 2 + spectrum.imag ** 2
    np.maximum(power, _FLOOR_F32, out=power)
    return power


def _freq_slice(freqs, lo, hi=None, hi_inclusive=True):
    """Contiguous slice on a sorted frequency vector (avoids 2D boolean copies)."""
    i0 = int(np.searchsorted(freqs, lo, side="left"))
    if hi is None:
        i1 = int(freqs.size)
    else:
        i1 = int(np.searchsorted(freqs, hi, side="right" if hi_inclusive else "left"))
    return slice(i0, i1), (i1 > i0)


def _exact_linear_threshold(thr):
    """Exact float32 linear power threshold equivalent to `10*log10(max(p, 1e-28)) > thr`."""
    thr_lin = (10.0 ** (thr.astype(np.float64) / 10.0)).astype(np.float32)
    for _ in range(16):
        too_high = (10.0 * np.log10(np.maximum(thr_lin, 1e-28))) > thr
        if not np.any(too_high):
            break
        thr_lin = np.where(too_high, np.nextafter(thr_lin, np.float32(0.0)), thr_lin)
    for _ in range(16):
        nxt = np.nextafter(thr_lin, np.float32(np.inf))
        too_low = (10.0 * np.log10(np.maximum(nxt, 1e-28))) <= thr
        if not np.any(too_low):
            break
        thr_lin = np.where(too_low, nxt, thr_lin)
    return thr_lin

class PsychoacousticSTFTAccumulator:
    """Vector 2: temporal jitter of the cut-off and MDCT spectral holes.

    An LTI mastering filter cuts at the same frequency in every frame; a lossy
    codec decides per frame, so its instantaneous cut-off wanders (high IQR) and
    it zeroes whole 600 Hz sub-bands that look like rectangular holes. Only
    frames with real energy in 4-11 kHz are judged: silence has no cut-off to
    measure.

    The power spectra come from `_stft_complejo`, already normalised by the
    window sum; the unit-variance white-noise gain is removed so the -52 dBFS
    gate is independent of nperseg."""

    BAND_THRESHOLD_DBFS = -52.0
    START_SUBBAND_HZ = 13500.0
    WIDTH_SUBBAND_HZ = 600.0
    N_SUBBANDS = 10
    FALL_GAP_DB = 22.0
    MIN_FRAMES_FC = 8

    def __init__(self, samplerate):
        self.sr = int(samplerate)
        self._fc = []
        self._frames = 0
        self._frames_with_gap = 0
        self._sum_power = None
        self._freqs = None
        self._error = None
        # sfb21 islands (per active frame, dB relative to the 4-11 kHz band) and
        # the paired envelopes used by the harmonic-exciter detector.
        self._e_sfb21 = []
        self._env_8_14 = []
        self._env_16_20 = []
        # Upper-band psychoacoustic modulation (16.0-19.5 kHz vs 20.2-21.8 kHz)
        # and frame-level Mid/Side stereo collapse in 13.5-16.5 kHz.
        self._e_16_195 = []
        self._e_202_218 = []
        self._ms_excess = []

    def add(self, power, freqs, window, power_mid=None, power_side=None):
        """Consumes the (frames, bins) power of one selected block."""
        if self._error or power.shape[0] == 0:
            return
        try:
            n = window.size
            sum_w = float(np.sum(window, dtype=np.float64))
            if sum_w <= 0.0:
                return
            # Mean bin power of unit-variance white noise with this window/FFT.
            gain = _white_noise_gain(n, window)
            sl_band, has_band = _freq_slice(freqs, 4000.0, 11000.0, True)
            if not has_band or gain <= 0.0:
                return

            band = (power[:, sl_band].mean(axis=1, dtype=np.float64)
                    / gain)
            # Upper-band modulation (16.0-19.5 kHz vs 20.2-21.8 kHz) uses a
            # -78 dBFS gate so quieter passages where a full-band codec starves
            # its top scalefactor band are also captured.
            if 41000 <= self.sr <= 48000:
                mod_mask = band > 10.0 ** (-78.0 / 10.0)
                if np.any(mod_mask):
                    sl_16_195, ok_16_195 = _freq_slice(freqs, 16000.0, 19500.0, True)
                    hi_20 = min(21800.0, self.sr * 0.495)
                    sl_202_218, ok_202_218 = _freq_slice(freqs, 20200.0, hi_20, True)
                    if ok_16_195 and ok_202_218:
                        bp_m = power if np.all(mod_mask) else power[mod_mask]
                        e_16 = 10.0 * np.log10(np.maximum(
                            bp_m[:, sl_16_195], _FLOOR_F32)).mean(axis=1, dtype=np.float64)
                        e_20 = 10.0 * np.log10(np.maximum(
                            bp_m[:, sl_202_218], _FLOOR_F32)).mean(axis=1, dtype=np.float64)
                        self._e_16_195.extend(e_16.tolist())
                        self._e_202_218.extend(e_20.tolist())

            active_mask = band > 10.0 ** (self.BAND_THRESHOLD_DBFS / 10.0)
            if not np.any(active_mask):
                return
            all_active = bool(np.all(active_mask))
            block_power = power if all_active else power[active_mask]
            self._frames += int(block_power.shape[0])
            self._freqs = freqs
            block_sum = block_power.sum(axis=0, dtype=np.float64)
            self._sum_power = (block_sum if self._sum_power is None
                               else self._sum_power + block_sum)

            # Instantaneous cut-off: last bin above -60 dB relative to the
            # frame's 4-11 kHz mean power. Referencing the frame peak made a
            # strong bass sweep the reference and produced fake cut-off jitter.
            ref_db = 10.0 * np.log10(np.maximum(
                block_power[:, sl_band].mean(axis=1), _FLOOR_F32))

            # sfb21 islands and the exciter envelopes. Both are referenced to the
            # 4-11 kHz band of the SAME frame: a 1-8 kHz reference would follow a
            # strong bass line and make a continuous high band look intermittent.
            if self.sr >= 41000:
                sl_8_14, ok_8_14 = _freq_slice(freqs, 8000.0, 14000.0, True)
                sl_sfb21, ok_sfb21 = _freq_slice(freqs, 16000.0, 20500.0, True)
                if ok_8_14 and ok_sfb21:
                    db_8_14 = 10.0 * np.log10(np.maximum(
                        block_power[:, sl_8_14].mean(axis=1, dtype=np.float64), 1e-28))
                    db_16_20 = 10.0 * np.log10(np.maximum(
                        block_power[:, sl_sfb21].mean(axis=1, dtype=np.float64), 1e-28))
                    e_sfb21 = db_16_20 - ref_db.astype(np.float64)
                    self._e_sfb21.extend(e_sfb21.tolist())
                    self._env_8_14.extend(db_8_14.tolist())
                    self._env_16_20.extend(db_16_20.tolist())

            # Frame-level Mid/Side stereo collapse in 13.5-16.5 kHz vs 2-8 kHz.
            if power_side is not None and self.sr >= 41000:
                sl_ms_lo, ok_ms_lo = _freq_slice(freqs, 2000.0, 8000.0, True)
                sl_ms_hi, ok_ms_hi = _freq_slice(freqs, 13500.0, 16500.0, True)
                if ok_ms_lo and ok_ms_hi:
                    ps_act = power_side if all_active else power_side[active_mask]
                    if power_mid is not None:
                        pm_act = power_mid if all_active else power_mid[active_mask]
                    else:
                        pm_act = np.maximum(block_power - ps_act, _FLOOR_F32)
                    m_lo = 10.0 * np.log10(np.maximum(
                        pm_act[:, sl_ms_lo].mean(axis=1, dtype=np.float64), 1e-28))
                    s_lo = 10.0 * np.log10(np.maximum(
                        ps_act[:, sl_ms_lo].mean(axis=1, dtype=np.float64), 1e-28))
                    m_hi = 10.0 * np.log10(np.maximum(
                        pm_act[:, sl_ms_hi].mean(axis=1, dtype=np.float64), 1e-28))
                    s_hi = 10.0 * np.log10(np.maximum(
                        ps_act[:, sl_ms_hi].mean(axis=1, dtype=np.float64), 1e-28))
                    valid_ms = (m_hi > (ref_db.astype(np.float64) - 35.0)) & ((m_lo - s_lo) < 20.0)
                    if np.any(valid_ms):
                        excess = (m_hi[valid_ms] - s_hi[valid_ms]) - (m_lo[valid_ms] - s_lo[valid_ms])
                        self._ms_excess.extend(excess.tolist())

            # A cut-off is only meaningful when the frame also has energy in
            # 10-14 kHz; otherwise a voice/guitar frame with no cymbals looks
            # like a codec cutting and its on/off nature fakes jitter.
            # Cap the upper bound at 22.05 kHz so natural ultrasonic decay in
            # 96/192 kHz Hi-Res recordings is not mistaken for codec jitter.
            sl_presence, ok_presence = _freq_slice(freqs, 10000.0, 14000.0, True)
            sl_hf, ok_hf = _freq_slice(freqs, 11000.0, min(22050.0, self.sr * 0.49), True)
            if ok_presence and ok_hf:
                presence_db = 10.0 * np.log10(np.maximum(
                    block_power[:, sl_presence].mean(axis=1), _FLOOR_F32))
                idx_pres = np.flatnonzero(presence_db > (ref_db - np.float32(25.0)))
                if idx_pres.size:
                    all_pres = idx_pres.size == block_power.shape[0]
                    bp_hf = block_power[:, sl_hf] if all_pres else block_power[idx_pres, sl_hf]
                    thr_hf = (ref_db if all_pres else ref_db[idx_pres]) - np.float32(60.0)
                    thr_lin = _exact_linear_threshold(thr_hf)
                    over_hf = bp_hf > thr_lin[:, None]
                    has_over = np.any(over_hf, axis=1)
                    if np.any(has_over):
                        over_valid = over_hf if np.all(has_over) else over_hf[has_over]
                        last = (over_valid.shape[1] - 1
                                - np.argmax(over_valid[:, ::-1], axis=1))
                        self._fc.extend(freqs[sl_hf][last].tolist())

            # MDCT holes: 10 x 600 Hz sub-bands in 13.5-19.5 kHz.
            if self.sr < 39000:
                return                     # the band does not exist
            subbands = []
            for i in range(self.N_SUBBANDS):
                lo = self.START_SUBBAND_HZ + i * self.WIDTH_SUBBAND_HZ
                hi = lo + self.WIDTH_SUBBAND_HZ
                sl_sb, ok_sb = _freq_slice(freqs, lo, hi, False)
                if not ok_sb:
                    return
                subbands.append(10.0 * np.log10(
                    np.maximum(block_power[:, sl_sb].mean(axis=1), 1e-28)))
            sb = np.column_stack(subbands)
            gap = np.any(
                (sb[:, 1:-1] < sb[:, :-2] - self.FALL_GAP_DB)
                & (sb[:, 1:-1] < sb[:, 2:] - self.FALL_GAP_DB),
                axis=1,
            )
            self._frames_with_gap += int(np.count_nonzero(gap))
        except Exception as e:             # never abort the analysis of a file
            self._error = str(e)

    def result(self):
        fc = np.asarray(self._fc, dtype=np.float64)
        if fc.size >= self.MIN_FRAMES_FC and np.all(np.isfinite(fc)):
            iqr = float(np.percentile(fc, 75) - np.percentile(fc, 25))
        else:
            iqr = 0.0
        ratio = (self._frames_with_gap / float(self._frames)
                 if self._frames else 0.0)

        # Vector 5: libopus fullband applies a deterministic 20.00 kHz low-pass
        # (a >45 dB cliff between 19.85 and 20.15 kHz) while 18-19.7 kHz stays
        # flat. A mastering anti-alias filter can look the same in isolation, so
        # this is reported as a fact and only scored with corroborating evidence.
        fall_opus = 0.0
        opus_suspicion = False
        if (self._sum_power is not None and self._freqs is not None
                and self._frames >= self.MIN_FRAMES_FC and self.sr >= 41000):
            freqs = self._freqs
            mean = self._sum_power / float(self._frames)

            def level_db(lo, hi):
                mask = (freqs >= lo) & (freqs < hi)
                if not np.any(mask):
                    return -300.0
                return 10.0 * math.log10(max(float(np.mean(mean[mask])), 1e-28))

            low = level_db(18000.0, 19700.0)
            before = level_db(19700.0, 19850.0)
            after = level_db(20150.0, 20300.0)
            fall_opus = before - after
            mask_plano = (freqs >= 18000.0) & (freqs <= 19700.0)
            plano = False
            if np.count_nonzero(mask_plano) > 8:
                curva = 10.0 * np.log10(np.maximum(mean[mask_plano], 1e-28))
                plano = bool(np.percentile(curva, 95)
                             - np.percentile(curva, 5) < 6.0)
            opus_suspicion = bool(fall_opus > 45.0 and plano
                                 and after < low - 40.0)

        # sfb21 islands: a lowpass-disabled lossy codec (LAME -V 0, AAC without
        # lowpass) zeroes the top scalefactor band in some frames but not in
        # others, so 16-20.5 kHz switches on and off relative to 4-11 kHz.
        brecha_sfb21 = 0.0
        ratio_off = 0.0
        islands_sfb21 = False
        if len(self._e_sfb21) >= 24:
            arr_sfb = np.asarray(self._e_sfb21, dtype=np.float64)
            if np.all(np.isfinite(arr_sfb)):
                brecha_sfb21 = float(np.percentile(arr_sfb, 90)
                                     - np.percentile(arr_sfb, 10))
                ratio_off = float(np.mean(arr_sfb < -55.0))
                islands_sfb21 = bool(brecha_sfb21 > 28.0
                                   and 0.25 <= ratio_off <= 0.85)

        # Harmonic exciter / SBR: a bandwidth extension rebuilds the high band
        # from the 8-14 kHz envelope (so both move together) and leaves a notch
        # or a step where the original band was cut.
        corr_env = 0.0
        notch_db = 0.0
        exciter_suspicion = False
        if (len(self._env_8_14) >= 24 and self._sum_power is not None
                and self._freqs is not None and self.sr >= 41000):
            arr_lo = np.asarray(self._env_8_14, dtype=np.float64)
            arr_hi = np.asarray(self._env_16_20, dtype=np.float64)
            if (np.all(np.isfinite(arr_lo)) and np.all(np.isfinite(arr_hi))
                    and float(np.std(arr_lo)) > 1.5
                    and float(np.std(arr_hi)) > 1.5):
                corr_env = float(np.corrcoef(arr_lo, arr_hi)[0, 1])
                if not math.isfinite(corr_env):
                    corr_env = 0.0
            freqs = self._freqs
            mean = self._sum_power / float(self._frames)
            curva_db = 10.0 * np.log10(np.maximum(mean, 1e-28))
            mask_pre = (freqs >= 14500.0) & (freqs <= 15800.0)
            mask_post = (freqs >= 16200.0) & (freqs <= 17500.0)
            mask_notch = (freqs >= 14800.0) & (freqs <= 16500.0)
            mask_flank_lo = (freqs >= 13500.0) & (freqs < 14800.0)
            mask_flank_hi = (freqs > 16500.0) & (freqs <= 18500.0)
            jump_band = 0.0
            if np.any(mask_pre) and np.any(mask_post):
                # Signed: only a DROP going up in frequency is an exciter notch.
                # An ascending step (mean(post) > mean(pre)) gives a negative
                # value and must not fire the notch detector.
                jump_band = float(np.mean(curva_db[mask_pre])
                                    - np.mean(curva_db[mask_post]))
            prof_notch = 0.0
            if (np.any(mask_notch) and np.any(mask_flank_lo)
                    and np.any(mask_flank_hi)):
                flancos = 0.5 * float(np.mean(curva_db[mask_flank_lo])
                                      + np.mean(curva_db[mask_flank_hi]))
                prof_notch = float(flancos - np.min(curva_db[mask_notch]))
            notch_db = max(0.0, jump_band, prof_notch)
            exciter_suspicion = bool(corr_env > 0.92 and notch_db >= 15.0)

        # Upper-band psychoacoustic modulation (16.0-19.5 kHz vs 20.2-21.8 kHz):
        # high-bitrate codecs (MP3 V0/320, AAC 256/320, Vorbis Q6/Q9, Opus, WMA)
        # dynamically starve or quantize the >20 kHz band frame-by-frame, which
        # creates a wide P95-P05 spread in the 16-19.5 -> 20.2-21.8 kHz drop, or
        # combines a deep >48 dB drop with cut-off jitter. Static LTI filters
        # have near-zero modulation spread across active frames.
        drp_med = 0.0
        drp_iqr = 0.0
        psycho_mod_20k = False
        if len(self._e_16_195) >= 16 and 41000 <= self.sr <= 48000:
            arr_16 = np.asarray(self._e_16_195, dtype=np.float64)
            arr_20 = np.asarray(self._e_202_218, dtype=np.float64)
            if np.all(np.isfinite(arr_16)) and np.all(np.isfinite(arr_20)):
                drp = arr_16 - arr_20
                drp_med = float(np.median(drp))
                drp_iqr = float(np.percentile(drp, 95) - np.percentile(drp, 5))
                top_iqr = float(np.percentile(arr_20, 95) - np.percentile(arr_20, 5))
                psycho_mod_20k = bool(
                    (drp_iqr >= 10.0 and top_iqr >= 5.0 and drp_med >= 32.0)
                    or (drp_iqr >= 30.0 and top_iqr >= 30.0)
                    or (drp_med >= 48.0 and drp_iqr >= 12.0 and iqr > 550.0)
                )

        # Dynamic frame-by-frame Mid/Side stereo collapse in 13.5-16.5 kHz:
        # LAME/AAC Joint Stereo switches between LR and MS per frame, so the
        # 95th percentile of the M/S excess is high and its frame spread is wide.
        self._ms_p95_db = 0.0
        self._ms_dynamic_collapse = False
        if len(self._ms_excess) >= 16:
            arr_ms = np.asarray(self._ms_excess, dtype=np.float64)
            if np.all(np.isfinite(arr_ms)):
                ms_p95 = float(np.percentile(arr_ms, 95))
                ms_iqr = float(np.percentile(arr_ms, 90) - np.percentile(arr_ms, 10))
                self._ms_p95_db = ms_p95
                self._ms_dynamic_collapse = bool(ms_p95 > 12.0 and ms_iqr > 5.0)

        return {
            "iqr_corte_hz":      round(iqr, 1) if math.isfinite(iqr) else 0.0,
            "ratio_huecos_mdct": round(ratio, 4) if math.isfinite(ratio) else 0.0,
            "caida_opus_20k_db": (round(fall_opus, 2)
                                  if math.isfinite(fall_opus) else 0.0),
            "sospecha_corte_opus_20k": opus_suspicion,
            "brecha_p90_p10_agudos_db": (round(brecha_sfb21, 2)
                                         if math.isfinite(brecha_sfb21) else 0.0),
            "ratio_apagado_sfb21":      (round(ratio_off, 4)
                                         if math.isfinite(ratio_off) else 0.0),
            "islas_mdct_sfb21":         islands_sfb21,
            "corr_env_excitador":       (round(corr_env, 4)
                                         if math.isfinite(corr_env) else 0.0),
            "muesca_excitador_db":      (round(notch_db, 2)
                                         if math.isfinite(notch_db) else 0.0),
            "sospecha_excitador_armonico": exciter_suspicion,
            "caida_modulacion_20k_db":  (round(drp_med, 2)
                                         if math.isfinite(drp_med) else 0.0),
            "brecha_modulacion_20k_db": (round(drp_iqr, 2)
                                         if math.isfinite(drp_iqr) else 0.0),
            "modulacion_psicoacustica_20k": psycho_mod_20k,
        }

class _SpectralAccumulator:
    """Keeps the K most active blocks in memory and runs the STFT on them at the
    end. Shared by the streaming path and the array path so both give the same
    metrics. Blocks are never concatenated: a frame must not cross the splice
    between two non-adjacent blocks, which would invent high-frequency content."""

    def __init__(self, samplerate, is_stereo=None, save_png=False, path=None,
                 renderer=None):
        self.sr = int(samplerate)
        self.is_stereo = is_stereo
        self.save_png = bool(save_png)
        self.path = path
        self._renderer = renderer or _save_spectrogram
        self._k = _needed_active_blocks(self.sr)
        self._seleccion = []          # min-heap by (E_diff, -index)
        self._backup = None         # loudest block, used if none passes -65 dBFS
        self._index = 0
        self._last = None
        self._error = None
        self._psycho = PsychoacousticSTFTAccumulator(self.sr)

    def add(self, block, start=0, mono=None):
        if self._error or block.shape[0] == 0:
            return
        try:
            rms_db, e_diff = _activity_score(block, self._last, mono=mono)
            if block.ndim > 1:
                self._last = float(np.mean(block[-1]))
                if self.is_stereo is None:
                    self.is_stereo = block.shape[1] >= 2
            else:
                self._last = float(block[-1])
                self.is_stereo = False

            index = self._index
            self._index += 1
            needs_backup = self._backup is None or rms_db > self._backup[0]
            above_gate = rms_db > ACTIVE_RMS_THRESHOLD_DB
            needs_heap = above_gate and (
                len(self._seleccion) < self._k
                or (e_diff, -index) > self._seleccion[0][:2]
            )
            if not needs_backup and not needs_heap:
                return

            audio = np.asarray(block, dtype=np.float32)
            if needs_backup:
                self._backup = (rms_db, -index, index, int(start), audio)
            if needs_heap:
                entry = (e_diff, -index, index, int(start), audio)
                if len(self._seleccion) < self._k:
                    heapq.heappush(self._seleccion, entry)
                else:
                    heapq.heapreplace(self._seleccion, entry)
        except Exception as e:            # never abort the analysis of the file
            self._error = str(e)

    def result(self):
        if self._error:
            return {"error": self._error}
        chosen = self._seleccion or ([self._backup] if self._backup else [])
        if not chosen:
            return {"error": "could not analyze the spectrum"}
        chosen = sorted(chosen, key=lambda item: item[2])   # chronological

        lengths = [len(item[4]) for item in chosen]
        total_samples = sum(lengths)
        nperseg = max(256, min(4096, total_samples // 8))
        nperseg = min(nperseg, max(lengths))
        nperseg = max(32, nperseg)
        hop = max(1, nperseg // 8)      # 87.5 % overlap: enough time detail for the PNG

        window = np.hanning(nperseg + 1)[:-1].astype(np.float32)   # periodic Hann
        scale = np.float32(1.0 / float(window.sum()))
        gain_noise = _white_noise_gain(nperseg, window)
        frames_estimados = sum((n - nperseg) // hop + 1
                               for n in lengths if n >= nperseg)
        if frames_estimados <= 0:
            return {"error": "could not analyze the spectrum"}
        step_png = max(1, frames_estimados // MAX_FRAMES_PNG)
        # Display-only cap: a 75-87 % overlap gives far more frames than the PNG
        # can show, and that per-column detail is random noise that JPEG cannot
        # compress (it used to bloat the PDF). One displayed frame per 1/25 s
        # keeps the image faithful and the PDF small; metrics still average all
        # frames, so no evidence is lost.
        step_png = max(step_png,
                       int(round(self.sr / (hop * float(PNG_SPECTROGRAM_FPS)))))

        sum_l = sum_r = sum_total = None
        sum_mid = sum_side = None
        freqs = np.fft.rfftfreq(nperseg, 1.0 / self.sr)
        frames = 0
        frames_global = 0
        png_power, png_times, png_n = [], [], 0
        frame_buf = buf_a = buf_b = buf_tmp = None
        medium = np.float32(0.5)

        for _, _, _, start, audio in chosen:
            if len(audio) < nperseg:
                continue                  # a very short trailing block
            n_frames_blk = (len(audio) - nperseg) // hop + 1
            if frame_buf is None or frame_buf.shape != (n_frames_blk, nperseg):
                frame_buf = np.empty((n_frames_blk, nperseg), dtype=np.float32)
            zl = _stft_complex(audio[:, 0], nperseg, hop, window, scale,
                               frame_buf=frame_buf)
            if buf_a is None or buf_a.shape != zl.shape:
                buf_a = np.empty(zl.shape, dtype=np.float32)
                buf_b = np.empty(zl.shape, dtype=np.float32)
                buf_tmp = np.empty(zl.shape, dtype=np.float32)
            power_l = _power_from_fft(zl, out=buf_a, tmp=buf_tmp)
            cur_sum_l = power_l.sum(axis=0, dtype=np.float32)
            sum_l = cur_sum_l if sum_l is None else sum_l + cur_sum_l

            if self.is_stereo:
                zr = _stft_complex(audio[:, 1], nperseg, hop, window, scale,
                                   frame_buf=frame_buf)
                power_r = _power_from_fft(zr, out=buf_a, tmp=buf_tmp)
                cur_sum_r = power_r.sum(axis=0, dtype=np.float32)
                sum_r = cur_sum_r if sum_r is None else sum_r + cur_sum_r

                zm = (zl + zr) * medium
                zs = (zl - zr) * medium
                del zl, zr

                power_mid = _power_from_fft(zm, out=buf_a, tmp=buf_tmp)
                del zm
                cur_sum_mid = power_mid.sum(axis=0, dtype=np.float32)
                sum_mid = cur_sum_mid if sum_mid is None else sum_mid + cur_sum_mid

                power_side = _power_from_fft(zs, out=buf_b, tmp=buf_tmp)
                del zs
                cur_sum_side = power_side.sum(axis=0, dtype=np.float32)
                sum_side = cur_sum_side if sum_side is None else sum_side + cur_sum_side

                np.add(power_mid, power_side, out=power_mid)
                power_total = power_mid
                cur_sum_total = power_total.sum(axis=0, dtype=np.float32)
                sum_total = cur_sum_total if sum_total is None else sum_total + cur_sum_total
            else:
                del zl
                power_total = power_l
                sum_r = sum_l
                sum_total = sum_l

            self._psycho.add(power_total, freqs, window,
                             power_side=power_side if self.is_stereo else None)

            n_frames = power_total.shape[0]
            if self.save_png:
                remaining = MAX_FRAMES_PNG - png_n
                if remaining > 0:
                    i0 = (-frames_global) % step_png
                    idx = np.arange(i0, n_frames, step_png)[:remaining]
                    if idx.size:
                        # power_total is (frames, bins): keep the time rows here and
                        # transpose once, so the PNG gets (bins, frames).
                        png_power.append(power_total[idx])
                        png_times.append((start + idx * hop) / float(self.sr))
                        png_n += idx.size
            frames_global += n_frames
            frames += n_frames

        if frames == 0 or sum_l is None:
            return {"error": "could not analyze the spectrum"}

        mean_l = sum_l / np.float32(frames)
        mean_r = mean_l if not self.is_stereo else sum_r / np.float32(frames)
        mean_total = sum_total / np.float32(frames)
        mean_mid = (sum_mid / np.float32(frames)) if self.is_stereo else None
        mean_side = (sum_side / np.float32(frames)) if self.is_stereo else None
        spectrum = _spectral_metrics(freqs, mean_l, mean_r, self.sr,
                                    is_stereo=self.is_stereo,
                                    power=mean_total,
                                    power_mid=mean_mid, power_side=mean_side,
                                    gain_noise=gain_noise)
        spectrum.update(self._psycho.result())
        if (getattr(self._psycho, "_ms_dynamic_collapse", False)
                and not spectrum.get("es_dual_mono")):
            spectrum["sospecha_joint_stereo"] = True
            ms_p95 = float(getattr(self._psycho, "_ms_p95_db", 0.0))
            if math.isfinite(ms_p95) and ms_p95 > spectrum.get("caida_side_agudos_db", 0.0):
                spectrum["caida_side_agudos_db"] = round(ms_p95, 2)
        if self.save_png and png_power:
            png_path, error = self._renderer(
                freqs, np.concatenate(png_times),
                np.concatenate(png_power, axis=0).T,
                self.sr, self.path, spectrum["techo_hz"], spectrum["frecuencia_corte"])
            spectrum["espectrograma"] = png_path
            if error:
                spectrum["espectrograma_error"] = error
        return spectrum
