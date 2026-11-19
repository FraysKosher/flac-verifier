"""Python surface called from JavaScript through Pyodide.

Wraps the unmodified engine (motor_flac) in string-in, string-out functions.
"""
import copy
import json
import os
import sys

BROWSER = sys.platform == "emscripten"

motor_flac = None
_original_iter_s32 = None
_s32_sources = {}


def setup(cache_dir):
    global motor_flac
    os.environ["FLAC_VERIFIER_CACHE_DIR"] = cache_dir
    if sys.platform == "emscripten":
        import wasm_compat  # noqa: F401
    import motor_flac as engine
    motor_flac = engine
    if BROWSER:
        install_s32_reader()
    return engine.ENGINE_VERSION


def needs_s32(path):
    try:
        from mutagen.flac import FLAC
        return FLAC(path).info.bits_per_sample == 32
    except Exception:
        return False


def set_s32_source(path, raw_path):
    if raw_path:
        _s32_sources[path] = raw_path
    else:
        _s32_sources.pop(path, None)


def _iter_blocks_s32_raw(path, meta=None, frames_by_block=None):
    """Same contract as motor_flac._iter_blocks_s32_flac, reading the
    interleaved little-endian int32 file registered with set_s32_source."""
    import numpy as np
    if frames_by_block is None:
        frames_by_block = motor_flac.FRAMES_BY_BLOCK
    raw_path = _s32_sources.get(path)
    if raw_path is None:
        raise RuntimeError("32-bit FLAC was not decoded before analysis")
    if meta is None:
        meta = motor_flac.verify_metadata(path)
    if not (meta and meta.get("valido") and meta.get("bits_per_sample") == 32):
        raise ValueError("not a 32-bit integer FLAC container")
    ch = max(1, int(meta.get("canales") or 1))
    bytes_per_block = int(frames_by_block) * ch * 4
    with open(raw_path, "rb") as handle:
        while True:
            raw = handle.read(bytes_per_block)
            n_int = (len(raw) // (4 * ch)) * ch
            if n_int <= 0:
                break
            arr = np.frombuffer(raw[: n_int * 4], dtype="<i4").reshape(-1, ch)
            yield arr.astype(np.float64) * (1.0 / 2147483648.0)


def install_s32_reader():
    global _original_iter_s32
    if _original_iter_s32 is None:
        _original_iter_s32 = motor_flac._iter_blocks_s32_flac
    motor_flac._iter_blocks_s32_flac = _iter_blocks_s32_raw


def restore_s32_reader():
    global _original_iter_s32
    if _original_iter_s32 is not None:
        motor_flac._iter_blocks_s32_flac = _original_iter_s32
        _original_iter_s32 = None


def analyze(path, mode, seconds):
    result = motor_flac.analyze_file_data(
        path, mode, seconds if mode == "seconds" else None, True, False)
    line = motor_flac._json_line(result)
    png = None
    png_path = (result.get("esp") or {}).get("espectrograma")
    if png_path and os.path.exists(png_path):
        with open(png_path, "rb") as handle:
            png = handle.read()
        os.remove(png_path)
    return line, png


def disposition(result_json):
    kind = motor_flac.classify_batch_disposition(json.loads(result_json))
    return "deferred" if kind == motor_flac.BatchDisposition.DEFERRED else "immediate"


def album_coherence(results_json):
    results = copy.deepcopy(json.loads(results_json))
    motor_flac.apply_album_coherence(results)
    return json.dumps(results)


def sort_names(names_json):
    return json.dumps(sorted(json.loads(names_json),
                             key=motor_flac.natural_sort_key))
