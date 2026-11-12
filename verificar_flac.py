"""FLAC VERIFIER — command-line interface.

This is a PRESENTATION LAYER: there is no analysis logic here. The whole engine
(signature, metadata, MD5, clipping, dynamic range, bit-depth, spectrum and
scoring) lives in motor_flac.py. This file only calls the engine, formats the
report and drives the interactive menu.

Usage:
    python verificar_flac.py          # interactive menu
    python motor_flac.py --path ...   # raw engine, JSON protocol per line
"""
import os
import sys
from collections import Counter

# ─── BLAS THREADS (before numpy/scipy are imported) ──────────────────────────
# Pinned to one thread per process; the engine parallelises across processes.
for _variable_blas in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS",
                       "MKL_NUM_THREADS", "VECLIB_MAXIMUM_THREADS",
                       "NUMEXPR_NUM_THREADS"):
    os.environ.setdefault(_variable_blas, "1")
del _variable_blas

# Allows the script to be run from any working directory.
_DIRECTORY = os.path.dirname(os.path.abspath(__file__))
if _DIRECTORY not in sys.path:
    sys.path.insert(0, _DIRECTORY)

import motor_flac as engine

# The version has a SINGLE source: main.py owns it, build.py stamps the .exe from
# it and version_info.txt mirrors it. It used to be a literal here as well, and it
# drifted ("v4.0" while the program announced "2.0.0"), so this now reads it back
# from that one place instead of keeping a second copy in sync by hand.
try:
    from main import VERSION
except Exception:                        # main.py unavailable: the CLI still works
    VERSION = "unknown"

EXIT_CMDS = {"exit", "q", "quit", "0"}
AFFIRMATIVE = ("y", "yes")

# Canonical engine verdicts -> presentation.
# They are compared by EXACT EQUALITY against motor_flac.calculate_score, so there
# are no substring comparisons any more and no two sets of strings that could
# diverge.
VERDICTS = {
    "GENUINE LOSSLESS":       ("✅", "GENUINE LOSSLESS"),
    "PROBABLY LOSSLESS": ("⚠️ ", "PROBABLY LOSSLESS"),
    "SUSPICIOUS":                 ("🔶", "SUSPICIOUS — possible upscale"),
    "PROBABLE UPSCALE":       ("❌", "PROBABLE UPSCALE / TRANSCODE"),
    "indeterminate":          ("❓", "indeterminate"),
}

# ─── REPORT PRESENTATION ─────────────────────────────────────────────────────

def verdict_label(verdict):
    icon, text = VERDICTS.get(verdict, ("❓", str(verdict)))
    return f"{icon}  {text}"

def _header(name):
    print(f"\n{'─'*60}")
    print(f"📄 {name}")

def _md5(md5):
    if not md5:
        return
    state = md5.get("estado")
    if state == "match":
        print(f"  ✅ MD5 verified: {md5.get('calculado')}")
    elif state == "mismatch":
        print("  ❌ MD5 MISMATCH — file corrupt or modified")
        print(f"     stored    : {md5.get('almacenado')}")
        print(f"     computed  : {md5.get('calculado')}")
    else:
        print(f"  ⚠️  MD5 {str(state).replace('_', ' ')}: {md5.get('detalle', '')}")

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
    icon = "✅" if dr >= 14 else ("⚠️ " if dr >= 8 else "🔶")
    print(f"  {icon} Estimated dynamic range: {dr} dB")

def _bit_depth(bdi):
    if not bdi.get("aplica"):
        return
    if "error" in bdi:
        print(f"  ⚠️  Could not evaluate the LSB utilization: {bdi['error']}")
        return
    # "Active LSBs" is not good news: it only means that the test cannot disprove
    # anything (a lossy transcode at 24 bits fills the LSB as well).
    ic = {"empty": "❌", "partial": "⚠️ ", "active": "ℹ️ "}.get(bdi.get("lsbs"), "⚠️ ")
    print(f"  {ic} LSB utilization: {bdi['conclusion']}")

