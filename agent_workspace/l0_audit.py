"""Mechanism-independent L0 audit for the NRG mesh-free thermochemistry interface.

Auditable core of the G1-S1a trusted bootstrap.  It plans and executes the
**existing, unmodified** NRG program
``package_interface_0D_chemical_equilibrium_validation`` for a fixed, hash-pinned
set of mechanisms and grammar fixtures.

Design rules (see ``G1S1A_DEFECT_REGISTER.md`` and ``G1S1A_TEST_EVIDENCE.md``):

* no import of ``lab_bridge``; the host supplies every privileged primitive, so
  this module is unit-testable without git, subprocess or a laboratory;
* no shell, no command string, no arbitrary executable path, and no
  user-supplied argument can reach the child process;
* every input file is verified against the **approved commit blob**, never
  against a working tree;
* every run happens in its own freshly created directory under ``run_root``;
* expected outputs are asserted, then hashed; raw bytes are never edited;
* any surprise is an error, never a warning.

Host contract (``AuditHost.run_bounded``)
-----------------------------------------
``run_bounded(argv, label, timeout, log_path, cwd=<dir>)`` runs one fixed argv in
``cwd`` and:

* returns (normally) only on a zero exit status;
* raises an exception carrying ``returncode: int``, ``timed_out: bool`` and
  ``cancelled: bool`` attributes on any other outcome (the patched bridge raises
  ``_BoundedRunError``);
* always writes ``log_path`` with a ``# result: EXIT_<n>|TIMEOUT|CANCELLED``
  header, even on failure.

Outcome classification never relies on message text alone: a timeout is a
timeout even if the log contains a matching parser diagnostic, and an expected
grammar refusal requires a provable nonzero exit status.

``COMPLETE`` is never returned unless the case directory is clean (no unexpected
entry, no symlink, no unexpected directory) **and** mandatory provenance (the
trusted runner log) could be hashed.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

SCHEMA = "nrg-l0-audit/3"
MODES = ("tp", "hp")

_ID_RE = re.compile(r"^[A-Z0-9_]{2,8}$")
_FILE_RE = re.compile(r"^[A-Za-z0-9_./-]+\.txt$")
_SPECIES_RE = re.compile(r"^[A-Za-z0-9*()+_\-]{1,20}$")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_LOG_RESULT_RE = re.compile(r"^# result: ([A-Z0-9_]+)\s*$", re.MULTILINE)
_PATH_PREFIXES = ("hydrogen/", "ammonia/", "acetylene/")

T_MIN, T_MAX = 200.0, 6000.0
P_MIN, P_MAX = 1.0e2, 1.0e8
MAX_COMPONENTS = 8
MAX_FIXTURE_RUNS = 8
MAX_ARTIFACTS = 64
MAX_COMPONENT_TOTAL = 1.0e6

COMMON_OUTPUTS = (
    "_input.csv",
    "_initial_composition.csv",
    "_species.csv",
    "_elements.csv",
    "_transport.csv",
    "_reaction_parameters.csv",
    "_third_body_efficiencies.csv",
    "_thermodynamics.csv",
    "_kinetics_source.csv",
    "_kinetics_grid.csv",
    "_reaction_rop.csv",
)
MODE_OUTPUTS = {
    "tp": COMMON_OUTPUTS + ("_tp_equilibrium_rates.csv", "_tp_state.csv", "_tp_diagnostics.csv"),
    "hp": COMMON_OUTPUTS + ("_hp_state.csv", "_hp_diagnostics.csv"),
}

# The single directory a case directory is allowed to contain: the per-case
# copy of the staged task_setup tree.  Any other directory is an
# UNEXPECTED_OUTPUT result; any symlink at all is an UNSAFE_OUTPUT result.
ALLOWED_CASE_SUBDIR = "task_setup"

# Files a case directory is ALLOWED to contain in addition to the expected
# output set.  Anything else is an UNEXPECTED_OUTPUT result: reported with
# hashes, never silently ignored, never classified COMPLETE.
#   * the namelist we wrote,
#   * the trusted runner log (stdout/stderr + outcome header),
#   * the two binary scratch data files the Fortran classes rewrite in cwd.
ALLOWED_EXTRA_FILES = frozenset(
    {
        "equilibrium_validation.nml",
        "run.log",
        "chemical_data.inf",
        "thermophysical_setup.inf",
    }
)

# Result vocabulary.  Only STATUS_OK results contribute to ``complete``.
STATUS_COMPLETE = "COMPLETE"
STATUS_REFUSED = "REFUSED_AS_EXPECTED"
STATUS_REFUSED_NO_MESSAGE = "REFUSED_WITHOUT_MESSAGE"
STATUS_EXIT_UNVERIFIED = "EXIT_UNVERIFIED"
STATUS_UNEXPECTED_SUCCESS = "UNEXPECTED_SUCCESS"
STATUS_UNEXPECTED_OUTPUT = "UNEXPECTED_OUTPUT"
STATUS_UNSAFE_OUTPUT = "UNSAFE_OUTPUT"
STATUS_MISSING_OUTPUT = "MISSING_OUTPUT"
STATUS_PROVENANCE_INCOMPLETE = "PROVENANCE_INCOMPLETE"
STATUS_PROVENANCE_MISMATCH = "PROVENANCE_MISMATCH"
STATUS_TIMEOUT = "TIMEOUT"
STATUS_CANCELLED = "CANCELLED"
STATUS_FAILED = "FAILED"
STATUS_BLOCKED = "BLOCKED"
STATUS_OK = (STATUS_COMPLETE, STATUS_REFUSED)

_FIXTURE_HEAD = "ELEMENTS\nH O N\nEND\nSPECIES\nH2 O2 H2O H O OH HO2\nEND\n"

# Grammar fixtures.  Embedded constants (not files) so their bytes cannot drift.
# Each is written into a *copy* of the staged task_setup and hash-checked there
# against FIXTURE_SHA256, which is frozen data -- not a value computed from these
# strings.
FIXTURES: dict[str, dict[str, Any]] = {
    "P1_MOLECULES": {
        "kind": "positive",
        "text": _FIXTURE_HEAD + "REACTIONS MOLECULES\nH+O2=OH+O 1.2E+14 0.0 15286.0\nEND\n",
        "expect": (),
    },
    "P1_MOLES": {
        "kind": "positive",
        "text": _FIXTURE_HEAD + "REACTIONS MOLES\nH+O2=OH+O 1.2E+14 0.0 15286.0\nEND\n",
        "expect": (),
    },
    "N1_PLOG": {
        "kind": "refusal",
        "text": _FIXTURE_HEAD
        + "REACTIONS CAL/MOLE\nH+O2=OH+O 1.2E+14 0.0 15286.0\n"
        + "    PLOG / 1.0 1.2E+14 0.0 15286.0 /\nEND\n",
        "expect": ("unsupported kinetics directive: PLOG",),
    },
    "N2_CHEB": {
        "kind": "refusal",
        "text": _FIXTURE_HEAD
        + "REACTIONS CAL/MOLE\nH+O2=OH+O 1.2E+14 0.0 15286.0\n"
        + "    CHEB / 1 2 3 4 /\nEND\n",
        "expect": ("unsupported kinetics directive: CHEB",),
    },
    "N3_SRI": {
        "kind": "refusal",
        "text": _FIXTURE_HEAD
        + "REACTIONS CAL/MOLE\nH+O2=OH+O 1.2E+14 0.0 15286.0\n"
        + "    SRI / 1.0 2.0 3.0 /\nEND\n",
        "expect": ("unsupported kinetics directive: SRI",),
    },
    "N4_REV": {
        "kind": "refusal",
        "text": _FIXTURE_HEAD
        + "REACTIONS CAL/MOLE\nH+O2=OH+O 1.2E+14 0.0 15286.0\n"
        + "    REV / 1.2E+14 0.0 15286.0 /\nEND\n",
        "expect": ("unsupported kinetics directive: REV",),
    },
    "N5_FRACTIONAL": {
        "kind": "refusal",
        # The parser tests the numeric prefix before the "." test, so a leading
        # fractional coefficient is refused as "invalid stoichiometry" while a
        # fractional tail reaches the dedicated message.  Both refuse the same
        # unsupported construct; this frozen set is the accepted outcome.
        "text": _FIXTURE_HEAD
        + "REACTIONS CAL/MOLE\n0.5O2+H=OH+O 1.2E+14 0.0 15286.0\nEND\n",
        "expect": (
            "invalid stoichiometry",
            "fractional stoichiometry unsupported",
        ),
    },
}

# Independently frozen fixture identities.  These literals were produced with
# ``sha256sum`` over the packaged fixture artifacts
# (g1_security_bootstrap_patch_final/evidence/fixtures/*.txt), NOT computed from
# the ``FIXTURES`` strings at import time.  A drift in either the embedded text
# or the artifact therefore fails the module load (see
# ``_verify_fixture_identity``), instead of silently agreeing with itself.
FIXTURE_SHA256: dict[str, str] = {
    "P1_MOLECULES": "a20080a6bc23141a71d05f44da5e2b6fb09734d608e2b9cdb8903349e7fe31e3",
    "P1_MOLES": "e85a25201121b859c07ca3203f7262a380b41673e94c8d8ea7d76b00fc5af4cd",
    "N1_PLOG": "ccdf5adefd90cf299523b8e230ad614d207a5ecd8df13e12d6bb3c1838b8fb19",
    "N2_CHEB": "b949f9815c689a84af8d247b7a18c44a3056b3ea553d6d8f64bac7da8daa2241",
    "N3_SRI": "d13912dd872a72b607705505e3bd274f6b830eaf25ccbd9088098890b98afc68",
    "N4_REV": "96fa9bbc7eee068025d8cdd23826888124ed293ea78a506c74e29490a8a01ae5",
    "N5_FRACTIONAL": "95b640149537cac2b8d6c470e89748f08b5c8dfce3d38ab63d4375daa6579f73",
}


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canonical_sha256(value: Any) -> str:
    return _sha256_text(json.dumps(value, sort_keys=True, separators=(",", ":")))


class AuditError(RuntimeError):
    """Fail-closed audit condition."""


def _verify_fixture_identity() -> None:
    """Fail closed if the embedded fixture text drifted from frozen data."""
    if set(FIXTURE_SHA256) != set(FIXTURES):
        raise AuditError("FIXTURE_SHA256 does not cover exactly the frozen fixture set")
    for name, spec in FIXTURES.items():
        expected = FIXTURE_SHA256[name]
        if not _HASH_RE.match(expected):
            raise AuditError(f"frozen fixture hash for {name} is not a 64-hex SHA-256")
        if _sha256_text(spec["text"]) != expected:
            raise AuditError(
                f"fixture {name} text does not match its independently frozen SHA-256; "
                "audit fixtures are immutable without a new reviewed package"
            )


_verify_fixture_identity()


@dataclass(frozen=True)
class AuditHost:
    """Privileged primitives supplied by the trusted bridge (never by a user)."""

    repo_root: Path
    build_root: Path
    run_root: Path
    commit: str
    source_sha256: str
    executable_name: str
    build_type: str
    sha256_file: Callable[[Path], str]
    commit_blob_sha256: Callable[[str], str]
    stage_task_setup: Callable[[Path], dict[str, Any]]
    run_bounded: Callable[..., str]
    verify_worktree: Callable[[], dict[str, Any]]
    utc_now: Callable[[], str]


def load_audit_authorization(table: dict[str, Any]) -> dict[str, Any]:
    """Validate and normalize ``[e0_authorization.audit]``; fail closed."""
    if not isinstance(table, dict):
        raise AuditError("authorization record is not a table")
    audit = table.get("audit")
    if not isinstance(audit, dict):
        raise AuditError("authorization record has no [e0_authorization.audit] table")
    exe = audit.get("executable_sha256")
    if not isinstance(exe, str) or not _HASH_RE.match(exe):
        raise AuditError("[e0_authorization.audit].executable_sha256 must be a 64-hex SHA-256")
    build_commit = audit.get("build_commit")
    if not isinstance(build_commit, str) or not _COMMIT_RE.match(build_commit):
        raise AuditError("[e0_authorization.audit].build_commit must be a full 40-hex commit")
    if build_commit != table.get("commit"):
        raise AuditError("[e0_authorization.audit].build_commit must equal [e0_authorization].commit")
    times: dict[str, int] = {}
    for key, lo, hi in (("timeout_seconds", 10, 600), ("total_timeout_seconds", 60, 1500)):
        value = audit.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or not lo <= value <= hi:
            raise AuditError(f"[e0_authorization.audit].{key} must be an int in {lo}..{hi}")
        times[key] = value
    if times["total_timeout_seconds"] < times["timeout_seconds"]:
        raise AuditError("[e0_authorization.audit].total_timeout_seconds must be >= timeout_seconds")
    fixtures_enabled = audit.get("fixtures_enabled", False)
    if not isinstance(fixtures_enabled, bool):
        raise AuditError("[e0_authorization.audit].fixtures_enabled must be a boolean")
    raw_cases = audit.get("case")
    if not isinstance(raw_cases, list) or not raw_cases:
        raise AuditError("[e0_authorization.audit] must contain at least one [[e0_authorization.audit.case]] block")
    cases: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw_cases:
        case = _normalize_case(item)
        if case["id"] in seen:
            raise AuditError(f"duplicate audit case id: {case['id']}")
        if case["id"] in FIXTURES:
            raise AuditError(f"audit case id {case['id']} collides with a frozen fixture id")
        seen.add(case["id"])
        cases.append(case)
    return {
        "executable_sha256": exe,
        "build_commit": build_commit,
        "timeout_seconds": times["timeout_seconds"],
        "total_timeout_seconds": times["total_timeout_seconds"],
        "fixtures_enabled": fixtures_enabled,
        "cases": cases,
        "cases_sha256": _canonical_sha256(cases),
        "fixtures_sha256": _canonical_sha256(FIXTURE_SHA256),
    }


def _normalize_case(item: Any) -> dict[str, Any]:
    """Validate one authorized case.  Required keys are exactly the documented set."""
    if not isinstance(item, dict):
        raise AuditError("audit case must be a table")
    required_keys = (
        "id",
        "mode",
        "mechanism",
        "thermo",
        "transport",
        "mechanism_sha256",
        "thermo_sha256",
        "transport_sha256",
        "tp_temperature",
        "initial_temperature",
        "pressure",
        "components",
    )
    missing = [k for k in required_keys if k not in item]
    if missing:
        raise AuditError(f"audit case is missing required keys: {missing}")
    cid = item.get("id")
    if not isinstance(cid, str) or not _ID_RE.match(cid):
        raise AuditError("audit case id must match ^[A-Z0-9_]{2,8}$")
    mode = item.get("mode")
    if mode not in MODES:
        raise AuditError(f"audit case {cid}: mode must be one of {MODES} (all/crossover are forbidden)")
    files: dict[str, str] = {}
    for key in ("mechanism", "thermo", "transport"):
        value = item.get(key)
        if not isinstance(value, str) or not _FILE_RE.match(value) or ".." in value or value.startswith("/"):
            raise AuditError(f"audit case {cid}: {key} must be a safe relative .txt path")
        files[key] = value
    if not files["mechanism"].startswith(_PATH_PREFIXES):
        raise AuditError(f"audit case {cid}: mechanism must live under hydrogen/, ammonia/ or acetylene/")
    hashes: dict[str, str] = {}
    for key in ("mechanism_sha256", "thermo_sha256", "transport_sha256"):
        value = item.get(key)
        if not isinstance(value, str) or not _HASH_RE.match(value):
            raise AuditError(f"audit case {cid}: {key} must be a 64-hex SHA-256")
        hashes[key] = value
    numbers: dict[str, float] = {}
    for key, lo, hi in (
        ("tp_temperature", T_MIN, T_MAX),
        ("initial_temperature", T_MIN, T_MAX),
        ("pressure", P_MIN, P_MAX),
    ):
        value = item.get(key)
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not lo <= float(value) <= hi:
            raise AuditError(f"audit case {cid}: {key} must be a number in [{lo}, {hi}]")
        numbers[key] = float(value)
    raw = item.get("components")
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_COMPONENTS:
        raise AuditError(f"audit case {cid}: components must be a list of 1..{MAX_COMPONENTS} pairs")
    comps: list[list[Any]] = []
    for pair in raw:
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            raise AuditError(f"audit case {cid}: each component must be [species, moles]")
        name, amount = pair
        if not isinstance(name, str) or not _SPECIES_RE.match(name):
            raise AuditError(f"audit case {cid}: invalid component species {name!r}")
        if not isinstance(amount, (int, float)) or isinstance(amount, bool):
            raise AuditError(f"audit case {cid}: component {name} amount must be numeric")
        amount = float(amount)
        if not amount > 0.0 or amount in (float("inf"),) or amount != amount:
            raise AuditError(f"audit case {cid}: component {name} amount must be finite and positive")
        comps.append([name, amount])
    total = sum(a for _, a in comps)
    if total <= 0.0:
        raise AuditError(f"audit case {cid}: component total must be positive")
    if total > MAX_COMPONENT_TOTAL:
        raise AuditError(f"audit case {cid}: component total {total} exceeds {MAX_COMPONENT_TOTAL}")
    if len({n for n, _ in comps}) != len(comps):
        raise AuditError(f"audit case {cid}: duplicate component species")
    return {"id": cid, "mode": mode, "components": comps, **files, **hashes, **numbers}


def render_namelist(case: dict[str, Any]) -> str:
    """Render the fixed /equilibrium_validation/ namelist from validated values."""
    lines = [
        "&equilibrium_validation",
        f"  mode = '{case['mode']}'",
        f"  mechanism_file = '{case['mechanism']}'",
        f"  thermo_file = '{case['thermo']}'",
        f"  transport_file = '{case['transport']}'",
        f"  components_number = {len(case['components'])}",
    ]
    for index, (name, _) in enumerate(case["components"], start=1):
        lines.append(f"  component_species({index}) = '{name}'")
    for index, (_, amount) in enumerate(case["components"], start=1):
        lines.append(f"  component_moles({index}) = {amount:.16e}")
    lines += [
        f"  tp_temperature = {case['tp_temperature']:.16e}",
        f"  initial_temperature = {case['initial_temperature']:.16e}",
        f"  pressure = {case['pressure']:.16e}",
        "  crossover_temperature_min = 4.0000000000000000e+02",
        "  crossover_temperature_max = 3.0000000000000000e+03",
        f"  output_prefix = '{case['id'].lower()}'",
        "/",
        "",
    ]
    return "\n".join(lines)


def _executable(host: AuditHost) -> Path:
    return host.build_root / "bin" / host.build_type.title() / host.executable_name


def _output_policy() -> dict[str, Any]:
    """The single published statement of the case-directory policy."""
    return {
        "allowed_extra_files": sorted(ALLOWED_EXTRA_FILES),
        "allowed_subdirectory": ALLOWED_CASE_SUBDIR,
        "symlink_policy": "rejected: any symlink entry in the case directory",
        "max_artifacts": MAX_ARTIFACTS,
        "on_unexpected_entry": STATUS_UNEXPECTED_OUTPUT,
        "on_unsafe_entry": STATUS_UNSAFE_OUTPUT,
        "on_incomplete_provenance": STATUS_PROVENANCE_INCOMPLETE,
    }


# --------------------------------------------------------------------------- #
# planning
# --------------------------------------------------------------------------- #
def plan(host: AuditHost, auth: dict[str, Any]) -> dict[str, Any]:
    """Read-only readiness plan.  Writes nothing."""
    if not _COMMIT_RE.match(host.commit) or host.commit != auth["build_commit"]:
        raise AuditError("host commit does not match authorized build_commit")
    exe = _executable(host)
    if not exe.is_file():
        raise AuditError(f"audit executable missing (build not performed or build_root wrong): {exe}")
    exe_sha = host.sha256_file(exe)
    if exe_sha != auth["executable_sha256"]:
        raise AuditError("audit executable SHA-256 does not match authorized executable_sha256")
    manifest_path = host.build_root / "build_manifest.json"
    if not manifest_path.is_file():
        raise AuditError(f"build_manifest.json missing: {manifest_path}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - fail closed on any read/parse error
        raise AuditError(f"build_manifest.json unreadable: {exc}") from exc
    for key, want in (
        ("pinned_commit", host.commit),
        ("pinned_source_sha256", host.source_sha256),
        ("cmake_target", "package_interface"),
        ("executable_sha256", exe_sha),
    ):
        if manifest.get(key) != want:
            raise AuditError(f"build_manifest.json {key}={manifest.get(key)!r} != approved {want!r}")
    pre = host.verify_worktree()
    if pre.get("commit") != host.commit:
        raise AuditError("checkout HEAD does not match the authorized commit")
    if pre.get("source_sha256") != host.source_sha256:
        raise AuditError("audit source SHA-256 does not match the authorized source_sha256")
    for case in auth["cases"]:
        for rel, key in (
            (f"chemical_mechanisms/{case['mechanism']}", "mechanism_sha256"),
            (f"thermophysical_data/{case['thermo']}", "thermo_sha256"),
            (f"thermophysical_data/{case['transport']}", "transport_sha256"),
        ):
            if host.commit_blob_sha256(rel) != case[key]:
                raise AuditError(f"case {case['id']}: approved-commit blob hash mismatch for {rel}")
    if host.run_root.exists() and any(host.run_root.iterdir()):
        raise AuditError(f"audit run root already exists and is non-empty: {host.run_root}")
    planned = []
    for case in auth["cases"]:
        planned.append(
            {
                "id": case["id"],
                "mode": case["mode"],
                "mechanism": case["mechanism"],
                "mechanism_sha256": case["mechanism_sha256"],
                "thermo": case["thermo"],
                "thermo_sha256": case["thermo_sha256"],
                "transport": case["transport"],
                "transport_sha256": case["transport_sha256"],
                "case_dir": str(host.run_root / "scratch" / case["id"]),
                "output_prefix": case["id"].lower(),
                "expected_outputs": list(MODE_OUTPUTS[case["mode"]]),
            }
        )
    return {
        "schema": SCHEMA,
        "can_run": True,
        "commit": host.commit,
        "source_sha256": host.source_sha256,
        "build_root": str(host.build_root),
        "executable_path": str(exe),
        "executable_sha256": exe_sha,
        "build_manifest_sha256": host.sha256_file(manifest_path),
        "audit_run_root": str(host.run_root),
        "cases": planned,
        "cases_sha256": auth["cases_sha256"],
        "fixtures_enabled": auth["fixtures_enabled"],
        "fixture_ids": sorted(FIXTURES) if auth["fixtures_enabled"] else [],
        "fixtures_sha256": auth["fixtures_sha256"],
        "timeout_seconds": auth["timeout_seconds"],
        "total_timeout_seconds": auth["total_timeout_seconds"],
        "unexpected_output_policy": _output_policy(),
        "argv_preview": [str(exe), "<case>/equilibrium_validation.nml"],
    }


# --------------------------------------------------------------------------- #
# execution
# --------------------------------------------------------------------------- #
def execute(
    host: AuditHost,
    auth: dict[str, Any],
    expected: dict[str, Any],
    *,
    continue_on_failure: bool = False,
) -> dict[str, Any]:
    """Execute every authorized case; fail closed; never retry."""
    for key in ("executable_sha256", "commit", "source_sha256", "cases_sha256"):
        if not expected.get(key):
            raise AuditError(f"plan->execute binding requires --expected-{key.replace('_', '-')}")
    fresh = plan(host, auth)
    for key in ("executable_sha256", "commit", "source_sha256", "cases_sha256"):
        if fresh[key] != expected[key]:
            raise AuditError(f"{key} changed since plan; refusing to execute")
    if host.run_root.exists():
        if any(host.run_root.iterdir()):
            raise AuditError(f"audit run root already exists and is non-empty: {host.run_root}")
    else:
        host.run_root.mkdir(parents=True, exist_ok=False)
    staging = host.run_root / "scratch" / "input"
    staged = host.stage_task_setup(staging)
    started = time.monotonic()
    results: list[dict[str, Any]] = []
    stopped = False
    for case, planned_case in zip(auth["cases"], fresh["cases"]):
        if stopped:
            results.append({"id": case["id"], "status": STATUS_BLOCKED,
                            "reason": "not run: previous case did not complete"})
            continue
        budget = _budget(auth, started)
        if budget <= 0:
            results.append({"id": case["id"], "status": STATUS_BLOCKED,
                            "reason": "total wall-time budget exhausted"})
            stopped = True
            continue
        result = _run_one(host, auth, case, planned_case, staging,
                          is_fixture=False, timeout_seconds=budget)
        results.append(result)
        if result["status"] not in STATUS_OK and not continue_on_failure:
            stopped = True
    if auth["fixtures_enabled"] and not stopped:
        runs = 0
        for name in sorted(FIXTURES):
            if runs >= MAX_FIXTURE_RUNS:
                break
            if time.monotonic() - started > auth["total_timeout_seconds"]:
                results.append({"id": name, "status": STATUS_BLOCKED,
                                "reason": "total wall-time budget exhausted"})
                stopped = True
                break
            runs += 1
            budget = _budget(auth, started)
            if budget <= 0:
                results.append({"id": name, "status": STATUS_BLOCKED,
                                "reason": "total wall-time budget exhausted"})
                stopped = True
                break
            result = _run_one(host, auth, _fixture_case(name), None, staging,
                              is_fixture=True, timeout_seconds=budget)
            results.append(result)
            if result["status"] not in STATUS_OK and not continue_on_failure:
                stopped = True
                break
    expected_total = len(auth["cases"]) + (len(FIXTURES) if auth["fixtures_enabled"] else 0)
    manifest = {
        "schema": SCHEMA,
        "generated_at_utc": host.utc_now(),
        "commit": host.commit,
        "source_sha256": host.source_sha256,
        "executable_sha256": fresh["executable_sha256"],
        "cases_sha256": auth["cases_sha256"],
        "fixtures_sha256": auth["fixtures_sha256"],
        "timeout_seconds": auth["timeout_seconds"],
        "total_timeout_seconds": auth["total_timeout_seconds"],
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "staged_input_manifest": staged.get("staged_input_manifest"),
        "staged_input_manifest_sha256": staged.get("staged_input_manifest_sha256"),
        "unexpected_output_policy": _output_policy(),
        "results": results,
    }
    manifest_path = host.run_root / "output_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {
        "schema": SCHEMA,
        "executed": True,
        "manifest": str(manifest_path),
        "manifest_sha256": host.sha256_file(manifest_path),
        "audit_run_root": str(host.run_root),
        "status_counts": _counts(results),
        "results": results,
        "complete": len(results) == expected_total and all(r["status"] in STATUS_OK for r in results),
    }


def _budget(auth: dict[str, Any], started: float) -> int:
    """Per-launch wall cap: the smaller of the per-case limit and what is left.

    The remaining total budget is **floored**, never rounded up: with 0.9 s left
    the only admissible answer is "no launch" (0), because a 1 s launch would
    exceed the authoritative total wall-time limit.  Sub-second remainders are
    therefore reported as exhausted rather than spent.
    """
    remaining = auth["total_timeout_seconds"] - (time.monotonic() - started)
    if remaining <= 0:
        return 0
    allowed = int(remaining)
    if allowed < 1:
        return 0
    return min(int(auth["timeout_seconds"]), allowed)


def _counts(results: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in results:
        counts[item["status"]] = counts.get(item["status"], 0) + 1
    return counts


def _fixture_case(name: str) -> dict[str, Any]:
    return {
        "id": name,
        "mode": "tp",
        "mechanism": f"hydrogen/L0_FIX_{name}.txt",
        "thermo": "KEROMNES_THERMO.txt",
        "transport": "KEROMNES_TRANSDATA.txt",
        "components": [["H2", 2.0], ["O2", 1.0]],
        "tp_temperature": 1200.0,
        "initial_temperature": 300.0,
        "pressure": 101325.0,
    }


# --------------------------------------------------------------------------- #
# one case
# --------------------------------------------------------------------------- #
def _run_one(
    host: AuditHost,
    auth: dict[str, Any],
    case: dict[str, Any],
    planned_case: dict[str, Any] | None,
    staging: Path,
    *,
    is_fixture: bool,
    timeout_seconds: int,
) -> dict[str, Any]:
    if timeout_seconds <= 0:
        raise AuditError("nonpositive wall-time budget")
    case_dir = host.run_root / "scratch" / case["id"]
    if case_dir.is_symlink() or case_dir.exists():
        raise AuditError(f"case directory already exists (refusing to overwrite): {case_dir}")
    case_dir.mkdir(parents=True, exist_ok=False)
    log_path = case_dir / "run.log"
    result: dict[str, Any] = {
        "id": case["id"],
        "case_dir": str(case_dir),
        "timeout_seconds": timeout_seconds,
    }
    if is_fixture:
        result["fixture_kind"] = FIXTURES[case["id"]]["kind"]
    try:
        shutil.copytree(staging / "package_interface" / "task_setup", case_dir / ALLOWED_CASE_SUBDIR)
        if is_fixture:
            spec = FIXTURES[case["id"]]
            target = case_dir / ALLOWED_CASE_SUBDIR / "chemical_mechanisms" / case["mechanism"]
            target.write_text(spec["text"], encoding="utf-8")
            if host.sha256_file(target) != FIXTURE_SHA256[case["id"]]:
                raise AuditError(f"fixture {case['id']} staged bytes do not match the frozen hash")
        namelist = case_dir / "equilibrium_validation.nml"
        namelist.write_text(render_namelist(case), encoding="utf-8")
        exe = _executable(host)
        if host.sha256_file(exe) != auth["executable_sha256"]:
            raise AuditError("audit executable changed before launch; refusing to execute")
    except AuditError:
        raise
    except Exception as exc:  # noqa: BLE001 - classify, never retry
        return {
            **result,
            "status": STATUS_FAILED,
            "reason": f"{type(exc).__name__}: {exc}",
            **_provenance(host, case_dir, log_path),
        }

    launched = _launch(
        host,
        [str(exe), str(namelist)],
        f"l0 audit {case['id']}",
        log_path,
        case_dir,
        timeout_seconds,
    )
    result.update(
        returncode=launched["returncode"],
        timed_out=launched["timed_out"],
        cancelled=launched["cancelled"],
    )

    if is_fixture:
        spec = FIXTURES[case["id"]]
        if spec["kind"] == "refusal":
            status, matched, header = _refusal_status(spec, launched, log_path)
            provenance = _provenance(host, case_dir, log_path)
            # A refusal is an OK outcome only when its mandatory provenance is
            # also complete: an unhashable or missing log can never certify a
            # grammar probe, even when the diagnostic text is present.
            refusal_downgraded = status == STATUS_REFUSED and not provenance["provenance_complete"]
            if refusal_downgraded:
                status = STATUS_PROVENANCE_INCOMPLETE
            result.update(
                status=status,
                message_matched=matched[0] if matched else None,
                messages_matched=matched,
                log_result=header,
                refusal_evidence_ok=bool(matched),
                reason=("mandatory provenance is missing or unhashable; refusal not certified"
                        if refusal_downgraded
                        else (None if status == STATUS_REFUSED
                              else _failure_reason(launched, status))),
            )
            return {**result, **provenance}
        if launched["status"] != STATUS_COMPLETE:
            result.update(status=launched["status"], reason=launched["reason"],
                          log_result=_log_result(log_path))
            return {**result, **_provenance(host, case_dir, log_path)}
        return _finish(host, result, case_dir, log_path, case["id"].lower(), MODE_OUTPUTS["tp"])

    if launched["status"] != STATUS_COMPLETE:
        result.update(status=launched["status"], reason=launched["reason"],
                      log_result=_log_result(log_path))
        return {**result, **_provenance(host, case_dir, log_path)}
    result["planned_case_dir"] = (planned_case or {}).get("case_dir")
    return _finish(host, result, case_dir, log_path, case["id"].lower(),
                   MODE_OUTPUTS[case["mode"]])


def _failure_reason(launched: dict[str, Any], status: str) -> str | None:
    if status == STATUS_REFUSED:
        return None
    if status == STATUS_TIMEOUT:
        return "child timed out; a timeout is never an expected parser refusal"
    if status == STATUS_CANCELLED:
        return "child cancelled; never an expected parser refusal"
    if status == STATUS_UNEXPECTED_SUCCESS:
        return "expected parser refusal but the child exited 0"
    if status == STATUS_EXIT_UNVERIFIED:
        return "failure without a verifiable nonzero exit status; not accepted as a refusal"
    if status == STATUS_PROVENANCE_MISMATCH:
        return "log outcome header disagrees with the structured child outcome"
    return launched.get("reason")


def _launch(
    host: AuditHost,
    argv: list[str],
    label: str,
    log_path: Path,
    case_dir: Path,
    timeout_seconds: int,
) -> dict[str, Any]:
    """Run one fixed argv and convert the host outcome into structured fields.

    Timeout detection is structural (``timed_out`` attribute or
    ``subprocess.TimeoutExpired``), never textual: a log that merely contains the
    word "timed out" cannot downgrade or upgrade the classification.
    """
    outcome: dict[str, Any] = {
        "status": STATUS_COMPLETE,
        "returncode": None,
        "timed_out": False,
        "cancelled": False,
        "reason": None,
        "error": None,
    }
    try:
        host.run_bounded(argv, label, timeout_seconds, log_path, cwd=case_dir)
    except Exception as exc:  # noqa: BLE001 - classify, never retry
        timed_out = bool(getattr(exc, "timed_out", False)) or isinstance(exc, subprocess.TimeoutExpired)
        cancelled = bool(getattr(exc, "cancelled", False))
        code = getattr(exc, "returncode", None)
        if isinstance(code, bool) or not isinstance(code, int):
            code = None
        outcome.update(
            returncode=code,
            timed_out=timed_out,
            cancelled=cancelled,
            reason=str(exc),
            error=type(exc).__name__,
        )
        if timed_out:
            outcome["status"] = STATUS_TIMEOUT
        elif cancelled:
            outcome["status"] = STATUS_CANCELLED
        else:
            outcome["status"] = STATUS_FAILED
    return outcome


def _log_result(log_path: Path) -> str | None:
    """Read the trusted runner's ``# result:`` header (None if absent)."""
    if not log_path.is_file():
        return None
    try:
        head = log_path.read_text(encoding="utf-8", errors="replace")[:8192]
    except OSError:
        return None
    # _run_bounded always puts the result on the third line.  Never accept a
    # lookalike marker embedded later in untrusted child stdout/stderr.
    lines = head.splitlines()
    found = _LOG_RESULT_RE.fullmatch(lines[2]) if len(lines) >= 3 else None
    return found.group(1) if found else None


