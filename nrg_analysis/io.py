"""NRG result readers and canonical reactor-history representation."""

from __future__ import annotations

from dataclasses import dataclass
import csv
import math
from pathlib import Path
import re
from typing import Iterable, Mapping

from . import namelist

NUMBER_RE = re.compile(r"[-+]?(?:(?:\d+\.\d*)|(?:\.\d+)|(?:\d+))(?:[EeDd][-+]?\d+)?")
TIME_FACTORS = {
    "seconds": 1.0,
    "milliseconds": 1.0e-3,
    "microseconds": 1.0e-6,
    "nanoseconds": 1.0e-9,
}
_COORD_NAMES = ("cell_i", "cell_j", "cell_k")


def _sanitize(name: str) -> str:
    """Create a collision-resistant canonical token.

    Alphanumeric ASCII characters are preserved. Every other character,
    including ``*``, ``+``, parentheses, whitespace, and underscore, is encoded
    by Unicode code point. Thus chemically distinct names such as ``OH`` and
    ``OH*`` cannot collapse to the same observable name.
    """
    value: list[str] = []
    for char in name.strip():
        if char.isascii() and char.isalnum():
            value.append(char)
        else:
            value.append(f"_x{ord(char):X}_")
    return "".join(value) or "unknown"


def canonical_operation_name(field_name: str) -> str:
    lower = field_name.strip().lower()
    if lower == "temperature":
        return "temperature_K"
    if lower == "pressure":
        return "pressure_Pa"
    if lower == "density":
        return "density_kg_m3"
    match = re.fullmatch(r"specie_mass_fraction\((.+)\)", field_name.strip(), flags=re.IGNORECASE)
    if match:
        return "Y_" + _sanitize(match.group(1))
    return _sanitize(field_name)


def species_from_field(field_name: str) -> str | None:
    match = re.fullmatch(r"specie_mass_fraction\((.+)\)", field_name.strip(), flags=re.IGNORECASE)
    return match.group(1).strip() if match else None


def parse_number_line(line: str) -> list[float]:
    return [float(m.group(0).replace("D", "E").replace("d", "e")) for m in NUMBER_RE.finditer(line)]


def read_numeric_rows(path: str | Path) -> list[list[float]]:
    rows: list[list[float]] = []
    path = Path(path)
    with path.open(encoding="utf-8", errors="replace") as stream:
        for line in stream:
            if not line.strip():
                continue
            values = parse_number_line(line)
            if values:
                rows.append(values)
    if not rows:
        raise ValueError(f"no numerical rows in {path}")
    width = len(rows[0])
    if any(len(row) != width for row in rows):
        raise ValueError(f"inconsistent numerical row widths in {path}")
    return rows


def find_postprocessor_setup(case_path: str | Path, output_file: str) -> tuple[Path, str, list[str]]:
    case_path = Path(case_path)
    candidates = sorted((case_path / "task_setup").glob("post_processor*.inf"))
    for path in candidates:
        text = namelist.read(path)
        configured = namelist.string_value(text, "post_processor_output_file")
        if configured and Path(configured).name.lower() == Path(output_file).name.lower():
            units = namelist.string_value(text, "save_time_units")
            if not units:
                raise ValueError(f"{path}: SAVE_TIME_UNITS missing")
            fields = namelist.repeated_quoted_values(text, "field_name")
            expected = namelist.int_value(text, "operations_number")
            if expected is not None and len(fields) != expected:
                raise ValueError(
                    f"{path}: expected {expected} operations but found {len(fields)} FIELD_NAME entries"
                )
            if not fields:
                raise ValueError(f"{path}: no FIELD_NAME entries found")
            return path, units.lower(), fields
    raise FileNotFoundError(
        f"no task_setup/post_processor*.inf matches output {output_file!r} in {case_path}"
    )


