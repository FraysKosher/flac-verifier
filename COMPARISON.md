# FLAC VERIFIER vs FLAC Detective

A question worth answering honestly, because both tools scan the same files and
solve overlapping problems:

> How is this better than [FLAC Detective](https://github.com/Guillain-RDCDE/FLAC_Detective),
> and why not just improve or fork that project?

**Short answer: it is not better.** FLAC Detective is the more mature project by
every measure that matters for a library-wide scan, and if you are choosing one
tool today, you should probably choose it. This page exists to state the few
measured differences that are real, and what we intend to do about the overlap.

## Where FLAC Detective is the better tool

| | FLAC Detective |
|---|---|
| Distribution | On PyPI (`pip install flac-detective`), Docker images for amd64 and arm64 |
| Integration | Plugin for **beets**, plus an optional desktop GUI (`flac-detective[gui]`) |
| Formats | FLAC, ALAC and other containers; MP3, AAC, Vorbis and partially Opus fingerprints |
| Documentation | Documentation site, user guide, reference, a beginner's guide |
| Release engineering | 8 CI workflows (including CodeQL), code coverage, mypy/black/isort, Dependabot, issue and PR templates, security policy |
| Test suite | 562 test functions in 68 test files |
| Track record | Version 1.13.16, continuously released, a changelog of real fixes reported by users |

Its verdict model is also deliberately conservative: three independent families of
evidence, and a file is only convicted when two of them agree
([reference](https://github.com/Guillain-RDCDE/FLAC_Detective/blob/main/docs/REFERENCE.md)).

## Where FLAC VERIFIER does something different

Four things, all checked against its source tree (see *How these claims were
checked* below):

1. **Integrity is a separate, hard check.** FLAC Detective reads the spectral and
   bitrate fingerprints that lossy coding leaves behind; it does not verify the
   STREAMINFO MD5 (0 references in `src/`). FLAC VERIFIER reconstructs the
   decoded PCM, compares it against the MD5 stored in the file and treats a
   mismatch as a hard error, outside the scoring. That catches a corrupt or
   doctored file, which is orthogonal to transcode detection.
2. **LSB utilisation is measured directly.** 0 references to low-bit or LSB-grid
   analysis in FLAC Detective; it infers padded depth from other evidence. FLAC
   VERIFIER measures the share of samples sitting exactly on the 16-bit grid,
   which is a cheap, direct measurement of *effective* bit depth in a 24-bit
   container. Same question, different method, and they can disagree.
3. **Per-album PDF report** with the spectrogram pages embedded (FLAC Detective
   writes CSV, HTML and text reports; there is no PDF in its tree). This is the
   piece that fits an album-collection workflow: one document per album,
   one page per track.
4. **Windows distribution with no Python**: `FLAC_Verifier.exe`, a folder you
   copy and double-click, and an 11-check verification script that compares the
   frozen executable against the source field by field.

## Where FLAC VERIFIER is worse

Said plainly, because it is true and because the criticism is fair:

- **One behavioural difference is not a project.** There is no CI, no packaging,
  no docs site and no issue templates here; there is one initial commit.
- **Only FLAC.** FLAC Detective covers more containers and codecs.
- **Comments and docstrings are still in Spanish** while everything the user
  sees is English.
- **Built with heavy AI assistance**, and the repository shows it in the shape of
  that single large first commit.

## What we intend to do about it

The end state worth having is one good implementation, not two mediocre ones:

1. Offer the two pieces that are not already in FLAC Detective — the STREAMINFO
   MD5 integrity check and the PDF report generator — as a pull request against
   [FLAC_Detective](https://github.com/Guillain-RDCDE/FLAC_Detective).
2. If the maintainer wants them, reduce this repository to the thing that does
   not belong upstream: the Windows one-click front-end (GUI + packaged
   executable), calling the established engine.
3. If the maintainer does not want them, archive this repository and point at
   FLAC Detective rather than keeping a parallel implementation of the same
   idea.

Both projects are MIT licensed, so either path is available.

| | FLAC Detective | FLAC VERIFIER |
|---|---|---|
| Code size | 70 files, 15,426 lines in `src/` | 7 modules, 3,618 lines |
| Tests | 562 test functions in 68 files | 229 test functions in 12 files |
| Version checked | 1.13.16 (`1ae00ad`, 2026-09-15) | 1.0.0 |

## How these claims were checked

Everything above comes from a clone made on 2026-09-16, and the numbers were
produced the same way for both projects:

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

Two numbers in this file have already been corrected once, after re-running the
counts with a single method instead of two: line and test counts depend on
whether you recurse and whether you count definitions or executed tests. If you
find another mistake, the numbers are the ones to attack — open an issue and it
gets fixed, and the other project moving forward is the point in its favour.
