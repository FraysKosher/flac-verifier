"""wasm32 compatibility patch for numpy, applied before the engine runs.

On wasm32, numpy rejects the logical size of a full ``sliding_window_view``
(``n_windows * window * itemsize > 2**31``) even when the result is
immediately sliced with ``[::hop]``. The engine does exactly that, so long
files fail with a ValueError. This module wraps ``sliding_window_view`` and,
on that failure, returns a lazy object whose first-axis slice builds the small
strided view the engine actually needs. The engine itself stays unmodified.
"""
import numpy as np
from numpy.lib import stride_tricks as _st
from numpy.lib.stride_tricks import as_strided

_original = _st.sliding_window_view


class _LazyWindows:
    def __init__(self, x, window):
        self.x = x
        self.window = window
        self.n = x.shape[0] - window + 1

    def __len__(self):
        return self.n

    def __getitem__(self, key):
        if isinstance(key, slice):
            start, stop, step = key.indices(self.n)
            count = len(range(start, stop, step))
            stride = self.x.strides[0]
            return as_strided(self.x[start:], shape=(count, self.window),
                              strides=(step * stride, stride), writeable=False)
        raise TypeError("lazy windows: unsupported key %r" % (key,))


def sliding_window_view(x, window_shape, axis=None, **kwargs):
    try:
        return _original(x, window_shape, axis=axis, **kwargs)
    except ValueError as error:
        if (axis is None and x.ndim == 1 and isinstance(window_shape, int)
                and 0 < window_shape <= x.shape[0]
                and "too big" in str(error)):
            return _LazyWindows(x, window_shape)
        raise


_st.sliding_window_view = sliding_window_view
np.lib.stride_tricks.sliding_window_view = sliding_window_view
