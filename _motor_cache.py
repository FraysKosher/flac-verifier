"""Result cache: identity, atomic read/write and Windows retries.

Extracted from the motor facade in Phase 4. The version and the render
fingerprint are passed by the facade, so mutating `motor_flac.VERSION_MOTOR`
still invalidates the cache (the tests rely on that).
"""
import json
import math
import os
import time

from _motor_bits import _normalize_path_fs

def _sanitize(obj):
    """Replaces any non-finite float with None before serialising.
    NaN/Infinity are not valid JSON: JavaScript's JSON.parse rejects them."""
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: _sanitize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sanitize(v) for v in obj]
    return obj

PNG_FOLDER_NAME = "_spectrograms"

def _png_folder(audio_path):
    return os.path.join(os.path.dirname(os.path.abspath(audio_path)), PNG_FOLDER_NAME)

def _cache_path(audio_path, mode, seconds):
    """One entry per file AND per mode: analysing in 'center' does not erase the
    cache of the analysis in 'full', so the two can be alternated without
    recomputing."""
    key = f"{mode}{seconds if seconds else ''}"
    return os.path.join(_png_folder(audio_path), "_cache",
                        f"{os.path.basename(audio_path)}.{key}.json")

def _identity(path, meta):
    """Cheap and robust fingerprint of the file: size, date and STREAMINFO MD5."""
    try:
        info = os.stat(_normalize_path_fs(path))
        return {"tamano": info.st_size, "mtime_ns": info.st_mtime_ns,
                "md5": meta.get("md5") if isinstance(meta.get("md5"), int) else None}
    except OSError:
        return None

def _analysis_key(mode, seconds, version, schema):
    return {"motor": version, "esquema": schema,
            "modo": mode, "seconds": seconds}

def read_cache(path, meta, mode, seconds, requires_png, version, schema,
               render):
    """Returns the cached result, or None if it cannot be reused."""
    try:
        with open(_normalize_path_fs(_cache_path(path, mode, seconds)),
                  encoding="utf-8") as f:
            input = json.load(f)
    except (OSError, ValueError):
        return None                    # no cache, or an unreadable entry

    if input.get("identidad") != _identity(path, meta):
        return None
    if input.get("clave") != _analysis_key(mode, seconds, version, schema):
        return None
    result = input.get("resultado")
    if not isinstance(result, dict):
        return None

    if requires_png:
        # The PNG must exist and match the current parameters.
        png_path = (result.get("esp") or {}).get("espectrograma")
        if input.get("render") != render or not png_path:
            return None
        if not os.path.exists(png_path) or os.path.getsize(png_path) == 0:
            return None
        return result

    # No PNG was requested: the caller must not see a path that this run has not
    # generated (the cache entry does keep it for next time).
    copy = dict(result)
    copy["esp"] = dict(result.get("esp") or {})
    copy["esp"]["espectrograma"] = None
    copy["esp"].pop("espectrograma_error", None)
    return copy

def _replace_with_retries(source, destination, attempts=3, wait_time=0.015):
    """os.replace with a short retry: on Windows, another worker or the antivirus
    indexer can hold the destination open for a few milliseconds and os.replace
    then raises PermissionError. The retry does not hide the failure: after the
    last attempt the exception is raised and the caller decides."""
    for attempt in range(attempts):
        try:
            os.replace(source, destination)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(wait_time)

def write_cache(path, meta, mode, seconds, result, version, schema,
                   render):
    """Stores the result. A failure while writing cannot affect the analysis."""
    try:
        folder = _normalize_path_fs(os.path.join(_png_folder(path), "_cache"))
        os.makedirs(folder, exist_ok=True)
        input = {
            "identidad": _identity(path, meta),
            "clave":     _analysis_key(mode, seconds, version, schema),
            "render":    render,
            "resultado": result,
        }
        destination = _normalize_path_fs(_cache_path(path, mode, seconds))
        temporal = destination + ".tmp"
        with open(temporal, "w", encoding="utf-8") as f:
            json.dump(_sanitize(input), f, ensure_ascii=False)
        _replace_with_retries(temporal, destination)  # atomic, with a short retry
    except Exception:
        pass
