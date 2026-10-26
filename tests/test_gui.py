"""GUI: event parsing, status formatting and process management.

The tests do NOT start the visual loop (`mainloop`): they exercise the pure
logic, the subprocess lifecycle with fake launchers and, when there is a screen,
the construction of the hidden window and its state changes.

To skip the tests that need a window (CI without a desktop):
    FLAC_VERIFIER_SIN_GUI=1 python -m unittest discover -s tests
"""
import importlib
import io
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fixtures as fx

import gui
import customtkinter as ctk

DND_REAL = gui.DND_OK                       # real state, to restore it after a reload


def _there_is_screen():
    """Can a Tk window be created in this environment?"""
    if os.environ.get("FLAC_VERIFIER_SIN_GUI"):
        return False
    try:
        import tkinter
        root = tkinter.Tk()
        root.withdraw()
        root.destroy()
        return True
    except Exception:
        return False


VISUAL = gui.CTK_OK and _there_is_screen()


# ─── test doubles ────────────────────────────────────────────────────────────

class BlockedStream(io.StringIO):
    """A stdout that serves its lines and then waits instead of reaching the end.

    The manager considers the analysis finished when the reader thread hits the end
    of the stream and the process reports an exit code. A fake that serves a single
    line reaches that point in microseconds, so a test that wants to inspect the
    manager *while the analysis runs* is racing the reader thread: it passed on a
    loaded machine and failed on a fast one (Ubuntu with Python 3.10, where the
    thread won the race). Holding the stream open until the test — or a call to
    `terminate`/`kill` — releases it removes the race entirely.
    """

    def __init__(self, text, liberar):
        super().__init__(text)
        self._liberar = liberar

    def readline(self, *args, **kwargs):
        line = super().readline(*args, **kwargs)
        if line:
            return line
        # A bounded wait: a bug must not hang the whole suite for ever.
        self._liberar.wait(timeout=5.0)
        return ""

    def __next__(self):
        line = self.readline()
        if not line:
            raise StopIteration
        return line


class FakeProcess:
    """Mimics just the part of subprocess.Popen that the manager uses."""

    def __init__(self, lines=(), error=(), code=0, fails_terminate=False,
                 bloquear=False):
        # With `bloquear=True` the process stays alive until the test releases it,
        # so assertions about a running analysis are deterministic.
        self._liberar = threading.Event()
        text = "\n".join(lines) + ("\n" if lines else "")
        self.stdout = (BlockedStream(text, self._liberar) if bloquear
                       else io.StringIO(text))
        self.stderr = io.StringIO("\n".join(error) + ("\n" if error else ""))
        self.pid = 4242
        self.returncode = None
        self._code = code
        self.fails_terminate = fails_terminate
        self.finished = 0
        self.dead = 0

    def liberar(self):
        """Lets a blocked stdout reach its end, so the reader thread can finish."""
        self._liberar.set()

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.liberar()
        self.returncode = self._code if self.returncode is None else self.returncode
        return self.returncode

    def terminate(self):
        self.finished += 1
        self.liberar()                      # a dead process cannot keep writing
        if self.fails_terminate:
            raise OSError("could not terminate")
        self.returncode = 1

    def kill(self):
        self.dead += 1
        self.liberar()
        self.returncode = 1


class test_command(unittest.TestCase):
    """The subprocess command is what keeps the GUI from blocking."""

    def test_carries_u_for_no_store_buffer(self):
        command = gui.build_command("C:/Music/Album")
        self.assertEqual(command[0], sys.executable)
        self.assertEqual(command[1], "-u")          # without this there is no real time
        self.assertTrue(command[2].endswith("motor_flac.py"))

    def test_options_by_default(self):
        command = gui.build_command("C:/Album")
        self.assertIn("--mode", command)
        self.assertEqual(command[command.index("--mode") + 1], "center")
        self.assertIn("--pdf", command)
        self.assertNotIn("--workers", command)      # 0 = the engine chooses
        self.assertNotIn("--no-cache", command)

    def test_mode_seconds_adds_value(self):
        command = gui.build_command("C:/Album", mode="seconds", seconds=20)
        self.assertEqual(command[command.index("--seconds") + 1], "20")

    def test_workers_cache(self):
        command = gui.build_command("C:/Album", workers=4, without_cache=True)
        self.assertEqual(command[command.index("--workers") + 1], "4")
        self.assertIn("--no-cache", command)

    def test_without_pdf_no_requests_report(self):
        self.assertNotIn("--pdf", gui.build_command("C:/Album", pdf=False))


