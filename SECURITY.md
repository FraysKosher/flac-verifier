# Security Policy

## Supported Versions

Security fixes and forensic false-positive/false-negative patches are provided for the current minor release series only:

| Version | Supported | Notes |
| --- | --- | --- |
| `3.0.x` | Yes | Active development and security fixes (current: `3.0.0`) |
| `< 3.0` | No | Unsupported; upgrade to the latest `3.0.x` release |

## Scope & Threat Model

FLAC VERIFIER runs offline for all audio analysis and processes untrusted local audio files (`.flac` containers, Vorbis/ID3/PICTURE metadata blocks, embedded/sidecar cover art, and filenames) inside user-selected folders:

- **Parser & codec inputs**: Crafted `.flac` headers, malformed metadata tags, corrupted embedded/sidecar cover artwork (`covers.py` enforces Pillow decompression-bomb guards and atomic RGB WebP transcoding), extreme sample rates or channel layouts parsed via `soundfile`, `mutagen`, `Pillow`, `numpy`, and `scipy`.
- **Filesystem outputs**: SQLite WAL library index (`library.db`), cached `320x320` WebP album covers, compressed spectrogram previews (`*.specz`), and `settings.json` inside `%APPDATA%\FLAC Verifier` (or next to `FLAC_Verifier.exe` when `portable.flag` is present), plus OS user cache entries (`FLAC_VERIFIER_CACHE_DIR` override) and optional per-album PDF reports (`flac_verifier_report.pdf` or `<Album>_flac_verifier_report.pdf` inside `--output-dir`).
- **Process execution**: The desktop shell (`gui_v3.py`, Edge WebView2) spawns the engine subprocess (`flac_motor.exe` / `main.py --motor-cli`), and `build.py` packages frozen Windows binaries verified with SHA-256 checksums (`SHA256SUMS_<version>.txt`).
- **Optional update check (`updater.py`)**: Background update checks can be disabled completely in **Settings & Updates** (*Offline mode*). When enabled, `updater.py` strictly enforces `HTTPS` against an explicit GitHub Releases host allowlist (`github.com/FraysKosher/flac-verifier` and GitHub release CDN hosts), blocks cross-host/HTTP redirects, and mandates `SHA-256` verification (`hmac.compare_digest`) against `latest.json` before executing a downloaded installer (auto-deleting the file on any mismatch).

Out of scope: vulnerabilities requiring prior local code execution by the user, or third-party upstream media players used solely to open generated PDF/PNG outputs.

## Reporting a Vulnerability

Do **not** open a public GitHub issue for security vulnerabilities.

1. Use **GitHub Private Vulnerability Reporting** under the **Security** tab (`Security -> Report a vulnerability`) in [`FraysKosher/flac-verifier`](https://github.com/FraysKosher/flac-verifier/security/advisories/new).
2. Include the affected version (`python main.py --version`), operating system, reproduction steps, and a minimal synthetic proof-of-concept file or traceback if applicable.

### Disclosure Timeline

- **Acknowledgment**: within **72 hours** of receiving a private report.
- **Triage & fix target**: within **14 days** for confirmed vulnerabilities, followed by a coordinated advisory and release with updated `SHA256SUMS`.

## Verifying Release Binaries

Always verify downloaded Windows release archives against the `SHA256SUMS_<version>.txt` file published on the corresponding GitHub Release before running the executable:

```powershell
Get-FileHash .\FLAC_Verifier_Setup_3.0.0-win64.exe -Algorithm SHA256
Get-FileHash .\FLAC_Verifier_3.0.0-win64-portable.zip -Algorithm SHA256
```
