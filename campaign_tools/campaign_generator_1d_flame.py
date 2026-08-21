#!/usr/bin/env python3
"""Generate deterministic 1D H2-air wall/radial flame campaigns for NRG.

Execution contract:

    campaign TOML -> cases.csv + _setups/*.nml -> package interface -> case dirs

The generator is intentionally specific to the 1D wall/radial flame problem.
Logical case identity is controlled by the shared campaign_identity policy;
run-control and output cadence may be changed as reviewed attempt overrides.

Python 3.11+.
"""

from __future__ import annotations

import argparse
import copy
import csv
from datetime import datetime, timezone
import hashlib
import itertools
import json
import math
from pathlib import Path
import re
import shutil
import subprocess
from typing import Any

from nrg_analysis.laboratory import Laboratory
from nrg_analysis.provenance import sha256_file
from campaign_tools.campaign_identity import (
    attempt_fingerprint,
    build_policy as build_identity_policy,
    identity_fingerprint,
)

try:
    import tomllib
except ModuleNotFoundError as exc:  # pragma: no cover
    raise SystemExit("Python 3.11+ is required (tomllib unavailable).") from exc


NAMELIST_GROUPS = (
    "case_config",
    "geometry_config",
    "mixture_config",
    "ignition_config",
    "physics_config",
    "run_control_config",
    "output_config",
)

GROUP_KEYS: dict[str, set[str]] = {
    "case_config": {
        "case_id",
        "case_fingerprint",
        "case_directory",
        "case_label",
        "campaign_id",
        "results_root",
        "numerical_variant",
    },
    "geometry_config": {
        "coordinate_system",
        "domain_length_m",
        "cell_size_m",
    },
    "mixture_config": {
        "hydrogen_mole_percent",
        "n2_o2_molar_ratio",
        "initial_temperature",
        "initial_pressure",
    },
    "ignition_config": {
        "ignition_temperature",
        "ignition_width_m",
        "ignition_h2o_moles",
        "ignition_n2_moles",
    },
    "physics_config": {
        "mechanism_id",
        "solver_id",
        "thermal_radiation_enabled",
        "soret_diffusion_enabled",
        "initial_time_step",
        "cfl_enabled",
        "cfl_coefficient",
    },
    "run_control_config": {
        "termination_mode",
        "final_time_s",
        "wall_time_limit_s",
        "wall_time_reserve_s",
    },
    "output_config": {
        "postprocess_interval_ms",
        "field_save_interval_ms",
        "checkpoint_interval_ms",
        "save_spatial_fields",
    },
}

# Numerical variants are deliberately limited to attempt-level knobs.  Mesh
# spacing, geometry, chemistry, radiation and Soret are logical case axes and
# should be expressed as campaign sweeps rather than recalculation variants.
NUMERICAL_VARIANT_KEYS = {
    "initial_time_step": ("physics_config", "initial_time_step"),
    "cfl_enabled": ("physics_config", "cfl_enabled"),
    "cfl_coefficient": ("physics_config", "cfl_coefficient"),
    "final_time_s": ("run_control_config", "final_time_s"),
    "wall_time_limit_s": ("run_control_config", "wall_time_limit_s"),
    "wall_time_reserve_s": ("run_control_config", "wall_time_reserve_s"),
    "postprocess_interval_ms": ("output_config", "postprocess_interval_ms"),
    "field_save_interval_ms": ("output_config", "field_save_interval_ms"),
    "checkpoint_interval_ms": ("output_config", "checkpoint_interval_ms"),
}

ALLOWED_COORDINATE_SYSTEMS = {"cartesian", "cylindrical", "spherical"}
ALLOWED_SOLVERS = {"fds", "fds_low_mach"}
ALLOWED_MECHANISMS = {"keromnes", "konnov", "zhang", "agafonov", "tereza"}

GENERATOR_SCHEMA_VERSION = 1
FINGERPRINT_HEX_LENGTH = 32

