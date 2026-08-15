#!/usr/bin/env python3
"""Cross-platform laboratory-aware manifest-driven NRG campaign runner v10.

Key properties
--------------
* cases.csv is immutable experimental design metadata.
* laboratory.toml is authoritative for runs_root and trusted computing_module.
* run_status.json is canonical per-case execution state.
* monitor activation can use physical simulation time from NRG postprocessors.
* monitor/timeout/operator stops first request graceful NRG finalization through
  run_control.stop; process-tree termination is only a configurable fallback.
* trusted operator-control requests can stop one active case or the whole runner
  without launching another case afterward.
* case fingerprints are verified before execution.
* run_summary.csv is rebuilt from all per-case statuses, so skipped/prior runs
  never disappear from the campaign summary.

Python requirement: 3.11+; standard library only.
"""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
import hashlib
from datetime import datetime
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import time
from typing import Any

from nrg_analysis.laboratory import Laboratory
from nrg_analysis.execution_lock import LaboratoryRunnerLock, RunnerAlreadyActive, probe_runner_lock
from campaign_tools.campaign_attempts import effective_field_value, load_override_record
from nrg_analysis.quasistationary import (
    QuasistationaryProfile,
    evaluate_case as evaluate_quasistationary_case,
    load_profile as load_quasistationary_profile,
    profile_file_sha256,
)


NUMBER_RE = re.compile(r"[-+]?(?:(?:\d+\.\d*)|(?:\.\d+)|(?:\d+))(?:[EeDd][-+]?\d+)?")
TIME_UNITS = {
    "s": ("seconds", 1.0),
    "sec": ("seconds", 1.0),
    "secs": ("seconds", 1.0),
    "second": ("seconds", 1.0),
    "seconds": ("seconds", 1.0),
    "ms": ("milliseconds", 1.0e-3),
    "msec": ("milliseconds", 1.0e-3),
    "msecs": ("milliseconds", 1.0e-3),
    "millisecond": ("milliseconds", 1.0e-3),
    "milliseconds": ("milliseconds", 1.0e-3),
    "us": ("microseconds", 1.0e-6),
    "usec": ("microseconds", 1.0e-6),
    "usecs": ("microseconds", 1.0e-6),
    "microsecond": ("microseconds", 1.0e-6),
    "microseconds": ("microseconds", 1.0e-6),
    "µs": ("microseconds", 1.0e-6),
    "ns": ("nanoseconds", 1.0e-9),
    "nsec": ("nanoseconds", 1.0e-9),
    "nsecs": ("nanoseconds", 1.0e-9),
    "nanosecond": ("nanoseconds", 1.0e-9),
    "nanoseconds": ("nanoseconds", 1.0e-9),
}


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def full_path(path: str | Path, base: Path) -> Path:
    value = Path(path)
    if not value.is_absolute():
        value = base / value
    return value.resolve()


def normalize_time_units(units: str) -> tuple[str, float]:
    key = units.strip().lower()
    if key not in TIME_UNITS:
        raise ValueError(
            f"Unsupported time units {units!r}. Supported: seconds, milliseconds, "
            "microseconds, nanoseconds."
        )
    return TIME_UNITS[key]


def time_to_seconds(value: float, units: str) -> float:
    _, factor = normalize_time_units(units)
    return value * factor


def numeric_tokens(line: str) -> list[float]:
    values: list[float] = []
    for match in NUMBER_RE.finditer(line):
        token = match.group(0).replace("D", "E").replace("d", "e")
        try:
            values.append(float(token))
        except ValueError:
            pass
    return values


def namelist_string_value(text: str, name: str) -> str | None:
    escaped = re.escape(name)
    pattern = re.compile(
        rf"^\s*{escaped}\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([^,/\r\n]+))",
        re.IGNORECASE | re.MULTILINE,
    )
    match = pattern.search(text)
    if not match:
        return None
    for group in match.groups():
        if group is not None:
            return group.strip()
    return None


def namelist_float_value(text: str, name: str) -> float | None:
    value = namelist_string_value(text, name)
    if value is None:
        return None
    try:
        return float(value.replace("D", "E").replace("d", "e"))
    except ValueError:
        return None


def namelist_logical_value(text: str, name: str) -> bool | None:
    value = namelist_string_value(text, name)
    if value is None:
        return None
    normalized = value.strip().lower().strip(".")
    if normalized in {"true", "t", "1"}:
        return True
    if normalized in {"false", "f", "0"}:
        return False
    return None


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, payload: dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


@dataclass
class TimeSource:
    time_column: int
    source_units: str
    setup_path: Path | None
    detected_from_setup: bool


@dataclass
class MonitorSample:
    value: float
    time_raw: float | None


@dataclass
class RuleState:
    valid_reads: int = 0
    consecutive_hits: int = 0
    last_value: float | None = None
    last_simulation_time_raw: float | None = None
    last_simulation_time_s: float | None = None
    last_seen_simulation_time_raw: float | None = None
    time_source: TimeSource | None = None


def process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def process_matches_executable(pid: int, executable: Path) -> bool:
    """Best-effort guard against PID reuse during interrupted-run recovery."""
    if not process_alive(pid):
        return False
    if os.name != "nt":
        proc_exe = Path(f"/proc/{pid}/exe")
        try:
            observed = proc_exe.resolve(strict=True)
            return observed == executable.resolve()
        except OSError:
            return False
    # On Windows, without psutil, existence is the strongest stdlib-only check.
    return True


