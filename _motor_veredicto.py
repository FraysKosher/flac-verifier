"""Verdict tree, scoring and album-level coherence.

Extracted from the motor facade in Phase 4.
"""
from enum import Enum, IntEnum
import os

LIMIT_CD_HZ = 22050


class EvidenceTier(Enum):
    HARD_PROOF = "hard_proof"
    SOFT_BLOCKING = "soft_blocking"
    SOFT_ADVISORY = "soft_advisory"


class EvidenceFamily(str, Enum):
    FILTERBANK_LATTICE = "filterbank_lattice"
    SPECTRAL_CUTOFF = "spectral_cutoff"
    BIT_DEPTH_CONTAINER = "bit_depth_container"
    TEMPORAL_MODULATION = "temporal_modulation"
    STEREO_COLLAPSE = "stereo_collapse"
    SPECTRAL_ENVELOPE = "spectral_envelope"


HARD_PROOF_FAMILIES = frozenset((
    EvidenceFamily.FILTERBANK_LATTICE.value,
    EvidenceFamily.SPECTRAL_CUTOFF.value,
    EvidenceFamily.BIT_DEPTH_CONTAINER.value,
))


class VerdictCap(IntEnum):
    GENUINE_LOSSLESS = 4
    PROBABLY_LOSSLESS = 3
    SUSPICIOUS = 2
    PROBABLE_UPSCALE = 1


class _ScoreLedger:
    """Deterministic score and verdict ceiling accumulator."""

    def __init__(self):
        self.votes = 0.0
        self.max_votes = 0.0
        self.penalty = 0.0
        self.blocks = False
        self.cap = VerdictCap.GENUINE_LOSSLESS
        self.problems = []
        self.notes = []
        self.families = []

    def add_votes(self, votes_val, max_val=0.0):
        self.votes += float(votes_val)
        self.max_votes += float(max_val)

    def note(self, text):
        self.notes.append(text)

    def add_family(self, family):
        if not family:
            return
        family_name = family.value if isinstance(family, Enum) else str(family)
        if family_name and family_name not in self.families:
            self.families.append(family_name)

    def lower_cap(self, target_cap):
        if target_cap < self.cap:
            self.cap = target_cap

    def penalize(self, text, amount, blocks_max_verdict=True, family=None, tier=None):
        self.problems.append(text)
        self.penalty += float(amount)
        self.add_family(family)
        family_name = family.value if isinstance(family, Enum) else (str(family) if family else None)
        if tier is None:
            if not blocks_max_verdict:
                tier = EvidenceTier.SOFT_ADVISORY
            elif family_name in HARD_PROOF_FAMILIES:
                tier = EvidenceTier.HARD_PROOF
            else:
                tier = EvidenceTier.SOFT_BLOCKING
        if blocks_max_verdict or tier in (EvidenceTier.HARD_PROOF, EvidenceTier.SOFT_BLOCKING):
            self.blocks = True
            self.lower_cap(VerdictCap.SUSPICIOUS)

    def finalize(self, spectrum, include_notes=False):
        hard_proof = any(f in HARD_PROOF_FAMILIES for f in self.families)
        corroborated = bool(len(self.families) >= 2 or hard_proof)
        if not spectrum.get("error"):
            spectrum["familias_evidencia_lossy"] = list(self.families)
            spectrum["corroboracion_multifamilia"] = corroborated

        if self.max_votes == 0:
            if include_notes:
                return 0.0, "indeterminate", self.problems, self.notes
            return 0.0, "indeterminate", self.problems

        score = max(0.0, self.votes / self.max_votes - self.penalty)
        if self.blocks and not corroborated and score < 0.50:
            score = 0.50

        if self.blocks or self.cap <= VerdictCap.SUSPICIOUS:
            verdict = "SUSPICIOUS" if (score >= 0.50 or not corroborated) else "PROBABLE UPSCALE"
        elif score >= 0.80:
            verdict = "GENUINE LOSSLESS"
        elif score >= 0.60:
            verdict = "PROBABLY LOSSLESS"
        elif score >= 0.40:
            verdict = "SUSPICIOUS"
        else:
            verdict = "PROBABLE UPSCALE" if corroborated else "SUSPICIOUS"
        if include_notes:
            return score, verdict, self.problems, self.notes
        return score, verdict, self.problems


def _has_non_opus_codec_signature(spectrum) -> bool:
    """True when an explicit non-Opus filterbank, SBR, or MP3 sfb21 signature is present."""
    lattice_family = spectrum.get("mdct_lattice_family") or ""
    return bool(
        (
            spectrum.get("mdct_lattice_detected")
            and lattice_family
            and "Opus" not in lattice_family
        )
        or spectrum.get("islas_mdct_sfb21")
        or spectrum.get("sbr_qmf_detectado")
    )