def _spectrum(spectrum):
    if spectrum.get("error"):
        print(f"  ⚠️  Spectral error: {spectrum['error']}")
        return
    nyquist = spectrum.get("nyquist") or 0
    pct_nyquist = spectrum["techo_hz"] * 100 // nyquist if nyquist else 0
    print(f"  • Spectral ceiling (-60 dB): {spectrum['techo_hz']:,} Hz  "
          f"({pct_nyquist}% of Nyquist = {nyquist//1000} kHz)")
    if spectrum.get("ratio_aplica"):
        print(f"  • High/mid ratio:           {spectrum['ratio']:.5f}")
        print(f"  • High-band variance:       {spectrum['var_alta_db']:.1f} dB²")
    else:
        print("  • High band:                not evaluable (no content above 18 kHz)")
    if spectrum.get("separacion_stereo") is not None:
        print(f"  • L-R separation:           {spectrum['separacion_stereo']:.4f}")

    category = spectrum.get("clase_corte")
    if category == "far_cutoff":
        fc = spectrum.get("frecuencia_corte") or 0
        print(f"  ❌ Artificial cut-off at {fc:,} Hz ({fc/1000:.1f} kHz, "
              f"{(spectrum.get('distancia_nyquist') or 0)*100:.0f}% of Nyquist): codec signature")
    elif category == "near_nyquist_cutoff":
        fc = spectrum.get("frecuencia_corte") or 0
        print(f"  ℹ️  Band cut at {fc:,} Hz ({fc/1000:.1f} kHz, "
              f"{(spectrum.get('distancia_nyquist') or 0)*100:.0f}% of Nyquist) — inconclusive: "
              f"a legitimate master's anti-alias filter lives here too")
    if spectrum.get("espectrograma"):
        print(f"  📊 Spectrogram: {spectrum['espectrograma']}")

def _body(res):
    """Prints the report for an already analysed file. Returns the verdict or None."""
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
    _spectrum(res.get("esp") or {})

    print(f"\n  {verdict_label(res['veredicto'])}  (score {res['score']:.0%})")
    for problem in res.get("problemas", []):
        print(f"    → {problem}")
    return res["veredicto"]

# ─── MENU ────────────────────────────────────────────────────────────────────

def ask_mode():
    print('\nHow do you want to analyze the spectrum?')
    print("  [1] First N seconds (fast)")
    print("  [2] 45 s of highest-energy active blocks (recommended)")
    print("  [3] Full track (more accurate, slower)")
    option = input("Choose an option (1/2/3): ").strip()
    mode = "full"; seconds = None
    if option == "1":
        seg_input = input("How many seconds? (default 15): ").strip()
        seconds = int(seg_input) if seg_input.isdigit() else 15
        mode = "seconds"; print(f"\nMode: first {seconds} seconds")
    elif option in ("", "2"):
        mode = "center"; print("\nMode: 45 s of highest-energy active blocks")
    else:
        mode = "full"; print('\nMode: full track')
    return mode, seconds

def request_spectrogram():
    if not engine.PIL_OK:
        return False
    resp = input('\nSave a PNG spectrogram per track? (y/N): ').strip().lower()
    return resp in AFFIRMATIVE

def ask_report():
    resp = input("Generate the album PDF report when finished? (y/N): ").strip().lower()
    return resp in AFFIRMATIVE

# ─── EXECUTION ───────────────────────────────────────────────────────────────

def analyze_file(path, mode, seconds=None, save_png=False, result=None):
    """Analyses a file (or presents one already computed) and returns the result.

    Returns the engine dict (for the PDF report) or None if it failed
    unexpectedly."""
    _header(os.path.basename(path))
    if result is None:
        if sys.stdout.isatty():
            print("  ⏳ analyzing…", flush=True)
        try:
            result = engine.analyze_file_data(path, mode, seconds, save_png)
        except Exception as e:                 # the engine already shields; safety net
            print(f"  ❌ Unexpected failure: {type(e).__name__}: {e}")
            return None
    elif result.get("desde_cache"):
        print("  ⚡ result reused from cache (the file has not changed)")
    _body(result)
    return result

