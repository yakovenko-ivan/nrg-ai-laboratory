"""Deterministic post-ignition quasistationarity evaluation for 0D reactors.

This module contains no campaign-specific interpretation.  It answers one
operational question: has the latest physical-time window reached a stable
post-ignition state according to a reviewed trusted profile?
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import asdict, dataclass
import hashlib
import json
import math
from pathlib import Path
from statistics import fmean
from typing import Any

from .io import ReactorHistory, load_reactor_history


@dataclass(frozen=True)
class QuasistationaryProfile:
    name: str
    history_file: str
    check_wall_interval_s: float
    window_duration_s: float
    min_window_points: int
    min_temperature_rise_K: float
    fuel_species: str
    min_fuel_consumed_fraction: float
    activation_mode: str
    relative_temperature_span: float
    relative_pressure_span: float
    relative_density_span: float
    max_species_mass_fraction_span: float
    max_sumY_error: float
    required_run_control_modes: tuple[str, ...]
    description: str = ""

    @classmethod
    def from_mapping(cls, name: str, data: dict[str, Any]) -> "QuasistationaryProfile":
        activation = data.get("activation", {})
        window = data.get("window", {})
        tolerances = data.get("tolerances", {})
        modes = tuple(str(x).lower() for x in data.get("required_run_control_modes", ["wall_time"]))
        profile = cls(
            name=name,
            history_file=str(data.get("history_file", "reactor_history.dat")),
            check_wall_interval_s=float(data.get("check_wall_interval_s", 5.0)),
            window_duration_s=float(window.get("duration_s", 1.0e-4)),
            min_window_points=int(window.get("min_points", 50)),
            min_temperature_rise_K=float(activation.get("minimum_temperature_rise_K", 200.0)),
            fuel_species=str(activation.get("fuel_species", "H2")),
            min_fuel_consumed_fraction=float(
                activation.get("minimum_fuel_consumed_fraction", 0.05)
            ),
            activation_mode=str(activation.get("mode", "any")).lower(),
            relative_temperature_span=float(
                tolerances.get("relative_temperature_span", 1.0e-6)
            ),
            relative_pressure_span=float(
                tolerances.get("relative_pressure_span", 1.0e-6)
            ),
            relative_density_span=float(
                tolerances.get("relative_density_span", 1.0e-8)
            ),
            max_species_mass_fraction_span=float(
                tolerances.get("max_species_mass_fraction_span", 1.0e-8)
            ),
            max_sumY_error=float(tolerances.get("max_sumY_error", 1.0e-8)),
            required_run_control_modes=modes,
            description=str(data.get("description", "")),
        )
        profile.validate()
        return profile

    def validate(self) -> None:
        if self.check_wall_interval_s <= 0:
            raise ValueError("check_wall_interval_s must be positive")
        if self.window_duration_s <= 0:
            raise ValueError("window duration must be positive")
        if self.min_window_points < 2:
            raise ValueError("min_window_points must be at least 2")
        if self.min_temperature_rise_K <= 0:
            raise ValueError("minimum_temperature_rise_K must be positive")
        if not 0.0 < self.min_fuel_consumed_fraction <= 1.0:
            raise ValueError("minimum_fuel_consumed_fraction must lie in (0,1]")
        if self.activation_mode not in {"any", "all"}:
            raise ValueError("activation.mode must be 'any' or 'all'")
        for name, value in (
            ("relative_temperature_span", self.relative_temperature_span),
            ("relative_pressure_span", self.relative_pressure_span),
            ("relative_density_span", self.relative_density_span),
            ("max_species_mass_fraction_span", self.max_species_mass_fraction_span),
            ("max_sumY_error", self.max_sumY_error),
        ):
            if value < 0:
                raise ValueError(f"{name} must be nonnegative")
        if not self.required_run_control_modes:
            raise ValueError("required_run_control_modes must not be empty")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class QuasistationaryResult:
    profile: str
    status: str
    reached: bool
    history_rows: int
    history_end_time_s: float | None
    window_start_time_s: float | None
    window_end_time_s: float | None
    window_duration_s: float | None
    window_points: int
    initial_temperature_K: float | None
    product_temperature_K: float | None
    product_pressure_Pa: float | None
    product_density_kg_m3: float | None
    product_species_mass_fractions: dict[str, float]
    temperature_rise_K: float | None
    fuel_consumed_fraction: float | None
    activation_temperature: bool
    activation_fuel: bool | None
    activation_satisfied: bool
    relative_temperature_span: float | None
    relative_pressure_span: float | None
    relative_density_span: float | None
    max_species_mass_fraction_span: float | None
    species_with_max_span: str | None
    max_sumY_error: float | None
    criteria: dict[str, bool]
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def profile_file_sha256(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load_profiles(path: str | Path) -> dict[str, QuasistationaryProfile]:
    path = Path(path)
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if int(data.get("schema_version", 0)) != 1:
        raise ValueError(f"unsupported termination-profile schema in {path}")
    raw = data.get("profiles")
    if not isinstance(raw, dict) or not raw:
        raise ValueError(f"no profiles defined in {path}")
    return {
        str(name): QuasistationaryProfile.from_mapping(str(name), dict(value))
        for name, value in raw.items()
    }


def load_profile(path: str | Path, name: str) -> QuasistationaryProfile:
    profiles = load_profiles(path)
    try:
        return profiles[name]
    except KeyError as exc:
        raise KeyError(
            f"termination profile {name!r} not found; available: {', '.join(sorted(profiles))}"
        ) from exc


def _mean(values: tuple[float, ...] | list[float]) -> float:
    return float(fmean(values))


def _relative_span(values: list[float], floor: float) -> float:
    mean = _mean(values)
    return (max(values) - min(values)) / max(abs(mean), floor)


def _empty_result(profile: QuasistationaryProfile, status: str, reason: str, rows: int = 0,
                  end_time: float | None = None) -> QuasistationaryResult:
    return QuasistationaryResult(
        profile=profile.name,
        status=status,
        reached=False,
        history_rows=rows,
        history_end_time_s=end_time,
        window_start_time_s=None,
        window_end_time_s=end_time,
        window_duration_s=None,
        window_points=0,
        initial_temperature_K=None,
        product_temperature_K=None,
        product_pressure_Pa=None,
        product_density_kg_m3=None,
        product_species_mass_fractions={},
        temperature_rise_K=None,
        fuel_consumed_fraction=None,
        activation_temperature=False,
        activation_fuel=None,
        activation_satisfied=False,
        relative_temperature_span=None,
        relative_pressure_span=None,
        relative_density_span=None,
        max_species_mass_fraction_span=None,
        species_with_max_span=None,
        max_sumY_error=None,
        criteria={},
        reason=reason,
    )


def evaluate_history(
    history: ReactorHistory,
    profile: QuasistationaryProfile,
) -> QuasistationaryResult:
    """Evaluate the latest complete physical-time window of a reactor history."""

    rows = history.rows
    if rows < 2:
        return _empty_result(
            profile,
            "insufficient_history",
            "history contains fewer than two samples",
            rows,
            history.time_s[-1] if rows else None,
        )
    if any(not math.isfinite(t) for t in history.time_s):
        return _empty_result(profile, "invalid_history", "non-finite time values", rows, history.time_s[-1])
    if any(b <= a for a, b in zip(history.time_s, history.time_s[1:])):
        return _empty_result(profile, "invalid_history", "time is not strictly increasing", rows, history.time_s[-1])

    end_time = history.time_s[-1]
    target_start = end_time - profile.window_duration_s
    # Choose the last sample at or before the requested physical-time
    # boundary. This guarantees that the accepted window spans at least
    # window_duration_s even when binary floating-point places an exact
    # grid point infinitesimally above target_start.
    start = max(0, bisect_right(history.time_s, target_start) - 1)
    if start >= rows - 1:
        return _empty_result(
            profile, "insufficient_window", "physical-time window has fewer than two points", rows, end_time
        )

    actual_duration = end_time - history.time_s[start]
    points = rows - start
    if actual_duration < profile.window_duration_s * (1.0 - 1.0e-9) or points < profile.min_window_points:
        result = _empty_result(
            profile,
            "insufficient_window",
            (
                f"latest window duration/points insufficient: duration={actual_duration:.6g} s, "
                f"points={points}"
            ),
            rows,
            end_time,
        )
        return QuasistationaryResult(
            **{
                **result.to_dict(),
                "window_start_time_s": history.time_s[start],
                "window_duration_s": actual_duration,
                "window_points": points,
                "initial_temperature_K": history.temperature_K[0],
            }
        )

    sl = slice(start, rows)
    t_values = list(history.temperature_K[sl])
    p_values = list(history.pressure_Pa[sl])
    rho_values = list(history.density_kg_m3[sl])

    product_T = _mean(t_values)
    product_p = _mean(p_values)
    product_rho = _mean(rho_values)
    T0 = history.temperature_K[0]
    temperature_rise = product_T - T0
    activation_temperature = temperature_rise >= profile.min_temperature_rise_K

    fuel_fraction: float | None = None
    activation_fuel: bool | None = None
    try:
        fuel = history.species_mass_fraction(profile.fuel_species)
    except KeyError:
        fuel = None
    if fuel is not None:
        y0 = fuel[0]
        y_window = _mean(list(fuel[sl]))
        if abs(y0) > 1.0e-30:
            fuel_fraction = max(0.0, (y0 - y_window) / abs(y0))
            activation_fuel = fuel_fraction >= profile.min_fuel_consumed_fraction
        else:
            activation_fuel = False

    activation_terms = [activation_temperature]
    if activation_fuel is not None:
        activation_terms.append(activation_fuel)
    activation_satisfied = (
        any(activation_terms) if profile.activation_mode == "any" else all(activation_terms)
    )

    rel_T = _relative_span(t_values, 1.0)
    rel_p = _relative_span(p_values, 1.0)
    rel_rho = _relative_span(rho_values, 1.0e-30)

    product_species: dict[str, float] = {}
    max_species_span = 0.0
    max_species_name: str | None = None
    max_sumY_error = 0.0
    species_series: list[tuple[str, tuple[float, ...]]] = []
    for species in history.species_names:
        series = history.species_mass_fraction(species)
        species_series.append((species, series))
        values = list(series[sl])
        product_species[species] = _mean(values)
        span = max(values) - min(values)
        if max_species_name is None or span > max_species_span:
            max_species_span = span
            max_species_name = species

    if species_series:
        for i in range(start, rows):
            max_sumY_error = max(
                max_sumY_error,
                abs(sum(series[i] for _name, series in species_series) - 1.0),
            )
    else:
        max_species_span = 0.0
        max_sumY_error = 0.0

    criteria = {
        "activation": activation_satisfied,
        "temperature_span": rel_T <= profile.relative_temperature_span,
        "pressure_span": rel_p <= profile.relative_pressure_span,
        "density_span": rel_rho <= profile.relative_density_span,
        "species_span": max_species_span <= profile.max_species_mass_fraction_span,
        "sumY_closure": max_sumY_error <= profile.max_sumY_error,
    }
    reached = all(criteria.values())
    if not activation_satisfied:
        status = "pre_ignition"
        reason = (
            f"post-ignition gate not satisfied: ΔT={temperature_rise:.6g} K, "
            f"fuel_consumed_fraction={fuel_fraction}"
        )
    elif reached:
        status = "quasistationary"
        reason = "all post-ignition stability criteria are satisfied over the latest window"
    else:
        status = "not_quasistationary"
        failed = ", ".join(name for name, ok in criteria.items() if not ok)
        reason = f"post-ignition gate satisfied but stability criteria failed: {failed}"

    return QuasistationaryResult(
        profile=profile.name,
        status=status,
        reached=reached,
        history_rows=rows,
        history_end_time_s=end_time,
        window_start_time_s=history.time_s[start],
        window_end_time_s=end_time,
        window_duration_s=actual_duration,
        window_points=points,
        initial_temperature_K=T0,
        product_temperature_K=product_T,
        product_pressure_Pa=product_p,
        product_density_kg_m3=product_rho,
        product_species_mass_fractions=product_species,
        temperature_rise_K=temperature_rise,
        fuel_consumed_fraction=fuel_fraction,
        activation_temperature=activation_temperature,
        activation_fuel=activation_fuel,
        activation_satisfied=activation_satisfied,
        relative_temperature_span=rel_T,
        relative_pressure_span=rel_p,
        relative_density_span=rel_rho,
        max_species_mass_fraction_span=max_species_span,
        species_with_max_span=max_species_name,
        max_sumY_error=max_sumY_error,
        criteria=criteria,
        reason=reason,
    )


def evaluate_case(
    case_path: str | Path,
    profile: QuasistationaryProfile,
) -> QuasistationaryResult:
    history = load_reactor_history(case_path, profile.history_file)
    return evaluate_history(history, profile)
