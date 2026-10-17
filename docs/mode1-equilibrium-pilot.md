# Mode 1 E0 acceptance mission — NRG equilibrium versus Cantera

**Status:** proposed first scientific acceptance test, **not yet executed or validated**.

## Purpose

Demonstrate a complete prompt → select/approve interface → compile → namelist → NRG run → Cantera comparison → physical checks → plots and report workflow, before building additional infrastructure.

NRG source already contains:

- `package_interface/src/tests/classic_tests/0D_chemical_equilibrium_validation.f90`
- `package_interface/task_setup/equilibrium_validation.nml`
- `package_library/src/chemical_equilibrium_class.f90`

Inspect the exact NRG checkout. The source selector is `-DPACKAGE_INTERFACE_SOURCE=src/tests/classic_tests/0D_chemical_equilibrium_validation.f90`. The inspected current namelist uses `mechanism_file='hydrogen/KEROMNES.txt'`. Avoid assuming an older flat mechanism path.

## First two cases

- **TP:** T = 1500 K, P = 101325 Pa, molar initial mixture `H2:O2:N2=2:1:3.762`, `mode='tp'`; compare all equilibrium species mole fractions.
- **HP:** T0 = 300 K, P = 101325 Pa, same mixture, `mode='hp'`; compare equilibrium species composition and adiabatic temperature, as well as initial/final enthalpy.

The default `mode='all'` also runs a kinetic chain-branching crossover diagnostic, **not** a Gibbs-equilibrium result; exclude crossover from this pilot.

## Thermochemistry is the principal scientific validation requirement

Equilibrium depends on species Gibbs free energies. Build an independent Cantera ideal-gas phase with the same admissible NRG species, elemental compositions, molecular weights and matching NASA thermodynamic data where possible. Verify `cp°(T)`, `h°(T)`, `s°(T)` and pressure conventions before comparing. A stock Cantera H2 mechanism with differing thermochemical data is only a qualitative check, not quantitative validation.

Cantera reference: set `gas.TPX=(T,P,composition)` and call `gas.equilibrate('TP')` or `gas.equilibrate('HP')` on a **verified matched phase**.

## Minimal agent procedure

1. Read-only inspection of actual NRG and laboratory commits, files, compiler/build configuration, Cantera availability, task-setup paths and current permissions.
2. Prefer the committed Fortran equilibrium interface unchanged. Build it with the standard NRG CMake selector from a separate build directory, through a **user-authorized workflow**, and run from a scratch directory outside tracked sources.
3. Generate two explicit `/equilibrium_validation/` namelists. Save stdout, stderr, status, input summaries, species CSVs and solver diagnostics.
4. Prepare a reproducible matched-thermo Cantera phase and independent script; record version and data hashes.
5. Analyze convergence flags, stationarity residual, element/mass conservation, nonnegative fractions and fraction sums; check **HP enthalpy conservation**, TP species distribution and HP equilibrium temperature. Distinguish major-species absolute error and qualified trace-species metrics.
6. Create `comparison.csv`, plot(s), input/binary/source manifest and `report.md`. Explain discrepancies and limitations; do not tune tolerances retrospectively.
7. Only after first baseline success, consider TP 1000/1500/2500 K, HP 300/500 K, and a pressure sensitivity case if thermo validity allows.

## Operational gaps, if observed

The existing `campaign_generator_0d.py` targets ignition-reactor namelists, not `/equilibrium_validation/`; `nrg_analysis/equilibrium.py` is about time-history stationarity. Existing ordinary Pi policy prohibits compiling or editing NRG. Do **not** circumvent policy with alternate shell syntax. Use an expressly authorized workflow and introduce the smallest missing tool only if the live calculation is blocked.

## Acceptance gate

E0 passes only if an authorized agent produces reproducible TP and HP NRG results; verifies a scientifically matched Cantera reference; reports physical invariants, numerical diagnostics, quantitative comparisons, plots and provenance; and makes no NRG computing-core or unrelated OS infrastructure changes. If matching thermo cannot be established, mark the comparison **unvalidated** despite successful numerical execution.
