"""FLAC VERIFIER — interfaz de línea de comandos.

Esta es una CAPA DE PRESENTACIÓN: aquí no hay lógica de análisis. Todo el motor
(firma, metadatos, MD5, clipping, rango dinámico, bit-depth, espectro y scoring)
vive en motor_flac.py. Este archivo solo llama al motor, formatea el informe y
gestiona el menú interactivo.

Uso:
    python verificar_flac.py          # menú interactivo
    python motor_flac.py --ruta ...   # motor crudo, protocolo JSON por línea
"""
import os
import sys
from collections import Counter

# Permite ejecutar el script desde cualquier directorio de trabajo.
_DIRECTORIO = os.path.dirname(os.path.abspath(__file__))
if _DIRECTORIO not in sys.path:
    sys.path.insert(0, _DIRECTORIO)

import motor_flac as motor

VERSION = "v4.0"

SALIR_CMDS = {"exit", "q", "quit", "0"}
AFIRMATIVOS = ("y", "yes")

# Veredictos canónicos del motor -> presentación.
# Se comparan por IGUALDAD EXACTA contra motor_flac.calcular_score, así que ya no
# hay comparaciones por subcadena ni dos juegos de cadenas que puedan divergir.
VEREDICTOS = {
    "GENUINE LOSSLESS":       ("✅", "GENUINE LOSSLESS"),
    "PROBABLY LOSSLESS": ("⚠️ ", "PROBABLY LOSSLESS"),
    "SUSPICIOUS":                 ("🔶", "SUSPICIOUS — possible upscale"),
    "PROBABLE UPSCALE":       ("❌", "PROBABLE UPSCALE / TRANSCODE"),
    "indeterminate":          ("❓", "indeterminate"),
}

# ─── PRESENTACIÓN DEL INFORME ────────────────────────────────────────────────

def etiqueta_veredicto(veredicto):
    icono, texto = VEREDICTOS.get(veredicto, ("❓", str(veredicto)))
    return f"{icono}  {texto}"

def _encabezado(nombre):
    print(f"\n{'─'*60}")
    print(f"📄 {nombre}")

def _md5(md5):
    if not md5:
        return
    estado = md5.get("estado")
    if estado == "match":
        print(f"  ✅ MD5 verificado: {md5.get('calculado')}")
    elif estado == "mismatch":
        print("  ❌ MD5 MISMATCH — file corrupt or modified")
        print(f"     stored    : {md5.get('almacenado')}")
        print(f"     computed  : {md5.get('calculado')}")
    else:
        print(f"  ⚠️  MD5 {str(estado).replace('_', ' ')}: {md5.get('detalle', '')}")

def _clipping(clip):
    if not clip:
        return
    if clip["hay_clipping"]:
        print(f"  ⚠️  Clipping: {clip['runs_clip']} clipping run(s) of consecutive full-scale samples")
    else:
        print("  ✅ No clipping")

def _dr(dr):
    if dr is None:
        return
    icono = "✅" if dr >= 14 else ("⚠️ " if dr >= 8 else "🔶")
    print(f"  {icono} Estimated dynamic range: {dr} dB")

def _bit_depth(bdi):
    if not bdi.get("aplica"):
        return
    if "error" in bdi:
        print(f"  ⚠️  Could not evaluate the LSB utilization: {bdi['error']}")
        return
    # "LSB activos" no es una buena noticia: solo significa que el test no puede
    # desmentir nada (un transcódigo lossy en 24 bits también llena los LSB).
    ic = {"empty": "❌", "partial": "⚠️ ", "active": "ℹ️ "}.get(bdi.get("lsbs"), "⚠️ ")
    print(f"  {ic} LSB utilization: {bdi['conclusion']}")

def _espectro(esp):
    if esp.get("error"):
        print(f"  ⚠️  Spectral error: {esp['error']}")
        return
    nyquist = esp.get("nyquist") or 0
    pct_nyquist = esp["techo_hz"] * 100 // nyquist if nyquist else 0
    print(f"  • Spectral ceiling (-60 dB): {esp['techo_hz']:,} Hz  "
          f"({pct_nyquist}% of Nyquist = {nyquist//1000} kHz)")
    if esp.get("ratio_aplica"):
        print(f"  • High/mid ratio:           {esp['ratio']:.5f}")
        print(f"  • High-band variance:       {esp['var_alta_db']:.1f} dB²")
    else:
        print("  • High band:                not evaluable (no content above 18 kHz)")
    if esp.get("separacion_stereo") is not None:
        print(f"  • L-R separation:           {esp['separacion_stereo']:.4f}")

    clase = esp.get("clase_corte")
    if clase == "far_cutoff":
        fc = esp.get("frecuencia_corte") or 0
        print(f"  ❌ Artificial cut-off at {fc:,} Hz ({fc/1000:.1f} kHz, "
              f"{(esp.get('distancia_nyquist') or 0)*100:.0f}% of Nyquist): codec signature")
    elif clase == "near_nyquist_cutoff":
        fc = esp.get("frecuencia_corte") or 0
        print(f"  ℹ️  Band cut at {fc:,} Hz ({fc/1000:.1f} kHz, "
              f"{(esp.get('distancia_nyquist') or 0)*100:.0f}% of Nyquist) — inconclusive: "
              f"a legitimate master's anti-alias filter lives here too")
    if esp.get("espectrograma"):
        print(f"  📊 Spectrogram: {esp['espectrograma']}")

