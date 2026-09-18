# Two designs for the same problem

Both FLAC VERIFIER and [FLAC Detective](https://github.com/Guillain-RDCDE/FLAC_Detective)
answer the same question — *is this FLAC file what it claims to be?* — from two
different designs. This page is a factual comparison of the two approaches, with
the method used to obtain every number, so you can pick the one that fits your
workflow.

- **FLAC Detective** is a Python library and CLI meant to be installed and
  scripted: a scanner you run over a whole library, and a building block inside
  other tools.
- **FLAC VERIFIER** is a desktop application for Windows: a self-contained
  executable that analyses an album and produces a document.

## Side by side

| | FLAC Detective | FLAC VERIFIER |
|---|---|---|
| Shape | Python library + CLI + beets plugin | Windows desktop application (GUI + CLI in the same binary) |
| Installed by | `pip install flac-detective`, Docker (amd64/arm64) | Copying a folder and double-clicking an `.exe`; no Python on the target machine |
| Interface | Command line, optional desktop GUI extra, beets integration | Two-column GUI, interactive CLI, single `main.py` entry point that dispatches between them |
| Integrity check | Not part of the analysis model | Reconstructs the decoded PCM and compares it against the STREAMINFO MD5; a mismatch is a hard error outside the scoring |
| Evidence for upscales | Spectral fingerprints, bitrate behaviour and corroboration across evidence families | LSB utilisation measured directly on the 16-bit grid, plus spectral ceiling, distance to Nyquist, cut-off detection and declared-vs-actual resolution coherence |
| Verdict model | `AUTHENTIC` / `WARNING` / `SUSPICIOUS` / `FAKE_CERTAIN`, conviction requires two evidence families to agree | `GENUINE LOSSLESS` / `PROBABLY LOSSLESS` / `SUSPICIOUS` / `PROBABLE UPSCALE` / `indeterminate`, from positive-evidence scoring with thresholds; a confirmed defect caps the verdict |
| Reports | CSV, HTML and text reports | One PDF per album: cover, summary table, one page per track with its spectrogram and its detected issues |
| Formats | FLAC, ALAC and other containers; MP3, AAC, Vorbis and partially Opus fingerprints | FLAC (16/24-bit, CD and hi-res), including the 44.1/48 kHz → 96 kHz upscale case |
| Parallelism and cache | Process pool | Process pool plus a per-file result cache keyed by size, mtime, STREAMINFO MD5, mode and engine version |
| Packaging | PyPI, Docker, wheel smoke tests in CI | PyInstaller onedir with two executables, plus `comprobar_ejecutable.py`, which verifies the frozen build against the source field by field |
| Code size | 70 files, 15,426 lines in `src/` | 7 modules, 3,618 lines |
| Tests | 562 test functions in 68 files | 229 test functions in 12 files |
| Version checked | 1.13.16 (`1ae00ad`, 2026-09-15) | 1.0.0 |
| Licence | MIT | MIT |

## The library-and-CLI design

FLAC Detective is the design you pick when the tool has to live inside a bigger
pipeline. It is on PyPI, so `pip install flac-detective` is the whole
installation; it ships Docker images for amd64 and arm64; it plugs into **beets**
so a library scan happens as part of tagging; and it writes CSV, HTML and text
reports that other tools can consume. Its verdicts are deliberately conservative:
three families of evidence, and a file is only convicted when two of them agree
([reference](https://github.com/Guillain-RDCDE/FLAC_Detective/blob/main/docs/REFERENCE.md)).
It covers more containers and codecs than this project does, and it has the
release engineering that comes with a widely installed package: eight CI
workflows (including CodeQL), coverage reporting, mypy/black/isort, Dependabot,
issue and pull-request templates and a security policy.

## The desktop-application design

FLAC VERIFIER is the design you pick when the tool has to be handed to someone
who does not run Python. There is no installation step: the released folder
contains `FLAC_Verifier.exe` and everything it needs, and the engine ships as a
second console executable (`flac_motor.exe`) because a windowed PyInstaller build
has no usable stdout for the NDJSON protocol the GUI reads.

Three consequences of that goal are visible in the feature set:

- **The album is the unit of work.** `--pdf` produces one document per album —
  cover, global summary, and one page per track with its spectrogram and its
  detected problems — which is the artefact people file away with a collection.
- **Integrity is separate from verdict scoring.** The decoded PCM is hashed and
  compared against the MD5 recorded in the STREAMINFO block; a mismatch is
  reported as a hard error and never turned into a score. Transcode detection and
  file integrity are different questions, and the report answers both.
- **The packaged build is verified as a build.** `comprobar_ejecutable.py` runs
  eleven checks on the frozen executables, including an analysis in parallel with
  eight processes (the `multiprocessing.freeze_support()` path) and a comparison
  of the executable's records against the source's, field by field.

## How these claims were checked

Both projects were measured the same day, with the same method, from a clone
made on 2026-09-16:

```bash
git clone --depth 1 https://github.com/Guillain-RDCDE/FLAC_Detective
cd FLAC_Detective
git log -1 --pretty="%h %ad" --date=short      # 1ae00ad 2026-09-15 (v1.13.16)

# Capability greps over the whole source tree, not just the top level.
# 'grep' prints the files it matched; the count is what matters here: 0.
grep -ril 'md5\|streaminfo'      src --include='*.py' | wc -l    # 0
grep -ril '\blsb\b\|low.bits'    src --include='*.py' | wc -l    # 0
grep -ril 'pdf'                  src --include='*.py' | wc -l    # 0

# Size and tests (recursive, and counting definitions, not test runs):
find src -name '*.py' | wc -l                                    # 70
find src -name '*.py' -exec cat {} + | wc -l                     # 15426
grep -rE '^\s*def test_' tests | wc -l                           # 562
find tests -name 'test_*.py' | wc -l                             # 68
```

Two of these numbers were wrong in the first version of this page, because lines
and tests were counted with two different methods; they were re-measured with a
single one. Line, test and capability counts are the parts most likely to drift
as either project moves, so they are stated with the commit they were taken from
rather than as permanent claims.

Both projects are MIT licensed.
