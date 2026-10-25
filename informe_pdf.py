"""Per-album PDF report — presentation layer built on reportlab.

OPTIONAL dependency: `reportlab`. If it is not installed, the analysis still
works and this module only reports that it cannot generate the report. Neither
the engine nor the CLI import reportlab: they use it only through `available()`
and `generate()`, and `generate()` NEVER raises: it returns a dict with
`ok`/`error` so that a failure while writing the PDF does not abort the analysis.

Report structure:
    1. Cover: album path, date, executive summary by colour.
    2. One page per song: verdict + score, technical table, issues and
       spectrogram.
    3. Final page: global table of every song.
"""
import html
import os
import shutil
import tempfile
import time
from collections.abc import MutableSequence

try:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.lib.utils import ImageReader
    from reportlab.platypus import (Image, KeepTogether, PageBreak, Paragraph,
                                    SimpleDocTemplate, Spacer, Table, TableStyle)
    REPORTLAB_OK = True
except ImportError:                     # the analysis keeps working without this
    REPORTLAB_OK = False

REPORT_NAME = "flac_verifier_report.pdf"
MAX_PX_SPECTROGRAM = 1800            # PNG files are recompressed so as not to bloat the PDF

# Frame geometry, in millimetres (they are multiplied by `mm` inside the
# functions: `mm` comes from reportlab and cannot be used at module import time).
WIDTH_FRAME_MM = 170                   # A4 minus the side margins (20+20)
HEIGHT_FRAME_MM  = 259                   # A4 minus the top and bottom margins (18+20)
HEIGHT_IMG_MAX_MM = 62                   # desired height of the spectrogram
HEIGHT_IMG_MIN_MM = 34                   # below this it is no longer shrunk

# Canonical verdict -> (label, text colour, background colour).
# Green / yellow / orange / red, with dark text on a light background so that it
# reads well in print.
STYLE_VERDICT = {
    "GENUINE LOSSLESS":       ("GENUINE LOSSLESS",        "#14632C", "#E3F4E7"),
    "PROBABLY LOSSLESS": ("PROBABLY LOSSLESS",  "#8A6D00", "#FBF3D5"),
    "SUSPICIOUS":                 ("SUSPICIOUS",                  "#A85B00", "#FDE8D5"),
    "PROBABLE UPSCALE":       ("PROBABLE UPSCALE",        "#A11616", "#FBE0E0"),
    "indeterminate":          ("INDETERMINATE",           "#444444", "#EDEDED"),
}
STYLE_ERROR = ("NO VERDICT (ERROR)", "#A11616", "#FBE0E0")

CUTOFF_CLASS_TEXT = {
    "no_cutoff":            "no cut-off detected",
    "near_nyquist_cutoff":  "inconclusive (consistent with a mastering filter)",
    "far_cutoff":         "lossy codec signature",
}
MD5_TEXT = {
    "match":      "verified (match)",
    "mismatch":   "MISMATCH — file corrupt",
    "absent":       "no MD5 recorded",
    "unverifiable": "not verifiable at this bit depth",
}
LSBS_TEXT = {
    "active":       "active",
    "partial":     "partially inactive",
    "empty":        "empty (bit-depth upscale)",
    "indeterminate": "indeterminate",
}


def available():
    """Can the report be generated in this environment?"""
    return REPORTLAB_OK


def report_path(analyzed_path):
    """The report goes into the album folder; if that is a file, next to it."""
    absolute = os.path.abspath(analyzed_path)
    base = absolute if os.path.isdir(absolute) else os.path.dirname(absolute)
    return os.path.join(base, REPORT_NAME)


# ─── FORMAT HELPERS ──────────────────────────────────────────────────────────

def _style_verdict(res):
    if res.get("error"):
        return STYLE_ERROR
    return STYLE_VERDICT.get(res.get("veredicto"), STYLE_VERDICT["indeterminate"])


