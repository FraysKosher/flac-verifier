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
import theme
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

    def __init__(self, text, release):
        super().__init__(text)
        self._release = release

    def readline(self, *args, **kwargs):
        line = super().readline(*args, **kwargs)
        if line:
            return line
        # A bounded wait: a bug must not hang the whole suite for ever.
        self._release.wait(timeout=5.0)
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
        self._release = threading.Event()
        text = "\n".join(lines) + ("\n" if lines else "")
        self.stdout = (BlockedStream(text, self._release) if bloquear
                       else io.StringIO(text))
        self.stderr = io.StringIO("\n".join(error) + ("\n" if error else ""))
        self.pid = 4242
        self.returncode = None
        self._code = code
        self.fails_terminate = fails_terminate
        self.finished = 0
        self.dead = 0

    def release(self):
        """Lets a blocked stdout reach its end, so the reader thread can finish."""
        self._release.set()

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.release()
        self.returncode = self._code if self.returncode is None else self.returncode
        return self.returncode

    def terminate(self):
        self.finished += 1
        self.release()                      # a dead process cannot keep writing
        if self.fails_terminate:
            raise OSError("could not terminate")
        self.returncode = 1

    def kill(self):
        self.dead += 1
        self.release()
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


class TestParsing(unittest.TestCase):

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


class TestResultRow(unittest.TestCase):
    """The presentation of a protocol record: read, mapped, never recomputed."""

    GENUINE = {
        "tipo": "resultado", "archivo": "01.flac", "ruta": "C:/Music/01.flac",
        "veredicto": "GENUINE LOSSLESS", "score": 0.9, "problemas": [],
        "meta": {"bits_per_sample": 16, "sample_rate": 44100},
        "md5": {"estado": "match"}, "dr": 12.0,
    }

    def test_maps_a_genuine_track(self):
        row = gui.result_row(self.GENUINE)
        self.assertEqual(row["file"], "01.flac")
        self.assertEqual(row["path"], "C:/Music/01.flac")
        self.assertEqual(row["label"], "OK")
        self.assertEqual(row["tone"], "ok")
        self.assertEqual(row["score"], 90)
        self.assertEqual(row["depth"], "16 bit")
        self.assertEqual(row["rate"], "44.1 kHz")
        self.assertEqual(row["dr"], "12")
        self.assertEqual(row["md5"], "verified")
        self.assertEqual(row["md5_icon"], "check")
        self.assertFalse(row["cached"])

    def test_error_record_has_no_verdict_or_score(self):
        row = gui.result_row({"tipo": "resultado", "archivo": "x.flac",
                              "error": "invalid signature"})
        self.assertEqual(row["label"], "ERROR")
        self.assertEqual(row["tone"], "danger")
        self.assertEqual(row["verdict"], "")
        self.assertIsNone(row["score"])
        self.assertEqual(row["error"], "invalid signature")

    def test_missing_metadata_degrades_to_dashes(self):
        row = gui.result_row({"tipo": "resultado", "archivo": "x.flac",
                              "veredicto": "indeterminate", "score": 0.5})
        self.assertEqual(row["label"], "?")
        self.assertEqual(row["tone"], "unknown")
        self.assertEqual(row["depth"], "—")
        self.assertEqual(row["rate"], "—")
        self.assertEqual(row["dr"], "—")
        self.assertEqual(row["md5"], "—")
        self.assertEqual(row["md5_icon"], "info")

    def test_unknown_verdict_is_not_invented(self):
        row = gui.result_row({"tipo": "resultado", "archivo": "x.flac",
                              "veredicto": "NEW VERDICT", "score": 0})
        self.assertEqual(row["label"], "NEW VERDICT")
        self.assertEqual(row["tone"], "unknown")
        self.assertEqual(row["score"], 0)

    def test_cached_and_md5_states(self):
        row = gui.result_row(dict(self.GENUINE, desde_cache=True,
                                  md5={"estado": "unverifiable"}))
        self.assertTrue(row["cached"])
        self.assertEqual(row["md5"], "unverifiable")
        self.assertEqual(row["md5_icon"], "info")

    def test_format_helpers(self):
        self.assertEqual(gui.format_depth(24), "24 bit")
        self.assertEqual(gui.format_depth(None), "—")
        self.assertEqual(gui.format_rate(44100), "44.1 kHz")
        self.assertEqual(gui.format_rate(96000), "96 kHz")
        self.assertEqual(gui.format_rate(0), "—")
        self.assertEqual(gui.format_dr(12.0), "12")
        self.assertEqual(gui.format_dr(15.25), "15.25")
        self.assertEqual(gui.format_dr(None), "—")

    def test_count_verdicts_ignores_non_results(self):
        events = [{"tipo": "inicio", "total": 3},
                  dict(self.GENUINE, tipo="resultado"),
                  {"tipo": "resultado", "archivo": "b.flac",
                   "veredicto": "SUSPICIOUS"},
                  {"tipo": "resultado", "archivo": "c.flac", "error": "x"}]
        counts, errors, total = gui.count_verdicts(events)
        self.assertEqual(counts, {"GENUINE LOSSLESS": 1, "SUSPICIOUS": 1})
        self.assertEqual(errors, 1)
        self.assertEqual(total, 3)

    def test_summary_uses_the_same_counts(self):
        colour, text = gui.format_summary(
            [dict(self.GENUINE, tipo="resultado"),
             {"tipo": "resultado", "archivo": "b.flac", "veredicto": "SUSPICIOUS"}])
        self.assertEqual(colour, "naranja")
        self.assertIn("2 file(s)", text)
        self.assertIn("1 genuine", text)
        self.assertIn("1 suspicious", text)


