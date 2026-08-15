"""Deterministic Tecplot/gnuplot-oriented table export for NRG studies.

This module deliberately separates scientific structured results from plotting
serialization.  It writes dense point tables: one physical value of the
independent variable corresponds to exactly one data row, and every dependent
series occupies its own column.

Study code should compute/validate scientific quantities first, then call these
helpers to create presentation/export products under ``results/plot_data``.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Iterable, Mapping, Sequence
import csv
import math
from pathlib import Path
from typing import Any


_MISSING_STRINGS = {"", "nan", "+nan", "-nan", "none", "null", "na", "n/a"}


def _clean_name(name: str, *, what: str) -> str:
    value = str(name).strip()
    if not value:
        raise ValueError(f"{what} may not be empty")
    if any(ch in value for ch in ('"', "\r", "\n")):
        raise ValueError(f"{what} contains an unsupported quote/newline: {value!r}")
    return value


def _escape_text(value: str) -> str:
    return str(value).replace('"', "'").replace("\r", " ").replace("\n", " ").strip()


def _is_missing(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip().lower() in _MISSING_STRINGS
    try:
        return math.isnan(float(value))
    except (TypeError, ValueError):
        return False


def _as_float(value: Any, *, field: str) -> float:
    if _is_missing(value):
        return math.nan
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} is not numeric: {value!r}") from exc
    if math.isinf(number):
        raise ValueError(f"{field} may not be infinite: {value!r}")
    return number


def _format_number(value: Any, *, precision: int) -> str:
    number = _as_float(value, field="table value")
    if math.isnan(number):
        return "NaN"
    return f"{number:.{precision}E}"


def _normalized_table(
    x_values: Sequence[Any],
    series: Mapping[str, Sequence[Any]],
    *,
    x_name: str,
    sort_x: bool,
) -> tuple[list[float], OrderedDict[str, list[float]]]:
    x_name = _clean_name(x_name, what="independent variable name")
    if not series:
        raise ValueError("at least one dependent series is required")

    x = [_as_float(v, field=x_name) for v in x_values]
    if any(math.isnan(v) for v in x):
        raise ValueError("independent-variable values may not be NaN")

    names: list[str] = []
    normalized = OrderedDict()
    for raw_name, raw_values in series.items():
        name = _clean_name(raw_name, what="series name")
        if name == x_name or name in names:
            raise ValueError(f"duplicate table variable name: {name!r}")
        values = list(raw_values)
        if len(values) != len(x):
            raise ValueError(
                f"series {name!r} has {len(values)} points but {x_name!r} has {len(x)}"
            )
        normalized[name] = [_as_float(v, field=name) for v in values]
        names.append(name)

    if len(set(x)) != len(x):
        duplicates = sorted({value for value in x if x.count(value) > 1})
        raise ValueError(
            "independent-variable values must be unique within one zone; "
            f"duplicates: {duplicates}"
        )

    order = list(range(len(x)))
    if sort_x:
        order.sort(key=x.__getitem__)

    sorted_x = [x[i] for i in order]
    sorted_series = OrderedDict(
        (name, [values[i] for i in order]) for name, values in normalized.items()
    )
    return sorted_x, sorted_series



def format_compact_number(value: Any, *, precision: int = 12) -> str:
    """Return a human-readable numeric token for filenames and labels.

    Examples: ``2.0 -> "2"``, ``2.5000 -> "2.5"``, ``1500 -> "1500"``.
    This is deliberately different from legacy encodings such as ``2p000``.
    """

    number = _as_float(value, field="numeric filename value")
    if math.isnan(number):
        raise ValueError("numeric filename value may not be NaN")
    if precision < 1 or precision > 17:
        raise ValueError("precision must be between 1 and 17")
    nearest = round(number)
    if math.isclose(number, nearest, rel_tol=0.0, abs_tol=10.0 ** (-precision)):
        return str(int(nearest))
    return f"{number:.{precision}g}"


def format_parameter_label(prefix: str, value: Any, unit: str) -> str:
    """Build compact human-readable labels such as ``P2atm`` or ``T1500K``."""

    prefix_clean = _clean_name(prefix, what="parameter prefix")
    unit_clean = _clean_name(unit, what="parameter unit")
    return f"{prefix_clean}{format_compact_number(value)}{unit_clean}"


def compose_series_name(quantity: str, qualifier: str, unit: str | None = None) -> str:
    """Compose a self-describing plotting-series variable name.

    ``quantity`` describes the physical quantity and/or measurement definition,
    while ``qualifier`` identifies the compared category (for example a
    chemical mechanism).  ``unit`` is optional but recommended when it makes the
    exported table easier to interpret without external metadata.

    Examples::

        compose_series_name("tau_dTdt", "Konnov", "s")
        # -> "tau_dTdt_Konnov_s"

        compose_series_name("Tproduct", "Keromnes", "K")
        # -> "Tproduct_Keromnes_K"
    """

    quantity_clean = _clean_name(quantity, what="series quantity")
    qualifier_clean = _clean_name(qualifier, what="series qualifier")
    parts = [quantity_clean, qualifier_clean]
    if unit is not None:
        parts.append(_clean_name(unit, what="series unit"))
    return "_".join(parts)



def _semantic_normalize(value: str) -> str:
    """Normalize semantic labels for conservative in-file context checks."""

    return "".join(ch.lower() for ch in str(value) if ch.isalnum())


def validate_semantic_context(
    *,
    semantic_label: str,
    series_names: Sequence[str],
    title: str | None = None,
    zone: str | None = None,
) -> None:
    """Require a scientific metric/definition to be identifiable in-file.

    The filename is intentionally excluded from this check. A plotting file
    should remain interpretable after it is copied or renamed. The semantic
    label may be carried by a dependent variable name, the TITLE, or the ZONE.

    Example: bare mechanism columns are acceptable for an ignition-delay table
    only when ``tau_dTdt`` (or the chosen metric token) appears in TITLE/ZONE.
    """

    label = _clean_name(semantic_label, what="semantic label")
    needle = _semantic_normalize(label)
    if not needle:
        raise ValueError("semantic label must contain at least one alphanumeric character")

    metadata = [*(str(name) for name in series_names)]
    if title is not None:
        metadata.append(str(title))
    if zone is not None:
        metadata.append(str(zone))

    if any(needle in _semantic_normalize(item) for item in metadata):
        return

    raise ValueError(
        f"semantic label {label!r} is not identifiable from dependent VARIABLES, "
        "TITLE, or ZONE; filenames are not accepted as the sole semantic context"
    )

def write_tecplot_point_table(
    path: str | Path,
    *,
    x_name: str,
    x_values: Sequence[Any],
    series: Mapping[str, Sequence[Any]],
    title: str | None = None,
    zone: str | None = None,
    precision: int = 12,
    sort_x: bool = True,
    include_title: bool = True,
    include_zone: bool = True,
) -> Path:
    """Write a dense Tecplot ASCII table using the NRG laboratory contract.

    Canonical output::

        TITLE = "..."
        VARIABLES = "x" "series_a" "series_b"
        ZONE T = "..."
        x1 y_a1 y_b1
        x2 y_a2 y_b2

    The laboratory plotting convention intentionally omits ``N``, ``E``,
    ``DATAPACKING``, ``I`` and ``F`` metadata from the ``ZONE`` line.  One
    physical value of the independent variable corresponds to exactly one data
    row. Missing dependent values are serialized as ``NaN``; missing, duplicate
    or non-numeric independent-variable values are rejected.

    ``include_title`` and ``include_zone`` are retained only for backward API
    compatibility. New NRG study exports should leave both enabled.
    """

    if precision < 1 or precision > 17:
        raise ValueError("precision must be between 1 and 17 significant decimals")

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    x, normalized = _normalized_table(x_values, series, x_name=x_name, sort_x=sort_x)

    variable_names = [_clean_name(x_name, what="independent variable name"), *normalized.keys()]
    lines: list[str] = []
    title_name = _escape_text(title or target.stem)
    zone_name = _escape_text(zone or title or target.stem)
    if include_title:
        lines.append(f'TITLE = "{title_name}"')
    lines.append("VARIABLES = " + " ".join(f'"{name}"' for name in variable_names))
    if include_zone:
        lines.append(f'ZONE T = "{zone_name}"')

    for index, xv in enumerate(x):
        row = [_format_number(xv, precision=precision)]
        row.extend(_format_number(values[index], precision=precision) for values in normalized.values())
        lines.append(" ".join(row))

    target.write_text("\n".join(lines) + "\n", encoding="ascii")
    return target


def write_dense_csv_table(
    path: str | Path,
    *,
    x_name: str,
    x_values: Sequence[Any],
    series: Mapping[str, Sequence[Any]],
    precision: int = 12,
    sort_x: bool = True,
) -> Path:
    """Write the same dense table as a conventional comma-separated file."""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    x, normalized = _normalized_table(x_values, series, x_name=x_name, sort_x=sort_x)
    fieldnames = [_clean_name(x_name, what="independent variable name"), *normalized.keys()]

    with target.open("w", newline="", encoding="ascii") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(fieldnames)
        for index, xv in enumerate(x):
            row = [_format_number(xv, precision=precision)]
            row.extend(_format_number(values[index], precision=precision) for values in normalized.values())
            writer.writerow(row)
    return target


def pivot_records(
    records: Iterable[Mapping[str, Any]],
    *,
    x_field: str,
    series_field: str,
    value_field: str,
    series_order: Sequence[str] | None = None,
    require_complete: bool = True,
) -> tuple[list[float], OrderedDict[str, list[float]]]:
    """Pivot long-form records into one x-array plus dense dependent series.

    Each ``(x_field, series_field)`` pair must occur at most once.  This strict
    rule prevents the sparse pseudo-wide layout where the same x value appears
    repeatedly with only one populated mechanism column.
    """

    rows = list(records)
    if not rows:
        raise ValueError("cannot pivot an empty record sequence")

    x_values: set[float] = set()
    discovered_series: list[str] = []
    matrix: dict[tuple[float, str], float] = {}

    for row_number, row in enumerate(rows, start=1):
        if x_field not in row or series_field not in row or value_field not in row:
            missing = [key for key in (x_field, series_field, value_field) if key not in row]
            raise KeyError(f"record {row_number} missing required field(s): {', '.join(missing)}")
        x = _as_float(row[x_field], field=x_field)
        if math.isnan(x):
            raise ValueError(f"record {row_number}: {x_field} may not be NaN")
        series_name = _clean_name(str(row[series_field]), what=series_field)
        value = _as_float(row[value_field], field=value_field)
        key = (x, series_name)
        if key in matrix:
            raise ValueError(
                f"duplicate ({x_field}, {series_field}) pair for {value_field}: "
                f"({x!r}, {series_name!r})"
            )
        matrix[key] = value
        x_values.add(x)
        if series_name not in discovered_series:
            discovered_series.append(series_name)

    if series_order is None:
        names = sorted(discovered_series)
    else:
        names = [_clean_name(name, what="series_order entry") for name in series_order]
        if len(set(names)) != len(names):
            raise ValueError("series_order contains duplicates")
        unexpected = sorted(set(discovered_series) - set(names))
        if unexpected:
            raise ValueError(f"records contain series absent from series_order: {unexpected}")

    x_sorted = sorted(x_values)
    output: OrderedDict[str, list[float]] = OrderedDict()
    missing_cells: list[str] = []
    for name in names:
        values: list[float] = []
        for x in x_sorted:
            key = (x, name)
            if key not in matrix:
                if require_complete:
                    missing_cells.append(f"{x_field}={x:g}, {series_field}={name}")
                    values.append(math.nan)
                else:
                    values.append(math.nan)
            else:
                values.append(matrix[key])
        output[name] = values

    if missing_cells and require_complete:
        preview = "; ".join(missing_cells[:8])
        extra = "" if len(missing_cells) <= 8 else f"; ... ({len(missing_cells)} missing total)"
        raise ValueError(f"pivot is incomplete: {preview}{extra}")

    return x_sorted, output


def write_pivoted_tecplot(
    path: str | Path,
    records: Iterable[Mapping[str, Any]],
    *,
    x_field: str,
    series_field: str,
    value_field: str,
    series_order: Sequence[str] | None = None,
    series_label_map: Mapping[str, str] | None = None,
    title: str | None = None,
    zone: str | None = None,
    semantic_label: str | None = None,
    precision: int = 12,
    require_complete: bool = True,
    csv_path: str | Path | None = None,
) -> Path:
    """Pivot long-form records and write Tecplot (and optionally dense CSV).

    ``series_label_map`` may rename categorical series to self-describing
    Tecplot variable names without changing the category values used for the
    pivot.  ``semantic_label`` optionally enforces that the physical quantity
    or measurement definition is identifiable from the dependent VARIABLES,
    TITLE, or ZONE.  The filename is deliberately not considered sufficient
    semantic context.
    """

    x, series = pivot_records(
        records,
        x_field=x_field,
        series_field=series_field,
        value_field=value_field,
        series_order=series_order,
        require_complete=require_complete,
    )

    if series_label_map is not None:
        label_map = {
            _clean_name(str(key), what="series label key"):
            _clean_name(str(value), what="series label")
            for key, value in series_label_map.items()
        }
        missing = [name for name in series if name not in label_map]
        if missing:
            raise ValueError(f"series_label_map missing label(s) for: {missing}")
        extra = sorted(set(label_map) - set(series))
        if extra:
            raise ValueError(f"series_label_map contains unknown series: {extra}")
        renamed = OrderedDict((label_map[name], values) for name, values in series.items())
        if len(set(renamed)) != len(renamed):
            raise ValueError("series_label_map produces duplicate variable names")
        series = renamed

    effective_zone = zone or value_field
    if semantic_label is not None:
        validate_semantic_context(
            semantic_label=semantic_label,
            series_names=list(series.keys()),
            title=title,
            zone=effective_zone,
        )

    target = write_tecplot_point_table(
        path,
        x_name=x_field,
        x_values=x,
        series=series,
        title=title,
        zone=effective_zone,
        precision=precision,
    )
    if csv_path is not None:
        write_dense_csv_table(
            csv_path,
            x_name=x_field,
            x_values=x,
            series=series,
            precision=precision,
        )
    return target


def write_grouped_metric_tecplot_tables(
    records: Iterable[Mapping[str, Any]],
    *,
    group_field: str,
    x_field: str,
    value_fields: Sequence[str],
    targets: Mapping[str, str | Path],
    titles: Mapping[str, str] | None = None,
    zones: Mapping[str, str] | None = None,
    value_label_map: Mapping[str, str] | None = None,
    precision: int = 12,
) -> OrderedDict[str, Path]:
    """Write one dense Tecplot metric-comparison table per categorical group.

    This helper is intended for data such as ignition-definition comparisons
    where the source records contain both an independent variable (temperature
    or pressure) and a categorical dimension (for example chemical mechanism).
    The categorical dimension is split into separate files so that no repeated
    x value depends on undocumented row order for interpretation.

    ``targets`` explicitly maps each expected group value to its output path.
    Unexpected or missing groups are rejected.
    """

    rows = list(records)
    if not rows:
        raise ValueError("cannot export an empty record sequence")
    if not value_fields:
        raise ValueError("at least one value field is required")

    grouped: OrderedDict[str, list[Mapping[str, Any]]] = OrderedDict()
    for row_number, row in enumerate(rows, start=1):
        if group_field not in row:
            raise KeyError(f"record {row_number} missing required field: {group_field}")
        group = _clean_name(str(row[group_field]), what=group_field)
        grouped.setdefault(group, []).append(row)

    target_map = OrderedDict(
        (_clean_name(str(group), what="target group"), Path(path))
        for group, path in targets.items()
    )
    discovered = set(grouped)
    expected = set(target_map)
    unexpected = sorted(discovered - expected)
    missing = sorted(expected - discovered)
    if unexpected:
        raise ValueError(f"records contain groups absent from targets: {unexpected}")
    if missing:
        raise ValueError(f"targets contain groups absent from records: {missing}")

    labels = dict(value_label_map or {})
    unknown_labels = sorted(set(labels) - set(value_fields))
    if unknown_labels:
        raise ValueError(f"value_label_map contains unknown value fields: {unknown_labels}")
    output_labels = [labels.get(field, field) for field in value_fields]
    if len(set(output_labels)) != len(output_labels):
        raise ValueError("value_label_map produces duplicate variable names")

    title_map = dict(titles or {})
    zone_map = dict(zones or {})
    outputs: OrderedDict[str, Path] = OrderedDict()
    for group, target in target_map.items():
        group_rows = grouped[group]
        x_values: list[Any] = []
        series: OrderedDict[str, list[Any]] = OrderedDict(
            (label, []) for label in output_labels
        )
        for row_number, row in enumerate(group_rows, start=1):
            if x_field not in row:
                raise KeyError(f"group {group!r} record {row_number} missing field: {x_field}")
            x_values.append(row[x_field])
            for field, label in zip(value_fields, series):
                if field not in row:
                    raise KeyError(f"group {group!r} record {row_number} missing field: {field}")
                series[label].append(row[field])

        outputs[group] = write_tecplot_point_table(
            target,
            x_name=x_field,
            x_values=x_values,
            series=series,
            title=title_map.get(group, target.stem),
            zone=zone_map.get(group, group),
            precision=precision,
        )
    return outputs


def collapse_sparse_wide_rows(
    records: Iterable[Mapping[str, Any]],
    *,
    key_fields: Sequence[str],
    value_fields: Sequence[str] | None = None,
) -> list[dict[str, Any]]:
    """Collapse a sparse pseudo-wide table into one dense row per key.

    This is mainly a compatibility helper for older study outputs.  For each
    group of rows sharing ``key_fields``, each non-key column may have zero or
    one non-missing value.  Multiple conflicting values are rejected.
    """

    rows = list(records)
    if not rows:
        return []
    if not key_fields:
        raise ValueError("at least one key field is required")

    field_order = list(rows[0].keys())
    for key in key_fields:
        if key not in field_order:
            raise KeyError(f"key field not present: {key}")
    if value_fields is None:
        values = [field for field in field_order if field not in key_fields]
    else:
        values = list(value_fields)
        unknown = [field for field in values if field not in field_order]
        if unknown:
            raise KeyError(f"value field(s) not present: {', '.join(unknown)}")

    groups: OrderedDict[tuple[str, ...], list[Mapping[str, Any]]] = OrderedDict()
    for row in rows:
        key = tuple(str(row.get(field, "")).strip() for field in key_fields)
        groups.setdefault(key, []).append(row)

    output: list[dict[str, Any]] = []
    for key, group in groups.items():
        dense: dict[str, Any] = {field: value for field, value in zip(key_fields, key)}
        for field in values:
            candidates = [row.get(field) for row in group if not _is_missing(row.get(field))]
            if not candidates:
                dense[field] = "NaN"
                continue
            canonical = [str(value).strip() for value in candidates]
            if len(set(canonical)) > 1:
                raise ValueError(
                    f"conflicting values while collapsing {key_fields}={key!r}, "
                    f"field={field!r}: {canonical}"
                )
            dense[field] = candidates[0]
        output.append(dense)
    return output


def read_csv_records(path: str | Path) -> tuple[list[str], list[dict[str, str]]]:
    source = Path(path)
    with source.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError(f"CSV has no header: {source}")
        return list(reader.fieldnames), list(reader)


def write_csv_records(path: str | Path, fieldnames: Sequence[str], rows: Iterable[Mapping[str, Any]]) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", newline="", encoding="ascii") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(fieldnames), extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({name: row.get(name, "") for name in fieldnames})
    return target
