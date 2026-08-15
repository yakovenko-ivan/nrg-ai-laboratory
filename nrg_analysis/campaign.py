"""Campaign manifest access without embedding problem-specific assumptions."""

from __future__ import annotations

from dataclasses import dataclass
import csv
from pathlib import Path
from typing import Iterator, Mapping

from .io import ReactorHistory, load_reactor_history


@dataclass(frozen=True)
class CaseRecord:
    row: Mapping[str, str]
    case_root: Path

    @property
    def case_id(self) -> str:
        return self.row.get("case_id", "")

    @property
    def fingerprint(self) -> str:
        return self.row.get("case_fingerprint", "")

    @property
    def case_path(self) -> Path:
        value = Path(self.row["case_path"]).expanduser()
        if value.is_absolute():
            return value.resolve()
        return (self.case_root / value).resolve()

    @property
    def workspace_root(self) -> Path:
        """Backward-compatible alias for pre-laboratory study code."""

        return self.case_root

    def value(self, key: str, default: str = "") -> str:
        return self.row.get(key, default)

    def float_value(self, key: str) -> float | None:
        value = self.row.get(key, "")
        if value == "":
            return None
        try:
            return float(value)
        except ValueError:
            return None

    def int_value(self, key: str) -> int | None:
        value = self.row.get(key, "")
        if value == "":
            return None
        try:
            return int(value)
        except ValueError:
            return None

    def history(self, output_file: str = "reactor_history.dat") -> ReactorHistory:
        return load_reactor_history(self.case_path, output_file=output_file)


@dataclass(frozen=True)
class Campaign:
    cases_csv: Path
    case_root: Path
    rows: tuple[CaseRecord, ...]

    @classmethod
    def load(
        cls,
        cases_csv: str | Path,
        case_root: str | Path | None = None,
        *,
        workspace_root: str | Path | None = None,
    ) -> "Campaign":
        """Load a campaign manifest.

        New manifests should contain absolute ``case_path`` values, in which case
        ``case_root`` is only a harmless fallback.  Older manifests containing
        relative paths remain supported through ``case_root`` or the legacy
        keyword ``workspace_root``.
        """

        if case_root is not None and workspace_root is not None:
            raise ValueError("specify case_root or workspace_root, not both")
        if workspace_root is not None:
            case_root = workspace_root
        if case_root is None:
            case_root = "."

        cases_csv = Path(cases_csv).expanduser().resolve()
        case_root_path = Path(case_root).expanduser().resolve()
        with cases_csv.open(newline="", encoding="utf-8") as stream:
            reader = csv.DictReader(stream)
            if reader.fieldnames is None:
                raise ValueError(f"manifest has no header: {cases_csv}")
            required = {"case_id", "case_path"}
            missing = required.difference(reader.fieldnames)
            if missing:
                raise ValueError(f"manifest missing required columns: {sorted(missing)}")
            rows = tuple(CaseRecord(dict(row), case_root_path) for row in reader)
        ids = [case.case_id for case in rows]
        if len(set(ids)) != len(ids):
            raise ValueError("case_id values in manifest are not unique")
        return cls(cases_csv=cases_csv, case_root=case_root_path, rows=rows)

    @property
    def workspace_root(self) -> Path:
        """Backward-compatible alias for pre-laboratory callers."""

        return self.case_root

    def __iter__(self) -> Iterator[CaseRecord]:
        return iter(self.rows)

    def __len__(self) -> int:
        return len(self.rows)

    def case(self, case_id: str) -> CaseRecord:
        for case in self.rows:
            if case.case_id == case_id:
                return case
        raise KeyError(case_id)
