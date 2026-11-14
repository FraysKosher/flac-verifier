# Security Policy

## Supported Versions

Security fixes and forensic false-positive/false-negative patches are provided for the current minor release series only:

| Version | Supported | Notes |
| --- | --- | --- |
| `2.9.x` | Yes | Active development and security fixes (current: `2.9.7`) |
| `< 2.9` | No | Unsupported; upgrade to the latest `2.9.x` release |

## Scope & Threat Model

FLAC VERIFIER runs completely offline and processes untrusted local audio files (`.flac` containers, Vorbis/ID3/PICTURE metadata blocks, and filenames) inside user-selected folders:

- **Parser & codec inputs**: Crafted `.flac` headers, malformed metadata tags, extreme sample rates or channel layouts parsed via `soundfile`, `mutagen`, `numpy`, and `scipy`.
- **Filesystem outputs**: Cache files (`.flac_verifier_cache.json`), rendered spectrograms (`_spectrograms/`), and album PDF reports (`_report.pdf`) derived from input paths.
- **Process execution**: The GUI spawns the engine subprocess (`flac_motor.exe` / `main.py --motor-cli`) and `build.py` bundles frozen Windows executables verified with SHA-256 checksums (`SHA256SUMS_<version>.txt`).

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
Get-FileHash .\FLAC_Verifier_2.9.7-win64.zip -Algorithm SHA256
```
