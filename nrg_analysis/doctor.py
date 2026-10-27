"""Read-only environment diagnostics for a cloned NRG AI Laboratory repository."""

from __future__ import annotations

import argparse
import importlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
from typing import Any

from ._version import __version__
from .laboratory import Laboratory
from .provenance import sha256_file


MIN_NODE_MAJOR = 20
# Checks owned by the external NRG project.  ``--require-nrg`` promotes them to
# required severity for the CLI gate, but repository readiness is evaluated
# independently of them.
NRG_CHECK_NAMES = {
    "task-setup-template",
    "computing-module",
    "laboratory-runtime-validation",
}
NRG_CHECK_NAME_PREFIX = "package-interface-"


def _is_nrg_owned_check(name: str) -> bool:
    return name in NRG_CHECK_NAMES or name.startswith(NRG_CHECK_NAME_PREFIX)


def _check(name: str, ok: bool, *, required: bool = True, detail: Any = None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "name": name,
        "ok": bool(ok),
        "severity": "required" if required else "optional",
    }
    if detail is not None:
        result["detail"] = detail
    return result


def _command_version(command: str, args: list[str] | None = None) -> tuple[str | None, str | None]:
    path = shutil.which(command)
    if path is None:
        return None, None
    try:
        completed = subprocess.run(
            [path, *(args or ["--version"])],
            text=True,
            capture_output=True,
            timeout=5,
            check=False,
        )
        text = (completed.stdout or completed.stderr).strip().splitlines()
        return path, text[0] if text else f"exit={completed.returncode}"
    except Exception as exc:  # diagnostic tool must keep reporting other checks
        return path, f"{type(exc).__name__}: {exc}"


def _node_major(version_text: str | None) -> int | None:
    if not version_text:
        return None
    match = re.search(r"v?(\d+)", version_text)
    return int(match.group(1)) if match else None


def _file_kind(path: Path) -> str:
    try:
        head = path.read_bytes()[:4]
    except OSError:
        return "unreadable"
    if head == b"\x7fELF":
        return "ELF"
    if head[:2] == b"MZ":
        return "PE"
    if head.startswith(b"#!"):
        return "script"
    return "unknown"


def _component(path: Path) -> dict[str, Any]:
    """Component diagnostic using the P1A/P1B-FU readiness semantics.

    ``sha256`` is a real digest whenever the file exists and can be read, even
    if it is not executable; it is ``None`` (with ``read_error``) only when the
    file is missing or unreadable.  Executable readiness requires ``executable``
    and ``readable`` together.
    """

    exists = path.is_file()
    executable = bool(exists and (os.name == "nt" or os.access(path, os.X_OK)))
    digest: str | None = None
    read_error: str | None = None
    if exists:
        try:
            digest = sha256_file(path)
        except OSError as exc:
            read_error = f"{type(exc).__name__}: {exc}"
    return {
        "path": str(path),
        "exists": bool(exists),
        "executable": executable,
        "readable": digest is not None,
        "sha256": digest,
        "read_error": read_error,
    }


def _component_ready(component: dict[str, Any]) -> bool:
    return bool(component["executable"] and component["readable"])


