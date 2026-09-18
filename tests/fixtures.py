"""Fixtures and utilities shared by the test suite.

Derived from the audit scripts (_audit_flac/sondas*.py): it builds synthetic
FLAC files and corrupt variants, and runs the engine as a subprocess to
validate the JSON protocol.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

import numpy as np
import soundfile as sf

RAIZ  = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # .../FLAC VERIFIER
MOTOR = os.path.join(RAIZ, "motor_flac.py")
CLI   = os.path.join(RAIZ, "verificar_flac.py")

if RAIZ not in sys.path:
    sys.path.insert(0, RAIZ)


# ─── SIGNALS ─────────────────────────────────────────────────────────────────

def ruido_banda_completa(sr=44100, dur=2.0, seed=1):
    """Stereo pink noise: content up to near Nyquist (a 'real' lossless file)."""
    r = np.random.default_rng(seed)
    n = int(sr * dur)
    blanco = r.standard_normal(n)
    espectro = np.fft.rfft(blanco)
    freqs = np.fft.rfftfreq(n, 1 / sr)
    espectro[1:] /= np.sqrt(np.maximum(freqs[1:], 1.0))       # 1/f
    x = np.fft.irfft(espectro, n)
    x *= 0.6 / np.max(np.abs(x))
    return np.stack([x, np.roll(x, 97)], axis=1)


def brickwall(audio, sr, fc):
    """Cuts the band with an abrupt filter: mimics a codec's lowpass."""
    espectro = np.fft.rfft(audio, axis=0)
    freqs = np.fft.rfftfreq(len(audio), 1 / sr)
    espectro[freqs > fc] = 0.0
    return np.fft.irfft(espectro, len(audio), axis=0)


# ─── BUILDING THE FIXTURES ───────────────────────────────────────────────────

