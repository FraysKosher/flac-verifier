"""Language: nothing the project ships may contain Spanish.

The interface (GUI, CLI, PDF report and the engine's JSON protocol) is in English
because the project is shared with an international audience. This module is the
guard for that promise, and it checks three kinds of text:

  1. The text literals of the application and tool modules, piece by piece. Only
     the static parts of an f-string are inspected, so an interpolated variable
     name is not mistaken for prose.
  2. Every comment in every Python file of the repository.
  3. Every docstring in every Python file of the repository.

Two deliberate exceptions:

  · The internal names of the JSON protocol keys and of the reportlab styles
    (`archivo`, `ruta`, `veredicto`, `problema`, `celda`…) are identifiers, not
    interface text, and are listed in `PERMITIDOS`.
  · Identifiers written in Spanish are accepted: the code is not being renamed.
    Only prose is checked, and a word glued to an underscore or to another word
    is treated as part of an identifier.
"""
import ast
import io
import os
import re
import sys
import tokenize
import unittest

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RAIZ)

# Everything the user can read: the application modules and the two tools that are
# run by hand (the builder and the executable checker).
MODULOS = ("main.py", "motor_flac.py", "gui.py", "verificar_flac.py",
           "informe_pdf.py", "build.py", "comprobar_ejecutable.py")

PROPIOS = re.compile(r"[ñÑáéíóúÁÉÍÓÚüÜ¿¡]")

# Words that do not exist in English: if one shows up, the text was not translated.
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
    r"compilando|distribuible|instalado|instalaci[oó]n|ejecuta|ejecuci[oó]n|"
    r"quiz[aá]|tambi[eé]n|tres|cuatro|cada|mismo|misma|tiene|tienen|hace|hacen|"
    r"debe|deben|puede|pueden|queda|quedan|sale|salen|existe|existen|sobra|sobran|"
    # Spanish participles: the pattern that slipped through twice, because they
    # carry no accent and read like a verb in a status message.
    r"verificad[oa]|verificar|comprobad[oa]|comprobar|obtenid[oa]|encontrad[oa]|"
    r"seleccionad[oa]|elegid[oa]|iniciad[oa]|detenid[oa]|guardad[oa]|escrit[oa]|"
    r"le[ií]d[oa]|abiert[oa]|cerrad[oa]|mostrad[oa]|calculad[oa]|medid[oa]|"
    r"detectad[oa]|analizad[oa]|procesad[oa]|cread[oa]|borrad[oa]|a[nñ]adid[oa]|"
    r"cambiad[oa]|actualizad[oa]|marcad[oa]|activad[oa]|desactivad[oa])\b",
    re.IGNORECASE)
# Words spelled the same in English: no signal of the language.
FALSOS_POSITIVOS = {"album", "compatible", "control", "pie", "solo", "dos", "pro"}

# Internal identifiers (protocol keys, reportlab style names) and file paths: not
# interface text.
PERMITIDOS = {
    "archivo", "ruta", "veredicto", "problemas", "problema", "informe", "aviso",
    "modo", "cache", "pico", "tamano", "cancion", "etiqueta", "celda", "pie",
    "seccion", "codigo", "cancelado", "normal", "subtitulo", "titulo", "insignia",
    "error", "ok", "paginas", "salida", "output",
    # Protocol keys and argparse destinations that stay as they are: internal
    # identifiers, not interface text.
    "espectrograma", "espectrogramas", "carpeta", "segundos",
}
RUTA_O_ARCHIVO = re.compile(r"[\w/\\-]*\.(ico|png|svg|py|txt|pdf|zip|exe|flac|json)")


def archivos_con_prosa():
    """Every Python file whose comments and docstrings are checked."""
    carpeta = os.path.join(RAIZ, "tests")
    pruebas = sorted(f"tests/{nombre}" for nombre in os.listdir(carpeta)
                     if nombre.endswith(".py"))
    return MODULOS + tuple(pruebas) + ("Logo/generar_logo.py",)


