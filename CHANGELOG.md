# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [2.9.2] - 2026-11-11

### Added

- Raised `ENGINE_VERSION` to `"13.2"` and expanded the automated test suite to
  **345 tests** across `_motor_bits.py`, `_motor_espectral.py`,
  `_motor_veredicto.py`, `_motor_cache.py`, `motor_flac.py`, `verificar_flac.py`,
  `main.py`, `gui.py` and `tests/`:
  - **Filterbank & subband lattice calibration (`_motor_espectral.py`)**:
    - Added the ISO/IEC 14496-3 AAC / WMA 2048-sample Sine analysis window
      (`_get_sine_2048`) alongside Vorbis and AAC `KBD` ($\alpha=4$) in
      `MDCT-1024`, eliminated the left-channel selection bias (`el >= er`),
      evaluated the opposite channel on asymmetric stereo blocks, and replaced
      center-slicing with energy-guided continuous subwindow selection
      (`_select_continuous_subwindow`).
    - Centered the 512-tap Kaiser PQMF prototype at tap `256.0` in
      `_get_pqmf_fast` and disambiguated 18-sample single-granule `PQMF-576 (MP3)`
      periodicity (`dft_m[2]`) from 12-sample subblock `PQMF-32 (Musepack)`
      periodicity (`dft_m[3]`), while enabling `PQMF-32` on 8-subband sets with
      `ceiling_hz >= 13.5 kHz`.
    - Replaced whole-buffer rectangular FFT resampling at 44.1 kHz in `CELT-960`
      with a Kaiser-windowed polyphase resampler (`scipy.signal.resample_poly(160, 147)`).
    - Restricted `_detect_sbr_qmf_replication` to single continuous blocks
      (never concatenating non-adjacent blocks), subtracted the per-frame
      instantaneous common mode before column normalization to immunize against
      sidechain/trance-gated harmonic synth chords, and vectorized the `dk`
      shift sweep.
    - Introduced `_robust_z_peak(values, min_scale)` combining `1.4826 * MAD`,
      `0.5 * IQR / 1.349` and a family-specific scale floor (`1e-3`, `0.25`,
      `1e-2`) so `MAD == 0` never inflates Z-scores.
  - **Soft heuristic calibration, typed score ledger & album coherence
    (`_motor_veredicto.py`, `_motor_espectral.py`, `_motor_bits.py`)**:
    - Replaced `nonlocal` score mutation in `calculate_score` with typed
      `EvidenceTier`, `EvidenceFamily`, `VerdictCap` and `_ScoreLedger` state
      transitions. An isolated soft-blocking heuristic (`corroborated == False`)
      caps the verdict at `"SUSPICIOUS"` (`score >= 0.50`), while condemning a
      track as `"PROBABLE UPSCALE"` requires multi-family corroboration or a
      hard structural proof.
    - Moved `sospecha_excitador_armonico` to `EvidenceFamily.SPECTRAL_ENVELOPE`,
      required lateral HF cut-off evidence (`delta_cutoff >= 900 Hz` or
      `side_ceiling_hz < 17.5 kHz`) in the static `collapse > 16.5 dB`
      Joint-Stereo branch, and required `max(drp_med, mean(drp)) >= 24.0 dB` on
      the `30 dB` spread branch of `modulacion_psicoacustica_20k`.
    - Lowered the global 16-bit boundary micro-dither bin count gate from `3`
      to `2` occupied modulo-256 LSB8 bins (`r_glob["cajones"] >= 2`) so 4–6 s
      `PCM_32` 16-in-32 dithered fakes trigger `es_16bit_dither_en_24`, and
      surfaced DoP 1.0 markers (`0x05`/`0xFA`) in `forensic_evidence` as an
      informational note.
    - Expanded `apply_album_coherence` to allow benign mastering notes
      (`mastering brickwall limiter`, `studio anti-aliasing`,
      `analogue/dsd master`, `dsd/sacd noise-shaping`), build the 80 % album
      quorum from `"GENUINE LOSSLESS"` plus clean `"PROBABLY LOSSLESS"` tracks
      (`score >= 0.70` with at least one `"GENUINE LOSSLESS"` or `score >= 0.75`
      anchor), and scale the rescuable candidate limit to `max(2, N // 6)`.
  - **Partial PNG cache hydration, LPT pool scheduling & CLI/GUI state guards**:
    - Added `CacheLookupStatus.METRICS_HIT_NEEDS_PNG` (`_motor_cache.py`,
      `motor_flac.py`) so requesting `--png`/`--pdf` on a track with cached
      metrics renders only the missing spectrogram without recomputing MD5,
      bit lattices or lapped filterbanks.
    - Enforced `_BlockAnalysis.result()` idempotency on EOF clipping runs,
      32-bit unsigned two's-complement De Bruijn bit-depth algebra without
      NumPy 2.x overflow warnings, single DCT-IV evaluation per `MDCT-1024`
      window, native `complex64` `scipy.fft.rfft` STFTs, LPT (`os.path.getsize`)
      process pool scheduling, PID-isolated `.tmp` cache writes, asynchronous
      `ffmpeg` `stderr` draining, direct `--path` CLI dispatch in `main.py`, and
      explicit `RUN_STATE_TRANSITIONS` / logical inspector selection in `gui.py`.