def _num(value, unidad="", decimales=1, if_none="—"):
    if value is None:
        return if_none
    if isinstance(value, float):
        text = f"{value:,.{decimales}f}".replace(",", " ")
    else:
        text = f"{value:,}".replace(",", " ")
    return f"{text}{unidad}"


def _pct(value):
    return "—" if value is None else f"{value * 100:.0f} %"


def _format_meta(meta):
    if not meta:
        return "—"
    return (f"{_num(meta.get('sample_rate'), ' Hz', 0)} · "
            f"{meta.get('bits_per_sample', '?')} bits · "
            f"{meta.get('canales', '?')} channel(s)")


def _summary(results):
    """Counts verdicts and errors. Returns (rows, totals)."""
    conteo = {key: 0 for key in STYLE_VERDICT}
    errors = 0
    for res in results:
        if res.get("error") or "veredicto" not in res:
            errors += 1
        else:
            conteo[res["veredicto"]] = conteo.get(res["veredicto"], 0) + 1
    return conteo, errors


# ─── DOCUMENT CONSTRUCTION ───────────────────────────────────────────────────

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


def _table(payload, anchos, estilos, style_cells=None, repeat_rows=0):
    table = Table(payload, colWidths=anchos, hAlign="LEFT", repeatRows=repeat_rows)
    comandos = [("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 4),
                ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]
    table.setStyle(TableStyle(comandos + list(style_cells or [])))
    return table


def _technical_table(res, est):
    """Technical data table: two label/value pairs per row."""
    meta = res.get("meta") or {}
    esp  = res.get("esp") or {}
    bdi  = res.get("bdi") or {}
    md5  = res.get("md5") or {}
    clip = res.get("clip") or {}

    def value(key, text):
        return [Paragraph(key, est["etiqueta"]),
                Paragraph(html.escape(str(text)), est["celda"])]

    duracion = meta.get("duracion")
    partes = [
        value("Format", _format_meta(meta)),
        value("Duration", _num(duracion, " s", 1) if duracion is not None else "—"),
    ]

    size = None
    path = res.get("ruta")
    if path and os.path.exists(path):
        try:
            size = f"{os.path.getsize(path) / 2 ** 20:.1f} MB"
        except OSError:
            size = None
    partes.append(value("Size", size or "—"))

    partes.append(value("MD5 integrity", MD5_TEXT.get(md5.get("estado"), "—")))
    partes.append(value("Clipping", (f"{clip.get('runs_clip', 0)} clipping run(s) at full scale"
                                     if clip.get("hay_clipping") else "no clipping")
                        if clip else "—"))
    partes.append(value("Dynamic range", _num(res.get("dr"), " dB (estimated)", 1)))

    if bdi.get("aplica"):
        text_bits = (f"{LSBS_TEXT.get(bdi.get('lsbs'), '—')}"
                      f" · effective resolution "
                      f"{bdi.get('resolucion_efectiva_bits', '?')} of "
                      f"{meta.get('bits_per_sample', '?')} bits")
    else:
        text_bits = "not applicable (≤ 16 bits)"
    partes.append(value("LSB utilization", text_bits))

    if esp.get("error"):
        partes.append(value("Spectrum", f"not analyzed ({esp['error']})"))
    else:
        nyq = esp.get("nyquist") or 0
        partes.append(value("Spectral ceiling",
                            f"{_num(esp.get('techo_hz'), ' Hz', 0)}"
                            f" ({_pct(esp.get('techo_rel_nyquist'))} of Nyquist)"))
        if esp.get("corte_detectado"):
            category = CUTOFF_CLASS_TEXT.get(esp.get("clase_corte"), esp.get("clase_corte"))
            text_cutoff = (f"{_num(esp.get('frecuencia_corte'), ' Hz', 0)}"
                           f" ({_pct(esp.get('distancia_nyquist'))} of Nyquist) · {category}")
        else:
            text_cutoff = "no cut-off detected"
        partes.append(value("Spectral cut-off", text_cutoff))
        partes.append(value("High/mid ratio",
                            _num(esp.get("ratio"), "", 5)
                            if esp.get("ratio_aplica") else "not applicable"))
        partes.append(value("High-band variance", _num(esp.get("var_alta_db"), " dB²", 1)))
        partes.append(value("L-R separation", _num(esp.get("separacion_stereo"), "", 4)))
        partes.append(value("Nyquist", _num(nyq, " Hz", 0)))

    # 4-column grid: label, value, label, value. The width of the labels
    # (34 mm) keeps them from wrapping onto two lines on A4.
    rows = []
    for i in range(0, len(partes), 2):
        left = partes[i]
        right = partes[i + 1] if i + 1 < len(partes) else ["", ""]
        rows.append(left + right)
    anchos = [34 * mm, 50 * mm, 34 * mm, 50 * mm]
    return _table(rows, anchos, est,
                  [("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#DDDDDD")),
                   ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#FAFAFA")),
                   ("BACKGROUND", (2, 0), (2, -1), colors.HexColor("#FAFAFA"))])


