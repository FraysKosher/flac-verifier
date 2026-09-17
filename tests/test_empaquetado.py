"""Empaquetado: despacho de `main.py`, comando del motor congelado y `.spec`.

Lo importante que se fija aquí:
  - `multiprocessing.freeze_support()` es la PRIMERA instrucción del bloque
    `__main__` de `main.py`. Si se mueve, los procesos del análisis paralelo
    vuelven a arrancar la aplicación entera (en el .exe de ventana: una ventana
    nueva por worker, en bucle).
  - La GUI sabe invocar al motor cuando está empaquetada (ejecutable aparte o la
    bandera interna `--motor-cli`), y en modo fuente sigue usando `-u` con el .py.
  - El `.spec` recolecta de verdad todo lo necesario y no depende de rutas
    absolutas.
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
        self.assertEqual(len(bloque), 2, "main.py no tiene bloque __main__")
        cuerpo = [linea.strip() for linea in bloque[1].splitlines()
                  if linea.strip() and not linea.strip().startswith("#")]
        self.assertTrue(cuerpo, "el bloque __main__ está vacío")
        self.assertEqual(cuerpo[0], "multiprocessing.freeze_support()",
                         "freeze_support() tiene que ser la primera instrucción: "
                         "si no, cada worker del análisis paralelo reabre la app")

    def test_multiprocessing_no_se_importa_dentro_de_una_funcion(self):
        """Importarlo tarde funcionaría, pero no puede condicionarse a los args."""
        fuente = open(os.path.join(RAIZ, "main.py"), encoding="utf-8").read()
        self.assertRegex(fuente, r"(?m)^import multiprocessing$")

    def test_version_y_ayuda(self):
        self.assertEqual(main.principal(["--version"]), 0)
        self.assertEqual(main.principal(["-V"]), 0)
        # Ojo: si `--help` dejara de reconocerse, esta llamada caería en la rama
        # de la interfaz gráfica y el test se quedaría colgado con una ventana
        # abierta (ya pasó con la bandera antigua).
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
        for argumentos, esperada in ((["--gui", "D:/Musica"], "D:/Musica"),
                                     (["-g", "D:/Musica"], "D:/Musica"),
                                     (["D:/Musica"], "D:/Musica")):
            with self.subTest(argumentos=argumentos):
                with mock.patch.object(gui, "main", return_value=0) as interfaz:
                    main.principal(argumentos)
                interfaz.assert_called_once_with(esperada)

    def test_si_falla_la_interfaz_avisa_sin_trazas(self):
        with mock.patch.dict(sys.modules, {"gui": None}):
            self.assertEqual(main.principal([]), 1)


class TestEjecutableDelMotor(unittest.TestCase):
    """`flac_motor.exe` es el motor: sus argumentos NO abren la ventana.

    Esto se escapó una vez y es de los fallos que peor se ven: sin esta regla,
    `flac_motor.exe --ruta …` abría la interfaz gráfica (una ventana que no
    termina nunca y no imprime nada), así que un script o una comprobación
    automática se quedaban esperando para siempre.
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
            self.assertEqual(main.principal(["--gui", "D:/Musica"]), 0)
        interfaz.assert_called_once_with("D:/Musica")

    def test_desde_el_codigo_fuente_no_cambia_nada(self):
        """Sin congelar no existe `flac_motor.exe`: `main.py` sigue abriendo la GUI."""
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
    """Cómo se construye el comando del motor según cómo se ejecute todo."""

    def test_en_modo_fuente_usa_el_interprete_con_u(self):
        with mock.patch.object(gui, "empaquetado", return_value=False):
            comando = gui.construir_comando("C:/Album")
        self.assertEqual(comando[0], sys.executable)
        self.assertEqual(comando[1], "-u")                 # tiempo real
        self.assertTrue(comando[2].endswith("motor_flac.py"))

    def test_empaquetado_prefiere_el_ejecutable_del_motor(self):
        # La disposición real (la que produce el .spec): los dos .exe en la
        # MISMA carpeta, dist/FLAC_Verifier/.
        carpeta = os.path.join(RAIZ, "dist", "FLAC_Verifier")
        falso_gui = os.path.join(carpeta, "FLAC_Verifier.exe")
        falso_motor = os.path.join(carpeta, "flac_motor.exe")
        with mock.patch.object(gui, "empaquetado", return_value=True), \
             mock.patch.object(gui.sys, "executable", falso_gui), \
             mock.patch.object(gui.os.path, "exists", lambda ruta: ruta == falso_motor):
            comando = gui.construir_comando("C:/Album")
        self.assertEqual(comando[0], falso_motor)
        self.assertNotIn("-u", comando)                    # el .exe ya va sin buffer
        # Empaquetado, la bandera interna va SIEMPRE: así la llamada no depende
        # de adivinar qué binario es (y el .exe de ventana no abre su ventana).
        self.assertIn("--motor-cli", comando)
        self.assertEqual(comando[1], "--motor-cli")

    def test_el_motor_se_busca_junto_a_la_gui(self):
        """Si el motor no está al lado, no se inventa una ruta: se usa el mismo .exe."""
        carpeta = os.path.join(RAIZ, "dist", "FLAC_Verifier")
        with mock.patch.object(gui, "empaquetado", return_value=True), \
             mock.patch.object(gui.sys, "executable",
                               os.path.join(carpeta, "FLAC_Verifier.exe")), \
             mock.patch.object(gui.os.path, "exists", return_value=False):
            self.assertEqual(gui.ruta_motor(),
                             [os.path.join(carpeta, "FLAC_Verifier.exe"), "--motor-cli"])

    def test_ningun_comando_empaquetado_depende_del_nombre_del_binario(self):
        """El motor nunca se invoca sin la bandera cuando está empaquetado.

        `flac_motor.exe` interpreta sus opciones como motor aunque no lleve la
        bandera, pero de eso no debe depender la GUI: si alguien renombra el
        ejecutable, la bandera sigue decidiendo.
        """
        carpeta = os.path.join(RAIZ, "dist", "FLAC_Verifier")
        casos = (
            ("con el motor al lado", lambda ruta: ruta.endswith(gui.NOMBRE_MOTOR_EXE)),
            ("sin el motor al lado", lambda ruta: False),
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
        """Es el caso de un único .exe: el motor vive en el mismo binario."""
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
        """Un .exe de ventana deja stdout=None y print() no escribiría nada."""
        import motor_flac
        original = sys.stdout
        try:
            sys.stdout = None
            motor_flac._asegurar_flujos()
            self.assertIsNotNone(sys.stdout)
            print("esto tiene que poder imprimirse")
        finally:
            sys.stdout = original


class TestEspecificacion(unittest.TestCase):
    """El .spec tiene que recolectar todo lo que el proyecto necesita."""

    @classmethod
    def setUpClass(cls):
        cls.fuente = open(SPEC, encoding="utf-8").read()

    def test_existe_y_es_ejecutable_como_python(self):
        compile(self.fuente, SPEC, "exec")      # sintaxis válida

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
        # El nombre del motor se declara una sola vez, en una constante, para
        # que el .spec y las pruebas no puedan desincronizarse.
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
        """UPX comprime más, pero multiplica los falsos positivos de antivirus."""
        self.assertIn("upx=False", self.fuente)
        self.assertNotIn("upx=True", self.fuente)

    def test_los_recursos_de_version_son_coherentes(self):
        # PyInstaller ejecuta este archivo con SUS clases (VSVersionInfo,
        # FixedFileInfo...) ya definidas; aquí se aportan dobles que aceptan
        # cualquier argumento para comprobar que el archivo es Python válido.
        def nodo(*args, **kwargs):
            return args, kwargs

        espacio = {nombre: nodo for nombre in
                   ("VSVersionInfo", "FixedFileInfo", "StringFileInfo",
                    "StringTable", "StringStruct", "VarFileInfo", "VarStruct")}
        exec(open(VERSION_INFO, encoding="utf-8").read(), espacio)   # pyinstaller lo ejecuta
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
    """El paso 7 del verificador: ejecutable y código fuente, mismas fichas.

    Empaquetar no puede cambiar el análisis. Esta es la comprobación que lo
    demuestra, y estas pruebas fijan que compara lo que tiene que comparar (y que
    no compara lo que cambia solo, como la ruta o la marca de caché).
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
                            ruta="C:/otra/ruta.flac", espectrograma="C:/x.png",
                            desde_cache=True, md5="abc123")
        ficha = self.ce.Comprobador.fichas(texto)["01.flac"]
        for volatil in ("ruta", "espectrograma", "desde_cache"):
            self.assertNotIn(volatil, ficha, f"{volatil} no debe compararse")
        # …pero el análisis en sí sí: es lo que tiene que coincidir.
        self.assertEqual(ficha["md5"], "abc123")
        self.assertEqual(ficha["veredicto"], "GENUINE LOSSLESS")

    def test_las_lineas_ilegibles_no_tumban_la_comparacion(self):
        texto = "\n".join(["{esto no es json", "", "   ",
                           self._linea(archivo="01.flac", veredicto="SUSPICIOUS")])
        self.assertEqual(list(self.ce.Comprobador.fichas(texto)), ["01.flac"])

    def test_un_archivo_corrupto_cuenta_igual_en_los_dos_lados(self):
        """El error de un FLAC ilegible también tiene que coincidir."""
        texto = json.dumps({"tipo": "resultado", "archivo": "06_corrupto.flac",
                            "error": "no se pudieron leer metadatos"}, ensure_ascii=False)
        ficha = self.ce.Comprobador.fichas(texto)["06_corrupto.flac"]
        self.assertIn("error", ficha)
        self.assertNotIn("veredicto", ficha)


class TestCacheDeMatplotlib(unittest.TestCase):
    """La caché de fuentes no puede ensuciar el protocolo que lee la GUI.

    Si matplotlib no puede escribir su caché, avisa por stderr; el motor le habla
    a la GUI por stdout con líneas JSON, así que ese aviso saldría en el registro
    de la ventana como una línea ilegible. Peor aún: reconstruiría la caché en
    cada ejecución (más lento en cada espectrograma).
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
        # Las comprobaciones van dentro del `with`: al salir, mock deshace el
        # diccionario de entorno y con él la variable que acaba de poner.
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": tempfile.gettempdir()}):
            elegida = self.motor.preparar_cache_matplotlib()
            self.assertTrue(elegida, "no se eligió ninguna carpeta")
            self.assertTrue(os.path.isdir(elegida))
            self.assertEqual(os.environ["MPLCONFIGDIR"], elegida)
            self.assertIn("FLAC_VERIFIER", elegida)
            shutil.rmtree(os.path.dirname(elegida), ignore_errors=True)

    def test_lo_que_configura_el_usuario_no_se_toca(self):
        with mock.patch.dict(os.environ, {"MPLCONFIGDIR": "C:/mia"}):
            self.assertIsNone(self.motor.preparar_cache_matplotlib())
            self.assertEqual(os.environ["MPLCONFIGDIR"], "C:/mia")

    def test_si_no_se_puede_crear_no_se_rompe_nada(self):
        """Sin permiso en ninguna candidata se deja el valor por defecto."""
        with mock.patch.dict(os.environ, {"LOCALAPPDATA": "C:/inventado"}), \
             mock.patch.object(self.motor.os, "makedirs", side_effect=PermissionError):
            self.assertIsNone(self.motor.preparar_cache_matplotlib())
        self.assertNotIn("MPLCONFIGDIR", os.environ)

    def test_se_decide_antes_de_importar_matplotlib(self):
        fuente = open(os.path.join(RAIZ, "motor_flac.py"), encoding="utf-8").read()
        llamada = fuente.index("preparar_cache_matplotlib()\n\ntry:")
        self.assertLess(llamada, fuente.index("import matplotlib"),
                        "la variable se lee al importar matplotlib: la llamada va antes")


if __name__ == "__main__":
    unittest.main(verbosity=2)
