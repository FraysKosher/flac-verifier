"""Packaging: `main.py` dispatch, frozen engine command and the `.spec`.

What matters and is pinned down here:
  - `multiprocessing.freeze_support()` is the FIRST statement of the `__main__`
    block of `main.py`. If it moves, the parallel analysis processes start the
    whole application again (in the windowed .exe: a new window per worker, in a
    loop).
  - The GUI knows how to invoke the engine when it is packaged (a separate
    executable or the internal `--motor-cli` flag), and in source mode it still
    uses `-u` with the .py.
  - The `.spec` really collects everything that is needed and does not depend on
    absolute paths.
"""
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fixtures as fx

import gui
import main

RAIZ = fx.RAIZ
SPEC = os.path.join(RAIZ, "flac_verifier.spec")
VERSION_INFO = os.path.join(RAIZ, "version_info.txt")


class TestPuntoDeEntrada(unittest.TestCase):

    def test_freeze_support_es_lo_primero_del_bloque_principal(self):
        fuente = open(os.path.join(RAIZ, "main.py"), encoding="utf-8").read()
        bloque = fuente.split('if __name__ == "__main__":', 1)
        self.assertEqual(len(bloque), 2, "main.py has no __main__ block")
        cuerpo = [linea.strip() for linea in bloque[1].splitlines()
                  if linea.strip() and not linea.strip().startswith("#")]
        self.assertTrue(cuerpo, "the __main__ block is empty")
        self.assertEqual(cuerpo[0], "multiprocessing.freeze_support()",
                         "freeze_support() must be the first statement: "
                         "otherwise every worker of the parallel analysis reopens the app")

    def test_multiprocessing_no_se_importa_dentro_de_una_funcion(self):
        """Importing it late would work, but it must not be conditional on the args."""
        fuente = open(os.path.join(RAIZ, "main.py"), encoding="utf-8").read()
        self.assertRegex(fuente, r"(?m)^import multiprocessing$")

    def test_version_y_ayuda(self):
        self.assertEqual(main.principal(["--version"]), 0)
        self.assertEqual(main.principal(["-V"]), 0)
        # Careful: if `--help` stopped being recognised, this call would fall
        # into the GUI branch and the test would hang with a window open
        # (this already happened with the old flag).
        self.assertEqual(main.principal(["--help"]), 0)
        self.assertEqual(main.principal(["-h"]), 0)

    def test_la_bandera_del_motor_va_al_motor(self):
        import motor_flac
        with mock.patch.object(motor_flac, "cli_principal", return_value=0) as motor:
            codigo = main.principal(["--motor-cli", "--path", "C:/Album", "--pdf"])
        self.assertEqual(codigo, 0)
        motor.assert_called_once_with(["--path", "C:/Album", "--pdf"])

    def test_la_bandera_cli_va_al_cli_interactivo(self):
        import verificar_flac
        with mock.patch.object(verificar_flac, "main", return_value=0) as cli:
            codigo = main.principal(["--cli"])
        self.assertEqual(codigo, 0)
        cli.assert_called_once_with([])

    def test_por_defecto_abre_la_interfaz(self):
        with mock.patch.object(gui, "main", return_value=0) as interfaz:
            self.assertEqual(main.principal([]), 0)
        interfaz.assert_called_once_with(None)

    def test_la_interfaz_acepta_la_ruta_inicial(self):
        for argumentos, esperada in ((["--gui", "D:/Music"], "D:/Music"),
                                     (["-g", "D:/Music"], "D:/Music"),
                                     (["D:/Music"], "D:/Music")):
            with self.subTest(argumentos=argumentos):
                with mock.patch.object(gui, "main", return_value=0) as interfaz:
                    main.principal(argumentos)
                interfaz.assert_called_once_with(esperada)

    def test_si_falla_la_interfaz_avisa_sin_trazas(self):
        with mock.patch.dict(sys.modules, {"gui": None}):
            self.assertEqual(main.principal([]), 1)