## [2.9.1] - 2026-11-11

### Fixed

- Raised `ENGINE_VERSION` to `"13.1"` across `_motor_bits.py`,
  `_motor_espectral.py`, `_motor_veredicto.py` and `motor_flac.py`:
  - **24-bit audio inside 32-bit FLAC container (`ForensicBitsAccumulator`)**:
    computes `min_zeros_16 = max(8, self.bits - 16)` (`16` trailing zeros for
    32-bit containers, `8` for 24-bit containers) and scales `effective` bit
    resolution from `self.bits` so genuine 24-bit recordings stored in 32-bit
    FLAC containers (`8` trailing zero bits) are recognized as 24-bit active
    lossless instead of being misflagged as 16-bit zero-padded containers.
  - **Soft heuristic calibration (`modulacion_psicoacustica_20k`,
    `_ms_dynamic_collapse`, `es_estacionario`) & uncorroborated soft blocking**:
    tightens upper-band 20 kHz modulation (`drp_iqr >= 13.5 dB`,
    `top_iqr >= 6.5 dB`, `drp_med >= 36.0 dB` with `grad >= 20 dB/kHz` or
    `drp_med >= 42 dB`), requires static HF Side collapse alongside dynamic M/S
    swings before triggering `sospecha_joint_stereo`, widens `es_estacionario`
    to cover rhythmic electronic tracks with sustained harmonic synth pads, and
    only lets soft heuristics (`temporal_modulation`, `stereo_collapse`) cap the
    verdict at `SUSPICIOUS` when corroborated by another independent family or
    when `score < 0.70`.
  - **Full-track `--mode full` active STFT budget (`_needed_active_blocks`)**:
    caps `--mode full` at `SPECTRO_SECONDS + 15` (`60 s` of the highest-energy
    active blocks selected across 100 % of the track timeline while MD5,
    24/32-bit lattice, clipping and DR audit 100 % of all samples), avoiding
    unnecessary STFT CPU overhead on long tracks.

## [2.9.0] - 2026-11-11

### Added

- Raised `ENGINE_VERSION` to `"13.0"` with **Phase 13 Forensic Precision &
  Architecture Hardening** across `_motor_bits.py`, `_motor_espectral.py`,
  `_motor_veredicto.py`, `motor_flac.py` and `tests/benchmark_validacion.py`:
  - **Multi-family corroboration gate (`familias_evidencia_lossy` &
    `corroboracion_multifamilia`)**: prevents false-positive condemnation from
    any single isolated soft spectral heuristic (`islas_mdct_sfb21`,
    `modulacion_psicoacustica_20k`, `sospecha_joint_stereo`, `iqr_corte_hz`,
    `ratio_huecos_mdct`). Condemning a file as `"PROBABLE UPSCALE"` now requires
    either `>= 2` independent evidence families or a deterministic structural
    proof (`filterbank_lattice`, `spectral_cutoff` or `bit_depth_container`);
    a single uncorroborated soft heuristic is capped at `"SUSPICIOUS"`.
  - **Spectral stationarity guard (`es_estacionario`)**: detects sustained
    electronic synthesizer drones and harmonic pads (`std(env_4_11) < 1.8 dB`
    coupled with a high-frequency tonal crest `P95 - P50 >= 12.0 dB` in
    `13.5–19.5 kHz`) and suppresses false subband-hole, island, modulation and
    inter-harmonic valley cut-off triggers between discrete overtones.
  - **MPEG-4 AAC Kaiser-Bessel Derived (`KBD`, $\alpha = 4$) window
    (`_get_kbd_2048`) + multi-channel (`L`/`R`/`Mid`) & 2nd-block lapped
    transform selection**: evaluates `MDCT-1024` with both the Vorbis/Sine
    window and the ISO/IEC 14496-3 AAC `KBD` ($\alpha = 4$) window, and selects
    the higher-transient stereo channel (`L` vs `R`), `Mid`, and the second most
    active block so asymmetric stereo AAC transcodes cannot evade lattice
    inversion.
  - **Active `--mode` (`center` | `full` | `seconds`) and `--seconds N`
    spectral windowing + native 32-bit integer FLAC (`PCM_32`) streaming
    audit**: wires `mode` and `seconds` into `_needed_active_blocks`,
    `_SpectralAccumulator` and `_BlockAnalysis`, and extends `ForensicBitsAccumulator`,
    `analyze_bit_depth` and `motor_flac._iter_blocks_s32_flac` to stream and
    audit 32-bit integer FLAC (`PCM_32`) containers in bounded memory.
  - **260-track validation benchmark (`tests/benchmark_validacion.py`,
    `VALIDATION.md`)**: expands the benchmark to `n = 260` (`110` authentic
    across 11 families + `150` fakes across 21 families) and adds 3 Phase 13
    unit tests (`test_32` to `test_34`) in `tests/test_forense_riguroso.py`
    (suite is now 338 tests).