def _refusal_status(
    spec: dict[str, Any],
    launched: dict[str, Any],
    log_path: Path,
) -> tuple[str, list[str], str | None]:
    """Accept an expected parser refusal only on a genuine nonzero parser exit.

    Requirements, all mandatory:

    1. the child did not time out and was not cancelled -- a timeout is never a
       refusal, even when its log contains the matching diagnostic;
    2. a structured nonzero exit status is present (no exit status means no
       proof of a parser rejection);
    3. if the trusted log carries a ``# result:`` header, it corroborates
       ``EXIT_<returncode>`` exactly;
    4. one of the frozen diagnostics appears in the log.
    """
    header = _log_result(log_path)
    if launched["timed_out"] or header == "TIMEOUT":
        return STATUS_TIMEOUT, [], header
    if launched["cancelled"] or (header or "").startswith("CANCELLED"):
        return STATUS_CANCELLED, [], header
    if header is None:
        return STATUS_PROVENANCE_MISMATCH, [], header
    if launched["status"] == STATUS_COMPLETE:
        return STATUS_UNEXPECTED_SUCCESS, [], header
    code = launched["returncode"]
    if code is None or code == 0:
        return STATUS_EXIT_UNVERIFIED, [], header
    if header is not None and header != f"EXIT_{code}":
        return STATUS_PROVENANCE_MISMATCH, [], header
    text = ""
    if log_path.is_file():
        text = log_path.read_text(encoding="utf-8", errors="replace")
    matched = [message for message in spec["expect"] if message in text]
    if not matched:
        return STATUS_REFUSED_NO_MESSAGE, [], header
    return STATUS_REFUSED, matched, header


