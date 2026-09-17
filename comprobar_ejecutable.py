"""Comprueba el ejecutable ya construido (dist/FLAC_Verifier).

Se ejecuta en la máquina de compilación, después de `python build.py`:

    python comprobar_ejecutable.py                       (usa el álbum de pruebas)
    python comprobar_ejecutable.py --album "D:/Music/Album"
    python comprobar_ejecutable.py --quick               (se salta el PDF y el GUI)

Cada paso lanza el .exe con la salida REDIRIGIDA A UN ARCHIVO, nunca a una
tubería: los procesos hijos del análisis paralelo heredan los descriptores, y con
una tubería el lector se queda esperando un fin de flujo que no llega. Además cada
paso tiene su propio tiempo límite, así que un cuelgue se detecta y se reporta en
vez de quedarse ahí.

Lo que se comprueba, en orden:
  1. Los dos ejecutables existen y dicen su versión.
  2. El motor analiza en serie (camino sin multiprocessing), invocado como lo
     haría un usuario: `flac_motor.exe --ruta …`, sin banderas de más.
  3. El motor analiza EN PARALELO con 8 procesos: es el camino de
     multiprocessing.freeze_support(). Sin él, cada worker volvería a arrancar la
     aplicación y esto se colgaría.
  4. El motor genera espectrogramas PNG y el informe PDF (matplotlib y reportlab
     empaquetados).
  5. El comando EXACTO que arma la GUI empaquetada (se le pregunta a
     `gui.ruta_motor()`, no se copia a mano) y el respaldo `--motor-cli` sobre el
     ejecutable de ventana.
  6. La interfaz gráfica abre una ventana real y se cierra sin dejar procesos.
  7. El ejecutable da las MISMAS fichas que el código fuente sobre el mismo
     álbum, campo a campo: es lo que garantiza que empaquetar no cambió el
     análisis.
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
# El álbum de pruebas de la auditoría vive dos carpetas más arriba (junto al
# proyecto, no dentro de él).
ALBUM_PRUEBA = os.path.join(os.path.dirname(os.path.dirname(AQUI)),
                            "_audit_flac", "_demo_album")
SIN_VENTANA = getattr(subprocess, "CREATE_NO_WINDOW", 0)
LIMITE = 300


def _utf8() -> None:
    for nombre in ("stdout", "stderr"):
        try:
            # line_buffering: si la salida va a una tubería (por ejemplo un
            # script o una tarea en segundo plano) se ve el progreso paso a paso
            # en lugar de todo de golpe al terminar.
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

    # ── utilidades ──────────────────────────────────────────────────────────
    def anotar(self, nombre: str, bien: bool, detalle: str = "") -> None:
        self.resultados.append((nombre, bien, detalle))
        print(f"  {'OK  ' if bien else 'FALLA'} {nombre}"
              + (f"  ·  {detalle}" if detalle else ""))

    def lanzar(self, comando: list[str], limite: int = LIMITE,
               etiqueta: str = "output") -> tuple[int | None, str]:
        """Ejecuta con la salida a un archivo y devuelve (código, texto)."""
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
        """Los eventos `resultado` por archivo, sin los campos que cambian solos.

        Se quitan la ruta, el PNG y la marca de caché: lo demás (veredicto,
        puntuación, MD5, problemas, DR…) tiene que ser idéntico entre el código
        fuente y el ejecutable, porque es el análisis en sí.
        """
        volatiles = ("ruta", "espectrograma", "desde_cache")
        return {e["archivo"]: {k: v for k, v in e.items() if k not in volatiles}
                for e in Comprobador.eventos(texto)
                if e.get("tipo") == "resultado" and e.get("archivo")}

    @staticmethod
    def abreviar(texto: str, limite: int = 90) -> str:
        texto = " ".join(texto.split())
        return texto[:limite] + ("…" if len(texto) > limite else "")

    # ── pasos ───────────────────────────────────────────────────────────────
    def paso_1_ejecutables(self) -> None:
        print("\n[1] Both executables exist and respond")
        for etiqueta, ruta in (("interfaz", self.gui), ("motor", self.motor)):
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
                                    etiqueta="serie")
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
                                    etiqueta="paralelo")
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

        # (a) Lo que de verdad ejecuta la GUI cuando está empaquetada. Se finge
        # que este proceso es el .exe para que `ruta_motor()` decida con las
        # reglas reales, en lugar de copiar aquí el comando a mano (copiarlo es
        # justo lo que dejó pasar el fallo de que faltaba --motor-cli).
        try:
            import gui
        except Exception as e:                 # sin customtkinter en esta máquina
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
            codigo, texto = self.lanzar(comando, limite=180, etiqueta="gui_empaquetada")
            eventos = self.eventos(texto)
            resultados = [e for e in eventos if e.get("tipo") == "resultado"]
            self.anotar(f"command built by the packaged GUI: {' '.join(base)}",
                        codigo == 0 and bool(resultados),
                        f"{len(resultados)} result(s), exit code {codigo}")

        # (b) La bandera interna sobre el .exe de ventana: es el respaldo de un
        # único ejecutable. Funciona porque `_asegurar_flujos()` reconstruye
        # stdout, que en un .exe de ventana llega a None.
        codigo, texto = self.lanzar([self.gui, "--motor-cli", "--path", album,
                                     "--mode", "center", "--workers", "1",
                                     "--no-cache"], limite=180, etiqueta="bandera")
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
            for _ in range(40):                     # hasta 20 s
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
        """El ejecutable no puede analizar distinto que el código fuente.

        Es la comprobación que de verdad cierra el empaquetado: mismo álbum,
        mismos argumentos, y las fichas tienen que coincidir campo a campo
        (veredicto, puntuación, MD5, DR, problemas…).
        """
        print("\n[7] The executable gives the same verdicts as the source code")
        argumentos = ["--path", album, "--mode", "center", "--workers", "1",
                      "--no-cache"]
        _, texto_fuente = self.lanzar([sys.executable, "-u", MOTOR_FUENTE, *argumentos],
                                      etiqueta="fuente")
        _, texto_exe = self.lanzar([self.motor, *argumentos], etiqueta="congelado_exe")
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
                   "distintas: " + ", ".join(diferencias[:3]))
        self.anotar("same records field by field (verdict, MD5, DR…)", not diferencias,
                    detalle)
        for nombre in diferencias[:2]:
            print(f"     fuente     {nombre}: {self.abreviar(json.dumps(fuente[nombre], ensure_ascii=False), 200)}")
            print(f"     executable {nombre}: {self.abreviar(json.dumps(ejecutable.get(nombre), ensure_ascii=False), 200)}")

    def resumen(self) -> int:
        print("\n" + "=" * 72)
        fallos = [r for r in self.resultados if not r[1]]
        if fallos:
            print(f"RESULTADO: {len(fallos)} check(s) failed out of "
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
    print(f"  ejecutables: {args.carpeta}")
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
                           f"no existe {args.album}: use --album")
    if not args.rapido:
        comprobador.paso_6_ventana()
    return comprobador.resumen()


if __name__ == "__main__":
    raise SystemExit(main())