def execute_analysis(path, mode, seconds, save_png, save_pdf=False):
    is_folder = os.path.isdir(path)
    if os.path.isfile(path) and path.lower().endswith(".flac"):
        files = [path]
    elif is_folder:
        files = [os.path.join(path, f)
                    for f in sorted(os.listdir(path), key=engine.natural_sort_key)
                    if f.lower().endswith(".flac")]
        if not files:
            print("  No .flac files found in that folder.")
            return
        print(f"\nAnalyzing {len(files)} file(s)...")
    else:
        print("  Invalid path, or the file is not a .flac")
        return

    # The PDF report needs the spectrograms: they are generated even when the PNG
    # files were not requested, so that the user does not have to combine options.
    if save_pdf:
        save_png = True

    analysis, workers = engine.analyze_files(files, mode, seconds, save_png)
    if workers > 1:
        print(f"  ({workers} processes in parallel)")

    verdicts   = Counter()
    results = []
    failures     = 0
    pending_by_index = {}
    announced_holds = set()
    next_index = 0

    def _emit_track(file_path, result):
        nonlocal failures
        res = analyze_file(file_path, mode, seconds, save_png, result)
        if res is None:
            failures += 1
            return
        results.append(res)
        if res.get("veredicto"):
            verdicts[res["veredicto"]] += 1
        else:
            failures += 1

    for index, result in enumerate(analysis):
        original_index = result.get("indice_original", index)
        if not isinstance(original_index, int) or not (0 <= original_index < len(files)):
            original_index = min(index, len(files) - 1)
        file_path = result.get("ruta") or files[original_index]
        pending_by_index[original_index] = (file_path, result)
        while next_index in pending_by_index:
            queued_path, queued_res = pending_by_index.pop(next_index)
            _emit_track(queued_path, queued_res)
            next_index += 1
        if (original_index > next_index
                and next_index not in announced_holds
                and sys.stdout.isatty()):
            print(
                f"  ⏳ Holding {os.path.basename(files[next_index])} "
                f"for album-level coherence check...",
                flush=True,
            )
            announced_holds.add(next_index)

    for original_index in sorted(pending_by_index):
        queued_path, queued_res = pending_by_index[original_index]
        _emit_track(queued_path, queued_res)

    if is_folder:
        print(f"\n{'═'*60}")
        print(f"SUMMARY: {verdicts['GENUINE LOSSLESS']} ✅ GENUINE  |  "
              f"{verdicts['PROBABLY LOSSLESS'] + verdicts['SUSPICIOUS']} ⚠️  SUSPICIOUS  |  "
              f"{verdicts['PROBABLE UPSCALE']} ❌ UPSCALE"
              + (f"  |  {failures} ❌ WITH ERRORS" if failures else ""))
        print(f"{'═'*60}")
        if save_png and engine.PIL_OK:
            print(f"📊 Spectrograms in: {os.path.join(path, '_spectrograms')}")

    if save_pdf:
        # Sort by original track index so held-back coherence tracks appear
        # in album order inside the PDF report.
        results.sort(key=lambda r: r.get("indice_original", 0)
                     if isinstance(r.get("indice_original"), int) else 0)
        # A failed PDF write is reported; the analysis is never aborted.
        report = engine.generate_pdf_report(path, results, mode=mode)
        if report.get("ok"):
            print(f"📄 PDF report: {report['ruta']}  ({report.get('paginas', '?')} pages)")
        else:
            print(f"⚠️  Could not generate the PDF report: {report.get('error')}")

# ─── MAIN ────────────────────────────────────────────────────────────────────

def main(argv=None):
    """Interactive CLI. With `--gui` (or `-g`) it opens the graphical interface."""
    # Before printing ANYTHING: if the output is redirected on Windows, cp1252
    # cannot cope with the frames or with the emoji, and the error message itself
    # would die with UnicodeEncodeError.
    engine.force_utf8_output()

    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] in ("--gui", "-g"):
        try:
            from gui import main as main_grafico
        except Exception as e:
            print(f"⚠️  Could not load the graphical interface: {type(e).__name__}: {e}")
            print("   Install the dependencies with: pip install -r requirements.txt")
            return 1
        return main_grafico(arguments[1] if len(arguments) > 1 else None)

    print(f"verificar_flac {VERSION} — advanced FLAC authenticity analysis")
    print("═" * 60)
    print("  Optional dependencies: pip install pillow      (PNG spectrograms)")
    print("                         pip install reportlab    (PDF report)")
    print("                         pip install customtkinter  (graphical interface)")
    print()
    if not engine.PIL_OK:
        print("⚠️  Pillow not found — PNG spectrograms disabled.")
        print('   Install it with: pip install pillow\n')
    print('ℹ️  Type "exit" or "q" as the path to quit.')
    print('ℹ️  For the graphical interface:  python verificar_flac.py --gui\n')

    try:
        mode, seconds = ask_mode()
        save_png    = request_spectrogram()
        save_pdf    = ask_report()

        while True:
            print()
            path = input("Enter the path (or 'exit' to quit): ").strip().strip('"')
            if path.lower() in EXIT_CMDS:
                print('\nExiting. Goodbye!')
                break
            execute_analysis(path, mode, seconds, save_png, save_pdf)
            print()
            otra = input("Analyze another folder? (y/N): ").strip().lower()
            if otra not in AFFIRMATIVE:
                print('\nExiting. Goodbye!')
                break
            change = input("Change analysis mode? (y/N): ").strip().lower()
            if change in AFFIRMATIVE:
                mode, seconds = ask_mode()
                save_png    = request_spectrogram()
                save_pdf    = ask_report()
    except (EOFError, KeyboardInterrupt):
        # stdin closed (scripts, pipes) or Ctrl+C: exit without a traceback
        print('\n\nExiting. Goodbye!')

if __name__ == "__main__":
    main()