TOKEN_ORDER = [
    ("geometry_config", "coordinate_system"),
    ("mixture_config", "hydrogen_mole_percent"),
    ("physics_config", "mechanism_id"),
    ("geometry_config", "cell_size_m"),
    ("physics_config", "thermal_radiation_enabled"),
    ("physics_config", "soret_diffusion_enabled"),
    ("physics_config", "solver_id"),
]


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def validate_group_keys(groups: dict[str, Any], where: str) -> None:
    for group, values in groups.items():
        if group not in NAMELIST_GROUPS:
            raise ValueError(f"{where}: unsupported namelist group {group!r}")
        if not isinstance(values, dict):
            raise ValueError(f"{where}.{group} must be a TOML table")
        unknown = set(values) - GROUP_KEYS[group]
        if unknown:
            raise ValueError(
                f"{where}.{group}: unsupported key(s): {', '.join(sorted(unknown))}"
            )


def read_campaign(path: Path) -> dict[str, Any]:
    with path.open("rb") as stream:
        data = tomllib.load(stream)
    if "campaign" not in data:
        raise ValueError("campaign TOML must contain [campaign]")
    if "defaults" not in data:
        raise ValueError("campaign TOML must contain [defaults.*] tables")

    validate_group_keys(data.get("defaults", {}), "defaults")
    validate_group_keys(data.get("sweep", {}), "sweep")

    case_identity = data.get("case_identity", {})
    if case_identity:
        if not isinstance(case_identity, dict):
            raise ValueError("[case_identity] must be a TOML table")
        unknown = set(case_identity) - {"fields"}
        if unknown:
            raise ValueError(
                "[case_identity] unsupported key(s): " + ", ".join(sorted(unknown))
            )
        if not isinstance(case_identity.get("fields"), list) or not case_identity["fields"]:
            raise ValueError("[case_identity].fields must be a non-empty array")

    attempt_overrides = data.get("attempt_overrides", {})
    if attempt_overrides:
        if not isinstance(attempt_overrides, dict):
            raise ValueError("[attempt_overrides] must be a TOML table")
        unknown = set(attempt_overrides) - {"allowed"}
        if unknown:
            raise ValueError(
                "[attempt_overrides] unsupported key(s): " + ", ".join(sorted(unknown))
            )
        if not isinstance(attempt_overrides.get("allowed"), list) or not attempt_overrides["allowed"]:
            raise ValueError("[attempt_overrides].allowed must be a non-empty array")

    # Shared policy parsing validates field-path syntax and overlap rules.
    build_identity_policy(data)

    for idx, variant in enumerate(data.get("numerical_variants", []), start=1):
        if not isinstance(variant, dict):
            raise ValueError(f"numerical_variants[{idx}] must be a TOML table")
        unknown = set(variant) - set(NUMERICAL_VARIANT_KEYS) - {"name", "description"}
        if unknown:
            raise ValueError(
                f"numerical_variants[{idx}]: unsupported key(s): "
                + ", ".join(sorted(unknown))
            )
    return data


def expand_sweeps(sweep: dict[str, dict[str, Any]]) -> list[dict[str, dict[str, Any]]]:
    axes: list[tuple[str, str, list[Any]]] = []
    for group in NAMELIST_GROUPS:
        for key, values in sweep.get(group, {}).items():
            if not isinstance(values, list) or not values:
                raise ValueError(f"sweep.{group}.{key} must be a non-empty TOML array")
            axes.append((group, key, values))
    if not axes:
        return [{}]

    result: list[dict[str, dict[str, Any]]] = []
    for product_values in itertools.product(*(axis[2] for axis in axes)):
        override: dict[str, dict[str, Any]] = {}
        for (group, key, _), value in zip(axes, product_values):
            override.setdefault(group, {})[key] = value
        result.append(override)
    return result


