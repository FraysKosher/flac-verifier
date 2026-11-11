"""PDF report per album.

The tests are not satisfied with the file existing: they decompress the PDF
streams and check the TEXT it contains, the number of pages and that every song
appears. PyMuPDF is used only to verify; it is not a dependency of the project.
"""
import base64
import json
import os
import re
import subprocess
import sys
import unittest
import zlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fixtures as fx

import motor_flac as engine
import informe_pdf as pdf


def _unescape(stream):
    """Resolves the escapes of PDF strings: \\(  \\)  \\\\  and octal \\ddd."""
    def replacement(found):
        body = found.group(1)
        if body[:1] in b"01234567":          # only 0-7 are octal digits
            return bytes([int(body[:3], 8) & 0xFF])
        return body

    return re.sub(rb"\\([0-7]{1,3}|.)", replacement, stream)


def _a_ascii85(raw):
    """Decodes ASCII85 the way PDF's ASCII85Decode does (without delimiters)."""
    end = raw.find(b"~>")
    return base64.a85decode(raw[:end] if end >= 0 else raw, adobe=False,
                            ignorechars=b" \t\r\n\f\v")


def _decode_stream(raw, filters):
    """Applies the filters declared in the object (reportlab uses ASCII85+Flate)."""
    payload = raw
    if b"ASCII85Decode" in filters:
        payload = _a_ascii85(payload)
    if b"FlateDecode" in filters:
        payload = zlib.decompress(payload)
    elif not filters:
        for attempt in (lambda d: zlib.decompress(_a_ascii85(d)),
                        lambda d: zlib.decompress(d),
                        _a_ascii85):
            try:
                return attempt(payload)
            except Exception:
                continue
    return payload


def text_pdf(path):
    """Text contained in the PDF streams, honouring their filters."""
    with open(path, "rb") as f:
        payload = f.read()
    partes = []
    for found in re.finditer(rb"stream\r?\n", payload):
        start = found.end()
        end = payload.find(b"endstream", start)
        if end < 0:
            continue
        # the object dictionary is just before the word 'stream'
        header = payload[max(0, found.start() - 400) : found.start()]
        filters = b" ".join(re.findall(rb"/(ASCII85Decode|FlateDecode)", header))
        try:
            partes.append(_decode_stream(payload[start:end], filters))
        except Exception:
            continue                               # a stream that is not content
    return _unescape(b"".join(partes)).decode("latin-1", "replace")


def pages_pdf(path):
    """Number of pages, read from the PDF page tree."""
    with open(path, "rb") as f:
        payload = f.read()
    counts = re.findall(rb"/Count\s+(\d+)", payload)
    return max(int(c) for c in counts) if counts else 0


def _contrast(foreground, background):
    """WCAG contrast ratio between two #RRGGBB colours."""
    def luminance(colour):
        channels = [int(colour[index:index + 2], 16) / 255 for index in (1, 3, 5)]
        linear = [value / 12.92 if value <= 0.04045
                  else ((value + 0.055) / 1.055) ** 2.4 for value in channels]
        return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

    first, second = luminance(foreground), luminance(background)
    high, low = max(first, second), min(first, second)
    return (high + 0.05) / (low + 0.05)


def _fitz_available():
    try:
        import fitz                                    # noqa: F401
        return True
    except ImportError:
        return False


def analyze_album(folder, save_png=True):
    return [engine.analyze_file_data(os.path.join(folder, name), "center", None,
                                         save_png)
            for name in sorted(os.listdir(folder))
            if name.lower().endswith(".flac")]


