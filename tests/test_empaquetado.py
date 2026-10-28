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

ROOT = fx.ROOT
SPEC = os.path.join(ROOT, "flac_verifier.spec")
VERSION_INFO = os.path.join(ROOT, "version_info.txt")


class test_point_input(unittest.TestCase):

    def test_freeze_support_is_first_block_main(self):
        source = open(os.path.join(ROOT, "main.py"), encoding="utf-8").read()
        block = source.split('if __name__ == "__main__":', 1)
        self.assertEqual(len(block), 2, "main.py has no __main__ block")
        body = [line.strip() for line in block[1].splitlines()
                  if line.strip() and not line.strip().startswith("#")]
        self.assertTrue(body, "the __main__ block is empty")
        self.assertEqual(body[0], "multiprocessing.freeze_support()",
                         "freeze_support() must be the first statement: "
                         "otherwise every worker of the parallel analysis reopens the app")

    def test_multiprocessing_no_imports_inside_function(self):
        """Importing it late would work, but it must not be conditional on the args."""
        source = open(os.path.join(ROOT, "main.py"), encoding="utf-8").read()
        self.assertRegex(source, r"(?m)^import multiprocessing$")

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
        with mock.patch.object(motor_flac, "main_cli", return_value=0) as engine:
            code = main.main(["--motor-cli", "--path", "C:/Album", "--pdf"])
        self.assertEqual(code, 0)
        engine.assert_called_once_with(["--path", "C:/Album", "--pdf"])

    def test_flag_cli_goes_cli_interactive(self):
        import verificar_flac
        with mock.patch.object(verificar_flac, "main", return_value=0) as cli:
            code = main.main(["--cli"])
        self.assertEqual(code, 0)
        cli.assert_called_once_with([])

    def test_by_default_opens_ui(self):
        with mock.patch.object(gui, "main", return_value=0) as ui:
            self.assertEqual(main.main([]), 0)
        ui.assert_called_once_with(None)

    def test_ui_accepts_path_initial(self):
        for arguments, expected in ((["--gui", "D:/Music"], "D:/Music"),
                                     (["-g", "D:/Music"], "D:/Music"),
                                     (["D:/Music"], "D:/Music")):
            with self.subTest(arguments=arguments):
                with mock.patch.object(gui, "main", return_value=0) as ui:
                    main.main(arguments)
                ui.assert_called_once_with(expected)

    def test_if_fails_ui_warns_without_tracebacks(self):
        with mock.patch.dict(sys.modules, {"gui": None}):
            self.assertEqual(main.main([]), 1)


class test_executable_engine(unittest.TestCase):
    """`flac_motor.exe` is the engine: its arguments do NOT open the window.

    This escaped once and it is one of the failures that look worst: without this
    rule, `flac_motor.exe --path …` opened the GUI (a window that never finishes
    and prints nothing), so a script or an automated check waited forever.
    """

    def _as_engine_exe(self):
        return mock.patch.multiple(
            sys, create=True, frozen=True,
            executable=os.path.join(ROOT, "dist", "FLAC_Verifier",
                                    main.ENGINE_EXE_NAME))

    def test_options_engine_go_engine_without_flags(self):
        import motor_flac
        with self._as_engine_exe(), \
             mock.patch.object(motor_flac, "main_cli", return_value=0) as engine:
            code = main.main(["--path", "C:/Album", "--mode", "center"])
        self.assertEqual(code, 0)
        engine.assert_called_once_with(["--path", "C:/Album", "--mode", "center"])

    def test_without_arguments_sample_help_no_opens_window(self):
        with self._as_engine_exe(), mock.patch.object(gui, "main") as ui:
            self.assertEqual(main.main([]), 0)
        ui.assert_not_called()

    def test_version_still_working(self):
        with self._as_engine_exe():
            self.assertEqual(main.main(["--version"]), 0)

    def test_can_open_window_if_requests(self):
        with self._as_engine_exe(), \
             mock.patch.object(gui, "main", return_value=0) as ui:
            self.assertEqual(main.main(["--gui", "D:/Music"]), 0)
        ui.assert_called_once_with("D:/Music")

    def test_from_code_source_no_changes_nothing(self):
        """Without freezing there is no `flac_motor.exe`: `main.py` still opens the GUI."""
        import motor_flac
        with mock.patch.object(motor_flac, "main_cli") as engine, \
             mock.patch.object(gui, "main", return_value=0) as ui:
            self.assertEqual(main.main(["--path", "C:/Album"]), 0)
        engine.assert_not_called()
        ui.assert_called_once_with(None)

    def test_name_engine_matches_with_spec(self):
        spec = open(SPEC, encoding="utf-8").read()
        self.assertIn(f'NOMBRE_MOTOR = "{main.ENGINE_EXE_NAME[:-4]}"', spec)
        self.assertEqual(gui.ENGINE_EXE_NAME, main.ENGINE_EXE_NAME)
        self.assertEqual(main.ENGINE_EXE_NAME, "flac_motor.exe")


