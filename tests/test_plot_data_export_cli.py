from __future__ import annotations

import csv
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "campaign_tools" / "plot_data_export.py"


class PlotDataExportCliTests(unittest.TestCase):
    def test_pivot_cli(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "long.csv"
            with source.open("w", newline="", encoding="ascii") as f:
                writer = csv.DictWriter(f, fieldnames=["T0_K", "P0_atm", "mechanism", "tau_dTdt_s"])
                writer.writeheader()
                for p in (1, 2):
                    writer.writerow({"T0_K": 1500, "P0_atm": p, "mechanism": "KONNOV", "tau_dTdt_s": p * 1e-5})
                    writer.writerow({"T0_K": 1500, "P0_atm": p, "mechanism": "TEREZA", "tau_dTdt_s": p * 2e-5})
            out = root / "curve.dat"
            dense_csv = root / "curve.csv"
            subprocess.run(
                [
                    sys.executable, str(SCRIPT), "pivot",
                    "--input", str(source),
                    "--output", str(out),
                    "--csv-output", str(dense_csv),
                    "--x", "P0_atm",
                    "--series", "mechanism",
                    "--value", "tau_dTdt_s",
                    "--where", "T0_K=1500",
                    "--series-order", "KONNOV,TEREZA",
                ],
                check=True,
                cwd=SCRIPT.parents[1],
            )
            lines = out.read_text(encoding="ascii").splitlines()
            self.assertIn('VARIABLES = "P0_atm" "KONNOV" "TEREZA"', lines)
            self.assertIn('ZONE T = "tau_dTdt_s"', lines)
            header = '\n'.join(lines[:3])
            for forbidden in ('N=', 'E=', 'DATAPACKING=', 'I=', 'F='):
                self.assertNotIn(forbidden, header)
            self.assertEqual(sum(1 for line in lines if line[:1].isdigit()), 2)

    def test_collapse_cli(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "sparse.csv"
            source.write_text(
                "P0_atm,KONNOV,TEREZA\n"
                "1,1e-5,\n"
                "1,,2e-5\n"
                "2,3e-5,\n"
                "2,,4e-5\n",
                encoding="ascii",
            )
            dense = root / "dense.csv"
            dat = root / "dense.dat"
            subprocess.run(
                [
                    sys.executable, str(SCRIPT), "collapse",
                    "--input", str(source),
                    "--csv-output", str(dense),
                    "--key", "P0_atm",
                    "--tecplot-output", str(dat),
                    "--x", "P0_atm",
                ],
                check=True,
                cwd=SCRIPT.parents[1],
            )
            with dense.open(newline="", encoding="ascii") as f:
                rows = list(csv.DictReader(f))
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]["KONNOV"], "1e-5")
            self.assertEqual(rows[0]["TEREZA"], "2e-5")


if __name__ == "__main__":
    unittest.main()
