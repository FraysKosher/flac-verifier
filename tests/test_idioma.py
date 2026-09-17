"""Idioma: ningún texto visible puede contener español.

La interfaz del programa (GUI, CLI, informe PDF y protocolo del motor) va en
inglés porque el proyecto se comparte con la comunidad internacional. Este módulo
lo vigila sobre los literales de texto del código, no sobre los comentarios ni los
docstrings: el encargo dice explícitamente que el código de dentro no se traduce.

Qué se comprueba, en cada trozo de texto de cada literal:
  1. Que no haya caracteres propios del español (ñ, ¿, ¡, vocales acentuadas…).
  2. Que no aparezca ninguna palabra de la lista de palabras españolas.

Dos excepciones deliberadas:
  · Las claves y los nombres internos del protocolo JSON y de los estilos de
    reportlab (`archivo`, `ruta`, `veredicto`, `problema`, `celda`…) son
    identificadores, no texto de interfaz: están en `PERMITIDOS`.
  · Los nombres de archivo (iconos, logo) pueden llevar palabras en español.
"""
import io
import os
import re
import sys
import tokenize
import unittest

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RAIZ)

# Todo lo que el usuario puede llegar a leer: los cuatro módulos de la aplicación
# y las dos herramientas que se ejecutan a mano (compilar y comprobar el .exe).
MODULOS = ("main.py", "motor_flac.py", "gui.py", "verificar_flac.py",
           "informe_pdf.py", "build.py", "comprobar_ejecutable.py")

PROPIOS = re.compile(r"[ñÑáéíóúÁÉÍÓÚüÜ¿¡]")

# Palabras que no existen en inglés: si aparece alguna, el texto está sin traducir.
PALABRAS = re.compile(
    r"\b(de|del|la|las|los|una|uno|unos|unas|que|con|para|por|sin|al|su|sus|"
    r"es|son|está|están|hay|más|muy|pero|como|cuando|donde|este|esta|esto|"
    r"archivo|archivos|carpeta|carpetas|ruta|rutas|modo|modos|segundos|informe|"
    r"informes|problema|problemas|veredicto|veredictos|pista|pistas|canci[oó]n|"
    r"canciones|an[aá]lisis|banda|bandas|resoluci[oó]n|muestra|muestras|rejilla|"
    r"v[aá]cios|parciales|cortada|cortado|declarada|declarado|compatible|filtro|"
    r"tasa|leg[ií]timo|concluyente|[aá]lbum|contenido|profundidad|techo|techos|"
    r"descarta|prueba|origen|nivel|pico|frecuencia|tiempo|activos|inactivos|"
    r"p[eé]rdida|maestro|ejecutable|procesos|proceso|salir|racha|rachas|"
    r"analizando|analizar|an[aá]lisis|cach[eé]|autom[aá]tico|examinar|elegida|"
    r"elegir|listo|falta|inexistente|todav[ií]a|ning[uú]n|cancela|cancelado|"
    r"terminado|termin[oó]|c[oó]digo|salida|aviso|avisos|abrir|cerrar|cerrando|"
    r"ventana|reproducir|marca|icono|control|seg[uú]n|oculta|descendencia|"
    r"entorno|inst[aá]lalo|mientras|puedes|usar|pulsa|elige|resumen|porcentaje|"
    r"espectrograma|espectrogramas|ejecutando|espera|soltado|procesamiento|"
    r"terminar|fichero|portada|p[aá]gina|p[aá]ginas|cuenta|devuelve|formato|"
    r"etiqueta|valor|fila|filas|columna|ancho|alto|peso|tama[nñ]o|fecha|hora|"
    r"siguiente|anterior|cuerpo|cabecera|pie|generado|generar|guardar|ignorar|"
    r"aplicable|disponible|comprobaci[oó]n|comprobando|verificando|compilaci[oó]n|"
    r"compilando|distribuible|instalado|instalaci[oó]n|ejecuta|ejecuci[oó]n)\b",
    re.IGNORECASE)
# Palabras que existen igual en inglés: no sirven como señal de idioma.
FALSOS_POSITIVOS = {"album", "compatible", "control", "pie"}

# Identificadores internos (claves del protocolo, nombres de estilos de reportlab)
# y rutas de archivo: no son texto de interfaz.
PERMITIDOS = {
    "archivo", "ruta", "veredicto", "problemas", "problema", "informe", "aviso",
    "modo", "cache", "pico", "tamano", "cancion", "etiqueta", "celda", "pie",
    "seccion", "codigo", "cancelado", "normal", "subtitulo", "titulo", "insignia",
    "error", "ok", "paginas", "salida", "output",
    # Claves del protocolo y destinos de argparse que se quedan como están: son
    # identificadores internos, no texto de interfaz.
    "espectrograma", "espectrogramas", "carpeta", "segundos",
}
RUTA_O_ARCHIVO = re.compile(r"[\w/\\-]*\.(ico|png|svg|py|txt|pdf|zip|exe|flac|json)")


