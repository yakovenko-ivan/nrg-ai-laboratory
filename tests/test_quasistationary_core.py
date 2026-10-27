import json
from pathlib import Path
import tempfile
import unittest

from nrg_analysis.quasistationary_core import (
    CoreProfile,
    TransientHistoryError,
    evaluate,
    load_profile,
    parse_reactor_history,
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


def _g3c_setup_text() -> str:
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


def _g3c_variables_line() -> str:
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


def _g3c_row(time_us: float, temperature_k: float) -> str:
    values = [
        time_us,
        temperature_k,
        0.00005,
        101325.0,
        0.28618308058,
        *G3C_MASS_FRACTIONS,
    ]
    return " ".join(f"{value:.12E}" for value in values)


def _write_g3c_case(case: Path, body: str) -> None:
    setup = case / "task_setup"
    setup.mkdir()
    (setup / "post_processor_1.inf").write_text(_g3c_setup_text(), encoding="utf-8")
    (case / "reactor_history.dat").write_text(body, encoding="utf-8")


def profile(name="test", dY=1e-8, relT=1e-6, relP=1e-6):
    return CoreProfile.from_mapping(name, {
        "check_wall_interval_s": 1.0,
        "required_run_control_modes": ["wall_time"],
        "activation": {
            "minimum_temperature_rise_K": 200,
            "fuel_species": "H2",
            "minimum_fuel_consumed_fraction": 0.05,
            "mode": "any",
        },
        "window": {"duration_s": 1e-4, "min_points": 5},
        "tolerances": {
            "relative_temperature_span": relT,
            "relative_pressure_span": relP,
            "relative_density_span": 1e-8,
            "max_species_mass_fraction_span": dY,
            "max_sumY_error": 1e-8,
        },
    })


def eval_history(T, p, rho, h2, h2o, prof=None):
    times = [i * 1e-5 for i in range(len(T))]
    return evaluate(
        time_s=times,
        temperature_K=T,
        pressure_Pa=p,
        density_kg_m3=rho,
        species_names=("H2", "H2O"),
        species_series={"H2": h2, "H2O": h2o},
        profile=prof or profile(),
    )


class CoreTests(unittest.TestCase):
    def test_preignition(self):
        n = 21
        r = eval_history([1000.0]*n, [101325.0]*n, [1.0]*n, [0.1]*n, [0.9]*n)
        self.assertFalse(r.reached)
        self.assertEqual(r.status, "pre_ignition")

    def test_stable_products(self):
        n = 31
        r = eval_history(
            [1000.0]*5 + [2600.0]*26,
            [101325.0]*5 + [250000.0]*26,
            [1.0]*n,
            [0.1]*5 + [0.001]*26,
            [0.9]*5 + [0.999]*26,
        )
        self.assertTrue(r.reached)
        self.assertEqual(r.status, "quasistationary")
        self.assertAlmostEqual(r.product_temperature_K, 2600.0)

    def test_v2_relaxes_species_span(self):
        n = 31
        h2 = [0.1]*5 + [0.001 + (2e-8 if i % 2 else 0.0) for i in range(26)]
        h2o = [1.0-y for y in h2]
        args = ([1000.0]*5 + [2600.0]*26, [101325.0]*5+[250000.0]*26, [1.0]*n, h2, h2o)
        self.assertFalse(eval_history(*args, prof=profile("v1", dY=1e-8)).reached)
        self.assertTrue(eval_history(*args, prof=profile("v2", dY=1e-6, relT=1e-5, relP=1e-5)).reached)

    def test_nonmonotonic_time_invalid(self):
        r = evaluate(
            time_s=[0.0, 1e-5, 1e-5], temperature_K=[1000,1000,1000],
            pressure_Pa=[1,1,1], density_kg_m3=[1,1,1],
            species_names=("H2",), species_series={"H2":[1,1,1]}, profile=profile()
        )
        self.assertEqual(r.status, "invalid_history")

    def test_online_parser_drops_partial_trailing_row(self):
        with tempfile.TemporaryDirectory() as td:
            cp = Path(td)
            ts = cp / "task_setup"
            ts.mkdir()
            (ts / "post_processor_1.inf").write_text(
                "post_processor_output_file='reactor_history.dat'\n"
                "save_time_units='microseconds'\n"
                "operations_number=5\n"
                "field_name='temperature'\n"
                "field_name='pressure'\n"
                "field_name='density'\n"
                "field_name='specie_mass_fraction(H2)'\n"
                "field_name='specie_mass_fraction(H2O)'\n",
                encoding="utf-8",
            )
            # 1D native layout: time, op1, coord, op2, op3, op4, op5
            (cp / "reactor_history.dat").write_text(
                "0 1000 1 101325 1 0.1 0.9\n"
                "0.1 1000 1 101325 1 0.1 0.9\n"
                "0.2 1000 1 101325\n",
                encoding="utf-8",
            )
            h = parse_reactor_history(cp)
            self.assertEqual(len(h["time_s"]), 2)
            self.assertAlmostEqual(h["time_s"][1], 0.1e-6)
            self.assertEqual(h["species_names"], ("H2", "H2O"))

    def test_empty_online_history_is_transient(self):
        with tempfile.TemporaryDirectory() as td:
            cp = Path(td)
            ts = cp / "task_setup"
            ts.mkdir()
            (ts / "post_processor_1.inf").write_text(
                "post_processor_output_file='reactor_history.dat'\n"
                "save_time_units='seconds'\n"
                "operations_number=3\n"
                "field_name='temperature'\nfield_name='pressure'\nfield_name='density'\n",
                encoding="utf-8",
            )
            (cp / "reactor_history.dat").write_text("", encoding="utf-8")
            with self.assertRaises(TransientHistoryError):
                parse_reactor_history(cp)

    def test_uploaded_v2_profile(self):
        root = Path(__file__).resolve().parents[1]
        p = load_profile(root / "config" / "termination_profiles.json",
                         "0d_cv_post_ignition_quasistationary_v2")
        self.assertEqual(p.check_wall_interval_s, 1.0)
        self.assertEqual(p.relative_temperature_span, 1e-5)
        self.assertEqual(p.max_species_mass_fraction_span, 1e-6)

    def test_online_headered_g3c_schema_returns_2000_physical_rows(self):
        with tempfile.TemporaryDirectory() as td:
            case = Path(td)
            body = _g3c_variables_line() + "\n" + "\n".join(
                _g3c_row(index * 0.1, 1000.0 + index * 0.001)
                for index in range(2000)
            ) + "\n"
            _write_g3c_case(case, body)

            history = parse_reactor_history(case)
            self.assertEqual(len(history["time_s"]), 2000)
            self.assertAlmostEqual(history["time_s"][0], 0.0)
            self.assertAlmostEqual(history["time_s"][-1], 199.9e-6)
            self.assertEqual(history["species_names"], G3C_SPECIES)
            self.assertEqual(history["temperature_K"][0], 1000.0)
            self.assertEqual(history["pressure_Pa"][0], 101325.0)
            self.assertEqual(history["density_kg_m3"][0], 0.28618308058)
            for index in (0, 999, 1999):
                total = sum(
                    history["species_series"][species][index]
                    for species in G3C_SPECIES
                )
                self.assertEqual(total, 1.0)

    def test_online_headered_partial_trailing_row_is_dropped(self):
        with tempfile.TemporaryDirectory() as td:
            case = Path(td)
            body = "\n".join(
                [
                    _g3c_variables_line(),
                    _g3c_row(0.0, 1000.0),
                    _g3c_row(0.11, 1000.1),
                    "0.22 1000 1 101325",
                    "",
                ]
            )
            _write_g3c_case(case, body)
            history = parse_reactor_history(case)
            self.assertEqual(len(history["time_s"]), 2)
            self.assertAlmostEqual(history["time_s"][1], 0.11e-6)

    def test_online_malformed_interior_row_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            case = Path(td)
            body = "\n".join(
                [
                    _g3c_variables_line(),
                    _g3c_row(0.0, 1000.0),
                    _g3c_row(0.11, 1000.1) + " trailing",
                    _g3c_row(0.22, 1000.2),
                    "",
                ]
            )
            _write_g3c_case(case, body)
            with self.assertRaisesRegex(ValueError, "malformed numeric row"):
                parse_reactor_history(case)

    def test_online_metadata_after_data_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            case = Path(td)
            body = "\n".join(
                [
                    _g3c_variables_line(),
                    _g3c_row(0.0, 1000.0),
                    'TITLE="late"',
                    "",
                ]
            )
            _write_g3c_case(case, body)
            with self.assertRaisesRegex(ValueError, "metadata line after numerical data"):
                parse_reactor_history(case)

    def test_online_unsupported_header_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            case = Path(td)
            body = 'ZONE T="unsupported"\n' + _g3c_row(0.0, 1000.0) + "\n"
            _write_g3c_case(case, body)
            with self.assertRaisesRegex(ValueError, "malformed numeric row"):
                parse_reactor_history(case)

    def test_online_malformed_final_newline_row_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            case = Path(td)
            body = "\n".join(
                [
                    _g3c_variables_line(),
                    _g3c_row(0.0, 1000.0),
                    "0.11 1000 1 101325 trailing",
                    "",
                ]
            )
            _write_g3c_case(case, body)
            with self.assertRaisesRegex(ValueError, "malformed numeric row"):
                parse_reactor_history(case)

    def test_online_unterminated_truncated_final_record_is_dropped(self):
        with tempfile.TemporaryDirectory() as td:
            case = Path(td)
            body = (
                _g3c_variables_line()
                + "\n"
                + _g3c_row(0.0, 1000.0)
                + "\n"
                + _g3c_row(0.11, 1000.1)
                + "\n0.22 1000 1 1.013E"
            )
            _write_g3c_case(case, body)
            history = parse_reactor_history(case)
            self.assertEqual(len(history["time_s"]), 2)


if __name__ == "__main__":
    unittest.main()
