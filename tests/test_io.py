from __future__ import annotations

import tempfile
from pathlib import Path
import unittest

from nrg_analysis.io import (
    canonical_operation_name,
    load_reactor_history,
    read_numeric_rows,
)

G3C_SPECIES = (
    "H2",
    "O2",
    "N2",
    "O",
    "H",
    "OH",
    "H2O",
    "HO2",
    "H2O2",
    "AR",
    "HE",
)
# Exact binary fractions: the synthetic species sum is exactly 1.0.
G3C_MASS_FRACTIONS = (
    0.25,
    0.125,
    0.25,
    0.0625,
    0.03125,
    0.015625,
    0.125,
    0.03125,
    0.015625,
    0.0625,
    0.03125,
)


def _setup_text() -> str:
    lines = [
        "&pproc_parameters",
        " post_processor_output_file='reactor_history.dat',",
        f" operations_number={3 + len(G3C_SPECIES)},",
        " save_time=0.1,",
        " save_time_units='microseconds'",
        "/",
    ]
    fields = ["temperature", "pressure", "density"]
    fields.extend(f"specie_mass_fraction({species})" for species in G3C_SPECIES)
    for field in fields:
        lines.extend(
            [
                "&pproc_operation",
                f" field_name='{field}',",
                " operation_type='transducer'",
                "/",
            ]
        )
    return "\n".join(lines) + "\n"


def _variables_line() -> str:
    names = [
        "time [us]",
        "temperature_transducer",
        "x [m]",
        "pressure_transducer",
        "density_transducer",
    ]
    names.extend(f"specie_mass_fraction({species})_transducer" for species in G3C_SPECIES)
    return "TITLE=\"NRG post-processor\"\nVARIABLES=" + " ".join(
        f'"{name}"' for name in names
    )


def _row(time_us: float, temperature_k: float) -> str:
    values = [
        time_us,
        temperature_k,
        0.00005,
        101325.0,
        0.28618308058,
        *G3C_MASS_FRACTIONS,
    ]
    return " ".join(f"{value:.12E}" for value in values)


def _write_case(case: Path, body: str) -> None:
    setup = case / "task_setup"
    setup.mkdir()
    (setup / "post_processor# 1.inf").write_text(_setup_text(), encoding="utf-8")
    (case / "reactor_history.dat").write_text(body, encoding="utf-8")


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

    def test_headered_g3c_schema_yields_only_physical_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            case = Path(tmp)
            body = "\n".join(
                [
                    _variables_line(),
                    _row(0.0, 1000.0),
                    _row(0.11, 1000.1),
                    "",
                ]
            )
            _write_case(case, body)

            rows = read_numeric_rows(case / "reactor_history.dat")
            self.assertEqual(len(rows), 2)
            self.assertTrue(all(len(row) == 16 for row in rows))

            history = load_reactor_history(case)
            self.assertEqual(history.rows, 2)
            self.assertEqual(history.species_names, G3C_SPECIES)
            self.assertEqual(history.time_s[0], 0.0)
            self.assertAlmostEqual(history.time_s[1], 0.11e-6)
            self.assertEqual(history.temperature_K[0], 1000.0)
            self.assertEqual(history.pressure_Pa[0], 101325.0)
            self.assertEqual(history.density_kg_m3[0], 0.28618308058)
            self.assertEqual(history.coordinates["cell_i"][0], 0.00005)
            for index in range(2):
                total = sum(
                    history.species_mass_fraction(species)[index]
                    for species in G3C_SPECIES
                )
                self.assertEqual(total, 1.0)

    def test_data_row_with_trailing_text_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            case = Path(tmp)
            _write_case(case, _variables_line() + "\n" + _row(0.0, 1000.0) + " extra\n")
            with self.assertRaisesRegex(ValueError, "malformed numeric row"):
                load_reactor_history(case)

    def test_metadata_after_data_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "history.dat"
            path.write_text(
                _variables_line() + "\n" + _row(0.0, 1000.0) + '\nTITLE="late"\n',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "metadata line after numerical data"):
                read_numeric_rows(path)

    def test_unsupported_header_is_not_silently_dropped(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "history.dat"
            path.write_text('ZONE T="unsupported"\n0.0 1.0\n', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "malformed numeric row"):
                read_numeric_rows(path)

    def test_fortran_d_exponent_and_adjacent_fixed_width_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "history.dat"
            path.write_text("0.100000000000D+00-0.250000000000E-01\n", encoding="utf-8")
            self.assertEqual(read_numeric_rows(path), [[0.1, -0.025]])


if __name__ == "__main__":
    unittest.main()