def calculate_score(meta, spectrum, bdi, include_notes=False):
    ledger = _ScoreLedger()

    sr      = meta["sample_rate"]
    bits    = meta["bits_per_sample"]
    nyquist = sr // 2
    is_hires = sr >= 88200

    if not spectrum.get("error"):
        ceiling     = spectrum["techo_hz"]
        category     = spectrum.get("clase_corte", "no_cutoff")
        rel_cutoff = spectrum.get("distancia_nyquist")
        rel_ceiling = spectrum.get("techo_rel_nyquist")
        if rel_ceiling is None:
            rel_ceiling = ceiling / nyquist if nyquist else 0.0
        nyq_txt = f"{nyquist/1000:.1f} kHz"

        # 1. Content above the CD band: it can only be demanded of a file that
        #    claims to be hi-res. At 44.1/48 kHz it is physically impossible.
        if is_hires:
            ledger.add_votes(0.0, 3.0)
            if ceiling >= LIMIT_CD_HZ:
                ledger.add_votes(3.0)
            elif spectrum.get("rolloff_analogico_genuino") or spectrum.get("es_transferencia_dsd"):
                ledger.add_votes(3.0)
                ledger.note("Genuine analogue/DSD master: smooth ultrasonic "
                            "roll-off with a continuous floor")
            elif ceiling >= 18000:
                ledger.add_votes(1.5)
                ledger.note(f"Partial high-res content: spectral ceiling at "
                            f"{ceiling/1000:.1f} kHz ({rel_ceiling*100:.0f}% of Nyquist of {nyq_txt})")
            else:
                ledger.penalize(
                    f"Likely upscaled from 44.1/48 kHz: declares {sr/1000:.1f} kHz "
                    f"but its spectral ceiling sits at {ceiling/1000:.1f} kHz (below the CD band)",
                    0.40,
                    family=EvidenceFamily.SPECTRAL_CUTOFF,
                )

        # 2. High band. The reference depends on the sample rate:
        #    - Up to 48 kHz it is measured as a proportion of Nyquist: the ceiling
        #      of a healthy CD sits at 88-100 % of it, and below 70 % the band is
        #      taken to be destroyed.
        #    - Above 48 kHz a proportion of Nyquist means nothing: 70 % of 48 kHz
        #      is 33.6 kHz, where real acoustic music has no energy. A 96/24 master
        #      with the natural roll-off of its converters at 30 kHz has its
        #      ceiling at ~26 kHz and is legitimate. The reference becomes the CD
        #      limit: content above 22.05 kHz is real ultrasonic content and is NOT
        #      penalised.
        # A low ceiling is only a defect when it cannot be explained: an acoustic
        # or low-centroid mix with clean bits and no codec signature ends its band
        # early because there is nothing above it.
        forensic_bdi = bdi.get("forense_bits") or {}
        # Fail-closed on 24-bit: when the bit-depth audit applies, only a fully
        # active LSB lattice earns the acoustic exemption. "partial",
        # "indeterminate" and None all mean "not verified", not "clean".
        bits_ok_acoustic = (
            (not bdi.get("aplica"))
            or (
                bdi.get("lsbs") == "active"
                and not forensic_bdi.get("es_padded_16_en_24")
                and not forensic_bdi.get("es_24bit_escalado_en_peine")
                and not forensic_bdi.get("es_16bit_dither_en_24")
            )
        )
        near_adc_edge = (
            category == "no_cutoff"
            or (category == "near_nyquist_cutoff" and (rel_cutoff or 0.0) >= 0.94)
        )
        without_signatures_lossy = (
            near_adc_edge
            and not spectrum.get("sospecha_joint_stereo")
            and not spectrum.get("sospecha_corte_opus_20k")
            and not spectrum.get("clipping_reconstruccion_lossy")
            and not spectrum.get("islas_mdct_sfb21")
            and not spectrum.get("sospecha_excitador_armonico")
            and not spectrum.get("sbr_qmf_detectado")
            and not spectrum.get("modulacion_psicoacustica_20k")
            and not spectrum.get("mdct_lattice_detected")
            and (spectrum.get("iqr_corte_hz") or 0.0) <= 550.0
            and (spectrum.get("ratio_huecos_mdct") or 0.0) <= 0.10
        )
        acoustic_exemption = bool(
            spectrum.get("decaimiento_acustico_suave")
            and bits_ok_acoustic
            and without_signatures_lossy
        )
        band_limited_exemption = bool(
            not acoustic_exemption
            and spectrum.get("rolloff_banda_limitada_genuina")
            and bits_ok_acoustic
            and without_signatures_lossy
        )
        ledger.add_votes(0.0, 3.0)
        if sr > 48000:
            if ceiling >= LIMIT_CD_HZ:
                ledger.add_votes(3.0)
            elif spectrum.get("rolloff_analogico_genuino") or spectrum.get("es_transferencia_dsd"):
                ledger.add_votes(3.0)
                ledger.note("Genuine analogue/DSD master: the declared high-res "
                            "band is backed by smooth ultrasonic morphology")
            else:
                ledger.penalize(
                    f"No ultrasonic content for {sr/1000:.1f} kHz high-res: "
                    f"spectral ceiling is {ceiling/1000:.1f} kHz (below the "
                    f"{LIMIT_CD_HZ/1000:.2f} kHz CD limit, so the declared "
                    f"{sr/1000:.1f} kHz band is not backed by content)",
                    0.40,
                    family=EvidenceFamily.SPECTRAL_CUTOFF,
                )
        elif rel_ceiling >= 0.88:
            ledger.add_votes(3.0)
        elif rel_ceiling >= 0.70:
            ledger.add_votes(1.5)
            if acoustic_exemption:
                ledger.note(f"Acoustic or low-brightness mix: spectral ceiling "
                            f"at {ceiling/1000:.1f} kHz ({rel_ceiling*100:.0f}% of Nyquist "
                            f"of {nyq_txt}), consistent with solo "
                            f"acoustic/low-centroid instrumentation")
            else:
                ledger.note(f"Reduced high-frequency band: spectral ceiling at "
                            f"{ceiling/1000:.1f} kHz ({rel_ceiling*100:.0f}% of Nyquist of {nyq_txt})")
        elif acoustic_exemption:
            ledger.add_votes(1.5)
            ledger.note(f"Acoustic or low-brightness mix: low spectral ceiling "
                        f"at {ceiling/1000:.1f} kHz ({rel_ceiling*100:.0f}% of Nyquist "
                        f"of {nyq_txt}), consistent with solo "
                        f"acoustic/low-centroid instrumentation")
        elif band_limited_exemption:
            ledger.add_votes(1.5)
            ledger.note(f"Genuine analogue/archival transfer: smooth high-frequency "
                        f"roll-off with no codec signature (spectral ceiling "
                        f"{ceiling/1000:.1f} kHz, {rel_ceiling*100:.0f}% of Nyquist of {nyq_txt})")
        else:
            ledger.penalize(
                f"Severely truncated high frequencies (the high band is destroyed): "
                f"spectral ceiling drops to {ceiling/1000:.1f} kHz "
                f"({rel_ceiling*100:.0f}% of Nyquist of {nyq_txt})",
                0.40,
                family=EvidenceFamily.SPECTRAL_CUTOFF,
            )

        # 3b. Morphology that no legitimate master produces.
        if spectrum.get("muro_brickwall_nyquist"):
            ledger.penalize(
                f"Upscaled from a 44.1/48 kHz source into a high-res container: "
                f"abrupt brickwall drop at the CD/DAT Nyquist "
                f"({spectrum.get('gradiente_nyquist_db_khz', 0.0):.1f} dB/kHz, "
                f"{spectrum.get('salto_ultrasonico_db', 0.0):.1f} dB step into silence)",
                0.40,
                family=EvidenceFamily.SPECTRAL_CUTOFF,
            )
        if spectrum.get("espejo_aliasing_detectado"):
            ledger.penalize(
                f"Unfiltered-interpolator upscale (spectral aliasing): "
                f"ultrasonic band mirrors content below 22.05 kHz "
                f"(r = {spectrum.get('correlacion_espejo_22k', 0.0):.2f})",
                0.40,
                family=EvidenceFamily.SPECTRAL_CUTOFF,
            )
        if spectrum.get("es_transferencia_dsd"):
            ledger.note("Genuine DSD/SACD transfer: characteristic ultrasonic "
                        "noise-shaping profile detected in 24-40 kHz")

        # 3. Cut-off: it is only flagged if it is clearly below the Nyquist.
        # A prior cut plus a flat dither floor is never an analogue roll-off:
        # `rolloff_analogico_genuino` is false in that case, so a Hi-Res
        # far_cutoff cannot bypass this penalty.
        ledger.add_votes(0.0, 3.0)
        if category == "far_cutoff":
            if spectrum.get("es_transferencia_dsd") or spectrum.get("rolloff_analogico_genuino"):
                ledger.note(f"Genuine analogue/DSD master roll-off: musical "
                            f"band ends naturally at {spectrum['frecuencia_corte']/1000:.1f} kHz "
                            f"({rel_cutoff*100:.0f}% of Nyquist)")
            elif is_hires and spectrum.get("muro_brickwall_nyquist"):
                ledger.penalize(
                    f"Upscaled lower-rate source (artificial cut-off at "
                    f"{spectrum['frecuencia_corte']/1000:.1f} kHz, "
                    f"{rel_cutoff*100:.0f}% of Nyquist of {nyq_txt}): "
                    f"high frequencies stop at the source Nyquist",
                    0.40,
                    family=EvidenceFamily.SPECTRAL_CUTOFF,
                )
            else:
                ledger.penalize(
                    f"Lossy codec signature (artificial cut-off at "
                    f"{spectrum['frecuencia_corte']/1000:.1f} kHz, "
                    f"{rel_cutoff*100:.0f}% of Nyquist of {nyq_txt}): "
                    f"high frequencies are sharply cut off",
                    0.40,
                    family=EvidenceFamily.SPECTRAL_CUTOFF,
                )
        elif category == "near_nyquist_cutoff":
            # It is neither flagged nor rewarded unless a genuinely band-limited
            # ADC transition sits right on the Nyquist edge (>= 95%) with zero
            # codec evidence; otherwise it stays inconclusive.
            if (0.75 <= rel_ceiling < 0.88
                    and (rel_cutoff or 0.0) >= 0.95
                    and without_signatures_lossy):
                ledger.add_votes(1.5)
            ledger.note(f"Ambiguous near-Nyquist transition at "
                        f"{spectrum['frecuencia_corte']/1000:.1f} kHz "
                        f"({rel_cutoff*100:.0f}% of Nyquist): consistent with the "
                        f"anti-alias filter of a legitimate master AND with a "
                        f"high-rate codec — inconclusive")
        else:
            ledger.add_votes(3.0)

        # 4. High-band metrics (not applicable if the file has no high band).
        if (spectrum.get("ratio_aplica")
                and not (band_limited_exemption
                         or (acoustic_exemption and rel_ceiling < 0.70))):
            ledger.add_votes(0.0, 2.0)
            r = spectrum["ratio"]
            if r > 0.05:   ledger.add_votes(2.0)
            elif r > 0.01: ledger.add_votes(1.0)
            else:          ledger.note(f"Very low high-to-mid energy balance "
                                       f"(high/mid ratio {r:.5f})")

            ledger.add_votes(0.0, 1.0)
            if spectrum.get("var_alta_db", 0) > 3:
                ledger.add_votes(1.0)

        sep = spectrum.get("separacion_stereo")
        if sep is not None and not spectrum.get("es_dual_mono"):
            ledger.add_votes(0.0, 1.0)
            if sep > 0.1:
                ledger.add_votes(1.0)

        # 4b. Mid/Side, cut-off jitter, and MDCT subband holes.
        # Only ADD penalties when the signature is present: existing keys, votes
        # and verdicts of files without it stay exactly as they were.
        if spectrum.get("es_dual_mono"):
            ledger.note("Dual-mono programme: left and right channels carry the same "
                        "audio, so stereo separation is not informative")
        if spectrum.get("sospecha_joint_stereo"):
            ledger.penalize(
                f"Lossy joint-stereo signature (MP3/AAC): Side channel (L-R) "
                f"collapses at high frequencies (Mid-Side drop "
                f"{spectrum.get('caida_side_agudos_db', 0.0):.1f} dB, cut-off delta "
                f"{spectrum.get('delta_corte_ms_hz', 0.0):.0f} Hz)",
                0.30,
                family=EvidenceFamily.STEREO_COLLAPSE,
            )
        iqr = spectrum.get("iqr_corte_hz") or 0.0
        # The jitter is already part of the Opus evidence: do not penalise the
        # same symptom twice when the 20.00 kHz cliff is already corroborated.
        opus_corroborado = bool(spectrum.get("sospecha_corte_opus_20k") and
                                not (spectrum.get("es_transferencia_dsd") or
                                     spectrum.get("rolloff_analogico_genuino")) and
                                (spectrum.get("sospecha_joint_stereo") or iqr > 550.0 or
                                 (spectrum.get("ratio_huecos_mdct") or 0.0) > 0.10))
        if iqr > 550.0 and not opus_corroborado:
            extreme_jitter = (
                iqr > 1800.0
                and not spectrum.get("es_estacionario")
                and not spectrum.get("rolloff_analogico_genuino")
                and not spectrum.get("es_transferencia_dsd")
            )
            if extreme_jitter:
                ledger.penalize(
                    f"Severe frame-adaptive lossy codec signature: instantaneous "
                    f"cut-off jitters strongly across frames (IQR {iqr:.0f} Hz)",
                    0.30,
                    blocks_max_verdict=True,
                    family=EvidenceFamily.TEMPORAL_MODULATION,
                )
            else:
                ledger.penalize(
                    f"Frame-adaptive lossy codec signature: instantaneous "
                    f"cut-off jitters across frames (IQR {iqr:.0f} Hz)",
                    0.20,
                    blocks_max_verdict=False,
                    family=EvidenceFamily.TEMPORAL_MODULATION,
                )
        huecos = spectrum.get("ratio_huecos_mdct") or 0.0
        if huecos > 0.10:
            ledger.penalize(
                f"Lossy codec MDCT spectral holes: zeroed-out high-frequency "
                f"subbands in {huecos*100:.0f}% of loud frames",
                0.25,
                blocks_max_verdict=False,
                family=EvidenceFamily.TEMPORAL_MODULATION,
            )

        # Top-band islands (sfb21) and harmonic-exciter / SBR reconstruction. Both are
        # positive proof of a codec: the top scalefactor band switches on and off
        # frame by frame, or the high band is rebuilt from the 8-14 kHz envelope
        # and leaves a notch where the original band was cut.
        if spectrum.get("islas_mdct_sfb21"):
            ledger.penalize(
                f"Lowpass-disabled lossy codec signature (MP3 sfb21): "
                f"intermittent spectral islands in 16-20.5 kHz "
                f"(P90-P10 gap {spectrum.get('brecha_p90_p10_agudos_db', 0.0):.1f} dB, "
                f"{(spectrum.get('ratio_huecos_sfb21') or spectrum.get('ratio_apagado_sfb21') or 0.0)*100:.0f}% "
                f"of frames zeroed)",
                0.25,
                blocks_max_verdict=True,
                family=EvidenceFamily.TEMPORAL_MODULATION,
            )
        if spectrum.get("sospecha_excitador_armonico"):
            ledger.penalize(
                f"Artificial high-frequency reconstruction (harmonic exciter / SBR): "
                f"synthetic upper band tracks lower-band envelope "
                f"(r = {spectrum.get('corr_env_excitador', 0.0):.2f}, "
                f"{spectrum.get('muesca_excitador_db', 0.0):.1f} dB notch)",
                0.30,
                blocks_max_verdict=True,
                family=EvidenceFamily.SPECTRAL_ENVELOPE,
            )
        elif spectrum.get("sbr_qmf_detectado"):
            ledger.penalize(
                f"HE-AAC / SBR bandwidth extension transcode: seamless QMF "
                f"subband replication detected (patch correlation "
                f"r = {spectrum.get('sbr_qmf_correlacion', 0.0):.2f}, "
                f"Z = {spectrum.get('sbr_qmf_z', 0.0):.1f} sigma)",
                0.55,
                blocks_max_verdict=True,
                family=EvidenceFamily.FILTERBANK_LATTICE,
            )
        if (spectrum.get("modulacion_psicoacustica_20k")
                and not (spectrum.get("sospecha_corte_opus_20k") and iqr > 550.0)):
            ledger.penalize(
                f"Frame-adaptive lossy codec modulation above 20 kHz: "
                f"upper band pumps with signal frames (median drop "
                f"{spectrum.get('caida_modulacion_20k_db', 0.0):.1f} dB, "
                f"P95-P05 spread {spectrum.get('brecha_modulacion_20k_db', 0.0):.1f} dB)",
                0.30,
                blocks_max_verdict=True,
                family=EvidenceFamily.TEMPORAL_MODULATION,
            )
        if spectrum.get("mdct_lattice_detected"):
            family = spectrum.get("mdct_lattice_family") or "MDCT"
            snr_grid = spectrum.get("mdct_lattice_snr") or 0.0
            ledger.penalize(
                f"Full-bandwidth lossy transcode signature: phase-locked lapped "
                f"transform quantization lattice detected "
                f"({family}, Z = {snr_grid:.1f} sigma)",
                0.55,
                blocks_max_verdict=True,
                family=EvidenceFamily.FILTERBANK_LATTICE,
            )

        # Steep 20.00 kHz shelf (libopus fullband or high-rate codec low-pass).
        # In isolation it is indistinguishable from a mastering anti-alias
        # low-pass, so it is scored only together with another codec signature;
        # on its own it stays a fact.
        if (spectrum.get("sospecha_corte_opus_20k")
                and not (spectrum.get("es_transferencia_dsd")
                         or spectrum.get("rolloff_analogico_genuino"))):
            other_corroboration = bool(
                spectrum.get("sospecha_joint_stereo")
                or (spectrum.get("iqr_corte_hz") or 0.0) > 550.0
                or (spectrum.get("ratio_huecos_mdct") or 0.0) > 0.10
                or spectrum.get("modulacion_psicoacustica_20k")
                or spectrum.get("mdct_lattice_detected")
            )
            sub_lattice = bool(
                not other_corroboration
                and (spectrum.get("mdct_lattice_snr") or 0.0) >= 3.8
            )
            corroborado = other_corroboration or sub_lattice
            if corroborado:
                drop_20k = spectrum.get("caida_opus_20k_db", 0.0)
                if _has_non_opus_codec_signature(spectrum):
                    msg_20k = (
                        f"Steep 20.00 kHz codec low-pass shelf "
                        f"({drop_20k:.0f} dB) corroborated by filterbank evidence"
                    )
                else:
                    msg_20k = (
                        f"Opus (libopus) fullband transcode signature: steep 20.00 kHz "
                        f"shelf ({drop_20k:.0f} dB) corroborated by codec evidence"
                    )
                ledger.penalize(
                    msg_20k,
                    0.45 if sub_lattice else 0.20,
                    blocks_max_verdict=sub_lattice,
                    family=EvidenceFamily.SPECTRAL_CUTOFF,
                )
            elif category != "far_cutoff":
                ledger.note("Inconclusive 20.00 kHz shelf: consistent with both a "
                            "libopus fullband encode and a mastering low-pass filter "
                            "(no other codec signature found)")

        # Nature of the clipping. Only Gibbs overshoot is evidence of
        # lossy reconstruction; a mastering limiter is reported, never penalised.
        if spectrum.get("clipping_reconstruccion_lossy"):
            ledger.penalize(
                f"Lossy decoding overshoot (Gibbs ringing): "
                f"{spectrum.get('clipping_fraccion_cortos', 0.0)*100:.0f}% of clipping "
                f"events are isolated 1-2 sample spikes under a <20.5 kHz band limit",
                0.25,
                blocks_max_verdict=False,
                family=EvidenceFamily.TEMPORAL_MODULATION,
            )
        if spectrum.get("clipping_mastering_limitador"):
            ledger.note("Mastering brickwall limiter peaks: 4+ consecutive clipped "
                        "samples with full-bandwidth content up to Nyquist "
                        "(loudness mastering observation, not an authenticity defect)")

        # 5. Format/content coherence: claiming 24 bits at a CD sample rate
        #    demands some content above the CD band. It is the only signal that
        #    separates a 320 kbps MP3 repackaged as 24 bits from a real 16-bit
        #    master. A ceiling between 88 % and 95 % of Nyquist can still be a
        #    genuine 24-bit master whose converters have a gentle anti-aliasing
        #    filter, so that band is only penalised when the bit depth itself or
        #    the morphology smells like a transcode.
        if bits > 16 and sr <= 48000:
            cutoff_txt = (f"cut at {spectrum['frecuencia_corte']/1000:.1f} kHz"
                         if spectrum.get("frecuencia_corte") else
                         f"limited to {rel_ceiling*100:.0f}% of Nyquist "
                         f"({ceiling/1000:.1f} kHz)")
            forensic = bdi.get("forense_bits") or {}
            native_24b_tail_ok = bool(
                category != "far_cutoff"
                and (acoustic_exemption or band_limited_exemption)
                and spectrum.get("native_24bit_hf_tail")
            )
            if native_24b_tail_ok:
                ledger.note(
                    f"Genuine {bits}-bit high-headroom master: continuous sub-ceiling "
                    f"converter roll-off up to Nyquist backs the declared "
                    f"{bits}-bit/{sr/1000:.1f} kHz format"
                )
            elif category == "far_cutoff" or rel_ceiling < 0.88:
                ledger.penalize(
                    f"Repackaged {bits}-bit container from a band-limited/lossy source: "
                    f"declares {bits} bits at {sr/1000:.1f} kHz but high frequencies are "
                    f"{cutoff_txt} (declared resolution is not backed by the content)",
                    0.25,
                    blocks_max_verdict=True,
                    family=EvidenceFamily.BIT_DEPTH_CONTAINER,
                )
            elif 0.88 <= rel_ceiling < 0.95:
                bits_clean = (
                    bdi.get("lsbs") == "active"
                    and not forensic.get("es_padded_16_en_24")
                    and not forensic.get("es_24bit_escalado_en_peine")
                    and not forensic.get("es_16bit_dither_en_24")
                )
                without_codec_nor_brickwall = (
                    near_adc_edge
                    and not spectrum.get("sospecha_joint_stereo")
                    and not spectrum.get("sospecha_corte_opus_20k")
                    and not spectrum.get("sbr_qmf_detectado")
                    and not spectrum.get("modulacion_psicoacustica_20k")
                    and not spectrum.get("mdct_lattice_detected")
                    and (spectrum.get("iqr_corte_hz") or 0.0) <= 550.0
                    and (spectrum.get("ratio_huecos_mdct") or 0.0) <= 0.10
                    and (
                        (spectrum.get("gradiente_borde_db_khz") or 0.0) < 35.0
                        or (
                            rel_ceiling >= 0.94
                            and (rel_cutoff or 0.0) >= 0.96
                            and (spectrum.get("iqr_corte_hz") or 0.0) <= 150.0
                        )
                    )
                )
                if not (bits_clean and without_codec_nor_brickwall):
                    ledger.penalize(
                        f"Repackaged {bits}-bit container from a band-limited/lossy source: "
                        f"declares {bits} bits at {sr/1000:.1f} kHz but high frequencies are "
                        f"{cutoff_txt} (declared resolution is not backed by the content)",
                        0.25,
                        blocks_max_verdict=True,
                        family=EvidenceFamily.BIT_DEPTH_CONTAINER,
                    )
                else:
                    ledger.note(f"Genuine {bits}-bit master with studio anti-aliasing filter: "
                                f"smooth transition band ({rel_ceiling*100:.0f}% of Nyquist, "
                                f"active LSB lattice and no lossy signature)")

    # 6. LSB utilisation: it ONLY penalises (Block 4).
    if bdi.get("aplica"):
        lsbs = bdi.get("lsbs")
        forensic = bdi.get("forense_bits") or {}
        if lsbs == "empty":
            if forensic.get("es_padded_16_en_24"):
                ledger.penalize(
                    f"16-bit audio zero-padded inside a {bits}-bit container "
                    f"(LSBs are empty: {bdi.get('fraccion_16bit_grid','?')}% of samples "
                    f"on the 16-bit grid, {forensic.get('trailing_zeros_lsb', 8)} "
                    f"trailing zero bits; effective resolution "
                    f"{bdi.get('resolucion_efectiva_bits','?')} bits)",
                    0.40,
                    family=EvidenceFamily.BIT_DEPTH_CONTAINER,
                )
            elif forensic.get("es_16bit_dither_en_24"):
                occ = (forensic.get("lsb8_ocupacion_interior") or 0.0) * 100.0
                ledger.penalize(
                    f"16-bit source padded with synthetic boundary micro-dither "
                    f"inside a {bits}-bit container ({bdi.get('fraccion_16bit_grid','?')}% "
                    f"of samples hug the 16-bit grid, interior LSB8 occupancy {occ:.2f}%; "
                    f"effective resolution ~16 bits)",
                    0.80,
                    family=EvidenceFamily.BIT_DEPTH_CONTAINER,
                )
            else:
                ledger.penalize(
                    f"Bit-depth upscale from a 16-bit source (LSBs are empty: "
                    f"{bdi.get('fraccion_16bit_grid','?')}% of samples on the 16-bit grid; "
                    f"effective resolution {bdi.get('resolucion_efectiva_bits','?')} bits "
                    f"in a {bits}-bit container)",
                    0.40,
                    family=EvidenceFamily.BIT_DEPTH_CONTAINER,
                )
        elif lsbs == "partial":
            ledger.penalize(
                f"Bit-depth upscale with later processing: LSBs partially inactive "
                f"({bdi.get('fraccion_16bit_grid','?')}% on the 16-bit grid; "
                f"effective resolution ~{bdi.get('resolucion_efectiva_bits','?')} bits "
                f"in a {bits}-bit container)",
                0.15,
                blocks_max_verdict=False,
                family=EvidenceFamily.BIT_DEPTH_CONTAINER,
            )
        elif lsbs == "indeterminate" and bdi.get("error"):
            ledger.note(f"Bit-depth LSB check skipped: {bdi['error']}")

        # Native-integer audit (zero padding and quantisation comb).
        if forensic.get("es_padded_16_en_24") and lsbs != "empty":
            ledger.penalize(
                f"16-bit audio zero-padded inside a {bits}-bit container "
                f"(the lowest {forensic.get('trailing_zeros_lsb', 8)} bits are always "
                f"zero across the whole file)",
                0.40,
                family=EvidenceFamily.BIT_DEPTH_CONTAINER,
            )
        if forensic.get("es_24bit_escalado_en_peine"):
            step_size = forensic.get("paso_reticula_mediano")
            bins = forensic.get("lsb8_cajones_ocupados")
            entropy = forensic.get("lsb8_entropia_bits")
            # A loud compressed fake caught by the global lattice still occupies
            # every bin with ~8 bits of entropy: quoting them would contradict the
            # comb itself, so the lattice step is the evidence in that case.
            if isinstance(bins, int) and 0 < bins < 256:
                comb_detail = (f"lattice step {step_size}, entropy {entropy} bits, "
                                 f"{bins}/256 bins occupied")
            else:
                comb_detail = f"global lattice step {step_size} integers"
            ledger.penalize(
                f"16-bit source gain-scaled into a {bits}-bit container "
                f"(quantisation comb in the lowest 8 bits: {comb_detail})",
                0.40,
                family=EvidenceFamily.BIT_DEPTH_CONTAINER,
            )
        if forensic.get("es_16bit_dither_en_24") and lsbs != "empty":
            occ = (forensic.get("lsb8_ocupacion_interior") or 0.0) * 100.0
            ledger.penalize(
                f"16-bit source padded with synthetic boundary micro-dither "
                f"inside a {bits}-bit container (interior LSB8 occupancy {occ:.2f}%)",
                0.40,
                family=EvidenceFamily.BIT_DEPTH_CONTAINER,
            )

    # MD5: it is NO longer scored. Its check is reported as a fact of its own in
    # resultado["md5"] (verify_md5), and an MD5 that does not match is a hard error.
    return ledger.finalize(spectrum, include_notes=include_notes)

