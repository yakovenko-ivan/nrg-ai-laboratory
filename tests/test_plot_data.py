from __future__ import annotations

import csv
import math
from pathlib import Path
import tempfile
import unittest

from nrg_analysis.plot_data import (
    collapse_sparse_wide_rows,
    compose_series_name,
    format_compact_number,
    format_parameter_label,
    pivot_records,
    write_dense_csv_table,
    write_grouped_metric_tecplot_tables,
    write_pivoted_tecplot,
    write_tecplot_point_table,
)


class PlotDataTests(unittest.TestCase):
    def test_tecplot_dense_one_row_per_x(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "curve.dat"
            write_tecplot_point_table(
                out,
                x_name="P0_atm",
                x_values=[2, 1, 3],
                series={"KONNOV": [2e-5, 1e-5, 3e-5], "TEREZA": [4e-5, 3e-5, 5e-5]},
                title="test",
                zone="tau",
            )
            lines = out.read_text(encoding="ascii").splitlines()
            self.assertEqual(lines[0], 'TITLE = "test"')
            self.assertEqual(lines[1], 'VARIABLES = "P0_atm" "KONNOV" "TEREZA"')
            self.assertEqual(lines[2], 'ZONE T = "tau"')
            header = "\n".join(lines[:3])
            for forbidden in ("N=", "E=", "DATAPACKING=", "I=", "F="):
                self.assertNotIn(forbidden, header)
            data = lines[3:]
            self.assertEqual(len(data), 3)
            self.assertTrue(data[0].startswith("1.000000000000E+00 "))
            self.assertTrue(data[1].startswith("2.000000000000E+00 "))
            self.assertTrue(data[2].startswith("3.000000000000E+00 "))

    def test_duplicate_x_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaisesRegex(ValueError, "unique"):
                write_tecplot_point_table(
                    Path(td) / "bad.dat",
                    x_name="P0_atm",
                    x_values=[1.0, 1.0],
                    series={"KONNOV": [1.0, 2.0]},
                )

    def test_pivot_long_records(self):
        records = [
            {"P0_atm": 2, "mechanism": "TEREZA", "tau": 4.0},
            {"P0_atm": 1, "mechanism": "KONNOV", "tau": 1.0},
            {"P0_atm": 2, "mechanism": "KONNOV", "tau": 2.0},
            {"P0_atm": 1, "mechanism": "TEREZA", "tau": 3.0},
        ]
        x, series = pivot_records(
            records,
            x_field="P0_atm",
            series_field="mechanism",
            value_field="tau",
            series_order=["KONNOV", "TEREZA"],
        )
        self.assertEqual(x, [1.0, 2.0])
        self.assertEqual(series["KONNOV"], [1.0, 2.0])
        self.assertEqual(series["TEREZA"], [3.0, 4.0])

    def test_pivot_duplicate_pair_rejected(self):
        records = [
            {"P": 1, "mechanism": "A", "tau": 1},
            {"P": 1, "mechanism": "A", "tau": 2},
        ]
        with self.assertRaisesRegex(ValueError, "duplicate"):
            pivot_records(records, x_field="P", series_field="mechanism", value_field="tau")

    def test_pivot_missing_series_rejected_by_default(self):
        records = [
            {"P": 1, "mechanism": "A", "tau": 1},
            {"P": 2, "mechanism": "A", "tau": 2},
            {"P": 1, "mechanism": "B", "tau": 3},
        ]
        with self.assertRaisesRegex(ValueError, "incomplete"):
            pivot_records(
                records,
                x_field="P",
                series_field="mechanism",
                value_field="tau",
                series_order=["A", "B"],
            )

    def test_sparse_wide_collapse(self):
        rows = [
            {"P0_atm": "1", "KONNOV": "1e-5", "TEREZA": ""},
            {"P0_atm": "1", "KONNOV": "", "TEREZA": "2e-5"},
            {"P0_atm": "2", "KONNOV": "3e-5", "TEREZA": ""},
            {"P0_atm": "2", "KONNOV": "", "TEREZA": "4e-5"},
        ]
        dense = collapse_sparse_wide_rows(rows, key_fields=["P0_atm"])
        self.assertEqual(len(dense), 2)
        self.assertEqual(dense[0]["KONNOV"], "1e-5")
        self.assertEqual(dense[0]["TEREZA"], "2e-5")
        self.assertEqual(dense[1]["KONNOV"], "3e-5")
        self.assertEqual(dense[1]["TEREZA"], "4e-5")

    def test_dense_csv_matches_point_count(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "curve.csv"
            write_dense_csv_table(
                out,
                x_name="T0_K",
                x_values=[1000, 1100],
                series={"KONNOV": [1e-3, 1e-4], "ZHANG": [2e-3, 2e-4]},
            )
            with out.open(newline="", encoding="ascii") as f:
                rows = list(csv.reader(f))
            self.assertEqual(len(rows), 3)
            self.assertEqual(rows[0], ["T0_K", "KONNOV", "ZHANG"])

    def test_write_pivoted_with_csv_twin(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            records = [
                {"P": 1, "m": "A", "tau": 1e-5},
                {"P": 1, "m": "B", "tau": 2e-5},
                {"P": 2, "m": "A", "tau": 3e-5},
                {"P": 2, "m": "B", "tau": 4e-5},
            ]
            write_pivoted_tecplot(
                root / "x.dat",
                records,
                x_field="P",
                series_field="m",
                value_field="tau",
                series_order=["A", "B"],
                csv_path=root / "x.csv",
            )
            self.assertTrue((root / "x.dat").is_file())
            self.assertTrue((root / "x.csv").is_file())

    def test_pivoted_mechanism_table_has_one_row_per_independent_value(self):
        records = []
        for inv_t in [1.0, 0.9, 0.8]:
            for mechanism, offset in [("KONNOV", 0.0), ("KEROMNES", 1.0), ("TEREZA", 2.0), ("ZHANG", 3.0)]:
                records.append({"invT": inv_t, "mechanism": mechanism, "tau": inv_t + offset})
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "curve.dat"
            write_pivoted_tecplot(
                out,
                records,
                x_field="invT",
                series_field="mechanism",
                value_field="tau",
                series_order=["KONNOV", "KEROMNES", "TEREZA", "ZHANG"],
                title="fixed pressure",
                zone="P0=2 atm",
            )
            lines = out.read_text(encoding="ascii").splitlines()
            data = lines[3:]
            self.assertEqual(len(data), 3)
            self.assertEqual(len({line.split()[0] for line in data}), 3)
            for line in data:
                self.assertEqual(len(line.split()), 5)


    def test_grouped_metric_export_splits_mechanisms_into_separate_files(self):
        records = []
        for temperature in (1000.0, 1100.0):
            records.extend([
                {"T0_K": temperature, "mechanism": "KONNOV", "tau_dTdt_s": 1.0, "tau_dpdt_s": 1.1},
                {"T0_K": temperature, "mechanism": "TEREZA", "tau_dTdt_s": 2.0, "tau_dpdt_s": 2.1},
            ])
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            outputs = write_grouped_metric_tecplot_tables(
                records,
                group_field="mechanism",
                x_field="T0_K",
                value_fields=["tau_dTdt_s", "tau_dpdt_s"],
                targets={
                    "KONNOV": root / "Konnov.dat",
                    "TEREZA": root / "Tereza.dat",
                },
                titles={
                    "KONNOV": "Konnov metrics",
                    "TEREZA": "Tereza metrics",
                },
            )
            self.assertEqual(set(outputs), {"KONNOV", "TEREZA"})
            for path in outputs.values():
                lines = path.read_text(encoding="ascii").splitlines()
                self.assertEqual(len(lines[3:]), 2)
                self.assertEqual(len({line.split()[0] for line in lines[3:]}), 2)
                self.assertEqual(lines[1], 'VARIABLES = "T0_K" "tau_dTdt_s" "tau_dpdt_s"')

    def test_compose_series_name(self):
        self.assertEqual(compose_series_name("tau_dTdt", "Konnov"), "tau_dTdt_Konnov")
        self.assertEqual(compose_series_name("tau_dTdt", "Konnov", "s"), "tau_dTdt_Konnov_s")
        self.assertEqual(compose_series_name("Tproduct", "Keromnes"), "Tproduct_Keromnes")

    def test_pivoted_series_label_map_makes_variables_self_describing(self):
        records = [
            {"P0_atm": 1, "mechanism": "KONNOV", "tau": 1e-5},
            {"P0_atm": 1, "mechanism": "TEREZA", "tau": 2e-5},
            {"P0_atm": 2, "mechanism": "KONNOV", "tau": 3e-5},
            {"P0_atm": 2, "mechanism": "TEREZA", "tau": 4e-5},
        ]
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "curve.dat"
            write_pivoted_tecplot(
                out,
                records,
                x_field="P0_atm",
                series_field="mechanism",
                value_field="tau",
                series_order=["KONNOV", "TEREZA"],
                series_label_map={
                    "KONNOV": "tau_dTdt_Konnov",
                    "TEREZA": "tau_dTdt_Tereza",
                },
            )
            lines = out.read_text(encoding="ascii").splitlines()
            self.assertEqual(
                lines[1],
                'VARIABLES = "P0_atm" "tau_dTdt_Konnov" "tau_dTdt_Tereza"',
            )

    def test_pivoted_series_label_map_must_cover_exact_series_set(self):
        records = [
            {"P0_atm": 1, "mechanism": "KONNOV", "tau": 1e-5},
            {"P0_atm": 1, "mechanism": "TEREZA", "tau": 2e-5},
        ]
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaisesRegex(ValueError, "missing label"):
                write_pivoted_tecplot(
                    Path(td) / "curve.dat",
                    records,
                    x_field="P0_atm",
                    series_field="mechanism",
                    value_field="tau",
                    series_order=["KONNOV", "TEREZA"],
                    series_label_map={"KONNOV": "tau_dTdt_Konnov"},
                )

    def test_compact_parameter_labels(self):
        self.assertEqual(format_compact_number(2.0), "2")
        self.assertEqual(format_compact_number(2.5000), "2.5")
        self.assertEqual(format_parameter_label("P", 2.0, "atm"), "P2atm")
        self.assertEqual(format_parameter_label("P", 2.5, "atm"), "P2.5atm")
        self.assertEqual(format_parameter_label("T", 1500.0, "K"), "T1500K")


if __name__ == "__main__":
    unittest.main()
