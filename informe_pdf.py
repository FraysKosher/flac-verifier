"""Informe PDF por álbum — capa de presentación con reportlab.

Dependencia OPCIONAL: `reportlab`. Si no está instalado, el análisis funciona
igual y este módulo solo informa de que no puede generar el informe. Ni el motor
ni el CLI importan reportlab: solo lo usan a través de `disponible()` y
`generar()`, y `generar()` NUNCA lanza: devuelve un dict con `ok`/`error` para
que un fallo al escribir el PDF no aborte el análisis.

Estructura del informe:
    1. Portada: ruta del álbum, fecha, resumen ejecutivo por colores.
    2. Una página por canción: veredicto + score, tabla técnica, problemas y
       espectrograma.
    3. Página final: tabla global de todas las canciones.
"""
import os
import shutil
import tempfile
import time

try:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.lib.utils import ImageReader
    from reportlab.platypus import (Image, KeepTogether, PageBreak, Paragraph,
                                    SimpleDocTemplate, Spacer, Table, TableStyle)
    REPORTLAB_OK = True
except ImportError:                     # el análisis sigue funcionando sin esto
    REPORTLAB_OK = False

NOMBRE_INFORME = "flac_verifier_report.pdf"
MAX_PX_ESPECTROGRAMA = 1800            # los PNG se recomprimen para no hinchar el PDF

# Geometría del marco, en milímetros (se multiplican por `mm` dentro de las
# funciones: `mm` viene de reportlab y no puede usarse al importar el módulo).
ANCHO_MARCO_MM = 170                   # A4 menos los márgenes laterales (20+20)
ALTO_MARCO_MM  = 259                   # A4 menos los márgenes superior e inferior (18+20)
ALTO_IMG_MAX_MM = 62                   # altura deseada del espectrograma
ALTO_IMG_MIN_MM = 34                   # por debajo de esto no se encoge más

# Veredicto canónico -> (etiqueta, color de texto, color de fondo).
# Verde / amarillo / naranja / rojo, con texto oscuro sobre fondo claro para que
# se lea bien impreso.
ESTILO_VEREDICTO = {
    "GENUINE LOSSLESS":       ("GENUINE LOSSLESS",        "#14632C", "#E3F4E7"),
    "PROBABLY LOSSLESS": ("PROBABLY LOSSLESS",  "#8A6D00", "#FBF3D5"),
    "SUSPICIOUS":                 ("SUSPICIOUS",                  "#A85B00", "#FDE8D5"),
    "PROBABLE UPSCALE":       ("PROBABLE UPSCALE",        "#A11616", "#FBE0E0"),
    "indeterminate":          ("INDETERMINATE",           "#444444", "#EDEDED"),
}
ESTILO_ERROR = ("NO VERDICT (ERROR)", "#A11616", "#FBE0E0")

TEXTO_CLASE_CORTE = {
    "no_cutoff":            "no cut-off detected",
    "near_nyquist_cutoff":  "inconclusive (consistent with a mastering filter)",
    "far_cutoff":         "lossy codec signature",
}
TEXTO_MD5 = {
    "match":      "verified (match)",
    "mismatch":   "MISMATCH — file corrupt",
    "absent":       "no MD5 recorded",
    "unverifiable": "not verifiable at this bit depth",
}
TEXTO_LSBS = {
    "active":       "active",
    "partial":     "partially inactive",
    "empty":        "empty (bit-depth upscale)",
    "indeterminate": "indeterminate",
}


def disponible():
    """¿Se puede generar el informe en este entorno?"""
    return REPORTLAB_OK


def ruta_informe(ruta_analizada):
    """El informe va en la carpeta del álbum; si es un archivo, junto a él."""
    absoluta = os.path.abspath(ruta_analizada)
    base = absoluta if os.path.isdir(absoluta) else os.path.dirname(absoluta)
    return os.path.join(base, NOMBRE_INFORME)


# ─── HELPERS DE FORMATO ──────────────────────────────────────────────────────

def _estilo_veredicto(res):
    if res.get("error"):
        return ESTILO_ERROR
    return ESTILO_VEREDICTO.get(res.get("veredicto"), ("INDETERMINATE", "#444444", "#EDEDED"))


