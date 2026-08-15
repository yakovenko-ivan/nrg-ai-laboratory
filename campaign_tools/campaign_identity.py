"""Campaign-specific logical case identity and execution-attempt fingerprints.

A logical case is identified only by fields declared as campaign identity axes.
Execution attempts may change reviewed tunable fields without changing logical
case identity.  Full effective configuration is still hashed separately as an
attempt fingerprint.

For legacy generated campaigns without an explicit [case_identity] table, the
identity axes are inferred from sweep axes and numerical-variant fields.  This
matches the research-design interpretation: parameters varied by the campaign
distinguish cases; fixed numerical/output/run-control settings do not.
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError as exc:  # pragma: no cover
    raise RuntimeError("Python 3.11+ is required") from exc


DEFAULT_ATTEMPT_TUNABLE_FIELDS = (
    "physics_config.initial_time_step",
    "physics_config.cfl_enabled",
    "physics_config.cfl_coefficient",
    "run_control_config.*",
    "output_config.*",
)

NUMERICAL_VARIANT_FIELD_MAP = {
    "cells_number_x": "reactor_config.cells_number_x",
    "cell_length_x": "reactor_config.cell_length_x",
    "initial_time_step": "physics_config.initial_time_step",
    "cfl_enabled": "physics_config.cfl_enabled",
    "cfl_coefficient": "physics_config.cfl_coefficient",
    "postprocess_interval_us": "output_config.postprocess_interval_us",
}

NON_CASE_GROUPS = (
    "reactor_config",
    "mixture_config",
    "physics_config",
    "run_control_config",
    "output_config",
)


def _hash(payload: Any) -> str:
    raw = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return hashlib.blake2s(raw, digest_size=16).hexdigest()


def split_field(field: str) -> tuple[str, str]:
    field = str(field).strip()
    if field.count(".") != 1:
        raise ValueError(f"field must be 'group.key': {field!r}")
    group, key = field.split(".", 1)
    if group not in NON_CASE_GROUPS:
        raise ValueError(f"unsupported identity/override group: {group!r}")
    if not key:
        raise ValueError(f"empty key in field {field!r}")
    return group, key


def normalize_fields(fields: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    normalized = []
    for raw in fields:
        field = str(raw).strip()
        split_field(field.replace(".*", ".placeholder") if field.endswith(".*") else field)
        if field not in normalized:
            normalized.append(field)
    return tuple(sorted(normalized))


def infer_identity_fields(campaign_data: dict[str, Any]) -> tuple[str, ...]:
    explicit = campaign_data.get("case_identity", {})
    if isinstance(explicit, dict) and explicit.get("fields"):
        return normalize_fields(list(explicit["fields"]))

    fields: list[str] = []
    sweep = campaign_data.get("sweep", {})
    if isinstance(sweep, dict):
        for group, values in sweep.items():
            if not isinstance(values, dict):
                continue
            for key in values:
                field = f"{group}.{key}"
                if field not in fields:
                    fields.append(field)

    for variant in campaign_data.get("numerical_variants", []):
        if not isinstance(variant, dict):
            continue
        for key in variant:
            if key in {"name", "description"}:
                continue
            mapped = NUMERICAL_VARIANT_FIELD_MAP.get(key)
            if mapped and mapped not in fields:
                fields.append(mapped)

    return normalize_fields(fields)


def attempt_tunable_fields(campaign_data: dict[str, Any]) -> tuple[str, ...]:
    explicit = campaign_data.get("attempt_overrides", {})
    if isinstance(explicit, dict) and explicit.get("allowed"):
        return normalize_fields(list(explicit["allowed"]))
    return normalize_fields(list(DEFAULT_ATTEMPT_TUNABLE_FIELDS))


def build_policy(campaign_data: dict[str, Any]) -> dict[str, Any]:
    identity = infer_identity_fields(campaign_data)
    tunable = attempt_tunable_fields(campaign_data)
    source = (
        "explicit"
        if isinstance(campaign_data.get("case_identity"), dict)
        and campaign_data["case_identity"].get("fields")
        else "inferred_from_campaign_design"
    )
    schema_payload = {
        "schema": "campaign_identity_v1",
        "identity_fields": list(identity),
    }
    return {
        "policy_schema_version": 1,
        "fingerprint_scheme": "campaign_identity_v1",
        "identity_fields": list(identity),
        "attempt_tunable_fields": list(tunable),
        "identity_source": source,
        "schema_fingerprint": _hash(schema_payload),
    }


def load_campaign_data_from_generated(cases_csv: Path) -> dict[str, Any]:
    campaign_toml = cases_csv.parent / "campaign.toml"
    if not campaign_toml.is_file():
        raise FileNotFoundError(
            f"generated campaign definition copy not found: {campaign_toml}"
        )
    with campaign_toml.open("rb") as f:
        return tomllib.load(f)


def load_policy_for_generated(cases_csv: Path) -> dict[str, Any]:
    manifest_path = cases_csv.parent / "campaign_manifest.json"
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            manifest = {}
        policy = manifest.get("identity_policy")
        if isinstance(policy, dict) and policy.get("identity_fields") is not None:
            return policy
    return build_policy(load_campaign_data_from_generated(cases_csv))


def get_value(groups: dict[str, dict[str, Any]], field: str) -> Any:
    group, key = split_field(field)
    try:
        return groups[group][key]
    except KeyError as exc:
        raise KeyError(f"configuration field missing: {field}") from exc


def set_value(groups: dict[str, dict[str, Any]], field: str, value: Any) -> None:
    group, key = split_field(field)
    if group not in groups:
        raise KeyError(f"configuration group missing: {group}")
    if key not in groups[group]:
        raise KeyError(f"configuration field missing: {field}")

    current = groups[group][key]
    if isinstance(current, bool):
        if not isinstance(value, bool):
            raise TypeError(f"{field} requires a boolean override")
        coerced = value
    elif isinstance(current, int) and not isinstance(current, bool):
        if not isinstance(value, int) or isinstance(value, bool):
            raise TypeError(f"{field} requires an integer override")
        coerced = int(value)
    elif isinstance(current, float):
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise TypeError(f"{field} requires a numeric override")
        coerced = float(value)
    elif isinstance(current, str):
        if not isinstance(value, str):
            raise TypeError(f"{field} requires a string override")
        coerced = value
    else:
        if type(value) is not type(current):
            raise TypeError(
                f"{field} override type {type(value).__name__} does not match "
                f"{type(current).__name__}"
            )
        coerced = copy.deepcopy(value)

    groups[group][key] = coerced


def identity_payload(
    groups: dict[str, dict[str, Any]],
    policy: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schema_fingerprint": policy["schema_fingerprint"],
        "values": {
            field: get_value(groups, field)
            for field in policy.get("identity_fields", [])
        },
    }


def identity_fingerprint(
    groups: dict[str, dict[str, Any]],
    policy: dict[str, Any],
) -> str:
    return _hash(identity_payload(groups, policy))


def attempt_payload(groups: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return {
        group: copy.deepcopy(groups[group])
        for group in NON_CASE_GROUPS
    }


def attempt_fingerprint(groups: dict[str, dict[str, Any]]) -> str:
    return _hash(attempt_payload(groups))


def wildcard_matches(pattern: str, field: str) -> bool:
    if pattern.endswith(".*"):
        return field.startswith(pattern[:-1])
    return pattern == field


def is_identity_field(field: str, policy: dict[str, Any]) -> bool:
    return field in set(policy.get("identity_fields", []))


def is_attempt_override_allowed(field: str, policy: dict[str, Any]) -> bool:
    split_field(field)
    if is_identity_field(field, policy):
        return False
    return any(
        wildcard_matches(pattern, field)
        for pattern in policy.get("attempt_tunable_fields", [])
    )


def validate_overrides(
    overrides: dict[str, Any],
    policy: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    accepted: dict[str, Any] = {}
    rejected: list[dict[str, str]] = []
    for field, value in overrides.items():
        try:
            split_field(field)
        except ValueError as exc:
            rejected.append({"field": field, "reason": str(exc)})
            continue
        if is_identity_field(field, policy):
            rejected.append({
                "field": field,
                "reason": "field is a logical case-identity axis; changing it requires a new case",
            })
            continue
        if not is_attempt_override_allowed(field, policy):
            rejected.append({
                "field": field,
                "reason": "field is not in the campaign's reviewed attempt-tunable allow-list",
            })
            continue
        accepted[field] = copy.deepcopy(value)
    return accepted, rejected


def apply_overrides(
    groups: dict[str, dict[str, Any]],
    overrides: dict[str, Any],
    policy: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    accepted, rejected = validate_overrides(overrides, policy)
    if rejected:
        raise ValueError("invalid attempt overrides: " + json.dumps(rejected))
    result = copy.deepcopy(groups)
    for field, value in accepted.items():
        set_value(result, field, value)
    return result


def protected_constant_fields(
    groups: dict[str, dict[str, Any]],
    policy: dict[str, Any],
) -> tuple[str, ...]:
    fields: list[str] = []
    for group in NON_CASE_GROUPS:
        for key in groups[group]:
            field = f"{group}.{key}"
            if field in policy.get("identity_fields", []):
                continue
            if is_attempt_override_allowed(field, policy):
                continue
            fields.append(field)
    return tuple(sorted(fields))