class CampaignRunner:
    def __init__(
        self,
        manifest: Path,
        config_path: Path,
        laboratory: Path | None = None,
        *,
        selective_rerun_case_ids: list[str] | None = None,
        rerun_job_id: str | None = None,
        runner_job_id: str | None = None,
        termination_profile_name: str | None = None,
        control_file: Path | None = None,
    ):
        self.manifest_path = manifest.resolve()
        composite_sidecar = self.manifest_path.parent / "composite_manifest.json"
        if composite_sidecar.is_file():
            try:
                composite_meta = json.loads(composite_sidecar.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid composite manifest sidecar: {composite_sidecar}") from exc
            if composite_meta.get("analysis_only") is True:
                raise ValueError(
                    f"analysis-only composite manifest cannot be executed: {self.manifest_path}"
                )
        self.config_path = config_path.resolve()
        self.config = read_json(self.config_path)
        self.config_base = self.config_path.parent
        self.laboratory = Laboratory.load(laboratory)
        self.workspace_root = self.laboratory.runs_root
        self.exe_path = self.laboratory.computing_module

        # v3 run-config files sometimes duplicated machine-specific paths.  Keep
        # compatibility only when they agree with the authoritative laboratory.
        legacy_executable = self.config.get("executable")
        if legacy_executable:
            legacy_path = full_path(legacy_executable, self.workspace_root)
            if legacy_path != self.exe_path.resolve():
                raise ValueError(
                    "run_config executable conflicts with laboratory.toml: "
                    f"{legacy_path} != {self.exe_path}"
                )

        self.manifest_dir = self.manifest_path.parent
        self.global_log = self.manifest_dir / "batch_run.log"
        self.summary_path = self.manifest_dir / "run_summary.csv"

        self.threads = int(self.config.get("threads", self.laboratory.default_threads))
        if self.threads <= 0:
            raise ValueError("threads must be positive")
        self.max_concurrent_cases = int(self.config.get("max_concurrent_cases", 1))
        if self.max_concurrent_cases != 1:
            raise ValueError(
                "campaign_runner v5 intentionally supports exactly one concurrent CFD case; "
                "set max_concurrent_cases=1"
            )
        self.limit_library_threads = bool(self.config.get("limit_library_threads", True))
        self.poll_seconds = float(self.config.get("poll_seconds", 5))
        self.max_runtime_seconds = float(self.config.get("max_runtime_seconds", 0))
        self.success_exit_codes = {int(x) for x in self.config.get("success_exit_codes", [0])}
        self.skip_statuses = {str(x).lower() for x in self.config.get("skip_statuses", [])}
        self.rerun_failed = bool(self.config.get("rerun_failed", True))
        self.archive_old_monitor_files = bool(self.config.get("archive_old_monitor_files", True))
        self.monitor_log_each_read = bool(self.config.get("monitor_log_each_read", False))
        self.verify_case_fingerprint = bool(self.config.get("verify_case_fingerprint", True))
        self.stop_request_file = str(self.config.get("stop_request_file", "run_control.stop"))
        self.run_control_status_file = str(
            self.config.get("run_control_status_file", "run_control_status.inf")
        )
        self.graceful_stop_timeout = float(self.config.get("graceful_stop_timeout_seconds", 30))
        self.force_kill_on_graceful_timeout = bool(
            self.config.get("force_kill_on_graceful_stop_timeout", True)
        )
        self.enabled_rules = [r for r in self.config.get("monitor_rules", []) if bool(r.get("enabled", False))]

        if not self.exe_path.exists():
            raise FileNotFoundError(f"computing_module executable not found: {self.exe_path}")
        self.executable_sha256 = sha256_file(self.exe_path)
        self.manifest_sha256 = sha256_file(self.manifest_path)
        self.run_config_sha256 = sha256_file(self.config_path)
        self.laboratory_config_sha256 = sha256_file(self.laboratory.config_path)
        self.laboratory_local_config_sha256 = (
            sha256_file(self.laboratory.local_config_path)
            if self.laboratory.local_config_path
            else None
        )
        with self.manifest_path.open(newline="", encoding="utf-8-sig") as f:
            self.cases = list(csv.DictReader(f))
        if not self.cases:
            raise ValueError("campaign manifest contains no cases")

        manifest_ids = [str(row.get("case_id", "")).strip() for row in self.cases]
        if len(manifest_ids) != len(set(manifest_ids)):
            raise ValueError("campaign manifest contains duplicate case_id values")

        requested = [str(x).strip() for x in (selective_rerun_case_ids or []) if str(x).strip()]
        if len(requested) != len(set(requested)):
            raise ValueError("selective rerun case ids must be unique")
        unknown = [case_id for case_id in requested if case_id not in set(manifest_ids)]
        if unknown:
            raise ValueError("selective rerun case ids not found in manifest: " + ", ".join(unknown))

        self.selective_rerun_case_ids = tuple(requested)
        self.selective_rerun_set = set(requested)
        self.rerun_job_id = str(rerun_job_id).strip() if rerun_job_id else None
        self.runner_job_id = str(runner_job_id).strip() if runner_job_id else None
        self.control_file = control_file.expanduser().resolve() if control_file else None
        self.campaign_stop_requested = False
        self.operator_stop_events: list[dict[str, Any]] = []

        self.termination_profile_name = (
            str(termination_profile_name).strip() if termination_profile_name else None
        )
        self.termination_profile: QuasistationaryProfile | None = None
        self.termination_profile_sha256: str | None = None
        if self.termination_profile_name:
            profile_path = self.laboratory.termination_profiles
            if not profile_path.is_file():
                raise FileNotFoundError(
                    f"trusted termination profile registry not found: {profile_path}"
                )
            self.termination_profile = load_quasistationary_profile(
                profile_path, self.termination_profile_name
            )
            self.termination_profile_sha256 = profile_file_sha256(profile_path)

        self.execution_cases = (
            [row for row in self.cases if str(row.get("case_id", "")).strip() in self.selective_rerun_set]
            if self.selective_rerun_set
            else self.cases
        )

    def log(self, message: str) -> None:
        line = f"{now_text()}  {message}"
        print(line, flush=True)
        with self.global_log.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    def case_path(self, row: dict[str, str]) -> Path:
        return full_path(row["case_path"], self.workspace_root)

    def previous_status(self, case_path: Path) -> str | None:
        path = case_path / "run_status.json"
        if not path.exists():
            return None
        try:
            return str(read_json(path).get("status", "")).lower() or None
        except Exception:
            return None

    def verify_physical_termination_compatibility(
        self, row: dict[str, str]
    ) -> tuple[bool, str]:
        profile = self.termination_profile
        if profile is None:
            return True, ""
        case_id = str(row.get("case_id", "")).strip()
        try:
            mode = str(
                effective_field_value(
                    self.manifest_path,
                    case_id,
                    "run_control_config.termination_mode",
                )
            ).strip().lower()
        except Exception:
            mode = str(row.get("run_control_config.termination_mode", "")).strip().lower()
        if not mode:
            return False, (
                "effective case configuration lacks run_control_config.termination_mode "
                f"required for termination profile {profile.name!r}"
            )
        if mode not in set(profile.required_run_control_modes):
            return False, (
                f"termination profile {profile.name!r} requires run-control mode in "
                f"{list(profile.required_run_control_modes)}, but the effective attempt uses {mode!r}. "
                "Reset/reprepare this logical case with a compatible attempt override."
            )
        return True, ""

    def verify_fingerprint(self, row: dict[str, str], case_path: Path) -> tuple[bool, str]:
        manifest_fp = str(row.get("case_fingerprint", "")).strip()
        if not manifest_fp:
            return False, "cases.csv has no case_fingerprint"
        setup = case_path / "setup_input.nml"
        if not setup.exists():
            return False, f"setup_input.nml not found: {setup}"
        try:
            text = setup.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return False, f"cannot read setup_input.nml: {exc}"
        setup_fp = namelist_string_value(text, "case_fingerprint")
        if not setup_fp:
            return False, "setup_input.nml has no case_fingerprint"
        if manifest_fp.lower() != setup_fp.lower():
            return False, f"fingerprint mismatch: manifest={manifest_fp} setup={setup_fp}"
        return True, ""

    def postprocessor_metadata(self, setup_path: Path) -> dict[str, str | None] | None:
        if not setup_path.exists():
            return None
        try:
            text = setup_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        output_file = namelist_string_value(text, "POST_PROCESSOR_OUTPUT_FILE")
        units = namelist_string_value(text, "SAVE_TIME_UNITS")
        if output_file is None and units is None:
            return None
        return {"output_file": output_file, "save_time_units": units}

    def resolve_time_source(self, case_path: Path, rule: dict[str, Any]) -> TimeSource | None:
        activation = rule.get("activation")
        if not activation or str(activation.get("mode", "")).lower() != "simulation_time":
            return None
        time_column = int(activation.get("time_column", 1))
        source_units_text = str(activation.get("source_units", "auto"))
        setup_file_text = str(activation.get("post_processor_setup_file", "auto"))

        if source_units_text.lower() != "auto":
            normalized, _ = normalize_time_units(source_units_text)
            return TimeSource(time_column, normalized, None, False)

        if setup_file_text.lower() != "auto":
            candidates = [full_path(setup_file_text, case_path)]
        else:
            task_setup = case_path / "task_setup"
            candidates = sorted(task_setup.glob("post_processor*.inf")) if task_setup.exists() else []

        monitor_leaf = Path(str(rule["file"])).name.lower()
        fallback: tuple[Path, dict[str, str | None]] | None = None
        for candidate in candidates:
            meta = self.postprocessor_metadata(candidate)
            if meta is None:
                continue
            if fallback is None:
                fallback = (candidate, meta)
            output = meta.get("output_file")
            if output and Path(str(output).strip()).name.lower() == monitor_leaf:
                units = meta.get("save_time_units")
                if not units:
                    raise ValueError(f"rule {rule['name']!r}: SAVE_TIME_UNITS missing in {candidate}")
                normalized, _ = normalize_time_units(str(units))
                return TimeSource(time_column, normalized, candidate, True)

        if setup_file_text.lower() != "auto" and fallback is not None:
            candidate, meta = fallback
            units = meta.get("save_time_units")
            if not units:
                raise ValueError(f"rule {rule['name']!r}: SAVE_TIME_UNITS missing in {candidate}")
            normalized, _ = normalize_time_units(str(units))
            return TimeSource(time_column, normalized, candidate, True)

        raise ValueError(
            f"rule {rule['name']!r} uses simulation_time activation but time units for "
            f"{rule['file']!r} could not be determined"
        )

    @staticmethod
    def column(row: list[float], index: int) -> float | None:
        if index == -1:
            return row[-1] if row else None
        if index < 1 or index > len(row):
            return None
        return row[index - 1]

    def monitor_sample(
        self, path: Path, row_mode: str, row_index: int, column: int, time_column: int = 0
    ) -> MonitorSample | None:
        if not path.exists():
            return None
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return None
        if row_mode != "numeric_row":
            lines = lines[-300:]
        rows: list[list[float]] = []
        for raw in lines:
            line = raw.strip()
            if not line or line.startswith(("#", "!")):
                continue
            nums = numeric_tokens(line)
            if nums:
                rows.append(nums)
        if not rows:
            return None
        if row_mode == "last_numeric_row":
            selected = rows[-1]
        elif row_mode == "numeric_row":
            if row_index < 1 or row_index > len(rows):
                return None
            selected = rows[row_index - 1]
        else:
            raise ValueError(f"unknown row_mode {row_mode!r}")
        value = self.column(selected, column)
        if value is None:
            return None
        time_raw = None
        if time_column:
            time_raw = self.column(selected, time_column)
            if time_raw is None:
                return None
        return MonitorSample(value=float(value), time_raw=time_raw)

    @staticmethod
    def threshold(value: float, operator: str, threshold: float) -> bool:
        return {
            "<": value < threshold,
            "<=": value <= threshold,
            ">": value > threshold,
            ">=": value >= threshold,
            "==": value == threshold,
            "!=": value != threshold,
        }.get(operator, False) if operator in {"<", "<=", ">", ">=", "==", "!="} else (_ for _ in ()).throw(ValueError(f"unknown monitor operator {operator!r}"))

    def activation_reached(
        self,
        rule: dict[str, Any],
        elapsed_wall_s: float,
        sample: MonitorSample,
        source: TimeSource | None,
    ) -> bool:
        activation = rule.get("activation")
        if activation is None:
            return elapsed_wall_s >= float(rule.get("grace_seconds", 0))
        mode = str(activation.get("mode", "immediate")).lower()
        if mode == "immediate":
            return True
        if mode == "wall_clock":
            return elapsed_wall_s >= time_to_seconds(
                float(activation.get("after", 0)), str(activation.get("units", "seconds"))
            )
        if mode == "simulation_time":
            if sample.time_raw is None or source is None:
                return False
            sim_s = time_to_seconds(sample.time_raw, source.source_units)
            required_s = time_to_seconds(
                float(activation.get("after", 0)), str(activation.get("units", "seconds"))
            )
            return sim_s >= required_s
        raise ValueError(f"unknown activation.mode {mode!r} for rule {rule['name']!r}")

    def archive_monitors(self, case_path: Path) -> None:
        if not self.archive_old_monitor_files:
            return
        seen: set[Path] = set()
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        for rule in self.enabled_rules:
            path = case_path / str(rule["file"])
            if path in seen:
                continue
            seen.add(path)
            if path.exists():
                shutil.copy2(path, case_path / f"{path.name}.prev_{stamp}")
                path.unlink()

    @staticmethod
    def _selective_archive_candidate(path: Path) -> bool:
        """Return True for top-level run products worth preserving before rerun.

        `task_setup/` and `setup_input.nml` are immutable case inputs and are not
        copied. Large recursive output trees are intentionally not duplicated;
        the archive is for execution metadata and compact analysis histories.
        """
        if not path.is_file() or path.name == "setup_input.nml":
            return False
        exact = {
            "run_status.json",
            "run_control.stop",
            "run_control_status.inf",
            "computing_module.stdout.log",
            "computing_module.stderr.log",
        }
        if path.name in exact or (path.name.startswith("run_") and path.suffix == ".done"):
            return True
        return path.suffix.lower() in {".dat", ".csv", ".json", ".log", ".inf", ".done", ".txt"}

    def archive_previous_run(self, case_path: Path, case_id: str, previous_status: str | None) -> Path:
        """Preserve the previous compact run record before an explicit selective rerun."""
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        token = self.rerun_job_id or stamp
        archive_root = case_path / "_rerun_archive"
        archive_dir = archive_root / token
        if archive_dir.exists():
            archive_dir = archive_root / f"{token}_{stamp}"
        archive_dir.mkdir(parents=True, exist_ok=False)

        copied: list[dict[str, Any]] = []
        for source in sorted(case_path.iterdir(), key=lambda p: p.name.lower()):
            if not self._selective_archive_candidate(source):
                continue
            destination = archive_dir / source.name
            shutil.copy2(source, destination)
            copied.append(
                {
                    "name": source.name,
                    "size_bytes": source.stat().st_size,
                    "sha256": sha256_file(source),
                }
            )

        manifest = {
            "archive_version": 1,
            "created_at": now_text(),
            "case_id": case_id,
            "previous_status": previous_status,
            "rerun_job_id": self.rerun_job_id,
            "source_case_path": str(case_path),
            "files": copied,
            "scope": (
                "top-level compact run artifacts only; immutable task_setup/ and "
                "setup_input.nml are not duplicated, and recursive data directories "
                "are not copied"
            ),
        }
        write_json(archive_dir / "archive_manifest.json", manifest)
        self.log(
            f"ARCHIVE PRIOR RUN {case_id}: {archive_dir}; "
            f"{len(copied)} top-level artifact(s)"
        )
        return archive_dir

    def start_process(self, case_path: Path, stdout_log: Path, stderr_log: Path):
        stdout = stdout_log.open("w", encoding="utf-8", errors="replace")
        stderr = stderr_log.open("w", encoding="utf-8", errors="replace")
        child_environment = os.environ.copy()
        child_environment["OMP_NUM_THREADS"] = str(self.threads)
        child_environment["OMP_DYNAMIC"] = "FALSE"
        child_environment["OMP_MAX_ACTIVE_LEVELS"] = "1"
        child_environment["OMP_WAIT_POLICY"] = "PASSIVE"
        if self.limit_library_threads:
            # Prevent nested BLAS/math-library pools from multiplying the NRG
            # OpenMP thread count.  NRG owns the outer parallelism.
            for name in (
                "MKL_NUM_THREADS",
                "OPENBLAS_NUM_THREADS",
                "BLIS_NUM_THREADS",
                "NUMEXPR_NUM_THREADS",
                "VECLIB_MAXIMUM_THREADS",
            ):
                child_environment[name] = "1"
            child_environment["MKL_DYNAMIC"] = "FALSE"
            child_environment["KMP_BLOCKTIME"] = "0"
        kwargs: dict[str, Any] = {
            "cwd": case_path,
            "stdout": stdout,
            "stderr": stderr,
            "env": child_environment,
        }
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        try:
            process = subprocess.Popen(
                [str(self.exe_path), f"--num_threads={self.threads}"], **kwargs
            )
        except Exception:
            stdout.close()
            stderr.close()
            raise
        return process, stdout, stderr

    @staticmethod
    def force_kill_tree(process: subprocess.Popen[Any]) -> None:
        if process.poll() is not None:
            return
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
            else:
                os.killpg(process.pid, signal.SIGKILL)
        except Exception:
            try:
                process.kill()
            except Exception:
                pass

    def request_graceful_stop(
        self,
        process: subprocess.Popen[Any],
        case_path: Path,
        reason: str,
        details: list[str],
    ) -> tuple[bool, bool]:
        request = case_path / self.stop_request_file
        request.write_text(
            "\n".join([f"reason={reason}", f"requested_at={now_text()}", *details]) + "\n",
            encoding="utf-8",
        )
        deadline = time.monotonic() + max(1.0, self.graceful_stop_timeout)
        while process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.25)
        if process.poll() is not None:
            return True, False
        if self.force_kill_on_graceful_timeout:
            self.force_kill_tree(process)
            try:
                process.wait(timeout=5)
            except Exception:
                pass
            return False, True
        return False, False

    def read_operator_control(self) -> dict[str, Any] | None:
        """Return a pending trusted operator-control request for this runner.

        The control file is written by the trusted bridge.  Only requests in
        state=requested and targeted at this runner job are actionable.
        Malformed control files are ignored by the execution loop and logged.
        """
        path = self.control_file
        if path is None or not path.is_file():
            return None
        try:
            payload = read_json(path)
        except Exception as exc:
            self.log(f"WARNING: invalid operator control file {path}: {exc}")
            return None
        if str(payload.get("state", "")).lower() != "requested":
            return None
        target_job = str(payload.get("runner_job_id") or "").strip()
        if self.runner_job_id and target_job and target_job != self.runner_job_id:
            return None
        action = str(payload.get("action", "")).strip().lower()
        if action not in {"stop_campaign", "stop_case"}:
            self.update_operator_control(payload, "rejected", reason="unsupported_action")
            return None
        return payload

    def update_operator_control(
        self,
        request: dict[str, Any],
        state: str,
        **updates: Any,
    ) -> None:
        path = self.control_file
        if path is None:
            return
        payload = dict(request)
        payload.update(updates)
        payload["state"] = state
        payload["updated_at"] = now_text()
        write_json(path, payload)

    def operator_control_before_case(self) -> bool:
        """Handle a campaign-stop request between cases.

        Returns True when the runner must stop before launching another case.
        A stop_case request cannot be applied between cases and is rejected so
        the caller receives an explicit result instead of silently skipping a
        not-yet-running case.
        """
        request = self.read_operator_control()
        if request is None:
            return False
        action = str(request.get("action", "")).lower()
        if action == "stop_campaign":
            self.campaign_stop_requested = True
            event = {
                "action": action,
                "request_id": request.get("request_id"),
                "case_id": None,
                "handled_at": now_text(),
                "outcome": "runner_stopped_between_cases",
            }
            self.operator_stop_events.append(event)
            self.update_operator_control(request, "handled", **event)
            self.log("OPERATOR STOP: campaign runner stopping before next case")
            return True
        if action == "stop_case":
            self.update_operator_control(
                request,
                "rejected",
                reason="target_case_not_currently_running",
            )
            return False
        return False

    def full_summary(self) -> None:
        fields = [
            "case_id",
            "case_fingerprint",
            "status",
            "exit_code",
            "duration_s",
            "condition",
            "simulation_time_s",
            "nrg_termination_reason",
            "attempt_id",
            "runner_job_id",
            "restart_required",
            "case_path",
            "label",
        ]
        output: list[dict[str, Any]] = []
        for row in self.cases:
            case_path = self.case_path(row)
            base = {
                "case_id": row.get("case_id", ""),
                "case_fingerprint": row.get("case_fingerprint", ""),
                "case_path": str(case_path),
                "label": row.get("label", ""),
            }
            if not case_path.exists():
                output.append({**base, "status": "missing_case", "exit_code": "", "duration_s": 0, "condition": "", "simulation_time_s": ""})
                continue
            status_path = case_path / "run_status.json"
            if not status_path.exists():
                output.append({**base, "status": "not_started", "exit_code": "", "duration_s": 0, "condition": "", "simulation_time_s": ""})
                continue
            try:
                st = read_json(status_path)
                output.append({
                    **base,
                    "status": st.get("status", "invalid_status"),
                    "exit_code": st.get("exit_code", ""),
                    "duration_s": st.get("duration_s", 0),
                    "condition": st.get("termination_condition", ""),
                    "simulation_time_s": st.get("termination_simulation_time_s", ""),
                    "nrg_termination_reason": st.get("nrg_termination_reason", ""),
                    "attempt_id": st.get("attempt_id", ""),
                    "runner_job_id": st.get("runner_job_id") or st.get("selective_rerun_job_id", ""),
                    "restart_required": st.get("nrg_restart_required", ""),
                })
            except Exception:
                output.append({**base, "status": "invalid_status", "exit_code": "", "duration_s": 0, "condition": "", "simulation_time_s": ""})
        with self.summary_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            writer.writerows(output)

    def nrg_run_control_status(self, case_path: Path) -> dict[str, Any] | None:
        path = case_path / self.run_control_status_file
        if not path.exists():
            return None
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        reason = namelist_string_value(text, "termination_reason")
        if not reason:
            return None
        return {
            "termination_reason": reason.strip(),
            "final_simulation_time_s": namelist_float_value(
                text, "final_simulation_time"
            ),
            "elapsed_wall_time_s": namelist_float_value(text, "elapsed_wall_time"),
            "restart_required": namelist_logical_value(
                text, "restart_checkpoint_required"
            ),
        }

    def failed_preflight(self, row: dict[str, str], case_path: Path, condition: str, message: str) -> None:
        stamp = now_text()
        payload = {
            "case_id": row.get("case_id", ""),
            "case_fingerprint": row.get("case_fingerprint", ""),
            "label": row.get("label", ""),
            "case_path": str(case_path),
            "status": "failed",
            "start_time": stamp,
            "end_time": stamp,
            "duration_s": 0,
            "exit_code": None,
            "termination_condition": condition,
            "termination_value": None,
            "termination_simulation_time_raw": None,
            "termination_simulation_time_units": None,
            "termination_simulation_time_s": None,
            "termination_message": message,
            "graceful_stop_requested": False,
            "graceful_stop_completed": False,
            "force_kill_used": False,
            "executable_sha256": self.executable_sha256,
            "manifest_sha256": self.manifest_sha256,
            "run_config_sha256": self.run_config_sha256,
            "laboratory_config": str(self.laboratory.config_path),
            "laboratory_config_sha256": self.laboratory_config_sha256,
            "laboratory_local_config": (
                str(self.laboratory.local_config_path)
                if self.laboratory.local_config_path
                else None
            ),
            "laboratory_local_config_sha256": self.laboratory_local_config_sha256,
        }
        write_json(case_path / "run_status.json", payload)
        (case_path / "run_failed.done").write_text(message + "\n", encoding="utf-8")

    def run_case(self, index: int, row: dict[str, str]) -> None:
        total = len(self.execution_cases)
        case_id = str(row.get("case_id", ""))
        label = str(row.get("label", ""))
        case_path = self.case_path(row)
        if not case_path.exists():
            self.log(f"[{index}/{total}] MISSING {case_id}: {case_path}")
            self.full_summary()
            return

        previous = self.previous_status(case_path)
        force_selected_rerun = case_id in self.selective_rerun_set
        if previous and not force_selected_rerun:
            skip = previous in self.skip_statuses
            if previous == "failed" and self.rerun_failed:
                skip = False
            if skip:
                self.log(f"[{index}/{total}] SKIP {case_id}; prior status={previous}; {label}")
                self.full_summary()
                return
        elif previous and force_selected_rerun:
            self.log(
                f"[{index}/{total}] SELECTIVE RERUN {case_id}; "
                f"bypassing prior status={previous}; {label}"
            )

        if self.verify_case_fingerprint:
            ok, message = self.verify_fingerprint(row, case_path)
            if not ok:
                self.failed_preflight(row, case_path, "provenance_mismatch", message)
                self.log(f"FAILED PROVENANCE {case_id}: {message}")
                self.full_summary()
                return

        ok, message = self.verify_physical_termination_compatibility(row)
        if not ok:
            self.failed_preflight(row, case_path, "physical_termination_config", message)
            self.log(f"FAILED PHYSICAL CONFIG {case_id}: {message}")
            self.full_summary()
            return

        previous_archive: Path | None = None
        if force_selected_rerun:
            previous_archive = self.archive_previous_run(case_path, case_id, previous)

        stdout_log = case_path / "computing_module.stdout.log"
        stderr_log = case_path / "computing_module.stderr.log"
        for path in [
            stdout_log,
            stderr_log,
            case_path / "run_finished.done",
            case_path / "run_condition_met.done",
            case_path / "run_condition_not_met.done",
            case_path / "run_failed.done",
            case_path / "run_timeout.done",
            case_path / "run_restart_required.done",
            case_path / "run_external_stop.done",
            case_path / "run_interrupted.done",
            case_path / "run_stopped.done",
            case_path / self.stop_request_file,
            case_path / self.run_control_status_file,
            case_path / "quasistationary_status.json",
        ]:
            try:
                path.unlink()
            except FileNotFoundError:
                pass

        self.archive_monitors(case_path)
        started_wall = time.monotonic()
        started_text = now_text()
        self.log(f"[{index}/{total}] RUN {case_id}: {label}")
        status_path = case_path / "run_status.json"
        attempt_config_path = case_path / "attempt_config.json"
        attempt_config: dict[str, Any] = {}
        if attempt_config_path.is_file():
            try:
                attempt_config = read_json(attempt_config_path)
            except Exception:
                attempt_config = {}
        else:
            try:
                override = load_override_record(self.manifest_path, case_id)
            except Exception:
                override = None
            if isinstance(override, dict):
                attempt_config = override

        status: dict[str, Any] = {
            "case_id": case_id,
            "case_fingerprint": row.get("case_fingerprint", ""),
            "label": label,
            "case_path": str(case_path),
            "status": "running",
            "start_time": started_text,
            "end_time": None,
            "duration_s": 0,
            "exit_code": None,
            "termination_condition": None,
            "termination_value": None,
            "termination_simulation_time_raw": None,
            "termination_simulation_time_units": None,
            "termination_simulation_time_s": None,
            "termination_message": None,
            "graceful_stop_requested": False,
            "graceful_stop_completed": False,
            "force_kill_used": False,
            "nrg_termination_reason": None,
            "nrg_restart_required": None,
            "nrg_final_simulation_time_s": None,
            "nrg_elapsed_wall_time_s": None,
            "process_pid": None,
            "requested_openmp_threads": self.threads,
            "max_concurrent_cases": self.max_concurrent_cases,
            "library_thread_pools_limited_to_one": self.limit_library_threads,
            "selective_rerun": force_selected_rerun,
            "selective_rerun_job_id": self.rerun_job_id if force_selected_rerun else None,
            "runner_job_id": self.runner_job_id,
            "previous_run_archive": str(previous_archive) if previous_archive else None,
            "case_identity_fingerprint": attempt_config.get("case_identity_fingerprint"),
            "base_attempt_fingerprint": attempt_config.get("base_attempt_fingerprint"),
            "attempt_fingerprint": attempt_config.get("attempt_fingerprint"),
            "attempt_overrides": attempt_config.get("overrides", {}),
            "attempt_id": attempt_config.get("attempt_id"),
            "identifier_semantics": {
                "attempt_id": (
                    "recalculation/configuration lineage created by reset/attempt preparation; "
                    "it is not a runner job identifier and may span multiple runner jobs"
                ),
                "runner_job_id": (
                    "the background runner invocation that executed this current run_status"
                ),
            },
            "termination_profile": self.termination_profile_name,
            "termination_profile_registry": (
                str(self.laboratory.termination_profiles)
                if self.termination_profile_name else None
            ),
            "termination_profile_registry_sha256": self.termination_profile_sha256,
            "physical_condition_met": False if self.termination_profile_name else None,
            "physical_condition_status": None,
            "physical_condition": None,
            "product_state": None,
            "executable_sha256": self.executable_sha256,
            "manifest_sha256": self.manifest_sha256,
            "run_config_sha256": self.run_config_sha256,
            "laboratory_config": str(self.laboratory.config_path),
            "laboratory_config_sha256": self.laboratory_config_sha256,
            "laboratory_local_config": (
                str(self.laboratory.local_config_path)
                if self.laboratory.local_config_path
                else None
            ),
            "laboratory_local_config_sha256": self.laboratory_local_config_sha256,
        }
        write_json(status_path, status)

        states: dict[str, RuleState] = {}
        try:
            for rule in self.enabled_rules:
                source = self.resolve_time_source(case_path, rule)
                states[str(rule["name"])] = RuleState(time_source=source)
                if source:
                    setup = str(source.setup_path) if source.setup_path else "<explicit units>"
                    self.log(
                        f"    monitor {rule['name']}: simulation-time source "
                        f"column={source.time_column}, units={source.source_units}, setup={setup}"
                    )
        except Exception as exc:
            message = f"Monitor configuration error: {exc}"
            self.failed_preflight(row, case_path, "monitor_config", message)
            self.log(f"FAILED CONFIG {case_id}: {message}")
            self.full_summary()
            return

        try:
            process, stdout_handle, stderr_handle = self.start_process(case_path, stdout_log, stderr_log)
            status["process_pid"] = process.pid
            write_json(status_path, status)
        except Exception as exc:
            message = f"Failed to start computing_module: {exc}"
            self.failed_preflight(row, case_path, "start_failure", message)
            self.log(f"FAILED START {case_id}: {message}")
            self.full_summary()
            return

        triggered_rule: dict[str, Any] | None = None
        triggered_value: float | None = None
        triggered_time_raw: float | None = None
        triggered_time_s: float | None = None
        triggered_time_units: str | None = None
        timed_out = False
        graceful_requested = False
        graceful_completed = False
        force_kill_used = False
        physical_result: dict[str, Any] | None = None
        physical_triggered = False
        last_physical_check_wall = -math.inf
        operator_stop_action: str | None = None
        operator_stop_request: dict[str, Any] | None = None

        try:
            while process.poll() is None:
                time.sleep(max(0.05, self.poll_seconds))
                elapsed = time.monotonic() - started_wall

                control = self.read_operator_control()
                if control is not None:
                    action = str(control.get("action", "")).strip().lower()
                    target_case = str(control.get("case_id") or "").strip()
                    if action == "stop_campaign" or (
                        action == "stop_case" and target_case == case_id
                    ):
                        operator_stop_action = action
                        operator_stop_request = control
                        if action == "stop_campaign":
                            self.campaign_stop_requested = True
                        self.update_operator_control(
                            control,
                            "acknowledged",
                            active_case_id=case_id,
                            acknowledged_at=now_text(),
                        )
                        self.log(
                            f"    OPERATOR {action.upper()} {case_id}: graceful external stop requested"
                        )
                        graceful_requested = True
                        graceful_completed, force_kill_used = self.request_graceful_stop(
                            process,
                            case_path,
                            f"operator:{action}",
                            [
                                f"case_id={case_id}",
                                f"request_id={control.get('request_id', '')}",
                                f"runner_job_id={self.runner_job_id or ''}",
                            ],
                        )
                        break
                    if action == "stop_case" and target_case != case_id:
                        self.update_operator_control(
                            control,
                            "rejected",
                            reason="target_case_not_currently_running",
                            active_case_id=case_id,
                        )

                if operator_stop_action is not None:
                    break

                if self.max_runtime_seconds > 0 and elapsed >= self.max_runtime_seconds:
                    timed_out = True
                    self.log(
                        f"    TIMEOUT {case_id}: elapsed={elapsed:.0f} s >= "
                        f"{self.max_runtime_seconds:g} s"
                    )
                    graceful_requested = True
                    graceful_completed, force_kill_used = self.request_graceful_stop(
                        process,
                        case_path,
                        "runner_timeout",
                        [f"elapsed_s={elapsed}", f"limit_s={self.max_runtime_seconds}"],
                    )
                    break

                if (
                    self.termination_profile is not None
                    and elapsed - last_physical_check_wall
                    >= self.termination_profile.check_wall_interval_s
                ):
                    last_physical_check_wall = elapsed
                    try:
                        qs = evaluate_quasistationary_case(
                            case_path, self.termination_profile
                        )
                        physical_result = qs.to_dict()
                        write_json(case_path / "quasistationary_status.json", {
                            "observed_at": now_text(),
                            "profile_registry": str(self.laboratory.termination_profiles),
                            "profile_registry_sha256": self.termination_profile_sha256,
                            **physical_result,
                        })
                        if self.monitor_log_each_read:
                            self.log(
                                f"    {case_id}: physical={qs.status}; "
                                f"t_sim={qs.history_end_time_s}; "
                                f"rel_dT={qs.relative_temperature_span}; "
                                f"max_dY={qs.max_species_mass_fraction_span}"
                            )
                        if qs.reached:
                            physical_triggered = True
                            triggered_time_s = qs.history_end_time_s
                            self.log(
                                f"    PHYSICAL CONDITION {case_id}: "
                                f"{self.termination_profile.name}; "
                                f"t_sim={triggered_time_s:.8g} s"
                            )
                            details = [
                                f"condition={self.termination_profile.name}",
                                f"simulation_time_s={triggered_time_s}",
                                f"relative_temperature_span={qs.relative_temperature_span}",
                                f"relative_pressure_span={qs.relative_pressure_span}",
                                f"relative_density_span={qs.relative_density_span}",
                                f"max_species_mass_fraction_span={qs.max_species_mass_fraction_span}",
                                f"max_sumY_error={qs.max_sumY_error}",
                            ]
                            graceful_requested = True
                            graceful_completed, force_kill_used = self.request_graceful_stop(
                                process,
                                case_path,
                                f"physical:{self.termination_profile.name}",
                                details,
                            )
                            break
                    except (FileNotFoundError, ValueError, OSError) as exc:
                        # While NRG is appending a file, a partial last line can
                        # transiently make the full-history parser reject the
                        # snapshot. Retry on the next physical-monitor interval.
                        if self.monitor_log_each_read:
                            self.log(
                                f"    {case_id}: physical monitor waiting: {exc}"
                            )

                if physical_triggered:
                    break

                for rule in self.enabled_rules:
                    name = str(rule["name"])
                    state = states[name]
                    source = state.time_source
                    sample = self.monitor_sample(
                        case_path / str(rule["file"]),
                        str(rule.get("row_mode", "last_numeric_row")),
                        int(rule.get("row_index", 0)),
                        int(rule.get("column", -1)),
                        source.time_column if source else 0,
                    )
                    if sample is None:
                        continue
                    if source:
                        assert sample.time_raw is not None
                        if (
                            state.last_seen_simulation_time_raw is not None
                            and sample.time_raw <= state.last_seen_simulation_time_raw
                        ):
                            continue
                        state.last_seen_simulation_time_raw = sample.time_raw
                        state.last_simulation_time_raw = sample.time_raw
                        state.last_simulation_time_s = time_to_seconds(
                            sample.time_raw, source.source_units
                        )
                    state.valid_reads += 1
                    state.last_value = sample.value

                    if self.monitor_log_each_read:
                        if source:
                            self.log(
                                f"    {case_id}: {name}={sample.value:.8g}; "
                                f"t_sim={sample.time_raw:.8g} {source.source_units} "
                                f"({state.last_simulation_time_s:.8g} s)"
                            )
                        else:
                            self.log(f"    {case_id}: {name}={sample.value:.8g}; wall={elapsed:.0f} s")

                    active = self.activation_reached(rule, elapsed, sample, source)
                    eligible = active and state.valid_reads >= int(rule.get("min_valid_reads", 1))
                    hit = eligible and self.threshold(
                        sample.value,
                        str(rule["operator"]),
                        float(rule["threshold"]),
                    )
                    state.consecutive_hits = state.consecutive_hits + 1 if hit else 0
                    if eligible and state.consecutive_hits >= int(rule.get("consecutive_hits", 1)):
                        triggered_rule = rule
                        triggered_value = sample.value
                        if source:
                            triggered_time_raw = sample.time_raw
                            triggered_time_s = state.last_simulation_time_s
                            triggered_time_units = source.source_units
                            self.log(
                                f"    CONDITION {case_id}: {name}; value={triggered_value:.8g} "
                                f"{rule['operator']} {float(rule['threshold']):.8g}; "
                                f"t_sim={triggered_time_raw:.8g} {triggered_time_units}"
                            )
                        else:
                            self.log(
                                f"    CONDITION {case_id}: {name}; value={triggered_value:.8g} "
                                f"{rule['operator']} {float(rule['threshold']):.8g}; wall={elapsed:.0f} s"
                            )
                        details = [
                            f"condition={name}",
                            f"value={triggered_value}",
                            f"operator={rule['operator']}",
                            f"threshold={rule['threshold']}",
                        ]
                        if triggered_time_s is not None:
                            details.append(f"simulation_time_s={triggered_time_s}")
                        graceful_requested = True
                        graceful_completed, force_kill_used = self.request_graceful_stop(
                            process, case_path, f"monitor:{name}", details
                        )
                        break
                if triggered_rule is not None:
                    break
        finally:
            try:
                exit_code = process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.force_kill_tree(process)
                exit_code = process.wait(timeout=10)
                force_kill_used = True
            stdout_handle.close()
            stderr_handle.close()

        end_text = now_text()
        duration = int(round(time.monotonic() - started_wall))
        core_status = self.nrg_run_control_status(case_path)
        nrg_reason = core_status.get("termination_reason") if core_status else None
        nrg_restart = core_status.get("restart_required") if core_status else None
        nrg_final_time = core_status.get("final_simulation_time_s") if core_status else None
        nrg_elapsed_wall = core_status.get("elapsed_wall_time_s") if core_status else None

        if physical_triggered and self.termination_profile is not None:
            run_status = "condition_met"
            condition = self.termination_profile.name
            message = "Post-ignition quasistationary product-state condition met."
            lines = [
                f"Condition: {condition}",
                f"Simulation time [s]: {triggered_time_s}",
                f"Message: {message}",
                f"Wall-clock completion time: {end_text}",
            ]
            (case_path / "run_condition_met.done").write_text(
                "\n".join(lines) + "\n", encoding="utf-8"
            )
            self.log(f"CONDITION MET {case_id}: {condition}")
        elif triggered_rule is not None:
            run_status = "condition_met"
            condition = str(triggered_rule["name"])
            message = str(triggered_rule.get("message", "Monitor condition met."))
            lines = [f"Condition: {condition}", f"Value: {triggered_value}"]
            if triggered_time_s is not None:
                lines += [
                    f"Simulation time: {triggered_time_raw} {triggered_time_units}",
                    f"Simulation time [s]: {triggered_time_s}",
                ]
            lines += [f"Message: {message}", f"Wall-clock completion time: {end_text}"]
            (case_path / "run_condition_met.done").write_text("\n".join(lines) + "\n", encoding="utf-8")
            self.log(f"CONDITION MET {case_id}: {condition}")
        elif operator_stop_action is not None:
            run_status = "stopped"
            condition = "operator_campaign_stop" if operator_stop_action == "stop_campaign" else "operator_case_stop"
            message = (
                "Run stopped cleanly by an explicit trusted operator request. "
                "This is not a successful scientific completion and the case is rerunnable."
            )
            (case_path / "run_stopped.done").write_text(
                f"{message}\nStopped at {end_text}; exit code={exit_code}; "
                f"NRG reason={nrg_reason or 'not_reported'}.\n",
                encoding="utf-8",
            )
            event = {
                "action": operator_stop_action,
                "request_id": (operator_stop_request or {}).get("request_id"),
                "reason": (operator_stop_request or {}).get("reason"),
                "case_id": case_id,
                "handled_at": end_text,
                "outcome": "case_stopped",
                "exit_code": exit_code,
                "nrg_termination_reason": nrg_reason,
                "graceful_stop_completed": graceful_completed,
                "force_kill_used": force_kill_used,
            }
            self.operator_stop_events.append(event)
            if operator_stop_request is not None:
                self.update_operator_control(operator_stop_request, "handled", **event)
            self.log(
                f"STOPPED {case_id}; operator={operator_stop_action}; "
                f"NRG reason={nrg_reason or 'not_reported'}"
            )
        elif timed_out:
            run_status = "timeout"
            condition = "max_runtime"
            message = f"Maximum runtime of {self.max_runtime_seconds:g} s exceeded."
            (case_path / "run_timeout.done").write_text(message + "\n", encoding="utf-8")
            self.log(f"TIMEOUT {case_id}")
        elif exit_code in self.success_exit_codes and nrg_reason == "wall_time_limit" and nrg_restart:
            run_status = "restart_required"
            condition = "wall_time_limit"
            message = "NRG reached its wall-time reserve and wrote a restart checkpoint."
            (case_path / "run_restart_required.done").write_text(
                f"Restart required at {end_text}; exit code {exit_code}; "
                f"simulation_time_s={nrg_final_time}.\n",
                encoding="utf-8",
            )
            self.log(f"RESTART REQUIRED {case_id}; NRG wall-time limit")
        elif exit_code in self.success_exit_codes and nrg_reason == "external_stop_request":
            run_status = "external_stop"
            condition = "external_stop_request"
            message = "NRG observed an external stop request not classified by this runner invocation."
            (case_path / "run_external_stop.done").write_text(message + "\n", encoding="utf-8")
            self.log(f"EXTERNAL STOP {case_id}; exit code {exit_code}")
        elif (
            self.termination_profile is not None
            and exit_code in self.success_exit_codes
        ):
            run_status = "condition_not_met"
            condition = "physical_condition_not_reached"
            message = (
                f"Process ended before trusted physical termination profile "
                f"{self.termination_profile.name!r} was satisfied; "
                f"NRG reason={nrg_reason or 'not_reported'}."
            )
            (case_path / "run_condition_not_met.done").write_text(
                message + "\n", encoding="utf-8"
            )
            self.log(
                f"CONDITION NOT MET {case_id}; profile={self.termination_profile.name}; "
                f"NRG reason={nrg_reason or 'not_reported'}"
            )
        elif exit_code in self.success_exit_codes:
            run_status = "finished"
            condition = nrg_reason or ""
            if nrg_reason == "final_simulation_time":
                message = "NRG reached the configured final simulation time."
            else:
                message = "Process completed with an accepted exit code."
            (case_path / "run_finished.done").write_text(
                f"Finished normally at {end_text}; exit code {exit_code}; "
                f"NRG reason={nrg_reason or 'not_reported'}.\n", encoding="utf-8"
            )
            self.log(
                f"FINISHED {case_id}; exit code {exit_code}; "
                f"NRG reason={nrg_reason or 'not_reported'}"
            )
        else:
            run_status = "failed"
            condition = "process_exit"
            message = f"Process exited with unaccepted code {exit_code}."
            (case_path / "run_failed.done").write_text(
                message + "\nSee computing_module.stdout.log and computing_module.stderr.log.\n",
                encoding="utf-8",
            )
            self.log(f"FAILED {case_id}; exit code={exit_code}")

        status.update(
            {
                "status": run_status,
                "end_time": end_text,
                "duration_s": duration,
                "exit_code": exit_code,
                "termination_condition": condition,
                "termination_value": triggered_value,
                "termination_simulation_time_raw": triggered_time_raw,
                "termination_simulation_time_units": triggered_time_units,
                "termination_simulation_time_s": triggered_time_s,
                "termination_message": message,
                "graceful_stop_requested": graceful_requested,
                "graceful_stop_completed": graceful_completed,
                "force_kill_used": force_kill_used,
                "operator_stop_requested": operator_stop_action is not None,
                "operator_stop_action": operator_stop_action,
                "operator_stop_request_id": (operator_stop_request or {}).get("request_id"),
                "operator_stop_reason": (operator_stop_request or {}).get("reason"),
                "nrg_termination_reason": nrg_reason,
                "nrg_restart_required": nrg_restart,
                "nrg_final_simulation_time_s": nrg_final_time,
                "nrg_elapsed_wall_time_s": nrg_elapsed_wall,
                "physical_condition_met": (
                    physical_triggered if self.termination_profile is not None else None
                ),
                "physical_condition_status": (
                    physical_result.get("status")
                    if physical_result is not None else None
                ),
                "physical_condition": physical_result,
                "product_state": (
                    {
                        "temperature_K": physical_result.get("product_temperature_K"),
                        "pressure_Pa": physical_result.get("product_pressure_Pa"),
                        "density_kg_m3": physical_result.get("product_density_kg_m3"),
                        "species_mass_fractions": physical_result.get(
                            "product_species_mass_fractions", {}
                        ),
                        "averaging_window_start_s": physical_result.get(
                            "window_start_time_s"
                        ),
                        "averaging_window_end_s": physical_result.get(
                            "window_end_time_s"
                        ),
                    }
                    if physical_triggered and physical_result is not None
                    else None
                ),
            }
        )
        write_json(status_path, status)
        self.full_summary()

    def recover_interrupted_cases(self) -> int:
        """Convert stale `running` statuses to `interrupted` after lock acquisition.

        If a recorded computing_module PID is still the trusted executable, abort
        instead of starting another CFD process.  This protects against a runner
        crash that left its child alive.
        """
        recovered = 0
        live_orphans: list[str] = []
        for row in self.cases:
            cp = self.case_path(row)
            status_path = cp / "run_status.json"
            if not status_path.is_file():
                continue
            try:
                payload = read_json(status_path)
            except Exception:
                continue
            if str(payload.get("status", "")).lower() != "running":
                continue
            pid = int(payload.get("process_pid") or 0)
            if pid and process_matches_executable(pid, self.exe_path):
                live_orphans.append(f"{row.get('case_id','')} pid={pid} path={cp}")
                continue
            case_id = str(row.get("case_id", "")).strip()
            if self.selective_rerun_set and case_id not in self.selective_rerun_set:
                # Preserve unrelated stale metadata during a targeted rerun. We
                # still performed the live-process safety check above.
                continue
            stamp = now_text()
            payload.update({
                "status": "interrupted",
                "end_time": stamp,
                "termination_condition": "runner_interrupted",
                "termination_message": (
                    "Recovered stale running state after the previous campaign runner "
                    "ceased to own the laboratory execution lock. The case is runnable again."
                ),
                "interrupted_recovered_at": stamp,
            })
            write_json(status_path, payload)
            (cp / "run_interrupted.done").write_text(
                f"Recovered interrupted case at {stamp}; previous process_pid={pid or 'not_recorded'}.\n",
                encoding="utf-8",
            )
            recovered += 1
            self.log(f"RECOVER INTERRUPTED {row.get('case_id','')}; previous pid={pid or 'not_recorded'}")
        if live_orphans:
            raise RuntimeError(
                "refusing to start because a previous computing_module process still appears alive: "
                + "; ".join(live_orphans)
            )
        return recovered

    def run(self) -> int:
        self.log("=== Starting laboratory-aware NRG campaign run v10 ===")
        recovered = self.recover_interrupted_cases()
        if recovered:
            self.log(f"Recovered interrupted cases: {recovered}")
        self.log(f"Manifest: {self.manifest_path}")
        self.log(f"Laboratory config: {self.laboratory.config_path}")
        self.log(f"Runs root: {self.workspace_root}")
        self.log(f"Executable: {self.exe_path}")
        self.log(f"Executable SHA-256: {self.executable_sha256}")
        self.log(f"Manifest SHA-256: {self.manifest_sha256}")
        self.log(f"Run-config SHA-256: {self.run_config_sha256}")
        self.log(f"Laboratory-config SHA-256: {self.laboratory_config_sha256}")
        if self.laboratory.local_config_path:
            self.log(
                "Laboratory local config: "
                f"{self.laboratory.local_config_path} "
                f"(SHA-256 {self.laboratory_local_config_sha256})"
            )
        self.log(
            f"Cases in manifest: {len(self.cases)}; cases selected for this invocation: "
            f"{len(self.execution_cases)}; threads/case: {self.threads}; "
            f"max concurrent cases: {self.max_concurrent_cases}; polling: {self.poll_seconds:g} s"
        )
        if self.selective_rerun_set:
            self.log(
                "Selective rerun case ids: " + ", ".join(self.selective_rerun_case_ids)
            )
        self.log(f"Nested numerical-library thread pools limited to one: {self.limit_library_threads}")
        self.log(f"Enabled monitor rules: {len(self.enabled_rules)}")
        self.log(f"NRG run-control status file: {self.run_control_status_file}")
        if self.termination_profile is not None:
            self.log(
                f"Trusted physical termination profile: {self.termination_profile.name}; "
                f"registry={self.laboratory.termination_profiles}; "
                f"sha256={self.termination_profile_sha256}"
            )
        self.log(
            f"Graceful stop request: {self.stop_request_file}; wait "
            f"{self.graceful_stop_timeout:g} s; force fallback={self.force_kill_on_graceful_timeout}"
        )
        self.log(f"Case fingerprint verification: {self.verify_case_fingerprint}")
        self.full_summary()
        for index, row in enumerate(self.execution_cases, 1):
            if self.operator_control_before_case():
                break
            self.run_case(index, row)
            if self.campaign_stop_requested:
                break
        self.full_summary()
        if self.campaign_stop_requested:
            self.log("=== Laboratory-aware NRG campaign run v10 stopped by operator ===")
        else:
            self.log("=== Laboratory-aware NRG campaign run v10 completed ===")
        return 0