ALBUM_COHERENCE_NOTE = ("Acoustic/low-HF track validated by album-level "
                        "master consistency")

def _deserves_album_coherence(result):
    """True when the only evidence against the track is a low spectral ceiling."""
    meta = result.get("meta") or {}
    sr = int(meta.get("sample_rate") or 0)
    bits = int(meta.get("bits_per_sample") or 0)
    spectrum = result.get("esp") or {}
    bdi = result.get("bdi") or {}
    if spectrum.get("error"):
        return False
    if (result.get("md5") or {}).get("estado") not in ("match", "absent"):
        return False
    if spectrum.get("corte_artificial") or spectrum.get("clase_corte") == "far_cutoff":
        return False                       # a below-Nyquist codec step is never rescued
    max_coh_rel = 0.95 if (bits > 16 and sr <= 48000) else 0.88
    if (spectrum.get("techo_rel_nyquist") or 1.0) >= max_coh_rel:
        return False
    for key in ("muro_brickwall_nyquist", "espejo_aliasing_detectado",
                  "sospecha_joint_stereo", "sospecha_corte_opus_20k",
                  "clipping_reconstruccion_lossy", "islas_mdct_sfb21",
                  "sospecha_excitador_armonico", "sbr_qmf_detectado",
                  "modulacion_psicoacustica_20k", "mdct_lattice_detected"):
        if spectrum.get(key):
            return False
    dark_sparse_iqr_ok = (
        (result.get("md5") or {}).get("estado") == "match"
        and sr <= 48000
        and spectrum.get("clase_corte") == "no_cutoff"
        and 0.0 < float(spectrum.get("centroide_hz") or 0.0) < 460.0
        and (
            (
                (spectrum.get("iqr_corte_hz") or 0.0) <= 950.0
                and (spectrum.get("ratio_huecos_mdct") or 0.0) <= 0.01
                and float(spectrum.get("mdct_lattice_snr") or 0.0) < 3.5
            )
            or (
                float(spectrum.get("techo_hz") or 0.0) < 12500.0
                and float(spectrum.get("gradiente_borde_db_khz") or 0.0) < 25.0
                and (spectrum.get("ratio_huecos_mdct") or 0.0) <= 0.05
            )
        )
    )
    if (spectrum.get("iqr_corte_hz") or 0.0) > 550.0 and not dark_sparse_iqr_ok:
        return False
    if (spectrum.get("ratio_huecos_mdct") or 0.0) > 0.10:
        return False
    if sr >= 88200 and (bits > 16 or bdi.get("aplica")) and bdi.get("lsbs") != "active":
        return False
    forensic = bdi.get("forense_bits") or {}
    if (forensic.get("es_padded_16_en_24")
            or forensic.get("es_24bit_escalado_en_peine")
            or forensic.get("es_16bit_dither_en_24")
            or bdi.get("lsbs") == "empty"):
        return False
    permitidos = [
        "spectral ceiling", "partial hi-res content", "partial high-res content",
        "reduced high band", "reduced high-frequency band",
        "high band is destroyed", "severely truncated high frequencies",
        "smooth analogue/archival roll-off", "smooth high-frequency roll-off",
        "dual-mono", "high/mid ratio", "mastering brickwall limiter",
        "studio anti-aliasing", "anti-alias filter of a legitimate master",
        "analogue/dsd master", "dsd/sacd noise-shaping", "dsd/sacd transfer",
    ]
    if sr >= 88200 and (not bdi.get("aplica") or bdi.get("lsbs") == "active"):
        permitidos.extend(["below the cd band", "not backed by content"])
    allow_24b_dark_vhs = (
        sr <= 48000
        and bits > 16
        and bdi.get("lsbs") == "active"
        and 0.0 < float(spectrum.get("centroide_hz") or 0.0) < 1800.0
    )
    for problem in result.get("problemas") or []:
        low = problem.lower()
        if any(text in low for text in permitidos):
            continue
        if allow_24b_dark_vhs and "not backed by the content" in low:
            continue
        if dark_sparse_iqr_ok and "cut-off jitters" in low:
            continue
        return False
    return True


