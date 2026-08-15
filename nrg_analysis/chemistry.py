"""Species and composition utilities used by study-specific analyses."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence


@dataclass(frozen=True)
class MolarMassDatabase:
    """Molar masses indexed case-insensitively.

    NRG's ``molar_masses.dat`` values are in kg/kmol numerically equivalent to
    g/mol.  For concentration ``rho*Y/M`` with rho in kg/m3, the result is
    kmol/m3 when M is in kg/kmol.
    """

    values_kg_per_kmol: Mapping[str, float]
    source: Path | None = None

    @classmethod
    def from_file(cls, path: str | Path) -> "MolarMassDatabase":
        path = Path(path)
        values: dict[str, float] = {}
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith(("#", "!")):
                continue
            parts = stripped.split()
            if len(parts) < 2:
                continue
            try:
                mass = float(parts[1].replace("D", "E").replace("d", "e"))
            except ValueError:
                continue
            if mass > 0.0:
                values[parts[0].upper()] = mass
        if not values:
            raise ValueError(f"no positive molar masses parsed from {path}")
        return cls(values, path.resolve())

    @classmethod
    def discover(cls, case_path: str | Path) -> "MolarMassDatabase | None":
        case_path = Path(case_path)
        candidates = (
            case_path / "task_setup" / "thermophysical_data" / "molar_masses.dat",
            case_path / "task_setup" / "molar_masses.dat",
        )
        for path in candidates:
            if path.exists():
                return cls.from_file(path)
        return None

    def get(self, species: str) -> float:
        try:
            return self.values_kg_per_kmol[species.upper()]
        except KeyError as exc:
            raise KeyError(f"molar mass unavailable for species {species!r}") from exc

    def has(self, species: str) -> bool:
        return species.upper() in self.values_kg_per_kmol


def mass_to_mole_fractions(
    mass_fractions: Mapping[str, float], molar_masses: MolarMassDatabase
) -> dict[str, float]:
    amounts: dict[str, float] = {}
    for species, y in mass_fractions.items():
        if not molar_masses.has(species):
            continue
        amounts[species] = max(float(y), 0.0) / molar_masses.get(species)
    total = sum(amounts.values())
    if total <= 0.0:
        return {}
    return {species: amount / total for species, amount in amounts.items()}


def molar_concentration(
    density_kg_m3: Sequence[float],
    mass_fraction: Sequence[float],
    species: str,
    molar_masses: MolarMassDatabase,
) -> list[float]:
    if len(density_kg_m3) != len(mass_fraction):
        raise ValueError("density and mass-fraction arrays have different lengths")
    mass = molar_masses.get(species)
    return [rho * y / mass for rho, y in zip(density_kg_m3, mass_fraction)]
