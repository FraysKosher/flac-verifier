"""Python surface called from JavaScript through Pyodide.

Wraps the unmodified engine (motor_flac) in string-in, string-out functions.
"""
import copy
import json
import os
import sys

BROWSER = sys.platform == "emscripten"

_ERROR_32BIT = "32-bit FLAC is not supported in the browser. Use the desktop app."

motor_flac = None


def setup(cache_dir):
    global motor_flac
    os.environ["FLAC_VERIFIER_CACHE_DIR"] = cache_dir
    if sys.platform == "emscripten":
        import wasm_compat  # noqa: F401
    import motor_flac as engine
    motor_flac = engine
    return engine.ENGINE_VERSION


def _is_32bit(path):
    try:
        from mutagen.flac import FLAC
        return FLAC(path).info.bits_per_sample == 32
    except Exception:
        return False


def analyze(path, mode, seconds):
    if BROWSER and _is_32bit(path):
        error = {"archivo": os.path.basename(path), "ruta": path,
                 "error": _ERROR_32BIT}
        return json.dumps(error), None
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
