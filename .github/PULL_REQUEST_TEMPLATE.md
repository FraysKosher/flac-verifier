<!--
Short and specific beats long. Say what changed, why, and how you checked it.
-->

## What this changes

<!-- One paragraph. Which problem does it solve, and for whom? -->

## Why

<!--
The reason matters more than the diff. If it fixes a wrong verdict, show the file
or the measurement that proves the old behaviour was wrong.
-->

## How it was checked

<!--
Say exactly what you ran. For anything touching the analysis, the useful evidence is
a comparison against the fixtures: verdict, score, issues and MD5, before and after.
-->

- [ ] `python -m unittest discover -s tests` passes (229 tests)
- [ ] The change is covered by a test, or the reason it cannot be is written above
- [ ] Any measurement quoted above was produced on this branch, not recalled

## Checklist of the project's invariants

These four have caused real bugs. Please confirm each one that applies:

- [ ] **`multiprocessing.freeze_support()` is still the first statement** of `main.py`'s `__main__` block. If it moves, every worker of the parallel analysis relaunches the whole application — a new window per worker in the packaged build. `tests/test_empaquetado.py` checks it.
- [ ] **If the analysis changed at all, `VERSION_MOTOR` in `motor_flac.py` was bumped.** The cache stores finished results keyed by that version; without the bump, stale verdicts survive an upgrade. If your change only affects presentation, no bump is needed.
- [ ] **Nothing the user sees is in Spanish**, and the JSON protocol values match in the engine, the GUI and the PDF. `tests/test_idioma.py` is the guard for both.
- [ ] **The presentation layers stay thin.** `motor_flac.py` owns every analysis decision; `verificar_flac.py`, `gui.py` and `informe_pdf.py` format and display. No scoring logic may be duplicated in them.

## Packaging, only if the executable is affected

- [ ] `python build.py` completes and both executables respond to `--version`
- [ ] `python comprobar_ejecutable.py --album "<a small album>"` reports all 11 checks passed

## Anything else

<!--
Trade-offs you accepted, an alternative you rejected, a limit the reviewer should
know about. If this makes a documented behaviour worse, say so here explicitly.
-->