def numerical_variants(
    data: dict[str, Any],
) -> list[tuple[str, str, dict[str, dict[str, Any]]]]:
    variants = data.get("numerical_variants", [])
    if not variants:
        return [("default", "", {})]

    result: list[tuple[str, str, dict[str, dict[str, Any]]]] = []
    names: set[str] = set()
    for idx, raw in enumerate(variants, start=1):
        name = str(raw.get("name", f"variant_{idx}")).strip()
        if not name:
            raise ValueError("numerical variant name may not be empty")
        if name in names:
            raise ValueError(f"duplicate numerical variant name {name!r}")
        names.add(name)
        description = str(raw.get("description", "")).strip()
        override: dict[str, dict[str, Any]] = {}
        for key, value in raw.items():
            if key in {"name", "description"}:
                continue
            group, target = NUMERICAL_VARIANT_KEYS[key]
            override.setdefault(group, {})[target] = value
        result.append((name, description, override))
    return result


def _require_grid_aligned(length: float, dx: float, label: str) -> int:
    ratio = length / dx
    nearest = round(ratio)
    if not math.isclose(ratio, nearest, rel_tol=0.0, abs_tol=1.0e-10):
        raise ValueError(f"{label} must be exactly cell-aligned; got ratio={ratio:.16g}")
    return int(nearest)


def validate_case(groups: dict[str, dict[str, Any]]) -> None:
    for group in NAMELIST_GROUPS:
        if group not in groups:
            raise ValueError(f"missing namelist group {group}")
        unknown = set(groups[group]) - GROUP_KEYS[group]
        if unknown:
            raise ValueError(f"{group}: unsupported key(s): {', '.join(sorted(unknown))}")

    geo = groups["geometry_config"]
    mix = groups["mixture_config"]
    ign = groups["ignition_config"]
    phy = groups["physics_config"]
    run = groups["run_control_config"]
    out = groups["output_config"]

    coordinate = str(geo["coordinate_system"]).lower()
    if coordinate not in ALLOWED_COORDINATE_SYSTEMS:
        raise ValueError(f"unsupported coordinate_system {coordinate!r}")

    domain_length = float(geo["domain_length_m"])
    dx = float(geo["cell_size_m"])
    if domain_length <= 0.0 or dx <= 0.0:
        raise ValueError("domain_length_m and cell_size_m must be positive")
    cells = _require_grid_aligned(domain_length, dx, "domain_length_m")
    if cells < 8:
        raise ValueError("1D domain must contain at least 8 physical cells")

    ignition_width = float(ign["ignition_width_m"])
    if not 0.0 < ignition_width < domain_length:
        raise ValueError("ignition_width_m must lie in (0, domain_length_m)")
    ignition_cells = _require_grid_aligned(ignition_width, dx, "ignition_width_m")
    if ignition_cells < 3:
        raise ValueError("ignition region must span at least three cells")
    if float(ign["ignition_temperature"]) <= 0.0:
        raise ValueError("ignition_temperature must be positive")
    if float(ign["ignition_h2o_moles"]) <= 0.0 or float(ign["ignition_n2_moles"]) <= 0.0:
        raise ValueError("ignition H2O and N2 mole amounts must be positive")

    xh2 = float(mix["hydrogen_mole_percent"])
    if not 0.0 < xh2 < 100.0:
        raise ValueError("hydrogen_mole_percent must lie in (0,100)")
    if float(mix["n2_o2_molar_ratio"]) <= 0.0:
        raise ValueError("n2_o2_molar_ratio must be positive")
    if float(mix["initial_temperature"]) <= 0.0 or float(mix["initial_pressure"]) <= 0.0:
        raise ValueError("initial temperature and pressure must be positive")

    mechanism = str(phy["mechanism_id"]).lower()
    solver = str(phy["solver_id"]).lower()
    if mechanism not in ALLOWED_MECHANISMS:
        raise ValueError(f"unsupported mechanism_id {mechanism!r}")
    if solver not in ALLOWED_SOLVERS:
        raise ValueError(f"unsupported solver_id {solver!r}; 1D campaign requires FDS")
    if float(phy["initial_time_step"]) <= 0.0:
        raise ValueError("initial_time_step must be positive")
    cfl = float(phy["cfl_coefficient"])
    if not 0.0 < cfl <= 1.0:
        raise ValueError("cfl_coefficient must lie in (0,1]")

    mode = str(run["termination_mode"]).lower()
    if mode not in {"simulation_time", "wall_time", "either"}:
        raise ValueError("unsupported termination_mode")
    if mode in {"simulation_time", "either"} and float(run["final_time_s"]) <= 0.0:
        raise ValueError("final_time_s must be positive")
    if mode in {"wall_time", "either"}:
        limit = float(run["wall_time_limit_s"])
        reserve = float(run["wall_time_reserve_s"])
        if limit <= 0.0 or reserve < 0.0 or reserve >= limit:
            raise ValueError("invalid wall-time limit/reserve")

    for key in (
        "postprocess_interval_ms",
        "field_save_interval_ms",
        "checkpoint_interval_ms",
    ):
        if float(out[key]) <= 0.0:
            raise ValueError(f"{key} must be positive")