def _cuerpo(res):
    """Imprime el informe de un archivo ya analizado. Devuelve el veredicto o None."""
    error = res.get("error")
    if error:
        print(f"  ❌ FAILED — {error}")
        if (res.get("md5") or {}).get("estado") == "mismatch":
            print(f"     stored    : {res['md5'].get('almacenado')}")
            print(f"     computed  : {res['md5'].get('calculado')}")
        return None

    meta = res["meta"]
    print(f"  ✅ Metadata: {meta['sample_rate']} Hz | {meta['bits_per_sample']} bits | "
          f"{meta['canales']} channel(s) | {meta['duracion']} s")
    _md5(res.get("md5"))
    _clipping(res.get("clip"))
    _dr(res.get("dr"))
    _bit_depth(res.get("bdi") or {})
    _espectro(res.get("esp") or {})

    print(f"\n  {etiqueta_veredicto(res['veredicto'])}  (score {res['score']:.0%})")
    for problema in res.get("problemas", []):
        print(f"    → {problema}")
    return res["veredicto"]

def imprimir_informe(res):
    """Informe completo (encabezado + cuerpo) de un resultado del motor."""
    _encabezado(res.get("archivo", "?"))
    return _cuerpo(res)

# ─── MENÚ ────────────────────────────────────────────────────────────────────

def pedir_modo():
    print('\nHow do you want to analyze the spectrum?')
    print("  [1] First N seconds (fast)")
    print("  [2] 30 seconds from the center (recommended)")
    print("  [3] Full track (more accurate, slower)")
    opcion = input("Choose an option (1/2/3): ").strip()
    modo = "full"; segundos = None
    if opcion == "1":
        seg_input = input("How many seconds? (default 15): ").strip()
        segundos = int(seg_input) if seg_input.isdigit() else 15
        modo = "seconds"; print(f"\nMode: first {segundos} seconds")
    elif opcion == "2":
        modo = "center"; print('\nMode: 30 seconds from the center')
    else:
        modo = "full"; print('\nMode: full track')
    return modo, segundos

def pedir_espectrograma():
    if not motor.MATPLOTLIB_OK:
        return False
    resp = input('\nSave a PNG spectrogram per track? (y/N): ').strip().lower()
    return resp in AFIRMATIVOS

def pedir_informe():
    resp = input("Generate the album PDF report when finished? (y/N): ").strip().lower()
    return resp in AFIRMATIVOS

# ─── EJECUCIÓN ───────────────────────────────────────────────────────────────

def analizar_archivo(ruta, modo, segundos=None, guardar_png=False, resultado=None):
    """Analiza un archivo (o presenta uno ya calculado) y devuelve el resultado.

    Devuelve el dict del motor (para el informe PDF) o None si falló de forma
    inesperada."""
    _encabezado(os.path.basename(ruta))
    if resultado is None:
        if sys.stdout.isatty():
            print("  ⏳ analyzing…", flush=True)
        try:
            resultado = motor.analizar_archivo_datos(ruta, modo, segundos, guardar_png)
        except Exception as e:                 # el motor ya blinda; red de seguridad
            print(f"  ❌ Unexpected failure: {type(e).__name__}: {e}")
            return None
    elif resultado.get("desde_cache"):
        print("  ⚡ result reused from cache (the file has not changed)")
    _cuerpo(resultado)
    return resultado