def _finish(
    host: AuditHost,
    result: dict[str, Any],
    case_dir: Path,
    log_path: Path,
    prefix: str,
    expected: tuple[str, ...],
) -> dict[str, Any]:
    """Classify a completed child; ``COMPLETE`` requires a clean, provable case.

    Decision order (first failure wins):

    1. ``UNSAFE_OUTPUT``         -- a symlink entry, or an expected output that is
                                    not a regular file;
    2. ``UNEXPECTED_OUTPUT``     -- any entry outside the expected outputs, the
                                    allowed extra files and the allowed subdirectory;
    3. ``MISSING_OUTPUT``        -- an expected output is absent, empty or a symlink;
    4. ``PROVENANCE_INCOMPLETE`` -- mandatory provenance (trusted runner log) is
                                    missing or unhashable.
    """
    try:
        checks = _collect_outputs(host, case_dir, prefix, expected)
    except Exception as exc:  # noqa: BLE001 - never a COMPLETE on a failed enumeration
        return {
            **result,
            "status": STATUS_PROVENANCE_INCOMPLETE,
            "reason": f"case directory could not be enumerated: {type(exc).__name__}: {exc}",
            **_provenance(host, case_dir, log_path),
        }
    provenance = _provenance(host, case_dir, log_path)
    payload = {
        **result,
        "outputs": checks["outputs"],
        "output_count": len(checks["outputs"]),
        "unexpected": checks["unexpected"],
        "unexpected_hashes": checks["unexpected_hashes"],
        "unexpected_dirs": checks["unexpected_dirs"],
        "unexpected_truncated": checks["unexpected_truncated"],
        "unsafe": checks["unsafe"],
        "missing": checks["missing"],
        "hash_errors": checks["hash_errors"],
        "total_bytes": checks["total_bytes"],
        **provenance,
    }
    if checks["unsafe"]:
        return {**payload, "status": STATUS_UNSAFE_OUTPUT,
                "reason": "case directory contains a symlink or a non-regular expected output"}
    if checks["unexpected"] or checks["unexpected_dirs"]:
        return {**payload, "status": STATUS_UNEXPECTED_OUTPUT,
                "reason": "case directory contains entries outside the expected output, "
                          "allowed-extra and allowed-subdirectory sets"}
    if checks["missing"]:
        return {**payload, "status": STATUS_MISSING_OUTPUT,
                "reason": "missing, empty or non-regular expected outputs: "
                          + ", ".join(checks["missing"])}
    if checks["hash_errors"] or not provenance["provenance_complete"]:
        return {**payload, "status": STATUS_PROVENANCE_INCOMPLETE,
                "reason": "mandatory provenance is missing or unhashable"}
    if _log_result(log_path) != "EXIT_0":
        return {**payload, "status": STATUS_PROVENANCE_MISMATCH,
                "reason": "trusted runner log lacks a corroborating EXIT_0 header"}
    return {**payload, "status": STATUS_COMPLETE}


