"""Checks the already built executable (dist/FLAC_Verifier).

It runs on the build machine, after `python build.py`:

    python comprobar_ejecutable.py                       (uses the test album)
    python comprobar_ejecutable.py --album "D:/Music/Album"
    python comprobar_ejecutable.py --quick               (skips the PDF and the GUI)

Every step launches the .exe with the output REDIRECTED TO A FILE, never to a
pipe: the child processes of the parallel analysis inherit the descriptors, and with
a pipe the reader waits forever for an end of stream that never arrives. In addition,
every step has its own time limit, so a hang is detected and reported instead of
sitting there. Before anything is launched, the folder that holds the executables
is raised to the Medium mandatory integrity label: a build produced inside a
low-integrity workspace inherits that `Low` label, and a Low process cannot write
into a Medium folder (the Windows no-write-up rule). That mismatch, and not a
reportlab bug, is what made the PDF step report PermissionError.

What is checked, in order:
  1. Both executables exist and state their version.
  2. The engine analyses serially (the path without multiprocessing), invoked as a
     user would: `flac_motor.exe --path …`, with no extra flags.
  3. The engine analyses IN PARALLEL with 8 processes: it is the
     multiprocessing.freeze_support() path. Without it, each worker would start the
     application again and this would hang.
  4. The engine generates PNG spectrograms and the PDF report (Pillow and
     reportlab packaged).
  5. The EXACT command that the packaged GUI builds (it asks
     `gui.engine_path()`, it is not copied by hand) and the `--motor-cli` fallback on
     the windowed executable.
  6. The graphical interface opens a real window and closes without leaving processes.
  7. The executable gives the SAME records as the source code on the same
     album, field by field: that is what guarantees that packaging did not change
     the analysis.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

AQUI = os.path.dirname(os.path.abspath(__file__))
DIST = os.path.join(AQUI, "dist", "FLAC_Verifier")
ENGINE_SOURCE = os.path.join(AQUI, "motor_flac.py")
# The test album of the audit lives two folders above (next to the
# project, not inside it).
ALBUM_TEST = os.path.join(os.path.dirname(os.path.dirname(AQUI)),
                            "_audit_flac", "_demo_album")
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
LIMIT = 300

TOKEN_INTEGRITY_LEVEL = 25        # TokenIntegrityLevel, from winnt.h
INTEGRITY_MEAN = 0x2000         # SECURITY_MANDATORY_MEDIUM_RID


def _integrity_process_windows() -> int | None:
    """RID of the mandatory integrity level of THIS process token (0x1000 Low…).

    Windows does not let a Low process write into a folder labelled Medium
    (no-write-up). Knowing the caller's level is the difference between reporting
    "the PDF step failed" and reporting why, so it is read straight from the token
    with ctypes: no extra dependency and no external command.

    Returns None when the query cannot be made (not Windows, no permission).
    """
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes

    advapi32 = ctypes.windll.advapi32
    kernel32 = ctypes.windll.kernel32

    # The prototypes are mandatory: without them the pseudo-handle returned by
    # GetCurrentProcess() is truncated to 32 bits on a 64-bit interpreter and
    # OpenProcessToken fails with a meaningless error.
    advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                          ctypes.POINTER(wintypes.HANDLE)]
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                             ctypes.c_void_p, wintypes.DWORD,
                                             ctypes.POINTER(wintypes.DWORD)]
    advapi32.GetTokenInformation.restype = wintypes.BOOL
    advapi32.GetSidSubAuthorityCount.argtypes = [ctypes.c_void_p]
    advapi32.GetSidSubAuthorityCount.restype = ctypes.POINTER(ctypes.c_ubyte)
    advapi32.GetSidSubAuthority.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    advapi32.GetSidSubAuthority.restype = ctypes.POINTER(wintypes.DWORD)
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    class SID_AND_ATTRIBUTES(ctypes.Structure):
        _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]

    class TOKEN_MANDATORY_LABEL(ctypes.Structure):
        _fields_ = [("Label", SID_AND_ATTRIBUTES)]

    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), 0x0008,
                                     ctypes.byref(token)):
        return None
    try:
        size = wintypes.DWORD(0)
        advapi32.GetTokenInformation(token, TOKEN_INTEGRITY_LEVEL, None, 0,
                                     ctypes.byref(size))
        if not size.value:
            return None
        buffer = ctypes.create_string_buffer(size.value)
        if not advapi32.GetTokenInformation(token, TOKEN_INTEGRITY_LEVEL, buffer,
                                            size.value, ctypes.byref(size)):
            return None
        label = ctypes.cast(buffer,
                               ctypes.POINTER(TOKEN_MANDATORY_LABEL)).contents
        subautoridades = advapi32.GetSidSubAuthorityCount(label.Label.Sid).contents.value
        if not subautoridades:
            return None
        # The integrity level is the LAST sub-authority of the label SID.
        return advapi32.GetSidSubAuthority(label.Label.Sid,
                                           subautoridades - 1).contents.value
    finally:
        kernel32.CloseHandle(token)


def _normalize_integrity_folder(folder: str) -> None:
    """Raises the label of `carpeta` to Medium before the executables inside run.

    It is the repair for a build made inside a Low workspace: the .exe files
    inherit the Low label and then cannot write the PDF into any normal folder.
    Only attempted when the caller is already Medium or above, because a Low
    process cannot hand out a label it does not hold.
    """
    if os.name != "nt" or not os.path.isdir(folder):
        return
    integrity = _integrity_process_windows()
    if integrity is None or integrity < INTEGRITY_MEAN:
        return
    try:
        result = subprocess.run(["icacls", folder, "/setintegritylevel",
                                    "(OI)(CI)M", "/T", "/Q"],
                                   capture_output=True, text=True, timeout=120,
                                   creationflags=NO_WINDOW)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"  note: could not normalise the integrity label "
              f"({type(exc).__name__}: {exc})")
        return
    if result.returncode != 0:
        print(f"  note: icacls returned {result.returncode} while normalising "
              f"the integrity label of {folder}")


def _titles_windows_visible(pid: int) -> list[str]:
    """Visible top-level window titles owned by `pid` (Win32, via ctypes).

    Tk/CustomTkinter keeps the real toplevel under the root window; PowerShell's
    Get-Process.MainWindowTitle can return an empty string for it. EnumWindows
    gives every top-level window, then the PID and IsWindowVisible filters keep
    only the window the user can actually see.
    """
    if os.name != "nt":
        return []
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    prototipo = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [prototipo, wintypes.LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND,
                                                ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int

    titles: list[str] = []

    def visitar(hwnd, _lparam):
        pid_window = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid_window))
        if pid_window.value != pid:
            return True
        if not user32.IsWindowVisible(hwnd):
            return True
        long = user32.GetWindowTextLengthW(hwnd)
        if long > 0:
            buffer = ctypes.create_unicode_buffer(long + 1)
            user32.GetWindowTextW(hwnd, buffer, long + 1)
            title = buffer.value.strip()
            if title:
                titles.append(title)
        return True

    user32.EnumWindows(prototipo(visitar), 0)
    return titles


def _close_process_and_children(process: subprocess.Popen) -> None:
    """Terminates the GUI and, if it does not exit, its whole process tree."""
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=30)
        return
    except subprocess.TimeoutExpired:
        pass
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                       capture_output=True, text=True, timeout=30,
                       creationflags=NO_WINDOW)
    else:
        process.kill()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass


def _utf8() -> None:
    for name in ("stdout", "stderr"):
        try:
            # line_buffering: if the output goes to a pipe (for example a
            # script or a background task) the progress is seen step by step
            # instead of all at once at the end.
            getattr(sys, name, None).reconfigure(encoding="utf-8", errors="replace",
                                                   line_buffering=True)
        except (AttributeError, ValueError, OSError):
            pass


class Comprobador:
    def __init__(self, folder: str) -> None:
        self.folder = folder
        self.gui = os.path.join(folder, "FLAC_Verifier.exe")
        self.engine = os.path.join(folder, "flac_motor.exe")
        self.results: list[tuple[str, bool, str]] = []
        self.temporal = tempfile.mkdtemp(prefix="flac_exe_")

    # ── utilities ───────────────────────────────────────────────────────────
    def anotar(self, name: str, well: bool, detail: str = "") -> None:
        self.results.append((name, well, detail))
        print(f"  {'OK  ' if well else 'FALLA'} {name}"
              + (f"  ·  {detail}" if detail else ""))

    def launch(self, command: list[str], limit: int = LIMIT,
               label: str = "output") -> tuple[int | None, str]:
        """Runs with the output to a file and returns (code, text)."""
        path = os.path.join(self.temporal, f"{label}.txt")
        with open(path, "w", encoding="utf-8", errors="replace") as output:
            try:
                process = subprocess.run(command, stdout=output,
                                         stderr=subprocess.STDOUT, timeout=limit,
                                         creationflags=NO_WINDOW, cwd=AQUI)
                code = process.returncode
            except subprocess.TimeoutExpired:
                return None, f"TIMED OUT ({limit} s)"
        with open(path, encoding="utf-8", errors="replace") as f:
            return code, f.read()

    @staticmethod
    def events(text: str) -> list[dict]:
        list = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                list.append(json.loads(line))
            except ValueError:
                continue
        return list

    @staticmethod
    def cards(text: str) -> dict[str, dict]:
        """The `resultado` events per file, without the fields that change on their own.

        The path, the PNG and the cache mark are removed: the rest (verdict,
        score, MD5, problems, DR…) has to be identical between the source
        code and the executable, because it is the analysis itself.
        """
        volatile = ("ruta", "espectrograma", "desde_cache")
        return {e["archivo"]: {k: v for k, v in e.items() if k not in volatile}
                for e in Comprobador.events(text)
                if e.get("tipo") == "resultado" and e.get("archivo")}

    @staticmethod
    def abreviar(text: str, limit: int = 90) -> str:
        text = " ".join(text.split())
        return text[:limit] + ("…" if len(text) > limit else "")

    def close(self) -> None:
        """Removes the scratch folder; without this every run leaves a flac_exe_*.

        The per-step output files live in that folder and are only useful while
        the run lasts, so the checker owns their lifetime instead of trusting the
        caller to clean up after a failure halfway through.
        """
        shutil.rmtree(self.temporal, ignore_errors=True)

    # ── steps ───────────────────────────────────────────────────────────────
    def step_1_executables(self) -> None:
        print("\n[1] Both executables exist and respond")
        for label, path in (("interface", self.gui), ("engine", self.engine)):
            if not os.path.exists(path):
                self.anotar(f"{label}: {os.path.basename(path)}", False, "no existe")
                continue
            size = os.path.getsize(path) / 1e6
            code, text = self.launch([path, "--version"], limit=180,
                                        label=f"version_{label}")
            well = code == 0
            self.anotar(f"{label}: {os.path.basename(path)} ({size:.1f} MB)",
                        well, self.abreviar(text) or f"exit code {code}")

    def step_2_series(self, album: str) -> None:
        print("\n[2] Frozen engine, serial (no multiprocessing)")
        start = time.perf_counter()
        code, text = self.launch([self.engine, "--path", album, "--mode", "center",
                                     "--workers", "1", "--no-cache"],
                                    label="serial")
        seconds = time.perf_counter() - start
        events = self.events(text)
        tipos = [e.get("tipo") for e in events]
        results = [e for e in events if e.get("tipo") == "resultado"]
        well = (code == 0 and tipos[:1] == ["inicio"] and tipos[-1:] == ["fin"]
                and bool(results))
        self.anotar(f"serial analysis of {len(results)} file(s)",
                    well, f"{seconds:.1f} s, exit code {code}, fin={('fin' in tipos)}")
        if not well:
            print("     " + self.abreviar(text, 300))

    def step_3_parallel(self, album: str, workers: int = 8) -> None:
        print(f"\n[3] Frozen engine IN PARALLEL with {workers} processes "
              f"(freeze_support path)")
        start = time.perf_counter()
        code, text = self.launch([self.engine, "--path", album, "--mode", "center",
                                     "--workers", str(workers), "--no-cache"],
                                    label="parallel")
        seconds = time.perf_counter() - start
        if code is None:
            self.anotar(f"analysis with {workers} processes", False, text)
            return
        events = self.events(text)
        results = [e for e in events if e.get("tipo") == "resultado"]
        start_event = next((e for e in events if e.get("tipo") == "inicio"), {})
        well = (code == 0 and len(results) > 0
                and [e.get("tipo") for e in events][-1:] == ["fin"])
        detail = (f"{seconds:.1f} s, {len(results)} resultados, "
                   f"workers={start_event.get('workers')}, "
                   f"paralelo={start_event.get('paralelo')}, exit code {code}")
        self.anotar(f"analysis with {workers} processes", well, detail)
        if not well:
            print("     " + self.abreviar(text, 300))
        elif "paralelo" not in start_event:
            # The protocol keys are Spanish by design: they are identifiers and were
            # never renamed. Reading "parallel" here — the English word — silently
            # returned None and made this check report a fallback that had not
            # happened, which is worse than not checking at all.
            self.anotar("real processes were used", False,
                        "the engine did not report the 'paralelo' field: the protocol "
                        "contract changed")
        elif not start_event.get("paralelo"):
            self.anotar("real processes were used", False,
                        "the engine fell back to serial: this environment cannot create "
                        "processes (the intended fallback). Run it in a "
                        "normal console, without a sandbox, to test the parallel path")

    def step_4_pdf(self, album: str) -> None:
        print("\n[4] Spectrograms and PDF report from the executable")
        start = time.perf_counter()
        code, text = self.launch([self.engine, "--path", album, "--mode", "center",
                                     "--pdf", "--workers", "2", "--no-cache"],
                                    label="pdf")
        seconds = time.perf_counter() - start
        events = self.events(text)
        report = next((e for e in events if e.get("tipo") == "informe"), None)
        warnings = [e for e in events if e.get("tipo") == "aviso"]
        if report is None:
            detail = self.abreviar(warnings[0].get("mensaje", "") if warnings else text)
            integrity = _integrity_process_windows()
            if integrity is not None and integrity < INTEGRITY_MEAN:
                detail += (f" [caller token has Low Mandatory Integrity "
                            f"0x{integrity:x}; run: icacls <venv> "
                            f"/setintegritylevel (OI)(CI)M /T]")
            self.anotar("PDF report", False, detail)
            return
        pdf_path = report.get("ruta", "")
        exists = os.path.exists(pdf_path)
        self.anotar("PDF report generated", exists and code == 0,
                    f"{os.path.basename(pdf_path)} "
                    f"({os.path.getsize(pdf_path) / 1e6:.2f} MB, "
                    f"{report.get('paginas')} pages, {seconds:.1f} s)" if exists else pdf_path)
        png_folder = os.path.join(album, "_spectrograms")
        pngs = [f for f in os.listdir(png_folder)] if os.path.isdir(png_folder) else []
        self.anotar("PNG spectrograms (Pillow packaged)",
                    any(f.endswith(".png") for f in pngs), f"{len(pngs)} file(s)")

    def step_5_flag_internal(self, album: str) -> None:
        print("\n[5] The command the packaged GUI builds, and the --motor-cli flag")

        # (a) What the GUI really runs when it is packaged. This process is made
        # to pretend that it is the .exe so that `ruta_motor()` decides with the
        # real rules, instead of copying the command by hand here (copying it is
        # exactly what let the bug of the missing --motor-cli through).
        try:
            import gui
        except Exception as e:                 # no customtkinter on this machine
            self.anotar("command built by the packaged GUI", False,
                        f"could not read gui.engine_path(): {type(e).__name__}: {e}"
                        " · install customtkinter on the build machine")
        else:
            original_frozen, original_exe = getattr(sys, "frozen", None), sys.executable
            try:
                sys.frozen = True
                sys.executable = self.gui
                base = gui.engine_path()
            finally:
                if original_frozen is None:
                    del sys.frozen
                else:
                    sys.frozen = original_frozen
                sys.executable = original_exe
            command = [*base, "--path", album, "--mode", "center",
                       "--workers", "1", "--no-cache"]
            code, text = self.launch(command, limit=180, label="packaged_gui")
            events = self.events(text)
            results = [e for e in events if e.get("tipo") == "resultado"]
            self.anotar(f"command built by the packaged GUI: {' '.join(base)}",
                        code == 0 and bool(results),
                        f"{len(results)} result(s), exit code {code}")

        # (b) The internal flag on the windowed .exe: it is the fallback of a
        # single executable. It works because `_asegurar_flujos()` rebuilds
        # stdout, which in a windowed .exe arrives as None.
        code, text = self.launch([self.gui, "--motor-cli", "--path", album,
                                     "--mode", "center", "--workers", "1",
                                     "--no-cache"], limit=180, label="flag")
        events = self.events(text)
        results = [e for e in events if e.get("tipo") == "resultado"]
        self.anotar("the windowed .exe delivers the NDJSON protocol with --motor-cli",
                    code == 0 and bool(results),
                    f"{len(results)} result(s), exit code {code}")
        if not results:
            self.anotar("(that is why the GUI uses flac_motor.exe, which does have a console)",
                        True, "expected behaviour in a windowed .exe")

    def step_6_window(self) -> None:
        print("\n[6] The graphical interface opens and closes leaving no processes")
        if not os.path.exists(self.gui):
            self.anotar("GUI window", False, "the executable does not exist")
            return
        process = subprocess.Popen([self.gui, "--gui"], creationflags=NO_WINDOW)
        try:
            title = ""
            titles: list[str] = []
            for _ in range(40):                     # up to 20 s
                time.sleep(0.5)
                if process.poll() is not None:
                    break
                titles = _titles_windows_visible(process.pid)
                title = next((t for t in titles if "FLAC" in t.upper()), "")
                if title:
                    break
            well = process.poll() is None and bool(title)
            if well:
                detail = f"title: '{title}'"
            elif titles:
                detail = (f"no visible FLAC window; titles: "
                           + ", ".join(repr(t) for t in titles[:3]))
            else:
                detail = f"no visible FLAC window (exit code {process.poll()})"
            self.anotar("the window opens", well, detail)
        finally:
            _close_process_and_children(process)
        time.sleep(1.5)
        overflow = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "@(Get-Process -Name FLAC_Verifier,flac_motor -ErrorAction SilentlyContinue).Count"],
            capture_output=True, text=True, timeout=60, creationflags=NO_WINDOW)
        cuantos = (overflow.stdout or "0").strip() or "0"
        self.anotar("no processes left after closing", cuantos == "0",
                    f"live processes: {cuantos}")

    def step_7_same_verdicts(self, album: str) -> None:
        """The executable cannot analyse differently from the source code.

        It is the check that really closes out the packaging: same album,
        same arguments, and the records have to match field by field
        (verdict, score, MD5, DR, problems…).
        """
        print("\n[7] The executable gives the same verdicts as the source code")
        arguments = ["--path", album, "--mode", "center", "--workers", "1",
                      "--no-cache"]
        _, text_source = self.launch([sys.executable, "-u", ENGINE_SOURCE, *arguments],
                                      label="source")
        _, text_exe = self.launch([self.engine, *arguments], label="frozen_exe")
        source = self.cards(text_source)
        exe_file = self.cards(text_exe)
        if not source or not exe_file:
            self.anotar("comparison against the source code", False,
                        f"records read: source={len(source)}, executable={len(exe_file)}")
            return
        diferencias = [name for name, card in source.items()
                       if card != exe_file.get(name)]
        detail = (f"{len(source)} record(s) compared"
                   if not diferencias else
                   "differing: " + ", ".join(diferencias[:3]))
        self.anotar("same records field by field (verdict, MD5, DR…)", not diferencias,
                    detail)
        for name in diferencias[:2]:
            print(f"     fuente     {name}: {self.abreviar(json.dumps(source[name], ensure_ascii=False), 200)}")
            print(f"     executable {name}: {self.abreviar(json.dumps(exe_file.get(name), ensure_ascii=False), 200)}")

    def summary(self) -> int:
        print("\n" + "=" * 72)
        failures = [r for r in self.results if not r[1]]
        if failures:
            print(f"RESULT: {len(failures)} check(s) failed out of "
                  f"{len(self.results)}")
            for name, _, detail in failures:
                print(f"  ✗ {name}  {detail}")
            return 1
        print(f"RESULT: all {len(self.results)} checks have passed")
        return 0


def main() -> int:
    _utf8()
    parser = argparse.ArgumentParser(description="Check the built .exe")
    parser.add_argument("--folder", dest="carpeta", default=DIST,
                        help="folder with the .exe files (default dist/FLAC_Verifier)")
    parser.add_argument("--album", default=ALBUM_TEST,
                        help="album used for the analysis tests")
    parser.add_argument("--quick", dest="rapido", action="store_true",
                        help="only steps 1-3 (no PDF, no window)")
    args = parser.parse_args()

    print("=" * 72)
    print("EXECUTABLE CHECK")
    print("=" * 72)
    print(f"  executables: {args.folder}")
    print(f"  test album: {args.album}")

    comprobador = Comprobador(args.folder)
    try:
        _normalize_integrity_folder(args.folder)
        comprobador.step_1_executables()
        if os.path.isdir(args.album):
            comprobador.step_2_series(args.album)
            comprobador.step_3_parallel(args.album)
            if not args.rapido:
                comprobador.step_4_pdf(args.album)
                comprobador.step_5_flag_internal(args.album)
                comprobador.step_7_same_verdicts(args.album)
        else:
            comprobador.anotar("test album", False,
                               f"not found: {args.album} — pass --album")
        if not args.rapido:
            comprobador.step_6_window()
    finally:
        comprobador.close()
    return comprobador.summary()


if __name__ == "__main__":
    raise SystemExit(main())
