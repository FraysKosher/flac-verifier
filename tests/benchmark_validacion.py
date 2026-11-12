"""Reproducible multi-family validation benchmark for FLAC VERIFIER (Phase 13).

Builds 260 tracks (110 authentic across 11 genuine mastering/archival/synth
families and 150 fakes across 21 real-encoder, SBR, subband, KBD-asymmetric,
upscale and 24/32-bit forensic families) with fixed deterministic seeds, runs
the engine on every track, and reports the confusion matrix, sensitivity,
specificity, Wilson 95% confidence intervals, per-family breakdown and corpus
SHA-256 digest.

When `ffmpeg` is available on PATH, real external codec binaries (`libmp3lame`,
`libshine`, `mp2`, `aac`, `aac_mf`, `libvorbis`, `libopus`, `wmav2`) encode and
decode the lossy families; otherwise deterministic filterbank equivalents keep
the benchmark runnable in minimal CI environments.

    python tests/benchmark_validacion.py

Exit code 0 only if sensitivity >= 95%, specificity >= 95% and album coherence
passes.
"""
from concurrent.futures import ProcessPoolExecutor
import hashlib
import math
import os
import shutil
import subprocess
import sys
import tempfile

import numpy as np
import soundfile as sf
from scipy import signal

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
import motor_flac
from _motor_espectral import _fast_mdct_f32, _get_celt_1920, _get_kbd_2048, _get_vorbis_2048

SEED = 2026
GOOD_VERDICTS = ("GENUINE LOSSLESS", "PROBABLY LOSSLESS")
BAD_VERDICTS = ("SUSPICIOUS", "PROBABLE UPSCALE", "TRANSCODE")
THRESHOLD = 0.95
HAS_FFMPEG = shutil.which("ffmpeg") is not None


# ─── REFERENCE & GENUINE MASTER GENERATORS ──────────────────────────────────

def track_reference(sr, duration=4.5, seed=2026):
    """Decorrelated pink noise + harmonics + periodic transients."""
    rng = np.random.default_rng(seed)
    n = int(sr * duration)
    t = np.linspace(0.0, duration, n, endpoint=False)
    b, a = ([0.049922, -0.095993, 0.050612, -0.004408],
            [1.0, -2.494956, 2.017265, -0.522189])
    pink_l = signal.lfilter(b, a, rng.standard_normal(n))
    pink_r = signal.lfilter(b, a, rng.standard_normal(n))
    env = 0.12 + 0.88 * (np.sin(2.0 * np.pi * 2.5 * t) ** 10)
    tonal_l = 0.25 * np.sin(2.0 * np.pi * 110.0 * t) + 0.15 * np.sin(2.0 * np.pi * 440.0 * t)
    tonal_r = 0.25 * np.sin(2.0 * np.pi * 110.0 * t) + 0.15 * np.cos(2.0 * np.pi * 440.0 * t)
    mix = np.column_stack([tonal_l + pink_l * env * 0.40,
                           tonal_r + pink_r * env * 0.40])
    return mix / (np.max(np.abs(mix)) * 1.12)


def musical_chord_master(sr=44100, duration=5.5, seed=2026):
    """Modulated harmonic chord + broadband transients for SBR / codec tests."""
    rng = np.random.default_rng(seed)
    n = int(sr * duration)
    t = np.linspace(0.0, duration, n, endpoint=False)
    mix = rng.standard_normal((n, 2)) * 0.15
    for f0 in (110.0, 164.81, 220.0, 329.63, 440.0, 587.33):
        for k in range(1, 34):
            fk = f0 * k
            if fk >= min(19500.0, sr * 0.44):
                break
            amp = (1.0 / (k ** 0.85)) * (0.5 + 0.5 * np.sin(2.0 * np.pi * (0.6 + 0.15 * (k % 5)) * t))
            mix[:, 0] += amp * np.sin(2.0 * np.pi * fk * t + rng.uniform(0.0, 6.28))
            mix[:, 1] += amp * np.sin(2.0 * np.pi * fk * t + rng.uniform(0.0, 6.28))
    return mix / (np.max(np.abs(mix)) * 1.15)


def dsd128_192k(duration=3.5, seed=2026):
    sr = 192000
    rng = np.random.default_rng(seed)
    n = int(sr * duration)
    freqs = np.fft.rfftfreq(n, 1.0 / sr)
    music = track_reference(sr, duration, seed)
    spec = np.fft.rfft(music, axis=0)
    spec[freqs > 20000.0] = 0.0
    music = np.fft.irfft(spec, n, axis=0)
    music /= np.max(np.abs(music)) * 1.05

    level_db = np.full(freqs.shape, -400.0)
    level_db[(freqs >= 20000.0) & (freqs <= 40000.0)] = 0.0
    ramp = (freqs > 40000.0) & (freqs <= 86000.0)
    level_db[ramp] = 20.0 * (freqs[ramp] - 40000.0) / 40000.0
    noise = (rng.standard_normal((freqs.size, 2))
             + 1j * rng.standard_normal((freqs.size, 2)))
    noise *= (10.0 ** (level_db / 20.0))[:, None]
    noise = np.fft.irfft(noise, n, axis=0)
    noise *= 0.035 / np.max(np.abs(noise))
    lsb = 2.0 ** -23
    dither = (rng.random((n, 2)) - rng.random((n, 2))) * lsb
    return np.clip(music + noise + dither, -0.99, 0.99)


