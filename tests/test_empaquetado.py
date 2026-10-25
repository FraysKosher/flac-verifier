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
import sys
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

    def test_freeze_support_is_first_block_main(self):
        fuente = open(os.path.join(RAIZ, "main.py"), encoding="utf-8").read()
        bloque = fuente.split('if __name__ == "__main__":', 1)
        self.assertEqual(len(bloque), 2, "main.py has no __main__ block")
        cuerpo = [linea.strip() for linea in bloque[1].splitlines()
                  if linea.strip() and not linea.strip().startswith("#")]
        self.assertTrue(cuerpo, "the __main__ block is empty")
        self.assertEqual(cuerpo[0], "multiprocessing.freeze_support()",
                         "freeze_support() must be the first statement: "
                         "otherwise every worker of the parallel analysis reopens the app")

    def test_multiprocessing_no_imports_inside_function(self):
        """Importing it late would work, but it must not be conditional on the args."""
        fuente = open(os.path.join(RAIZ, "main.py"), encoding="utf-8").read()
        self.assertRegex(fuente, r"(?m)^import multiprocessing$")

    def test_version_help(self):
        self.assertEqual(main.main(["--version"]), 0)
        self.assertEqual(main.main(["-V"]), 0)
        # Careful: if `--help` stopped being recognised, this call would fall
        # into the GUI branch and the test would hang with a window open
        # (this already happened with the old flag).
        self.assertEqual(main.main(["--help"]), 0)
        self.assertEqual(main.main(["-h"]), 0)

    def test_flag_engine_goes_engine(self):
        import motor_flac
        with mock.patch.object(motor_flac, "main_cli", return_value=0) as motor:
            codigo = main.main(["--motor-cli", "--path", "C:/Album", "--pdf"])
        self.assertEqual(codigo, 0)
        motor.assert_called_once_with(["--path", "C:/Album", "--pdf"])

    def test_flag_cli_goes_cli_interactive(self):
        import verificar_flac
        with mock.patch.object(verificar_flac, "main", return_value=0) as cli:
            codigo = main.main(["--cli"])
        self.assertEqual(codigo, 0)
        cli.assert_called_once_with([])

    def test_by_default_opens_ui(self):
        with mock.patch.object(gui, "main", return_value=0) as interfaz:
            self.assertEqual(main.main([]), 0)
        interfaz.assert_called_once_with(None)

    def test_ui_accepts_path_initial(self):
        for argumentos, esperada in ((["--gui", "D:/Music"], "D:/Music"),
                                     (["-g", "D:/Music"], "D:/Music"),
                                     (["D:/Music"], "D:/Music")):
            with self.subTest(argumentos=argumentos):
                with mock.patch.object(gui, "main", return_value=0) as interfaz:
                    main.main(argumentos)
                interfaz.assert_called_once_with(esperada)

    def test_if_fails_ui_warns_without_tracebacks(self):
        with mock.patch.dict(sys.modules, {"gui": None}):
            self.assertEqual(main.main([]), 1)


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
                                    main.ENGINE_EXE_NAME))

    def test_options_engine_go_engine_without_flags(self):
        import motor_flac
        with self._como_motor_exe(), \
             mock.patch.object(motor_flac, "main_cli", return_value=0) as motor:
            codigo = main.main(["--path", "C:/Album", "--mode", "center"])
        self.assertEqual(codigo, 0)
        motor.assert_called_once_with(["--path", "C:/Album", "--mode", "center"])

    def test_without_arguments_sample_help_no_opens_window(self):
        with self._como_motor_exe(), mock.patch.object(gui, "main") as interfaz:
            self.assertEqual(main.main([]), 0)
        interfaz.assert_not_called()

    def test_version_still_working(self):
        with self._como_motor_exe():
            self.assertEqual(main.main(["--version"]), 0)

    def test_can_open_window_if_requests(self):
        with self._como_motor_exe(), \
             mock.patch.object(gui, "main", return_value=0) as interfaz:
            self.assertEqual(main.main(["--gui", "D:/Music"]), 0)
        interfaz.assert_called_once_with("D:/Music")

    def test_from_code_source_no_changes_nothing(self):
        """Without freezing there is no `flac_motor.exe`: `main.py` still opens the GUI."""
        import motor_flac
        with mock.patch.object(motor_flac, "main_cli") as motor, \
             mock.patch.object(gui, "main", return_value=0) as interfaz:
            self.assertEqual(main.main(["--path", "C:/Album"]), 0)
        motor.assert_not_called()
        interfaz.assert_called_once_with(None)

    def test_name_engine_matches_with_spec(self):
        spec = open(SPEC, encoding="utf-8").read()
        self.assertIn(f'NOMBRE_MOTOR = "{main.ENGINE_EXE_NAME[:-4]}"', spec)
        self.assertEqual(gui.ENGINE_EXE_NAME, main.ENGINE_EXE_NAME)
        self.assertEqual(main.ENGINE_EXE_NAME, "flac_motor.exe")


