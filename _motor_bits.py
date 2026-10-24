"""Integrity, native-bit audit, clipping and dynamic range.

Extracted from the motor facade in Phase 4. Nothing here imports the spectral
or cache modules: this layer only needs NumPy, soundfile/mutagen and stdlib.
"""
import hashlib
import os

import numpy as np

FRAMES_POR_BLOQUE = 1 << 18            # 262 144 frames (~6 s at 44.1 kHz) per read

MAX_MUESTRAS_BITS = 1 << 20            # sample for the LSB test

# Vector 4 (high-amplitude slices). A hyper-compressed master has no sample in
# the [-90, -48] dBFS window, so the comb is looked for around -18 and -12 dBFS
# instead: 1 050 000 / 8 388 608 is -18.05 dBFS and 2 100 000 is -12.03 dBFS.
# Each slice is one 16-bit grid step wide (32 768 integers) because that is the
# period of the comb left behind by a 16-bit source scaled into 24 bits.
REBANADA_1_BASE = 1050000
REBANADA_2_BASE = 2100000
REBANADA_ANCHO  = 32768

def _normalizar_ruta_fs(ruta):
    r"""Returns `ruta` in a form the Windows API can open even past MAX_PATH.

    On Windows the classic file APIs stop at 260 characters, so a classical album
    with long folder and track names can raise FileNotFoundError while the file is
    perfectly readable. The extended-length prefix (\\?\\) lifts that limit; UNC
    paths use \\?\UNC\ instead. Only long ABSOLUTE paths are rewritten (the prefix
    requires an absolute path), and on other systems the path is returned as is.
    """
    if os.name != "nt" or not isinstance(ruta, str) or not ruta:
        return ruta
    if ruta.startswith("\\\\?\\"):
        return ruta
    # ntpath.isabs() returns False for UNC paths without a drive (\\server\share),
    # so the two leading-separator forms are accepted explicitly.
    es_absoluta = os.path.isabs(ruta) or ruta.startswith(("\\\\", "//"))
    if not es_absoluta or len(ruta) <= 240:
        return ruta
    ruta = ruta.replace("/", "\\")
    if ruta.startswith("\\\\"):
        return "\\\\?\\UNC\\" + ruta[2:]
    return "\\\\?\\" + ruta

_ANCHO_POR_BITS = {8: 1, 16: 2, 24: 3, 32: 4}

_DTYPE_POR_ANCHO = {1: "<i1", 2: "<i2", 4: "<i4"}

def _pcm_bytes(bloque, bits_per_sample, ancho):
    """Little-endian interleaved PCM of a block, exactly as the format hashes it.

    float64 -> exact integer: the soundfile conversion is m / 2**(bits-1)."""
    escala = float(1 << (bits_per_sample - 1))
    dt = _DTYPE_POR_ANCHO.get(ancho)
    if dt is not None:
        return np.rint(bloque * escala).astype(dt).tobytes()
    # 24 bits (width 3): store as <i4 and keep the low 3 bytes, which is the same
    # little-endian layout the old <i8 path produced, at half the memory.
    q = np.rint(bloque * escala).astype("<i4")
    return q.view(np.uint8).reshape(-1, 4)[:, :ancho].tobytes()

def _md5_pcm(data, bits_per_sample, frames_por_bloque=1 << 20):
    """MD5 of the decoded PCM of a whole array. None if it is not verifiable."""
    ancho = _ANCHO_POR_BITS.get(bits_per_sample)
    if ancho is None:
        return None
    md5 = hashlib.md5()
    for i in range(0, len(data), frames_por_bloque):
        md5.update(_pcm_bytes(data[i : i + frames_por_bloque], bits_per_sample, ancho))
    return md5.hexdigest()

