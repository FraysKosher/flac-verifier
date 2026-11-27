# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 FLAC VERIFIER Contributors
"""Secure one-click auto-updater against official GitHub Releases.

Security guarantees:
  - Strict HTTPS-only URL validation against an explicit GitHub Releases
    allowlist (`github.com/FraysKosher/flac-verifier/...` and official GitHub
    release asset CDN hosts), enforced on both the initial request and every
    HTTP redirect.
  - Mandatory cryptographic SHA-256 verification against `latest.json` before
    any downloaded installer is accepted; mismatched files are immediately
    unlinked.
  - Non-blocking short-timeout checks that can be disabled completely via the
    user's `offline_mode` preference.
  - Installed builds run the per-user Inno Setup installer silently while
    preserving `%APPDATA%\\FLAC Verifier\\library.db`; portable builds never
    self-overwrite and instead open the official GitHub Release page.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
import re
import socket
import ssl
import subprocess
import tempfile
import urllib.error
from urllib.parse import urlparse
import urllib.request
from typing import Any, Callable

import library_db as ldb

GITHUB_OWNER = "FraysKosher"
GITHUB_REPO = "flac-verifier"
REPO_PATH_PREFIX = f"/{GITHUB_OWNER}/{GITHUB_REPO}/"
RELEASES_BASE_URL = f"https://github.com/{GITHUB_OWNER}/{GITHUB_REPO}/releases"
LATEST_JSON_URL = f"{RELEASES_BASE_URL}/latest/download/latest.json"
API_LATEST_RELEASE_PATH = f"/repos/{GITHUB_OWNER}/{GITHUB_REPO}/releases/latest"
GITHUB_API_LATEST_URL = f"https://api.github.com{API_LATEST_RELEASE_PATH}"

ALLOWED_DOWNLOAD_HOSTS = frozenset((
    "github.com",
    "objects.githubusercontent.com",
    "release-assets.githubusercontent.com",
    "github-releases.githubusercontent.com",
))

DEFAULT_TIMEOUT_SECONDS = 12.0
DOWNLOAD_TIMEOUT_SECONDS = 60.0
MAX_MANIFEST_BYTES = 128 * 1024
MAX_INSTALLER_BYTES = 350 * 1024 * 1024
DOWNLOAD_CHUNK_BYTES = 65536

_SEMVER_RE = re.compile(
    r"^\s*v?(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?\s*$"
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

ProgressCallback = Callable[[int, int], None]


def parse_semver(version: str) -> tuple[int, int, int, int, str]:
    """Parses a strict SemVer string into a sortable tuple.

    Final releases (`3.0.0`) sort strictly higher than prereleases (`3.0.0-rc1`).
    """
    match = _SEMVER_RE.match(str(version or ""))
    if not match:
        raise ValueError(f"Invalid semantic version: {version!r}")
    major, minor, patch = int(match.group(1)), int(match.group(2)), int(match.group(3))
    prerelease = match.group(4) or ""
    is_final = 1 if not prerelease else 0
    return (major, minor, patch, is_final, prerelease)


def is_newer_version(candidate: str, current: str) -> bool:
    """True iff `candidate` is strictly newer than `current` under SemVer."""
    try:
        return parse_semver(candidate) > parse_semver(current)
    except ValueError:
        return False


def validate_release_url(url: str) -> str:
    """Validates that `url` uses strict HTTPS on an allowed GitHub Releases host."""
    clean = str(url or "").strip()
    parsed = urlparse(clean)
    if parsed.scheme.lower() != "https":
        raise ValueError(f"Only HTTPS release URLs are permitted: {clean!r}")
    host = (parsed.hostname or "").lower()
    if host == "api.github.com":
        if parsed.path != API_LATEST_RELEASE_PATH:
            raise ValueError(f"Unauthorized GitHub API endpoint: {parsed.path!r}")
        return clean
    if host not in ALLOWED_DOWNLOAD_HOSTS:
        raise ValueError(f"Unauthorized release download host: {host!r}")
    if host == "github.com":
        if not parsed.path.startswith(REPO_PATH_PREFIX):
            raise ValueError(f"GitHub URL outside official repository: {parsed.path!r}")
    return clean


def validate_sha256_hex(digest: str) -> str:
    """Validates and normalises a 64-character lowercase SHA-256 hex digest."""
    clean = str(digest or "").strip().lower()
    if not _SHA256_RE.match(clean):
        raise ValueError("Invalid SHA-256 digest in release manifest")
    return clean


def compute_file_sha256(path: str) -> str:
    """Streams a file in 64 KB blocks and returns its lowercase SHA-256 digest."""
    hasher = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(DOWNLOAD_CHUNK_BYTES)
            if not chunk:
                break
            hasher.update(chunk)
    return hasher.hexdigest().lower()


class _StrictGitHubRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Enforces HTTPS and official host allowlist on every HTTP redirect."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_release_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _build_opener() -> urllib.request.OpenerDirector:
    ctx = ssl.create_default_context()
    return urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=ctx),
        _StrictGitHubRedirectHandler(),
    )


def _open_strict_https(
    url: str,
    *,
    timeout: float,
    user_agent: str,
    opener: Any = None,
):
    safe_url = validate_release_url(url)
    req = urllib.request.Request(
        safe_url,
        headers={
            "User-Agent": user_agent,
            "Accept": "application/json, application/octet-stream",
        },
    )
    active_opener = opener if opener is not None else _build_opener()
    return active_opener.open(req, timeout=float(timeout))


def parse_manifest(raw_payload: bytes | str) -> dict[str, Any]:
    """Parses and validates a `latest.json` release manifest."""
    text = raw_payload.decode("utf-8") if isinstance(raw_payload, (bytes, bytearray)) else str(raw_payload)
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("Release manifest must be a JSON object")

    version = str(data.get("version") or "").strip().lstrip("v")
    parse_semver(version)

    release_url = validate_release_url(
        str(data.get("release_url") or f"{RELEASES_BASE_URL}/tag/v{version}")
    )

    def _parse_asset(key: str) -> dict[str, Any] | None:
        raw_asset = data.get(key)
        if not isinstance(raw_asset, dict):
            return None
        asset_url = validate_release_url(str(raw_asset.get("url") or ""))
        asset_sha = validate_sha256_hex(str(raw_asset.get("sha256") or ""))
        filename = os.path.basename(str(raw_asset.get("filename") or urlparse(asset_url).path))
        size = int(raw_asset.get("size") or 0)
        return {
            "filename": filename,
            "url": asset_url,
            "sha256": asset_sha,
            "size": max(0, size),
        }

    return {
        "version": version,
        "tag": str(data.get("tag") or f"v{version}"),
        "published_at": str(data.get("published_at") or ""),
        "release_url": release_url,
        "notes": str(data.get("notes") or ""),
        "installer": _parse_asset("installer"),
        "portable": _parse_asset("portable"),
    }


def _check_github_api_fallback(
    current_version: str,
    *,
    api_url: str,
    timeout: float,
    is_portable: bool,
    opener: Any = None,
) -> dict[str, Any]:
    """Read-only fallback to GitHub Releases API when `latest.json` returns 404.

    Never downloads binaries without a verified SHA-256 manifest: if a newer tag
    exists without `latest.json`, it only offers opening the official GitHub
    Release page.
    """
    try:
        with _open_strict_https(
            api_url,
            timeout=timeout,
            user_agent=f"FLAC-Verifier/{current_version}",
            opener=opener,
        ) as resp:
            raw = resp.read(MAX_MANIFEST_BYTES + 1)
            if len(raw) > MAX_MANIFEST_BYTES:
                raise ValueError("Release API response exceeds size limit")
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("Unexpected release API payload")
        tag = str(payload.get("tag_name") or "").strip()
        version = tag.lstrip("v")
        parse_semver(version)
        release_url = validate_release_url(
            str(payload.get("html_url") or f"{RELEASES_BASE_URL}/tag/{tag}")
        )
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return {
                "ok": True,
                "available": False,
                "current_version": str(current_version),
                "is_portable": is_portable,
                "source": "no_manifest_yet",
                "message": f"You are up to date (v{current_version}).",
            }
        return {
            "ok": False,
            "available": False,
            "error": "No update manifest is published yet for this release. Check GitHub Releases later.",
            "current_version": str(current_version),
            "is_portable": is_portable,
        }
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError):
        return {
            "ok": False,
            "available": False,
            "error": "No update manifest is published yet for this release. Check GitHub Releases later.",
            "current_version": str(current_version),
            "is_portable": is_portable,
        }

    available = is_newer_version(version, current_version)
    if not available:
        return {
            "ok": True,
            "available": False,
            "version": version,
            "tag": tag or f"v{version}",
            "release_url": release_url,
            "current_version": str(current_version),
            "is_portable": is_portable,
            "source": "github_api_fallback",
            "message": f"You are up to date (v{current_version}).",
        }
    return {
        "ok": True,
        "available": True,
        "version": version,
        "tag": tag or f"v{version}",
        "published_at": str(payload.get("published_at") or ""),
        "release_url": release_url,
        "notes": str(payload.get("name") or ""),
        "installer": None,
        "portable": None,
        "current_version": str(current_version),
        # Without `latest.json` SHA-256 digests, open the official Release page instead of auto-downloading.
        "is_portable": True,
        "source": "github_api_fallback",
    }


def _format_network_error(exc: BaseException) -> str:
    """Returns a concise, user-readable message for network/TLS/timeout errors."""
    reason: Any = getattr(exc, "reason", exc)
    if isinstance(exc, (TimeoutError, socket.timeout)) or isinstance(
        reason, (TimeoutError, socket.timeout)
    ):
        return "Update check timed out while contacting GitHub Releases."
    if isinstance(reason, str) and "timed out" in reason.lower():
        return "Update check timed out while contacting GitHub Releases."

    raw_text = ""
    if isinstance(reason, ssl.SSLError):
        raw_text = str(getattr(reason, "reason", "") or reason)
        short = " ".join(raw_text.split())[:90].strip()
        if short:
            return f"TLS/SSL check failed while contacting GitHub Releases ({short})."
        return "TLS/SSL check failed while contacting GitHub Releases."

    raw_text = str(reason or exc)
    short = " ".join(raw_text.split())
    # Strip verbose OS errno prefixes while preserving the human-readable cause.
    short = re.sub(r"^\[(?:Errno|WinError)\s+\d+\]\s*", "", short).strip()[:90]
    if short:
        return f"Could not reach GitHub Releases ({short}). Check your internet connection."
    return "Could not reach GitHub Releases. Check your internet connection."


def check_for_update(
    current_version: str,
    *,
    offline_mode: bool = False,
    manifest_url: str = LATEST_JSON_URL,
    api_fallback_url: str = GITHUB_API_LATEST_URL,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    portable: bool | None = None,
    opener: Any = None,
) -> dict[str, Any]:
    """Checks GitHub Releases `latest.json` for a newer version.

    Falls back read-only to the official GitHub Releases API on HTTP 404 (for
    example before a release publishes `latest.json`), never raises on network
    errors or timeouts, and returns a structured dict.
    """
    is_portable = ldb.is_portable_mode() if portable is None else bool(portable)
    if offline_mode:
        return {
            "ok": True,
            "available": False,
            "skipped": True,
            "reason": "offline_mode",
            "current_version": str(current_version),
            "is_portable": is_portable,
        }

    try:
        with _open_strict_https(
            manifest_url,
            timeout=timeout,
            user_agent=f"FLAC-Verifier/{current_version}",
            opener=opener,
        ) as resp:
            raw = resp.read(MAX_MANIFEST_BYTES + 1)
            if len(raw) > MAX_MANIFEST_BYTES:
                raise ValueError("Release manifest exceeds size limit")
        manifest = parse_manifest(raw)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return _check_github_api_fallback(
                current_version,
                api_url=api_fallback_url,
                timeout=timeout,
                is_portable=is_portable,
                opener=opener,
            )
        return {
            "ok": False,
            "available": False,
            "error": f"Could not check GitHub Releases (HTTP {exc.code}). Try again later.",
            "current_version": str(current_version),
            "is_portable": is_portable,
        }
    except (TimeoutError, socket.timeout, urllib.error.URLError, OSError) as exc:
        return {
            "ok": False,
            "available": False,
            "error": _format_network_error(exc),
            "current_version": str(current_version),
            "is_portable": is_portable,
        }
    except (ValueError, json.JSONDecodeError) as exc:
        return {
            "ok": False,
            "available": False,
            "error": f"Update check failed ({type(exc).__name__}: {exc})",
            "current_version": str(current_version),
            "is_portable": is_portable,
        }

    available = is_newer_version(manifest["version"], current_version)
    return {
        **manifest,
        "ok": True,
        "available": available,
        "current_version": str(current_version),
        "is_portable": is_portable,
    }


def download_and_verify_installer(
    update_info: dict[str, Any],
    *,
    dest_dir: str | None = None,
    timeout: float = DOWNLOAD_TIMEOUT_SECONDS,
    on_progress: ProgressCallback | None = None,
    opener: Any = None,
) -> dict[str, Any]:
    """Downloads the Windows installer into a temp folder and verifies SHA-256.

    Deletes the file immediately and returns `ok=False` on any checksum mismatch
    or unauthorized redirect.
    """
    asset = update_info.get("installer") if isinstance(update_info, dict) else None
    if not isinstance(asset, dict):
        return {"ok": False, "error": "No Windows installer entry found in release manifest"}

    try:
        url = validate_release_url(str(asset.get("url") or ""))
        expected_sha = validate_sha256_hex(str(asset.get("sha256") or ""))
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}

    raw_name = os.path.basename(str(asset.get("filename") or "FLAC_Verifier_Setup.exe"))
    safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", raw_name) or "FLAC_Verifier_Setup.exe"
    if not safe_name.lower().endswith(".exe"):
        return {"ok": False, "error": "Installer filename must end with .exe"}

    target_dir = dest_dir or tempfile.mkdtemp(prefix="flac_verifier_update_")
    os.makedirs(target_dir, exist_ok=True)
    target_path = os.path.join(target_dir, safe_name)
    hasher = hashlib.sha256()
    downloaded = 0
    expected_size = int(asset.get("size") or 0)

    try:
        with _open_strict_https(
            url,
            timeout=timeout,
            user_agent=f"FLAC-Verifier/{update_info.get('current_version', '3.0.0')}",
            opener=opener,
        ) as resp:
            content_len = resp.headers.get("Content-Length") if hasattr(resp, "headers") else None
            total = int(content_len) if (content_len and str(content_len).isdigit()) else expected_size
            with open(target_path, "wb") as fh:
                while True:
                    chunk = resp.read(DOWNLOAD_CHUNK_BYTES)
                    if not chunk:
                        break
                    downloaded += len(chunk)
                    if downloaded > MAX_INSTALLER_BYTES:
                        raise ValueError("Installer download exceeded maximum allowed size")
                    fh.write(chunk)
                    hasher.update(chunk)
                    if on_progress:
                        try:
                            on_progress(downloaded, total)
                        except Exception:
                            pass
                fh.flush()
                os.fsync(fh.fileno())
    except Exception as exc:
        _safe_unlink(target_path)
        return {"ok": False, "error": f"Download failed ({type(exc).__name__}: {exc})"}

    actual_sha = hasher.hexdigest().lower()
    if not hmac.compare_digest(actual_sha, expected_sha):
        _safe_unlink(target_path)
        return {
            "ok": False,
            "error": (
                f"SHA-256 verification failed (expected {expected_sha[:12]}…, "
                f"got {actual_sha[:12]}…); file deleted"
            ),
        }

    return {
        "ok": True,
        "installer_path": target_path,
        "sha256": actual_sha,
        "bytes": downloaded,
    }


def _safe_unlink(path: str) -> None:
    try:
        if path and os.path.exists(path):
            os.unlink(path)
    except OSError:
        pass


def build_silent_installer_command(installer_path: str) -> list[str]:
    """Returns the standard silent per-user Inno Setup upgrade command."""
    return [
        installer_path,
        "/VERYSILENT",
        "/SUPPRESSMSGBOXES",
        "/NORESTART",
        "/SP-",
    ]


def launch_installer(
    installer_path: str,
    *,
    launcher: Callable[..., Any] = subprocess.Popen,
) -> dict[str, Any]:
    """Launches a verified Inno Setup installer detached from the current app."""
    if not installer_path or not os.path.isfile(installer_path):
        return {"ok": False, "error": "Verified installer executable not found"}
    cmd = build_silent_installer_command(installer_path)
    kwargs: dict[str, Any] = {"close_fds": True}
    if os.name == "nt":
        detached = getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
        new_group = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        kwargs["creationflags"] = detached | new_group
    try:
        launcher(cmd, **kwargs)
        return {"ok": True, "command": cmd}
    except Exception as exc:
        return {"ok": False, "error": f"Could not launch installer ({type(exc).__name__}: {exc})"}


# ─── Release build helpers (`latest.json` + `SHA256SUMS`) ────────────────────

def generate_release_manifest(
    version: str,
    *,
    installer_path: str | None = None,
    portable_zip_path: str | None = None,
    notes: str = "",
) -> dict[str, Any]:
    """Builds a verified `latest.json` dictionary from local build artefacts."""
    clean_ver = str(version).strip().lstrip("v")
    parse_semver(clean_ver)
    tag = f"v{clean_ver}"
    dl_base = f"{RELEASES_BASE_URL}/download/{tag}"

    def _entry(path: str | None) -> dict[str, Any] | None:
        if not path or not os.path.isfile(path):
            return None
        fname = os.path.basename(path)
        return {
            "filename": fname,
            "url": f"{dl_base}/{fname}",
            "sha256": compute_file_sha256(path),
            "size": int(os.path.getsize(path)),
        }

    return {
        "version": clean_ver,
        "tag": tag,
        "published_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "release_url": f"{RELEASES_BASE_URL}/tag/{tag}",
        "notes": notes,
        "installer": _entry(installer_path),
        "portable": _entry(portable_zip_path),
    }


def write_release_metadata(
    out_dir: str,
    version: str,
    *,
    installer_path: str | None = None,
    portable_zip_path: str | None = None,
    notes: str = "",
) -> dict[str, str]:
    """Writes `latest.json` and `SHA256SUMS_<version>.txt` into `out_dir`."""
    os.makedirs(out_dir, exist_ok=True)
    manifest = generate_release_manifest(
        version,
        installer_path=installer_path,
        portable_zip_path=portable_zip_path,
        notes=notes,
    )
    latest_path = os.path.join(out_dir, "latest.json")
    with open(latest_path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(manifest, fh, indent=2)
        fh.write("\n")

    sums_lines: list[str] = []
    for key in ("installer", "portable"):
        entry = manifest.get(key)
        if isinstance(entry, dict):
            sums_lines.append(f"{entry['sha256']}  {entry['filename']}")
    sums_path = os.path.join(out_dir, f"SHA256SUMS_{manifest['version']}.txt")
    with open(sums_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(sums_lines) + ("\n" if sums_lines else ""))

    return {"latest_json": latest_path, "sha256sums": sums_path}
