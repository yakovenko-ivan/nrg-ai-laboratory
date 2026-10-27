"""Canonical stdlib-only quasistationarity evaluation for NRG 0D reactors.

This module is intentionally self-contained so that the exact same scientific
criterion can be used by the laboratory analysis layer and copied verbatim into
a portable execution bundle.  It contains no third-party dependencies.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import asdict, dataclass
import hashlib
import json
import math
from pathlib import Path
import re
from statistics import fmean
from typing import Any, Mapping, Sequence


NUMBER_RE = re.compile(r"[-+]?(?:(?:\d+\.\d*)|(?:\.\d+)|(?:\d+))(?:[EeDd][-+]?\d+)?")
_METADATA_RE = re.compile(r"^\s*(?:TITLE|VARIABLES)\s*=", re.IGNORECASE)
TIME_FACTORS = {
    "seconds": 1.0,
    "milliseconds": 1.0e-3,
    "microseconds": 1.0e-6,
    "nanoseconds": 1.0e-9,
}


class TransientHistoryError(ValueError):
    """History exists but is not yet complete enough for an online read."""


@dataclass(frozen=True)
class CoreProfile:
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
    def from_mapping(cls, name: str, data: Mapping[str, Any]) -> "CoreProfile":
        activation = data.get("activation", {})
        window = data.get("window", {})
        tolerances = data.get("tolerances", {})
        modes = tuple(str(x).lower() for x in data.get("required_run_control_modes", ["wall_time"]))
        profile = cls(
            name=str(name),
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
        if not self.history_file.strip():
            raise ValueError("history_file must not be empty")
        if self.check_wall_interval_s <= 0:
            raise ValueError("check_wall_interval_s must be positive")
        if self.window_duration_s <= 0:
            raise ValueError("window duration must be positive")
        if self.min_window_points < 2:
            raise ValueError("min_window_points must be at least 2")
        if self.min_temperature_rise_K <= 0:
            raise ValueError("minimum_temperature_rise_K must be positive")
        if not self.fuel_species.strip():
            raise ValueError("fuel_species must not be empty")
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
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be finite and nonnegative")
        if not self.required_run_control_modes:
            raise ValueError("required_run_control_modes must not be empty")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_registry_mapping(self) -> dict[str, Any]:
        return {
            "description": self.description,
            "history_file": self.history_file,
            "check_wall_interval_s": self.check_wall_interval_s,
            "required_run_control_modes": list(self.required_run_control_modes),
            "activation": {
                "mode": self.activation_mode,
                "minimum_temperature_rise_K": self.min_temperature_rise_K,
                "fuel_species": self.fuel_species,
                "minimum_fuel_consumed_fraction": self.min_fuel_consumed_fraction,
            },
            "window": {
                "duration_s": self.window_duration_s,
                "min_points": self.min_window_points,
            },
            "tolerances": {
                "relative_temperature_span": self.relative_temperature_span,
                "relative_pressure_span": self.relative_pressure_span,
                "relative_density_span": self.relative_density_span,
                "max_species_mass_fraction_span": self.max_species_mass_fraction_span,
                "max_sumY_error": self.max_sumY_error,
            },
        }


@dataclass(frozen=True)
class CoreResult:
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


def _canonical_json_sha256(payload: Any) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def profile_sha256(profile: CoreProfile) -> str:
    return _canonical_json_sha256({"name": profile.name, **profile.to_registry_mapping()})


def profile_file_sha256(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load_profiles(path: str | Path) -> dict[str, CoreProfile]:
    p = Path(path)
    data = json.loads(p.read_text(encoding="utf-8-sig"))
    if int(data.get("schema_version", 0)) != 1:
        raise ValueError(f"unsupported termination-profile schema in {p}")
    raw = data.get("profiles")
    if not isinstance(raw, dict) or not raw:
        raise ValueError(f"no profiles defined in {p}")
    return {
        str(name): CoreProfile.from_mapping(str(name), dict(value))
        for name, value in raw.items()
    }


def load_profile(path: str | Path, name: str) -> CoreProfile:
    profiles = load_profiles(path)
    try:
        return profiles[name]
    except KeyError as exc:
        raise KeyError(
            f"termination profile {name!r} not found; available: {', '.join(sorted(profiles))}"
        ) from exc


def _mean(values: Sequence[float]) -> float:
    return float(fmean(values))


def _relative_span(values: Sequence[float], floor: float) -> float:
    mean = _mean(values)
    return (max(values) - min(values)) / max(abs(mean), floor)


def _empty_result(
    profile: CoreProfile,
    status: str,
    reason: str,
    rows: int = 0,
    end_time: float | None = None,
) -> CoreResult:
    return CoreResult(
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


def _validate_series_lengths(
    n: int,
    temperature_K: Sequence[float],
    pressure_Pa: Sequence[float],
    density_kg_m3: Sequence[float],
    species_names: Sequence[str],
    species_series: Mapping[str, Sequence[float]],
) -> str | None:
    if len(temperature_K) != n or len(pressure_Pa) != n or len(density_kg_m3) != n:
        return "thermodynamic series lengths do not match time_s"
    for name in species_names:
        if name not in species_series:
            return f"species series missing for {name!r}"
        if len(species_series[name]) != n:
            return f"species series length mismatch for {name!r}"
    return None


def evaluate(
    time_s: Sequence[float],
    temperature_K: Sequence[float],
    pressure_Pa: Sequence[float],
    density_kg_m3: Sequence[float],
    species_names: Sequence[str],
    species_series: Mapping[str, Sequence[float]],
    profile: CoreProfile,
) -> CoreResult:
    """Evaluate the latest complete physical-time window using one canonical algorithm."""
    profile.validate()
    rows = len(time_s)
    end_time = float(time_s[-1]) if rows else None
    if rows < 2:
        return _empty_result(
            profile,
            "insufficient_history",
            "history contains fewer than two samples",
            rows,
            end_time,
        )
    mismatch = _validate_series_lengths(
        rows, temperature_K, pressure_Pa, density_kg_m3, species_names, species_series
    )
    if mismatch:
        return _empty_result(profile, "invalid_history", mismatch, rows, end_time)
    all_series: list[Sequence[float]] = [time_s, temperature_K, pressure_Pa, density_kg_m3]
    all_series.extend(species_series[name] for name in species_names)
    if any(not math.isfinite(float(v)) for series in all_series for v in series):
        return _empty_result(profile, "invalid_history", "non-finite history values", rows, end_time)
    if any(float(b) <= float(a) for a, b in zip(time_s, time_s[1:])):
        return _empty_result(profile, "invalid_history", "time is not strictly increasing", rows, end_time)

    times = tuple(float(x) for x in time_s)
    end_time = times[-1]
    target_start = end_time - profile.window_duration_s
    start = max(0, bisect_right(times, target_start) - 1)
    if start >= rows - 1:
        return _empty_result(
            profile,
            "insufficient_window",
            "physical-time window has fewer than two points",
            rows,
            end_time,
        )

    actual_duration = end_time - times[start]
    points = rows - start
    if actual_duration < profile.window_duration_s * (1.0 - 1.0e-9) or points < profile.min_window_points:
        result = _empty_result(
            profile,
            "insufficient_window",
            f"latest window duration/points insufficient: duration={actual_duration:.6g} s, points={points}",
            rows,
            end_time,
        )
        data = result.to_dict()
        data.update(
            window_start_time_s=times[start],
            window_duration_s=actual_duration,
            window_points=points,
            initial_temperature_K=float(temperature_K[0]),
        )
        return CoreResult(**data)

    t_values = [float(x) for x in temperature_K[start:rows]]
    p_values = [float(x) for x in pressure_Pa[start:rows]]
    rho_values = [float(x) for x in density_kg_m3[start:rows]]

    product_T = _mean(t_values)
    product_p = _mean(p_values)
    product_rho = _mean(rho_values)
    T0 = float(temperature_K[0])
    temperature_rise = product_T - T0
    activation_temperature = temperature_rise >= profile.min_temperature_rise_K

    fuel_fraction: float | None = None
    activation_fuel: bool | None = None
    fuel_key = next((name for name in species_names if name.upper() == profile.fuel_species.upper()), None)
    if fuel_key is not None:
        fuel = species_series[fuel_key]
        y0 = float(fuel[0])
        y_window = _mean([float(x) for x in fuel[start:rows]])
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
    ordered_species: list[tuple[str, Sequence[float]]] = []
    for species in species_names:
        series = species_series[species]
        ordered_species.append((species, series))
        values = [float(x) for x in series[start:rows]]
        product_species[species] = _mean(values)
        span = max(values) - min(values)
        if max_species_name is None or span > max_species_span:
            max_species_span = span
            max_species_name = species

    if ordered_species:
        for i in range(start, rows):
            max_sumY_error = max(
                max_sumY_error,
                abs(sum(float(series[i]) for _name, series in ordered_species) - 1.0),
            )

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

    return CoreResult(
        profile=profile.name,
        status=status,
        reached=reached,
        history_rows=rows,
        history_end_time_s=end_time,
        window_start_time_s=times[start],
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


def _string_value(text: str, name: str) -> str | None:
    pattern = re.compile(
        rf"^\s*{re.escape(name)}\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([^,/\r\n]+))",
        re.IGNORECASE | re.MULTILINE,
    )
    match = pattern.search(text)
    if not match:
        return None
    for group in match.groups():
        if group is not None:
            return group.strip()
    return None


def _int_value(text: str, name: str) -> int | None:
    value = _string_value(text, name)
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _repeated_quoted_values(text: str, name: str) -> list[str]:
    pattern = re.compile(
        rf"^\s*{re.escape(name)}\s*=\s*(?:\"([^\"]*)\"|'([^']*)')",
        re.IGNORECASE | re.MULTILINE,
    )
    values: list[str] = []
    for match in pattern.finditer(text):
        values.append(next(group for group in match.groups() if group is not None).strip())
    return values


def _species_from_field(field_name: str) -> str | None:
    match = re.fullmatch(r"specie_mass_fraction\((.+)\)", field_name.strip(), flags=re.IGNORECASE)
    return match.group(1).strip() if match else None


def _parse_number_line(line: str) -> list[float]:
    return [float(m.group(0).replace("D", "E").replace("d", "e")) for m in NUMBER_RE.finditer(line)]


def _is_metadata_line(line: str) -> bool:
    return bool(_METADATA_RE.match(line))


def _parse_numeric_row(line: str, path: Path, line_number: int) -> list[float]:
    """Parse one complete numeric record without accepting stray text."""
    values: list[float] = []
    cursor = 0
    for match in NUMBER_RE.finditer(line):
        gap = line[cursor : match.start()]
        if gap.strip():
            raise ValueError(f"{path}:{line_number}: malformed numeric row near {gap.strip()!r}")
        token = match.group(0)
        try:
            values.append(float(token.replace("D", "E").replace("d", "e")))
        except ValueError as exc:
            raise ValueError(f"{path}:{line_number}: invalid numeric token {token!r}") from exc
        cursor = match.end()
    tail = line[cursor:]
    if tail.strip():
        raise ValueError(f"{path}:{line_number}: malformed numeric row near {tail.strip()!r}")
    if not values:
        raise ValueError(f"{path}:{line_number}: no numeric values")
    return values


def _find_postprocessor_setup(case_path: Path, output_file: str) -> tuple[str, list[str]]:
    candidates = sorted((case_path / "task_setup").glob("post_processor*.inf"))
    for path in candidates:
        text = path.read_text(encoding="utf-8", errors="replace")
        configured = _string_value(text, "post_processor_output_file")
        if configured and Path(configured).name.lower() == Path(output_file).name.lower():
            units = _string_value(text, "save_time_units")
            if not units:
                raise ValueError(f"{path}: SAVE_TIME_UNITS missing")
            fields = _repeated_quoted_values(text, "field_name")
            expected = _int_value(text, "operations_number")
            if expected is not None and len(fields) != expected:
                raise ValueError(
                    f"{path}: expected {expected} operations but found {len(fields)} FIELD_NAME entries"
                )
            if not fields:
                raise ValueError(f"{path}: no FIELD_NAME entries found")
            return units.lower(), fields
    raise FileNotFoundError(
        f"no task_setup/post_processor*.inf matches output {output_file!r} in {case_path}"
    )


def _read_numeric_rows_online(path: Path) -> list[list[float]]:
    """Read complete numeric rows while tolerating one partially written final record."""
    if not path.is_file():
        raise FileNotFoundError(path)
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    parsed: list[list[float]] = []
    data_started = False
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        if _is_metadata_line(line):
            if data_started:
                raise ValueError(f"{path}:{index + 1}: metadata line after numerical data")
            continue
        is_last = not any(candidate.strip() for candidate in lines[index + 1 :])
        try:
            row = _parse_numeric_row(line, path, index + 1)
        except ValueError:
            if not is_last:
                raise
            # Only a final record without a newline is assumed to be mid-write.
            # A newline-terminated malformed record is a schema/data error.
            if not text.endswith(("\n", "\r")) and _parse_number_line(line):
                break
            raise
        parsed.append(row)
        data_started = True

    if not parsed:
        raise TransientHistoryError(f"history contains no complete numerical rows yet: {path}")
    width = len(parsed[0])
    bad = [i for i, row in enumerate(parsed) if len(row) != width]
    if bad:
        # Concurrent append is allowed to leave only the final row incomplete.
        last = len(parsed) - 1
        if bad == [last] and len(parsed[last]) < width:
            parsed = parsed[:-1]
        else:
            raise ValueError(f"inconsistent numerical row widths in {path}; bad rows={bad[:8]}")
    if not parsed:
        raise TransientHistoryError(f"history has only an incomplete trailing record: {path}")
    return parsed


def parse_reactor_history(case_path: str | Path, output_file: str = "reactor_history.dat") -> dict[str, Any]:
    case = Path(case_path).resolve()
    raw_path = case / output_file
    units, operation_fields = _find_postprocessor_setup(case, raw_path.name)
    if units not in TIME_FACTORS:
        raise ValueError(f"unsupported NRG postprocessor time units {units!r}")
    rows = _read_numeric_rows_online(raw_path)
    dimensions = len(rows[0]) - 1 - len(operation_fields)
    if dimensions < 1 or dimensions > 3:
        raise ValueError(
            "cannot infer NRG postprocessor dimensionality: "
            f"row_width={len(rows[0])}, operations={len(operation_fields)}"
        )
    expected_width = 1 + len(operation_fields) + dimensions
    if len(rows[0]) != expected_width:
        raise ValueError(f"unexpected reactor-history width {len(rows[0])}; expected {expected_width}")

    factor = TIME_FACTORS[units]
    time_s = tuple(row[0] * factor for row in rows)
    # Native layout: time, operation_1, coordinates..., operation_2, ...
    raw_series: dict[str, tuple[float, ...]] = {}
    raw_series[operation_fields[0]] = tuple(row[1] for row in rows)
    for op_index in range(1, len(operation_fields)):
        col = 1 + dimensions + op_index
        raw_series[operation_fields[op_index]] = tuple(row[col] for row in rows)

    def find_field(name: str) -> tuple[float, ...]:
        for field, series in raw_series.items():
            if field.strip().lower() == name.lower():
                return series
        raise ValueError(f"reactor history lacks required observable {name!r}")

    species_names: list[str] = []
    species_series: dict[str, tuple[float, ...]] = {}
    for field, series in raw_series.items():
        species = _species_from_field(field)
        if species is not None:
            if species in species_series:
                raise ValueError(f"duplicate species field in reactor history: {species}")
            species_names.append(species)
            species_series[species] = series

    return {
        "time_s": time_s,
        "temperature_K": find_field("temperature"),
        "pressure_Pa": find_field("pressure"),
        "density_kg_m3": find_field("density"),
        "species_names": tuple(species_names),
        "species_series": species_series,
    }


def evaluate_case(case_path: str | Path, profile: CoreProfile) -> CoreResult:
    history = parse_reactor_history(case_path, profile.history_file)
    return evaluate(profile=profile, **history)