## [2.8.0] - 2026-11-11

### Added

- Raised `ENGINE_VERSION` to `"12.0"` with **Phase 12 Forensic Closure &
  240-Track Real-Encoder Benchmark** across `_motor_espectral.py`,
  `_motor_veredicto.py` and `tests/benchmark_validacion.py`:
  - **Seamless HE-AAC v1/v2 QMF SBR detector (`_detect_sbr_qmf_replication`,
    `sbr_qmf_detectado`, `sbr_qmf_correlacion`, `sbr_qmf_z`)**: subtracts the
    local ~1.1 kHz spectral envelope per STFT frame to factor out broadband
    percussive transients and SBR coarse envelope adjusters, then sweeps
    additive QMF subband translation shifts (`3.5–9.5 kHz`) across `11.5–18.5
    kHz`. Catches boundary-matched SBR transcodes (`0 dB` notch) at both CD
    (`44.1/48 kHz`) and upsampled Hi-Res (`88.2/96/192 kHz`) rates while
    sparing dense polyphonic harmonic overtones ($f \mapsto k f_0$).
  - **Musepack SV7/SV8 `PQMF-32` pure subband frame lattice**: extends
    `_detect_lapped_codec_grid` to evaluate spectral-tilt-invariant temporal
    hole periodicity across all 32 polyphase offsets and 36 subband-frame
    phases (covering both 12-subband-sample SV8 sub-blocks and 36-sample SV7
    frames) with zero extra rFFT passes.
  - **Genuine band-limited / vintage / analogue roll-off protection
    (`rolloff_banda_limitada_genuina`) + adaptive passband lattice inversion**:
    spares genuine 1950s–1970s archival tape, vinyl and cassette transfers
    whose musical band ends smoothly below 70 % of Nyquist (`10.0–15.4 kHz`)
    with no codec signature (`PROBABLY LOSSLESS`), while adapting `MDCT-1024`
    and `PQMF-576` to the passband and ATH roll-off tail (`9.5–17.5 kHz`
    ceilings) so those same vintage masters transcoded through MP3, AAC or
    Vorbis are still caught (`PROBABLE UPSCALE`).
  - **240-track reproducible validation benchmark (`tests/benchmark_validacion.py`,
    `VALIDATION.md`)**: expands validation from `n = 40` synthetic tracks to
    `n = 240` (`100` authentic across 10 mastering/archival/vinyl/DSD/Hi-Res
    families + `140` fakes across 20 real `ffmpeg` codec, seamless SBR,
    Musepack `PQMF-32`, vintage-transcode, upscale and 24-bit micro-dither
    families) with per-family breakdown and SHA-256 corpus manifest digest.
  - Added 3 deterministic Phase 12 unit tests (`test_29` to `test_31`) in
    `tests/test_forense_riguroso.py` (suite is now 335 tests).

## [2.7.0] - 2026-11-11

### Added

- Raised `ENGINE_VERSION` to `"11.0"` with **Phase 11 Orthogonal Lapped
  Transform Inversion** (`_detect_lapped_codec_grid` in `_motor_espectral.py`
  and scoring in `_motor_veredicto.py`) to detect full-bandwidth high-bitrate
  lossy transcodes (`Vorbis -q8/-q10` up to 500 kbps, `AAC 320` with
  `--cutoff 22000`, `LAME CBR 320` with `--lowpass -1`, `LAME V0`, and `Opus
  160/256` at 44.1 and 48 kHz) whose power spectra extend to 22.05 kHz without
  a spectral cliff:
  - **`MDCT-1024 (Vorbis/AAC)`**: projects the decoded waveform onto the
    2,048-sample Vorbis-windowed Type-IV DCT across all 128 sample-level phase
    offsets and the 8 block-switching subgrids of 128 samples, measuring the
    robust Z-score ($\sigma$) of upper-band (`12.0–20.5 kHz`) near-zero
    coefficient sparsity where Princen-Bradley TDAC aliasing cancels at the
    true encoder frame phase $\phi_0$.
  - **`PQMF-576 (MP3)`**: applies a single-rFFT batched 32-band ISO/IEC 11172-3
    polyphase quadrature mirror filterbank (subbands `22..29`, `15.1–20.7 kHz`,
    512-tap Kaiser prototype decimated by 32 with odd-subband frequency
    inversion) followed by a vectorised 36-tap 18-point sine MDCT across all
    `576 = 32 x 18` granule phases.
  - **`CELT-960 (Opus)`**: applies inverse CELT post-emphasis filtering
    $y[n] = x[n] - 0.85\,x[n - 1]$ at 48 kHz (resampling a 0.33 s active window
    by `160/147` when `sr == 44100`) and evaluates the 960-sample low-overlap
    IETF RFC 6716 CELT MDCT across all 120 short-block phases, distinguishing
    Opus 20.00 kHz TDAC stopband boundary cancellation (`17.6–19.9 kHz` vs
    `20.05–21.75 kHz`) from static LTI mastering brickwall filters.
  - Added `mdct_lattice_detected`, `mdct_lattice_snr` and `mdct_lattice_family`
    to `esp`, scored confirmed phase-locked lattices (`-0.55`, blocking) in
    `_motor_veredicto.py`, and added 3 deterministic Phase 11 unit tests
    (`test_26` to `test_28`) in `tests/test_forense_riguroso.py` (suite is now
    332 tests).

