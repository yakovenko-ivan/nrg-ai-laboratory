from __future__ import annotations

from pathlib import Path
import unittest

from nrg_analysis.equilibrium import EquilibriumCriteria, assess_equilibrium
from nrg_analysis.io import ReactorHistory


class EquilibriumTests(unittest.TestCase):
    def make_history(self, varying=False):
        t = tuple(i * 1.0e-6 for i in range(20))
        T = tuple(2000.0 + (i * 0.1 if varying else 0.0) for i in range(20))
        p = tuple(150000.0 + (i * 10.0 if varying else 0.0) for i in range(20))
        rho = tuple(1.0 for _ in t)
        h2 = tuple(0.1 for _ in t)
        o2 = tuple(0.2 for _ in t)
        n2 = tuple(0.7 for _ in t)
        return ReactorHistory(
            case_path=Path("."), raw_path=Path("raw"), postprocessor_setup=Path("setup"),
            source_time_units="seconds",
            operation_fields=("temperature", "pressure", "density", "specie_mass_fraction(H2)", "specie_mass_fraction(O2)", "specie_mass_fraction(N2)"),
            time_s=t,
            coordinates={"cell_i": tuple(1.0 for _ in t)},
            observables={"temperature_K": T, "pressure_Pa": p, "density_kg_m3": rho, "Y_H2": h2, "Y_O2": o2, "Y_N2": n2},
            species_columns={"H2": "Y_H2", "O2": "Y_O2", "N2": "Y_N2"},
        )

    def test_stationary_history_reaches_equilibrium(self):
        result = assess_equilibrium(self.make_history(), EquilibriumCriteria())
        self.assertTrue(result.reached)

    def test_drifting_history_fails_equilibrium(self):
        result = assess_equilibrium(self.make_history(varying=True), EquilibriumCriteria())
        self.assertFalse(result.reached)


if __name__ == "__main__":
    unittest.main()