def _comparar_md5(calculado, meta):
    """Compares an already computed MD5 with the one recorded in the STREAMINFO."""
    almacenado = meta.get("md5")
    if isinstance(almacenado, (bytes, bytearray)):
        almacenado = int.from_bytes(almacenado, "big")
    if not almacenado:
        return {"estado": "absent",
                "detalle": "STREAMINFO has no MD5 recorded (encoder built with --no-md5, or altered metadata)"}
    if calculado is None:
        return {"estado": "unverifiable",
                "detalle": f"bit depth {meta.get('bits_per_sample')} bits: not verifiable",
                "almacenado": f"{int(almacenado):032x}"}
    coincide = int(calculado, 16) == int(almacenado)
    return {
        "estado":     "match" if coincide else "mismatch",
        "calculado":  calculado,
        "almacenado": f"{int(almacenado):032x}",
    }

def verificar_md5(data, meta):
    """Compares the recomputed MD5 of an array with the STREAMINFO one. A fact, not a vote."""
    return _comparar_md5(_md5_pcm(data, meta.get("bits_per_sample")), meta)

def analizar_bit_depth(data, meta, incluir_forense=True):
    """LSB utilisation of the container (Block 4).

    This analysis CAN ONLY REFUTE: empty low bits, or low bits aligned to the
    16-bit grid, betray a container deeper than its content. It never proves
    authenticity — a lossy transcode stored in 24 bits also fills the LSBs, and a
    renormalisation breaks the alignment. That is why the result is reported as a
    fact and can only penalise in the scoring.

    `forense_bits` (Vector 4) audits the native integers: zero padding and the
    quantisation comb of a 16-bit source scaled into 24 bits. The streaming path
    disables it here and attaches its own whole-file accumulator instead.
    """
    bits = meta["bits_per_sample"]
    if bits <= 16:
        return {"aplica": False}
    forense = None
    try:
        if incluir_forense:
            forense = _analizar_forense_bits(data, meta)

        # All channels, not just the left one: a silent channel or a file with
        # misaligned channels biased the previous test.
        muestras = np.ascontiguousarray(data).reshape(-1)
        activas  = muestras[np.abs(muestras) > 0.001]
        if activas.size < 1000:
            salida = {"aplica": True, "lsbs": "indeterminate",
                      "conclusion": "indeterminate: too little signal to measure LSB utilization"}
            if forense is not None:
                salida["forense_bits"] = forense
            return salida

        # 1) Are the samples on the 16-bit grid? (the classic upscale test)
        escalado = activas * 32768.0
        fraccion = float(np.mean(np.abs(escalado - np.round(escalado)) < 0.05))

        # 2) Effective resolution: declared bits minus the mode of trailing zeros
        #    of the integer samples (16 bits inside 24 -> 8 zeros -> 16 bits).
        q = np.round(activas * float(1 << (bits - 1))).astype(np.int64)
        q = q[q != 0]
        if q.size:
            pot     = np.abs(q) & -np.abs(q)                  # 2^(trailing zeros)
            ceros   = np.log2(pot).round().astype(np.int64)
            moda    = int(np.bincount(ceros).argmax())
            efectiva = max(1, bits - moda)
        else:
            efectiva = bits

        pct = f"{fraccion * 100:.1f}%"
        if fraccion > 0.97:
            lsbs = "empty"
            conclusion = (f"the LSBs are empty ({pct} of samples on the 16-bit grid): "
                          f"effective resolution of {efectiva} bits in a {bits}-bit container "
                          f"— bit-depth upscale")
        elif fraccion > 0.80:
            lsbs = "partial"
            conclusion = (f"LSBs partially inactive ({pct} on the 16-bit grid), "
                          f"effective resolution ~{efectiva} bits — upscale with later processing")
        else:
            lsbs = "active"
            conclusion = (f"LSBs active, effective resolution ~{efectiva} of {bits} bits: "
                          f"rules out grid upscaling, but does NOT prove the origin")
        salida = {
            "aplica":                   True,
            "fraccion_16bit_grid":      round(fraccion * 100, 1),
            "resolucion_efectiva_bits": int(efectiva),
            "lsbs":                     lsbs,
            "conclusion":               conclusion,
        }
        if forense is not None:
            salida["forense_bits"] = forense
        return salida
    except Exception as e:
        salida = {"aplica": True, "lsbs": "indeterminate", "error": str(e)}
        if forense is not None:
            salida["forense_bits"] = forense
        return salida