class TestParseo(unittest.TestCase):

    def test_event_correct(self):
        event = gui.parse_event('{"tipo": "resultado", "archivo": "a.flac"}')
        self.assertEqual(event, {"tipo": "resultado", "archivo": "a.flac"})

    def test_line_empty_returns_none(self):
        self.assertIsNone(gui.parse_event(""))
        self.assertIsNone(gui.parse_event("   \n"))

    def test_line_unreadable_no_raises(self):
        event = gui.parse_event("this is not json")
        self.assertEqual(event["tipo"], "__ilegible__")
        self.assertIn("this is not json", event["mensaje"])

    def test_json_no_is_object(self):
        self.assertEqual(gui.parse_event("[1, 2, 3]")["tipo"], "__ilegible__")
        self.assertEqual(gui.parse_event('"hello"')["tipo"], "__ilegible__")

    def test_event_without_kind(self):
        self.assertEqual(gui.parse_event('{"total": 3}')["tipo"], "__desconocido__")

    def test_accents_arrive_well(self):
        event = gui.parse_event(json.dumps({"tipo": "aviso", "mensaje": "naïve"}))
        self.assertEqual(event["mensaje"], "naïve")


class test_format(unittest.TestCase):

    def test_result_correct(self):
        text = gui.format_result({"archivo": "01 - Luminol.flac",
                                         "veredicto": "GENUINE LOSSLESS",
                                         "score": 1.0, "problemas": []})
        self.assertEqual(text, "[OK] 01 - Luminol.flac: GENUINE LOSSLESS · "
                                "score 100 % · 0 issues")

    def test_result_with_only_problem_no_pluralizes(self):
        text = gui.format_result({"archivo": "a.flac", "veredicto": "SUSPICIOUS",
                                         "score": 0.5, "problemas": ["one"]})
        self.assertIn("[SUSPICIOUS]", text)
        self.assertTrue(text.endswith("1 issue"), text)
        self.assertNotIn("1 issues", text)

    def test_result_from_cache(self):
        text = gui.format_result({"archivo": "a.flac", "veredicto": "SUSPICIOUS",
                                         "desde_cache": True})
        self.assertIn("cache", text)

    def test_result_with_error(self):
        text = gui.format_result({"archivo": "a.flac", "error": "invalid signature"})
        self.assertEqual(text, "[ERROR] a.flac: invalid signature")

    def test_summary_green_only_with_genuine(self):
        color, text = gui.format_summary(
            [{"tipo": "resultado", "veredicto": "GENUINE LOSSLESS"}] * 3)
        self.assertEqual(color, "verde")
        self.assertIn("3 genuine", text)

    def test_summary_yellow_with_probable(self):
        color, _ = gui.format_summary([
            {"tipo": "resultado", "veredicto": "GENUINE LOSSLESS"},
            {"tipo": "resultado", "veredicto": "PROBABLY LOSSLESS"}])
        self.assertEqual(color, "amarillo")

    def test_summary_orange_with_doubtful(self):
        color, _ = gui.format_summary([
            {"tipo": "resultado", "veredicto": "SUSPICIOUS"}])
        self.assertEqual(color, "naranja")

    def test_summary_red_with_upscale_o_error(self):
        color, _ = gui.format_summary([{"tipo": "resultado", "veredicto": "PROBABLE UPSCALE"}])
        self.assertEqual(color, "rojo")
        color, _ = gui.format_summary([{"tipo": "resultado", "error": "x"}])
        self.assertEqual(color, "rojo")

    def test_summary_ignores_events_no_are_result(self):
        color, text = gui.format_summary([{"tipo": "inicio", "total": 5}])
        self.assertEqual(color, "gris")
        self.assertEqual(text, "No results")


class TestArrastrarYSoltar(unittest.TestCase):

    def test_paths_with_spaces_between_braces(self):
        self.assertEqual(gui.dnd_paths("{C:/my music/Album 1} D:/other/path"),
                         ["C:/my music/Album 1", "D:/other/path"])

    def test_path_simple(self):
        self.assertEqual(gui.dnd_paths("C:/Album"), ["C:/Album"])

    def test_empty(self):
        self.assertEqual(gui.dnd_paths(""), [])

    def test_without_tkinterdnd2_category_base_no_mix(self):
        """Without the library, the window simply inherits from ctk.CTk and does not fail."""
        if DND_REAL:
            self.skipTest("tkinterdnd2 is installed")
        self.assertFalse(gui.DND_OK)
        self.assertTrue(gui.CTK_OK)
        # the real requirement: nothing may break because it is missing
        self.assertTrue(issubclass(gui.FlacVerifierWindow, ctk.CTk))

    def test_with_tkinterdnd2_mocked_mix_wrapper(self):
        """The optional path has to work without the library installed."""
        wrapper = type("DnDWrapper", (), {})
        root_dnd = type("TkinterDnD", (), {"DnDWrapper": wrapper,
                                           "_require": staticmethod(lambda window: "2.8")})
        false = types.ModuleType("tkinterdnd2")
        false.DND_FILES = "DND_Files"
        false.TkinterDnD = root_dnd
        try:
            with mock.patch.dict(sys.modules, {"tkinterdnd2": false}):
                importlib.reload(gui)
                self.assertTrue(gui.DND_OK)
                self.assertTrue(issubclass(gui._BaseWindow, wrapper),
                                "the tkinterdnd2 wrapper was not mixed in")
                self.assertTrue(issubclass(gui.FlacVerifierWindow, wrapper))
        finally:
            importlib.reload(gui)           # the real module is restored
        self.assertEqual(gui.DND_OK, DND_REAL, "the module was not restored")


