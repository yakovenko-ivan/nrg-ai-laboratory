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


if __name__ == "__main__":
    unittest.main()
