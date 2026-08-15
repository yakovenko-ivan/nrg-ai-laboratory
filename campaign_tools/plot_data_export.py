#!/usr/bin/env python3
"""Convert structured study CSV outputs to dense Tecplot/gnuplot tables.

The preferred workflow is to call :mod:`nrg_analysis.plot_data` directly from a
study's ``analyze.py``.  This CLI is provided for human use and for repairing
legacy sparse CSV outputs without rerunning CFD.
"""

from __future__ import annotations

import argparse
import csv
import math
from pathlib import Path
from typing import Any

# Allow direct execution as ``python campaign_tools/plot_data_export.py``
# without requiring an externally configured PYTHONPATH.
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from nrg_analysis.plot_data import (
    collapse_sparse_wide_rows,
    read_csv_records,
    write_csv_records,
    write_dense_csv_table,
    write_pivoted_tecplot,
    write_tecplot_point_table,
)


def _parse_scalar(text: str) -> str | float:
    try:
        return float(text)
    except ValueError:
        return text


def _matches(actual: str, expected: str | float) -> bool:
    if isinstance(expected, float):
        try:
            value = float(actual)
        except ValueError:
            return False
        return math.isclose(value, expected, rel_tol=1.0e-12, abs_tol=1.0e-12)
    return actual.strip() == expected


def _where(items: list[str]) -> dict[str, str | float]:
    result: dict[str, str | float] = {}
    for item in items:
        if "=" not in item:
            raise ValueError(f"--where expects FIELD=VALUE, got {item!r}")
        field, value = item.split("=", 1)
        field = field.strip()
        if not field:
            raise ValueError(f"--where has empty field: {item!r}")
        result[field] = _parse_scalar(value.strip())
    return result


def cmd_pivot(args: argparse.Namespace) -> int:
    fieldnames, rows = read_csv_records(args.input)
    filters = _where(args.where)
    for field in filters:
        if field not in fieldnames:
            raise KeyError(f"filter field absent from CSV: {field}")
    selected = [
        row for row in rows
        if all(_matches(row.get(field, ""), expected) for field, expected in filters.items())
    ]
    if not selected:
        raise ValueError("filters selected no CSV rows")

    order = None
    if args.series_order:
        order = [item.strip() for item in args.series_order.split(",") if item.strip()]
    write_pivoted_tecplot(
        args.output,
        selected,
        x_field=args.x,
        series_field=args.series,
        value_field=args.value,
        series_order=order,
        title=args.title,
        zone=args.zone or args.value,
        precision=args.precision,
        require_complete=not args.allow_missing,
        csv_path=args.csv_output,
    )
    return 0


def cmd_collapse(args: argparse.Namespace) -> int:
    fieldnames, rows = read_csv_records(args.input)
    dense = collapse_sparse_wide_rows(rows, key_fields=args.key)
    write_csv_records(args.csv_output, fieldnames, dense)

    if args.tecplot_output:
        x_field = args.x or args.key[0]
        if x_field not in fieldnames:
            raise KeyError(f"x field absent from CSV: {x_field}")
        dependent_fields = [field for field in fieldnames if field != x_field]
        if args.variables:
            dependent_fields = [item.strip() for item in args.variables.split(",") if item.strip()]
        x_values: list[Any] = [row[x_field] for row in dense]
        series = {field: [row.get(field, "NaN") for row in dense] for field in dependent_fields}
        write_tecplot_point_table(
            args.tecplot_output,
            x_name=x_field,
            x_values=x_values,
            series=series,
            title=args.title,
            zone=args.zone or Path(args.tecplot_output).stem,
            precision=args.precision,
        )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    pivot = sub.add_parser("pivot", help="pivot long-form CSV into a dense mechanism/series table")
    pivot.add_argument("--input", required=True, type=Path)
    pivot.add_argument("--output", required=True, type=Path, help="Tecplot ASCII .dat output")
    pivot.add_argument("--csv-output", type=Path, default=None, help="optional dense CSV twin")
    pivot.add_argument("--x", required=True, help="independent-variable field")
    pivot.add_argument("--series", required=True, help="field identifying dependent series")
    pivot.add_argument("--value", required=True, help="dependent value field")
    pivot.add_argument("--where", action="append", default=[], help="FIELD=VALUE filter; repeatable")
    pivot.add_argument("--series-order", default=None, help="comma-separated explicit series order")
    pivot.add_argument("--title", default=None)
    pivot.add_argument("--zone", default=None)
    pivot.add_argument("--precision", type=int, default=12)
    pivot.add_argument("--allow-missing", action="store_true")
    pivot.set_defaults(func=cmd_pivot)

    collapse = sub.add_parser("collapse", help="repair a sparse pseudo-wide CSV")
    collapse.add_argument("--input", required=True, type=Path)
    collapse.add_argument("--csv-output", required=True, type=Path)
    collapse.add_argument("--key", action="append", required=True, help="grouping key field; repeatable")
    collapse.add_argument("--tecplot-output", type=Path, default=None)
    collapse.add_argument("--x", default=None, help="x field for optional Tecplot output")
    collapse.add_argument("--variables", default=None, help="comma-separated dependent fields for Tecplot")
    collapse.add_argument("--title", default=None)
    collapse.add_argument("--zone", default=None)
    collapse.add_argument("--precision", type=int, default=12)
    collapse.set_defaults(func=cmd_collapse)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