class test_open_file(unittest.TestCase):

    def test_windows_uses_startfile(self):
        false_os = mock.MagicMock()
        false_os.name = "nt"
        with mock.patch.object(gui, "os", false_os):
            self.assertTrue(gui.open_file("C:/a.pdf"))
        false_os.startfile.assert_called_once_with("C:/a.pdf")

    def test_unix_uses_viewer_system(self):
        false_os = mock.MagicMock()
        false_os.name = "posix"
        false_sys = mock.MagicMock()
        false_sys.platform = "linux"
        with mock.patch.object(gui, "os", false_os), \
             mock.patch.object(gui, "sys", false_sys), \
             mock.patch.object(gui.subprocess, "Popen") as popen:
            self.assertTrue(gui.open_file("/tmp/a.pdf"))
        popen.assert_called_once_with(["xdg-open", "/tmp/a.pdf"])

    def test_mac_uses_open(self):
        false_os = mock.MagicMock()
        false_os.name = "posix"
        false_sys = mock.MagicMock()
        false_sys.platform = "darwin"
        with mock.patch.object(gui, "os", false_os), \
             mock.patch.object(gui, "sys", false_sys), \
             mock.patch.object(gui.subprocess, "Popen") as popen:
            self.assertTrue(gui.open_file("/tmp/a.pdf"))
        popen.assert_called_once_with(["open", "/tmp/a.pdf"])

    def test_failure_no_raises(self):
        false_os = mock.MagicMock()
        false_os.name = "nt"
        false_os.startfile.side_effect = OSError("no viewer")
        with mock.patch.object(gui, "os", false_os):
            self.assertFalse(gui.open_file("C:/a.pdf"))


