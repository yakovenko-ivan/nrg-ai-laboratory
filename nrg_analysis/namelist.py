"""Small, deliberately conservative helpers for NRG Fortran namelist files.

This is not intended to be a general Fortran namelist parser.  It only reads
simple scalar assignments and repeated ``field_name`` entries emitted by NRG.
Keeping this parser narrow makes failures explicit rather than silently
misinterpreting arbitrary namelist syntax.
"""

from __future__ import annotations

import re
from pathlib import Path


def string_value(text: str, name: str) -> str | None:
    pattern = re.compile(
        rf"^\s*{re.escape(name)}\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([^,/\r\n]+))",
        re.IGNORECASE | re.MULTILINE,
    )
    match = pattern.search(text)
    if not match:
        return None
    for group in match.groups():
        if group is not None:
            return group.strip()
    return None


def int_value(text: str, name: str) -> int | None:
    value = string_value(text, name)
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def float_value(text: str, name: str) -> float | None:
    value = string_value(text, name)
    if value is None:
        return None
    try:
        return float(value.replace("D", "E").replace("d", "e"))
    except ValueError:
        return None


def bool_value(text: str, name: str) -> bool | None:
    value = string_value(text, name)
    if value is None:
        return None
    normalized = value.strip().lower().strip(".")
    if normalized in {"true", "t"}:
        return True
    if normalized in {"false", "f"}:
        return False
    return None


def repeated_quoted_values(text: str, name: str) -> list[str]:
    pattern = re.compile(
        rf"^\s*{re.escape(name)}\s*=\s*(?:\"([^\"]*)\"|'([^']*)')",
        re.IGNORECASE | re.MULTILINE,
    )
    values: list[str] = []
    for match in pattern.finditer(text):
        values.append(next(group for group in match.groups() if group is not None).strip())
    return values


def read(path: str | Path) -> str:
    return Path(path).read_text(encoding="utf-8", errors="replace")
