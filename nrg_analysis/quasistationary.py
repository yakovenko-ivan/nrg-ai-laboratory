"""Deterministic post-ignition quasistationarity evaluation for 0D reactors.

Public compatibility wrapper around :mod:`nrg_analysis.quasistationary_core`.
The core contains the canonical mathematics shared with the portable executor;
this module preserves the established laboratory API and existing NRG I/O path.
"""

from __future__ import annotations

from pathlib import Path

from .io import ReactorHistory, load_reactor_history
from .quasistationary_core import (
    CoreProfile as QuasistationaryProfile,
    CoreResult as QuasistationaryResult,
    evaluate as _core_evaluate,
    load_profile,
    load_profiles,
    profile_file_sha256,
    profile_sha256,
)


def evaluate_history(
    history: ReactorHistory,
    profile: QuasistationaryProfile,
) -> QuasistationaryResult:
    """Evaluate an already parsed laboratory reactor history using the canonical core."""
    species_names = tuple(history.species_names)
    species_series = {
        name: history.species_mass_fraction(name)
        for name in species_names
    }
    return _core_evaluate(
        time_s=history.time_s,
        temperature_K=history.temperature_K,
        pressure_Pa=history.pressure_Pa,
        density_kg_m3=history.density_kg_m3,
        species_names=species_names,
        species_series=species_series,
        profile=profile,
    )


def evaluate_case(
    case_path: str | Path,
    profile: QuasistationaryProfile,
) -> QuasistationaryResult:
    history = load_reactor_history(case_path, profile.history_file)
    return evaluate_history(history, profile)


__all__ = [
    "QuasistationaryProfile",
    "QuasistationaryResult",
    "evaluate_history",
    "evaluate_case",
    "load_profile",
    "load_profiles",
    "profile_file_sha256",
    "profile_sha256",
]
