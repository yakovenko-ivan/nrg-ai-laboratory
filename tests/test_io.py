from __future__ import annotations

import tempfile
from pathlib import Path
import unittest

from nrg_analysis.io import canonical_operation_name, load_reactor_history


class IOTests(unittest.TestCase):
    def test_species_canonicalization_preserves_star(self):
        self.assertEqual(canonical_operation_name("specie_mass_fraction(OH)"), "Y_OH")
        self.assertEqual(canonical_operation_name("specie_mass_fraction(OH*)"), "Y_OH_x2A_")
        self.assertNotEqual(
            canonical_operation_name("specie_mass_fraction(OH)"),
            canonical_operation_name("specie_mass_fraction(OH*)"),
        )

    def test_native_nrg_column_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            case = Path(tmp)
            setup = case / "task_setup"
            setup.mkdir()
            (setup / "post_processor# 1.inf").write_text(
                """&pproc_parameters
 post_processor_output_file='reactor_history.dat',
 operations_number=6,
 save_time=0.1,
 save_time_units='microseconds'
/
&pproc_operation
 field_name='temperature',
 operation_type='transducer'
/
&pproc_operation
 field_name='pressure',
 operation_type='transducer'
/
&pproc_operation
 field_name='density',
 operation_type='transducer'
/
&pproc_operation
 field_name='specie_mass_fraction(H2)',
 operation_type='transducer'
/
&pproc_operation
 field_name='specie_mass_fraction(O2)',
 operation_type='transducer'
/
&pproc_operation
 field_name='specie_mass_fraction(N2)',
 operation_type='transducer'
/
""",
                encoding="utf-8",
            )
            # Native order: t, operation1(T), leading cell i, p, rho, Y_H2, Y_O2, Y_N2
            (case / "reactor_history.dat").write_text(
                "0.0 1500.0 1 101325.0 1.2 0.10 0.20 0.70\n"
                "0.1 1510.0 1 102000.0 1.2 0.09 0.20 0.71\n",
                encoding="utf-8",
            )
            history = load_reactor_history(case)
            self.assertEqual(history.dimensions, 1)
            self.assertEqual(history.temperature_K[0], 1500.0)
            self.assertEqual(history.coordinates["cell_i"][0], 1.0)
            self.assertEqual(history.pressure_Pa[0], 101325.0)
            self.assertAlmostEqual(history.time_s[1], 1.0e-7)
            self.assertEqual(history.species_names, ("H2", "O2", "N2"))


if __name__ == "__main__":
    unittest.main()
