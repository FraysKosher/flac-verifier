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

ROOT  = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # .../FLAC VERIFIER
ENGINE = os.path.join(ROOT, "motor_flac.py")
CLI   = os.path.join(ROOT, "verificar_flac.py")

if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


# ─── SIGNALS ─────────────────────────────────────────────────────────────────

def full_band_noise(sr=44100, dur=2.0, seed=1):
    """Stereo pink noise: content up to near Nyquist (a 'real' lossless file)."""
    r = np.random.default_rng(seed)
    n = int(sr * dur)
    white = r.standard_normal(n)
    spectrum = np.fft.rfft(white)
    freqs = np.fft.rfftfreq(n, 1 / sr)
    spectrum[1:] /= np.sqrt(np.maximum(freqs[1:], 1.0))       # 1/f
    x = np.fft.irfft(spectrum, n)
    x *= 0.6 / np.max(np.abs(x))
    return np.stack([x, np.roll(x, 97)], axis=1)


def brickwall(audio, sr, fc):
    """Cuts the band with an abrupt filter: mimics a codec's lowpass."""
    spectrum = np.fft.rfft(audio, axis=0)
    freqs = np.fft.rfftfreq(len(audio), 1 / sr)
    spectrum[freqs > fc] = 0.0
    return np.fft.irfft(spectrum, len(audio), axis=0)


# ─── BUILDING THE FIXTURES ───────────────────────────────────────────────────

def build_fixtures(directory):
    """Builds the set of test files. Returns {name: path}."""
    os.makedirs(directory, exist_ok=True)
    paths = {}
    audio = full_band_noise()

    for name, subtipo in (("ok16", "PCM_16"), ("ok24", "PCM_24"),
                            ("ok8", "PCM_S8")):
        path = os.path.join(directory, f"{name}.flac")
        sf.write(path, audio, 44100, subtype=subtipo)
        paths[name] = path

    path = os.path.join(directory, "silence.flac")
    sf.write(path, np.zeros((44100 * 3, 2)), 44100, subtype="PCM_16")
    paths["silence"] = path

    # Mono: the streaming path and the array path must agree here too
    # (separacion_stereo must be None in both).
    path = os.path.join(directory, "ok16_mono.flac")
    sf.write(path, audio[:, :1], 44100, subtype="PCM_16")
    paths["ok16_mono"] = path

    # 32 kHz: Nyquist 16 kHz -> the high-band masks (f > 18 kHz) come out
    # empty. This is the case that produced separacion_stereo = NaN.
    path = os.path.join(directory, "estereo32k.flac")
    x32 = full_band_noise(sr=32000, seed=2)
    sf.write(path, x32, 32000, subtype="PCM_16")
    paths["estereo32k"] = path

    # Legitimate CD master: mastered with an anti-alias filter at 20 kHz, which
    # is the case the detector wrongly accused of transcoding.
    path = os.path.join(directory, "master_cd_20k.flac")
    sf.write(path, brickwall(audio, 44100, 20000), 44100, subtype="PCM_16")
    paths["master_cd_20k"] = path

    # Clearly lossy encoding: abrupt cut-off at 16 kHz (128 kbps).
    path = os.path.join(directory, "corte_16k.flac")
    sf.write(path, brickwall(audio, 44100, 16000), 44100, subtype="PCM_16")
    paths["corte_16k"] = path

    # ── Hi-Res 96/24 ────────────────────────────────────────────────────────
    # Genuine full-band: content up to Nyquist (48 kHz).
    path = os.path.join(directory, "hires96.flac")
    sf.write(path, full_band_noise(sr=96000, dur=2.0, seed=6), 96000,
             subtype="PCM_24")
    paths["hires96"] = path

    # Genuine hi-res master with an analogue roll-off at 30 kHz, like the Lavry
    # converters: spectral ceiling ~30 kHz, well below the 48 kHz Nyquist.
    # This is the case that was wrongly accused of upscaling.
    path = os.path.join(directory, "hires96_rolloff30k.flac")
    sf.write(path, brickwall(full_band_noise(sr=96000, dur=2.0, seed=7),
                             96000, 30000), 96000, subtype="PCM_24")
    paths["hires96_rolloff30k"] = path

    # Fake hi-res: CD content (cut at 20 kHz) upsampled to 96 kHz.
    # It does not go above the CD limit, so it must still be detected.
    path = os.path.join(directory, "upscale96.flac")
    sf.write(path, brickwall(full_band_noise(sr=96000, dur=2.0, seed=8),
                             96000, 20000), 96000, subtype="PCM_24")
    paths["upscale96"] = path

    # Depth upscale: 16 real bits inside a 24-bit container.
    audio16 = np.round(audio * 32767) / 32768.0
    path = os.path.join(directory, "upscale16a24.flac")
    sf.write(path, audio16, 44100, subtype="PCM_24")
    paths["upscale16a24"] = path

    # Corrupt variants derived from the ok16 byte stream.
    with open(paths["ok16"], "rb") as f:
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

    for name, content in variantes.items():
        path = os.path.join(directory, f"{name}.flac")
        with open(path, "wb") as f:
            f.write(content)
        paths[name] = path

    return paths


