"""Compatibility tests for the current 0D seven-namelist NRG interface.

These tests exercise the Python generator/identity layer only.  They never
prepare NRG cases, invoke the package interface, or start CFD.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from campaign_tools.campaign_generator_0d import (
    ALL_NAMELIST_GROUPS,
    ALLOWED_CHEMISTRY_BACKENDS,
    CHEMISTRY_GROUP,
    CHEMISTRY_GROUP_KEYS,
    NAMELIST_GROUPS,
    canonical_scientific_payload,
    compute_case_fingerprint,
    create_files,
    effective_thermo_transport,
    expand_campaign,
    read_campaign,
    render_namelist,
    task_setup_resource_provenance,
)
from campaign_tools.campaign_identity import (
    attempt_fingerprint,
    build_policy,
    identity_fingerprint,
    validate_overrides,
)
from nrg_analysis.laboratory import Laboratory
from nrg_analysis.provenance import sha256_file


EXPECTED_CHEMISTRY_KEYS = {
    "chemistry_backend",
    "chemistry_cvode_relative_tolerance",
    "chemistry_cvode_absolute_tolerance",
    "chemistry_cvode_max_steps",
    "chemistry_qss1_relative_change_limit",
    "chemistry_qss1_minimum_internal_step",
    "chemistry_qss1_active_concentration_fraction",
    "chemistry_qss1_step_growth_factor",
    "chemistry_qss1_max_steps",
    "chemistry_qss2_error_tolerance",
    "chemistry_qss2_minimum_internal_step",
    "chemistry_qss2_active_concentration_fraction",
    "chemistry_qss2_max_steps",
}


def make_lab(root: Path) -> Laboratory:
    for p in (
        root / "campaigns",
        root / "runs",
        root / "studies",
        root / "resources" / "task_setup",
        root / "bin",
        root / "config",
    ):
        p.mkdir(parents=True, exist_ok=True)

    hydrogen = root / "resources" / "task_setup" / "chemical_mechanisms" / "hydrogen"
    thermo = root / "resources" / "task_setup" / "thermophysical_data"
    hydrogen.mkdir(parents=True, exist_ok=True)
    thermo.mkdir(parents=True, exist_ok=True)
    for mechanism in ("KEROMNES", "KONNOV", "ZHANG", "TEREZA"):
        (hydrogen / f"{mechanism}.txt").write_text(
            f"synthetic {mechanism} kinetic mechanism\n", encoding="utf-8"
        )
        (thermo / f"{mechanism}_THERMO.txt").write_text(
            f"synthetic {mechanism} thermo\n", encoding="utf-8"
        )
        (thermo / f"{mechanism}_TRANSDATA.txt").write_text(
            f"synthetic {mechanism} transport\n", encoding="utf-8"
        )
    (thermo / "molar_masses.dat").write_text("synthetic molar masses\n", encoding="utf-8")

    computing = root / "bin" / "computing_module"
    computing.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    computing.chmod(0o755)
    interface = root / "bin" / "package_interface"
    interface.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    interface.chmod(0o755)

    runner_config = root / "config" / "campaign_runner.json"
    runner_config.write_text(
        json.dumps(
            {
                "threads": 1,
                "max_concurrent_cases": 1,
                "limit_library_threads": True,
                "poll_seconds": 0.01,
                "max_runtime_seconds": 10,
                "success_exit_codes": [0],
                "skip_statuses": ["finished", "condition_met"],
                "rerun_failed": True,
                "archive_old_monitor_files": False,
                "monitor_log_each_read": False,
                "verify_case_fingerprint": True,
            }
        ),
        encoding="utf-8",
    )
    lab_toml = root / "config" / "laboratory.toml"
    lab_toml.write_text(
        f"""
[paths]
research_root = "{root}"
campaign_root = "{root / 'campaigns'}"
runs_root = "{root / 'runs'}"
studies_root = "{root / 'studies'}"
task_setup_template = "{root / 'resources' / 'task_setup'}"

[runtime]
computing_module = "{computing}"
package_interface_0d = "{interface}"

