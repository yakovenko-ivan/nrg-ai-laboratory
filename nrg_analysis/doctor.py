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


MIN_NODE_MAJOR = 20
NRG_CHECK_NAMES = {
    "task-setup-template",
    "computing-module",
    "package-interface-0d",
    "laboratory-runtime-validation",
}


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


def collect_diagnostics(
    laboratory: str | Path | None = None,
    *,
    local: str | Path | None = None,
    use_local: bool = True,
    require_nrg: bool = False,
) -> dict[str, Any]:
    """Collect repository and external-NRG readiness without changing state.

    NRG is intentionally external to this repository.  Unless ``require_nrg``
    is true, missing NRG source/resources/executables are reported as optional
    integration warnings rather than repository failures.
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

        nrg_path_checks = (
            ("task-setup-template", lab.task_setup_template, "dir"),
            ("computing-module", lab.computing_module, "file"),
            ("package-interface-0d", lab.package_interface_0d, "file"),
        )
        for label, path, kind in nrg_path_checks:
            exists = path.is_dir() if kind == "dir" else path.is_file()
            detail: dict[str, Any] = {"path": str(path), "owner": "external NRG project"}
            if kind == "file" and exists:
                detail.update(
                    {
                        "executable": bool(os.access(path, os.X_OK)) if os.name != "nt" else True,
                        "file_kind": _file_kind(path),
                    }
                )
                exists = exists and detail["executable"]
            checks.append(_check(label, exists, required=require_nrg, detail=detail))

        try:
            lab.validate(require_runtime=require_nrg)
            checks.append(_check("laboratory-base-validation", True))
        except Exception as exc:
            checks.append(
                _check(
                    "laboratory-base-validation",
                    False,
                    detail=f"{type(exc).__name__}: {exc}",
                )
            )

        try:
            lab.validate(require_runtime=True)
            runtime_ok = True
            runtime_detail = None
        except Exception as exc:
            runtime_ok = False
            runtime_detail = f"{type(exc).__name__}: {exc}"
        checks.append(
            _check(
                "laboratory-runtime-validation",
                runtime_ok,
                required=require_nrg,
                detail=runtime_detail,
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
    nrg_checks = [item for item in checks if item["name"] in NRG_CHECK_NAMES]
    nrg_runtime_ready = bool(nrg_checks) and all(item["ok"] for item in nrg_checks)

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
        "summary": {
            "required_failures": len(required_failures),
            "optional_warnings": len(optional_failures),
            "repository_ready": not required_failures,
            "nrg_runtime_ready": nrg_runtime_ready,
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
    if not summary["repository_ready"]:
        return 2
    if args.require_nrg and not summary["nrg_runtime_ready"]:
        return 2
    if args.strict and not summary["full_environment_ready"]:
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
