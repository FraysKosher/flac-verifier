"""Language: exported prose is English; only the JSON protocol keeps Spanish keys.

The interface (GUI, CLI, PDF report and the engine's JSON protocol) is in English
because the project is shared with an international audience. This module is the
guard for that promise, and it checks three kinds of text:

  1. The text literals of the application and tool modules, piece by piece. Only
     the static parts of an f-string are inspected, so an interpolated variable
     name is not mistaken for prose.
  2. Every comment in every Python file of the repository.
  3. Every docstring in every Python file of the repository.

Two deliberate exceptions:

  · JSON protocol keys (`archivo`, `ruta`, `veredicto`, `problema`, `celda`…) and
    reportlab style names are public/internal identifiers, not interface text,
    and are listed in `JSON_KEYS` and `INTERNAL_STYLE_NAMES`. They are the only
    Spanish names left: Python identifiers are English.
  · A word glued to an underscore or to another word is part of an identifier
    and is not checked as prose.
"""
import ast
import io
import os
import re
import sys
import tokenize
import unittest
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# Everything the user can read: the application modules and the two tools that are
# run by hand (the builder and the executable checker).
MODULES = ("main.py", "motor_flac.py", "gui.py", "theme.py",
           "verificar_flac.py",
           "informe_pdf.py", "build.py", "comprobar_ejecutable.py",
           "_motor_bits.py", "_motor_espectral.py", "_motor_veredicto.py",
           "_motor_cache.py")

OWN = re.compile(r"[ñÑáéíóúÁÉÍÓÚüÜ¿¡]")

# Words that do not exist in English: if one shows up, the text was not translated.
SPANISH_WORDS = re.compile(
    r"\b(de|del|la|las|los|una|uno|unos|unas|que|con|para|por|sin|al|su|sus|"
    r"es|son|está|están|hay|más|muy|pero|como|cuando|donde|este|esta|esto|"
    r"archivo|archivos|carpeta|carpetas|ruta|rutas|modo|modos|segundos|informe|"
    r"informes|problema|problemas|veredicto|veredictos|pista|pistas|canci[oó]n|"
    r"canciones|an[aá]lisis|banda|bandas|resoluci[oó]n|muestra|muestras|rejilla|"
    r"v[aá]cios|parciales|cortada|cortado|declarada|declarado|reclama|reclaman|"
    r"declara|declaran|compatible|filtro|"
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
    r"cambiad[oa]|actualizad[oa]|marcad[oa]|activad[oa]|desactivad[oa]|"
    r"sanear|sanead[oa]|puntuaci[oó]n|puntuar|diagnosticar|diagn[oó]stico|"
    r"autenticidad|desead[oa]|potencia|elegid[oa]s?|intentos?|largos|largas|"
    r"inicios?|longitud|longitudes)\b",
    re.IGNORECASE)
# Words spelled the same in English: no signal of the language.
FALSE_POSITIVES = {
    "album", "compatible", "control", "pie", "solo", "dos", "pro",
    "volatile", "cached", "nonexistent", "anchor", "variable",
    "error", "total", "cache", "ratio", "output", "normal",
    "controls", "decimals",
}   # English names that happen to contain a Spanish fragment or are homographs

# The lexicon is vendored so the guard does not depend on a regex written by
# hand. SPANISH_WORDS stays as the prose guard; identifiers use the file.
def _load_spanish_lexicon():
    path = os.path.join(ROOT, "tests", "data", "spanish_words.txt")
    words = set()
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            words.add(line.lower())
    return words


SPANISH_LEXICON = _load_spanish_lexicon()
SPANISH_SUBSTRINGS = re.compile(
    "|".join(re.escape(word) for word in sorted(
        (word for word in SPANISH_LEXICON if len(word) >= 5),
        key=len, reverse=True)),
    re.IGNORECASE)


