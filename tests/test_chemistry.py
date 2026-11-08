from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from nrg_analysis.chemistry import MolarMassDatabase, mass_to_mole_fractions, molar_concentration
from nrg_analysis.ignition import default_ignition_suite
from nrg_analysis.io import ReactorHistory


H2_MOLAR_MASS_KG_PER_MOL = 2.016e-3
O2_MOLAR_MASS_KG_PER_MOL = 31.998e-3
OH_MOLAR_MASS_KG_PER_MOL = 17.007e-3


def reactor_history() -> ReactorHistory:
    return ReactorHistory(
        case_path=Path("case"),
        raw_path=Path("raw"),
        postprocessor_setup=Path("setup"),
        source_time_units="s",
        operation_fields=(),
        time_s=(0.0, 1.0e-4, 2.0e-4),
        coordinates={},
        observables={
            "temperature_K": (1000.0, 1500.0, 1800.0),
            "pressure_Pa": (101325.0, 101325.0, 101325.0),
            "density_kg_m3": (1.0, 1.0, 1.0),
            "Y_H2": (0.1, 0.05, 0.02),
            "Y_OH": (0.0, 0.01, 0.005),
        },
        species_columns={"H2": "Y_H2", "OH": "Y_OH"},
    )


class ChemistryTests(unittest.TestCase):
    def test_mass_to_mole(self):
        db = MolarMassDatabase(
            {"H2": H2_MOLAR_MASS_KG_PER_MOL, "O2": O2_MOLAR_MASS_KG_PER_MOL}
        )
        total = H2_MOLAR_MASS_KG_PER_MOL + O2_MOLAR_MASS_KG_PER_MOL
        result = mass_to_mole_fractions(
            {
                "H2": H2_MOLAR_MASS_KG_PER_MOL / total,
                "O2": O2_MOLAR_MASS_KG_PER_MOL / total,
            },
            db,
        )
        self.assertAlmostEqual(result["H2"], 0.5)
        self.assertAlmostEqual(result["O2"], 0.5)

    def test_molar_concentration_is_mol_per_m3(self):
        db = MolarMassDatabase({"H2": H2_MOLAR_MASS_KG_PER_MOL})
        result = molar_concentration([1.0], [0.2], "H2", db)
        self.assertAlmostEqual(result[0], 0.2 / H2_MOLAR_MASS_KG_PER_MOL)

    def test_values_kg_per_mol_alias(self):
        db = MolarMassDatabase({"H2": H2_MOLAR_MASS_KG_PER_MOL})
        self.assertEqual(db.values_kg_per_mol, db.values_kg_per_kmol)
        self.assertAlmostEqual(db.get("h2"), H2_MOLAR_MASS_KG_PER_MOL)

    def test_from_file_reads_legacy_kg_per_mol_values(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "molar_masses.dat"
            path.write_text(
                "H2          2.0160E-03\n"
                "N2          2.8013E-02\n",
                encoding="utf-8",
            )
            db = MolarMassDatabase.from_file(path)
            self.assertAlmostEqual(db.get("H2"), H2_MOLAR_MASS_KG_PER_MOL)
            self.assertAlmostEqual(db.get("N2"), 2.8013e-2)
            self.assertEqual(db.source, path.resolve())

    def test_discover_legacy_file_is_optional(self):
        with tempfile.TemporaryDirectory() as td:
            case_path = Path(td)
            self.assertIsNone(MolarMassDatabase.discover(case_path))

            thermo = case_path / "task_setup" / "thermophysical_data"
            thermo.mkdir(parents=True)
            (thermo / "molar_masses.dat").write_text(
                "H2          2.0160E-03\n",
                encoding="utf-8",
            )
            db = MolarMassDatabase.discover(case_path)
            self.assertIsNotNone(db)
            self.assertAlmostEqual(db.get("H2"), H2_MOLAR_MASS_KG_PER_MOL)

    def test_concentration_diagnostics_require_a_molar_mass_database(self):
        metrics = default_ignition_suite(reactor_history(), None)
        self.assertEqual(metrics["tau_max_dOHdt"].peak_units, "1/s")
        self.assertEqual(metrics["tau_peak_OH"].peak_units, "mass fraction")
        self.assertEqual(metrics["tau_max_dOHdt"].metadata["signal"], "Y(OH)")

    def test_concentration_diagnostics_remain_available_with_masses(self):
        db = MolarMassDatabase(
            {
                "H2": H2_MOLAR_MASS_KG_PER_MOL,
                "OH": OH_MOLAR_MASS_KG_PER_MOL,
            }
        )
        metrics = default_ignition_suite(reactor_history(), db)
        self.assertEqual(metrics["tau_max_dOHdt"].peak_units, "mol/m3/s")
        self.assertEqual(metrics["tau_peak_OH"].peak_units, "mol/m3")
        self.assertEqual(metrics["tau_max_dOHdt"].metadata["signal"], "[OH]")


if __name__ == "__main__":
    unittest.main()
