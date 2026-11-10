"""Block 7 (structure): thin layer, __main__ guard, UTF-8 and no duplication.

These tests watch over the structural invariants of the project:
  - importing a module must not run any CLI.
  - the analysis logic must NOT be duplicated again in the CLI.
  - output with stdout redirected must not die from encoding.
  - the CLI's verdict table covers the engine's real outputs.
"""
import json
import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fixtures as fx

import motor_flac as engine
import verificar_flac as cli

# Functions that must live ONLY in the engine.
FUNCTIONS_ENGINE = (
    "verify_signature", "verify_metadata", "read_audio", "_md5_pcm",
    "verify_md5", "analyze_bit_depth", "analyze_spectrum",
    "_save_spectrogram", "detect_clipping", "calculate_dr",
    "calculate_score", "analyze_file_data",
)


class test_without_effects_side_effects(unittest.TestCase):

    def test_import_modules_no_executes_no_cli(self):
        """Before, 'import verificar_flac' started the interactive menu."""
        code = "import motor_flac, verificar_flac; print('IMPORT_OK')"
        p = subprocess.run([sys.executable, "-c", code], cwd=fx.ROOT,
                           capture_output=True, text=True, encoding="utf-8", timeout=180)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout.strip(), "IMPORT_OK")
        self.assertEqual(p.stderr.strip(), "")


class TestThinLayer(unittest.TestCase):

    def test_cli_no_redefines_engine(self):
        for name in FUNCTIONS_ENGINE:
            with self.subTest(funcion=name):
                self.assertNotIn(
                    name, vars(cli),
                    f"{name} is defined again in verificar_flac.py: "
                    f"the logic has been duplicated instead of using motor_flac")

    def test_cli_imports_engine(self):
        self.assertIs(cli.engine, engine)

    def test_dependency_pdf_remains_isolated(self):
        """Neither the engine nor the CLI imports reportlab: only informe_pdf uses
        it, and that is why both work even when reportlab is not installed.
        Pillow is different: since Phase 3 the engine renders the spectrograms
        with it, so it is imported (guarded) by motor_flac itself."""
        modules = (fx.ENGINE, fx.CLI,
                   os.path.join(fx.ROOT, "_motor_bits.py"),
                   os.path.join(fx.ROOT, "_motor_espectral.py"),
                   os.path.join(fx.ROOT, "_motor_veredicto.py"),
                   os.path.join(fx.ROOT, "_motor_cache.py"))
        for path in modules:
            with self.subTest(modulo=os.path.basename(path)):
                with open(path, encoding="utf-8") as f:
                    source = f.read()
                self.assertNotRegex(source, r"(?m)^\s*(?:import|from)\s+reportlab")

    def test_report_no_uses_reportlab_import(self):
        """A default value in the signature is evaluated when the module is
        imported: using a reportlab name there (mm, A4, colors…) broke the import
        when the library is missing, and with it the warning about the missing
        dependency."""
        with open(os.path.join(fx.ROOT, "informe_pdf.py"), encoding="utf-8") as f:
            source = f.read()
        for line in source.splitlines():
            if line.lstrip().startswith("def "):
                with self.subTest(firma=line.strip()[:70]):
                    self.assertNotRegex(line, r"=\s*[^,()]*\b(mm|A4)\b")
                    self.assertNotRegex(line, r"=\s*[^,()]*colors\.")

    def test_engine_report_matter_without_reportlab(self):
        code = ("import sys\n"
                  "for m in list(sys.modules):\n"
                  "    if m.startswith('reportlab'):\n"
                  "        del sys.modules[m]\n"
                  "sys.modules['reportlab'] = None\n"
                  "import motor_flac, informe_pdf\n"
                  "print('motor OK', informe_pdf.available())\n"
                  "print(informe_pdf.generate('.', [{'archivo': 'x'}])['error'])\n")
        # The child prints a message with an accent: without this, a redirected
        # stdout on Windows uses cp1252 and the test itself would die with
        # UnicodeEncodeError.
        environment = {**os.environ, "PYTHONIOENCODING": "utf-8"}
        p = subprocess.run([sys.executable, "-c", code], cwd=fx.ROOT, env=environment,
                           capture_output=True, text=True, encoding="utf-8", timeout=180)
        self.assertEqual(p.returncode, 0, p.stderr)
        lines = p.stdout.strip().splitlines()
        self.assertEqual(lines[0], "motor OK False")
        self.assertIn("reportlab", lines[1])

    def test_table_verdicts_covers_outputs_engine(self):
        spectrum_ok = {"error": None, "techo_hz": 22050, "corte_artificial": False,
                  "frecuencia_corte": None, "ratio": 1.0, "var_alta_db": 50.0,
                  "separacion_stereo": 1.0, "nyquist": 22050, "espectrograma": None}
        meta = {"sample_rate": 44100, "bits_per_sample": 16, "canales": 2,
                "duracion": 10.0, "md5": 1}
        cases = [spectrum_ok,
                 dict(spectrum_ok, techo_hz=8000, corte_artificial=True,
                      frecuencia_corte=16000, ratio=0.0, var_alta_db=0.0),
                 dict(spectrum_ok, error="simulated failure")]
        for spectrum in cases:
            with self.subTest(spectrum=spectrum.get("error") or spectrum["techo_hz"]):
                score, verdict, problems = engine.calculate_score(meta, spectrum, {"aplica": False})
                self.assertTrue(0.0 <= score <= 1.0)
                self.assertIn(verdict, cli.VERDICTS)
                self.assertIsInstance(problems, list)

    def test_label_verdict_no_depends_substrings(self):
        for verdict in cli.VERDICTS:
            with self.subTest(verdict=verdict):
                self.assertTrue(cli.verdict_label(verdict))
        # an unknown verdict must not break the presentation
        self.assertIn("new", cli.verdict_label("new"))