class TestMotorEmpaquetado(unittest.TestCase):
    """How the engine command is built depending on how everything is run."""

    def test_mode_source_uses_interpreter_with_u(self):
        with mock.patch.object(gui, "packaging", return_value=False):
            comando = gui.build_command("C:/Album")
        self.assertEqual(comando[0], sys.executable)
        self.assertEqual(comando[1], "-u")                 # real time
        self.assertTrue(comando[2].endswith("motor_flac.py"))

    def test_packaging_prefers_executable_engine(self):
        # The real layout (the one the .spec produces): both .exe files in the
        # SAME folder, dist/FLAC_Verifier/.
        carpeta = os.path.join(RAIZ, "dist", "FLAC_Verifier")
        falso_gui = os.path.join(carpeta, "FLAC_Verifier.exe")
        falso_motor = os.path.join(carpeta, "flac_motor.exe")
        with mock.patch.object(gui, "packaging", return_value=True), \
             mock.patch.object(gui.sys, "executable", falso_gui), \
             mock.patch.object(gui.os.path, "exists", lambda ruta: ruta == falso_motor):
            comando = gui.build_command("C:/Album")
        self.assertEqual(comando[0], falso_motor)
        self.assertNotIn("-u", comando)                    # the .exe is already unbuffered
        # When packaged, the internal flag ALWAYS goes in: that way the call does
        # not depend on guessing which binary it is (and the windowed .exe does
        # not open its window).
        self.assertIn("--motor-cli", comando)
        self.assertEqual(comando[1], "--motor-cli")

    def test_engine_searches_next_to_gui(self):
        """If the engine is not next to it, no path is invented: the same .exe is used."""
        carpeta = os.path.join(RAIZ, "dist", "FLAC_Verifier")
        with mock.patch.object(gui, "packaging", return_value=True), \
             mock.patch.object(gui.sys, "executable",
                               os.path.join(carpeta, "FLAC_Verifier.exe")), \
             mock.patch.object(gui.os.path, "exists", return_value=False):
            self.assertEqual(gui.engine_path(),
                             [os.path.join(carpeta, "FLAC_Verifier.exe"), "--motor-cli"])

    def test_no_command_packaging_depends_name_binary(self):
        """The engine is never invoked without the flag when it is packaged.

        `flac_motor.exe` interprets its options as the engine even without the
        flag, but the GUI must not depend on that: if someone renames the
        executable, the flag still decides.
        """
        carpeta = os.path.join(RAIZ, "dist", "FLAC_Verifier")
        casos = (
            ("with the engine next to it", lambda ruta: ruta.endswith(gui.ENGINE_EXE_NAME)),
            ("without the engine next to it", lambda ruta: False),
        )
        for nombre, existe in casos:
            with self.subTest(caso=nombre):
                with mock.patch.object(gui, "packaging", return_value=True), \
                     mock.patch.object(gui.sys, "executable",
                                       os.path.join(carpeta, "FLAC_Verifier.exe")), \
                     mock.patch.object(gui.os.path, "exists", existe):
                    comando = gui.build_command("C:/Album")
                self.assertIn("--motor-cli", comando)

    def test_packaging_without_executable_uses_flag_internal(self):
        """This is the single .exe case: the engine lives in the same binary."""
        with mock.patch.object(gui, "packaging", return_value=True), \
             mock.patch.object(gui.sys, "executable",
                               os.path.join(RAIZ, "dist", "FLAC_Verifier.exe")), \
             mock.patch.object(gui.os.path, "exists", return_value=False):
            comando = gui.build_command("C:/Album")
        self.assertEqual(comando[0], os.path.join(RAIZ, "dist", "FLAC_Verifier.exe"))
        self.assertEqual(comando[1], "--motor-cli")
        self.assertNotIn("-u", comando)

    def test_options_follow_equal_both_modes(self):
        with mock.patch.object(gui, "packaging", return_value=True), \
             mock.patch.object(gui, "engine_path", return_value=["C:/dist/flac_motor.exe"]):
            comando = gui.build_command("C:/Album", mode="seconds", seconds=30,
                                            pdf=True, workers=4, without_cache=True)
        self.assertEqual(comando, ["C:/dist/flac_motor.exe", "--path", "C:/Album",
                                   "--mode", "seconds", "--seconds", "30", "--pdf",
                                   "--workers", "4", "--no-cache"])

    def test_engine_exposes_function_for_executable(self):
        import motor_flac
        self.assertTrue(callable(motor_flac.main_cli))

    def test_engine_ensures_streams_when_no_there_is_console(self):
        """A windowed .exe leaves stdout=None and print() would write nothing."""
        import motor_flac
        original = sys.stdout
        try:
            sys.stdout = None
            motor_flac._ensure_streams()
            self.assertIsNotNone(sys.stdout)
            print("this must be printable")
        finally:
            sys.stdout = original