def ejecutar_analisis(ruta, modo, segundos, guardar_png, guardar_pdf=False):
    es_carpeta = os.path.isdir(ruta)
    if os.path.isfile(ruta) and ruta.lower().endswith(".flac"):
        archivos = [ruta]
    elif es_carpeta:
        archivos = [os.path.join(ruta, f) for f in sorted(os.listdir(ruta))
                    if f.lower().endswith(".flac")]
        if not archivos:
            print("  No .flac files found in that folder.")
            return
        print(f"\nAnalyzing {len(archivos)} file(s)...")
    else:
        print("  Invalid path, or the file is not a .flac")
        return

    # El informe PDF necesita los espectrogramas: se generan aunque no se hayan
    # pedido los PNG, para que el usuario no tenga que combinar opciones.
    if guardar_pdf:
        guardar_png = True

    analisis, workers = motor.analizar_archivos(archivos, modo, segundos, guardar_png)
    if workers > 1:
        print(f"  ({workers} processes in parallel)")

    verdicts   = Counter()
    resultados = []
    fallos     = 0
    for ruta_archivo, resultado in zip(archivos, analisis):
        res = analizar_archivo(ruta_archivo, modo, segundos, guardar_png, resultado)
        if res is None:
            fallos += 1
            continue
        resultados.append(res)
        if res.get("veredicto"):
            verdicts[res["veredicto"]] += 1
        else:
            fallos += 1

    if es_carpeta:
        print(f"\n{'═'*60}")
        print(f"SUMMARY: {verdicts['GENUINE LOSSLESS']} ✅ GENUINE  |  "
              f"{verdicts['PROBABLY LOSSLESS'] + verdicts['SUSPICIOUS']} ⚠️  SUSPICIOUS  |  "
              f"{verdicts['PROBABLE UPSCALE']} ❌ UPSCALE"
              + (f"  |  {fallos} ❌ WITH ERRORS" if fallos else ""))
        print(f"{'═'*60}")
        if guardar_png and motor.MATPLOTLIB_OK:
            print(f"📊 Spectrograms in: {os.path.join(ruta, '_spectrograms')}")

    if guardar_pdf:
        # Un fallo al escribir el PDF se avisa; nunca aborta el análisis ya hecho.
        informe = motor.generar_informe_pdf(ruta, resultados, modo=modo)
        if informe.get("ok"):
            print(f"📄 PDF report: {informe['ruta']}  ({informe.get('paginas', '?')} pages)")
        else:
            print(f"⚠️  Could not generate the PDF report: {informe.get('error')}")

# ─── MAIN ────────────────────────────────────────────────────────────────────

def main(argv=None):
    """CLI interactivo. Con `--gui` (o `-g`) abre la interfaz gráfica."""
    # Antes de imprimir CUALQUIER cosa: si la salida está redirigida en Windows,
    # cp1252 no puede con los marcos ni con los emojis, y el propio mensaje de
    # error moriría con UnicodeEncodeError.
    motor.forzar_utf8_salida()

    argumentos = list(sys.argv[1:] if argv is None else argv)
    if argumentos and argumentos[0] in ("--gui", "-g"):
        try:
            from gui import main as main_grafico
        except Exception as e:
            print(f"⚠️  Could not load the graphical interface: {type(e).__name__}: {e}")
            print("   Install the dependencies with: pip install -r requirements.txt")
            return 1
        return main_grafico(argumentos[1] if len(argumentos) > 1 else None)

    print(f"verificar_flac {VERSION} — advanced FLAC authenticity analysis")
    print("═" * 60)
    print("  Optional dependencies: pip install matplotlib  (PNG spectrograms)")
    print("                         pip install reportlab    (PDF report)")
    print("                         pip install customtkinter  (graphical interface)")
    print()
    if not motor.MATPLOTLIB_OK:
        print("⚠️  matplotlib not found — PNG spectrograms disabled.")
        print('   Install it with: pip install matplotlib\n')
    print('ℹ️  Type "exit" or "q" as the path to quit.')
    print('ℹ️  For the graphical interface:  python verificar_flac.py --gui\n')

    try:
        modo, segundos = pedir_modo()
        guardar_png    = pedir_espectrograma()
        guardar_pdf    = pedir_informe()

        while True:
            print()
            ruta = input("Enter the path (or 'exit' to quit): ").strip().strip('"')
            if ruta.lower() in SALIR_CMDS:
                print('\nExiting. Goodbye!')
                break
            ejecutar_analisis(ruta, modo, segundos, guardar_png, guardar_pdf)
            print()
            otra = input("Analyze another folder? (y/N): ").strip().lower()
            if otra not in AFIRMATIVOS:
                print('\nExiting. Goodbye!')
                break
            cambiar = input("Change analysis mode? (y/N): ").strip().lower()
            if cambiar in AFIRMATIVOS:
                modo, segundos = pedir_modo()
                guardar_png    = pedir_espectrograma()
                guardar_pdf    = pedir_informe()
    except (EOFError, KeyboardInterrupt):
        # stdin cerrado (scripts, tuberías) o Ctrl+C: salir sin traceback
        print('\n\nExiting. Goodbye!')

if __name__ == "__main__":
    main()