class Fixtures:
    """Temporary context with all the fixtures built only once."""
    NAMES_BROKEN = ("basura_magic", "truncado", "texto", "cero_bytes")

    def __init__(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="flac_verifier_")
        self.dir = self._tmp.name
        self.paths = build_fixtures(self.dir)

    def __getitem__(self, name):
        return self.paths[name]

    def subfolder(self, name, files):
        """Copies a subset of fixtures into a folder of its own (fast batches)."""
        destination = os.path.join(self.dir, name)
        os.makedirs(destination, exist_ok=True)
        for file in files:
            shutil.copy(self.paths[file], os.path.join(destination, f"{file}.flac"))
        return destination

    def close(self):
        self._tmp.cleanup()


# ─── RUNNING THE ENGINE ──────────────────────────────────────────────────────

def run_engine(path, mode="center", extra=()):
    """Runs the engine as a subprocess. Returns CompletedProcess (UTF-8)."""
    return subprocess.run(
        [sys.executable, ENGINE, "--path", str(path), "--mode", mode, *extra],
        capture_output=True, text=True, encoding="utf-8", cwd=ROOT, timeout=600,
    )

def results(folder):
    """Runs the engine over a folder and returns (events, raw lines)."""
    p = run_engine(folder)
    crudas = [l for l in p.stdout.splitlines() if l.strip()]
    return p, [json.loads(l) for l in crudas], crudas

def rechaza_constante(name):
    """json's parse_constant: rejects NaN/Infinity (they are not valid JSON)."""
    raise AssertionError(f"literal not allowed in JSON: {name}")


def transcode_mp3_to_flac(directory, source, kbps, bits=24):
    """source -> WAV -> MP3 (kbps) -> FLAC. Requires ffmpeg on the PATH.

    Reproduces the real case of an MP3 upsampled into a 24-bit FLAC container."""
    sample = os.path.join(directory, "_tmp.wav")
    mp3     = os.path.join(directory, f"_tmp_{kbps}.mp3")
    destination = os.path.join(directory, f"mp3_{kbps}_up{bits}.flac")
    data, sr = sf.read(source, dtype="float64", always_2d=True)
    sf.write(sample, data, sr, subtype="PCM_16")
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", sample,
                    "-c:a", "libmp3lame", "-b:a", f"{kbps}k", mp3],
                   check=True, timeout=300)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", mp3,
                    "-c:a", "flac", "-sample_fmt", "s32", destination],
                   check=True, timeout=300)
    for temporal in (sample, mp3):
        if os.path.exists(temporal):
            os.remove(temporal)
    return destination
