# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 FLAC VERIFIER Contributors
"""Spectral analysis, spectrogram rendering and forensic spectral detectors.

Extracted from the motor facade in Phase 4. The STFT is float32 with
sliding_window_view; the PNG is NumPy + Pillow (no matplotlib).
"""
from dataclasses import dataclass
from enum import Enum
import functools
import heapq
import inspect
import importlib.machinery
import importlib.util
import math
import os
import sys
import threading

import numpy as np


class CutoffClass(str, Enum):
    """Distance-to-Nyquist classification for a detected spectral step."""

    NO_CUTOFF = "no_cutoff"
    NEAR_NYQUIST = "near_nyquist_cutoff"
    FAR_CUTOFF = "far_cutoff"


class CodecLatticeFamily(str, Enum):
    """Standardized lapped transform and filterbank families detected in PCM."""

    MDCT_1024 = "MDCT-1024 (Vorbis/AAC)"
    PQMF_576 = "PQMF-576 (MP3)"
    MDCT_512_AC3 = "MDCT-512 (AC-3/E-AC-3)"
    PQMF_32 = "PQMF-32 (Musepack)"
    CELT_960 = "CELT-960 (Opus)"


@dataclass(frozen=True)
class CutoffAssessment:
    """Consistent value object for the coupled spectral cut-off protocol fields."""

    cutoff_hz: int | None
    distance_nyquist: float | None
    cutoff_class: CutoffClass

    @property
    def detected(self) -> bool:
        return self.cutoff_hz is not None

    @property
    def is_artificial(self) -> bool:
        return self.cutoff_class is CutoffClass.FAR_CUTOFF

    @classmethod
    def none(cls) -> "CutoffAssessment":
        return cls(
            cutoff_hz=None,
            distance_nyquist=None,
            cutoff_class=CutoffClass.NO_CUTOFF,
        )

    @classmethod
    def from_cutoff(
        cls, cutoff_hz: float | None, nyquist: float
    ) -> "CutoffAssessment":
        if cutoff_hz is None or cutoff_hz <= 0.0 or nyquist <= 0.0:
            return cls.none()
        distance = float(cutoff_hz) / float(nyquist)
        cutoff_class = (
            CutoffClass.NEAR_NYQUIST if distance >= 0.88 else CutoffClass.FAR_CUTOFF
        )
        return cls(
            cutoff_hz=round(cutoff_hz),
            distance_nyquist=distance,
            cutoff_class=cutoff_class,
        )

    def to_protocol_fields(self) -> dict:
        return {
            "clase_corte": self.cutoff_class.value,
            "corte_detectado": self.detected,
            "frecuencia_corte": self.cutoff_hz,
            "distancia_nyquist": (
                round(self.distance_nyquist, 4)
                if self.distance_nyquist is not None
                else None
            ),
            "corte_artificial": self.is_artificial,
        }


@dataclass(frozen=True)
class PsychoacousticSummary:
    """Immutable summary of frame-level psychoacoustic STFT metrics."""

    metrics: dict
    ms_p95_db: float = 0.0
    ms_dynamic_collapse: bool = False
    error: str | None = None


class FftBackendKind(str, Enum):
    """Resolved FFT backend source."""

    SYS_MODULES = "sys_modules"
    MEIPASS_EXT = "meipass_ext"
    SCIPY_PRIVATE = "scipy_private"
    SCIPY_ADAPTER = "scipy_adapter"


class DsdProfile(str, Enum):
    """Recognized DSD/SACD ultrasonic noise-shaping profiles."""

    DIRECT_24_40K = "direct_24_40k"
    SCARLET_30K = "scarlet_30k"
    DSD128_40_80K = "dsd128_40_80k"


class HiresMorphology(str, Enum):
    """Mutually exclusive Hi-Res (>= 88.2 kHz) spectral morphology classification."""

    NOT_APPLICABLE = "not_applicable"
    DSD_TRANSFER = "dsd_transfer"
    NYQUIST_BRICKWALL = "nyquist_brickwall"
    ALIASING_MIRROR = "aliasing_mirror"
    GENUINE_ANALOG_ROLLOFF = "genuine_analog_rolloff"
    FULLBAND_NORMAL = "fullband_normal"


@dataclass(frozen=True)
class HiresAssessment:
    """Mutually exclusive Hi-Res (>= 88.2 kHz) spectral morphology assessment."""

    morphology: HiresMorphology = HiresMorphology.NOT_APPLICABLE
    nyquist_gradient_db_khz: float = 0.0
    ultrasonic_jump_db: float = 0.0
    mirror_correlation_22k: float = 0.0
    dsd_profile: DsdProfile | None = None

    @classmethod
    def not_applicable(cls) -> "HiresAssessment":
        return cls(morphology=HiresMorphology.NOT_APPLICABLE)

    def to_protocol_fields(self) -> dict:
        is_dsd = self.morphology is HiresMorphology.DSD_TRANSFER
        return {
            "muro_brickwall_nyquist": self.morphology is HiresMorphology.NYQUIST_BRICKWALL,
            "espejo_aliasing_detectado": self.morphology is HiresMorphology.ALIASING_MIRROR,
            "rolloff_analogico_genuino": self.morphology is HiresMorphology.GENUINE_ANALOG_ROLLOFF,
            "gradiente_nyquist_db_khz": round(abs(float(self.nyquist_gradient_db_khz)), 2),
            "salto_ultrasonico_db": round(float(self.ultrasonic_jump_db), 2),
            "correlacion_espejo_22k": round(float(self.mirror_correlation_22k), 3),
            "es_transferencia_dsd": is_dsd,
            "perfil_dsd": (
                self.dsd_profile.value
                if (is_dsd and self.dsd_profile is not None)
                else None
            ),
        }


_POCKETFFT_LOCK = threading.Lock()
_POCKETFFT_MOD = None
_POCKETFFT_KIND: FftBackendKind | None = None


class _ScipyFftAdapter:
    """Public `scipy.fft` fallback exposing pypocketfft's r2c/c2r/dct calls
    for SciPy >= 1.16, where `scipy.fft._pocketfft` was removed."""

    def __init__(self, sp_fft):
        self._fft = sp_fft

    def r2c(self, arr, axes, forward, fct, out, nthreads):
        norm = "backward" if fct == 0 else ("ortho" if fct == 1 else "forward")
        fn = self._fft.rfft if forward else self._fft.ihfft
        res = fn(arr, axis=axes[0], norm=norm, workers=nthreads)
        if res.dtype != np.complex64:
            res = res.astype(np.complex64, copy=False)
        if out is not None:
            np.copyto(out, res)
            return out
        return res

    def c2r(self, arr, axes, lastsize, forward, fct, out, nthreads):
        norm = "backward" if fct == 2 else ("ortho" if fct == 1 else "forward")
        res = self._fft.irfft(
            arr, n=int(lastsize), axis=axes[0], norm=norm, workers=nthreads
        )
        if res.dtype != np.float32:
            res = res.astype(np.float32, copy=False)
        if out is not None:
            np.copyto(out, res)
            return out
        return res

    def dct(self, arr, dct_type, axes, fct, out, nthreads, ortho):
        res = self._fft.dct(
            arr,
            type=int(dct_type),
            norm="ortho" if ortho else "backward",
            axis=axes[0],
            workers=nthreads,
            overwrite_x=(out is arr),
        )
        if res.dtype != np.float32:
            res = res.astype(np.float32, copy=False)
        if out is not None and out is not res:
            np.copyto(out, res)
            return out
        return res


def _get_pocketfft():
    """Returns SciPy's compiled C++ pypocketfft extension without importing
    scipy.fft / scipy.special / scipy.linalg when running frozen, or a public
    scipy.fft adapter on SciPy >= 1.16 where _pocketfft was removed."""
    global _POCKETFFT_MOD, _POCKETFFT_KIND
    if _POCKETFFT_MOD is not None:
        return _POCKETFFT_MOD
    with _POCKETFFT_LOCK:
        if _POCKETFFT_MOD is not None:
            return _POCKETFFT_MOD
        existing = (
            sys.modules.get("scipy.fft._pocketfft.pypocketfft")
            or sys.modules.get("pypocketfft")
        )
        if existing is not None:
            _POCKETFFT_MOD = existing
            _POCKETFFT_KIND = FftBackendKind.SYS_MODULES
            return existing
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            for rel in (("scipy", "fft", "_pocketfft"), ()):
                folder = os.path.join(meipass, *rel)
                if not os.path.isdir(folder):
                    continue
                for suffix in importlib.machinery.EXTENSION_SUFFIXES:
                    candidate = os.path.join(folder, "pypocketfft" + suffix)
                    if os.path.isfile(candidate):
                        spec = importlib.util.spec_from_file_location("pypocketfft", candidate)
                        if spec is not None and spec.loader is not None:
                            mod = importlib.util.module_from_spec(spec)
                            sys.modules["pypocketfft"] = mod
                            spec.loader.exec_module(mod)
                            _POCKETFFT_MOD = mod
                            _POCKETFFT_KIND = FftBackendKind.MEIPASS_EXT
                            return mod
        try:
            from scipy.fft._pocketfft import pypocketfft as mod
            kind = FftBackendKind.SCIPY_PRIVATE
        except ModuleNotFoundError:
            from scipy import fft as sp_fft
            mod = _ScipyFftAdapter(sp_fft)
            kind = FftBackendKind.SCIPY_ADAPTER
        _POCKETFFT_MOD = mod
        _POCKETFFT_KIND = kind
        return mod


def _rfft_f32(x, n=None, axis=-1, out=None):
    """Single-threaded float32 real-to-complex FFT via pypocketfft."""
    arr = np.asarray(x, dtype=np.float32)
    ax = axis if axis >= 0 else arr.ndim + axis
    if n is not None:
        target_n = int(n)
        cur_n = arr.shape[ax]
        if target_n < cur_n:
            sl = [slice(None)] * arr.ndim
            sl[ax] = slice(0, target_n)
            arr = arr[tuple(sl)]
        elif target_n > cur_n:
            pad_shape = list(arr.shape)
            pad_shape[ax] = target_n
            padded = np.zeros(pad_shape, dtype=np.float32)
            sl = [slice(None)] * arr.ndim
            sl[ax] = slice(0, cur_n)
            padded[tuple(sl)] = arr
            arr = padded
    return _get_pocketfft().r2c(arr, (ax,), True, 0, out, 1)


def _irfft_f32(z, n, axis=-1, out=None):
    """Single-threaded complex64-to-float32 inverse real FFT via pypocketfft."""
    arr = np.asarray(z, dtype=np.complex64)
    ax = axis if axis >= 0 else arr.ndim + axis
    return _get_pocketfft().c2r(arr, (ax,), int(n), False, 2, out, 1)


def _dct4_ortho_f32(x, axis=-1, overwrite_x=False):
    """Single-threaded orthogonal float32 DCT-IV via pypocketfft."""
    arr = np.asarray(x, dtype=np.float32)
    ax = axis if axis >= 0 else arr.ndim + axis
    return _get_pocketfft().dct(
        arr, 4, (ax,), 1, arr if overwrite_x else None, 1, True
    )

try:
    from PIL import Image, ImageDraw, ImageFont
    PIL_OK = True
except ImportError:
    Image = ImageDraw = ImageFont = None
    PIL_OK = False

from _motor_bits import (
    AccumulatorState,
    FRAMES_BY_BLOCK,
    _classify_streaks,
    _normalize_path_fs,
)
from _motor_cache import SpectralMode, _png_folder, _png_path, _replace_with_retries

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

@functools.lru_cache(maxsize=32)
def _source_png(font_size=14):
    """TrueType font with support for the em dash; bitmap fallback otherwise."""
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
    # Resize the single-channel index raster BEFORE applying the RGB palette:
    # 3x less bilinear interpolation work and exact LUT colours (cleaner PNG).
    idx_img = Image.fromarray(idx, mode="L").resize(
        (width, height), resample=Image.Resampling.BILINEAR)
    rgb = _LUT_SPECTROGRAM[np.asarray(idx_img)]
    img_spec = Image.fromarray(rgb, mode="RGB")

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
    canvas.convert("RGB").save(output_png_path, format="PNG", compress_level=4)

def _save_spectrogram(freqs, times, mix, samplerate, audio_path, ceiling_hz, frequency_cutoff,
                      mode="full", seconds=None):
    """Draws the spectrogram atomically to a mode-scoped PNG. Returns
    (png_path, error): it never raises, and the reason for any failure is kept."""
    temp_fs = None
    try:
        if not PIL_OK:
            return None, "Pillow is not available: PNG spectrograms are disabled"
        png_path = _png_path(audio_path, mode=mode, seconds=seconds)
        png_directory = os.path.dirname(png_path)
        os.makedirs(_normalize_path_fs(png_directory), exist_ok=True)   # race-safe across processes

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

        dest_fs = _normalize_path_fs(png_path)
        temp_fs = f"{dest_fs}.{os.getpid()}.tmp"
        render_spectrogram_fast(
            db, samplerate, temp_fs, db_min=-80.0, db_max=0.0,
            title=f"Spectrogram — {os.path.basename(audio_path)}",
            ceiling_hz=ceiling_hz, frequency_cutoff=frequency_cutoff,
            times=times)
        _replace_with_retries(temp_fs, dest_fs)
        temp_fs = None
        return png_path, None
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"
    finally:
        if temp_fs is not None:
            try:
                os.unlink(temp_fs)
            except OSError:
                pass

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

def _assess_hires_authenticity(freqs_hz, spectrum_mid_db, sample_rate,
                               cutoff_hz=None, ceiling_hz=None) -> HiresAssessment:
    """Vector 3: mutually exclusive morphology assessment for Hi-Res (>= 88.2 kHz).

    Evaluates DSD first, then CD/DAT Nyquist brickwall (only if not DSD), then
    spectral aliasing mirror (only if neither DSD nor brickwall), then genuine
    analogue/acoustic roll-off (only if none of the above), returning a frozen
    `HiresAssessment` value object.
    """
    freqs = np.asarray(freqs_hz, dtype=np.float64)
    spectrum = np.asarray(spectrum_mid_db, dtype=np.float64)
    if sample_rate < 88200 or freqs.size < 8 or spectrum.shape != freqs.shape:
        return HiresAssessment.not_applicable()
    if not np.all(np.isfinite(spectrum)):
        return HiresAssessment.not_applicable()

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

    # Stage 1: DSD64/128 to PCM rising noise-shaping ramp (24-40 / 23-31 / 40-80 kHz).
    # Checked first because a DSD transfer always carries ultrasonic content and
    # must never be flagged as a brickwall or aliasing mirror.
    dsd_profile: DsdProfile | None = None
    mask_dsd = (freqs >= 24000.0) & (freqs <= 40000.0)
    if np.count_nonzero(mask_dsd) > 8:
        y = curva[mask_dsd]
        x = freqs[mask_dsd]
        slope = float(np.polyfit(x, y, 1)[0] * 1000.0)
        subida = float(y[-1] - y[0])
        monotona = float(np.mean(np.diff(y) >= 0.0)) >= 0.6
        if (slope > 0.8 and subida > 12.0 and monotona
                and float(np.max(y)) > -90.0 and jump_db < 25.0):
            dsd_profile = DsdProfile.DIRECT_24_40K

    if dsd_profile is None:
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
                dsd_profile = DsdProfile.SCARLET_30K

    if dsd_profile is None and sample_rate >= 176400:
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
                dsd_profile = DsdProfile.DSD128_40_80K

    # Spectral mirror correlation around 22.05 kHz is always reported as a metric.
    fine_bins = max(3, int(round(250.0 / df)))
    fine_curve = np.convolve(spectrum,
                             np.ones(fine_bins, dtype=np.float32) / fine_bins,
                             mode="same")
    deltas = np.linspace(1000.0, 7000.0, 48)
    band_lower = np.interp(22050.0 - deltas, freqs, fine_curve)
    band_upper = np.interp(22050.0 + deltas, freqs, fine_curve)
    if (np.std(band_lower) > 0.5 and np.std(band_upper) > 0.5
            and np.mean(band_upper) > -115.0):
        def _detrended(values):
            t = np.arange(values.size, dtype=np.float64)
            return values - np.polyval(np.polyfit(t, values, 1), t)
        mirror_correlation = float(np.corrcoef(_detrended(band_lower),
                                        _detrended(band_upper))[0, 1])
    else:
        mirror_correlation = 0.0

    if dsd_profile is not None:
        return HiresAssessment(
            morphology=HiresMorphology.DSD_TRANSFER,
            nyquist_gradient_db_khz=worse_slope,
            ultrasonic_jump_db=jump_db,
            mirror_correlation_22k=mirror_correlation,
            dsd_profile=dsd_profile,
        )

    # Stage 2: CD/DAT Nyquist brickwall & polyphase transition-skirt detectors.
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

    mask_skirt_44k = (freqs >= 19800.0) & (freqs <= 26200.0)
    mask_skirt_48k = (freqs >= 22500.0) & (freqs <= 28500.0)
    mask_mid_44k = (freqs >= 23800.0) & (freqs <= 25800.0)
    mask_mid_48k = (freqs >= 26200.0) & (freqs <= 28200.0)
    mask_stopband = ((freqs >= 28500.0)
                     & (freqs <= min(38500.0, sample_rate * 0.44)))
    slope_skirt_44k = (float(np.min(grad_db_khz[mask_skirt_44k]))
                       if np.any(mask_skirt_44k) else 0.0)
    slope_skirt_48k = (float(np.min(grad_db_khz[mask_skirt_48k]))
                       if np.any(mask_skirt_48k) else 0.0)
    level_stopband_db = (float(np.median(curva[mask_stopband]))
                         if np.any(mask_stopband) else level_after_db)
    level_mid_44k_db = (float(np.median(curva[mask_mid_44k]))
                        if np.any(mask_mid_44k) else level_stopband_db)
    level_mid_48k_db = (float(np.median(curva[mask_mid_48k]))
                        if np.any(mask_mid_48k) else level_stopband_db)
    drop_mid_44k_db = level_before_db - level_mid_44k_db
    drop_mid_48k_db = level_pre24_db - level_mid_48k_db
    jump_stop_44k_db = level_before_db - level_stopband_db
    jump_stop_48k_db = level_pre24_db - level_stopband_db

    mask_ultra_hi_lo = (freqs >= 24500.0) & (freqs <= 28000.0)
    mask_ultra_hi_top = ((freqs >= 34000.0)
                         & (freqs <= min(42000.0, sample_rate * 0.44)))
    if np.any(mask_ultra_hi_lo) and np.any(mask_ultra_hi_top):
        level_ultra_lo_db = float(np.median(curva[mask_ultra_hi_lo]))
        level_ultra_top_db = float(np.median(curva[mask_ultra_hi_top]))
        decay_ultra_24_40k_db = level_ultra_lo_db - level_ultra_top_db
    else:
        level_ultra_top_db = -140.0
        decay_ultra_24_40k_db = 0.0

    clipped_skirt_44k = (
        (
            ceiling_hz is not None
            and 21200.0 <= float(ceiling_hz) < 24350.0
            and slope_skirt_44k < -10.0
            and slope_skirt_44k < slope_base * 2.2
            and drop_mid_44k_db > 25.0
            and jump_stop_44k_db > 29.5
            and drop_mid_44k_db >= jump_stop_44k_db * 0.68
            and decay_ultra_24_40k_db < 4.5
            and level_stopband_db < -60.0
        )
        or (
            ceiling_hz is not None
            and 21200.0 <= float(ceiling_hz) < 25000.0
            and jump_db > 42.0
            and slope_skirt_44k < -20.0
            and decay_ultra_24_40k_db < 2.5
            and level_stopband_db < -60.0
        )
    )
    skirt_44k_brickwall = (
        (
            slope_skirt_44k < -21.5
            and slope_skirt_44k < slope_base * 2.5
            and drop_mid_44k_db > 32.0
            and drop_mid_44k_db > jump_stop_44k_db * 0.38
            and jump_stop_44k_db > 40.0
            and level_stopband_db < -90.0
        )
        or clipped_skirt_44k
    )
    skirt_48k_brickwall = (
        slope_skirt_48k < -21.5
        and slope_skirt_48k < slope_base * 2.5
        and drop_mid_48k_db > 32.0
        and drop_mid_48k_db > jump_stop_48k_db * 0.38
        and jump_stop_48k_db > 40.0
        and level_stopband_db < -90.0
    )

    muro_brickwall = (
        (worse_slope < -28.0 and worse_slope < slope_base * 2.1
         and jump_db > 15.0)
        or (
            jump_db > 21.0
            and level_after_db < -125.0
            and (
                level_mid_44k_db < -120.0
                or (drop_mid_44k_db > 21.0
                    and min(worse_slope, slope_skirt_44k) < -18.5)
            )
        )
        or muro_24k
        or skirt_44k_brickwall
        or skirt_48k_brickwall
    )

    if muro_brickwall:
        reported_slope = worse_slope
        reported_jump = jump_db
        if muro_24k:
            reported_jump = max(reported_jump, jump_24k_db)
        if skirt_44k_brickwall:
            reported_slope = min(reported_slope, slope_skirt_44k)
            reported_jump = max(reported_jump, jump_stop_44k_db)
        if skirt_48k_brickwall:
            reported_slope = min(reported_slope, slope_skirt_48k)
            reported_jump = max(reported_jump, jump_stop_48k_db)
        return HiresAssessment(
            morphology=HiresMorphology.NYQUIST_BRICKWALL,
            nyquist_gradient_db_khz=reported_slope,
            ultrasonic_jump_db=reported_jump,
            mirror_correlation_22k=mirror_correlation,
        )

    # Stage 3: Spectral aliasing mirror around 22.05 kHz.
    if mirror_correlation > 0.82 and jump_db < 18.0:
        return HiresAssessment(
            morphology=HiresMorphology.ALIASING_MIRROR,
            nyquist_gradient_db_khz=worse_slope,
            ultrasonic_jump_db=jump_db,
            mirror_correlation_22k=mirror_correlation,
        )

    # Stage 4: Genuine analogue/acoustic roll-off (only if no prior cut-off).
    mask_14_17k = (freqs >= 14000.0) & (freqs <= 17000.0)
    level_14_17k = (float(np.median(curva[mask_14_17k]))
                    if np.any(mask_14_17k) else -120.0)
    fall_14_17k_db = level_14_17k - level_after_db
    cutoff_previous = cutoff_hz is not None and cutoff_hz < 22050.0

    gentle_rolloff = (
        worse_slope > -17.5
        and jump_db < 14.5
        and level_after_db > -124.0
        and fall_14_17k_db > 8.0
    )
    multipole_analog_rolloff = (
        worse_slope > -12.0
        and jump_db < 23.2
        and level_after_db > -122.0
        and fall_14_17k_db > 10.0
        and (level_14_17k - level_before_db) >= 1.5
    )
    mask_ultra_pass = (freqs >= 25500.0) & (freqs <= 27500.0)
    mask_ultra_stop = ((freqs >= 31000.0)
                       & (freqs <= min(42000.0, sample_rate * 0.44)))
    if np.any(mask_ultra_pass) and np.any(mask_ultra_stop):
        snr_ultra_pass_db = (float(np.median(curva[mask_ultra_pass]))
                             - float(np.median(curva[mask_ultra_stop])))
    else:
        snr_ultra_pass_db = 0.0
    ultrasonic_studio_rolloff = (
        worse_slope > -12.0
        and slope_skirt_44k > -18.0
        and 2.0 <= drop_mid_44k_db < 21.0
        and snr_ultra_pass_db >= 20.0
        and (level_14_17k - level_before_db) >= 1.5
    )
    continuous_steep_rolloff = (
        worse_slope > -15.0
        and slope_skirt_44k > -18.0
        and slope_skirt_48k > -18.0
        and jump_db < 40.0
        and level_after_db > -95.0
        and level_ultra_top_db > -105.0
        and decay_ultra_24_40k_db >= 7.5
        and (level_14_17k - level_before_db) >= 4.0
    )
    rolloff_analog = bool(
        not cutoff_previous
        and (
            gentle_rolloff
            or multipole_analog_rolloff
            or ultrasonic_studio_rolloff
            or continuous_steep_rolloff
        )
    )
    morphology = (
        HiresMorphology.GENUINE_ANALOG_ROLLOFF
        if rolloff_analog
        else HiresMorphology.FULLBAND_NORMAL
    )
    return HiresAssessment(
        morphology=morphology,
        nyquist_gradient_db_khz=worse_slope,
        ultrasonic_jump_db=jump_db,
        mirror_correlation_22k=mirror_correlation,
    )


