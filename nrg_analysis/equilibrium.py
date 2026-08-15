"""Final-window stationarity and equilibrium-state diagnostics."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from statistics import fmean
from typing import Any, Mapping

from .chemistry import MolarMassDatabase, mass_to_mole_fractions
from .io import ReactorHistory
from .timeseries import derivative, final_window_index, max_abs, mean_abs, relative_span, rms
from .validation import validate_history


@dataclass(frozen=True)
class EquilibriumCriteria:
    final_window_fraction: float = 0.10
    final_window_duration_s: float | None = None
    max_relative_temperature_span: float = 1.0e-6
    max_relative_pressure_span: float = 1.0e-6
    max_species_mass_fraction_span: float = 1.0e-8
    max_sumY_error: float = 1.0e-9


@dataclass(frozen=True)
class EquilibriumAssessment:
    reached: bool
    window_start_s: float
    window_end_s: float
    temperature_mean_K: float
    pressure_mean_Pa: float
    density_mean_kg_m3: float
    relative_temperature_span: float
    relative_pressure_span: float
    max_species_mass_fraction_span: float
    max_abs_sumY_error: float | None
    max_abs_dTdt_K_per_s: float | None
    mean_abs_dTdt_K_per_s: float | None
    rms_dTdt_K_per_s: float | None
    final_abs_dTdt_K_per_s: float | None
    max_abs_dpdt_Pa_per_s: float | None
    max_abs_dYdt_per_s: float | None
    mass_fractions: Mapping[str, float]
    mole_fractions: Mapping[str, float] | None
    criteria: EquilibriumCriteria

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        return payload


def assess_equilibrium(
    history: ReactorHistory,
    criteria: EquilibriumCriteria | None = None,
    molar_masses: MolarMassDatabase | None = None,
) -> EquilibriumAssessment:
    criteria = criteria or EquilibriumCriteria()
    w0 = final_window_index(
        history.time_s,
        fraction=None if criteria.final_window_duration_s is not None else criteria.final_window_fraction,
        duration_s=criteria.final_window_duration_s,
    )
    time = history.time_s[w0:]
    temperature = history.temperature_K[w0:]
    pressure = history.pressure_Pa[w0:]
    density = history.density_kg_m3[w0:]

    dTdt = derivative(time, temperature)
    dpdt = derivative(time, pressure)

    means: dict[str, float] = {}
    max_species_span = 0.0
    max_dYdt = 0.0
    for species in history.species_names:
        values = history.species_mass_fraction(species)[w0:]
        means[species] = fmean(values)
        max_species_span = max(max_species_span, max(values) - min(values))
        local = max_abs(derivative(time, values))
        if local is not None:
            max_dYdt = max(max_dYdt, local)

    validation = validate_history(history)
    rel_T = relative_span(temperature)
    rel_p = relative_span(pressure)
    reached = (
        validation.monotonic_time
        and validation.nonfinite_values == 0
        and rel_T <= criteria.max_relative_temperature_span
        and rel_p <= criteria.max_relative_pressure_span
        and max_species_span <= criteria.max_species_mass_fraction_span
        and (
            validation.max_abs_sumY_error is None
            or validation.max_abs_sumY_error <= criteria.max_sumY_error
        )
    )

    mole = mass_to_mole_fractions(means, molar_masses) if molar_masses is not None else None

    return EquilibriumAssessment(
        reached=reached,
        window_start_s=time[0],
        window_end_s=time[-1],
        temperature_mean_K=fmean(temperature),
        pressure_mean_Pa=fmean(pressure),
        density_mean_kg_m3=fmean(density),
        relative_temperature_span=rel_T,
        relative_pressure_span=rel_p,
        max_species_mass_fraction_span=max_species_span,
        max_abs_sumY_error=validation.max_abs_sumY_error,
        max_abs_dTdt_K_per_s=max_abs(dTdt),
        mean_abs_dTdt_K_per_s=mean_abs(dTdt),
        rms_dTdt_K_per_s=rms(dTdt),
        final_abs_dTdt_K_per_s=abs(dTdt[-1]) if dTdt else None,
        max_abs_dpdt_Pa_per_s=max_abs(dpdt),
        max_abs_dYdt_per_s=max_dYdt,
        mass_fractions=means,
        mole_fractions=mole,
        criteria=criteria,
    )