class TestEjecutableDelMotor(unittest.TestCase):
    """`flac_motor.exe` is the engine: its arguments do NOT open the window.

    This escaped once and it is one of the failures that look worst: without this
    rule, `flac_motor.exe --path …` opened the GUI (a window that never finishes
    and prints nothing), so a script or an automated check waited forever.
    """

    def _como_motor_exe(self):
        return mock.patch.multiple(
            sys, create=True, frozen=True,
            executable=os.path.join(RAIZ, "dist", "FLAC_Verifier",
                                    main.NOMBRE_MOTOR_EXE))

    def test_las_opciones_del_motor_van_al_motor_sin_banderas(self):
        import motor_flac
        with self._como_motor_exe(), \
             mock.patch.object(motor_flac, "cli_principal", return_value=0) as motor:
            codigo = main.principal(["--path", "C:/Album", "--mode", "center"])
        self.assertEqual(codigo, 0)
        motor.assert_called_once_with(["--path", "C:/Album", "--mode", "center"])

    def test_sin_argumentos_muestra_la_ayuda_y_no_abre_ventana(self):
        with self._como_motor_exe(), mock.patch.object(gui, "main") as interfaz:
            self.assertEqual(main.principal([]), 0)
        interfaz.assert_not_called()

    def test_la_version_sigue_funcionando(self):
        with self._como_motor_exe():
            self.assertEqual(main.principal(["--version"]), 0)

    def test_puede_abrir_la_ventana_si_se_pide(self):
        with self._como_motor_exe(), \
             mock.patch.object(gui, "main", return_value=0) as interfaz:
            self.assertEqual(main.principal(["--gui", "D:/Music"]), 0)
        interfaz.assert_called_once_with("D:/Music")

    def test_desde_el_codigo_fuente_no_cambia_nada(self):
        """Without freezing there is no `flac_motor.exe`: `main.py` still opens the GUI."""
        import motor_flac
        with mock.patch.object(motor_flac, "cli_principal") as motor, \
             mock.patch.object(gui, "main", return_value=0) as interfaz:
            self.assertEqual(main.principal(["--path", "C:/Album"]), 0)
        motor.assert_not_called()
        interfaz.assert_called_once_with(None)

    def test_el_nombre_del_motor_coincide_con_el_del_spec(self):
        spec = open(SPEC, encoding="utf-8").read()
        self.assertIn(f'NOMBRE_MOTOR = "{main.NOMBRE_MOTOR_EXE[:-4]}"', spec)
        self.assertEqual(gui.NOMBRE_MOTOR_EXE, main.NOMBRE_MOTOR_EXE)
        self.assertEqual(main.NOMBRE_MOTOR_EXE, "flac_motor.exe")