[execution]
default_threads = 1
runner_config = "{runner_config}"
""",
        encoding="utf-8",
    )
    return Laboratory.load(lab_toml)


def write_campaign(
    path: Path,
    name: str,
    *,
    temps: list[float],
    backends: list[str] | None = None,
    chemistry_defaults: dict | None = None,
    identity_fields: list[str] | None = None,
    mechanism: str = "keromnes",
    supplied_thermo: str | None = None,
    supplied_transport: str | None = None,
    numerical_variants: str | None = None,
) -> None:
    lines = [
        "[campaign]",
        f'name = "{name}"',
        f'generator_output_directory = "{name}_generated"',
        "max_cases = 100",
        "",
    ]
    if identity_fields:
        lines += [
            "[case_identity]",
            "fields = [" + ", ".join(json.dumps(f) for f in identity_fields) + "]",
            "",
        ]
    lines += ["[defaults.case_config]", ""]
    lines += [
        "[defaults.reactor_config]",
        'reactor_type = "constant_volume"',
        "cells_number_x = 2",
        "cell_length_x = 1.0e-4",
        "",
    ]
    lines += [
        "[defaults.mixture_config]",
        "hydrogen_mole_percent = 20.0",
        "n2_o2_molar_ratio = 3.762",
        "initial_temperature = 1000.0",
        "initial_pressure = 101325.0",
        "",
    ]
    lines += [
        "[defaults.physics_config]",
        f'mechanism_id = "{mechanism}"',
        'solver_id = "cpm"',
        "initial_time_step = 1.0e-8",
        "cfl_enabled = false",
        "cfl_coefficient = 0.25",
    ]
    if supplied_thermo:
        lines.append(f'thermo_data_file = "{supplied_thermo}"')
    if supplied_transport:
        lines.append(f'transport_data_file = "{supplied_transport}"')
    lines.append("")
    if chemistry_defaults is not None:
        lines.append("[defaults.chemistry_backend_config]")
        for key, value in chemistry_defaults.items():
            lines.append(f"{key} = {json.dumps(value)}")
        lines.append("")
    lines += [
        "[defaults.run_control_config]",
        'termination_mode = "either"',
        "final_time_ms = 2.0",
        "wall_time_limit_s = 3600.0",
        "wall_time_reserve_s = 60.0",
        "",
        "[defaults.output_config]",
        "postprocess_interval_us = 0.1",
        "field_save_interval_us = 1000.0",
        "checkpoint_interval_us = 250.0",
        "save_spatial_fields = false",
        "",
        "[sweep.mixture_config]",
        "initial_temperature = " + json.dumps(temps),
        "",
    ]
    if backends:
        lines += [
            "[sweep.chemistry_backend_config]",
            "chemistry_backend = " + json.dumps(backends),
            "",
        ]
    if numerical_variants:
        lines.append(numerical_variants)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def expand(path: Path, lab: Laboratory):
    return expand_campaign(read_campaign(path), lab)


class ChemistryBackendCompatibilityTests(unittest.TestCase):

    def test_seven_group_schema_matches_current_fortran_declarations(self):
        self.assertEqual(len(NAMELIST_GROUPS), 6)
        self.assertEqual(len(ALL_NAMELIST_GROUPS), 7)
        self.assertIn(CHEMISTRY_GROUP, ALL_NAMELIST_GROUPS)
        self.assertEqual(CHEMISTRY_GROUP_KEYS, EXPECTED_CHEMISTRY_KEYS)
        self.assertEqual(len(CHEMISTRY_GROUP_KEYS), 13)
        self.assertEqual(ALLOWED_CHEMISTRY_BACKENDS, {"slatec", "cvode", "qss1", "qss2"})

        groups = {
            "case_config": {},
            "reactor_config": {},
            "mixture_config": {},
            "physics_config": {},
            "chemistry_backend_config": {},
            "run_control_config": {},
            "output_config": {},
        }
        text = render_namelist(groups)
        for group in ALL_NAMELIST_GROUPS:
            self.assertIn(f"&{group}", text)
        self.assertIn("&chemistry_backend_config\n/\n", text)

    def test_legacy_definition_renders_empty_chemistry_group(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            lab = make_lab(root)
            definition = root / "legacy.toml"
            write_campaign(definition, "legacy", temps=[1000.0])
            cases = expand(definition, lab)
            self.assertEqual(cases[0]["groups"][CHEMISTRY_GROUP], {})
            text = render_namelist(cases[0]["groups"])
            self.assertIn("&chemistry_backend_config\n/\n", text)
            self.assertEqual(text.count("&chemistry_backend_config"), 1)

    def test_explicit_slatec_and_cvode_values_render_correctly(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            lab = make_lab(root)
            slatec = root / "slatec.toml"
            write_campaign(
                slatec,
                "slatec",
                temps=[1000.0],
                chemistry_defaults={
                    "chemistry_backend": "slatec",
                    "chemistry_cvode_relative_tolerance": 1.0e-8,
                    "chemistry_cvode_absolute_tolerance": 1.0e-12,
                    "chemistry_cvode_max_steps": 10000,
                },
            )
            text = render_namelist(expand(slatec, lab)[0]["groups"])
            self.assertIn("chemistry_backend = 'slatec'", text)
            self.assertIn("chemistry_cvode_relative_tolerance = 1e-08", text)
            self.assertIn("chemistry_cvode_absolute_tolerance = 1e-12", text)
            self.assertIn("chemistry_cvode_max_steps = 10000", text)

            cvode = root / "cvode.toml"
            write_campaign(
                cvode,
                "cvode",
                temps=[1000.0],
                chemistry_defaults={
                    "chemistry_backend": "cvode",
                    "chemistry_cvode_relative_tolerance": 2.5e-9,
                    "chemistry_cvode_max_steps": 25000,
                    "chemistry_qss1_step_growth_factor": 1.05,
                },
            )
            text = render_namelist(expand(cvode, lab)[0]["groups"])
            self.assertIn("chemistry_backend = 'cvode'", text)
            self.assertIn("chemistry_cvode_relative_tolerance = 2.5e-09", text)
            self.assertIn("chemistry_cvode_max_steps = 25000", text)
            self.assertIn("chemistry_qss1_step_growth_factor = 1.05", text)

    def test_invalid_chemistry_configuration_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            lab = make_lab(root)

            unknown = root / "unknown.toml"
            write_campaign(
                unknown, "unknown", temps=[1000.0],
                chemistry_defaults={"chemistry_not_a_fortran_member": 1},
            )
            with self.assertRaisesRegex(ValueError, "unsupported key"):
                read_campaign(unknown)

            bad_backend = root / "bad_backend.toml"
            write_campaign(
                bad_backend, "bad_backend", temps=[1000.0],
                chemistry_defaults={"chemistry_backend": "duff"},
            )
            with self.assertRaisesRegex(ValueError, "chemistry_backend must be one of"):
                expand(bad_backend, lab)

            bad_tolerance = root / "bad_tolerance.toml"
            write_campaign(
                bad_tolerance, "bad_tolerance", temps=[1000.0],
                chemistry_defaults={"chemistry_cvode_relative_tolerance": -1.0e-8},
            )
            with self.assertRaisesRegex(ValueError, "finite and positive"):
                expand(bad_tolerance, lab)

            bad_type = root / "bad_type.toml"
            write_campaign(
                bad_type, "bad_type", temps=[1000.0],
                chemistry_defaults={"chemistry_cvode_max_steps": 10000.0},
            )
            with self.assertRaisesRegex(TypeError, "must be an integer"):
                expand(bad_type, lab)

            bad_growth = root / "bad_growth.toml"
            write_campaign(
                bad_growth, "bad_growth", temps=[1000.0],
                chemistry_defaults={"chemistry_qss1_step_growth_factor": 0.9},
            )
            with self.assertRaisesRegex(ValueError, ">= 1"):
                expand(bad_growth, lab)

    def test_backend_sweep_yields_six_unique_cases_and_noncolliding_paths(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            lab = make_lab(root)
            definition = root / "backend_sweep.toml"
            write_campaign(
                definition,
                "backend_sweep",
                temps=[1000.0, 1250.0, 1500.0],
                backends=["slatec", "cvode"],
                identity_fields=[
                    "mixture_config.initial_temperature",
                    "chemistry_backend_config.chemistry_backend",
                ],
            )
            cases = expand(definition, lab)
            self.assertEqual(len(cases), 6)
            fingerprints = {c["case_fingerprint"] for c in cases}
            directories = {c["case_directory"] for c in cases}
            self.assertEqual(len(fingerprints), 6)
            self.assertEqual(len(directories), 6)
            backends = [
                c["groups"][CHEMISTRY_GROUP]["chemistry_backend"] for c in cases
            ]
            self.assertEqual(backends.count("slatec"), 3)
            self.assertEqual(backends.count("cvode"), 3)
            self.assertTrue(any("CH_SLATEC" in d for d in directories))
            self.assertTrue(any("CH_CVODE" in d for d in directories))

            out = create_files(
                read_campaign(definition), definition, cases, False, lab
            )
            self.assertEqual(len(list((out / "_setups").glob("*.nml"))), 6)
            self.assertEqual(len(list((out / "_setups").glob("*.json"))), 6)

    def test_backend_identity_and_attempt_fingerprint_semantics(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            lab = make_lab(root)
            definition = root / "identity.toml"
            write_campaign(
                definition,
                "identity",
                temps=[1000.0],
                backends=["slatec", "cvode"],
                identity_fields=[
                    "mixture_config.initial_temperature",
                    "chemistry_backend_config.chemistry_backend",
                ],
            )
            data = read_campaign(definition)
            cases = expand_campaign(data, lab)
            by_backend = {
                c["groups"][CHEMISTRY_GROUP]["chemistry_backend"]: c for c in cases
            }
            slatec, cvode = by_backend["slatec"], by_backend["cvode"]
            self.assertNotEqual(slatec["case_fingerprint"], cvode["case_fingerprint"])
            self.assertNotEqual(
                slatec["generated_attempt_fingerprint"],
                cvode["generated_attempt_fingerprint"],
            )

            # Same logical identity, perturbed CVODE tolerance -> new attempt hash.
            perturbed_definition = root / "perturbed.toml"
            write_campaign(
                perturbed_definition,
                "perturbed",
                temps=[1000.0],
                chemistry_defaults={
                    "chemistry_backend": "cvode",
                    "chemistry_cvode_relative_tolerance": 1.0e-10,
                },
                identity_fields=[
                    "mixture_config.initial_temperature",
                    "chemistry_backend_config.chemistry_backend",
                ],
            )
            perturbed = expand(perturbed_definition, lab)[0]
            policy = build_policy(data)
            self.assertEqual(
                identity_fingerprint(perturbed["groups"], policy),
                identity_fingerprint(cvode["groups"], policy),
            )
            self.assertNotEqual(
                perturbed["generated_attempt_fingerprint"],
                cvode["generated_attempt_fingerprint"],
            )

            # Repeat generation is deterministic.
            repeat = expand_campaign(read_campaign(definition), lab)
            self.assertEqual(
                [(c["case_fingerprint"], c["generated_attempt_fingerprint"]) for c in cases],
                [(c["case_fingerprint"], c["generated_attempt_fingerprint"]) for c in repeat],
            )

            # Chemistry controls are not freely attempt-tunable by default.
            _accepted, rejected = validate_overrides(
                {
                    "chemistry_backend_config.chemistry_backend": "qss1",
                    "chemistry_backend_config.chemistry_cvode_relative_tolerance": 1.0e-9,
                },
                policy,
            )
            self.assertEqual(len(rejected), 2)

            # Legacy empty group and explicit backend are both deterministic but
            # are distinguishable attempts (explicit provenance is recorded).
            legacy_definition = root / "legacy_attempt.toml"
            write_campaign(legacy_definition, "legacy_attempt", temps=[1000.0])
            legacy = expand(legacy_definition, lab)[0]
            explicit_definition = root / "explicit_attempt.toml"
            write_campaign(
                explicit_definition,
                "explicit_attempt",
                temps=[1000.0],
                chemistry_defaults={"chemistry_backend": "slatec"},
            )
            explicit = expand(explicit_definition, lab)[0]
            self.assertNotEqual(
                legacy["generated_attempt_fingerprint"],
                explicit["generated_attempt_fingerprint"],
            )
            self.assertEqual(
                legacy["generated_attempt_fingerprint"],
                expand_campaign(read_campaign(legacy_definition), lab)[0][
                    "generated_attempt_fingerprint"
                ],
            )

    def test_chemistry_backend_numerical_variant_axis(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            lab = make_lab(root)
            definition = root / "variant_axis.toml"
            write_campaign(
                definition,
                "variant_axis",
                temps=[1000.0],
                numerical_variants=(
                    "[[numerical_variants]]\n"
                    'name = "slatec"\n'
                    'chemistry_backend = "slatec"\n'
                    "\n"
                    "[[numerical_variants]]\n"
                    'name = "cvode"\n'
                    'chemistry_backend = "cvode"\n'
                ),
            )
            data = read_campaign(definition)
            policy = build_policy(data)
            self.assertIn(
                "chemistry_backend_config.chemistry_backend",
                policy["identity_fields"],
            )
            cases = expand_campaign(data, lab)
            self.assertEqual(len(cases), 2)
            self.assertEqual(
                {c["groups"][CHEMISTRY_GROUP]["chemistry_backend"] for c in cases},
                {"slatec", "cvode"},
            )
            self.assertEqual(len({c["case_fingerprint"] for c in cases}), 2)
            self.assertEqual(len({c["case_directory"] for c in cases}), 2)
            self.assertEqual(len({c["generated_attempt_fingerprint"] for c in cases}), 2)
            for case in cases:
                self.assertIn(
                    f"backend={case['groups'][CHEMISTRY_GROUP]['chemistry_backend']}",
                    case["label"],
                )
                self.assertIn("V_", case["case_directory"])

    def test_legacy_policy_free_identity_contract_ignores_seventh_group(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            lab = make_lab(root)
            definition = root / "legacy_identity.toml"
            write_campaign(definition, "legacy_identity", temps=[1000.0, 1100.0])
            case = expand(definition, lab)[0]
            groups = case["groups"]
            self.assertEqual(
                set(canonical_scientific_payload(groups)),
                set(NAMELIST_GROUPS) - {"case_config"},
            )
            expected = hashlib.blake2s(
                json.dumps(
                    canonical_scientific_payload(groups),
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=True,
                ).encode("utf-8"),
                digest_size=16,
            ).hexdigest()
            self.assertEqual(compute_case_fingerprint(groups), expected)

            populated = json.loads(json.dumps(groups))
            populated[CHEMISTRY_GROUP] = {
                "chemistry_backend": "cvode",
                "chemistry_cvode_relative_tolerance": 1.0e-10,
            }
            # Legacy no-policy hashing must not retroactively include the group.
            self.assertEqual(
                compute_case_fingerprint(groups), compute_case_fingerprint(populated)
            )

    def test_keromnes_effective_provenance_records_forced_files(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            lab = make_lab(root)
            definition = root / "common_supplied.toml"
            write_campaign(
                definition,
                "common_supplied",
                temps=[1000.0],
                mechanism="keromnes",
                supplied_thermo="COMMON_THERMO.txt",
                supplied_transport="COMMON_TRANSDATA.txt",
            )
            cases = expand(definition, lab)
            effective = effective_thermo_transport(
                "keromnes", cases[0]["groups"]["physics_config"]
            )
            self.assertEqual(
                effective,
                ("KEROMNES_THERMO.txt", "KEROMNES_TRANSDATA.txt", True),
            )
            resources = task_setup_resource_provenance(cases, lab)
            self.assertIn("KEROMNES_THERMO.txt", resources["thermophysical_data"])
            self.assertIn("KEROMNES_TRANSDATA.txt", resources["thermophysical_data"])
            self.assertNotIn("COMMON_THERMO.txt", resources["thermophysical_data"])
            per_case = resources["effective_case_resources"][cases[0]["case_id"]]
            self.assertEqual(per_case["thermo_data_file"], "KEROMNES_THERMO.txt")
            self.assertEqual(per_case["transport_data_file"], "KEROMNES_TRANSDATA.txt")
            self.assertTrue(per_case["supplied_thermo_transport_overridden"])
            entry = resources["thermophysical_data"]["KEROMNES_THERMO.txt"]
            self.assertEqual(
                entry["sha256"],
                sha256_file(lab.task_setup_template / entry["relative_path"]),
            )

    def test_real_keromnes_resources_resolve_from_configured_laboratory(self):
        here = Path(__file__).resolve()
        candidates = [here.parents[1] / "config" / "laboratory.toml"]
        if len(here.parents) > 4:
            # <main>/.local/dev_worktrees/<transaction>/tests/<this file>
            candidates.append(here.parents[4] / "config" / "laboratory.toml")
        lab = None
        for candidate in candidates:
            if not candidate.is_file():
                continue
            try:
                trial = Laboratory.load(candidate, validate=False)
            except Exception:
                continue
            real_thermo = (
                trial.task_setup_template
                / "thermophysical_data"
                / "KEROMNES_THERMO.txt"
            )
            if real_thermo.is_file():
                lab = trial
                break
        if lab is None:
            self.skipTest("real KEROMNES resources are not discoverable")

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            definition = root / "real_keromnes.toml"
            write_campaign(definition, "real_keromnes", temps=[1000.0], mechanism="keromnes")
            cases = expand(definition, lab)
            resources = task_setup_resource_provenance(cases, lab)
            kinetic = resources["kinetic_mechanisms"]["keromnes"]
            self.assertTrue(kinetic["relative_path"].endswith("hydrogen/KEROMNES.txt"))
            per_case = resources["effective_case_resources"][cases[0]["case_id"]]
            self.assertEqual(per_case["thermo_data_file"], "KEROMNES_THERMO.txt")
            self.assertEqual(per_case["transport_data_file"], "KEROMNES_TRANSDATA.txt")
            self.assertTrue(per_case["kinetic_file"].endswith("hydrogen/KEROMNES.txt"))
            self.assertFalse(any("COMMON_" in name for name in resources["thermophysical_data"]))
            for name in ("KEROMNES_THERMO.txt", "KEROMNES_TRANSDATA.txt"):
                entry = resources["thermophysical_data"][name]
                actual = lab.task_setup_template / entry["relative_path"]
                self.assertTrue(actual.is_file())
                self.assertEqual(entry["sha256"], sha256_file(actual))


if __name__ == "__main__":
    unittest.main()