def sanitize_token(value: Any) -> str:
    if isinstance(value, bool):
        return "ON" if value else "OFF"
    text = str(value).strip()
    text = text.replace("+", "p").replace("-", "m").replace(".", "p")
    text = re.sub(r"[^A-Za-z0-9_]+", "_", text)
    return text.strip("_") or "x"


def compact_number(value: float) -> str:
    return f"{value:g}".replace(".", "p")


def coordinate_token(value: str) -> str:
    return {
        "cartesian": "CART",
        "cylindrical": "CYL",
        "spherical": "SPH",
    }[value.lower()]


def directory_token(group: str, key: str, value: Any) -> str:
    if (group, key) == ("geometry_config", "coordinate_system"):
        return coordinate_token(str(value))
    if (group, key) == ("mixture_config", "hydrogen_mole_percent"):
        return f"H2_{float(value):g}pct".replace(".", "p")
    if (group, key) == ("physics_config", "mechanism_id"):
        return str(value).upper()
    if (group, key) == ("geometry_config", "cell_size_m"):
        return f"DX{compact_number(float(value)*1.0e6)}um"
    if (group, key) == ("physics_config", "thermal_radiation_enabled"):
        return "RAD_ON" if bool(value) else "RAD_OFF"
    if (group, key) == ("physics_config", "soret_diffusion_enabled"):
        return "SORET_ON" if bool(value) else "SORET_OFF"
    if (group, key) == ("physics_config", "solver_id"):
        return "FDS"
    return f"{key}_{sanitize_token(value)}"


def varying_fields(cases: list[dict[str, Any]]) -> set[tuple[str, str]]:
    values: dict[tuple[str, str], set[str]] = {}
    for item in cases:
        for group in NAMELIST_GROUPS:
            if group == "case_config":
                continue
            for key, value in item["groups"][group].items():
                values.setdefault((group, key), set()).add(json.dumps(value, sort_keys=True))
    return {key for key, vals in values.items() if len(vals) > 1}


def make_label(item: dict[str, Any]) -> str:
    g = item["groups"]
    geo = g["geometry_config"]
    mix = g["mixture_config"]
    phy = g["physics_config"]
    return (
        f"1D H2-air wall/radial flame | {str(geo['coordinate_system']).lower()} | "
        f"H2={float(mix['hydrogen_mole_percent']):g} mol.% | "
        f"{str(phy['mechanism_id']).upper()} | FDS | "
        f"dx={float(geo['cell_size_m']):.3e} m | "
        f"radiation={'ON' if bool(phy['thermal_radiation_enabled']) else 'OFF'}, "
        f"Soret={'ON' if bool(phy['soret_diffusion_enabled']) else 'OFF'} | "
        f"{item['numerical_variant']}"
    )