def _num(valor, unidad="", decimales=1, si_none="—"):
    if valor is None:
        return si_none
    if isinstance(valor, float):
        texto = f"{valor:,.{decimales}f}".replace(",", " ")
    else:
        texto = f"{valor:,}".replace(",", " ")
    return f"{texto}{unidad}"


def _pct(valor):
    return "—" if valor is None else f"{valor * 100:.0f} %"


def _formato_meta(meta):
    if not meta:
        return "—"
    return (f"{_num(meta.get('sample_rate'), ' Hz', 0)} · "
            f"{meta.get('bits_per_sample', '?')} bits · "
            f"{meta.get('canales', '?')} channel(s)")


def _resumen(resultados):
    """Cuenta veredictos y errores. Devuelve (filas, totales)."""
    conteo = {clave: 0 for clave in ESTILO_VEREDICTO}
    errores = 0
    for res in resultados:
        if res.get("error") or "veredicto" not in res:
            errores += 1
        else:
            conteo[res["veredicto"]] = conteo.get(res["veredicto"], 0) + 1
    return conteo, errores


# ─── CONSTRUCCIÓN DEL DOCUMENTO ──────────────────────────────────────────────

def _estilos():
    hoja = getSampleStyleSheet()
    return {
        "titulo":    ParagraphStyle("titulo", parent=hoja["Title"], fontSize=24,
                                    leading=28, spaceAfter=2 * mm),
        "subtitulo": ParagraphStyle("subtitulo", parent=hoja["Normal"], fontSize=10,
                                    textColor=colors.HexColor("#555555"), leading=14),
        "seccion":   ParagraphStyle("seccion", parent=hoja["Heading2"], fontSize=13,
                                    spaceBefore=4 * mm, spaceAfter=2.5 * mm,
                                    textColor=colors.HexColor("#222222")),
        "cancion":   ParagraphStyle("cancion", parent=hoja["Heading2"], fontSize=12.5,
                                    spaceAfter=1 * mm, textColor=colors.HexColor("#111111")),
        "normal":    ParagraphStyle("normal", parent=hoja["Normal"], fontSize=9.5,
                                    leading=13),
        "celda":     ParagraphStyle("celda", parent=hoja["Normal"], fontSize=8.5,
                                    leading=11),
        "etiqueta":  ParagraphStyle("etiqueta", parent=hoja["Normal"], fontSize=8.5,
                                    leading=11, textColor=colors.HexColor("#666666")),
        "problema":  ParagraphStyle("problema", parent=hoja["Normal"], fontSize=9,
                                    leading=12.5, leftIndent=4 * mm,
                                    bulletIndent=1 * mm, spaceAfter=1 * mm),
        "insignia":  ParagraphStyle("insignia", parent=hoja["Normal"], fontSize=10.5,
                                    leading=13, alignment=1),
        "pie":       ParagraphStyle("pie", parent=hoja["Normal"], fontSize=7.5,
                                    textColor=colors.HexColor("#888888")),
    }


