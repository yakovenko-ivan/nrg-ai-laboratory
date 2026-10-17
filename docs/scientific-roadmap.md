# Scientific-first implementation roadmap

**Baseline inspected 2026-10-16.** This document distinguishes present source-code functionality from aspirations and unverified end-to-end operation. It does not declare a new release or a completed campaign.

## Present repository (published v0.7.0)

- Portable Python package, configuration/diagnostics, Pi extension, trusted campaign execution, provenance, export/import, and reusable analysis.
- Existing **0D ignition** and **1D wall-flame** campaign generators, runner, tests and research-study templates.
- Lab-only guarded development transaction tools exist, but ordinary Pi sessions still prohibit editing/building NRG and arbitrary trusted-source changes (see `AGENTS.md`). **Agent-managed Mode 1 interface building and Mode 2 NRG core development are not yet operationally authorized.**
- `nrg_analysis/equilibrium.py` assesses **reactor history stationarity**, not thermodynamic Gibbs equilibrium.

## Present NRG upstream

The NRG branch `refactor/core-solvers-validation` contains `package_interface/src/tests/classic_tests/0D_chemical_equilibrium_validation.f90`, `package_interface/task_setup/equilibrium_validation.nml`, and `package_library/src/chemical_equilibrium_class.f90`. The interface supports TP/HP equilibrium and separately kinetic crossover temperature. In the branch inspected 2026-10-16, the namelist mechanism path is `hydrogen/KEROMNES.txt`. Inspect the current checkout before running. Existence of source is **not evidence of a successful installation, Cantera verification or agent-operated workflow**.

## Acceptance milestones

| Gate | Observable acceptance result | Status |
|---|---|---|
| G0: portable baseline | Independently clone, install and build both repositories on fresh Xubuntu/WSL2; pass appropriate tests | To demonstrate on ASUS/WSL2 |
| E0: Mode 1 equilibrium | Agent runs NRG TP/HP and matched-thermo Cantera cases and publishes validated comparison, plots and provenance | First priority; not yet demonstrated |
| E1: equilibrium campaign | Sweep temperatures/pressures/compositions by namelists without recompilation | Planned |
| M1-0D | Homogeneous reactor kinetics verification and performance benchmarks | Existing components; updated acceptance pending |
| M1-1D | Inlet/wall-flame and shock-tube workflows with references, convergence and reports | Partial components exist; broader mission pending |
| M2 first loop | Researcher-reviewed NRG core candidate autonomously compiled/tested/analyzed in local worktree | Planned |
| Portable integrated release | Reproduce Mode 1/2 on new WSL2 install with Mode 3 used to maintain tooling | Planned |

G0 and E0 may proceed in parallel. Prioritize urgent science rather than treating all stages as hard prerequisites.

## Known capability gaps to verify in a live attempt

1. The current 0D campaign generator has a fixed ignition-reactor namelist schema; NRG equilibrium uses a different `/equilibrium_validation/` schema. Begin with a narrow adapter or direct runner; generalize only if observed campaign needs justify it.
2. Ordinary Pi agent restrictions prohibit NRG editing/compilation. Implement an expressly approved, auditable execution path rather than bypassing existing tool policy.
3. Relative runtime defaults may differ from actual CMake output layout (e.g. `build/bin/Release`); verify on both target environments.
4. Quantitative Cantera equilibrium comparison requires matching species and standard-state thermochemistry, not an arbitrary stock mechanism.
5. Mode 2 requires a source-review boundary plus repeatable accuracy/performance benchmarks, not merely a patch-generation tool.

For any change record the blocked scientific task, actual error, narrowest remedy, new capability, and observable acceptance test.

## Research evidence and portability

A completed scientific mission preserves inputs, source version, binary hash, thermo/mechanism data hashes, output, exit status, physical invariants, reference scripts and exact phase, figures and concise limitations/report. Results must not be claimed verified merely because scripts exited successfully. Keep raw data and machine-local configurations out of the reusable Git repository.
