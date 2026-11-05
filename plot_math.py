"""Numbers behind the live graph: rate of change per minute, and thinning long runs for drawing.

No Qt or matplotlib here (only numpy/scipy), so it can be tested on its own.
"""

import numpy as np

MAX_DRAWN_POINTS = 2000  # more than this many points per line are thinned before drawing


def derivative_per_minute(minutes, values, smooth_window=5):
    """d(log10 C)/dt in decades per minute, one value per reading (NaN where it can't be known).

    ``minutes`` are the readings' times in minutes (any origin), ``values`` the conductivity
    (any unit; readings <= 0 are gaps). Readings that share a time are averaged first, and the
    log values are smoothed with a Savitzky-Golay filter of ``smooth_window`` points before
    taking the gradient against time, so the result doesn't depend on the sampling rate.
    """
    t = np.asarray(minutes, dtype=float)
    y = np.asarray(values, dtype=float)
    out = np.full(t.shape, np.nan)
    good = np.isfinite(t) & np.isfinite(y) & (y > 0)
    if good.sum() < 2:
        return out
    t_good, log_y = t[good], np.log10(y[good])
    t_unique, inverse = np.unique(t_good, return_inverse=True)
    if len(t_unique) < 2:
        return out
    sums = np.bincount(inverse, weights=log_y)
    counts = np.bincount(inverse)
    y_unique = sums / counts
    window = int(smooth_window)
    if window % 2 == 0:
        window += 1
    if window >= 3 and len(y_unique) > window:
        try:
            from scipy.signal import savgol_filter

            y_unique = savgol_filter(y_unique, window_length=window, polyorder=2)
        except Exception:  # noqa: S110 - smoothing is optional; the raw gradient still works
            pass
    slope = np.gradient(y_unique, t_unique)
    out[np.flatnonzero(good)] = slope[inverse]
    return out


def thin_indices(values, max_points=MAX_DRAWN_POINTS):
    """Indices of the points to draw: all of them, or, for a long run, each bucket's lowest and
    highest point (so a short spike or dip still shows) plus the first and last point."""
    n = len(values)
    if n <= max_points:
        return np.arange(n)
    y = np.asarray(values, dtype=float)
    buckets = max(1, max_points // 2)
    size = -(-n // buckets)  # ceil
    padded = np.full(buckets * size, np.nan)
    padded[:n] = y
    grid = padded.reshape(buckets, size)
    missing = ~np.isfinite(grid)
    lows = np.where(missing, np.inf, grid).argmin(axis=1)
    highs = np.where(missing, -np.inf, grid).argmax(axis=1)
    base = np.arange(buckets) * size
    keep = np.concatenate([base + lows, base + highs, [0, n - 1]])
    return np.unique(keep[keep < n])


def window_start(times, width):
    """Index of the first reading within ``width`` of the last one (times sorted ascending)."""
    if len(times) == 0:
        return 0
    import bisect

    return bisect.bisect_left(times, times[-1] - width)
