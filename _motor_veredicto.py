# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 FLAC VERIFIER Contributors
"""Verdict tree, scoring and album-level coherence.

Extracted from the motor facade in Phase 4.
"""
from dataclasses import dataclass
from enum import Enum, IntEnum
import os

from _motor_bits import LsbStatus, Md5Status
from _motor_espectral import CodecLatticeFamily, CutoffClass

LIMIT_CD_HZ = 22050


class Verdict(str, Enum):
    """Canonical authenticity verdicts emitted by the scoring engine."""

    GENUINE_LOSSLESS = "GENUINE LOSSLESS"
    PROBABLY_LOSSLESS = "PROBABLY LOSSLESS"
    SUSPICIOUS = "SUSPICIOUS"
    PROBABLE_UPSCALE = "PROBABLE UPSCALE"
    INDETERMINATE = "indeterminate"


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


class FindingCode(str, Enum):
    HIRES_BELOW_CD = "hires_below_cd"
    HIRES_NO_ULTRASONIC = "hires_no_ultrasonic"
    LOW_SPECTRAL_CEILING = "low_spectral_ceiling"
    NYQUIST_BRICKWALL = "nyquist_brickwall"
    ALIASING_MIRROR = "aliasing_mirror"
    HIRES_UPSCALE_CUTOFF = "hires_upscale_cutoff"
    ARTIFICIAL_CUTOFF = "artificial_cutoff"
    JOINT_STEREO_COLLAPSE = "joint_stereo_collapse"
    STEREO_PHASE_COUPLING = "stereo_phase_coupling"
    CUTOFF_JITTER = "cutoff_jitter"
    MDCT_SPECTRAL_HOLES = "mdct_spectral_holes"
    MP3_SFB21_ISLANDS = "mp3_sfb21_islands"
    EXCITER_RECTIFICATION = "exciter_rectification"
    SBR_REPLICA = "sbr_replica"
    PSYCHOACOUSTIC_MOD_20K = "psychoacoustic_mod_20k"
    LAPPED_CODEC_LATTICE = "lapped_codec_lattice"
    OPUS_20K_CUTOFF = "opus_20k_cutoff"
    GIBBS_LOSSY_CLIPPING = "gibbs_lossy_clipping"
    CONTAINER_NOT_BACKED = "container_not_backed"
    PADDED_16_IN_24 = "padded_16_in_24"
    DITHER_16_IN_24 = "dither_16_in_24"
    EMPTY_LSBS = "empty_lsbs"
    PARTIAL_LSBS = "partial_lsbs"
    GAIN_COMB_24 = "gain_comb_24"
    NOISE_SHAPED_16_IN_24 = "noise_shaped_16_in_24"
    NEURAL_VOCODER_ARTIFACTS = "neural_vocoder_artifacts"
    SPECTRAL_FOLDING_BWE = "spectral_folding_bwe"
    ALBUM_LOSSY_CONSISTENCY = "album_lossy_consistency"


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


_VERDICT_TO_CAP_RANK = {
    Verdict.GENUINE_LOSSLESS: VerdictCap.GENUINE_LOSSLESS,
    Verdict.PROBABLY_LOSSLESS: VerdictCap.PROBABLY_LOSSLESS,
    Verdict.SUSPICIOUS: VerdictCap.SUSPICIOUS,
    Verdict.PROBABLE_UPSCALE: VerdictCap.PROBABLE_UPSCALE,
}
_CAP_RANK_TO_VERDICT = {
    VerdictCap.GENUINE_LOSSLESS: Verdict.GENUINE_LOSSLESS,
    VerdictCap.PROBABLY_LOSSLESS: Verdict.PROBABLY_LOSSLESS,
    VerdictCap.SUSPICIOUS: Verdict.SUSPICIOUS,
    VerdictCap.PROBABLE_UPSCALE: Verdict.PROBABLE_UPSCALE,
}


class AlbumCoherenceAction(str, Enum):
    """Explicit action stamped on tracks modified by album-level coherence."""

    NONE = "none"
    RESCUED_LOSSLESS = "rescued_lossless"
    CONVICTED_LOSSY = "convicted_lossy"


class AlbumQuorumKind(str, Enum):
    """Directory-level quorum outcome resolved by `_evaluate_album_quorum()`."""

    NONE = "none"
    CLEAN_STANDARD = "clean_standard"
    CLEAN_MIXED_VHS = "clean_mixed_vhs"
    LOSSY_CODEC = "lossy_codec"


@dataclass(frozen=True)
class AlbumQuorumDecision:
    """Pure album-level quorum decision for a single directory batch."""

    kind: AlbumQuorumKind
    reference_format: tuple[int, int] | None = None
    candidates: tuple[dict, ...] = ()
    dominant_family: str | None = None


@dataclass(frozen=True)
class ScoreEvaluation:
    """Pure, immutable output of `_ScoreLedger.evaluate()`."""

    score: float
    verdict: Verdict
    problems: tuple[str, ...]
    notes: tuple[str, ...]
    families: tuple[str, ...]
    finding_codes: tuple[str, ...]
    corroborated: bool


