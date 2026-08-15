from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from nrg_analysis.plot_data import validate_semantic_context, write_pivoted_tecplot


RECORDS = [
    {"invT": 1.0, "mechanism": "KONNOV", "tau_dTdt_s": 1.0e-4},
    {"invT": 1.0, "mechanism": "KEROMNES", "tau_dTdt_s": 2.0e-4},
    {"invT": 0.9, "mechanism": "KONNOV", "tau_dTdt_s": 5.0e-5},
    {"invT": 0.9, "mechanism": "KEROMNES", "tau_dTdt_s": 6.0e-5},
]


class SemanticContextTests(unittest.TestCase):
    def test_bare_mechanisms_and_generic_title_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "not identifiable"):
            validate_semantic_context(
                semantic_label="tau_dTdt",
                series_names=["KONNOV", "KEROMNES"],
                title="Ignition delay vs inverse temperature, P0=1atm",
                zone="P0=1atm",
            )

    def test_metric_in_zone_makes_bare_mechanisms_unambiguous(self) -> None:
        validate_semantic_context(
            semantic_label="tau_dTdt",
            series_names=["KONNOV", "KEROMNES"],
            title="Ignition delay vs inverse temperature, P0=1atm",
            zone="tau_dTdt, P0=1atm",
        )

    def test_self_describing_variables_are_sufficient(self) -> None:
        validate_semantic_context(
            semantic_label="tau_dTdt",
            series_names=["tau_dTdt_Konnov", "tau_dTdt_Keromnes"],
            title="Ignition delay vs inverse temperature, P0=1atm",
            zone="P0=1atm",
        )

    def test_pivoted_export_enforces_requested_semantics(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "tau_dTdt_vs_invT_1atm.dat"
            with self.assertRaisesRegex(ValueError, "filenames are not accepted"):
                write_pivoted_tecplot(
                    path,
                    RECORDS,
                    x_field="invT",
                    series_field="mechanism",
                    value_field="tau_dTdt_s",
                    series_order=["KONNOV", "KEROMNES"],
                    title="Ignition delay vs inverse temperature, P0=1atm",
                    zone="P0=1atm",
                    semantic_label="tau_dTdt",
                )

            write_pivoted_tecplot(
                path,
                RECORDS,
                x_field="invT",
                series_field="mechanism",
                value_field="tau_dTdt_s",
                series_order=["KONNOV", "KEROMNES"],
                title="Ignition delay tau_dTdt vs inverse temperature, P0=1atm",
                zone="tau_dTdt, P0=1atm",
                semantic_label="tau_dTdt",
            )
            content = path.read_text(encoding="ascii")
            self.assertIn('VARIABLES = "invT" "KONNOV" "KEROMNES"', content)
            self.assertIn('ZONE T = "tau_dTdt, P0=1atm"', content)


if __name__ == "__main__":
    unittest.main()
