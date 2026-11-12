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
        self.families = []

    def add_votes(self, votes_val, max_val=0.0):
        self.votes += float(votes_val)
        self.max_votes += float(max_val)

    def note(self, text):
        self.problems.append(text)

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

    def finalize(self, spectrum):
        hard_proof = any(f in HARD_PROOF_FAMILIES for f in self.families)
        corroborated = bool(len(self.families) >= 2 or hard_proof)
        if not spectrum.get("error"):
            spectrum["familias_evidencia_lossy"] = list(self.families)
            spectrum["corroboracion_multifamilia"] = corroborated

        if self.max_votes == 0:
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
        return score, verdict, self.problems


def calculate_score(meta, spectrum, bdi):
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
            if spectrum.get("rolloff_analogico_genuino") or spectrum.get("es_transferencia_dsd"):
                ledger.add_votes(3.0)
                ledger.note("hi-res morphology consistent with an analogue/DSD "
                            "master (smooth roll-off and continuous ultrasonic floor)")
            elif ceiling >= LIMIT_CD_HZ:
                ledger.add_votes(3.0)
            elif ceiling >= 18000:
                ledger.add_votes(1.5)
                ledger.note(f"partial hi-res content: ceiling of {ceiling/1000:.1f} kHz "
                            f"({rel_ceiling*100:.0f}% of Nyquist of {nyq_txt})")
            else:
                ledger.penalize(
                    f"claims {sr/1000:.1f} kHz but the ceiling is {ceiling/1000:.1f} kHz, "
                    f"below the CD band: upscaled from 44.1/48 kHz",
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
        without_signatures_lossy = (
            category == "no_cutoff"
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
                ledger.note("hi-res ceiling explained by an analogue/DSD master: "
                            "the declared band is backed by the morphology")
            else:
                ledger.penalize(
                    f"spectral ceiling {ceiling/1000:.1f} kHz: there is no content above "
                    f"the CD limit ({LIMIT_CD_HZ/1000:.2f} kHz), so "
                    f"the band of {sr/1000:.1f} kHz is not backed by content",
                    0.40,
                    family=EvidenceFamily.SPECTRAL_CUTOFF,
                )
        elif rel_ceiling >= 0.88:
            ledger.add_votes(3.0)
        elif rel_ceiling >= 0.70:
            ledger.add_votes(1.5)
            if acoustic_exemption:
                ledger.note(f"spectral ceiling {ceiling/1000:.1f} kHz "
                            f"({rel_ceiling*100:.0f}% of Nyquist of {nyq_txt}): "
                            f"low spectral ceiling consistent with solo "
                            f"acoustic/low-centroid instrumentation")
            else:
                ledger.note(f"spectral ceiling {ceiling/1000:.1f} kHz "
                            f"({rel_ceiling*100:.0f}% of Nyquist of {nyq_txt}): "
                            f"reduced high band")
        elif acoustic_exemption:
            ledger.add_votes(1.5)
            ledger.note(f"spectral ceiling {ceiling/1000:.1f} kHz "
                        f"({rel_ceiling*100:.0f}% of Nyquist of {nyq_txt}): "
                        f"low spectral ceiling consistent with solo "
                        f"acoustic/low-centroid instrumentation")
        elif band_limited_exemption:
            ledger.add_votes(1.5)
            ledger.note(f"spectral ceiling {ceiling/1000:.1f} kHz "
                        f"({rel_ceiling*100:.0f}% of Nyquist of {nyq_txt}): "
                        f"smooth analogue/archival roll-off with no codec signature")
        else:
            ledger.penalize(
                f"spectral ceiling {ceiling/1000:.1f} kHz ({rel_ceiling*100:.0f}% of Nyquist "
                f"of {nyq_txt}): the high band is destroyed",
                0.40,
                family=EvidenceFamily.SPECTRAL_CUTOFF,
            )

        # 3b. Vector 3: morphology that no legitimate master produces.
        if spectrum.get("muro_brickwall_nyquist"):
            ledger.penalize(
                f"Hi-Res container with a brickwall at the CD/DAT Nyquist "
                f"({spectrum.get('gradiente_nyquist_db_khz', 0.0):.1f} dB/kHz, "
                f"{spectrum.get('salto_ultrasonico_db', 0.0):.1f} dB into the digital void): "
                f"upscaled from a 44.1/48 kHz source",
                0.40,
                family=EvidenceFamily.SPECTRAL_CUTOFF,
            )
        if spectrum.get("espejo_aliasing_detectado"):
            ledger.penalize(
                f"ultrasonic band mirrors the content below 22.05 kHz "
                f"(r = {spectrum.get('correlacion_espejo_22k', 0.0):.2f}): "
                f"unfiltered-interpolator upscale (spectral aliasing)",
                0.40,
                family=EvidenceFamily.SPECTRAL_CUTOFF,
            )
        if spectrum.get("es_transferencia_dsd"):
            ledger.note("DSD/SACD noise-shaping profile detected in 24-40 kHz "
                        "(legitimate hi-res transfer)")

        # 3. Cut-off: it is only flagged if it is clearly below the Nyquist.
        # A prior cut plus a flat dither floor is never an analogue roll-off:
        # `rolloff_analogico_genuino` is false in that case, so a Hi-Res
        # far_cutoff cannot bypass this penalty.
        ledger.add_votes(0.0, 3.0)
        if category == "far_cutoff":
            if spectrum.get("es_transferencia_dsd") or spectrum.get("rolloff_analogico_genuino"):
                ledger.note(f"band edge at {spectrum['frecuencia_corte']/1000:.1f} kHz "
                            f"({rel_cutoff*100:.0f}% of Nyquist): the musical band ends "
                            f"there, consistent with an analogue/DSD master")
            else:
                ledger.penalize(
                    f"artificial cut-off at {spectrum['frecuencia_corte']/1000:.1f} kHz "
                    f"({rel_cutoff*100:.0f}% of Nyquist of {nyq_txt}): lossy codec signature",
                    0.40,
                    family=EvidenceFamily.SPECTRAL_CUTOFF,
                )
        elif category == "near_nyquist_cutoff":
            # It is neither flagged nor rewarded: the anti-alias filter of a
            # legitimate master lives here too. It is reported as inconclusive.
            ledger.note(f"band cut at {spectrum['frecuencia_corte']/1000:.1f} kHz "
                        f"({rel_cutoff*100:.0f}% of Nyquist): consistent with the anti-alias "
                        f"filter of a legitimate master AND with a high-rate codec "
                        f"— inconclusive")
        else:
            ledger.add_votes(3.0)

        # 4. High-band metrics (not applicable if the file has no high band).
        if spectrum.get("ratio_aplica") and not band_limited_exemption:
            ledger.add_votes(0.0, 2.0)
            r = spectrum["ratio"]
            if r > 0.05:   ledger.add_votes(2.0)
            elif r > 0.01: ledger.add_votes(1.0)
            else:          ledger.note(f"high/mid ratio very low ({r:.5f})")

            ledger.add_votes(0.0, 1.0)
            if spectrum.get("var_alta_db", 0) > 3:
                ledger.add_votes(1.0)

        sep = spectrum.get("separacion_stereo")
        if sep is not None and not spectrum.get("es_dual_mono"):
            ledger.add_votes(0.0, 1.0)
            if sep > 0.1:
                ledger.add_votes(1.0)

        # 4b. Vector 1 (Mid/Side) and Vector 2 (cut-off jitter + MDCT holes).
        # Only ADD penalties when the signature is present: existing keys, votes
        # and verdicts of files without it stay exactly as they were.
        if spectrum.get("es_dual_mono"):
            ledger.note("dual-mono (left and right carry the same programme): "
                        "stereo separation is not informative")
        if spectrum.get("sospecha_joint_stereo"):
            ledger.penalize(
                f"the Side channel (L-R) collapses at high frequency "
                f"(Mid-Side drop {spectrum.get('caida_side_agudos_db', 0.0):.1f} dB, "
                f"cut-off delta {spectrum.get('delta_corte_ms_hz', 0.0):.0f} Hz): "
                f"joint-stereo lossy codec signature",
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
            ledger.penalize(
                f"the instantaneous cut-off jitters (IQR {iqr:.0f} Hz): "
                f"frame-adaptive codec signature",
                0.20,
                blocks_max_verdict=False,
                family=EvidenceFamily.TEMPORAL_MODULATION,
            )
        huecos = spectrum.get("ratio_huecos_mdct") or 0.0
        if huecos > 0.10:
            ledger.penalize(
                f"MDCT spectral holes in {huecos*100:.0f}% of the high-energy "
                f"frames: lossy codec signature",
                0.25,
                blocks_max_verdict=False,
                family=EvidenceFamily.TEMPORAL_MODULATION,
            )

        # Vector 7: sfb21 islands and harmonic-exciter / SBR signature. Both are
        # positive proof of a codec: the top scalefactor band switches on and off
        # frame by frame, or the high band is rebuilt from the 8-14 kHz envelope
        # and leaves a notch where the original band was cut.
        if spectrum.get("islas_mdct_sfb21"):
            ledger.penalize(
                f"intermittent MDCT spectral islands in 16-20.5 kHz "
                f"(P90-P10 gap {spectrum.get('brecha_p90_p10_agudos_db', 0.0):.1f} dB, "
                f"{(spectrum.get('ratio_huecos_sfb21') or spectrum.get('ratio_apagado_sfb21') or 0.0)*100:.0f}% "
                f"of frames zeroed): lowpass-disabled lossy codec signature",
                0.25,
                blocks_max_verdict=True,
                family=EvidenceFamily.TEMPORAL_MODULATION,
            )
        if spectrum.get("sospecha_excitador_armonico"):
            ledger.penalize(
                f"synthetic high-frequency harmonic exciter / SBR signature "
                f"(envelope correlation r = {spectrum.get('corr_env_excitador', 0.0):.2f}, "
                f"{spectrum.get('muesca_excitador_db', 0.0):.1f} dB notch): "
                f"adversarial bandwidth extension",
                0.30,
                blocks_max_verdict=True,
                family=EvidenceFamily.SPECTRAL_ENVELOPE,
            )
        elif spectrum.get("sbr_qmf_detectado"):
            ledger.penalize(
                f"seamless QMF subband replication detected "
                f"(additive patch correlation r = {spectrum.get('sbr_qmf_correlacion', 0.0):.2f}, "
                f"Z = {spectrum.get('sbr_qmf_z', 0.0):.1f} sigma): "
                f"HE-AAC / SBR bandwidth extension signature",
                0.55,
                blocks_max_verdict=True,
                family=EvidenceFamily.FILTERBANK_LATTICE,
            )
        if (spectrum.get("modulacion_psicoacustica_20k")
                and not (spectrum.get("sospecha_corte_opus_20k") and iqr > 550.0)):
            ledger.penalize(
                f"upper-band psychoacoustic modulation above 20 kHz "
                f"(median drop {spectrum.get('caida_modulacion_20k_db', 0.0):.1f} dB, "
                f"P95-P05 spread {spectrum.get('brecha_modulacion_20k_db', 0.0):.1f} dB): "
                f"frame-adaptive lossy codec signature",
                0.30,
                blocks_max_verdict=True,
                family=EvidenceFamily.TEMPORAL_MODULATION,
            )
        if spectrum.get("mdct_lattice_detected"):
            family = spectrum.get("mdct_lattice_family") or "MDCT"
            snr_grid = spectrum.get("mdct_lattice_snr") or 0.0
            ledger.penalize(
                f"phase-locked lapped transform quantization lattice detected "
                f"({family}, Z = {snr_grid:.1f} sigma): "
                f"full-bandwidth lossy codec signature",
                0.55,
                blocks_max_verdict=True,
                family=EvidenceFamily.FILTERBANK_LATTICE,
            )

        # Vector 5: libopus fullband cliff at 20.00 kHz. In isolation it is
        # indistinguishable from a mastering anti-alias low-pass, so it is scored
        # only together with another codec signature; on its own it stays a fact.
        if (spectrum.get("sospecha_corte_opus_20k")
                and not (spectrum.get("es_transferencia_dsd")
                         or spectrum.get("rolloff_analogico_genuino"))):
            corroborado = bool(
                spectrum.get("sospecha_joint_stereo")
                or (spectrum.get("iqr_corte_hz") or 0.0) > 550.0
                or (spectrum.get("ratio_huecos_mdct") or 0.0) > 0.10
                or spectrum.get("modulacion_psicoacustica_20k")
                or spectrum.get("mdct_lattice_detected")
            )
            if corroborado:
                ledger.penalize(
                    f"20.00 kHz cliff of "
                    f"{spectrum.get('caida_opus_20k_db', 0.0):.0f} dB with other "
                    f"codec evidence: libopus fullband transcode signature",
                    0.20,
                    blocks_max_verdict=False,
                    family=EvidenceFamily.SPECTRAL_CUTOFF,
                )
            else:
                ledger.note("a 20.00 kHz cliff is consistent with libopus fullband "
                            "and also with a mastering low-pass: inconclusive "
                            "without other codec evidence")

        # Vector 6: nature of the clipping. Only Gibbs overshoot is evidence of
        # lossy reconstruction; a mastering limiter is reported, never penalised.
        if spectrum.get("clipping_reconstruccion_lossy"):
            ledger.penalize(
                f"clipping made of isolated 1-2 sample events "
                f"({spectrum.get('clipping_fraccion_cortos', 0.0)*100:.0f}% of them) "
                f"with a band limit below 20.5 kHz: Gibbs overshoot from lossy "
                f"reconstruction",
                0.25,
                blocks_max_verdict=False,
                family=EvidenceFamily.TEMPORAL_MODULATION,
            )
        if spectrum.get("clipping_mastering_limitador"):
            ledger.note("clipping runs of 4+ consecutive samples with content up to "
                        "Nyquist: mastering brickwall limiter (observation, not an "
                        "authenticity defect)")

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
            if category == "far_cutoff" or rel_ceiling < 0.88:
                ledger.penalize(
                    f"declares {bits} bits at {sr/1000:.1f} kHz but its high band is "
                    f"{cutoff_txt}: the declared resolution is not backed by the "
                    f"content (consistent with a repackaged high-rate transcode)",
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
                    category == "no_cutoff"
                    and not spectrum.get("sospecha_joint_stereo")
                    and not spectrum.get("sospecha_corte_opus_20k")
                    and not spectrum.get("sbr_qmf_detectado")
                    and not spectrum.get("modulacion_psicoacustica_20k")
                    and not spectrum.get("mdct_lattice_detected")
                    and (spectrum.get("iqr_corte_hz") or 0.0) <= 550.0
                    and (spectrum.get("ratio_huecos_mdct") or 0.0) <= 0.10
                    and (spectrum.get("gradiente_borde_db_khz") or 0.0) < 35.0
                )
                if not (bits_clean and without_codec_nor_brickwall):
                    ledger.penalize(
                        f"declares {bits} bits at {sr/1000:.1f} kHz but its high band is "
                        f"{cutoff_txt}: the declared resolution is not backed by the "
                        f"content (consistent with a repackaged high-rate transcode)",
                        0.25,
                        blocks_max_verdict=True,
                        family=EvidenceFamily.BIT_DEPTH_CONTAINER,
                    )
                else:
                    ledger.note("Vector 5: 24-bit master with studio anti-aliasing transition band "
                                f"({rel_ceiling*100:.0f}% of Nyquist, clean LSB lattice and no lossy signature)")

    # 6. LSB utilisation: it ONLY penalises (Block 4).
    if bdi.get("aplica"):
        lsbs = bdi.get("lsbs")
        if lsbs == "empty":
            ledger.penalize(
                f"the LSBs are empty ({bdi.get('fraccion_16bit_grid','?')}% of samples "
                f"on the 16-bit grid): effective resolution of "
                f"{bdi.get('resolucion_efectiva_bits','?')} bits in a "
                f"{bits} bits — bit-depth upscale",
                0.40,
                family=EvidenceFamily.BIT_DEPTH_CONTAINER,
            )
        elif lsbs == "partial":
            ledger.penalize(
                f"LSBs partially inactive ({bdi.get('fraccion_16bit_grid','?')}% on the "
                f"16-bit grid): upscale with later processing",
                0.15,
                blocks_max_verdict=False,
                family=EvidenceFamily.BIT_DEPTH_CONTAINER,
            )
        elif lsbs == "indeterminate" and bdi.get("error"):
            ledger.note(f"could not evaluate the LSB utilization: {bdi['error']}")

        # Vector 4: native-integer audit (zero padding and quantisation comb).
        forensic = bdi.get("forense_bits") or {}
        if forensic.get("es_padded_16_en_24"):
            ledger.note(f"Vector 4: every sample has "
                        f"{forensic.get('trailing_zeros_lsb', 0)} trailing zero bits "
                        f"(16-bit zero-padded into 24 bits)")
            if lsbs != "empty":
                ledger.penalize(
                    f"the 8 low bits are always zero in the whole file: "
                    f"16-bit content zero-padded into a {bits}-bit container",
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
                f"24-bit container with a quantisation comb in the 8 LSBs "
                f"({comb_detail}): a 16-bit source scaled into 24 bits",
                0.40,
                family=EvidenceFamily.BIT_DEPTH_CONTAINER,
            )
        if forensic.get("es_16bit_dither_en_24"):
            occ = (forensic.get("lsb8_ocupacion_interior") or 0.0) * 100.0
            ledger.penalize(
                f"24-bit container with a 16-bit grid and boundary micro-dither "
                f"(interior LSB8 occupancy {occ:.2f}%): "
                f"16-bit source with synthetic dither in the lowest bits",
                0.40,
                family=EvidenceFamily.BIT_DEPTH_CONTAINER,
            )

    # MD5: it is NO longer scored. Its check is reported as a fact of its own in
    # resultado["md5"] (verify_md5), and an MD5 that does not match is a hard error.
    return ledger.finalize(spectrum)

ALBUM_COHERENCE_NOTE = ("Acoustic/low-HF track validated by album-level "
                        "master consistency")

def _deserves_album_coherence(result):
    """True when the only evidence against the track is a low spectral ceiling."""
    spectrum = result.get("esp") or {}
    bdi = result.get("bdi") or {}
    if spectrum.get("error"):
        return False
    if (result.get("md5") or {}).get("estado") != "match":
        return False
    if spectrum.get("corte_detectado"):
        return False                       # a step is not "only a low ceiling"
    if (spectrum.get("techo_rel_nyquist") or 1.0) >= 0.88:
        return False
    for key in ("muro_brickwall_nyquist", "espejo_aliasing_detectado",
                  "sospecha_joint_stereo", "sospecha_corte_opus_20k",
                  "clipping_reconstruccion_lossy", "islas_mdct_sfb21",
                  "sospecha_excitador_armonico", "sbr_qmf_detectado",
                  "modulacion_psicoacustica_20k", "mdct_lattice_detected"):
        if spectrum.get(key):
            return False
    if (spectrum.get("ratio_huecos_mdct") or 0.0) > 0.10:
        return False
    forensic = bdi.get("forense_bits") or {}
    if (forensic.get("es_padded_16_en_24")
            or forensic.get("es_24bit_escalado_en_peine")
            or forensic.get("es_16bit_dither_en_24")
            or bdi.get("lsbs") == "empty"):
        return False
    permitidos = ("spectral ceiling", "partial hi-res content",
                  "reduced high band", "high band is destroyed",
                  "smooth analogue/archival roll-off", "dual-mono",
                  "high/mid ratio", "mastering brickwall limiter",
                  "studio anti-aliasing", "analogue/dsd master",
                  "dsd/sacd noise-shaping")
    for problem in result.get("problemas") or []:
        if not any(text in problem.lower() for text in permitidos):
            return False
    return True


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
    return full_band and score >= 0.70 and len(fams) == 0


def apply_album_coherence(results):
    """Vector 6: rescue acoustic/low-HF tracks of a consistent album.

    A candidate is any track accepted by `_deserves_album_coherence` (only the
    spectral ceiling is against it), whatever its current label: SUSPICIOUS or
    PROBABLE UPSCALE, and also PROBABLY LOSSLESS when it was already excused on
    its own by the Phase 8D acoustic exemption (`decaimiento_acustico_suave`
    with clean bits and no lossy signature). When at least 80% of the tracks in a
    directory are GENUINE LOSSLESS (or clean PROBABLY LOSSLESS with score >= 0.70
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
        if not quorum_tracks:
            continue
        reference = Counter(
            (r["meta"]["sample_rate"], r["meta"]["bits_per_sample"])
            for r in quorum_tracks).most_common(1)[0][0]
        quorum_ref = [r for r in quorum_tracks
                      if (r["meta"]["sample_rate"],
                          r["meta"]["bits_per_sample"]) == reference]
        has_strong_anchor = any(
            r.get("veredicto") == "GENUINE LOSSLESS"
            or float(r.get("score") or 0.0) >= 0.75
            for r in quorum_ref
        )
        if not has_strong_anchor or len(quorum_ref) / float(len(validas)) < 0.80:
            continue

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
        max_candidates = max(2, len(validas) // 6)
        if not 1 <= len(candidatos) <= max_candidates:
            continue

        for r in candidatos:
            r["veredicto"] = "PROBABLY LOSSLESS"
            r["score"] = max(float(r.get("score") or 0.0), 0.60)
            r["problemas"] = list(r.get("problemas") or []) + [ALBUM_COHERENCE_NOTE]
            r["evidencias_forenses"] = list(r.get("evidencias_forenses") or []) + [ALBUM_COHERENCE_NOTE]
            r["coherencia_album"] = True
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
        evidence.append(ALBUM_COHERENCE_NOTE)
    return evidence