def construir_fixtures(directorio):
    """Builds the set of test files. Returns {name: path}."""
    os.makedirs(directorio, exist_ok=True)
    rutas = {}
    audio = ruido_banda_completa()

    for nombre, subtipo in (("ok16", "PCM_16"), ("ok24", "PCM_24"),
                            ("ok8", "PCM_S8")):
        ruta = os.path.join(directorio, f"{nombre}.flac")
        sf.write(ruta, audio, 44100, subtype=subtipo)
        rutas[nombre] = ruta

    ruta = os.path.join(directorio, "silence.flac")
    sf.write(ruta, np.zeros((44100 * 3, 2)), 44100, subtype="PCM_16")
    rutas["silence"] = ruta

    # Mono: the streaming path and the array path must agree here too
    # (separacion_stereo must be None in both).
    ruta = os.path.join(directorio, "ok16_mono.flac")
    sf.write(ruta, audio[:, :1], 44100, subtype="PCM_16")
    rutas["ok16_mono"] = ruta

    # 32 kHz: Nyquist 16 kHz -> the high-band masks (f > 18 kHz) come out
    # empty. This is the case that produced separacion_stereo = NaN.
    ruta = os.path.join(directorio, "estereo32k.flac")
    x32 = ruido_banda_completa(sr=32000, seed=2)
    sf.write(ruta, x32, 32000, subtype="PCM_16")
    rutas["estereo32k"] = ruta

    # Legitimate CD master: mastered with an anti-alias filter at 20 kHz, which
    # is the case the detector wrongly accused of transcoding.
    ruta = os.path.join(directorio, "master_cd_20k.flac")
    sf.write(ruta, brickwall(audio, 44100, 20000), 44100, subtype="PCM_16")
    rutas["master_cd_20k"] = ruta

    # Clearly lossy encoding: abrupt cut-off at 16 kHz (128 kbps).
    ruta = os.path.join(directorio, "corte_16k.flac")
    sf.write(ruta, brickwall(audio, 44100, 16000), 44100, subtype="PCM_16")
    rutas["corte_16k"] = ruta

    # ── Hi-Res 96/24 ────────────────────────────────────────────────────────
    # Genuine full-band: content up to Nyquist (48 kHz).
    ruta = os.path.join(directorio, "hires96.flac")
    sf.write(ruta, ruido_banda_completa(sr=96000, dur=2.0, seed=6), 96000,
             subtype="PCM_24")
    rutas["hires96"] = ruta

    # Genuine hi-res master with an analogue roll-off at 30 kHz, like the Lavry
    # converters: spectral ceiling ~30 kHz, well below the 48 kHz Nyquist.
    # This is the case that was wrongly accused of upscaling.
    ruta = os.path.join(directorio, "hires96_rolloff30k.flac")
    sf.write(ruta, brickwall(ruido_banda_completa(sr=96000, dur=2.0, seed=7),
                             96000, 30000), 96000, subtype="PCM_24")
    rutas["hires96_rolloff30k"] = ruta

    # Fake hi-res: CD content (cut at 20 kHz) upsampled to 96 kHz.
    # It does not go above the CD limit, so it must still be detected.
    ruta = os.path.join(directorio, "upscale96.flac")
    sf.write(ruta, brickwall(ruido_banda_completa(sr=96000, dur=2.0, seed=8),
                             96000, 20000), 96000, subtype="PCM_24")
    rutas["upscale96"] = ruta

    # Depth upscale: 16 real bits inside a 24-bit container.
    audio16 = np.round(audio * 32767) / 32768.0
    ruta = os.path.join(directorio, "upscale16a24.flac")
    sf.write(ruta, audio16, 44100, subtype="PCM_24")
    rutas["upscale16a24"] = ruta

    # Corrupt variants derived from the ok16 byte stream.
    with open(rutas["ok16"], "rb") as f:
        crudo = f.read()
    variantes = {
        "basura_magic": b"fLaC" + os.urandom(4000),   # valid header, the rest is garbage
        "truncado":     crudo[: len(crudo) // 2],
        "texto":        b"this is not a flac " * 200,
        "cero_bytes":   b"",
    }
    # The STREAMINFO MD5 field lives in bytes 26-41 (after "fLaC" + the block
    # header + 8 bytes of min/max block and frame size + 8 of sample_rate...).
    buf = bytearray(crudo)
    buf[26] ^= 0xFF
    variantes["md5_obsoleto"] = bytes(buf)

    buf = bytearray(crudo)
    buf[26:42] = b"\x00" * 16                        # simulates a --no-md5 encoder
    variantes["md5_ausente"] = bytes(buf)

    for nombre, contenido in variantes.items():
        ruta = os.path.join(directorio, f"{nombre}.flac")
        with open(ruta, "wb") as f:
            f.write(contenido)
        rutas[nombre] = ruta

    return rutas


class Fixtures:
    """Temporary context with all the fixtures built only once."""
    NOMBRES_ROTOS = ("basura_magic", "truncado", "texto", "cero_bytes")

    def __init__(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="flac_verifier_")
        self.dir = self._tmp.name
        self.rutas = construir_fixtures(self.dir)

    def __getitem__(self, nombre):
        return self.rutas[nombre]

    def subcarpeta(self, nombre, archivos):
        """Copies a subset of fixtures into a folder of its own (fast batches)."""
        destino = os.path.join(self.dir, nombre)
        os.makedirs(destino, exist_ok=True)
        for archivo in archivos:
            shutil.copy(self.rutas[archivo], os.path.join(destino, f"{archivo}.flac"))
        return destino

    def cerrar(self):
        self._tmp.cleanup()


# ─── RUNNING THE ENGINE ──────────────────────────────────────────────────────

def correr_motor(ruta, modo="center", extra=()):
    """Runs the engine as a subprocess. Returns CompletedProcess (UTF-8)."""
    return subprocess.run(
        [sys.executable, MOTOR, "--path", str(ruta), "--mode", modo, *extra],
        capture_output=True, text=True, encoding="utf-8", cwd=RAIZ, timeout=600,
    )

def resultados(carpeta):
    """Runs the engine over a folder and returns (events, raw lines)."""
    p = correr_motor(carpeta)
    crudas = [l for l in p.stdout.splitlines() if l.strip()]
    return p, [json.loads(l) for l in crudas], crudas

def rechaza_constante(nombre):
    """json's parse_constant: rejects NaN/Infinity (they are not valid JSON)."""
    raise AssertionError(f"literal not allowed in JSON: {nombre}")


def transcodificar_mp3_a_flac(directorio, origen, kbps, bits=24):
    """source -> WAV -> MP3 (kbps) -> FLAC. Requires ffmpeg on the PATH.

    Reproduces the real case of an MP3 upsampled into a 24-bit FLAC container."""
    muestra = os.path.join(directorio, "_tmp.wav")
    mp3     = os.path.join(directorio, f"_tmp_{kbps}.mp3")
    destino = os.path.join(directorio, f"mp3_{kbps}_up{bits}.flac")
    data, sr = sf.read(origen, dtype="float64", always_2d=True)
    sf.write(muestra, data, sr, subtype="PCM_16")
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", muestra,
                    "-c:a", "libmp3lame", "-b:a", f"{kbps}k", mp3],
                   check=True, timeout=300)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", mp3,
                    "-c:a", "flac", "-sample_fmt", "s32", destino],
                   check=True, timeout=300)
    for temporal in (muestra, mp3):
        if os.path.exists(temporal):
            os.remove(temporal)
    return destino