def _badge(res, est):
    label, text_color, background_color = _style_verdict(res)
    if res.get("error"):
        text = label
    else:
        text = f"{label} &nbsp;·&nbsp; score {_pct(res.get('score'))}"
    cell = Paragraph(f'<font color="{text_color}"><b>{text}</b></font>', est["insignia"])
    return _table([[cell]], [172 * mm], est,
                  [("BACKGROUND", (0, 0), (-1, -1), colors.HexColor(background_color)),
                   ("BOX", (0, 0), (-1, -1), 0.6, colors.HexColor(text_color)),
                   ("TOPPADDING", (0, 0), (-1, -1), 4),
                   ("BOTTOMPADDING", (0, 0), (-1, -1), 4)])


def _spectrogram_image(res, temporal, width_max=None, height_max=None):
    """Returns (flowable, warning). The warning explains why there is no image.

    The PNG files weigh several hundred KB each; for the PDF they are rescaled and
    recompressed to JPEG inside a temporary directory (reportlab needs a file
    path). Without Pillow the original PNG is embedded: the report comes out the
    same either way.

    The sizes are computed INSIDE the function on purpose: `mm` comes from
    reportlab, and a default value in the signature would be evaluated when the
    module is imported, breaking the import when reportlab is not installed."""
    width_max = width_max or 170 * mm
    height_max  = height_max or 62 * mm
    path = (res.get("esp") or {}).get("espectrograma")
    if not path:
        return None, "spectrogram not generated (Pillow not available)"
    if not os.path.exists(path):
        return None, "the spectrogram file is no longer available"

    source = path
    try:
        from PIL import Image as ImagenPIL
        with ImagenPIL.open(path) as image:
            px_width, px_height = image.size
            copia = image.convert("RGB")
            copia.thumbnail((MAX_PX_SPECTROGRAM, MAX_PX_SPECTROGRAM))
            source = os.path.join(temporal, os.path.basename(path) + ".jpg")
            copia.save(source, format="JPEG", quality=88, optimize=True)
    except Exception:
        source = path                              # without Pillow: original PNG
        try:
            px_width, px_height = ImageReader(path).getSize()
        except Exception as e:
            return None, f"could not embed the spectrogram ({type(e).__name__})"

    altura = width_max * px_height / float(px_width)
    width = width_max
    if altura > height_max:                          # scaled down keeping the aspect ratio
        altura = height_max
        width = altura * px_width / float(px_height)
    return Image(source, width=width, height=altura), None


def _height_of(flowables):
    """Actual height those flowables will occupy inside the report frame."""
    total = 0.0
    for flowable in flowables:
        try:
            _, height = flowable.wrap(WIDTH_FRAME_MM * mm, HEIGHT_FRAME_MM * mm)
        except Exception:
            height = 0.0
        total += height + flowable.getSpaceBefore() + flowable.getSpaceAfter()
    return total