def build_case_directory(
    item: dict[str, Any], data: dict[str, Any], cases: list[dict[str, Any]]
) -> str:
    campaign = data["campaign"]
    case_id = item["case_id"]
    if not bool(campaign.get("human_readable_directories", True)):
        return case_id

    max_len = int(campaign.get("max_case_directory_length", 120))
    varying = varying_fields(cases)
    tokens: list[str] = []
    for group, field in TOKEN_ORDER:
        if (group, field) in varying:
            tokens.append(directory_token(group, field, item["groups"][group][field]))

    variants = {x["numerical_variant"] for x in cases}
    if len(variants) > 1:
        tokens.append("V_" + sanitize_token(item["numerical_variant"]))

    if not tokens:
        return case_id
    full = case_id + "__" + "__".join(tokens)
    if len(full) <= max_len:
        return full

    digest = hashlib.blake2s(full.encode("utf-8"), digest_size=4).hexdigest()
    suffix = f"__X{digest}"
    keep = max(max_len - len(case_id) - len(suffix) - 2, 8)
    descriptor = "__".join(tokens)[:keep].rstrip("_.-")
    return f"{case_id}__{descriptor}{suffix}"


def expand_campaign(data: dict[str, Any], laboratory: Laboratory) -> list[dict[str, Any]]:
    defaults = copy.deepcopy(data["defaults"])
    defaults.setdefault("case_config", {})
    sweeps = expand_sweeps(data.get("sweep", {}))
    variants = numerical_variants(data)

    raw: list[dict[str, Any]] = []
    for sweep_override, (variant_name, variant_desc, variant_override) in itertools.product(
        sweeps, variants
    ):
        groups = deep_merge(defaults, sweep_override)
        groups = deep_merge(groups, variant_override)
        groups.setdefault("case_config", {})["numerical_variant"] = variant_name
        validate_case({**groups, "case_config": groups["case_config"]})
        raw.append(
            {
                "groups": groups,
                "numerical_variant": variant_name,
                "numerical_variant_description": variant_desc,
            }
        )

    campaign = data["campaign"]
    max_cases = int(campaign.get("max_cases", 400))
    if len(raw) > max_cases:
        raise ValueError(
            f"campaign expands to {len(raw)} cases, exceeding max_cases={max_cases}; "
            "raise the limit explicitly after reviewing --preview"
        )

    prefix = str(campaign.get("case_id_prefix", "R"))
    width = int(campaign.get("case_id_width", 6))
    campaign_id = str(campaign.get("name", "1D_wall_flame"))
    results_root = str(laboratory.runs_root)

    cases: list[dict[str, Any]] = []
    for number, raw_item in enumerate(raw, start=1):
        item = copy.deepcopy(raw_item)
        item["case_id"] = f"{prefix}{number:0{width}d}"
        c = item["groups"].setdefault("case_config", {})
        c.update(
            {
                "case_id": item["case_id"],
                "case_fingerprint": "",
                "case_directory": item["case_id"],
                "case_label": "",
                "campaign_id": campaign_id,
                "results_root": results_root,
                "numerical_variant": item["numerical_variant"],
            }
        )
        cases.append(item)

    identity_policy = build_identity_policy(data)
    seen_identities: dict[str, str] = {}
    for item in cases:
        item["label"] = make_label(item)
        item["case_directory"] = build_case_directory(item, data, cases)
        c = item["groups"]["case_config"]
        c["case_directory"] = item["case_directory"]
        c["case_label"] = item["label"]
        item["case_fingerprint"] = identity_fingerprint(item["groups"], identity_policy)
        item["case_identity_fingerprint"] = item["case_fingerprint"]
        item["generated_attempt_fingerprint"] = attempt_fingerprint(item["groups"])
        item["fingerprint_scheme"] = identity_policy["fingerprint_scheme"]

        duplicate = seen_identities.get(item["case_fingerprint"])
        if duplicate is not None:
            raise ValueError(
                "campaign contains duplicate logical case identity: "
                f"{duplicate} and {item['case_id']} share {item['case_fingerprint']}"
            )
        seen_identities[item["case_fingerprint"]] = item["case_id"]
        c["case_fingerprint"] = item["case_fingerprint"]
        validate_case(item["groups"])

    return cases