def nodos_docstring(fuente):
    """The Constant nodes that are a module, class or function docstring."""
    for nodo in ast.walk(ast.parse(fuente)):
        if isinstance(nodo, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            cuerpo = getattr(nodo, "body", [])
            if (cuerpo and isinstance(cuerpo[0], ast.Expr)
                    and isinstance(cuerpo[0].value, ast.Constant)
                    and isinstance(cuerpo[0].value.value, str)):
                yield cuerpo[0].value


def rangos_docstring(fuente):
    """Character ranges of the docstrings.

    Watch the units: `col_offset`/`end_col_offset` from the syntax tree are UTF-8
    BYTE offsets, while the token walk uses characters. Mixing them makes the
    ranges miss, and then the filter lets docstrings through into the visible-text
    scan — which is wrong in both directions.
    """
    datos = fuente.encode("utf-8")
    saltos = [0]
    for indice, byte in enumerate(datos):
        if byte == 0x0A:
            saltos.append(indice + 1)

    def a_caracter(posicion_en_bytes):
        return len(datos[:posicion_en_bytes].decode("utf-8"))

    return [(a_caracter(saltos[nodo.lineno - 1] + nodo.col_offset),
             a_caracter(saltos[nodo.end_lineno - 1] + nodo.end_col_offset))
            for nodo in nodos_docstring(fuente)]


def piezas(interior):
    """Text pieces of a literal, without the `{…}` expressions of an f-string.

    What the user sees is the text, not the names of the interpolated variables:
    `f"{icono} Ready"` shows "Ready".
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
    """(line, text) of every visible piece of every text literal."""
    fuente = open(ruta, "rb").read().decode("utf-8")
    omitir = rangos_docstring(fuente)
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


def prosa(ruta):
    """(line, text) of every comment line and every docstring line."""
    fuente = open(ruta, "rb").read().decode("utf-8")
    for token in tokenize.generate_tokens(io.StringIO(fuente).readline):
        if token.type == tokenize.COMMENT:
            yield token.start[0], token.string
    for nodo in nodos_docstring(fuente):
        for desplazamiento, linea in enumerate(nodo.value.splitlines()):
            yield nodo.lineno + desplazamiento, linea


class TestIdioma(unittest.TestCase):
    """Interface text, comments and docstrings have to be in English."""

    def _revisar(self, patron, descripcion):
        problemas = []
        fuentes = [(os.path.join(RAIZ, nombre), trozos_visibles, "text")
                   for nombre in MODULOS]
        fuentes += [(os.path.join(RAIZ, ruta), prosa, "prose")
                    for ruta in archivos_con_prosa()]
        for ruta, generador, origen in fuentes:
            for linea, texto in generador(ruta):
                if not texto.strip():
                    continue
                # File names and paths may contain anything ("Logo/icono_app.ico").
                # The rule is "looks like a path": no spaces. Skipping every text
                # with a slash used to let prose through, such as the warning the
                # engine prints when the high/mid ratio is very low.
                if RUTA_O_ARCHIVO.search(texto) or ("/" in texto and " " not in texto.strip()):
                    continue
                if texto.strip().strip("\"'") in PERMITIDOS:
                    continue
                for coincidencia in patron.finditer(texto):
                    palabra = coincidencia.group(0)
                    if palabra.lower() in PERMITIDOS or palabra.lower() in FALSOS_POSITIVOS:
                        continue
                    # "process(es)" and "(s)" are not Spanish, and a word glued to
                    # an underscore, to another word or to a call parenthesis
                    # belongs to an identifier: Spanish identifiers are accepted
                    # (the code keeps its names), Spanish prose is not. That is how
                    # an English docstring may mention `informe_pdf.generar()`.
                    inicio, fin = coincidencia.span()
                    antes = texto[inicio - 1] if inicio else ""
                    despues = texto[fin] if fin < len(texto) else ""
                    if antes in "([{" or despues in ")]}(":
                        continue
                    if antes.isalnum() or antes == "_" or despues.isalnum() or despues == "_":
                        continue
                    problemas.append(
                        f"{os.path.basename(ruta)}:{linea} [{origen}]: "
                        f"{palabra!r} in {texto.strip()[:60]!r}")
        self.assertEqual(problemas, [], f"{descripcion} in " + "; ".join(problemas[:6]))

    def test_no_hay_caracteres_espanoles(self):
        self._revisar(PROPIOS, "Spanish characters")

    def test_no_hay_palabras_espanolas(self):
        self._revisar(PALABRAS, "Spanish words")

    def test_los_veredictos_estan_en_ingles_y_coinciden(self):
        """The traffic light depends on engine, GUI and PDF using the SAME strings."""
        import gui
        import informe_pdf
        esperados = {"GENUINE LOSSLESS", "PROBABLY LOSSLESS", "SUSPICIOUS",
                     "PROBABLE UPSCALE", "indeterminate"}
        self.assertEqual(set(gui.ETIQUETA_VEREDICTO), esperados)
        self.assertEqual(set(gui.COLOR_VEREDICTO), esperados)
        self.assertEqual(set(informe_pdf.ESTILO_VEREDICTO), esperados)
        # The engine can only emit those labels. Only text literals are inspected,
        # so a comment or a docstring may still quote an old name on purpose.
        visibles = {texto.strip() for _, texto in
                    trozos_visibles(os.path.join(RAIZ, "motor_flac.py"))}
        for veredicto in esperados:
            with self.subTest(veredicto=veredicto):
                self.assertIn(veredicto, visibles)
        for antiguo in ("LOSSLESS GENUINO", "PROBABLEMENTE LOSSLESS", "DUDOSO"):
            with self.subTest(antiguo=antiguo):
                self.assertNotIn(antiguo, visibles)

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
        # The engine's command-line entry point stays importable and callable.
        self.assertTrue(callable(motor_flac.cli_principal))

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