class _ScoreLedger:
    """Deterministic score and verdict ceiling accumulator."""

    def __init__(self):
        self.votes = 0.0
        self.max_votes = 0.0
        self.penalty = 0.0
        self.cap = VerdictCap.GENUINE_LOSSLESS
        self.has_hard_proof = False
        self.problems = []
        self.notes = []
        self.families = []
        self.finding_codes: list[str] = []

    @property
    def is_blocked(self) -> bool:
        """True when a blocking defect caps the verdict at SUSPICIOUS or below."""
        return self.cap <= VerdictCap.SUSPICIOUS

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
        coerced = VerdictCap(target_cap)
        if coerced < self.cap:
            self.cap = coerced

    def penalize(
        self,
        text,
        amount,
        blocks_max_verdict=True,
        family=None,
        tier=None,
        code: FindingCode | str | None = None,
    ):
        self.problems.append(text)
        if code is not None:
            code_value = code.value if isinstance(code, Enum) else str(code)
            if code_value:
                self.finding_codes.append(code_value)
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
        if tier is EvidenceTier.HARD_PROOF:
            self.has_hard_proof = True
        if blocks_max_verdict or tier in (EvidenceTier.HARD_PROOF, EvidenceTier.SOFT_BLOCKING):
            self.lower_cap(VerdictCap.SUSPICIOUS)

    def evaluate(self) -> ScoreEvaluation:
        """Pure score and verdict computation with zero external mutation."""
        corroborated = bool(len(self.families) >= 2 or self.has_hard_proof)

        if self.max_votes == 0:
            return ScoreEvaluation(
                score=0.0,
                verdict=Verdict.INDETERMINATE,
                problems=tuple(self.problems),
                notes=tuple(self.notes),
                families=tuple(self.families),
                finding_codes=tuple(self.finding_codes),
                corroborated=corroborated,
            )

        score = max(0.0, self.votes / self.max_votes - self.penalty)
        if self.is_blocked and not corroborated and score < 0.50:
            score = 0.50

        if self.cap is VerdictCap.PROBABLE_UPSCALE:
            verdict = (
                Verdict.PROBABLE_UPSCALE if corroborated else Verdict.SUSPICIOUS
            )
        elif self.cap is VerdictCap.SUSPICIOUS:
            verdict = (
                Verdict.SUSPICIOUS
                if (score >= 0.50 or not corroborated)
                else Verdict.PROBABLE_UPSCALE
            )
        else:
            if score >= 0.80:
                verdict = Verdict.GENUINE_LOSSLESS
            elif score >= 0.60:
                verdict = Verdict.PROBABLY_LOSSLESS
            elif score >= 0.40:
                verdict = Verdict.SUSPICIOUS
            else:
                verdict = (
                    Verdict.PROBABLE_UPSCALE if corroborated else Verdict.SUSPICIOUS
                )
            if _VERDICT_TO_CAP_RANK[verdict] > self.cap:
                verdict = _CAP_RANK_TO_VERDICT[self.cap]

        return ScoreEvaluation(
            score=score,
            verdict=verdict,
            problems=tuple(self.problems),
            notes=tuple(self.notes),
            families=tuple(self.families),
            finding_codes=tuple(self.finding_codes),
            corroborated=corroborated,
        )

    def finalize(self, spectrum, include_notes=False):
        evaluation = self.evaluate()
        if not spectrum.get("error"):
            spectrum["familias_evidencia_lossy"] = list(evaluation.families)
            spectrum["corroboracion_multifamilia"] = evaluation.corroborated
            spectrum["finding_codes"] = list(evaluation.finding_codes)
        if include_notes:
            return (
                evaluation.score,
                evaluation.verdict.value,
                list(evaluation.problems),
                list(evaluation.notes),
            )
        return evaluation.score, evaluation.verdict.value, list(evaluation.problems)


def _has_non_opus_codec_signature(spectrum) -> bool:
    """True when an explicit non-Opus filterbank, SBR, neural BWE, or MP3 sfb21 signature is present."""
    lattice_family = spectrum.get("mdct_lattice_family") or ""
    return bool(
        (
            spectrum.get("mdct_lattice_detected")
            and lattice_family
            and lattice_family != CodecLatticeFamily.CELT_960.value
            and "Opus" not in str(lattice_family)
        )
        or spectrum.get("islas_mdct_sfb21")
        or spectrum.get("sbr_qmf_detectado")
        or spectrum.get("neural_vocoder_detected")
        or spectrum.get("spectral_folding_detected")
    )


def _opus_corroboration_flags(spectrum) -> tuple[bool, bool, bool]:
    """Return `(iqr_suppressed_by_opus, other_corroboration, sub_lattice)` for
    both 20.00 kHz Opus shelf scoring stages."""
    opus_eligible = bool(
        spectrum.get("sospecha_corte_opus_20k")
        and not (
            spectrum.get("es_transferencia_dsd")
            or spectrum.get("rolloff_analogico_genuino")
        )
    )
    if not opus_eligible:
        return False, False, False
    iqr = spectrum.get("iqr_corte_hz") or 0.0
    holes = spectrum.get("ratio_huecos_mdct") or 0.0
    iqr_suppressed_by_opus = bool(
        spectrum.get("sospecha_joint_stereo")
        or iqr > 550.0
        or holes > 0.10
    )
    other_corroboration = bool(
        iqr_suppressed_by_opus
        or spectrum.get("modulacion_psicoacustica_20k")
        or spectrum.get("mdct_lattice_detected")
    )
    ceiling_hz = spectrum.get("techo_hz")
    if "celt_lattice_snr" in spectrum:
        celt_snr = float(spectrum.get("celt_lattice_snr") or 0.0)
        lat_snr = float(spectrum.get("mdct_lattice_snr") or 0.0)
        drop_20k = float(spectrum.get("caida_opus_20k_db") or 0.0)
        has_sub_peak = bool(
            celt_snr >= 3.8 or (lat_snr >= 4.8 and drop_20k >= 25.0)
        )
    else:
        has_sub_peak = float(spectrum.get("mdct_lattice_snr") or 0.0) >= 3.8
    sub_lattice = bool(
        not other_corroboration
        and (ceiling_hz is None or float(ceiling_hz) <= 20600.0)
        and has_sub_peak
    )
    return iqr_suppressed_by_opus, other_corroboration, sub_lattice


def _has_clean_active_24bit_lattice(bdi) -> bool:
    """True when the 24/32-bit LSB lattice is active with zero padding, comb, or micro-dither flags."""
    forensic = (bdi or {}).get("forense_bits") or {}
    return bool(
        (bdi or {}).get("lsbs") == LsbStatus.ACTIVE.value
        and not forensic.get("es_padded_16_en_24")
        and not forensic.get("es_24bit_escalado_en_peine")
        and not forensic.get("es_16bit_dither_en_24")
    )


