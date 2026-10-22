# NRG AI Laboratory — E0 Mode 3 minimum implementation brief

**Prepared:** 2026-10-18  
**Status:** proposed, **not authorized for implementation**.  
**Purpose:** enable an agent-operated, fresh KEROMNES NRG–Cantera validation with the existing V3 interface and archived validation harness. The manual six-mechanism suite is historical evidence, not an agentic execution.

## 1. Current verified facts

- The Pi multi-agent smoke test passed for `nrg-evidence` → `nrg-scientist`, and the conditional `nrg-evidence` → `nrg-architect` gate also passed; runtime metadata confirmed the configured models and separate contexts.
- The local read-only audit found the equilibrium binary missing; the existing CMake cache selects `1D_laminar_velocity.f90`. Ordinary Pi policy prohibits compiling or editing NRG. The NRG interface already contains `NRG_CHEMKIN_VALIDATION_AUDIT_V3`.
- Inspected remote NRG commit: `f198fa8ad0085bf995feb0dce06b6f79481db7b4`. The laboratory baseline at the 2026-10-22 review was `feature/mode1-interface-lifecycle`; the exact consolidated HEAD must be recorded after operator commits. Reconfirm **actual local HEAD, dirty state, and file hashes** before work; never substitute a remote revision for local evidence.
- Historical `NRG_Cantera_equilibrium.zip` contains `tools/run_validation.py`, including `--mode input|cantera|full`, `--only`, V3 checks and Cantera `ck2yaml` conversion. In `full` mode it already runs **both** NRG and Cantera, performs scientific comparisons, and writes `result.json`, per-mechanism outputs and a summary. Avoid building a second comparator.
- Current harness `run_nrg()` passes `cwd=nrg/'package_interface'`. The Fortran classes write `task_setup/chemical_data.inf` and `task_setup/thermophysical_setup.inf` relative to cwd: an isolated execution directory is required to avoid mutating the NRG source checkout.
- Historical/reference and current input-data hashes must be compared. Previous audit found KEROMNES mechanism and thermo identical, but KEROMNES transport differs; do not assume bitwise historical comparability without per-file evidence.

## 2. Mission contract

**Scientific question:** Can the authorized laboratory agent independently reproduce the established KEROMNES TP/HP equilibrium agreement with Cantera while preserving source identity and all raw evidence?

**Cases:** KEROMNES H2–air; TP 1500 K and 101325 Pa; HP initially 300 K and 101325 Pa; H2:O2:N2 = 2:1:3.762. No crossover mode. Reuse the authoritative validation configuration and mechanism manifest.

**Primary deliverable:** a *fresh* complete-mode KEROMNES `result.json`, raw NRG diagnostics, independent Cantera reference, comparison, source/data/executable hashes, exit codes, logs, and a concise `report.md` containing the scientific assessment. Do not accept an `input` or `cantera`-only PASS as full E0 success.

## 3. Minimal implementation boundary

**A. Authorized build.** A narrow tool similar to `nrg_package_interface_build`:

1. Read an explicitly configured local NRG checkout; require approved source file identity and a clean or explicitly permitted checkout. Do not alter NRG computing-core or package-interface source.
2. Select only `src/tests/classic_tests/0D_chemical_equilibrium_validation.f90` using the existing CMake `PACKAGE_INTERFACE_SOURCE` selector.
3. Use an isolated build directory, never reuse/reconfigure the researcher's active `build/` cache. Detect generator/compiler and CMake config (`Debug`/`Release`) from actual build settings. Record source revision, relevant hashes, compiler version, CMake cache/options, selected source, executable hash, stdout/stderr and exit code.
4. Never invoke `tools/install_audit.py` by default; V3 instrumentation already exists. If a new source change is actually required, propose it for **researcher review before build**.

**B. Authorized validation execution.** Prefer a narrow tool similar to `nrg_equilibrium_validation_run` **wrapping the existing `run_validation.py --mode full`**, rather than separate homegrown NRG and Cantera comparison logic:

1. Version the reviewed reusable harness source under a controlled lab component; retain the historical archive read-only under an ignored research directory. Freeze/record SHA-256 of the harness and validation config.
2. Resolve the approved executable by recorded binary hash; resolve the approved input mechanism, thermo and transport files and record hashes.
3. Stage the necessary `task_setup/chemical_mechanisms/...` and `task_setup/thermophysical_data/...` in a fresh **short-path** scratch working directory. Set the Fortran process cwd there; adapt the harness with a minimal explicit workdir parameter. Keep the original NRG checkout read-only. Check path buffer limits (the Fortran `output_prefix` is 128 characters).
4. Run the reviewed harness with `--only hydrogen/KEROMNES.txt --mode full`, capture the exact Cantera/Python version, YAML conversion warnings, all stdout/stderr, return codes, `result.json`, detailed comparisons and raw diagnostic CSVs. Use fixed tool input shapes, not arbitrary shell-command parameters. Reject output-path reuse unless explicitly approved.
5. Check failure handling: mismatched executable hash/source identity, absent Cantera, missing input, parser/chemistry exceptions, failed Cantera conversion, nonzero exit, unexpected outputs, and timeout must produce **FAILED/BLOCKED**, never a synthetic PASS.
6. Run independent numerical/physical acceptance checks on the resulting data: convergence, finite/nonnegative mole fractions, normalized sum, element and mass conservation, HP enthalpy residual, Gibbs stationarity, TP/HP mole-fraction discrepancies and HP equilibrium-temperature discrepancy. Existing PASS screening does not itself threshold *all* diagnostics; report them separately and apply **predeclared** tolerances.

**C. Authorization.** Changing trusted `.pi/extensions/`, `agent_workspace/lab_bridge.py`, or associated policy/configuration requires **separately approved Mode 3 development**. Ordinary Pi or subagent session access does not confer these permissions. Existing `nrg_dev_*` lab transactions do not automatically cover protected extension/bridge source. Do not bypass restrictions by invoking Python via a study runner or by changing tool arguments.

## 4. Scientific acceptance and limits

- A full E0 PASS requires a **fresh** matched-data NRG–Cantera comparison; archived six-mechanism PASS outcomes are historical references only.
- Golden-baseline comparisons require input-file/hash equivalence. If the transport file changed, note it and compare the thermodynamics and equilibrium separately from the broader transport audit.
- Bitwise identical results are **not** a universal acceptance requirement across compilers, platforms, or numerical libraries. Within a pinned environment, repeated results should be numerically reproducible; test invariants and predeclared error tolerances. A binary hash does not replace source-data provenance.
- Show separate verdicts for **scientific validity**, **agentic completeness**, and **portability**.
- Do not build general OS-level privilege machinery or introduce persistent special accounts or worktrees merely to run E0.

## 5. Minimal test ladder

1. Static unit tests for approval checks, path containment, source/hash checks and fixed command construction.
2. A scratch directory dry-run with a stub executable (no NRG) confirming cwd isolation and diagnostic parsing; test failure modes.
3. Authorized real CMake build of unmodified V3 interface. Verify binary and clean checkout.
4. Fresh, newly named KEROMNES full-mode NRG–Cantera run. Verify independent diagnostics, no writes to authoritative NRG checkout, and provenance.
5. One comparable scientific rerun under identical inputs; report numerical differences and environment versions.
6. Only after success consider six-mechanism regression and WSL2 clean-clone portability.

## 6. Work allocation

- LOW evidence model: inspect current local environment, hashes, source diffs, status and assemble manifest; use deterministic tools to compute hashes.
- MEDIUM implementation model: draft narrow implementation and tests under a separately approved Mode 3 scope; never self-approve patches.
- HIGH architect: already delivered the design. Re-invoke only for an unresolved architectural conflict or substantive new constraint.
- HIGH scientific reviewer: evaluate final physical/numerical evidence; do not use it to prepare routine files.

**Next approval requested:** authorize a *scoped Mode 3 implementation proposal* (design/patched-code review), not yet compilation or scientific execution. The researcher retains approval of protected laboratory changes and any changed executable Fortran source.
