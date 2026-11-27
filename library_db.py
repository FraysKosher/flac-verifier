# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 FLAC VERIFIER Contributors
"""Persistent SQLite library store for FLAC VERIFIER v3.

Stores library root folders, discovered albums, indexed track verdicts,
user review states, and recent Quick Drop runs in a WAL-mode SQLite database
located under `%APPDATA%\\FLAC Verifier\\library.db` (or `<app>\\data\\library.db`
when `portable.flag` sits beside the executable).

Design invariants:
  - Zero audio analysis happens here: this module only indexes results already
    computed by `motor_flac.py` and preserves the raw JSON payload verbatim.
  - Windows paths are normalised consistently and deduplicated via canonical
    `*_key` columns (`os.path.normcase`) so `D:\\Music` and `d:/music/` never
    produce duplicate roots, albums, or tracks.
  - Album writes execute inside a single transaction with `busy_timeout`,
    allowing background scans of 10,000+ tracks without blocking UI reads.
  - Engine version upgrades never discard indexed results or user `reviewed`
    markers; outdated albums remain queryable for selective re-verification.
"""
from __future__ import annotations

import csv
from datetime import datetime, timezone
import json
import os
import re
import sqlite3
import sys
import tempfile
import threading
from typing import Any, Iterable

from mutagen.flac import FLAC

APP_DATA_FOLDER = "FLAC Verifier"
DB_FILENAME = "library.db"
PORTABLE_FLAG_NAME = "portable.flag"
DATA_DIR_ENV_VAR = "FLAC_VERIFIER_DATA_DIR"
SCHEMA_VERSION = 1

IGNORED_SCAN_DIRS = frozenset((
    "_spectrograms",
    "__pycache__",
    "system volume information",
    "@eadir",
    "#recycle",
))

_LOSSY_FINDING_CODES = frozenset((
    "lapped_codec_lattice",
    "artificial_cutoff",
    "opus_20k_cutoff",
    "sbr_replica",
    "mp3_sfb21_islands",
    "psychoacoustic_mod_20k",
    "joint_stereo_collapse",
    "stereo_phase_coupling",
    "cutoff_jitter",
    "mdct_spectral_holes",
    "exciter_rectification",
    "gibbs_lossy_clipping",
    "album_lossy_consistency",
))

_HIRES_UPSCALE_CODES = frozenset((
    "hires_below_cd",
    "hires_no_ultrasonic",
    "nyquist_brickwall",
    "aliasing_mirror",
    "hires_upscale_cutoff",
))

_FAKE_24BIT_CODES = frozenset((
    "padded_16_in_24",
    "dither_16_in_24",
    "empty_lsbs",
    "partial_lsbs",
    "gain_comb_24",
    "noise_shaped_16_in_24",
    "container_not_backed",
))

_AI_NEURAL_CODES = frozenset((
    "neural_vocoder_artifacts",
    "spectral_folding_bwe",
))

_TRACK_SORT_COLUMNS: dict[str, str] = {
    "num": "t.disc_number, CAST(NULLIF(t.track_number, '') AS INTEGER), t.track_number, t.filename",
    "title": "t.title COLLATE NOCASE",
    "artist": "t.artist COLLATE NOCASE",
    "album": "a.title COLLATE NOCASE",
    "verdict": "t.verdict COLLATE NOCASE",
    "score": "COALESCE(t.score, -1.0)",
    "sr": "COALESCE(t.sample_rate, 0)",
    "bits": "COALESCE(t.bit_depth, 0)",
    "dr": "COALESCE(t.dr, -1.0)",
    "ceiling": "COALESCE(t.ceiling_hz, 0.0)",
    "finding": "t.primary_finding COLLATE NOCASE",
    "updated": "t.updated_at",
}

_DRIVE_PREFIX_RE = re.compile(r"^([a-zA-Z]):([\\/]|$)")
_TRACK_NUM_RE = re.compile(r"^\s*(\d+)")
_YEAR_RE = re.compile(r"\b((?:19|20)\d{2})\b")