def _is_codec_clean_core(spectrum, category, rel_cutoff) -> bool:
    """Shared codec-free core predicate composed by Rule 2 and Rule 5 exemptions."""
    near_adc_edge = (
        category == CutoffClass.NO_CUTOFF.value
        or (category == CutoffClass.NEAR_NYQUIST.value and (rel_cutoff or 0.0) >= 0.94)
    )
    return bool(
        near_adc_edge
        and not spectrum.get("sospecha_joint_stereo")
        and not spectrum.get("sospecha_corte_opus_20k")
        and not spectrum.get("sbr_qmf_detectado")
        and not spectrum.get("modulacion_psicoacustica_20k")
        and not spectrum.get("mdct_lattice_detected")
        and not spectrum.get("neural_vocoder_detected")
        and not spectrum.get("spectral_folding_detected")
        and (spectrum.get("iqr_corte_hz") or 0.0) <= 550.0
        and (spectrum.get("ratio_huecos_mdct") or 0.0) <= 0.10
    )


def _allows_dark_sparse_iqr_rescue(sr, spectrum, md5_state) -> bool:
    """True when a dark, sparse, uncut <=48 kHz track with verified MD5 may
    still qualify for album coherence despite elevated cut-off IQR."""
    return bool(
        md5_state == Md5Status.MATCH.value
        and sr <= 48000
        and spectrum.get("clase_corte") == CutoffClass.NO_CUTOFF.value
        and 0.0 < float(spectrum.get("centroide_hz") or 0.0) < 460.0
        and (
            (
                (spectrum.get("iqr_corte_hz") or 0.0) <= 950.0
                and (spectrum.get("ratio_huecos_mdct") or 0.0) <= 0.01
                and float(spectrum.get("mdct_lattice_snr") or 0.0) < 5.5
            )
            or (
                float(spectrum.get("techo_hz") or 0.0) < 12500.0
                and float(spectrum.get("gradiente_borde_db_khz") or 0.0) < 25.0
                and (spectrum.get("ratio_huecos_mdct") or 0.0) <= 0.05
            )
        )
    )


def _allows_24b_dark_vhs_rescue(sr, bits, bdi, spectrum) -> bool:
    """True when a dark <=48 kHz 24-bit track with active LSBs may waive Rule 5
    during album coherence."""
    return bool(
        sr <= 48000
        and bits > 16
        and bdi.get("lsbs") == LsbStatus.ACTIVE.value
        and 0.0 < float(spectrum.get("centroide_hz") or 0.0) < 1800.0
    )