class test_manager_process(unittest.TestCase):
    """Start-up, reading and cancellation, with a fake launcher."""

    def _manager(self, process, llamadas=None):
        def lanzador(command, **kwargs):
            if llamadas is not None:
                llamadas.append((command, kwargs))
            return process
        return gui.ProcessManager(queue.Queue(), lanzador=lanzador), llamadas

    def test_starts_with_options_critical_windows(self):
        process = FakeProcess(['{"tipo": "inicio", "total": 1}'])
        llamadas = []
        manager, _ = self._manager(process, llamadas)
        manager.start("C:/Album", mode="center", pdf=True)
        self.assertEqual(len(llamadas), 1)
        command, options = llamadas[0]
        self.assertEqual(command[1], "-u")
        self.assertEqual(options["stdout"], subprocess.PIPE)
        self.assertEqual(options["stderr"], subprocess.PIPE)
        self.assertTrue(options["text"])
        self.assertEqual(options["encoding"], "utf-8")
        self.assertEqual(options["bufsize"], 1)
        self.assertIn("creationflags", options)
        if os.name == "nt":
            self.assertEqual(options["creationflags"],
                             getattr(subprocess, "CREATE_NO_WINDOW", 0))

    def test_events_arrive_queue_finishes_with_end(self):
        process = FakeProcess(['{"tipo": "inicio", "total": 1}',
                                '{"tipo": "resultado", "archivo": "a.flac", "veredicto": "SUSPICIOUS"}',
                                '{"tipo": "fin"}'])
        manager, _ = self._manager(process)
        manager.start("C:/Album")
        manager.wait(duration=5)
        recibidos = []
        while not manager.message_queue.empty():
            recibidos.append(manager.message_queue.get_nowait())
        tipos = [event.get("tipo") for _, event in recibidos]
        self.assertEqual(tipos, ["inicio", "resultado", "fin", "__fin__"])
        self.assertEqual(recibidos[-1][0], "fin")
        self.assertEqual(recibidos[-1][1]["codigo"], 0)

    def test_stderr_goes_queue_as_warning(self):
        process = FakeProcess(['{"tipo": "fin"}'], error=["[clipping] something odd"])
        manager, _ = self._manager(process)
        manager.start("C:/Album")
        manager.wait(duration=5)
        channels = []
        while not manager.message_queue.empty():
            channels.append(manager.message_queue.get_nowait()[0])
        self.assertIn("stderr", channels)

    def test_no_leaves_start_two_times(self):
        # The stream is held open: the second call has to be refused because the
        # first analysis is still running, not because the reader raced ahead.
        process = FakeProcess(['{"tipo": "fin"}'], bloquear=True)
        manager, _ = self._manager(process)
        try:
            manager.start("C:/Album")
            with self.assertRaises(RuntimeError):
                manager.start("C:/Album")
        finally:
            process.liberar()
            manager.wait(duration=5)

    def test_cancel_finishes_process(self):
        process = FakeProcess(['{"tipo": "inicio", "total": 9}'], bloquear=True)
        manager, _ = self._manager(process)
        try:
            manager.start("C:/Album")
            self.assertTrue(manager.active)
            manager.cancel()
            self.assertTrue(manager.cancelled)
            self.assertGreaterEqual(process.finished, 1)
            self.assertFalse(manager.active)
        finally:
            process.liberar()
            manager.wait(duration=5)

    def test_if_terminate_fails_uses_kill(self):
        # The stream is held open so the fake process is still alive when the test
        # cancels it. With a stream that reaches EOF immediately, the reader thread
        # can reap the process first, and `terminar_proceso()` would (correctly) see
        # it already dead and return without calling terminate/kill — that race made
        # this test flaky on fast runners.
        process = FakeProcess(['{"tipo": "fin"}'], fails_terminate=True, bloquear=True)
        manager, _ = self._manager(process)
        try:
            manager.start("C:/Album")
            manager.cancel()
            self.assertGreaterEqual(process.finished, 1)
            self.assertGreaterEqual(process.dead, 1)
        finally:
            process.liberar()
            manager.wait(duration=5)

    def test_finish_process_kills_tree_windows(self):
        """The engine spreads work across child processes: they all have to be killed."""
        process = FakeProcess([])
        with mock.patch.object(gui.subprocess, "run") as run, \
             mock.patch.object(gui.os, "name", "nt"):
            gui.finish_process(process)
        self.assertTrue(run.called)
        arguments = run.call_args[0][0]
        self.assertEqual(arguments[:3], ["taskkill", "/F", "/T"])
        self.assertEqual(arguments[3], "/PID")
        self.assertEqual(arguments[4], str(process.pid))

    def test_finish_process_already_dead_no_does_nothing(self):
        process = FakeProcess([])
        process.returncode = 0
        with mock.patch.object(gui.subprocess, "run") as run:
            gui.finish_process(process)
        self.assertFalse(run.called)
        self.assertEqual(process.finished, 0)

    def test_finish_none_no_raises(self):
        gui.finish_process(None)

    def test_thread_active_distinguishes_reader_running(self):
        """`hilo_activo` is what the window uses so as not to lose the final `__fin__`.

        It has to be True while the reader thread is working and False once that
        thread has ended, even if the process has already been reaped."""
        process = FakeProcess(['{"tipo": "fin"}'], bloquear=True)
        manager, _ = self._manager(process)
        try:
            manager.start("C:/Album")
            self.assertTrue(manager.active_thread)
        finally:
            process.liberar()
            manager.wait(duration=5)
        self.assertFalse(manager.active_thread)


class test_process_real(unittest.TestCase):
    """A real subprocess: it checks real time and cancellation."""

    def setUp(self):
        self.folder = tempfile.mkdtemp(prefix="gui_motor_")
        self.engine = os.path.join(self.folder, "motor_falso.py")
        with open(self.engine, "w", encoding="utf-8") as f:
            f.write(
                "import sys, time\n"
                "print('{\"tipo\": \"inicio\", \"total\": 2}', flush=True)\n"
                "time.sleep(0.2)\n"
                "print('{\"tipo\": \"resultado\", \"archivo\": \"a.flac\", "
                "\"veredicto\": \"GENUINE LOSSLESS\"}', flush=True)\n"
                "time.sleep(0.2)\n"
                "print('{\"tipo\": \"fin\"}', flush=True)\n")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.folder, ignore_errors=True)

    def test_events_arrive_before_process_finishes(self):
        manager = gui.ProcessManager(queue.Queue(), engine=self.engine)
        start = time.perf_counter()
        manager.start("C:/Album", pdf=False)
        _, first = manager.message_queue.get(timeout=30)
        moment_first = time.perf_counter() - start
        self.assertEqual(first["tipo"], "inicio")
        self.assertLess(moment_first, 1.5,
                        "the first event arrived late: is the -u flag missing?")
        manager.wait(duration=30)
        tipos = []
        while not manager.message_queue.empty():
            tipos.append(manager.message_queue.get_nowait()[1]["tipo"])
        self.assertIn("resultado", tipos)
        self.assertEqual(tipos[-1], "__fin__")

    def test_cancel_process_real_kills_no_remains_alive(self):
        slow = os.path.join(self.folder, "motor_lento.py")
        with open(slow, "w", encoding="utf-8") as f:
            f.write("import time\nprint('{\"tipo\": \"inicio\", \"total\": 1}', flush=True)\n"
                    "time.sleep(60)\n")
        manager = gui.ProcessManager(queue.Queue(), engine=slow)
        manager.start("C:/Album", pdf=False)
        self.assertTrue(manager.active)
        manager.cancel()
        manager.wait(duration=30)
        self.assertIsNotNone(manager.process.poll(), "the process is still alive")
        self.assertFalse(manager.active)