def utc_now_iso() -> str:
    """ISO-8601 UTC timestamp with second precision."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def natural_sort_key(path_or_name: str) -> tuple[Any, ...]:
    """Natural numeric sort key for folders and track filenames."""
    name = os.path.basename(str(path_or_name))
    parts = [
        (0, int(token), "") if token.isdecimal() else (1, 0, token.casefold())
        for token in re.split(r"(\d+)", name)
        if token
    ]
    return (parts, name.casefold(), name)


def normalize_path(path: str) -> str:
    """Normalises a filesystem path consistently across Windows and POSIX.

    Preserves readable casing while unifying separators, removing redundant
    segments, and uppercasing Windows drive letters (`d:\\Music` -> `D:\\Music`).
    """
    clean = str(path or "").strip().strip('"')
    if not clean:
        return ""
    # Strip extended-length Windows prefix so display and DB keys stay uniform.
    if clean.startswith("\\\\?\\UNC\\"):
        clean = "\\\\" + clean[8:]
    elif clean.startswith("\\\\?\\"):
        clean = clean[4:]
    normalized = os.path.normpath(os.path.abspath(clean))
    match = _DRIVE_PREFIX_RE.match(normalized)
    if match:
        normalized = match.group(1).upper() + normalized[1:]
    return normalized


def path_key(path: str, *, case_insensitive: bool | None = None) -> str:
    """Canonical deduplication key for a filesystem path.

    On Windows (or when `case_insensitive=True`), drive letter and path casing
    are folded so `D:\\Music\\Album` and `d:/music/album/` map to one row.
    """
    norm = normalize_path(path)
    if not norm:
        return ""
    fold = (os.name == "nt") if case_insensitive is None else bool(case_insensitive)
    if fold:
        return os.path.normcase(norm).lower()
    return norm


def _app_base_dir(executable_path: str | None = None) -> str:
    if executable_path:
        return os.path.dirname(os.path.abspath(executable_path))
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def is_portable_mode(executable_path: str | None = None) -> bool:
    """True when `portable.flag` sits beside the executable or app root."""
    flag_path = os.path.join(_app_base_dir(executable_path), PORTABLE_FLAG_NAME)
    return os.path.isfile(flag_path)


def resolve_data_dir(
    base_dir: str | None = None,
    *,
    executable_path: str | None = None,
) -> str:
    """Resolves the persistent data directory (`library.db`, `covers/`, settings).

    Precedence:
      1. Explicit `base_dir` argument (used by tests or callers).
      2. `FLAC_VERIFIER_DATA_DIR` environment variable.
      3. Portable `<app_dir>/data` when `<app_dir>/portable.flag` exists.
      4. Standard per-user OS data folder (`%APPDATA%\\FLAC Verifier` on Windows).
    """
    if base_dir:
        return normalize_path(base_dir)
    env_override = os.environ.get(DATA_DIR_ENV_VAR, "").strip()
    if env_override:
        return normalize_path(env_override)
    if is_portable_mode(executable_path):
        return os.path.join(_app_base_dir(executable_path), "data")
    if os.name == "nt":
        root = os.environ.get("APPDATA") or os.path.expanduser("~")
        return os.path.join(root, APP_DATA_FOLDER)
    if sys.platform == "darwin":
        return os.path.join(
            os.path.expanduser("~"), "Library", "Application Support", APP_DATA_FOLDER
        )
    xdg = os.environ.get("XDG_DATA_HOME") or os.path.join(
        os.path.expanduser("~"), ".local", "share"
    )
    return os.path.join(xdg, "flac-verifier")


def default_db_path(
    base_dir: str | None = None,
    *,
    executable_path: str | None = None,
) -> str:
    """Returns the default `library.db` path."""
    return os.path.join(
        resolve_data_dir(base_dir, executable_path=executable_path),
        DB_FILENAME,
    )


def discover_albums(root_path: str) -> list[dict[str, Any]]:
    """Recursively discovers album folders containing `.flac` files.

    Each returned item contains:
      - `folder_path`: normalised folder path
      - `folder_key`: canonical deduplication key
      - `flac_files`: naturally sorted list of normalised `.flac` file paths
    """
    norm_root = normalize_path(root_path)
    if not norm_root or not os.path.exists(norm_root):
        return []
    if os.path.isfile(norm_root):
        if not norm_root.lower().endswith(".flac"):
            return []
        parent = normalize_path(os.path.dirname(norm_root))
        return [{
            "folder_path": parent,
            "folder_key": path_key(parent),
            "flac_files": [norm_root],
        }]
    if not os.path.isdir(norm_root):
        return []

    albums: list[dict[str, Any]] = []
    for current_root, dirnames, filenames in os.walk(
        norm_root, topdown=True, followlinks=False
    ):
        dirnames[:] = sorted(
            (
                name for name in dirnames
                if not name.startswith((".", "$"))
                and name.lower() not in IGNORED_SCAN_DIRS
            ),
            key=natural_sort_key,
        )
        flacs = sorted(
            (name for name in filenames if name.lower().endswith(".flac")),
            key=natural_sort_key,
        )
        if flacs:
            folder_norm = normalize_path(current_root)
            albums.append({
                "folder_path": folder_norm,
                "folder_key": path_key(folder_norm),
                "flac_files": [
                    normalize_path(os.path.join(folder_norm, name)) for name in flacs
                ],
            })
    return albums


def _first_tag(tags: Any, *names: str) -> str:
    if not tags:
        return ""
    for name in names:
        val = tags.get(name)
        if not val:
            continue
        raw = val[0] if isinstance(val, (list, tuple)) else val
        text = str(raw or "").strip()
        if text:
            return text
    return ""


def _parse_track_number(raw: str, filename: str) -> str:
    """Extracts zero-padded track number (`01`, `12`) from tag or filename."""
    text = str(raw or "").strip()
    if text:
        head = text.split("/", 1)[0].strip()
        if head.isdigit():
            return head.zfill(2)
    match = _TRACK_NUM_RE.match(os.path.basename(filename))
    if match:
        return match.group(1).zfill(2)
    return ""


def _parse_year(raw: str) -> str:
    match = _YEAR_RE.search(str(raw or ""))
    return match.group(1) if match else ""


def _clean_title_from_filename(filename: str) -> str:
    stem = os.path.splitext(os.path.basename(filename))[0]
    cleaned = re.sub(r"^\s*\d{1,3}\s*[-._)]\s*", "", stem).strip()
    return cleaned or stem


def read_flac_tags(file_path: str) -> dict[str, str]:
    """Reads display metadata from a FLAC file's Vorbis comments safely.

    Never raises on corrupt or truncated FLAC files; falls back to filename and
    parent directory name.
    """
    filename = os.path.basename(file_path)
    folder_name = os.path.basename(os.path.dirname(normalize_path(file_path)))
    fallback_title = _clean_title_from_filename(filename)
    fallback_num = _parse_track_number("", filename)
    info = {
        "title": fallback_title,
        "artist": "",
        "album_artist": "",
        "album": folder_name or "Unknown Album",
        "year": "",
        "track_number": fallback_num,
        "disc_number": "",
    }
    try:
        audio = FLAC(file_path)
        tags = audio.tags
    except Exception:
        return info
    if not tags:
        return info

    title = _first_tag(tags, "title")
    artist = _first_tag(tags, "artist")
    album_artist = _first_tag(tags, "albumartist", "album artist")
    album = _first_tag(tags, "album")
    date_raw = _first_tag(tags, "date", "year", "originaldate")
    track_raw = _first_tag(tags, "tracknumber", "track")
    disc_raw = _first_tag(tags, "discnumber", "disc")

    if title:
        info["title"] = title
    if artist:
        info["artist"] = artist
    if album_artist:
        info["album_artist"] = album_artist
    if album:
        info["album"] = album
    info["year"] = _parse_year(date_raw)
    info["track_number"] = _parse_track_number(track_raw, filename)
    if disc_raw:
        disc_head = disc_raw.split("/", 1)[0].strip()
        info["disc_number"] = disc_head.zfill(2) if disc_head.isdigit() else disc_head
    return info


def _normalize_score_pct(raw_score: Any) -> float | None:
    """Converts engine 0..1 score to 0..100 scale for UI sorting and display."""
    if raw_score is None or isinstance(raw_score, bool):
        return None
    try:
        val = float(raw_score)
    except (TypeError, ValueError):
        return None
    if val != val:  # NaN
        return None
    if 0.0 <= val <= 1.0:
        return round(val * 100.0, 1)
    return round(max(0.0, min(100.0, val)), 1)


def _extract_finding_codes(result: dict[str, Any]) -> list[str]:
    """Extracts canonical finding codes from an engine result without re-analyzing."""
    codes: list[str] = []
    seen: set[str] = set()

    def _add(code: str) -> None:
        if code and code not in seen:
            seen.add(code)
            codes.append(code)

    for raw in result.get("finding_codes") or ():
        _add(str(raw))

    spectrum = result.get("esp") or {}
    bdi = result.get("bdi") or {}
    forensic = bdi.get("forense_bits") or {}
    md5 = result.get("md5") or {}

    if spectrum.get("mdct_lattice_detected"):
        _add("lapped_codec_lattice")
    if spectrum.get("corte_artificial"):
        _add("artificial_cutoff")
    if spectrum.get("sospecha_corte_opus_20k"):
        _add("opus_20k_cutoff")
    if spectrum.get("sbr_qmf_detectado"):
        _add("sbr_replica")
    if spectrum.get("islas_mdct_sfb21"):
        _add("mp3_sfb21_islands")
    if spectrum.get("modulacion_psicoacustica_20k"):
        _add("psychoacoustic_mod_20k")
    if spectrum.get("sospecha_joint_stereo"):
        _add("joint_stereo_collapse")
    if spectrum.get("stereo_phase_coupling"):
        _add("stereo_phase_coupling")
    if spectrum.get("sospecha_excitador_armonico"):
        _add("exciter_rectification")
    if spectrum.get("clipping_reconstruccion_lossy"):
        _add("gibbs_lossy_clipping")
    if spectrum.get("muro_brickwall_nyquist"):
        _add("nyquist_brickwall")
    if spectrum.get("espejo_aliasing_detectado"):
        _add("aliasing_mirror")
    if spectrum.get("neural_vocoder_detected"):
        _add("neural_vocoder_artifacts")
    if spectrum.get("spectral_folding_detected"):
        _add("spectral_folding_bwe")
    if spectrum.get("noise_shaped_16_in_24"):
        _add("noise_shaped_16_in_24")
    if forensic.get("es_padded_16_en_24"):
        _add("padded_16_in_24")
    if forensic.get("es_16bit_dither_en_24"):
        _add("dither_16_in_24")
    if forensic.get("es_24bit_escalado_en_peine"):
        _add("gain_comb_24")
    if bdi.get("aplica"):
        state = str(bdi.get("estado") or "").lower()
        if state == "empty":
            _add("empty_lsbs")
        elif state == "partial":
            _add("partial_lsbs")
    if md5.get("estado") == "mismatch":
        _add("corrupt_md5")
    if result.get("coherencia_album_lossy"):
        _add("album_lossy_consistency")

    return codes


def _derive_smart_tags(
    result: dict[str, Any],
    finding_codes: list[str],
    verdict_class: str,
) -> list[str]:
    """Maps an indexed engine result to sidebar Forensic View buckets."""
    code_set = set(finding_codes)
    meta = result.get("meta") or {}
    spectrum = result.get("esp") or {}
    md5 = result.get("md5") or {}
    sr = int(meta.get("sample_rate") or 0)
    ceiling = spectrum.get("techo_hz")

    tags: list[str] = []
    if code_set & _LOSSY_FINDING_CODES:
        tags.append("lossy")

    is_hires_upscaled = bool(code_set & _HIRES_UPSCALE_CODES)
    if (
        not is_hires_upscaled
        and sr > 48000
        and verdict_class in ("bad", "warn")
        and isinstance(ceiling, (int, float))
        and 0 < ceiling <= 25000
    ):
        is_hires_upscaled = True
    if is_hires_upscaled:
        tags.append("hires_upscaled")

    if code_set & _FAKE_24BIT_CODES:
        tags.append("fake_24bit")

    if code_set & _AI_NEURAL_CODES:
        tags.append("ai_neural")

    if md5.get("estado") == "mismatch" or "corrupt_md5" in code_set:
        tags.append("corrupt_md5")

    return tags


def _derive_primary_finding(
    result: dict[str, Any],
    finding_codes: list[str],
    verdict: str,
    verdict_class: str,
) -> str:
    """Short tabular label describing the primary engine finding."""
    md5 = result.get("md5") or {}
    if md5.get("estado") == "mismatch":
        return "Corrupt MD5"
    if result.get("error"):
        err_lower = str(result.get("error") or "").lower()
        if "silence" in err_lower:
            return "Digital silence"
        return "Decode error"

    spectrum = result.get("esp") or {}
    bdi = result.get("bdi") or {}
    forensic = bdi.get("forense_bits") or {}
    meta = result.get("meta") or {}
    sr = int(meta.get("sample_rate") or 0)
    bits = int(meta.get("bits_per_sample") or 0)

    if spectrum.get("neural_vocoder_detected"):
        spacing = spectrum.get("vocoder_comb_spacing_hz")
        if isinstance(spacing, (int, float)) and spacing > 0:
            return f"AI · {spacing/1000.0:.1f}k comb" if spacing >= 1000 else f"AI · {spacing:.0f} Hz comb"
        return "AI · Neural vocoder"
    if spectrum.get("spectral_folding_detected"):
        return "AI · Spectral folding"
    if spectrum.get("mdct_lattice_detected"):
        family = str(spectrum.get("mdct_lattice_family") or "MDCT")
        return family.split("(", 1)[0].strip() or family
    if spectrum.get("sbr_qmf_detectado"):
        return "HE-AAC SBR"
    if spectrum.get("muro_brickwall_nyquist"):
        ceiling = spectrum.get("techo_hz")
        if isinstance(ceiling, (int, float)) and ceiling > 0:
            return f"Brickwall {ceiling/1000.0:.1f} kHz"
        return "Nyquist brickwall"
    if spectrum.get("espejo_aliasing_detectado"):
        return "Aliasing mirror"
    if forensic.get("es_padded_16_en_24"):
        return "16-in-24 padded"
    if forensic.get("es_16bit_dither_en_24"):
        return "16-in-24 dither"
    if forensic.get("es_24bit_escalado_en_peine"):
        return "24-bit gain comb"
    if spectrum.get("noise_shaped_16_in_24"):
        return "16-in-24 shaped"
    if spectrum.get("islas_mdct_sfb21"):
        return "MP3 sfb21"
    if spectrum.get("sospecha_corte_opus_20k"):
        return "20 kHz shelf"
    if spectrum.get("corte_artificial"):
        fc = spectrum.get("frecuencia_corte") or spectrum.get("techo_hz")
        if isinstance(fc, (int, float)) and fc > 0:
            return f"Cut-off {fc/1000.0:.1f} kHz"
        return "Codec cut-off"
    if spectrum.get("sospecha_joint_stereo") or spectrum.get("stereo_phase_coupling"):
        return "Stereo collapse"
    if "cutoff_jitter" in finding_codes:
        return "Cut-off jitter"
    if verdict_class == "ok":
        if bits >= 24 and sr >= 88200:
            return f"Native {bits}/{sr//1000}k"
        if sr == 44100 and bits == 16:
            return "Clean CD"
        if verdict == "PROBABLY LOSSLESS":
            return "Acoustic / clean"
        return "Clean"
    if verdict_class == "neutral":
        return "Indeterminate"
    return "Check evidence"


def _classify_verdict(result: dict[str, Any]) -> tuple[str, str]:
    """Returns `(verdict_label, verdict_class)` from an engine result dict."""
    md5 = result.get("md5") or {}
    if md5.get("estado") == "mismatch":
        return ("CORRUPT MD5", "bad")
    if result.get("error"):
        err_lower = str(result.get("error") or "").lower()
        if "silence" in err_lower:
            return ("indeterminate", "neutral")
        return ("ERROR", "bad")
    verdict = str(result.get("veredicto") or "indeterminate")
    mapping = {
        "GENUINE LOSSLESS": "ok",
        "PROBABLY LOSSLESS": "ok",
        "SUSPICIOUS": "warn",
        "PROBABLE UPSCALE": "bad",
        "indeterminate": "neutral",
    }
    return (verdict, mapping.get(verdict, "neutral"))


_CAUTIONARY_NOTE_TOKENS = (
    "partial",
    "limited",
    "below",
    "missing",
    "warning",
    "suspicious",
    "unverified",
    "borderline",
    "reduced",
    "no ultrasonic",
    "no high",
    "low ceiling",
    "low spectral",
    "inactive",
)


def _classify_note_level(text: str, verdict_class: str) -> str:
    """Reserves green (`ok`) strictly for genuinely positive confirmations."""
    low = str(text or "").lower()
    if any(tok in low for tok in _CAUTIONARY_NOTE_TOKENS):
        return "neutral"
    if verdict_class in ("bad", "warn"):
        # On flagged/suspicious tracks, keep informational notes neutral gray.
        return "neutral"
    return "ok"


def _build_evidence_items(result: dict[str, Any]) -> list[dict[str, str]]:
    """Structured evidence list for the Forensic Inspector."""
    _, verdict_class = _classify_verdict(result)
    items: list[dict[str, str]] = []
    if result.get("error"):
        items.append({"level": "bad", "text": str(result["error"])})
    for prob in result.get("problemas") or ():
        items.append({"level": "bad", "text": str(prob)})
    for note in result.get("notes") or ():
        txt = str(note)
        items.append({"level": _classify_note_level(txt, verdict_class), "text": txt})
    if not items:
        for ev in result.get("evidencias_forenses") or ():
            txt = str(ev)
            items.append({"level": _classify_note_level(txt, verdict_class), "text": txt})
    return items


def _summarize_album_coherence(results: list[dict[str, Any]]) -> str:
    """Produces a concise human-readable album summary from track results."""
    if not results:
        return ""
    rescued = sum(1 for r in results if r.get("coherencia_album"))
    convicted = sum(1 for r in results if r.get("coherencia_album_lossy"))
    bad = sum(1 for r in results if _classify_verdict(r)[1] == "bad")
    warn = sum(1 for r in results if _classify_verdict(r)[1] == "warn")
    ar_ok = sum(1 for r in results if (r.get("accuraterip") or {}).get("aplica"))
    total = len(results)

    if bad == 0 and warn == 0:
        if rescued:
            return f"Album verified · {rescued} acoustic track(s) confirmed by coherence"
        if ar_ok == total and total > 0:
            return "AccurateRip CDDA layout · all tracks verified lossless"
        return "All tracks verified lossless"
    if convicted:
        return f"Lossy album quorum · {bad} of {total} track(s) flagged"
    if bad > 0:
        return f"Mixed provenance · {bad} of {total} track(s) flagged"
    return f"{warn} of {total} track(s) marked suspicious"


_V1_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS schema_meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS library_roots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        path TEXT NOT NULL,
        path_key TEXT NOT NULL UNIQUE,
        label TEXT NOT NULL,
        added_at TEXT NOT NULL,
        last_scanned_at TEXT,
        enabled INTEGER NOT NULL DEFAULT 1
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS albums (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        root_id INTEGER REFERENCES library_roots(id) ON DELETE SET NULL,
        folder_path TEXT NOT NULL,
        folder_key TEXT NOT NULL UNIQUE,
        title TEXT NOT NULL,
        artist TEXT NOT NULL,
        year TEXT NOT NULL DEFAULT '',
        cover_hash TEXT,
        track_count INTEGER NOT NULL DEFAULT 0,
        flagged_count INTEGER NOT NULL DEFAULT 0,
        suspicious_count INTEGER NOT NULL DEFAULT 0,
        reviewed_count INTEGER NOT NULL DEFAULT 0,
        sample_rate INTEGER,
        bit_depth INTEGER,
        coherence_summary TEXT NOT NULL DEFAULT '',
        engine_version TEXT NOT NULL DEFAULT '',
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS tracks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        album_id INTEGER NOT NULL REFERENCES albums(id) ON DELETE CASCADE,
        file_path TEXT NOT NULL,
        file_key TEXT NOT NULL UNIQUE,
        filename TEXT NOT NULL,
        track_number TEXT NOT NULL DEFAULT '',
        disc_number TEXT NOT NULL DEFAULT '',
        title TEXT NOT NULL,
        artist TEXT NOT NULL DEFAULT '',
        file_size INTEGER NOT NULL DEFAULT 0,
        file_mtime REAL NOT NULL DEFAULT 0.0,
        engine_version TEXT NOT NULL DEFAULT '',
        verdict TEXT NOT NULL DEFAULT '',
        verdict_class TEXT NOT NULL DEFAULT 'neutral',
        score REAL,
        bit_depth INTEGER,
        sample_rate INTEGER,
        dr REAL,
        ceiling_hz REAL,
        md5_status TEXT NOT NULL DEFAULT '',
        accuraterip_json TEXT,
        primary_finding TEXT NOT NULL DEFAULT '',
        finding_codes_json TEXT NOT NULL DEFAULT '[]',
        smart_tags_json TEXT NOT NULL DEFAULT '[]',
        evidence_json TEXT NOT NULL DEFAULT '[]',
        raw_result_json TEXT NOT NULL DEFAULT '{}',
        spec_cache_path TEXT,
        reviewed INTEGER NOT NULL DEFAULT 0,
        reviewed_note TEXT NOT NULL DEFAULT '',
        reviewed_at TEXT,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS recent_quick_drops (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        path TEXT NOT NULL,
        path_key TEXT NOT NULL,
        label TEXT NOT NULL,
        track_count INTEGER NOT NULL DEFAULT 0,
        flagged_count INTEGER NOT NULL DEFAULT 0,
        engine_version TEXT NOT NULL DEFAULT '',
        scanned_at TEXT NOT NULL,
        summary_json TEXT NOT NULL DEFAULT '{}'
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_albums_root_id ON albums(root_id)",
    "CREATE INDEX IF NOT EXISTS idx_albums_engine_ver ON albums(engine_version)",
    "CREATE INDEX IF NOT EXISTS idx_tracks_album_id ON tracks(album_id)",
    "CREATE INDEX IF NOT EXISTS idx_tracks_verdict_class ON tracks(verdict_class)",
    "CREATE INDEX IF NOT EXISTS idx_tracks_reviewed ON tracks(reviewed)",
    "CREATE INDEX IF NOT EXISTS idx_tracks_md5 ON tracks(md5_status)",
    "CREATE INDEX IF NOT EXISTS idx_tracks_engine_ver ON tracks(engine_version)",
    "CREATE INDEX IF NOT EXISTS idx_tracks_score ON tracks(score)",
    "CREATE INDEX IF NOT EXISTS idx_quick_drops_scanned ON recent_quick_drops(scanned_at DESC)",
)


def _migrate_v1(conn: sqlite3.Connection) -> None:
    for stmt in _V1_STATEMENTS:
        conn.execute(stmt)


_MIGRATIONS: dict[int, Any] = {
    1: _migrate_v1,
}


class LibraryDB:
    """Thread-safe SQLite WAL persistent store for the v3 FLAC library."""

    def __init__(self, db_path: str | None = None) -> None:
        self.db_path = normalize_path(db_path) if db_path else default_db_path()
        folder = os.path.dirname(self.db_path)
        if folder:
            os.makedirs(folder, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = self._open_connection(self.db_path)
        self._ensure_schema()

    @staticmethod
    def _open_connection(db_path: str) -> sqlite3.Connection:
        conn = sqlite3.connect(
            db_path,
            timeout=30.0,
            isolation_level=None,  # explicit BEGIN/COMMIT transactions
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA synchronous = NORMAL;")
        conn.execute("PRAGMA busy_timeout = 10000;")
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute("PRAGMA temp_store = MEMORY;")
        return conn

    def _ensure_schema(self) -> None:
        with self._lock:
            current = self._current_schema_version()
            for version in sorted(_MIGRATIONS):
                if version <= current:
                    continue
                self._conn.execute("BEGIN IMMEDIATE")
                try:
                    _MIGRATIONS[version](self._conn)
                    self._conn.execute(
                        "INSERT INTO schema_meta(key, value) VALUES('schema_version', ?) "
                        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                        (str(version),),
                    )
                    self._conn.execute("COMMIT")
                except Exception:
                    self._conn.execute("ROLLBACK")
                    raise

    def _current_schema_version(self) -> int:
        row = self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_meta'"
        ).fetchone()
        if not row:
            return 0
        meta = self._conn.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()
        if not meta:
            return 0
        try:
            return int(meta["value"])
        except (TypeError, ValueError):
            return 0

    def schema_version(self) -> int:
        with self._lock:
            return self._current_schema_version()

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass

    def __enter__(self) -> "LibraryDB":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()

    # ── Library roots ────────────────────────────────────────────────────────

    def add_root(self, folder_path: str, label: str | None = None) -> dict[str, Any]:
        """Adds or updates a root library folder deduplicated by canonical key."""
        norm = normalize_path(folder_path)
        key = path_key(norm)
        if not norm or not key:
            raise ValueError("Root folder path cannot be empty")
        display_label = (label or os.path.basename(norm) or norm).strip()
        now = utc_now_iso()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(
                    """
                    INSERT INTO library_roots(path, path_key, label, added_at, enabled)
                    VALUES(?, ?, ?, ?, 1)
                    ON CONFLICT(path_key) DO UPDATE SET
                        path = excluded.path,
                        label = excluded.label,
                        enabled = 1
                    """,
                    (norm, key, display_label, now),
                )
                row = self._conn.execute(
                    "SELECT * FROM library_roots WHERE path_key = ?", (key,)
                ).fetchone()
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        return dict(row)

    def remove_root(self, root_id: int, *, delete_albums: bool = True) -> bool:
        """Removes a root folder and optionally its indexed albums/tracks."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                if delete_albums:
                    self._conn.execute("DELETE FROM albums WHERE root_id = ?", (root_id,))
                cur = self._conn.execute(
                    "DELETE FROM library_roots WHERE id = ?", (root_id,)
                )
                self._conn.execute("COMMIT")
                return cur.rowcount > 0
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def touch_root_scanned(self, root_id: int) -> None:
        now = utc_now_iso()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(
                    "UPDATE library_roots SET last_scanned_at = ? WHERE id = ?",
                    (now, root_id),
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def list_roots(self) -> list[dict[str, Any]]:
        """Lists all configured root folders with album/track counts and online flag."""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT
                    r.id, r.path, r.path_key, r.label, r.added_at,
                    r.last_scanned_at, r.enabled,
                    COUNT(DISTINCT a.id) AS album_count,
                    COALESCE(SUM(a.track_count), 0) AS track_count,
                    COALESCE(SUM(a.flagged_count), 0) AS flagged_count
                FROM library_roots r
                LEFT JOIN albums a ON a.root_id = r.id
                GROUP BY r.id
                ORDER BY r.added_at ASC, r.id ASC
                """
            ).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["online"] = os.path.isdir(item["path"])
            out.append(item)
        return out

    # ── Incremental scan planning ────────────────────────────────────────────

    def plan_incremental_scan(
        self,
        folder_path: str,
        flac_files: Iterable[str],
        current_engine_version: str,
        *,
        force: bool = False,
    ) -> dict[str, Any]:
        """Classifies an album folder's files against the database state.

        Returns a dict with:
          - `needs_scan`: list of normalised file paths requiring analysis
          - `up_to_date`: list of normalised file paths unchanged under current engine
          - `removed_keys`: list of `file_key` rows missing on disk
          - `album_needs_scan`: True if any file changed, disappeared, or engine upgraded
        """
        folder_key = path_key(folder_path)
        current_files = [normalize_path(p) for p in flac_files]
        current_by_key = {path_key(p): p for p in current_files}

        with self._lock:
            album_row = self._conn.execute(
                "SELECT id, engine_version FROM albums WHERE folder_key = ?",
                (folder_key,),
            ).fetchone()
            existing: dict[str, sqlite3.Row] = {}
            if album_row:
                for row in self._conn.execute(
                    "SELECT file_key, file_size, file_mtime, engine_version "
                    "FROM tracks WHERE album_id = ?",
                    (album_row["id"],),
                ).fetchall():
                    existing[row["file_key"]] = row

        needs_scan: list[str] = []
        up_to_date: list[str] = []
        for fkey, fpath in current_by_key.items():
            if force or fkey not in existing:
                needs_scan.append(fpath)
                continue
            row = existing[fkey]
            try:
                st = os.stat(fpath)
                size_ok = int(st.st_size) == int(row["file_size"])
                mtime_ok = abs(float(st.st_mtime) - float(row["file_mtime"])) <= 1e-3
            except OSError:
                size_ok = False
                mtime_ok = False
            ver_ok = str(row["engine_version"]) == str(current_engine_version)
            if size_ok and mtime_ok and ver_ok:
                up_to_date.append(fpath)
            else:
                needs_scan.append(fpath)

        removed_keys = [k for k in existing if k not in current_by_key]
        album_ver_ok = (
            album_row is not None
            and str(album_row["engine_version"]) == str(current_engine_version)
        )
        return {
            "needs_scan": needs_scan,
            "up_to_date": up_to_date,
            "removed_keys": removed_keys,
            "album_needs_scan": bool(needs_scan or removed_keys or not album_ver_ok),
        }

    # ── Atomic per-album writes ──────────────────────────────────────────────

    def upsert_album_results(
        self,
        folder_path: str,
        results: list[dict[str, Any]],
        engine_version: str,
        *,
        root_id: int | None = None,
        cover_hash: str | None = None,
        album_meta: dict[str, str] | None = None,
        prune_missing: bool = True,
        read_tags: bool = True,
    ) -> dict[str, Any]:
        """Atomically writes an album and all its track results in one transaction.

        Preserves existing user `reviewed` flags and `reviewed_note` text across
        re-scans and engine upgrades unless a track's new verdict becomes
        `GENUINE LOSSLESS`.
        """
        folder_norm = normalize_path(folder_path)
        fkey = path_key(folder_norm)
        now = utc_now_iso()

        # Gather tags outside the DB write lock so disk I/O never stalls readers.
        tags_by_key: dict[str, dict[str, str]] = {}
        stats_by_key: dict[str, tuple[int, float]] = {}
        for item in results:
            fpath = normalize_path(
                str(item.get("ruta") or os.path.join(folder_norm, str(item.get("archivo") or "")))
            )
            tkey = path_key(fpath)
            if read_tags and os.path.isfile(fpath):
                tags_by_key[tkey] = read_flac_tags(fpath)
            else:
                fname = os.path.basename(fpath or str(item.get("archivo") or ""))
                tags_by_key[tkey] = {
                    "title": _clean_title_from_filename(fname),
                    "artist": "",
                    "album_artist": "",
                    "album": os.path.basename(folder_norm) or "Unknown Album",
                    "year": "",
                    "track_number": _parse_track_number("", fname),
                    "disc_number": "",
                }
            try:
                st = os.stat(fpath)
                stats_by_key[tkey] = (int(st.st_size), float(st.st_mtime))
            except OSError:
                stats_by_key[tkey] = (
                    int(item.get("file_size") or 0),
                    float(item.get("file_mtime") or 0.0),
                )

        # Infer album title / artist / year from tags or explicit override.
        inferred_title = os.path.basename(folder_norm) or folder_norm
        inferred_artist = ""
        inferred_year = ""
        artists_seen: list[str] = []
        for tag in tags_by_key.values():
            if tag.get("album") and tag["album"] != "Unknown Album":
                inferred_title = tag["album"]
            if tag.get("album_artist") and not inferred_artist:
                inferred_artist = tag["album_artist"]
            if tag.get("artist"):
                artists_seen.append(tag["artist"])
            if tag.get("year") and not inferred_year:
                inferred_year = tag["year"]
        if not inferred_artist and artists_seen:
            unique_artists = list(dict.fromkeys(artists_seen))
            inferred_artist = unique_artists[0] if len(unique_artists) <= 2 else "Various Artists"
        if not inferred_artist:
            inferred_artist = "Unknown Artist"

        if album_meta:
            inferred_title = album_meta.get("title") or inferred_title
            inferred_artist = album_meta.get("artist") or inferred_artist
            inferred_year = album_meta.get("year") or inferred_year

        coherence_summary = _summarize_album_coherence(results)

        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                existing_album = self._conn.execute(
                    "SELECT id, root_id, cover_hash FROM albums WHERE folder_key = ?",
                    (fkey,),
                ).fetchone()
                effective_root_id = (
                    root_id
                    if root_id is not None
                    else (existing_album["root_id"] if existing_album else None)
                )
                effective_cover = (
                    cover_hash
                    if cover_hash is not None
                    else (existing_album["cover_hash"] if existing_album else None)
                )

                if existing_album is None:
                    cur = self._conn.execute(
                        """
                        INSERT INTO albums(
                            root_id, folder_path, folder_key, title, artist, year,
                            cover_hash, coherence_summary, engine_version, updated_at
                        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            effective_root_id,
                            folder_norm,
                            fkey,
                            inferred_title,
                            inferred_artist,
                            inferred_year,
                            effective_cover,
                            coherence_summary,
                            str(engine_version),
                            now,
                        ),
                    )
                    album_id = int(cur.lastrowid)
                else:
                    album_id = int(existing_album["id"])
                    self._conn.execute(
                        """
                        UPDATE albums SET
                            root_id = ?,
                            folder_path = ?,
                            title = ?,
                            artist = ?,
                            year = ?,
                            cover_hash = ?,
                            coherence_summary = ?,
                            engine_version = ?,
                            updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            effective_root_id,
                            folder_norm,
                            inferred_title,
                            inferred_artist,
                            inferred_year,
                            effective_cover,
                            coherence_summary,
                            str(engine_version),
                            now,
                            album_id,
                        ),
                    )

                existing_reviews: dict[str, tuple[int, str, str | None]] = {}
                for row in self._conn.execute(
                    "SELECT file_key, reviewed, reviewed_note, reviewed_at "
                    "FROM tracks WHERE album_id = ?",
                    (album_id,),
                ).fetchall():
                    existing_reviews[row["file_key"]] = (
                        int(row["reviewed"] or 0),
                        str(row["reviewed_note"] or ""),
                        row["reviewed_at"],
                    )

                seen_file_keys: set[str] = set()
                for item in results:
                    fpath = normalize_path(
                        str(item.get("ruta") or os.path.join(folder_norm, str(item.get("archivo") or "")))
                    )
                    tkey = path_key(fpath)
                    seen_file_keys.add(tkey)
                    fname = str(item.get("archivo") or os.path.basename(fpath))
                    tag = tags_by_key.get(tkey, {})
                    file_size, file_mtime = stats_by_key.get(tkey, (0, 0.0))

                    verdict, verdict_class = _classify_verdict(item)
                    score_pct = _normalize_score_pct(item.get("score"))
                    meta = item.get("meta") or {}
                    spectrum = item.get("esp") or {}
                    md5 = item.get("md5") or {}
                    bit_depth = meta.get("bits_per_sample")
                    sample_rate = meta.get("sample_rate")
                    dr_val = item.get("dr")
                    ceiling_hz = spectrum.get("techo_hz")
                    md5_status = str(md5.get("estado") or "")
                    ar_payload = item.get("accuraterip")
                    finding_codes = _extract_finding_codes(item)
                    smart_tags = _derive_smart_tags(item, finding_codes, verdict_class)
                    primary_finding = _derive_primary_finding(
                        item, finding_codes, verdict, verdict_class
                    )
                    evidence_items = _build_evidence_items(item)
                    spec_cache = spectrum.get("espectrograma")

                    prev_rev, prev_note, prev_rev_at = existing_reviews.get(
                        tkey, (0, "", None)
                    )
                    if verdict == "GENUINE LOSSLESS":
                        reviewed, reviewed_note, reviewed_at = 0, "", None
                    else:
                        reviewed, reviewed_note, reviewed_at = (
                            prev_rev,
                            prev_note,
                            prev_rev_at,
                        )

                    self._conn.execute(
                        """
                        INSERT INTO tracks(
                            album_id, file_path, file_key, filename, track_number,
                            disc_number, title, artist, file_size, file_mtime,
                            engine_version, verdict, verdict_class, score, bit_depth,
                            sample_rate, dr, ceiling_hz, md5_status, accuraterip_json,
                            primary_finding, finding_codes_json, smart_tags_json,
                            evidence_json, raw_result_json, spec_cache_path,
                            reviewed, reviewed_note, reviewed_at, updated_at
                        ) VALUES(
                            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                        )
                        ON CONFLICT(file_key) DO UPDATE SET
                            album_id = excluded.album_id,
                            file_path = excluded.file_path,
                            filename = excluded.filename,
                            track_number = excluded.track_number,
                            disc_number = excluded.disc_number,
                            title = excluded.title,
                            artist = excluded.artist,
                            file_size = excluded.file_size,
                            file_mtime = excluded.file_mtime,
                            engine_version = excluded.engine_version,
                            verdict = excluded.verdict,
                            verdict_class = excluded.verdict_class,
                            score = excluded.score,
                            bit_depth = excluded.bit_depth,
                            sample_rate = excluded.sample_rate,
                            dr = excluded.dr,
                            ceiling_hz = excluded.ceiling_hz,
                            md5_status = excluded.md5_status,
                            accuraterip_json = excluded.accuraterip_json,
                            primary_finding = excluded.primary_finding,
                            finding_codes_json = excluded.finding_codes_json,
                            smart_tags_json = excluded.smart_tags_json,
                            evidence_json = excluded.evidence_json,
                            raw_result_json = excluded.raw_result_json,
                            spec_cache_path = excluded.spec_cache_path,
                            reviewed = excluded.reviewed,
                            reviewed_note = excluded.reviewed_note,
                            reviewed_at = excluded.reviewed_at,
                            updated_at = excluded.updated_at
                        """,
                        (
                            album_id,
                            fpath,
                            tkey,
                            fname,
                            tag.get("track_number", ""),
                            tag.get("disc_number", ""),
                            tag.get("title") or _clean_title_from_filename(fname),
                            tag.get("artist") or inferred_artist,
                            file_size,
                            file_mtime,
                            str(engine_version),
                            verdict,
                            verdict_class,
                            score_pct,
                            int(bit_depth) if bit_depth is not None else None,
                            int(sample_rate) if sample_rate is not None else None,
                            float(dr_val) if isinstance(dr_val, (int, float)) else None,
                            float(ceiling_hz) if isinstance(ceiling_hz, (int, float)) else None,
                            md5_status,
                            json.dumps(ar_payload, ensure_ascii=False) if ar_payload is not None else None,
                            primary_finding,
                            json.dumps(finding_codes, ensure_ascii=False),
                            json.dumps(smart_tags, ensure_ascii=False),
                            json.dumps(evidence_items, ensure_ascii=False),
                            json.dumps(item, ensure_ascii=False, default=str),
                            str(spec_cache) if spec_cache else None,
                            reviewed,
                            reviewed_note,
                            reviewed_at,
                            now,
                        ),
                    )

                if prune_missing and seen_file_keys:
                    placeholders = ",".join("?" for _ in seen_file_keys)
                    self._conn.execute(
                        f"DELETE FROM tracks WHERE album_id = ? AND file_key NOT IN ({placeholders})",
                        (album_id, *seen_file_keys),
                    )

                self._recompute_album_aggregates(album_id)
                album_row = self._conn.execute(
                    "SELECT * FROM albums WHERE id = ?", (album_id,)
                ).fetchone()
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        return dict(album_row)

    def _recompute_album_aggregates(self, album_id: int) -> None:
        agg = self._conn.execute(
            """
            SELECT
                COUNT(*) AS track_count,
                SUM(CASE WHEN verdict_class = 'bad' AND reviewed = 0 THEN 1 ELSE 0 END) AS flagged_count,
                SUM(CASE WHEN verdict_class = 'warn' AND reviewed = 0 THEN 1 ELSE 0 END) AS suspicious_count,
                SUM(CASE WHEN reviewed = 1 THEN 1 ELSE 0 END) AS reviewed_count,
                MAX(sample_rate) AS sample_rate,
                MAX(bit_depth) AS bit_depth
            FROM tracks
            WHERE album_id = ?
            """,
            (album_id,),
        ).fetchone()
        self._conn.execute(
            """
            UPDATE albums SET
                track_count = ?,
                flagged_count = ?,
                suspicious_count = ?,
                reviewed_count = ?,
                sample_rate = ?,
                bit_depth = ?
            WHERE id = ?
            """,
            (
                int(agg["track_count"] or 0),
                int(agg["flagged_count"] or 0),
                int(agg["suspicious_count"] or 0),
                int(agg["reviewed_count"] or 0),
                agg["sample_rate"],
                agg["bit_depth"],
                album_id,
            ),
        )

    def update_album_cover(self, album_id: int, cover_hash: str | None) -> None:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(
                    "UPDATE albums SET cover_hash = ? WHERE id = ?",
                    (cover_hash, album_id),
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    # ── Review state ─────────────────────────────────────────────────────────

    def set_track_reviewed(
        self,
        track_id: int,
        reviewed: bool,
        note: str = "",
    ) -> dict[str, Any] | None:
        """Marks or unmarks a track as reviewed and refreshes album counters."""
        now = utc_now_iso() if reviewed else None
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT album_id FROM tracks WHERE id = ?", (track_id,)
                ).fetchone()
                if not row:
                    self._conn.execute("ROLLBACK")
                    return None
                self._conn.execute(
                    """
                    UPDATE tracks SET
                        reviewed = ?,
                        reviewed_note = ?,
                        reviewed_at = ?
                    WHERE id = ?
                    """,
                    (1 if reviewed else 0, str(note or "").strip(), now, track_id),
                )
                self._recompute_album_aggregates(int(row["album_id"]))
                updated = self._conn.execute(
                    "SELECT * FROM tracks WHERE id = ?", (track_id,)
                ).fetchone()
                self._conn.execute("COMMIT")
                return self._hydrate_track_row(updated)
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    # ── Engine version coherence ─────────────────────────────────────────────

    def count_outdated_albums(self, current_engine_version: str) -> dict[str, Any]:
        """Counts albums analyzed by a previous engine version."""
        ver = str(current_engine_version)
        with self._lock:
            row = self._conn.execute(
                """
                SELECT
                    COUNT(*) AS total_outdated,
                    SUM(CASE WHEN flagged_count > 0 OR suspicious_count > 0 THEN 1 ELSE 0 END) AS flagged_outdated
                FROM albums
                WHERE engine_version != ?
                """,
                (ver,),
            ).fetchone()
            versions = [
                r["engine_version"]
                for r in self._conn.execute(
                    "SELECT DISTINCT engine_version FROM albums "
                    "WHERE engine_version != '' AND engine_version != ? "
                    "ORDER BY engine_version ASC",
                    (ver,),
                ).fetchall()
            ]
        return {
            "total_outdated": int(row["total_outdated"] or 0),
            "flagged_outdated": int(row["flagged_outdated"] or 0),
            "versions": versions,
        }

    def list_outdated_albums(
        self,
        current_engine_version: str,
        *,
        flagged_only: bool = False,
    ) -> list[dict[str, Any]]:
        """Returns albums analyzed by an older engine version for selective re-scan."""
        ver = str(current_engine_version)
        where = "engine_version != ?"
        if flagged_only:
            where += " AND (flagged_count > 0 OR suspicious_count > 0)"
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM albums WHERE {where} ORDER BY flagged_count DESC, title COLLATE NOCASE ASC",
                (ver,),
            ).fetchall()
        return [dict(r) for r in rows]

    # ── Queries & global filters ─────────────────────────────────────────────

    def get_sidebar_counts(self) -> dict[str, int]:
        """Returns global counters for the sidebar navigation and top summary strip."""
        with self._lock:
            album_count = int(
                self._conn.execute("SELECT COUNT(*) FROM albums").fetchone()[0]
            )
            agg = self._conn.execute(
                """
                SELECT
                    COUNT(*) AS total_tracks,
                    SUM(CASE WHEN verdict_class = 'ok' THEN 1 ELSE 0 END) AS genuine,
                    SUM(CASE WHEN verdict_class = 'warn' AND reviewed = 0 THEN 1 ELSE 0 END) AS suspicious,
                    SUM(CASE WHEN verdict_class = 'bad' AND reviewed = 0 THEN 1 ELSE 0 END) AS flagged,
                    SUM(CASE WHEN verdict_class IN ('bad', 'warn') AND reviewed = 0 THEN 1 ELSE 0 END) AS needs_review,
                    SUM(CASE WHEN reviewed = 1 THEN 1 ELSE 0 END) AS reviewed,
                    SUM(CASE WHEN md5_status = 'mismatch' THEN 1 ELSE 0 END) AS corrupt_md5,
                    SUM(CASE WHEN smart_tags_json LIKE '%"lossy"%' THEN 1 ELSE 0 END) AS lossy,
                    SUM(CASE WHEN smart_tags_json LIKE '%"hires_upscaled"%' THEN 1 ELSE 0 END) AS hires_upscaled,
                    SUM(CASE WHEN smart_tags_json LIKE '%"fake_24bit"%' THEN 1 ELSE 0 END) AS fake_24bit,
                    SUM(CASE WHEN smart_tags_json LIKE '%"ai_neural"%' THEN 1 ELSE 0 END) AS ai_neural
                FROM tracks
                """
            ).fetchone()
        return {
            "albums": album_count,
            "tracks": int(agg["total_tracks"] or 0),
            "genuine": int(agg["genuine"] or 0),
            "suspicious": int(agg["suspicious"] or 0),
            "flagged": int(agg["flagged"] or 0),
            "needs_review": int(agg["needs_review"] or 0),
            "reviewed": int(agg["reviewed"] or 0),
            "corrupt_md5": int(agg["corrupt_md5"] or 0),
            "lossy": int(agg["lossy"] or 0),
            "hires_upscaled": int(agg["hires_upscaled"] or 0),
            "fake_24bit": int(agg["fake_24bit"] or 0),
            "ai_neural": int(agg["ai_neural"] or 0),
        }

    def get_album(self, album_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT a.*, r.label AS root_label
                FROM albums a
                LEFT JOIN library_roots r ON r.id = a.root_id
                WHERE a.id = ?
                """,
                (album_id,),
            ).fetchone()
        return dict(row) if row else None

    def query_albums(
        self,
        *,
        root_id: int | None = None,
        verdict_pill: str = "all",
        search: str = "",
        sort_by: str = "artist",
        sort_desc: bool = False,
    ) -> list[dict[str, Any]]:
        """Queries albums with optional root folder, verdict pill, search, and sort."""
        clauses: list[str] = []
        params: list[Any] = []
        if root_id is not None:
            clauses.append("a.root_id = ?")
            params.append(root_id)
        if verdict_pill == "ok":
            clauses.append("a.flagged_count = 0 AND a.suspicious_count = 0")
        elif verdict_pill == "warn":
            clauses.append("a.flagged_count = 0 AND a.suspicious_count > 0")
        elif verdict_pill == "bad":
            clauses.append("a.flagged_count > 0")
        query = search.strip()
        if query:
            like = f"%{query}%"
            clauses.append(
                "(a.title LIKE ? COLLATE NOCASE OR a.artist LIKE ? COLLATE NOCASE "
                "OR a.folder_path LIKE ? COLLATE NOCASE)"
            )
            params.extend((like, like, like))

        direction = "DESC" if sort_desc else "ASC"
        sort_key = str(sort_by or "artist").lower()
        if sort_key == "title":
            order_sql = f"a.title COLLATE NOCASE {direction}, a.artist COLLATE NOCASE ASC"
        elif sort_key == "flagged":
            order_sql = (
                f"a.flagged_count {direction}, a.suspicious_count {direction}, "
                "a.artist COLLATE NOCASE ASC, a.title COLLATE NOCASE ASC"
            )
        elif sort_key == "year":
            order_sql = (
                "CASE WHEN TRIM(a.year) = '' THEN 1 ELSE 0 END ASC, "
                f"a.year {direction}, a.artist COLLATE NOCASE ASC, a.title COLLATE NOCASE ASC"
            )
        elif sort_key == "format":
            order_sql = (
                f"COALESCE(a.sample_rate, 0) {direction}, "
                f"COALESCE(a.bit_depth, 0) {direction}, "
                "a.artist COLLATE NOCASE ASC, a.title COLLATE NOCASE ASC"
            )
        elif sort_key == "tracks":
            order_sql = (
                f"a.track_count {direction}, "
                "a.artist COLLATE NOCASE ASC, a.title COLLATE NOCASE ASC"
            )
        elif sort_key == "recent":
            order_sql = f"a.updated_at {direction}, a.id {direction}"
        else:
            # Default: Artist (A-Z) -> Year -> Album title (push empty artists to end).
            order_sql = (
                "CASE WHEN TRIM(a.artist) = '' THEN 1 ELSE 0 END ASC, "
                f"a.artist COLLATE NOCASE {direction}, "
                "CASE WHEN TRIM(a.year) = '' THEN 1 ELSE 0 END ASC, "
                f"a.year {direction}, a.title COLLATE NOCASE {direction}"
            )

        where_sql = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = f"""
            SELECT a.*, r.label AS root_label
            FROM albums a
            LEFT JOIN library_roots r ON r.id = a.root_id
            {where_sql}
            ORDER BY {order_sql}
        """
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def _hydrate_track_row(self, row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        for src, dst, default in (
            ("accuraterip_json", "accuraterip", None),
            ("finding_codes_json", "finding_codes", []),
            ("smart_tags_json", "smart_tags", []),
            ("evidence_json", "evidence", []),
        ):
            raw = item.pop(src, None)
            if raw:
                try:
                    item[dst] = json.loads(raw)
                except ValueError:
                    item[dst] = default
            else:
                item[dst] = default
        item["reviewed"] = bool(item.get("reviewed"))
        return item

    def get_track(self, track_id: int, *, include_raw: bool = True) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT
                    t.*,
                    a.title AS album_title,
                    a.artist AS album_artist,
                    a.year AS album_year,
                    a.cover_hash AS cover_hash,
                    a.coherence_summary AS album_coherence
                FROM tracks t
                JOIN albums a ON a.id = t.album_id
                WHERE t.id = ?
                """,
                (track_id,),
            ).fetchone()
        if not row:
            return None
        hydrated = self._hydrate_track_row(row)
        raw_json = hydrated.pop("raw_result_json", None)
        if include_raw:
            try:
                hydrated["raw_result"] = json.loads(raw_json) if raw_json else {}
            except ValueError:
                hydrated["raw_result"] = {}
        return hydrated

    def query_tracks(
        self,
        *,
        album_id: int | None = None,
        root_id: int | None = None,
        view: str = "all",
        verdict_pill: str = "all",
        search: str = "",
        sort_by: str = "score",
        sort_desc: bool = False,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Queries tracks across global views, forensic filters, search, and sorting."""
        clauses: list[str] = []
        params: list[Any] = []

        if album_id is not None:
            clauses.append("t.album_id = ?")
            params.append(album_id)
        if root_id is not None:
            clauses.append("a.root_id = ?")
            params.append(root_id)

        if view == "needs_review":
            clauses.append("t.verdict_class IN ('bad', 'warn') AND t.reviewed = 0")
        elif view == "reviewed":
            clauses.append("t.reviewed = 1")
        elif view == "corrupt_md5":
            clauses.append("t.md5_status = 'mismatch'")
        elif view in ("lossy", "hires_upscaled", "fake_24bit", "ai_neural"):
            clauses.append("t.smart_tags_json LIKE ?")
            params.append(f'%"{view}"%')

        if verdict_pill in ("ok", "warn", "bad", "neutral"):
            clauses.append("t.verdict_class = ?")
            params.append(verdict_pill)

        query = search.strip()
        if query:
            like = f"%{query}%"
            clauses.append(
                "(t.title LIKE ? COLLATE NOCASE OR t.artist LIKE ? COLLATE NOCASE "
                "OR t.filename LIKE ? COLLATE NOCASE OR t.primary_finding LIKE ? COLLATE NOCASE "
                "OR t.verdict LIKE ? COLLATE NOCASE OR a.title LIKE ? COLLATE NOCASE "
                "OR a.artist LIKE ? COLLATE NOCASE)"
            )
            params.extend((like,) * 7)

        where_sql = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        order_expr = _TRACK_SORT_COLUMNS.get(sort_by, _TRACK_SORT_COLUMNS["score"])
        direction = "DESC" if sort_desc else "ASC"
        if sort_by == "num":
            # Natural track ordering preserves ascending numeric order unless flipped.
            order_sql = (
                f"t.disc_number {direction}, "
                f"CAST(NULLIF(t.track_number, '') AS INTEGER) {direction}, "
                f"t.track_number {direction}, t.filename COLLATE NOCASE {direction}"
            )
        else:
            order_sql = f"{order_expr} {direction}, t.id ASC"

        limit_sql = ""
        if limit is not None:
            limit_sql = "LIMIT ? OFFSET ?"
            params.extend((int(limit), max(0, int(offset))))

        sql = f"""
            SELECT
                t.id, t.album_id, t.file_path, t.file_key, t.filename,
                t.track_number, t.disc_number, t.title, t.artist,
                t.file_size, t.file_mtime, t.engine_version, t.verdict,
                t.verdict_class, t.score, t.bit_depth, t.sample_rate, t.dr,
                t.ceiling_hz, t.md5_status, t.accuraterip_json,
                t.primary_finding, t.finding_codes_json, t.smart_tags_json,
                t.evidence_json, t.spec_cache_path, t.reviewed,
                t.reviewed_note, t.reviewed_at, t.updated_at,
                a.title AS album_title, a.artist AS album_artist,
                a.year AS album_year, a.cover_hash AS cover_hash,
                a.coherence_summary AS album_coherence
            FROM tracks t
            JOIN albums a ON a.id = t.album_id
            {where_sql}
            ORDER BY {order_sql}
            {limit_sql}
        """
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [self._hydrate_track_row(r) for r in rows]

    # ── Export helpers ───────────────────────────────────────────────────────

    def export_tracks_csv(self, dest_path: str, **query_kwargs: Any) -> int:
        """Atomically exports filtered tracks to a UTF-8 CSV file. Returns row count."""
        tracks = self.query_tracks(**query_kwargs)
        dest = normalize_path(dest_path)
        folder = os.path.dirname(dest)
        if folder:
            os.makedirs(folder, exist_ok=True)
        fieldnames = [
            "track_number", "title", "artist", "album_title", "verdict",
            "score", "bit_depth", "sample_rate", "dr", "ceiling_hz",
            "md5_status", "primary_finding", "reviewed", "reviewed_note",
            "engine_version", "file_path",
        ]
        fd, tmp = tempfile.mkstemp(prefix=".export-", suffix=".csv", dir=folder or ".")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
                writer = csv.DictWriter(fh, fieldnames=fieldnames)
                writer.writeheader()
                for row in tracks:
                    writer.writerow({k: row.get(k, "") for k in fieldnames})
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, dest)
            tmp = ""
        finally:
            if tmp and os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
        return len(tracks)

    def export_tracks_json(self, dest_path: str, **query_kwargs: Any) -> int:
        """Atomically exports filtered tracks to a UTF-8 JSON file. Returns row count."""
        tracks = self.query_tracks(**query_kwargs)
        dest = normalize_path(dest_path)
        folder = os.path.dirname(dest)
        if folder:
            os.makedirs(folder, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".export-", suffix=".json", dir=folder or ".")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(
                    {"exported_at": utc_now_iso(), "count": len(tracks), "tracks": tracks},
                    fh,
                    indent=2,
                    ensure_ascii=False,
                )
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, dest)
            tmp = ""
        finally:
            if tmp and os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
        return len(tracks)

    # ── Recent Quick Drops ───────────────────────────────────────────────────

    def record_quick_drop(
        self,
        target_path: str,
        *,
        label: str | None = None,
        track_count: int = 0,
        flagged_count: int = 0,
        engine_version: str = "",
        summary: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Stores a one-off Quick Drop audit without inserting it into the library."""
        norm = normalize_path(target_path)
        key = path_key(norm)
        display = (label or os.path.basename(norm) or norm).strip()
        now = utc_now_iso()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                cur = self._conn.execute(
                    """
                    INSERT INTO recent_quick_drops(
                        path, path_key, label, track_count, flagged_count,
                        engine_version, scanned_at, summary_json
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        norm,
                        key,
                        display,
                        int(track_count),
                        int(flagged_count),
                        str(engine_version),
                        now,
                        json.dumps(summary or {}, ensure_ascii=False),
                    ),
                )
                row = self._conn.execute(
                    "SELECT * FROM recent_quick_drops WHERE id = ?", (cur.lastrowid,)
                ).fetchone()
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        item = dict(row)
        item["summary"] = json.loads(item.pop("summary_json") or "{}")
        return item

    def list_recent_quick_drops(self, limit: int = 12) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM recent_quick_drops ORDER BY scanned_at DESC, id DESC LIMIT ?",
                (max(1, int(limit)),),
            ).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            try:
                item["summary"] = json.loads(item.pop("summary_json") or "{}")
            except ValueError:
                item["summary"] = {}
            out.append(item)
        return out

    def active_cover_hashes(self) -> set[str]:
        """Returns all non-null cover hashes currently referenced by albums."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT cover_hash FROM albums WHERE cover_hash IS NOT NULL AND cover_hash != ''"
            ).fetchall()
        return {str(r["cover_hash"]) for r in rows}