def _provenance(host: AuditHost, case_dir: Path, log_path: Path) -> dict[str, Any]:
    """Log and failure-artifact hashes, plus an explicit completeness verdict.

    ``provenance_complete`` is False when the trusted runner log is missing or
    cannot be hashed.  A case with incomplete mandatory provenance is never
    reported COMPLETE by ``_finish``.
    """
    out: dict[str, Any] = {
        "log": None,
        "scratch": [],
        "provenance_error": None,
        "provenance_complete": False,
    }
    try:
        if not log_path.is_file() or log_path.is_symlink():
            raise AuditError(f"mandatory trusted runner log missing: {log_path.name}")
        out["log"] = {
            "name": log_path.name,
            "sha256": host.sha256_file(log_path),
            "bytes": log_path.stat().st_size,
        }
        scratch = []
        for name in sorted(ALLOWED_EXTRA_FILES - {"equilibrium_validation.nml", "run.log"}):
            path = case_dir / name
            if path.is_symlink():
                continue
            if path.is_file():
                scratch.append(
                    {
                        "name": name,
                        "sha256": host.sha256_file(path),
                        "bytes": path.stat().st_size,
                    }
                )
        out["scratch"] = scratch
        if not _HASH_RE.match(str(out["log"]["sha256"])):
            raise AuditError("log hash is not a 64-hex SHA-256")
        out["provenance_complete"] = True
    except Exception as exc:  # noqa: BLE001 - recorded, never hidden
        out["provenance_error"] = f"{type(exc).__name__}: {exc}"
        out["provenance_complete"] = False
    return out


