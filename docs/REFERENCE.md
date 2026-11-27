# Technical Reference

This document holds the in-depth technical reference for **FLAC VERIFIER**:
the NDJSON streaming protocol, the forensic scoring model, the physical limits
of spectral analysis, the cache and performance architecture, and the Windows
packaging details.

For installation, everyday usage and the validation benchmark, see
[`README.md`](../README.md).

---

## Contents

1. [JSON protocol contract](#json-protocol-contract)
2. [How the verdict is decided](#how-the-verdict-is-decided)
3. [Per-album PDF report](#per-album-pdf-report)
4. [Known physical limits](#known-physical-limits)
5. [Performance and streaming architecture](#performance-and-streaming-architecture)
6. [Cache architecture](#cache-architecture)
7. [Windows packaging and verification](#windows-packaging-and-verification)

---

## JSON protocol contract

The engine writes **one JSON line per event** to `stdout` and always ends with
`fin`. `stderr` is left free for diagnostics. Every line is strict JSON: no
`NaN` and no `Infinity` (non-finite floats are emitted as `null`).

### Events

| `tipo` | When | Fields |
|---|---|---|
| `inicio` | Once, before analysing | `total` (number of files), `workers` (processes), `paralelo` (bool), `cache` (bool) |
| `resultado` | Once per file | See below |
| `informe` | Only with `--pdf`, when the PDF was generated successfully | `ruta`, `paginas`, `total`, `desde_cache` |
| `aviso` | Only with `--pdf`, if the PDF could not be generated | `mensaje` |
| `fin` | Once, when finished | — |
| `error` | Only if the path is not valid or there are no `.flac` files | `mensaje` |

No file can abort the batch: an unreadable file produces its own `resultado`
with `error` and the process continues until it emits `fin`.

### `resultado` — successful analysis

```json
{
  "tipo": "resultado", "indice": 1, "total": 3, "indice_original": 0,
  "archivo": "track.flac", "ruta": "C:/Music/track.flac",
  "meta": { "valido": true, "sample_rate": 44100, "bits_per_sample": 24,
            "canales": 2, "duracion": 245.3, "md5": 12345,
            "vendor": "reference libFLAC 1.4.3 20230623" },
  "md5":  { "estado": "match", "calculado": "76a6…", "almacenado": "76a6…" },
  "accuraterip": { "aplica": false },
  "clip": { "runs_clip": 0, "hay_clipping": false },
  "dr": 13.1,
  "bdi": { "aplica": true, "lsbs": "active",
           "resolucion_efectiva_bits": 24, "fraccion_16bit_grid": 9.8,
           "forense_bits": { "es_padded_16_en_24": false,
                             "es_16bit_dither_en_24": false,
                             "trailing_zeros_lsb": 0,
                             "lsb8_entropia_bits": 7.2,
                             "lsb8_cajones_ocupados": 250,
                             "lsb8_ocupacion_interior": 0.96,
                             "paso_reticula_mediano": 1.0,
                             "es_24bit_escalado_en_peine": false },
           "conclusion": "…" },
  "esp": { "error": null, "techo_hz": 20004, "techo_rel_nyquist": 0.9072,
           "clase_corte": "near_nyquist_cutoff", "corte_detectado": true,
           "frecuencia_corte": 19821, "distancia_nyquist": 0.8989,
           "corte_artificial": false, "ratio": 0.42439, "ratio_aplica": true,
           "var_alta_db": 683.6, "separacion_stereo": 0.438, "nyquist": 22050,
           "perfil_dsd": null, "gradiente_borde_db_khz": 12.4,
           "centroide_hz": 1420.0, "decaimiento_acustico_suave": false,
           "rolloff_banda_limitada_genuina": false,
           "brecha_p90_p10_agudos_db": 0.0, "ratio_apagado_sfb21": 0.0,
           "islas_mdct_sfb21": false, "caida_modulacion_20k_db": 12.1,
           "brecha_modulacion_20k_db": 4.2, "modulacion_psicoacustica_20k": false,
           "mdct_lattice_detected": false, "mdct_lattice_snr": 0.0,
           "mdct_lattice_family": null,
           "pre_echo_ratio_db": 0.0, "mgd_hf_dispersion": 0.0,
           "corr_env_excitador": 0.0, "muesca_excitador_db": 0.0,
           "sospecha_excitador_armonico": false,
           "sbr_qmf_detectado": false, "sbr_qmf_correlacion": 0.0,
           "sbr_qmf_z": 0.0, "espectrograma": null },
  "score": 0.7, "veredicto": "PROBABLY LOSSLESS",
  "problemas": ["…"],
  "notes": ["…"],
  "evidencias_forenses": ["Spectral ceiling 20.0 kHz (91% of Nyquist)",
                          "STREAMINFO MD5 verified (match)"]
}
```

#### Facts (`meta`, `md5`, `clip`, `dr`, `bdi`, `esp`)

| Field | Meaning |
|---|---|
| `meta.md5` | Value of the STREAMINFO MD5 (128-bit integer), or `null` |
| `md5.estado` | `match` · `mismatch` · `absent` · `unverifiable` |
| `md5.calculado` / `almacenado` | Hashes as 32-character hex strings |
| `clip.runs_clip` | Runs of 3 or more consecutive samples at the ceiling (≥ 0.9999) |
| `dr` | **Estimated** dynamic range in dB, or `null` if the file is shorter than 6 s |
| `bdi.aplica` | `false` on 16-bit files or lower |
| `bdi.lsbs` | `empty` · `partial` · `active` · `indeterminate` |
| `bdi.resolucion_efectiva_bits` | Bits actually used (16 inside a 24-bit container → `16`) |
| `bdi.fraccion_16bit_grid` | % of samples aligned with the 16-bit grid |
| `bdi.forense_bits` | Native-integer audit (24-bit and 32-bit containers): `es_padded_16_en_24`, `es_16bit_dither_en_24`, `trailing_zeros_lsb`, `lsb8_entropia_bits`, `lsb8_cajones_ocupados`, `lsb8_ocupacion_interior`, `muestras_pasaje_bajo`, `paso_reticula_mediano`, `es_24bit_escalado_en_peine`, `es_flujo_dop`, `dop_pares_inspeccionados`, `dop_ratio` |
| `esp.techo_hz` | Highest frequency with energy above −60 dB |
| `esp.techo_rel_nyquist` | That ceiling as a fraction of Nyquist |
| `esp.clase_corte` | `no_cutoff` · `near_nyquist_cutoff` (≥ 88 % of Nyquist) · `far_cutoff` (< 88 %) |
| `esp.frecuencia_corte` / `distancia_nyquist` | Where the step is and how far it is from Nyquist |
| `esp.corte_artificial` | `true` **only** if the cut-off is far away (confirmed accusation) |
| `esp.ratio` / `ratio_aplica` | Energy in the band above 18 kHz versus 15–18 kHz; `false` if that band does not exist |
| `esp.var_alta_db` | Variance of the spectrum above 18 kHz |
| `esp.separacion_stereo` | L−R difference in the high band versus the mid band; `null` in mono |
| `esp.caida_side_agudos_db` | Extra Mid−Side drop in 15.5–19.8 kHz versus 2–8 kHz (joint-stereo collapse) |
| `esp.delta_corte_ms_hz` | Mid cut-off minus Side cut-off: a lossy joint-stereo encoder cuts Side first |
| `esp.sospecha_joint_stereo` | `true` when the Side channel collapses and cuts before Mid |
| `esp.msc_lo` / `esp.msc_hi` | Inter-channel magnitude-squared coherence (MSC) in `2–6 kHz` and peak post-cliff HF coherence (`6–18.5 kHz` sliding 1 kHz step) |
| `esp.msc_step_hf` / `esp.ipd_phase_lock_hf` | Sharpest 1 kHz HF coherence jump (`post - pre`) and median `|sin(IPD)|` phase-lock residual above the cliff |
| `esp.stereo_phase_coupling` | `true` when independent stereo transitions abruptly into phase-locked high-frequency coupling (AC-3 channel coupling / Opus intensity stereo; `SOFT_BLOCKING`) |
| `esp.es_dual_mono` | `true` when L and R are identical (Side more than 45 dB below Mid) |
| `esp.iqr_corte_hz` | Interquartile range of the per-frame cut-off in high-energy frames (codec jitter) |
| `esp.ratio_huecos_mdct` | Fraction of high-energy frames with a 600 Hz MDCT hole (> 22 dB below both neighbours) |
| `esp.muro_brickwall_nyquist` | `true` for a Hi-Res container with a cliff at 22.05/24.0 kHz (upscale with a sinc filter) |
| `esp.espejo_aliasing_detectado` | `true` when the 23–29 kHz band mirrors 15–21 kHz (unfiltered interpolator) |
| `esp.rolloff_analogico_genuino` | `true` for a smooth analogue/acoustic roll-off with a continuous ultrasonic floor |
| `esp.gradiente_nyquist_db_khz` | Steepest slope around the CD/DAT Nyquist, in dB/kHz |
| `esp.salto_ultrasonico_db` | Level step between 18–20.8 kHz and 23.5–36 kHz |
| `esp.correlacion_espejo_22k` | Detrended Pearson r of the mirrored bands |
| `esp.es_transferencia_dsd` | `true` for a DSD/SACD noise-shaping ramp in 24–40 kHz |
| `esp.perfil_dsd` | DSD profile: `direct_24_40k`, `scarlet_30k`, `dsd128_40_80k`, or `null` (no DSD ramp) |
| `esp.gradiente_borde_db_khz` | Steepest slope of the transition band around the ceiling, in dB/kHz |
| `esp.centroide_hz` | Spectral centroid: a low-centroid acoustic mix sits below 1800 Hz |
| `esp.decaimiento_acustico_suave` | `true` for a low-centroid, no-cutoff acoustic decay with a gentle (< 20 dB/kHz) edge (≥ 11 kHz, or 7.5–10.5 kHz with an analogue floor or continuous post-ceiling acoustic decay): the acoustic exemption candidate |
| `esp.rolloff_banda_limitada_genuina` | `true` for a smooth (< 18 dB/kHz), no-cutoff, 10–15.4 kHz ceiling with an analogue noise floor: vintage/archival/vinyl band-limited roll-off candidate |
| `esp.native_24bit_hf_tail` | `true` on `44.1–48 kHz` streams when sub-ceiling high-frequency energy tapers monotonically into a genuine 24-bit studio converter stopband floor (`<= -105 dBFS` at `44.1 kHz`, `<= -98 dBFS` above `44.1 kHz`) |
| `esp.noise_shaped_16_in_24` | `true` on 24-bit containers when a quiet block reveals a rising noise-shaped 16-bit dither shelf (`+6 dB` slope `12–16 -> 18–21.5 kHz`, flat noise floor `>= -94 dBFS`, no 16-bit grid alignment) |
| `esp.quiet_hf_slope_db` / `quiet_hf_flatness` / `quiet_hf_floor_dbfs` | Quiet-block `12–16 -> 18–21.5 kHz` noise-floor slope (dB), spectral flatness (`GM/AM`), and P10-calibrated floor level (dBFS) used by `noise_shaped_16_in_24` |
| `esp.brecha_p90_p10_agudos_db` | P90–P10 energy gap in 16–20.5 kHz: large values expose intermittent sfb21 islands |
| `esp.ratio_apagado_sfb21` | Fraction of high-energy frames whose sfb21 band is switched off |
| `esp.islas_mdct_sfb21` | `true` for intermittent 16–20.5 kHz islands (28 dB gap, 0.25–0.85 shut-off): lowpass-disabled codec signature |
| `esp.caida_modulacion_20k_db` / `brecha_modulacion_20k_db` | Median drop and P95–P05 modulation gap between 16.0–19.5 kHz and 20.2–21.8 kHz across active frames |
| `esp.modulacion_psicoacustica_20k` | `true` when the 20.2–21.8 kHz band exhibits both a deep drop and dynamic psychoacoustic modulation |
| `esp.mdct_lattice_detected` | `true` when orthogonal lapped transform / subband inversion recovers a phase-locked codec quantization lattice (`MDCT-1024`, `PQMF-576`, `MDCT-512`, `PQMF-32` or `CELT-960`) at CD/48 kHz or via pure-NumPy Hi-Res decimation (`88.2–192 kHz`) |
| `esp.mdct_lattice_snr` / `esp.mdct_lattice_family` | Peak robust Z-score / harmonic SNR across sample offsets and identified filterbank family (`MDCT-1024 (Vorbis/AAC)`, `PQMF-576 (MP3)`, `MDCT-512 (AC-3/E-AC-3)`, `PQMF-32 (Musepack)` or `CELT-960 (Opus)`) |
| `esp.corr_env_excitador` | Correlation between the 8–14 kHz envelope and the high band |
| `esp.muesca_excitador_db` | Depth of the notch left where the original high band was cut |
| `esp.sospecha_excitador_armonico` | `true` when correlation > 0.92 **and** notch ≥ 15 dB: harmonic exciter / notched SBR signature |
| `esp.sbr_qmf_detectado` / `esp.sbr_qmf_correlacion` / `esp.sbr_qmf_z` | Local-whitened additive QMF subband transposition detector (`3.5–9.5 kHz` shift sweep) for seamless HE-AAC v1/v2 SBR without a boundary notch (CD and Hi-Res rates, including guarded low-ceiling `5.5–14.5 kHz` tracks requiring `r >= 0.34` or `Z >= 15.0`) |
| `esp.neural_vocoder_detected` / `vocoder_phase_coh_diff` / `vocoder_hf_comb_z` / `vocoder_comb_spacing_hz` | Neural vocoder transposed-convolution detector: upper-band phase coherence delta (`14–21.5 kHz` vs `1–7 kHz`), peak log-spectrum comb quefrency Z-score (`50–250 Hz` spacing), and detected comb spacing in Hz (`44.1/48 kHz` and decimated Hi-Res) |
| `esp.spectral_folding_detected` / `spectral_folding_corr` / `spectral_folding_z` / `spectral_folding_pivot_hz` | 2D Gram time-domain spectral-folding BWE detector: peak mirrored lower/upper trajectory Pearson correlation, robust Z-score, and estimated pivot frequency in Hz across `4–16 kHz` |
| `esp.es_estacionario` | `true` for a stationary harmonic/tonal programme (`std(env_4_11) < 1.8 dB` and `13.5–19.5 kHz` tonal crest `P95 - P50 >= 12 dB`), protecting sustained electronic synth drones from false subband-hole or modulation flags |
| `esp.familias_evidencia_lossy` / `esp.corroboracion_multifamilia` | Independent lossy evidence families triggered (`filterbank_lattice`, `spectral_cutoff`, `spectral_envelope`, `temporal_modulation`, `stereo_collapse`, `bit_depth_container`) and whether multi-family or hard-proof corroboration holds |
| `esp.finding_codes` | Typed canonical `FindingCode` identifiers emitted alongside `problemas` (cleared on album-coherence rescue; appends `album_lossy_consistency` on album-coherence lossy conviction) |
| `esp.psycho_detector_error` | Optional diagnostic string emitted only if the streaming psychoacoustic STFT accumulator hits an internal runtime error (omitted on clean runs); similarly `vocoder_detector_error` and `folding_detector_error` appear only on runtime exceptions |
| `esp.sospecha_corte_opus_20k` | `true` for a 20.00 kHz cliff (> 45 dB between 19.85 and 20.15 kHz) with a flat 18–19.7 kHz band |
| `esp.caida_opus_20k_db` | Depth of that cliff in dB |
| `esp.clipping_eventos` / `clipping_longitud_media` | Saturation runs and their mean length |
| `esp.clipping_fraccion_cortos` | Fraction of clipping events lasting only 1–2 samples |
| `esp.clipping_reconstruccion_lossy` | `true` for Gibbs overshoot (short events with a band limit below 20.5 kHz) |
| `esp.clipping_mastering_limitador` | `true` for 4+ sample plateaus with content up to Nyquist (observation, not a defect) |
| `esp.espectrograma` | Path of the PNG, or `null` |

### `resultado` — rejected file

The same identification fields plus `error`, and **without** `veredicto` or
`score`. A verdict is never emitted for a file that could not be analysed
completely:

| `error` (prefix) | Cause |
|---|---|
| `Not a valid FLAC signature` | Does not start with `fLaC`, or is empty |
| `Could not read FLAC metadata (…)` | STREAMINFO unreadable or corrupt |
| `Could not decode the audio (…)` | Truncated or corrupt (decoded samples checked against declared count) |
| `digital silence: …` | Peak below 1 LSB: no signal to analyse |
| `STREAMINFO MD5 does not match …` | Hard integrity failure |

---

## How the verdict is decided

The score is **positive evidence minus confirmed defects**. Only measurements
that reliably separate genuine masters from transcodes or inflated containers
contribute.

### Positive evidence

| Block | Weight | Condition |
|---|---|---|
| Full high band | 3 | Up to 48 kHz: ceiling ≥ 88 % of Nyquist (1.5 if ≥ 70 %). Above 48 kHz: ceiling ≥ 22.05 kHz, the CD limit |
| No anomalous step | 3 | `clase_corte == no_cutoff` |
| Hi-res content | 3 | Only if `sample_rate ≥ 88.2 kHz`: ceiling ≥ 22.05 kHz (1.5 if ≥ 18 kHz) |
| High/mid ratio | 2 | > 0.05 (1 if > 0.01) |
| High-band variance | 1 | > 3 dB² |
| Stereo separation | 1 | > 0.1 (not applicable in mono) |
| Analogue/DSD morphology | 3 | `rolloff_analogico_genuino` or `es_transferencia_dsd`: the declared band is backed by a smooth roll-off / DSD ramp |
| Acoustic / archival roll-off exemption | 1.5 | `decaimiento_acustico_suave` or `rolloff_banda_limitada_genuina` + active LSB lattice + no lossy signature |

### Confirmed defects

| Defect | Subtracts | Condition |
|---|---|---|
| Far artificial cut-off | 0.40 | Step below 88 % of Nyquist, including low-band brickwall steps in 6.5–12.5 kHz (blocks; hard proof) |
| Destroyed high band | 0.40 | Up to 48 kHz: ceiling < 70 % of Nyquist (unless exempted by smooth acoustic/archival roll-off with clean bits and no codec lattice; soft-blocking when isolated -> caps at `SUSPICIOUS`). Above 48 kHz: ceiling < 22.05 kHz (blocks) |
| Hi-res with no high content | 0.40 | ≥ 88.2 kHz and ceiling < 18 kHz (blocks) |
| Empty LSBs | 0.40 | > 97 % of samples on the 16-bit grid (blocks) |
| Partially inactive LSBs | 0.15 | 80–97 % (does not block) |
| Declared resolution with no support | 0.25 | Claims > 16 bits at CD/48 kHz rate with no content above the CD band (blocks, unless backed by active 24-bit LSBs, smooth acoustic/archival decay and `native_24bit_hf_tail`) |
| Noise-shaped 16-in-24 dither | 0.30 | Rising 16-bit noise-shaped dither shelf in quiet 24-bit blocks (`noise_shaped_16_in_24`, soft-blocking) |
| Joint-stereo collapse | 0.30 | Side channel drops at HF (`sospecha_joint_stereo`, blocks) |
| High-frequency stereo phase coupling | 0.25 | Abrupt MSC coherence cliff with near-zero IPD residual (`stereo_phase_coupling`, soft-blocking; checked via `elif` after `sospecha_joint_stereo`) |
| Cut-off jitter | 0.20 / 0.30 | Moderate (550–1800 Hz): 0.20 advisory. Extreme (> 1800 Hz without stationarity, analogue roll-off or DSD): 0.30 (blocks at `SUSPICIOUS`) |
| MDCT holes | 0.25 | > 10 % of high-energy frames with a 600 Hz hole (does not block) |
| sfb21 islands | 0.25 | Intermittent 16–20.5 kHz islands (blocks) |
| Upper-band 20 kHz modulation | 0.30 | Frame-level drop and psychoacoustic modulation in 20.2–21.8 kHz (blocks) |
| Lapped transform / subband lattice | 0.55 | Phase-locked `MDCT-1024` (including mid-band `8.5–14.8 kHz` sub-splice probe), `PQMF-576`, `MDCT-512`, `PQMF-32` or `CELT-960` lattice (`mdct_lattice_detected`, blocks; also runs on `88.2–192 kHz` via pure-NumPy decimation) |
| Neural vocoder synthesis comb | 0.55 | Periodic HF spectral comb (`50–250 Hz`) with phase-locked upper band (`neural_vocoder_detected`, `HARD_PROOF`, blocks) |
| Time-domain spectral folding BWE | 0.55 | Mirrored upper/lower spectral trajectories around a `4–16 kHz` pivot (`spectral_folding_detected`, `HARD_PROOF`, blocks; suppressed when SBR/exciter already fired) |
| Harmonic exciter / notched SBR | 0.30 | Envelope correlation > 0.92 and notch ≥ 15 dB (blocks) |
| Seamless QMF SBR | 0.55 | Additive QMF subband transposition (`sbr_qmf_detectado`, blocks; guarded below `14.5 kHz` ceiling) |
| Opus 20 kHz cliff | 0.20 | 20.00 kHz cliff with other codec evidence (does not block) |
| Gibbs clipping | 0.25 | 1–2 sample clipping events with a band limit < 20.5 kHz (does not block) |
| 16-in-24 padding / micro-dither | 0.40 | Trailing zero bits or interior modulo-256 occupancy < 5 % (blocks) |
| 24-bit quantisation comb | 0.40 | Global lattice step in the 8 LSBs (blocks) |
| Brickwall / aliasing | 0.40 | Hi-Res cliff at 22.05/24.0 kHz or mirrored 23–29 kHz (blocks) |

**Thresholds:** ≥ 0.80 `GENUINE LOSSLESS` · ≥ 0.60 `PROBABLY LOSSLESS` ·
≥ 0.40 `SUSPICIOUS` · `< 0.40` `PROBABLE UPSCALE`. Any blocking defect caps the
verdict at `SUSPICIOUS` (score ≥ 0.50) or `PROBABLE UPSCALE`. Condemning a track
as `PROBABLE UPSCALE` requires `corroboracion_multifamilia` (`≥ 2` independent
evidence families in `familias_evidencia_lossy` or a deterministic structural
`HARD_PROOF` such as a filterbank lattice, far artificial cut-off, Hi-Res
brickwall/aliasing or bit-depth container proof); an isolated, uncorroborated
soft-blocking heuristic (including unexplained low spectral ceiling without a
cut-off or codec evidence) is capped at `SUSPICIOUS` (`score ≥ 0.50`).

---

## Per-album PDF report

With `--pdf` (or by checking *Generate PDF report* in the GUI),
`flac_verifier_report.pdf` is generated in the album folder by default. Passing
`--output-dir PATH` (or setting **PDF folder** in the GUI) writes reports to
that directory as `<Album>_flac_verifier_report.pdf` (adding parent-folder or
numeric suffixes on name collisions) so read-only music folders remain
untouched:

| Page | Content |
|---|---|
| **Cover** | Title, album path, timestamp and **executive summary** (verdict counts and percentages) |
| **One per song** | File name, verdict chip with score, technical data table (format, duration, size, MD5 integrity, clipping, dynamic range, LSB utilisation, spectral ceiling, cut-off, ratio, variance, stereo separation), detected issues (or technical notes when clean) and embedded spectrogram |
| **Final** | Overall summary table with every track in natural numeric order |

The spectrogram height is fitted dynamically (34–62 mm) so a track's card never
overflows onto a second page. A failed PDF write emits an `aviso` event and
never aborts the analysis.

---

## Known physical limits

1. **Unconstrained ~500 kbps VBR or post-processed transcodes whose sample grid was destroyed:**
   full-bandwidth high-bitrate codecs up to 320 kbps (`Vorbis -q9`, `AAC 320`
   with `--cutoff 22000`, `LAME CBR 320` with `--lowpass -1`, `LAME V0`,
   `Musepack SV7/SV8`, `Opus 160/256`, `AC-3 / E-AC-3`) are caught by Phase 11/12/15
   orthogonal lapped transform & subband inversion (`MDCT-1024`, `PQMF-576`,
   `MDCT-512`, `PQMF-32`, `CELT-960`, including `88.2–192 kHz` Hi-Res containers
   via pure-NumPy decimation) as long as the decoded sample grid is intact. At unconstrained
   `Vorbis -q10` (~500 kbps, half-CD bitrate), perceptual dead-zone zeroing
   vanishes across the 1,024 MDCT bins, and if a transcode is pitch-shifted,
   time-stretched or re-recorded through an analogue loop, the sample-level
   phase lock is broken. Similarly, while transposed-convolution neural vocoders
   (HiFi-GAN / BigVGAN comb + phase lock), time-domain spectral-folding BWE, and
   AI-spliced lossy bases (`8.5–14.8 kHz` `MDCT-1024`) are detected in Engine
   `17.0`, a **pure full-track `iSTFT` neural vocoder without transposed
   convolutions** (which synthesizes fresh incoherent high-frequency phase
   directly in the STFT domain and re-synthesizes the entire lower band so no
   lossy MDCT lattice or spectral folding survives) remains an open physical
   limit without learned model weights.
2. **CD master brickwall-filtered below 70 % of Nyquist:** a steep (> 18 dB/kHz)
   low-pass below 15.4 kHz without an analogue floor is scored as `PROBABLE
   UPSCALE`; genuine solo-acoustic (`decaimiento_acustico_suave`) and vintage
   analogue/archival (`rolloff_banda_limitada_genuina`) masters with smooth
   roll-off and no codec lattice are spared as `PROBABLY LOSSLESS`.
3. **`dr` is an informative mono RMS/peak estimate**, not the reference 3 s
   gated DR meter, and does not affect the score.

---

## Performance and streaming architecture

The engine decodes each track in **a single streaming pass** with bounded
memory (`FRAMES_BY_BLOCK = 262144` (`1 << 18`), `SPECTRO_SECONDS = 45`):

| Fixture (`--mode full`) | Before streaming | Current streaming | Improvement |
|---|---|---|---|
| 10 min 16/44.1 | 5.4 s / 1,818 MB | **4.3 s / 151 MB** | ×1.3 faster, ×12 less RAM |
| 10 min 24/96 | 18.4 s / 6,142 MB | **7.1 s / 296 MB** | ×2.6 faster, ×20.7 less RAM |
| 17-track 24/96 album + PDF (first pass) | 339.7 s | **26.3 s** | ×12.9 faster |
| 17-track 24/96 album + PDF (cached) | 339.7 s | **2.7 s** | ×127 faster |

---

## Cache architecture

Working PNG spectrograms and per-track cache entries live outside the music
folder by default inside a centralized OS user cache bucket keyed by a 16-char
SHA-256 hash of the normalized album directory (`FLAC_VERIFIER_CACHE_DIR`
overrides the root):

- Windows: `%LOCALAPPDATA%\FLAC_Verifier\cache\<bucket_hash>\_cache\<file>.<mode>.json`
- macOS: `~/Library/Caches/FLAC_Verifier/cache/<bucket_hash>/_cache/<file>.<mode>.json`
- Linux / POSIX: `$XDG_CACHE_HOME/flac-verifier/cache/<bucket_hash>/_cache/<file>.<mode>.json` (or `~/.cache/flac-verifier/cache/...`)

An entry is reused only if all three fingerprints match:

| Key component | Fields checked |
|---|---|
| File identity | Size + modification time (`mtime_ns`) + **STREAMINFO MD5** |
| Analysis parameters | `ENGINE_VERSION` (`"17.2"`), `CACHE_SCHEMA_VERSION` (`"2.10.0"`), analysis window parameters |
| Render fingerprint | `render_fingerprint()` and on-disk PNG existence |

---

### Windows packaging and verification

```powershell
python build.py                                     # cleans, builds and verifies dist/FLAC_Verifier
python build.py --release-assets                    # builds folder, portable ZIP, Inno Setup installer, SHA256SUMS & latest.json
python comprobar_ejecutable.py                      # 12 checks across 8 steps
python comprobar_ejecutable.py --album "D:\Album"   # verify against a specific folder
```

### Desktop shell, persistent library and portable mode (v3.0.0+)

- **Default desktop UI:** `main.py` launches `gui_v3.py` (Edge WebView2 via
  `pywebview` serving static `ui_v3/` assets over a local Bottle WSGI bridge).
  CustomTkinter, `tkinterdnd2`, and Tcl/Tk are excluded from the frozen Windows
  executable; `gui.py` remains available from source via `--legacy-gui`.
- **Data directory & portable mode:** `library_db.py` resolves its data folder
  to `%APPDATA%\FLAC Verifier\` (storing `library.db`, `settings.json`,
  `covers/*.webp`, and `specs/*.specz`), unless a `portable.flag` file exists
  next to the executable (included in `FLAC_Verifier_<ver>-win64-portable.zip`),
  in which case all state is stored locally in `<app_dir>\data\`.
- **Per-user installer & auto-updater:** `installer/flac_verifier.iss` builds a
  per-user Inno Setup 6 installer (`{localappdata}\Programs\FLAC Verifier`,
  `PrivilegesRequired=lowest`) that preserves `%APPDATA%\FLAC Verifier\library.db`
  across upgrades and silent 1-click updates verified by `updater.py` via strict
  HTTPS allowlist + mandatory `SHA-256` digest check against `latest.json`.

### Why there are two executables

A windowed executable (`FLAC_Verifier.exe`, `--noconsole`) has no `stdout` pipe
on Windows. The engine is therefore packaged alongside it as `flac_motor.exe`
(with a console pipe, launched with `CREATE_NO_WINDOW`), and `main.py` also
supports `--motor-cli` as a fallback that reconstructs the standard streams.

### `multiprocessing.freeze_support()`

`main.py` calls `multiprocessing.freeze_support()` as the very first statement
of its `__main__` block (`tests/test_empaquetado.py` enforces this) so child
worker processes in the frozen build do not spawn extra GUI windows.

### Windows Mandatory Integrity Control (MIC) and Antivirus

- `build.py` and `comprobar_ejecutable.py` normalise the mandatory integrity
  label of `dist/FLAC_Verifier` to `Medium` so a workspace that inherited a
  `Low` label can still write PDF reports and spectrograms into normal user
  folders.
- Unsigned PyInstaller bundles can trigger false-positive heuristics in Windows
  Defender or third-party antivirus software. The spec disables UPX
  (`upx=False`) and embeds version resources (`version_info.txt`) and icons to
  minimise false positives; if flagged, add the `FLAC_Verifier` folder to your
  antivirus exclusion list.
