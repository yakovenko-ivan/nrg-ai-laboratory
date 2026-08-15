#!/usr/bin/env python3
"""Generate deterministic 0D constant-volume NRG ignition campaigns.

The generator is intentionally problem-specific at the experimental-design
layer, while producing the same execution contract as the generic NRG campaign
runner:

    campaign TOML -> cases.csv + _setups/*.nml -> package interface -> case dirs

Each case receives a campaign-local ID and a campaign-specific logical
fingerprint.  By default, identity fields are inferred from sweep axes and
numerical variants; an explicit [case_identity] table is recommended for
long-lived appendable campaigns.  The full generated configuration is hashed
separately as an attempt fingerprint.

Python 3.11+; standard library only.
"""

from __future__ import annotations

import argparse
import copy
import csv
from datetime import datetime, timezone
import hashlib
import itertools
import json
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
    "reactor_config",
    "mixture_config",
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
    "reactor_config": {
        "reactor_type",
        "cells_number_x",
        "cell_length_x",
    },
    "mixture_config": {
        "hydrogen_mole_percent",
        "n2_o2_molar_ratio",
        "initial_temperature",
        "initial_pressure",
    },
    "physics_config": {
        "mechanism_id",
        "solver_id",
        "initial_time_step",
        "cfl_enabled",
        "cfl_coefficient",
    },
    "run_control_config": {
        "termination_mode",
        "final_time_ms",
        "wall_time_limit_s",
        "wall_time_reserve_s",
    },
    "output_config": {
        "postprocess_interval_us",
        "field_save_interval_us",
        "checkpoint_interval_us",
        "save_spatial_fields",
    },
}

NUMERICAL_VARIANT_KEYS = {
    "cells_number_x": ("reactor_config", "cells_number_x"),
    "cell_length_x": ("reactor_config", "cell_length_x"),
    "initial_time_step": ("physics_config", "initial_time_step"),
    "cfl_enabled": ("physics_config", "cfl_enabled"),
    "cfl_coefficient": ("physics_config", "cfl_coefficient"),
    "postprocess_interval_us": ("output_config", "postprocess_interval_us"),
}

ALLOWED_SOLVERS = {
    "cpm",
    "cabaret",
    "cabaret_compressible",
    "cabaret_low_mach",
    "cabaret_lm",
    "fds",
    "fds_low_mach",
}
ALLOWED_MECHANISMS = {"keromnes", "konnov", "zhang", "agafonov", "tereza"}

GENERATOR_SCHEMA_VERSION = 3
FINGERPRINT_HEX_LENGTH = 32