## [2.6.0] - 2026-11-10

### Added

- Raised `ENGINE_VERSION` to `"10.0"` with the Phase 9/10 forensic detection
  suite across `_motor_bits.py`, `_motor_espectral.py` and `_motor_veredicto.py`:
  - **24-bit LSB8 interior occupancy audit (`lsb8_ocupacion_interior`,
    `es_16bit_dither_en_24`)**: detects 16-bit sources packaged inside 24-bit
    containers with $\pm 1\dots 3\text{ LSB}_{24}$ boundary micro-dither where
    interior modulo-256 bins (`[16..239]`) have $< 5\%$ occupancy.
  - **24.0 kHz DAT/broadcast Nyquist brickwall detector**: extends
    `muro_brickwall_nyquist` in `_diagnose_hires_authenticity` to catch
    `48 kHz -> 96/192 kHz` upscales whose passband exceeds the 22.05 kHz CD
    limit and drops at 24.0 kHz.
  - **Upper-band psychoacoustic modulation detector (`caida_modulacion_20k_db`,
    `brecha_modulacion_20k_db`, `modulacion_psicoacustica_20k`)**: measures
    frame-by-frame log-spectral drop between `16.0–19.5 kHz` and
    `20.2–21.8 kHz`, catching high-bitrate lossy codecs (`LAME V0/320`,
    `AAC-LC 256/320`, `Vorbis Q6/Q9`, `Opus 128->44.1k`, `WMA 192`) while
    sparing static LTI mastering filters.
  - **Dynamic frame-by-frame Mid/Side stereo collapse**: tracks 95th-percentile
    Mid/Side high-frequency excess (`13.5–16.5 kHz` vs `2–8 kHz`) across active
    frames in `PsychoacousticSTFTAccumulator`.
  - **Multi-scale spectral step detector**: adds a medium-window (`n_pr // 14`)
    step pass in `_spectral_metrics` guarded by active passband energy
    (`bef_m > -52.0 dB`) and transition-band gradient (`> 38.0 dB/kHz`).
  - Added 4 deterministic forensic unit tests (`test_22` to `test_25`) in
    `tests/test_forense_riguroso.py` (suite is now 329 tests).
- **Natural numeric track ordering (`natural_sort_key`)**: folder discovery in
  `motor_flac.py` and `verificar_flac.py` sorts numbered filenames in natural
  numeric order (`1, 2, ..., 9, 10, ..., 100, ...`) rather than ASCII order,
  and both the CLI and the GUI `ACTIVITY` console reorder deferred
  album-coherence tracks into sequential album order on batch completion.

### Fixed

- Capped the upper bound of the instantaneous cut-off slice in
  `PsychoacousticSTFTAccumulator.add` at `min(22050.0, sr * 0.49)` so natural
  ultrasonic decay in genuine `24/96` and `24/192` masters does not trigger a
  false `iqr_corte_hz > 550 Hz` jitter penalty.
- Fixed a blank results table in `gui.py` after large batches caused by
  `CTkFrame`'s default 200 px height inflating `table_canvas` `scrollregion`
  before `grid` geometry propagation, and scrolled the table to the top when a
  batch finishes.

## [2.5.0] - 2026-11-09

### Changed

- Vectorised the hot paths in `_motor_bits.py`, `_motor_espectral.py` and
  `motor_flac.py` (31 % faster on 24-bit/96 kHz tracks and 34 % faster with PNG
  rendering, with 100 % identical numerical outputs and verdicts):
  - Shared `<i4` quantisation per 24-bit block between STREAMINFO MD5 and
    `ForensicBitsAccumulator`, and passed C-contiguous buffers directly to
    `md5.update()`.
  - Made `_bitmap_low`, `_bitmap_reb1` and `_bitmap_reb2` slice views into
    `_bitmap_global` for a single scatter write per block, and replaced two
    full-array `np.partition` passes in `_evaluate_bitmap` with a 1,025-bin
    `np.bincount` histogram.
  - Replaced `np.log2` + a $2^{21}$-bin `np.bincount` in `analyze_bit_depth`
    with a 32-bit De Bruijn shift and a 32-bin histogram.
  - Replaced boolean-mask 2D copies in `PsychoacousticSTFTAccumulator.add` with
    contiguous index slices, an IEEE-754 ULP-exact linear threshold on the
    high-frequency band, vectorized MDCT sub-band gap checks and L2-hot STFT
    buffer reuse.
  - Added TrueType font caching, bounded RGBA legend compositing in
    `_draw_legend`, and $O(\log N)$ binary-search text truncation in
    `gui.FlacVerifierWindow._fit`.

