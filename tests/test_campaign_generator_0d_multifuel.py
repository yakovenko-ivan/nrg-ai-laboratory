"""D3b multi-fuel generator tests (unapplied proposal).

These tests exercise the Python generator/identity layer only. They never
prepare NRG cases, invoke the package interface, or start CFD.
"""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from campaign_tools.campaign_generator_0d import (
    MECHANISM_FILES,
    MECHANISM_FUEL,
    STRICT_THERMO_TRANSPORT_MECHANISMS,
    effective_thermo_transport,
    expand_campaign,
    read_campaign,
    render_namelist,
    task_setup_resource_provenance,
)
from nrg_analysis.laboratory import Laboratory


LEGACY_MIXTURE_KEYS = {
    "hydrogen_mole_percent",
    "n2_o2_molar_ratio",
    "initial_temperature",
    "initial_pressure",
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

    mechanisms = root / "resources" / "task_setup" / "chemical_mechanisms"
    thermo = root / "resources" / "task_setup" / "thermophysical_data"
    for subdir in ("hydrogen", "ammonia", "acetylene"):
        (mechanisms / subdir).mkdir(parents=True, exist_ok=True)
    thermo.mkdir(parents=True, exist_ok=True)

    resources = {
        "hydrogen/KEROMNES.txt": ("KEROMNES", "KEROMNES"),
        "ammonia/OKAFOR.txt": ("OKAFOR", "OKAFOR"),
        "acetylene/VARATHARAJAN.txt": ("VARATHARAJAN", "VARATHARAJAN"),
        "acetylene/TEREZA_acetylene.txt": ("TEREZA_ACETYLENE", "TEREZA"),
    }
    for relative, (kinetic, thermo_name) in resources.items():
        (mechanisms / relative).write_text(
            f"synthetic {kinetic} kinetic mechanism\n", encoding="utf-8"
        )
        for suffix in ("THERMO", "TRANSDATA"):
            (thermo / f"{thermo_name}_{suffix}.txt").write_text(
                f"synthetic {thermo_name} {suffix}\n", encoding="utf-8"
            )

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
    mechanism: str = "keromnes",
    legacy_percent: float | None = 20.0,
    fuel_percent: float | None = None,
    fuel_species: str | None = None,
    stoich_o2: float | None = None,
    temps: list[float] | None = None,
    max_cases: int = 100,
    supplied_thermo: str | None = None,
    supplied_transport: str | None = None,
    identity_fields: list[str] | None = None,
) -> None:
    lines = [
        "[campaign]",
        f'name = "{name}"',
        f'generator_output_directory = "{name}_generated"',
        f"max_cases = {max_cases}",
        "",
    ]
    if identity_fields:
        lines += [
            "[case_identity]",
            "fields = [" + ", ".join(json.dumps(f) for f in identity_fields) + "]",
            "",
        ]
    lines += [
        "[defaults.reactor_config]",
        'reactor_type = "constant_volume"',
        "cells_number_x = 2",
        "cell_length_x = 1.0e-4",
        "",
        "[defaults.mixture_config]",
    ]
    if legacy_percent is not None:
        lines.append(f"hydrogen_mole_percent = {json.dumps(legacy_percent)}")
    if fuel_percent is not None:
        lines.append(f"fuel_mole_percent = {json.dumps(fuel_percent)}")
    if fuel_species is not None:
        lines.append(f'fuel_specie_name = "{fuel_species}"')
    if stoich_o2 is not None:
        lines.append(f"stoich_o2 = {json.dumps(stoich_o2)}")
    lines += [
        "n2_o2_molar_ratio = 3.762",
        "initial_temperature = 1200.0",
        "initial_pressure = 101325.0",
        "",
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
    lines += [
        "",
        "[defaults.run_control_config]",
        'termination_mode = "wall_time"',
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
    ]
    if temps is not None:
        lines += [
            "[sweep.mixture_config]",
            "initial_temperature = " + json.dumps(temps),
            "",
        ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def expand(path: Path, lab: Laboratory):
    return expand_campaign(read_campaign(path), lab)


class MultiFuelGeneratorTests(unittest.TestCase):
    def test_mechanism_fuel_map_is_complete_and_explicit(self):
        self.assertEqual(MECHANISM_FUEL["keromnes"], ("H2", 0.5))
        self.assertEqual(MECHANISM_FUEL["okafor"], ("NH3", 0.75))
        self.assertEqual(MECHANISM_FUEL["varatharajan"], ("C2H2", 2.5))
        self.assertEqual(MECHANISM_FUEL["tereza_acetylene"], ("C2H2", 2.5))
        self.assertEqual(
            set(MECHANISM_FUEL),
            {
                "keromnes", "konnov", "zhang", "gerasimov_shatalov2013",
                "popov2007", "maas_pope1992", "li2004", "williams2004",
                "gri_mech3", "tereza", "hong2010", "okafor", "varatharajan",
                "tereza_acetylene",
            },
        )
        self.assertEqual(
            STRICT_THERMO_TRANSPORT_MECHANISMS,
            {"okafor", "varatharajan", "tereza_acetylene"},
        )
        self.assertEqual(
            MECHANISM_FILES["tereza_acetylene"],
            "acetylene/TEREZA_acetylene.txt",
        )

    def test_legacy_definition_is_byte_compatible(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            lab = make_lab(root)
            definition = root / "legacy.toml"
            write_campaign(definition, "legacy", temps=[1200.0, 1400.0])
            cases = expand(definition, lab)
            self.assertEqual(len(cases), 2)
            for case in cases:
                self.assertEqual(
                    set(case["groups"]["mixture_config"]), LEGACY_MIXTURE_KEYS
                )
                self.assertNotIn("fuel_mole_percent", case["groups"]["mixture_config"])
                self.assertTrue(case["label"].startswith("0D CV H2-air | "))
                self.assertIn(f"H2={20:g} mol.%", case["label"])
                text = render_namelist(case["groups"])
                self.assertIn("hydrogen_mole_percent = 20", text)
                self.assertNotIn("fuel_mole_percent", text)
                self.assertNotIn("fuel_specie_name", text)
                self.assertNotIn("stoich_o2", text)
            self.assertEqual(
                len({case["case_fingerprint"] for case in cases}), 2
            )

    def test_nh3_preview_resolves_canonical_fuel_and_resources(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            lab = make_lab(root)
            definition = root / "nh3.toml"
            write_campaign(
                definition,
                "nh3",
                mechanism="okafor",
                legacy_percent=None,
                fuel_percent=21.874658,
            )
            case = expand(definition, lab)[0]
            mix = case["groups"]["mixture_config"]
            self.assertEqual(mix["fuel_specie_name"], "NH3")
            self.assertEqual(mix["stoich_o2"], 0.75)
            self.assertIn("NH3-air", case["label"])
            self.assertIn("NH3=21.8747 mol.%", case["label"])
            self.assertIn("fuel_mole_percent = 21.874658", render_namelist(case["groups"]))
            effective = effective_thermo_transport("okafor", case["groups"]["physics_config"])
            self.assertEqual(effective, ("OKAFOR_THERMO.txt", "OKAFOR_TRANSDATA.txt", False))
            resources = task_setup_resource_provenance([case], lab)
            per_case = resources["effective_case_resources"][case["case_id"]]
            self.assertEqual(per_case["thermo_data_file"], "OKAFOR_THERMO.txt")
            self.assertEqual(per_case["transport_data_file"], "OKAFOR_TRANSDATA.txt")
            self.assertNotIn("KEROMNES_THERMO.txt", resources["thermophysical_data"])
            self.assertTrue(
                resources["kinetic_mechanisms"]["okafor"]["relative_path"].endswith(
                    "ammonia/OKAFOR.txt"
                )
            )

    def test_c2h2_preview_resolves_both_mechanisms(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            lab = make_lab(root)
            for mechanism in ("varatharajan", "tereza_acetylene"):
                definition = root / f"{mechanism}.toml"
                write_campaign(
                    definition,
                    mechanism,
                    mechanism=mechanism,
                    legacy_percent=None,
                    fuel_percent=7.748934,
                )
                case = expand(definition, lab)[0]
                mix = case["groups"]["mixture_config"]
                self.assertEqual(mix["fuel_specie_name"], "C2H2")
                self.assertEqual(mix["stoich_o2"], 2.5)
                self.assertIn("C2H2-air", case["label"])
                resources = task_setup_resource_provenance([case], lab)
                per_case = resources["effective_case_resources"][case["case_id"]]
                self.assertEqual(per_case["transport_data_file"], "TEREZA_TRANSDATA.txt"
                                 if mechanism == "tereza_acetylene"
                                 else "VARATHARAJAN_TRANSDATA.txt")
                if mechanism == "tereza_acetylene":
                    self.assertTrue(
                        per_case["kinetic_file"].endswith("acetylene/TEREZA_acetylene.txt")
                    )
                    self.assertFalse(
                        per_case["kinetic_file"].endswith("hydrogen/TEREZA.txt")
                    )

    def test_fuel_alias_and_consistency_failures_are_closed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            lab = make_lab(root)

            both = root / "both.toml"
            write_campaign(both, "both", fuel_percent=21.0)
            with self.assertRaisesRegex(ValueError, "exactly one"):
                expand(both, lab)

            neither = root / "neither.toml"
            write_campaign(neither, "neither", legacy_percent=None)
            with self.assertRaisesRegex(ValueError, "neither"):
                expand(neither, lab)

            wrong_fuel = root / "wrong_fuel.toml"
            write_campaign(
                wrong_fuel,
                "wrong_fuel",
                mechanism="okafor",
                legacy_percent=None,
                fuel_percent=21.0,
                fuel_species="H2",
            )
            with self.assertRaisesRegex(ValueError, "inconsistent"):
                expand(wrong_fuel, lab)

            wrong_stoich = root / "wrong_stoich.toml"
            write_campaign(
                wrong_stoich,
                "wrong_stoich",
                mechanism="okafor",
                legacy_percent=None,
                fuel_percent=21.0,
                stoich_o2=0.5,
            )
            with self.assertRaisesRegex(ValueError, "inconsistent"):
                expand(wrong_stoich, lab)

            alias_without_percent = root / "partial.toml"
            write_campaign(
                alias_without_percent,
                "partial",
                legacy_percent=20.0,
                fuel_species="H2",
            )
            with self.assertRaisesRegex(ValueError, "legacy alias"):
                expand(alias_without_percent, lab)

            legacy_with_nh3 = root / "legacy_nh3.toml"
            write_campaign(
                legacy_with_nh3,
                "legacy_nh3",
                mechanism="okafor",
                legacy_percent=20.0,
            )
            with self.assertRaisesRegex(ValueError, "legacy H2 alias"):
                expand(legacy_with_nh3, lab)

            unknown = root / "unknown.toml"
            write_campaign(unknown, "unknown", mechanism="not_a_mechanism")
            with self.assertRaisesRegex(ValueError, "unsupported mechanism_id"):
                expand(unknown, lab)

    def test_strict_thermo_transport_rejects_keromnes_pairing(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            lab = make_lab(root)
            definition = root / "wrong_thermo.toml"
            write_campaign(
                definition,
                "wrong_thermo",
                mechanism="okafor",
                legacy_percent=None,
                fuel_percent=21.0,
                supplied_thermo="KEROMNES_THERMO.txt",
                supplied_transport="KEROMNES_TRANSDATA.txt",
            )
            with self.assertRaisesRegex(ValueError, "inconsistent"):
                expand(definition, lab)

    def test_max_cases_and_duplicate_identity_guards(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            lab = make_lab(root)
            too_many = root / "too_many.toml"
            write_campaign(too_many, "too_many", temps=[1000.0 + i for i in range(101)])
            with self.assertRaisesRegex(ValueError, "max_cases"):
                expand(too_many, lab)

            raised = root / "raised.toml"
            write_campaign(
                raised,
                "raised",
                temps=[1000.0 + i for i in range(101)],
                max_cases=101,
            )
            self.assertEqual(len(expand(raised, lab)), 101)

            duplicate = root / "duplicate.toml"
            write_campaign(
                duplicate,
                "duplicate",
                temps=[1200.0, 1200.0],
                identity_fields=["mixture_config.initial_temperature"],
            )
            with self.assertRaisesRegex(ValueError, "duplicate logical case identity"):
                expand(duplicate, lab)

    def test_archived_m1_h2_fingerprints_are_unchanged(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            lab = make_lab(root)
            definition = root / "m1.toml"
            definition.write_text(
                """
[campaign]
name = "0D_CV_H2_KEROMNES_T1500_P1atm_m1_harmonized"
generator_output_directory = "m1_generated"
max_cases = 1

[case_identity]
fields = [
  "mixture_config.initial_temperature",
  "mixture_config.initial_pressure",
  "physics_config.mechanism_id",
]

[defaults.reactor_config]
reactor_type = "constant_volume"
cells_number_x = 2
cell_length_x = 1.0e-4

[defaults.mixture_config]
hydrogen_mole_percent = 20.0
n2_o2_molar_ratio = 3.762
initial_temperature = 1500.0
initial_pressure = 101325.0

[defaults.physics_config]
mechanism_id = "keromnes"
thermo_data_file = "KEROMNES_THERMO.txt"
transport_data_file = "KEROMNES_TRANSDATA.txt"
solver_id = "cpm"
initial_time_step = 1.0e-8
cfl_enabled = false
cfl_coefficient = 0.25

[defaults.chemistry_backend_config]
chemistry_backend = "slatec"

[defaults.run_control_config]
termination_mode = "wall_time"
final_time_ms = 10.0
wall_time_limit_s = 3600.0
wall_time_reserve_s = 60.0

[defaults.output_config]
postprocess_interval_us = 0.5
field_save_interval_us = 100.0
checkpoint_interval_us = 250.0
save_spatial_fields = true
""",
                encoding="utf-8",
            )
            case = expand(definition, lab)[0]
            self.assertEqual(
                case["case_fingerprint"], "ec7351a313274fccac9c7f7ff9928307"
            )
            self.assertEqual(
                case["generated_attempt_fingerprint"],
                "e2946a16c9d0ff5b784d4f1d396920e0",
            )
            self.assertEqual(
                set(case["groups"]["mixture_config"]), LEGACY_MIXTURE_KEYS
            )
            text = render_namelist(case["groups"])
            self.assertIn("hydrogen_mole_percent = 20", text)
            self.assertNotIn("fuel_mole_percent", text)


if __name__ == "__main__":
    unittest.main()
