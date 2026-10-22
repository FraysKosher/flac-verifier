"""Result cache: identity, atomic read/write and Windows retries.

Extracted from the motor facade in Phase 4. The version and the render
fingerprint are passed by the facade, so mutating `motor_flac.VERSION_MOTOR`
still invalidates the cache (the tests rely on that).
"""
import json
import math
import os
import time

from _motor_bits import _normalizar_ruta_fs

def _sanear(obj):
    """Replaces any non-finite float with None before serialising.
    NaN/Infinity are not valid JSON: JavaScript's JSON.parse rejects them."""
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: _sanear(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sanear(v) for v in obj]
    return obj

NOMBRE_CARPETA_PNG = "_spectrograms"

def _carpeta_png(ruta_audio):
    return os.path.join(os.path.dirname(os.path.abspath(ruta_audio)), NOMBRE_CARPETA_PNG)

def _ruta_cache(ruta_audio, modo, segundos):
    """One entry per file AND per mode: analysing in 'center' does not erase the
    cache of the analysis in 'full', so the two can be alternated without
    recomputing."""
    clave = f"{modo}{segundos if segundos else ''}"
    return os.path.join(_carpeta_png(ruta_audio), "_cache",
                        f"{os.path.basename(ruta_audio)}.{clave}.json")

def _identidad(ruta, meta):
    """Cheap and robust fingerprint of the file: size, date and STREAMINFO MD5."""
    try:
        info = os.stat(_normalizar_ruta_fs(ruta))
        return {"tamano": info.st_size, "mtime_ns": info.st_mtime_ns,
                "md5": meta.get("md5") if isinstance(meta.get("md5"), int) else None}
    except OSError:
        return None

def _clave_analisis(modo, segundos, version, esquema):
    return {"motor": version, "esquema": esquema,
            "modo": modo, "seconds": segundos}

def leer_cache(ruta, meta, modo, segundos, requiere_png, version, esquema,
               render):
    """Returns the cached result, or None if it cannot be reused."""
    try:
        with open(_normalizar_ruta_fs(_ruta_cache(ruta, modo, segundos)),
                  encoding="utf-8") as f:
            entrada = json.load(f)
    except (OSError, ValueError):
        return None                    # no cache, or an unreadable entry

    if entrada.get("identidad") != _identidad(ruta, meta):
        return None
    if entrada.get("clave") != _clave_analisis(modo, segundos, version, esquema):
        return None
    resultado = entrada.get("resultado")
    if not isinstance(resultado, dict):
        return None

    if requiere_png:
        # The PNG must exist and match the current parameters.
        ruta_png = (resultado.get("esp") or {}).get("espectrograma")
        if entrada.get("render") != render or not ruta_png:
            return None
        if not os.path.exists(ruta_png) or os.path.getsize(ruta_png) == 0:
            return None
        return resultado

    # No PNG was requested: the caller must not see a path that this run has not
    # generated (the cache entry does keep it for next time).
    copia = dict(resultado)
    copia["esp"] = dict(resultado.get("esp") or {})
    copia["esp"]["espectrograma"] = None
    copia["esp"].pop("espectrograma_error", None)
    return copia

def _reemplazar_con_reintentos(origen, destino, intentos=3, espera=0.015):
    """os.replace with a short retry: on Windows, another worker or the antivirus
    indexer can hold the destination open for a few milliseconds and os.replace
    then raises PermissionError. The retry does not hide the failure: after the
    last attempt the exception is raised and the caller decides."""
    for intento in range(intentos):
        try:
            os.replace(origen, destino)
            return
        except PermissionError:
            if intento == intentos - 1:
                raise
            time.sleep(espera)

def escribir_cache(ruta, meta, modo, segundos, resultado, version, esquema,
                   render):
    """Stores the result. A failure while writing cannot affect the analysis."""
    try:
        carpeta = _normalizar_ruta_fs(os.path.join(_carpeta_png(ruta), "_cache"))
        os.makedirs(carpeta, exist_ok=True)
        entrada = {
            "identidad": _identidad(ruta, meta),
            "clave":     _clave_analisis(modo, segundos, version, esquema),
            "render":    render,
            "resultado": resultado,
        }
        destino = _normalizar_ruta_fs(_ruta_cache(ruta, modo, segundos))
        temporal = destino + ".tmp"
        with open(temporal, "w", encoding="utf-8") as f:
            json.dump(_sanear(entrada), f, ensure_ascii=False)
        _reemplazar_con_reintentos(temporal, destino)  # atomic, with a short retry
    except Exception:
        pass
