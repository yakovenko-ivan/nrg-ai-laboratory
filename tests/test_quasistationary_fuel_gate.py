"""Fail-closed fuel-activation tests for quasistationary_core.

D3b Workstream C companion test. A profile that declares a fuel species must
never let an absent fuel silently degrade activation into the temperature gate.
"""

from __future__ import annotations

import unittest

from nrg_analysis.quasistationary_core import CoreProfile, evaluate


def make_profile(mode: str, fuel_species: str) -> CoreProfile:
    return CoreProfile.from_mapping(
        "fuel_gate",
        {
            "check_wall_interval_s": 1.0,
            "required_run_control_modes": ["wall_time"],
            "activation": {
                "mode": mode,
                "minimum_temperature_rise_K": 200.0,
                "fuel_species": fuel_species,
                "minimum_fuel_consumed_fraction": 0.05,
            },
            "window": {"duration_s": 1.0e-4, "min_points": 5},
            "tolerances": {
                "relative_temperature_span": 1.0e-5,
                "relative_pressure_span": 1.0e-5,
                "relative_density_span": 1.0e-8,
                "max_species_mass_fraction_span": 1.0e-6,
                "max_sumY_error": 1.0e-8,
            },
        },
    )


def evaluate_case(mode: str, fuel_species: str, species_names, species_series):
    n = len(next(iter(species_series.values())))
    return evaluate(
        time_s=[i * 1.0e-5 for i in range(n)],
        temperature_K=[1000.0] * 5 + [2600.0] * (n - 5),
        pressure_Pa=[101325.0] * 5 + [250000.0] * (n - 5),
        density_kg_m3=[1.0] * n,
        species_names=species_names,
        species_series=species_series,
        profile=make_profile(mode, fuel_species),
    )


class FuelActivationGateTests(unittest.TestCase):
    def test_absent_fuel_cannot_activate_in_all_mode(self):
        n = 31
        h2 = [0.1] * 5 + [0.001] * 26
        h2o = [1.0 - value for value in h2]
        result = evaluate_case("all", "NH3", ("H2", "H2O"), {"H2": h2, "H2O": h2o})
        self.assertIsNone(result.activation_fuel)
        self.assertFalse(result.activation_satisfied)
        self.assertFalse(result.reached)
        self.assertEqual(result.status, "pre_ignition")

    def test_present_fuel_consumption_activates_in_all_mode(self):
        n = 31
        nh3 = [0.2] * 5 + [0.01] * 26
        n2 = [1.0 - value for value in nh3]
        result = evaluate_case("all", "NH3", ("NH3", "N2"), {"NH3": nh3, "N2": n2})
        self.assertIsNotNone(result.activation_fuel)
        self.assertTrue(result.activation_fuel)
        self.assertTrue(result.activation_satisfied)
        self.assertTrue(result.reached)
        self.assertEqual(result.status, "quasistationary")

    def test_any_mode_keeps_legacy_temperature_activation(self):
        n = 31
        h2 = [0.1] * 5 + [0.001] * 26
        h2o = [1.0 - value for value in h2]
        result = evaluate_case("any", "NH3", ("H2", "H2O"), {"H2": h2, "H2O": h2o})
        self.assertIsNone(result.activation_fuel)
        self.assertTrue(result.activation_satisfied)
        self.assertTrue(result.reached)


if __name__ == "__main__":
    unittest.main()
