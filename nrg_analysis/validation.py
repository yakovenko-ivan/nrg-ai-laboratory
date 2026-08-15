"""Numerical-integrity diagnostics independent of scientific interpretation."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import math
from typing import Any

from .io import ReactorHistory
from .timeseries import strictly_increasing


@dataclass(frozen=True)
class HistoryValidation:
    monotonic_time: bool
    nonfinite_values: int
    max_abs_sumY_error: float | None
    min_species_mass_fraction: float | None
    max_species_mass_fraction: float | None
    species_bound_violations: int
    max_relative_density_drift: float | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def validate_history(history: ReactorHistory, *, species_tolerance: float = 1.0e-12) -> HistoryValidation:
    all_series = [history.time_s, *history.coordinates.values(), *history.observables.values()]
    nonfinite = sum(1 for series in all_series for value in series if not math.isfinite(value))

    species = [history.species_mass_fraction(name) for name in history.species_names]
    if species:
        sum_errors = []
        min_y = math.inf
        max_y = -math.inf
        violations = 0
        for i in range(history.rows):
            values = [series[i] for series in species]
            sum_errors.append(abs(sum(values) - 1.0))
            min_y = min(min_y, *values)
            max_y = max(max_y, *values)
            violations += sum(value < -species_tolerance or value > 1.0 + species_tolerance for value in values)
        max_sum_error: float | None = max(sum_errors)
    else:
        min_y = max_y = math.nan
        violations = 0
        max_sum_error = None

    rho0 = history.density_kg_m3[0]
    if rho0 != 0.0:
        density_drift = max(abs(rho - rho0) / abs(rho0) for rho in history.density_kg_m3)
    else:
        density_drift = None

    return HistoryValidation(
        monotonic_time=strictly_increasing(history.time_s),
        nonfinite_values=nonfinite,
        max_abs_sumY_error=max_sum_error,
        min_species_mass_fraction=None if math.isnan(min_y) else min_y,
        max_species_mass_fraction=None if math.isnan(max_y) else max_y,
        species_bound_violations=violations,
        max_relative_density_drift=density_drift,
    )