def fortran_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return ".true." if value else ".false."
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return f"{value:.16g}"
    raise TypeError(f"unsupported namelist value type: {type(value).__name__}")


def fortran_value(value: Any) -> str:
    if isinstance(value, list):
        return ", ".join(fortran_scalar(v) for v in value)
    return fortran_scalar(value)


def render_namelist(groups: dict[str, dict[str, Any]]) -> str:
    lines = [
        "! Generated by campaign_generator_1d_flame.py",
        "! Agentic 1D H2-air wall/radial flame campaign.",
        "",
    ]
    for group in NAMELIST_GROUPS:
        lines.append(f"&{group}")
        for key, value in groups[group].items():
            lines.append(f"    {key} = {fortran_value(value)}")
        lines.append("/")
        lines.append("")
    return "\n".join(lines)


def expected_paths(item: dict[str, Any]) -> tuple[Path, Path, Path]:
    cfg = item["groups"]["case_config"]
    case_path = (
        Path(str(cfg["results_root"]))
        / str(cfg["campaign_id"])
        / item["case_directory"]
    )
    return case_path, case_path / "data_save", case_path / "data_output"


def flatten_case(item: dict[str, Any]) -> dict[str, Any]:
    case_path, data_save, data_output = expected_paths(item)
    row: dict[str, Any] = {
        "case_id": item["case_id"],
        "case_fingerprint": item["case_fingerprint"],
        "case_identity_fingerprint": item.get(
            "case_identity_fingerprint", item["case_fingerprint"]
        ),
        "generated_attempt_fingerprint": item.get("generated_attempt_fingerprint", ""),
        "fingerprint_scheme": item.get("fingerprint_scheme", "campaign_identity_v1"),
        "case_directory": item["case_directory"],
        "label": item["label"],
        "case_path": case_path.as_posix(),
        "data_save_path": data_save.as_posix(),
        "data_output_path": data_output.as_posix(),
        "numerical_variant": item["numerical_variant"],
    }
    for group in NAMELIST_GROUPS:
        for key, value in item["groups"][group].items():
            row[f"{group}.{key}"] = value
    return row


def campaign_directory(data: dict[str, Any], laboratory: Laboratory) -> Path:
    setting = data["campaign"].get("generator_output_directory")
    if setting:
        path = Path(str(setting)).expanduser()
        if not path.is_absolute():
            path = laboratory.campaign_root / path
        return path.resolve()
    name = str(data["campaign"].get("name", "1D_wall_flame"))
    return (laboratory.campaign_root / name).resolve()


def package_interface_1d(laboratory: Laboratory) -> Path:
    try:
        return laboratory.package_interfaces["1d"]
    except KeyError as exc:
        raise ValueError(
            "laboratory runtime has no package_interface_1d; add "
            "runtime.package_interface_1d to laboratory.toml/local override"
        ) from exc