def _is_spanish_token(token):
    token = token.lower()
    if token in SPANISH_LEXICON:
        return True
    if len(token) >= 5 and SPANISH_SUBSTRINGS.search(token):
        return True
    for prefix in ("sub", "sobre", "anti", "pre"):
        rest = token[len(prefix):]
        if token.startswith(prefix) and rest and rest in SPANISH_LEXICON:
            return True
    return False

# Word boundaries are right for prose, but an identifier such as `subcarpeta`
# hides a Spanish word inside a longer token. The identifier guard searches
# these fragments and keeps only matches of five characters or more.
SPANISH_FRAGMENTS = re.compile(SPANISH_WORDS.pattern[3:-3], re.IGNORECASE)

# JSON protocol keys: the public contract, never translated.
JSON_KEYS = {
    "archivo", "ruta", "veredicto", "problemas", "problema", "informe", "aviso",
    "modo", "cache", "pico", "tamano", "cancion", "error", "ok", "paginas",
    "salida",
}

# Values of the `tipo` field: protocol vocabulary, not Spanish prose.
PROTOCOL_VALUES = {"inicio", "resultado", "informe", "aviso", "fin", "error"}

# reportlab style names used by the PDF: internal identifiers, not prose.
INTERNAL_STYLE_NAMES = {
    "etiqueta", "celda", "pie", "seccion", "codigo", "cancelado", "normal",
    "subtitulo", "titulo", "insignia", "encabezado", "marca",
}

# argparse destinations that stay as they are: internal identifiers, not prose.
ARGPARSE_DESTS = {"espectrograma", "espectrogramas", "output"}

# Union kept for the checks below; the split above documents why each name is
# allowed, and the behaviour is identical to the old single set.
ALLOWED = JSON_KEYS | INTERNAL_STYLE_NAMES | ARGPARSE_DESTS | PROTOCOL_VALUES
PATH_OR_FILE = re.compile(r"[\w/\\-]*\.(ico|png|svg|py|txt|pdf|zip|exe|flac|json)")