def _runtime_readiness(lab: Laboratory) -> dict[str, Any]:
    """Structured runtime readiness for the configured runtime.

    ``runtime_available`` means at least one usable capability exists (execution
    or preparation); it does **not** imply that a CFD job can be launched.
    ``nrg_runtime_ready`` is the strict complete-runtime diagnostic: execution
    ready, a valid task-setup directory, and every configured package interface
    ready.  Hashes describe files present at diagnostic time and need not
    describe the runtime that produced archived scientific results.
    """

    computing_module = _component(lab.computing_module)
    task_setup = {
        "path": str(lab.task_setup_template),
        "exists": lab.task_setup_template.is_dir(),
        "named_task_setup": lab.task_setup_template.name == "task_setup",
    }
    task_setup_ready = bool(task_setup["exists"] and task_setup["named_task_setup"])
    package_interfaces = {
        name: _component(path)
        for name, path in sorted(lab.package_interfaces.items())
    }
    execution_ready = _component_ready(computing_module)
    preparation_ready = {
        name: bool(task_setup_ready and _component_ready(component))
        for name, component in package_interfaces.items()
    }
    preparation_ready.setdefault("0d", False)
    preparation_ready.setdefault("1d", False)

    unavailable: list[str] = []
    unreadable: list[str] = []
    if not execution_ready:
        unavailable.append("computing_module")
    if (
        computing_module["exists"]
        and computing_module["executable"]
        and not computing_module["readable"]
    ):
        unreadable.append("computing_module")
    if not task_setup["exists"]:
        unavailable.append("task_setup")
    elif not task_setup["named_task_setup"]:
        unavailable.append("task_setup_name")
    for name, component in package_interfaces.items():
        if not _component_ready(component):
            unavailable.append(f"package_interface_{name}")
        if (
            component["exists"]
            and component["executable"]
            and not component["readable"]
        ):
            unreadable.append(f"package_interface_{name}")

    complete_runtime_ready = bool(
        execution_ready
        and task_setup_ready
        and package_interfaces
        and all(
            _component_ready(component) for component in package_interfaces.values()
        )
    )
    return {
        "computing_module": computing_module,
        "task_setup": task_setup,
        "package_interfaces": package_interfaces,
        "execution_ready": execution_ready,
        "preparation_ready": preparation_ready,
        "preparation_ready_0d": preparation_ready["0d"],
        "preparation_ready_1d": preparation_ready["1d"],
        "runtime_available": bool(execution_ready or any(preparation_ready.values())),
        "nrg_runtime_ready": complete_runtime_ready,
        "unavailable": unavailable,
        "unreadable": unreadable,
        "note": (
            "Diagnostic-time runtime files. runtime_available means at least one "
            "usable capability (execution or preparation) and does not imply that "
            "a CFD job can be launched; nrg_runtime_ready is strict complete-runtime "
            "readiness. Readiness requires executable and readable, not merely a "
            "hash; these hashes need not describe the runtime that produced "
            "archived scientific results."
        ),
    }


