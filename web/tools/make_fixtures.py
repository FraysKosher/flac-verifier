"""Generate the parity fixtures: python web/tools/make_fixtures.py <outdir>."""
import os
import subprocess
import sys
import tempfile

import numpy as np
import soundfile as sf
from mutagen.flac import FLAC

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "tests"))
from fixtures import full_band_noise  # noqa: E402

SR = 44100


def ffmpeg(*args):
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *args], check=True)


def main(out):
    os.makedirs(out, exist_ok=True)
    x = np.concatenate([full_band_noise(SR, 2.0, seed=s) for s in range(30)])
    t = np.arange(len(x)) / SR
    x = x * 0.8 + 0.1 * np.sin(2 * np.pi * 440 * t)[:, None] \
        + 0.03 * np.sin(2 * np.pi * 17000 * t)[:, None]
    x = np.clip(x, -0.99, 0.99)
    sf.write(os.path.join(out, "genuine.flac"), x, SR, subtype="PCM_16")
    with tempfile.TemporaryDirectory() as tmp:
        wav24 = os.path.join(tmp, "src24.wav")
        wav32 = os.path.join(tmp, "src32.wav")
        mp3 = os.path.join(tmp, "t.mp3")
        sf.write(wav24, x, SR, subtype="PCM_24")
        sf.write(wav32, x, SR, subtype="PCM_32")
        ffmpeg("-i", wav24, "-c:a", "libmp3lame", "-b:a", "128k", mp3)
        ffmpeg("-i", mp3, "-sample_fmt", "s16", os.path.join(out, "fake_mp3.flac"))
        ffmpeg("-i", wav32, "-c:a", "flac", "-sample_fmt", "s32",
               "-bits_per_raw_sample", "32", "-strict", "experimental",
               os.path.join(out, "flac32.flac"))
    q = np.round(x * 32767).astype(np.int32)
    sf.write(os.path.join(out, "fake_padded24.flac"), (q << 8).astype(np.int32),
             SR, subtype="PCM_24")
    y = np.concatenate([full_band_noise(96000, 2.0, seed=100 + s) for s in range(10)])
    sf.write(os.path.join(out, "genuine24_96.flac"), y, 96000, subtype="PCM_24")
    bits = FLAC(os.path.join(out, "flac32.flac")).info.bits_per_sample
    if bits != 32:
        sys.exit(f"flac32.flac is {bits}-bit, expected 32")


if __name__ == "__main__":
    main(sys.argv[1])