class TestMotorEmpaquetado(unittest.TestCase):
    """How the engine command is built depending on how everything is run."""

    def test_en_modo_fuente_usa_el_interprete_con_u(self):
        with mock.patch.object(gui, "empaquetado", return_value=False):
            comando = gui.construir_comando("C:/Album")
        self.assertEqual(comando[0], sys.executable)
        self.assertEqual(comando[1], "-u")                 # real time
        self.assertTrue(comando[2].endswith("motor_flac.py"))

    def test_empaquetado_prefiere_el_ejecutable_del_motor(self):
        # The real layout (the one the .spec produces): both .exe files in the
        # SAME folder, dist/FLAC_Verifier/.
        carpeta = os.path.join(RAIZ, "dist", "FLAC_Verifier")
        falso_gui = os.path.join(carpeta, "FLAC_Verifier.exe")
        falso_motor = os.path.join(carpeta, "flac_motor.exe")
        with mock.patch.object(gui, "empaquetado", return_value=True), \
             mock.patch.object(gui.sys, "executable", falso_gui), \
             mock.patch.object(gui.os.path, "exists", lambda ruta: ruta == falso_motor):
            comando = gui.construir_comando("C:/Album")
        self.assertEqual(comando[0], falso_motor)
        self.assertNotIn("-u", comando)                    # the .exe is already unbuffered
        # When packaged, the internal flag ALWAYS goes in: that way the call does
        # not depend on guessing which binary it is (and the windowed .exe does
        # not open its window).
        self.assertIn("--motor-cli", comando)
        self.assertEqual(comando[1], "--motor-cli")

    def test_el_motor_se_busca_junto_a_la_gui(self):
        """If the engine is not next to it, no path is invented: the same .exe is used."""
        carpeta = os.path.join(RAIZ, "dist", "FLAC_Verifier")
        with mock.patch.object(gui, "empaquetado", return_value=True), \
             mock.patch.object(gui.sys, "executable",
                               os.path.join(carpeta, "FLAC_Verifier.exe")), \
             mock.patch.object(gui.os.path, "exists", return_value=False):
            self.assertEqual(gui.ruta_motor(),
                             [os.path.join(carpeta, "FLAC_Verifier.exe"), "--motor-cli"])

    def test_ningun_comando_empaquetado_depende_del_nombre_del_binario(self):
        """The engine is never invoked without the flag when it is packaged.

        `flac_motor.exe` interprets its options as the engine even without the
        flag, but the GUI must not depend on that: if someone renames the
        executable, the flag still decides.
        """
        carpeta = os.path.join(RAIZ, "dist", "FLAC_Verifier")
        casos = (
            ("with the engine next to it", lambda ruta: ruta.endswith(gui.NOMBRE_MOTOR_EXE)),
            ("without the engine next to it", lambda ruta: False),
        )
        for nombre, existe in casos:
            with self.subTest(caso=nombre):
                with mock.patch.object(gui, "empaquetado", return_value=True), \
                     mock.patch.object(gui.sys, "executable",
                                       os.path.join(carpeta, "FLAC_Verifier.exe")), \
                     mock.patch.object(gui.os.path, "exists", existe):
                    comando = gui.construir_comando("C:/Album")
                self.assertIn("--motor-cli", comando)

    def test_empaquetado_sin_el_ejecutable_usa_la_bandera_interna(self):
        """This is the single .exe case: the engine lives in the same binary."""
        with mock.patch.object(gui, "empaquetado", return_value=True), \
             mock.patch.object(gui.sys, "executable",
                               os.path.join(RAIZ, "dist", "FLAC_Verifier.exe")), \
             mock.patch.object(gui.os.path, "exists", return_value=False):
            comando = gui.construir_comando("C:/Album")
        self.assertEqual(comando[0], os.path.join(RAIZ, "dist", "FLAC_Verifier.exe"))
        self.assertEqual(comando[1], "--motor-cli")
        self.assertNotIn("-u", comando)

    def test_las_opciones_siguen_igual_en_ambos_modos(self):
        with mock.patch.object(gui, "empaquetado", return_value=True), \
             mock.patch.object(gui, "ruta_motor", return_value=["C:/dist/flac_motor.exe"]):
            comando = gui.construir_comando("C:/Album", modo="seconds", segundos=30,
                                            pdf=True, workers=4, sin_cache=True)
        self.assertEqual(comando, ["C:/dist/flac_motor.exe", "--path", "C:/Album",
                                   "--mode", "seconds", "--seconds", "30", "--pdf",
                                   "--workers", "4", "--no-cache"])

    def test_el_motor_expone_una_funcion_para_el_ejecutable(self):
        import motor_flac
        self.assertTrue(callable(motor_flac.cli_principal))

    def test_el_motor_asegura_los_flujos_cuando_no_hay_consola(self):
        """A windowed .exe leaves stdout=None and print() would write nothing."""
        import motor_flac
        original = sys.stdout
        try:
            sys.stdout = None
            motor_flac._asegurar_flujos()
            self.assertIsNotNone(sys.stdout)
            print("this must be printable")
        finally:
            sys.stdout = original


