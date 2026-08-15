"""Portable laboratory configuration for NRG research workflows.

The committed ``config/laboratory.toml`` describes repository-relative defaults.
Optional machine-local overrides live in ``config/laboratory.local.toml`` (or in a
file selected with ``NRG_LABORATORY_LOCAL_CONFIG``).  Scientific campaign files
remain experiment definitions; filesystem locations and trusted executable
locations are resolved by this module.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Any, Mapping

try:
    import tomllib
except ModuleNotFoundError as exc:  # pragma: no cover - Python >= 3.11 is required
    raise RuntimeError("Python 3.11+ is required (tomllib unavailable)") from exc


DEFAULT_CONFIG_ENV = "NRG_LABORATORY_CONFIG"
LOCAL_CONFIG_ENV = "NRG_LABORATORY_LOCAL_CONFIG"
LOCAL_CONFIG_NAME = "laboratory.local.toml"


def _expand(path: str | Path) -> Path:
    return Path(os.path.expandvars(str(path))).expanduser()


def _resolve_under(value: str | Path, base: Path) -> Path:
    path = _expand(value)
    if not path.is_absolute():
        path = base / path
    return path.resolve()


def _read_toml(path: Path) -> dict[str, Any]:
    with path.open("rb") as stream:
        data = tomllib.load(stream)
    if not isinstance(data, dict):  # defensive; tomllib returns dict for valid TOML
        raise ValueError(f"laboratory configuration must contain TOML tables: {path}")
    return data


def _merge_tables(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    """Recursively merge TOML tables, with ``override`` taking precedence."""

    merged: dict[str, Any] = dict(base)
    for key, value in override.items():
        current = merged.get(key)
        if isinstance(current, Mapping) and isinstance(value, Mapping):
            merged[key] = _merge_tables(current, value)
        else:
            merged[key] = value
    return merged


@dataclass(frozen=True)
class Laboratory:
    """Resolved laboratory paths and trusted runtime executables."""

    # ``config_path`` remains the committed/base configuration for backwards
    # compatibility with v0.5.x provenance fields and command-line forwarding.
    config_path: Path
    local_config_path: Path | None
    research_root: Path
    campaign_root: Path
    runs_root: Path
    studies_root: Path
    task_setup_template: Path
    computing_module: Path
    package_interfaces: Mapping[str, Path]
    runner_config: Path
    termination_profiles: Path
    default_threads: int = 1

    @classmethod
    def default_config_path(cls) -> Path:
        env = os.environ.get(DEFAULT_CONFIG_ENV)
        if env:
            return _expand(env).resolve()
        # Checkout/editable-install layout: <repository>/nrg_analysis/laboratory.py
        package_candidate = Path(__file__).resolve().parent.parent / "config" / "laboratory.toml"
        if package_candidate.exists():
            return package_candidate.resolve()
        # Compatibility fallback for unusual invocation layouts.
        cwd_candidate = Path.cwd() / "config" / "laboratory.toml"
        return cwd_candidate.resolve()

    @classmethod
    def default_local_config_path(cls, base_config_path: Path) -> Path | None:
        env = os.environ.get(LOCAL_CONFIG_ENV)
        if env:
            return _expand(env).resolve()
        # If the caller deliberately selected the local file as the complete
        # configuration, do not attempt to overlay it onto itself.
        if base_config_path.name == LOCAL_CONFIG_NAME:
            return None
        candidate = base_config_path.with_name(LOCAL_CONFIG_NAME)
        return candidate.resolve() if candidate.is_file() else None

    @classmethod
    def load(
        cls,
        path: str | Path | None = None,
        *,
        local_path: str | Path | None = None,
        use_local: bool = True,
        validate: bool = True,
        require_runtime: bool = True,
    ) -> "Laboratory":
        """Load the portable base config and optional machine-local override.

        Configuration precedence is:

        1. explicit ``path`` argument;
        2. ``NRG_LABORATORY_CONFIG``;
        3. repository ``config/laboratory.toml``.

        When ``use_local`` is true, a second layer is applied on top:

        1. explicit ``local_path`` argument;
        2. ``NRG_LABORATORY_LOCAL_CONFIG``;
        3. sibling ``config/laboratory.local.toml`` if present.

        The local file may contain only the keys it overrides.  Unspecified keys
        inherit from the committed base configuration.
        """

        config_path = _expand(path).resolve() if path is not None else cls.default_config_path()
        if not config_path.is_file():
            raise FileNotFoundError(
                f"laboratory configuration not found: {config_path}. "
                f"Pass --laboratory or set {DEFAULT_CONFIG_ENV}."
            )

        base_data = _read_toml(config_path)

        resolved_local_path: Path | None = None
        local_data: dict[str, Any] = {}
        if use_local:
            if local_path is not None:
                resolved_local_path = _expand(local_path).resolve()
            else:
                resolved_local_path = cls.default_local_config_path(config_path)
            if resolved_local_path is not None:
                if not resolved_local_path.is_file():
                    raise FileNotFoundError(
                        f"laboratory local configuration not found: {resolved_local_path}. "
                        f"Unset {LOCAL_CONFIG_ENV} or provide a valid override file."
                    )
                local_data = _read_toml(resolved_local_path)

        data = _merge_tables(base_data, local_data)

        paths = data.get("paths")
        runtime = data.get("runtime")
        execution = data.get("execution", {})
        if not isinstance(paths, dict):
            raise ValueError("laboratory.toml must contain [paths]")
        if not isinstance(runtime, dict):
            raise ValueError("laboratory.toml must contain [runtime]")

        if "research_root" not in paths:
            raise ValueError("laboratory.toml [paths] requires research_root")

        # The portable base file is normally config/laboratory.toml with
        # research_root = "..".  If a local override explicitly replaces
        # research_root, resolve that value relative to the local file so an
        # externally stored local configuration remains intuitive.
        research_root_base = config_path.parent
        local_paths = local_data.get("paths") if isinstance(local_data, dict) else None
        if (
            resolved_local_path is not None
            and isinstance(local_paths, dict)
            and "research_root" in local_paths
        ):
            research_root_base = resolved_local_path.parent
        research_root = _resolve_under(paths["research_root"], research_root_base)

        required_paths = ("campaign_root", "runs_root", "studies_root", "task_setup_template")
        missing_paths = [key for key in required_paths if key not in paths]
        if missing_paths:
            raise ValueError(f"laboratory.toml [paths] missing: {', '.join(missing_paths)}")

        campaign_root = _resolve_under(paths["campaign_root"], research_root)
        runs_root = _resolve_under(paths["runs_root"], research_root)
        studies_root = _resolve_under(paths["studies_root"], research_root)
        task_setup_template = _resolve_under(paths["task_setup_template"], research_root)

        if "computing_module" not in runtime:
            raise ValueError("laboratory.toml [runtime] requires computing_module")
        computing_module = _resolve_under(runtime["computing_module"], research_root)

        package_interfaces: dict[str, Path] = {}
        for key, value in runtime.items():
            if key.startswith("package_interface_"):
                package_interfaces[key.removeprefix("package_interface_")] = _resolve_under(
                    value, research_root
                )
        if "0d" not in package_interfaces:
            raise ValueError("laboratory.toml [runtime] requires package_interface_0d")

        default_threads = int(execution.get("default_threads", 1))
        if default_threads <= 0:
            raise ValueError("execution.default_threads must be positive")

        # Runner policy is trusted laboratory infrastructure, not an
        # agent-authored campaign input.
        if "runner_config" in execution:
            runner_config = _resolve_under(execution["runner_config"], research_root)
        else:
            preferred_runner_config = research_root / "config" / "campaign_runner.json"
            legacy_runner_config = (
                research_root / "campaign_tools" / "examples" / "run_config_0d_mechanisms.json"
            )
            # New installations use config/campaign_runner.json.  The legacy
            # path is retained only so older v0.3.x installations can be
            # inspected before the migration file is copied.
            runner_config = (
                preferred_runner_config.resolve()
                if preferred_runner_config.is_file()
                else legacy_runner_config.resolve()
            )

        if "termination_profiles" in execution:
            termination_profiles = _resolve_under(execution["termination_profiles"], research_root)
        else:
            termination_profiles = (research_root / "config" / "termination_profiles.json").resolve()

        lab = cls(
            config_path=config_path,
            local_config_path=resolved_local_path,
            research_root=research_root,
            campaign_root=campaign_root,
            runs_root=runs_root,
            studies_root=studies_root,
            task_setup_template=task_setup_template,
            computing_module=computing_module,
            package_interfaces=package_interfaces,
            runner_config=runner_config,
            termination_profiles=termination_profiles,
            default_threads=default_threads,
        )
        if validate:
            lab.validate(require_runtime=require_runtime)
        return lab

    @property
    def package_interface_0d(self) -> Path:
        return self.package_interfaces["0d"]

    @property
    def package_interface_workdir(self) -> Path:
        """Working directory required by the current NRG package interface.

        ``global_data.f90`` expects ``./task_setup``.  Therefore the interface is
        launched from the parent of the configured template directory.
        """

        return self.task_setup_template.parent

    @property
    def runtime_root(self) -> Path:
        # Common external NRG CMake layout: <NRG build>/bin/computing_module.
        if self.computing_module.parent.name == "bin":
            return self.computing_module.parent.parent.resolve()
        return self.computing_module.parent.resolve()

    @property
    def runtime_manifest(self) -> Path:
        return self.runtime_root / "runtime_manifest.json"

    @property
    def config_sources(self) -> tuple[Path, ...]:
        if self.local_config_path is None:
            return (self.config_path,)
        return (self.config_path, self.local_config_path)

    def validate(self, *, require_runtime: bool = True) -> None:
        if not self.research_root.is_dir():
            raise FileNotFoundError(f"research_root does not exist: {self.research_root}")

        # Output/workspace roots are allowed to be absent before first use.
        for name, path in (
            ("campaign_root", self.campaign_root),
            ("runs_root", self.runs_root),
            ("studies_root", self.studies_root),
        ):
            if path.exists() and not path.is_dir():
                raise NotADirectoryError(f"{name} is not a directory: {path}")

        if require_runtime:
            # NRG itself is an external dependency of this repository.  The
            # task_setup tree and executables are validated only when an
            # execution-ready NRG installation is required.
            if self.task_setup_template.name != "task_setup":
                raise ValueError(
                    "task_setup_template must point to a directory named 'task_setup' "
                    "because the current package interface expects ./task_setup"
                )
            if not self.task_setup_template.is_dir():
                raise FileNotFoundError(
                    f"task_setup_template not found: {self.task_setup_template}"
                )
            self._validate_executable("computing_module", self.computing_module)
            for name, path in self.package_interfaces.items():
                self._validate_executable(f"package_interface_{name}", path)

    @staticmethod
    def _validate_executable(name: str, path: Path) -> None:
        if not path.is_file():
            raise FileNotFoundError(f"{name} not found: {path}")
        if os.name != "nt" and not os.access(path, os.X_OK):
            raise PermissionError(f"{name} is not executable: {path}")

    def ensure_output_roots(self) -> None:
        """Create only laboratory-owned output roots, never runtime resources."""

        for path in (self.campaign_root, self.runs_root, self.studies_root):
            path.mkdir(parents=True, exist_ok=True)

    def to_dict(self) -> dict[str, Any]:
        return {
            "config_path": str(self.config_path),
            "local_config_path": (
                str(self.local_config_path) if self.local_config_path is not None else None
            ),
            "config_sources": [str(path) for path in self.config_sources],
            "research_root": str(self.research_root),
            "campaign_root": str(self.campaign_root),
            "runs_root": str(self.runs_root),
            "studies_root": str(self.studies_root),
            "task_setup_template": str(self.task_setup_template),
            "package_interface_workdir": str(self.package_interface_workdir),
            "computing_module": str(self.computing_module),
            "package_interfaces": {key: str(value) for key, value in self.package_interfaces.items()},
            "runner_config": str(self.runner_config),
            "termination_profiles": str(self.termination_profiles),
            "default_threads": self.default_threads,
        }