def _adjust_image(image, available):
    """Shrinks the image (down to HEIGHT_IMG_MIN_MM) so that it fits in what is left.

    Without this, a song page with many issues pushed the spectrogram onto the
    next page and left it loose at the top, separated from its heading."""
    if image is None:
        return None
    wanted = min(image.drawHeight, HEIGHT_IMG_MAX_MM * mm)
    height = max(HEIGHT_IMG_MIN_MM * mm, min(wanted, available))
    if height >= image.drawHeight - 0.5:
        return image
    proportion = height / float(image.drawHeight)
    return Image(image.filename, width=image.drawWidth * proportion, height=height)


def _song_card(index, total, res, est, temporal):
    """Page for one song."""
    element = [Paragraph(
        f"{index} of {total} &nbsp;·&nbsp; "
        f"{html.escape(str(res.get('archivo', '?')))}",
        est["cancion"]),
                _badge(res, est),
                Spacer(1, 3 * mm)]

    if res.get("error"):
        element.append(Paragraph(
            f"<b>Could not analyze:</b> {html.escape(str(res['error']))}",
            est["normal"]))
        element.append(Spacer(1, 3 * mm))

    element.append(_technical_table(res, est))
    element.append(Spacer(1, 4 * mm))

    problems = res.get("problemas") or []
    element.append(Paragraph("Detected issues", est["seccion"]))
    if problems:
        for problem in problems:
            element.append(Paragraph(
                f"• {html.escape(str(problem))}", est["problema"]))
    else:
        element.append(Paragraph("No issues detected.", est["normal"]))

    evidence = res.get("evidencias_forenses") or []
    # With a very long issue list the page budget is already at its limit: the
    # issues themselves are the evidence then, so the duplicate block is skipped.
    if evidence and len(problems) < 8:
        element.append(Spacer(1, 3 * mm))
        element.append(Paragraph("Forensic evidence", est["seccion"]))
        for evidence_item in evidence:
            element.append(Paragraph(
                f"• {html.escape(str(evidence_item))}", est["problema"]))
    element.append(Spacer(1, 4 * mm))

    # The image is measured and adjusted so that the heading and the spectrogram
    # always stay together on the same page. KeepTogether is the safety net for
    # the extreme case (an issue list that leaves no room even for the minimum).
    spectrum_title = Paragraph("Spectrogram", est["seccion"])
    image, warning = _spectrogram_image(res, temporal)
    image = _adjust_image(image,
                             HEIGHT_FRAME_MM * mm - _height_of(element + [spectrum_title])
                             - 2 * mm)
    if image is not None:
        element.append(KeepTogether([spectrum_title, image]))
    else:
        element.append(spectrum_title)
        element.append(Paragraph(
            f"(no image: {html.escape(str(warning))})", est["normal"]))
    return element


