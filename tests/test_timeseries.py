from __future__ import annotations

import unittest

from nrg_analysis.timeseries import derivative, threshold_crossing


class TimeSeriesTests(unittest.TestCase):
    def test_quadratic_derivative_nonuniform(self):
        time = [0.0, 0.7, 1.5, 2.2, 3.0]
        values = [t * t + 2.0 * t + 1.0 for t in time]
        deriv = derivative(time, values)
        for i in range(1, len(time) - 1):
            self.assertAlmostEqual(deriv[i], 2.0 * time[i] + 2.0, places=12)

    def test_threshold_interpolation(self):
        self.assertAlmostEqual(threshold_crossing([0, 1, 2], [0, 2, 4], 3), 1.5)


if __name__ == "__main__":
    unittest.main()