def calculate_score(meta, spectrum, bdi, include_notes=False):
    ledger = _ScoreLedger()

    sr      = meta["sample_rate"]
    bits    = meta["bits_per_sample"]
    nyquist = sr // 2
    is_hires = sr >= 88200

    if not spectrum.get("error"):
        ceiling     = spectrum["techo_hz"]
        category     = spectrum.get("clase_corte", CutoffClass.NO_CUTOFF.value)
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
                    code=FindingCode.HIRES_BELOW_CD,
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
        # Fail-closed on 24-bit: when the bit-depth audit applies, only a fully
        # active LSB lattice earns the acoustic exemption. "partial",
        # "indeterminate" and None all mean "not verified", not "clean".
        bits_ok_acoustic = (not bdi.get("aplica")) or _has_clean_active_24bit_lattice(bdi)
        codec_clean_core = _is_codec_clean_core(spectrum, category, rel_cutoff)
        without_signatures_lossy = (
            codec_clean_core
            and not spectrum.get("clipping_reconstruccion_lossy")
            and not spectrum.get("islas_mdct_sfb21")
            and not spectrum.get("sospecha_excitador_armonico")
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
                    code=FindingCode.HIRES_NO_ULTRASONIC,
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
                f"Unexplained low spectral ceiling (the high band is destroyed): "
                f"spectral ceiling drops to {ceiling/1000:.1f} kHz "
                f"({rel_ceiling*100:.0f}% of Nyquist of {nyq_txt})",
                0.40,
                family=EvidenceFamily.SPECTRAL_CUTOFF,
                tier=EvidenceTier.SOFT_BLOCKING,
                code=FindingCode.LOW_SPECTRAL_CEILING,
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
                code=FindingCode.NYQUIST_BRICKWALL,
            )
        if spectrum.get("espejo_aliasing_detectado"):
            ledger.penalize(
                f"Unfiltered-interpolator upscale (spectral aliasing): "
                f"ultrasonic band mirrors content below 22.05 kHz "
                f"(r = {spectrum.get('correlacion_espejo_22k', 0.0):.2f})",
                0.40,
                family=EvidenceFamily.SPECTRAL_CUTOFF,
                code=FindingCode.ALIASING_MIRROR,
            )
        if spectrum.get("es_transferencia_dsd"):
            ledger.note("Genuine DSD/SACD transfer: characteristic ultrasonic "
                        "noise-shaping profile detected in 24-40 kHz")

        # 3. Cut-off: it is only flagged if it is clearly below the Nyquist.
        # A prior cut plus a flat dither floor is never an analogue roll-off:
        # `rolloff_analogico_genuino` is false in that case, so a Hi-Res
        # far_cutoff cannot bypass this penalty.
        ledger.add_votes(0.0, 3.0)
        if category == CutoffClass.FAR_CUTOFF.value:
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
                    code=FindingCode.HIRES_UPSCALE_CUTOFF,
                )
            else:
                ledger.penalize(
                    f"Lossy codec signature (artificial cut-off at "
                    f"{spectrum['frecuencia_corte']/1000:.1f} kHz, "
                    f"{rel_cutoff*100:.0f}% of Nyquist of {nyq_txt}): "
                    f"high frequencies are sharply cut off",
                    0.40,
                    family=EvidenceFamily.SPECTRAL_CUTOFF,
                    code=FindingCode.ARTIFICIAL_CUTOFF,
                )
        elif category == CutoffClass.NEAR_NYQUIST.value:
            # It is neither flagged nor rewarded unless a genuinely band-limited
            # ADC transition sits right on the Nyquist edge (>= 95%) with zero
            # codec evidence; otherwise it stays inconclusive.
            backed_24b_adc = bool(
                bits > 16
                and sr <= 48000
                and (acoustic_exemption or band_limited_exemption)
                and spectrum.get("native_24bit_hf_tail")
            )
            if ((0.75 <= rel_ceiling < 0.88 or backed_24b_adc)
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
        if (
            spectrum.get("ratio_aplica")
            and not (
                band_limited_exemption
                or (acoustic_exemption and rel_ceiling < 0.70)
                or (
                    acoustic_exemption
                    and rel_ceiling < 0.88
                    and float(spectrum.get("ratio") or 0.0) <= 0.01
                )
            )
        ):
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
                code=FindingCode.JOINT_STEREO_COLLAPSE,
            )
        elif spectrum.get("stereo_phase_coupling"):
            ledger.penalize(
                f"Lossy intensity-stereo / channel-coupling signature: "
                f"inter-channel coherence snaps from {spectrum.get('msc_lo', 0.0):.2f} "
                f"to {spectrum.get('msc_hi', 0.0):.2f} (step +{spectrum.get('msc_step_hf', 0.0):.2f}) "
                f"with {spectrum.get('ipd_phase_lock_hf', 0.0)*100:.0f}% IPD phase lock",
                0.25,
                blocks_max_verdict=True,
                family=EvidenceFamily.STEREO_COLLAPSE,
                tier=EvidenceTier.SOFT_BLOCKING,
                code=FindingCode.STEREO_PHASE_COUPLING,
            )
        iqr = spectrum.get("iqr_corte_hz") or 0.0
        # The jitter is already part of the Opus evidence: do not penalise the
        # same symptom twice when the 20.00 kHz cliff is already corroborated.
        iqr_suppressed_by_opus, opus_other_corroboration, opus_sub_lattice = (
            _opus_corroboration_flags(spectrum)
        )
        if iqr > 550.0 and not iqr_suppressed_by_opus:
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
                    code=FindingCode.CUTOFF_JITTER,
                )
            else:
                ledger.penalize(
                    f"Frame-adaptive lossy codec signature: instantaneous "
                    f"cut-off jitters across frames (IQR {iqr:.0f} Hz)",
                    0.20,
                    blocks_max_verdict=False,
                    family=EvidenceFamily.TEMPORAL_MODULATION,
                    code=FindingCode.CUTOFF_JITTER,
                )
        huecos = spectrum.get("ratio_huecos_mdct") or 0.0
        if huecos > 0.10:
            ledger.penalize(
                f"Lossy codec MDCT spectral holes: zeroed-out high-frequency "
                f"subbands in {huecos*100:.0f}% of loud frames",
                0.25,
                blocks_max_verdict=False,
                family=EvidenceFamily.TEMPORAL_MODULATION,
                code=FindingCode.MDCT_SPECTRAL_HOLES,
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
                code=FindingCode.MP3_SFB21_ISLANDS,
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
                code=FindingCode.EXCITER_RECTIFICATION,
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
                code=FindingCode.SBR_REPLICA,
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
                code=FindingCode.PSYCHOACOUSTIC_MOD_20K,
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
                code=FindingCode.LAPPED_CODEC_LATTICE,
            )
        if spectrum.get("neural_vocoder_detected"):
            ledger.penalize(
                f"AI super-resolution / neural vocoder synthesis signature: "
                f"periodic high-frequency spectral comb ({spectrum.get('vocoder_comb_spacing_hz', 0.0):.1f} Hz spacing, "
                f"Z = {spectrum.get('vocoder_hf_comb_z', 0.0):.1f} sigma) with phase-locked upper band "
                f"(phase coherence delta +{spectrum.get('vocoder_phase_coh_diff', 0.0):.2f})",
                0.55,
                blocks_max_verdict=True,
                family=EvidenceFamily.FILTERBANK_LATTICE,
                tier=EvidenceTier.HARD_PROOF,
                code=FindingCode.NEURAL_VOCODER_ARTIFACTS,
            )
        if (spectrum.get("spectral_folding_detected")
                and not spectrum.get("sbr_qmf_detectado")
                and not spectrum.get("sospecha_excitador_armonico")):
            ledger.penalize(
                f"Artificial bandwidth extension / time-domain spectral folding: "
                f"upper band mirrors lower-band trajectories around "
                f"{spectrum.get('spectral_folding_pivot_hz', 0.0)/1000.0:.2f} kHz "
                f"(r = {spectrum.get('spectral_folding_corr', 0.0):.2f}, "
                f"Z = {spectrum.get('spectral_folding_z', 0.0):.1f} sigma)",
                0.55,
                blocks_max_verdict=True,
                family=EvidenceFamily.SPECTRAL_ENVELOPE,
                tier=EvidenceTier.HARD_PROOF,
                code=FindingCode.SPECTRAL_FOLDING_BWE,
            )

        # Steep 20.00 kHz shelf (libopus fullband or high-rate codec low-pass).
        # In isolation it is indistinguishable from a mastering anti-alias
        # low-pass, so it is scored only together with another codec signature;
        # on its own it stays a fact.
        if (spectrum.get("sospecha_corte_opus_20k")
                and not (spectrum.get("es_transferencia_dsd")
                         or spectrum.get("rolloff_analogico_genuino"))):
            corroborated_20k = opus_other_corroboration or opus_sub_lattice
            if corroborated_20k:
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
                    0.45 if opus_sub_lattice else 0.20,
                    blocks_max_verdict=opus_sub_lattice,
                    family=EvidenceFamily.SPECTRAL_CUTOFF,
                    code=FindingCode.OPUS_20K_CUTOFF,
                )
            elif category != CutoffClass.FAR_CUTOFF.value:
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
                code=FindingCode.GIBBS_LOSSY_CLIPPING,
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
            native_24b_tail_ok = bool(
                category != CutoffClass.FAR_CUTOFF.value
                and (acoustic_exemption or band_limited_exemption)
                and spectrum.get("native_24bit_hf_tail")
            )
            if native_24b_tail_ok:
                ledger.note(
                    f"Genuine {bits}-bit high-headroom master: continuous sub-ceiling "
                    f"converter roll-off up to Nyquist backs the declared "
                    f"{bits}-bit/{sr/1000:.1f} kHz format"
                )
            elif category == CutoffClass.FAR_CUTOFF.value or rel_ceiling < 0.88:
                ledger.penalize(
                    f"Repackaged {bits}-bit container from a band-limited/lossy source: "
                    f"declares {bits} bits at {sr/1000:.1f} kHz but high frequencies are "
                    f"{cutoff_txt} (declared resolution is not backed by the content)",
                    0.25,
                    blocks_max_verdict=True,
                    family=EvidenceFamily.BIT_DEPTH_CONTAINER,
                    code=FindingCode.CONTAINER_NOT_BACKED,
                )
            elif 0.88 <= rel_ceiling < 0.95:
                bits_clean = _has_clean_active_24bit_lattice(bdi)
                without_codec_nor_brickwall = (
                    codec_clean_core
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
                        code=FindingCode.CONTAINER_NOT_BACKED,
                    )
                else:
                    ledger.note(f"Genuine {bits}-bit master with studio anti-aliasing filter: "
                                f"smooth transition band ({rel_ceiling*100:.0f}% of Nyquist, "
                                f"active LSB lattice and no lossy signature)")

    # 6. LSB utilisation: it ONLY penalises (Block 4).
    if bdi.get("aplica"):
        lsbs = bdi.get("lsbs")
        forensic = bdi.get("forense_bits") or {}
        if lsbs == LsbStatus.EMPTY.value:
            if forensic.get("es_padded_16_en_24"):
                ledger.penalize(
                    f"16-bit audio zero-padded inside a {bits}-bit container "
                    f"(LSBs are empty: {bdi.get('fraccion_16bit_grid','?')}% of samples "
                    f"on the 16-bit grid, {forensic.get('trailing_zeros_lsb', 8)} "
                    f"trailing zero bits; effective resolution "
                    f"{bdi.get('resolucion_efectiva_bits','?')} bits)",
                    0.40,
                    family=EvidenceFamily.BIT_DEPTH_CONTAINER,
                    code=FindingCode.PADDED_16_IN_24,
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
                    code=FindingCode.DITHER_16_IN_24,
                )
            else:
                ledger.penalize(
                    f"Bit-depth upscale from a 16-bit source (LSBs are empty: "
                    f"{bdi.get('fraccion_16bit_grid','?')}% of samples on the 16-bit grid; "
                    f"effective resolution {bdi.get('resolucion_efectiva_bits','?')} bits "
                    f"in a {bits}-bit container)",
                    0.40,
                    family=EvidenceFamily.BIT_DEPTH_CONTAINER,
                    code=FindingCode.EMPTY_LSBS,
                )
        elif lsbs == LsbStatus.PARTIAL.value:
            ledger.penalize(
                f"Bit-depth upscale with later processing: LSBs partially inactive "
                f"({bdi.get('fraccion_16bit_grid','?')}% on the 16-bit grid; "
                f"effective resolution ~{bdi.get('resolucion_efectiva_bits','?')} bits "
                f"in a {bits}-bit container)",
                0.15,
                blocks_max_verdict=False,
                family=EvidenceFamily.BIT_DEPTH_CONTAINER,
                code=FindingCode.PARTIAL_LSBS,
            )
        elif lsbs == LsbStatus.INDETERMINATE.value and bdi.get("error"):
            ledger.note(f"Bit-depth LSB check skipped: {bdi['error']}")

        # Native-integer audit (zero padding and quantisation comb).
        if forensic.get("es_padded_16_en_24") and lsbs != LsbStatus.EMPTY.value:
            ledger.penalize(
                f"16-bit audio zero-padded inside a {bits}-bit container "
                f"(the lowest {forensic.get('trailing_zeros_lsb', 8)} bits are always "
                f"zero across the whole file)",
                0.40,
                family=EvidenceFamily.BIT_DEPTH_CONTAINER,
                code=FindingCode.PADDED_16_IN_24,
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
                code=FindingCode.GAIN_COMB_24,
            )
        if forensic.get("es_16bit_dither_en_24") and lsbs != LsbStatus.EMPTY.value:
            occ = (forensic.get("lsb8_ocupacion_interior") or 0.0) * 100.0
            ledger.penalize(
                f"16-bit source padded with synthetic boundary micro-dither "
                f"inside a {bits}-bit container (interior LSB8 occupancy {occ:.2f}%)",
                0.40,
                family=EvidenceFamily.BIT_DEPTH_CONTAINER,
                code=FindingCode.DITHER_16_IN_24,
            )
        if (spectrum.get("noise_shaped_16_in_24")
                and lsbs != LsbStatus.EMPTY.value
                and not forensic.get("es_padded_16_en_24")
                and not forensic.get("es_24bit_escalado_en_peine")
                and not forensic.get("es_16bit_dither_en_24")):
            floor_q = spectrum.get("quiet_hf_floor_dbfs")
            floor_txt = f"{floor_q:.1f} dBFS" if isinstance(floor_q, (int, float)) else "elevated"
            ledger.penalize(
                f"Noise-shaped 16-bit dither floor inside a {bits}-bit container "
                f"(quiet 19-21.5 kHz floor {floor_txt}, "
                f"+{spectrum.get('quiet_hf_slope_db', 0.0):.1f} dB upper-band rise, "
                f"flatness {spectrum.get('quiet_hf_flatness', 0.0):.2f})",
                0.30,
                blocks_max_verdict=True,
                family=EvidenceFamily.BIT_DEPTH_CONTAINER,
                tier=EvidenceTier.SOFT_BLOCKING,
                code=FindingCode.NOISE_SHAPED_16_IN_24,
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
    md5_state = (result.get("md5") or {}).get("estado")
    if md5_state not in (Md5Status.MATCH.value, Md5Status.ABSENT.value):
        return False
    if spectrum.get("corte_artificial") or spectrum.get("clase_corte") == CutoffClass.FAR_CUTOFF.value:
        return False                       # a below-Nyquist codec step is never rescued
    max_coh_rel = 0.95 if (bits > 16 and sr <= 48000) else 0.88
    if (spectrum.get("techo_rel_nyquist") or 1.0) >= max_coh_rel:
        return False
    for key in ("muro_brickwall_nyquist", "espejo_aliasing_detectado",
                  "sospecha_joint_stereo", "stereo_phase_coupling",
                  "sospecha_corte_opus_20k",
                  "clipping_reconstruccion_lossy", "islas_mdct_sfb21",
                  "sospecha_excitador_armonico", "sbr_qmf_detectado",
                  "modulacion_psicoacustica_20k", "mdct_lattice_detected",
                  "neural_vocoder_detected", "spectral_folding_detected",
                  "noise_shaped_16_in_24"):
        if spectrum.get(key):
            return False
    dark_sparse_iqr_ok = _allows_dark_sparse_iqr_rescue(sr, spectrum, md5_state)
    if (spectrum.get("iqr_corte_hz") or 0.0) > 550.0 and not dark_sparse_iqr_ok:
        return False
    if (spectrum.get("ratio_huecos_mdct") or 0.0) > 0.10:
        return False
    if sr >= 88200 and (bits > 16 or bdi.get("aplica")) and bdi.get("lsbs") != LsbStatus.ACTIVE.value:
        return False
    forensic = bdi.get("forense_bits") or {}
    if (forensic.get("es_padded_16_en_24")
            or forensic.get("es_24bit_escalado_en_peine")
            or forensic.get("es_16bit_dither_en_24")
            or bdi.get("lsbs") == LsbStatus.EMPTY.value):
        return False
    allow_24b_dark_vhs = _allows_24b_dark_vhs_rescue(sr, bits, bdi, spectrum)
    problems = result.get("problemas") or []
    if ("finding_codes" in spectrum
            and isinstance(spectrum["finding_codes"], list)
            and len(spectrum["finding_codes"]) == len(problems)):
        hires_clean_bits = (not bdi.get("aplica")) or bdi.get("lsbs") == LsbStatus.ACTIVE.value
        for raw_code in spectrum["finding_codes"]:
            code_val = raw_code.value if isinstance(raw_code, Enum) else str(raw_code)
            if code_val == FindingCode.LOW_SPECTRAL_CEILING.value:
                continue
            if (code_val == FindingCode.HIRES_BELOW_CD.value
                    and sr >= 88200 and hires_clean_bits):
                continue
            if (code_val == FindingCode.HIRES_NO_ULTRASONIC.value
                    and sr > 48000 and hires_clean_bits):
                continue
            if code_val == FindingCode.CONTAINER_NOT_BACKED.value and allow_24b_dark_vhs:
                continue
            if code_val == FindingCode.CUTOFF_JITTER.value and dark_sparse_iqr_ok:
                continue
            return False
        return True
    permitidos = [
        "spectral ceiling", "partial hi-res content", "partial high-res content",
        "reduced high band", "reduced high-frequency band",
        "high band is destroyed", "severely truncated high frequencies",
        "smooth analogue/archival roll-off", "smooth high-frequency roll-off",
        "dual-mono", "high/mid ratio", "mastering brickwall limiter",
        "studio anti-aliasing", "anti-alias filter of a legitimate master",
        "analogue/dsd master", "dsd/sacd noise-shaping", "dsd/sacd transfer",
    ]
    if sr >= 88200 and (not bdi.get("aplica") or bdi.get("lsbs") == LsbStatus.ACTIVE.value):
        permitidos.extend(["below the cd band", "not backed by content"])
    for problem in problems:
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
    if result.get("veredicto") not in (
        Verdict.GENUINE_LOSSLESS.value,
        Verdict.PROBABLY_LOSSLESS.value,
    ):
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
    lattice, neural vocoder/folding BWE proof, or a sharp ~20 kHz (78-87% Nyquist)
    codec brickwall in 48 kHz."""
    spectrum = result.get("esp") or {}
    if (spectrum.get("mdct_lattice_detected")
            or spectrum.get("neural_vocoder_detected")
            or spectrum.get("spectral_folding_detected")):
        return True
    if (spectrum.get("sospecha_corte_opus_20k")
            and result.get("veredicto") in (
                Verdict.SUSPICIOUS.value,
                Verdict.PROBABLE_UPSCALE.value,
            )):
        return True
    if result.get("veredicto") != Verdict.PROBABLE_UPSCALE.value:
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
    if verdict == Verdict.GENUINE_LOSSLESS.value:
        return True
    if verdict != Verdict.PROBABLY_LOSSLESS.value:
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
    if result.get("veredicto") != Verdict.PROBABLY_LOSSLESS.value:
        return False
    if (result.get("md5") or {}).get("estado") not in (
        Md5Status.MATCH.value,
        Md5Status.ABSENT.value,
    ):
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
                and (rel >= 0.25 or spectrum.get("native_24bit_hf_tail"))
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
    if spectrum.get("corte_artificial") or spectrum.get("clase_corte") == CutoffClass.FAR_CUTOFF.value:
        return False
    for key in (
        "muro_brickwall_nyquist", "espejo_aliasing_detectado",
        "sospecha_joint_stereo", "stereo_phase_coupling",
        "sospecha_corte_opus_20k",
        "clipping_reconstruccion_lossy", "islas_mdct_sfb21",
        "sospecha_excitador_armonico", "sbr_qmf_detectado",
        "modulacion_psicoacustica_20k", "mdct_lattice_detected",
        "neural_vocoder_detected", "spectral_folding_detected",
        "noise_shaped_16_in_24",
    ):
        if spectrum.get(key):
            return False
    bdi = result.get("bdi") or {}
    forensic = bdi.get("forense_bits") or {}
    if (forensic.get("es_padded_16_en_24")
            or forensic.get("es_24bit_escalado_en_peine")
            or forensic.get("es_16bit_dither_en_24")
            or bdi.get("lsbs") == LsbStatus.EMPTY.value):
        return False
    return True


def _evaluate_album_quorum(valid_tracks) -> AlbumQuorumDecision:
    """Resolve the directory-level coherence decision for a single folder."""
    from collections import Counter

    if len(valid_tracks) < 2:
        return AlbumQuorumDecision(AlbumQuorumKind.NONE)

    quorum_tracks = [r for r in valid_tracks if _is_clean_quorum_track(r)]
    if quorum_tracks:
        reference = Counter(
            (r["meta"]["sample_rate"], r["meta"]["bits_per_sample"])
            for r in quorum_tracks
        ).most_common(1)[0][0]
        quorum_ref = [
            r for r in quorum_tracks
            if (r["meta"]["sample_rate"], r["meta"]["bits_per_sample"]) == reference
        ]
        support_ref = [
            r for r in valid_tracks
            if _is_clean_album_support_track(r, reference)
        ]
        has_strong_anchor = any(
            r.get("veredicto") == Verdict.GENUINE_LOSSLESS.value
            or float(r.get("score") or 0.0) >= 0.75
            or (
                (r.get("md5") or {}).get("estado") == Md5Status.ABSENT.value
                and float(r.get("score") or 0.0) >= 0.70
                and float((r.get("esp") or {}).get("techo_rel_nyquist") or 0.0) >= 0.90
                and not ((r.get("esp") or {}).get("familias_evidencia_lossy") or [])
            )
            for r in quorum_ref
        )
        support_ratio = len(support_ref) / float(len(valid_tracks))
        standard_clean_ok = bool(has_strong_anchor and support_ratio >= 0.75)
        mixed_vhs_ok = False
        if not standard_clean_ok:
            non_green = [
                r for r in valid_tracks
                if r.get("veredicto") not in (
                    Verdict.GENUINE_LOSSLESS.value,
                    Verdict.PROBABLY_LOSSLESS.value,
                )
            ]
            green = [
                r for r in valid_tracks
                if r.get("veredicto") in (
                    Verdict.GENUINE_LOSSLESS.value,
                    Verdict.PROBABLY_LOSSLESS.value,
                )
            ]
            mixed_vhs_ok = bool(
                has_strong_anchor
                and len(valid_tracks) >= 6
                and len(support_ref) >= 3
                and support_ratio >= 0.40
                and non_green
                and all(_is_clean_batch_track(r) for r in green)
                and all(
                    (r["meta"]["sample_rate"], r["meta"]["bits_per_sample"]) == reference
                    and _deserves_album_coherence(r)
                    for r in non_green
                )
            )
        if standard_clean_ok or mixed_vhs_ok:
            candidates = []
            for r in valid_tracks:
                if (r.get("coherencia_album") is True
                        and r.get("veredicto") == Verdict.PROBABLY_LOSSLESS.value):
                    continue
                if r.get("veredicto") not in (
                    Verdict.SUSPICIOUS.value,
                    Verdict.PROBABLE_UPSCALE.value,
                    Verdict.PROBABLY_LOSSLESS.value,
                ):
                    continue
                meta = r.get("meta") or {}
                if (meta.get("sample_rate"), meta.get("bits_per_sample")) != reference:
                    continue
                if _deserves_album_coherence(r):
                    candidates.append(r)
            rescue_candidates = [
                r for r in candidates
                if r.get("veredicto") in (
                    Verdict.SUSPICIOUS.value,
                    Verdict.PROBABLE_UPSCALE.value,
                )
            ]
            max_candidates = max(2, len(valid_tracks) // 4)
            within_cap = (
                True if mixed_vhs_ok
                else len(rescue_candidates) <= max_candidates
            )
            if candidates and within_cap:
                return AlbumQuorumDecision(
                    kind=(
                        AlbumQuorumKind.CLEAN_STANDARD
                        if standard_clean_ok
                        else AlbumQuorumKind.CLEAN_MIXED_VHS
                    ),
                    reference_format=reference,
                    candidates=tuple(candidates),
                )

    if len(valid_tracks) < 3:
        return AlbumQuorumDecision(AlbumQuorumKind.NONE)
    lattice_tracks = [
        r for r in valid_tracks
        if _is_lossy_codec_quorum_track(r)
    ]
    if not lattice_tracks:
        return AlbumQuorumDecision(AlbumQuorumKind.NONE)
    lossy_ref_key = Counter(
        (r["meta"]["sample_rate"], r["meta"]["bits_per_sample"])
        for r in lattice_tracks
    ).most_common(1)[0][0]
    lattice_ref = [
        r for r in lattice_tracks
        if (r["meta"]["sample_rate"], r["meta"]["bits_per_sample"]) == lossy_ref_key
    ]
    if len(lattice_ref) / float(len(valid_tracks)) < 0.75:
        return AlbumQuorumDecision(AlbumQuorumKind.NONE)
    explicit_families = [
        (r.get("esp") or {}).get("mdct_lattice_family")
        for r in lattice_ref
        if (r.get("esp") or {}).get("mdct_lattice_family")
    ]
    neural_count = sum(
        1 for r in lattice_ref
        if (r.get("esp") or {}).get("neural_vocoder_detected")
        or (r.get("esp") or {}).get("spectral_folding_detected")
    )
    dominant_family = (
        Counter(explicit_families).most_common(1)[0][0]
        if explicit_families
        else (
            "neural vocoder/BWE"
            if neural_count * 2 >= len(lattice_ref)
            else "20 kHz lossy/Opus"
        )
    )
    lossy_candidates = []
    for r in valid_tracks:
        if (r.get("coherencia_album") is True
                and r.get("veredicto") == Verdict.PROBABLE_UPSCALE.value):
            continue
        meta = r.get("meta") or {}
        if (meta.get("sample_rate"), meta.get("bits_per_sample")) != lossy_ref_key:
            continue
        if _deserves_album_lossy_coherence(r):
            lossy_candidates.append(r)
    max_lossy = max(2, len(valid_tracks) // 4)
    if not 1 <= len(lossy_candidates) <= max_lossy:
        return AlbumQuorumDecision(AlbumQuorumKind.NONE)
    return AlbumQuorumDecision(
        kind=AlbumQuorumKind.LOSSY_CODEC,
        reference_format=lossy_ref_key,
        candidates=tuple(lossy_candidates),
        dominant_family=dominant_family,
    )


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
    groups = {}
    for result in results:
        if not isinstance(result, dict) or result.get("error"):
            continue
        path = result.get("ruta")
        if not path:
            continue
        folder_key = os.path.normcase(
            os.path.normpath(os.path.abspath(os.path.dirname(os.path.abspath(str(path)))))
        )
        groups.setdefault(folder_key, []).append(result)

    updated = 0
    for tracks in groups.values():
        valid_tracks = [
            r for r in tracks
            if (r.get("meta") or {}).get("sample_rate")
            and r.get("veredicto")
        ]
        decision = _evaluate_album_quorum(valid_tracks)
        if decision.kind in (
            AlbumQuorumKind.CLEAN_STANDARD,
            AlbumQuorumKind.CLEAN_MIXED_VHS,
        ):
            for r in decision.candidates:
                r["veredicto"] = Verdict.PROBABLY_LOSSLESS.value
                r["score"] = max(float(r.get("score") or 0.0), 0.60)
                old_problems = list(r.get("problemas") or [])
                notes = list(r.get("notes") or [])
                for item in old_problems:
                    if item not in notes:
                        notes.append(item)
                if ALBUM_COHERENCE_NOTE not in notes:
                    notes.append(ALBUM_COHERENCE_NOTE)
                r["problemas"] = []
                if isinstance(r.get("esp"), dict) and "finding_codes" in r["esp"]:
                    r["esp"] = {**r["esp"], "finding_codes": []}
                r["notes"] = notes
                evid = list(r.get("evidencias_forenses") or [])
                if ALBUM_COHERENCE_NOTE not in evid:
                    evid.append(ALBUM_COHERENCE_NOTE)
                r["evidencias_forenses"] = evid
                r["coherencia_album"] = True
                r["album_coherence_note"] = ALBUM_COHERENCE_NOTE
                r["album_coherence_action"] = AlbumCoherenceAction.RESCUED_LOSSLESS.value
                updated += 1
        elif decision.kind is AlbumQuorumKind.LOSSY_CODEC:
            dominant_family = decision.dominant_family or "20 kHz lossy/Opus"
            for r in decision.candidates:
                if (r.get("coherencia_album") is True
                        and r.get("veredicto") == Verdict.PROBABLE_UPSCALE.value):
                    continue
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
                r["veredicto"] = Verdict.PROBABLE_UPSCALE.value
                r["score"] = min(float(r.get("score") or 0.0), 0.35)
                r["problemas"] = list(r.get("problemas") or []) + [note]
                if isinstance(r.get("esp"), dict):
                    existing_codes = list(r["esp"].get("finding_codes") or [])
                    r["esp"] = {
                        **r["esp"],
                        "finding_codes": existing_codes
                        + [FindingCode.ALBUM_LOSSY_CONSISTENCY.value],
                    }
                r["evidencias_forenses"] = list(r.get("evidencias_forenses") or []) + [note]
                r["coherencia_album"] = True
                r["album_coherence_note"] = note
                r["album_coherence_action"] = AlbumCoherenceAction.CONVICTED_LOSSY.value
                updated += 1
    return updated

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
    elif spectrum.get("stereo_phase_coupling"):
        evidence.append(f"Inter-channel stereo coupling (MSC {spectrum.get('msc_lo', 0.0):.2f} -> "
                          f"{spectrum.get('msc_hi', 0.0):.2f}, IPD lock "
                          f"{spectrum.get('ipd_phase_lock_hf', 0.0)*100:.0f}%)")
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
    if spectrum.get("neural_vocoder_detected"):
        evidence.append(
            f"Neural vocoder synthesis comb ({spectrum.get('vocoder_comb_spacing_hz', 0.0):.1f} Hz, "
            f"Z = {spectrum.get('vocoder_hf_comb_z', 0.0):.1f} sigma, "
            f"phase coherence +{spectrum.get('vocoder_phase_coh_diff', 0.0):.2f})"
        )
    if spectrum.get("spectral_folding_detected"):
        evidence.append(
            f"Time-domain spectral folding BWE around "
            f"{spectrum.get('spectral_folding_pivot_hz', 0.0)/1000.0:.2f} kHz "
            f"(r = {spectrum.get('spectral_folding_corr', 0.0):.2f}, "
            f"Z = {spectrum.get('spectral_folding_z', 0.0):.1f} sigma)"
        )
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
    if spectrum.get("noise_shaped_16_in_24"):
        floor_q = spectrum.get("quiet_hf_floor_dbfs")
        floor_txt = f"{floor_q:.1f} dBFS" if isinstance(floor_q, (int, float)) else "elevated"
        evidence.append(
            f"Noise-shaped 16-bit dither floor in {bits_container}-bit "
            f"({floor_txt}, +{spectrum.get('quiet_hf_slope_db', 0.0):.1f} dB rise)"
        )
    if forensic.get("es_flujo_dop"):
        evidence.append("DoP 1.0 framing markers (0x05/0xFA) detected in top byte")
    fams = spectrum.get("familias_evidencia_lossy") or []
    if len(fams) >= 2:
        evidence.append(f"Multi-family corroboration ({len(fams)} families: "
                          f"{', '.join(fams)})")
    if (result.get("md5") or {}).get("estado") == Md5Status.MATCH.value:
        evidence.append("STREAMINFO MD5 verified (match)")
    if result.get("coherencia_album"):
        evidence.append(result.get("album_coherence_note") or ALBUM_COHERENCE_NOTE)
    return evidence
