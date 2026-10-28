"""Reproducible validation benchmark for FLAC VERIFIER (Phase 5).

Builds 40 synthetic tracks (20 authentic, 20 fake) with fixed seeds, runs the
engine on each one, and reports the confusion matrix, sensitivity, specificity
and Wilson 95% confidence intervals. It also writes VALIDATION.md and refreshes
the benchmark section of README.md / COMPARISON.md.

    python tests/benchmark_validacion.py

Exit code 0 only if sensitivity and specificity are both >= 95%.
"""
import math
import os
import shutil
import sys
import tempfile

import numpy as np
import soundfile as sf
from scipy import signal

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
import motor_flac

SEMILLA = 2026
GOOD_VERDICTS = ("GENUINE LOSSLESS", "PROBABLY LOSSLESS")
BAD_VERDICTS = ("SUSPICIOUS", "PROBABLE UPSCALE", "TRANSCODE")
THRESHOLD = 0.95


# ─── SIGNALS ────────────────────────────────────────────────────────────────

def track_reference(sr, duration=4.0, seed=2026):
    """Decorrelated pink noise + harmonics + periodic transients."""
    rng = np.random.default_rng(seed)
    n = int(sr * duration)
    t = np.linspace(0.0, duration, n, endpoint=False)
    b, a = ([0.049922, -0.095993, 0.050612, -0.004408],
            [1.0, -2.494956, 2.017265, -0.522189])
    rosa_l = signal.lfilter(b, a, rng.standard_normal(n))
    rosa_r = signal.lfilter(b, a, rng.standard_normal(n))
    env = 0.12 + 0.88 * (np.sin(2.0 * np.pi * 2.5 * t) ** 10)
    tonal_l = 0.25 * np.sin(2.0 * np.pi * 110.0 * t) + 0.15 * np.sin(2.0 * np.pi * 440.0 * t)
    tonal_r = 0.25 * np.sin(2.0 * np.pi * 110.0 * t) + 0.15 * np.cos(2.0 * np.pi * 440.0 * t)
    est = np.column_stack([tonal_l + rosa_l * env * 0.40,
                           tonal_r + rosa_r * env * 0.40])
    return est / (np.max(np.abs(est)) * 1.12)