def create_files(
    data: dict[str, Any],
    toml_path: Path,
    cases: list[dict[str, Any]],
    overwrite: bool,
    laboratory: Laboratory,
) -> Path:
    out_dir = campaign_directory(data, laboratory)
    setup_dir = out_dir / "_setups"
    if out_dir.exists() and not overwrite and any(out_dir.iterdir()):
        raise FileExistsError(f"{out_dir} already exists and is not empty; use --overwrite")
    if out_dir.exists() and overwrite:
        shutil.rmtree(out_dir)
    setup_dir.mkdir(parents=True, exist_ok=True)

    (out_dir / "campaign.toml").write_text(
        toml_path.read_text(encoding="utf-8"), encoding="utf-8"
    )

    interface = package_interface_1d(laboratory)
    runtime_manifest = laboratory.runtime_manifest
    manifest = {
        "generator_schema_version": GENERATOR_SCHEMA_VERSION,
        "campaign_type": "1d_wall_radial_h2_flame",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_campaign_sha256": hashlib.sha256(toml_path.read_bytes()).hexdigest(),
        "laboratory_config": str(laboratory.config_path),
        "laboratory_config_sha256": sha256_file(laboratory.config_path),
        "laboratory_local_config": (
            str(laboratory.local_config_path) if laboratory.local_config_path else None
        ),
        "laboratory_local_config_sha256": (
            sha256_file(laboratory.local_config_path)
            if laboratory.local_config_path
            else None
        ),
        "runs_root": str(laboratory.runs_root),
        "package_interface_1d": str(interface),
        "package_interface_1d_sha256": sha256_file(interface),
        "computing_module": str(laboratory.computing_module),
        "computing_module_sha256": sha256_file(laboratory.computing_module),
        "runtime_manifest": str(runtime_manifest) if runtime_manifest.is_file() else None,
        "runtime_manifest_sha256": (
            sha256_file(runtime_manifest) if runtime_manifest.is_file() else None
        ),
        "campaign_name": str(data["campaign"].get("name", "1D_wall_flame")),
        "case_count": len(cases),
        "identity_policy": build_identity_policy(data),
        "case_fingerprint_scheme": "campaign_identity_v1",
        "case_fingerprints": {x["case_id"]: x["case_fingerprint"] for x in cases},
        "generated_attempt_fingerprints": {
            x["case_id"]: x.get("generated_attempt_fingerprint") for x in cases
        },
        "front_history_schema": {
            "time_units": "milliseconds",
            "columns": [
                "time_actual",
                "temperature_min_grad_metric",
                "front_index_subcell",
                "temperature_at_integer_front_anchor",
                "Y_H2_at_integer_front_anchor",
                "max_energy_production_chemistry",
                "max_temperature",
            ],
        },
    }
    (out_dir / "campaign_manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )

    for item in cases:
        case_id = item["case_id"]
        (setup_dir / f"{case_id}.nml").write_text(
            render_namelist(item["groups"]), encoding="utf-8"
        )
        case_path, data_save, data_output = expected_paths(item)
        metadata = {
            "case_id": case_id,
            "case_fingerprint": item["case_fingerprint"],
            "case_identity_fingerprint": item.get(
                "case_identity_fingerprint", item["case_fingerprint"]
            ),
            "generated_attempt_fingerprint": item.get("generated_attempt_fingerprint"),
            "fingerprint_scheme": item.get("fingerprint_scheme", "campaign_identity_v1"),
            "case_directory": item["case_directory"],
            "label": item["label"],
            "numerical_variant": item["numerical_variant"],
            "numerical_variant_description": item["numerical_variant_description"],
            "case_path": case_path.as_posix(),
            "data_save_path": data_save.as_posix(),
            "data_output_path": data_output.as_posix(),
            "namelists": item["groups"],
        }
        (setup_dir / f"{case_id}.json").write_text(
            json.dumps(metadata, indent=2) + "\n", encoding="utf-8"
        )

    rows = [flatten_case(x) for x in cases]
    fieldnames: list[str] = []
    for row in rows:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)
    with (out_dir / "cases.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    catalog = ["1D WALL / RADIAL H2-AIR FLAME CAMPAIGN", "=" * 78, ""]
    for item in cases:
        case_path, _, _ = expected_paths(item)
        catalog.extend(
            [
                f"{item['case_id']}  {item['case_directory']}",
                f"  fingerprint: {item['case_fingerprint']}",
                f"  variant: {item['numerical_variant']}",
                f"  {item['label']}",
                f"  case_path: {case_path.as_posix()}",
                "",
            ]
        )
    (out_dir / "case_catalog.txt").write_text("\n".join(catalog), encoding="utf-8")
    return out_dir


def run_interface(
    out_dir: Path, cases: list[dict[str, Any]], laboratory: Laboratory
) -> None:
    exe = package_interface_1d(laboratory)
    workdir = laboratory.package_interface_workdir
    if not exe.is_file():
        raise FileNotFoundError(f"1D interface executable not found: {exe}")
    if not (workdir / "task_setup").is_dir():
        raise FileNotFoundError(
            f"package-interface working directory does not contain task_setup: {workdir}"
        )

    summary: list[dict[str, Any]] = []
    for item in cases:
        case_id = item["case_id"]
        setup = (out_dir / "_setups" / f"{case_id}.nml").resolve()
        status_path = out_dir / "_setups" / f"{case_id}.generation_status.json"
        print(f"[interface] {case_id}: {item['case_directory']}")
        started = datetime.now(timezone.utc)
        error_message = None
        try:
            result = subprocess.run([str(exe), str(setup)], cwd=workdir, check=False)
            code: int | None = int(result.returncode)
            status = "generated" if code == 0 else "failed"
        except OSError as exc:
            code = None
            status = "failed"
            error_message = str(exc)

        payload = {
            "case_id": case_id,
            "case_fingerprint": item["case_fingerprint"],
            "status": status,
            "interface_executable": str(exe),
            "interface_working_directory": str(workdir),
            "setup_file": str(setup),
            "started_at_utc": started.isoformat(),
            "finished_at_utc": datetime.now(timezone.utc).isoformat(),
            "exit_code": code,
            "error": error_message,
        }
        status_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        summary.append(payload)
        if status != "generated":
            raise RuntimeError(f"package interface failed for {case_id}; see {status_path}")

    fields = [
        "case_id",
        "case_fingerprint",
        "status",
        "exit_code",
        "started_at_utc",
        "finished_at_utc",
        "setup_file",
        "interface_executable",
        "interface_working_directory",
        "error",
    ]
    with (out_dir / "generation_summary.csv").open(
        "w", newline="", encoding="utf-8"
    ) as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(summary)


def print_preview(cases: list[dict[str, Any]], limit: int) -> None:
    print(f"Final cases: {len(cases)}")
    print("\nCases:")
    for item in cases[:limit]:
        print(f"  {item['case_directory']}  [{item['case_fingerprint']}]")
        print(f"      {item['label']}")
    if len(cases) > limit:
        print(f"  ... ({len(cases)-limit} more)")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("campaign", type=Path, help="1D flame campaign TOML")
    parser.add_argument(
        "--laboratory",
        type=Path,
        default=None,
        help=(
            "laboratory.toml; defaults to config/laboratory.toml or "
            "NRG_LABORATORY_CONFIG"
        ),
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--preview", action="store_true", help="validate and display expansion")
    mode.add_argument("--create", action="store_true", help="write cases.csv and _setups")
    mode.add_argument(
        "--run-interface", action="store_true", help="create files then invoke 1D package interface"
    )
    parser.add_argument("--overwrite", action="store_true", help="replace generator output directory")
    parser.add_argument("--limit", type=int, default=20, help="preview display limit")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    toml_path = args.campaign.resolve()
    laboratory = Laboratory.load(args.laboratory, require_runtime=not args.preview)
    laboratory.ensure_output_roots()
    data = read_campaign(toml_path)
    cases = expand_campaign(data, laboratory)

    if args.preview:
        print_preview(cases, args.limit)
        return 0

    out_dir = create_files(
        data, toml_path, cases, overwrite=args.overwrite, laboratory=laboratory
    )
    print(f"Generated {len(cases)} cases")
    print(f"Output directory: {out_dir}")
    print(f"Manifest: {out_dir / 'cases.csv'}")

    if args.run_interface:
        run_interface(out_dir, cases, laboratory)
        print("1D package-interface generation completed for all cases.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