def _deserves_album_lossy_coherence(result):
    """True when a dark/low-ceiling track in a 44.1/48 kHz container passed on
    its own only because its musical ceiling sits below the codec's upper-band
    detector, yet already shows a weak lapped-transform lattice peak."""
    if not isinstance(result, dict) or result.get("error"):
        return False
    if result.get("veredicto") not in ("GENUINE LOSSLESS", "PROBABLY LOSSLESS"):
        return False
    meta = result.get("meta") or {}
    sr = int(meta.get("sample_rate") or 0)
    if not 41000 <= sr <= 48000:
        return False
    spectrum = result.get("esp") or {}
    if spectrum.get("error") or spectrum.get("mdct_lattice_detected"):
        return False
    if (bool(spectrum.get("sospecha_corte_opus_20k"))
            and float(spectrum.get("brecha_modulacion_20k_db") or 0.0) >= 25.0):
        return True
    rel = spectrum.get("techo_rel_nyquist")
    if not isinstance(rel, (int, float)) or float(rel) >= 0.72:
        return False
    snr = float(spectrum.get("mdct_lattice_snr") or 0.0)
    return snr >= 3.5


def _is_lossy_codec_quorum_track(result):
    """True when a 44.1/48 kHz track carries either a confirmed lapped-codec
    lattice or a sharp ~20 kHz (78-87% Nyquist) codec brickwall in 48 kHz."""
    spectrum = result.get("esp") or {}
    if spectrum.get("mdct_lattice_detected"):
        return True
    if (spectrum.get("sospecha_corte_opus_20k")
            and result.get("veredicto") in (
                "SUSPICIOUS", "PROBABLE UPSCALE", "CONFIRMED LOSSY"
            )):
        return True
    if result.get("veredicto") not in ("CONFIRMED LOSSY", "PROBABLE UPSCALE"):
        return False
    sr = int((result.get("meta") or {}).get("sample_rate") or 0)
    rel = float(spectrum.get("techo_rel_nyquist") or 0.0)
    return (
        sr == 48000
        and bool(spectrum.get("corte_artificial"))
        and 0.78 <= rel <= 0.87
    )


