"""Regression tests for implicit Fortran exponent notation in NRG output.

When a value's exponent does not fit the configured two-digit Fortran exponent
field, NRG's post-processor writes the exponent without the ``E`` marker and
without any separator, for example ``0.566278959029-300``.  Both trusted
reactor-history readers (``nrg_analysis.io`` and
``nrg_analysis.quasistationary_core``) must recognise such tokens, normalise
them into valid Python float syntax, and keep every previously supported
behaviour: explicit ``E``/``D`` notation, adjacent fixed-width numeric fields,
metadata parsing, strict malformed-row rejection, and tolerance of one
partially written final record in the online reader.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
from pathlib import Path
import unittest

from nrg_analysis import io as lab_io
from nrg_analysis import quasistationary_core as core

SPECIES = ("C2H2", "O2", "N2", "AR")
OVERFLOW_TOKEN = "0.461710259574-306"
OVERFLOW_VALUE = float("0.461710259574e-306")


def _setup_text() -> str:
    lines = [
        "&pproc_parameters",
        " post_processor_output_file='reactor_history.dat',",
        f" operations_number={3 + len(SPECIES)},",
        " save_time=0.1,",
        " save_time_units='microseconds'",
        "/",
    ]
    fields = ["temperature", "pressure", "density"]
    fields.extend(f"specie_mass_fraction({species})" for species in SPECIES)
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
    names.extend(f"specie_mass_fraction({species})_transducer" for species in SPECIES)
    return 'TITLE="NRG post-processor"\nVARIABLES=' + " ".join(
        f'"{name}"' for name in names
    )


def _write_case(case: Path, body: str) -> None:
    setup = case / "task_setup"
    setup.mkdir()
    (setup / "post_processor_1.inf").write_text(_setup_text(), encoding="utf-8")
    (case / "reactor_history.dat").write_text(body, encoding="utf-8")


def _float_text(value: float, style: str) -> str:
    """Format one value in explicit or Fortran implicit exponent notation.

    Fortran only omits the ``E`` marker when the exponent does not fit its
    declared two-digit ``Ee`` field, so the implicit form always carries a
    three-digit exponent.  Shorter exponents keep explicit ``E`` notation.
    """
    text = f"{value:.12E}"
    if style == "explicit":
        return text
    mantissa, exponent = text.split("E")
    if len(exponent.lstrip("+-")) != 3:
        return text
    return f"{mantissa}{exponent}"


def _row(values, style: str = "explicit") -> str:
    return " ".join(_float_text(value, style) for value in values)


def _native_row(time_us, temperature, pressure, density, species, style="explicit"):
    # Native NRG layout: time, operation_1, leading coordinate, operation_2, ...
    values = [time_us, temperature, 5.0e-5, pressure, density, *species]
    return _row(values, style)


class ImplicitExponentTokenTests(unittest.TestCase):
    def test_normalize_float_token_recognises_all_supported_forms(self):
        self.assertEqual(
            lab_io.normalize_float_token("0.566278959029-300"),
            "0.566278959029e-300",
        )
        self.assertEqual(
            core._normalize_float_token("0.566278959029-300"),
            "0.566278959029e-300",
        )
        self.assertEqual(
            lab_io.normalize_float_token("-0.250000000000D-01"),
            "-0.250000000000E-01",
        )
        self.assertEqual(lab_io.normalize_float_token("101325.0"), "101325.0")
        self.assertEqual(lab_io.normalize_float_token("123"), "123")
        self.assertEqual(
            float(lab_io.normalize_float_token(OVERFLOW_TOKEN)), OVERFLOW_VALUE
        )

    def test_implicit_exponent_does_not_merge_adjacent_negative_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "history.dat"
            path.write_text("0.5-0.3\n", encoding="utf-8")
            self.assertEqual(lab_io.read_numeric_rows(path), [[0.5, -0.3]])

    def test_short_bare_suffixes_are_not_read_as_implicit_exponents(self):
        # A one- or two-digit bare suffix is indistinguishable from two adjacent
        # numeric fields, so it must never be guessed into an exponent.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "history.dat"
            path.write_text("1.2-3\n1.2-30\n", encoding="utf-8")
            self.assertEqual(lab_io.read_numeric_rows(path), [[1.2, -3], [1.2, -30]])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "history.dat"
            path.write_text("1.2-300\n", encoding="utf-8")
            self.assertEqual(lab_io.read_numeric_rows(path), [[1.2e-300]])
            self.assertEqual(lab_io.parse_number_line("1.2-300"), [1.2e-300])
            self.assertEqual(lab_io.parse_number_line("1.2-30"), [1.2, -30])

    def test_read_numeric_rows_accepts_implicit_and_explicit_notation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "history.dat"
            path.write_text(
                f"{OVERFLOW_TOKEN}  0.100000000000D+00-0.250000000000E-01\n",
                encoding="utf-8",
            )
            self.assertEqual(
                lab_io.read_numeric_rows(path), [[OVERFLOW_VALUE, 0.1, -0.025]]
            )


class IoReaderImplicitExponentTests(unittest.TestCase):
    def test_load_reactor_history_with_overflow_exponent_species(self):
        with tempfile.TemporaryDirectory() as tmp:
            case = Path(tmp)
            body = "\n".join(
                [
                    _variables_line(),
                    _native_row(0.0, 1200.0, 101325.0, 0.290779043921, (0.7, 0.21, 0.7, 0.0)),
                    "  {t}  0.313187374687E+04  0.500000000000E-04"
                    "  0.272916694972E+06  0.290779043922E+00  {y}"
                    "  0.403539500492E-01  0.713040166325E+00  0.000000000000E+00\n".format(
                        t="0.692010000042E+05", y=OVERFLOW_TOKEN
                    ),
                    "",
                ]
            )
            _write_case(case, body)
            history = lab_io.load_reactor_history(case)
            self.assertEqual(history.rows, 2)
            self.assertEqual(history.dimensions, 1)
            self.assertEqual(history.temperature_K[1], 3131.87374687)
            self.assertEqual(history.pressure_Pa[1], 272916.694972)
            self.assertEqual(history.species_mass_fraction("C2H2")[1], OVERFLOW_VALUE)
            self.assertAlmostEqual(history.time_s[1], 0.0692010000042)
            self.assertTrue(history.all_values_finite())

    def test_malformed_row_still_rejected_with_implicit_exponents(self):
        with tempfile.TemporaryDirectory() as tmp:
            case = Path(tmp)
            body = "\n".join(
                [
                    _native_row(0.0, 1200.0, 101325.0, 0.29, (0.7, 0.21, 0.7, 0.0)),
                    _row([0.1, 1200.0, 5.0e-5, 101325.0, 0.29, 0.7, 0.21, 0.7, 0.0], "implicit")
                    + " extra\n",
                    "",
                ]
            )
            _write_case(case, body)
            with self.assertRaisesRegex(ValueError, "malformed numeric row"):
                lab_io.load_reactor_history(case)


class CoreReaderImplicitExponentTests(unittest.TestCase):
    def test_parse_reactor_history_matches_explicit_notation(self):
        # Genuine overflow-form values, written with and without the ``E`` marker.
        species = (1.0e-306, 2.0e-307, 6.0e-306, 1.0e-307)
        rows_explicit = [
            _native_row(t, 1000.0 + t, 101325.0, 2.9e-307, species)
            for t in (0.0, 1.0, 2.0)
        ]
        rows_implicit = [
            _native_row(t, 1000.0 + t, 101325.0, 2.9e-307, species, "implicit")
            for t in (0.0, 1.0, 2.0)
        ]
        # The implicit body must really contain the marker-less overflow form.
        self.assertIn("1.000000000000-306", rows_implicit[0])
        self.assertNotIn("E-306", rows_implicit[0])
        self.assertIn("E-306", rows_explicit[0])
        with tempfile.TemporaryDirectory() as tmp:
            explicit_case = Path(tmp) / "explicit"
            implicit_case = Path(tmp) / "implicit"
            explicit_case.mkdir()
            implicit_case.mkdir()
            _write_case(explicit_case, "\n".join(rows_explicit) + "\n")
            _write_case(implicit_case, "\n".join(rows_implicit) + "\n")
            a = core.parse_reactor_history(explicit_case)
            b = core.parse_reactor_history(implicit_case)
            self.assertEqual(a, b)
            self.assertEqual(len(b["time_s"]), 3)
            self.assertEqual(b["species_names"], SPECIES)

    def test_parse_reactor_history_reads_overflow_exponent(self):
        with tempfile.TemporaryDirectory() as tmp:
            case = Path(tmp)
            body = (
                " 0.692010000042E+05  0.313187374687E+04  0.500000000000E-04"
                "  0.272916694972E+06  0.290779043922E+00  {y}"
                "  0.403539500492E-01  0.713040166325E+00  0.000000000000E+00\n"
            ).format(y=OVERFLOW_TOKEN)
            _write_case(case, _variables_line() + "\n" + body)
            history = core.parse_reactor_history(case)
            self.assertEqual(len(history["time_s"]), 1)
            self.assertEqual(history["species_series"]["C2H2"][0], OVERFLOW_VALUE)
            self.assertEqual(history["temperature_K"][0], 3131.87374687)

    def test_online_reader_still_drops_one_partial_final_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            case = Path(tmp)
            body = (
                _variables_line()
                + "\n"
                + _native_row(0.0, 1200.0, 101325.0, 0.29, (0.1, 0.2, 0.6, 1.0e-306), "implicit")
                + "\n"
                + _native_row(1.0, 1201.0, 101325.0, 0.29, (0.1, 0.2, 0.6, 1.0e-306), "implicit")
                + "\n 0.2 1202.0 5.0e-05\n"
            )
            _write_case(case, body)
            history = core.parse_reactor_history(case)
            self.assertEqual(len(history["time_s"]), 2)
            self.assertEqual(history["time_s"][1], 1.0e-6)

    def test_metadata_after_data_still_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            case = Path(tmp)
            body = (
                _variables_line()
                + "\n"
                + _native_row(0.0, 1200.0, 101325.0, 0.29, (0.1, 0.2, 0.6, 0.1), "implicit")
                + '\nTITLE="late"\n'
            )
            _write_case(case, body)
            with self.assertRaisesRegex(ValueError, "metadata line after numerical data"):
                core.parse_reactor_history(case)

    def test_evaluate_case_with_implicit_exponent_history(self):
        body_rows = []
        for index in range(31):
            temperature = 1000.0 if index < 5 else 2600.0
            fuel = 0.1 if index < 5 else 0.001
            species = (fuel, 0.2, 1.0 - fuel - 0.2 - 1.0e-306, 1.0e-306)
            body_rows.append(
                _native_row(float(index), temperature, 101325.0, 0.29, species, "implicit")
            )
        profile = core.CoreProfile.from_mapping(
            "implicit_exponent_test",
            {
                "check_wall_interval_s": 1.0,
                "required_run_control_modes": ["wall_time"],
                "activation": {
                    "minimum_temperature_rise_K": 200.0,
                    "fuel_species": "C2H2",
                    "minimum_fuel_consumed_fraction": 0.05,
                    "mode": "all",
                },
                "window": {"duration_s": 1.0e-5, "min_points": 5},
                "tolerances": {
                    "relative_temperature_span": 1.0e-6,
                    "relative_pressure_span": 1.0e-6,
                    "relative_density_span": 1.0e-8,
                    "max_species_mass_fraction_span": 1.0e-8,
                    "max_sumY_error": 1.0e-8,
                },
            },
        )
        with tempfile.TemporaryDirectory() as tmp:
            case = Path(tmp)
            _write_case(case, "\n".join(body_rows) + "\n")
            self.assertIn("1.000000000000-306", body_rows[0])
            result = core.evaluate_case(case, profile)
            self.assertEqual(result.history_rows, 31)
            self.assertTrue(result.activation_satisfied)
            self.assertTrue(result.reached)
            self.assertEqual(result.status, "quasistationary")
            self.assertAlmostEqual(result.product_temperature_K, 2600.0)


def _main_checkout_root() -> Path:
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


I7C_CASE = Path("runs") / "d3b_c2h2_varatharajan_phi1_smoke" / "R000001"
I7C_PROFILE = "0d_cv_post_ignition_quasistationary_c2h2_v1"


class ArchivedI7cReplayTests(unittest.TestCase):
    """Read-only replay of the unchanged archived I7c VARATHARAJAN history."""

    def test_both_readers_and_profile_replay_the_archived_i7c_case(self):
        main_root = _main_checkout_root()
        case = main_root / I7C_CASE
        history_path = case / "reactor_history.dat"
        status_path = case / "run_status.json"
        registry_path = main_root / "config" / "termination_profiles.json"
        if not (
            history_path.is_file() and status_path.is_file() and registry_path.is_file()
        ):
            self.skipTest(f"archived I7c case or registry not available under {main_root}")

        history_sha_before = hashlib.sha256(history_path.read_bytes()).hexdigest()
        status_sha_before = hashlib.sha256(status_path.read_bytes()).hexdigest()
        recorded = json.loads(status_path.read_text(encoding="utf-8"))

        offline = lab_io.load_reactor_history(case)
        online = core.parse_reactor_history(case)
        profile = core.load_profile(registry_path, I7C_PROFILE)
        result = core.evaluate(profile=profile, **online)

        # 1. Both trusted readers must accept the unchanged archived history and
        #    agree on its shape and time base.
        self.assertEqual(offline.rows, 138409)
        self.assertEqual(len(online["time_s"]), 138409)
        self.assertEqual(offline.rows, len(online["time_s"]))
        self.assertEqual(len(online["species_names"]), 17)
        self.assertEqual(offline.dimensions, 1)
        self.assertEqual(offline.time_s, online["time_s"])
        self.assertAlmostEqual(online["time_s"][-1], 0.0692040000042, places=15)

        # 2. The implicit Fortran exponent really occurs in this history and is
        #    normalised rather than truncated: the final C2H2 value is the last
        #    ``0.461710259574-306`` token of the file.
        self.assertEqual(offline.species_mass_fraction("C2H2")[-1], float("0.461710259574e-306"))
        self.assertEqual(online["species_series"]["C2H2"][-1], float("0.461710259574e-306"))
        for index in (0, 70000, 138408):
            offline_total = sum(
                offline.species_mass_fraction(species)[index]
                for species in online["species_names"]
            )
            online_total = sum(
                online["species_series"][species][index] for species in online["species_names"]
            )
            self.assertLess(abs(offline_total - 1.0), 1.0e-11)
            self.assertEqual(online_total, offline_total)

        # 3. The C2H2 profile must come from the installed I3 registry of the
        #    original checkout, and must be byte-identical to the registry the
        #    stopped I7c run recorded.
        self.assertEqual(
            core.profile_file_sha256(registry_path),
            recorded.get("termination_profile_registry_sha256"),
        )
        self.assertEqual(recorded.get("termination_profile"), I7C_PROFILE)
        self.assertEqual(profile.name, I7C_PROFILE)
        self.assertEqual(profile.activation_mode, "all")
        self.assertEqual(profile.fuel_species, "C2H2")

        # 4. Offline quasistationarity evaluation must complete decisively.
        self.assertEqual(result.history_rows, 138409)
        self.assertTrue(result.activation_satisfied)
        self.assertGreaterEqual(result.fuel_consumed_fraction, profile.min_fuel_consumed_fraction)
        self.assertGreaterEqual(result.temperature_rise_K, profile.min_temperature_rise_K)
        self.assertGreaterEqual(result.window_points, profile.min_window_points)
        self.assertGreaterEqual(result.window_duration_s, profile.window_duration_s)
        self.assertTrue(result.reached)
        self.assertEqual(result.status, "quasistationary")
        self.assertTrue(all(result.criteria.values()))
        self.assertAlmostEqual(result.product_temperature_K, 3131.87374687, places=6)
        self.assertAlmostEqual(result.product_pressure_Pa, 272916.694972, places=6)

        # 5. The replay is strictly read-only for the archived scientific record.
        self.assertEqual(hashlib.sha256(history_path.read_bytes()).hexdigest(), history_sha_before)
        self.assertEqual(hashlib.sha256(status_path.read_bytes()).hexdigest(), status_sha_before)

        # 6. The original I7c execution remains an operator-stopped, non-successful
        #    case.  This offline replay result does not change that state.
        self.assertEqual(recorded.get("status"), "stopped")
        self.assertIs(recorded.get("physical_condition_met"), False)
        self.assertEqual(recorded.get("nrg_termination_reason"), "external_stop_request")


if __name__ == "__main__":
    unittest.main()
