"""Fixtures y utilidades compartidas por la suite de tests.

Derivado de los scripts de auditoría (_audit_flac/sondas*.py): genera archivos
FLAC sintéticos, variantes corruptas y ejecuta el motor como subproceso para
validar el protocolo JSON.
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


# ─── SEÑALES ─────────────────────────────────────────────────────────────────

def ruido_banda_completa(sr=44100, dur=2.0, seed=1):
    """Ruido rosa estéreo: contenido hasta cerca del Nyquist (lossless 'real')."""
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
    """Corta la banda con un filtro abrupto: imita el lowpass de un códec."""
    espectro = np.fft.rfft(audio, axis=0)
    freqs = np.fft.rfftfreq(len(audio), 1 / sr)
    espectro[freqs > fc] = 0.0
    return np.fft.irfft(espectro, len(audio), axis=0)


# ─── CONSTRUCCIÓN DE FIXTURES ────────────────────────────────────────────────

def construir_fixtures(directorio):
    """Crea el juego de archivos de prueba. Devuelve {nombre: ruta}."""
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

    # Mono: la vía en streaming y la vía array deben coincidir también aquí
    # (separacion_stereo debe quedar en None en las dos).
    ruta = os.path.join(directorio, "ok16_mono.flac")
    sf.write(ruta, audio[:, :1], 44100, subtype="PCM_16")
    rutas["ok16_mono"] = ruta

    # 32 kHz: Nyquist 16 kHz -> las máscaras de banda alta (f > 18 kHz) quedan
    # vacías. Es el caso que producía separacion_stereo = NaN.
    ruta = os.path.join(directorio, "estereo32k.flac")
    x32 = ruido_banda_completa(sr=32000, seed=2)
    sf.write(ruta, x32, 32000, subtype="PCM_16")
    rutas["estereo32k"] = ruta

    # Master de CD legítimo: masterizado con filtro anti-alias a 20 kHz, que es
    # el caso que el detector acusaba en falso de transcodificación.
    ruta = os.path.join(directorio, "master_cd_20k.flac")
    sf.write(ruta, brickwall(audio, 44100, 20000), 44100, subtype="PCM_16")
    rutas["master_cd_20k"] = ruta

    # Codificación con pérdida evidente: corte abrupto a 16 kHz (128 kbps).
    ruta = os.path.join(directorio, "corte_16k.flac")
    sf.write(ruta, brickwall(audio, 44100, 16000), 44100, subtype="PCM_16")
    rutas["corte_16k"] = ruta

    # ── Hi-Res 96/24 ────────────────────────────────────────────────────────
    # Genuino de banda completa: contenido hasta el Nyquist (48 kHz).
    ruta = os.path.join(directorio, "hires96.flac")
    sf.write(ruta, ruido_banda_completa(sr=96000, dur=2.0, seed=6), 96000,
             subtype="PCM_24")
    rutas["hires96"] = ruta

    # Master hi-res genuino con roll-off analógico a 30 kHz, como los
    # convertidores Lavry: techo ~30 kHz, muy por debajo del Nyquist de 48 kHz.
    # Es el caso que se acusaba en falso de upscale.
    ruta = os.path.join(directorio, "hires96_rolloff30k.flac")
    sf.write(ruta, brickwall(ruido_banda_completa(sr=96000, dur=2.0, seed=7),
                             96000, 30000), 96000, subtype="PCM_24")
    rutas["hires96_rolloff30k"] = ruta

    # Falso hi-res: contenido de CD (cortado a 20 kHz) subido a 96 kHz.
    # No supera el límite del CD, así que debe seguir detectándose.
    ruta = os.path.join(directorio, "upscale96.flac")
    sf.write(ruta, brickwall(ruido_banda_completa(sr=96000, dur=2.0, seed=8),
                             96000, 20000), 96000, subtype="PCM_24")
    rutas["upscale96"] = ruta

    # Upscale de profundidad: 16 bits reales dentro de un contenedor de 24.
    audio16 = np.round(audio * 32767) / 32768.0
    ruta = os.path.join(directorio, "upscale16a24.flac")
    sf.write(ruta, audio16, 44100, subtype="PCM_24")
    rutas["upscale16a24"] = ruta

    # Variantes corruptas derivadas del byte stream de ok16.
    with open(rutas["ok16"], "rb") as f:
        crudo = f.read()
    variantes = {
        "basura_magic": b"fLaC" + os.urandom(4000),   # cabecera válida, resto basura
        "truncado":     crudo[: len(crudo) // 2],
        "texto":        b"esto no es un flac " * 200,
        "cero_bytes":   b"",
    }
    # El campo MD5 del STREAMINFO vive en los bytes 26-41 (tras "fLaC" + cabecera
    # del bloque + 8 bytes de min/max block y frame size + 8 de sample_rate...).
    buf = bytearray(crudo)
    buf[26] ^= 0xFF
    variantes["md5_obsoleto"] = bytes(buf)

    buf = bytearray(crudo)
    buf[26:42] = b"\x00" * 16                        # simula un encoder --no-md5
    variantes["md5_ausente"] = bytes(buf)

    for nombre, contenido in variantes.items():
        ruta = os.path.join(directorio, f"{nombre}.flac")
        with open(ruta, "wb") as f:
            f.write(contenido)
        rutas[nombre] = ruta

    return rutas


class Fixtures:
    """Contexto temporal con todos los fixtures construidos una sola vez."""
    NOMBRES_ROTOS = ("basura_magic", "truncado", "texto", "cero_bytes")

    def __init__(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="flac_verifier_")
        self.dir = self._tmp.name
        self.rutas = construir_fixtures(self.dir)

    def __getitem__(self, nombre):
        return self.rutas[nombre]

    def subcarpeta(self, nombre, archivos):
        """Copia un subconjunto de fixtures a una carpeta propia (lotes rápidos)."""
        destino = os.path.join(self.dir, nombre)
        os.makedirs(destino, exist_ok=True)
        for archivo in archivos:
            shutil.copy(self.rutas[archivo], os.path.join(destino, f"{archivo}.flac"))
        return destino

    def cerrar(self):
        self._tmp.cleanup()


# ─── EJECUCIÓN DEL MOTOR ─────────────────────────────────────────────────────

def correr_motor(ruta, modo="center", extra=()):
    """Ejecuta el motor como subproceso. Devuelve CompletedProcess (UTF-8)."""
    return subprocess.run(
        [sys.executable, MOTOR, "--path", str(ruta), "--mode", modo, *extra],
        capture_output=True, text=True, encoding="utf-8", cwd=RAIZ, timeout=600,
    )

def resultados(carpeta):
    """Ejecuta el motor sobre una carpeta y devuelve (eventos, lineas_crudas)."""
    p = correr_motor(carpeta)
    crudas = [l for l in p.stdout.splitlines() if l.strip()]
    return p, [json.loads(l) for l in crudas], crudas

def rechaza_constante(nombre):
    """parse_constant de json: rechaza NaN/Infinity (no son JSON válido)."""
    raise AssertionError(f"literal no permitido en JSON: {nombre}")


def transcodificar_mp3_a_flac(directorio, origen, kbps, bits=24):
    """origen -> WAV -> MP3 (kbps) -> FLAC. Requiere ffmpeg en el PATH.

    Reproduce el caso real de un MP3 subido a un contenedor FLAC de 24 bits."""
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
