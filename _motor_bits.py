"""Integrity, native-bit audit, clipping and dynamic range.

Extracted from the motor facade in Phase 4. Nothing here imports the spectral
or cache modules: this layer only needs NumPy, soundfile/mutagen and stdlib.
"""
import hashlib
import os

import numpy as np

FRAMES_POR_BLOQUE = 1 << 18            # 262 144 frames (~6 s at 44.1 kHz) per read

MAX_MUESTRAS_BITS = 1 << 20            # sample for the LSB test

# Vector 4 (lattice): absolute values kept to measure the quantisation step.
MAX_MUESTRAS_RETICULA  = 1 << 18        # 262 144 samples are plenty for a median
MIN_MUESTRAS_RETICULA  = 4096           # below this the median step is noise

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
        zero, but they form a quantisation comb, with Shannon entropy below
        7.45 bits or fewer than 235 of the 256 bins occupied.

    Three measurements are kept, because one is not enough:
      * low range: samples between -90 and -48 dBFS, where the low bits carry no
        musical signal and a genuine 24-bit/dithered master spreads them
        uniformly;
      * high-amplitude slices around -18 and -12 dBFS: a hyper-compressed master
        never leaves the loud range, so without them the comb detector simply
        switches itself off;
      * lattice: the median distance between distinct absolute values. A 16-bit
        source scaled into 24 bits keeps landing on the same sparse grid (a step
        close to 256 x its gain), whereas genuine 24-bit dither visits
        neighbouring integers.

    A fourth, independent measurement looks for DoP 1.0 (DSD over PCM) framing:
    at 24 bits and 176.4 kHz or above, a DoP stream marks every sample with
    0x05 or 0xFA in its top byte, alternating."""

    MIN_MUESTRAS_BAJAS     = 2048
    MIN_MUESTRAS_REBANADA  = 2048

    def __init__(self, bits_per_sample, sample_rate=0):
        self.bits = int(bits_per_sample)
        self.sr = int(sample_rate or 0)
        self.max_val = float(1 << (self.bits - 1))
        self._or = 0
        self._hist = np.zeros(256, dtype=np.int64)
        self._muestras = 0
        self._muestras_bajas = 0
        # High-amplitude slices: only meaningful when the low range is empty.
        self._hist_reb1 = np.zeros(256, dtype=np.int64)
        self._hist_reb2 = np.zeros(256, dtype=np.int64)
        # Lattice: a bounded sample of the active absolute values.
        self._muestra_absq = []
        self._total_muestra_absq = 0
        # DoP 1.0 framing counters (0x05 / 0xFA marker bytes).
        self._dop_validos = 0
        self._dop_pares = 0
        self._dop_ultimo = None

    def _seguir_dop(self, q):
        """Counts DoP marker transitions in the top byte of consecutive samples.

        The marker lives in the most significant byte of the 24-bit integer, so
        the shift is applied to the SIGNED sample: 0xFA0000 is a negative value
        whose byte is 0xFA, which |q| would report as 0x06."""
        tops = ((q.ravel() >> 16) & 0xFF).astype(np.int16)
        if self._dop_ultimo is not None:
            tops = np.concatenate((np.array([self._dop_ultimo], dtype=np.int16), tops))
        if tops.size == 0:
            return
        if tops.size >= 2:
            anterior  = tops[:-1]
            siguiente = tops[1:]
            validos = (((anterior == 0x05) & (siguiente == 0xFA))
                       | ((anterior == 0xFA) & (siguiente == 0x05)))
            self._dop_pares += int(validos.size)
            self._dop_validos += int(np.count_nonzero(validos))
        self._dop_ultimo = int(tops[-1])

    def anadir(self, bloque):
        if self.bits != 24 or bloque.shape[0] == 0:
            return
        q = np.rint(np.asarray(bloque, dtype=np.float64) * self.max_val)
        q = np.clip(q, -self.max_val, self.max_val - 1.0).astype(np.int64)
        self._muestras += int(q.size)
        self._or |= int(np.bitwise_or.reduce(np.abs(q).ravel()))

        absq = np.abs(q)
        bajo = self.max_val * 10.0 ** (-90.0 / 20.0)
        alto = self.max_val * 10.0 ** (-48.0 / 20.0)
        seleccion = absq[(absq >= bajo) & (absq <= alto)]
        if seleccion.size:
            residuos = (seleccion & 0xFF).astype(np.uint8)
            self._hist += np.bincount(residuos, minlength=256)
            self._muestras_bajas += int(residuos.size)

        # High-amplitude slices: the same low-byte histogram, an octave and a
        # half above the quiet window.
        for base, hist in ((REBANADA_1_BASE, self._hist_reb1),
                           (REBANADA_2_BASE, self._hist_reb2)):
            sel = absq[(absq >= base) & (absq < base + REBANADA_ANCHO)]
            if sel.size:
                hist += np.bincount((sel & 0xFF).astype(np.uint8), minlength=256)

        # Lattice: keep a bounded sample of the active absolute values.
        if self._total_muestra_absq < MAX_MUESTRAS_RETICULA:
            activos = absq[absq > 0]
            if activos.size:
                margen = MAX_MUESTRAS_RETICULA - self._total_muestra_absq
                tomar = activos[:margen]
                self._muestra_absq.append(tomar)
                self._total_muestra_absq += int(tomar.size)

        if self.bits == 24 and self.sr >= 176400:
            self._seguir_dop(q)

    def _paso_reticula(self):
        """Median distance between distinct absolute values (1.0 if unknown)."""
        if self._total_muestra_absq < MIN_MUESTRAS_RETICULA:
            return 1.0
        arr_muestras = np.concatenate(self._muestra_absq)
        unicos = np.unique(arr_muestras)
        if unicos.size < 64:
            return 1.0
        return float(np.median(np.diff(unicos)))

    def resultado(self):
        if self.bits != 24 or self._muestras == 0:
            return {"aplica": False}

        valor_or = int(self._or)
        if valor_or == 0:                      # digital silence: no grid to judge
            ceros = self.bits
        else:
            ceros = (valor_or & -valor_or).bit_length() - 1
        padded = bool(valor_or and ceros >= 8)

        def _calc_h(hist):
            """(entropy in bits, occupied bins, total samples) of a 256-bin LSB
            histogram. Entropy is None when the histogram is empty."""
            tot = int(hist.sum())
            if not tot:
                return None, 0, 0
            p = hist[hist > 0] / float(tot)
            return float(-np.sum(p * np.log2(p))), int(np.count_nonzero(hist)), tot

        entropia_bajo, ocupados_bajo, tot_bajo = _calc_h(self._hist)
        entropia_reb1, ocupados_reb1, tot_reb1 = _calc_h(self._hist_reb1)
        entropia_reb2, ocupados_reb2, tot_reb2 = _calc_h(self._hist_reb2)

        paso_reticula = self._paso_reticula()

        if not padded:
            if tot_bajo >= self.MIN_MUESTRAS_BAJAS:
                peine = bool(entropia_bajo < 7.45 or ocupados_bajo < 235)
                entropia_rep, ocupados_rep = entropia_bajo, ocupados_bajo
            else:
                # No quiet samples: judge the loud slices and the lattice instead.
                peine_reb = False
                entropia_rep, ocupados_rep = None, 0
                for e_r, o_r, t_r in ((entropia_reb1, ocupados_reb1, tot_reb1),
                                      (entropia_reb2, ocupados_reb2, tot_reb2)):
                    if t_r >= self.MIN_MUESTRAS_REBANADA and (e_r < 7.20 or o_r < 180):
                        peine_reb = True
                        entropia_rep, ocupados_rep = e_r, o_r
                        break
                peine = bool(peine_reb or (paso_reticula > 16.0))
                if not peine_reb:
                    entropia_rep, ocupados_rep = entropia_bajo, ocupados_bajo
        else:
            peine = False
            entropia_rep, ocupados_rep = entropia_bajo, ocupados_bajo

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
            "muestras_pasaje_bajo":       int(tot_bajo),
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