def _external_import_names():
    """Names imported from third-party modules: not ours, never renamed."""
    propias = {os.path.splitext(name)[0] for name in MODULES}
    externas = set()
    for name in files_with_prose():
        path = os.path.join(ROOT, name)
        tree = ast.parse(Path(path).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[0] not in propias:
                        externas.add(alias.asname or alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                root = (node.module or "").split(".")[0]
                if root and root not in propias:
                    for alias in node.names:
                        externas.add(alias.asname or alias.name)
    return externas


def files_with_prose():
    """Every Python file whose comments and docstrings are checked."""
    folder = os.path.join(ROOT, "tests")
    tests = sorted(f"tests/{name}" for name in os.listdir(folder)
                     if name.endswith(".py"))
    return MODULES + tuple(tests) + ("Logo/generar_logo.py",
                                     "Logo/generate_icons.py")


def docstring_nodes(source):
    """The Constant nodes that are a module, class or function docstring."""
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                yield body[0].value


def docstring_ranges(source):
    """Character ranges of the docstrings.

    Watch the units: `col_offset`/`end_col_offset` from the syntax tree are UTF-8
    BYTE offsets, while the token walk uses characters. Mixing them makes the
    ranges miss, and then the filter lets docstrings through into the visible-text
    scan — which is wrong in both directions.
    """
    payload = source.encode("utf-8")
    saltos = [0]
    for index, byte in enumerate(payload):
        if byte == 0x0A:
            saltos.append(index + 1)

    def to_character(position_bytes):
        return len(payload[:position_bytes].decode("utf-8"))

    return [(to_character(saltos[node.lineno - 1] + node.col_offset),
             to_character(saltos[node.end_lineno - 1] + node.end_col_offset))
            for node in docstring_nodes(source)]


def pieces(inner):
    """Text pieces of a literal, without the `{…}` expressions of an f-string.

    What the user sees is the text, not the names of the interpolated variables:
    `f"{icono} Ready"` shows "Ready".
    """
    results = []
    current = []
    index = 0
    depth = 0
    while index < len(inner):
        if inner[index] == "{":
            if depth == 0 and current:
                results.append("".join(current))
                current = []
            depth += 1
            index += 1
            continue
        if inner[index] == "}":
            depth -= 1
            index += 1
            continue
        if depth == 0 and inner[index:index + 2] in ("{{", "}}"):
            current.append(inner[index])
            index += 2
            continue
        if depth == 0:
            current.append(inner[index])
        index += 1
    if current:
        results.append("".join(current))
    return results


def chunks_visible(path):
    """(line, text) of every visible piece of every text literal."""
    source = Path(path).read_text(encoding="utf-8")
    omitir = docstring_ranges(source)
    start_line = [0]
    for index, character in enumerate(source):
        if character == "\n":
            start_line.append(index + 1)
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type != tokenize.STRING:
            continue
        comienzo = start_line[token.start[0] - 1] + token.start[1]
        end = start_line[token.end[0] - 1] + token.end[1]
        if any(comienzo >= a and end <= b for a, b in omitir):
            continue
        body = token.string
        while body and body[0] in "fFrRbBuU":
            body = body[1:]
        if not body or body[0] not in "\"'":
            continue
        recorte = 3 if body.startswith(body[0] * 3) else 1
        for chunk in pieces(body[recorte:len(body) - recorte]):
            yield token.start[0], chunk


def prose(path):
    """(line, text) of every comment line and every docstring line."""
    source = Path(path).read_text(encoding="utf-8")
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type == tokenize.COMMENT:
            yield token.start[0], token.string
    for node in docstring_nodes(source):
        for desplazamiento, line in enumerate(node.value.splitlines()):
            yield node.lineno + desplazamiento, line


class TestLanguage(unittest.TestCase):
    """Interface text, comments and docstrings have to be in English."""

    def _check(self, pattern, description):
        problems = []
        sources = [(os.path.join(ROOT, name), chunks_visible, "text")
                   for name in MODULES]
        sources += [(os.path.join(ROOT, path), prose, "prose")
                    for path in files_with_prose()]
        for path, generator, source in sources:
            for line, text in generator(path):
                if not text.strip():
                    continue
                # File names and paths may contain anything ("Logo/icono_app.ico").
                # The rule is "looks like a path": no spaces. Skipping every text
                # with a slash used to let prose through, such as the warning the
                # engine prints when the high/mid ratio is very low.
                if PATH_OR_FILE.search(text) or ("/" in text and " " not in text.strip()):
                    continue
                if text.strip().strip("\"'") in ALLOWED:
                    continue
                for match in pattern.finditer(text):
                    word = match.group(0)
                    if word.lower() in ALLOWED or word.lower() in FALSE_POSITIVES:
                        continue
                    # "process(es)" and "(s)" are not Spanish, and a word glued to
                    # an underscore, to another word or to a call parenthesis
                    # belongs to an identifier: JSON protocol keys and reportlab
                    # style names are allowed, Spanish prose is not. That is how
                    # an English docstring may mention `informe_pdf.generate()`.
                    start, end = match.span()
                    before = text[start - 1] if start else ""
                    after = text[end] if end < len(text) else ""
                    if before in "([{" or after in ")]}(":
                        continue
                    if before.isalnum() or before == "_" or after.isalnum() or after == "_":
                        continue
                    problems.append(
                        f"{os.path.basename(path)}:{line} [{source}]: "
                        f"{word!r} in {text.strip()[:60]!r}")
        self.assertEqual(problems, [], f"{description} in " + "; ".join(problems[:6]))

    def test_no_spanish_characters(self):
        self._check(OWN, "Spanish characters")

    def test_no_spanish_words(self):
        self._check(SPANISH_WORDS, "Spanish words")

    def test_verdicts_are_english_and_match(self):
        """The traffic light depends on engine, GUI and PDF using the SAME strings."""
        import gui
        import informe_pdf
        expected = {"GENUINE LOSSLESS", "PROBABLY LOSSLESS", "SUSPICIOUS",
                     "PROBABLE UPSCALE", "indeterminate"}
        self.assertEqual(set(gui.LABEL_VERDICT), expected)
        self.assertEqual(set(informe_pdf.STYLE_VERDICT), expected)
        # The engine can only emit those labels. Only text literals are inspected,
        # so a comment or a docstring may still quote an old name on purpose.
        visible = {text.strip() for _, text in
                    chunks_visible(os.path.join(ROOT, "motor_flac.py"))}
        for verdict in expected:
            with self.subTest(verdict=verdict):
                self.assertIn(verdict, visible)
        for antiguo in ("LOSSLESS GENUINO", "PROBABLEMENTE LOSSLESS", "DUDOSO"):
            with self.subTest(antiguo=antiguo):
                self.assertNotIn(antiguo, visible)

    def test_flags_are_english(self):
        import motor_flac
        source = Path(ROOT, "motor_flac.py").read_text(encoding="utf-8")
        for flag in ("--path", "--mode", "--seconds", "--png", "--pdf",
                        "--workers", "--no-cache"):
            with self.subTest(flag=flag):
                self.assertIn(f'"{flag}"', source)
        for antigua in ("--ruta", "--modo", "--seg", "--sin-cache"):
            with self.subTest(flag=antigua):
                self.assertNotIn(f'"{antigua}"', source)
        # The engine's command-line entry point stays importable and callable.
        self.assertTrue(callable(motor_flac.main_cli))

    def test_report_and_folder_names_changed(self):
        import gui
        import informe_pdf
        self.assertEqual(gui.REPORT_NAME, "flac_verifier_report.pdf")
        self.assertEqual(informe_pdf.REPORT_NAME, "flac_verifier_report.pdf")
        source = Path(ROOT, "motor_flac.py").read_text(encoding="utf-8")
        self.assertIn('"_spectrograms"', source)
        self.assertNotIn('"_espectrogramas"', source)

    def test_no_spanish_identifiers(self):
        """AST guard: every Python identifier in the repo is English.

        The lexicon is vendored in `tests/data/spanish_words.txt`. JSON protocol
        keys are strings, not identifiers: a Spanish key that leaked into a
        variable or function name is still a defect and must be renamed.
        Dunders, module file names, third-party imports and an explicit list of
        English homographs are the only exceptions.
        """
        external = _external_import_names()
        module_names = {os.path.splitext(name)[0] for name in MODULES}
        problems = []
        for name in files_with_prose():
            path = os.path.join(ROOT, name)
            source = Path(path).read_text(encoding="utf-8")
            for node in ast.walk(ast.parse(source)):
                # numpy's sine is an English function name, not the Spanish word.
                if (isinstance(node, ast.Attribute) and node.attr == "sin"
                        and isinstance(node.value, ast.Name) and node.value.id == "np"):
                    continue
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                     ast.ClassDef)):
                    candidate = node.name
                elif isinstance(node, ast.Name):
                    candidate = node.id
                elif isinstance(node, ast.arg):
                    candidate = node.arg
                elif isinstance(node, ast.Attribute):
                    candidate = node.attr
                else:
                    continue
                if not candidate or candidate in external:
                    continue
                if candidate in module_names:
                    continue
                if candidate.startswith("__") and candidate.endswith("__"):
                    continue
                for chunk in re.split(r"_+|(?<=[a-z])(?=[A-Z])", candidate):
                    low = chunk.lower()
                    if not low or low in FALSE_POSITIVES:
                        continue
                    if _is_spanish_token(low):
                        problems.append(f"{name}:{node.lineno}: {candidate!r}")
                        break
        self.assertEqual(problems, [],
                         "Spanish identifiers in " + "; ".join(problems[:12]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
