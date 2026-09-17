"""GUI: parsing de eventos, formateo de estados y gestión de procesos.

Los tests NO levantan el bucle visual (`mainloop`): se prueba la lógica pura, el
ciclo de vida del subproceso con lanzadores falsos y, cuando hay pantalla, la
construcción de la ventana oculta y sus cambios de estado.

Para saltarse los tests que necesitan ventana (CI sin escritorio):
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
import time
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fixtures as fx

import gui
import customtkinter as ctk

DND_REAL = gui.DND_OK                       # estado real, para restaurarlo tras un reload


def _hay_pantalla():
    """¿Se puede crear una ventana Tk en este entorno?"""
    if os.environ.get("FLAC_VERIFIER_SIN_GUI"):
        return False
    try:
        import tkinter
        raiz = tkinter.Tk()
        raiz.withdraw()
        raiz.destroy()
        return True
    except Exception:
        return False


VISUAL = gui.CTK_OK and _hay_pantalla()


# ─── dobles de prueba ────────────────────────────────────────────────────────

class ProcesoFalso:
    """Imita lo justo de subprocess.Popen que usa el gestor."""

    def __init__(self, lineas=(), error=(), codigo=0, falla_terminate=False):
        self.stdout = io.StringIO("\n".join(lineas) + ("\n" if lineas else ""))
        self.stderr = io.StringIO("\n".join(error) + ("\n" if error else ""))
        self.pid = 4242
        self.returncode = None
        self._codigo = codigo
        self.falla_terminate = falla_terminate
        self.terminado = 0
        self.muerto = 0

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.returncode = self._codigo if self.returncode is None else self.returncode
        return self.returncode

    def terminate(self):
        self.terminado += 1
        if self.falla_terminate:
            raise OSError("no se pudo terminar")
        self.returncode = 1

    def kill(self):
        self.muerto += 1
        self.returncode = 1


class TestComando(unittest.TestCase):
    """El comando del subproceso es lo que hace que la GUI no se bloquee."""

    def test_lleva_u_para_no_almacenar_en_buffer(self):
        comando = gui.construir_comando("C:/Musica/Album")
        self.assertEqual(comando[0], sys.executable)
        self.assertEqual(comando[1], "-u")          # sin esto no hay tiempo real
        self.assertTrue(comando[2].endswith("motor_flac.py"))

    def test_opciones_por_defecto(self):
        comando = gui.construir_comando("C:/Album")
        self.assertIn("--mode", comando)
        self.assertEqual(comando[comando.index("--mode") + 1], "center")
        self.assertIn("--pdf", comando)
        self.assertNotIn("--workers", comando)      # 0 = lo elige el motor
        self.assertNotIn("--no-cache", comando)

    def test_modo_segundos_añade_el_valor(self):
        comando = gui.construir_comando("C:/Album", modo="seconds", segundos=20)
        self.assertEqual(comando[comando.index("--seconds") + 1], "20")

    def test_workers_y_cache(self):
        comando = gui.construir_comando("C:/Album", workers=4, sin_cache=True)
        self.assertEqual(comando[comando.index("--workers") + 1], "4")
        self.assertIn("--no-cache", comando)

    def test_sin_pdf_no_se_pide_informe(self):
        self.assertNotIn("--pdf", gui.construir_comando("C:/Album", pdf=False))


class TestParseo(unittest.TestCase):

    def test_evento_correcto(self):
        evento = gui.parsear_evento('{"tipo": "resultado", "archivo": "a.flac"}')
        self.assertEqual(evento, {"tipo": "resultado", "archivo": "a.flac"})

    def test_linea_vacia_devuelve_none(self):
        self.assertIsNone(gui.parsear_evento(""))
        self.assertIsNone(gui.parsear_evento("   \n"))

    def test_linea_ilegible_no_lanza(self):
        evento = gui.parsear_evento("esto no es json")
        self.assertEqual(evento["tipo"], "__ilegible__")
        self.assertIn("esto no es json", evento["mensaje"])

    def test_json_que_no_es_objeto(self):
        self.assertEqual(gui.parsear_evento("[1, 2, 3]")["tipo"], "__ilegible__")
        self.assertEqual(gui.parsear_evento('"hola"')["tipo"], "__ilegible__")

    def test_evento_sin_tipo(self):
        self.assertEqual(gui.parsear_evento('{"total": 3}')["tipo"], "__desconocido__")

    def test_los_acentos_llegan_bien(self):
        evento = gui.parsear_evento(json.dumps({"tipo": "aviso", "mensaje": "análisis"}))
        self.assertEqual(evento["mensaje"], "análisis")


class TestFormato(unittest.TestCase):

    def test_resultado_correcto(self):
        texto = gui.formatear_resultado({"archivo": "01 - Luminol.flac",
                                         "veredicto": "GENUINE LOSSLESS",
                                         "score": 1.0, "problemas": []})
        self.assertEqual(texto, "[OK] 01 - Luminol.flac: GENUINE LOSSLESS · "
                                "score 100 % · 0 issues")

    def test_resultado_con_un_solo_problema_no_pluraliza(self):
        texto = gui.formatear_resultado({"archivo": "a.flac", "veredicto": "SUSPICIOUS",
                                         "score": 0.5, "problemas": ["uno"]})
        self.assertIn("[SUSPICIOUS]", texto)
        self.assertTrue(texto.endswith("1 issue"), texto)
        self.assertNotIn("1 issues", texto)

    def test_resultado_desde_cache(self):
        texto = gui.formatear_resultado({"archivo": "a.flac", "veredicto": "SUSPICIOUS",
                                         "desde_cache": True})
        self.assertIn("cache", texto)

    def test_resultado_con_error(self):
        texto = gui.formatear_resultado({"archivo": "a.flac", "error": "firma inválida"})
        self.assertEqual(texto, "[ERROR] a.flac: firma inválida")

    def test_resumen_verde_solo_con_genuinos(self):
        color, texto = gui.formatear_resumen(
            [{"tipo": "resultado", "veredicto": "GENUINE LOSSLESS"}] * 3)
        self.assertEqual(color, "verde")
        self.assertIn("3 genuine", texto)

    def test_resumen_amarillo_con_probables(self):
        color, _ = gui.formatear_resumen([
            {"tipo": "resultado", "veredicto": "GENUINE LOSSLESS"},
            {"tipo": "resultado", "veredicto": "PROBABLY LOSSLESS"}])
        self.assertEqual(color, "amarillo")

    def test_resumen_naranja_con_dudosos(self):
        color, _ = gui.formatear_resumen([
            {"tipo": "resultado", "veredicto": "SUSPICIOUS"}])
        self.assertEqual(color, "naranja")

    def test_resumen_rojo_con_upscale_o_error(self):
        color, _ = gui.formatear_resumen([{"tipo": "resultado", "veredicto": "PROBABLE UPSCALE"}])
        self.assertEqual(color, "rojo")
        color, _ = gui.formatear_resumen([{"tipo": "resultado", "error": "x"}])
        self.assertEqual(color, "rojo")

    def test_resumen_ignora_eventos_que_no_son_resultado(self):
        color, texto = gui.formatear_resumen([{"tipo": "inicio", "total": 5}])
        self.assertEqual(color, "gris")
        self.assertEqual(texto, "No results")


class TestArrastrarYSoltar(unittest.TestCase):

    def test_rutas_con_espacios_entre_llaves(self):
        self.assertEqual(gui.rutas_de_dnd("{C:/mi musica/Album 1} D:/otra/ruta"),
                         ["C:/mi musica/Album 1", "D:/otra/ruta"])

    def test_ruta_simple(self):
        self.assertEqual(gui.rutas_de_dnd("C:/Album"), ["C:/Album"])

    def test_vacio(self):
        self.assertEqual(gui.rutas_de_dnd(""), [])

    def test_sin_tkinterdnd2_la_clase_base_no_lo_mezcla(self):
        """Sin la librería, la ventana hereda de ctk.CTk sin más y no falla."""
        if DND_REAL:
            self.skipTest("tkinterdnd2 está instalado")
        self.assertFalse(gui.DND_OK)
        self.assertTrue(gui.CTK_OK)
        # el requisito real: no se puede romper nada por que falte
        self.assertTrue(issubclass(gui.VentanaFlacVerifier, ctk.CTk))

    def test_con_tkinterdnd2_simulado_se_mezcla_el_envoltorio(self):
        """El camino opcional tiene que funcionar sin la librería instalada."""
        envoltorio = type("DnDWrapper", (), {})
        raiz_dnd = type("TkinterDnD", (), {"DnDWrapper": envoltorio,
                                           "_require": staticmethod(lambda ventana: "2.8")})
        falso = types.ModuleType("tkinterdnd2")
        falso.DND_FILES = "DND_Files"
        falso.TkinterDnD = raiz_dnd
        try:
            with mock.patch.dict(sys.modules, {"tkinterdnd2": falso}):
                importlib.reload(gui)
                self.assertTrue(gui.DND_OK)
                self.assertTrue(issubclass(gui._BaseVentana, envoltorio),
                                "no se mezcló el envoltorio de tkinterdnd2")
                self.assertTrue(issubclass(gui.VentanaFlacVerifier, envoltorio))
        finally:
            importlib.reload(gui)           # se restaura el módulo de verdad
        self.assertEqual(gui.DND_OK, DND_REAL, "el módulo no se restauró")


class TestAbrirArchivo(unittest.TestCase):

    def test_windows_usa_startfile(self):
        falso_os = mock.MagicMock()
        falso_os.name = "nt"
        with mock.patch.object(gui, "os", falso_os):
            self.assertTrue(gui.abrir_archivo("C:/a.pdf"))
        falso_os.startfile.assert_called_once_with("C:/a.pdf")

    def test_unix_usa_el_visor_del_sistema(self):
        falso_os = mock.MagicMock()
        falso_os.name = "posix"
        falso_sys = mock.MagicMock()
        falso_sys.platform = "linux"
        with mock.patch.object(gui, "os", falso_os), \
             mock.patch.object(gui, "sys", falso_sys), \
             mock.patch.object(gui.subprocess, "Popen") as popen:
            self.assertTrue(gui.abrir_archivo("/tmp/a.pdf"))
        popen.assert_called_once_with(["xdg-open", "/tmp/a.pdf"])

    def test_mac_usa_open(self):
        falso_os = mock.MagicMock()
        falso_os.name = "posix"
        falso_sys = mock.MagicMock()
        falso_sys.platform = "darwin"
        with mock.patch.object(gui, "os", falso_os), \
             mock.patch.object(gui, "sys", falso_sys), \
             mock.patch.object(gui.subprocess, "Popen") as popen:
            self.assertTrue(gui.abrir_archivo("/tmp/a.pdf"))
        popen.assert_called_once_with(["open", "/tmp/a.pdf"])

    def test_un_fallo_no_lanza(self):
        falso_os = mock.MagicMock()
        falso_os.name = "nt"
        falso_os.startfile.side_effect = OSError("sin visor")
        with mock.patch.object(gui, "os", falso_os):
            self.assertFalse(gui.abrir_archivo("C:/a.pdf"))


class TestGestorProceso(unittest.TestCase):
    """Arranque, lectura y cancelación, con un lanzador falso."""

    def _gestor(self, proceso, llamadas=None):
        def lanzador(comando, **kwargs):
            if llamadas is not None:
                llamadas.append((comando, kwargs))
            return proceso
        return gui.GestorProceso(queue.Queue(), lanzador=lanzador), llamadas

    def test_arranca_con_las_opciones_criticas_de_windows(self):
        proceso = ProcesoFalso(['{"tipo": "inicio", "total": 1}'])
        llamadas = []
        gestor, _ = self._gestor(proceso, llamadas)
        gestor.iniciar("C:/Album", modo="center", pdf=True)
        self.assertEqual(len(llamadas), 1)
        comando, opciones = llamadas[0]
        self.assertEqual(comando[1], "-u")
        self.assertEqual(opciones["stdout"], subprocess.PIPE)
        self.assertEqual(opciones["stderr"], subprocess.PIPE)
        self.assertTrue(opciones["text"])
        self.assertEqual(opciones["encoding"], "utf-8")
        self.assertEqual(opciones["bufsize"], 1)
        self.assertIn("creationflags", opciones)
        if os.name == "nt":
            self.assertEqual(opciones["creationflags"],
                             getattr(subprocess, "CREATE_NO_WINDOW", 0))

    def test_los_eventos_llegan_a_la_cola_y_termina_con_fin(self):
        proceso = ProcesoFalso(['{"tipo": "inicio", "total": 1}',
                                '{"tipo": "resultado", "archivo": "a.flac", "veredicto": "DUDOSO"}',
                                '{"tipo": "fin"}'])
        gestor, _ = self._gestor(proceso)
        gestor.iniciar("C:/Album")
        gestor.esperar(tiempo=5)
        recibidos = []
        while not gestor.cola.empty():
            recibidos.append(gestor.cola.get_nowait())
        tipos = [evento.get("tipo") for _, evento in recibidos]
        self.assertEqual(tipos, ["inicio", "resultado", "fin", "__fin__"])
        self.assertEqual(recibidos[-1][0], "fin")
        self.assertEqual(recibidos[-1][1]["codigo"], 0)

    def test_stderr_va_a_la_cola_como_aviso(self):
        proceso = ProcesoFalso(['{"tipo": "fin"}'], error=["[clipping] algo raro"])
        gestor, _ = self._gestor(proceso)
        gestor.iniciar("C:/Album")
        gestor.esperar(tiempo=5)
        canales = []
        while not gestor.cola.empty():
            canales.append(gestor.cola.get_nowait()[0])
        self.assertIn("stderr", canales)

    def test_no_deja_arrancar_dos_veces(self):
        proceso = ProcesoFalso(['{"tipo": "fin"}'])
        gestor, _ = self._gestor(proceso)
        gestor.iniciar("C:/Album")
        with self.assertRaises(RuntimeError):
            gestor.iniciar("C:/Album")

    def test_cancelar_termina_el_proceso(self):
        proceso = ProcesoFalso(['{"tipo": "inicio", "total": 9}'])
        gestor, _ = self._gestor(proceso)
        gestor.iniciar("C:/Album")
        self.assertTrue(gestor.activo)
        gestor.cancelar()
        self.assertTrue(gestor.cancelado)
        self.assertGreaterEqual(proceso.terminado, 1)
        self.assertFalse(gestor.activo)

    def test_si_terminate_falla_se_usa_kill(self):
        proceso = ProcesoFalso(['{"tipo": "fin"}'], falla_terminate=True)
        gestor, _ = self._gestor(proceso)
        gestor.iniciar("C:/Album")
        gestor.cancelar()
        self.assertGreaterEqual(proceso.terminado, 1)
        self.assertGreaterEqual(proceso.muerto, 1)

    def test_terminar_proceso_mata_el_arbol_en_windows(self):
        """El motor reparte trabajo entre procesos hijos: hay que matarlos todos."""
        proceso = ProcesoFalso([])
        with mock.patch.object(gui.subprocess, "run") as correr, \
             mock.patch.object(gui.os, "name", "nt"):
            gui.terminar_proceso(proceso)
        self.assertTrue(correr.called)
        argumentos = correr.call_args[0][0]
        self.assertEqual(argumentos[:3], ["taskkill", "/F", "/T"])
        self.assertEqual(argumentos[3], "/PID")
        self.assertEqual(argumentos[4], str(proceso.pid))

    def test_terminar_un_proceso_ya_muerto_no_hace_nada(self):
        proceso = ProcesoFalso([])
        proceso.returncode = 0
        with mock.patch.object(gui.subprocess, "run") as correr:
            gui.terminar_proceso(proceso)
        self.assertFalse(correr.called)
        self.assertEqual(proceso.terminado, 0)

    def test_terminar_none_no_lanza(self):
        gui.terminar_proceso(None)


class TestProcesoReal(unittest.TestCase):
    """Subproceso de verdad: comprueba el tiempo real y la cancelación."""

    def setUp(self):
        self.carpeta = tempfile.mkdtemp(prefix="gui_motor_")
        self.motor = os.path.join(self.carpeta, "motor_falso.py")
        with open(self.motor, "w", encoding="utf-8") as f:
            f.write(
                "import sys, time\n"
                "print('{\"tipo\": \"inicio\", \"total\": 2}', flush=True)\n"
                "time.sleep(0.2)\n"
                "print('{\"tipo\": \"resultado\", \"archivo\": \"a.flac\", "
                "\"veredicto\": \"LOSSLESS GENUINO\"}', flush=True)\n"
                "time.sleep(0.2)\n"
                "print('{\"tipo\": \"fin\"}', flush=True)\n")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.carpeta, ignore_errors=True)

    def test_los_eventos_llegan_antes_de_que_el_proceso_termine(self):
        gestor = gui.GestorProceso(queue.Queue(), motor=self.motor)
        inicio = time.perf_counter()
        gestor.iniciar("C:/Album", pdf=False)
        _, primero = gestor.cola.get(timeout=30)
        momento_primero = time.perf_counter() - inicio
        self.assertEqual(primero["tipo"], "inicio")
        self.assertLess(momento_primero, 1.5,
                        "el primer evento llegó tarde: ¿falta el flag -u?")
        gestor.esperar(tiempo=30)
        tipos = []
        while not gestor.cola.empty():
            tipos.append(gestor.cola.get_nowait()[1]["tipo"])
        self.assertIn("resultado", tipos)
        self.assertEqual(tipos[-1], "__fin__")

    def test_cancelar_un_proceso_real_lo_mata_y_no_queda_vivo(self):
        lento = os.path.join(self.carpeta, "motor_lento.py")
        with open(lento, "w", encoding="utf-8") as f:
            f.write("import time\nprint('{\"tipo\": \"inicio\", \"total\": 1}', flush=True)\n"
                    "time.sleep(60)\n")
        gestor = gui.GestorProceso(queue.Queue(), motor=lento)
        gestor.iniciar("C:/Album", pdf=False)
        self.assertTrue(gestor.activo)
        gestor.cancelar()
        gestor.esperar(tiempo=30)
        self.assertIsNotNone(gestor.proceso.poll(), "el proceso sigue vivo")
        self.assertFalse(gestor.activo)


@unittest.skipUnless(VISUAL, "hace falta customtkinter y una pantalla")
class TestVentana(unittest.TestCase):
    """La ventana se construye oculta: nada de mainloop."""

    @classmethod
    def setUpClass(cls):
        cls.fx = fx.Fixtures()

    @classmethod
    def tearDownClass(cls):
        cls.fx.cerrar()

    def setUp(self):
        self.ventana = gui.VentanaFlacVerifier()
        self.ventana.withdraw()
        self.ventana.update_idletasks()
        # los diálogos nativos bloquearían el test
        parche = mock.patch("tkinter.messagebox.showerror"), \
                 mock.patch("tkinter.messagebox.showwarning")
        self.parches = parche
        for p in parche:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self.parches])
        self.addCleanup(self.ventana.destroy)

    def test_arranca_en_espera_con_los_botones_correctos(self):
        self.assertEqual(self.ventana.btn_analizar.cget("state"), "normal")
        self.assertEqual(self.ventana.btn_cancelar.cget("state"), "disabled")
        self.assertEqual(self.ventana.btn_abrir_pdf.cget("state"), "disabled")
        self.assertEqual(self.ventana.barra.get(), 0)

    def test_los_controles_por_defecto(self):
        self.assertEqual(self.ventana.var_modo.get(), "center")
        self.assertTrue(self.ventana.var_pdf.get())
        self.assertTrue(self.ventana.var_paralelo.get())
        self.assertFalse(self.ventana.var_sin_cache.get())
        self.assertEqual(self.ventana._workers(), 0)        # automático

    def test_el_slider_de_segundos_refleja_su_valor(self):
        """El control ya no se deshabilita: aparece y desaparece del layout.
        Lo que sí se comprueba aquí es que sigue funcionando al mostrarlo."""
        self.ventana.var_modo.set("seconds")
        self.ventana._cambiar_modo()
        self.ventana._cambiar_segundos(30)
        self.assertIn("30", self.ventana.lbl_segundos.cget("text"))
        self.assertGreaterEqual(self.ventana.slider_segundos.get(), 5)
        self.assertLessEqual(self.ventana.slider_segundos.get(), 120)

    def test_apagar_el_paralelo_fuerza_un_proceso(self):
        self.ventana.opcion_workers.set("8")
        self.assertEqual(self.ventana._workers(), 8)
        self.ventana.var_paralelo.set(False)
        self.ventana._cambiar_paralelo()
        self.assertEqual(self.ventana._workers(), 1)
        self.assertEqual(self.ventana.opcion_workers.cget("state"), "disabled")

    def test_los_botones_siguen_el_ciclo_de_vida(self):
        self.ventana.gestor.proceso = ProcesoFalso(['{"tipo": "fin"}'])   # en curso
        self.ventana._actualizar_estado()
        self.assertEqual(self.ventana.btn_analizar.cget("state"), "disabled")
        self.assertEqual(self.ventana.btn_cancelar.cget("state"), "normal")
        self.assertEqual(self.ventana.entrada.cget("state"), "disabled")

        self.ventana.gestor.proceso.returncode = 0                        # terminado
        self.ventana._actualizar_estado()
        self.assertEqual(self.ventana.btn_analizar.cget("state"), "normal")
        self.assertEqual(self.ventana.btn_cancelar.cget("state"), "disabled")
        self.assertEqual(self.ventana.entrada.cget("state"), "normal")

    def test_procesar_eventos_actualiza_progreso_log_y_tarjeta(self):
        self.ventana._procesar("stdout", {"tipo": "inicio", "total": 2, "workers": 2})
        self.assertEqual(self.ventana.total, 2)
        self.ventana._procesar("stdout", {"tipo": "resultado", "indice": 1, "total": 2,
                                          "archivo": "a.flac",
                                          "veredicto": "GENUINE LOSSLESS",
                                          "score": 1.0, "problemas": []})
        self.assertEqual(self.ventana.recibidos, 1)
        self.assertAlmostEqual(self.ventana.barra.get(), 0.5, places=6)
        self.assertIn("1 of 2", self.ventana.lbl_progreso.cget("text"))
        self.ventana._procesar("stdout", {"tipo": "resultado", "indice": 2, "total": 2,
                                          "archivo": "b.flac",
                                          "veredicto": "PROBABLE UPSCALE",
                                          "score": 0.2, "problemas": ["x"]})
        self.assertAlmostEqual(self.ventana.barra.get(), 1.0, places=6)
        self.ventana._procesar("fin", {"tipo": "__fin__", "codigo": 0})
        self.assertIn("2 file(s)", self.ventana.lbl_resumen.cget("text"))
        self.assertEqual(self.ventana.lbl_estado.cget("text"), "Finished")
        registro = self.ventana.log.get("1.0", "end")
        self.assertIn("GENUINE LOSSLESS", registro)
        self.assertIn("PROBABLE UPSCALE", registro)

    def test_el_evento_informe_habilita_el_boton_del_pdf(self):
        with tempfile.TemporaryDirectory() as carpeta:
            ruta = os.path.join(carpeta, gui.NOMBRE_INFORME)
            with open(ruta, "wb") as f:
                f.write(b"%PDF-1.4\n")
            self.ventana._procesar("stdout", {"tipo": "informe", "ruta": ruta,
                                              "paginas": 3, "total": 1})
            self.assertEqual(self.ventana.ruta_pdf, ruta)
            self.assertEqual(self.ventana.btn_abrir_pdf.cget("state"), "normal")
            os.remove(ruta)
            self.ventana._actualizar_estado()
            self.assertEqual(self.ventana.btn_abrir_pdf.cget("state"), "disabled")

    def test_un_aviso_y_un_error_van_al_log_sin_cerrar_nada(self):
        self.ventana._procesar("stdout", {"tipo": "aviso", "mensaje": "sin informe"})
        self.ventana._procesar("stdout", {"tipo": "error", "mensaje": "ruta no válida"})
        self.ventana._procesar("stdout", {"tipo": "__ilegible__", "mensaje": "basura"})
        self.ventana._procesar("stderr", {"tipo": "__stderr__", "mensaje": "diagnóstico"})
        registro = self.ventana.log.get("1.0", "end")
        for esperado in ("sin informe", "ruta no válida", "basura", "diagnóstico"):
            self.assertIn(esperado, registro)
        self.assertTrue(self.ventana.winfo_exists())        # sigue en pie

    def test_cancelar_sin_analisis_no_hace_nada(self):
        self.ventana._cancelar()                            # no debe lanzar
        self.assertEqual(self.ventana.btn_analizar.cget("state"), "normal")

    def test_analizar_sin_ruta_avisa_y_no_arranca(self):
        self.ventana.var_ruta.set("")
        self.ventana._analizar()
        self.assertFalse(self.ventana.gestor.activo)
        self.assertIn("Path missing", self.ventana.log.get("1.0", "end"))

    def test_analizar_con_ruta_inexistente_no_arranca(self):
        self.ventana.var_ruta.set("C:/no/existe/esta/ruta")
        self.ventana._analizar()
        self.assertFalse(self.ventana.gestor.activo)

    def test_al_soltar_una_ruta_se_rellena_el_campo(self):
        evento = types.SimpleNamespace(data="{C:/Musica/Mi Album}")
        self.ventana._al_soltar(evento)
        self.assertEqual(self.ventana.var_ruta.get(), "C:/Musica/Mi Album")

    def test_flujo_completo_sin_mainloop(self):
        """Arranca el motor de verdad: progreso, log, tarjeta y PDF."""
        album = self.fx.subcarpeta("gui_flujo", ("ok16", "corte_16k"))
        self.ventana.var_ruta.set(album)
        self.ventana.var_pdf.set(True)
        self.ventana.var_paralelo.set(False)          # un proceso: arranque más corto
        self.ventana.var_sin_cache.set(True)
        self.ventana._analizar()
        self.assertTrue(self.ventana.gestor.activo)
        # los widgets siguen el ciclo de vida mientras el motor trabaja
        self.assertEqual(self.ventana.btn_analizar.cget("state"), "disabled")
        self.assertEqual(self.ventana.btn_cancelar.cget("state"), "normal")

        limite = time.time() + 180
        while time.time() < limite:
            self.ventana.update()
            # se bombea el bucle de eventos como haría la aplicación real hasta
            # que el estado final esté pintado
            if (not self.ventana.gestor.activo and self.ventana.cola.empty()
                    and "Analizando" not in self.ventana.lbl_estado.cget("text")):
                break
            time.sleep(0.05)
        self.ventana.update()
        self.assertFalse(self.ventana.gestor.activo, "el análisis no terminó")

        resultados = [e for e in self.ventana.eventos if e.get("tipo") == "resultado"]
        self.assertEqual(len(resultados), 2)
        self.assertAlmostEqual(self.ventana.barra.get(), 1.0, places=3)
        self.assertEqual(self.ventana.lbl_estado.cget("text"), "Finished")
        self.assertIsNotNone(self.ventana.ruta_pdf, "no llegó el evento informe")
        self.assertTrue(os.path.exists(self.ventana.ruta_pdf))
        self.assertEqual(self.ventana.btn_abrir_pdf.cget("state"), "normal")
        self.assertEqual(self.ventana.btn_analizar.cget("state"), "normal")

        registro = self.ventana.log.get("1.0", "end")
        self.assertIn("GENUINE LOSSLESS", registro)      # ok16
        self.assertIn("PROBABLE UPSCALE", registro)      # corte_16k
        self.assertIn("PDF report:", registro)
        self.assertIn("2 file(s)", self.ventana.lbl_resumen.cget("text"))


class TestIntegracionConElCLI(unittest.TestCase):
    """`verificar_flac.py --gui` tiene que abrir la interfaz, no el menú."""

    def test_el_flag_gui_llama_a_la_interfaz(self):
        import verificar_flac
        with mock.patch.object(gui, "main", return_value=0) as principal:
            codigo = verificar_flac.main(["--gui", "C:/Musica/Album"])
        self.assertEqual(codigo, 0)
        principal.assert_called_once_with("C:/Musica/Album")

    def test_el_flag_corto_tambien(self):
        import verificar_flac
        with mock.patch.object(gui, "main", return_value=0) as principal:
            verificar_flac.main(["-g"])
        principal.assert_called_once_with(None)

    def test_si_falta_customtkinter_se_avisa_sin_romper(self):
        import verificar_flac
        with mock.patch.dict(sys.modules, {"gui": None}):
            codigo = verificar_flac.main(["--gui"])
        self.assertEqual(codigo, 1)


class TestLogo(unittest.TestCase):
    """Los assets de la identidad visual y su uso."""

    RUTAS = {
        "cuadrado": os.path.join(gui.LOGO_CARPETA, "logo_flac_verifier.png"),
        "transparente": os.path.join(gui.LOGO_CARPETA,
                                     "logo_flac_verifier_transparente.png"),
        "pequeno": os.path.join(gui.LOGO_CARPETA, "logo_flac_verifier_128.png"),
        "icono": gui.LOGO_ICONO,
        "svg": os.path.join(gui.LOGO_CARPETA, "logo_flac_verifier.svg"),
        "generador": os.path.join(gui.LOGO_CARPETA, "generar_logo.py"),
    }
    ALTURAS_SPLASH = (28, 44, 60, 36, 52)     # de splashscreen.html

    def test_los_assets_existen(self):
        for nombre, ruta in self.RUTAS.items():
            with self.subTest(asset=nombre):
                self.assertTrue(os.path.exists(ruta), f"falta {ruta}")

    def test_el_png_es_cuadrado_y_tiene_alfa(self):
        from PIL import Image
        with Image.open(self.RUTAS["cuadrado"]) as imagen:
            self.assertEqual(imagen.size, (1024, 1024))
            self.assertIn("A", imagen.mode)
        with Image.open(self.RUTAS["transparente"]) as imagen:
            self.assertEqual(imagen.size, (1024, 1024))
        with Image.open(self.RUTAS["pequeno"]) as imagen:
            self.assertEqual(imagen.size, (128, 128))

    def test_el_icono_es_multirresolucion(self):
        from PIL import Image
        with Image.open(self.RUTAS["icono"]) as icono:
            tamanos = sorted(icono.info["sizes"])
        self.assertGreaterEqual(len(tamanos), 6)
        self.assertIn((16, 16), tamanos)          # barra de tareas
        self.assertIn((256, 256), tamanos)        # .exe de alta densidad

    def test_el_dibujo_respeta_las_proporciones_del_splash(self):
        """Las cinco barras y sus alturas salen de splashscreen.html."""
        from PIL import Image
        with Image.open(self.RUTAS["cuadrado"]) as imagen:
            lienzo = imagen.convert("RGBA")
            ancho, alto = lienzo.size
            blanco = (255, 255, 255, 255)
            columnas = []
            for x in range(ancho):
                ys = [y for y in range(alto) if lienzo.getpixel((x, y)) == blanco]
                columnas.append((min(ys), max(ys)) if ys else None)

        grupos, actual = [], None
        for x, dato in enumerate(columnas):
            if dato is None:
                actual = None
                continue
            if actual is None:
                actual = [dato[0], dato[1]]
                grupos.append(actual)
            else:
                actual[0] = min(actual[0], dato[0])
                actual[1] = max(actual[1], dato[1])

        self.assertEqual(len(grupos), len(self.ALTURAS_SPLASH))
        alturas = [fin - inicio + 1 for inicio, fin in grupos]
        referencia = max(self.ALTURAS_SPLASH)
        for medida, esperada in zip(alturas, self.ALTURAS_SPLASH):
            with self.subTest(barra=esperada):
                self.assertAlmostEqual(medida / max(alturas), esperada / referencia,
                                       delta=0.02)

    def test_el_check_es_teal_y_esta_abajo_a_la_derecha(self):
        from PIL import Image
        teal = (0x4F, 0x98, 0xA3, 255)
        with Image.open(self.RUTAS["cuadrado"]) as imagen:
            lienzo = imagen.convert("RGBA")
            ancho, alto = lienzo.size
            pixeles = [(x, y) for x in range(0, ancho, 2) for y in range(0, alto, 2)
                       if lienzo.getpixel((x, y)) == teal]
        self.assertTrue(pixeles, "no hay check teal")
        self.assertGreater(min(x for x, _ in pixeles), ancho * 0.5)
        self.assertGreater(min(y for _, y in pixeles), alto * 0.5)

    def test_el_svg_lleva_el_mismo_diseno(self):
        svg = open(self.RUTAS["svg"], encoding="utf-8").read()
        self.assertEqual(svg.count("<rect"), 1 + len(self.ALTURAS_SPLASH))  # fondo + barras
        self.assertEqual(svg.count("<polyline"), 1)
        for color in ("#0f0f0d", "#ffffff", "#4f98a3"):
            with self.subTest(color=color):
                self.assertIn(color, svg)

    def test_el_readme_usa_un_logo_que_existe(self):
        import re
        readme = open(os.path.join(gui.DIRECTORIO, "README.md"), encoding="utf-8").read()
        referencias = re.findall(r"Logo/[\w.\-]+", readme)
        self.assertTrue(referencias, "el README no muestra ningún logo")
        for referencia in set(referencias):
            with self.subTest(archivo=referencia):
                self.assertTrue(os.path.exists(os.path.join(gui.DIRECTORIO, referencia)))

    def test_el_readme_documenta_los_assets(self):
        readme = open(os.path.join(gui.DIRECTORIO, "README.md"), encoding="utf-8").read()
        for nombre in ("logo_flac_verifier.png", "icono_app.ico",
                       "logo_flac_verifier.svg", "generar_logo.py"):
            with self.subTest(asset=nombre):
                self.assertIn(nombre, readme)

    def test_sin_assets_la_ventana_funciona_y_avisa(self):
        """Si alguien borra la carpeta Logo, la aplicación no puede romperse."""
        if not VISUAL:
            self.skipTest("hace falta customtkinter y una pantalla")
        inexistente = os.path.join(gui.LOGO_CARPETA, "no_existe.png")
        with mock.patch.object(gui, "LOGO_MARCA", inexistente), \
             mock.patch.object(gui, "LOGO_ICONO", inexistente), \
             mock.patch.object(gui, "LOGO_ICONPHOTO", inexistente):
            ventana = gui.VentanaFlacVerifier()
            try:
                ventana.withdraw()
                ventana.update_idletasks()
                self.assertFalse(ventana.icono_cargado)
                self.assertIsNone(ventana._imagen_marca(46))
                self.assertTrue(ventana.winfo_exists())
                self.assertIn("Project icon not found", ventana.log.get("1.0", "end"))
            finally:
                ventana.destroy()


@unittest.skipUnless(VISUAL, "hace falta customtkinter y una pantalla")
class TestControlDeSegundos(unittest.TestCase):
    """El control de segundos solo existe cuando el modo lo necesita.

    Se quita del grid en lugar de deshabilitarse, así que libera su espacio: los
    controles de abajo suben y no queda ningún hueco fantasma.
    """

    def setUp(self):
        self.ventana = gui.VentanaFlacVerifier()
        self.ventana.withdraw()
        self.ventana.update_idletasks()
        self.addCleanup(self.ventana.destroy)

    def _poner_modo(self, modo):
        self.ventana.var_modo.set(modo)
        self.ventana._cambiar_modo()
        self.ventana.update_idletasks()

    def _posiciones(self):
        return {nombre: getattr(self.ventana, nombre).winfo_y()
                for nombre in ("check_pdf", "switch_paralelo", "check_cache",
                               "btn_analizar", "btn_cancelar")}

    def test_en_centro_no_se_muestra(self):
        self._poner_modo("center")
        self.assertEqual(self.ventana.fila_segundos.grid_info(), {},
                         "el control de segundos sigue ocupando sitio")
        self.assertFalse(self.ventana.fila_segundos.winfo_manager())

    def test_en_completo_tampoco(self):
        self._poner_modo("full")
        self.assertEqual(self.ventana.fila_segundos.grid_info(), {})

    def test_en_segundos_si_se_muestra(self):
        self._poner_modo("seconds")
        info = self.ventana.fila_segundos.grid_info()
        self.assertTrue(info, "el control de segundos no aparece")
        self.assertEqual(int(info["row"]), 6)
        self.assertEqual(self.ventana.slider_segundos.cget("state"), "normal")

    def test_el_widget_sigue_existiendo_cuando_esta_oculto(self):
        self._poner_modo("center")
        self.assertTrue(self.ventana.lbl_segundos.winfo_exists())
        self.assertTrue(self.ventana.slider_segundos.winfo_exists())
        self.ventana._cambiar_segundos(45)
        self.assertIn("45", self.ventana.lbl_segundos.cget("text"))

    def test_al_mostrarlo_los_controles_de_abajo_bajan(self):
        self._poner_modo("center")
        sin_segundos = self._posiciones()
        self._poner_modo("seconds")
        con_segundos = self._posiciones()
        for nombre, y in sin_segundos.items():
            with self.subTest(control=nombre):
                self.assertLess(y, con_segundos[nombre],
                                f"{nombre} no bajó al aparecer el control de segundos")

    def test_al_ocultarlo_los_controles_vuelven_a_su_sitio(self):
        """Ida y vuelta sin deriva: ni huecos fantasma ni desplazamientos."""
        self._poner_modo("center")
        referencia = self._posiciones()
        self._poner_modo("seconds")
        self._poner_modo("full")
        self.assertEqual(self._posiciones(), referencia)

    def test_oculto_y_mostrado_varias_veces_no_acumula_hueco(self):
        self._poner_modo("center")
        referencia = self._posiciones()
        for _ in range(3):
            self._poner_modo("seconds")
            self._poner_modo("center")
        self.assertEqual(self._posiciones(), referencia)


if __name__ == "__main__":
    unittest.main(verbosity=2)
