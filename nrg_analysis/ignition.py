"""Reusable ignition-delay definitions.

The functions compute metrics; they do not decide which definition is the
scientifically preferred one for a particular study.  That choice belongs in
study-specific code and must be documented there.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Mapping

from .chemistry import MolarMassDatabase, molar_concentration
from .io import ReactorHistory
from .timeseries import derivative, maximum, threshold_crossing


@dataclass(frozen=True)
class IgnitionMetric:
    name: str
    time_s: float | None
    sample_time_s: float | None = None
    peak_value: float | None = None
    peak_units: str | None = None
    method: str = ""
    status: str = "diagnostic"
    metadata: Mapping[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        return payload


def max_temperature_rate(history: ReactorHistory, *, status: str = "primary") -> IgnitionMetric:
    rate = derivative(history.time_s, history.temperature_K)
    peak = maximum(history.time_s, rate)
    return IgnitionMetric(
        name="tau_max_dTdt",
        time_s=peak.refined_time,
        sample_time_s=peak.sample_time,
        peak_value=peak.value,
        peak_units="K/s",
        method="time of maximum temperature-rise rate",
        status=status,
    )


def max_pressure_rate(history: ReactorHistory, *, status: str = "secondary") -> IgnitionMetric:
    rate = derivative(history.time_s, history.pressure_Pa)
    peak = maximum(history.time_s, rate)
    return IgnitionMetric(
        name="tau_max_dpdt",
        time_s=peak.refined_time,
        sample_time_s=peak.sample_time,
        peak_value=peak.value,
        peak_units="Pa/s",
        method="time of maximum pressure-rise rate",
        status=status,
    )


def temperature_rise(
    history: ReactorHistory, delta_K: float = 400.0, *, status: str = "secondary"
) -> IgnitionMetric:
    threshold = history.temperature_K[0] + delta_K
    time = threshold_crossing(history.time_s, history.temperature_K, threshold)
    return IgnitionMetric(
        name=f"tau_T0_plus_{delta_K:g}K",
        time_s=time,
        method=f"first crossing of T0 + {delta_K:g} K",
        status=status,
        metadata={"threshold_temperature_K": threshold},
    )


def species_growth(
    history: ReactorHistory,
    species: str,
    molar_masses: MolarMassDatabase | None = None,
    *,
    use_molar_concentration: bool = True,
    status: str = "literature_supported",
) -> IgnitionMetric:
    y = history.species_mass_fraction(species)
    if use_molar_concentration:
        if molar_masses is None:
            raise ValueError("molar masses required for concentration-based species-growth metric")
        signal = molar_concentration(history.density_kg_m3, y, species, molar_masses)
        units = "kmol/m3/s"
        signal_name = f"[{species}]"
    else:
        signal = list(y)
        units = "1/s"
        signal_name = f"Y({species})"
    rate = derivative(history.time_s, signal)
    peak = maximum(history.time_s, rate)
    return IgnitionMetric(
        name=f"tau_max_d{species}dt",
        time_s=peak.refined_time,
        sample_time_s=peak.sample_time,
        peak_value=peak.value,
        peak_units=units,
        method=f"time of maximum growth rate of {signal_name}",
        status=status,
        metadata={"species": species, "signal": signal_name},
    )


def species_peak(
    history: ReactorHistory,
    species: str,
    molar_masses: MolarMassDatabase | None = None,
    *,
    use_molar_concentration: bool = True,
    status: str = "diagnostic",
) -> IgnitionMetric:
    y = history.species_mass_fraction(species)
    if use_molar_concentration:
        if molar_masses is None:
            raise ValueError("molar masses required for concentration-based species-peak metric")
        signal = molar_concentration(history.density_kg_m3, y, species, molar_masses)
        units = "kmol/m3"
        signal_name = f"[{species}]"
    else:
        signal = list(y)
        units = "mass fraction"
        signal_name = f"Y({species})"
    peak = maximum(history.time_s, signal)
    return IgnitionMetric(
        name=f"tau_peak_{species}",
        time_s=peak.refined_time,
        sample_time_s=peak.sample_time,
        peak_value=peak.value,
        peak_units=units,
        method=f"time of maximum {signal_name}",
        status=status,
        metadata={"species": species, "signal": signal_name},
    )


def default_ignition_suite(
    history: ReactorHistory,
    molar_masses: MolarMassDatabase | None = None,
    *,
    delta_T_K: float = 400.0,
    radical_species: tuple[str, ...] = ("OH", "H"),
) -> dict[str, IgnitionMetric]:
    metrics = [
        max_temperature_rate(history),
        max_pressure_rate(history),
        temperature_rise(history, delta_T_K),
    ]
    for species in radical_species:
        if any(name.upper() == species.upper() for name in history.species_names):
            if molar_masses is not None and molar_masses.has(species):
                metrics.append(species_growth(history, species, molar_masses))
                metrics.append(species_peak(history, species, molar_masses))
            else:
                metrics.append(
                    species_growth(history, species, None, use_molar_concentration=False, status="diagnostic")
                )
                metrics.append(
                    species_peak(history, species, None, use_molar_concentration=False, status="diagnostic")
                )
    return {metric.name: metric for metric in metrics}