class TestEspecificacion(unittest.TestCase):
    """The .spec must collect everything the project needs."""

    @classmethod
    def setUpClass(cls):
        cls.fuente = open(SPEC, encoding="utf-8").read()

    def test_existe_y_es_ejecutable_como_python(self):
        compile(self.fuente, SPEC, "exec")      # valid syntax

    def test_recolecta_los_paquetes_con_datos(self):
        for paquete in ("customtkinter", "tkinterdnd2", "soundfile",
                        "reportlab", "matplotlib"):
            with self.subTest(paquete=paquete):
                self.assertIn(f'"{paquete}"', self.fuente)
        self.assertIn("collect_all", self.fuente)

    def test_declara_las_dll_de_soundfile(self):
        self.assertIn("collect_dynamic_libs", self.fuente)
        self.assertIn("soundfile", self.fuente)

    def test_incluye_los_submodulos_de_scipy_y_numpy(self):
        for modulo in ("numpy.fft", "scipy.fft", "scipy.signal",
                       "scipy.special", "scipy.linalg"):
            with self.subTest(modulo=modulo):
                self.assertIn(f'"{modulo}"', self.fuente)

    def test_declara_los_modulos_propios_de_importacion_perezosa(self):
        for modulo in ("motor_flac", "informe_pdf", "verificar_flac", "gui"):
            with self.subTest(modulo=modulo):
                self.assertIn(f'"{modulo}"', self.fuente)

    def test_modo_carpeta_con_dos_ejecutables(self):
        self.assertIn("COLLECT(", self.fuente)
        self.assertIn('name="FLAC_Verifier"', self.fuente)      # dist/FLAC_Verifier
        # The engine name is declared only once, in a constant, so that the
        # .spec and the tests cannot drift apart.
        self.assertIn('NOMBRE_MOTOR = "flac_motor"', self.fuente)
        self.assertIn("name=NOMBRE_MOTOR", self.fuente)
        self.assertEqual(self.fuente.count("EXE("), 2)

    def test_la_gui_va_sin_consola_y_el_motor_con_consola(self):
        gui_exe = self.fuente.split("exe_gui = EXE(", 1)[1].split(")", 1)[0]
        motor_exe = self.fuente.split("exe_motor = EXE(", 1)[1].split(")", 1)[0]
        self.assertIn("console=False", gui_exe)
        self.assertIn("console=True", motor_exe)

    def test_lleva_icono_y_recursos_de_version(self):
        self.assertIn("version_info.txt", self.fuente)
        self.assertIn("Logo/icono_app.ico", self.fuente)
        self.assertIn("icon=ICONO", self.fuente)
        self.assertIn("version=VERSION", self.fuente)

    def test_incluye_los_assets_del_logo(self):
        for asset in ("logo_flac_verifier.png", "logo_flac_verifier_128.png",
                      "icono_app.ico"):
            with self.subTest(asset=asset):
                self.assertIn(asset, self.fuente)

    def test_no_usa_upx(self):
        """UPX compresses more, but multiplies antivirus false positives."""
        self.assertIn("upx=False", self.fuente)
        self.assertNotIn("upx=True", self.fuente)

    def test_los_recursos_de_version_son_coherentes(self):
        # PyInstaller runs this file with ITS classes (VSVersionInfo,
        # FixedFileInfo...) already defined; here we supply doubles that accept
        # any argument to check that the file is valid Python.
        def nodo(*args, **kwargs):
            return args, kwargs

        espacio = {nombre: nodo for nombre in
                   ("VSVersionInfo", "FixedFileInfo", "StringFileInfo",
                    "StringTable", "StringStruct", "VarFileInfo", "VarStruct")}
        exec(open(VERSION_INFO, encoding="utf-8").read(), espacio)   # pyinstaller runs it
        self.assertIn("VSVersionInfo", espacio)
        texto = open(VERSION_INFO, encoding="utf-8").read()
        self.assertIn("1.0.0", texto)
        self.assertIn("FLAC VERIFIER", texto)
        self.assertIn("LegalCopyright", texto)
        self.assertIn("OriginalFilename", texto)

    def test_el_constructor_existe_y_es_python_valido(self):
        compile(open(os.path.join(RAIZ, "build.py"), encoding="utf-8").read(),
                "build.py", "exec")

    def test_el_constructor_limpia_y_verifica(self):
        fuente = open(os.path.join(RAIZ, "build.py"), encoding="utf-8").read()
        for esperado in ("shutil.rmtree", "PyInstaller", "--noconfirm",
                         "FLAC_Verifier.exe", "flac_motor.exe", "--version"):
            with self.subTest(esperado=esperado):
                self.assertIn(esperado, fuente)


