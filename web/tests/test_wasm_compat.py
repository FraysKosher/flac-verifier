import os
import sys
import unittest
from unittest import mock

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import wasm_compat  # noqa: E402


class SlidingWindowFallbackTests(unittest.TestCase):
    def test_window_longer_than_input_still_raises(self):
        with self.assertRaises(ValueError):
            wasm_compat.sliding_window_view(np.zeros(4), 8)

    def test_unrelated_value_error_propagates(self):
        with mock.patch.object(wasm_compat, "_original",
                               side_effect=ValueError("other")):
            with self.assertRaises(ValueError):
                wasm_compat.sliding_window_view(np.zeros(16), 4)

    def test_size_overflow_falls_back_to_lazy_view(self):
        x = np.arange(16, dtype=np.float64)
        with mock.patch.object(wasm_compat, "_original",
                               side_effect=ValueError("array is too big")):
            view = wasm_compat.sliding_window_view(x, 4)
        np.testing.assert_array_equal(view[::2], np.lib.stride_tricks
                                      .sliding_window_view(x, 4)[::2])


if __name__ == "__main__":
    unittest.main()
