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

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if RAIZ not in sys.path:
    sys.path.insert(0, RAIZ)
import motor_flac

SEMILLA = 2026
VEREDICTOS_BUENOS = ("GENUINE LOSSLESS", "PROBABLY LOSSLESS")
VEREDICTOS_MALOS = ("SUSPICIOUS", "PROBABLE UPSCALE", "TRANSCODE")
UMBRAL = 0.95


# ─── SIGNALS ────────────────────────────────────────────────────────────────

def pista_referencia(sr, duracion=4.0, seed=2026):
    """Decorrelated pink noise + harmonics + periodic transients."""
    rng = np.random.default_rng(seed)
    n = int(sr * duracion)
    t = np.linspace(0.0, duracion, n, endpoint=False)
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


def dsd_like(sr, duracion=4.0, seed=2026):
    rng = np.random.default_rng(seed)
    n = int(sr * duracion)
    freqs = np.fft.rfftfreq(n, 1.0 / sr)
    musica = (rng.standard_normal((n // 2 + 1, 2))
              + 1j * rng.standard_normal((n // 2 + 1, 2)))
    musica[freqs > 20000.0] = 0.0
    musica = np.fft.irfft(musica, n, axis=0)
    musica /= np.max(np.abs(musica)) * 4.0

    ruido = (rng.standard_normal((n // 2 + 1, 2))
             + 1j * rng.standard_normal((n // 2 + 1, 2)))
    rampa = np.clip((freqs - 24000.0) / 16000.0, 0.0, 1.0)
    ruido *= (((freqs >= 24000.0) & (freqs <= 40000.0))[:, None]
              * (1.0 + 9.0 * rampa)[:, None])
    ruido = np.fft.irfft(ruido, n, axis=0)
    ruido /= np.max(np.abs(ruido)) * 3.0

    mezcla = musica + ruido
    return mezcla / (np.max(np.abs(mezcla)) * 1.05)


def _brickwall(x, sr, corte_hz):
    espectro = np.fft.rfft(x, axis=0)
    freqs = np.fft.rfftfreq(len(x), 1.0 / sr)
    espectro[freqs > corte_hz] = 0.0
    return np.fft.irfft(espectro, len(x), axis=0)


def _opus_fullband(x, sr):
    """libopus-like 20 kHz cliff with a frame-varying cut-off (19.6/20.0 kHz)."""
    n = len(x)
    espectro = np.fft.rfft(x, axis=0)
    freqs = np.fft.rfftfreq(n, 1.0 / sr)
    espectro[freqs > 20000.0] = 0.0
    y = np.fft.irfft(espectro, n, axis=0)
    segmento = max(512, int(sr * 0.2))
    for indice, inicio in enumerate(range(0, n, segmento)):
        fin = min(n, inicio + segmento)
        if fin - inicio < 512:
            continue
        bloque = y[inicio:fin].copy()
        if indice % 6 == 0:
            transformada = np.fft.rfft(bloque, axis=0)
            f_bloque = np.fft.rfftfreq(fin - inicio, 1.0 / sr)
            transformada[f_bloque > 19600.0] = 0.0
            bloque = np.fft.irfft(transformada, fin - inicio, axis=0)
        y[inicio:fin] = bloque
    return y / (max(1.0, np.max(np.abs(y))) * 1.1)


def _guardar(ruta, x, sr, subtipo):
    sf.write(ruta, np.clip(x, -0.99, 0.99), sr, subtype=subtipo)


# ─── DATASET ────────────────────────────────────────────────────────────────

def crear_dataset(directorio):
    """Returns [(path, authentic_bool, class_name)] with 20/20 balance."""
    casos = []
    sr = 44100

    for i in range(5):                                    # CD 16/44.1
        x = pista_referencia(sr, 4.0, 1000 + i)
        p = os.path.join(directorio, f"cd_{i}.flac")
        _guardar(p, x, sr, "PCM_16")
        casos.append((p, True, "CD 16/44.1"))

    for i in range(5):                                    # Hi-Res analogue
        x = pista_referencia(96000, 4.0, 2000 + i)
        x = signal.sosfilt(signal.butter(2, 18000, btype="low", fs=96000,
                                         output="sos"), x, axis=0)
        rng = np.random.default_rng(2100 + i)
        x = x + rng.normal(0.0, 10 ** (-104.0 / 20.0), x.shape)
        p = os.path.join(directorio, f"hires_analog_{i}.flac")
        _guardar(p, x, 96000, "PCM_24")
        casos.append((p, True, "Hi-Res analogue 24/96"))

    for i in range(5):                                    # DSD-like
        p = os.path.join(directorio, f"dsd_{i}.flac")
        _guardar(p, dsd_like(88200, 4.0, 3000 + i), 88200, "PCM_24")
        casos.append((p, True, "DSD/SACD-like 24/88.2"))

    for i in range(5):                                    # Acoustic low-HF
        x = pista_referencia(sr, 4.0, 4000 + i)
        x = signal.sosfilt(signal.butter(2, 18000, btype="low", fs=sr,
                                         output="sos"), x, axis=0)
        x /= max(1.0, np.max(np.abs(x)))
        p = os.path.join(directorio, f"acoustic_{i}.flac")
        _guardar(p, x, sr, "PCM_16")
        casos.append((p, True, "Acoustic 18 kHz roll-off"))

    for i in range(4):                                    # Sinc upscale
        x = signal.resample_poly(pista_referencia(sr, 4.0, 5000 + i),
                                 up=320, down=147, axis=0)
        p = os.path.join(directorio, f"fake_sinc_{i}.flac")
        _guardar(p, x, 96000, "PCM_24")
        casos.append((p, False, "Sinc upscale 44.1->96"))

    for i in range(4):                                    # Aliasing mirror
        x = pista_referencia(sr, 4.0, 5100 + i)
        n = len(x)
        y = np.empty((n * 2, 2), dtype=np.float64)
        y[0::2] = x
        y[1:-1:2] = 0.5 * (x[:-1] + x[1:])
        y[-1] = x[-1]
        p = os.path.join(directorio, f"fake_mirror_{i}.flac")
        _guardar(p, y, 88200, "PCM_24")
        casos.append((p, False, "Aliasing mirror 44.1->88.2"))

    for i in range(4):                                    # MP3 320 joint stereo
        x = pista_referencia(sr, 4.0, 5200 + i)
        mid = 0.5 * (x[:, 0] + x[:, 1])
        side = 0.5 * (x[:, 0] - x[:, 1])
        mid_f = signal.sosfilt(signal.ellip(10, 0.1, 80.0, 20100.0, btype="low",
                                            fs=sr, output="sos"), mid)
        side_f = signal.sosfilt(signal.ellip(10, 0.1, 80.0, 15800.0, btype="low",
                                             fs=sr, output="sos"), side)
        y = np.column_stack([mid_f + side_f, mid_f - side_f])
        p = os.path.join(directorio, f"fake_mp3_320_{i}.flac")
        _guardar(p, y, sr, "PCM_16")
        casos.append((p, False, "MP3 320 joint stereo"))

    for i in range(4):                                    # Opus fullband 20 kHz
        x = _opus_fullband(pista_referencia(sr, 4.0, 5300 + i), sr)
        p = os.path.join(directorio, f"fake_opus_{i}.flac")
        _guardar(p, x, sr, "PCM_16")
        casos.append((p, False, "Opus fullband 20 kHz"))

    for i in range(2):                                    # 16-in-24 padding
        x = np.round(pista_referencia(sr, 4.0, 5400 + i) * 32767.0) / 32768.0
        p = os.path.join(directorio, f"fake_padded_{i}.flac")
        _guardar(p, x, sr, "PCM_24")
        casos.append((p, False, "16-bit padded into 24"))

    for i in range(2):                                    # Quantisation comb
        x = np.round(pista_referencia(sr, 6.0, 5500 + i) * 32767.0) / 32768.0
        p = os.path.join(directorio, f"fake_comb_{i}.flac")
        _guardar(p, x * 0.96605, sr, "PCM_24")
        casos.append((p, False, "16-bit gain comb into 24"))

    return casos


def crear_album_coherencia(directorio):
    """8 homogeneous genuine tracks + 1 acoustic low-ceiling track (9 total)."""
    album = os.path.join(directorio, "album_coherencia")
    os.makedirs(album, exist_ok=True)
    sr = 44100
    for i in range(8):
        x = pista_referencia(sr, 4.0, 6000 + i)
        _guardar(os.path.join(album, f"genuine_{i}.flac"), x, sr, "PCM_16")
    x = pista_referencia(sr, 4.0, 6100)
    x = signal.sosfilt(signal.butter(2, 17000.0, btype="low", fs=sr,
                                     output="sos"), x, axis=0)
    x /= max(1.0, np.max(np.abs(x)))
    _guardar(os.path.join(album, "acoustic.flac"), x, sr, "PCM_16")
    return album


def evaluar_coherencia_album(directorio):
    """Runs the real batch path so `aplicar_coherencia_album` is exercised."""
    album = crear_album_coherencia(directorio)
    archivos = [os.path.join(album, nombre) for nombre in sorted(os.listdir(album))
                if nombre.endswith(".flac")]
    analisis, _ = motor_flac.analizar_archivos(
        archivos, "center", None, False, workers=1, usar_cache=False)
    resultados = list(analisis)
    acustica = next((r for r in resultados
                     if r.get("archivo") == "acoustic.flac"), None)
    ok = bool(acustica
              and acustica.get("coherencia_album")
              and acustica.get("veredicto") == "PROBABLY LOSSLESS"
              and motor_flac.NOTA_COHERENCIA_ALBUM in (acustica.get("problemas") or []))
    return ok, resultados


# ─── STATISTICS ─────────────────────────────────────────────────────────────

def wilson(k, n, z=1.96):
    """Wilson score interval for a proportion (95% by default)."""
    if n <= 0:
        return 0.0, 0.0
    p = k / float(n)
    denom = 1.0 + z * z / n
    centro = (p + z * z / (2.0 * n)) / denom
    margen = (z * math.sqrt(p * (1.0 - p) / n + z * z / (4.0 * n * n))
              / denom)
    return max(0.0, centro - margen), min(1.0, centro + margen)


def evaluar(directorio):
    casos = crear_dataset(directorio)
    filas = []
    for ruta, autentica, clase in casos:
        res = motor_flac.analizar_archivo_datos(ruta, usar_cache=False)
        veredicto = res.get("veredicto") or "indeterminate"
        if autentica:
            correcto = veredicto in VEREDICTOS_BUENOS
        else:
            correcto = veredicto in VEREDICTOS_MALOS
        filas.append({"clase": clase, "autentica": autentica,
                      "veredicto": veredicto, "correcto": correcto,
                      "archivo": os.path.basename(ruta),
                      "error": res.get("error")})
    return filas


def matriz(filas):
    tp = sum(1 for f in filas if not f["autentica"] and f["correcto"])
    tn = sum(1 for f in filas if f["autentica"] and f["correcto"])
    fp = sum(1 for f in filas if f["autentica"] and not f["correcto"])
    fn = sum(1 for f in filas if not f["autentica"] and not f["correcto"])
    return tp, tn, fp, fn


# ─── REPORT ─────────────────────────────────────────────────────────────────

INICIO = "<!-- BENCHMARK:START -->"
FIN = "<!-- BENCHMARK:END -->"


def _seccion(tp, tn, fp, fn, sens, spec, ic_sens, ic_spec, coherencia_ok):
    return f"""{INICIO}
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

Acceptance criterion: sensitivity >= 95% and specificity >= 95% — **{'PASS' if sens >= UMBRAL and spec >= UMBRAL else 'FAIL'}**.

### Album coherence (batch, addition A)

A second batch runs through `analizar_archivos`: 8 homogeneous GENUINE tracks and
1 acoustic low-ceiling track in the same directory. The acoustic track must be
raised to PROBABLY LOSSLESS with `coherencia_album = true`:

**{'PASS' if coherencia_ok else 'FAIL'}**

{FIN}"""


def actualizar_documento(ruta, seccion):
    texto = ""
    if os.path.exists(ruta):
        with open(ruta, encoding="utf-8") as f:
            texto = f.read()
    if INICIO in texto and FIN in texto:
        antes = texto.split(INICIO, 1)[0]
        despues = texto.split(FIN, 1)[1]
        texto = antes + seccion + despues
    else:
        texto = texto.rstrip() + "\n\n" + seccion + "\n"
    with open(ruta, "w", encoding="utf-8", newline="\n") as f:
        f.write(texto)


def main():
    directorio = tempfile.mkdtemp(prefix="flac_verifier_benchmark_")
    try:
        filas = evaluar(directorio)
        coherencia_ok, album = evaluar_coherencia_album(directorio)
    finally:
        shutil.rmtree(directorio, ignore_errors=True)

    tp, tn, fp, fn = matriz(filas)
    sens = tp / float(tp + fn) if tp + fn else 0.0
    spec = tn / float(tn + fp) if tn + fp else 0.0
    ic_sens = wilson(tp, tp + fn)
    ic_spec = wilson(tn, tn + fp)

    print("Validation benchmark (n = %d)" % len(filas))
    print("  TP=%d  TN=%d  FP=%d  FN=%d" % (tp, tn, fp, fn))
    print("  Sensitivity: %.1f%%  Wilson 95%% CI %.1f%%-%.1f%%"
          % (sens * 100, ic_sens[0] * 100, ic_sens[1] * 100))
    print("  Specificity: %.1f%%  Wilson 95%% CI %.1f%%-%.1f%%"
          % (spec * 100, ic_spec[0] * 100, ic_spec[1] * 100))

    por_clase = {}
    for f in filas:
        datos = por_clase.setdefault(f["clase"], [0, 0])
        datos[0] += 1
        datos[1] += int(f["correcto"])
    for clase in sorted(por_clase):
        total, aciertos = por_clase[clase]
        print("  %-32s %d/%d" % (clase, aciertos, total))

    errores = [f for f in filas if not f["correcto"]]
    for f in errores:
        print("  MISS %-32s %-20s -> %s" % (f["clase"], f["archivo"], f["veredicto"]))

    acustica = next((r for r in album if r.get("archivo") == "acoustic.flac"), {})
    print("  Album coherence (batch): %s · acoustic=%s · coherencia_album=%s"
          % ("PASS" if coherencia_ok else "FAIL",
             acustica.get("veredicto"), acustica.get("coherencia_album")))

    seccion = _seccion(tp, tn, fp, fn, sens, spec, ic_sens, ic_spec, coherencia_ok)
    with open(os.path.join(RAIZ, "VALIDATION.md"), "w", encoding="utf-8",
              newline="\n") as f:
        f.write("# FLAC VERIFIER 2.3.0 — Validation (2026-10-24)\n\n" + seccion + "\n")
    actualizar_documento(os.path.join(RAIZ, "README.md"), seccion)
    actualizar_documento(os.path.join(RAIZ, "COMPARISON.md"), seccion)

    if sens < UMBRAL or spec < UMBRAL or not coherencia_ok:
        print("FAILED: metrics below 95% or album coherence did not elevate the track")
        return 1
    print("PASS: sensitivity, specificity and album coherence")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