def _tabla(datos, anchos, estilos, estilo_celdas=None):
    tabla = Table(datos, colWidths=anchos, hAlign="LEFT")
    comandos = [("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 4),
                ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]
    tabla.setStyle(TableStyle(comandos + list(estilo_celdas or [])))
    return tabla


def _tabla_tecnica(res, est):
    """Tabla de datos técnicos: dos pares etiqueta/valor por fila."""
    meta = res.get("meta") or {}
    esp  = res.get("esp") or {}
    bdi  = res.get("bdi") or {}
    md5  = res.get("md5") or {}
    clip = res.get("clip") or {}

    def valor(clave, texto):
        return [Paragraph(clave, est["etiqueta"]), Paragraph(texto, est["celda"])]

    duracion = meta.get("duracion")
    partes = [
        valor("Format", _formato_meta(meta)),
        valor("Duration", _num(duracion, " s", 1) if duracion is not None else "—"),
    ]

    tamano = None
    ruta = res.get("ruta")
    if ruta and os.path.exists(ruta):
        try:
            tamano = f"{os.path.getsize(ruta) / 2 ** 20:.1f} MB"
        except OSError:
            tamano = None
    partes.append(valor("Size", tamano or "—"))

    partes.append(valor("MD5 integrity", TEXTO_MD5.get(md5.get("estado"), "—")))
    partes.append(valor("Clipping", (f"{clip.get('runs_clip', 0)} clipping run(s) at full scale"
                                     if clip.get("hay_clipping") else "no clipping")
                        if clip else "—"))
    partes.append(valor("Dynamic range", _num(res.get("dr"), " dB (estimated)", 1)))

    if bdi.get("aplica"):
        texto_bits = (f"{TEXTO_LSBS.get(bdi.get('lsbs'), '—')}"
                      f" · effective resolution "
                      f"{bdi.get('resolucion_efectiva_bits', '?')} of "
                      f"{meta.get('bits_per_sample', '?')} bits")
    else:
        texto_bits = "not applicable (≤ 16 bits)"
    partes.append(valor("LSB utilization", texto_bits))

    if esp.get("error"):
        partes.append(valor("Spectrum", f"not analyzed ({esp['error']})"))
    else:
        nyq = esp.get("nyquist") or 0
        partes.append(valor("Spectral ceiling",
                            f"{_num(esp.get('techo_hz'), ' Hz', 0)}"
                            f" ({_pct(esp.get('techo_rel_nyquist'))} of Nyquist)"))
        if esp.get("corte_detectado"):
            clase = TEXTO_CLASE_CORTE.get(esp.get("clase_corte"), esp.get("clase_corte"))
            texto_corte = (f"{_num(esp.get('frecuencia_corte'), ' Hz', 0)}"
                           f" ({_pct(esp.get('distancia_nyquist'))} of Nyquist) · {clase}")
        else:
            texto_corte = "no cut-off detected"
        partes.append(valor("Spectral cut-off", texto_corte))
        partes.append(valor("High/mid ratio",
                            _num(esp.get("ratio"), "", 5)
                            if esp.get("ratio_aplica") else "not applicable"))
        partes.append(valor("High-band variance", _num(esp.get("var_alta_db"), " dB²", 1)))
        partes.append(valor("L-R separation", _num(esp.get("separacion_stereo"), "", 4)))
        partes.append(valor("Nyquist", _num(nyq, " Hz", 0)))

    # Rejilla de 4 columnas: etiqueta, valor, etiqueta, valor. El ancho de las
    # etiquetas (34 mm) evita que se partan en dos líneas en A4.
    filas = []
    for i in range(0, len(partes), 2):
        izquierda = partes[i]
        derecha = partes[i + 1] if i + 1 < len(partes) else ["", ""]
        filas.append(izquierda + derecha)
    anchos = [34 * mm, 50 * mm, 34 * mm, 50 * mm]
    return _tabla(filas, anchos, est,
                  [("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#DDDDDD")),
                   ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#FAFAFA")),
                   ("BACKGROUND", (2, 0), (2, -1), colors.HexColor("#FAFAFA"))])


def _insignia(res, est):
    etiqueta, color_texto, color_fondo = _estilo_veredicto(res)
    if res.get("error"):
        texto = etiqueta
    else:
        texto = f"{etiqueta} &nbsp;·&nbsp; score {_pct(res.get('score'))}"
    celda = Paragraph(f'<font color="{color_texto}"><b>{texto}</b></font>', est["insignia"])
    return _tabla([[celda]], [172 * mm], est,
                  [("BACKGROUND", (0, 0), (-1, -1), colors.HexColor(color_fondo)),
                   ("BOX", (0, 0), (-1, -1), 0.6, colors.HexColor(color_texto)),
                   ("TOPPADDING", (0, 0), (-1, -1), 4),
                   ("BOTTOMPADDING", (0, 0), (-1, -1), 4)])