class test_engine_packaging(unittest.TestCase):
    """How the engine command is built depending on how everything is run."""

    def test_mode_source_uses_interpreter_with_u(self):
        with mock.patch.object(gui, "packaging", return_value=False):
            command = gui.build_command("C:/Album")
        self.assertEqual(command[0], sys.executable)
        self.assertEqual(command[1], "-u")                 # real time
        self.assertTrue(command[2].endswith("motor_flac.py"))

    def test_packaging_prefers_executable_engine(self):
        # The real layout (the one the .spec produces): both .exe files in the
        # SAME folder, dist/FLAC_Verifier/.
        folder = os.path.join(ROOT, "dist", "FLAC_Verifier")
        false_gui = os.path.join(folder, "FLAC_Verifier.exe")
        false_engine = os.path.join(folder, "flac_motor.exe")
        with mock.patch.object(gui, "packaging", return_value=True), \
             mock.patch.object(gui.sys, "executable", false_gui), \
             mock.patch.object(gui.os.path, "exists", lambda path: path == false_engine):
            command = gui.build_command("C:/Album")
        self.assertEqual(command[0], false_engine)
        self.assertNotIn("-u", command)                    # the .exe is already unbuffered
        # When packaged, the internal flag ALWAYS goes in: that way the call does
        # not depend on guessing which binary it is (and the windowed .exe does
        # not open its window).
        self.assertIn("--motor-cli", command)
        self.assertEqual(command[1], "--motor-cli")

    def test_engine_searches_next_to_gui(self):
        """If the engine is not next to it, no path is invented: the same .exe is used."""
        folder = os.path.join(ROOT, "dist", "FLAC_Verifier")
        with mock.patch.object(gui, "packaging", return_value=True), \
             mock.patch.object(gui.sys, "executable",
                               os.path.join(folder, "FLAC_Verifier.exe")), \
             mock.patch.object(gui.os.path, "exists", return_value=False):
            self.assertEqual(gui.engine_path(),
                             [os.path.join(folder, "FLAC_Verifier.exe"), "--motor-cli"])

    def test_no_command_packaging_depends_name_binary(self):
        """The engine is never invoked without the flag when it is packaged.

        `flac_motor.exe` interprets its options as the engine even without the
        flag, but the GUI must not depend on that: if someone renames the
        executable, the flag still decides.
        """
        folder = os.path.join(ROOT, "dist", "FLAC_Verifier")
        cases = (
            ("with the engine next to it", lambda path: path.endswith(gui.ENGINE_EXE_NAME)),
            ("without the engine next to it", lambda path: False),
        )
        for name, exists in cases:
            with self.subTest(caso=name):
                with mock.patch.object(gui, "packaging", return_value=True), \
                     mock.patch.object(gui.sys, "executable",
                                       os.path.join(folder, "FLAC_Verifier.exe")), \
                     mock.patch.object(gui.os.path, "exists", exists):
                    command = gui.build_command("C:/Album")
                self.assertIn("--motor-cli", command)

    def test_packaging_without_executable_uses_flag_internal(self):
        """This is the single .exe case: the engine lives in the same binary."""
        with mock.patch.object(gui, "packaging", return_value=True), \
             mock.patch.object(gui.sys, "executable",
                               os.path.join(ROOT, "dist", "FLAC_Verifier.exe")), \
             mock.patch.object(gui.os.path, "exists", return_value=False):
            command = gui.build_command("C:/Album")
        self.assertEqual(command[0], os.path.join(ROOT, "dist", "FLAC_Verifier.exe"))
        self.assertEqual(command[1], "--motor-cli")
        self.assertNotIn("-u", command)

    def test_options_follow_equal_both_modes(self):
        with mock.patch.object(gui, "packaging", return_value=True), \
             mock.patch.object(gui, "engine_path", return_value=["C:/dist/flac_motor.exe"]):
            command = gui.build_command("C:/Album", mode="seconds", seconds=30,
                                            pdf=True, workers=4, without_cache=True)
        self.assertEqual(command, ["C:/dist/flac_motor.exe", "--path", "C:/Album",
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


class TestSpecification(unittest.TestCase):
    """The .spec must collect everything the project needs."""

    @classmethod
    def setUpClass(cls):
        cls.source = open(SPEC, encoding="utf-8").read()

    def test_exists_is_executable_as_python(self):
        compile(self.source, SPEC, "exec")      # valid syntax

    def test_collects_packages_with_data(self):
        for paquete in ("customtkinter", "tkinterdnd2", "soundfile",
                        "reportlab", "PIL"):
            with self.subTest(paquete=paquete):
                self.assertIn(f'"{paquete}"', self.source)
        self.assertIn("collect_all", self.source)

    def test_declares_dll_soundfile(self):
        self.assertIn("collect_dynamic_libs", self.source)
        self.assertIn("soundfile", self.source)

    def test_includes_submodules_scipy_numpy(self):
        for modulo in ("numpy.fft", "scipy.fft", "scipy.signal",
                       "scipy.special", "scipy.linalg"):
            with self.subTest(modulo=modulo):
                self.assertIn(f'"{modulo}"', self.source)

    def test_declares_modules_own_import_lazy(self):
        for modulo in ("motor_flac", "informe_pdf", "verificar_flac", "gui"):
            with self.subTest(modulo=modulo):
                self.assertIn(f'"{modulo}"', self.source)

    def test_mode_folder_with_two_executables(self):
        self.assertIn("COLLECT(", self.source)
        self.assertIn('name="FLAC_Verifier"', self.source)      # dist/FLAC_Verifier
        # The engine name is declared only once, in a constant, so that the
        # .spec and the tests cannot drift apart.
        self.assertIn('NOMBRE_MOTOR = "flac_motor"', self.source)
        self.assertIn("name=NOMBRE_MOTOR", self.source)
        self.assertEqual(self.source.count("EXE("), 2)

    def test_gui_goes_without_console_engine_with_console(self):
        gui_exe = self.source.split("exe_gui = EXE(", 1)[1].split(")", 1)[0]
        engine_exe = self.source.split("exe_motor = EXE(", 1)[1].split(")", 1)[0]
        self.assertIn("console=False", gui_exe)
        self.assertIn("console=True", engine_exe)

    def test_carries_icon_resources_version(self):
        self.assertIn("version_info.txt", self.source)
        self.assertIn("Logo/icono_app.ico", self.source)
        self.assertIn("icon=ICONO", self.source)
        self.assertIn("version=VERSION", self.source)

    def test_includes_assets_logo(self):
        for asset in ("logo_flac_verifier.png", "logo_flac_verifier_128.png",
                      "icono_app.ico", "Logo/icons"):
            with self.subTest(asset=asset):
                self.assertIn(asset, self.source)

    def test_no_uses_upx(self):
        """UPX compresses more, but multiplies antivirus false positives."""
        self.assertIn("upx=False", self.source)
        self.assertNotIn("upx=True", self.source)

    def test_resources_version_are_coherent(self):
        # PyInstaller runs this file with ITS classes (VSVersionInfo,
        # FixedFileInfo...) already defined; here we supply doubles that accept
        # any argument to check that the file is valid Python.
        def node(*args, **kwargs):
            return args, kwargs

        espacio = {name: node for name in
                   ("VSVersionInfo", "FixedFileInfo", "StringFileInfo",
                    "StringTable", "StringStruct", "VarFileInfo", "VarStruct")}
        exec(open(VERSION_INFO, encoding="utf-8").read(), espacio)   # pyinstaller runs it
        self.assertIn("VSVersionInfo", espacio)
        text = open(VERSION_INFO, encoding="utf-8").read()
        # The Windows resources must announce the same version as the program, and
        # the check reads it from the one place that defines it instead of repeating
        # the literal: the tag, the release and the executable drifted apart once
        # precisely because these were independent numbers.
        self.assertIn(main.VERSION, text)
        self.assertIn(f'"{main.VERSION}.0"', text)      # the filevers style: 2.0.0.0
        self.assertIn("FLAC VERIFIER", text)
        self.assertIn("LegalCopyright", text)
        self.assertIn("OriginalFilename", text)

    def test_builder_announces_version_program(self):
        """The build banner takes the version from `main.py`, not from its own copy."""
        source = open(os.path.join(ROOT, "build.py"), encoding="utf-8").read()
        self.assertIn("project_version()", source)
        self.assertNotRegex(source, r"BUILDING FLAC VERIFIER \d")

    def test_builder_exists_is_python_valid(self):
        compile(open(os.path.join(ROOT, "build.py"), encoding="utf-8").read(),
                "build.py", "exec")

    def test_builder_clean_verifies(self):
        source = open(os.path.join(ROOT, "build.py"), encoding="utf-8").read()
        for expected in ("shutil.rmtree", "PyInstaller", "--noconfirm",
                         "FLAC_Verifier.exe", "flac_motor.exe", "--version"):
            with self.subTest(expected=expected):
                self.assertIn(expected, source)


class test_comparison_with_source(unittest.TestCase):
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
    def _line(**fields):
        line = {"tipo": "resultado", "indice": 1, "total": 1}
        line.update(fields)
        return json.dumps(line, ensure_ascii=False)

    def test_cards_index_by_file(self):
        text = "\n".join([
            self._line(archivo="01.flac", veredicto="GENUINE LOSSLESS"),
            '{"tipo": "inicio", "total": 2}',
            self._line(archivo="02.flac", veredicto="SUSPICIOUS"),
            '{"tipo": "fin"}',
        ])
        cards = self.ce.Checker.cards(text)
        self.assertEqual(sorted(cards), ["01.flac", "02.flac"])
        self.assertEqual(cards["02.flac"]["veredicto"], "SUSPICIOUS")

    def test_no_compare_fields_change_alone(self):
        text = self._line(archivo="01.flac", veredicto="GENUINE LOSSLESS",
                            ruta="C:/other/path.flac", espectrograma="C:/x.png",
                            desde_cache=True, md5="abc123")
        card = self.ce.Checker.cards(text)["01.flac"]
        for volatile in ("ruta", "espectrograma", "desde_cache"):
            self.assertNotIn(volatile, card, f"{volatile} must not be compared")
        # …but the analysis itself does: that is what has to match.
        self.assertEqual(card["md5"], "abc123")
        self.assertEqual(card["veredicto"], "GENUINE LOSSLESS")

    def test_lines_unreadable_no_knock_down_comparison(self):
        text = "\n".join(["{this is not json", "", "   ",
                           self._line(archivo="01.flac", veredicto="SUSPICIOUS")])
        self.assertEqual(list(self.ce.Checker.cards(text)), ["01.flac"])

    def test_file_corrupt_counts_equal_two_sides(self):
        """The error of an unreadable FLAC also has to match."""
        text = json.dumps({"tipo": "resultado", "archivo": "06_corrupto.flac",
                            "error": "Could not read FLAC metadata"}, ensure_ascii=False)
        card = self.ce.Checker.cards(text)["06_corrupto.flac"]
        self.assertIn("error", card)
        self.assertNotIn("veredicto", card)


class test_renderer_without_matplotlib(unittest.TestCase):
    """The spectrogram is a NumPy + Pillow raster: matplotlib is not packaged."""

    def test_engine_no_imports_matplotlib(self):
        source = open(os.path.join(ROOT, "motor_flac.py"), encoding="utf-8").read()
        self.assertNotIn("import matplotlib", source)
        self.assertNotIn("matplotlib.use", source)

    def test_engine_uses_pillow_lut(self):
        source = open(os.path.join(ROOT, "motor_flac.py"), encoding="utf-8").read()
        self.assertIn("from PIL import Image", source)
        self.assertIn("_LUT_SPECTROGRAM", source)
        self.assertIn("Image.fromarray(", source)

    def test_spec_excludes_matplotlib(self):
        source = open(SPEC, encoding="utf-8").read()
        block = source.split("excludes=[", 1)[1]
        self.assertIn('"matplotlib"', block)
        self.assertNotIn('collect_data_files("matplotlib")', source)

    def test_spec_collects_pillow(self):
        self.assertIn('"PIL"', open(SPEC, encoding="utf-8").read())

if __name__ == "__main__":
    unittest.main(verbosity=2)