class TestDragAndDrop(unittest.TestCase):

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

    def _manager(self, process, calls=None):
        def lanzador(command, **kwargs):
            if calls is not None:
                calls.append((command, kwargs))
            return process
        return gui.ProcessManager(queue.Queue(), lanzador=lanzador), calls

    def test_starts_with_options_critical_windows(self):
        process = FakeProcess(['{"tipo": "inicio", "total": 1}'])
        calls = []
        manager, _ = self._manager(process, calls)
        manager.start("C:/Album", mode="center", pdf=True)
        self.assertEqual(len(calls), 1)
        command, options = calls[0]
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
            process.release()
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
            process.release()
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
            process.release()
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
            process.release()
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
        # The primary action follows the design tokens: accent fill, 44 px and the
        # interface font, with a line icon instead of an emoji.
        button = self.window.btn_analyze
        self.assertEqual(button.cget("fg_color"), theme.color("accent"))
        self.assertEqual(button.cget("height"), 44)
        font = button.cget("font")
        self.assertEqual(font.cget("family"), theme.FONT_UI)
        self.assertEqual(abs(int(font.cget("size"))), 14)
        self.assertIsNotNone(button.cget("image"), "the primary action has no icon")
        self.assertEqual(button.cget("text"), "Analyze")

        # Focus is a designed state: the control is in the tab order, it listens
        # for focus, and the ring uses the focus token without moving anything.
        self.assertEqual(str(button.tk.call(button._w, "cget", "-takefocus")), "1")
        self.assertTrue(button.tk.call("bind", button._w, "<FocusIn>"))
        resting = button.cget("border_color")
        self.window._focus_ring(button, True)
        self.assertEqual(button.cget("border_color"), theme.color("focus"))
        self.assertEqual(button.cget("border_width"), 2)
        self.window._focus_ring(button, False)
        self.assertEqual(button.cget("border_color"), resting)

        # Hover and pressed are colour changes, not movement; disabled fades the
        # fill and ignores hover.
        for state, token in (("hover", "accent.hover"),
                             ("pressed", "accent.pressed")):
            self.window._paint(button, state)
            self.assertEqual(button.cget("fg_color"), theme.color(token))
        self.window._paint(button, "normal")
        self.assertEqual(button.cget("fg_color"), theme.color("accent"))
        self.window._set_enabled(button, False)
        self.assertNotEqual(button.cget("fg_color"), theme.color("accent"))
        self.window._paint(button, "hover")
        self.assertNotEqual(button.cget("fg_color"), theme.color("accent.hover"))
        self.window._set_enabled(button, True)
        self.assertEqual(button.cget("fg_color"), theme.color("accent"))

        # No emoji anywhere: the icons are the line set, not text characters.
        for widget in (self.window.btn_analyze, self.window.btn_cancel,
                       self.window.btn_browse, self.window.btn_open_pdf,
                       self.window.btn_folder):
            with self.subTest(text=str(widget.cget("text"))):
                self.assertTrue(str(widget.cget("text")).isascii())

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
        # While running, Analyze becomes Cancel: only one action is on screen.
        self.assertFalse(self.window.btn_analyze.winfo_manager())
        self.assertEqual(self.window.btn_cancel.winfo_manager(), "grid")

        self.window.manager.process.returncode = 0                        # finished
        self.window._update_state()
        self.assertEqual(self.window.btn_analyze.cget("state"), "normal")
        self.assertEqual(self.window.btn_cancel.cget("state"), "disabled")
        self.assertEqual(self.window.input.cget("state"), "normal")
        self.assertEqual(self.window.btn_analyze.winfo_manager(), "grid")
        self.assertFalse(self.window.btn_cancel.winfo_manager())

    def test_process_events_updates_progress_log_card(self):
        self.window._process("stdout", {"tipo": "inicio", "total": 2, "workers": 2})
        self.assertEqual(self.window.total, 2)
        self.window._process("stdout", {"tipo": "resultado", "indice": 1, "total": 2,
                                          "archivo": "a.flac",
                                          "veredicto": "GENUINE LOSSLESS",
                                          "score": 1.0, "problemas": []})
        self.assertEqual(self.window.recibidos, 1)
        self.assertAlmostEqual(self.window.barra.get(), 0.5, places=6)
        self.assertIn("1 / 2", self.window.label_progress.cget("text"))
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
        # The timestamp is painted with its own (tertiary) tag, the line with the
        # level colour.
        self.assertEqual(self.window.log.tag_cget("time", "foreground"),
                         theme.color("text.tertiary"))
        self.assertEqual(self.window.log.tag_cget("ok", "foreground"),
                         theme.color("ok"))

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
        for expected in ("no report", "invalid path", "junk", "diagnostics"):
            self.assertIn(expected, registro)
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
        album = self.fx.subfolder("gui_flujo", ("ok16", "corte_16k"))
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
        # The results surface streamed one row per file and the log collapsed.
        self.assertEqual(len(self.window.table_rows), 2)
        self.assertEqual(sorted(row["values"]["tone"]
                                for row in self.window.table_rows), ["bad", "ok"])
        self.assertEqual(self.window.activity_bar.winfo_manager(), "grid")
        self.assertFalse(self.window.activity_panel.winfo_manager())


    def test_empty_state_until_a_result_arrives(self):
        """Idle and empty: the drop zone, not an empty table (DESIGN.md 7.11)."""
        self.assertEqual(self.window.empty_state.winfo_manager(), "grid")
        self.assertFalse(self.window.results.winfo_manager())
        self.assertEqual(self.window.drop_zone.headline.cget("text"),
                         "Drop a folder or a .flac file")
        self.window._process("stdout", {"tipo": "resultado", "archivo": "a.flac",
                                        "veredicto": "GENUINE LOSSLESS",
                                        "score": 1.0, "problemas": []})
        self.assertFalse(self.window.empty_state.winfo_manager())
        self.assertEqual(self.window.results.winfo_manager(), "grid")

    def test_table_streams_one_row_per_result(self):
        """Each `resultado` becomes a 44 px row with the protocol values."""
        for index in range(2):
            self.window._process("stdout", {
                "tipo": "resultado", "archivo": f"0{index + 1}.flac",
                "ruta": f"C:/Music/0{index + 1}.flac",
                "veredicto": "GENUINE LOSSLESS" if index == 0 else "SUSPICIOUS",
                "score": 0.9 if index == 0 else 0.41, "problemas": [],
                "meta": {"bits_per_sample": 16, "sample_rate": 44100},
                "md5": {"estado": "match"}, "dr": 12.0})
        self.assertEqual(len(self.window.table_rows), 2)
        first, second = self.window.table_rows
        self.assertEqual(first["cells"]["file"].cget("text"), "01.flac")
        self.assertEqual(second["cells"]["file"].cget("text"), "02.flac")
        self.assertEqual(first["cells"]["score"].cget("text"), "90")
        self.assertEqual(second["cells"]["score"].cget("text"), "41")
        self.assertEqual(first["cells"]["depth"].cget("text"), "16 bit")
        self.assertEqual(first["cells"]["rate"].cget("text"), "44.1 kHz")
        self.assertEqual(first["cells"]["dr"].cget("text"), "12")
        self.assertIsNotNone(first["cells"]["md5"].cget("image"),
                             "the MD5 column has no verification mark")
        # The chip carries the short label and the tone of the verdict.
        self.assertEqual(first["values"]["tone"], "ok")
        self.assertEqual(second["values"]["tone"], "warn")

    def test_selecting_a_row_updates_the_hero(self):
        event = {"tipo": "resultado", "archivo": "01.flac", "ruta": "C:/Music/01.flac",
                 "veredicto": "PROBABLY LOSSLESS", "score": 0.78, "problemas": [],
                 "meta": {"bits_per_sample": 24, "sample_rate": 96000},
                 "md5": {"estado": "match"}, "dr": 15.0}
        self.window._process("stdout", event)
        # CTkFrame binds its own canvas, so the click is checked on the file
        # cell, which is one of the widgets the row wires to `_select`.
        cell = self.window.table_rows[0]["cells"]["file"]
        self.assertTrue(cell.tk.call("bind", cell._w, "<Button-1>"),
                        "the row is not clickable")
        self.window._select(event)
        self.assertEqual(self.window.hero_eyebrow.cget("text"), "01.flac")
        self.assertEqual(self.window.hero_headline.cget("text"), "PROBABLY LOSSLESS")
        self.assertEqual(self.window.hero_number.cget("text"), "78")
        self.assertEqual(self.window.hero_number_caption.cget("text"), "SCORE")
        facts = [(caption.cget("text"), value.cget("text"))
                 for caption, value in self.window.fact_pairs]
        self.assertEqual(facts[:3], [("DEPTH", "24 bit"), ("RATE", "96 kHz"),
                                     ("DR", "15")])
        self.assertEqual(facts[3], ("MD5", "verified"))
        self.window._clear_selection()
        self.assertEqual(self.window.hero_number_caption.cget("text"), "FILES")
        self.assertIn("1 file(s)", self.window.label_summary.cget("text"))

    def test_selected_row_shows_the_tint_and_the_accent_bar(self):
        events = [
            {"tipo": "resultado", "archivo": "a.flac", "veredicto": "GENUINE LOSSLESS",
             "score": 1.0},
            {"tipo": "resultado", "archivo": "b.flac", "veredicto": "SUSPICIOUS",
             "score": 0.4},
        ]
        for event in events:
            self.window._process("stdout", event)
        self.window._select(events[0])
        selected, other = self.window.table_rows
        self.assertEqual(selected["bar"].cget("fg_color"), theme.color("accent"))
        self.assertEqual(selected["frame"].cget("fg_color"),
                         theme.mix(theme.color("accent"),
                                   theme.color("bg.surface"), 0.08))
        self.assertEqual(other["bar"].cget("fg_color"), theme.color("bg.surface"))
        self.assertEqual(other["frame"].cget("fg_color"), theme.color("bg.surface"))

    def test_album_hero_consolidates_the_verdicts(self):
        """One verdict is shown as is; a mixed batch says so; errors weigh in."""
        genuine = {"tipo": "resultado", "archivo": "a.flac",
                   "veredicto": "GENUINE LOSSLESS", "score": 1.0}
        suspicious = {"tipo": "resultado", "archivo": "b.flac",
                      "veredicto": "SUSPICIOUS", "score": 0.4}
        self.window._process("stdout", genuine)
        self.assertEqual(self.window.hero_headline.cget("text"), "GENUINE LOSSLESS")
        self.window._process("stdout", suspicious)
        self.assertEqual(self.window.hero_headline.cget("text"), "MIXED RESULTS")
        self.assertEqual(self.window.hero_number.cget("text"), "2")
        self.window._process("stdout", {"tipo": "resultado", "archivo": "c.flac",
                                        "error": "corrupt"})
        self.assertEqual(self.window.hero_headline.cget("text"), "MIXED RESULTS")
        self.assertEqual(self.window.hero_headline.cget("text_color"),
                         theme.color("bad"))

    def test_long_file_names_are_truncated(self):
        event = {"tipo": "resultado", "archivo": "A" * 120 + ".flac",
                 "veredicto": "GENUINE LOSSLESS", "score": 1.0}
        self.window._process("stdout", event)
        label = self.window.table_rows[0]["file_label"]
        self.window._refit_file_name(label, types.SimpleNamespace(width=120))
        self.assertTrue(label.cget("text").endswith("…"), label.cget("text"))
        self.assertEqual(label.full_text, event["archivo"])
        self.assertEqual(self.window._fit("short.flac", theme.font(13), 400),
                         "short.flac")

    def test_activity_starts_collapsed_and_toggles(self):
        self.assertEqual(self.window.activity_bar.winfo_manager(), "grid")
        self.assertFalse(self.window.activity_panel.winfo_manager())
        self.window._toggle_activity()
        self.assertFalse(self.window.activity_bar.winfo_manager())
        self.assertEqual(self.window.activity_panel.winfo_manager(), "grid")
        self.window._toggle_activity()
        self.assertEqual(self.window.activity_bar.winfo_manager(), "grid")
        self.assertFalse(self.window.activity_panel.winfo_manager())

    def test_activity_clear_empties_the_log(self):
        self.window._set_activity(True)
        self.assertTrue(self.window.log.get("1.0", "end").strip())
        self.window._clear_log()
        self.assertEqual(self.window.log.get("1.0", "end").strip(), "")
        self.assertEqual(self.window.label_activity_preview.cget("text"),
                         "No activity yet")

    def test_activity_autoscroll_follows_the_toggle(self):
        self.assertTrue(self.window.var_autoscroll.get())
        with mock.patch.object(self.window.log, "see") as see:
            self.window.var_autoscroll.set(False)
            self.window._log("not followed")
            see.assert_not_called()
            self.window.var_autoscroll.set(True)
            self.window._log("followed")
            see.assert_called_once_with("end")

    def test_activity_preview_shows_the_last_line(self):
        self.window._log("Engine warning: cache disabled", "aviso")
        self.assertIn("Engine warning",
                      self.window.label_activity_preview.cget("text"))

    def test_drop_zone_is_keyboard_focusable(self):
        zone = self.window.drop_zone
        self.assertEqual(str(zone.tk.call(zone._w, "cget", "-takefocus")), "1")
        self.assertTrue(zone.tk.call("bind", zone._w, "<Button-1>"))
        self.assertTrue(zone.tk.call("bind", zone._w, "<Return>"))
        self.assertEqual(zone.headline.cget("text"),
                         "Drop a folder or a .flac file")
        zone.set_active(True)
        self.assertTrue(zone.active)
        zone.set_active(False)
        self.assertFalse(zone.active)

    def test_hero_actions_follow_the_report_and_the_selection(self):
        self.assertEqual(self.window.btn_open_pdf.cget("state"), "disabled")
        self.assertEqual(self.window.btn_reveal.cget("state"), "disabled")
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, gui.REPORT_NAME)
            with open(path, "wb") as report:
                report.write(b"%PDF-1.4\n")
            self.window._process("stdout", {"tipo": "informe", "ruta": path,
                                            "paginas": 1, "total": 1})
            self.assertEqual(self.window.btn_open_pdf.cget("state"), "normal")
            self.window.var_path.set(folder)
            self.window._update_state()
            self.assertEqual(self.window.btn_reveal.cget("state"), "normal")

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
        # The line-icon set of DESIGN.md section 8, at its three shipped sizes,
        # inside the size budget of the brief.
        icons = gui.ICON_FOLDER
        self.assertTrue(os.path.isdir(icons), f"missing {icons}")
        total = 0
        for name in ("folder", "play", "stop", "file-text", "external-link",
                     "settings", "sun", "moon", "chevron-down", "check", "x",
                     "alert-triangle", "info", "refresh", "trash", "search",
                     "music"):
            for size in gui.ICON_SIZES:
                path = os.path.join(icons, f"{name}_{size}.png")
                with self.subTest(icon=f"{name}_{size}"):
                    self.assertTrue(os.path.exists(path), f"missing {path}")
                    total += os.path.getsize(path)
        self.assertLess(total, 100_000, "the icon set is over the 100 kB budget")

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
            sizes = sorted(icon.info["sizes"])
        self.assertGreaterEqual(len(sizes), 6)
        self.assertIn((16, 16), sizes)          # taskbar
        self.assertIn((256, 256), sizes)        # high-density .exe

    def test_drawing_respects_splash_proportions(self):
        """The five bars and their heights come from splashscreen.html."""
        from PIL import Image
        with Image.open(self.PATHS["cuadrado"]) as image:
            canvas = image.convert("RGBA")
            width, height = canvas.size
            white = (255, 255, 255, 255)
            columns = []
            for x in range(width):
                ys = [y for y in range(height) if canvas.getpixel((x, y)) == white]
                columns.append((min(ys), max(ys)) if ys else None)

        grupos, current = [], None
        for x, dato in enumerate(columns):
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
        for measurement, expected in zip(alturas, self.ALTURAS_SPLASH):
            with self.subTest(barra=expected):
                self.assertAlmostEqual(measurement / max(alturas), expected / reference,
                                       delta=0.02)

    def test_check_is_teal_below_right(self):
        from PIL import Image
        teal = (0x4F, 0x98, 0xA3, 255)
        with Image.open(self.PATHS["cuadrado"]) as image:
            canvas = image.convert("RGBA")
            width, height = canvas.size
            pixeles = [(x, y) for x in range(0, width, 2) for y in range(0, height, 2)
                       if canvas.getpixel((x, y)) == teal]
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

    It is removed from the grid instead of being disabled, so it frees its space
    and no phantom row is left behind. The assertions are about existence, state
    and grid structure rather than about pixel positions: where a control lands
    is the business of the layout phase, and a pixel comparison of a withdrawn
    window depended on Tk's first idle pass instead of on the code.
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

    def _rail(self):
        return self.window.seconds_row.master

    def _gridded_rows(self):
        """Rows of the rail that currently hold a widget (empty row = no gap)."""
        return {int(widget.grid_info()["row"]) for widget in self._rail().grid_slaves()
                if widget.grid_info()}

    def _rail_height(self):
        self.window.update_idletasks()
        return self._rail().grid_bbox()[3]

    def _settle(self):
        """One show/hide cycle: Tk's very first idle pass measures 4 px short."""
        self._set_mode("seconds")
        self._set_mode("center")

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
        self.assertGreater(int(info["row"]),
                           int(self.window.option_mode.grid_info()["row"]),
                           "the seconds control is not under the mode selector")
        self.assertEqual(self.window.seconds_slider.cget("state"), "normal")

    def test_widget_still_existing_when_hidden(self):
        self._set_mode("center")
        self.assertTrue(self.window.label_seconds.winfo_exists())
        self.assertTrue(self.window.seconds_slider.winfo_exists())
        self.window._change_seconds(45)
        self.assertIn("45", self.window.label_seconds.cget("text"))

    def test_show_it_makes_room_and_the_action_bar_stays_pinned(self):
        self._set_mode("center")
        self._settle()
        hidden = self._rail_height()
        self._set_mode("seconds")
        self.assertGreater(self._rail_height(), hidden,
                           "the rail did not make room for the seconds control")
        # Analyze lives in the footer, below the flow, so it is always visible:
        # it is not pushed down with the rest of the controls.
        footer = self.window.btn_analyze.master
        self.assertIsNot(footer, self._rail())
        self.assertGreater(int(footer.grid_info()["row"]),
                           int(self.window.check_cache.grid_info()["row"]))
        self.assertTrue(self.window.btn_analyze.winfo_manager())

    def test_hide_it_no_leaves_phantom_row(self):
        self._set_mode("center")
        reference = self._gridded_rows()
        self._set_mode("seconds")
        self.assertGreater(len(self._gridded_rows()), len(reference),
                           "the seconds row is not in the grid")
        self._set_mode("full")
        self.assertEqual(self._gridded_rows(), reference,
                         "hiding the seconds control left a phantom row behind")

    def test_hidden_shown_several_times_no_accumulates_rows(self):
        self._set_mode("center")
        reference = self._gridded_rows()
        for _ in range(3):
            self._set_mode("seconds")
            self._set_mode("center")
        self.assertEqual(self._gridded_rows(), reference)
        self.assertEqual(self.window.seconds_row.grid_info(), {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