@unittest.skipUnless(VISUAL, "customtkinter and a display are required")
class test_window(unittest.TestCase):
    """The window is built hidden: no mainloop."""

    @classmethod
    def setUpClass(cls):
        cls.fx = fx.Fixtures()

    @classmethod
    def tearDownClass(cls):
        cls.fx.close()

    def setUp(self):
        self.window = gui.FlacVerifierWindow()
        self.window.withdraw()
        self.window.update_idletasks()
        # the native dialogs would block the test
        parche = mock.patch("tkinter.messagebox.showerror"), \
                 mock.patch("tkinter.messagebox.showwarning")
        self.parches = parche
        for p in parche:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self.parches])
        self.addCleanup(self.window.destroy)

    def test_starts_wait_with_buttons_correct(self):
        self.assertEqual(self.window.btn_analyze.cget("state"), "normal")
        self.assertEqual(self.window.btn_cancel.cget("state"), "disabled")
        self.assertEqual(self.window.btn_open_pdf.cget("state"), "disabled")
        self.assertEqual(self.window.barra.get(), 0)

    def test_controls_by_default(self):
        self.assertEqual(self.window.var_mode.get(), "center")
        self.assertTrue(self.window.var_pdf.get())
        self.assertTrue(self.window.var_parallel.get())
        self.assertFalse(self.window.var_without_cache.get())
        self.assertEqual(self.window._workers(), 0)        # automatic

    def test_slider_seconds_reflects_value(self):
        """The control is no longer disabled: it appears and disappears from the
        layout. What is checked here is that it still works when it is shown."""
        self.window.var_mode.set("seconds")
        self.window._change_mode()
        self.window._change_seconds(30)
        self.assertIn("30", self.window.label_seconds.cget("text"))
        self.assertGreaterEqual(self.window.seconds_slider.get(), 5)
        self.assertLessEqual(self.window.seconds_slider.get(), 120)

    def test_turn_off_parallel_forces_process(self):
        self.window.option_workers.set("8")
        self.assertEqual(self.window._workers(), 8)
        self.window.var_parallel.set(False)
        self.window._change_parallel()
        self.assertEqual(self.window._workers(), 1)
        self.assertEqual(self.window.option_workers.cget("state"), "disabled")

    def test_buttons_follow_cycle_life(self):
        self.window.manager.process = FakeProcess(['{"tipo": "fin"}'])   # in progress
        self.window._update_state()
        self.assertEqual(self.window.btn_analyze.cget("state"), "disabled")
        self.assertEqual(self.window.btn_cancel.cget("state"), "normal")
        self.assertEqual(self.window.input.cget("state"), "disabled")

        self.window.manager.process.returncode = 0                        # finished
        self.window._update_state()
        self.assertEqual(self.window.btn_analyze.cget("state"), "normal")
        self.assertEqual(self.window.btn_cancel.cget("state"), "disabled")
        self.assertEqual(self.window.input.cget("state"), "normal")

    def test_process_events_updates_progress_log_card(self):
        self.window._process("stdout", {"tipo": "inicio", "total": 2, "workers": 2})
        self.assertEqual(self.window.total, 2)
        self.window._process("stdout", {"tipo": "resultado", "indice": 1, "total": 2,
                                          "archivo": "a.flac",
                                          "veredicto": "GENUINE LOSSLESS",
                                          "score": 1.0, "problemas": []})
        self.assertEqual(self.window.recibidos, 1)
        self.assertAlmostEqual(self.window.barra.get(), 0.5, places=6)
        self.assertIn("1 of 2", self.window.label_progress.cget("text"))
        self.window._process("stdout", {"tipo": "resultado", "indice": 2, "total": 2,
                                          "archivo": "b.flac",
                                          "veredicto": "PROBABLE UPSCALE",
                                          "score": 0.2, "problemas": ["x"]})
        self.assertAlmostEqual(self.window.barra.get(), 1.0, places=6)
        self.window._process("fin", {"tipo": "__fin__", "codigo": 0})
        self.assertIn("2 file(s)", self.window.label_summary.cget("text"))
        self.assertEqual(self.window.label_state.cget("text"), "Finished")
        registro = self.window.log.get("1.0", "end")
        self.assertIn("GENUINE LOSSLESS", registro)
        self.assertIn("PROBABLE UPSCALE", registro)

    def test_event_report_enables_button_pdf(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, gui.REPORT_NAME)
            with open(path, "wb") as f:
                f.write(b"%PDF-1.4\n")
            self.window._process("stdout", {"tipo": "informe", "ruta": path,
                                              "paginas": 3, "total": 1})
            self.assertEqual(self.window.pdf_path, path)
            self.assertEqual(self.window.btn_open_pdf.cget("state"), "normal")
            os.remove(path)
            self.window._update_state()
            self.assertEqual(self.window.btn_open_pdf.cget("state"), "disabled")

    def test_warning_error_go_log_without_close_nothing(self):
        self.window._process("stdout", {"tipo": "aviso", "mensaje": "no report"})
        self.window._process("stdout", {"tipo": "error", "mensaje": "invalid path"})
        self.window._process("stdout", {"tipo": "__ilegible__", "mensaje": "junk"})
        self.window._process("stderr", {"tipo": "__stderr__", "mensaje": "diagnostics"})
        registro = self.window.log.get("1.0", "end")
        for esperado in ("no report", "invalid path", "junk", "diagnostics"):
            self.assertIn(esperado, registro)
        self.assertTrue(self.window.winfo_exists())        # still standing

    def test_cancel_without_analysis_no_does_nothing(self):
        self.window._cancel()                            # must not raise
        self.assertEqual(self.window.btn_analyze.cget("state"), "normal")

    def test_analyze_without_path_warns_no_starts(self):
        self.window.var_path.set("")
        self.window._analyze()
        self.assertFalse(self.window.manager.active)
        self.assertIn("Path missing", self.window.log.get("1.0", "end"))

    def test_analyze_with_path_nonexistent_no_starts(self):
        self.window.var_path.set("C:/no/such/path")
        self.window._analyze()
        self.assertFalse(self.window.manager.active)

    def test_drop_path_fills_field(self):
        event = types.SimpleNamespace(data="{C:/Music/My Album}")
        self.window._drop(event)
        self.assertEqual(self.window.var_path.get(), "C:/Music/My Album")

    def test_stream_full_without_mainloop(self):
        """Starts the real engine: progress, log, card and PDF."""
        album = self.fx.subcarpeta("gui_flujo", ("ok16", "corte_16k"))
        self.window.var_path.set(album)
        self.window.var_pdf.set(True)
        self.window.var_parallel.set(False)          # one process: shorter start-up
        self.window.var_without_cache.set(True)
        self.window._analyze()
        self.assertTrue(self.window.manager.active)
        # the widgets follow the lifecycle while the engine works
        self.assertEqual(self.window.btn_analyze.cget("state"), "disabled")
        self.assertEqual(self.window.btn_cancel.cget("state"), "normal")

        limit = time.time() + 180
        while time.time() < limit:
            self.window.update()
            # the event loop is pumped as the real application would, until the
            # final state is painted
            if (not self.window.manager.active and self.window.message_queue.empty()
                    and "Analyzing" not in self.window.label_state.cget("text")):
                break
            time.sleep(0.05)
        self.window.update()
        self.assertFalse(self.window.manager.active, "the analysis did not finish")

        results = [e for e in self.window.events if e.get("tipo") == "resultado"]
        self.assertEqual(len(results), 2)
        self.assertAlmostEqual(self.window.barra.get(), 1.0, places=3)
        self.assertEqual(self.window.label_state.cget("text"), "Finished")
        self.assertIsNotNone(self.window.pdf_path, "the report event did not arrive")
        self.assertTrue(os.path.exists(self.window.pdf_path))
        self.assertEqual(self.window.btn_open_pdf.cget("state"), "normal")
        self.assertEqual(self.window.btn_analyze.cget("state"), "normal")

        registro = self.window.log.get("1.0", "end")
        self.assertIn("GENUINE LOSSLESS", registro)      # ok16
        self.assertIn("PROBABLE UPSCALE", registro)      # corte_16k
        self.assertIn("PDF report:", registro)
        self.assertIn("2 file(s)", self.window.label_summary.cget("text"))