### Fixed

- Fixed a track-header mismatch in `verificar_flac.execute_analysis` when a
  track was held back in `retenidas` for album-coherence evaluation and yielded
  at the end of the batch; `generate_pdf_report` also restores original album
  order via `indice_original`.
- Inserted deferred album-coherence rows in `gui.FlacVerifierWindow._add_row`
  at their `indice_original` position instead of appending them at the bottom
  of the table.
- Added an explicit `run_state` lifecycle (`idle`, `running`, `finished`,
  `cancelled`, `error`) in `FlacVerifierWindow` so an `informe` event arriving
  while draining the queue after subprocess exit cannot unlock controls before
  `__fin__`.
- Cancelled pending Tcl `after` callbacks on window teardown in
  `FlacVerifierWindow.destroy`.

## [2.4.0] - 2026-10-31

### Added

- A results surface in the GUI: a verdict hero, a per-track table with the
  verdict, score, bit depth, sample rate, dynamic range and MD5, row selection
  and a track detail inspector.
- The detail inspector shows the spectrogram thumbnail, the full text of every
  detected issue, the evidence (cut-off, LSB usage, clipping and MD5) and the
  actions to open the report or reveal the file.
- Light and dark themes with a live in-place switch; the preference is stored
  in `%APPDATA%\FLAC Verifier\settings.json` and applied on the next start.
- A start-screen screenshot in `README.md`.

### Changed

- Renamed the Python identifiers (functions, classes, constants, variables,
  parameters and attributes) from Spanish to English across the production
  modules and the test methods. The JSON protocol keys (`tipo`, `veredicto`,
  `archivo`, `coherencia_album`, `esp`, `bdi`, ...), the reportlab style names,
  the argparse destinations and the module file names keep their names. No
  threshold, score, verdict, cache schema or behaviour changed.
- Renamed the public version constants to `ENGINE_VERSION` and
  `CACHE_SCHEMA_VERSION`. Their values (`"9.8"` and `"2.3.0"`) and the cache
  schema are unchanged, so existing caches remain valid; external code that
  imports the previous names must update.
- The GUI and the PDF report share the design tokens in `theme.py`; the report
  keeps the same data and page budget with a new visual layer.

### Fixed

- The small text of the PDF report (table labels, headers and footer) now meets
  the 4.5:1 contrast target.
- The PDF explains why a spectrogram is missing and no longer says "No issues
  detected" for a file that could not be analysed.
- The theme switch recolours the window in place, without rebuilding it, and
  preserves the album, selection, scroll, auto-scroll, focus and log.
- The batch start line pluralises files and processes correctly.

## [2.3.0] - 2026-10-24

### Added

- Phase 8 forensic detectors: algebraic 24-bit lattice and 24-bit master
  safeguard (`AcumuladorForenseBits` with unique-level bitmaps and the global
  lattice p10), universal DSD/Scarlet Book/DSD128 profiles, codec `sfb21`
  islands, harmonic-exciter detection, acoustic centroid and low-HF attenuation,
  plus album coherence for the 8D acoustic exemption.
- A fail-closed 24-bit acoustic exemption: only an **active** LSB lattice
  qualifies; `partial`, `indeterminate` and missing LSBs no longer receive it,
  so a 24-bit upscale cannot buy the exemption.
- A signed harmonic-exciter step (an ascending step cannot fire the notch
  detector) and the 0.25 lower bound for `sfb21` islands.

### Changed

- `VERSION_MOTOR` is now `"9.8"`, which invalidates every earlier cache; the
  cache schema is `"2.3.0"`.
- Rule 5 is restored for 24-bit low-ceiling tracks after the acoustic
  exemption, so the 8D exemption cannot bypass the 24-bit format check.
- `PROBABLY LOSSLESS` tracks that the 8D acoustic exemption has already
  justified can now receive the album-coherence note in a homogeneous album
  (the individual cache entry still does not carry it).
- The test suite grew to **266 tests**; the benchmark reports **20/20**
  (sensitivity and specificity 100 %, Wilson 95 % CI 83.9–100 %), and the
  packaged executable passes **11/11** checks in Gate 5.

## [2.2.0] - 2026-10-22

### Added

- `tests/test_forense_riguroso.py`: ten deterministic forensic regression
  cases (analogue Hi-Res, sinc/aliasing upscales, joint-stereo and Opus
  transcodes, 24-bit padding/comb, silence and a long quiet intro).
