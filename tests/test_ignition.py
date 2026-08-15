from __future__ import annotations

from pathlib import Path
import unittest

from nrg_analysis.ignition import temperature_rise
from nrg_analysis.io import ReactorHistory


class IgnitionTests(unittest.TestCase):
    def test_temperature_rise_interpolation(self):
        t = (0.0, 1.0, 2.0)
        history = ReactorHistory(
            case_path=Path("."), raw_path=Path("raw"), postprocessor_setup=Path("setup"),
            source_time_units="seconds", operation_fields=("temperature", "pressure", "density"),
            time_s=t,
            coordinates={"cell_i": (1.0, 1.0, 1.0)},
            observables={
                "temperature_K": (1000.0, 1200.0, 1600.0),
                "pressure_Pa": (1.0, 1.0, 1.0),
                "density_kg_m3": (1.0, 1.0, 1.0),
            },
            species_columns={},
        )
        metric = temperature_rise(history, 400.0)
        self.assertAlmostEqual(metric.time_s, 1.5)


if __name__ == "__main__":
    unittest.main()