def _diagnose_hires_authenticity(freqs_hz, spectrum_mid_db, sample_rate,
                                     cutoff_hz=None, ceiling_hz=None):
    """Vector 3: morphology of a Hi-Res spectrum (>= 88.2 kHz).

    `spectrum_mid_db` is calibrated to dBFS per bin (see _spectrum_dbfs)."""
    return _assess_hires_authenticity(
        freqs_hz, spectrum_mid_db, sample_rate,
        cutoff_hz=cutoff_hz, ceiling_hz=ceiling_hz,
    ).to_protocol_fields()


def _has_centered_upper_mid_vocal(freqs, diff_db):
    """True when Mid already leads Side strongly in 9-13 kHz without a
    further steep collapse into 15.5-19.8 kHz (typical of a dry wide-panned
    instrument bed beneath a centered mono vocal)."""
    mask_upper_mid = (freqs >= 9000.0) & (freqs <= 13000.0)
    mask_high = (freqs >= 15500.0) & (freqs <= 19800.0)
    if not np.any(mask_upper_mid) or not np.any(mask_high):
        return False
    diff_um = float(np.median(diff_db[mask_upper_mid]))
    diff_hi = float(np.median(diff_db[mask_high]))
    return bool(
        math.isfinite(diff_um)
        and math.isfinite(diff_hi)
        and diff_um >= 22.0
        and (diff_hi - diff_um) < 4.0
    )


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
    centered_upper_mid = _has_centered_upper_mid_vocal(freqs, diferencia)

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
        and (
            (collapse > 11.5 and delta_cutoff > 1100.0)
            or (collapse > 16.5
                and (delta_cutoff >= 900.0
                     or (side_ceiling_hz < 17500.0 and not centered_upper_mid)))
        )
    )

    output["caida_side_agudos_db"] = round(collapse, 2) if math.isfinite(collapse) else 0.0
    output["delta_corte_ms_hz"] = round(delta_cutoff, 1) if math.isfinite(delta_cutoff) else 0.0
    output["sospecha_joint_stereo"] = bool(suspicion)
    return output

def _max_smoothed_descent_db_khz(freqs_hz, power_db, f_min, f_max):
    """Steepest 500 Hz moving-average descent (dB/kHz) inside [f_min, f_max]."""
    freqs = np.asarray(freqs_hz, dtype=np.float64)
    power_db = np.asarray(power_db, dtype=np.float64)
    if freqs.size < 8 or power_db.shape != freqs.shape or f_max <= f_min:
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


def _coarse_envelope_descent_db_khz(
    freqs_hz, power_db, f_min, f_max, band_hz=1000.0, step_hz=250.0, percentile=75.0
):
    """Steepest adjacent-band envelope descent (dB/kHz) inside [f_min, f_max].

    Compares consecutive non-overlapping `band_hz` bands (`[f, f+B]` vs
    `[f+B, f+2B]`) stepped every `step_hz`. This suppresses narrow inter-harmonic
    valleys of discrete-overtone solo instruments (e.g. solo piano) while
    preserving the full step height of brickwall/resampler drops (>= 20-45 dB/kHz).
    Upward noise-shaped dither rises produce negative slopes and clamp to 0.0.
    """
    freqs = np.asarray(freqs_hz, dtype=np.float64)
    pdb = np.asarray(power_db, dtype=np.float64)
    b_hz = float(band_hz)
    s_hz = float(step_hz)
    if (freqs.size < 8 or pdb.shape != freqs.shape
            or b_hz <= 0.0 or s_hz <= 0.0
            or (float(f_max) - float(f_min)) < (2.0 * b_hz)):
        return 0.0
    max_slope = 0.0
    lo = float(f_min)
    hi_lim = float(f_max) + 1e-6
    inv_b_khz = 1000.0 / b_hz
    while lo + 2.0 * b_hz <= hi_lim:
        sl_a, ok_a = _freq_slice(freqs, lo, lo + b_hz, True)
        sl_b, ok_b = _freq_slice(freqs, lo + b_hz, lo + 2.0 * b_hz, True)
        if ok_a and ok_b:
            seg_a = pdb[sl_a]
            seg_b = pdb[sl_b]
            if np.all(np.isfinite(seg_a)) and np.all(np.isfinite(seg_b)):
                va = float(np.percentile(seg_a, percentile))
                vb = float(np.percentile(seg_b, percentile))
                slope = (va - vb) * inv_b_khz
                if math.isfinite(slope) and slope > max_slope:
                    max_slope = slope
        lo += s_hz
    return round(max(0.0, max_slope), 2)


def _gradient_edge_db_khz(freqs_hz, power_db, ceiling_hz, nyquist):
    """Steepest smoothed descent (dB/kHz) in the transition band around the ceiling.

    The band covers 1.5 kHz below the ceiling and 0.5 kHz above it, clamped to
    5.5 kHz and Nyquist. A 500 Hz moving average removes the bin-to-bin variance
    of real noise, which otherwise looks like a cliff on its own. A studio
    anti-alias filter leaves a gentle slope (well under 35 dB/kHz), while a
    brickwall or a resampler exceeds it.

    Returns a positive number for a descent, 0.0 when the band cannot be
    measured."""
    if not ceiling_hz:
        return 0.0
    f_min = max(5500.0, float(ceiling_hz) - 1500.0)
    f_max = min(float(nyquist), float(ceiling_hz) + 500.0)
    return _max_smoothed_descent_db_khz(freqs_hz, power_db, f_min, f_max)

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
    floor_db = 10.0 * math.log10(max(float(_FLOOR_F32), 1e-28))
    dynamic_span = max_global - float(np.min(power_db))
    if (not math.isfinite(max_global)
            or max_global <= floor_db + 10.0
            or (max_global < -200.0 and dynamic_span <= 1e-3)):
        return {"error": "insufficient signal energy for spectral analysis"}
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

    # Dedicated low-band brickwall pass (5.5-12.5 kHz): catches 12/16/22.05 -> 44.1 kHz
    # resamples and low-bitrate codec brickwalls without changing the >= 12 kHz
    # window step_size. Uses a fixed ~500 Hz physical window and requires a steep
    # transition slope (> 28 dB/kHz), coarse band-envelope confirmation (>= 16 dB/kHz),
    # and sustained post-cutoff suppression (>= 25 dB below `before` across the next
    # 1-4 kHz) so tonal inter-harmonic valleys never trigger a false cut-off.
    if cutoff_hz is None and samplerate <= 48000 and 5500.0 <= ceiling_hz < 12500.0:
        mask_lo = (freqs >= 5000.0) & (freqs <= 13500.0)
        if np.count_nonzero(mask_lo) > 10:
            pr_lo = power_db_n[mask_lo]
            fr_lo = freqs[mask_lo]
            n_lo = len(pr_lo)
            df_lo = float(fr_lo[1] - fr_lo[0]) if n_lo > 1 else 0.0
            step_lo = max(1, int(round(500.0 / df_lo))) if df_lo > 0.0 else 0
            if step_lo > 0 and n_lo > 2 * step_lo:
                cs_lo = np.empty(n_lo + 1, dtype=np.float64)
                cs_lo[0] = 0.0
                np.cumsum(pr_lo, dtype=np.float64, out=cs_lo[1:])
                inv_lo = 1.0 / step_lo
                bef_lo = (cs_lo[step_lo : n_lo - step_lo] - cs_lo[0 : n_lo - 2 * step_lo]) * inv_lo
                aft_lo = (cs_lo[2 * step_lo : n_lo] - cs_lo[step_lo : n_lo - step_lo]) * inv_lo
                cond_primary = ((bef_lo - aft_lo) > 25.0) & (aft_lo < -45.0)
                cond_medium = ((bef_lo - aft_lo) > 18.5) & (aft_lo < -42.0) & (bef_lo > -52.0)
                cond_steep_sub = ((bef_lo - aft_lo) > 16.5) & (aft_lo < -55.0)
                cond_lo = cond_primary | cond_medium | cond_steep_sub
                for idx_c in np.flatnonzero(cond_lo):
                    i_c = int(idx_c)
                    cand_hz = float(fr_lo[i_c + step_lo])
                    grad_c = _gradient_edge_db_khz(freqs, power_db_n, cand_hz, nyquist)
                    min_grad = 28.0 if (cond_primary[i_c] or cond_medium[i_c]) else 36.0
                    if grad_c <= min_grad:
                        continue
                    coarse_c = _coarse_envelope_descent_db_khz(
                        freqs,
                        power_db_n,
                        max(3500.0, cand_hz - 1800.0),
                        min(nyquist - 200.0, cand_hz + 1800.0),
                    )
                    if coarse_c < 16.0:
                        continue
                    post_lo = cand_hz + 1000.0
                    post_hi = min(nyquist - 500.0, cand_hz + 4000.0)
                    if post_hi <= post_lo:
                        continue
                    mask_post = (freqs >= post_lo) & (freqs <= post_hi)
                    if not np.any(mask_post):
                        continue
                    mask_pre = (freqs >= max(5000.0, cand_hz - 1000.0)) & (freqs <= cand_hz)
                    pre_ref = max(
                        float(bef_lo[i_c]),
                        float(np.median(power_db_n[mask_pre])) if np.any(mask_pre) else -300.0,
                    )
                    if (pre_ref - float(np.median(power_db_n[mask_post]))) >= 25.0:
                        cutoff_hz = cand_hz
                        break

    cutoff = CutoffAssessment.from_cutoff(cutoff_hz, nyquist)

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
        dh = _mean_safe(np.abs(power_l[mask_a] - power_r[mask_a]))
        dm = _mean_safe(np.abs(power_l[mask_m] - power_r[mask_m]))
        sep = float(dh / (dm + 1e-30))

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
    # For strictly out-of-phase signals (L = -R) Mid cancels to the floor, so
    # fall back to total power when Mid holds no signal above the floor.
    if power_mid is None or power_side is None:
        spectrum_mid_db = _spectrum_dbfs(power_mid if power_mid is not None else power_l,
                                         gain_noise)
    elif float(np.max(power_mid)) <= float(_FLOOR_F32) * 2.0:
        spectrum_mid_db = _spectrum_dbfs(power, gain_noise)
    else:
        spectrum_mid_db = mid_db
    hires = _diagnose_hires_authenticity(freqs, spectrum_mid_db, samplerate,
                                         cutoff_hz=cutoff_hz,
                                         ceiling_hz=ceiling_hz)

    # A DSD transfer closes its musical band at the SACD passband (20 kHz) while
    # the noise-shaping ramp continues above it. That edge is the passband of the
    # format, not a codec band limit and not a Nyquist-relative step, so it is not
    # reported as a cut-off at all: the ramp itself rules the truncation out.
    if hires["es_transferencia_dsd"] and cutoff.detected:
        cutoff = CutoffAssessment.none()

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
    near_nyq_high = (
        not cutoff.detected
        or (cutoff.cutoff_class is CutoffClass.NEAR_NYQUIST
            and cutoff.distance_nyquist is not None
            and cutoff.distance_nyquist >= 0.94)
    )
    gentle_post_ceiling = False
    if (samplerate <= 48000 and near_nyq_high
            and 13500.0 <= ceiling_hz < 0.80 * nyquist
            and gradient_edge < 33.0):
        m_c_pre = (freqs >= max(4000.0, ceiling_hz - 800.0)) & (freqs <= ceiling_hz)
        m_c_p1 = ((freqs >= ceiling_hz + 300.0)
                  & (freqs <= min(nyquist - 200.0, ceiling_hz + 1300.0)))
        m_c_p3 = ((freqs >= min(nyquist - 600.0, ceiling_hz + 2000.0))
                  & (freqs <= min(nyquist - 100.0, ceiling_hz + 4000.0)))
        if np.any(m_c_pre) and np.any(m_c_p1) and np.any(m_c_p3):
            step_p1 = float(np.median(power_db_n[m_c_pre]) - np.median(power_db_n[m_c_p1]))
            step_p3 = float(np.median(power_db_n[m_c_p1]) - np.median(power_db_n[m_c_p3]))
            gentle_post_ceiling = (-3.0 <= step_p1 < 12.0) and (-3.0 <= step_p3 < 18.0)

    subceiling_analog_floor = False
    if (samplerate <= 48000 and near_nyq_high
            and 7500.0 <= ceiling_hz < 10500.0
            and 0.0 < centroid_hz < 1200.0
            and gradient_edge < 18.0):
        s_m1, ok_m1 = _freq_slice(freqs, 0.55 * nyquist, 0.70 * nyquist)
        s_m2, ok_m2 = _freq_slice(freqs, 0.70 * nyquist, 0.84 * nyquist)
        s_m3, ok_m3 = _freq_slice(freqs, 0.84 * nyquist, 0.93 * nyquist)
        s_m4, ok_m4 = _freq_slice(freqs, 0.93 * nyquist, 0.985 * nyquist)
        if ok_m1 and ok_m2 and ok_m3 and ok_m4:
            e_m1 = float(np.median(spectrum_mid_db[s_m1]))
            e_m2 = float(np.median(spectrum_mid_db[s_m2]))
            e_m3 = float(np.median(spectrum_mid_db[s_m3]))
            e_m4 = float(np.median(spectrum_mid_db[s_m4]))
            subceiling_analog_floor = bool(
                -82.0 < e_m4 <= -45.0
                and e_m1 >= e_m2 - 1.5
                and e_m2 >= e_m3 - 1.5
                and e_m3 >= e_m4 - 1.5
                and 1.5 <= (e_m1 - e_m4) <= 18.0
                and (e_m3 - e_m4) <= 8.0
            )

    # Clean digital solo-instrument decay (e.g. modern ECM / concert solo piano
    # in 16/44.1 without analog tape hiss): -60 dB contour crosses in 5.8-13.5 kHz
    # with a low centroid (< 950 Hz below 7.5 kHz, < 1100 Hz above), progressive
    # pre-ceiling decay, smooth coarse band-envelope continuation (tracking
    # discrete piano harmonics via 1.2 kHz P75 windows while tolerating >17.5 kHz
    # noise-shaped dither), and no hidden resampler/codec brickwall cliff below
    # 17.5 kHz.
    subceiling_clean_acoustic_decay = False
    if (samplerate <= 48000 and near_nyq_high
            and 5800.0 <= ceiling_hz < 13500.0
            and gradient_edge >= 0.5):
        max_cent_sub = 950.0 if ceiling_hz < 7500.0 else 1100.0
        if 0.0 < centroid_hz < max_cent_sub:
            hi_post_cap = min(17500.0, nyquist - 500.0)
            s_pre_lo, ok_pre_lo = _freq_slice(
                freqs, max(1800.0, ceiling_hz - 4000.0), max(3200.0, ceiling_hz - 1800.0)
            )
            s_pre_hi, ok_pre_hi = _freq_slice(
                freqs, max(4000.0, ceiling_hz - 1200.0), ceiling_hz
            )
            s_p1, ok_p1 = _freq_slice(
                freqs, ceiling_hz + 250.0, min(hi_post_cap, ceiling_hz + 1500.0)
            )
            s_p2, ok_p2 = _freq_slice(
                freqs, ceiling_hz + 1500.0, min(hi_post_cap, ceiling_hz + 3500.0)
            )
            if ok_pre_lo and ok_pre_hi and ok_p1 and ok_p2:
                e_pre_lo = float(np.percentile(power_db_n[s_pre_lo], 75))
                e_pre_hi = float(np.percentile(power_db_n[s_pre_hi], 75))
                e_p1 = float(np.percentile(power_db_n[s_p1], 75))
                e_p2 = float(np.percentile(power_db_n[s_p2], 75))
                pre_drop = e_pre_lo - e_pre_hi
                step_p1 = e_pre_hi - e_p1
                step_p2 = e_p1 - e_p2
                coarse_edge_grad = _coarse_envelope_descent_db_khz(
                    freqs,
                    power_db_n,
                    max(3500.0, ceiling_hz - 2200.0),
                    min(hi_post_cap, ceiling_hz + 2200.0),
                    band_hz=1000.0,
                    step_hz=250.0,
                    percentile=75.0,
                )
                coarse_post_grad = _coarse_envelope_descent_db_khz(
                    freqs,
                    power_db_n,
                    ceiling_hz,
                    hi_post_cap,
                    band_hz=750.0,
                    step_hz=200.0,
                    percentile=50.0,
                )
                subceiling_clean_acoustic_decay = bool(
                    6.0 <= pre_drop <= 45.0
                    and step_p1 < 16.0
                    and step_p2 < 24.0
                    and coarse_edge_grad < 18.0
                    and coarse_post_grad < 11.0
                )

    smooth_acoustic_decay = bool(
        (
            0.0 < centroid_hz < 1800.0
            and near_nyq_high
            and ceiling_hz >= 10500.0
            and (gradient_edge < 20.0 or gentle_post_ceiling)
        )
        or subceiling_analog_floor
        or subceiling_clean_acoustic_decay
    )
    smooth_band_limited_rolloff = False
    if (samplerate <= 48000 and near_nyq_high
            and 10000.0 <= ceiling_hz < 0.72 * nyquist
            and (0.5 <= gradient_edge < 18.0 or gentle_post_ceiling)):
        mask_pre_lo = ((freqs >= max(5000.0, ceiling_hz - 4500.0))
                       & (freqs <= max(6500.0, ceiling_hz - 2200.0)))
        mask_pre_hi = ((freqs >= max(7500.0, ceiling_hz - 1400.0))
                       & (freqs <= ceiling_hz))
        mask_post_fl = (freqs >= ceiling_hz + 1500.0) & (freqs <= nyquist * 0.96)
        if (np.any(mask_pre_lo) and np.any(mask_pre_hi)
                and np.any(mask_post_fl)):
            drop_pre = float(np.mean(power_db_n[mask_pre_lo])
                             - np.mean(power_db_n[mask_pre_hi]))
            floor_post = float(np.mean(power_db_n[mask_post_fl]))
            smooth_band_limited_rolloff = bool(
                3.5 <= drop_pre <= 42.0 and floor_post > -125.0
            )

    # Sub-ceiling 24-bit converter roll-off: on high-DR (DR 13-16+) or warm
    # acoustic/indie 24-bit studio masters at 44.1-48 kHz, the -60 dB activity
    # contour (`ceiling_hz`) can cross early around 13-19 kHz while genuine
    # musical overtones and studio room/tape air continue below -60 dB right up
    # to the ADC anti-aliasing filter near Nyquist. At 44.1 kHz, the stopband
    # floor must sit strictly below the -96 dBFS 16-bit TPDF ceiling (<= -105 dBFS)
    # so 16/44.1 CDs re-dithered to 24-bit can never exploit this exemption.
    native_24bit_hf_tail = False
    if 44100 <= samplerate <= 48000 and near_nyq_high and ceiling_hz >= 10500.0:
        s_hi, ok_hi = _freq_slice(freqs, 0.84 * nyquist, 0.90 * nyquist)
        s_vhi, ok_vhi = _freq_slice(freqs, 0.90 * nyquist, 0.94 * nyquist)
        s_acd, ok_acd = _freq_slice(freqs, 0.925 * nyquist, 0.958 * nyquist)
        s_stop, ok_stop = _freq_slice(freqs, 0.975 * nyquist, 0.995 * nyquist)
        if ok_hi and ok_vhi and ok_acd and ok_stop:
            e_hi = float(np.median(spectrum_mid_db[s_hi]))
            e_vhi = float(np.median(spectrum_mid_db[s_vhi]))
            e_acd = float(np.median(spectrum_mid_db[s_acd]))
            e_stop = float(np.median(spectrum_mid_db[s_stop]))
            stop_lo = -130.0 if samplerate == 44100 else -125.0
            stop_hi = -105.0 if samplerate == 44100 else -90.0
            native_24bit_hf_tail = bool(
                stop_lo <= e_stop <= stop_hi
                and (e_hi - e_stop) >= 12.0
                and (e_vhi - e_stop) >= 6.0
                and (e_acd - e_stop) >= 2.8
                and e_hi > e_vhi > (e_stop + 5.0)
            )

    return {
        "error":              None,
        "techo_hz":           round(ceiling_hz),
        "techo_rel_nyquist":  round(ceiling_hz / nyquist, 4) if nyquist else 0.0,
        # Compatibility: "corte_artificial" now means "confirmed accusation"
        # (a cut-off clearly below the Nyquist). It used to be enabled by any
        # step, including the filter of a master.
        **cutoff.to_protocol_fields(),
        "ratio":              ratio,
        "ratio_aplica":       ratio_applies,
        "var_alta_db":        var_high,
        "separacion_stereo":  sep,
        # Vector 1 (additive keys: old consumers ignore them).
        "caida_side_agudos_db": asymmetry["caida_side_agudos_db"],
        "delta_corte_ms_hz":    asymmetry["delta_corte_ms_hz"],
        "sospecha_joint_stereo": asymmetry["sospecha_joint_stereo"],
        "es_dual_mono":         asymmetry["es_dual_mono"],
        "msc_lo":               0.0,
        "msc_hi":               0.0,
        "msc_step_hf":          0.0,
        "ipd_phase_lock_hf":    0.0,
        "stereo_phase_coupling": False,
        # Vector 3 (additive keys: old consumers ignore them).
        "muro_brickwall_nyquist":   hires["muro_brickwall_nyquist"],
        "espejo_aliasing_detectado": hires["espejo_aliasing_detectado"],
        "rolloff_analogico_genuino": hires["rolloff_analogico_genuino"],
        "gradiente_nyquist_db_khz": hires["gradiente_nyquist_db_khz"],
        "salto_ultrasonico_db":     hires["salto_ultrasonico_db"],
        "correlacion_espejo_22k":   round(hires["correlacion_espejo_22k"], 3),
        "es_transferencia_dsd":     hires["es_transferencia_dsd"],
        "perfil_dsd":               hires["perfil_dsd"],
        # Transition band around the ceiling (dB/kHz of steepest descent).
        "gradiente_borde_db_khz":   gradient_edge,
        "centroide_hz":             (round(centroid_hz, 1)
                                     if math.isfinite(centroid_hz) else 0.0),
        "decaimiento_acustico_suave": smooth_acoustic_decay,
        "rolloff_banda_limitada_genuina": smooth_band_limited_rolloff,
        "native_24bit_hf_tail":     native_24bit_hf_tail,
        "noise_shaped_16_in_24":    False,
        "quiet_hf_slope_db":        0.0,
        "quiet_hf_flatness":        0.0,
        "quiet_hf_floor_dbfs":      None,
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
        "sbr_qmf_detectado":        False,
        "sbr_qmf_correlacion":      0.0,
        "sbr_qmf_z":                0.0,
        "caida_modulacion_20k_db":  0.0,
        "brecha_modulacion_20k_db": 0.0,
        "modulacion_psicoacustica_20k": False,
        "es_estacionario":          False,
        "mdct_lattice_detected":    False,
        "mdct_lattice_snr":         0.0,
        "mdct_lattice_family":      None,
        "familias_evidencia_lossy": [],
        "finding_codes":            [],
        "corroboracion_multifamilia": False,
        "nyquist":            samplerate // 2,
        "espectrograma":      None,
    }

