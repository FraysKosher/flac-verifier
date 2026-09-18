"""Checks the already built executable (dist/FLAC_Verifier).

It runs on the build machine, after `python build.py`:

    python comprobar_ejecutable.py                       (uses the test album)
    python comprobar_ejecutable.py --album "D:/Music/Album"
    python comprobar_ejecutable.py --quick               (skips the PDF and the GUI)

Every step launches the .exe with the output REDIRECTED TO A FILE, never to a
pipe: the child processes of the parallel analysis inherit the descriptors, and with
a pipe the reader waits forever for an end of stream that never arrives. In addition,
every step has its own time limit, so a hang is detected and reported instead of
sitting there.

What is checked, in order:
  1. Both executables exist and state their version.
  2. The engine analyses serially (the path without multiprocessing), invoked as a
     user would: `flac_motor.exe --ruta …`, with no extra flags.
  3. The engine analyses IN PARALLEL with 8 processes: it is the
     multiprocessing.freeze_support() path. Without it, each worker would start the
     application again and this would hang.
  4. The engine generates PNG spectrograms and the PDF report (matplotlib and
     reportlab packaged).
  5. The EXACT command that the packaged GUI builds (it asks
     `gui.ruta_motor()`, it is not copied by hand) and the `--motor-cli` fallback on
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
import subprocess
import sys
import tempfile
import time

AQUI = os.path.dirname(os.path.abspath(__file__))
DIST = os.path.join(AQUI, "dist", "FLAC_Verifier")
MOTOR_FUENTE = os.path.join(AQUI, "motor_flac.py")
# The test album of the audit lives two folders above (next to the
# project, not inside it).
ALBUM_PRUEBA = os.path.join(os.path.dirname(os.path.dirname(AQUI)),
                            "_audit_flac", "_demo_album")
SIN_VENTANA = getattr(subprocess, "CREATE_NO_WINDOW", 0)
LIMITE = 300


def _utf8() -> None:
    for nombre in ("stdout", "stderr"):
        try:
            # line_buffering: if the output goes to a pipe (for example a
            # script or a background task) the progress is seen step by step
            # instead of all at once at the end.
            getattr(sys, nombre, None).reconfigure(encoding="utf-8", errors="replace",
                                                   line_buffering=True)
        except (AttributeError, ValueError, OSError):
            pass


class Comprobador:
    def __init__(self, carpeta: str) -> None:
        self.carpeta = carpeta
        self.gui = os.path.join(carpeta, "FLAC_Verifier.exe")
        self.motor = os.path.join(carpeta, "flac_motor.exe")
        self.resultados: list[tuple[str, bool, str]] = []
        self.temporal = tempfile.mkdtemp(prefix="flac_exe_")

    # ── utilities ───────────────────────────────────────────────────────────
    def anotar(self, nombre: str, bien: bool, detalle: str = "") -> None:
        self.resultados.append((nombre, bien, detalle))
        print(f"  {'OK  ' if bien else 'FALLA'} {nombre}"
              + (f"  ·  {detalle}" if detalle else ""))

    def lanzar(self, comando: list[str], limite: int = LIMITE,
               etiqueta: str = "output") -> tuple[int | None, str]:
        """Runs with the output to a file and returns (code, text)."""
        ruta = os.path.join(self.temporal, f"{etiqueta}.txt")
        with open(ruta, "w", encoding="utf-8", errors="replace") as salida:
            try:
                proceso = subprocess.run(comando, stdout=salida,
                                         stderr=subprocess.STDOUT, timeout=limite,
                                         creationflags=SIN_VENTANA, cwd=AQUI)
                codigo = proceso.returncode
            except subprocess.TimeoutExpired:
                return None, f"TIMED OUT ({limite} s)"
        with open(ruta, encoding="utf-8", errors="replace") as f:
            return codigo, f.read()

    @staticmethod
    def eventos(texto: str) -> list[dict]:
        lista = []
        for linea in texto.splitlines():
            linea = linea.strip()
            if not linea:
                continue
            try:
                lista.append(json.loads(linea))
            except ValueError:
                continue
        return lista

    @staticmethod
    def fichas(texto: str) -> dict[str, dict]:
        """The `resultado` events per file, without the fields that change on their own.

        The path, the PNG and the cache mark are removed: the rest (verdict,
        score, MD5, problems, DR…) has to be identical between the source
        code and the executable, because it is the analysis itself.
        """
        volatiles = ("ruta", "espectrograma", "desde_cache")
        return {e["archivo"]: {k: v for k, v in e.items() if k not in volatiles}
                for e in Comprobador.eventos(texto)
                if e.get("tipo") == "resultado" and e.get("archivo")}

    @staticmethod
    def abreviar(texto: str, limite: int = 90) -> str:
        texto = " ".join(texto.split())
        return texto[:limite] + ("…" if len(texto) > limite else "")

    # ── steps ───────────────────────────────────────────────────────────────
    def paso_1_ejecutables(self) -> None:
        print("\n[1] Both executables exist and respond")
        for etiqueta, ruta in (("interface", self.gui), ("engine", self.motor)):
            if not os.path.exists(ruta):
                self.anotar(f"{etiqueta}: {os.path.basename(ruta)}", False, "no existe")
                continue
            tamano = os.path.getsize(ruta) / 1e6
            codigo, texto = self.lanzar([ruta, "--version"], limite=180,
                                        etiqueta=f"version_{etiqueta}")
            bien = codigo == 0
            self.anotar(f"{etiqueta}: {os.path.basename(ruta)} ({tamano:.1f} MB)",
                        bien, self.abreviar(texto) or f"exit code {codigo}")

    def paso_2_serie(self, album: str) -> None:
        print("\n[2] Frozen engine, serial (no multiprocessing)")
        inicio = time.perf_counter()
        codigo, texto = self.lanzar([self.motor, "--path", album, "--mode", "center",
                                     "--workers", "1", "--no-cache"],
                                    etiqueta="serial")
        segundos = time.perf_counter() - inicio
        eventos = self.eventos(texto)
        tipos = [e.get("tipo") for e in eventos]
        resultados = [e for e in eventos if e.get("tipo") == "resultado"]
        bien = (codigo == 0 and tipos[:1] == ["inicio"] and tipos[-1:] == ["fin"]
                and bool(resultados))
        self.anotar(f"serial analysis of {len(resultados)} file(s)",
                    bien, f"{segundos:.1f} s, exit code {codigo}, fin={('fin' in tipos)}")
        if not bien:
            print("     " + self.abreviar(texto, 300))

    def paso_3_paralelo(self, album: str, workers: int = 8) -> None:
        print(f"\n[3] Frozen engine IN PARALLEL with {workers} processes "
              f"(freeze_support path)")
        inicio = time.perf_counter()
        codigo, texto = self.lanzar([self.motor, "--path", album, "--mode", "center",
                                     "--workers", str(workers), "--no-cache"],
                                    etiqueta="parallel")
        segundos = time.perf_counter() - inicio
        if codigo is None:
            self.anotar(f"analysis with {workers} processes", False, texto)
            return
        eventos = self.eventos(texto)
        resultados = [e for e in eventos if e.get("tipo") == "resultado"]
        inicio_evento = next((e for e in eventos if e.get("tipo") == "inicio"), {})
        bien = (codigo == 0 and len(resultados) > 0
                and [e.get("tipo") for e in eventos][-1:] == ["fin"])
        detalle = (f"{segundos:.1f} s, {len(resultados)} resultados, "
                   f"workers={inicio_evento.get('workers')}, "
                   f"paralelo={inicio_evento.get('paralelo')}, exit code {codigo}")
        self.anotar(f"analysis with {workers} processes", bien, detalle)
        if not bien:
            print("     " + self.abreviar(texto, 300))
        elif "paralelo" not in inicio_evento:
            # The protocol keys are Spanish by design: they are identifiers and were
            # never renamed. Reading "parallel" here — the English word — silently
            # returned None and made this check report a fallback that had not
            # happened, which is worse than not checking at all.
            self.anotar("real processes were used", False,
                        "the engine did not report the 'paralelo' field: the protocol "
                        "contract changed")
        elif not inicio_evento.get("paralelo"):
            self.anotar("real processes were used", False,
                        "the engine fell back to serial: this environment cannot create "
                        "processes (the intended fallback). Run it in a "
                        "normal console, without a sandbox, to test the parallel path")

    def paso_4_pdf(self, album: str) -> None:
        print("\n[4] Spectrograms and PDF report from the executable")
        inicio = time.perf_counter()
        codigo, texto = self.lanzar([self.motor, "--path", album, "--mode", "center",
                                     "--pdf", "--workers", "2", "--no-cache"],
                                    etiqueta="pdf")
        segundos = time.perf_counter() - inicio
        eventos = self.eventos(texto)
        informe = next((e for e in eventos if e.get("tipo") == "informe"), None)
        avisos = [e for e in eventos if e.get("tipo") == "aviso"]
        if informe is None:
            self.anotar("PDF report", False,
                        self.abreviar(avisos[0].get("mensaje", "") if avisos else texto))
            return
        ruta_pdf = informe.get("ruta", "")
        existe = os.path.exists(ruta_pdf)
        self.anotar("PDF report generated", existe and codigo == 0,
                    f"{os.path.basename(ruta_pdf)} "
                    f"({os.path.getsize(ruta_pdf) / 1e6:.2f} MB, "
                    f"{informe.get('paginas')} pages, {segundos:.1f} s)" if existe else ruta_pdf)
        carpeta_png = os.path.join(album, "_spectrograms")
        pngs = [f for f in os.listdir(carpeta_png)] if os.path.isdir(carpeta_png) else []
        self.anotar("PNG spectrograms (matplotlib packaged)",
                    any(f.endswith(".png") for f in pngs), f"{len(pngs)} file(s)")

    def paso_5_bandera_interna(self, album: str) -> None:
        print("\n[5] The command the packaged GUI builds, and the --motor-cli flag")

        # (a) What the GUI really runs when it is packaged. This process is made
        # to pretend that it is the .exe so that `ruta_motor()` decides with the
        # real rules, instead of copying the command by hand here (copying it is
        # exactly what let the bug of the missing --motor-cli through).
        try:
            import gui
        except Exception as e:                 # no customtkinter on this machine
            self.anotar("command built by the packaged GUI", False,
                        f"could not read gui.ruta_motor(): {type(e).__name__}: {e}"
                        " · install customtkinter on the build machine")
        else:
            original_frozen, original_exe = getattr(sys, "frozen", None), sys.executable
            try:
                sys.frozen = True
                sys.executable = self.gui
                base = gui.ruta_motor()
            finally:
                if original_frozen is None:
                    del sys.frozen
                else:
                    sys.frozen = original_frozen
                sys.executable = original_exe
            comando = [*base, "--path", album, "--mode", "center",
                       "--workers", "1", "--no-cache"]
            codigo, texto = self.lanzar(comando, limite=180, etiqueta="packaged_gui")
            eventos = self.eventos(texto)
            resultados = [e for e in eventos if e.get("tipo") == "resultado"]
            self.anotar(f"command built by the packaged GUI: {' '.join(base)}",
                        codigo == 0 and bool(resultados),
                        f"{len(resultados)} result(s), exit code {codigo}")

        # (b) The internal flag on the windowed .exe: it is the fallback of a
        # single executable. It works because `_asegurar_flujos()` rebuilds
        # stdout, which in a windowed .exe arrives as None.
        codigo, texto = self.lanzar([self.gui, "--motor-cli", "--path", album,
                                     "--mode", "center", "--workers", "1",
                                     "--no-cache"], limite=180, etiqueta="flag")
        eventos = self.eventos(texto)
        resultados = [e for e in eventos if e.get("tipo") == "resultado"]
        self.anotar("the windowed .exe delivers the NDJSON protocol with --motor-cli",
                    codigo == 0 and bool(resultados),
                    f"{len(resultados)} result(s), exit code {codigo}")
        if not resultados:
            self.anotar("(that is why the GUI uses flac_motor.exe, which does have a console)",
                        True, "expected behaviour in a windowed .exe")

    def paso_6_ventana(self) -> None:
        print("\n[6] The graphical interface opens and closes leaving no processes")
        if not os.path.exists(self.gui):
            self.anotar("GUI window", False, "the executable does not exist")
            return
        proceso = subprocess.Popen([self.gui, "--gui"], creationflags=SIN_VENTANA)
        try:
            titulo = ""
            for _ in range(40):                     # up to 20 s
                time.sleep(0.5)
                if proceso.poll() is not None:
                    break
                ventanas = subprocess.run(
                    ["powershell", "-NoProfile", "-Command",
                     f"(Get-Process -Id {proceso.pid} -ErrorAction SilentlyContinue)"
                     ".MainWindowTitle"],
                    capture_output=True, text=True, timeout=60,
                    creationflags=SIN_VENTANA)
                titulo = (ventanas.stdout or "").strip()
                if "FLAC" in titulo.upper():
                    break
            bien = proceso.poll() is None and "FLAC" in titulo.upper()
            self.anotar("the window opens", bien,
                        f"title: '{titulo}'" if bien else f"title: '{titulo}' (exit code "
                        f"{proceso.poll()})")
        finally:
            if proceso.poll() is None:
                proceso.terminate()
                try:
                    proceso.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    proceso.kill()
        time.sleep(1.5)
        sobran = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "@(Get-Process -Name FLAC_Verifier,flac_motor -ErrorAction SilentlyContinue).Count"],
            capture_output=True, text=True, timeout=60, creationflags=SIN_VENTANA)
        cuantos = (sobran.stdout or "0").strip() or "0"
        self.anotar("no processes left after closing", cuantos == "0",
                    f"live processes: {cuantos}")

    def paso_7_mismos_veredictos(self, album: str) -> None:
        """The executable cannot analyse differently from the source code.

        It is the check that really closes out the packaging: same album,
        same arguments, and the records have to match field by field
        (verdict, score, MD5, DR, problems…).
        """
        print("\n[7] The executable gives the same verdicts as the source code")
        argumentos = ["--path", album, "--mode", "center", "--workers", "1",
                      "--no-cache"]
        _, texto_fuente = self.lanzar([sys.executable, "-u", MOTOR_FUENTE, *argumentos],
                                      etiqueta="source")
        _, texto_exe = self.lanzar([self.motor, *argumentos], etiqueta="frozen_exe")
        fuente = self.fichas(texto_fuente)
        ejecutable = self.fichas(texto_exe)
        if not fuente or not ejecutable:
            self.anotar("comparison against the source code", False,
                        f"records read: source={len(fuente)}, executable={len(ejecutable)}")
            return
        diferencias = [nombre for nombre, ficha in fuente.items()
                       if ficha != ejecutable.get(nombre)]
        detalle = (f"{len(fuente)} record(s) compared"
                   if not diferencias else
                   "differing: " + ", ".join(diferencias[:3]))
        self.anotar("same records field by field (verdict, MD5, DR…)", not diferencias,
                    detalle)
        for nombre in diferencias[:2]:
            print(f"     fuente     {nombre}: {self.abreviar(json.dumps(fuente[nombre], ensure_ascii=False), 200)}")
            print(f"     executable {nombre}: {self.abreviar(json.dumps(ejecutable.get(nombre), ensure_ascii=False), 200)}")

    def resumen(self) -> int:
        print("\n" + "=" * 72)
        fallos = [r for r in self.resultados if not r[1]]
        if fallos:
            print(f"RESULT: {len(fallos)} check(s) failed out of "
                  f"{len(self.resultados)}")
            for nombre, _, detalle in fallos:
                print(f"  ✗ {nombre}  {detalle}")
            return 1
        print(f"RESULT: all {len(self.resultados)} checks have passed")
        return 0


def main() -> int:
    _utf8()
    parser = argparse.ArgumentParser(description="Check the built .exe")
    parser.add_argument("--folder", dest="carpeta", default=DIST,
                        help="folder with the .exe files (default dist/FLAC_Verifier)")
    parser.add_argument("--album", default=ALBUM_PRUEBA,
                        help="album used for the analysis tests")
    parser.add_argument("--quick", dest="rapido", action="store_true",
                        help="only steps 1-3 (no PDF, no window)")
    args = parser.parse_args()

    print("=" * 72)
    print("EXECUTABLE CHECK")
    print("=" * 72)
    print(f"  executables: {args.carpeta}")
    print(f"  test album: {args.album}")

    comprobador = Comprobador(args.carpeta)
    comprobador.paso_1_ejecutables()
    if os.path.isdir(args.album):
        comprobador.paso_2_serie(args.album)
        comprobador.paso_3_paralelo(args.album)
        if not args.rapido:
            comprobador.paso_4_pdf(args.album)
            comprobador.paso_5_bandera_interna(args.album)
            comprobador.paso_7_mismos_veredictos(args.album)
    else:
        comprobador.anotar("test album", False,
                           f"not found: {args.album} — pass --album")
    if not args.rapido:
        comprobador.paso_6_ventana()
    return comprobador.resumen()


if __name__ == "__main__":
    raise SystemExit(main())