class TestUTF8(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.fx = fx.Fixtures()
        cls.folder = cls.fx.subfolder("cli_lote", ("ok16", "basura_magic"))

    @classmethod
    def tearDownClass(cls):
        cls.fx.close()

    def _run_cli(self, folder, pdf=False):
        """CLI with stdin/stdout redirected and WITHOUT PYTHONIOENCODING.

        This is exactly the scenario where it used to die with UnicodeEncodeError.
        The order of the answers is: mode, PNG, PDF report, path, another folder."""
        environment = {k: v for k, v in os.environ.items() if k != "PYTHONIOENCODING"}
        input = f"2\nn\n{'y' if pdf else 'n'}\n{folder}\nn\n"
        return subprocess.run([sys.executable, fx.CLI], input=input, cwd=fx.ROOT,
                              capture_output=True, text=True, encoding="utf-8",
                              timeout=900, env=environment)

    def test_cli_with_stdout_redirected_no_dies_by_encoding(self):
        p = self._run_cli(self.folder)
        self.assertEqual(p.returncode, 0, p.stderr[-2000:])
        self.assertNotIn("UnicodeEncodeError", p.stderr or "")
        self.assertNotIn("Traceback", p.stderr or "")
        self.assertIn("═", p.stdout)                 # the character that blew up in cp1252
        self.assertIn("✅", p.stdout)

    def test_cli_analyzes_batch_integer_included_file_broken(self):
        p = self._run_cli(self.folder)
        self.assertIn("Analyzing 2 file(s)", p.stdout)
        self.assertIn("SUMMARY:", p.stdout)
        self.assertIn("1 ❌ WITH ERRORS", p.stdout)     # the batch is not aborted
        self.assertIn("ok16", p.stdout)
        self.assertIn("basura_magic", p.stdout)

    def test_cli_exits_clean_without_input(self):
        """EOF on stdin (stdin closed) must finish gracefully."""
        p = subprocess.run([sys.executable, fx.CLI], input="", cwd=fx.ROOT,
                           capture_output=True, text=True, encoding="utf-8", timeout=180)
        self.assertEqual(p.returncode, 0, p.stderr[-2000:])
        self.assertNotIn("Traceback", p.stderr or "")


class test_spectrogram_png(unittest.TestCase):
    """The PNG is produced by the streaming path; its signature changed in Block 6."""

    @classmethod
    def setUpClass(cls):
        cls.fx = fx.Fixtures()

    @classmethod
    def tearDownClass(cls):
        cls.fx.close()

    @unittest.skipUnless(engine.PIL_OK, "Pillow not installed")
    def test_generates_png_reports_path(self):
        res = engine.analyze_file_data(self.fx["ok16"], "center", None, True)
        self.assertNotIn("error", res, res.get("error"))
        path = res["esp"]["espectrograma"]
        self.assertIsNotNone(path, "the spectrogram was not generated")
        self.assertTrue(os.path.exists(path))
        self.assertGreater(os.path.getsize(path), 1000)

    def test_without_png_no_reports_path(self):
        res = engine.analyze_file_data(self.fx["ok16"], "center", None, False)
        self.assertIsNone(res["esp"]["espectrograma"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