def docstrings(fuente):
    """Rangos (en caracteres) de los docstrings.

    Ojo con las unidades: `col_offset`/`end_col_offset` del árbol sintáctico son
    desplazamientos en BYTES UTF-8, mientras que el recorrido de los tokens va en
    caracteres. Si se mezclan, los rangos no coinciden y el filtro deja pasar los
    docstrings (que sí llevan acentos y palabras españolas).
    """
    import ast
    datos = fuente.encode("utf-8")
    saltos = [0]
    for indice, byte in enumerate(datos):
        if byte == 0x0A:
            saltos.append(indice + 1)

    def a_caracter(posicion_en_bytes):
        return len(datos[:posicion_en_bytes].decode("utf-8"))

    rangos = []
    for nodo in ast.walk(ast.parse(fuente)):
        if isinstance(nodo, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            cuerpo = getattr(nodo, "body", [])
            if (cuerpo and isinstance(cuerpo[0], ast.Expr)
                    and isinstance(cuerpo[0].value, ast.Constant)
                    and isinstance(cuerpo[0].value.value, str)):
                literal = cuerpo[0].value
                inicio = a_caracter(saltos[literal.lineno - 1] + literal.col_offset)
                fin = a_caracter(saltos[literal.end_lineno - 1] + literal.end_col_offset)
                rangos.append((inicio, fin))
    return rangos


def piezas(interior):
    """Trozos de texto de un literal, sin las expresiones `{…}` de las f-strings.

    Lo que ve el usuario es el texto, no los nombres de las variables que se
    interpolan: `f"{icono} Ready"` muestra «Ready».
    """
    resultados = []
    actual = []
    indice = 0
    profundidad = 0
    while indice < len(interior):
        if interior[indice] == "{":
            if profundidad == 0 and actual:
                resultados.append("".join(actual))
                actual = []
            profundidad += 1
            indice += 1
            continue
        if interior[indice] == "}":
            profundidad -= 1
            indice += 1
            continue
        if profundidad == 0 and interior[indice:indice + 2] in ("{{", "}}"):
            actual.append(interior[indice])
            indice += 2
            continue
        if profundidad == 0:
            actual.append(interior[indice])
        indice += 1
    if actual:
        resultados.append("".join(actual))
    return resultados


def trozos_visibles(ruta):
    """(linea, texto) de cada trozo de literal visible, sin docstrings."""
    fuente = open(ruta, "rb").read().decode("utf-8")
    omitir = docstrings(fuente)
    inicio_linea = [0]
    for indice, caracter in enumerate(fuente):
        if caracter == "\n":
            inicio_linea.append(indice + 1)
    for token in tokenize.generate_tokens(io.StringIO(fuente).readline):
        if token.type != tokenize.STRING:
            continue
        comienzo = inicio_linea[token.start[0] - 1] + token.start[1]
        fin = inicio_linea[token.end[0] - 1] + token.end[1]
        if any(comienzo >= a and fin <= b for a, b in omitir):
            continue
        cuerpo = token.string
        while cuerpo and cuerpo[0] in "fFrRbBuU":
            cuerpo = cuerpo[1:]
        if not cuerpo or cuerpo[0] not in "\"'":
            continue
        recorte = 3 if cuerpo.startswith(cuerpo[0] * 3) else 1
        for trozo in piezas(cuerpo[recorte:len(cuerpo) - recorte]):
            yield token.start[0], trozo


def codigo_sin_comentarios(ruta):
    """El código de un archivo sin comentarios (para buscar banderas u textos)."""
    fuente = open(ruta, "rb").read().decode("utf-8")
    partes = []
    for token in tokenize.generate_tokens(io.StringIO(fuente).readline):
        if token.type in (tokenize.COMMENT, tokenize.NL, tokenize.NEWLINE,
                          tokenize.INDENT, tokenize.DEDENT):
            partes.append(" ")
            continue
        partes.append(token.string)
    return " ".join(partes)


class TestIdioma(unittest.TestCase):
    """Los textos de interfaz tienen que estar en inglés."""

    def _revisar(self, nombre, patron, descripcion):
        problemas = []
        for linea, texto in trozos_visibles(os.path.join(RAIZ, nombre)):
            if not texto.strip():
                continue
            # Los nombres de archivo y las rutas pueden llevar español
            # («Logo/icono_app.ico»). La condición es «parece una ruta»: sin
            # espacios. Saltarse cualquier texto con una barra dejaba pasar frases
            # como «ratio altas/medias muy bajo», que es prosa.
            if RUTA_O_ARCHIVO.search(texto) or ("/" in texto and " " not in texto.strip()):
                continue
            if texto.strip().strip("\"'") in PERMITIDOS:
                continue
            for coincidencia in patron.finditer(texto):
                palabra = coincidencia.group(0)
                if palabra.lower() in PERMITIDOS or palabra.lower() in FALSOS_POSITIVOS:
                    continue
                # "process(es)" o "(s)" no son español: se descartan los trozos que
                # van pegados a un paréntesis o dentro de otra palabra.
                inicio, fin = coincidencia.span()
                antes = texto[inicio - 1] if inicio else ""
                despues = texto[fin] if fin < len(texto) else ""
                if antes in "([{" or despues in ")]}":
                    continue
                problemas.append(f"{nombre}:{linea}: {palabra!r} en {texto!r}")
        self.assertEqual(problemas, [], f"{descripcion} en " + "; ".join(problemas[:6]))

    def test_no_hay_caracteres_espanoles(self):
        for nombre in MODULOS:
            with self.subTest(modulo=nombre):
                self._revisar(nombre, PROPIOS, "caracteres españoles")

    def test_no_hay_palabras_espanolas(self):
        for nombre in MODULOS:
            with self.subTest(modulo=nombre):
                self._revisar(nombre, PALABRAS, "palabras españolas")

    def test_los_veredictos_estan_en_ingles_y_coinciden(self):
        """El semáforo depende de que motor, GUI y PDF usen las MISMAS cadenas."""
        import gui
        import informe_pdf
        esperados = {"GENUINE LOSSLESS", "PROBABLY LOSSLESS", "SUSPICIOUS",
                     "PROBABLE UPSCALE", "indeterminate"}
        self.assertEqual(set(gui.ETIQUETA_VEREDICTO), esperados)
        self.assertEqual(set(gui.COLOR_VEREDICTO), esperados)
        self.assertEqual(set(informe_pdf.ESTILO_VEREDICTO), esperados)
        # El motor solo puede emitir esos: si alguien deja uno antiguo, aquí salta.
        # Se mira el código sin comentarios ni docstrings (ahí sí se puede citar el
        # nombre antiguo al explicar un cambio).
        fuente = codigo_sin_comentarios(os.path.join(RAIZ, "motor_flac.py"))
        for veredicto in ("GENUINE LOSSLESS", "PROBABLY LOSSLESS", "SUSPICIOUS",
                          "PROBABLE UPSCALE", "indeterminate"):
            with self.subTest(veredicto=veredicto):
                self.assertIn(f'"{veredicto}"', fuente)
        for antiguo in ("LOSSLESS GENUINO", "PROBABLEMENTE LOSSLESS", "DUDOSO"):
            with self.subTest(antiguo=antiguo):
                self.assertNotIn(f'"{antiguo}"', fuente)

    def test_las_banderas_estan_en_ingles(self):
        import motor_flac
        fuente = open(os.path.join(RAIZ, "motor_flac.py"), encoding="utf-8").read()
        for bandera in ("--path", "--mode", "--seconds", "--png", "--pdf",
                        "--workers", "--no-cache"):
            with self.subTest(bandera=bandera):
                self.assertIn(f'"{bandera}"', fuente)
        for antigua in ("--ruta", "--modo", "--seg", "--sin-cache"):
            with self.subTest(bandera=antigua):
                self.assertNotIn(f'"{antigua}"', fuente)
        # Los modos que acepta el motor son los ingleses.
        parser = motor_flac.cli_principal
        self.assertTrue(callable(parser))

    def test_el_informe_y_la_carpeta_cambiaron_de_nombre(self):
        import gui
        import informe_pdf
        self.assertEqual(gui.NOMBRE_INFORME, "flac_verifier_report.pdf")
        self.assertEqual(informe_pdf.NOMBRE_INFORME, "flac_verifier_report.pdf")
        fuente = open(os.path.join(RAIZ, "motor_flac.py"), encoding="utf-8").read()
        self.assertIn('"_spectrograms"', fuente)
        self.assertNotIn('"_espectrogramas"', fuente)


if __name__ == "__main__":
    unittest.main(verbosity=2)
