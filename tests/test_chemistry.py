from __future__ import annotations

import unittest

from nrg_analysis.chemistry import MolarMassDatabase, mass_to_mole_fractions, molar_concentration


class ChemistryTests(unittest.TestCase):
    def test_mass_to_mole(self):
        db = MolarMassDatabase({"H2": 2.0, "O2": 32.0})
        result = mass_to_mole_fractions({"H2": 2.0 / 34.0, "O2": 32.0 / 34.0}, db)
        self.assertAlmostEqual(result["H2"], 0.5)
        self.assertAlmostEqual(result["O2"], 0.5)

    def test_molar_concentration(self):
        db = MolarMassDatabase({"H2": 2.0})
        result = molar_concentration([1.0], [0.2], "H2", db)
        self.assertAlmostEqual(result[0], 0.1)


if __name__ == "__main__":
    unittest.main()
