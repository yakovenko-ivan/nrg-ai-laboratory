import unittest
from pathlib import Path

from nrg_analysis.io import ReactorHistory
from nrg_analysis.quasistationary import QuasistationaryProfile, evaluate_history


def history(times, T, p, rho, h2, h2o):
    return ReactorHistory(
        case_path=Path("."),
        raw_path=Path("reactor_history.dat"),
        postprocessor_setup=Path("post_processor.inf"),
        source_time_units="seconds",
        operation_fields=(),
        time_s=tuple(times),
        coordinates={"cell_i": tuple(1.0 for _ in times)},
        observables={
            "temperature_K": tuple(T),
            "pressure_Pa": tuple(p),
            "density_kg_m3": tuple(rho),
            "Y_H2": tuple(h2),
            "Y_H2O": tuple(h2o),
        },
        species_columns={"H2": "Y_H2", "H2O": "Y_H2O"},
    )


PROFILE = QuasistationaryProfile.from_mapping(
    "test",
    {
        "required_run_control_modes": ["wall_time"],
        "activation": {
            "minimum_temperature_rise_K": 200,
            "fuel_species": "H2",
            "minimum_fuel_consumed_fraction": 0.05,
            "mode": "any",
        },
        "window": {"duration_s": 1e-4, "min_points": 5},
        "tolerances": {
            "relative_temperature_span": 1e-6,
            "relative_pressure_span": 1e-6,
            "relative_density_span": 1e-8,
            "max_species_mass_fraction_span": 1e-8,
            "max_sumY_error": 1e-8,
        },
    },
)


class QuasistationaryTests(unittest.TestCase):
    def test_induction_plateau_does_not_false_trigger(self):
        times = [i * 1e-5 for i in range(21)]
        h = history(
            times,
            [1000.0] * len(times),
            [101325.0] * len(times),
            [1.0] * len(times),
            [0.1] * len(times),
            [0.9] * len(times),
        )
        result = evaluate_history(h, PROFILE)
        self.assertFalse(result.reached)
        self.assertEqual(result.status, "pre_ignition")

    def test_stable_products_trigger_and_are_averaged(self):
        times = [i * 1e-5 for i in range(31)]
        T = [1000.0] * 5 + [2600.0] * 26
        p = [101325.0] * 5 + [250000.0] * 26
        rho = [1.0] * 31
        h2 = [0.1] * 5 + [0.001] * 26
        h2o = [0.9] * 5 + [0.999] * 26
        result = evaluate_history(history(times, T, p, rho, h2, h2o), PROFILE)
        self.assertTrue(result.reached)
        self.assertEqual(result.status, "quasistationary")
        self.assertAlmostEqual(result.product_temperature_K, 2600.0)
        self.assertAlmostEqual(result.product_density_kg_m3, 1.0)
        self.assertLessEqual(result.max_species_mass_fraction_span, 1e-8)

    def test_post_ignition_drift_does_not_trigger(self):
        times = [i * 1e-5 for i in range(31)]
        T = [1000.0] * 5 + [2400.0 + 2.0 * i for i in range(26)]
        p = [101325.0] * 5 + [200000.0 + 100.0 * i for i in range(26)]
        rho = [1.0] * 31
        h2 = [0.1] * 5 + [0.02 - 1e-4 * i for i in range(26)]
        h2o = [0.9] * 5 + [0.98 + 1e-4 * i for i in range(26)]
        result = evaluate_history(history(times, T, p, rho, h2, h2o), PROFILE)
        self.assertFalse(result.reached)
        self.assertEqual(result.status, "not_quasistationary")


if __name__ == "__main__":
    unittest.main()