- `tests/benchmark_validacion.py` and `VALIDATION.md`: reproducible 40-track
  balanced benchmark with TP/TN/FP/FN, sensitivity, specificity and Wilson 95%
  confidence intervals (100% / 100% on the synthetic corpus).
- Vector 1: Mid/Side high-band asymmetry (`caida_side_agudos_db`,
  `delta_corte_ms_hz`, `sospecha_joint_stereo`, `es_dual_mono`), which catches
  joint-stereo lossy encoders that cut the Side channel before Mid.
- Vector 2: `AcumuladorPsicoacusticoSTFT` reports the temporal jitter of the
  per-frame cut-off (`iqr_corte_hz`) and the ratio of MDCT spectral holes
  (`ratio_huecos_mdct`); both add a score penalty when their signature appears.
- Vector 3: `_diagnosticar_autenticidad_hires` classifies Hi-Res morphology:
  CD/DAT brickwall (`muro_brickwall_nyquist`), spectral mirror
  (`espejo_aliasing_detectado`), DSD/SACD noise shaping
  (`es_transferencia_dsd`) and genuine analogue roll-off
  (`rolloff_analogico_genuino`), which rescues the Queen-type master from the
  old CD-limit penalty.
- Vector 4: `AcumuladorForenseBits` audits the native 24-bit samples over the
  whole file: zero padding (`es_padded_16_en_24`) and the quantisation comb of a
  16-bit source scaled into 24 bits (`es_24bit_escalado_en_peine`, Shannon
  entropy and occupied LSB bins).
- Vector 5: the psychoacoustic accumulator detects the libopus fullband cliff at
  20.00 kHz (`sospecha_corte_opus_20k`, `caida_opus_20k_db`); it is scored only
  with corroborating codec evidence because a mastering low-pass can look the
  same in isolation.
- Vector 6: `_clasificar_naturaleza_clipping` separates Gibbs overshoot
  (`clipping_reconstruccion_lossy`) from a mastering limiter
  (`clipping_mastering_limitador`), and `aplicar_coherencia_album` raises 1–2
  acoustic/low-HF tracks to PROBABLY LOSSLESS when at least 80% of the album is
  GENUINE LOSSLESS in the same format.

### Changed

- The spectrogram is rendered with a NumPy 256-level LUT and Pillow
  (`renderizar_espectrograma_rapido`) instead of matplotlib. Matplotlib is no
  longer imported by the engine and is excluded from the PyInstaller spec.
- The spectral analysis no longer measures a fixed window: every block of the
  whole file is scored by its transient/high-frequency energy (RMS above
  -65 dBFS) and the most active ones, about 45-60 s of audio, are transformed.
  A quiet intro no longer lowers the measured ceiling of a genuine track.
- The STFT runs in float32 with `sliding_window_view` and Mid/Side separation,
  without `np.sqrt`, and the spectrogram shows the selected blocks.

### Planned

- Rename the remaining Spanish internal identifiers in the JSON protocol and the
  cache entries (field names such as `tipo`, `veredicto` or `desde_cache`, and
  the module-level constants) once that does not break external consumers.
- A trained classifier for the 320 kbps MP3 stored at 16 bits, which is at
  present a documented physical limit of the spectral analysis.

## [2.1.0] - 2026-10-14

A performance release: the analysis, the report and the interface were optimised
without changing a single verdict (the full result digest is identical on real
albums).

### Changed

- **Engine (streaming path).** The spectral cut-off search is vectorised with
  `np.cumsum` (O(n) instead of the Python window loop), `_pcm_bytes` dumps
  contiguous PCM buffers (`<i1`/`<i2`/`<i4`), the LSB sample is no longer buffered
  for ≤16-bit tracks, `|Z|²` is squared in place and the dynamic range uses
  `np.partition`. On a real 21-track album: **35 % less time** and up to **164 MB
  less peak RAM** with the spectrograms on.
- **Report.** `informe_pdf.py` builds the PDF lazily (`_FlujoPerezoso`), repeats the
  summary header on every page and shapes each spectrogram once.
- **Interface.** The event loop drains the engine queue in bounded batches (no more
  micro-freezes), the `__fin__` race is fixed, blocking dialogs are reserved for
  fatal errors, and the event list, the log and the queue are bounded.
- **Packaging.** The bundle drops `PIL._avif`, `reportlab.graphics`, the non-Agg
  matplotlib backends and the non-Windows `tkinterdnd2` builds: the `_internal`
  folder goes from 168 MB to 154 MB.

### Fixed

- The process-manager cancellation test (`test_gui.py`) was flaky: it raced the
  reader thread and could reap the fake process before `cancel()` ran. It now
  holds the stream open, and a new test covers the reader-thread liveness
  (`hilo_activo`) the window uses so as not to lose the final event.

### Removed

- The stale Vite build shell `Logo/index.html`; and HTML is kept out of GitHub's
  language statistics (`.gitattributes`: `*.html linguist-detectable=false`).

