"""Result cache: identity, atomic read/write and Windows retries.

Extracted from the motor facade in Phase 4. The version and the render
fingerprint are passed by the facade, so mutating `motor_flac.ENGINE_VERSION`
still invalidates the cache (the tests rely on that).
"""
from enum import Enum
import hashlib
import json
import math
import os
import sys
import threading
import time

from _motor_bits import _normalize_path_fs, retry_io


class CacheLookupStatus(str, Enum):
    FULL_HIT = "full_hit"
    METRICS_HIT_NEEDS_PNG = "metrics_hit_needs_png"
    MISS = "miss"


class SpectralMode(str, Enum):
    """Canonical spectral scanning modes shared by cache keys, PNG names, and STFT."""

    FULL = "full"
    CENTER = "center"
    SECONDS = "seconds"

    @classmethod
    def coerce(cls, raw, default=None) -> "SpectralMode":
        """Normalizes a mode enum, string, or empty/None into a valid SpectralMode."""
        if default is None:
            fallback = cls.FULL
        elif isinstance(default, cls):
            fallback = default
        else:
            fallback = cls(str(default).strip().lower())
        if raw is None:
            return fallback
        if isinstance(raw, cls):
            return raw
        text = str(raw).strip().lower()
        if not text:
            return fallback
        try:
            return cls(text)
        except ValueError as exc:
            raise ValueError(f"unknown spectral mode: {raw!r}") from exc


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
CACHE_ENV_VAR = "FLAC_VERIFIER_CACHE_DIR"


def cache_root() -> str:
    """Centralized OS user cache root so music folders stay completely untouched."""
    env_dir = os.environ.get(CACHE_ENV_VAR, "").strip()
    if env_dir:
        return os.path.abspath(env_dir)
    if sys.platform.startswith("win"):
        base = (
            os.environ.get("LOCALAPPDATA")
            or os.environ.get("APPDATA")
            or os.path.join(os.path.expanduser("~"), "AppData", "Local")
        )
        return os.path.join(base, "FLAC_Verifier", "cache")
    if sys.platform == "darwin":
        return os.path.join(
            os.path.expanduser("~"), "Library", "Caches", "FLAC_Verifier", "cache"
        )
    xdg = os.environ.get("XDG_CACHE_HOME", "").strip() or os.path.join(
        os.path.expanduser("~"), ".cache"
    )
    return os.path.join(xdg, "flac-verifier", "cache")


def _album_bucket_hash(audio_path: str) -> str:
    """Stable hash of the normalized album folder path to isolate colliding names."""
    album_dir = os.path.normcase(
        os.path.normpath(os.path.abspath(os.path.dirname(os.path.abspath(audio_path))))
    )
    return hashlib.sha256(
        album_dir.encode("utf-8", errors="surrogatepass")
    ).hexdigest()[:16]


PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _png_folder(audio_path):
    return os.path.join(cache_root(), _album_bucket_hash(audio_path))


def _png_path(audio_path, mode="full", seconds=None):
    """Mode-scoped spectrogram PNG path so partial modes do not overwrite full PNGs."""
    name = os.path.splitext(os.path.basename(audio_path))[0]
    mode_norm = SpectralMode.coerce(mode, default=SpectralMode.FULL).value
    sec_tag = f"{seconds}" if seconds not in (None, "") else ""
    if mode_norm == SpectralMode.FULL.value and not sec_tag:
        filename = f"{name}_espectrograma.png"
    else:
        filename = f"{name}.{mode_norm}{sec_tag}_espectrograma.png"
    return os.path.join(_png_folder(audio_path), filename)


def _is_valid_png_file(png_path):
    """True iff `png_path` exists, exceeds the 8-byte signature and starts with PNG_MAGIC."""
    try:
        norm_path = _normalize_path_fs(png_path)
        if os.path.getsize(norm_path) <= len(PNG_MAGIC):
            return False
        with open(norm_path, "rb") as f:
            return f.read(len(PNG_MAGIC)) == PNG_MAGIC
    except OSError:
        return False


def _cache_path(audio_path, mode, seconds):
    """One entry per file AND per mode: analysing in 'center' does not erase the
    cache of the analysis in 'full', so the two can be alternated without
    recomputing."""
    mode_norm = SpectralMode.coerce(mode, default=SpectralMode.FULL).value
    key = f"{mode_norm}{seconds if seconds else ''}"
    return os.path.join(_png_folder(audio_path), "_cache",
                        f"{os.path.basename(audio_path)}.{key}.json")