@unittest.skipUnless(pdf.available(), "reportlab not installed")
class test_report_pdf(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.fx = fx.Fixtures()
        cls.album = cls.fx.subfolder(
            "album", ("ok16", "ok24", "master_cd_20k", "corte_16k", "basura_magic"))

    @classmethod
    def tearDownClass(cls):
        cls.fx.close()

    # ── generation and location ─────────────────────────────────────────────
    def test_generates_report_folder_album(self):
        report = pdf.generate(self.album, analyze_album(self.album))
        self.assertTrue(report["ok"], report.get("error"))
        self.assertEqual(os.path.dirname(report["ruta"]), os.path.abspath(self.album))
        self.assertEqual(os.path.basename(report["ruta"]), pdf.REPORT_NAME)
        with open(report["ruta"], "rb") as f:
            payload = f.read()
        self.assertTrue(payload.startswith(b"%PDF"))
        self.assertGreater(len(payload), 20 * 1024, "the PDF is suspiciously small")

    def test_only_file_saves_pdf_next_to(self):
        path = self.fx["ok16"]
        expected = os.path.join(os.path.dirname(os.path.abspath(path)), pdf.REPORT_NAME)
        self.assertEqual(pdf.report_path(path), expected)
        result = engine.analyze_file_data(path, "center", None, False)
        report = pdf.generate(path, [result])
        self.assertTrue(report["ok"], report.get("error"))
        self.assertEqual(report["ruta"], expected)
        self.assertTrue(os.path.exists(expected))
        os.remove(expected)

    # ── document structure ──────────────────────────────────────────────────
    def test_page_by_song_more_cover_summary(self):
        results = analyze_album(self.album)
        report = pdf.generate(self.album, results)
        self.assertTrue(report["ok"], report.get("error"))
        self.assertEqual(report["paginas"], len(results) + 2)
        self.assertEqual(pages_pdf(report["ruta"]), len(results) + 2)

    def test_text_contains_cover_songs_summary(self):
        results = analyze_album(self.album)
        report = pdf.generate(self.album, results)
        text = text_pdf(report["ruta"])
        self.assertIn("FLAC verification report", text)     # cover page, with an accent
        self.assertIn("Executive summary", text)
        self.assertIn("Overall summary", text)
        self.assertIn("Analysis date", text)
        for result in results:
            with self.subTest(file=result["archivo"]):
                self.assertIn(result["archivo"], text)
        verdicts = {r.get("veredicto") for r in results if r.get("veredicto")}
        self.assertTrue(verdicts)
        for verdict in verdicts:
            self.assertIn(pdf.STYLE_VERDICT[verdict][0], text)

    def test_songs_with_error_appear_marked(self):
        results = analyze_album(self.album)
        self.assertTrue([r for r in results if r.get("error")],
                        "the corrupt fixture should fail")
        report = pdf.generate(self.album, results)
        text = text_pdf(report["ruta"])
        self.assertIn(pdf.STYLE_ERROR[0], text)
        self.assertIn("Could not analyze", text)

    def test_tables_technical_include_data_key(self):
        report = pdf.generate(self.album, analyze_album(self.album))
        text = text_pdf(report["ruta"])
        for label in ("Format", "Duration", "Size", "MD5 integrity", "Clipping",
                         "Dynamic range", "LSB utilization", "Spectral ceiling",
                         "Spectral cut-off", "High/mid ratio", "Nyquist"):
            with self.subTest(label=label):
                self.assertIn(label, text)
        self.assertIn("verified (match)", text)            # MD5 state
        self.assertIn("Detected issues", text)

    # ── print palette ───────────────────────────────────────────────────────
    def test_palette_comes_from_the_design_tokens(self):
        """No hard-coded colours: the report reads the light tokens of theme.py."""
        import theme
        self.assertEqual(pdf.PAPER, theme.color("bg.surface", "light"))
        self.assertEqual(pdf.INK, theme.color("text.primary", "light"))
        self.assertEqual(pdf.RULE, theme.color("border.subtle", "light"))
        for label, text, background, border in (list(pdf.STYLE_VERDICT.values())
                                                + [pdf.STYLE_ERROR]):
            with self.subTest(verdict=label):
                self.assertNotEqual(text.upper(), "#000000",
                                    "pure black text is an anti-pattern")
                self.assertGreaterEqual(_contrast(text, background), 4.5,
                                        "the verdict text is not readable")
                self.assertNotEqual(border, background)

    def test_cover_shows_the_brand_and_the_footer_the_engine_version(self):
        results = analyze_album(self.album)
        report = pdf.generate(self.album, results)
        self.assertTrue(report["ok"], report.get("error"))
        text = text_pdf(report["ruta"])
        self.assertIn("FLAC VERIFIER", text)                       # wordmark
        self.assertIn("Authenticity and lossless verification", text)
        self.assertIn("engine 13.0", text)                         # footer
        self.assertIsNotNone(pdf._logo_image(), "the logo is missing")

    def test_every_style_is_readable_on_paper(self):
        """Small labels need 4.5:1 on paper; large bold text needs 3:1 (DESIGN.md 9)."""
        for name, style in pdf._styles().items():
            colour = style.textColor
            if colour is None:
                continue
            rendered = "#%02X%02X%02X" % (round(colour.red * 255),
                                          round(colour.green * 255),
                                          round(colour.blue * 255))
            bold = "Bold" in (style.fontName or "")
            minimum = 3.0 if (style.fontSize >= 14 and bold) else 4.5
            with self.subTest(style=name, size=style.fontSize):
                self.assertGreaterEqual(
                    _contrast(rendered, pdf.PAPER), minimum,
                    f"{name} ({rendered}) is not readable on the paper")

    def test_missing_spectrogram_says_why(self):
        """Three different reasons must not share one vague message."""
        clean = {"archivo": "ok.flac", "ruta": self.fx["ok16"],
                 "veredicto": "GENUINE LOSSLESS", "score": 0.9, "problemas": [],
                 "evidencias_forenses": [], "meta": {}, "esp": {}, "bdi": {},
                 "md5": {}, "clip": {}}
        broken = {"archivo": "broken.flac", "error": "Not a valid FLAC signature"}
        destination = os.path.join(self.fx.dir, "sin_png.pdf")
        report = pdf.generate(self.album, [clean, broken], destination=destination)
        self.assertTrue(report["ok"], report.get("error"))
        text = text_pdf(destination)
        self.assertIn("not generated (run with the PDF report or --png)", text)
        self.assertIn("the file could not be analyzed", text)
        self.assertIn("No issues detected.", text)
        self.assertIn("Not analyzed: no issues were evaluated.", text)
        # A record that failed is never described as clean.
        self.assertEqual(text.count("No issues detected."), 1)

    def test_styles_never_fall_back_to_black(self):
        for name, style in pdf._styles().items():
            colour = style.textColor
            if colour is None:
                continue
            rendered = "#%02X%02X%02X" % (round(colour.red * 255),
                                          round(colour.green * 255),
                                          round(colour.blue * 255))
            with self.subTest(style=name):
                self.assertNotEqual(rendered.upper(), "#000000")

    # ── spectrograms ────────────────────────────────────────────────────────
    def test_report_embeds_spectrograms(self):
        results = analyze_album(self.album, save_png=True)
        with_image = [r for r in results if (r.get("esp") or {}).get("espectrograma")]
        self.assertTrue(with_image)
        report = pdf.generate(self.album, results)
        self.assertTrue(report["ok"], report.get("error"))
        text = text_pdf(report["ruta"])
        self.assertIn("Spectrogram", text)
        # the "no image" notice can only appear in the records with no PNG
        without_png = sum(1 for r in results if not (r.get("esp") or {}).get("espectrograma"))
        self.assertEqual(text.count("no image"), without_png)

    def test_characters_special_xml_path_names_no_break_pdf(self):
        """Dynamic text is XML-escaped before ReportLab parses it."""
        album = "AC & DC - <Live> [Deluxe]"
        result = {
            "archivo": "01. Rock & Roll <Intro> & Outro.flac",
            "ruta": self.fx["ok16"],
            "veredicto": "SUSPICIOUS",
            "score": 0.5,
            "problemas": ["problem <tag> & more"],
            "evidencias_forenses": ["evidence <b> & value"],
            "meta": {"sample_rate": 44100, "bits_per_sample": 16,
                     "canales": 2, "duracion": 2.0},
            "esp": {}, "bdi": {}, "md5": {}, "clip": {},
        }
        destination = os.path.join(self.fx.dir, "special_chars.pdf")
        report = pdf.generate(album, [result], destination=destination)
        self.assertTrue(report["ok"], report.get("error"))
        self.assertTrue(os.path.exists(destination))
        self.assertGreater(os.path.getsize(destination), 4 * 1024)

    @unittest.skipUnless(_fitz_available(), "PyMuPDF not installed")
    def test_generate_direct_sorts_by_index_original(self):
        """The direct API restores track order before writing the pages."""
        import fitz

        base = {"ruta": self.fx["ok16"], "veredicto": "GENUINE LOSSLESS",
                "score": 1.0, "problemas": [], "evidencias_forenses": [],
                "meta": {"sample_rate": 44100, "bits_per_sample": 16,
                         "canales": 2, "duracion": 2.0},
                "esp": {}, "bdi": {}, "md5": {}, "clip": {}}
        desordenados = []
        for index in (2, 0, 1):
            card = dict(base)
            card["archivo"] = f"track_{index}.flac"
            card["indice_original"] = index
            desordenados.append(card)

        destination = os.path.join(self.fx.dir, "orden_directo.pdf")
        report = pdf.generate(self.album, desordenados, destination=destination)
        self.assertTrue(report["ok"], report.get("error"))
        document = fitz.open(destination)
        try:
            self.assertEqual(document.page_count, 5)   # cover + 3 + summary
            for position, index in enumerate((0, 1, 2)):
                text = document[1 + position].get_text()
                self.assertIn(f"track_{index}.flac", text)
        finally:
            document.close()

    def test_images_do_not_inflate_pdf(self):
        """The PNGs are recompressed: the PDF weighs noticeably less than its originals."""
        try:
            import PIL                                     # noqa: F401
            from PIL import Image as _
        except ImportError:
            self.skipTest("Pillow not installed: the original PNG is embedded")
        results = analyze_album(self.album, save_png=True)
        originales = [r["esp"]["espectrograma"] for r in results
                      if (r.get("esp") or {}).get("espectrograma")]
        self.assertTrue(originales)
        png_size = sum(os.path.getsize(r) for r in originales if os.path.exists(r))
        report = pdf.generate(self.album, results)
        self.assertTrue(report["ok"], report.get("error"))
        pdf_size = os.path.getsize(report["ruta"])
        self.assertLess(pdf_size, png_size * 0.6,
                        f"the PDF ({pdf_size/1024:.0f} KB) does not take advantage of the "
                        f"recompression of the PNGs ({png_size/1024:.0f} KB)")

    def test_without_spectrograms_report_generates_equal(self):
        """Without PNGs (for example without matplotlib) the PDF still comes out, with the notice."""
        results = analyze_album(self.album, save_png=False)
        report = pdf.generate(self.album, results)
        self.assertTrue(report["ok"], report.get("error"))
        self.assertGreater(os.path.getsize(report["ruta"]), 8 * 1024)
        text = text_pdf(report["ruta"])
        self.assertIn("Spectrogram", text)
        self.assertIn("(no image:", text)

    def test_image_deleted_after_analysis_no_breaks_report(self):
        results = analyze_album(self.album, save_png=True)
        deleted = 0
        for result in results:
            path = (result.get("esp") or {}).get("espectrograma")
            if path and os.path.exists(path):
                os.remove(path)
                deleted += 1
        self.assertGreater(deleted, 0)
        report = pdf.generate(self.album, results)
        self.assertTrue(report["ok"], report.get("error"))
        self.assertIn("is no longer available", text_pdf(report["ruta"]))

    # ── layout: the record must not overflow its page ───────────────────────
    def _result_with_problems(self, how_many):
        """A real result with a list of long issues."""
        base = [r for r in analyze_album(self.album, save_png=True)
                if (r.get("esp") or {}).get("espectrograma")][0]
        copy = dict(base)
        copy["esp"] = dict(base["esp"])
        copy["problemas"] = [
            f"{i + 1}) declares 24 bits at 44.1 kHz but its high band is cut at "
            f"16.4 kHz: the declared resolution is not backed by the content"
            for i in range(how_many)]
        return copy

    def test_card_with_many_problems_no_overflows_page(self):
        """Before, 12 issues pushed the spectrogram onto the next page
        (the record went from 1 page to 2). The image shrinks so that it fits."""
        clean = self.fx.subfolder("maqueta_larga", ("ok16",))
        destination = os.path.join(clean, "informe_largo.pdf")
        report = pdf.generate(clean, [self._result_with_problems(12)], destination=destination)
        self.assertTrue(report["ok"], report.get("error"))
        # cover + 1 song + summary: if the record overflowed it would be 4
        self.assertEqual(report["paginas"], 3)
        self.assertEqual(pages_pdf(destination), 3)

    def test_card_normal_still_using_image_size_full(self):
        results = analyze_album(self.album, save_png=True)
        report = pdf.generate(self.album, results)
        self.assertEqual(report["paginas"], len(results) + 2)
        self.assertTrue(os.path.exists(report["ruta"]))

    @unittest.skipUnless(_fitz_available(), "PyMuPDF not installed")
    def test_title_image_never_separate_page(self):
        """The symptom that was fixed: a loose image at the top of the next page,
        far from its title."""
        import fitz
        clean = self.fx.subfolder("maqueta_separa", ("ok16",))
        for how_many in (2, 6, 12, 20):
            with self.subTest(problems=how_many):
                destination = os.path.join(clean, f"informe_{how_many}.pdf")
                report = pdf.generate(clean, [self._result_with_problems(how_many)],
                                      destination=destination)
                self.assertTrue(report["ok"], report.get("error"))
                document = fitz.open(destination)
                try:
                    with_title = [i + 1 for i, p in enumerate(document)
                                  if "Spectrogram" in p.get_text()]
                    # The cover now carries the logo, so only the song pages
                    # count for this pairing.
                    with_image = [i + 1 for i, p in enumerate(document)
                                  if i > 0 and any(p.get_image_rects(im[0])
                                                   for im in p.get_images(full=True))]
                    self.assertEqual(with_title, with_image,
                                     "the title and its image ended up on different pages")
                    for i, page in enumerate(document, start=1):
                        for im in page.get_images(full=True):
                            for rect in page.get_image_rects(im[0]):
                                self.assertGreaterEqual(rect.y0, 10 * 2.8346)
                                self.assertLessEqual(rect.y1, (297 - 8) * 2.8346)
                finally:
                    document.close()
                os.remove(destination)

    # ── error handling: it never aborts ─────────────────────────────────────
    def test_without_reportlab_warns_without_generate_nothing(self):
        clean = self.fx.subfolder("album_sin_reportlab", ("ok16",))
        original = pdf.REPORTLAB_OK
        pdf.REPORTLAB_OK = False
        try:
            report = pdf.generate(clean, [{"archivo": "x.flac"}])
            self.assertFalse(report["ok"])
            self.assertIn("reportlab", report["error"])
            self.assertIsNone(report["ruta"])
            self.assertFalse(os.path.exists(pdf.report_path(clean)))
        finally:
            pdf.REPORTLAB_OK = original

    def test_destination_impossible_warns_without_raise(self):
        results = analyze_album(self.album, save_png=False)
        destination = os.path.join(self.fx["ok16"], pdf.REPORT_NAME)   # a file
        report = pdf.generate(self.album, results, destination=destination)
        self.assertFalse(report["ok"])
        self.assertIsNone(report["ruta"])
        self.assertTrue(report["error"])

    def test_without_results_warns(self):
        report = pdf.generate(self.album, [])
        self.assertFalse(report["ok"])
        self.assertIn("there are no results", report["error"].lower())

    def test_failure_generator_no_aborts_analysis(self):
        """The engine turns any issue in the report into a notice."""
        probe = self.fx.subfolder("album_roto", ("ok16",))
        # the report destination is a DIRECTORY: writing into it is bound to fail
        os.makedirs(os.path.join(probe, pdf.REPORT_NAME), exist_ok=True)
        environment = {k: v for k, v in os.environ.items() if k != "PYTHONIOENCODING"}
        p = subprocess.run([sys.executable, fx.CLI], input=f"2\nn\ny\n{probe}\nn\n",
                           cwd=fx.ROOT, capture_output=True, text=True,
                           encoding="utf-8", timeout=900, env=environment)
        self.assertEqual(p.returncode, 0, p.stderr[-1500:])
        self.assertNotIn("Traceback", p.stderr or "")
        self.assertIn("Could not generate the PDF report", p.stdout)
        self.assertIn("Summary: 1", p.stdout.replace("SUMMARY:", "Summary:"))

    # ── integration with the CLIs ───────────────────────────────────────────
    def test_engine_emits_event_report_before_end(self):
        probe = self.fx.subfolder("album_cli", ("ok16", "corte_16k"))
        p = fx.run_engine(probe, extra=("--pdf",))
        events = [json.loads(l) for l in p.stdout.splitlines() if l.strip()]
        tipos = [e["tipo"] for e in events]
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("informe", tipos)
        self.assertEqual(tipos[-1], "fin")
        self.assertLess(tipos.index("informe"), tipos.index("fin"))
        event = events[tipos.index("informe")]
        self.assertEqual(os.path.dirname(event["ruta"]), os.path.abspath(probe))
        self.assertTrue(os.path.exists(event["ruta"]))
        self.assertEqual(event["paginas"], 4)              # cover + 2 + summary
        # --pdf generates the spectrograms even when --png is not requested
        for result in (e for e in events if e["tipo"] == "resultado"):
            if not result.get("error"):
                self.assertIsNotNone(result["esp"].get("espectrograma"))

    def test_without_pdf_no_emits_event_report(self):
        probe = self.fx.subfolder("album_sin_pdf", ("ok16",))
        p = fx.run_engine(probe)
        tipos = [json.loads(l)["tipo"] for l in p.stdout.splitlines() if l.strip()]
        self.assertNotIn("informe", tipos)
        self.assertFalse(os.path.exists(os.path.join(probe, pdf.REPORT_NAME)))

    def test_cli_interactive_also_generates_report(self):
        probe = self.fx.subfolder("album_interactivo", ("ok16", "ok24"))
        environment = {k: v for k, v in os.environ.items() if k != "PYTHONIOENCODING"}
        p = subprocess.run([sys.executable, fx.CLI], input=f"2\nn\ny\n{probe}\nn\n",
                           cwd=fx.ROOT, capture_output=True, text=True,
                           encoding="utf-8", timeout=900, env=environment)
        self.assertEqual(p.returncode, 0, p.stderr[-1500:])
        self.assertIn("PDF report:", p.stdout)
        self.assertTrue(os.path.exists(os.path.join(probe, pdf.REPORT_NAME)))

    def test_pdf_png_are_combinable(self):
        probe = self.fx.subfolder("album_combinado", ("ok16",))
        p = fx.run_engine(probe, extra=("--png", "--pdf"))
        tipos = [json.loads(l)["tipo"] for l in p.stdout.splitlines() if l.strip()]
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("informe", tipos)
        self.assertTrue(os.path.isdir(os.path.join(probe, "_spectrograms")))
        self.assertTrue(os.path.exists(os.path.join(probe, pdf.REPORT_NAME)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