def analyze_spectrum(data, samplerate, mode, seconds=None, save_png=False, path=None,
                     bits_per_sample=None):
    """Spectral analysis over an already loaded array (direct API and oracle of
    the tests). The production path uses _BlockAnalysis, which does not load
    the track. Both paths share the selector and the float32 STFT, so they must
    give exactly the same metrics."""
    try:
        is_stereo = data.shape[1] >= 2
        accumulator = _SpectralAccumulator(
            samplerate, is_stereo,
            save_png=bool(save_png) and PIL_OK and bool(path),
            path=path, mode=mode, seconds=seconds,
            bits_per_sample=bits_per_sample)
        for start in range(0, len(data), FRAMES_BY_BLOCK):
            accumulator.add(data[start : start + FRAMES_BY_BLOCK], start)
        return accumulator.result()
    except Exception as e:
        return {"error": str(e)}

ACTIVE_RMS_THRESHOLD_DB = -65.0      # below this a block is silence for the selector

def _needed_active_blocks(samplerate, mode="center", seconds=None):
    """K blocks, chosen so that the selected audio covers the requested window.
    With FRAMES_BY_BLOCK fixed, K grows with the sample rate."""
    mode_enum = SpectralMode.coerce(mode, default=SpectralMode.CENTER)
    if mode_enum is SpectralMode.FULL:
        target_sec = float(SPECTRO_SECONDS + 15)
    elif mode_enum is SpectralMode.SECONDS and seconds is not None:
        target_sec = max(1.0, float(seconds))
    else:
        target_sec = float(SPECTRO_SECONDS)
    return max(1, int(math.ceil(target_sec * float(samplerate) / FRAMES_BY_BLOCK)))

def _mono_diff_mean_sq(mono, last_sample=None, diff_buf=None):
    """Mean squared adjacent-sample difference on a 1-D sequence."""
    n_diffs = mono.size - 1
    if n_diffs <= 0:
        return 0.0
    chunk = 16384
    if diff_buf is None or diff_buf.dtype != mono.dtype or diff_buf.size < min(chunk, n_diffs):
        diff_buf = np.empty(min(chunk, n_diffs), dtype=mono.dtype)
    total_sq = 0.0
    if last_sample is not None:
        first_diff = float(mono[0]) - float(last_sample)
        total_sq = first_diff * first_diff
    for idx in range(0, n_diffs, chunk):
        end = min(idx + chunk, n_diffs)
        cur = diff_buf[: end - idx]
        np.subtract(mono[idx + 1 : end + 1], mono[idx:end], out=cur)
        total_sq += float(np.dot(cur, cur))
    denom = float(mono.size if last_sample is not None else n_diffs)
    return float(total_sq / denom)


def _activity_score(block, last_sample=None, mono=None, diff_buf=None, int_scale=None):
    """(RMS in dBFS, E_diff) of a block. E_diff = mean((x[t]-x[t-1])^2) is a
    cheap high-frequency/transient indicator: it is large on cymbals and attacks
    and almost zero on a sustained bass line. The last sample of the previous
    block is carried over so the difference is not lost at the boundary."""
    scale_f = float(int_scale) if int_scale is not None else 1.0
    if mono is None:
        if block.ndim > 1:
            n_ch = block.shape[1]
            if n_ch == 1:
                mono = (block[:, 0] if int_scale is None
                        else block[:, 0].astype(np.float64) * scale_f)
            elif n_ch == 2:
                if int_scale is None:
                    mono = (block[:, 0] + block[:, 1]) * 0.5
                else:
                    mono = (block[:, 0].astype(np.float64)
                            + block[:, 1].astype(np.float64)) * (0.5 * scale_f)
            else:
                mono = (block.mean(axis=1) if int_scale is None
                        else block.mean(axis=1, dtype=np.float64) * scale_f)
        else:
            mono = block if int_scale is None else block.astype(np.float64) * scale_f
    if mono.size == 0:
        return -math.inf, 0.0

    sumsq = float(np.dot(mono, mono))
    if sumsq > 0.0:
        rms_db = 10.0 * math.log10(sumsq / mono.size)
    else:
        rms_db = -math.inf
    e_diff = _mono_diff_mean_sq(mono, last_sample=last_sample, diff_buf=diff_buf)

    if block.ndim > 1 and block.shape[1] > 2:
        # Multichannel blocks may hold active Centre/Surround while Front L/R
        # are silent or mutually out of phase; evaluate peak active channel.
        fblock = np.asarray(block, dtype=np.float64)
        sumsq_ch = np.einsum("ij,ij->j", fblock, fblock)
        best_ch = int(np.argmax(sumsq_ch))
        best_sumsq = float(sumsq_ch[best_ch]) * (scale_f * scale_f)
        if best_sumsq > 0.0:
            ch_rms = 10.0 * math.log10(best_sumsq / fblock.shape[0])
            best_sig = (fblock[:, best_ch] * scale_f if int_scale is not None
                        else fblock[:, best_ch])
            ch_diff = _mono_diff_mean_sq(best_sig, last_sample=None, diff_buf=None)
            if ch_rms > rms_db:
                rms_db = ch_rms
            if ch_diff > e_diff:
                e_diff = ch_diff
        return rms_db, e_diff

    if (block.ndim > 1 and block.shape[1] == 2
            and (rms_db <= ACTIVE_RMS_THRESHOLD_DB or e_diff <= 1e-12)):
        # Out-of-phase L = -R cancels in (L + R) * 0.5; rescue via Side only when
        # Mono fails the activity gate (zero extra cost on normal stereo audio).
        if int_scale is not None:
            side = (
                block[:, 0].astype(np.float64) - block[:, 1].astype(np.float64)
            ) * (0.5 * scale_f)
        else:
            side = (block[:, 0] - block[:, 1]) * 0.5
        side_sumsq = float(np.dot(side, side))
        if side_sumsq > sumsq and side_sumsq > 0.0:
            side_rms_db = 10.0 * math.log10(side_sumsq / side.size)
            side_e_diff = _mono_diff_mean_sq(side, last_sample=None, diff_buf=None)
            if side_rms_db > rms_db:
                rms_db = side_rms_db
            if side_e_diff > e_diff:
                e_diff = side_e_diff

    return rms_db, e_diff

def _stft_complex(channel, nperseg, hop, window, scale, frame_buf=None):
    """float32 STFT with sliding_window_view: no copies beyond the windowed
    frames, and the result stays in single precision (half the RAM of float64)."""
    x = np.ascontiguousarray(channel, dtype=np.float32)
    frames = np.lib.stride_tricks.sliding_window_view(x, nperseg)[::hop]
    effective_window = window * np.float32(scale) if scale is not None else window
    if frame_buf is not None and frame_buf.shape == frames.shape:
        np.multiply(frames, effective_window, out=frame_buf)
        return _rfft_f32(frame_buf, axis=1)
    return _rfft_f32(frames * effective_window, axis=1)


def _stft_complex_stereo_pair(ch_l, ch_r, nperseg, hop, scaled_window, stereo_buf=None):
    """Batched float32 STFT of left+right stereo channels in a single rfft call."""
    xl = np.ascontiguousarray(ch_l, dtype=np.float32)
    xr = np.ascontiguousarray(ch_r, dtype=np.float32)
    frames_l = np.lib.stride_tricks.sliding_window_view(xl, nperseg)[::hop]
    frames_r = np.lib.stride_tricks.sliding_window_view(xr, nperseg)[::hop]
    if stereo_buf is None or stereo_buf.shape != (2, frames_l.shape[0], nperseg):
        stereo_buf = np.empty((2, frames_l.shape[0], nperseg), dtype=np.float32)
    np.multiply(frames_l, scaled_window, out=stereo_buf[0])
    np.multiply(frames_r, scaled_window, out=stereo_buf[1])
    z_lr = _rfft_f32(stereo_buf, axis=2)
    return z_lr, stereo_buf

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
        self._env_4_11 = []
        self._ms_p95_db = 0.0
        self._ms_dynamic_collapse = False
        self._slice_key = None
        self._cached_slices = None
        self._state = AccumulatorState.COLLECTING
        self._finalized = False
        self._cached_summary = None

    @property
    def state(self) -> AccumulatorState:
        """Current streaming lifecycle phase."""
        return self._state

    def _get_cached_slices(self, freqs, window, n):
        sum_w = float(np.sum(window, dtype=np.float64))
        key = (int(freqs.size), int(n), sum_w)
        if self._slice_key == key and self._cached_slices is not None:
            return self._cached_slices
        if sum_w <= 0.0:
            return None
        gain = _white_noise_gain(n, window)
        sl_band, has_band = _freq_slice(freqs, 4000.0, 11000.0, True)
        if not has_band or gain <= 0.0:
            return None
        sl_16_195, ok_16_195 = _freq_slice(freqs, 16000.0, 19500.0, True)
        hi_20 = min(21800.0, self.sr * 0.495)
        sl_202_218, ok_202_218 = _freq_slice(freqs, 20200.0, hi_20, True)
        sl_8_14, ok_8_14 = _freq_slice(freqs, 8000.0, 14000.0, True)
        sl_sfb21, ok_sfb21 = _freq_slice(freqs, 16000.0, 20500.0, True)
        sl_ms_lo, ok_ms_lo = _freq_slice(freqs, 2000.0, 8000.0, True)
        sl_ms_hi, ok_ms_hi = _freq_slice(freqs, 13500.0, 16500.0, True)
        sl_presence, ok_presence = _freq_slice(freqs, 10000.0, 14000.0, True)
        sl_hf, ok_hf = _freq_slice(freqs, 11000.0, min(22050.0, self.sr * 0.49), True)
        sb_proj = None
        if self.sr >= 39000:
            sb_slices = []
            for i in range(self.N_SUBBANDS):
                lo = self.START_SUBBAND_HZ + i * self.WIDTH_SUBBAND_HZ
                hi = lo + self.WIDTH_SUBBAND_HZ
                sl_sb, ok_sb = _freq_slice(freqs, lo, hi, False)
                if not ok_sb:
                    sb_slices = None
                    break
                sb_slices.append(sl_sb)
            if sb_slices is not None:
                s0_sb = sb_slices[0].start
                s1_sb = sb_slices[-1].stop
                w_sb = np.zeros((s1_sb - s0_sb, len(sb_slices)), dtype=np.float32)
                for j, sl_sb in enumerate(sb_slices):
                    w_sb[sl_sb.start - s0_sb : sl_sb.stop - s0_sb, j] = (
                        1.0 / float(sl_sb.stop - sl_sb.start)
                    )
                sb_proj = (slice(s0_sb, s1_sb), w_sb)
        cached = (
            gain, sl_band,
            (sl_16_195, sl_202_218) if (ok_16_195 and ok_202_218) else None,
            (sl_8_14, sl_sfb21) if (ok_8_14 and ok_sfb21) else None,
            (sl_ms_lo, sl_ms_hi) if (ok_ms_lo and ok_ms_hi) else None,
            (sl_presence, sl_hf) if (ok_presence and ok_hf) else None,
            freqs[sl_hf] if (ok_presence and ok_hf) else None,
            sb_proj,
        )
        self._slice_key = key
        self._cached_slices = cached
        return cached

    def add(self, power, freqs, window, power_mid=None, power_side=None):
        """Consumes the (frames, bins) power of one selected block."""
        if self._finalized:
            raise RuntimeError("cannot add audio blocks after completion")
        if self._error or power.shape[0] == 0:
            return
        try:
            slices = self._get_cached_slices(freqs, window, window.size)
            if slices is None:
                return
            gain, sl_band, mod_sl, sfb_sl, ms_sl, pres_sl, freqs_hf, sb_proj = slices

            band_mean_f32 = power[:, sl_band].mean(axis=1)
            band = band_mean_f32.astype(np.float64) / gain
            # Upper-band modulation (16.0-19.5 kHz vs 20.2-21.8 kHz) uses a
            # -78 dBFS gate so quieter passages where a full-band codec starves
            # its top scalefactor band are also captured.
            if 41000 <= self.sr <= 48000 and mod_sl is not None:
                mod_mask = band > 10.0 ** (-78.0 / 10.0)
                if np.any(mod_mask):
                    sl_16_195, sl_202_218 = mod_sl
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
            band_act = band_mean_f32 if all_active else band_mean_f32[active_mask]
            self._frames += int(block_power.shape[0])
            self._freqs = freqs
            block_sum = block_power.sum(axis=0, dtype=np.float64)
            self._sum_power = (block_sum if self._sum_power is None
                               else self._sum_power + block_sum)

            # Instantaneous cut-off: last bin above -60 dB relative to the
            # frame's 4-11 kHz mean power. Referencing the frame peak made a
            # strong bass sweep the reference and produced fake cut-off jitter.
            ref_db = 10.0 * np.log10(np.maximum(band_act, _FLOOR_F32))
            ref_db_f64 = ref_db.astype(np.float64)
            self._env_4_11.extend(ref_db_f64.tolist())

            # sfb21 islands and the exciter envelopes. Both are referenced to the
            # 4-11 kHz band of the SAME frame: a 1-8 kHz reference would follow a
            # strong bass line and make a continuous high band look intermittent.
            if self.sr >= 41000 and sfb_sl is not None:
                sl_8_14, sl_sfb21 = sfb_sl
                db_8_14 = 10.0 * np.log10(np.maximum(
                    block_power[:, sl_8_14].mean(axis=1, dtype=np.float64), 1e-28))
                db_16_20 = 10.0 * np.log10(np.maximum(
                    block_power[:, sl_sfb21].mean(axis=1, dtype=np.float64), 1e-28))
                e_sfb21 = db_16_20 - ref_db_f64
                self._e_sfb21.extend(e_sfb21.tolist())
                self._env_8_14.extend(db_8_14.tolist())
                self._env_16_20.extend(db_16_20.tolist())

            # Frame-level Mid/Side stereo collapse in 13.5-16.5 kHz vs 2-8 kHz.
            if power_side is not None and self.sr >= 41000 and ms_sl is not None:
                sl_ms_lo, sl_ms_hi = ms_sl
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
                valid_ms = (m_hi > (ref_db_f64 - 35.0)) & ((m_lo - s_lo) < 20.0)
                if np.any(valid_ms):
                    excess = (m_hi[valid_ms] - s_hi[valid_ms]) - (m_lo[valid_ms] - s_lo[valid_ms])
                    self._ms_excess.extend(excess.tolist())

            # A cut-off is only meaningful when the frame also has energy in
            # 10-14 kHz; otherwise a voice/guitar frame with no cymbals looks
            # like a codec cutting and its on/off nature fakes jitter.
            # Cap the upper bound at 22.05 kHz so natural ultrasonic decay in
            # 96/192 kHz Hi-Res recordings is not mistaken for codec jitter.
            if pres_sl is not None:
                sl_presence, sl_hf = pres_sl
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
                        self._fc.extend(freqs_hf[last].tolist())

            # MDCT holes: 10 x 600 Hz sub-bands in 13.5-19.5 kHz.
            if sb_proj is None:
                return                     # the band does not exist
            sl_sb_union, w_sb = sb_proj
            sb_lin = block_power[:, sl_sb_union] @ w_sb
            sb = 10.0 * np.log10(np.maximum(sb_lin, 1e-28))
            gap = np.any(
                (sb[:, 1:-1] < sb[:, :-2] - self.FALL_GAP_DB)
                & (sb[:, 1:-1] < sb[:, 2:] - self.FALL_GAP_DB),
                axis=1,
            )
            self._frames_with_gap += int(np.count_nonzero(gap))
        except Exception as e:             # never abort the analysis of a file
            self._error = str(e)
            self._state = AccumulatorState.FAILED

    def summary(self) -> PsychoacousticSummary:
        """Finalizes and returns the immutable psychoacoustic summary."""
        if self._cached_summary is not None:
            return self._cached_summary
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
            if not opus_suspicion and 41000 <= self.sr <= 48000:
                pre_drop_20k = (level_db(18000.0, 19000.0)
                                - level_db(19650.0, 19850.0))
                cliff_20k = (level_db(19650.0, 19850.0)
                             - level_db(20200.0, 20450.0))
                post_drop_20k = (level_db(20450.0, 20900.0)
                                 - level_db(21100.0, 21850.0))
                opus_flat_shelf = bool(
                    pre_drop_20k <= 4.2
                    and cliff_20k >= 10.5
                    and cliff_20k >= 3.0 * max(pre_drop_20k, 0.8)
                    and (
                        abs(post_drop_20k) <= 1.8
                        or (cliff_20k >= 27.0
                            and 0.0 <= post_drop_20k <= 9.0
                            and cliff_20k >= 3.3 * post_drop_20k)
                    )
                )
                if opus_flat_shelf:
                    opus_suspicion = True
                    fall_opus = max(fall_opus, cliff_20k)

        # sfb21 islands: a lowpass-disabled lossy codec (LAME -V 0, AAC without
        # lowpass) zeroes the top scalefactor band in some frames but not in
        # others, so 16-20.5 kHz switches on and off relative to 4-11 kHz.
        brecha_sfb21 = 0.0
        ratio_off = 0.0
        islands_sfb21 = False
        if len(self._e_sfb21) >= 24:
            arr_sfb = np.asarray(self._e_sfb21, dtype=np.float64)
            arr_hi_sfb = np.asarray(self._env_16_20, dtype=np.float64)
            if np.all(np.isfinite(arr_sfb)) and np.all(np.isfinite(arr_hi_sfb)):
                brecha_sfb21 = float(np.percentile(arr_sfb, 90)
                                     - np.percentile(arr_sfb, 10))
                abs_gap_sfb21 = (
                    float(np.percentile(arr_hi_sfb, 90) - np.percentile(arr_hi_sfb, 10))
                    if arr_hi_sfb.size >= 24
                    else brecha_sfb21
                )
                ratio_off = float(np.mean(arr_sfb < -55.0))
                islands_sfb21 = bool(
                    brecha_sfb21 > 28.0
                    and abs_gap_sfb21 >= 12.0
                    and 0.25 <= ratio_off <= 0.85
                )

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
                    (drp_iqr >= 16.0 and top_iqr >= 16.0 and drp_med >= 24.0)
                    or (drp_iqr >= 10.0 and top_iqr >= 5.0 and drp_med >= 52.0)
                    or (drp_iqr >= 30.0 and top_iqr >= 30.0 and max(drp_med, float(np.mean(drp))) >= 24.0)
                    or (drp_med >= 48.0 and drp_iqr >= 12.0 and iqr > 550.0)
                )
                # Static LTI mastering LPFs around 19.5-20.3 kHz clamp quiet
                # frames of arr_20 at the 16-bit dither floor while loud frames
                # track arr_16 linearly (high corr(arr_16, drp), small loud spread);
                # ultra-quiet thermal/dither >20.2 kHz tails (<-122 dB without an
                # Opus cliff) also reflect dynamic acoustic fades rather than codecs.
                if (psycho_mod_20k and iqr <= 350.0 and not islands_sfb21
                        and ratio <= 0.05 and drp_med < 48.0 and drp_iqr < 28.0):
                    if (not opus_suspicion
                            and float(np.median(arr_20)) < -122.0
                            and drp_iqr < 27.5):
                        psycho_mod_20k = False
                    else:
                        hi_m = arr_16 >= float(np.median(arr_16))
                        if (np.count_nonzero(hi_m) >= 8
                                and float(np.std(arr_16)) > 1.5
                                and float(np.std(drp)) > 1.5):
                            loud_spread = float(
                                np.percentile(drp[hi_m], 90)
                                - np.percentile(drp[hi_m], 10)
                            )
                            corr_16_drp = float(np.corrcoef(arr_16, drp)[0, 1])
                            if (loud_spread < 13.0 and math.isfinite(corr_16_drp)
                                    and corr_16_drp > 0.40):
                                psycho_mod_20k = False

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
                self._ms_dynamic_collapse = bool(ms_p95 > 20.0 and ms_iqr > 10.0)

        # Phase 13: Spectral stationarity guard for sustained electronic synth
        # drones / modular harmonic pads. When the temporal envelope is nearly
        # static across active frames (< 1.8 dB std) and the high band consists
        # of discrete stationary harmonic overtones (>= 12 dB P95-P50 crest in
        # 13.5-19.5 kHz), inter-harmonic valleys are natural comb spacing rather
        # than MDCT frame holes or psychoacoustic frame starvation.
        is_stationary = False
        if (len(self._env_4_11) >= 16 and self._sum_power is not None
                and self._freqs is not None and self._frames >= 16):
            arr_env = np.asarray(self._env_4_11, dtype=np.float64)
            std_env = float(np.std(arr_env)) if np.all(np.isfinite(arr_env)) else 99.0
            if std_env < 5.5:
                freqs = self._freqs
                mean = self._sum_power / float(self._frames)
                mask_hb = (freqs >= 13500.0) & (freqs <= min(19500.0, self.sr * 0.45))
                if np.count_nonzero(mask_hb) >= 16:
                    hb_db = 10.0 * np.log10(np.maximum(mean[mask_hb], 1e-28))
                    crest_hb = float(np.percentile(hb_db, 95) - np.percentile(hb_db, 50))
                    if (std_env < 1.8 and crest_hb >= 12.0) or crest_hb >= 14.5:
                        is_stationary = True
                        ratio = 0.0
                        islands_sfb21 = False
                        psycho_mod_20k = False
                        iqr = min(iqr, 350.0)
                        self._ms_dynamic_collapse = False

        metrics = {
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
            "es_estacionario":          is_stationary,
        }
        summary = PsychoacousticSummary(
            metrics=metrics,
            ms_p95_db=self._ms_p95_db,
            ms_dynamic_collapse=self._ms_dynamic_collapse,
            error=self._error,
        )
        self._finalized = True
        self._state = (
            AccumulatorState.FAILED if self._error else AccumulatorState.COMPLETED
        )
        self._cached_summary = summary
        return summary

    def result(self):
        return dict(self.summary().metrics)