def collect_diagnostics(
    laboratory: str | Path | None = None,
    *,
    local: str | Path | None = None,
    use_local: bool = True,
    require_nrg: bool = False,
) -> dict[str, Any]:
    """Collect repository and external-NRG readiness without changing state.

    NRG is intentionally external to this repository.  Missing NRG
    sources/resources/executables are reported as optional integration warnings
    by default and never make the repository itself unhealthy:
    ``summary.repository_ready`` reflects only repository/Python/configuration
    assets.  ``--require-nrg`` promotes NRG checks to required severity for the
    CLI gate but does not change ``repository_ready``.

    Readiness states are reported separately:

    * ``summary.execution_ready`` - configured computing module present,
      executable and hashable;
    * ``summary.preparation_ready`` - per-kind (0d/1d) readiness: a valid
      ``task_setup`` directory plus the configured, executable, hashable
      interface for that kind;
    * ``summary.runtime_available`` - execution readiness **or** at least one
      preparation capability; this does not imply that a CFD job can be launched;
    * ``summary.nrg_runtime_ready`` - legacy strict *complete-runtime* diagnostic
      (execution ready, valid task setup, and every configured interface ready).

    Component-level detail (``path``, ``exists``, ``executable``, ``readable``,
    ``sha256``/``None``, ``read_error``) is available under the top-level
    ``runtime`` object.  A hashable but non-executable file keeps its real digest
    and is still not ready; hashes describe diagnostic-time files and need not
    describe the runtime that produced archived scientific results.
    """

    checks: list[dict[str, Any]] = []

    py_ok = sys.version_info >= (3, 11)
    checks.append(
        _check(
            "python>=3.11",
            py_ok,
            detail={"version": platform.python_version(), "executable": sys.executable},
        )
    )

    for module in ("nrg_analysis", "agent_workspace", "campaign_tools"):
        try:
            imported = importlib.import_module(module)
            checks.append(_check(f"import:{module}", True, detail=getattr(imported, "__file__", None)))
        except Exception as exc:
            checks.append(_check(f"import:{module}", False, detail=f"{type(exc).__name__}: {exc}"))

    lab: Laboratory | None = None
    runtime: dict[str, Any] | None = None
    try:
        lab = Laboratory.load(
            laboratory,
            local_path=local,
            use_local=use_local,
            validate=False,
            require_runtime=False,
        )
        checks.append(
            _check(
                "laboratory-config-load",
                True,
                detail={
                    "base": str(lab.config_path),
                    "local": str(lab.local_config_path) if lab.local_config_path else None,
                },
            )
        )
    except Exception as exc:
        checks.append(_check("laboratory-config-load", False, detail=f"{type(exc).__name__}: {exc}"))

    package_candidate = Path(__file__).resolve().parents[1]
    repository_root: Path | None = (
        package_candidate if (package_candidate / "pyproject.toml").is_file() else None
    )
    if lab is not None:
        # Internal repository resources remain required.  NRG-owned resources
        # and executables are external integration checks by default.
        internal_path_checks = (
            ("research-root", lab.research_root, "dir"),
            ("runner-config", lab.runner_config, "file"),
            ("termination-profiles", lab.termination_profiles, "file"),
        )
        for label, path, kind in internal_path_checks:
            exists = path.is_dir() if kind == "dir" else path.is_file()
            checks.append(_check(label, exists, detail={"path": str(path)}))

        runtime = _runtime_readiness(lab)

        checks.append(
            _check(
                "task-setup-template",
                bool(
                    runtime["task_setup"]["exists"]
                    and runtime["task_setup"]["named_task_setup"]
                ),
                required=require_nrg,
                detail={
                    "path": runtime["task_setup"]["path"],
                    "exists": runtime["task_setup"]["exists"],
                    "named_task_setup": runtime["task_setup"]["named_task_setup"],
                    "owner": "external NRG project",
                },
            )
        )

        computing_module = runtime["computing_module"]
        computing_detail: dict[str, Any] = {
            "path": computing_module["path"],
            "exists": computing_module["exists"],
            "executable": computing_module["executable"],
            "readable": computing_module["readable"],
            "sha256": computing_module["sha256"],
            "owner": "external NRG project",
        }
        if computing_module["read_error"]:
            computing_detail["read_error"] = computing_module["read_error"]
        if computing_module["exists"]:
            computing_detail["file_kind"] = _file_kind(lab.computing_module)
        checks.append(
            _check(
                "computing-module",
                _component_ready(computing_module),
                required=require_nrg,
                detail=computing_detail,
            )
        )

        # Every configured package interface is diagnosed.  A 1D-only laboratory
        # must not assume that a 0D interface exists.
        for name, component in runtime["package_interfaces"].items():
            interface_detail: dict[str, Any] = {
                "path": component["path"],
                "exists": component["exists"],
                "executable": component["executable"],
                "readable": component["readable"],
                "sha256": component["sha256"],
                "owner": "external NRG project",
            }
            if component["read_error"]:
                interface_detail["read_error"] = component["read_error"]
            if component["exists"]:
                interface_detail["file_kind"] = _file_kind(lab.package_interfaces[name])
            checks.append(
                _check(
                    f"package-interface-{name}",
                    _component_ready(component),
                    required=require_nrg,
                    detail=interface_detail,
                )
            )

        # Repository/configuration validation is always evaluated without the
        # external runtime, so --require-nrg cannot turn a healthy repository
        # into a failing one.
        try:
            lab.validate(require_runtime=False)
            checks.append(_check("laboratory-base-validation", True))
        except Exception as exc:
            checks.append(
                _check(
                    "laboratory-base-validation",
                    False,
                    detail=f"{type(exc).__name__}: {exc}",
                )
            )

        # Parser-level runtime validation is reported for auditability; the
        # authoritative strict state is the structured one, which additionally
        # requires hashable components and every configured interface.
        try:
            lab.validate(require_runtime=True)
            validation_error: str | None = None
        except Exception as exc:
            validation_error = f"{type(exc).__name__}: {exc}"
        runtime_validation_detail: dict[str, Any] = {
            "nrg_runtime_ready": runtime["nrg_runtime_ready"],
            "unavailable": list(runtime["unavailable"]),
        }
        if validation_error is not None:
            runtime_validation_detail["laboratory_validation_error"] = validation_error
        checks.append(
            _check(
                "laboratory-runtime-validation",
                runtime["nrg_runtime_ready"],
                required=require_nrg,
                detail=runtime_validation_detail,
            )
        )

        # Output roots may intentionally be absent before first use.  Diagnose
        # their state without creating anything.
        for label, path in (
            ("campaign-workspace", lab.campaign_root),
            ("runs-workspace", lab.runs_root),
            ("studies-workspace", lab.studies_root),
        ):
            parent = path if path.exists() else path.parent
            detail = {
                "path": str(path),
                "exists": path.exists(),
                "parent_writable": bool(os.access(parent, os.W_OK)) if parent.exists() else False,
            }
            checks.append(_check(label, (not path.exists()) or path.is_dir(), required=False, detail=detail))

        if repository_root is not None:
            repo_items = (
                ("repository-pyproject", repository_root / "pyproject.toml", "file"),
                ("pi-extension", repository_root / ".pi" / "extensions" / "nrg-laboratory" / "index.ts", "file"),
                ("pi-skills", repository_root / ".pi" / "skills", "dir"),
            )
            for label, path, kind in repo_items:
                ok = path.is_file() if kind == "file" else path.is_dir()
                checks.append(_check(label, ok, detail=str(path)))
            checks.append(
                _check(
                    "laboratory-research-root-is-repository",
                    lab.research_root == repository_root,
                    required=False,
                    detail={
                        "research_root": str(lab.research_root),
                        "repository_root": str(repository_root),
                    },
                )
            )
            checks.append(
                _check(
                    "no-bundled-nrg-runtime",
                    not (repository_root / "nrg_runtime").exists(),
                    detail="NRG binaries/resources must remain in the upstream NRG project or local ignored workspace",
                )
            )
        else:
            checks.append(
                _check(
                    "repository-checkout-detected",
                    False,
                    required=False,
                    detail="Python package is not running from a source checkout",
                )
            )

    git_path, git_version = _command_version("git")
    checks.append(_check("external:git", git_path is not None, required=False, detail={"path": git_path, "version": git_version}))

    node_path, node_version = _command_version("node")
    node_major = _node_major(node_version)
    checks.append(
        _check(
            f"external:node>={MIN_NODE_MAJOR}",
            node_path is not None and node_major is not None and node_major >= MIN_NODE_MAJOR,
            required=False,
            detail={"path": node_path, "version": node_version},
        )
    )

    pi_path, pi_version = _command_version("pi")
    checks.append(_check("external:pi", pi_path is not None, required=False, detail={"path": pi_path, "version": pi_version}))

    required_failures = [item for item in checks if item["severity"] == "required" and not item["ok"]]
    optional_failures = [item for item in checks if item["severity"] == "optional" and not item["ok"]]
    # Repository readiness never depends on the external NRG project, even when
    # --require-nrg has promoted NRG checks to required severity for the CLI gate.
    repository_failures = [
        item for item in required_failures if not _is_nrg_owned_check(item["name"])
    ]
    repository_ready = not repository_failures
    if runtime is None:
        runtime = {
            "computing_module": None,
            "task_setup": None,
            "package_interfaces": {},
            "execution_ready": False,
            "preparation_ready": {"0d": False, "1d": False},
            "preparation_ready_0d": False,
            "preparation_ready_1d": False,
            "runtime_available": False,
            "nrg_runtime_ready": False,
            "unavailable": ["laboratory_config"],
            "unreadable": [],
            "note": "Laboratory configuration could not be loaded; runtime state is unknown.",
        }

    env = {
        key: os.environ.get(key)
        for key in (
            "NRG_LABORATORY_CONFIG",
            "NRG_LABORATORY_LOCAL_CONFIG",
            "NRG_PYTHON",
            "NRG_READ_ONLY_SHELL_POLICY",
            "NRG_SOURCE_ROOT",
            "NRG_BUILD_ROOT",
            "NRG_COMPUTING_MODULE",
            "NRG_PACKAGE_INTERFACE_0D",
        )
        if os.environ.get(key) is not None
    }

    return {
        "schema_version": 2,
        "project": "NRG AI Laboratory Assistant",
        "project_version": __version__,
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
        },
        "repository_root": str(repository_root) if repository_root is not None else None,
        "nrg_external_dependency": True,
        "environment_overrides": env,
        "checks": checks,
        "runtime": runtime,
        "summary": {
            "required_failures": len(required_failures),
            "optional_warnings": len(optional_failures),
            "repository_failures": len(repository_failures),
            "repository_ready": repository_ready,
            "execution_ready": runtime["execution_ready"],
            "preparation_ready": runtime["preparation_ready"],
            "runtime_available": runtime["runtime_available"],
            "nrg_runtime_ready": runtime["nrg_runtime_ready"],
            "full_environment_ready": not required_failures and not optional_failures,
        },
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--laboratory", help="base laboratory TOML")
    parser.add_argument("--local", help="explicit local override TOML")
    parser.add_argument(
        "--require-nrg",
        action="store_true",
        help="treat missing external NRG task-setup resources/executables as required failures",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="return nonzero for any optional warning, including missing Pi/Node/Git or NRG integration",
    )
    parser.add_argument(
    "--no-local",
    action="store_true",
    help="ignore laboratory.local.toml and inspect only the portable base configuration",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    report = collect_diagnostics(args.laboratory, local=args.local, use_local=not args.no_local, require_nrg=args.require_nrg)
    print(json.dumps(report, indent=2, sort_keys=True))
    summary = report["summary"]
    # Exit-code precedence: repository defects (2) > --require-nrg complete-runtime
    # failure (2) > --strict optional-warning gate (3) > success (0).
    if not summary["repository_ready"]:
        return 2
    if args.require_nrg and not summary["nrg_runtime_ready"]:
        return 2
    if args.strict and not summary["full_environment_ready"]:
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