def update_job_file(path: Path | None, **updates: Any) -> None:
    if path is None:
        return
    payload: dict[str, Any] = {}
    if path.exists():
        try:
            payload = read_json(path)
        except Exception:
            payload = {}
    payload.update(updates)
    write_json(path, payload)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run an NRG cases.csv campaign")
    parser.add_argument("manifest", type=Path, help="cases.csv generated by campaign_generator.py")
    parser.add_argument("config", type=Path, help="run_config.json")
    parser.add_argument(
        "--laboratory", type=Path, default=None,
        help="laboratory.toml; defaults to config/laboratory.toml or NRG_LABORATORY_CONFIG",
    )
    parser.add_argument(
        "--job-file", type=Path, default=None,
        help="optional JSON job record updated when this runner exits",
    )
    parser.add_argument(
        "--selective-rerun-case-id",
        action="append",
        default=[],
        help=(
            "exact case_id to force-run even when its prior status is normally skipped; "
            "repeat for multiple cases"
        ),
    )
    parser.add_argument(
        "--rerun-job-id",
        default=None,
        help="job identifier used to name the per-case previous-run archive",
    )
    parser.add_argument(
        "--termination-profile",
        default=None,
        help=(
            "exact trusted physical termination profile name from "
            "config/termination_profiles.json"
        ),
    )
    parser.add_argument(
        "--control-file",
        type=Path,
        default=None,
        help="trusted operator-control JSON file for stop_case/stop_campaign requests",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    job_file = args.job_file.expanduser().resolve() if args.job_file else None
    update_job_file(
        job_file,
        state="running",
        runner_pid=os.getpid(),
        runner_started_at=now_text(),
        manifest=str(args.manifest.expanduser().resolve()),
        run_config=str(args.config.expanduser().resolve()),
        execution_mode="selective_rerun" if args.selective_rerun_case_id else "campaign",
        selected_case_ids=list(args.selective_rerun_case_id),
        termination_profile=args.termination_profile,
        control_file=str(args.control_file.expanduser().resolve()) if args.control_file else None,
    )
    try:
        lab = Laboratory.load(args.laboratory)
        lock_metadata = {
            "manifest": str(args.manifest.expanduser().resolve()),
            "run_config": str(args.config.expanduser().resolve()),
            "job_file": str(job_file) if job_file else None,
            "execution_mode": "selective_rerun" if args.selective_rerun_case_id else "campaign",
            "selected_case_ids": list(args.selective_rerun_case_id),
            "termination_profile": args.termination_profile,
            "control_file": str(args.control_file.expanduser().resolve()) if args.control_file else None,
        }
        with LaboratoryRunnerLock(lab.campaign_root, lock_metadata) as runner_lock:
            update_job_file(job_file, runner_lock=probe_runner_lock(lab.campaign_root))
            runner = CampaignRunner(
                args.manifest,
                args.config,
                args.laboratory,
                selective_rerun_case_ids=list(args.selective_rerun_case_id),
                rerun_job_id=args.rerun_job_id,
                runner_job_id=(job_file.stem if job_file is not None else args.rerun_job_id),
                termination_profile_name=args.termination_profile,
                control_file=(args.control_file.expanduser().resolve() if args.control_file else None),
            )
            rc = runner.run()
            runner_lock.update(
                exit_code=rc,
                operator_campaign_stop=runner.campaign_stop_requested,
            )
    except RunnerAlreadyActive as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        # No lock was acquired by this runner, but expose the current live lock
        # observation so the persisted job record is not mistaken for authority.
        live_lock = probe_runner_lock(lab.campaign_root) if "lab" in locals() else None
        update_job_file(
            job_file,
            state="blocked",
            exit_code=3,
            error=str(exc),
            runner_finished_at=now_text(),
            runner_lock=live_lock,
        )
        return 3
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        # If an exception occurred inside the with-block, __exit__ has already
        # released the OS lock before control reaches here.
        live_lock = probe_runner_lock(lab.campaign_root) if "lab" in locals() else None
        update_job_file(
            job_file,
            state="failed",
            exit_code=2,
            error=str(exc),
            runner_finished_at=now_text(),
            runner_lock=live_lock,
        )
        return 2

    # The with-block has exited here, therefore the OS-managed laboratory lock
    # has been released. Refresh the persisted snapshot so completed jobs do not
    # retain the earlier "active: true" acquisition-time observation.
    live_lock = probe_runner_lock(lab.campaign_root)
    final_state = (
        "stopped"
        if rc == 0 and "runner" in locals() and runner.campaign_stop_requested
        else ("completed" if rc == 0 else "failed")
    )
    update_job_file(
        job_file,
        state=final_state,
        exit_code=rc,
        runner_finished_at=now_text(),
        runner_lock=live_lock,
        operator_stop_events=(runner.operator_stop_events if "runner" in locals() else []),
    )
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