def dsd_like(sr, duration=4.0, seed=2026):
    rng = np.random.default_rng(seed)
    n = int(sr * duration)
    freqs = np.fft.rfftfreq(n, 1.0 / sr)
    musica = (rng.standard_normal((n // 2 + 1, 2))
              + 1j * rng.standard_normal((n // 2 + 1, 2)))
    musica[freqs > 20000.0] = 0.0
    musica = np.fft.irfft(musica, n, axis=0)
    musica /= np.max(np.abs(musica)) * 4.0

    noise = (rng.standard_normal((n // 2 + 1, 2))
             + 1j * rng.standard_normal((n // 2 + 1, 2)))
    ramp = np.clip((freqs - 24000.0) / 16000.0, 0.0, 1.0)
    noise *= (((freqs >= 24000.0) & (freqs <= 40000.0))[:, None]
              * (1.0 + 9.0 * ramp)[:, None])
    noise = np.fft.irfft(noise, n, axis=0)
    noise /= np.max(np.abs(noise)) * 3.0

    mix = musica + noise
    return mix / (np.max(np.abs(mix)) * 1.05)


def _brickwall(x, sr, cutoff_hz):
    spectrum = np.fft.rfft(x, axis=0)
    freqs = np.fft.rfftfreq(len(x), 1.0 / sr)
    spectrum[freqs > cutoff_hz] = 0.0
    return np.fft.irfft(spectrum, len(x), axis=0)


def _opus_fullband(x, sr):
    """libopus-like 20 kHz cliff with a frame-varying cut-off (19.6/20.0 kHz)."""
    n = len(x)
    spectrum = np.fft.rfft(x, axis=0)
    freqs = np.fft.rfftfreq(n, 1.0 / sr)
    spectrum[freqs > 20000.0] = 0.0
    y = np.fft.irfft(spectrum, n, axis=0)
    segmento = max(512, int(sr * 0.2))
    for index, start in enumerate(range(0, n, segmento)):
        end = min(n, start + segmento)
        if end - start < 512:
            continue
        block = y[start:end].copy()
        if index % 6 == 0:
            transformada = np.fft.rfft(block, axis=0)
            f_block = np.fft.rfftfreq(end - start, 1.0 / sr)
            transformada[f_block > 19600.0] = 0.0
            block = np.fft.irfft(transformada, end - start, axis=0)
        y[start:end] = block
    return y / (max(1.0, np.max(np.abs(y))) * 1.1)


def _save(path, x, sr, subtype):
    sf.write(path, np.clip(x, -0.99, 0.99), sr, subtype=subtype)


# ─── DATASET ────────────────────────────────────────────────────────────────

def create_dataset(directory):
    """Returns [(path, authentic_bool, class_name)] with 20/20 balance."""
    cases = []
    sr = 44100

    for i in range(5):                                    # CD 16/44.1
        x = track_reference(sr, 4.0, 1000 + i)
        p = os.path.join(directory, f"cd_{i}.flac")
        _save(p, x, sr, "PCM_16")
        cases.append((p, True, "CD 16/44.1"))

    for i in range(5):                                    # Hi-Res analogue
        x = track_reference(96000, 4.0, 2000 + i)
        x = signal.sosfilt(signal.butter(2, 18000, btype="low", fs=96000,
                                         output="sos"), x, axis=0)
        rng = np.random.default_rng(2100 + i)
        x = x + rng.normal(0.0, 10 ** (-104.0 / 20.0), x.shape)
        p = os.path.join(directory, f"hires_analog_{i}.flac")
        _save(p, x, 96000, "PCM_24")
        cases.append((p, True, "Hi-Res analogue 24/96"))

    for i in range(5):                                    # DSD-like
        p = os.path.join(directory, f"dsd_{i}.flac")
        _save(p, dsd_like(88200, 4.0, 3000 + i), 88200, "PCM_24")
        cases.append((p, True, "DSD/SACD-like 24/88.2"))

    for i in range(5):                                    # Acoustic low-HF
        x = track_reference(sr, 4.0, 4000 + i)
        x = signal.sosfilt(signal.butter(2, 18000, btype="low", fs=sr,
                                         output="sos"), x, axis=0)
        x /= max(1.0, np.max(np.abs(x)))
        p = os.path.join(directory, f"acoustic_{i}.flac")
        _save(p, x, sr, "PCM_16")
        cases.append((p, True, "Acoustic 18 kHz roll-off"))

    for i in range(4):                                    # Sinc upscale
        x = signal.resample_poly(track_reference(sr, 4.0, 5000 + i),
                                 up=320, down=147, axis=0)
        p = os.path.join(directory, f"fake_sinc_{i}.flac")
        _save(p, x, 96000, "PCM_24")
        cases.append((p, False, "Sinc upscale 44.1->96"))

    for i in range(4):                                    # Aliasing mirror
        x = track_reference(sr, 4.0, 5100 + i)
        n = len(x)
        y = np.empty((n * 2, 2), dtype=np.float64)
        y[0::2] = x
        y[1:-1:2] = 0.5 * (x[:-1] + x[1:])
        y[-1] = x[-1]
        p = os.path.join(directory, f"fake_mirror_{i}.flac")
        _save(p, y, 88200, "PCM_24")
        cases.append((p, False, "Aliasing mirror 44.1->88.2"))

    for i in range(4):                                    # MP3 320 joint stereo
        x = track_reference(sr, 4.0, 5200 + i)
        mid = 0.5 * (x[:, 0] + x[:, 1])
        side = 0.5 * (x[:, 0] - x[:, 1])
        mid_f = signal.sosfilt(signal.ellip(10, 0.1, 80.0, 20100.0, btype="low",
                                            fs=sr, output="sos"), mid)
        side_f = signal.sosfilt(signal.ellip(10, 0.1, 80.0, 15800.0, btype="low",
                                             fs=sr, output="sos"), side)
        y = np.column_stack([mid_f + side_f, mid_f - side_f])
        p = os.path.join(directory, f"fake_mp3_320_{i}.flac")
        _save(p, y, sr, "PCM_16")
        cases.append((p, False, "MP3 320 joint stereo"))

    for i in range(4):                                    # Opus fullband 20 kHz
        x = _opus_fullband(track_reference(sr, 4.0, 5300 + i), sr)
        p = os.path.join(directory, f"fake_opus_{i}.flac")
        _save(p, x, sr, "PCM_16")
        cases.append((p, False, "Opus fullband 20 kHz"))

    for i in range(2):                                    # 16-in-24 padding
        x = np.round(track_reference(sr, 4.0, 5400 + i) * 32767.0) / 32768.0
        p = os.path.join(directory, f"fake_padded_{i}.flac")
        _save(p, x, sr, "PCM_24")
        cases.append((p, False, "16-bit padded into 24"))

    for i in range(2):                                    # Quantisation comb
        x = np.round(track_reference(sr, 6.0, 5500 + i) * 32767.0) / 32768.0
        p = os.path.join(directory, f"fake_comb_{i}.flac")
        _save(p, x * 0.96605, sr, "PCM_24")
        cases.append((p, False, "16-bit gain comb into 24"))

    return cases


def create_album_coherence(directory):
    """8 homogeneous genuine tracks + 1 acoustic low-ceiling track (9 total)."""
    album = os.path.join(directory, "album_coherencia")
    os.makedirs(album, exist_ok=True)
    sr = 44100
    for i in range(8):
        x = track_reference(sr, 4.0, 6000 + i)
        _save(os.path.join(album, f"genuine_{i}.flac"), x, sr, "PCM_16")
    x = track_reference(sr, 4.0, 6100)
    x = signal.sosfilt(signal.butter(2, 17000.0, btype="low", fs=sr,
                                     output="sos"), x, axis=0)
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
    acoustic = next((r for r in results
                     if r.get("archivo") == "acoustic.flac"), None)
    ok = bool(acoustic
              and acoustic.get("coherencia_album")
              and acoustic.get("veredicto") == "PROBABLY LOSSLESS"
              and motor_flac.ALBUM_COHERENCE_NOTE in (acoustic.get("problemas") or []))
    return ok, results


# ─── STATISTICS ─────────────────────────────────────────────────────────────

def wilson(k, n, z=1.96):
    """Wilson score interval for a proportion (95% by default)."""
    if n <= 0:
        return 0.0, 0.0
    p = k / float(n)
    denom = 1.0 + z * z / n
    center = (p + z * z / (2.0 * n)) / denom
    margin = (z * math.sqrt(p * (1.0 - p) / n + z * z / (4.0 * n * n))
              / denom)
    return max(0.0, center - margin), min(1.0, center + margin)


def evaluate(directory):
    cases = create_dataset(directory)
    rows = []
    for path, autentica, category in cases:
        res = motor_flac.analyze_file_data(path, use_cache=False)
        verdict = res.get("veredicto") or "indeterminate"
        if autentica:
            correct = verdict in GOOD_VERDICTS
        else:
            correct = verdict in BAD_VERDICTS
        rows.append({"clase": category, "autentica": autentica,
                      "veredicto": verdict, "correcto": correct,
                      "archivo": os.path.basename(path),
                      "error": res.get("error")})
    return rows


def matriz(rows):
    tp = sum(1 for f in rows if not f["autentica"] and f["correcto"])
    tn = sum(1 for f in rows if f["autentica"] and f["correcto"])
    fp = sum(1 for f in rows if f["autentica"] and not f["correcto"])
    fn = sum(1 for f in rows if not f["autentica"] and not f["correcto"])
    return tp, tn, fp, fn


# ─── REPORT ─────────────────────────────────────────────────────────────────

START = "<!-- BENCHMARK:START -->"
END = "<!-- BENCHMARK:END -->"


def _section(tp, tn, fp, fn, sens, spec, ic_sens, ic_spec, coherence_ok):
    return f"""{START}
## Validation benchmark (synthetic, n = 40)

Generated by `tests/benchmark_validacion.py` with the fixed seed `2026`:
20 authentic tracks (CD 16/44.1, Hi-Res analogue 24/96, DSD-like 24/88.2 and
acoustic 18 kHz roll-off) and 20 fakes (sinc upscale, aliasing mirror, MP3 320
joint stereo, Opus fullband 20 kHz, 16-in-24 padding and gain comb).

| Ground truth | Detected as fake | Detected as authentic |
|---|---:|---:|
| Fake (n = 20) | **TP = {tp}** | FN = {fn} |
| Authentic (n = 20) | FP = {fp} | **TN = {tn}** |

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
        print("  %-32s %d/%d" % (category, aciertos, total))

    errors = [f for f in rows if not f["correcto"]]
    for f in errors:
        print("  MISS %-32s %-20s -> %s" % (f["clase"], f["archivo"], f["veredicto"]))

    acoustic = next((r for r in album if r.get("archivo") == "acoustic.flac"), {})
    print("  Album coherence (batch): %s · acoustic=%s · coherencia_album=%s"
          % ("PASS" if coherence_ok else "FAIL",
             acoustic.get("veredicto"), acoustic.get("coherencia_album")))

    section = _section(tp, tn, fp, fn, sens, spec, ic_sens, ic_spec, coherence_ok)
    with open(os.path.join(ROOT, "VALIDATION.md"), "w", encoding="utf-8",
              newline="\n") as f:
        f.write("# FLAC VERIFIER 2.3.0 — Validation (2026-10-24)\n\n" + section + "\n")
    update_document(os.path.join(ROOT, "README.md"), section)
    update_document(os.path.join(ROOT, "COMPARISON.md"), section)

    if sens < THRESHOLD or spec < THRESHOLD or not coherence_ok:
        print("FAILED: metrics below 95% or album coherence did not elevate the track")
        return 1
    print("PASS: sensitivity, specificity and album coherence")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