def _collect_outputs(
    host: AuditHost,
    case_dir: Path,
    prefix: str,
    expected: tuple[str, ...],
) -> dict[str, Any]:
    """Enumerate the case directory without following symlinks.

    Expected outputs must be regular, non-symlink files.  The only tolerated
    directory is ``task_setup``.  Every other entry is recorded (with a hash when
    it is a regular file) so the manifest can explain exactly why a case is not
    COMPLETE.
    """
    outputs: dict[str, str] = {}
    missing: list[str] = []
    hash_errors: list[dict[str, str]] = []
    unsafe: list[dict[str, str]] = []
    total = 0
    for suffix in expected:
        name = f"{prefix}{suffix}"
        path = case_dir / name
        if path.is_symlink():
            unsafe.append({"name": name, "kind": "symlink", "expected_output": "true"})
            missing.append(name)
            continue
        if not path.is_file():
            missing.append(name)
            continue
        if path.stat().st_size <= 0:
            missing.append(name)
            continue
        try:
            outputs[name] = host.sha256_file(path)
        except Exception as exc:  # noqa: BLE001 - unhashable output is not evidence
            hash_errors.append({"name": name, "error": f"{type(exc).__name__}: {exc}"})
            continue
        total += path.stat().st_size

    unexpected: list[str] = []
    unexpected_dirs: list[str] = []
    for entry in sorted(case_dir.iterdir(), key=lambda p: p.name):
        name = entry.name
        if entry.is_symlink():
            unsafe.append({"name": name, "kind": "symlink", "expected_output": "false"})
            continue
        if entry.is_dir():
            if name == ALLOWED_CASE_SUBDIR:
                continue
            unexpected_dirs.append(name)
            continue
        if not entry.is_file():
            unsafe.append({"name": name, "kind": "special", "expected_output": "false"})
            continue
        if name in {f"{prefix}{suffix}" for suffix in expected} or name in ALLOWED_EXTRA_FILES:
            continue
        unexpected.append(name)

    truncated = len(unexpected) > MAX_ARTIFACTS
    hashes = []
    for name in unexpected[:MAX_ARTIFACTS]:
        path = case_dir / name
        try:
            hashes.append({"name": name, "sha256": host.sha256_file(path),
                           "bytes": path.stat().st_size})
        except Exception as exc:  # noqa: BLE001 - record, do not fabricate a hash
            hashes.append({"name": name, "sha256": None,
                           "error": f"{type(exc).__name__}: {exc}"})
    return {
        "outputs": outputs,
        "missing": missing,
        "unexpected": unexpected,
        "unexpected_hashes": hashes,
        "unexpected_dirs": unexpected_dirs,
        "unexpected_truncated": truncated,
        "unsafe": unsafe,
        "hash_errors": hash_errors,
        "total_bytes": total,
    }