def _imagen_espectrograma(res, temporal, ancho_max=None, alto_max=None):
    """Devuelve (flowable, aviso). El aviso explica por qué no hay imagen.

    Los PNG de matplotlib pesan ~1.2 MB cada uno; para el PDF se reescalan y se
    recomprimen en JPEG dentro de un directorio temporal (reportlab necesita una
    ruta de archivo). Sin Pillow se embebe el PNG original: el informe sale igual.

    Los tamaños se calculan DENTRO de la función a propósito: `mm` viene de
    reportlab, y un valor por defecto en la firma se evaluaría al importar el
    módulo, rompiendo la importación cuando reportlab no está instalado."""
    ancho_max = ancho_max or 170 * mm
    alto_max  = alto_max or 62 * mm
    ruta = (res.get("esp") or {}).get("espectrograma")
    if not ruta:
        return None, "spectrogram not generated (matplotlib not available)"
    if not os.path.exists(ruta):
        return None, "the spectrogram file is no longer available"

    fuente = ruta
    try:
        from PIL import Image as ImagenPIL
        with ImagenPIL.open(ruta) as imagen:
            px_ancho, px_alto = imagen.size
            copia = imagen.convert("RGB")
            copia.thumbnail((MAX_PX_ESPECTROGRAMA, MAX_PX_ESPECTROGRAMA))
            fuente = os.path.join(temporal, os.path.basename(ruta) + ".jpg")
            copia.save(fuente, format="JPEG", quality=88, optimize=True)
    except Exception:
        fuente = ruta                              # sin Pillow: PNG original
        try:
            px_ancho, px_alto = ImageReader(ruta).getSize()
        except Exception as e:
            return None, f"could not embed the spectrogram ({type(e).__name__})"

    altura = ancho_max * px_alto / float(px_ancho)
    ancho = ancho_max
    if altura > alto_max:                          # se ajusta manteniendo la proporción
        altura = alto_max
        ancho = altura * px_ancho / float(px_alto)
    return Image(fuente, width=ancho, height=altura), None


def _alto_de(flowables):
    """Alto real que ocuparán esos flowables dentro del marco del informe."""
    total = 0.0
    for flowable in flowables:
        try:
            _, alto = flowable.wrap(ANCHO_MARCO_MM * mm, ALTO_MARCO_MM * mm)
        except Exception:
            alto = 0.0
        total += alto + flowable.getSpaceBefore() + flowable.getSpaceAfter()
    return total


def _ajustar_imagen(imagen, disponible):
    """Encoge la imagen (hasta ALTO_IMG_MIN_MM) para que quepa en lo que queda.

    Sin esto, una ficha con muchos problemas empujaba el espectrograma a la
    página siguiente y lo dejaba suelto arriba, separado de su título."""
    if imagen is None:
        return None
    deseado = min(imagen.drawHeight, ALTO_IMG_MAX_MM * mm)
    alto = max(ALTO_IMG_MIN_MM * mm, min(deseado, disponible))
    if alto >= imagen.drawHeight - 0.5:
        return imagen
    proporcion = alto / float(imagen.drawHeight)
    return Image(imagen.filename, width=imagen.drawWidth * proporcion, height=alto)


def _ficha_cancion(indice, total, res, est, temporal):
    """Página de una canción."""
    elemento = [Paragraph(f"{indice} of {total} &nbsp;·&nbsp; {res.get('archivo', '?')}",
                          est["cancion"]),
                _insignia(res, est),
                Spacer(1, 3 * mm)]

    if res.get("error"):
        elemento.append(Paragraph(f"<b>Could not analyze:</b> {res['error']}", est["normal"]))
        elemento.append(Spacer(1, 3 * mm))

    elemento.append(_tabla_tecnica(res, est))
    elemento.append(Spacer(1, 4 * mm))

    problemas = res.get("problemas") or []
    elemento.append(Paragraph("Detected issues", est["seccion"]))
    if problemas:
        for problema in problemas:
            elemento.append(Paragraph(f"• {problema}", est["problema"]))
    else:
        elemento.append(Paragraph("No issues detected.", est["normal"]))
    elemento.append(Spacer(1, 4 * mm))

    # La imagen se mide y se ajusta para que el título y el espectrograma queden
    # siempre juntos en la misma página. KeepTogether es la red de seguridad para
    # el caso extremo (una lista de problemas que no deja sitio ni al mínimo).
    titulo_espectro = Paragraph("Spectrogram", est["seccion"])
    imagen = _ajustar_imagen(_imagen_espectrograma(res, temporal)[0],
                             ALTO_MARCO_MM * mm - _alto_de(elemento + [titulo_espectro])
                             - 2 * mm)
    if imagen is not None:
        elemento.append(KeepTogether([titulo_espectro, imagen]))
    else:
        aviso = _imagen_espectrograma(res, temporal)[1]
        elemento.append(titulo_espectro)
        elemento.append(Paragraph(f"(no image: {aviso})", est["normal"]))
    return elemento


