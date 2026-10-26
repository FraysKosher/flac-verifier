"""Engine JSON protocol: one line per event, always valid.

Covers the failures reproduced in the audit:
  - an exception on one file interrupted the batch and 'fin' was NEVER emitted.
  - separacion_stereo = NaN produced invalid JSON that JSON.parse rejects.
"""
import json
import math
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fixtures as fx

import motor_flac as engine


class TestProtocolo(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.fx = fx.Fixtures()

    @classmethod
    def tearDownClass(cls):
        cls.fx.close()

    def test_batch_with_files_broken_emits_start_results_end(self):
        p, events, crudas = fx.results(self.fx.dir)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(events[0]["tipo"], "inicio")
        self.assertEqual(events[-1]["tipo"], "fin")        # before, it was lost
        results = [e for e in events if e["tipo"] == "resultado"]
        self.assertEqual(len(results), events[0]["total"])
        self.assertEqual(events[0]["total"], len(crudas) - 2)
        # all indices present and consecutive
        self.assertEqual([r["indice"] for r in results],
                         list(range(1, len(results) + 1)))

    def test_all_lines_are_json_strict(self):
        """JavaScript's JSON.parse rejects the literals NaN/Infinity."""
        _, _, crudas = fx.results(self.fx.dir)
        for line in crudas:
            with self.subTest(line=line[:70]):
                json.loads(line, parse_constant=fx.rechaza_constante)
                self.assertNotIn("NaN", line)
                self.assertNotIn("Infinity", line)

    def test_file_32k_stereo_no_produces_nan(self):
        """Nyquist 16 kHz -> empty high-band masks (the origin of the NaN)."""
        res = engine.analyze_file_data(self.fx["estereo32k"], "center", None, False)
        esp = res["esp"]
        self.assertIsNone(esp["separacion_stereo"])       # before: nan
        self.assertEqual(esp["ratio"], 0.0)
        for key, value in esp.items():
            if isinstance(value, float):
                with self.subTest(metrica=key):
                    self.assertTrue(math.isfinite(value), f"{key} = {value}")

    def test_sanitize_converts_non_finite_to_null(self):
        line = engine._json_line({"esp": {"ratio": float("nan")},
                                   "lista": [float("inf"), float("-inf")],
                                   "ok": 1.5})
        self.assertNotIn("NaN", line)
        self.assertNotIn("Infinity", line)
        self.assertEqual(json.loads(line)["esp"]["ratio"], None)
        self.assertEqual(json.loads(line)["lista"], [None, None])
        self.assertEqual(json.loads(line)["ok"], 1.5)

    def test_result_by_file_brings_verdict_o_error(self):
        _, events, _ = fx.results(self.fx.dir)
        for event in events:
            if event["tipo"] != "resultado":
                continue
            with self.subTest(file=event["archivo"]):
                has_error = "error" in event
                has_verdict = "veredicto" in event
                self.assertTrue(has_error or has_verdict)
                self.assertFalse(has_error and has_verdict)
                if has_verdict:
                    self.assertTrue(0.0 <= event["score"] <= 1.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