def _rachas(en, cuenta_previa=0):
    """Lengths of the runs of True, vectorised and without losing the ones that
    cross the boundary between blocks. Returns (rachas_cerradas, cuenta_abierta_final)."""
    if not en.any():
        return ([cuenta_previa] if cuenta_previa else []), 0
    flags   = en.astype(np.int8)
    bordes  = np.diff(np.concatenate(([0], flags, [0])))
    inicios = np.flatnonzero(bordes == 1)
    largos  = (np.flatnonzero(bordes == -1) - inicios).tolist()
    if inicios[0] == 0 and cuenta_previa:
        largos[0] += cuenta_previa          # continues the run of the previous block
    if flags[-1]:
        return largos[:-1], largos[-1]      # the last one is still open
    return largos, 0

def detectar_clipping(data):
    """Real clipping = 3+ CONSECUTIVE samples at the top (>= 0.9999). A clipping
    run is counted once; isolated inter-sample peaks are not clipping."""
    try:
        amp     = np.max(np.abs(data), axis=1) if data.ndim > 1 else np.abs(data)
        rachas, abierta = _rachas(amp >= 0.9999)
        if abierta:
            rachas.append(abierta)
        runs = sum(1 for largo in rachas if largo >= 3)
        return {"runs_clip": runs, "hay_clipping": runs > 0}
    except (ValueError, TypeError) as e:
        print(f"[clipping] {e}")
        return None

def _clasificar_rachas(longitudes, techo_hz=None, frecuencia_corte=None, nyquist=None):
    """Vector 6 classification from the lengths of the saturation runs.

    * Lossy reconstruction (Gibbs overshoot): most events are 1-2 isolated
      samples and the spectral cut-off sits below 20.5 kHz.
    * Mastering limiter: runs of 4+ consecutive samples on average while the
      spectrum still reaches Nyquist. It is an observation, never a defect."""
    largos = [int(l) for l in longitudes if int(l) > 0]
    eventos = len(largos)
    if not eventos:
        return {
            "clipping_eventos": 0,
            "clipping_fraccion_cortos": 0.0,
            "clipping_longitud_media": 0.0,
            "clipping_racha_maxima": 0,
            "clipping_reconstruccion_lossy": False,
            "clipping_mastering_limitador": False,
        }
    cortos = sum(1 for largo in largos if largo <= 2)
    fraccion = cortos / float(eventos)
    media = sum(largos) / float(eventos)
    corte_efectivo = (frecuencia_corte if frecuencia_corte is not None
                      else techo_hz)
    lossy = bool(fraccion > 0.75 and corte_efectivo is not None
                 and corte_efectivo < 20500.0)
    master = bool(media >= 4.0 and techo_hz is not None and nyquist
                  and techo_hz >= 0.97 * nyquist)
    return {
        "clipping_eventos":            eventos,
        "clipping_fraccion_cortos":    round(fraccion, 4),
        "clipping_longitud_media":     round(media, 2),
        "clipping_racha_maxima":       max(largos),
        "clipping_reconstruccion_lossy": lossy,
        "clipping_mastering_limitador":  master,
    }

def _clasificar_naturaleza_clipping(muestras_int, max_val_entero,
                                    techo_hz=None, frecuencia_corte=None,
                                    nyquist=None):
    """Vector 6 from native integer samples (array/direct API path).

    `max_val_entero` is the positive full-scale integer (32767 for 16 bits,
    8388607 for 24 bits). Spectral arguments are optional: without them only the
    run statistics are produced, because both classifications need the spectrum."""
    try:
        muestras = np.asarray(muestras_int).reshape(-1)
        en_clip = np.abs(muestras) >= int(max_val_entero)
        rachas, abierta = _rachas(en_clip)
        if abierta:
            rachas.append(abierta)
        return _clasificar_rachas(rachas, techo_hz, frecuencia_corte, nyquist)
    except Exception as e:
        return {"clipping_eventos": 0, "error": str(e)}