## [2.0.0] - 2026-09-18

The first release with continuous integration, an English code base and a version
number that means the same thing everywhere. The analysis is unchanged from
1.0.0: this version is about the project around it.

### Added

- Continuous integration on GitHub Actions: the 230 tests run on Ubuntu and
  Windows, with Python 3.10 and 3.12. The window tests skip on the Linux runner,
  where there is no display, and a stale run is cancelled when a new commit
  arrives on the same branch.
- Issue templates for bug reports and feature requests, a contact list pointing at
  the verdict model, the known limits and the comparison page, and a pull-request
  template that asks about the four invariants of the project
  (`freeze_support()` first, `VERSION_MOTOR` bump, no Spanish in what is shipped,
  presentation layers stay thin).
- `COMPARISON.md`: how this project and FLAC Detective differ, both described on
  their own terms, with the commands that produce every number in the tables.
- `test_empaquetado.py` now checks that the Windows version resources announce the
  same number as `main.py`, and that the build banner reads it from there instead
  of keeping its own copy.

### Changed

- **The code is English now, not only the interface.** Every comment and every
  docstring in the repository was translated from Spanish, including the
  PyInstaller `.spec`, `version_info.txt`, `requirements.txt` and `.gitignore`, and
  the remaining Spanish string literals in the tests. `test_idioma.py` fails if
  Spanish prose appears in any Python file — not just in the text the user can see.
  Internal identifiers (variable and function names, JSON protocol keys such as
  `tipo` or `veredicto`) keep their names: renaming them would break every consumer
  of the protocol.
- The version is announced from a single place. `build.py` read it from a literal in
  its own banner, which is how the executable, the tag and the release ended up
  saying different numbers.

### Fixed

- The program, its Windows version resources, the release tag and the ZIP archive
  announced different versions (`1.0.0` inside, `2.0.0` outside). Everything says
  **2.0.0** now, and a test keeps it that way.
- A race in `test_gui.TestGestorProceso`: the fake process served a single line, so
  the reader thread could reach the end of the stream, call `wait()` and leave the
  manager reporting "not running" before the test asserted the opposite. It passed
  on three of the four CI jobs and failed on Ubuntu with Python 3.10, where the
  thread won the race. The fake can now hold its stream open until the test
  releases it (`ProcesoFalso(..., bloquear=True)`), which makes both that test and
  the "does not start twice" one deterministic instead of timing-dependent.
- Three user-visible strings that the interface translation had missed:
  `MD5 verified:` in the interactive CLI, `Not found:` in the file-not-found
  alert of the GUI, and the console labels of the executable checker
  (`interface`, `engine`, `FAILED`, `RESULT:`).
- The `--seg` flag that the GUI still sent to the engine after the command-line
  flags were renamed to English, and the protocol status codes
  (`match`/`mismatch`, `far_cutoff`/`near_nyquist_cutoff`, `empty`/`partial`/
  `active`) that the CLI still compared against their old Spanish values.

## [1.0.0] - 2026-09-16

First published version: a FLAC authenticity verifier that combines integrity
checks with spectral analysis and refuses to guess when the evidence is not
conclusive.

### Added

- **Real STREAMINFO MD5 verification.** The MD5 stored in the FLAC metadata is
  recomputed over the decoded audio and compared bit by bit. It is the only
  objective, total check of the format, and it works at 8, 16 and 24 bits. A
  mismatch is a hard integrity error: the file gets no verdict at all.
- **Spectrogram and per-album PDF report.** `flac_verifier_report.pdf` is written
  in the analysed folder with a cover and an executive summary by colour, one
  record per song (verdict with score, technical table, plain-language list of
  problems and the embedded spectrogram) and a final overall table. Each record
  is measured before it is written so that it always fits on its page, and no PDF
  failure can abort the batch.
- **Graphical interface** (`gui.py`, CustomTkinter): two-column window, folder
  drag-and-drop when `tkinterdnd2` is installed, per-file progress, colour-coded
  log, a batch summary card and a button that opens the generated PDF.
- **NDJSON protocol** for the engine, with one JSON line per event
  (`inicio`, `resultado`, `informe`, `aviso`, `fin`, `error`), strict JSON output
  with no `NaN` or `Infinity`, and `stderr` left free for diagnostics.
- **Per-file cache**, keyed by file identity (size, modification time and the
  STREAMINFO MD5), analysis parameters (`VERSION_MOTOR`, spectral mode, seconds)
  and the PNG render fingerprint.
- **Windows packaging:** PyInstaller `onedir` build with two executables
  (`FLAC_Verifier.exe` windowed and `flac_motor.exe` with a console), driven by
  `build.py` and checked by `comprobar_ejecutable.py`.

### Changed

- **Architecture split.** All the analysis logic lives in `motor_flac.py`;
  `verificar_flac.py`, `gui.py` and `informe_pdf.py` are presentation layers that
  must not reimplement it, and tests enforce that. `main.py` became the single
  entry point: a thin flag dispatcher towards the GUI, the CLI, the engine or the
  internal `--motor-cli` mode used by the windowed executable.