def _portada(ruta_album, resultados, modo, est):
    conteo, errores = _resumen(resultados)
    total = len(resultados)
    filas = [[Paragraph("<b>Verdict</b>", est["celda"]),
              Paragraph("<b>Files</b>", est["celda"]),
              Paragraph("<b>Percentage</b>", est["celda"])]]
    comandos = [("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#DDDDDD")),
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#F0F0F0")),
                ("ALIGN", (1, 0), (-1, -1), "CENTER")]

    def fila(etiqueta, color_texto, cuenta):
        return [Paragraph(f'<font color="{color_texto}"><b>{etiqueta}</b></font>', est["celda"]),
                Paragraph(f'<font color="{color_texto}"><b>{cuenta}</b></font>', est["celda"]),
                Paragraph(f'<font color="{color_texto}">'
                          f'{(cuenta * 100 / total) if total else 0:.0f} %</font>', est["celda"])]

    for clave, (etiqueta, color_texto, color_fondo) in ESTILO_VEREDICTO.items():
        if conteo.get(clave):
            filas.append(fila(etiqueta, color_texto, conteo[clave]))
            comandos.append(("BACKGROUND", (0, len(filas) - 1), (-1, len(filas) - 1),
                             colors.HexColor(color_fondo)))
    if errores:
        etiqueta, color_texto, color_fondo = ESTILO_ERROR
        filas.append(fila(etiqueta, color_texto, errores))
        comandos.append(("BACKGROUND", (0, len(filas) - 1), (-1, len(filas) - 1),
                         colors.HexColor(color_fondo)))
    filas.append([Paragraph("<b>TOTAL</b>", est["celda"]),
                  Paragraph(f"<b>{total}</b>", est["celda"]),
                  Paragraph("<b>100 %</b>", est["celda"])])
    comandos.append(("BACKGROUND", (0, len(filas) - 1), (-1, len(filas) - 1),
                     colors.HexColor("#F0F0F0")))

    elemento = [
        Paragraph("FLAC verification report", est["titulo"]),
        Paragraph("Fake lossless detection: lossy transcodes and "
                  "bit-depth upgrades.", est["subtitulo"]),
        Spacer(1, 6 * mm),
        Paragraph(f"<b>Album analyzed:</b> {ruta_album}", est["normal"]),
        Paragraph(f"<b>Analysis date:</b> "
                  f"{time.strftime('%Y-%m-%d %H:%M:%S')}", est["normal"]),
        Paragraph(f"<b>Files analyzed:</b> {total}", est["normal"]),
    ]
    if modo:
        elemento.append(Paragraph(f"<b>Spectral analysis mode:</b> {modo}", est["normal"]))
    elemento += [Spacer(1, 6 * mm),
                 Paragraph("Executive summary", est["seccion"]),
                 _tabla(filas, [72 * mm, 30 * mm, 30 * mm], est, comandos),
                 Spacer(1, 4 * mm),
                 Paragraph("The verdict rests on four verifiable facts: "
                           "STREAMINFO MD5 integrity, actual LSB utilization, spectral "
                           "analysis with distance to Nyquist, and coherence between the declared "
                           "format and the content. A file with a confirmed defect "
                           "never receives the top verdict.", est["subtitulo"])]
    return elemento


