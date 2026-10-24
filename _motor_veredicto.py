"""Verdict tree, scoring and album-level coherence.

Extracted from the motor facade in Phase 4.
"""
import os

LIMITE_CD_HZ = 22050

def calcular_score(meta, esp, bdi):
    votos   = 0.0
    max_v   = 0.0
    resta   = 0.0
    bloquea = False
    problemas = []

    sr      = meta["sample_rate"]
    bits    = meta["bits_per_sample"]
    nyquist = sr // 2
    es_hires = sr >= 88200

    def penaliza(texto, cuanto, bloquea_maximo=True):
        nonlocal resta, bloquea
        problemas.append(texto)
        resta += cuanto
        bloquea = bloquea or bloquea_maximo

    if not esp.get("error"):
        techo     = esp["techo_hz"]
        clase     = esp.get("clase_corte", "no_cutoff")
        rel_corte = esp.get("distancia_nyquist")
        rel_techo = esp.get("techo_rel_nyquist")
        if rel_techo is None:
            rel_techo = techo / nyquist if nyquist else 0.0
        nyq_txt = f"{nyquist/1000:.1f} kHz"

        # 1. Content above the CD band: it can only be demanded of a file that
        #    claims to be hi-res. At 44.1/48 kHz it is physically impossible.
        if es_hires:
            max_v += 3
            if esp.get("rolloff_analogico_genuino") or esp.get("es_transferencia_dsd"):
                votos += 3
                problemas.append("hi-res morphology consistent with an analogue/DSD "
                                 "master (smooth roll-off and continuous ultrasonic floor)")
            elif techo >= LIMITE_CD_HZ:
                votos += 3
            elif techo >= 18000:
                votos += 1.5
                problemas.append(f"partial hi-res content: ceiling of {techo/1000:.1f} kHz "
                                 f"({rel_techo*100:.0f}% of Nyquist of {nyq_txt})")
            else:
                penaliza(f"claims {sr/1000:.1f} kHz but the ceiling is {techo/1000:.1f} kHz, "
                         f"below the CD band: upscaled from 44.1/48 kHz", 0.40)

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
        forense_bdi = bdi.get("forense_bits") or {}
        # Fail-closed on 24-bit: when the bit-depth audit applies, only a fully
        # active LSB lattice earns the acoustic exemption. "partial",
        # "indeterminate" and None all mean "not verified", not "clean".
        bits_ok_acustico = (
            (not bdi.get("aplica"))
            or (
                bdi.get("lsbs") == "active"
                and not forense_bdi.get("es_padded_16_en_24")
                and not forense_bdi.get("es_24bit_escalado_en_peine")
            )
        )
        sin_firmas_lossy = (
            clase == "no_cutoff"
            and not esp.get("sospecha_joint_stereo")
            and not esp.get("sospecha_corte_opus_20k")
            and not esp.get("clipping_reconstruccion_lossy")
            and not esp.get("islas_mdct_sfb21")
            and not esp.get("sospecha_excitador_armonico")
            and (esp.get("iqr_corte_hz") or 0.0) <= 550.0
            and (esp.get("ratio_huecos_mdct") or 0.0) <= 0.10
        )
        exencion_acustica = bool(
            esp.get("decaimiento_acustico_suave")
            and bits_ok_acustico
            and sin_firmas_lossy
        )
        max_v += 3
        if sr > 48000:
            if techo >= LIMITE_CD_HZ:
                votos += 3
            elif esp.get("rolloff_analogico_genuino") or esp.get("es_transferencia_dsd"):
                votos += 3
                problemas.append("hi-res ceiling explained by an analogue/DSD master: "
                                 "the declared band is backed by the morphology")
            else:
                penaliza(f"spectral ceiling {techo/1000:.1f} kHz: there is no content above "
                         f"the CD limit ({LIMITE_CD_HZ/1000:.2f} kHz), so "
                         f"the band of {sr/1000:.1f} kHz is not backed by content", 0.40)
        elif rel_techo >= 0.88:
            votos += 3
        elif rel_techo >= 0.70:
            votos += 1.5
            if exencion_acustica:
                problemas.append(f"spectral ceiling {techo/1000:.1f} kHz "
                                 f"({rel_techo*100:.0f}% of Nyquist of {nyq_txt}): "
                                 f"low spectral ceiling consistent with solo "
                                 f"acoustic/low-centroid instrumentation")
            else:
                problemas.append(f"spectral ceiling {techo/1000:.1f} kHz "
                                 f"({rel_techo*100:.0f}% of Nyquist of {nyq_txt}): "
                                 f"reduced high band")
        elif exencion_acustica:
            votos += 1.5
            problemas.append(f"spectral ceiling {techo/1000:.1f} kHz "
                             f"({rel_techo*100:.0f}% of Nyquist of {nyq_txt}): "
                             f"low spectral ceiling consistent with solo "
                             f"acoustic/low-centroid instrumentation")
        else:
            penaliza(f"spectral ceiling {techo/1000:.1f} kHz ({rel_techo*100:.0f}% of Nyquist "
                     f"of {nyq_txt}): the high band is destroyed", 0.40)

        # 3b. Vector 3: morphology that no legitimate master produces.
        if esp.get("muro_brickwall_nyquist"):
            penaliza(f"Hi-Res container with a brickwall at the CD/DAT Nyquist "
                     f"({esp.get('gradiente_nyquist_db_khz', 0.0):.1f} dB/kHz, "
                     f"{esp.get('salto_ultrasonico_db', 0.0):.1f} dB into the digital void): "
                     f"upscaled from a 44.1/48 kHz source", 0.40)
        if esp.get("espejo_aliasing_detectado"):
            penaliza(f"ultrasonic band mirrors the content below 22.05 kHz "
                     f"(r = {esp.get('correlacion_espejo_22k', 0.0):.2f}): "
                     f"unfiltered-interpolator upscale (spectral aliasing)", 0.40)
        if esp.get("es_transferencia_dsd"):
            problemas.append("DSD/SACD noise-shaping profile detected in 24-40 kHz "
                             "(legitimate hi-res transfer)")

        # 3. Cut-off: it is only flagged if it is clearly below the Nyquist.
        # A prior cut plus a flat dither floor is never an analogue roll-off:
        # `rolloff_analogico_genuino` is false in that case, so a Hi-Res
        # far_cutoff cannot bypass this penalty.
        max_v += 3
        if clase == "far_cutoff":
            if esp.get("es_transferencia_dsd") or esp.get("rolloff_analogico_genuino"):
                problemas.append(f"band edge at {esp['frecuencia_corte']/1000:.1f} kHz "
                                 f"({rel_corte*100:.0f}% of Nyquist): the musical band ends "
                                 f"there, consistent with an analogue/DSD master")
            else:
                penaliza(f"artificial cut-off at {esp['frecuencia_corte']/1000:.1f} kHz "
                         f"({rel_corte*100:.0f}% of Nyquist of {nyq_txt}): lossy codec signature",
                         0.40)
        elif clase == "near_nyquist_cutoff":
            # It is neither flagged nor rewarded: the anti-alias filter of a
            # legitimate master lives here too. It is reported as inconclusive.
            problemas.append(f"band cut at {esp['frecuencia_corte']/1000:.1f} kHz "
                             f"({rel_corte*100:.0f}% of Nyquist): consistent with the anti-alias "
                             f"filter of a legitimate master AND with a high-rate codec "
                             f"— inconclusive")
        else:
            votos += 3

        # 4. High-band metrics (not applicable if the file has no high band).
        if esp.get("ratio_aplica"):
            max_v += 2
            r = esp["ratio"]
            if r > 0.05:   votos += 2
            elif r > 0.01: votos += 1
            else:          problemas.append(f"high/mid ratio very low ({r:.5f})")

            max_v += 1
            if esp.get("var_alta_db", 0) > 3:
                votos += 1

        sep = esp.get("separacion_stereo")
        if sep is not None and not esp.get("es_dual_mono"):
            max_v += 1
            if sep > 0.1:
                votos += 1

        # 4b. Vector 1 (Mid/Side) and Vector 2 (cut-off jitter + MDCT holes).
        # Only ADD penalties when the signature is present: existing keys, votes
        # and verdicts of files without it stay exactly as they were.
        if esp.get("es_dual_mono"):
            problemas.append("dual-mono (left and right carry the same programme): "
                             "stereo separation is not informative")
        if esp.get("sospecha_joint_stereo"):
            penaliza(f"the Side channel (L-R) collapses at high frequency "
                     f"(Mid-Side drop {esp.get('caida_side_agudos_db', 0.0):.1f} dB, "
                     f"cut-off delta {esp.get('delta_corte_ms_hz', 0.0):.0f} Hz): "
                     f"joint-stereo lossy codec signature", 0.30)
        iqr = esp.get("iqr_corte_hz") or 0.0
        # The jitter is already part of the Opus evidence: do not penalise the
        # same symptom twice when the 20.00 kHz cliff is already corroborated.
        opus_corroborado = bool(esp.get("sospecha_corte_opus_20k") and
                                not (esp.get("es_transferencia_dsd") or
                                     esp.get("rolloff_analogico_genuino")) and
                                (esp.get("sospecha_joint_stereo") or iqr > 550.0 or
                                 (esp.get("ratio_huecos_mdct") or 0.0) > 0.10))
        if iqr > 550.0 and not opus_corroborado:
            penaliza(f"the instantaneous cut-off jitters (IQR {iqr:.0f} Hz): "
                     f"frame-adaptive codec signature", 0.20, bloquea_maximo=False)
        huecos = esp.get("ratio_huecos_mdct") or 0.0
        if huecos > 0.10:
            penaliza(f"MDCT spectral holes in {huecos*100:.0f}% of the high-energy "
                     f"frames: lossy codec signature", 0.25, bloquea_maximo=False)

        # Vector 7: sfb21 islands and harmonic-exciter / SBR signature. Both are
        # positive proof of a codec: the top scalefactor band switches on and off
        # frame by frame, or the high band is rebuilt from the 8-14 kHz envelope
        # and leaves a notch where the original band was cut.
        if esp.get("islas_mdct_sfb21"):
            penaliza(f"intermittent MDCT spectral islands in 16-20.5 kHz "
                     f"(P90-P10 gap {esp.get('brecha_p90_p10_agudos_db', 0.0):.1f} dB, "
                     f"{(esp.get('ratio_huecos_sfb21') or esp.get('ratio_apagado_sfb21') or 0.0)*100:.0f}% "
                     f"of frames zeroed): lowpass-disabled lossy codec signature",
                     0.25, bloquea_maximo=True)
        if esp.get("sospecha_excitador_armonico"):
            penaliza(f"synthetic high-frequency harmonic exciter / SBR signature "
                     f"(envelope correlation r = {esp.get('corr_env_excitador', 0.0):.2f}, "
                     f"{esp.get('muesca_excitador_db', 0.0):.1f} dB notch): "
                     f"adversarial bandwidth extension",
                     0.30, bloquea_maximo=True)

        # Vector 5: libopus fullband cliff at 20.00 kHz. In isolation it is
        # indistinguishable from a mastering anti-alias low-pass, so it is scored
        # only together with another codec signature; on its own it stays a fact.
        if (esp.get("sospecha_corte_opus_20k")
                and not (esp.get("es_transferencia_dsd")
                         or esp.get("rolloff_analogico_genuino"))):
            corroborado = bool(
                esp.get("sospecha_joint_stereo")
                or (esp.get("iqr_corte_hz") or 0.0) > 550.0
                or (esp.get("ratio_huecos_mdct") or 0.0) > 0.10
            )
            if corroborado:
                penaliza(f"20.00 kHz cliff of "
                         f"{esp.get('caida_opus_20k_db', 0.0):.0f} dB with other "
                         f"codec evidence: libopus fullband transcode signature", 0.20,
                          bloquea_maximo=False)
            else:
                problemas.append("a 20.00 kHz cliff is consistent with libopus fullband "
                                 "and also with a mastering low-pass: inconclusive "
                                 "without other codec evidence")

        # Vector 6: nature of the clipping. Only Gibbs overshoot is evidence of
        # lossy reconstruction; a mastering limiter is reported, never penalised.
        if esp.get("clipping_reconstruccion_lossy"):
            penaliza(f"clipping made of isolated 1-2 sample events "
                     f"({esp.get('clipping_fraccion_cortos', 0.0)*100:.0f}% of them) "
                     f"with a band limit below 20.5 kHz: Gibbs overshoot from lossy "
                     f"reconstruction", 0.25, bloquea_maximo=False)
        if esp.get("clipping_mastering_limitador"):
            problemas.append("clipping runs of 4+ consecutive samples with content up to "
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
            corte_txt = (f"cut at {esp['frecuencia_corte']/1000:.1f} kHz"
                         if esp.get("frecuencia_corte") else
                         f"limited to {rel_techo*100:.0f}% of Nyquist "
                         f"({techo/1000:.1f} kHz)")
            forense = bdi.get("forense_bits") or {}
            if clase == "far_cutoff" or rel_techo < 0.88:
                penaliza(f"declares {bits} bits at {sr/1000:.1f} kHz but its high band is "
                         f"{corte_txt}: the declared resolution is not backed by the "
                         f"content (consistent with a repackaged high-rate transcode)",
                         0.25, bloquea_maximo=True)
            elif 0.88 <= rel_techo < 0.95:
                bits_limpios = (
                    bdi.get("lsbs") == "active"
                    and not forense.get("es_padded_16_en_24")
                    and not forense.get("es_24bit_escalado_en_peine")
                )
                sin_codec_ni_brickwall = (
                    clase == "no_cutoff"
                    and not esp.get("sospecha_joint_stereo")
                    and not esp.get("sospecha_corte_opus_20k")
                    and (esp.get("iqr_corte_hz") or 0.0) <= 550.0
                    and (esp.get("ratio_huecos_mdct") or 0.0) <= 0.10
                    and (esp.get("gradiente_borde_db_khz") or 0.0) < 35.0
                )
                if not (bits_limpios and sin_codec_ni_brickwall):
                    penaliza(f"declares {bits} bits at {sr/1000:.1f} kHz but its high band is "
                             f"{corte_txt}: the declared resolution is not backed by the "
                             f"content (consistent with a repackaged high-rate transcode)",
                             0.25, bloquea_maximo=True)
                else:
                    problemas.append("Vector 5: 24-bit master with studio anti-aliasing transition band "
                                     f"({rel_techo*100:.0f}% of Nyquist, clean LSB lattice and no lossy signature)")

    # 6. LSB utilisation: it ONLY penalises (Block 4).
    if bdi.get("aplica"):
        lsbs = bdi.get("lsbs")
        if lsbs == "empty":
            penaliza(f"the LSBs are empty ({bdi.get('fraccion_16bit_grid','?')}% of samples "
                     f"on the 16-bit grid): effective resolution of "
                     f"{bdi.get('resolucion_efectiva_bits','?')} bits in a "
                     f"{bits} bits — bit-depth upscale", 0.40)
        elif lsbs == "partial":
            penaliza(f"LSBs partially inactive ({bdi.get('fraccion_16bit_grid','?')}% on the "
                     f"16-bit grid): upscale with later processing", 0.15,
                     bloquea_maximo=False)
        elif lsbs == "indeterminate" and bdi.get("error"):
            problemas.append(f"could not evaluate the LSB utilization: {bdi['error']}")

        # Vector 4: native-integer audit (zero padding and quantisation comb).
        forense = bdi.get("forense_bits") or {}
        if forense.get("es_padded_16_en_24"):
            problemas.append(f"Vector 4: every sample has "
                             f"{forense.get('trailing_zeros_lsb', 0)} trailing zero bits "
                             f"(16-bit zero-padded into 24 bits)")
            if lsbs != "empty":
                penaliza(f"the 8 low bits are always zero in the whole file: "
                         f"16-bit content zero-padded into a {bits}-bit container", 0.40)
        if forense.get("es_24bit_escalado_en_peine"):
            paso = forense.get("paso_reticula_mediano")
            cajones = forense.get("lsb8_cajones_ocupados")
            entropia = forense.get("lsb8_entropia_bits")
            # A loud compressed fake caught by the global lattice still occupies
            # every bin with ~8 bits of entropy: quoting them would contradict the
            # comb itself, so the lattice step is the evidence in that case.
            if isinstance(cajones, int) and 0 < cajones < 256:
                detalle_peine = (f"lattice step {paso}, entropy {entropia} bits, "
                                 f"{cajones}/256 bins occupied")
            else:
                detalle_peine = f"global lattice step {paso} integers"
            penaliza(f"24-bit container with a quantisation comb in the 8 LSBs "
                     f"({detalle_peine}): a 16-bit source scaled into 24 bits", 0.40)

    # MD5: it is NO longer scored. Its check is reported as a fact of its own in
    # resultado["md5"] (verificar_md5), and an MD5 that does not match is a hard error.

    if max_v == 0:
        return 0.0, "indeterminate", problemas

    score = max(0.0, votos / max_v - resta)

    if bloquea:
        # Block 2: a confirmed defect does not coexist with a "genuine" verdict.
        veredicto = "SUSPICIOUS" if score >= 0.50 else "PROBABLE UPSCALE"
    elif score >= 0.80:   veredicto = "GENUINE LOSSLESS"
    elif score >= 0.60:   veredicto = "PROBABLY LOSSLESS"
    elif score >= 0.40:   veredicto = "SUSPICIOUS"
    else:                 veredicto = "PROBABLE UPSCALE"
    return score, veredicto, problemas

NOTA_COHERENCIA_ALBUM = ("Acoustic/low-HF track validated by album-level "
                        "master consistency")

def _merece_coherencia_album(resultado):
    """True when the only evidence against the track is a low spectral ceiling."""
    esp = resultado.get("esp") or {}
    bdi = resultado.get("bdi") or {}
    if esp.get("error"):
        return False
    if (resultado.get("md5") or {}).get("estado") != "match":
        return False
    if esp.get("corte_detectado"):
        return False                       # a step is not "only a low ceiling"
    if (esp.get("techo_rel_nyquist") or 1.0) >= 0.88:
        return False
    for clave in ("muro_brickwall_nyquist", "espejo_aliasing_detectado",
                  "sospecha_joint_stereo", "sospecha_corte_opus_20k",
                  "clipping_reconstruccion_lossy", "islas_mdct_sfb21",
                  "sospecha_excitador_armonico"):
        if esp.get(clave):
            return False
    if (esp.get("ratio_huecos_mdct") or 0.0) > 0.10:
        return False
    forense = bdi.get("forense_bits") or {}
    if (forense.get("es_padded_16_en_24")
            or forense.get("es_24bit_escalado_en_peine")
            or bdi.get("lsbs") == "empty"):
        return False
    permitidos = ("spectral ceiling", "partial hi-res content",
                  "reduced high band", "high band is destroyed",
                  "high/mid ratio")
    for problema in resultado.get("problemas") or []:
        if not any(texto in problema.lower() for texto in permitidos):
            return False
    return True

def aplicar_coherencia_album(resultados):
    """Vector 6: rescue 1-2 acoustic/low-HF tracks of a consistent album.

    A candidate is any track accepted by `_merece_coherencia_album` (only the
    spectral ceiling is against it), whatever its current label: SUSPICIOUS or
    PROBABLE UPSCALE, and also PROBABLY LOSSLESS when it was already excused on
    its own by the Phase 8D acoustic exemption (`decaimiento_acustico_suave`
    with clean bits and no lossy signature). When at least 80% of the tracks in a
    directory are GENUINE LOSSLESS with the same sample rate and bit depth, the
    album context is stronger evidence than the isolated spectrum, so those
    candidates get the album validation (`coherencia_album = True` and
    NOTA_COHERENCIA_ALBUM) and are at least PROBABLY LOSSLESS. That mark exists
    only in this batch: it is never written into the per-file cache, so
    analysing the track alone does not inherit it. Returns how many tracks were
    raised or validated. Modifies the list in place."""
    from collections import Counter

    grupos = {}
    for resultado in resultados:
        if not isinstance(resultado, dict) or resultado.get("error"):
            continue
        ruta = resultado.get("ruta")
        if not ruta:
            continue
        grupos.setdefault(os.path.dirname(os.path.abspath(ruta)), []).append(resultado)

    elevados = 0
    for pistas in grupos.values():
        validas = [r for r in pistas
                   if (r.get("meta") or {}).get("sample_rate")
                   and r.get("veredicto")]
        if len(validas) < 2:
            continue
        genuinas = [r for r in validas if r.get("veredicto") == "GENUINE LOSSLESS"]
        if not genuinas:
            continue
        referencia = Counter(
            (r["meta"]["sample_rate"], r["meta"]["bits_per_sample"])
            for r in genuinas).most_common(1)[0][0]
        genuinas_ref = [r for r in genuinas
                        if (r["meta"]["sample_rate"],
                            r["meta"]["bits_per_sample"]) == referencia]
        if len(genuinas_ref) / float(len(validas)) < 0.80:
            continue

        candidatos = []
        for r in validas:
            if r.get("veredicto") not in ("SUSPICIOUS", "PROBABLE UPSCALE",
                                          "PROBABLY LOSSLESS"):
                continue
            meta = r.get("meta") or {}
            if (meta.get("sample_rate"), meta.get("bits_per_sample")) != referencia:
                continue
            if _merece_coherencia_album(r):
                candidatos.append(r)
        if not 1 <= len(candidatos) <= 2:
            continue

        for r in candidatos:
            r["veredicto"] = "PROBABLY LOSSLESS"
            r["score"] = max(float(r.get("score") or 0.0), 0.60)
            r["problemas"] = list(r.get("problemas") or []) + [NOTA_COHERENCIA_ALBUM]
            r["evidencias_forenses"] = list(r.get("evidencias_forenses") or []) + [NOTA_COHERENCIA_ALBUM]
            r["coherencia_album"] = True
            elevados += 1
    return elevados

def evidencias_forenses(resultado):
    """Short English technical evidence lines shown next to the verdict.

    It is additive to `problemas`: the GUI/PDF can explain *why* the verdict was
    reached without re-deriving the detectors from the raw metrics."""
    esp = resultado.get("esp") or {}
    bdi = resultado.get("bdi") or {}
    evidencias = []
    if esp.get("error"):
        return evidencias

    techo = esp.get("techo_hz")
    rel = esp.get("techo_rel_nyquist")
    if isinstance(techo, (int, float)) and techo > 0:
        evidencias.append(f"Spectral ceiling {techo/1000:.1f} kHz"
                          + (f" ({float(rel)*100:.0f}% of Nyquist)"
                             if isinstance(rel, (int, float)) else ""))
    if esp.get("muro_brickwall_nyquist"):
        evidencias.append(f"Hi-Res brickwall at the CD/DAT Nyquist "
                          f"({esp.get('gradiente_nyquist_db_khz', 0.0):.1f} dB/kHz)")
    if esp.get("espejo_aliasing_detectado"):
        evidencias.append(f"Spectral mirror around 22.05 kHz "
                          f"(r = {esp.get('correlacion_espejo_22k', 0.0):.2f})")
    if esp.get("es_transferencia_dsd"):
        evidencias.append("DSD/SACD noise-shaping profile in 24-40 kHz")
    elif esp.get("rolloff_analogico_genuino"):
        evidencias.append("Analogue/acoustic roll-off with a continuous ultrasonic floor")
    if esp.get("sospecha_joint_stereo"):
        evidencias.append(f"Side channel (L-R) collapse: "
                          f"{esp.get('caida_side_agudos_db', 0.0):.1f} dB, "
                          f"cut-off delta {esp.get('delta_corte_ms_hz', 0.0):.0f} Hz")
    if esp.get("es_dual_mono"):
        evidencias.append("Dual-mono: left and right carry the same programme")
    if (esp.get("iqr_corte_hz") or 0.0) > 550.0:
        evidencias.append(f"Instantaneous cut-off jitter: "
                          f"IQR {esp.get('iqr_corte_hz', 0.0):.0f} Hz")
    if (esp.get("ratio_huecos_mdct") or 0.0) > 0.10:
        evidencias.append(f"MDCT spectral holes in "
                          f"{(esp.get('ratio_huecos_mdct') or 0.0)*100:.0f}% of the frames")
    if esp.get("sospecha_corte_opus_20k"):
        evidencias.append(f"20.00 kHz cliff ({esp.get('caida_opus_20k_db', 0.0):.0f} dB): "
                          f"libopus fullband signature")
    if esp.get("clipping_reconstruccion_lossy"):
        evidencias.append("Clipping: isolated 1-2 sample Gibbs overshoot")
    if esp.get("clipping_mastering_limitador"):
        evidencias.append("Clipping: 4+ sample mastering limiter plateaus")
    if esp.get("islas_mdct_sfb21"):
        evidencias.append(f"Intermittent sfb21 MDCT islands: "
                          f"{esp.get('brecha_p90_p10_agudos_db', 0.0):.1f} dB P90-P10 gap")
    if esp.get("sospecha_excitador_armonico"):
        evidencias.append(f"Harmonic exciter / SBR signature: "
                          f"r = {esp.get('corr_env_excitador', 0.0):.2f}, "
                          f"{esp.get('muesca_excitador_db', 0.0):.1f} dB notch")
    forense = bdi.get("forense_bits") or {}
    if forense.get("es_padded_16_en_24"):
        evidencias.append("16-bit content zero-padded into a 24-bit container")
    if forense.get("es_24bit_escalado_en_peine"):
        paso = forense.get("paso_reticula_mediano")
        cajones = forense.get("lsb8_cajones_ocupados")
        entropia = forense.get("lsb8_entropia_bits")
        if isinstance(cajones, int) and 0 < cajones < 256:
            evidencias.append(f"24-bit quantisation comb: lattice step {paso}, "
                              f"entropy {entropia} bits, {cajones}/256 bins")
        else:
            evidencias.append(f"24-bit quantisation comb: global lattice step "
                              f"{paso} integers")
    if (resultado.get("md5") or {}).get("estado") == "match":
        evidencias.append("STREAMINFO MD5 verified (match)")
    if resultado.get("coherencia_album"):
        evidencias.append(NOTA_COHERENCIA_ALBUM)
    return evidencias
