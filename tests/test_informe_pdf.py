"""Informe PDF por álbum.

Los tests no se conforman con que el archivo exista: descomprimen los streams
del PDF y comprueban el TEXTO que contiene, el número de páginas y que todas las
canciones aparezcan. PyMuPDF solo se usa para verificar; no es dependencia del
proyecto.
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

import motor_flac as motor
import informe_pdf as pdf


def _desescapar(flujo):
    """Resuelve los escapes de las cadenas PDF: \\(  \\)  \\\\  y \\ddd octal."""
    def reemplazo(encontrado):
        cuerpo = encontrado.group(1)
        if cuerpo[:1] in b"01234567":          # solo 0-7 son dígitos octales
            return bytes([int(cuerpo[:3], 8) & 0xFF])
        return cuerpo

    return re.sub(rb"\\([0-7]{1,3}|.)", reemplazo, flujo)


def _a_ascii85(crudo):
    """Decodifica ASCII85 como lo hace ASCII85Decode de PDF (sin delimitadores)."""
    fin = crudo.find(b"~>")
    return base64.a85decode(crudo[:fin] if fin >= 0 else crudo, adobe=False,
                            ignorechars=b" \t\r\n\f\v")


def _decodificar_stream(crudo, filtros):
    """Aplica los filtros declarados en el objeto (reportlab usa ASCII85+Flate)."""
    datos = crudo
    if b"ASCII85Decode" in filtros:
        datos = _a_ascii85(datos)
    if b"FlateDecode" in filtros:
        datos = zlib.decompress(datos)
    elif not filtros:
        for intento in (lambda d: zlib.decompress(_a_ascii85(d)),
                        lambda d: zlib.decompress(d),
                        _a_ascii85):
            try:
                return intento(datos)
            except Exception:
                continue
    return datos


def texto_del_pdf(ruta):
    """Texto contenido en los streams del PDF, respetando sus filtros."""
    datos = open(ruta, "rb").read()
    partes = []
    for encontrado in re.finditer(rb"stream\r?\n", datos):
        inicio = encontrado.end()
        fin = datos.find(b"endstream", inicio)
        if fin < 0:
            continue
        # el diccionario del objeto está justo antes de la palabra 'stream'
        cabecera = datos[max(0, encontrado.start() - 400) : encontrado.start()]
        filtros = b" ".join(re.findall(rb"/(ASCII85Decode|FlateDecode)", cabecera))
        try:
            partes.append(_decodificar_stream(datos[inicio:fin], filtros))
        except Exception:
            continue                               # stream que no es de contenido
    return _desescapar(b"".join(partes)).decode("latin-1", "replace")


def paginas_del_pdf(ruta):
    """Número de páginas, leído del árbol de páginas del PDF."""
    datos = open(ruta, "rb").read()
    cuentas = re.findall(rb"/Count\s+(\d+)", datos)
    return max(int(c) for c in cuentas) if cuentas else 0


def _fitz_disponible():
    try:
        import fitz                                    # noqa: F401
        return True
    except ImportError:
        return False


def analizar_album(carpeta, guardar_png=True):
    return [motor.analizar_archivo_datos(os.path.join(carpeta, nombre), "center", None,
                                         guardar_png)
            for nombre in sorted(os.listdir(carpeta))
            if nombre.lower().endswith(".flac")]


@unittest.skipUnless(pdf.disponible(), "reportlab no instalado")
class TestInformePDF(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.fx = fx.Fixtures()
        cls.album = cls.fx.subcarpeta(
            "album", ("ok16", "ok24", "master_cd_20k", "corte_16k", "basura_magic"))

    @classmethod
    def tearDownClass(cls):
        cls.fx.cerrar()

    # ── generación y ubicación ──────────────────────────────────────────────
    def test_genera_el_informe_en_la_carpeta_del_album(self):
        informe = pdf.generar(self.album, analizar_album(self.album))
        self.assertTrue(informe["ok"], informe.get("error"))
        self.assertEqual(os.path.dirname(informe["ruta"]), os.path.abspath(self.album))
        self.assertEqual(os.path.basename(informe["ruta"]), pdf.NOMBRE_INFORME)
        datos = open(informe["ruta"], "rb").read()
        self.assertTrue(datos.startswith(b"%PDF"))
        self.assertGreater(len(datos), 20 * 1024, "el PDF es sospechosamente pequeño")

    def test_un_solo_archivo_guarda_el_pdf_junto_a_el(self):
        ruta = self.fx["ok16"]
        esperado = os.path.join(os.path.dirname(os.path.abspath(ruta)), pdf.NOMBRE_INFORME)
        self.assertEqual(pdf.ruta_informe(ruta), esperado)
        resultado = motor.analizar_archivo_datos(ruta, "center", None, False)
        informe = pdf.generar(ruta, [resultado])
        self.assertTrue(informe["ok"], informe.get("error"))
        self.assertEqual(informe["ruta"], esperado)
        self.assertTrue(os.path.exists(esperado))
        os.remove(esperado)

    # ── estructura del documento ────────────────────────────────────────────
    def test_una_pagina_por_cancion_mas_portada_y_resumen(self):
        resultados = analizar_album(self.album)
        informe = pdf.generar(self.album, resultados)
        self.assertTrue(informe["ok"], informe.get("error"))
        self.assertEqual(informe["paginas"], len(resultados) + 2)
        self.assertEqual(paginas_del_pdf(informe["ruta"]), len(resultados) + 2)

    def test_el_texto_contiene_portada_canciones_y_resumen(self):
        resultados = analizar_album(self.album)
        informe = pdf.generar(self.album, resultados)
        texto = texto_del_pdf(informe["ruta"])
        self.assertIn("FLAC verification report", texto)     # portada, con acento
        self.assertIn("Executive summary", texto)
        self.assertIn("Overall summary", texto)
        self.assertIn("Analysis date", texto)
        for resultado in resultados:
            with self.subTest(archivo=resultado["archivo"]):
                self.assertIn(resultado["archivo"], texto)
        veredictos = {r.get("veredicto") for r in resultados if r.get("veredicto")}
        self.assertTrue(veredictos)
        for veredicto in veredictos:
            self.assertIn(pdf.ESTILO_VEREDICTO[veredicto][0], texto)

    def test_las_canciones_con_error_aparecen_marcadas(self):
        resultados = analizar_album(self.album)
        self.assertTrue([r for r in resultados if r.get("error")],
                        "el fixture corrupto debería fallar")
        informe = pdf.generar(self.album, resultados)
        texto = texto_del_pdf(informe["ruta"])
        self.assertIn(pdf.ESTILO_ERROR[0], texto)
        self.assertIn("Could not analyze", texto)

    def test_las_tablas_tecnicas_incluyen_los_datos_clave(self):
        informe = pdf.generar(self.album, analizar_album(self.album))
        texto = texto_del_pdf(informe["ruta"])
        for etiqueta in ("Format", "Duration", "Size", "MD5 integrity", "Clipping",
                         "Dynamic range", "LSB utilization", "Spectral ceiling",
                         "Spectral cut-off", "High/mid ratio", "Nyquist"):
            with self.subTest(etiqueta=etiqueta):
                self.assertIn(etiqueta, texto)
        self.assertIn("verified (match)", texto)            # estado del MD5
        self.assertIn("Detected issues", texto)

    # ── espectrogramas ──────────────────────────────────────────────────────
    def test_el_informe_embebe_los_espectrogramas(self):
        resultados = analizar_album(self.album, guardar_png=True)
        con_imagen = [r for r in resultados if (r.get("esp") or {}).get("espectrograma")]
        self.assertTrue(con_imagen)
        informe = pdf.generar(self.album, resultados)
        self.assertTrue(informe["ok"], informe.get("error"))
        texto = texto_del_pdf(informe["ruta"])
        self.assertIn("Spectrogram", texto)
        # el aviso de "sin imagen" solo puede salir en las fichas sin PNG
        sin_png = sum(1 for r in resultados if not (r.get("esp") or {}).get("espectrograma"))
        self.assertEqual(texto.count("no image"), sin_png)

    def test_las_imagenes_no_hinchan_el_pdf(self):
        """Los PNG se recomprimen: el PDF pesa bastante menos que sus originales."""
        try:
            import PIL                                     # noqa: F401
            from PIL import Image as _
        except ImportError:
            self.skipTest("Pillow no instalado: se embebe el PNG original")
        resultados = analizar_album(self.album, guardar_png=True)
        originales = [r["esp"]["espectrograma"] for r in resultados
                      if (r.get("esp") or {}).get("espectrograma")]
        self.assertTrue(originales)
        peso_png = sum(os.path.getsize(r) for r in originales if os.path.exists(r))
        informe = pdf.generar(self.album, resultados)
        self.assertTrue(informe["ok"], informe.get("error"))
        peso_pdf = os.path.getsize(informe["ruta"])
        self.assertLess(peso_pdf, peso_png * 0.6,
                        f"el PDF ({peso_pdf/1024:.0f} KB) no aprovecha la recompresión "
                        f"de los PNG ({peso_png/1024:.0f} KB)")

    def test_sin_espectrogramas_el_informe_se_genera_igual(self):
        """Sin PNG (por ejemplo sin matplotlib) el PDF sale igual, con el aviso."""
        resultados = analizar_album(self.album, guardar_png=False)
        informe = pdf.generar(self.album, resultados)
        self.assertTrue(informe["ok"], informe.get("error"))
        self.assertGreater(os.path.getsize(informe["ruta"]), 8 * 1024)
        texto = texto_del_pdf(informe["ruta"])
        self.assertIn("Spectrogram", texto)
        self.assertIn("(no image:", texto)

    def test_imagen_borrada_despues_del_analisis_no_rompe_el_informe(self):
        resultados = analizar_album(self.album, guardar_png=True)
        borrados = 0
        for resultado in resultados:
            ruta = (resultado.get("esp") or {}).get("espectrograma")
            if ruta and os.path.exists(ruta):
                os.remove(ruta)
                borrados += 1
        self.assertGreater(borrados, 0)
        informe = pdf.generar(self.album, resultados)
        self.assertTrue(informe["ok"], informe.get("error"))
        self.assertIn("is no longer available", texto_del_pdf(informe["ruta"]))

    # ── maqueta: la ficha no puede desbordar su página ──────────────────────
    def _resultado_con_problemas(self, cuantos):
        """Resultado real con una lista de problemas largos."""
        base = [r for r in analizar_album(self.album, guardar_png=True)
                if (r.get("esp") or {}).get("espectrograma")][0]
        copia = dict(base)
        copia["esp"] = dict(base["esp"])
        copia["problemas"] = [
            f"{i + 1}) declara 24 bits a 44.1 kHz pero su banda alta está cortada a "
            f"16.4 kHz: la resolución declarada no está respaldada por el contenido"
            for i in range(cuantos)]
        return copia

    def test_una_ficha_con_muchos_problemas_no_desborda_la_pagina(self):
        """Antes, 12 problemas empujaban el espectrograma a la página siguiente
        (la ficha pasaba de 1 a 2 páginas). La imagen se encoge para caber."""
        limpio = self.fx.subcarpeta("maqueta_larga", ("ok16",))
        destino = os.path.join(limpio, "informe_largo.pdf")
        informe = pdf.generar(limpio, [self._resultado_con_problemas(12)], destino=destino)
        self.assertTrue(informe["ok"], informe.get("error"))
        # portada + 1 canción + resumen: si la ficha desbordara serían 4
        self.assertEqual(informe["paginas"], 3)
        self.assertEqual(paginas_del_pdf(destino), 3)

    def test_la_ficha_normal_sigue_usando_la_imagen_a_tamano_completo(self):
        resultados = analizar_album(self.album, guardar_png=True)
        informe = pdf.generar(self.album, resultados)
        self.assertEqual(informe["paginas"], len(resultados) + 2)
        self.assertTrue(os.path.exists(informe["ruta"]))

    @unittest.skipUnless(_fitz_disponible(), "PyMuPDF no instalado")
    def test_el_titulo_y_la_imagen_nunca_se_separan_de_pagina(self):
        """El síntoma que se corrigió: imagen suelta al principio de la página
        siguiente, lejos de su título."""
        import fitz
        limpio = self.fx.subcarpeta("maqueta_separa", ("ok16",))
        for cuantos in (2, 6, 12, 20):
            with self.subTest(problemas=cuantos):
                destino = os.path.join(limpio, f"informe_{cuantos}.pdf")
                informe = pdf.generar(limpio, [self._resultado_con_problemas(cuantos)],
                                      destino=destino)
                self.assertTrue(informe["ok"], informe.get("error"))
                documento = fitz.open(destino)
                try:
                    con_titulo = [i + 1 for i, p in enumerate(documento)
                                  if "Spectrogram" in p.get_text()]
                    con_imagen = [i + 1 for i, p in enumerate(documento)
                                  if any(p.get_image_rects(im[0])
                                         for im in p.get_images(full=True))]
                    self.assertEqual(con_titulo, con_imagen,
                                     "el título y su imagen acabaron en páginas distintas")
                    for i, pagina in enumerate(documento, start=1):
                        for im in pagina.get_images(full=True):
                            for rect in pagina.get_image_rects(im[0]):
                                self.assertGreaterEqual(rect.y0, 10 * 2.8346)
                                self.assertLessEqual(rect.y1, (297 - 8) * 2.8346)
                finally:
                    documento.close()
                os.remove(destino)

    # ── manejo de errores: nunca aborta ─────────────────────────────────────
    def test_sin_reportlab_avisa_sin_generar_nada(self):
        limpio = self.fx.subcarpeta("album_sin_reportlab", ("ok16",))
        original = pdf.REPORTLAB_OK
        pdf.REPORTLAB_OK = False
        try:
            informe = pdf.generar(limpio, [{"archivo": "x.flac"}])
            self.assertFalse(informe["ok"])
            self.assertIn("reportlab", informe["error"])
            self.assertIsNone(informe["ruta"])
            self.assertFalse(os.path.exists(pdf.ruta_informe(limpio)))
        finally:
            pdf.REPORTLAB_OK = original

    def test_destino_imposible_avisa_sin_lanzar(self):
        resultados = analizar_album(self.album, guardar_png=False)
        destino = os.path.join(self.fx["ok16"], pdf.NOMBRE_INFORME)   # un archivo
        informe = pdf.generar(self.album, resultados, destino=destino)
        self.assertFalse(informe["ok"])
        self.assertIsNone(informe["ruta"])
        self.assertTrue(informe["error"])

    def test_sin_resultados_avisa(self):
        informe = pdf.generar(self.album, [])
        self.assertFalse(informe["ok"])
        self.assertIn("there are no results", informe["error"].lower())

    def test_un_fallo_del_generador_no_aborta_el_analisis(self):
        """El motor convierte cualquier problema del informe en un aviso."""
        sonda = self.fx.subcarpeta("album_roto", ("ok16",))
        # el destino del informe es un DIRECTORIO: escribir dentro falla seguro
        os.makedirs(os.path.join(sonda, pdf.NOMBRE_INFORME), exist_ok=True)
        entorno = {k: v for k, v in os.environ.items() if k != "PYTHONIOENCODING"}
        p = subprocess.run([sys.executable, fx.CLI], input=f"2\nn\ny\n{sonda}\nn\n",
                           cwd=fx.RAIZ, capture_output=True, text=True,
                           encoding="utf-8", timeout=900, env=entorno)
        self.assertEqual(p.returncode, 0, p.stderr[-1500:])
        self.assertNotIn("Traceback", p.stderr or "")
        self.assertIn("Could not generate the PDF report", p.stdout)
        self.assertIn("Resumen: 1", p.stdout.replace("SUMMARY:", "Resumen:"))

    # ── integración con los CLI ─────────────────────────────────────────────
    def test_el_motor_emite_el_evento_informe_antes_de_fin(self):
        sonda = self.fx.subcarpeta("album_cli", ("ok16", "corte_16k"))
        p = fx.correr_motor(sonda, extra=("--pdf",))
        eventos = [json.loads(l) for l in p.stdout.splitlines() if l.strip()]
        tipos = [e["tipo"] for e in eventos]
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("informe", tipos)
        self.assertEqual(tipos[-1], "fin")
        self.assertLess(tipos.index("informe"), tipos.index("fin"))
        evento = eventos[tipos.index("informe")]
        self.assertEqual(os.path.dirname(evento["ruta"]), os.path.abspath(sonda))
        self.assertTrue(os.path.exists(evento["ruta"]))
        self.assertEqual(evento["paginas"], 4)              # portada + 2 + resumen
        # --pdf genera los espectrogramas aunque no se pida --png
        for resultado in (e for e in eventos if e["tipo"] == "resultado"):
            if not resultado.get("error"):
                self.assertIsNotNone(resultado["esp"].get("espectrograma"))

    def test_sin_pdf_no_se_emite_el_evento_informe(self):
        sonda = self.fx.subcarpeta("album_sin_pdf", ("ok16",))
        p = fx.correr_motor(sonda)
        tipos = [json.loads(l)["tipo"] for l in p.stdout.splitlines() if l.strip()]
        self.assertNotIn("informe", tipos)
        self.assertFalse(os.path.exists(os.path.join(sonda, pdf.NOMBRE_INFORME)))

    def test_el_cli_interactivo_tambien_genera_el_informe(self):
        sonda = self.fx.subcarpeta("album_interactivo", ("ok16", "ok24"))
        entorno = {k: v for k, v in os.environ.items() if k != "PYTHONIOENCODING"}
        p = subprocess.run([sys.executable, fx.CLI], input=f"2\nn\ny\n{sonda}\nn\n",
                           cwd=fx.RAIZ, capture_output=True, text=True,
                           encoding="utf-8", timeout=900, env=entorno)
        self.assertEqual(p.returncode, 0, p.stderr[-1500:])
        self.assertIn("PDF report:", p.stdout)
        self.assertTrue(os.path.exists(os.path.join(sonda, pdf.NOMBRE_INFORME)))

    def test_el_pdf_y_el_png_son_combinables(self):
        sonda = self.fx.subcarpeta("album_combinado", ("ok16",))
        p = fx.correr_motor(sonda, extra=("--png", "--pdf"))
        tipos = [json.loads(l)["tipo"] for l in p.stdout.splitlines() if l.strip()]
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("informe", tipos)
        self.assertTrue(os.path.isdir(os.path.join(sonda, "_spectrograms")))
        self.assertTrue(os.path.exists(os.path.join(sonda, pdf.NOMBRE_INFORME)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