def _tabla_final(resultados, est):
    filas = [[Paragraph("<b>#</b>", est["celda"]),
              Paragraph("<b>File</b>", est["celda"]),
              Paragraph("<b>Verdict</b>", est["celda"]),
              Paragraph("<b>Score</b>", est["celda"]),
              Paragraph("<b>Issues</b>", est["celda"])]]
    comandos = [("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#DDDDDD")),
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#F0F0F0")),
                ("ALIGN", (0, 0), (0, -1), "CENTER"),
                ("ALIGN", (3, 0), (4, -1), "CENTER")]
    for i, res in enumerate(resultados, start=1):
        etiqueta, color_texto, color_fondo = _estilo_veredicto(res)
        filas.append([Paragraph(str(i), est["celda"]),
                      Paragraph(res.get("archivo", "?"), est["celda"]),
                      Paragraph(f'<font color="{color_texto}"><b>{etiqueta}</b></font>',
                                est["celda"]),
                      Paragraph(_pct(res.get("score")), est["celda"]),
                      Paragraph("—" if res.get("error") else str(len(res.get("problemas") or [])),
                                est["celda"])])
        comandos.append(("BACKGROUND", (2, len(filas) - 1), (2, len(filas) - 1),
                         colors.HexColor(color_fondo)))
    return _tabla(filas, [10 * mm, 62 * mm, 52 * mm, 20 * mm, 22 * mm], est, comandos)


def _pie_de_pagina(canvas, doc, album, contador):
    """Pie con el nombre del álbum y el número de página."""
    contador["paginas"] = max(contador["paginas"], canvas.getPageNumber())
    canvas.saveState()
    canvas.setStrokeColor(colors.HexColor("#DDDDDD"))
    canvas.setLineWidth(0.4)
    canvas.line(20 * mm, 14 * mm, A4[0] - 20 * mm, 14 * mm)
    canvas.setFont("Helvetica", 7.5)
    canvas.setFillColor(colors.HexColor("#888888"))
    canvas.drawString(20 * mm, 10 * mm, f"FLAC VERIFIER — {album}"[:110])
    canvas.drawRightString(A4[0] - 20 * mm, 10 * mm, f"Page {canvas.getPageNumber()}")
    canvas.restoreState()


# ─── API PÚBLICA ─────────────────────────────────────────────────────────────

def generar(ruta_analizada, resultados, destino=None, modo=None):
    """Escribe el informe PDF. NUNCA lanza.

    Devuelve {"ok": bool, "ruta": str|None, "error": str|None, "paginas": int}.
    """
    if not REPORTLAB_OK:
        return {"ok": False, "ruta": None, "paginas": 0,
                "error": "reportlab is not installed (pip install reportlab)"}
    if not resultados:
        return {"ok": False, "ruta": None, "paginas": 0,
                "error": "there are no results to include in the report"}

    destino = destino or ruta_informe(ruta_analizada)
    est = _estilos()
    contador = {"paginas": 0}
    temporal = None
    try:
        # Las imágenes recomprimidas viven aquí y se borran al terminar el build.
        temporal = tempfile.mkdtemp(prefix="flac_verifier_report_")
        documento = SimpleDocTemplate(
            destino, pagesize=A4,
            leftMargin=20 * mm, rightMargin=20 * mm,
            topMargin=18 * mm, bottomMargin=20 * mm,
            title="FLAC verification report",
            author="FLAC VERIFIER",
            subject=f"Verification of {len(resultados)} file(s)")

        pie = lambda lienzo, doc: _pie_de_pagina(lienzo, doc, os.path.basename(
            os.path.dirname(destino) or destino), contador)

        elementos = _portada(ruta_analizada, resultados, modo, est)
        for i, res in enumerate(resultados, start=1):
            elementos.append(PageBreak())
            elementos += _ficha_cancion(i, len(resultados), res, est, temporal)
        elementos += [PageBreak(),
                      Paragraph("Overall summary", est["seccion"]),
                      _tabla_final(resultados, est)]

        documento.build(elementos, onFirstPage=pie, onLaterPages=pie)
        paginas = contador["paginas"]
        return {"ok": True, "ruta": destino, "error": None, "paginas": paginas}
    except Exception as e:
        return {"ok": False, "ruta": None, "paginas": 0,
                "error": f"{type(e).__name__}: {e}"}
    finally:
        if temporal:
            shutil.rmtree(temporal, ignore_errors=True)