class test_integration_with_cli(unittest.TestCase):
    """`verificar_flac.py --gui` has to open the GUI, not the menu."""

    def test_flag_gui_calls_ui(self):
        import verificar_flac
        with mock.patch.object(gui, "main", return_value=0) as main:
            code = verificar_flac.main(["--gui", "C:/Music/Album"])
        self.assertEqual(code, 0)
        main.assert_called_once_with("C:/Music/Album")

    def test_flag_short_also(self):
        import verificar_flac
        with mock.patch.object(gui, "main", return_value=0) as main:
            verificar_flac.main(["-g"])
        main.assert_called_once_with(None)

    def test_if_missing_customtkinter_warns_without_break(self):
        import verificar_flac
        with mock.patch.dict(sys.modules, {"gui": None}):
            code = verificar_flac.main(["--gui"])
        self.assertEqual(code, 1)


class test_logo(unittest.TestCase):
    """The visual identity assets and their use."""

    PATHS = {
        "cuadrado": os.path.join(gui.LOGO_FOLDER, "logo_flac_verifier.png"),
        "transparente": os.path.join(gui.LOGO_FOLDER,
                                     "logo_flac_verifier_transparente.png"),
        "pequeno": os.path.join(gui.LOGO_FOLDER, "logo_flac_verifier_128.png"),
        "icono": gui.LOGO_ICON,
        "svg": os.path.join(gui.LOGO_FOLDER, "logo_flac_verifier.svg"),
        "generador": os.path.join(gui.LOGO_FOLDER, "generar_logo.py"),
    }
    ALTURAS_SPLASH = (28, 44, 60, 36, 52)     # from splashscreen.html

    def test_assets_exist(self):
        for name, path in self.PATHS.items():
            with self.subTest(asset=name):
                self.assertTrue(os.path.exists(path), f"missing {path}")

    def test_png_is_square_has_alpha(self):
        from PIL import Image
        with Image.open(self.PATHS["cuadrado"]) as image:
            self.assertEqual(image.size, (1024, 1024))
            self.assertIn("A", image.mode)
        with Image.open(self.PATHS["transparente"]) as image:
            self.assertEqual(image.size, (1024, 1024))
        with Image.open(self.PATHS["pequeno"]) as image:
            self.assertEqual(image.size, (128, 128))

    def test_icon_is_multiresolution(self):
        from PIL import Image
        with Image.open(self.PATHS["icono"]) as icon:
            tamanos = sorted(icon.info["sizes"])
        self.assertGreaterEqual(len(tamanos), 6)
        self.assertIn((16, 16), tamanos)          # taskbar
        self.assertIn((256, 256), tamanos)        # high-density .exe

    def test_drawing_respects_splash_proportions(self):
        """The five bars and their heights come from splashscreen.html."""
        from PIL import Image
        with Image.open(self.PATHS["cuadrado"]) as image:
            lienzo = image.convert("RGBA")
            width, height = lienzo.size
            white = (255, 255, 255, 255)
            columnas = []
            for x in range(width):
                ys = [y for y in range(height) if lienzo.getpixel((x, y)) == white]
                columnas.append((min(ys), max(ys)) if ys else None)

        grupos, current = [], None
        for x, dato in enumerate(columnas):
            if dato is None:
                current = None
                continue
            if current is None:
                current = [dato[0], dato[1]]
                grupos.append(current)
            else:
                current[0] = min(current[0], dato[0])
                current[1] = max(current[1], dato[1])

        self.assertEqual(len(grupos), len(self.ALTURAS_SPLASH))
        alturas = [end - start + 1 for start, end in grupos]
        reference = max(self.ALTURAS_SPLASH)
        for measurement, esperada in zip(alturas, self.ALTURAS_SPLASH):
            with self.subTest(barra=esperada):
                self.assertAlmostEqual(measurement / max(alturas), esperada / reference,
                                       delta=0.02)

    def test_check_is_teal_below_right(self):
        from PIL import Image
        teal = (0x4F, 0x98, 0xA3, 255)
        with Image.open(self.PATHS["cuadrado"]) as image:
            lienzo = image.convert("RGBA")
            width, height = lienzo.size
            pixeles = [(x, y) for x in range(0, width, 2) for y in range(0, height, 2)
                       if lienzo.getpixel((x, y)) == teal]
        self.assertTrue(pixeles, "there is no teal check")
        self.assertGreater(min(x for x, _ in pixeles), width * 0.5)
        self.assertGreater(min(y for _, y in pixeles), height * 0.5)

    def test_svg_carries_same_design(self):
        svg = open(self.PATHS["svg"], encoding="utf-8").read()
        self.assertEqual(svg.count("<rect"), 1 + len(self.ALTURAS_SPLASH))  # background + bars
        self.assertEqual(svg.count("<polyline"), 1)
        for color in ("#0f0f0d", "#ffffff", "#4f98a3"):
            with self.subTest(color=color):
                self.assertIn(color, svg)

    def test_readme_uses_logo_exists(self):
        import re
        readme = open(os.path.join(gui.DIRECTORY, "README.md"), encoding="utf-8").read()
        referencias = re.findall(r"Logo/[\w.\-]+", readme)
        self.assertTrue(referencias, "the README shows no logo")
        for reference in set(referencias):
            with self.subTest(file=reference):
                self.assertTrue(os.path.exists(os.path.join(gui.DIRECTORY, reference)))

    def test_readme_documents_assets(self):
        readme = open(os.path.join(gui.DIRECTORY, "README.md"), encoding="utf-8").read()
        for name in ("logo_flac_verifier.png", "icono_app.ico",
                       "logo_flac_verifier.svg", "generar_logo.py"):
            with self.subTest(asset=name):
                self.assertIn(name, readme)

    def test_without_assets_window_works_warns(self):
        """If someone deletes the Logo folder, the application must not break."""
        if not VISUAL:
            self.skipTest("customtkinter and a display are required")
        nonexistent = os.path.join(gui.LOGO_FOLDER, "no_existe.png")
        with mock.patch.object(gui, "LOGO_BRAND", nonexistent), \
             mock.patch.object(gui, "LOGO_ICON", nonexistent), \
             mock.patch.object(gui, "LOGO_ICONPHOTO", nonexistent):
            window = gui.FlacVerifierWindow()
            try:
                window.withdraw()
                window.update_idletasks()
                self.assertFalse(window.icon_loaded)
                self.assertIsNone(window._brand_image(46))
                self.assertTrue(window.winfo_exists())
                self.assertIn("Project icon not found", window.log.get("1.0", "end"))
            finally:
                window.destroy()