def _cover(album_path, results, mode, est):
    conteo, errors = _summary(results)
    total = len(results)
    rows = [[Paragraph("<b>Verdict</b>", est["celda"]),
              Paragraph("<b>Files</b>", est["celda"]),
              Paragraph("<b>Percentage</b>", est["celda"])]]
    comandos = [("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#DDDDDD")),
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#F0F0F0")),
                ("ALIGN", (1, 0), (-1, -1), "CENTER")]

    def row(label, text_color, count):
        return [Paragraph(f'<font color="{text_color}"><b>{label}</b></font>', est["celda"]),
                Paragraph(f'<font color="{text_color}"><b>{count}</b></font>', est["celda"]),
                Paragraph(f'<font color="{text_color}">'
                          f'{(count * 100 / total) if total else 0:.0f} %</font>', est["celda"])]

    for key, (label, text_color, background_color) in STYLE_VERDICT.items():
        if conteo.get(key):
            rows.append(row(label, text_color, conteo[key]))
            comandos.append(("BACKGROUND", (0, len(rows) - 1), (-1, len(rows) - 1),
                             colors.HexColor(background_color)))
    if errors:
        label, text_color, background_color = STYLE_ERROR
        rows.append(row(label, text_color, errors))
        comandos.append(("BACKGROUND", (0, len(rows) - 1), (-1, len(rows) - 1),
                         colors.HexColor(background_color)))
    rows.append([Paragraph("<b>TOTAL</b>", est["celda"]),
                  Paragraph(f"<b>{total}</b>", est["celda"]),
                  Paragraph("<b>100 %</b>", est["celda"])])
    comandos.append(("BACKGROUND", (0, len(rows) - 1), (-1, len(rows) - 1),
                     colors.HexColor("#F0F0F0")))

    element = [
        Paragraph("FLAC verification report", est["titulo"]),
        Paragraph("Fake lossless detection: lossy transcodes and "
                  "bit-depth upgrades.", est["subtitulo"]),
        Spacer(1, 6 * mm),
        Paragraph(f"<b>Album analyzed:</b> {html.escape(str(album_path))}",
                  est["normal"]),
        Paragraph(f"<b>Analysis date:</b> "
                  f"{time.strftime('%Y-%m-%d %H:%M:%S')}", est["normal"]),
        Paragraph(f"<b>Files analyzed:</b> {total}", est["normal"]),
    ]
    if mode:
        element.append(Paragraph(
            f"<b>Spectral analysis mode:</b> {html.escape(str(mode))}",
            est["normal"]))
    element += [Spacer(1, 6 * mm),
                 Paragraph("Executive summary", est["seccion"]),
                 _table(rows, [72 * mm, 30 * mm, 30 * mm], est, comandos),
                 Spacer(1, 4 * mm),
                 Paragraph("The verdict rests on four verifiable facts: "
                           "STREAMINFO MD5 integrity, actual LSB utilization, spectral "
                           "analysis with distance to Nyquist, and coherence between the declared "
                           "format and the content. A file with a confirmed defect "
                           "never receives the top verdict.", est["subtitulo"])]
    return element


def _final_table(results, est):
    rows = [[Paragraph("<b>#</b>", est["celda"]),
              Paragraph("<b>File</b>", est["celda"]),
              Paragraph("<b>Verdict</b>", est["celda"]),
              Paragraph("<b>Score</b>", est["celda"]),
              Paragraph("<b>Issues</b>", est["celda"])]]
    comandos = [("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#DDDDDD")),
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#F0F0F0")),
                ("ALIGN", (0, 0), (0, -1), "CENTER"),
                ("ALIGN", (3, 0), (4, -1), "CENTER")]
    for i, res in enumerate(results, start=1):
        label, text_color, background_color = _style_verdict(res)
        rows.append([Paragraph(str(i), est["celda"]),
                      Paragraph(html.escape(str(res.get("archivo", "?"))),
                                est["celda"]),
                      Paragraph(f'<font color="{text_color}"><b>{label}</b></font>',
                                est["celda"]),
                      Paragraph(_pct(res.get("score")), est["celda"]),
                      Paragraph("—" if res.get("error") else str(len(res.get("problemas") or [])),
                                est["celda"])])
        comandos.append(("BACKGROUND", (2, len(rows) - 1), (2, len(rows) - 1),
                         colors.HexColor(background_color)))
    return _table(rows, [10 * mm, 62 * mm, 52 * mm, 20 * mm, 22 * mm], est, comandos,
                  repeat_rows=1)


def _page_footer(canvas, doc, album, contador):
    """Footer with the album name and the page number."""
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


# ─── PUBLIC API ──────────────────────────────────────────────────────────────