def _is_clean_quorum_track(result):
    verdict = result.get("veredicto")
    if verdict == "GENUINE LOSSLESS":
        return True
    if verdict != "PROBABLY LOSSLESS":
        return False
    if _deserves_album_coherence(result):
        return False
    spectrum = result.get("esp") or {}
    sr = int((result.get("meta") or {}).get("sample_rate") or 0)
    full_band = (
        (spectrum.get("techo_rel_nyquist") or 0.0) >= 0.88
        if sr <= 48000
        else (
            (spectrum.get("techo_hz") or 0) >= LIMIT_CD_HZ
            or bool(spectrum.get("rolloff_analogico_genuino"))
            or bool(spectrum.get("es_transferencia_dsd"))
            or (spectrum.get("techo_rel_nyquist") or 0.0) >= 0.88
        )
    )
    score = float(result.get("score") or 0.0)
    fams = spectrum.get("familias_evidencia_lossy") or []
    return full_band and score >= 0.60 and len(fams) == 0


def _is_clean_album_support_track(result, reference):
    """True when a track provides clean lossless album quorum support."""
    meta = result.get("meta") or {}
    if (meta.get("sample_rate"), meta.get("bits_per_sample")) != reference:
        return False
    if _is_clean_quorum_track(result):
        return True
    if result.get("veredicto") != "PROBABLY LOSSLESS":
        return False
    if (result.get("md5") or {}).get("estado") not in ("match", "absent"):
        return False
    spectrum = result.get("esp") or {}
    if spectrum.get("familias_evidencia_lossy") or []:
        return False
    if float(result.get("score") or 0.0) < 0.64:
        return False
    sr = int(meta.get("sample_rate") or 0)
    rel = float(spectrum.get("techo_rel_nyquist") or 0.0)
    if sr <= 48000:
        return bool(
            rel >= 0.80
            or (
                spectrum.get("decaimiento_acustico_suave")
                and (rel >= 0.55 or spectrum.get("native_24bit_hf_tail"))
            )
        )
    return bool(
        (spectrum.get("techo_hz") or 0) >= LIMIT_CD_HZ
        or spectrum.get("rolloff_analogico_genuino")
        or spectrum.get("es_transferencia_dsd")
        or rel >= 0.80
    )


