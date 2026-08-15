"""Deterministic time-series primitives shared by scientific studies."""

from __future__ import annotations

from dataclasses import dataclass
import math
from statistics import fmean
from typing import Sequence


@dataclass(frozen=True)
class Peak:
    refined_time: float | None
    sample_time: float | None
    value: float | None
    index: int | None


def strictly_increasing(x: Sequence[float]) -> bool:
    return all(x[i] > x[i - 1] for i in range(1, len(x)))


def derivative(time: Sequence[float], values: Sequence[float]) -> list[float]:
    """First derivative using a three-point local quadratic in the interior.

    Endpoints use one-sided secants.  The interior formula supports nonuniform
    sampling and reduces to the ordinary centered difference on a uniform grid.
    """
    n = len(time)
    if len(values) != n:
        raise ValueError("time and value arrays have different lengths")
    if n < 2:
        return [math.nan] * n
    if not strictly_increasing(time):
        raise ValueError("time values must be strictly increasing")

    out = [math.nan] * n
    out[0] = (values[1] - values[0]) / (time[1] - time[0])
    out[-1] = (values[-1] - values[-2]) / (time[-1] - time[-2])

    for i in range(1, n - 1):
        x0, x1, x2 = time[i - 1], time[i], time[i + 1]
        y0, y1, y2 = values[i - 1], values[i], values[i + 1]
        a0 = (x1 - x2) / ((x0 - x1) * (x0 - x2))
        a1 = (2.0 * x1 - x0 - x2) / ((x1 - x0) * (x1 - x2))
        a2 = (x1 - x0) / ((x2 - x0) * (x2 - x1))
        out[i] = a0 * y0 + a1 * y1 + a2 * y2
    return out


def threshold_crossing(time: Sequence[float], values: Sequence[float], threshold: float) -> float | None:
    if len(time) != len(values) or not time:
        raise ValueError("invalid threshold-crossing input")
    if values[0] >= threshold:
        return time[0]
    for i in range(1, len(time)):
        if values[i] >= threshold and values[i - 1] < threshold:
            dv = values[i] - values[i - 1]
            if dv == 0.0:
                return time[i]
            fraction = (threshold - values[i - 1]) / dv
            return time[i - 1] + fraction * (time[i] - time[i - 1])
    return None


def quadratic_peak_time(time: Sequence[float], values: Sequence[float], index: int) -> float:
    """Sub-sample peak estimate using a quadratic fit for nearly uniform data."""
    if index <= 0 or index >= len(values) - 1:
        return time[index]
    h1 = time[index] - time[index - 1]
    h2 = time[index + 1] - time[index]
    if h1 <= 0.0 or h2 <= 0.0:
        return time[index]
    if abs(h1 - h2) > 1.0e-6 * max(h1, h2):
        return time[index]
    h = 0.5 * (h1 + h2)
    ym, y0, yp = values[index - 1], values[index], values[index + 1]
    denom = ym - 2.0 * y0 + yp
    scale = max(abs(ym), abs(y0), abs(yp), 1.0)
    if abs(denom) <= 1.0e-15 * scale:
        return time[index]
    offset = 0.5 * h * (ym - yp) / denom
    if abs(offset) > h:
        return time[index]
    return time[index] + offset


def maximum(time: Sequence[float], values: Sequence[float]) -> Peak:
    if len(time) != len(values):
        raise ValueError("time and value arrays have different lengths")
    finite = [(value, i) for i, value in enumerate(values) if math.isfinite(value)]
    if not finite:
        return Peak(None, None, None, None)
    value, index = max(finite, key=lambda pair: pair[0])
    return Peak(quadratic_peak_time(time, values, index), time[index], value, index)


def max_abs(values: Sequence[float]) -> float | None:
    finite = [abs(value) for value in values if math.isfinite(value)]
    return max(finite) if finite else None


def rms(values: Sequence[float]) -> float | None:
    finite = [value for value in values if math.isfinite(value)]
    return math.sqrt(sum(value * value for value in finite) / len(finite)) if finite else None


def mean_abs(values: Sequence[float]) -> float | None:
    finite = [abs(value) for value in values if math.isfinite(value)]
    return fmean(finite) if finite else None


def relative_span(values: Sequence[float], floor: float = 1.0) -> float:
    mean = fmean(values)
    return (max(values) - min(values)) / max(abs(mean), floor)


def final_window_index(
    time: Sequence[float], *, fraction: float | None = None, duration_s: float | None = None, minimum_points: int = 5
) -> int:
    if not time:
        raise ValueError("empty time array")
    if fraction is not None and duration_s is not None:
        raise ValueError("specify final-window fraction or duration, not both")
    if duration_s is not None:
        if duration_s <= 0.0:
            raise ValueError("duration_s must be positive")
        cutoff = time[-1] - duration_s
    else:
        use_fraction = 0.1 if fraction is None else fraction
        if not (0.0 < use_fraction <= 1.0):
            raise ValueError("fraction must lie in (0,1]")
        cutoff = time[-1] - use_fraction * (time[-1] - time[0])
    index = next((i for i, t in enumerate(time) if t >= cutoff), len(time) - 1)
    return max(0, min(index, max(0, len(time) - minimum_points)))