class TestEspecificacion(unittest.TestCase):
    """The .spec must collect everything the project needs."""

    @classmethod
    def setUpClass(cls):
        cls.fuente = open(SPEC, encoding="utf-8").read()

    def test_exists_is_executable_as_python(self):
        compile(self.fuente, SPEC, "exec")      # valid syntax

    def test_collects_packages_with_data(self):
        for paquete in ("customtkinter", "tkinterdnd2", "soundfile",
                        "reportlab", "PIL"):
            with self.subTest(paquete=paquete):
                self.assertIn(f'"{paquete}"', self.fuente)
        self.assertIn("collect_all", self.fuente)

    def test_declares_dll_soundfile(self):
        self.assertIn("collect_dynamic_libs", self.fuente)
        self.assertIn("soundfile", self.fuente)

    def test_includes_submodules_scipy_numpy(self):
        for modulo in ("numpy.fft", "scipy.fft", "scipy.signal",
                       "scipy.special", "scipy.linalg"):
            with self.subTest(modulo=modulo):
                self.assertIn(f'"{modulo}"', self.fuente)

    def test_declares_modules_own_import_lazy(self):
        for modulo in ("motor_flac", "informe_pdf", "verificar_flac", "gui"):
            with self.subTest(modulo=modulo):
                self.assertIn(f'"{modulo}"', self.fuente)

    def test_mode_folder_with_two_executables(self):
        self.assertIn("COLLECT(", self.fuente)
        self.assertIn('name="FLAC_Verifier"', self.fuente)      # dist/FLAC_Verifier
        # The engine name is declared only once, in a constant, so that the
        # .spec and the tests cannot drift apart.
        self.assertIn('NOMBRE_MOTOR = "flac_motor"', self.fuente)
        self.assertIn("name=NOMBRE_MOTOR", self.fuente)
        self.assertEqual(self.fuente.count("EXE("), 2)

    def test_gui_goes_without_console_engine_with_console(self):
        gui_exe = self.fuente.split("exe_gui = EXE(", 1)[1].split(")", 1)[0]
        motor_exe = self.fuente.split("exe_motor = EXE(", 1)[1].split(")", 1)[0]
        self.assertIn("console=False", gui_exe)
        self.assertIn("console=True", motor_exe)

    def test_carries_icon_resources_version(self):
        self.assertIn("version_info.txt", self.fuente)
        self.assertIn("Logo/icono_app.ico", self.fuente)
        self.assertIn("icon=ICONO", self.fuente)
        self.assertIn("version=VERSION", self.fuente)

    def test_includes_assets_logo(self):
        for asset in ("logo_flac_verifier.png", "logo_flac_verifier_128.png",
                      "icono_app.ico"):
            with self.subTest(asset=asset):
                self.assertIn(asset, self.fuente)

    def test_no_uses_upx(self):
        """UPX compresses more, but multiplies antivirus false positives."""
        self.assertIn("upx=False", self.fuente)
        self.assertNotIn("upx=True", self.fuente)

    def test_resources_version_are_coherent(self):
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
        # The Windows resources must announce the same version as the program, and
        # the check reads it from the one place that defines it instead of repeating
        # the literal: the tag, the release and the executable drifted apart once
        # precisely because these were independent numbers.
        self.assertIn(main.VERSION, texto)
        self.assertIn(f'"{main.VERSION}.0"', texto)      # the filevers style: 2.0.0.0
        self.assertIn("FLAC VERIFIER", texto)
        self.assertIn("LegalCopyright", texto)
        self.assertIn("OriginalFilename", texto)

    def test_builder_announces_version_program(self):
        """The build banner takes the version from `main.py`, not from its own copy."""
        fuente = open(os.path.join(RAIZ, "build.py"), encoding="utf-8").read()
        self.assertIn("project_version()", fuente)
        self.assertNotRegex(fuente, r"BUILDING FLAC VERIFIER \d")

    def test_builder_exists_is_python_valid(self):
        compile(open(os.path.join(RAIZ, "build.py"), encoding="utf-8").read(),
                "build.py", "exec")

    def test_builder_clean_verifies(self):
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

    def test_cards_index_by_file(self):
        texto = "\n".join([
            self._linea(archivo="01.flac", veredicto="GENUINE LOSSLESS"),
            '{"tipo": "inicio", "total": 2}',
            self._linea(archivo="02.flac", veredicto="SUSPICIOUS"),
            '{"tipo": "fin"}',
        ])
        fichas = self.ce.Comprobador.cards(texto)
        self.assertEqual(sorted(fichas), ["01.flac", "02.flac"])
        self.assertEqual(fichas["02.flac"]["veredicto"], "SUSPICIOUS")

    def test_no_compare_fields_change_alone(self):
        texto = self._linea(archivo="01.flac", veredicto="GENUINE LOSSLESS",
                            ruta="C:/other/path.flac", espectrograma="C:/x.png",
                            desde_cache=True, md5="abc123")
        ficha = self.ce.Comprobador.cards(texto)["01.flac"]
        for volatil in ("ruta", "espectrograma", "desde_cache"):
            self.assertNotIn(volatil, ficha, f"{volatil} must not be compared")
        # …but the analysis itself does: that is what has to match.
        self.assertEqual(ficha["md5"], "abc123")
        self.assertEqual(ficha["veredicto"], "GENUINE LOSSLESS")

    def test_lines_unreadable_no_knock_down_comparison(self):
        texto = "\n".join(["{this is not json", "", "   ",
                           self._linea(archivo="01.flac", veredicto="SUSPICIOUS")])
        self.assertEqual(list(self.ce.Comprobador.cards(texto)), ["01.flac"])

    def test_file_corrupt_counts_equal_two_sides(self):
        """The error of an unreadable FLAC also has to match."""
        texto = json.dumps({"tipo": "resultado", "archivo": "06_corrupto.flac",
                            "error": "Could not read FLAC metadata"}, ensure_ascii=False)
        ficha = self.ce.Comprobador.cards(texto)["06_corrupto.flac"]
        self.assertIn("error", ficha)
        self.assertNotIn("veredicto", ficha)


class TestRenderizadorSinMatplotlib(unittest.TestCase):
    """The spectrogram is a NumPy + Pillow raster: matplotlib is not packaged."""

    def test_engine_no_imports_matplotlib(self):
        fuente = open(os.path.join(RAIZ, "motor_flac.py"), encoding="utf-8").read()
        self.assertNotIn("import matplotlib", fuente)
        self.assertNotIn("matplotlib.use", fuente)

    def test_engine_uses_pillow_lut(self):
        fuente = open(os.path.join(RAIZ, "motor_flac.py"), encoding="utf-8").read()
        self.assertIn("from PIL import Image", fuente)
        self.assertIn("_LUT_SPECTROGRAM", fuente)
        self.assertIn("Image.fromarray(", fuente)

    def test_el_spec_excluye_matplotlib(self):
        fuente = open(SPEC, encoding="utf-8").read()
        bloque = fuente.split("excludes=[", 1)[1]
        self.assertIn('"matplotlib"', bloque)
        self.assertNotIn('collect_data_files("matplotlib")', fuente)

    def test_spec_collects_pillow(self):
        self.assertIn('"PIL"', open(SPEC, encoding="utf-8").read())

if __name__ == "__main__":
    unittest.main(verbosity=2)