- **Coherent verdicts.** The verdict is now positive evidence minus confirmed
  defects, with a fixed threshold scale (`GENUINE LOSSLESS` ≥ 0.80,
  `PROBABLY LOSSLESS` ≥ 0.60, `SUSPICIOUS` ≥ 0.40, otherwise `PROBABLE UPSCALE`).
  Any blocking defect caps the verdict, so a file with a transcoding signature can
  never come out as genuine, and an inconclusive file is reported as
  `indeterminate` instead of being accused.
- **Honest bit-depth reporting.** The low-bit test reports the effective
  resolution actually in use (`empty`, `partial`, `active`, `indeterminate`),
  distinguishes a 16-bit payload inside a 24-bit container, and can only
  disprove — never confirm — provenance. The bit-depth penalty never upgrades a
  verdict on its own.
- **Nyquist-distance cut-off rule.** A spectral step only counts as an accusation
  when it sits below 88 % of Nyquist; a cut-off glued to Nyquist (the anti-alias
  filter of a legitimate CD master) is reported as inconclusive rather than
  penalised. `corte_artificial` now means "confirmed accusation".
- **Hi-res correction above 48 kHz.** The proportional-to-Nyquist rule is replaced
  by the CD limit (22.05 kHz) as the reference, because 70 % of 48 kHz is
  33.6 kHz, a frequency where real acoustic music has no energy. A 96/24 master
  that rolls off at 30 kHz is no longer flagged as a destroyed band, while a
  CD-to-96-kHz upscale is still detected.
- **Robustness and validation hardening.** No broken file can raise an exception
  or abort a batch: invalid signatures, corrupt or truncated STREAMINFO, failed
  decodes, digital silence and MD5 mismatches all produce their own rejected
  record with a reason and no invented verdict. Empty-band guards prevent the
  `NaN` that used to produce invalid JSON at 32 kHz, and redirections to a file
  (never a pipe) keep the child processes of the parallel path from hanging.
- **Performance.** A single streaming pass replaced the array-based path:
  measured on 10 minutes of audio the peak memory drops from 1818 MB to 151 MB at
  16/44.1 and from 6142 MB to 296 MB at 24/96, with the same verdicts, the same
  score and the same problems. The changes behind it are the clipped vectorised
  run detector (423 MB → 4 MB), the block-wise spectrum (45 s STFT pieces,
  ×7.9 less memory), the `imshow` spectrogram renderer instead of
  `pcolormesh(shading="gouraud")` (339.7 s → 75.1 s on a 17-track album), the
  process pool (75.1 s → 26.3 s) and the per-track cache (26.3 s → 2.7 s).
- **Documentation** (`README.md`) rewritten in English with the measured numbers,
  the JSON protocol contract, the verdict model, the known limits and the
  antivirus false-positive guidance.
- **Internationalisation.** The user interface, the command-line flags, the JSON
  protocol values, the PDF report and all the documentation are now in English,
  because the project is published for an international audiophile community.
  The CLI flags became `--path`, `--mode`, `--seconds`, `--png`, `--pdf`,
  `--workers` and `--no-cache`; the spectral modes became `center` (default),
  `full` and `seconds`; the verdict labels became `GENUINE LOSSLESS`,
  `PROBABLY LOSSLESS`, `SUSPICIOUS`, `PROBABLE UPSCALE` and `indeterminate`; the
  report is `flac_verifier_report.pdf` and the spectrogram folder is
  `_spectrograms`. `build.py` now takes `--no-clean`, and
  `comprobar_ejecutable.py` takes `--folder`, `--album` and `--quick`. Code
  comments and docstrings are still in Spanish: a known pending task.

### Fixed

- A NaN could reach the JSON protocol for sample rates where the high band does
  not exist (32 kHz, Nyquist 16 kHz); non-finite floats are now emitted as `null`.
- A spectral ceiling measured in proportion to Nyquist wrongly flagged legitimate
  hi-res masters; the hi-res rule now uses the CD limit as reference.
- The spectrogram title and image could be split across pages when a song had a
  long list of problems; they are now kept together and the image is fitted to the
  remaining space.
- The packaged windowed executable needed the `--motor-cli` flag to deliver the
  NDJSON protocol at all, because a `--noconsole` build leaves
  `sys.stdout = None`; the engine is now also shipped as `flac_motor.exe`, with a
  real console, and the GUI always passes the flag.
- `multiprocessing.freeze_support()` is pinned as the first statement of
  `main.py`; without it every parallel worker relaunched the whole application
  (eight windows in a loop in the windowed executable).

### Security

- The build disables UPX and adds version resources and an icon to both
  executables, to reduce antivirus false positives. No UPX compression, no
  signature: the README documents how to add an exclusion and warns that code
  signing is the real solution for serious distribution.