@dataclass(frozen=True)
class ReactorHistory:
    """Canonical in-memory view of one homogeneous-reactor history.

    NRG natively writes rows as::

        time, operation_1, leading-point coordinates, operation_2, ...

    The class hides that storage detail.  ``time_s`` is always SI seconds,
    coordinates are exposed separately, and observables use canonical names.
    """

    case_path: Path
    raw_path: Path
    postprocessor_setup: Path
    source_time_units: str
    operation_fields: tuple[str, ...]
    time_s: tuple[float, ...]
    coordinates: Mapping[str, tuple[float, ...]]
    observables: Mapping[str, tuple[float, ...]]
    species_columns: Mapping[str, str]

    @property
    def rows(self) -> int:
        return len(self.time_s)

    @property
    def dimensions(self) -> int:
        return len(self.coordinates)

    @property
    def species_names(self) -> tuple[str, ...]:
        return tuple(self.species_columns.keys())

    def series(self, name: str) -> tuple[float, ...]:
        try:
            return self.observables[name]
        except KeyError as exc:
            available = ", ".join(sorted(self.observables))
            raise KeyError(f"observable {name!r} not found; available: {available}") from exc

    def species_mass_fraction(self, species: str) -> tuple[float, ...]:
        key = next((name for name in self.species_columns if name.upper() == species.upper()), None)
        if key is None:
            raise KeyError(f"species {species!r} not present; available: {', '.join(self.species_names)}")
        return self.observables[self.species_columns[key]]

    @property
    def temperature_K(self) -> tuple[float, ...]:
        return self.series("temperature_K")

    @property
    def pressure_Pa(self) -> tuple[float, ...]:
        return self.series("pressure_Pa")

    @property
    def density_kg_m3(self) -> tuple[float, ...]:
        return self.series("density_kg_m3")

    def canonical_columns(self) -> list[str]:
        return ["time_s", *self.coordinates.keys(), *self.observables.keys()]

    def iter_canonical_rows(self) -> Iterable[list[float]]:
        coord_names = list(self.coordinates)
        obs_names = list(self.observables)
        for i, t in enumerate(self.time_s):
            yield [
                t,
                *(self.coordinates[name][i] for name in coord_names),
                *(self.observables[name][i] for name in obs_names),
            ]

    def write_csv(self, path: str | Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.writer(stream)
            writer.writerow(self.canonical_columns())
            writer.writerows(self.iter_canonical_rows())
        return path

    def all_values_finite(self) -> bool:
        for series in [self.time_s, *self.coordinates.values(), *self.observables.values()]:
            if any(not math.isfinite(value) for value in series):
                return False
        return True


def load_reactor_history(case_path: str | Path, output_file: str = "reactor_history.dat") -> ReactorHistory:
    case_path = Path(case_path).resolve()
    raw_path = case_path / output_file
    if not raw_path.exists():
        raise FileNotFoundError(raw_path)

    setup_path, units, operation_fields = find_postprocessor_setup(case_path, raw_path.name)
    if units not in TIME_FACTORS:
        raise ValueError(f"unsupported NRG postprocessor time units {units!r}")

    rows = read_numeric_rows(raw_path)
    dimensions = len(rows[0]) - 1 - len(operation_fields)
    if dimensions < 1 or dimensions > 3:
        raise ValueError(
            "cannot infer NRG postprocessor dimensionality: "
            f"row_width={len(rows[0])}, operations={len(operation_fields)}"
        )

    # Native NRG layout: time, operation_1, coordinates..., operation_2, ...
    expected_width = 1 + len(operation_fields) + dimensions
    if len(rows[0]) != expected_width:
        raise ValueError(f"unexpected reactor-history width {len(rows[0])}; expected {expected_width}")

    factor = TIME_FACTORS[units]
    time_s = tuple(row[0] * factor for row in rows)
    coordinates: dict[str, tuple[float, ...]] = {}
    for d in range(dimensions):
        coordinates[_COORD_NAMES[d]] = tuple(row[2 + d] for row in rows)

    canonical_names = [canonical_operation_name(name) for name in operation_fields]
    if len(set(canonical_names)) != len(canonical_names):
        raise ValueError(f"canonical operation names are not unique: {canonical_names}")

    observables: dict[str, tuple[float, ...]] = {}
    observables[canonical_names[0]] = tuple(row[1] for row in rows)
    for op_index in range(1, len(operation_fields)):
        raw_column = 1 + dimensions + op_index
        observables[canonical_names[op_index]] = tuple(row[raw_column] for row in rows)

    species_columns: dict[str, str] = {}
    for field, canonical in zip(operation_fields, canonical_names):
        species = species_from_field(field)
        if species is not None:
            species_columns[species] = canonical

    required = {"temperature_K", "pressure_Pa", "density_kg_m3"}
    missing = required.difference(observables)
    if missing:
        raise ValueError(f"reactor history lacks required observables: {sorted(missing)}")

    return ReactorHistory(
        case_path=case_path,
        raw_path=raw_path,
        postprocessor_setup=setup_path,
        source_time_units=units,
        operation_fields=tuple(operation_fields),
        time_s=time_s,
        coordinates=coordinates,
        observables=observables,
        species_columns=species_columns,
    )