class TestComparacionConLaFuente(unittest.TestCase):
    """Step 7 of the verifier: executable and source code, the same records.

    Packaging must not change the analysis. This is the check that proves it, and
    these tests pin down that it compares what it has to compare (and that it does
    not compare what changes on its own, such as the path or the cache mark).
    """

    @classmethod
    def setUpClass(cls):
        import comprobar_ejecutable as ce
        cls.ce = ce

    @staticmethod
    def _linea(**campos):
        linea = {"tipo": "resultado", "indice": 1, "total": 1}
        linea.update(campos)
        return json.dumps(linea, ensure_ascii=False)

    def test_las_fichas_se_indexan_por_archivo(self):
        texto = "\n".join([
            self._linea(archivo="01.flac", veredicto="GENUINE LOSSLESS"),
            '{"tipo": "inicio", "total": 2}',
            self._linea(archivo="02.flac", veredicto="SUSPICIOUS"),
            '{"tipo": "fin"}',
        ])
        fichas = self.ce.Comprobador.fichas(texto)
        self.assertEqual(sorted(fichas), ["01.flac", "02.flac"])
        self.assertEqual(fichas["02.flac"]["veredicto"], "SUSPICIOUS")

    def test_no_se_comparan_los_campos_que_cambian_solos(self):
        texto = self._linea(archivo="01.flac", veredicto="GENUINE LOSSLESS",
                            ruta="C:/other/path.flac", espectrograma="C:/x.png",
                            desde_cache=True, md5="abc123")
        ficha = self.ce.Comprobador.fichas(texto)["01.flac"]
        for volatil in ("ruta", "espectrograma", "desde_cache"):
            self.assertNotIn(volatil, ficha, f"{volatil} must not be compared")
        # …but the analysis itself does: that is what has to match.
        self.assertEqual(ficha["md5"], "abc123")
        self.assertEqual(ficha["veredicto"], "GENUINE LOSSLESS")

    def test_las_lineas_ilegibles_no_tumban_la_comparacion(self):
        texto = "\n".join(["{this is not json", "", "   ",
                           self._linea(archivo="01.flac", veredicto="SUSPICIOUS")])
        self.assertEqual(list(self.ce.Comprobador.fichas(texto)), ["01.flac"])

    def test_un_archivo_corrupto_cuenta_igual_en_los_dos_lados(self):
        """The error of an unreadable FLAC also has to match."""
        texto = json.dumps({"tipo": "resultado", "archivo": "06_corrupto.flac",
                            "error": "Could not read FLAC metadata"}, ensure_ascii=False)
        ficha = self.ce.Comprobador.fichas(texto)["06_corrupto.flac"]
        self.assertIn("error", ficha)
        self.assertNotIn("veredicto", ficha)


class TestCacheDeMatplotlib(unittest.TestCase):
    """The font cache must not pollute the protocol the GUI reads.

    If matplotlib cannot write its cache, it warns on stderr; the engine talks to
    the GUI on stdout with JSON lines, so that warning would appear in the
    window's log as an unreadable line. Worse still: it would rebuild the cache on
    every run (slower on every spectrogram).
    """

    @classmethod
    def setUpClass(cls):
        import motor_flac
        cls.motor = motor_flac

    def setUp(self):
        self.entorno = mock.patch.dict(os.environ, {}, clear=False)
        self.entorno.start()
        os.environ.pop("MPLCONFIGDIR", None)

    def tearDown(self):
        self.entorno.stop()

    def test_se_elige_una_carpeta_escribible_del_usuario(self):
        # The checks go inside the `with`: on exit, mock undoes the environment
        # dictionary and with it the variable it has just set.
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": tempfile.gettempdir()}):
            elegida = self.motor.preparar_cache_matplotlib()
            self.assertTrue(elegida, "no folder was chosen")
            self.assertTrue(os.path.isdir(elegida))
            self.assertEqual(os.environ["MPLCONFIGDIR"], elegida)
            self.assertIn("FLAC_VERIFIER", elegida)
            shutil.rmtree(os.path.dirname(elegida), ignore_errors=True)

    def test_lo_que_configura_el_usuario_no_se_toca(self):
        with mock.patch.dict(os.environ, {"MPLCONFIGDIR": "C:/mine"}):
            self.assertIsNone(self.motor.preparar_cache_matplotlib())
            self.assertEqual(os.environ["MPLCONFIGDIR"], "C:/mine")

    def test_si_no_se_puede_crear_no_se_rompe_nada(self):
        """With no permission on any candidate, the default value is left."""
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": "C:/made_up"}), \
             mock.patch.object(self.motor.os, "makedirs", side_effect=PermissionError):
            self.assertIsNone(self.motor.preparar_cache_matplotlib())
        self.assertNotIn("MPLCONFIGDIR", os.environ)

    def test_se_decide_antes_de_importar_matplotlib(self):
        fuente = open(os.path.join(RAIZ, "motor_flac.py"), encoding="utf-8").read()
        llamada = fuente.index("preparar_cache_matplotlib()\n\ntry:")
        self.assertLess(llamada, fuente.index("import matplotlib"),
                        "the variable is read when matplotlib is imported: the call goes before")


if __name__ == "__main__":
    unittest.main(verbosity=2)
