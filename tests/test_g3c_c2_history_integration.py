from __future__ import annotations

import subprocess
import unittest
from pathlib import Path

from nrg_analysis.io import load_reactor_history
from nrg_analysis.quasistationary_core import parse_reactor_history

CAMPAIGN = "0D_CV_H2_air_20mol_KEROMNES_T1000_backend_pair_smoke"
CASES = ("R000001__CH_SLATEC", "R000002__CH_CVODE")
SPECIES = (
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


def _main_repository_root() -> Path:
    """Locate the main checkout from either a normal checkout or a worktree."""
    checkout = Path(__file__).resolve().parents[1]
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"],
            cwd=checkout,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return checkout
    common = Path(completed.stdout.strip())
    if not common.is_absolute():
        common = (checkout / common).resolve()
    return common.parent


class G3CC2HistoryIntegrationTests(unittest.TestCase):
    """Read-only validation against the completed Gate C2 histories, when present."""

    def test_both_read_paths_accept_the_actual_g3c_histories(self):
        runs = _main_repository_root() / "runs" / CAMPAIGN
        available = [runs / case for case in CASES if (runs / case / "reactor_history.dat").is_file()]
        if len(available) != len(CASES):
            self.skipTest(f"Gate C2 histories not available under {runs}")

        for case in available:
            with self.subTest(case=case.name):
                offline = load_reactor_history(case)
                online = parse_reactor_history(case)

                self.assertEqual(offline.rows, 2000)
                self.assertEqual(len(online["time_s"]), 2000)
                self.assertEqual(offline.species_names, SPECIES)
                self.assertEqual(online["species_names"], SPECIES)
                self.assertAlmostEqual(offline.time_s[-1], online["time_s"][-1])
                self.assertAlmostEqual(offline.time_s[-1], 199.91e-6, places=15)

                for index in (0, 999, 1999):
                    offline_total = sum(
                        offline.species_mass_fraction(species)[index]
                        for species in SPECIES
                    )
                    online_total = sum(
                        online["species_series"][species][index]
                        for species in SPECIES
                    )
                    self.assertLess(abs(offline_total - 1.0), 1.0e-11)
                    self.assertEqual(online_total, offline_total)


if __name__ == "__main__":
    unittest.main()
