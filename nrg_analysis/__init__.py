"""Reusable analysis primitives for NRG campaign results.

The package intentionally contains no study-specific policy.  It provides
parsers, chemistry conversions, time-series operations, ignition metrics,
equilibrium diagnostics, validation, campaign access, and provenance helpers.
Study-specific scientific logic belongs in ``agent_workspace/studies``.
"""

from .campaign import Campaign, CaseRecord
from .chemistry import MolarMassDatabase, mass_to_mole_fractions, molar_concentration
from .equilibrium import EquilibriumAssessment, EquilibriumCriteria, assess_equilibrium
from .ignition import IgnitionMetric, default_ignition_suite
from .io import ReactorHistory, load_reactor_history
from .laboratory import Laboratory
from .validation import HistoryValidation, validate_history

__all__ = [
    "Campaign",
    "CaseRecord",
    "MolarMassDatabase",
    "mass_to_mole_fractions",
    "molar_concentration",
    "EquilibriumAssessment",
    "EquilibriumCriteria",
    "assess_equilibrium",
    "IgnitionMetric",
    "default_ignition_suite",
    "ReactorHistory",
    "load_reactor_history",
    "Laboratory",
    "HistoryValidation",
    "validate_history",
]

from ._version import __version__