def _identity(path, meta):
    """Cheap and robust fingerprint of the file: size, date and STREAMINFO MD5."""
    try:
        info = retry_io(lambda: os.stat(_normalize_path_fs(path)))
        return {"tamano": info.st_size, "mtime_ns": info.st_mtime_ns,
                "md5": meta.get("md5") if isinstance(meta.get("md5"), int) else None}
    except OSError:
        return None

def _analysis_key(mode, seconds, version, schema):
    mode_norm = SpectralMode.coerce(mode, default=SpectralMode.FULL).value
    return {"motor": version, "esquema": schema,
            "modo": mode_norm, "seconds": seconds}

def read_cache_entry(path, meta, mode, seconds, requires_png, version, schema,
                     render):
    """Returns (CacheLookupStatus, cached_result_or_None)."""
    try:
        with open(_normalize_path_fs(_cache_path(path, mode, seconds)),
                  encoding="utf-8") as f:
            cached_entry = json.load(f)
        if not isinstance(cached_entry, dict):
            return CacheLookupStatus.MISS, None

        ident = cached_entry.get("identidad")
        key_entry = cached_entry.get("clave")
        expected_ident = _identity(path, meta)
        if (not isinstance(ident, dict) or expected_ident is None
                or ident != expected_ident):
            return CacheLookupStatus.MISS, None
        if (not isinstance(key_entry, dict)
                or key_entry != _analysis_key(mode, seconds, version, schema)):
            return CacheLookupStatus.MISS, None

        result = cached_entry.get("resultado")
        if not isinstance(result, dict):
            return CacheLookupStatus.MISS, None
        spectrum = result.get("esp")
        if not isinstance(spectrum, dict):
            return CacheLookupStatus.MISS, None
        for nested_key in ("clip", "bdi", "md5", "meta"):
            nested_val = result.get(nested_key)
            if nested_val is not None and not isinstance(nested_val, dict):
                return CacheLookupStatus.MISS, None
        for list_key in ("problemas", "notes"):
            if list_key in result and not isinstance(result.get(list_key), list):
                return CacheLookupStatus.MISS, None

        fresh_entry = dict(result)
        fresh_entry["ruta"] = path
        fresh_entry["archivo"] = os.path.basename(path)
        fresh_entry["esp"] = dict(spectrum)

        if requires_png:
            # The PNG must exist, match render parameters, and carry a valid PNG header.
            png_path = spectrum.get("espectrograma")
            if (cached_entry.get("render") == render
                    and isinstance(png_path, str) and png_path
                    and _is_valid_png_file(png_path)):
                return CacheLookupStatus.FULL_HIT, fresh_entry
            if spectrum.get("error") is None:
                fresh_entry["esp"]["espectrograma"] = None
                fresh_entry["esp"].pop("espectrograma_error", None)
                return CacheLookupStatus.METRICS_HIT_NEEDS_PNG, fresh_entry
            return CacheLookupStatus.MISS, None

        # No PNG was requested: the caller must not see a path that this run has not
        # generated (the cache entry does keep it for next time).
        fresh_entry["esp"]["espectrograma"] = None
        fresh_entry["esp"].pop("espectrograma_error", None)
        return CacheLookupStatus.FULL_HIT, fresh_entry
    except Exception:
        return CacheLookupStatus.MISS, None


def read_cache(path, meta, mode, seconds, requires_png, version, schema,
               render):
    """Returns the cached result, or None if it cannot be reused."""
    status, result = read_cache_entry(
        path, meta, mode, seconds, requires_png, version, schema, render
    )
    if status is CacheLookupStatus.FULL_HIT:
        return result
    return None

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
    temporal = None
    try:
        folder = _normalize_path_fs(os.path.join(_png_folder(path), "_cache"))
        os.makedirs(folder, exist_ok=True)
        cache_entry = {
            "identidad": _identity(path, meta),
            "clave":     _analysis_key(mode, seconds, version, schema),
            "render":    render,
            "resultado": result,
        }
        destination = _normalize_path_fs(_cache_path(path, mode, seconds))
        temporal = f"{destination}.{os.getpid()}.{threading.get_ident()}.tmp"
        with open(temporal, "w", encoding="utf-8") as f:
            json.dump(_sanitize(cache_entry), f, ensure_ascii=False)
        _replace_with_retries(temporal, destination)  # atomic, with a short retry
    except Exception:
        if temporal is not None:
            try:
                os.unlink(temporal)
            except OSError:
                pass