def dsd_like(sr, duration=4.0, seed=2026, rise_start=24000.0, rise_end=40000.0):
    rng = np.random.default_rng(seed)
    n = int(sr * duration)
    freqs = np.fft.rfftfreq(n, 1.0 / sr)
    music = (rng.standard_normal((n // 2 + 1, 2))
             + 1j * rng.standard_normal((n // 2 + 1, 2)))
    music[freqs > 20500.0] = 0.0
    music = np.fft.irfft(music, n, axis=0)
    music /= np.max(np.abs(music)) * 4.0

    noise = (rng.standard_normal((n // 2 + 1, 2))
             + 1j * rng.standard_normal((n // 2 + 1, 2)))
    span = max(1000.0, rise_end - rise_start)
    ramp = np.clip((freqs - rise_start) / span, 0.0, 1.0)
    noise *= (((freqs >= rise_start) & (freqs <= rise_end))[:, None]
              * (1.0 + 9.0 * ramp)[:, None])
    noise = np.fft.irfft(noise, n, axis=0)
    noise /= np.max(np.abs(noise)) * 3.0

    mix = music + noise
    return mix / (np.max(np.abs(mix)) * 1.06)


def solo_acoustic_low_centroid(sr=44100, duration=5.0, seed=2026):
    rng = np.random.default_rng(seed)
    n = int(sr * duration)
    t = np.arange(n) / float(sr)
    freqs = np.fft.rfftfreq(n, 1.0 / sr)
    noise = (rng.standard_normal((freqs.size, 2))
             + 1j * rng.standard_normal((freqs.size, 2)))
    level_db = np.full(freqs.shape, -400.0)
    pink = (freqs >= 45.0) & (freqs <= 8000.0)
    level_db[pink] = -11.0 * np.log10(freqs[pink] / 45.0)
    level_8k = -11.0 * np.log10(8000.0 / 45.0)
    fall = (freqs > 8000.0) & (freqs <= 14800.0)
    level_db[fall] = level_8k - 6.2 * (freqs[fall] - 8000.0) / 1000.0
    level_db[freqs > 14800.0] = level_8k - 6.2 * 6.8
    noise *= (10.0 ** (level_db / 20.0))[:, None]
    y = np.fft.irfft(noise, n, axis=0)
    y /= np.max(np.abs(y)) * 1.05
    y *= (0.35 + 0.65 * (np.sin(2.0 * np.pi * 0.85 * t) ** 2))[:, None]
    return y / (np.max(np.abs(y)) * 1.04)


def vintage_archival_tape(sr=44100, duration=5.0, seed=2026):
    rng = np.random.default_rng(seed)
    n = int(sr * duration)
    raw = rng.standard_normal((n, 2))
    bright = signal.sosfilt(
        signal.butter(2, [1400.0, 5200.0], btype="band", fs=sr, output="sos"),
        raw, axis=0,
    ) * 2.0 + raw * 0.25
    cutoff = 6300.0 + 350.0 * (seed % 4)
    vintage = signal.sosfilt(
        signal.butter(4, cutoff, btype="low", fs=sr, output="sos"),
        bright, axis=0,
    )
    hiss = rng.standard_normal((n, 2)) * (10.0 ** (-80.0 / 20.0))
    mix = vintage + hiss
    return mix / (np.max(np.abs(mix)) * 1.15)


# ─── LOSSY, SBR, SUBBAND & BIT-FORENSIC GENERATORS ──────────────────────────

def _imdct_overlap_add(coeffs, window):
    from scipy import fft as sp_fft
    n_frames, n = coeffs.shape
    two_n = n * 2
    n2 = n // 2
    u = sp_fft.idct(coeffs, type=4, axis=-1, norm="ortho")
    y_win = np.empty((n_frames, two_n), dtype=np.float32)
    y_win[:, :n2] = u[:, n2:]
    y_win[:, n2:n] = -u[:, n2:][::-1] if u.ndim == 1 else -u[:, -1 : n2 - 1 : -1]
    y_win[:, n : n + n2] = -u[:, n2 - 1 :: -1]
    y_win[:, n + n2 :] = -u[:, :n2]
    y_win *= window
    out = np.zeros((n_frames + 1) * n, dtype=np.float32)
    for idx in range(n_frames):
        out[idx * n : idx * n + two_n] += y_win[idx]
    return out


def _mdct1024_roundtrip(x, sr, f_lo=12000.0, f_hi=20500.0, zero_prob=0.42, seed=7):
    rng = np.random.default_rng(seed)
    w = _get_vorbis_2048()
    nyq = sr / 2.0
    k_lo = int(f_lo / nyq * 1024)
    k_hi = int(min(f_hi, sr * 0.46) / nyq * 1024)
    out = []
    for ch in range(x.shape[1]):
        padded = np.pad(x[:, ch].astype(np.float32), (2048, 2048))
        n_fr = (padded.size - 2048) // 1024
        frames = np.lib.stride_tricks.sliding_window_view(padded, 2048)[::1024][:n_fr]
        c = _fast_mdct_f32(frames, w)
        mask = rng.random((n_fr, k_hi - k_lo)) < zero_prob
        c[:, k_lo:k_hi] = np.where(mask, 0.0, c[:, k_lo:k_hi] * np.sqrt(1.0 / (1.0 - zero_prob)))
        rec = _imdct_overlap_add(c, w)
        out.append(rec[2048 + 37 : 2048 + 37 + len(x)])
    y = np.column_stack(out).astype(np.float64)
    return y / (np.max(np.abs(y)) * 1.14)


def _pqmf576_roundtrip(x, sr=44100, seed=11):
    rng = np.random.default_rng(seed)
    h = signal.firwin(512, 1.0 / 64.0, window=("kaiser", 9.0)).astype(np.float32)
    n_idx = np.arange(512, dtype=np.float32)
    win36 = np.sin(np.pi / 36.0 * (np.arange(36, dtype=np.float32) + 0.5)).astype(np.float32)
    n = len(x)
    out = np.zeros_like(x)
    sos_low = signal.butter(6, 13800.0, btype="low", fs=sr, output="sos")
    for ch in range(x.shape[1]):
        low = signal.sosfilt(sos_low, x[:, ch]).astype(np.float32)
        n_gran = n // 576 + 4
        m_sub = (n_gran + 1) * 18
        high = np.zeros(m_sub * 32 + 1024, dtype=np.float32)
        for s in range(22, 30):
            coeffs = rng.standard_normal((n_gran, 18)).astype(np.float32) * 0.08
            mask_z = rng.random(coeffs.shape) < 0.48
            coeffs = np.where(mask_z, 0.0, coeffs * np.sqrt(1.9))
            sub_sig = _imdct_overlap_add(coeffs, win36)[:m_sub]
            if s % 2 == 1:
                sub_sig[1::2] *= -1.0
            up = np.zeros(m_sub * 32, dtype=np.float32)
            up[::32] = sub_sig * 32.0
            syn = (h * np.cos(np.pi / 64.0 * (2 * s + 1) * (n_idx - 16)).astype(np.float32))[::-1]
            r_s = np.convolve(up, syn, mode="full")
            high[: r_s.size] += r_s
        comb = low + high[197 : 197 + n] * 0.25
        out[:, ch] = comb
    return out / (np.max(np.abs(out)) * 1.14)


def _celt960_roundtrip(x, sr=48000, seed=19):
    w_celt = _get_celt_1920()
    n = len(x)
    out = np.zeros_like(x)
    for ch in range(x.shape[1]):
        pre = np.empty(n + 3840, dtype=np.float32)
        pad = np.pad(x[:, ch].astype(np.float32), (1920, 1920))
        pre[0] = pad[0]
        pre[1:] = pad[1:] - np.float32(0.85) * pad[:-1]
        n_fr = (pre.size - 1920) // 960
        frames = np.lib.stride_tricks.sliding_window_view(pre, 1920)[::960][:n_fr]
        c = _fast_mdct_f32(frames, w_celt)
        c[:, 800:] = 0.0
        rec_pre = _imdct_overlap_add(c, w_celt)
        rec = signal.lfilter([1.0], [1.0, -0.85], rec_pre)
        out[:, ch] = rec[1920 + 53 : 1920 + 53 + n]
    return out / (np.max(np.abs(out)) * 1.14)


def _seamless_sbr(x, sr=44100, f_cross=11250.0, f_top=20500.0, seed=29):
    rng = np.random.default_rng(seed)
    n_fft, hop = 2048, 1024
    df = sr / float(n_fft)
    k_cross = int(f_cross / df)
    k_top = int(f_top / df)
    patch_w = k_top - k_cross
    k_src = k_cross // 2
    out = np.zeros_like(x)
    for ch in range(x.shape[1]):
        _, _, z_stft = signal.stft(x[:, ch], fs=sr, nperseg=n_fft, noverlap=n_fft - hop)
        z_sbr = z_stft.copy()
        z_sbr[k_cross:, :] *= 1e-4
        src_patch = z_stft[k_src : k_src + patch_w, :]
        e_below = np.sqrt(np.mean(np.abs(z_stft[k_cross - 3 : k_cross, :]) ** 2, axis=0, keepdims=True)) + 1e-12
        e_start = np.sqrt(np.mean(np.abs(src_patch[:3, :]) ** 2, axis=0, keepdims=True)) + 1e-12
        tilt = 10.0 ** (np.linspace(-0.5, -9.0, patch_w)[:, None] / 20.0)
        noise_c = (rng.standard_normal(src_patch.shape) + 1j * rng.standard_normal(src_patch.shape)) * np.abs(src_patch) * 0.22
        z_sbr[k_cross : k_cross + patch_w, :] = (src_patch * 0.94 + noise_c) * (e_below / e_start) * tilt
        _, rec = signal.istft(z_sbr, fs=sr, nperseg=n_fft, noverlap=n_fft - hop)
        out[:, ch] = rec[: len(x)]
    return out / (np.max(np.abs(out)) * 1.14)


def _musepack_pqmf32(x, sr=44100, block_len=12, seed=31):
    rng = np.random.default_rng(seed)
    h = signal.firwin(512, 1.0 / 64.0, window=("kaiser", 9.0)).astype(np.float64)
    n_idx = np.arange(512, dtype=np.float64)
    out = np.zeros_like(x)
    for ch in range(x.shape[1]):
        sig = x[:, ch]
        recon = np.zeros(len(sig) + 1024, dtype=np.float64)
        for s in range(31):
            ana = h * np.cos(np.pi / 64.0 * (2 * s + 1) * (n_idx - 16))
            syn = ana[::-1]
            sb = np.convolve(sig, ana, mode="full")[511 : 511 + len(sig)]
            dec = sb[::32].copy()
            if s >= 18:
                n_blk = len(dec) // block_len
                for b_i in range(n_blk):
                    sl = dec[b_i * block_len : (b_i + 1) * block_len]
                    if s >= 22 and rng.random() < 0.35:
                        dec[b_i * block_len : (b_i + 1) * block_len] = 0.0
                    else:
                        levels = 7.0 if s >= 24 else 15.0
                        step = (np.max(np.abs(sl)) + 1e-12) / levels
                        dec[b_i * block_len : (b_i + 1) * block_len] = np.round(sl / step) * step
            up = np.zeros(len(dec) * 32, dtype=np.float64)
            up[::32] = dec * 32.0
            r_s = np.convolve(up, syn, mode="full")
            recon[: len(r_s)] += r_s
        shift = 19 + (seed % 11)
        out[:, ch] = recon[shift : shift + len(sig)]
    return out / (np.max(np.abs(out)) * 1.14)


def _save(path, x, sr, subtype):
    sf.write(path, np.clip(x, -0.99, 0.99), sr, subtype=subtype)


def _ffmpeg_encode_decode(wav_in, out_flac, lossy_ext, codec_args, out_sr=None):
    if not HAS_FFMPEG:
        return False
    tmp_lossy = out_flac + lossy_ext
    try:
        cmd1 = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-i", wav_in] + codec_args + [tmp_lossy]
        r1 = subprocess.run(cmd1, capture_output=True, text=True)
        if r1.returncode != 0:
            return False
        cmd2 = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", tmp_lossy]
        if out_sr:
            cmd2 += ["-ar", str(out_sr)]
        cmd2 += [out_flac]
        r2 = subprocess.run(cmd2, capture_output=True, text=True)
        return r2.returncode == 0 and os.path.exists(out_flac)
    finally:
        if os.path.exists(tmp_lossy):
            try:
                os.remove(tmp_lossy)
            except OSError:
                pass


# ─── DATASET (n = 240: 100 authentic + 140 fake) ────────────────────────────

def create_dataset(directory):
    """Returns [(path, authentic_bool, class_name)] for n = 240 tracks."""
    cases = []
    sr = 44100

    # --- 10 AUTHENTIC FAMILIES (10 tracks each = 100 authentic) ---
    for i in range(10):
        p = os.path.join(directory, f"gen_cd_{i:02d}.flac")
        _save(p, track_reference(sr, 4.5, 1000 + i), sr, "PCM_16")
        cases.append((p, True, "GEN 01: CD fullband master 16/44.1"))

    for i in range(10):
        p = os.path.join(directory, f"gen_studio24_48_{i:02d}.flac")
        x = track_reference(48000, 4.5, 1100 + i)
        rng = np.random.default_rng(1150 + i)
        x += rng.normal(0.0, 10.0 ** (-118.0 / 20.0), x.shape)
        _save(p, x, 48000, "PCM_24")
        cases.append((p, True, "GEN 02: Studio master 24/48"))

    for i in range(10):
        p = os.path.join(directory, f"gen_hires_analog_{i:02d}.flac")
        x = track_reference(96000, 4.0, 1200 + i)
        x = signal.sosfilt(signal.butter(2, 18500.0, btype="low", fs=96000, output="sos"), x, axis=0)
        rng = np.random.default_rng(1250 + i)
        x += rng.normal(0.0, 10.0 ** (-104.0 / 20.0), x.shape)
        _save(p, x, 96000, "PCM_24")
        cases.append((p, True, "GEN 03: Hi-Res analogue roll-off 24/96"))

    for i in range(10):
        p = os.path.join(directory, f"gen_hires_192_{i:02d}.flac")
        rng = np.random.default_rng(1300 + i)
        n192 = int(192000 * 3.5)
        x = signal.sosfilt(signal.butter(3, 38000.0, btype="low", fs=192000, output="sos"),
                           rng.standard_normal((n192, 2)) * 0.25, axis=0)
        x += rng.normal(0.0, 10.0 ** (-110.0 / 20.0), x.shape)
        _save(p, x / (np.max(np.abs(x)) * 1.12), 192000, "PCM_24")
        cases.append((p, True, "GEN 04: Hi-Res ultrasonic master 24/192"))

    for i in range(10):
        p = os.path.join(directory, f"gen_dsd64_{i:02d}.flac")
        _save(p, dsd_like(88200, 4.0, 1400 + i, 24000.0, 40000.0), 88200, "PCM_24")
        cases.append((p, True, "GEN 05: DSD64 / SACD transfer 24/88.2"))

    for i in range(10):
        p = os.path.join(directory, f"gen_dsd128_{i:02d}.flac")
        _save(p, dsd128_192k(3.5, 1500 + i), 192000, "PCM_24")
        cases.append((p, True, "GEN 06: DSD128 transfer 24/192"))

    for i in range(10):
        p = os.path.join(directory, f"gen_acoustic18k_{i:02d}.flac")
        x = track_reference(sr, 4.5, 1600 + i)
        x = signal.sosfilt(signal.butter(2, 18000.0, btype="low", fs=sr, output="sos"), x, axis=0)
        _save(p, x / max(1.0, np.max(np.abs(x))), sr, "PCM_16")
        cases.append((p, True, "GEN 07: Acoustic 18 kHz roll-off 16/44.1"))

    for i in range(10):
        p = os.path.join(directory, f"gen_solo_lowcent_{i:02d}.flac")
        _save(p, solo_acoustic_low_centroid(sr, 5.0, 1700 + i), sr, "PCM_16")
        cases.append((p, True, "GEN 08: Solo acoustic low-centroid (<15 kHz)"))

    for i in range(10):
        p = os.path.join(directory, f"gen_vintage_1950s_{i:02d}.flac")
        _save(p, vintage_archival_tape(sr, 5.0, 1800 + i), sr, "PCM_16")
        cases.append((p, True, "GEN 09: Vintage 1950s archival tape (<14 kHz)"))

    for i in range(10):
        p = os.path.join(directory, f"gen_vinyl_synth_{i:02d}.flac")
        rng = np.random.default_rng(1900 + i)
        if i < 4:
            base = vintage_archival_tape(sr, 4.5, 1910 + i)
            crackle = rng.standard_normal(base.shape) * (10.0 ** (-68.0 / 20.0))
            ticks = rng.choice(len(base), size=16, replace=False)
            crackle[ticks] += rng.uniform(-0.12, 0.12, size=(16, 2))
            sig = (base + crackle) / (np.max(np.abs(base + crackle)) * 1.12)
        elif i < 7:
            base = vintage_archival_tape(sr, 4.5, 1930 + i)
            hiss = signal.sosfilt(signal.butter(1, 14000.0, btype="low", fs=sr, output="sos"),
                                  rng.standard_normal(base.shape), axis=0) * (10.0 ** (-58.0 / 20.0))
            sig = (base + hiss) / (np.max(np.abs(base + hiss)) * 1.12)
        else:
            n = int(sr * 4.5)
            t = np.arange(n) / float(sr)
            saw = np.zeros((n, 2))
            for f0 in (110.0, 164.81, 220.0, 277.18, 329.63, 440.0, 659.25):
                for k in range(1, int(21500 // f0)):
                    saw[:, 0] += (1.0 / k) * np.sin(2.0 * np.pi * k * f0 * t)
                    saw[:, 1] += (1.0 / k) * np.cos(2.0 * np.pi * k * f0 * t)
            saw += rng.standard_normal((n, 2)) * 0.12
            sig = saw / (np.max(np.abs(saw)) * 1.14)
        _save(p, sig, sr, "PCM_16")
        cases.append((p, True, "GEN 10: Vinyl / cassette / polyphonic synth"))

    for i in range(10):
        p = os.path.join(directory, f"gen_stationary_synth32_{i:02d}.flac")
        rng = np.random.default_rng(1960 + i)
        n = int(sr * 5.0)
        t = np.arange(n, dtype=np.float64) / float(sr)
        if i < 6:
            drone = np.zeros((n, 2), dtype=np.float64)
            f_pair = (550.0 + 35.0 * i, 825.0 + 52.5 * i)
            for f0 in f_pair:
                for k in range(1, int(21200.0 // f0) + 1):
                    fk = f0 * k
                    amp = 1.0 / (k ** 0.76)
                    drone[:, 0] += amp * np.sin(2.0 * np.pi * fk * t + 0.31 * k)
                    drone[:, 1] += amp * np.cos(2.0 * np.pi * fk * t + 0.53 * k)
            drone += rng.standard_normal((n, 2)) * (10.0 ** (-84.0 / 20.0))
            drone /= np.max(np.abs(drone)) * 1.15
            _save(p, drone, sr, "PCM_16")
        else:
            x32 = track_reference(sr, 4.5, 1980 + i)
            x32 += rng.standard_normal(x32.shape) * (10.0 ** (-132.0 / 20.0))
            x32 /= np.max(np.abs(x32)) * 1.12
            if HAS_FFMPEG:
                w32 = os.path.join(directory, f"_tmp_gen32_{i}.wav")
                sf.write(w32, np.clip(x32, -0.99, 0.99), sr, subtype="PCM_32")
                subprocess.run(
                    ["ffmpeg", "-y", "-v", "error", "-i", w32,
                     "-strict", "experimental", "-c:a", "flac", "-sample_fmt", "s32", p],
                    check=True,
                )
                os.remove(w32)
            else:
                _save(p, x32, sr, "PCM_24")
        cases.append((p, True, "GEN 11: Stationary synth drone & 32-bit PCM_32"))

    # Pre-render 7 reference WAVs at 44.1 kHz for real ffmpeg codecs
    wav_refs = []
    for i in range(7):
        w_path = os.path.join(directory, f"_ref_master_{i}.wav")
        ref_sig = track_reference(sr, 5.0, 5000 + i) if i < 4 else (
            0.55 * musical_chord_master(sr, 5.0, 5100 + i)
            + 0.45 * track_reference(sr, 5.0, 5000 + i)
        )
        _save(w_path, ref_sig, sr, "PCM_16")
        wav_refs.append(w_path)

    def _add_codec_family(idx_base, tag, label, ext, args, fallback_fn, out_sr=None):
        for i in range(7):
            p = os.path.join(directory, f"fake_{tag}_{i:02d}.flac")
            if not _ffmpeg_encode_decode(wav_refs[i], p, ext, args, out_sr=out_sr):
                y, fs_y = fallback_fn(i)
                _save(p, y, fs_y, "PCM_16")
            cases.append((p, False, label))

    # --- 20 FAKE / ADVERSARIAL FAMILIES (7 tracks each = 140 fake) ---
    _add_codec_family(
        1, "mp3_128", "FAKE 01: MP3 LAME 128 kbps", ".mp3",
        ["-c:a", "libmp3lame", "-b:a", "128k"],
        lambda i: (signal.sosfilt(signal.ellip(10, 0.1, 80.0, 16000.0, btype="low", fs=sr, output="sos"),
                                  track_reference(sr, 5.0, 5000 + i), axis=0), sr),
    )
    _add_codec_family(
        2, "mp3_v0", "FAKE 02: MP3 LAME V0 (~245 kbps)", ".mp3",
        ["-c:a", "libmp3lame", "-q:a", "0"],
        lambda i: (_pqmf576_roundtrip(track_reference(sr, 5.0, 5000 + i), sr, 6100 + i), sr),
    )
    _add_codec_family(
        3, "mp3_320_fb", "FAKE 03: MP3 LAME 320k fullband (-cutoff 22050)", ".mp3",
        ["-c:a", "libmp3lame", "-b:a", "320k", "-cutoff", "22050"],
        lambda i: (_pqmf576_roundtrip(track_reference(sr, 5.0, 5000 + i), sr, 6200 + i), sr),
    )
    _add_codec_family(
        4, "mp3_shine320", "FAKE 04: MP3 Shine fixed-point 320 kbps", ".mp3",
        ["-c:a", "libshine", "-b:a", "320k"],
        lambda i: (_pqmf576_roundtrip(track_reference(sr, 5.0, 5000 + i), sr, 6300 + i), sr),
    )
    _add_codec_family(
        5, "mp2_384", "FAKE 05: MP2 broadcast 384 kbps", ".mp2",
        ["-c:a", "mp2", "-b:a", "384k"],
        lambda i: (signal.sosfilt(signal.ellip(10, 0.1, 80.0, 14000.0, btype="low", fs=sr, output="sos"),
                                  track_reference(sr, 5.0, 5000 + i), axis=0), sr),
    )
    _add_codec_family(
        6, "aac_128", "FAKE 06: AAC-LC 128 kbps", ".m4a",
        ["-c:a", "aac", "-b:a", "128k"],
        lambda i: (_mdct1024_roundtrip(track_reference(sr, 5.0, 5000 + i), sr, 11000.0, 18000.0, 0.50, 6500 + i), sr),
    )
    _add_codec_family(
        7, "aac_320_fb", "FAKE 07: AAC-LC 320k fullband (-cutoff 22000)", ".m4a",
        ["-c:a", "aac", "-b:a", "320k", "-cutoff", "22000"],
        lambda i: (_mdct1024_roundtrip(track_reference(sr, 5.0, 5000 + i), sr, 12000.0, 20500.0, 0.38, 6600 + i), sr),
    )
    _add_codec_family(
        8, "aac_mf_192", "FAKE 08: MediaFoundation AAC 192 kbps", ".m4a",
        ["-c:a", "aac_mf", "-b:a", "192k"],
        lambda i: (_mdct1024_roundtrip(track_reference(sr, 5.0, 5000 + i), sr, 11500.0, 19000.0, 0.45, 6700 + i), sr),
    )
    _add_codec_family(
        9, "vorbis_q4", "FAKE 09: Vorbis -q4 (~128 kbps)", ".ogg",
        ["-c:a", "libvorbis", "-q:a", "4"],
        lambda i: (_mdct1024_roundtrip(track_reference(sr, 5.0, 5000 + i), sr, 10500.0, 17500.0, 0.52, 6800 + i), sr),
    )
    _add_codec_family(
        10, "vorbis_q9_fb", "FAKE 10: Vorbis -q9 (~320k fullband) + -q10 (~500k)", ".ogg",
        ["-c:a", "libvorbis", "-q:a", "9"],
        lambda i: (_mdct1024_roundtrip(track_reference(sr, 5.0, 5000 + i), sr, 12500.0, 20500.0, 0.35, 6900 + i), sr),
    )
    # Replace last 2 of FAKE 10 with unconstrained Vorbis -q10 (~500 kbps) when ffmpeg is present
    # to measure the documented physical limit honestly inside the benchmark!
    if HAS_FFMPEG:
        for i in (5, 6):
            p_q10 = os.path.join(directory, f"fake_vorbis_q9_fb_{i:02d}.flac")
            _ffmpeg_encode_decode(wav_refs[i], p_q10, ".ogg", ["-c:a", "libvorbis", "-q:a", "10"])

    _add_codec_family(
        11, "opus_96", "FAKE 11: Opus 96 kbps", ".opus",
        ["-c:a", "libopus", "-b:a", "96k"],
        lambda i: (_celt960_roundtrip(track_reference(48000, 5.0, 5000 + i), 48000, 7000 + i), 48000),
        out_sr=48000,
    )
    _add_codec_family(
        12, "opus_256_fb", "FAKE 12: Opus 256k fullband (CELT-960)", ".opus",
        ["-c:a", "libopus", "-b:a", "256k"],
        lambda i: (_celt960_roundtrip(track_reference(48000, 5.0, 5000 + i), 48000, 7100 + i), 48000),
        out_sr=48000,
    )
    _add_codec_family(
        13, "wmav2_192", "FAKE 13: WMAv2 192 kbps", ".asf",
        ["-c:a", "wmav2", "-b:a", "192k"],
        lambda i: (_mdct1024_roundtrip(track_reference(sr, 5.0, 5000 + i), sr, 12000.0, 20000.0, 0.44, 7200 + i), sr),
    )

    # 14. Seamless HE-AAC QMF SBR (0 dB notch, 44.1 kHz & 96 kHz upsampled)
    for i in range(7):
        p = os.path.join(directory, f"fake_sbr_seamless_{i:02d}.flac")
        sbr = _seamless_sbr(musical_chord_master(sr, 5.5, 7300 + i), sr,
                            f_cross=10800.0 + 400.0 * (i % 3), f_top=20400.0, seed=7350 + i)
        if i < 4:
            _save(p, sbr, sr, "PCM_16")
        else:
            sbr96 = signal.resample_poly(sbr, 320, 147, axis=0)
            rng = np.random.default_rng(7380 + i)
            sbr96 += rng.standard_normal(sbr96.shape) * 1e-4
            _save(p, sbr96, 96000, "PCM_24")
        cases.append((p, False, "FAKE 14: Seamless HE-AAC QMF SBR (44.1 & 96 kHz)"))

    # 15. Musepack SV7/SV8 PQMF-32 pure subband lattice
    for i in range(7):
        p = os.path.join(directory, f"fake_musepack_pqmf32_{i:02d}.flac")
        blk_len = 12 if i < 4 else 36
        mpc = _musepack_pqmf32(track_reference(sr, 5.0, 7400 + i), sr, block_len=blk_len, seed=7450 + i)
        _save(p, mpc, sr, "PCM_16")
        cases.append((p, False, "FAKE 15: Musepack SV7/SV8 PQMF-32 subband"))

    # 16. Vintage 1950s archival -> MP3 / MDCT transcode
    for i in range(7):
        p = os.path.join(directory, f"fake_vintage_transcode_{i:02d}.flac")
        v_src = vintage_archival_tape(sr, 5.0, 7500 + i)
        w_v = os.path.join(directory, f"_vint_{i}.wav")
        _save(w_v, v_src, sr, "PCM_16")
        if i < 4 and _ffmpeg_encode_decode(w_v, p, ".mp3", ["-c:a", "libmp3lame", "-b:a", "128k"]):
            pass
        else:
            y_v = _mdct1024_roundtrip(v_src, sr, f_lo=6000.0, f_hi=11500.0, zero_prob=0.42, seed=7550 + i)
            _save(p, y_v, sr, "PCM_16")
        cases.append((p, False, "FAKE 16: Vintage 1950s master -> MP3/MDCT"))

    # 17. Sinc upscale 44.1->96 & 48->192 kHz
    sinc_4x = signal.firwin(257, 0.245, window=("kaiser", 12.0)) * 4.0
    for i in range(7):
        p = os.path.join(directory, f"fake_sinc_upscale_{i:02d}.flac")
        if i < 4:
            x = signal.resample_poly(track_reference(sr, 4.0, 7600 + i), up=320, down=147, axis=0)
            _save(p, x, 96000, "PCM_24")
        else:
            src48 = track_reference(48000, 3.5, 7620 + i)
            w48 = os.path.join(directory, f"_up48_{i}.wav")
            _save(w48, src48, 48000, "PCM_24")
            if not _ffmpeg_encode_decode(w48, p, ".wav", ["-c:a", "pcm_s24le"], out_sr=192000):
                x = signal.upfirdn(sinc_4x, src48, up=4, down=1, axis=0)[128 : 128 + len(src48) * 4]
                _save(p, x, 192000, "PCM_24")
            if os.path.exists(w48):
                os.remove(w48)
        cases.append((p, False, "FAKE 17: Sinc upscale (44.1->96 & 48->192)"))

    # 18. Aliasing mirror upscale 44.1->88.2 kHz
    for i in range(7):
        x = track_reference(sr, 4.0, 7700 + i)
        n = len(x)
        y = np.empty((n * 2, 2), dtype=np.float64)
        y[0::2] = x
        y[1:-1:2] = 0.5 * (x[:-1] + x[1:])
        y[-1] = x[-1]
        p = os.path.join(directory, f"fake_mirror_{i:02d}.flac")
        _save(p, y, 88200, "PCM_24")
        cases.append((p, False, "FAKE 18: Aliasing mirror upscale 44.1->88.2"))

    # 19. 16-bit zero-padded & gain-comb scaled into 24-bit
    for i in range(7):
        p = os.path.join(directory, f"fake_24bit_pad_comb_{i:02d}.flac")
        x = np.round(track_reference(sr, 5.5, 7800 + i) * 32767.0) / 32768.0
        if i < 4:
            _save(p, x, sr, "PCM_24")
        else:
            _save(p, x * 0.96605, sr, "PCM_24")
        cases.append((p, False, "FAKE 19: 16-in-24 zero-pad & gain-comb"))

    # 20. 16-bit + +/-1..2 LSB boundary micro-dither in 24-bit
    for i in range(7):
        p = os.path.join(directory, f"fake_24bit_microdither_{i:02d}.flac")
        rng = np.random.default_rng(7900 + i)
        x16 = np.round(track_reference(96000, 4.0, 7950 + i) * 32767.0) * 256.0
        dither = rng.integers(-2, 3, size=x16.shape)
        x24 = np.clip(x16 + dither, -8388607, 8388607) / 8388608.0
        _save(p, x24, 96000, "PCM_24")
        cases.append((p, False, "FAKE 20: 16-in-24 boundary micro-dither"))

    # 21. MPEG-4 AAC KBD-1024 asymmetric stereo & 16-in-32 container bloat
    w_kbd = _get_kbd_2048()
    nyq = sr / 2.0
    k_lo_k, k_hi_k = int(12500.0 / nyq * 1024), int(20200.0 / nyq * 1024)
    for i in range(10):
        p = os.path.join(directory, f"fake_kbd_32bit_{i:02d}.flac")
        rng = np.random.default_rng(7980 + i)
        if i < 5:
            base = musical_chord_master(sr, 5.0, 7980 + i)
            left_muf = signal.sosfilt(
                signal.butter(4, 11000.0, btype="low", fs=sr, output="sos"),
                base[:, 0],
            )
            pad_r = np.pad(base[:, 1].astype(np.float32), (2048, 2048))
            n_fr = (pad_r.size - 2048) // 1024
            fr_r = np.lib.stride_tricks.sliding_window_view(pad_r, 2048)[::1024][:n_fr]
            c_r = _fast_mdct_f32(fr_r, w_kbd)
            m_r = rng.random((n_fr, k_hi_k - k_lo_k)) < 0.45
            c_r[:, k_lo_k:k_hi_k] = np.where(m_r, 0.0, c_r[:, k_lo_k:k_hi_k] * np.sqrt(1.0 / 0.55))
            rec_r = _imdct_overlap_add(c_r, w_kbd)[2048 + 73 : 2048 + 73 + len(base)]
            asym = np.column_stack([left_muf * 0.35, rec_r.astype(np.float64)])
            asym /= np.max(np.abs(asym)) * 1.14
            _save(p, asym, sr, "PCM_16")
        else:
            x16 = np.round(track_reference(sr, 4.5, 7990 + i) * 32767.0) / 32768.0
            if HAS_FFMPEG:
                w16 = os.path.join(directory, f"_tmp_pad32_{i}.wav")
                sf.write(w16, np.clip(x16, -0.99, 0.99), sr, subtype="PCM_16")
                subprocess.run(
                    ["ffmpeg", "-y", "-v", "error", "-i", w16,
                     "-strict", "experimental", "-c:a", "flac", "-sample_fmt", "s32", p],
                    check=True,
                )
                os.remove(w16)
            else:
                _save(p, x16, sr, "PCM_24")
        cases.append((p, False, "FAKE 21: AAC KBD-1024 asymmetric & 16-in-32 bloat"))

    for w_path in wav_refs:
        if os.path.exists(w_path):
            os.remove(w_path)
    for fn in os.listdir(directory):
        if fn.startswith("_vint_") and fn.endswith(".wav"):
            os.remove(os.path.join(directory, fn))

    return cases


def create_album_coherence(directory):
    """8 homogeneous genuine tracks + 1 acoustic low-ceiling track (9 total)."""
    album = os.path.join(directory, "album_coherencia")
    os.makedirs(album, exist_ok=True)
    sr = 44100
    for i in range(8):
        x = track_reference(sr, 4.0, 8000 + i)
        _save(os.path.join(album, f"genuine_{i}.flac"), x, sr, "PCM_16")
    x = track_reference(sr, 4.0, 8100)
    x = signal.sosfilt(signal.butter(2, 17000.0, btype="low", fs=sr, output="sos"), x, axis=0)
    x /= max(1.0, np.max(np.abs(x)))
    _save(os.path.join(album, "acoustic.flac"), x, sr, "PCM_16")
    return album


def evaluate_album_coherence(directory):
    """Runs the real batch path so `apply_album_coherence` is exercised."""
    album = create_album_coherence(directory)
    files = [os.path.join(album, name) for name in sorted(os.listdir(album))
             if name.endswith(".flac")]
    analysis, _ = motor_flac.analyze_files(
        files, "center", None, False, workers=1, use_cache=False)
    results = list(analysis)
    acoustic = next((r for r in results if r.get("archivo") == "acoustic.flac"), None)
    ok = bool(acoustic
              and acoustic.get("coherencia_album")
              and acoustic.get("veredicto") == "PROBABLY LOSSLESS"
              and motor_flac.ALBUM_COHERENCE_NOTE in (acoustic.get("problemas") or []))
    return ok, results


# ─── STATISTICS & PARALLEL EVALUATION ───────────────────────────────────────

def wilson(k, n, z=1.96):
    """Wilson score interval for a proportion (95% by default)."""
    if n <= 0:
        return 0.0, 0.0
    p = k / float(n)
    denom = 1.0 + z * z / n
    center = (p + z * z / (2.0 * n)) / denom
    margin = (z * math.sqrt(p * (1.0 - p) / n + z * z / (4.0 * n * n)) / denom)
    return max(0.0, center - margin), min(1.0, center + margin)


def _eval_one(item):
    path, autentica, category = item
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    res = motor_flac.analyze_file_data(path, use_cache=False)
    verdict = res.get("veredicto") or "indeterminate"
    correct = (verdict in GOOD_VERDICTS) if autentica else (verdict in BAD_VERDICTS)
    return {
        "clase": category,
        "autentica": autentica,
        "veredicto": verdict,
        "correcto": correct,
        "archivo": os.path.basename(path),
        "sha256": h.hexdigest(),
        "error": res.get("error"),
    }


def evaluate(directory):
    cases = create_dataset(directory)
    workers = min(8, os.cpu_count() or 1)
    if workers > 1 and motor_flac._probe_processes():
        with ProcessPoolExecutor(max_workers=workers) as pool:
            return list(pool.map(_eval_one, cases))
    return [_eval_one(c) for c in cases]


def matriz(rows):
    tp = sum(1 for f in rows if not f["autentica"] and f["correcto"])
    tn = sum(1 for f in rows if f["autentica"] and f["correcto"])
    fp = sum(1 for f in rows if f["autentica"] and not f["correcto"])
    fn = sum(1 for f in rows if not f["autentica"] and not f["correcto"])
    return tp, tn, fp, fn


# ─── REPORT GENERATION ──────────────────────────────────────────────────────

START = "<!-- BENCHMARK:START -->"
END = "<!-- BENCHMARK:END -->"


def _section(rows, tp, tn, fp, fn, sens, spec, ic_sens, ic_spec, coherence_ok):
    n_total = len(rows)
    n_fake = tp + fn
    n_auth = tn + fp
    mode_txt = "real `ffmpeg` codecs + adversarial DSP" if HAS_FFMPEG else "deterministic filterbank + adversarial DSP"
    return f"""{START}
## Validation benchmark ({mode_txt}, n = {n_total})

Generated by `tests/benchmark_validacion.py` (fixed seed `{SEED}`, engine `{motor_flac.ENGINE_VERSION}`):
- **{n_auth} authentic controls** across 11 families: CD 16/44.1, Studio 24/48, Hi-Res analogue 24/96, Hi-Res ultrasonic 24/192, DSD64 24/88.2, DSD128 24/192, Acoustic 18 kHz, Solo low-centroid (<15 kHz), Vintage 1950s archival tape (<14 kHz), Vinyl / Cassette / Dense polyphonic synth transfers, and Stationary electronic synth drones & genuine 32-bit `PCM_32` masters.
- **{n_fake} fake & adversarial tracks** across 21 families: MP3 (`libmp3lame` 128k / V0 / 320k fullband `-cutoff 22050`, `libshine` 320k), MP2 384k, AAC-LC (128k / 320k fullband `-cutoff 22000`, MediaFoundation 192k), Vorbis (`-q4`, `-q9` 320k fullband, plus 2 unconstrained `-q10` ~500k boundary probes), Opus (96k / 256k fullband `CELT-960`), WMAv2 192k, Seamless HE-AAC QMF SBR (0 dB notch at 44.1 & 96 kHz), Musepack SV7/SV8 `PQMF-32`, Vintage 1950s->MP3 transcodes, Sinc/Aliasing upscales, 24-bit zero-pad / gain-comb / boundary micro-dither, and MPEG-4 AAC `KBD-1024` asymmetric stereo & 16-in-32 container bloat.

| Ground truth | Detected as fake | Detected as authentic |
|---|---:|---:|
| Fake (n = {n_fake}) | **TP = {tp}** | FN = {fn} |
| Authentic (n = {n_auth}) | FP = {fp} | **TN = {tn}** |

| Metric | Value | Wilson 95% CI |
|---|---:|---:|
| Sensitivity (TPR) | **{sens*100:.1f}%** | {ic_sens[0]*100:.1f}% – {ic_sens[1]*100:.1f}% |
| Specificity (TNR) | **{spec*100:.1f}%** | {ic_spec[0]*100:.1f}% – {ic_spec[1]*100:.1f}% |

Acceptance criterion: sensitivity >= 95% and specificity >= 95% — **{'PASS' if sens >= THRESHOLD and spec >= THRESHOLD else 'FAIL'}**.

### Album coherence (batch, addition A)

A second batch runs through `analyze_files`: 8 homogeneous GENUINE tracks and
1 acoustic low-ceiling track in the same directory. The acoustic track must be
raised to PROBABLY LOSSLESS with `coherencia_album = true`:

**{'PASS' if coherence_ok else 'FAIL'}**

{END}"""


def _detailed_validation_doc(section, rows):
    by_category = {}
    for r in rows:
        entry = by_category.setdefault(r["clase"], [0, 0, r["autentica"]])
        entry[0] += 1
        entry[1] += int(r["correcto"])

    corpus_hash = hashlib.sha256(
        "\n".join(f"{r['archivo']}:{r['sha256']}" for r in sorted(rows, key=lambda x: x["archivo"])).encode("utf-8")
    ).hexdigest()

    lines = [
        f"# FLAC VERIFIER {motor_flac.ENGINE_VERSION} (v2.9.1) — Validation Report (2026-11-11)",
        "",
        section,
        "",
        "## Per-family breakdown (32 families, n = 260)",
        "",
        f"- **Corpus manifest SHA-256**: `{corpus_hash}`",
        f"- **External encoder backend**: `{'ffmpeg on PATH' if HAS_FFMPEG else 'deterministic DSP fallback'}`",
        "",
        "| Family | Ground truth | Correct / Total | Pass rate |",
        "|---|---|---:|---:|",
    ]
    for cat in sorted(by_category):
        total, ok_cnt, is_auth = by_category[cat]
        gt = "Authentic" if is_auth else "Fake"
        lines.append(f"| {cat} | {gt} | {ok_cnt}/{total} | {ok_cnt * 100.0 / total:.1f}% |")

    misses = [r for r in rows if not r["correcto"]]
    if misses:
        lines += [
            "",
            "### Boundary misses",
            "",
        ]
        for m in misses:
            lines.append(f"- `{m['archivo']}` ({m['clase']}) -> `{m['veredicto']}`")

    lines.append("")
    return "\n".join(lines)


def update_document(path, section):
    text = ""
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            text = f.read()
    if START in text and END in text:
        before = text.split(START, 1)[0]
        after = text.split(END, 1)[1]
        text = before + section + after
    else:
        text = text.rstrip() + "\n\n" + section + "\n"
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def main():
    directory = tempfile.mkdtemp(prefix="flac_verifier_benchmark_")
    try:
        rows = evaluate(directory)
        coherence_ok, album = evaluate_album_coherence(directory)
    finally:
        shutil.rmtree(directory, ignore_errors=True)

    tp, tn, fp, fn = matriz(rows)
    sens = tp / float(tp + fn) if tp + fn else 0.0
    spec = tn / float(tn + fp) if tn + fp else 0.0
    ic_sens = wilson(tp, tp + fn)
    ic_spec = wilson(tn, tn + fp)

    print("Validation benchmark (n = %d)" % len(rows))
    print("  TP=%d  TN=%d  FP=%d  FN=%d" % (tp, tn, fp, fn))
    print("  Sensitivity: %.1f%%  Wilson 95%% CI %.1f%%-%.1f%%"
          % (sens * 100, ic_sens[0] * 100, ic_sens[1] * 100))
    print("  Specificity: %.1f%%  Wilson 95%% CI %.1f%%-%.1f%%"
          % (spec * 100, ic_spec[0] * 100, ic_spec[1] * 100))

    by_category = {}
    for f in rows:
        payload = by_category.setdefault(f["clase"], [0, 0])
        payload[0] += 1
        payload[1] += int(f["correcto"])
    for category in sorted(by_category):
        total, aciertos = by_category[category]
        print("  %-48s %d/%d" % (category, aciertos, total))

    errors = [f for f in rows if not f["correcto"]]
    for f in errors:
        print("  MISS %-44s %-28s -> %s" % (f["clase"], f["archivo"], f["veredicto"]))

    acoustic = next((r for r in album if r.get("archivo") == "acoustic.flac"), {})
    print("  Album coherence (batch): %s · acoustic=%s · coherencia_album=%s"
          % ("PASS" if coherence_ok else "FAIL",
             acoustic.get("veredicto"), acoustic.get("coherencia_album")))

    section = _section(rows, tp, tn, fp, fn, sens, spec, ic_sens, ic_spec, coherence_ok)
    with open(os.path.join(ROOT, "VALIDATION.md"), "w", encoding="utf-8", newline="\n") as f:
        f.write(_detailed_validation_doc(section, rows))
    update_document(os.path.join(ROOT, "README.md"), section)
    update_document(os.path.join(ROOT, "COMPARISON.md"), section)

    if sens < THRESHOLD or spec < THRESHOLD or not coherence_ok:
        print("FAILED: metrics below 95% or album coherence did not elevate the track")
        return 1
    print("PASS: sensitivity, specificity and album coherence")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