@functools.lru_cache(maxsize=None)
def _get_vorbis_2048():
    n = np.arange(2048, dtype=np.float32)
    win = np.sin(
        0.5 * np.pi * (np.sin(np.pi / 2048.0 * (n + 0.5)) ** 2)
    ).astype(np.float32)
    win.setflags(write=False)
    return win


@functools.lru_cache(maxsize=None)
def _get_kbd_2048():
    """ISO/IEC 14496-3 MPEG-4 AAC Kaiser-Bessel Derived (KBD, alpha=4) window."""
    cs = np.cumsum(np.kaiser(1025, 4.0 * np.pi), dtype=np.float64)
    half = np.sqrt(cs[:1024] / cs[1024]).astype(np.float32)
    win = np.concatenate([half, half[::-1]])
    win.setflags(write=False)
    return win


@functools.lru_cache(maxsize=None)
def _get_sine_2048():
    """ISO/IEC 14496-3 AAC / WMA 2048-sample Sine analysis window."""
    n = np.arange(2048, dtype=np.float32)
    win = np.sin(np.pi / 2048.0 * (n + 0.5)).astype(np.float32)
    win.setflags(write=False)
    return win


@functools.lru_cache(maxsize=None)
def _get_kbd_512_ac3():
    """ATSC A/52 AC-3 / E-AC-3 512-sample Kaiser-Bessel Derived (alpha=5) window."""
    n_half = 256
    idx = np.arange(n_half + 1, dtype=np.float64)
    beta = 5.0 * np.pi
    arg = beta * np.sqrt(np.maximum(0.0, 1.0 - (2.0 * idx / float(n_half) - 1.0) ** 2))
    w_kaiser = np.i0(arg) / np.i0(beta)
    cs = np.cumsum(w_kaiser, dtype=np.float64)
    half = np.sqrt(cs[:n_half] / cs[n_half]).astype(np.float32)
    win = np.concatenate([half, half[::-1]])
    win.setflags(write=False)
    return win


@functools.lru_cache(maxsize=None)
def _get_celt_1920():
    n_mdct, span_ov = 960, 120
    two_n = 1920
    pad = (n_mdct - span_ov) // 2
    w = np.zeros(two_n, dtype=np.float32)
    n = np.arange(span_ov, dtype=np.float32)
    trans = np.sin(
        0.5 * np.pi * (np.sin(0.5 * np.pi * (n + 0.5) / span_ov) ** 2)
    ).astype(np.float32)
    w[pad : pad + span_ov] = trans
    w[pad + span_ov : two_n - pad - span_ov] = 1.0
    w[two_n - pad - span_ov : two_n - pad] = trans[::-1]
    w.setflags(write=False)
    return w


@functools.lru_cache(maxsize=8)
def _get_pqmf_fast_cached(n_fft: int, sb_indices: tuple[int, ...]):
    m_tap = np.arange(512, dtype=np.float64) - 256.0
    win_kaiser = np.concatenate(([0.0], np.kaiser(511, 9.0)))
    h_raw = (1.0 / 64.0) * np.sinc(m_tap / 64.0) * win_kaiser
    h = (h_raw / np.sum(h_raw)).astype(np.float32)
    n_idx = np.arange(512, dtype=np.float32)
    bank = np.stack([
        h * np.cos(np.pi / 64.0 * (2 * s + 1) * (n_idx - 16)).astype(np.float32)
        for s in sb_indices
    ], axis=0)
    bank_fft = _rfft_f32(bank, n=n_fft, axis=1)
    win36 = np.sin(np.pi / 36.0 * (np.arange(36, dtype=np.float32) + 0.5)).astype(np.float32)
    bank_fft.setflags(write=False)
    win36.setflags(write=False)
    return bank_fft, win36


def _get_pqmf_fast(n_fft, sb_indices=(19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29)):
    return _get_pqmf_fast_cached(int(n_fft), tuple(sb_indices))


def _robust_z_peak(values, min_scale=1e-3):
    """Peak Z-score against a scale floor combining MAD, IQR/1.349 and min_scale."""
    arr = np.asarray(values, dtype=np.float64)
    if arr.size == 0:
        return 0.0, 0.0, 0.0
    med = float(np.median(arr))
    mx = float(np.max(arr))
    if mx <= med:
        return 0.0, med, mx
    mad = float(np.median(np.abs(arr - med)))
    iqr_scale = float(np.percentile(arr, 75) - np.percentile(arr, 25)) / 1.349
    scale = max(1.4826 * mad, 0.5 * iqr_scale, float(min_scale))
    return (mx - med) / scale, med, mx