TOKEN_ORDER = [
    ("mixture_config", "initial_temperature"),
    ("mixture_config", "initial_pressure"),
    ("mixture_config", "hydrogen_mole_percent"),
    ("physics_config", "mechanism_id"),
    ("physics_config", "solver_id"),
    ("reactor_config", "cells_number_x"),
    ("reactor_config", "cell_length_x"),
    ("physics_config", "initial_time_step"),
    ("run_control_config", "final_time_ms"),
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
    with path.open("rb") as f:
        data = tomllib.load(f)
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
            raise ValueError("[case_identity] unsupported key(s): " + ", ".join(sorted(unknown)))
        if not isinstance(case_identity.get("fields"), list) or not case_identity["fields"]:
            raise ValueError("[case_identity].fields must be a non-empty array")

    attempt_overrides = data.get("attempt_overrides", {})
    if attempt_overrides:
        if not isinstance(attempt_overrides, dict):
            raise ValueError("[attempt_overrides] must be a TOML table")
        unknown = set(attempt_overrides) - {"allowed"}
        if unknown:
            raise ValueError("[attempt_overrides] unsupported key(s): " + ", ".join(sorted(unknown)))
        if not isinstance(attempt_overrides.get("allowed"), list) or not attempt_overrides["allowed"]:
            raise ValueError("[attempt_overrides].allowed must be a non-empty array")

    # Building the policy also validates field-path syntax.
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


def numerical_variants(data: dict[str, Any]) -> list[tuple[str, str, dict[str, dict[str, Any]]]]:
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


def validate_case(groups: dict[str, dict[str, Any]]) -> None:
    for group in NAMELIST_GROUPS:
        if group not in groups:
            raise ValueError(f"missing namelist group {group}")
        unknown = set(groups[group]) - GROUP_KEYS[group]
        if unknown:
            raise ValueError(f"{group}: unsupported key(s): {', '.join(sorted(unknown))}")

    reactor = groups["reactor_config"]
    mix = groups["mixture_config"]
    phy = groups["physics_config"]
    run = groups["run_control_config"]
    out = groups["output_config"]

    if str(reactor["reactor_type"]).lower() != "constant_volume":
        raise ValueError("reactor_type must be 'constant_volume'")
    if int(reactor["cells_number_x"]) not in {2, 4}:
        raise ValueError("cells_number_x must be 2 or 4")
    if float(reactor["cell_length_x"]) <= 0:
        raise ValueError("cell_length_x must be positive")

    xh2 = float(mix["hydrogen_mole_percent"])
    if not 0.0 < xh2 < 100.0:
        raise ValueError("hydrogen_mole_percent must lie in (0,100)")
    if float(mix["n2_o2_molar_ratio"]) <= 0:
        raise ValueError("n2_o2_molar_ratio must be positive")
    if float(mix["initial_temperature"]) <= 0 or float(mix["initial_pressure"]) <= 0:
        raise ValueError("initial temperature and pressure must be positive")

    mechanism = str(phy["mechanism_id"]).lower()
    solver = str(phy["solver_id"]).lower()
    if mechanism not in ALLOWED_MECHANISMS:
        raise ValueError(f"unsupported mechanism_id {mechanism!r}")
    if solver not in ALLOWED_SOLVERS:
        raise ValueError(f"unsupported solver_id {solver!r}")
    if float(phy["initial_time_step"]) <= 0:
        raise ValueError("initial_time_step must be positive")
    cfl = float(phy["cfl_coefficient"])
    if not 0.0 < cfl <= 1.0:
        raise ValueError("cfl_coefficient must lie in (0,1]")

    mode = str(run["termination_mode"]).lower()
    if mode not in {"simulation_time", "wall_time", "either"}:
        raise ValueError("unsupported termination_mode")
    if mode in {"simulation_time", "either"} and float(run["final_time_ms"]) <= 0:
        raise ValueError("final_time_ms must be positive")
    if mode in {"wall_time", "either"}:
        limit = float(run["wall_time_limit_s"])
        reserve = float(run["wall_time_reserve_s"])
        if limit <= 0 or reserve < 0 or reserve >= limit:
            raise ValueError("invalid wall-time limit/reserve")

    for key in ("postprocess_interval_us", "field_save_interval_us", "checkpoint_interval_us"):
        if float(out[key]) <= 0:
            raise ValueError(f"{key} must be positive")


def canonical_scientific_payload(groups: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return {
        group: copy.deepcopy(groups[group])
        for group in NAMELIST_GROUPS
        if group != "case_config"
    }


def compute_case_fingerprint(
    groups: dict[str, dict[str, Any]],
    identity_policy: dict[str, Any] | None = None,
) -> str:
    """Return logical identity fingerprint when a campaign policy is supplied.

    The no-policy form intentionally retains the legacy full-configuration hash
    because the independent extension/composite subsystem imports this helper.
    """
    if identity_policy is not None:
        return identity_fingerprint(groups, identity_policy)
    canonical = json.dumps(
        canonical_scientific_payload(groups),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.blake2s(canonical, digest_size=16).hexdigest()


def sanitize_token(value: Any) -> str:
    if isinstance(value, bool):
        return "ON" if value else "OFF"
    text = str(value).strip()
    text = text.replace("+", "p").replace("-", "m").replace(".", "p")
    text = re.sub(r"[^A-Za-z0-9_]+", "_", text)
    return text.strip("_") or "x"


def compact_number(value: float) -> str:
    return f"{value:g}".replace(".", "p")


def solver_token(value: str) -> str:
    key = value.lower()
    return {
        "cpm": "CPM",
        "fds": "FDS",
        "fds_low_mach": "FDS",
        "cabaret": "CAB",
        "cabaret_compressible": "CAB",
        "cabaret_low_mach": "CABLM",
        "cabaret_lm": "CABLM",
    }.get(key, sanitize_token(value))


def varying_fields(cases: list[dict[str, Any]]) -> set[tuple[str, str]]:
    values: dict[tuple[str, str], set[str]] = {}
    for item in cases:
        for group in NAMELIST_GROUPS:
            if group == "case_config":
                continue
            for key, value in item["groups"][group].items():
                values.setdefault((group, key), set()).add(json.dumps(value, sort_keys=True))
    return {key for key, vals in values.items() if len(vals) > 1}


def directory_token(group: str, key: str, value: Any) -> str:
    if (group, key) == ("mixture_config", "initial_temperature"):
        return f"T{float(value):g}"
    if (group, key) == ("mixture_config", "initial_pressure"):
        return f"P{float(value)/101325.0:.3g}atm".replace(".", "p")
    if (group, key) == ("mixture_config", "hydrogen_mole_percent"):
        return f"H2_{float(value):g}".replace(".", "p")
    if (group, key) == ("physics_config", "mechanism_id"):
        return str(value).upper()
    if (group, key) == ("physics_config", "solver_id"):
        return solver_token(str(value))
    if (group, key) == ("reactor_config", "cells_number_x"):
        return f"N{int(value)}"
    if (group, key) == ("reactor_config", "cell_length_x"):
        return f"DX{compact_number(float(value)*1.0e6)}um"
    if (group, key) == ("physics_config", "initial_time_step"):
        return f"DT{compact_number(float(value)*1.0e9)}ns"
    if (group, key) == ("run_control_config", "final_time_ms"):
        return f"TF{float(value):g}ms".replace(".", "p")
    return f"{key}_{sanitize_token(value)}"


def make_label(item: dict[str, Any]) -> str:
    g = item["groups"]
    r = g["reactor_config"]
    m = g["mixture_config"]
    p = g["physics_config"]
    return (
        f"0D CV H2-air | T0={float(m['initial_temperature']):g} K, "
        f"p0={float(m['initial_pressure'])/101325.0:.3g} atm, "
        f"H2={float(m['hydrogen_mole_percent']):g} mol.% | "
        f"{str(p['mechanism_id']).upper()} | {solver_token(str(p['solver_id']))} | "
        f"N={int(r['cells_number_x'])}, dx={float(r['cell_length_x']):.3e} m, "
        f"dt0={float(p['initial_time_step']):.3e} s | {item['numerical_variant']}"
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
    for key in TOKEN_ORDER:
        if key in varying:
            group, field = key
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

    for sweep_override, (variant_name, variant_desc, variant_override) in itertools.product(sweeps, variants):
        groups = deep_merge(defaults, sweep_override)
        groups = deep_merge(groups, variant_override)
        groups.setdefault("case_config", {})["numerical_variant"] = variant_name
        validate_case({**groups, "case_config": groups["case_config"]})
        raw.append({
            "groups": groups,
            "numerical_variant": variant_name,
            "numerical_variant_description": variant_desc,
        })

    campaign = data["campaign"]
    max_cases = int(campaign.get("max_cases", 100))
    if len(raw) > max_cases:
        raise ValueError(
            f"campaign expands to {len(raw)} cases, exceeding max_cases={max_cases}; "
            "raise the limit explicitly after reviewing --preview"
        )

    prefix = str(campaign.get("case_id_prefix", "R"))
    width = int(campaign.get("case_id_width", 6))
    campaign_id = str(campaign.get("name", "0D_CV_validation"))
    results_root = str(laboratory.runs_root)

    cases: list[dict[str, Any]] = []
    for number, raw_item in enumerate(raw, start=1):
        item = copy.deepcopy(raw_item)
        item["case_id"] = f"{prefix}{number:0{width}d}"
        c = item["groups"].setdefault("case_config", {})
        c.update({
            "case_id": item["case_id"],
            "case_fingerprint": "",
            "case_directory": item["case_id"],
            "case_label": "",
            "campaign_id": campaign_id,
            "results_root": results_root,
            "numerical_variant": item["numerical_variant"],
        })
        cases.append(item)

    identity_policy = build_identity_policy(data)
    seen_identities: dict[str, str] = {}
    for item in cases:
        item["label"] = make_label(item)
        item["case_directory"] = build_case_directory(item, data, cases)
        c = item["groups"]["case_config"]
        c["case_directory"] = item["case_directory"]
        c["case_label"] = item["label"]
        item["case_fingerprint"] = compute_case_fingerprint(
            item["groups"], identity_policy
        )
        item["case_identity_fingerprint"] = item["case_fingerprint"]
        item["generated_attempt_fingerprint"] = attempt_fingerprint(item["groups"])
        item["fingerprint_scheme"] = identity_policy["fingerprint_scheme"]
        duplicate = seen_identities.get(item["case_fingerprint"])
        if duplicate is not None:
            raise ValueError(
                f"campaign contains duplicate logical case identity: "
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
        "! Generated by campaign_generator_0d.py",
        "! Agentic homogeneous constant-volume reactor campaign.",
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
    case_path = Path(str(cfg["results_root"])) / str(cfg["campaign_id"]) / item["case_directory"]
    return case_path, case_path / "data_save", case_path / "data_output"


def flatten_case(item: dict[str, Any]) -> dict[str, Any]:
    case_path, data_save, data_output = expected_paths(item)
    row: dict[str, Any] = {
        "case_id": item["case_id"],
        "case_fingerprint": item["case_fingerprint"],
        "case_identity_fingerprint": item.get("case_identity_fingerprint", item["case_fingerprint"]),
        "generated_attempt_fingerprint": item.get("generated_attempt_fingerprint", ""),
        "fingerprint_scheme": item.get("fingerprint_scheme", "legacy_full_configuration_v1"),
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
    name = str(data["campaign"].get("name", "0D_CV"))
    return (laboratory.campaign_root / name).resolve()


def create_files(data: dict[str, Any], toml_path: Path, cases: list[dict[str, Any]], overwrite: bool, laboratory: Laboratory) -> Path:
    out_dir = campaign_directory(data, laboratory)
    setup_dir = out_dir / "_setups"
    if out_dir.exists() and not overwrite and any(out_dir.iterdir()):
        raise FileExistsError(f"{out_dir} already exists and is not empty; use --overwrite")
    if out_dir.exists() and overwrite:
        shutil.rmtree(out_dir)
    setup_dir.mkdir(parents=True, exist_ok=True)

    (out_dir / "campaign.toml").write_text(toml_path.read_text(encoding="utf-8"), encoding="utf-8")

    runtime_manifest = laboratory.runtime_manifest
    manifest = {
        "generator_schema_version": GENERATOR_SCHEMA_VERSION,
        "campaign_type": "0d_constant_volume_ignition",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_campaign_sha256": hashlib.sha256(toml_path.read_bytes()).hexdigest(),
        "laboratory_config": str(laboratory.config_path),
        "laboratory_config_sha256": sha256_file(laboratory.config_path),
        "laboratory_local_config": (
            str(laboratory.local_config_path) if laboratory.local_config_path else None
        ),
        "laboratory_local_config_sha256": (
            sha256_file(laboratory.local_config_path) if laboratory.local_config_path else None
        ),
        "runs_root": str(laboratory.runs_root),
        "package_interface_0d": str(laboratory.package_interface_0d),
        "package_interface_0d_sha256": sha256_file(laboratory.package_interface_0d),
        "computing_module": str(laboratory.computing_module),
        "computing_module_sha256": sha256_file(laboratory.computing_module),
        "runtime_manifest": str(runtime_manifest) if runtime_manifest.is_file() else None,
        "runtime_manifest_sha256": sha256_file(runtime_manifest) if runtime_manifest.is_file() else None,
        "campaign_name": str(data["campaign"].get("name", "0D_CV")),
        "case_count": len(cases),
        "identity_policy": build_identity_policy(data),
        "case_fingerprint_scheme": "campaign_identity_v1",
        "case_fingerprints": {x["case_id"]: x["case_fingerprint"] for x in cases},
        "generated_attempt_fingerprints": {
            x["case_id"]: x.get("generated_attempt_fingerprint") for x in cases
        },
    }
    (out_dir / "campaign_manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )

    for item in cases:
        case_id = item["case_id"]
        (setup_dir / f"{case_id}.nml").write_text(render_namelist(item["groups"]), encoding="utf-8")
        case_path, data_save, data_output = expected_paths(item)
        metadata = {
            "case_id": case_id,
            "case_fingerprint": item["case_fingerprint"],
            "case_identity_fingerprint": item.get("case_identity_fingerprint", item["case_fingerprint"]),
            "generated_attempt_fingerprint": item.get("generated_attempt_fingerprint"),
            "fingerprint_scheme": item.get("fingerprint_scheme", "legacy_full_configuration_v1"),
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
    with (out_dir / "cases.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    catalog = ["0D CONSTANT-VOLUME IGNITION CAMPAIGN", "=" * 78, ""]
    for item in cases:
        case_path, _, _ = expected_paths(item)
        catalog.extend([
            f"{item['case_id']}  {item['case_directory']}",
            f"  fingerprint: {item['case_fingerprint']}",
            f"  variant: {item['numerical_variant']}",
            f"  {item['label']}",
            f"  case_path: {case_path.as_posix()}",
            "",
        ])
    (out_dir / "case_catalog.txt").write_text("\n".join(catalog), encoding="utf-8")
    return out_dir


def run_interface(
    data: dict[str, Any], out_dir: Path, cases: list[dict[str, Any]], laboratory: Laboratory
) -> None:
    exe = laboratory.package_interface_0d
    workdir = laboratory.package_interface_workdir
    if not exe.is_file():
        raise FileNotFoundError(f"interface executable not found: {exe}")
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
        "case_id", "case_fingerprint", "status", "exit_code",
        "started_at_utc", "finished_at_utc", "setup_file",
        "interface_executable", "interface_working_directory", "error",
    ]
    with (out_dir / "generation_summary.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
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
    parser.add_argument("campaign", type=Path, help="0D campaign TOML")
    parser.add_argument("--laboratory", type=Path, default=None, help="laboratory.toml; defaults to config/laboratory.toml or NRG_LABORATORY_CONFIG")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--preview", action="store_true", help="validate and display expansion")
    mode.add_argument("--create", action="store_true", help="write cases.csv and _setups")
    mode.add_argument("--run-interface", action="store_true", help="create files then invoke package interface")
    parser.add_argument("--overwrite", action="store_true", help="replace generator output directory")
    parser.add_argument("--limit", type=int, default=20, help="preview display limit")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    toml_path = args.campaign.resolve()
    laboratory = Laboratory.load(args.laboratory)
    laboratory.ensure_output_roots()
    data = read_campaign(toml_path)
    cases = expand_campaign(data, laboratory)
    if args.preview:
        print_preview(cases, args.limit)
        return 0

    out_dir = create_files(data, toml_path, cases, overwrite=args.overwrite, laboratory=laboratory)
    print(f"Generated {len(cases)} cases")
    print(f"Output directory: {out_dir}")
    print(f"Manifest: {out_dir / 'cases.csv'}")
    if args.run_interface:
        run_interface(data, out_dir, cases, laboratory)
        print("Package-interface generation completed for all cases.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