def _dr_desde_rms(rms_list, pico, n_bloques):
    """Dynamic range formula in O(n) via np.partition, shared by the array path and
    the block path. It accepts a Python list or an ndarray alike."""
    if n_bloques < 2 or pico <= 0:          # at least 6 seconds are needed
        return None
    arr     = np.asarray(rms_list, dtype=np.float64)
    validos = arr[arr > 1e-6]
    if validos.size == 0:
        return None
    k       = max(1, validos.size // 5)
    rms_top = float(np.mean(np.partition(validos, -k)[-k:]))
    return round(20 * np.log10(pico / (rms_top + 1e-30)), 1)

def calcular_dr(data, sr):
    """Estimated DR over an already loaded array. It is not the DR of the
    reference meter: it averages the channels to mono and applies neither its
    calibration nor its gating, so it is not comparable with published values."""
    try:
        mono   = np.mean(data, axis=1) if data.ndim > 1 else np.asarray(data)
        bloque = sr * 3
        n      = len(mono) // bloque
        if n < 2:
            return None
        trozos = mono[: n * bloque].reshape(n, bloque)
        rms    = np.sqrt(np.einsum("ij,ij->i", trozos, trozos) / bloque)
        pico   = float(np.max(np.abs(mono)))
        return _dr_desde_rms(rms.tolist(), pico, n)
    except (ValueError, TypeError) as e:
        print(f"[dr] {e}")
        return None

class AcumuladorForenseBits:
    """Vector 4: 24-bit container audit on the native integer samples.

    Detects two different fakes:
      * zero padding (16-in-24): the eight low bits are always zero, so an OR of
        every |sample| has at least 8 trailing zeros;
      * 16 bits scaled into 24 (gain/ReplayGain/DSP): the low bits are no longer
        zero, but the distinct absolute values stay on a sparse grid, the
        quantisation comb left behind by a 16-bit source.

    The comb is measured on unique levels, never on sample counts, so the result
    does not depend on how loud or how dense the programme material is. Four
    boolean bitmaps of the whole file are kept, one bit per absolute integer:

      * low window (-90 to -48 dBFS), where a genuine 24-bit/dithered master
        spreads the levels uniformly;
      * two high-amplitude slices around -18 and -12 dBFS, one 16-bit grid step
        wide: a hyper-compressed master never leaves the loud range, so without
        them the detector would simply have nothing to look at;
      * one global bitmap of every active level, which works at any level and is
        the one that always sees the grid.

    Each bitmap is judged by the distances between consecutive occupied levels:
    a comb leaves steps close to 256 x gain, while genuine dither visits
    neighbouring integers. The tenth percentile of those distances is reported
    as the lattice step.

    A fifth, independent measurement looks for DoP 1.0 (DSD over PCM) framing:
    at 24 bits and 176.4 kHz or above, a DoP stream marks every sample with
    0x05 or 0xFA in its top byte, alternating. It is tracked per channel, so
    interleaved stereo keeps its L/R pairs instead of mixing them."""

    def __init__(self, bits_per_sample, sample_rate=0):
        self.bits = int(bits_per_sample)
        self.sr = int(sample_rate or 0)
        self.max_val = float(1 << (self.bits - 1))
        self._or = 0
        self._muestras = 0

        # Integer bounds of the low window [-90, -48] dBFS.
        self.bajo_int = int(np.rint(self.max_val * (10.0 ** (-90.0 / 20.0))))
        self.alto_int = int(np.rint(self.max_val * (10.0 ** (-48.0 / 20.0))))
        self.ancho_bajo = max(1, self.alto_int - self.bajo_int + 1)

        if self.bits == 24:
            self._bitmap_bajo = np.zeros(self.ancho_bajo, dtype=bool)
            self._bitmap_reb1 = np.zeros(REBANADA_ANCHO, dtype=bool)
            self._bitmap_reb2 = np.zeros(REBANADA_ANCHO, dtype=bool)
            self._bitmap_global = np.zeros(int(self.max_val) + 1, dtype=bool)
        else:
            self._bitmap_bajo = None
            self._bitmap_reb1 = None
            self._bitmap_reb2 = None
            self._bitmap_global = None

        # Samples that fed each bitmap: they decide whether it is judged at all.
        self._muestras_bajas = 0
        self._muestras_reb1 = 0
        self._muestras_reb2 = 0
        self._muestras_activas = 0

        # DoP 1.0 framing counters (0x05 / 0xFA marker bytes).
        self._dop_validos = 0
        self._dop_pares = 0
        self._dop_ultimo = None

    def _seguir_dop(self, q):
        """Counts DoP marker transitions in the top byte of consecutive samples.

        The marker lives in the most significant byte of the 24-bit integer, so
        the shift is applied to the SIGNED sample: 0xFA0000 is a negative value
        whose byte is 0xFA, which |q| would report as 0x06. The scan stays 2D,
        one column per channel: ravel() would interleave L and R and destroy
        every marker pair of a stereo stream."""
        vista = q[:, None] if q.ndim == 1 else q
        if vista.shape[0] == 0:
            return
        tops = ((vista >> 16) & 0xFF).astype(np.int16)
        if self._dop_ultimo is not None and self._dop_ultimo.shape[0] == tops.shape[1]:
            tops = np.vstack((self._dop_ultimo[None, :], tops))
        if tops.shape[0] >= 2:
            anterior = tops[:-1, :]
            siguiente = tops[1:, :]
            validos = (((anterior == 0x05) & (siguiente == 0xFA))
                       | ((anterior == 0xFA) & (siguiente == 0x05))
                       | ((anterior == 0x06) & (siguiente == 0xF9))
                       | ((anterior == 0xF9) & (siguiente == 0x06)))
            self._dop_pares += int(validos.size)
            self._dop_validos += int(np.count_nonzero(validos))
        self._dop_ultimo = tops[-1, :].copy()

    def anadir(self, bloque):
        if self.bits != 24 or bloque.shape[0] == 0:
            return
        q = np.rint(np.asarray(bloque, dtype=np.float64) * self.max_val)
        q = np.clip(q, -self.max_val, self.max_val - 1.0).astype(np.int64)
        self._muestras += int(q.size)
        absq = np.abs(q).ravel()
        self._or |= int(np.bitwise_or.reduce(absq))

        mask_b = (absq >= self.bajo_int) & (absq <= self.alto_int)
        if np.any(mask_b):
            self._bitmap_bajo[absq[mask_b] - self.bajo_int] = True
            self._muestras_bajas += int(np.count_nonzero(mask_b))

        mask_1 = (absq >= REBANADA_1_BASE) & (absq < REBANADA_1_BASE + REBANADA_ANCHO)
        if np.any(mask_1):
            self._bitmap_reb1[absq[mask_1] - REBANADA_1_BASE] = True
            self._muestras_reb1 += int(np.count_nonzero(mask_1))

        mask_2 = (absq >= REBANADA_2_BASE) & (absq < REBANADA_2_BASE + REBANADA_ANCHO)
        if np.any(mask_2):
            self._bitmap_reb2[absq[mask_2] - REBANADA_2_BASE] = True
            self._muestras_reb2 += int(np.count_nonzero(mask_2))

        activos = absq[absq > 0]
        if activos.size:
            self._bitmap_global[activos] = True
            self._muestras_activas += int(activos.size)

        if self.bits == 24 and self.sr >= 176400:
            self._seguir_dop(q)

    @staticmethod
    def _evaluar_bitmap(bitmap, offset, n_muestras):
        """Evaluates the unique-level lattice inside a boolean bitmap window.

        Only the occupied levels matter, never how many samples hit them, so a
        quiet window with few samples is reported as not evaluated instead of
        being mistaken for a comb. The comb test is `p10 > 16`, the distance
        that separates the 16-bit grid (256 x gain integers) from dither that
        visits neighbouring levels."""
        niveles_unicos = np.flatnonzero(bitmap) + int(offset)
        n_unicos = int(niveles_unicos.size)
        if n_muestras < 2048 or n_unicos < 64:
            return {
                "evaluado": False,
                "es_peine": False,
                "p10": 1.0,
                "entropia": None,
                "cajones": n_unicos,
            }
        hist_mod = np.bincount((niveles_unicos & 0xFF).astype(np.uint8), minlength=256)
        cajones = int(np.count_nonzero(hist_mod))
        p = hist_mod[hist_mod > 0] / float(n_unicos)
        entropia = float(-np.sum(p * np.log2(p)))

        diffs = np.diff(niveles_unicos)
        diffs_locales = diffs[diffs <= 1024]
        if diffs_locales.size < 32:
            return {
                "evaluado": False,
                "es_peine": False,
                "p10": 1.0,
                "entropia": entropia,
                "cajones": cajones,
            }
        p10 = float(np.percentile(diffs_locales, 10))
        med = float(np.median(diffs_locales))
        frac = float(np.mean(np.abs(diffs_locales - med) <= 2.0))
        es_peine = bool(p10 > 16.0 and frac >= 0.70)
        return {
            "evaluado": True,
            "es_peine": es_peine,
            "p10": p10,
            "entropia": entropia,
            "cajones": cajones,
        }

    def resultado(self):
        if self.bits != 24 or self._muestras == 0:
            return {"aplica": False}

        valor_or = int(self._or)
        if valor_or == 0:                      # digital silence: no grid to judge
            ceros = self.bits
        else:
            ceros = (valor_or & -valor_or).bit_length() - 1
        padded = bool(valor_or and ceros >= 8)

        r_bajo = self._evaluar_bitmap(self._bitmap_bajo, self.bajo_int,
                                      self._muestras_bajas)
        r_reb1 = self._evaluar_bitmap(self._bitmap_reb1, REBANADA_1_BASE,
                                      self._muestras_reb1)
        r_reb2 = self._evaluar_bitmap(self._bitmap_reb2, REBANADA_2_BASE,
                                      self._muestras_reb2)
        r_glob = self._evaluar_bitmap(self._bitmap_global, 0, self._muestras_activas)
        ventanas = (r_bajo, r_reb1, r_reb2, r_glob)

        if not padded:
            peine = any(r["es_peine"] for r in ventanas)
            # Representative window: the first one that saw the comb, otherwise
            # the first one that could be measured at all (the low window if
            # nothing could be measured).
            rep = None
            for r in ventanas:
                if r["es_peine"]:
                    rep = r
                    break
            if rep is None:
                for r in ventanas:
                    if r["evaluado"]:
                        rep = r
                        break
            if rep is None:
                rep = r_bajo
            entropia_rep = rep["entropia"]
            ocupados_rep = rep["cajones"]
            if peine:
                paso_reticula = rep["p10"]
            else:
                paso_reticula = r_glob["p10"] if r_glob["evaluado"] else rep["p10"]
        else:
            peine = False
            entropia_rep = r_bajo["entropia"]
            ocupados_rep = r_bajo["cajones"]
            paso_reticula = 1.0

        dop_pares = int(self._dop_pares)
        dop_ratio = (self._dop_validos / float(dop_pares)) if dop_pares else 0.0
        es_flujo_dop = bool(self.bits == 24 and self.sr >= 176400
                            and dop_pares >= 100 and dop_ratio >= 0.90)

        return {
            "aplica":                     True,
            "es_padded_16_en_24":         padded,
            "trailing_zeros_lsb":         int(ceros),
            "lsb8_entropia_bits":         round(entropia_rep, 3) if entropia_rep is not None else None,
            "lsb8_cajones_ocupados":      ocupados_rep,
            "muestras_pasaje_bajo":       int(self._muestras_bajas),
            "paso_reticula_mediano":      round(paso_reticula, 2),
            "es_24bit_escalado_en_peine": peine,
            "es_flujo_dop":              es_flujo_dop,
            "dop_pares_inspeccionados":   dop_pares,
            "dop_ratio":                  round(dop_ratio, 4),
        }

def _analizar_forense_bits(data, meta):
    """Array-path counterpart of AcumuladorForenseBits (used by the oracle and
    by direct API callers). It walks the same FRAMES_POR_BLOQUE blocks."""
    bits = meta.get("bits_per_sample")
    if bits != 24:
        return {"aplica": False}
    acumulador = AcumuladorForenseBits(bits,
                                       sample_rate=meta.get("sample_rate", 0))
    for inicio in range(0, len(data), FRAMES_POR_BLOQUE):
        acumulador.anadir(data[inicio : inicio + FRAMES_POR_BLOQUE])
    return acumulador.resultado()