def _select_continuous_subwindow(mono, needed):
    """Selects a contiguous `needed`-sample window from `mono` by evaluating
    up to 5 equispaced candidate offsets and choosing the one with the highest
    20th-percentile local energy (avoiding micro-silences or edit gaps)."""
    total = int(mono.size)
    needed = int(needed)
    if total <= needed:
        return np.ascontiguousarray(mono[:needed], dtype=np.float32)
    slack = total - needed
    center = slack // 2
    if slack < 2048:
        return np.ascontiguousarray(mono[center : center + needed], dtype=np.float32)
    candidates = [int(round(i * slack / 4.0)) for i in range(5)]
    if center not in candidates:
        candidates[2] = center
    best_s0 = center
    best_score = -1.0
    chunk_len = max(256, needed // 16)
    n_chunks = max(1, needed // chunk_len)
    for s0 in candidates:
        sub = mono[s0 : s0 + n_chunks * chunk_len].reshape(n_chunks, chunk_len)
        e_chunks = np.mean(sub * sub, axis=1)
        p20 = float(np.percentile(e_chunks, 20))
        score = p20 * (1.02 if s0 == center else 1.0)
        if score > best_score:
            best_score = score
            best_s0 = s0
    return np.ascontiguousarray(mono[best_s0 : best_s0 + needed], dtype=np.float32)


def _select_hf_subwindow_candidates(mono, needed, max_windows=3):
    """Selects up to `max_windows` non-overlapping contiguous `needed`-sample
    windows from `mono`, ranked by 75th-percentile second-order difference
    energy so high-frequency transients are prioritized over bass/midrange
    passages while avoiding silence gaps."""
    total = int(mono.size)
    needed = int(needed)
    if total <= needed:
        return [np.ascontiguousarray(mono[:needed], dtype=np.float32)]
    slack = total - needed
    if slack < 2048:
        center = slack // 2
        return [np.ascontiguousarray(mono[center : center + needed], dtype=np.float32)]
    n_cand = 9
    candidates = []
    for i in range(n_cand):
        s0 = int(round(i * slack / float(n_cand - 1)))
        if s0 not in candidates:
            candidates.append(s0)
    center = slack // 2
    if center not in candidates:
        candidates.append(center)
    chunk_len = max(256, needed // 16)
    n_chunks = max(1, needed // chunk_len)
    ranked = []
    for s0 in candidates:
        sub = mono[s0 : s0 + n_chunks * chunk_len].reshape(n_chunks, chunk_len)
        bb_chunks = np.mean(sub * sub, axis=1)
        p10_bb = float(np.percentile(bb_chunks, 10))
        d2 = sub[:, 2:] - np.float32(2.0) * sub[:, 1:-1] + sub[:, :-2]
        hf_chunks = np.mean(d2 * d2, axis=1)
        p75_hf = float(np.percentile(hf_chunks, 75))
        has_floor = 1 if p10_bb > 1e-9 else 0
        ranked.append((has_floor, p75_hf, s0))
    ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
    chosen = []
    min_sep = max(1024, needed // 2)
    for _, _, s0 in ranked:
        if all(abs(s0 - prev) >= min_sep for prev in chosen):
            chosen.append(s0)
            if len(chosen) >= int(max_windows):
                break
    if not chosen:
        chosen.append(ranked[0][2])
    return [
        np.ascontiguousarray(mono[s0 : s0 + needed], dtype=np.float32)
        for s0 in chosen
    ]


def _fast_mdct_f32(frames, window):
    """Vectorized float32 MDCT via TDAC folding + orthogonal DCT-IV."""
    two_n = frames.shape[-1]
    n = two_n // 2
    n2 = n // 2
    folded = np.empty(frames.shape[:-1] + (n,), dtype=np.float32)
    if frames.ndim == 2 and frames.shape[0] > 64:
        # Tile rows inside L2 cache to avoid materializing a full (B, 2N) buffer.
        tile = 64
        w_tile = np.empty((tile, two_n), dtype=np.float32)
        for start in range(0, frames.shape[0], tile):
            end = min(start + tile, frames.shape[0])
            cur = w_tile[: end - start]
            np.multiply(frames[start:end], window, out=cur)
            np.subtract(-cur[:, n + n2 - 1 : n - 1 : -1], cur[:, n + n2 :], out=folded[start:end, :n2])
            np.subtract(cur[:, :n2], cur[:, n - 1 : n2 - 1 : -1], out=folded[start:end, n2:])
    else:
        w_frames = frames * window
        a = w_frames[..., :n2]
        b = w_frames[..., n2:n]
        c = w_frames[..., n : n + n2]
        d = w_frames[..., n + n2 :]
        folded[..., :n2] = -c[..., ::-1] - d
        folded[..., n2:] = a - b[..., ::-1]
    return _dct4_ortho_f32(folded, axis=-1, overwrite_x=True)


@functools.lru_cache(maxsize=None)
def _get_mdct36_basis():
    """Cached (36, 18) float32 transform matrix for PQMF-576 36-tap MDCT."""
    win36 = np.sin(
        np.pi / 36.0 * (np.arange(36, dtype=np.float32) + 0.5)
    ).astype(np.float32)
    basis = np.ascontiguousarray(
        _fast_mdct_f32(np.eye(36, dtype=np.float32), win36), dtype=np.float32
    )
    basis.setflags(write=False)
    return basis


@functools.lru_cache(maxsize=None)
def _get_polyphase_160_147_f32():
    """Cached (160, 21) Kaiser (beta=8.6) FIR polyphase bank for 44.1 -> 48 kHz."""
    up, down = 160, 147
    half_len = 10 * up
    n_taps = 2 * half_len + 1
    idx = np.arange(n_taps, dtype=np.float64) - half_len
    h64 = np.sinc((1.0 / up) * idx) * np.kaiser(n_taps, 8.6)
    h64 *= up / np.sum(h64)
    bank = np.zeros((up, 21), dtype=np.float32)
    for p in range(up):
        for k, j in enumerate(range(-10, 11)):
            t_idx = half_len + p - j * up
            if 0 <= t_idx < n_taps:
                bank[p, k] = np.float32(h64[t_idx])
    out = np.ascontiguousarray(bank, dtype=np.float32)
    out.setflags(write=False)
    return out


def _resample_celt_44k_to_48k_slice(seg_src, start_out, length_out):
    """Exact polyphase 160/147 slice resampler without importing scipy.signal."""
    n_in = len(seg_src)
    if n_in == 0 or length_out <= 0:
        return np.empty(0, dtype=np.float32)
    up, down = 160, 147
    n_out = (n_in * up + down - 1) // down
    hi = min(n_out, start_out + length_out)
    if hi <= start_out:
        return np.empty(0, dtype=np.float32)
    bank = _get_polyphase_160_147_f32()
    m = np.arange(start_out, hi, dtype=np.int64)
    prod = m * down
    base_i = prod // up
    phase = prod % up
    max_needed_in = min(n_in, int(base_i[-1]) + 11)
    x_pad = np.empty(max_needed_in + 22, dtype=np.float32)
    x_pad[:10] = 0.0
    x_pad[10 : 10 + max_needed_in] = seg_src[:max_needed_in]
    x_pad[10 + max_needed_in :] = 0.0
    offsets = np.arange(21, dtype=np.int64)[None, :]
    windows = x_pad[base_i[:, None] + offsets]
    return np.sum(windows * bank[phase], axis=1, dtype=np.float32)


@functools.lru_cache(maxsize=8)
def _get_decim_fir_f32(factor: int):
    """Cached Kaiser (beta=9.0) low-pass FIR for integer Hi-Res decimation."""
    f_int = int(factor)
    half = 16 * f_int
    n_taps = 2 * half + 1
    idx = np.arange(n_taps, dtype=np.float64) - half
    fc = 0.96 / float(f_int)
    h = fc * np.sinc(fc * idx) * np.kaiser(n_taps, 9.0)
    h /= np.sum(h)
    out = np.ascontiguousarray(h, dtype=np.float32)
    out.setflags(write=False)
    return out


def _decimate_int_f32(x, factor: int, trim_edges: bool = True):
    """Pure-NumPy / pypocketfft integer-factor decimation with optional edge trim."""
    arr = np.asarray(x, dtype=np.float32).reshape(-1)
    f_int = int(factor)
    if f_int <= 1 or arr.size == 0:
        return np.ascontiguousarray(arr, dtype=np.float32)
    h = _get_decim_fir_f32(f_int)
    n = arr.size
    n_taps = h.size
    half = n_taps // 2
    n_fft = 1 << int(math.ceil(math.log2(max(2, n + n_taps - 1))))
    y_full = _irfft_f32(_rfft_f32(arr, n=n_fft) * _rfft_f32(h, n=n_fft), n=n_fft)
    y_dec = y_full[half : half + n : f_int]
    if trim_edges:
        trim = min(16, max(0, (y_dec.size - 24576) // 2))
        if trim > 0:
            y_dec = y_dec[trim:-trim]
    return np.ascontiguousarray(y_dec, dtype=np.float32)


@functools.lru_cache(maxsize=None)
def _get_polyphase_147_320_f32():
    """Cached (147, 47) Kaiser (beta=8.6) FIR polyphase bank for 96 -> 44.1 kHz."""
    up, down = 147, 320
    half_len = 10 * down
    n_taps = 2 * half_len + 1
    idx = np.arange(n_taps, dtype=np.float64) - half_len
    h64 = (1.0 / float(down)) * np.sinc((1.0 / float(down)) * idx) * np.kaiser(n_taps, 8.6)
    h64 *= float(up) / np.sum(h64)
    max_j = int(math.ceil(half_len / float(up))) + 1
    width = 2 * max_j + 1
    bank = np.zeros((up, width), dtype=np.float32)
    for p in range(up):
        for k, j in enumerate(range(-max_j, max_j + 1)):
            t_idx = half_len + p - j * up
            if 0 <= t_idx < n_taps:
                bank[p, k] = np.float32(h64[t_idx])
    out = np.ascontiguousarray(bank, dtype=np.float32)
    out.setflags(write=False)
    return out, max_j


def _resample_96k_to_44k1_f32(x):
    """Chunked pure-NumPy 147/320 polyphase resampler (96 -> 44.1 kHz) with trim."""
    arr = np.asarray(x, dtype=np.float32).reshape(-1)
    up, down = 147, 320
    n_in = arr.size
    n_out = (n_in * up) // down
    if n_in == 0 or n_out <= 0:
        return np.empty(0, dtype=np.float32)
    bank, max_j = _get_polyphase_147_320_f32()
    width = 2 * max_j + 1
    m = np.arange(n_out, dtype=np.int64)
    prod = m * down
    base_i = prod // up
    phase = prod % up
    x_pad = np.empty(n_in + 2 * max_j + 2, dtype=np.float32)
    x_pad[:max_j] = 0.0
    x_pad[max_j : max_j + n_in] = arr
    x_pad[max_j + n_in :] = 0.0
    offsets = np.arange(width, dtype=np.int64)[None, :]
    out = np.empty(n_out, dtype=np.float32)
    chunk = 16384
    for start in range(0, n_out, chunk):
        end = min(start + chunk, n_out)
        windows = x_pad[base_i[start:end, None] + offsets]
        out[start:end] = np.sum(windows * bank[phase[start:end]], axis=1, dtype=np.float32)
    trim = min(16, max(0, (out.size - 24576) // 2))
    if trim > 0:
        out = out[trim:-trim]
    return np.ascontiguousarray(out, dtype=np.float32)


def _select_aligned_subwindow(mono, needed, block_start=0, align_mod=1):
    """Selects an energetic contiguous slice whose file-global start index is
    aligned to `align_mod`, preserving zero sub-sample phase offset for
    rational polyphase decimation (e.g., 320 samples for 96 -> 44.1 kHz)."""
    total = int(mono.size)
    needed = int(needed)
    mod = max(1, int(align_mod))
    rem = int(block_start) % mod
    first_aligned = (mod - rem) % mod
    if total - first_aligned <= needed:
        return np.ascontiguousarray(mono[first_aligned : first_aligned + needed], dtype=np.float32)
    slack = total - first_aligned - needed
    best_s0 = first_aligned + ((slack // 2) // mod) * mod
    if slack < max(2048, mod):
        return np.ascontiguousarray(mono[best_s0 : best_s0 + needed], dtype=np.float32)
    candidates = []
    for i in range(5):
        raw_off = int(round(i * slack / 4.0))
        s0 = first_aligned + (raw_off // mod) * mod
        if s0 + needed <= total and s0 not in candidates:
            candidates.append(s0)
    if best_s0 not in candidates and best_s0 + needed <= total:
        candidates.append(best_s0)
    best_score = -1.0
    chunk_len = max(256, needed // 16)
    n_chunks = max(1, needed // chunk_len)
    center_s0 = best_s0
    for s0 in candidates:
        sub = mono[s0 : s0 + n_chunks * chunk_len].reshape(n_chunks, chunk_len)
        p20 = float(np.percentile(np.mean(sub * sub, axis=1), 20))
        score = p20 * (1.02 if s0 == center_s0 else 1.0)
        if score > best_score:
            best_score = score
            best_s0 = s0
    return np.ascontiguousarray(mono[best_s0 : best_s0 + needed], dtype=np.float32)


def _decimate_hires_to_target(mono_blk, sr, target_sr, block_start=0):
    """Decimates one active Hi-Res channel slice to 48 kHz or 44.1 kHz."""
    arr = np.asarray(mono_blk, dtype=np.float32).reshape(-1)
    sr_int = int(sr)
    tgt = int(target_sr)
    if arr.size < 32768 or sr_int < 88200:
        return np.empty(0, dtype=np.float32)
    if sr_int % 48000 == 0:
        ratio_96 = sr_int // 96000
        if ratio_96 < 1 or (ratio_96 & (ratio_96 - 1)) != 0:
            return np.empty(0, dtype=np.float32)
        target_slice = min(arr.size, 98304 * ratio_96)
        if tgt == 48000:
            factor = 2 * ratio_96
            seg = _select_aligned_subwindow(arr, target_slice, block_start, align_mod=factor)
            return _decimate_int_f32(seg, factor)
        if tgt == 44100:
            align_m = 320 * ratio_96
            seg = _select_aligned_subwindow(arr, target_slice, block_start, align_mod=align_m)
            if ratio_96 > 1:
                seg96 = _decimate_int_f32(seg, ratio_96, trim_edges=False)
                if seg96.size > 640 + 56000:
                    seg96 = seg96[320:-320]
            else:
                seg96 = seg
            return _resample_96k_to_44k1_f32(seg96)
        return np.empty(0, dtype=np.float32)
    if sr_int % 44100 == 0:
        ratio_88 = sr_int // 88200
        if ratio_88 < 1 or (ratio_88 & (ratio_88 - 1)) != 0:
            return np.empty(0, dtype=np.float32)
        target_slice = min(arr.size, 98304 * ratio_88)
        factor = 2 * ratio_88
        if tgt == 44100:
            seg = _select_aligned_subwindow(arr, target_slice, block_start, align_mod=factor)
            return _decimate_int_f32(seg, factor)
        if tgt == 48000:
            align_m = 147 * factor
            seg = _select_aligned_subwindow(arr, target_slice, block_start, align_mod=align_m)
            seg44 = _decimate_int_f32(seg, factor, trim_edges=False)
            if seg44.size > 294 + 26000:
                seg44 = seg44[147:-147]
            n_out = (seg44.size * 160) // 147
            if n_out <= 64:
                return np.empty(0, dtype=np.float32)
            return _resample_celt_44k_to_48k_slice(seg44, 16, n_out - 32)
    return np.empty(0, dtype=np.float32)


def _detect_sbr_qmf_replication(mono, sr):
    """Phase 12: seamless QMF subband replication detector for HE-AAC v1/v2 SBR.

    Subtracts the local ~1.1 kHz spectral envelope per STFT frame to factor out
    broadband percussive transients and SBR coarse envelope adjusters, then
    correlates fine spectral trajectories across additive QMF patch shifts in
    3.5-9.5 kHz. Genuine harmonics dilate multiplicatively (f -> 2f) rather than
    by a constant additive translation (f -> f + delta_f). Works at CD and Hi-Res
    sample rates alike, without concatenating non-adjacent blocks.
    """
    neutral = {
        "sbr_qmf_detectado": False,
        "sbr_qmf_correlacion": 0.0,
        "sbr_qmf_z": 0.0,
    }
    if sr < 32000:
        return neutral
    try:
        if isinstance(mono, (list, tuple)):
            blocks = [np.asarray(blk, dtype=np.float32).reshape(-1)
                      for blk in mono if blk is not None and len(blk) > 0]
        else:
            arr = np.asarray(mono, dtype=np.float32).reshape(-1)
            blocks = [arr] if arr.size else []
        total_samples = sum(blk.size for blk in blocks)
        if total_samples < sr:
            return neutral

        n_fft = 2048 * (1 << int(round(math.log2(max(1.0, sr / 46000.0)))))
        hop = n_fft // 2
        win = np.hanning(n_fft + 1)[:-1].astype(np.float32)
        max_samples = int(sr * 6)
        gathered_frames = []
        gathered_samples = 0
        for blk in blocks:
            if blk.size < n_fft or float(np.std(blk)) < 1e-4:
                continue
            rem_samples = max_samples - gathered_samples
            if rem_samples < n_fft:
                break
            take = min(blk.size, rem_samples)
            s0 = (blk.size - take) // 2
            seg = blk[s0 : s0 + take]
            n_frames_blk = (seg.size - n_fft) // hop + 1
            if n_frames_blk <= 0:
                continue
            frames_blk = np.lib.stride_tricks.sliding_window_view(seg, n_fft)[::hop][:n_frames_blk]
            gathered_frames.append(frames_blk)
            gathered_samples += take
            if gathered_samples >= max_samples:
                break

        if not gathered_frames:
            return neutral
        frames = (gathered_frames[0] if len(gathered_frames) == 1
                  else np.concatenate(gathered_frames, axis=0))
        if len(frames) < 24:
            return neutral
        if len(frames) > 160:
            frames = frames[:: max(1, len(frames) // 160)][:160]
        spec = _rfft_f32(frames * win, axis=1)
        df = sr / float(n_fft)
        k_max = min(spec.shape[1], int(20200.0 / df))
        p_db = 10.0 * np.log10(np.abs(spec[:, :k_max]) ** 2 + np.float32(1e-24))

        frame_e = np.mean(p_db[:, int(1000.0 / df) : int(9000.0 / df)], axis=1)
        active = frame_e > (np.max(frame_e) - 35.0)
        if int(np.count_nonzero(active)) < 20:
            return neutral
        p_db = p_db[active]

        k_115 = int(11500.0 / df)
        k_185 = min(k_max, int(18500.0 / df))
        k_40 = int(4000.0 / df)
        k_100 = int(10000.0 / df)
        if k_185 <= k_115 + 32:
            return neutral
        if float(np.mean(p_db[:, k_115:k_185])) < float(np.mean(p_db[:, k_40:k_100])) - 45.0:
            return neutral

        w_bins = max(9, int(round(1100.0 / df)) | 1)
        padded = np.pad(p_db, ((0, 0), (w_bins // 2, w_bins // 2 + 1)), mode="edge")
        cs = np.cumsum(padded, axis=1, dtype=np.float32)
        smooth_f = (cs[:, w_bins:] - cs[:, :-w_bins]) / np.float32(w_bins)
        resid = p_db - smooth_f
        resid -= np.mean(resid, axis=1, keepdims=True)
        resid -= np.mean(resid, axis=0, keepdims=True)
        norms = np.sqrt(np.sum(resid * resid, axis=0, keepdims=True)) + np.float32(1e-9)
        normed = resid / norms

        dk_min = int(3500.0 / df)
        dk_max = min(k_115 - 4, int(9500.0 / df))
        if dk_max <= dk_min + 8:
            return neutral
        tgt = normed[:, k_115:k_185]
        b_len = tgt.shape[1]
        patch_bins = min(b_len, max(32, int(round(2200.0 / df))))
        inv_patch = np.float32(1.0 / float(patch_bins))
        src_region = normed[:, k_115 - dk_max : k_185 - dk_min]
        src_windows = np.lib.stride_tricks.sliding_window_view(
            src_region, b_len, axis=1
        )[:, ::-1, :]
        corr_all = np.einsum("fb,fsb->sb", tgt, src_windows)
        cs_all = np.cumsum(
            np.pad(corr_all, ((0, 0), (1, 0))), axis=1, dtype=np.float32
        )
        win_means_all = (cs_all[:, patch_bins:] - cs_all[:, :-patch_bins]) * inv_patch
        shift_scores = np.max(win_means_all, axis=1)

        z_sbr, _med, mx = _robust_z_peak(shift_scores, min_scale=1e-2)
        detected = bool(
            (mx >= 0.36 and z_sbr >= 6.5)
            or (mx >= 0.28 and z_sbr >= 11.0)
            or (mx >= 0.21 and z_sbr >= 16.0)
        )
        return {
            "sbr_qmf_detectado": detected,
            "sbr_qmf_correlacion": round(float(mx), 4) if (detected and math.isfinite(mx)) else 0.0,
            "sbr_qmf_z": round(float(z_sbr), 2) if (detected and math.isfinite(z_sbr)) else 0.0,
        }
    except Exception as exc:
        return {**neutral, "sbr_detector_error": f"{type(exc).__name__}: {exc}"}


def _eval_lapped_codec_grid(mono, sr, ceiling_hz=None, skip_celt: bool = False):
    """Single-mode lapped transform & subband lattice evaluator."""
    neutral = {
        "mdct_lattice_detected": False,
        "mdct_lattice_snr": 0.0,
        "mdct_lattice_family": None,
    }
    if not (41000 <= sr <= 48000):
        return neutral
    try:
        mono = np.asarray(mono, dtype=np.float32)
        if mono.size < 24576 or float(np.std(mono)) < 1e-4:
            return neutral

        nyq = sr / 2.0
        fullband_mode = ceiling_hz is None or ceiling_hz >= 17500.0
        if fullband_mode:
            bands_1024 = [(12000.0, min(20500.0, sr * 0.46))]
        elif ceiling_hz >= 9500.0:
            bands_1024 = [
                (max(4200.0, ceiling_hz * 0.48), ceiling_hz * 0.90),
                (max(7000.0, ceiling_hz * 0.78), min(20500.0, sr * 0.46, max(15500.0, ceiling_hz * 1.25))),
            ]
        else:
            return neutral

        # 1. MDCT-1024 (Vorbis / AAC Sine & KBD alpha=4)
        z1024 = 0.0
        n_blocks_1024 = 10
        needed_1024 = n_blocks_1024 * 1024 + 2048
        if mono.size >= needed_1024:
            seg = _select_continuous_subwindow(mono, needed_1024)
            n_all_1024 = n_blocks_1024 * 1024
            all_frames = np.lib.stride_tricks.sliding_window_view(seg, 2048)[:n_all_1024]
            valid_slices_1024 = []
            for f_lo, f_hi in bands_1024:
                k_lo = int(f_lo / nyq * 1024)
                k_hi = int(f_hi / nyq * 1024)
                if k_hi > k_lo + 8:
                    valid_slices_1024.append((k_lo, k_hi))
            if valid_slices_1024:
                for w in (_get_vorbis_2048(), _get_kbd_2048(), _get_sine_2048()):
                    sp_by_band = [
                        np.empty(n_all_1024, dtype=np.float32)
                        for _ in valid_slices_1024
                    ]
                    for i in range(0, n_all_1024, 1024):
                        chunk = all_frames[i : i + 1024]
                        coeffs_chunk = np.abs(_fast_mdct_f32(chunk, w))
                        for b_idx, (k_lo, k_hi) in enumerate(valid_slices_1024):
                            coeffs = coeffs_chunk[:, k_lo:k_hi]
                            rms_c = np.sqrt(np.mean(coeffs * coeffs, axis=1, keepdims=True)) + np.float32(1e-12)
                            active_c = rms_c[:, 0] > np.float32(1e-6)
                            sp_chunk = np.mean(coeffs < (rms_c * np.float32(0.05)), axis=1)
                            sp_by_band[b_idx][i : i + 1024] = np.where(active_c, sp_chunk, np.float32(0.0))
                    for sp_all in sp_by_band:
                        scores_1024 = sp_all.reshape(n_blocks_1024, 8, 128).mean(axis=0).max(axis=0)
                        z_cand, med_c, mx_c = _robust_z_peak(scores_1024, min_scale=1e-3)
                        if med_c < 0.55 and (mx_c - med_c) >= 0.008:
                            z1024 = max(z1024, z_cand)
                        if z1024 >= 10.0:
                            break
                    if z1024 >= 10.0:
                        break

        if z1024 >= 10.0:
            return {
                "mdct_lattice_detected": True,
                "mdct_lattice_snr": round(float(z1024), 2),
                "mdct_lattice_family": CodecLatticeFamily.MDCT_1024.value,
            }

        # 2. PQMF-576 (MP3 hybrid filterbank) & PQMF-32 (Musepack subband frames)
        z576 = 0.0
        z_mpc = 0.0
        n_gran = 32
        needed_576 = (n_gran + 3) * 576 + 512
        if mono.size >= needed_576:
            seg = _select_continuous_subwindow(mono, needed_576)
            n_fft = 1 << int(np.ceil(np.log2(needed_576 + 512)))
            seg_fft = _rfft_f32(seg, n=n_fft)

            if fullband_mode:
                pqmf_sets = [(19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29)]
                if sr > 44100 or (ceiling_hz is not None and ceiling_hz < 0.91 * nyq):
                    f_mp3_top = min(20250.0, (ceiling_hz * 0.96) if ceiling_hz else 20250.0)
                    sb_top_48 = min(27, max(21, int(round(f_mp3_top / (sr / 64.0))) - 1))
                    cand_48 = tuple(range(sb_top_48 - 7, sb_top_48 + 1))
                    if cand_48 not in pqmf_sets:
                        pqmf_sets.append(cand_48)
            else:
                pqmf_sets = []
                for _, f_hi in bands_1024:
                    sb_top = min(29, max(14, int(f_hi / (sr / 64.0))))
                    cand = tuple(range(sb_top - 7, sb_top + 1))
                    if cand not in pqmf_sets:
                        pqmf_sets.append(cand)

            for sb_set in pqmf_sets:
                bank_fft, win36 = _get_pqmf_fast(n_fft, sb_set)
                conv_all = _irfft_f32(seg_fft[None, :] * bank_fft, n=n_fft, axis=1)
                sb_arr_all = conv_all[:, 511:needed_576].astype(np.float32)
                m_sub_total = sb_arr_all.shape[1] // 32

                # In fullband mode, sb_set has 11 bands (19..29): [3:11] are 22..29 for MP3,
                # while [0:3] vs [4:11] evaluate Musepack 36-sample pure subband frames.
                if len(sb_set) == 11:
                    sb_mp3 = sb_arr_all[3:11]
                    mp3_sbs = sb_set[3:11]
                else:
                    sb_mp3 = sb_arr_all
                    mp3_sbs = sb_set

                dec_all = sb_mp3[:, : m_sub_total * 32].reshape(8, m_sub_total, 32).transpose(2, 0, 1).copy()
                for idx_s, s_val in enumerate(mp3_sbs):
                    if s_val % 2 == 1:
                        dec_all[:, idx_s, 1::2] *= -1.0
                all_win36 = np.lib.stride_tricks.sliding_window_view(dec_all, 36, axis=2)
                n_avail = (all_win36.shape[2] // 18) * 18
                if n_avail >= 18 * 10:
                    m_frames = n_avail // 18
                    grouped = all_win36[:, :, :n_avail, :].reshape(32, 8, m_frames, 18, 36)
                    coeffs = np.abs(grouped @ _get_mdct36_basis())

                    def _score_pqmf_slice(coeffs_slice):
                        n_sb_cur = coeffs_slice.shape[1]
                        flat_cur = coeffs_slice.transpose(0, 3, 2, 1, 4).reshape(
                            576, m_frames, n_sb_cur * 18
                        )
                        rms_cur = np.sqrt(np.mean(flat_cur * flat_cur, axis=2, keepdims=True)) + np.float32(1e-12)
                        active_cur = rms_cur[:, :, 0] > np.float32(1e-6)
                        sp_cur = np.where(
                            active_cur,
                            np.mean(flat_cur < (rms_cur * np.float32(0.05)), axis=2),
                            np.float32(0.0),
                        )
                        scores_cur = sp_cur.mean(axis=1)
                        z_cur, med_cur, mx_cur = _robust_z_peak(scores_cur, min_scale=1e-3)
                        if med_cur < 0.55 and (mx_cur - med_cur) >= 0.015:
                            return z_cur
                        return 0.0

                    z576 = max(z576, _score_pqmf_slice(coeffs))
                    sub_rms = np.sqrt(np.mean(coeffs * coeffs, axis=(0, 2, 3, 4)))
                    max_sub_rms = float(np.max(sub_rms))
                    if max_sub_rms > 0.0:
                        active_sb = sub_rms >= (0.05 * max_sub_rms)
                        n_act_sb = int(np.count_nonzero(active_sb))
                        if 4 <= n_act_sb < 8:
                            z576 = max(z576, _score_pqmf_slice(coeffs[:, active_sb, :, :, :]))

                if z576 >= 12.0:
                    break

                allow_mpc = len(sb_set) == 11 or (
                    not fullband_mode
                    and len(sb_set) >= 8
                    and ceiling_hz is not None
                    and ceiling_hz >= 13500.0
                )
                if allow_mpc and m_sub_total >= 36 * 10:
                    n_sb_mpc = len(sb_set)
                    dec_mpc = sb_arr_all[:, : m_sub_total * 32].reshape(
                        n_sb_mpc, m_sub_total, 32).transpose(2, 0, 1)
                    w_mpc = np.lib.stride_tricks.sliding_window_view(dec_mpc, 6, axis=2)
                    nf_mpc = (w_mpc.shape[2] // 36) * 36
                    n_frames_36 = nf_mpc // 36
                    half_36 = n_frames_36 // 2
                    if n_frames_36 >= 8 and half_36 >= 4:
                        blks_m = w_mpc[:, :, :nf_mpc, :].reshape(32, n_sb_mpc, n_frames_36, 36, 6)
                        rms_m = np.sqrt(np.mean(blks_m * blks_m, axis=4)) + np.float32(1e-12)
                        if n_sb_mpc == 11:
                            anchor_m = np.mean(rms_m[:, :3], axis=1, keepdims=True)
                            upper_m = rms_m[:, 4:11]
                        else:
                            anchor_m = np.mean(rms_m[:, :2], axis=1, keepdims=True)
                            upper_m = rms_m[:, 2:]
                        p80_anc = float(np.percentile(anchor_m[0], 80)) + 1e-12
                        p80_upp = np.percentile(upper_m[0], 80, axis=(1, 2))
                        valid_sb = (p80_upp > 1e-5) & ((p80_upp / p80_anc) > 0.015)
                        n_valid = int(np.count_nonzero(valid_sb))
                        if n_valid >= 2:
                            scale_s = (p80_upp / p80_anc)[None, :, None, None].astype(np.float32)
                            act_m = anchor_m[:, 0] > (p80_anc * 0.15)
                            holes_m = (
                                (upper_m[:, valid_sb] < (anchor_m * scale_s[:, valid_sb] * np.float32(0.06)))
                                & act_m[:, None, :, :]
                            )
                            denom_m = float(n_valid) * np.maximum(1, np.sum(act_m, axis=1))
                            curves_36 = np.sum(holes_m, axis=(1, 2)) / denom_m
                            spans = np.max(curves_36, axis=1) - np.min(curves_36, axis=1)
                            best_off = int(np.argmax(spans))
                            c36 = curves_36[best_off]
                            mx_m = float(np.max(c36))
                            mn_m = float(np.min(c36))
                            if mx_m >= 0.045 and (mx_m - mn_m) >= 0.035:
                                dft_m = np.abs(np.fft.rfft(c36 - np.mean(c36)))
                                bg_m = np.delete(dft_m, [0, 1, 2, 3, 6])
                                med_bg_raw = float(np.median(bg_m))
                                med_bg = med_bg_raw + 1e-9
                                r1 = float(dft_m[1]) / med_bg
                                r2 = float(dft_m[2]) / med_bg
                                r3 = float(dft_m[3]) / med_bg
                                if z576 >= 9.0 and r2 >= 3.5 and dft_m[2] >= dft_m[3]:
                                    z576 = max(z576, 12.0, r1, r2)
                                    break
                                elif r3 >= 5.5 or z576 < 8.5:
                                    k_win = 1 if dft_m[1] >= dft_m[3] else 3
                                    act_off = act_m[best_off]
                                    holes_off = holes_m[best_off]
                                    denom_h1 = float(n_valid) * np.maximum(
                                        1, np.sum(act_off[:half_36], axis=0)
                                    )
                                    denom_h2 = float(n_valid) * np.maximum(
                                        1, np.sum(act_off[half_36 : 2 * half_36], axis=0)
                                    )
                                    c_h1 = np.sum(holes_off[:, :half_36, :], axis=(0, 1)) / denom_h1
                                    c_h2 = np.sum(
                                        holes_off[:, half_36 : 2 * half_36, :], axis=(0, 1)
                                    ) / denom_h2
                                    dft_h1 = np.fft.rfft(c_h1 - np.mean(c_h1))
                                    dft_h2 = np.fft.rfft(c_h2 - np.mean(c_h2))
                                    amp_h1 = np.abs(dft_h1)
                                    amp_h2 = np.abs(dft_h2)
                                    if float(amp_h1[k_win]) > 1e-6 and float(amp_h2[k_win]) > 1e-6:
                                        phase_coh = float(
                                            np.cos(np.angle(dft_h1[k_win]) - np.angle(dft_h2[k_win]))
                                        )
                                        bg_h1 = np.delete(amp_h1, [0, 1, 2, 3, 6])
                                        bg_h2 = np.delete(amp_h2, [0, 1, 2, 3, 6])
                                        r_h1 = float(amp_h1[k_win]) / max(float(np.median(bg_h1)), 1e-3)
                                        r_h2 = float(amp_h2[k_win]) / max(float(np.median(bg_h2)), 1e-3)
                                        mad_bg = float(np.median(np.abs(bg_m - med_bg_raw)))
                                        iqr_bg = (
                                            float(np.percentile(bg_m, 75) - np.percentile(bg_m, 25))
                                            / 1.349
                                        )
                                        scale_bg = max(
                                            1.4826 * mad_bg, 0.5 * iqr_bg, 0.5 * med_bg_raw, 1e-3
                                        )
                                        z_mpc_rob = (float(dft_m[k_win]) - med_bg_raw) / scale_bg
                                        r_mpc_floored = float(dft_m[k_win]) / max(med_bg_raw, 1e-3)
                                        if (phase_coh >= 0.70
                                                and min(r_h1, r_h2) >= 3.5
                                                and r_mpc_floored >= 7.5
                                                and z_mpc_rob >= 8.0):
                                            z_mpc = max(z_mpc, min(z_mpc_rob, r_mpc_floored))

        if z576 >= 12.0:
            return {
                "mdct_lattice_detected": True,
                "mdct_lattice_snr": round(float(z576), 2),
                "mdct_lattice_family": CodecLatticeFamily.PQMF_576.value,
            }

        # 2b. MDCT-512 (ATSC A/52 AC-3 / E-AC-3, KBD alpha=5, 256-sample hop).
        # Evaluated before PQMF-32 so Dolby Digital streams are never hijacked
        # by the lower-threshold Musepack subband harmonic test.
        z512 = 0.0
        n_blocks_512 = 32
        needed_512 = n_blocks_512 * 256 + 512
        if mono.size >= needed_512:
            f_top_512 = min(
                20200.0,
                (ceiling_hz * 0.94)
                if (ceiling_hz is not None and ceiling_hz < 18000.0)
                else 20200.0,
            )
            bands_512 = (
                (max(6000.0, f_top_512 * 0.55), f_top_512),
                (max(9000.0, f_top_512 * 0.72), f_top_512),
            )
            valid_slices_512 = []
            for f_lo_5, f_hi_5 in bands_512:
                k_lo_5 = int(f_lo_5 / nyq * 256)
                k_hi_5 = int(f_hi_5 / nyq * 256)
                if k_hi_5 > k_lo_5 + 12:
                    valid_slices_512.append((k_lo_5, k_hi_5))
            if valid_slices_512:
                seg_512 = _select_continuous_subwindow(mono, needed_512)
                n_all_512 = n_blocks_512 * 256
                frames_512 = np.lib.stride_tricks.sliding_window_view(seg_512, 512)[:n_all_512]
                w_512 = _get_kbd_512_ac3()
                sp_512_by_band = [
                    np.empty(n_all_512, dtype=np.float32)
                    for _ in valid_slices_512
                ]
                for i in range(0, n_all_512, 512):
                    end_i = min(i + 512, n_all_512)
                    c_chunk_5 = np.abs(_fast_mdct_f32(frames_512[i:end_i], w_512))
                    for b_idx, (k_lo_5, k_hi_5) in enumerate(valid_slices_512):
                        coeffs_5 = c_chunk_5[:, k_lo_5:k_hi_5]
                        rms_5 = np.sqrt(
                            np.mean(coeffs_5 * coeffs_5, axis=1, keepdims=True)
                        ) + np.float32(1e-12)
                        active_5 = rms_5[:, 0] > np.float32(1e-6)
                        sp_5 = np.mean(coeffs_5 < (rms_5 * np.float32(0.05)), axis=1)
                        sp_512_by_band[b_idx][i:end_i] = np.where(
                            active_5, sp_5, np.float32(0.0)
                        )
                for sp_all_5 in sp_512_by_band:
                    scores_512 = sp_all_5.reshape(n_blocks_512, 256).mean(axis=0)
                    z_cand_5, med_5, mx_5 = _robust_z_peak(scores_512, min_scale=1e-3)
                    if med_5 < 0.65 and (mx_5 - med_5) >= 0.008:
                        z512 = max(z512, z_cand_5)
                    if z512 >= 10.0:
                        break

        if z512 >= 10.0:
            return {
                "mdct_lattice_detected": True,
                "mdct_lattice_snr": round(float(z512), 2),
                "mdct_lattice_family": CodecLatticeFamily.MDCT_512_AC3.value,
            }

        if z_mpc >= 7.5:
            return {
                "mdct_lattice_detected": True,
                "mdct_lattice_snr": round(float(z_mpc), 2),
                "mdct_lattice_family": CodecLatticeFamily.PQMF_32.value,
            }

        # 3. CELT-960 (Opus 20.00 kHz TDAC stopband boundary cancellation)
        z_opus = 0.0
        z_opus_sub = 0.0
        n_blocks_opus = 14
        need_48 = 16000
        need_src = (need_48 * sr) // 48000
        if (not skip_celt
                and (fullband_mode or (ceiling_hz is not None and ceiling_hz >= 14500.0))
                and mono.size >= need_src and sr in (44100, 48000)):
            w_celt = _get_celt_1920()
            for seg_src in _select_hf_subwindow_candidates(mono, need_src, max_windows=3):
                if sr == 44100:
                    mono_48 = _resample_celt_44k_to_48k_slice(
                        seg_src, 320, n_blocks_opus * 960 + 1920
                    )
                else:
                    mono_48 = np.asarray(seg_src[: n_blocks_opus * 960 + 1920], dtype=np.float32)
                if mono_48.size < n_blocks_opus * 960 + 1920:
                    continue
                pre = np.empty_like(mono_48)
                pre[0] = mono_48[0]
                pre[1:] = mono_48[1:] - np.float32(0.85) * mono_48[:-1]
                all_f = np.lib.stride_tricks.sliding_window_view(pre, 1920)[: n_blocks_opus * 960]
                c0 = np.abs(_fast_mdct_f32(all_f[::960], w_celt))
                ep0 = float(np.mean(c0[:, 704:796] ** 2)) + 1e-28
                es0 = float(np.mean(c0[:, 802:870] ** 2)) + 1e-28
                emid0 = float(np.mean(c0[:, 160:440] ** 2)) + 1e-28
                if 10.0 * math.log10(ep0 / es0) >= 15.0 and ep0 > emid0 * 1e-4 and ep0 > 1e-9:
                    n_all = len(all_f)
                    drops_all = np.empty(n_all, dtype=np.float32)
                    for i in range(0, n_all, 960):
                        chunk = all_f[i : i + 960]
                        c = np.abs(_fast_mdct_f32(chunk, w_celt))
                        e_pass = np.mean(c[:, 704:796] ** 2, axis=1) + np.float32(1e-28)
                        e_stop = np.mean(c[:, 802:870] ** 2, axis=1) + np.float32(1e-28)
                        drops_all[i : i + 960] = 10.0 * np.log10(e_pass / e_stop)
                    grid_d = drops_all.reshape(n_blocks_opus, 8, 120).mean(axis=0).max(axis=0)
                    z_cand_opus, med_d, mx_d = _robust_z_peak(grid_d, min_scale=0.25)
                    if (
                        (mx_d >= 38.0 and (mx_d - med_d) >= 6.0)
                        or (mx_d >= 26.0 and (mx_d - med_d) >= 2.6 and z_cand_opus >= 6.2)
                    ):
                        z_opus = max(z_opus, z_cand_opus)
                        if z_opus >= 6.2:
                            break
                    elif mx_d >= 24.0 and (mx_d - med_d) >= 2.0:
                        z_opus_sub = max(z_opus_sub, min(z_cand_opus, 6.0))

        if z_opus >= 6.2:
            return {
                "mdct_lattice_detected": True,
                "mdct_lattice_snr": round(float(z_opus), 2),
                "mdct_lattice_family": CodecLatticeFamily.CELT_960.value,
            }

        best_z = max(z1024, z576, z512, z_mpc, z_opus, z_opus_sub)
        return {
            "mdct_lattice_detected": False,
            "mdct_lattice_snr": round(float(best_z), 2) if math.isfinite(best_z) else 0.0,
            "mdct_lattice_family": None,
        }
    except Exception as exc:
        return {**neutral, "mdct_detector_error": f"{type(exc).__name__}: {exc}"}


def _detect_lapped_codec_grid(mono, sr, ceiling_hz=None, skip_celt: bool = False):
    """Phase 11/12/13.2/14.7: orthogonal lapped transform & subband inversion.

    Evaluates five standardized codec filterbanks across sample-level phase offsets:
      1. MDCT-1024 (Vorbis / AAC Sine & KBD) with 128-sample block-switching subgrids.
      2. PQMF-576 (MP3 32-band polyphase filterbank + 18-point MDCT).
      3. MDCT-512 (ATSC A/52 AC-3 / E-AC-3 KBD alpha=5, 256-sample hop).
      4. PQMF-32 (Musepack SV7/SV8 32-band pure subband 36-sample frame grid).
      5. CELT-960 (Opus 20 ms low-overlap MDCT with 1 - 0.85*z^-1 preemphasis).
    When 5.5 kHz <= ceiling_hz < 17.5 kHz, checks the adaptive passband first and
    falls back to the fullband upper-hiss probe (12.0-20.5 kHz + CELT-960) so
    high-bitrate transcodes of dark ambient/solo piano recordings cannot hide
    below the -60 dB ceiling.
    """
    if not (41000 <= sr <= 48000):
        return {
            "mdct_lattice_detected": False,
            "mdct_lattice_snr": 0.0,
            "mdct_lattice_family": None,
        }
    if ceiling_hz is not None and ceiling_hz < 5500.0:
        return {
            "mdct_lattice_detected": False,
            "mdct_lattice_snr": 0.0,
            "mdct_lattice_family": None,
        }
    eval_kw = {"skip_celt": True} if skip_celt else {}
    if ceiling_hz is None or ceiling_hz >= 17500.0:
        return _eval_lapped_codec_grid(mono, sr, ceiling_hz=ceiling_hz, **eval_kw)
    eff_ceil = max(9500.0, float(ceiling_hz))
    res_adapt = _eval_lapped_codec_grid(mono, sr, ceiling_hz=eff_ceil, **eval_kw)
    if res_adapt.get("mdct_lattice_detected"):
        return res_adapt
    res_fb = _eval_lapped_codec_grid(mono, sr, ceiling_hz=None, **eval_kw)
    if res_fb.get("mdct_lattice_detected"):
        return res_fb
    if "mdct_detector_error" in res_fb and "mdct_detector_error" not in res_adapt:
        return {**res_adapt, "mdct_detector_error": res_fb["mdct_detector_error"]}
    return res_adapt

def _select_multichannel_layout(chosen):
    """Selects active non-floor channels and primary L/R pair for C > 2 blocks."""
    n_ch = max((blk[4].shape[1] for blk in chosen if blk[4].ndim > 1), default=1)
    ch_energy = np.zeros(n_ch, dtype=np.float64)
    total_frames = 0
    for _, _, _, _, audio in chosen:
        if audio.ndim <= 1:
            ch_energy[0] += float(np.dot(audio, audio))
        else:
            cols = min(n_ch, audio.shape[1])
            if cols:
                a64 = np.asarray(audio[:, :cols], dtype=np.float64)
                ch_energy[:cols] += np.einsum("ij,ij->j", a64, a64)
        total_frames += len(audio)
    if total_frames > 0:
        ch_energy /= float(total_frames)
    max_e = float(np.max(ch_energy)) if n_ch > 0 else 0.0
    if max_e <= 1e-26:
        return [], ((0, 1) if n_ch >= 2 else (0,))

    # In 5.1 / 6.1 / 7.1 (SMPTE channel 3 = LFE), exclude sub-only LFE when
    # full-range channels hold real program energy.
    candidates = list(range(n_ch))
    if n_ch in (6, 7, 8):
        non_lfe = [idx for idx in candidates if idx != 3]
        non_lfe_max = max(float(ch_energy[idx]) for idx in non_lfe)
        if non_lfe_max > 1e-24 and non_lfe_max >= 1e-4 * max_e:
            candidates = non_lfe

    cand_max = max(float(ch_energy[idx]) for idx in candidates)
    thresh = max(1e-26, 1e-7 * cand_max)
    active = [idx for idx in candidates if float(ch_energy[idx]) >= thresh]
    if not active:
        active = [int(np.argmax(ch_energy))]

    if len(active) == 1:
        pair = (active[0],)
    elif (0 in active and 1 in active
          and min(float(ch_energy[0]), float(ch_energy[1])) >= 1e-4 * cand_max):
        pair = (0, 1)
    else:
        ranked = sorted(active, key=lambda idx: float(ch_energy[idx]), reverse=True)
        pair = (ranked[0], ranked[1])
    return active, pair


def _apply_stationary_and_ms_guards(
    spectrum, psycho, freqs, mean_mid, mean_side, gain_noise, sr
):
    """Post-STFT stage 1: fuse psychoacoustic summary, stationarity, and M/S guards."""
    spectrum.update(psycho.metrics)
    if psycho.error is not None:
        spectrum["psycho_detector_error"] = psycho.error
    if spectrum.get("es_estacionario"):
        if (spectrum.get("sospecha_joint_stereo")
                and (spectrum.get("delta_corte_ms_hz") or 0.0) < 2500.0):
            spectrum["sospecha_joint_stereo"] = False
        if ((spectrum.get("techo_rel_nyquist") or 0.0) >= 0.88
                and spectrum.get("clase_corte") == CutoffClass.FAR_CUTOFF.value
                and (spectrum.get("frecuencia_corte") or 0) < 0.85 * (spectrum.get("techo_hz") or 0)):
            spectrum.update(CutoffAssessment.none().to_protocol_fields())
    ceiling_val = spectrum.get("techo_hz", 0)
    hires_clean_rolloff = bool(
        sr >= 88200
        and spectrum.get("clase_corte") == CutoffClass.NO_CUTOFF.value
        and (spectrum.get("rolloff_analogico_genuino")
             or spectrum.get("es_transferencia_dsd"))
    )
    if hires_clean_rolloff and ceiling_val < 13500:
        cur_iqr = float(spectrum.get("iqr_corte_hz") or 0.0)
        if cur_iqr > 350.0:
            spectrum["iqr_corte_hz"] = 350.0
    ms_p95 = float(psycho.ms_p95_db)
    delta_cutoff = float(spectrum.get("delta_corte_ms_hz") or 0.0)
    collapse = float(spectrum.get("caida_side_agudos_db") or 0.0)
    centered_hf_vocal = False
    hires_ultrasonic_stereo = False
    narrow_midrange_stereo = False
    if mean_mid is not None and mean_side is not None:
        diff_ms_db = 10.0 * np.log10(
            np.maximum(mean_mid, 1e-28) / np.maximum(mean_side, 1e-28)
        )
        mask_midrange = (freqs >= 2000.0) & (freqs <= 8000.0)
        if np.any(mask_midrange):
            med_midrange = float(np.median(diff_ms_db[mask_midrange]))
            narrow_midrange_stereo = bool(
                math.isfinite(med_midrange) and med_midrange >= 14.5
            )
        if delta_cutoff < 800.0 and math.isfinite(ms_p95) and ms_p95 < 42.0:
            centered_hf_vocal = _has_centered_upper_mid_vocal(freqs, diff_ms_db)
        if hires_clean_rolloff and delta_cutoff < 500.0:
            mask_us = ((freqs >= 24000.0)
                       & (freqs <= min(36000.0, sr * 0.42)))
            if np.any(mask_us):
                mid_us_db = _spectrum_dbfs(mean_mid[mask_us], gain_noise)
                side_us_db = _spectrum_dbfs(mean_side[mask_us], gain_noise)
                hires_ultrasonic_stereo = bool(
                    float(np.median(side_us_db)) > -85.0
                    and float(np.median(mid_us_db - side_us_db)) < 12.0
                )
    if (psycho.ms_dynamic_collapse
            and not spectrum.get("es_dual_mono")
            and not spectrum.get("es_estacionario")
            and not narrow_midrange_stereo
            and not centered_hf_vocal
            and not hires_ultrasonic_stereo
            and not (hires_clean_rolloff and ceiling_val < 13500)
            and (delta_cutoff >= 800.0
                 or (collapse >= 6.0 and math.isfinite(ms_p95) and ms_p95 >= 25.0))):
        spectrum["sospecha_joint_stereo"] = True
        if math.isfinite(ms_p95) and ms_p95 > collapse:
            spectrum["caida_side_agudos_db"] = round(ms_p95, 2)
    return spectrum


def _probe_lapped_codec_lattice(chosen, mc_pair, sr, ceiling_val):
    """Post-STFT stage 2: multi-channel / multi-block lapped codec lattice probe."""
    ranked_blks = sorted(
        chosen, key=lambda item: (len(item[4]) >= 24576, item[0]), reverse=True
    )
    best_blk = ranked_blks[0][4]
    if best_blk.ndim > 1 and len(mc_pair) >= 2:
        c0, c1 = mc_pair[0], mc_pair[1]
        dl = np.diff(best_blk[:, c0])
        dr = np.diff(best_blk[:, c1])
        el = float(np.dot(dl, dl))
        er = float(np.dot(dr, dr))
        primary_idx = c0 if el >= er else c1
        opp_idx = c1 if primary_idx == c0 else c0
        mono_blk = best_blk[:, primary_idx]
        opp_blk = best_blk[:, opp_idx]
        asym_ch = abs(el - er) > 0.25 * max(el, er, 1e-12)
    else:
        ch0 = mc_pair[0] if best_blk.ndim > 1 else 0
        mono_blk = best_blk[:, ch0] if best_blk.ndim > 1 else best_blk
        opp_blk = None
        asym_ch = False

    first_error = None

    def _record_error(probe_res):
        nonlocal first_error
        if first_error is None and "mdct_detector_error" in probe_res:
            first_error = probe_res["mdct_detector_error"]

    lat = _detect_lapped_codec_grid(mono_blk, sr, ceiling_hz=ceiling_val)
    _record_error(lat)
    if (not lat.get("mdct_lattice_detected")
            and opp_blk is not None
            and (asym_ch or lat.get("mdct_lattice_snr", 0.0) >= 3.2)):
        lat_opp = _detect_lapped_codec_grid(opp_blk, sr, ceiling_hz=ceiling_val)
        _record_error(lat_opp)
        if (lat_opp.get("mdct_lattice_detected")
                or lat_opp.get("mdct_lattice_snr", 0.0) > lat.get("mdct_lattice_snr", 0.0)):
            lat = lat_opp
    if (not lat.get("mdct_lattice_detected")
            and best_blk.ndim > 1 and len(mc_pair) >= 2
            and (asym_ch or lat.get("mdct_lattice_snr", 0.0) >= 3.2)):
        c0, c1 = mc_pair[0], mc_pair[1]
        mid_blk = 0.5 * (best_blk[:, c0] + best_blk[:, c1])
        if float(np.dot(mid_blk, mid_blk)) <= 1e-6 * max(
            float(np.dot(best_blk[:, c0], best_blk[:, c0])),
            float(np.dot(best_blk[:, c1], best_blk[:, c1])),
            0.0,
        ):
            mid_blk = 0.5 * (best_blk[:, c0] - best_blk[:, c1])
        lat_mid = _detect_lapped_codec_grid(mid_blk, sr, ceiling_hz=ceiling_val)
        _record_error(lat_mid)
        if (lat_mid.get("mdct_lattice_detected")
                or lat_mid.get("mdct_lattice_snr", 0.0) > lat.get("mdct_lattice_snr", 0.0)):
            lat = lat_mid
    for sec_idx, sec_item in enumerate(ranked_blks[1:3]):
        if lat.get("mdct_lattice_detected"):
            break
        sec_b = sec_item[4]
        if len(sec_b) < 24576:
            continue
        if sec_b.ndim > 1 and len(mc_pair) >= 2:
            c0, c1 = mc_pair[0], mc_pair[1]
            dl2 = np.diff(sec_b[:, c0])
            dr2 = np.diff(sec_b[:, c1])
            el2 = float(np.dot(dl2, dl2))
            er2 = float(np.dot(dr2, dr2))
            sec_mono = sec_b[:, c0] if el2 >= er2 else sec_b[:, c1]
        else:
            ch0 = mc_pair[0] if sec_b.ndim > 1 else 0
            sec_mono = sec_b[:, ch0] if sec_b.ndim > 1 else sec_b
        lat_sec = _detect_lapped_codec_grid(sec_mono, sr, ceiling_hz=ceiling_val)
        _record_error(lat_sec)
        if lat_sec.get("mdct_lattice_detected") and (
            sec_idx == 0
            or lat_sec.get("mdct_lattice_family") == CodecLatticeFamily.CELT_960.value
        ):
            lat = lat_sec
    if (not lat.get("mdct_lattice_detected")
            and first_error is not None
            and "mdct_detector_error" not in lat):
        lat = {**lat, "mdct_detector_error": first_error}
    return lat


_HIGH_SELECTIVITY_LATTICE_FAMILIES = frozenset((
    CodecLatticeFamily.MDCT_1024.value,
    CodecLatticeFamily.PQMF_576.value,
    CodecLatticeFamily.MDCT_512_AC3.value,
    CodecLatticeFamily.CELT_960.value,
))


def _rank_hires_lattice(probe_res):
    """Tie-break key favoring high-selectivity families over cross-rate PQMF-32."""
    detected = bool(probe_res.get("mdct_lattice_detected"))
    fam = probe_res.get("mdct_lattice_family")
    snr = float(probe_res.get("mdct_lattice_snr") or 0.0)
    if detected and fam in _HIGH_SELECTIVITY_LATTICE_FAMILIES:
        tier = 2
    elif detected:
        tier = 1
    else:
        tier = 0
    return (tier, snr)


def _probe_hires_lapped_codec_lattice(chosen, mc_pair, sr, ceiling_val):
    """Post-STFT stage 2b: single-block Hi-Res (>= 88.2 kHz) decimation bridge.

    Decimates a single active block (up to 98,304 samples at 88.2/96 kHz or
    196,608 at 176.4/192 kHz) to 48 kHz and 44.1 kHz in pure NumPy to catch
    full-band lossy transcodes upsampled into Hi-Res containers even when
    ultrasonic noise masks the CD/DAT brickwall. Never short-circuits on
    PQMF-32 before checking the alternate target rate.
    """
    neutral = {
        "mdct_lattice_detected": False,
        "mdct_lattice_snr": 0.0,
        "mdct_lattice_family": None,
    }
    sr_int = int(sr)
    if sr_int % 48000 == 0:
        target_rates = (48000, 44100)
    elif sr_int % 44100 == 0:
        target_rates = (44100, 48000)
    else:
        return neutral

    min_len = 65536 * max(1, sr_int // 88200)
    ranked_blks = sorted(
        chosen, key=lambda item: (len(item[4]) >= min_len, item[0]), reverse=True
    )
    best_start = int(ranked_blks[0][3])
    best_blk = ranked_blks[0][4]
    if best_blk.ndim > 1 and len(mc_pair) >= 2:
        c0, c1 = mc_pair[0], mc_pair[1]
        dl = np.diff(best_blk[:, c0])
        dr = np.diff(best_blk[:, c1])
        el = float(np.dot(dl, dl))
        er = float(np.dot(dr, dr))
        primary_idx = c0 if el >= er else c1
        opp_idx = c1 if primary_idx == c0 else c0
        mono_blk = best_blk[:, primary_idx]
        opp_blk = best_blk[:, opp_idx]
        asym_ch = min(el, er) < 0.25 * max(el, er, 1e-12)
    else:
        ch0 = mc_pair[0] if best_blk.ndim > 1 else 0
        mono_blk = best_blk[:, ch0] if best_blk.ndim > 1 else best_blk
        opp_blk = None
        asym_ch = False

    eff_ceil = None if (ceiling_val is None or ceiling_val >= 16500) else ceiling_val
    first_error = None

    def _record_error(probe_res):
        nonlocal first_error
        if first_error is None and "mdct_detector_error" in probe_res:
            first_error = probe_res["mdct_detector_error"]

    def _probe_channel(ch_blk):
        best_res = neutral
        for tgt_sr in target_rates:
            sig_dec = _decimate_hires_to_target(
                ch_blk, sr_int, tgt_sr, block_start=best_start
            )
            if sig_dec.size < 24576:
                continue
            cand = _detect_lapped_codec_grid(
                sig_dec,
                tgt_sr,
                ceiling_hz=eff_ceil,
                skip_celt=(tgt_sr == 44100 and 48000 in target_rates),
            )
            _record_error(cand)
            if _rank_hires_lattice(cand) > _rank_hires_lattice(best_res):
                best_res = cand
            if (cand.get("mdct_lattice_detected")
                    and cand.get("mdct_lattice_family") in _HIGH_SELECTIVITY_LATTICE_FAMILIES):
                return cand
        return best_res

    lat = _probe_channel(mono_blk)
    if (not (lat.get("mdct_lattice_detected")
             and lat.get("mdct_lattice_family") in _HIGH_SELECTIVITY_LATTICE_FAMILIES)
            and opp_blk is not None
            and (asym_ch or float(lat.get("mdct_lattice_snr") or 0.0) >= 6.5)):
        lat_opp = _probe_channel(opp_blk)
        if _rank_hires_lattice(lat_opp) > _rank_hires_lattice(lat):
            lat = lat_opp

    if (not lat.get("mdct_lattice_detected")
            and first_error is not None
            and "mdct_detector_error" not in lat):
        lat = {**lat, "mdct_detector_error": first_error}
    return lat


def _probe_sbr_qmf_from_chosen(chosen, mc_pair, sr, ceiling_val):
    """Post-STFT stage 3: seamless HE-AAC SBR QMF replication probe."""
    if sr < 32000 or ceiling_val < 14500:
        return {}
    target_samples = int(sr * 6)
    mid_parts = []
    gathered = 0
    for item in sorted(chosen, key=lambda it: it[0], reverse=True):
        blk = item[4]
        if len(blk) == 0:
            continue
        if blk.ndim > 1 and len(mc_pair) >= 2:
            c0, c1 = mc_pair[0], mc_pair[1]
            mid_p = 0.5 * (blk[:, c0] + blk[:, c1])
            if float(np.dot(mid_p, mid_p)) <= 1e-6 * max(
                float(np.dot(blk[:, c0], blk[:, c0])),
                float(np.dot(blk[:, c1], blk[:, c1])),
                0.0,
            ):
                mid_p = 0.5 * (blk[:, c0] - blk[:, c1])
        else:
            ch0 = mc_pair[0] if blk.ndim > 1 else 0
            mid_p = blk[:, ch0] if blk.ndim > 1 else blk
        mid_parts.append(np.asarray(mid_p, dtype=np.float32))
        gathered += len(mid_p)
        if gathered >= target_samples:
            break
    if not mid_parts:
        return {}
    return _detect_sbr_qmf_replication(mid_parts, sr)


def _analyze_quiet_24bit_floor(quiet_audio, samplerate, is_stationary=False):
    """Audits one quiet non-silent block (RMS in [-92, -40] dBFS) of a >16-bit
    <=48 kHz container using the per-bin 10th percentile across STFT frames.

    Only clearly rising noise-shaped 16-bit dither (slope >= +3.5 dB from
    8-12 kHz to 19-21.5 kHz, spectral flatness >= 0.90, calibrated floor
    >= -96.0 dBFS) triggers `noise_shaped_16_in_24`; flat or rolling-off
    analogue tape/preamp floors are never flagged.
    """
    neutral = {
        "noise_shaped_16_in_24": False,
        "quiet_hf_slope_db": 0.0,
        "quiet_hf_flatness": 0.0,
        "quiet_hf_floor_dbfs": None,
    }
    if quiet_audio is None or not (41000 <= int(samplerate) <= 48000):
        return neutral
    ch0 = (
        np.ascontiguousarray(quiet_audio[:, 0], dtype=np.float32)
        if quiet_audio.ndim > 1
        else np.ascontiguousarray(quiet_audio, dtype=np.float32)
    )
    nperseg = 4096
    hop = 512
    if ch0.size < nperseg + hop * 7:
        return neutral
    window = np.hanning(nperseg + 1)[:-1].astype(np.float32)
    scale = np.float32(1.0 / float(window.sum()))
    gain = _white_noise_gain(nperseg, window)
    if gain <= 0.0:
        return neutral
    freqs = np.fft.rfftfreq(nperseg, 1.0 / float(samplerate))
    z_q = _stft_complex(ch0, nperseg, hop, window, scale)
    if z_q.shape[0] < 8:
        return neutral
    p_q = _power_from_fft(z_q).astype(np.float64) / gain
    # Per-bin 10th percentile removes transient fade-ins; dividing by the exact
    # Exp(1) P10 quantile (-ln(0.9)) recovers the calibrated white-noise dBFS.
    p10_cal = np.percentile(p_q, 10, axis=0) / (-math.log(0.9))
    sl_mid, ok_mid = _freq_slice(freqs, 8000.0, 12000.0, True)
    sl_hi, ok_hi = _freq_slice(
        freqs, 19000.0, min(21500.0, float(samplerate) * 0.488), True
    )
    if not (ok_mid and ok_hi):
        return neutral
    mid_vals = np.maximum(p10_cal[sl_mid], 1e-28)
    hi_vals = np.maximum(p10_cal[sl_hi], 1e-28)
    mid_db = float(10.0 * math.log10(max(float(np.median(mid_vals)), 1e-28)))
    hi_db = float(10.0 * math.log10(max(float(np.median(hi_vals)), 1e-28)))
    slope_db = hi_db - mid_db
    flatness = float(math.exp(float(np.mean(np.log(hi_vals)))) / float(np.mean(hi_vals)))
    if not (math.isfinite(slope_db) and math.isfinite(flatness) and math.isfinite(hi_db)):
        return neutral
    detected = bool(
        not is_stationary
        and slope_db >= 3.5
        and flatness >= 0.90
        and hi_db >= -96.0
    )
    return {
        "noise_shaped_16_in_24": detected,
        "quiet_hf_slope_db": round(slope_db, 2),
        "quiet_hf_flatness": round(flatness, 4),
        "quiet_hf_floor_dbfs": round(hi_db, 2),
    }


def _analyze_stereo_phase_coupling(
    freqs,
    sum_l64,
    sum_r64,
    sum_cross_re,
    sum_cross_im,
    samplerate,
    is_dual_mono=False,
    is_stationary=False,
):
    """Inter-channel MSC and IPD phase-lock detector for Intensity Stereo /
    Channel Coupling (AC-3, E-AC-3, low-bitrate Opus/MP3/AAC).

    Scans 6.0-16.5 kHz for a discontinuous upward coherence cliff (1 kHz
    windows separated by 1.5 kHz) where genuinely decorrelated stereo
    (`pre_msc < 0.35`, `msc_lo < 0.35`) snaps into phase-locked coupling
    (`post_msc > 0.85`, `step > 0.60`, `median(|sin(IPD)|) < 0.08`). Real
    mixes with wide guitars and centered hi-hats/overheads transition smoothly
    across 4-8 kHz without a razor-sharp high-band step cliff.
    """
    neutral = {
        "msc_lo": 0.0,
        "msc_hi": 0.0,
        "msc_step_hf": 0.0,
        "ipd_phase_lock_hf": 0.0,
        "stereo_phase_coupling": False,
    }
    if (sum_l64 is None or sum_r64 is None
            or sum_cross_re is None or sum_cross_im is None
            or int(samplerate) < 32000 or freqs.size < 32):
        return neutral
    df = float(freqs[1] - freqs[0])
    if df <= 0.0:
        return neutral
    sl_lo, ok_lo = _freq_slice(freqs, 2000.0, 6000.0, True)
    sl_hi_default, ok_hi_def = _freq_slice(
        freqs, 10000.0, min(16000.0, float(samplerate) * 0.42), True
    )
    if not (ok_lo and ok_hi_def):
        return neutral
    denom = np.maximum(sum_l64 * sum_r64, 1e-30)
    msc = np.clip((sum_cross_re * sum_cross_re + sum_cross_im * sum_cross_im) / denom, 0.0, 1.0)
    msc_lo_2_6 = float(np.median(msc[sl_lo]))

    # Sliding 1 kHz coherence-cliff scan across [6.0 kHz .. min(16.5 kHz, 0.43*sr - 1.5 kHz)].
    w_bins = max(4, int(round(1000.0 / df)))
    gap_bins = max(w_bins, int(round(1500.0 / df)))
    f_scan_hi = min(18500.0, float(samplerate) * 0.43)
    k_lo = int(round(6000.0 / df))
    k_hi = min(msc.size - w_bins, int(round(f_scan_hi / df)) - w_bins)
    if k_hi <= k_lo + gap_bins:
        return neutral

    cs = np.empty(msc.size + 1, dtype=np.float64)
    cs[0] = 0.0
    np.cumsum(msc, dtype=np.float64, out=cs[1:])
    avg_1k = (cs[w_bins:] - cs[:-w_bins]) / float(w_bins)
    pre_arr = avg_1k[k_lo : k_hi - gap_bins]
    post_arr = avg_1k[k_lo + gap_bins : k_hi]
    steps = post_arr - pre_arr
    if steps.size == 0:
        return neutral

    best_idx = int(np.argmax(steps))
    best_step = float(steps[best_idx])
    pre_msc = float(pre_arr[best_idx])
    post_msc = float(post_arr[best_idx])
    k_post_start = k_lo + best_idx + gap_bins
    k_post_end = min(msc.size, k_post_start + max(w_bins, int(round(2000.0 / df))))
    if k_post_end <= k_post_start + 2:
        return neutral

    ipd_post = np.arctan2(
        sum_cross_im[k_post_start:k_post_end],
        sum_cross_re[k_post_start:k_post_end],
    )
    sine_ipd_med = float(np.median(np.abs(np.sin(ipd_post))))
    phase_lock = max(0.0, min(1.0, 1.0 - sine_ipd_med))
    if not (math.isfinite(msc_lo_2_6) and math.isfinite(post_msc)
            and math.isfinite(best_step) and math.isfinite(sine_ipd_med)):
        return neutral

    detected = bool(
        not is_dual_mono
        and not is_stationary
        and msc_lo_2_6 < 0.35
        and pre_msc < 0.35
        and post_msc > 0.85
        and best_step > 0.60
        and sine_ipd_med < 0.08
    )
    return {
        "msc_lo": round(pre_msc, 4),
        "msc_hi": round(post_msc, 4),
        "msc_step_hf": round(best_step, 4),
        "ipd_phase_lock_hf": round(phase_lock, 4),
        "stereo_phase_coupling": detected,
    }


class _SpectralAccumulator:
    """Keeps the K most active blocks in memory and runs the STFT on them at the
    end. Shared by the streaming path and the array path so both give the same
    metrics. Blocks are never concatenated: a frame must not cross the splice
    between two non-adjacent blocks, which would invent high-frequency content."""

    def __init__(self, samplerate, is_stereo=None, save_png=False, path=None,
                 renderer=None, mode="center", seconds=None, bits_per_sample=None):
        self.sr = int(samplerate)
        self.is_stereo = is_stereo
        self.save_png = bool(save_png)
        self.path = path
        self.mode = SpectralMode.coerce(mode, default=SpectralMode.CENTER).value
        self.seconds = seconds
        self.bits_per_sample = int(bits_per_sample) if bits_per_sample is not None else None
        self._track_quiet_24 = bool(
            self.bits_per_sample is not None
            and self.bits_per_sample > 16
            and 41000 <= self.sr <= 48000
        )
        self._quiet_block = None    # (rms_db, audio_f32) for 1 quiet non-silent block
        self._max_sample = (
            int(round(max(1.0, float(seconds if seconds is not None else 15.0)) * self.sr))
            if self.mode == SpectralMode.SECONDS.value else None
        )
        self._renderer = renderer or _save_spectrogram
        self._k = _needed_active_blocks(self.sr, mode=self.mode, seconds=self.seconds)
        self._seleccion = []          # min-heap by (E_diff, -index)
        self._backup = None         # loudest block, used if none passes -65 dBFS
        self._index = 0
        self._last = None
        self._error = None
        self._state = AccumulatorState.COLLECTING
        self._finalized = False
        self._cached_result = None
        self._cached_png_result = None
        self._psycho = PsychoacousticSTFTAccumulator(self.sr)
        self._diff_buf = np.empty(16384, dtype=np.float64)

    @property
    def state(self) -> AccumulatorState:
        """Current streaming lifecycle phase."""
        return self._state

    def __repr__(self) -> str:
        return (
            f"_SpectralAccumulator(sr={self.sr}, mode={self.mode!r}, "
            f"state={self._state.value!r}, blocks={self._index}, "
            f"selected={len(self._seleccion)})"
        )

    def add(self, block, start=0, mono=None, int_scale=None):
        if self._finalized:
            raise RuntimeError("cannot add audio blocks after completion")
        if self._error or block.shape[0] == 0:
            return
        try:
            if self._max_sample is not None:
                if start >= self._max_sample:
                    return
                if start + block.shape[0] > self._max_sample:
                    n_keep = self._max_sample - int(start)
                    block = block[:n_keep]
                    if mono is not None:
                        mono = mono[:n_keep]
                    if block.shape[0] == 0:
                        return
            rms_db, e_diff = _activity_score(
                block, self._last, mono=mono, diff_buf=self._diff_buf,
                int_scale=int_scale,
            )
            if block.ndim > 1:
                if mono is not None:
                    self._last = float(mono[-1])
                else:
                    scale_f = float(int_scale) if int_scale is not None else 1.0
                    self._last = float(np.mean(block[-1])) * scale_f
                if self.is_stereo is None:
                    self.is_stereo = block.shape[1] >= 2
            else:
                if mono is not None:
                    self._last = float(mono[-1])
                else:
                    scale_f = float(int_scale) if int_scale is not None else 1.0
                    self._last = float(block[-1]) * scale_f
                self.is_stereo = False

            index = self._index
            self._index += 1
            needs_backup = self._backup is None or rms_db > self._backup[0]
            above_gate = rms_db > ACTIVE_RMS_THRESHOLD_DB
            needs_heap = above_gate and (
                len(self._seleccion) < self._k
                or (e_diff, -index) > self._seleccion[0][:2]
            )
            needs_quiet = (
                self._track_quiet_24
                and block.shape[0] >= 8192
                and -92.0 <= rms_db <= -40.0
                and (self._quiet_block is None or rms_db < self._quiet_block[0])
            )
            if not needs_backup and not needs_heap and not needs_quiet:
                return

            if int_scale is not None:
                audio = block.astype(np.float32)
                audio *= np.float32(int_scale)
            else:
                audio = np.asarray(block, dtype=np.float32)
            if needs_backup:
                self._backup = (rms_db, -index, index, int(start), audio)
            if needs_quiet:
                self._quiet_block = (rms_db, audio)
            if needs_heap:
                entry = (e_diff, -index, index, int(start), audio)
                if len(self._seleccion) < self._k:
                    heapq.heappush(self._seleccion, entry)
                else:
                    heapq.heapreplace(self._seleccion, entry)
        except Exception as e:            # never abort the analysis of the file
            self._error = str(e)
            self._state = AccumulatorState.FAILED

    def _invoke_renderer(self, freqs, times, mix, ceiling_hz, cutoff_hz):
        """Forwards mode/seconds to the PNG renderer when accepted."""
        try:
            sig = inspect.signature(self._renderer)
            accepts_mode = (
                "mode" in sig.parameters
                or any(
                    p.kind is inspect.Parameter.VAR_KEYWORD
                    for p in sig.parameters.values()
                )
            )
        except (TypeError, ValueError):
            accepts_mode = False
        if accepts_mode:
            return self._renderer(
                freqs, times, mix, self.sr, self.path, ceiling_hz, cutoff_hz,
                mode=self.mode, seconds=self.seconds,
            )
        return self._renderer(
            freqs, times, mix, self.sr, self.path, ceiling_hz, cutoff_hz
        )

    def _finish_error(self, message):
        err_out = {"error": message}
        self._state = AccumulatorState.FAILED
        self._finalized = True
        self._cached_result = dict(err_out)
        return err_out

    def _finish_png_only(self, payload, state):
        self._state = state
        self._finalized = True
        self._cached_png_result = dict(payload)
        return payload

    def result(self):
        if self._cached_png_result is not None:
            raise RuntimeError(
                "cannot call result() after render_png_only() finalized this accumulator"
            )
        if self._cached_result is not None:
            return dict(self._cached_result)
        if self._error:
            return self._finish_error(self._error)
        chosen = self._seleccion or ([self._backup] if self._backup else [])
        if not chosen:
            return self._finish_error("could not analyze the spectrum")
        chosen = sorted(chosen, key=lambda item: item[2])   # chronological

        is_multichannel = any(
            item[4].ndim > 1 and item[4].shape[1] > 2 for item in chosen
        )
        if is_multichannel:
            mc_active, mc_pair = _select_multichannel_layout(chosen)
            if not mc_active:
                return self._finish_error("insufficient signal energy for spectral analysis")
            effective_stereo = len(mc_active) >= 2
        else:
            mc_active = ()
            mc_pair = (0, 1) if self.is_stereo else (0,)
            effective_stereo = bool(self.is_stereo)

        lengths = [len(item[4]) for item in chosen]
        total_samples = sum(lengths)
        nperseg = max(256, min(4096, total_samples // 8))
        nperseg = min(nperseg, max(lengths))
        nperseg = max(32, nperseg)
        hop = max(1, nperseg // 8)      # 87.5 % overlap: enough time detail for the PNG

        window = np.hanning(nperseg + 1)[:-1].astype(np.float32)   # periodic Hann
        scale = np.float32(1.0 / float(window.sum()))
        scaled_window = (window * scale).astype(np.float32)
        gain_noise = _white_noise_gain(nperseg, window)
        frames_estimados = sum((n - nperseg) // hop + 1
                               for n in lengths if n >= nperseg)
        if frames_estimados <= 0:
            return self._finish_error("could not analyze the spectrum")
        step_png = max(1, frames_estimados // MAX_FRAMES_PNG)
        # Display-only cap: a 75-87 % overlap gives far more frames than the PNG
        # can show, and that per-column detail is random noise that JPEG cannot
        # compress (it used to bloat the PDF). One displayed frame per 1/25 s
        # keeps the image faithful and the PDF small; metrics still average all
        # frames, so no evidence is lost.
        step_png = max(step_png,
                       int(round(self.sr / (hop * float(PNG_SPECTROGRAM_FPS)))))

        n_bins = nperseg // 2 + 1
        stft_tile = 64
        sum_l = np.zeros(n_bins, dtype=np.float32)
        sum_r = np.zeros(n_bins, dtype=np.float32) if effective_stereo else None
        sum_total = np.zeros(n_bins, dtype=np.float32) if effective_stereo else None
        sum_mid = np.zeros(n_bins, dtype=np.float32) if effective_stereo else None
        sum_side = np.zeros(n_bins, dtype=np.float32) if effective_stereo else None
        sum_l64 = np.zeros(n_bins, dtype=np.float64) if effective_stereo else None
        sum_r64 = np.zeros(n_bins, dtype=np.float64) if effective_stereo else None
        sum_cross_re = np.zeros(n_bins, dtype=np.float64) if effective_stereo else None
        sum_cross_im = np.zeros(n_bins, dtype=np.float64) if effective_stereo else None
        freqs = np.fft.rfftfreq(nperseg, 1.0 / self.sr)
        frames = 0
        frames_global = 0
        png_power, png_times, png_n = [], [], 0
        medium = np.float32(0.5)
        zero_f32 = np.float32(0.0)
        pfft = _get_pocketfft()

        if is_multichannel:
            n_act = len(mc_active)
            tile_in = np.empty((n_act, stft_tile, nperseg), dtype=np.float32)
            tile_z = np.empty((n_act, stft_tile, n_bins), dtype=np.complex64)
            mc_acc = np.empty((stft_tile, n_bins), dtype=np.float32)
            mc_pos = {ch: pos for pos, ch in enumerate(mc_active)}
            inv_act = np.float32(1.0 / float(n_act))
        elif effective_stereo:
            tile_in = np.empty((2, stft_tile, nperseg), dtype=np.float32)
            tile_z = np.empty((2, stft_tile, n_bins), dtype=np.complex64)
        else:
            tile_in = np.empty((stft_tile, nperseg), dtype=np.float32)
            tile_z = np.empty((stft_tile, n_bins), dtype=np.complex64)
        tile_sq = np.empty((stft_tile, n_bins * 2), dtype=np.float32)
        tile_a = np.empty((stft_tile, n_bins), dtype=np.float32)
        tile_b = np.empty((stft_tile, n_bins), dtype=np.float32)
        tile_c = np.empty((stft_tile, n_bins), dtype=np.float32)

        for _, _, _, start, audio in chosen:
            if len(audio) < nperseg:
                continue                  # a very short trailing block
            n_frames_blk = (len(audio) - nperseg) // hop + 1
            if is_multichannel:
                n_act = len(mc_active)
                frames_ch = [
                    np.lib.stride_tricks.sliding_window_view(
                        np.ascontiguousarray(audio[:, ch], dtype=np.float32), nperseg
                    )[::hop]
                    for ch in mc_active
                ]
                for f0 in range(0, n_frames_blk, stft_tile):
                    f1 = min(f0 + stft_tile, n_frames_blk)
                    m = f1 - f0
                    tin = tile_in[:, :m, :]
                    tz = tile_z[:, :m, :]
                    tsq = tile_sq[:m, :]
                    raw_l = tile_a[:m, :]
                    raw_r = tile_b[:m, :]
                    tmp_f = tile_c[:m, :]
                    p_total_t = mc_acc[:m, :]
                    p_total_t.fill(zero_f32)

                    for pos in range(n_act):
                        np.multiply(frames_ch[pos][f0:f1], scaled_window, out=tin[pos])
                    pfft.r2c(tin, (2,), True, 0, tz, 1)

                    for pos in range(n_act):
                        zc = tz[pos]
                        np.square(zc.view(np.float32), out=tsq)
                        np.add(tsq[:, 0::2], tsq[:, 1::2], out=tmp_f)
                        np.maximum(tmp_f, _FLOOR_F32, out=tmp_f)
                        p_total_t += tmp_f
                    p_total_t *= inv_act

                    if effective_stereo:
                        p0 = mc_pos[mc_pair[0]]
                        p1 = mc_pos[mc_pair[1]]
                        zl_t, zr_t = tz[p0], tz[p1]
                        np.square(zl_t.view(np.float32), out=tsq)
                        np.add(tsq[:, 0::2], tsq[:, 1::2], out=raw_l)
                        np.maximum(raw_l, _FLOOR_F32, out=tmp_f)
                        sum_l += tmp_f.sum(axis=0, dtype=np.float32)

                        np.square(zr_t.view(np.float32), out=tsq)
                        np.add(tsq[:, 0::2], tsq[:, 1::2], out=raw_r)
                        np.maximum(raw_r, _FLOOR_F32, out=tmp_f)
                        sum_r += tmp_f.sum(axis=0, dtype=np.float32)

                        sum_l64 += raw_l.sum(axis=0, dtype=np.float32)
                        sum_r64 += raw_r.sum(axis=0, dtype=np.float32)
                        np.multiply(zl_t.real, zr_t.real, out=tmp_f)
                        sum_cross_re += tmp_f.sum(axis=0, dtype=np.float32)
                        np.multiply(zl_t.imag, zr_t.imag, out=tmp_f)
                        sum_cross_re += tmp_f.sum(axis=0, dtype=np.float32)
                        np.multiply(zl_t.imag, zr_t.real, out=tmp_f)
                        sum_cross_im += tmp_f.sum(axis=0, dtype=np.float32)
                        np.multiply(zl_t.real, zr_t.imag, out=tmp_f)
                        sum_cross_im -= tmp_f.sum(axis=0, dtype=np.float32)

                        np.add(raw_l, raw_r, out=raw_l)
                        raw_l *= medium
                        np.subtract(zl_t, zr_t, out=zl_t)
                        zl_t *= medium
                        np.square(zl_t.view(np.float32), out=tsq)
                        np.add(tsq[:, 0::2], tsq[:, 1::2], out=raw_r)

                        np.subtract(raw_l, raw_r, out=raw_l)
                        np.maximum(raw_l, zero_f32, out=raw_l)
                        np.maximum(raw_l, _FLOOR_F32, out=raw_l)
                        sum_mid += raw_l.sum(axis=0, dtype=np.float32)

                        np.maximum(raw_r, _FLOOR_F32, out=raw_r)
                        sum_side += raw_r.sum(axis=0, dtype=np.float32)

                        sum_total += p_total_t.sum(axis=0, dtype=np.float32)
                        self._psycho.add(p_total_t, freqs, window, power_side=raw_r)
                    else:
                        sum_l += p_total_t.sum(axis=0, dtype=np.float32)
                        self._psycho.add(p_total_t, freqs, window, power_side=None)

                    if self.save_png:
                        remaining = MAX_FRAMES_PNG - png_n
                        if remaining > 0:
                            i0 = (-(frames_global + f0)) % step_png
                            idx = np.arange(i0, m, step_png)[:remaining]
                            if idx.size:
                                png_power.append(p_total_t[idx].copy())
                                png_times.append((start + (f0 + idx) * hop) / float(self.sr))
                                png_n += idx.size
            elif effective_stereo and audio.ndim > 1 and audio.shape[1] >= 2:
                xl = np.ascontiguousarray(audio[:, 0], dtype=np.float32)
                xr = np.ascontiguousarray(audio[:, 1], dtype=np.float32)
                frames_l = np.lib.stride_tricks.sliding_window_view(xl, nperseg)[::hop]
                frames_r = np.lib.stride_tricks.sliding_window_view(xr, nperseg)[::hop]
                for f0 in range(0, n_frames_blk, stft_tile):
                    f1 = min(f0 + stft_tile, n_frames_blk)
                    m = f1 - f0
                    tin = tile_in[:, :m, :]
                    tz = tile_z[:, :m, :]
                    tsq = tile_sq[:m, :]
                    raw_l = tile_a[:m, :]
                    raw_r = tile_b[:m, :]
                    tmp_f = tile_c[:m, :]

                    np.multiply(frames_l[f0:f1], scaled_window, out=tin[0])
                    np.multiply(frames_r[f0:f1], scaled_window, out=tin[1])
                    pfft.r2c(tin, (2,), True, 0, tz, 1)
                    zl_t, zr_t = tz[0], tz[1]

                    np.square(zl_t.view(np.float32), out=tsq)
                    np.add(tsq[:, 0::2], tsq[:, 1::2], out=raw_l)
                    np.maximum(raw_l, _FLOOR_F32, out=tmp_f)
                    sum_l += tmp_f.sum(axis=0, dtype=np.float32)

                    np.square(zr_t.view(np.float32), out=tsq)
                    np.add(tsq[:, 0::2], tsq[:, 1::2], out=raw_r)
                    np.maximum(raw_r, _FLOOR_F32, out=tmp_f)
                    sum_r += tmp_f.sum(axis=0, dtype=np.float32)

                    sum_l64 += raw_l.sum(axis=0, dtype=np.float32)
                    sum_r64 += raw_r.sum(axis=0, dtype=np.float32)
                    np.multiply(zl_t.real, zr_t.real, out=tmp_f)
                    sum_cross_re += tmp_f.sum(axis=0, dtype=np.float32)
                    np.multiply(zl_t.imag, zr_t.imag, out=tmp_f)
                    sum_cross_re += tmp_f.sum(axis=0, dtype=np.float32)
                    np.multiply(zl_t.imag, zr_t.real, out=tmp_f)
                    sum_cross_im += tmp_f.sum(axis=0, dtype=np.float32)
                    np.multiply(zl_t.real, zr_t.imag, out=tmp_f)
                    sum_cross_im -= tmp_f.sum(axis=0, dtype=np.float32)

                    # Exact Parseval Mid/Side identity: |ZM|^2 + |ZS|^2 = 0.5*(|ZL|^2 + |ZR|^2).
                    np.add(raw_l, raw_r, out=raw_l)
                    raw_l *= medium
                    np.subtract(zl_t, zr_t, out=zl_t)
                    zl_t *= medium
                    np.square(zl_t.view(np.float32), out=tsq)
                    np.add(tsq[:, 0::2], tsq[:, 1::2], out=raw_r)

                    np.subtract(raw_l, raw_r, out=raw_l)
                    np.maximum(raw_l, zero_f32, out=raw_l)
                    np.maximum(raw_l, _FLOOR_F32, out=raw_l)
                    sum_mid += raw_l.sum(axis=0, dtype=np.float32)

                    np.maximum(raw_r, _FLOOR_F32, out=raw_r)
                    sum_side += raw_r.sum(axis=0, dtype=np.float32)

                    np.add(raw_l, raw_r, out=raw_l)
                    power_total_t = raw_l
                    power_side_t = raw_r
                    sum_total += power_total_t.sum(axis=0, dtype=np.float32)

                    self._psycho.add(power_total_t, freqs, window,
                                     power_side=power_side_t)
                    if self.save_png:
                        remaining = MAX_FRAMES_PNG - png_n
                        if remaining > 0:
                            i0 = (-(frames_global + f0)) % step_png
                            idx = np.arange(i0, m, step_png)[:remaining]
                            if idx.size:
                                png_power.append(power_total_t[idx].copy())
                                png_times.append((start + (f0 + idx) * hop) / float(self.sr))
                                png_n += idx.size
            else:
                ch0 = np.ascontiguousarray(
                    audio[:, 0] if audio.ndim > 1 else audio, dtype=np.float32
                )
                frames_0 = np.lib.stride_tricks.sliding_window_view(ch0, nperseg)[::hop]
                for f0 in range(0, n_frames_blk, stft_tile):
                    f1 = min(f0 + stft_tile, n_frames_blk)
                    m = f1 - f0
                    tin0 = tile_in[:m, :] if tile_in.ndim == 2 else tile_in[0, :m, :]
                    tz0 = tile_z[:m, :] if tile_z.ndim == 2 else tile_z[0, :m, :]
                    tsq = tile_sq[:m, :]
                    power_l_t = tile_a[:m, :]

                    np.multiply(frames_0[f0:f1], scaled_window, out=tin0)
                    pfft.r2c(tin0, (1,), True, 0, tz0, 1)
                    np.square(tz0.view(np.float32), out=tsq)
                    np.add(tsq[:, 0::2], tsq[:, 1::2], out=power_l_t)
                    np.maximum(power_l_t, _FLOOR_F32, out=power_l_t)
                    sum_l += power_l_t.sum(axis=0, dtype=np.float32)

                    self._psycho.add(power_l_t, freqs, window, power_side=None)
                    if self.save_png:
                        remaining = MAX_FRAMES_PNG - png_n
                        if remaining > 0:
                            i0 = (-(frames_global + f0)) % step_png
                            idx = np.arange(i0, m, step_png)[:remaining]
                            if idx.size:
                                png_power.append(power_l_t[idx].copy())
                                png_times.append((start + (f0 + idx) * hop) / float(self.sr))
                                png_n += idx.size

            frames_global += n_frames_blk
            frames += n_frames_blk

        if frames == 0:
            return self._finish_error("could not analyze the spectrum")
        if not effective_stereo:
            sum_r = sum_l
            sum_total = sum_l

        mean_l = sum_l / np.float32(frames)
        mean_r = mean_l if not effective_stereo else sum_r / np.float32(frames)
        mean_total = sum_total / np.float32(frames)
        mean_mid = (sum_mid / np.float32(frames)) if effective_stereo else None
        mean_side = (sum_side / np.float32(frames)) if effective_stereo else None
        spectrum = _spectral_metrics(freqs, mean_l, mean_r, self.sr,
                                    is_stereo=effective_stereo,
                                    power=mean_total,
                                    power_mid=mean_mid, power_side=mean_side,
                                    gain_noise=gain_noise)
        if spectrum.get("error"):
            self._state = AccumulatorState.FAILED
            self._finalized = True
            self._cached_result = dict(spectrum)
            return spectrum
        psycho = self._psycho.summary()
        spectrum.update(psycho.metrics)
        if psycho.error is not None:
            spectrum["psycho_detector_error"] = psycho.error
        _apply_stationary_and_ms_guards(
            spectrum, psycho, freqs, mean_mid, mean_side, gain_noise, self.sr
        )
        if effective_stereo:
            spectrum.update(
                _analyze_stereo_phase_coupling(
                    freqs,
                    sum_l64,
                    sum_r64,
                    sum_cross_re,
                    sum_cross_im,
                    self.sr,
                    is_dual_mono=bool(spectrum.get("es_dual_mono")),
                    is_stationary=bool(spectrum.get("es_estacionario")),
                )
            )
        ceiling_val = spectrum.get("techo_hz", 0)
        if 41000 <= self.sr <= 48000 and ceiling_val >= 5500:
            spectrum.update(
                _probe_lapped_codec_lattice(chosen, mc_pair, self.sr, ceiling_val)
            )
        elif self.sr >= 88200 and ceiling_val >= 5500:
            spectrum.update(
                _probe_hires_lapped_codec_lattice(chosen, mc_pair, self.sr, ceiling_val)
            )
        if (ceiling_val < 12000
                and spectrum.get("decaimiento_acustico_suave")
                and float(spectrum.get("brecha_p90_p10_agudos_db") or 0.0) < 42.0
                and not spectrum.get("mdct_lattice_detected")):
            spectrum["islas_mdct_sfb21"] = False
        low_centroid_smooth_iqr = bool(
            self.sr <= 48000
            and not spectrum.get("corte_detectado")
            and not spectrum.get("islas_mdct_sfb21")
            and not spectrum.get("sospecha_corte_opus_20k")
            and not spectrum.get("sospecha_joint_stereo")
            and (spectrum.get("ratio_huecos_mdct") or 0.0) <= 0.01
            and not spectrum.get("mdct_lattice_detected")
            and (
                (
                    ceiling_val >= 14000
                    and 0.0 < float(spectrum.get("centroide_hz") or 0.0) < 750.0
                    and float(spectrum.get("iqr_corte_hz") or 0.0) <= 680.0
                )
                or (
                    spectrum.get("decaimiento_acustico_suave")
                    and 0.0 < float(spectrum.get("centroide_hz") or 0.0) < 950.0
                    and float(spectrum.get("iqr_corte_hz") or 0.0) <= 900.0
                )
            )
        )
        if low_centroid_smooth_iqr:
            cur_iqr = float(spectrum.get("iqr_corte_hz") or 0.0)
            if cur_iqr > 350.0:
                spectrum["iqr_corte_hz"] = 350.0
        if (self.sr >= 32000
                and ceiling_val >= 14500
                and spectrum.get("clase_corte") != CutoffClass.FAR_CUTOFF.value
                and not spectrum.get("mdct_lattice_detected")):
            spectrum.update(
                _probe_sbr_qmf_from_chosen(chosen, mc_pair, self.sr, ceiling_val)
            )
        if self._track_quiet_24 and self._quiet_block is not None:
            spectrum.update(
                _analyze_quiet_24bit_floor(
                    self._quiet_block[1],
                    self.sr,
                    is_stationary=bool(spectrum.get("es_estacionario")),
                )
            )
        if self.save_png and png_power:
            png_path, error = self._invoke_renderer(
                freqs, np.concatenate(png_times),
                np.concatenate(png_power, axis=0).T,
                spectrum["techo_hz"], spectrum["frecuencia_corte"])
            spectrum["espectrograma"] = png_path
            if error:
                spectrum["espectrograma_error"] = error
        self._state = AccumulatorState.COMPLETED
        self._finalized = True
        self._cached_result = dict(spectrum)
        return spectrum

    def render_png_only(self, ceiling_hz=None, cutoff_hz=None):
        """Lightweight spectrogram render from selected active blocks when cached
        spectral metrics are already valid and only the PNG is missing."""
        if self._cached_result is not None:
            raise RuntimeError(
                "cannot call render_png_only() after result() finalized this accumulator"
            )
        if self._cached_png_result is not None:
            return dict(self._cached_png_result)
        if self._error:
            return self._finish_png_only(
                {"espectrograma": None, "espectrograma_error": self._error},
                AccumulatorState.FAILED,
            )
        if not self.save_png:
            return self._finish_png_only(
                {"espectrograma": None},
                AccumulatorState.COMPLETED,
            )
        chosen = self._seleccion or ([self._backup] if self._backup else [])
        if not chosen:
            return self._finish_png_only(
                {"espectrograma": None,
                 "espectrograma_error": "could not analyze the spectrum"},
                AccumulatorState.FAILED,
            )
        chosen = sorted(chosen, key=lambda item: item[2])

        is_multichannel = any(
            item[4].ndim > 1 and item[4].shape[1] > 2 for item in chosen
        )
        if is_multichannel:
            mc_active, _ = _select_multichannel_layout(chosen)
            if not mc_active:
                return self._finish_png_only(
                    {"espectrograma": None,
                     "espectrograma_error": "insufficient signal energy for spectral analysis"},
                    AccumulatorState.FAILED,
                )
            effective_stereo = len(mc_active) >= 2
        else:
            mc_active = ()
            effective_stereo = bool(self.is_stereo)

        lengths = [len(item[4]) for item in chosen]
        total_samples = sum(lengths)
        nperseg = max(256, min(4096, total_samples // 8))
        nperseg = min(nperseg, max(lengths))
        nperseg = max(32, nperseg)
        hop = max(1, nperseg // 8)

        window = np.hanning(nperseg + 1)[:-1].astype(np.float32)
        scale = np.float32(1.0 / float(window.sum()))
        scaled_window = (window * scale).astype(np.float32)
        frames_estimados = sum((n - nperseg) // hop + 1
                               for n in lengths if n >= nperseg)
        if frames_estimados <= 0:
            return self._finish_png_only(
                {"espectrograma": None,
                 "espectrograma_error": "could not analyze the spectrum"},
                AccumulatorState.FAILED,
            )
        step_png = max(1, frames_estimados // MAX_FRAMES_PNG)
        step_png = max(step_png,
                       int(round(self.sr / (hop * float(PNG_SPECTROGRAM_FPS)))))

        freqs = np.fft.rfftfreq(nperseg, 1.0 / self.sr)
        frames_global = 0
        png_power, png_times, png_n = [], [], 0
        medium = np.float32(0.5)

        for _, _, _, start, audio in chosen:
            if len(audio) < nperseg:
                continue
            n_frames_blk = (len(audio) - nperseg) // hop + 1
            remaining = MAX_FRAMES_PNG - png_n
            if remaining > 0:
                i0 = (-frames_global) % step_png
                idx = np.arange(i0, n_frames_blk, step_png)[:remaining]
                if idx.size:
                    if is_multichannel:
                        mc_frames = np.stack([
                            np.lib.stride_tricks.sliding_window_view(
                                np.ascontiguousarray(audio[:, ch], dtype=np.float32),
                                nperseg,
                            )[::hop][idx] * scaled_window
                            for ch in mc_active
                        ], axis=0)
                        z_all = _rfft_f32(mc_frames, axis=2)
                        power_total = np.mean(
                            np.maximum(_power_from_fft(z_all), _FLOOR_F32), axis=0
                        )
                    elif effective_stereo and audio.ndim > 1 and audio.shape[1] >= 2:
                        ch_l = np.ascontiguousarray(audio[:, 0], dtype=np.float32)
                        ch_r = np.ascontiguousarray(audio[:, 1], dtype=np.float32)
                        frames_l = np.lib.stride_tricks.sliding_window_view(
                            ch_l, nperseg
                        )[::hop][idx]
                        frames_r = np.lib.stride_tricks.sliding_window_view(
                            ch_r, nperseg
                        )[::hop][idx]
                        stereo_frames = np.empty((2, idx.size, nperseg), dtype=np.float32)
                        np.multiply(frames_l, scaled_window, out=stereo_frames[0])
                        np.multiply(frames_r, scaled_window, out=stereo_frames[1])
                        z_lr = _rfft_f32(stereo_frames, axis=2)
                        zm = (z_lr[0] + z_lr[1]) * medium
                        zs = (z_lr[0] - z_lr[1]) * medium
                        power_total = _power_from_fft(zm) + _power_from_fft(zs)
                    else:
                        ch_l = np.ascontiguousarray(
                            audio[:, 0] if audio.ndim > 1 else audio, dtype=np.float32
                        )
                        frames_l = np.lib.stride_tricks.sliding_window_view(
                            ch_l, nperseg
                        )[::hop][idx] * scaled_window
                        zl = _rfft_f32(frames_l, axis=1)
                        power_total = _power_from_fft(zl)
                    png_power.append(power_total)
                    png_times.append((start + idx * hop) / float(self.sr))
                    png_n += idx.size
            frames_global += n_frames_blk

        if not png_power:
            return self._finish_png_only(
                {"espectrograma": None},
                AccumulatorState.COMPLETED,
            )
        png_path, error = self._invoke_renderer(
            freqs, np.concatenate(png_times),
            np.concatenate(png_power, axis=0).T,
            ceiling_hz, cutoff_hz
        )
        out = {"espectrograma": png_path}
        if error:
            out["espectrograma_error"] = error
            return self._finish_png_only(out, AccumulatorState.FAILED)
        return self._finish_png_only(out, AccumulatorState.COMPLETED)