class _LazyStream(MutableSequence):
    """Generator-backed sequence for reportlab's document builder.

    `BaseDocTemplate.build()` requires a *sequence*: it reads `flowables[0]`,
    calls `len(flowables)` and deletes from the front, so a plain generator is not
    accepted. This adapter materialises flowables on demand, so the report is not
    held as one huge list: reportlab pulls page by page and each page's flowables
    are released after it is drawn. `len()` only has to tell whether anything is
    left (the count is used only for progress reporting, which the report does not
    use)."""

    def __init__(self, generator):
        self._generator = generator
        self._buffer = []
        self._agotado = False

    def _rellenar(self, cuantos):
        while not self._agotado and len(self._buffer) < cuantos:
            try:
                self._buffer.append(next(self._generator))
            except StopIteration:
                self._agotado = True

    def __len__(self):
        self._rellenar(1)                    # just enough to know if there is one
        return len(self._buffer)

    def __getitem__(self, index):
        if isinstance(index, slice):
            if index.stop is not None:
                self._rellenar(index.stop)
            return self._buffer[index]
        self._rellenar(index + 1)
        return self._buffer[index]

    def __setitem__(self, index, value):
        if isinstance(index, slice):
            self._buffer[index] = list(value)
        else:
            self._rellenar(index + 1)
            self._buffer[index] = value

    def __delitem__(self, index):
        if isinstance(index, slice):
            if index.stop is not None:
                self._rellenar(index.stop)
            del self._buffer[index]
        else:
            self._rellenar(index + 1)
            del self._buffer[index]

    def insert(self, position, value):
        self._buffer.insert(position, value)


def _elements(analyzed_path, results, mode, est, temporal):
    """Yields the report flowables lazily: cover, one page per song, final table."""
    yield from _cover(analyzed_path, results, mode, est)
    for i, res in enumerate(results, start=1):
        yield PageBreak()
        yield from _song_card(i, len(results), res, est, temporal)
    yield PageBreak()
    yield Paragraph("Overall summary", est["seccion"])
    yield _final_table(results, est)


def generate(analyzed_path, results, destination=None, mode=None):
    """Writes the PDF report. NEVER raises.

    Returns {"ok": bool, "ruta": str|None, "error": str|None, "paginas": int}.
    """
    if not REPORTLAB_OK:
        return {"ok": False, "ruta": None, "paginas": 0,
                "error": "reportlab is not installed (pip install reportlab)"}
    if not results:
        return {"ok": False, "ruta": None, "paginas": 0,
                "error": "there are no results to include in the report"}
    # The direct API must honour the original track order even when the caller
    # passes a streamed/suspicious-last list. Missing indices keep input order.
    results = [r for _, r in sorted(
        enumerate(results),
        key=lambda par: par[1].get("indice_original", par[0]))]

    destination = destination or report_path(analyzed_path)
    est = _estilos()
    contador = {"paginas": 0}
    temporal = None
    try:
        # The recompressed images live here and are deleted when the build ends.
        temporal = tempfile.mkdtemp(prefix="flac_verifier_report_")
        document = SimpleDocTemplate(
            destination, pagesize=A4,
            leftMargin=20 * mm, rightMargin=20 * mm,
            topMargin=18 * mm, bottomMargin=20 * mm,
            title="FLAC verification report",
            author="FLAC VERIFIER",
            subject=f"Verification of {len(results)} file(s)")

        footer = lambda lienzo, doc: _page_footer(lienzo, doc, os.path.basename(
            os.path.dirname(destination) or destination), contador)

        document.build(
            _LazyStream(_elements(analyzed_path, results, mode, est, temporal)),
            onFirstPage=footer, onLaterPages=footer)
        pages = contador["paginas"]
        return {"ok": True, "ruta": destination, "error": None, "paginas": pages}
    except Exception as e:
        return {"ok": False, "ruta": None, "paginas": 0,
                "error": f"{type(e).__name__}: {e}"}
    finally:
        if temporal:
            shutil.rmtree(temporal, ignore_errors=True)