def _is_clean_batch_track(result):
    """True when a track shows zero codec, brickwall, or fake bit-depth flags."""
    spectrum = result.get("esp") or {}
    if spectrum.get("error"):
        return False
    if spectrum.get("familias_evidencia_lossy") or []:
        return False
    if spectrum.get("corte_artificial") or spectrum.get("clase_corte") == "far_cutoff":
        return False
    for key in (
        "muro_brickwall_nyquist", "espejo_aliasing_detectado",
        "sospecha_joint_stereo", "sospecha_corte_opus_20k",
        "clipping_reconstruccion_lossy", "islas_mdct_sfb21",
        "sospecha_excitador_armonico", "sbr_qmf_detectado",
        "modulacion_psicoacustica_20k", "mdct_lattice_detected",
    ):
        if spectrum.get(key):
            return False
    bdi = result.get("bdi") or {}
    forensic = bdi.get("forense_bits") or {}
    if (forensic.get("es_padded_16_en_24")
            or forensic.get("es_24bit_escalado_en_peine")
            or forensic.get("es_16bit_dither_en_24")
            or bdi.get("lsbs") == "empty"):
        return False
    return True


def apply_album_coherence(results):
    """Vector 6: rescue acoustic/low-HF tracks of a consistent album.

    A candidate is any track accepted by `_deserves_album_coherence` (only the
    spectral ceiling is against it), whatever its current label: SUSPICIOUS or
    PROBABLE UPSCALE, and also PROBABLY LOSSLESS when it was already excused on
    its own by the Phase 8D acoustic exemption (`decaimiento_acustico_suave`
    with clean bits and no lossy signature). When at least 75% of the tracks in a
    directory are GENUINE LOSSLESS (or clean PROBABLY LOSSLESS with score >= 0.60
    and at least one GENUINE LOSSLESS / clean score >= 0.75 anchor) with the same
    sample rate and bit depth, the album context is stronger evidence than the
    isolated spectrum, so those candidates get the album validation
    (`coherencia_album = True` and ALBUM_COHERENCE_NOTE) and are at least
    PROBABLY LOSSLESS. That mark exists only in this batch: it is never written
    into the per-file cache, so analysing the track alone does not inherit it.
    Returns how many tracks were raised or validated. Modifies the list in place."""
    from collections import Counter

    grupos = {}
    for result in results:
        if not isinstance(result, dict) or result.get("error"):
            continue
        path = result.get("ruta")
        if not path:
            continue
        grupos.setdefault(os.path.dirname(os.path.abspath(path)), []).append(result)

    elevados = 0
    for tracks in grupos.values():
        validas = [r for r in tracks
                   if (r.get("meta") or {}).get("sample_rate")
                   and r.get("veredicto")]
        if len(validas) < 2:
            continue
        quorum_tracks = [r for r in validas if _is_clean_quorum_track(r)]
        if quorum_tracks:
            reference = Counter(
                (r["meta"]["sample_rate"], r["meta"]["bits_per_sample"])
                for r in quorum_tracks).most_common(1)[0][0]
            quorum_ref = [r for r in quorum_tracks
                          if (r["meta"]["sample_rate"],
                              r["meta"]["bits_per_sample"]) == reference]
            support_ref = [
                r for r in validas
                if _is_clean_album_support_track(r, reference)
            ]
            has_strong_anchor = any(
                r.get("veredicto") == "GENUINE LOSSLESS"
                or float(r.get("score") or 0.0) >= 0.75
                or (
                    (r.get("md5") or {}).get("estado") == "absent"
                    and float(r.get("score") or 0.0) >= 0.70
                    and float((r.get("esp") or {}).get("techo_rel_nyquist") or 0.0) >= 0.90
                    and not ((r.get("esp") or {}).get("familias_evidencia_lossy") or [])
                )
                for r in quorum_ref
            )
            support_ratio = len(support_ref) / float(len(validas))
            mixed_vhs_ok = False
            if not (has_strong_anchor and support_ratio >= 0.75):
                non_verde = [
                    r for r in validas
                    if r.get("veredicto") not in ("GENUINE LOSSLESS", "PROBABLY LOSSLESS")
                ]
                verde = [
                    r for r in validas
                    if r.get("veredicto") in ("GENUINE LOSSLESS", "PROBABLY LOSSLESS")
                ]
                mixed_vhs_ok = bool(
                    has_strong_anchor
                    and len(validas) >= 6
                    and len(support_ref) >= 3
                    and support_ratio >= 0.40
                    and non_verde
                    and all(_is_clean_batch_track(r) for r in verde)
                    and all(
                        (r["meta"]["sample_rate"], r["meta"]["bits_per_sample"]) == reference
                        and _deserves_album_coherence(r)
                        for r in non_verde
                    )
                )
            if (has_strong_anchor and support_ratio >= 0.75) or mixed_vhs_ok:
                candidatos = []
                for r in validas:
                    if r.get("veredicto") not in ("SUSPICIOUS", "PROBABLE UPSCALE",
                                                  "PROBABLY LOSSLESS"):
                        continue
                    meta = r.get("meta") or {}
                    if (meta.get("sample_rate"), meta.get("bits_per_sample")) != reference:
                        continue
                    if _deserves_album_coherence(r):
                        candidatos.append(r)
                rescue_candidates = [
                    r for r in candidatos
                    if r.get("veredicto") in ("SUSPICIOUS", "PROBABLE UPSCALE")
                ]
                max_candidates = max(2, len(validas) // 4)
                within_cap = (
                    True if mixed_vhs_ok
                    else len(rescue_candidates) <= max_candidates
                )
                if candidatos and within_cap:
                    for r in candidatos:
                        r["veredicto"] = "PROBABLY LOSSLESS"
                        r["score"] = max(float(r.get("score") or 0.0), 0.60)
                        old_problems = list(r.get("problemas") or [])
                        notes = list(r.get("notes") or [])
                        for item in old_problems:
                            if item not in notes:
                                notes.append(item)
                        if ALBUM_COHERENCE_NOTE not in notes:
                            notes.append(ALBUM_COHERENCE_NOTE)
                        r["problemas"] = []
                        r["notes"] = notes
                        evid = list(r.get("evidencias_forenses") or [])
                        if ALBUM_COHERENCE_NOTE not in evid:
                            evid.append(ALBUM_COHERENCE_NOTE)
                        r["evidencias_forenses"] = evid
                        r["coherencia_album"] = True
                        r["album_coherence_note"] = ALBUM_COHERENCE_NOTE
                        elevados += 1
                    continue

        if len(validas) < 3:
            continue
        lattice_tracks = [
            r for r in validas
            if _is_lossy_codec_quorum_track(r)
        ]
        if not lattice_tracks:
            continue
        lossy_ref_key = Counter(
            (r["meta"]["sample_rate"], r["meta"]["bits_per_sample"])
            for r in lattice_tracks
        ).most_common(1)[0][0]
        lattice_ref = [
            r for r in lattice_tracks
            if (r["meta"]["sample_rate"], r["meta"]["bits_per_sample"]) == lossy_ref_key
        ]
        if len(lattice_ref) / float(len(validas)) < 0.75:
            continue
        explicit_families = [
            (r.get("esp") or {}).get("mdct_lattice_family")
            for r in lattice_ref
            if (r.get("esp") or {}).get("mdct_lattice_family")
        ]
        dominant_family = (
            Counter(explicit_families).most_common(1)[0][0]
            if explicit_families
            else "20 kHz lossy/Opus"
        )
        lossy_candidates = []
        for r in validas:
            meta = r.get("meta") or {}
            if (meta.get("sample_rate"), meta.get("bits_per_sample")) != lossy_ref_key:
                continue
            if _deserves_album_lossy_coherence(r):
                lossy_candidates.append(r)
        max_lossy = max(2, len(validas) // 4)
        if not 1 <= len(lossy_candidates) <= max_lossy:
            continue
        for r in lossy_candidates:
            spec = r.get("esp") or {}
            if spec.get("sospecha_corte_opus_20k"):
                spread_db = float(spec.get("brecha_modulacion_20k_db") or 0.0)
                note = (
                    f"Likely {dominant_family} transcode by album consistency: "
                    f"20.00 kHz shelf track in a confirmed lossy batch "
                    f"(upper-band spread {spread_db:.1f} dB)"
                )
            else:
                snr_val = float(spec.get("mdct_lattice_snr") or 0.0)
                note = (
                    f"Likely {dominant_family} transcode by album consistency: "
                    f"low-ceiling track in a confirmed lossy batch "
                    f"(sub-threshold lattice Z = {snr_val:.1f} sigma)"
                )
            r["veredicto"] = "PROBABLE UPSCALE"
            r["score"] = min(float(r.get("score") or 0.0), 0.35)
            r["problemas"] = list(r.get("problemas") or []) + [note]
            r["evidencias_forenses"] = list(r.get("evidencias_forenses") or []) + [note]
            r["coherencia_album"] = True
            r["album_coherence_note"] = note
            elevados += 1
    return elevados

def forensic_evidence(result):
    """Short English technical evidence lines shown next to the verdict.

    It is additive to `problemas`: the GUI/PDF can explain *why* the verdict was
    reached without re-deriving the detectors from the raw metrics."""
    spectrum = result.get("esp") or {}
    bdi = result.get("bdi") or {}
    evidence = []
    if spectrum.get("error"):
        return evidence

    ceiling = spectrum.get("techo_hz")
    rel = spectrum.get("techo_rel_nyquist")
    if isinstance(ceiling, (int, float)) and ceiling > 0:
        evidence.append(f"Spectral ceiling {ceiling/1000:.1f} kHz"
                          + (f" ({float(rel)*100:.0f}% of Nyquist)"
                             if isinstance(rel, (int, float)) else ""))
    if spectrum.get("muro_brickwall_nyquist"):
        evidence.append(f"Hi-Res brickwall at the CD/DAT Nyquist "
                          f"({spectrum.get('gradiente_nyquist_db_khz', 0.0):.1f} dB/kHz)")
    if spectrum.get("espejo_aliasing_detectado"):
        evidence.append(f"Spectral mirror around 22.05 kHz "
                          f"(r = {spectrum.get('correlacion_espejo_22k', 0.0):.2f})")
    if spectrum.get("es_transferencia_dsd"):
        evidence.append("DSD/SACD noise-shaping profile in 24-40 kHz")
    elif spectrum.get("rolloff_analogico_genuino") or spectrum.get("rolloff_banda_limitada_genuina"):
        evidence.append("Analogue/acoustic roll-off with a continuous ultrasonic floor")
    if spectrum.get("es_estacionario"):
        evidence.append("Stationary harmonic/tonal programme (sustained drone/synth guard)")
    if spectrum.get("sospecha_joint_stereo"):
        evidence.append(f"Side channel (L-R) collapse: "
                          f"{spectrum.get('caida_side_agudos_db', 0.0):.1f} dB, "
                          f"cut-off delta {spectrum.get('delta_corte_ms_hz', 0.0):.0f} Hz")
    if spectrum.get("es_dual_mono"):
        evidence.append("Dual-mono: left and right carry the same programme")
    if (spectrum.get("iqr_corte_hz") or 0.0) > 550.0:
        evidence.append(f"Instantaneous cut-off jitter: "
                          f"IQR {spectrum.get('iqr_corte_hz', 0.0):.0f} Hz")
    if (spectrum.get("ratio_huecos_mdct") or 0.0) > 0.10:
        evidence.append(f"MDCT spectral holes in "
                          f"{(spectrum.get('ratio_huecos_mdct') or 0.0)*100:.0f}% of the frames")
    if spectrum.get("sospecha_corte_opus_20k"):
        if _has_non_opus_codec_signature(spectrum):
            evidence.append(f"20.00 kHz cliff ({spectrum.get('caida_opus_20k_db', 0.0):.0f} dB): "
                              f"steep codec low-pass shelf")
        else:
            evidence.append(f"20.00 kHz cliff ({spectrum.get('caida_opus_20k_db', 0.0):.0f} dB): "
                              f"libopus fullband signature")
    if spectrum.get("clipping_reconstruccion_lossy"):
        evidence.append("Clipping: isolated 1-2 sample Gibbs overshoot")
    if spectrum.get("clipping_mastering_limitador"):
        evidence.append("Clipping: 4+ sample mastering limiter plateaus")
    if spectrum.get("islas_mdct_sfb21"):
        evidence.append(f"Intermittent sfb21 MDCT islands: "
                          f"{spectrum.get('brecha_p90_p10_agudos_db', 0.0):.1f} dB P90-P10 gap")
    if spectrum.get("sospecha_excitador_armonico"):
        evidence.append(f"Harmonic exciter / SBR signature: "
                          f"r = {spectrum.get('corr_env_excitador', 0.0):.2f}, "
                          f"{spectrum.get('muesca_excitador_db', 0.0):.1f} dB notch")
    if spectrum.get("sbr_qmf_detectado"):
        evidence.append(f"Seamless QMF subband replication (HE-AAC SBR): "
                          f"r = {spectrum.get('sbr_qmf_correlacion', 0.0):.2f}, "
                          f"Z = {spectrum.get('sbr_qmf_z', 0.0):.1f} sigma")
    if spectrum.get("modulacion_psicoacustica_20k"):
        evidence.append(f"Upper-band psychoacoustic modulation (>20 kHz): "
                          f"{spectrum.get('caida_modulacion_20k_db', 0.0):.1f} dB median drop, "
                          f"{spectrum.get('brecha_modulacion_20k_db', 0.0):.1f} dB P95-P05 spread")
    if spectrum.get("mdct_lattice_detected"):
        family = spectrum.get("mdct_lattice_family") or "MDCT"
        evidence.append(f"Lapped transform quantization lattice ({family}): "
                          f"Z = {spectrum.get('mdct_lattice_snr', 0.0):.1f} sigma")
    forensic = bdi.get("forense_bits") or {}
    bits_container = int((result.get("meta") or {}).get("bits_per_sample") or 24)
    if forensic.get("es_padded_16_en_24"):
        evidence.append(f"16-bit content zero-padded into a {bits_container}-bit container")
    if forensic.get("es_24bit_escalado_en_peine"):
        step_size = forensic.get("paso_reticula_mediano")
        bins = forensic.get("lsb8_cajones_ocupados")
        entropy = forensic.get("lsb8_entropia_bits")
        if isinstance(bins, int) and 0 < bins < 256:
            evidence.append(f"{bits_container}-bit quantisation comb: lattice step {step_size}, "
                              f"entropy {entropy} bits, {bins}/256 bins")
        else:
            evidence.append(f"{bits_container}-bit quantisation comb: global lattice step "
                              f"{step_size} integers")
    if forensic.get("es_16bit_dither_en_24"):
        occ = (forensic.get("lsb8_ocupacion_interior") or 0.0) * 100.0
        evidence.append(f"16-bit grid with boundary micro-dither in {bits_container}-bit "
                          f"(interior LSB8 occupancy {occ:.2f}%)")
    if forensic.get("es_flujo_dop"):
        evidence.append("DoP 1.0 framing markers (0x05/0xFA) detected in top byte")
    fams = spectrum.get("familias_evidencia_lossy") or []
    if len(fams) >= 2:
        evidence.append(f"Multi-family corroboration ({len(fams)} families: "
                          f"{', '.join(fams)})")
    if (result.get("md5") or {}).get("estado") == "match":
        evidence.append("STREAMINFO MD5 verified (match)")
    if result.get("coherencia_album"):
        evidence.append(result.get("album_coherence_note") or ALBUM_COHERENCE_NOTE)
    return evidence