@unittest.skipUnless(VISUAL, "customtkinter and a display are required")
class test_control_seconds(unittest.TestCase):
    """The seconds control only exists when the mode needs it.

    It is removed from the grid instead of being disabled, so it frees its space:
    the controls below move up and no phantom gap is left.
    """

    def setUp(self):
        self.window = gui.FlacVerifierWindow()
        self.window.withdraw()
        self.window.update_idletasks()
        self.addCleanup(self.window.destroy)

    def _set_mode(self, mode):
        self.window.var_mode.set(mode)
        self.window._change_mode()
        self.window.update_idletasks()

    def _posiciones(self):
        return {name: getattr(self.window, name).winfo_y()
                for name in ("check_pdf", "parallel_switch", "check_cache",
                               "btn_analyze", "btn_cancel")}

    def test_center_no_sample(self):
        self._set_mode("center")
        self.assertEqual(self.window.seconds_row.grid_info(), {},
                         "the seconds control still takes up space")
        self.assertFalse(self.window.seconds_row.winfo_manager())

    def test_full_neither(self):
        self._set_mode("full")
        self.assertEqual(self.window.seconds_row.grid_info(), {})

    def test_seconds_if_sample(self):
        self._set_mode("seconds")
        info = self.window.seconds_row.grid_info()
        self.assertTrue(info, "the seconds control does not appear")
        self.assertEqual(int(info["row"]), 6)
        self.assertEqual(self.window.seconds_slider.cget("state"), "normal")

    def test_widget_still_existing_when_hidden(self):
        self._set_mode("center")
        self.assertTrue(self.window.label_seconds.winfo_exists())
        self.assertTrue(self.window.seconds_slider.winfo_exists())
        self.window._change_seconds(45)
        self.assertIn("45", self.window.label_seconds.cget("text"))

    def test_show_it_controls_below_go_down(self):
        self._set_mode("center")
        without_seconds = self._posiciones()
        self._set_mode("seconds")
        with_seconds = self._posiciones()
        for name, y in without_seconds.items():
            with self.subTest(control=name):
                self.assertLess(y, with_seconds[name],
                                f"{name} did not move down when the seconds control appeared")

    def test_hide_it_controls_return_place(self):
        """Round trip without drift: no phantom gaps and no displacement."""
        self._set_mode("center")
        reference = self._posiciones()
        self._set_mode("seconds")
        self._set_mode("full")
        self.assertEqual(self._posiciones(), reference)

    def test_hidden_shown_several_times_no_accumulates_gap(self):
        self._set_mode("center")
        reference = self._posiciones()
        for _ in range(3):
            self._set_mode("seconds")
            self._set_mode("center")
        self.assertEqual(self._posiciones(), reference)


if __name__ == "__main__":
    unittest.main(verbosity=2)
