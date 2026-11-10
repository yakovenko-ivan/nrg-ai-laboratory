# Windows 11 / WSL2 deployment and validation

This guide is the reproducible **control-plane + execution-plane** installation
procedure for one WSL2 Ubuntu instance: the external NRG CFD package and this
laboratory, side by side, with machine-level and (separately authorized)
single-case scientific acceptance checks.

Deployment to a **separate** Linux/HPC compute host is a different workflow: use
the v0.7 portable campaign export/import mechanism instead (see
[`user-guide.md`](user-guide.md) and [`release-v0.7.0.md`](release-v0.7.0.md)).

## Trust boundary of this document

Documentation only. Reading or following this guide grants **no** additional
agent permission.

- Every shell block below is an **operator-controlled Ubuntu shell**, not Pi's
  restricted read-only inspection shell.
- Building NRG is an operator action in the external NRG project. The laboratory
  repository never vendors, builds or modifies NRG.
- Step 8 (scientific acceptance) requires a **separate explicit authorization**.
  Nothing in this guide authorizes unattended CFD execution.
- `AGENTS.md` remains authoritative for what an agent may do. Existing campaigns,
  raw histories and studies are scientific records and are not part of this
  installation procedure.

## 1. Prerequisites

Install WSL2 and an Ubuntu distribution following current Microsoft
documentation, then work **inside** Ubuntu (not PowerShell). Prefer the Linux
home directory (`~/projects/...`) over `/mnt/c/...`: Windows-mounted paths slow
Fortran builds and high-I/O CFD output, and can produce confusing path behavior.

General development packages:

```bash
sudo apt update
sudo apt install -y git cmake ninja-build build-essential \
    gfortran python3 python3-venv python3-pip
```

For the validated **Intel** build, install Intel oneAPI Fortran (`ifx`) with its
runtime libraries using Intel's current Linux installation instructions. The
reference deployment used oneAPI compiler 2026.2 (`/opt/intel/oneapi`, `ifx` on
PATH after `setvars.sh`); do not assume that exact version is present on a new
machine.

A GNU `gfortran` build is supported by NRG and is useful for compiler
portability testing, but it is a **different executable** and requires its own
numerical acceptance; it is not interchangeable with an `ifx` build.

Install Node.js 20 or newer and the Pi coding-agent CLI per the Pi project's
documentation, and confirm `pi` resolves on `PATH` (Pi is not installed by
`pip`).

Record the actual versions:

```bash
uname -a
cat /etc/os-release
python3 --version
cmake --version
gfortran --version
node --version
command -v pi
```

## 2. Two independent Git checkouts

The laboratory and NRG are separate Git projects with separate provenance:

- `yakovenko-ivan/nrg-ai-laboratory` — campaign/analysis layer, trusted Pi
  bridge, project skills, configuration;
- `yakovenko-ivan/NRG` — Fortran solver, package interfaces, mechanism and
  thermophysical data.

The laboratory's committed defaults assume an **ignored** NRG checkout at
`.local/NRG`, which is a convention rather than a requirement:

```bash
mkdir -p ~/projects
cd ~/projects
git clone https://github.com/yakovenko-ivan/nrg-ai-laboratory.git
cd nrg-ai-laboratory
mkdir -p .local
git clone https://github.com/yakovenko-ivan/NRG.git .local/NRG
```

The validated reference deployment used the NRG branch
`refactor/core-solvers-validation`. Select that branch explicitly and record the
exact revisions you are building from:

```bash
git -C .local/NRG switch refactor/core-solvers-validation
git -C .local/NRG rev-parse HEAD
git -C .local/NRG status --short
git rev-parse HEAD
git status --short
```

A reproducible result is bound to source SHAs, compiled-binary hashes,
chemistry-resource hashes and calculation settings. Two clones at different
revisions are not equivalent even on the same branch, so prefer a reviewed commit
over a moving branch for repeated studies and do not copy a Windows `.exe` into
WSL.

### Validated revisions versus published revisions

The SHAs recorded with a deployment are the **validated pair for that deployment
evidence**. They say nothing about the current GitHub tip: a fresh public clone
resolves to whatever the published branch head is at clone time, which may be
newer or older than the validated commit. Record the SHAs you actually built,
compare them with any earlier record, and rerun the acceptance checks before
describing a new clone as reproducing a previous result.

The validated deployment behind this guide recorded:

```text
laboratory  562c501747074cd9011804f7bf6dfc2ae06e02ce   branch main
NRG         07be3cb34f5658806f080f0d5f783924ebb7fa5b   branch refactor/core-solvers-validation
```

The NRG SHA is the revision printed by that build in the validated case's
`problem_setup.log`; the laboratory SHA is the clean `main` revision recorded in
the same case's study provenance. Fresh public clones of either project are not
guaranteed to be at these revisions.

## 3. Intel oneAPI runtime environment

Initialize oneAPI **before** configuring/building NRG and before starting Pi:

```bash
source /opt/intel/oneapi/setvars.sh
command -v ifx
ifx --version
printf '%s\n' "$LD_LIBRARY_PATH" | tr ':' '\n'
```

Observed failure mode: 0-D case preparation failed with exit code 127 and
`libiomp5.so: cannot open shared object file`. A successful compile and a passing
`nrg-lab-doctor --require-nrg` do **not** prove dynamic-loader readiness, because
`nrg-lab-doctor` checks path existence and executable permission, not every
shared-library dependency (see
[`environment-diagnostics.md`](environment-diagnostics.md)). Sourcing
`setvars.sh` and restarting Pi **from that same initialized shell** resolved it.
Environment variables set in another terminal do not reach an already-running Pi
process.

If the library appears to be missing:

```bash
find /opt/intel/oneapi -type f -name libiomp5.so -print
```

Do not paper over the failure with system-wide symlinks or by copying runtime
libraries into a source tree; repair the oneAPI installation instead.

## 4. Python laboratory environment, Pi, optional Cantera

```bash
cd ~/projects/nrg-ai-laboratory
python3 scripts/bootstrap.py --dev --init-local-config
source .venv/bin/activate
```

`scripts/bootstrap.py` creates `.venv/`, installs this checkout in editable
mode, optionally creates `config/laboratory.local.toml` from the committed
example, and runs read-only diagnostics. It does **not** install Pi, Node.js, a
Fortran compiler or NRG, and it is safe to rerun.

The NRG Pi extension prefers `<repository>/.venv/bin/python`; `NRG_PYTHON` is
the explicit override when a different interpreter must be used.

Cantera is an **optional** independent reference environment, not a laboratory
dependency; `pyproject.toml` pins `cantera==3.2.0`:

```bash
python -m pip install -e '.[cantera]'

python -c "import cantera as ct; print(ct.__version__, ct.__file__)"
python -c "import cantera as ct; gas = ct.Solution('h2o2.yaml'); print(gas.n_species, gas.n_reactions)"
python -m cantera.ck2yaml --help
python -m pip check
```

`h2o2.yaml` only proves that the Cantera installation works; it is **not** the
curated KEROMNES mechanism used for NRG comparison. Do not upgrade the reference
dependency during a reproducibility test without recording a new environment.

Pi itself is started later, after the NRG builds and the machine-level
deployment checks (sections 5 and 7). Launching it now would register tools that
point at NRG executables which do not exist yet.

## 5. Build the NRG computing module and the 0D and 1D interfaces

NRG's `PACKAGE_INTERFACE_SOURCE` is a CMake **cache selector**: the target name
stays `package_interface`, and the executable is named
`package_interface_<source basename>`. One configured build tree therefore
produces exactly one interface binary. **Separate build directories are
recommended** for the computing module, the 0D interface and the 1D interface, so
that each binary stays independently selectable and rebuildable without
reconfiguring another build; the three-directory layout below is the one used by
the validated deployment.

NRG places executables under `${CMAKE_BINARY_DIR}/bin/$<CONFIG>`, so a
single-configuration Linux `Release` build still produces `bin/Release/`.
Selectors are relative to `package_interface/`.

```bash
cd ~/projects/nrg-ai-laboratory/.local/NRG

# CFD solver
cmake -S . -B build-ifx -DCMAKE_Fortran_COMPILER=ifx \
    -DCMAKE_BUILD_TYPE=Release
cmake --build build-ifx --target computing_module --parallel 4

# 0D constant-volume ignition campaign interface
cmake -S . -B build-ifx-0d -DCMAKE_Fortran_COMPILER=ifx \
    -DCMAKE_BUILD_TYPE=Release \
    -DPACKAGE_INTERFACE_SOURCE=src/tests/classic_tests/0D_ignition_delay_campaign.f90
cmake --build build-ifx-0d --target package_interface --parallel 4

# 1D wall/radial flame campaign interface
cmake -S . -B build-ifx-1d -DCMAKE_Fortran_COMPILER=ifx \
    -DCMAKE_BUILD_TYPE=Release \
    -DPACKAGE_INTERFACE_SOURCE=src/tests/classic_tests/1D_wall_flame_campaign.f90
cmake --build build-ifx-1d --target package_interface --parallel 4
```

Expected products:

```text
.local/NRG/build-ifx/bin/Release/computing_module
.local/NRG/build-ifx-0d/bin/Release/package_interface_0D_ignition_delay_campaign
.local/NRG/build-ifx-1d/bin/Release/package_interface_1D_wall_flame_campaign
```

Verify existence **and** dynamic linkage:

```bash
cd ~/projects/nrg-ai-laboratory
ls -l .local/NRG/build-ifx/bin/Release/computing_module
ls -l .local/NRG/build-ifx-0d/bin/Release/package_interface_0D_ignition_delay_campaign
ls -l .local/NRG/build-ifx-1d/bin/Release/package_interface_1D_wall_flame_campaign

ldd .local/NRG/build-ifx/bin/Release/computing_module
ldd .local/NRG/build-ifx-0d/bin/Release/package_interface_0D_ignition_delay_campaign
ldd .local/NRG/build-ifx-1d/bin/Release/package_interface_1D_wall_flame_campaign
```

**Stop if any `ldd` line reports `not found`.** Do not run a CFD smoke test
before the dynamic dependencies resolve. The 1D interface generator also invokes
`cmake -E` at execution time, so `cmake` must remain on `PATH`.

For a GNU build use separate `build-gfortran*` directories with
`-DCMAKE_Fortran_COMPILER=gfortran`. Never reuse a cache configured with another
compiler; if in doubt, configure a new build directory. See NRG's
`INSTALLATION.md` for the authoritative build-option reference (optional CVODE
and CGNS configurations, `NRG_ENABLE_OPENMP`, and stale-cache guidance).

## 6. Machine-local laboratory configuration

`config/laboratory.local.toml` is gitignored and **overlays**
`config/laboratory.toml`: omitted keys inherit, listed keys win. Keep unrelated
existing content (for example `[e0]` settings) intact; merge the entries below
rather than replacing the file.

The committed portable defaults assume `build/bin/...`. The WSL2 Intel layout
above produces `build-ifx*/bin/Release/...`, so the override is **required** for
this layout:

```toml
[paths]
task_setup_template = ".local/NRG/package_interface/task_setup"

[runtime]
computing_module = ".local/NRG/build-ifx/bin/Release/computing_module"
package_interface_0d = ".local/NRG/build-ifx-0d/bin/Release/package_interface_0D_ignition_delay_campaign"
package_interface_1d = ".local/NRG/build-ifx-1d/bin/Release/package_interface_1D_wall_flame_campaign"
```

Relative values resolve under `research_root` (the repository root). Absolute
machine-local paths work equally well and are often clearer.

`${NAME}` expansion reads **process environment variables** that must already be
exported before the laboratory (or Pi) starts; a TOML key does not define an
environment variable. See [`configuration.md`](configuration.md).

Create and inspect the overlay:

```bash
nrg-lab-config init-local     # only if it does not exist yet
nrg-lab-config show
nrg-lab-config validate
```

Whether `package_interface/task_setup/thermophysical_data/molar_masses.dat`
exists **depends on the NRG revision you built**: older task-setup trees shipped
it, and the revision used by the validated deployment does not. Inspect the tree
you actually have instead of assuming either way:

```bash
ls .local/NRG/package_interface/task_setup/thermophysical_data
```

Do not hand-author or restore that table to satisfy a diagnostic. The 0-D
campaign generator takes chemistry provenance from the mechanism, NASA thermo and
transport files that are actually present, and
`nrg_analysis.MolarMassDatabase.discover()` returns "unavailable" when the table
is absent — which correctly makes concentration-based radical diagnostics
unavailable instead of inventing molar masses. When a table is genuinely present
in your NRG revision, it is used as-is.

## 7. Machine-level acceptance checks (no CFD launched)

```bash
cd ~/projects/nrg-ai-laboratory
source .venv/bin/activate

python -m pytest -q
nrg-lab-config validate
nrg-lab-doctor
nrg-lab-doctor --require-nrg
nrg-lab lab-info

sha256sum \
  .local/NRG/build-ifx/bin/Release/computing_module \
  .local/NRG/build-ifx-0d/bin/Release/package_interface_0D_ignition_delay_campaign \
  .local/NRG/build-ifx-1d/bin/Release/package_interface_1D_wall_flame_campaign
```

Record actual exit codes and output. Do not require a fixed pytest case count
across versions. `nrg-lab-doctor` distinguishes `repository_ready` from
`nrg_runtime_ready`; without NRG, the expected state is
`repository_ready = true`, `nrg_runtime_ready = false`, no required failures.
`--strict` additionally fails on optional warnings (missing Git, Node.js/Pi,
external NRG).

Only now, with all three NRG binaries built and the checks above passing, start
Pi from the laboratory repository root in the same oneAPI-initialized shell:

```bash
cd ~/projects/nrg-ai-laboratory
pi
```

Within Pi, call `nrg_lab_info` and confirm the registered `nrg_*` tools, 0D/1D
`preparation_ready` and computing-module `execution_ready`. This is a
registration/smoke check and must not generate or run cases.

## 8. Single-case scientific acceptance (separately authorized)

Only after an explicit authorization to execute CFD. The validated reference
workflow used:

- H2-air with `X(H2) = 0.2`, `X(O2) = 0.16799664`, `X(N2) = 0.63200336`,
  `T0 = 1500 K`, `p0 = 101325 Pa`;
- closed, adiabatic, constant-volume uniform surrogate (2 cells,
  `dx = 1e-4 m`), CPM solver, SLATEC chemistry backend;
- the curated KEROMNES hydrogen-only mechanism, 11 species / 24 reaction
  channels, with verified NASA-7 thermodynamics and recorded data SHA-256;
- a trusted physical termination profile from
  [`config/termination_profiles.json`](../config/termination_profiles.json) —
  the reference run used `0d_cv_post_ignition_quasistationary_v2` (profile
  `_v1` is the stricter variant recorded in
  [`validation.md`](validation.md)). Never substitute the generic start path or a
  `wall_time` timeout when physical termination is intended.

Reference values from that one implementation, to be treated as **examples, not
bitwise targets**:

```text
tau(max dT/dt)   ~ 14.799 us   (NRG 0.5 us output cadence)
product state    T ~ 2802.372 K, p ~ 174704.9 Pa
Cantera 3.2.0    tau(max dT/dt) ~ 14.717 us on dense output
cadence-matched   difference ~ 0.0115 %  (output-sampling comparison)
```

The last line matters: most of the apparent NRG-vs-Cantera delay difference is
the discretisation of `max dT/dt` by the NRG output cadence, not a chemistry
difference. It is an output-sampling comparison, never a time-step convergence
proof.

These numbers come from the single validated deployment recorded in section 2.
They are **not** a property of the published repositories: a fresh public clone
must be pinned to a known revision and must reproduce the result independently
before it is described as reproducing it.

Sequence for a fresh machine:

```text
preview  -> generate   (trusted preview/generation only; review the case count)
status   -> prepare    (missing cases only)
start    -> physical start with the selected profile (authorized cases only)
verify   -> execution summary, quasistationary audit, case inspection
optional -> independent reference study (exact same mechanism, matching
            reactor assumptions, solver-tolerance and sampling sensitivity)
```

Manual forms of the same steps:

```bash
DEFINITION=0d_cv_h2_keromnes_1500K_1atm_pilot.toml          # under campaigns/definitions/
CASES=campaigns/0D_CV_H2_KEROMNES_T1500_P1atm_pilot_generated/cases.csv
PROFILE=0d_cv_post_ignition_quasistationary_v2

nrg-lab campaign-preview   --campaign "$DEFINITION"
nrg-lab campaign-generate  --campaign "$DEFINITION"
nrg-lab campaign-status    --cases "$CASES"
nrg-lab campaign-prepare   --cases "$CASES"
nrg-lab campaign-physical-plan  --cases "$CASES" --profile "$PROFILE"
nrg-lab campaign-physical-start --cases "$CASES" --profile "$PROFILE"
nrg-lab campaign-execution-summary --cases "$CASES"
nrg-lab campaign-physical-audit    --cases "$CASES" --profile "$PROFILE"
```

These are concrete, shell-safe examples: substitute your own definition name,
generated campaign directory and profile. Angle brackets are deliberately avoided
because an unquoted `<` or `>` is a shell redirection operator.

Accept scientific equivalence only after all four of:

1. chemistry-resource and executable provenance verified (hashes recorded);
2. a valid NRG termination reason, finite state and mass-fraction closure;
3. reference computation under **matching** reactor assumptions;
4. output-sampling and solver-tolerance sensitivity quantified.

The pilot campaign definition, generated state, studies and raw histories are
ignored research workspaces and are **not** present in a fresh clone. Import an
authorized portable definition/evidence bundle or construct a fresh reviewed
definition; never assume the historical pilot data are in Git, and never
overwrite or re-run existing raw cases to "reproduce" them.

This single 0-D test does not demonstrate transport accuracy, elemental
conservation, spatial homogeneity, chemical equilibrium, or experimental
agreement.

## 9. Prepared-campaign portability versus development environments

To run already-prepared cases on another Linux/HPC host, prefer the v0.7
portable bundle: the compute host needs Python 3.11+ and a compatible
`computing_module`, and does not need Pi or the package-interface executables.
Edit only `execution.local.toml`, then `portable_executor.py validate`, an
authorized `run`/`resume`, and `pack-results`; import results through the
verified laboratory path rather than copying files into `runs/`. Restart
checkpoints are raw binaries and are portable only between binary-compatible NRG
builds.

## 10. Troubleshooting

| Symptom | Likely cause | First safe check |
|---|---|---|
| `libiomp5.so: cannot open shared object file`, exit 127 | oneAPI runtime path not initialized or runtime missing | `source /opt/intel/oneapi/setvars.sh`, then `ldd`, then restart Pi |
| `nrg_runtime_ready = true` but case preparation fails | doctor checks path/permission, not every shared library | `ldd` the computing module and both interfaces |
| a previously built interface binary is missing after building the other one | both interfaces shared one build tree with one `PACKAGE_INTERFACE_SOURCE` cache value | use separate `build-ifx-0d` / `build-ifx-1d` directories (recommended) |
| `nrg-lab` cannot find the executable | local paths omit the `bin/Release` component | `nrg-lab-config show`, then `ls` the real build tree |
| executable hash differs from an old generated manifest | NRG was rebuilt or build options changed | regenerate only an unattempted campaign after provenance/overwrite review |
| `molar_masses.dat` missing | NRG-revision dependent: the validated revision does not ship it | inspect `task_setup/thermophysical_data`; do not hand-author the table |
| Cantera import fails | optional reference extra not installed in `.venv` | `python -m pip install -e '.[cantera]'` and rerun the version smoke test |
| `cmake: command not found` when running the 1D interface | portable generator calls `cmake -E` at execution time | install CMake and keep it on `PATH` |
| poor build throughput or odd path behavior | working under `/mnt/c/...` | rebuild/run under the WSL2 Linux home |
| `ifx` not found after a shell restart | oneAPI not initialized in this shell | `source setvars.sh` again |

## 11. Deployment record

Keep one short redacted record per tested machine outside raw-run directories,
attached to release evidence rather than committed with personal paths:

```text
Date / machine alias / Ubuntu and WSL versions:
Laboratory Git commit, branch, clean/dirty:
NRG Git commit, branch, clean/dirty:
Compiler (ifx version) and oneAPI runtime path, or gfortran:
CMake / Python / Node / Pi / Cantera versions:
NRG build options and package-interface source selectors:
computing_module / 0D / 1D executable SHA-256:
Mechanism + thermo + transport resource SHA-256:
Local configuration SHA-256 (content stays private):
ldd checks: all dependencies resolved? [yes/no]
pytest / config validate / doctor --require-nrg / lab-info results:
Pi NRG-tool registration results:
Optional scientific smoke test: case ID, termination reason, metrics, error vs reference:
Known limitations and follow-up:
```

Never commit local path overrides, executables, simulation histories, tokens or
proprietary data.

## Related documentation

- [`installation.md`](installation.md) — bootstrap and prerequisites
- [`configuration.md`](configuration.md) — configuration layering and paths
- [`nrg-integration.md`](nrg-integration.md) — external NRG integration boundary
- [`environment-diagnostics.md`](environment-diagnostics.md) — doctor semantics
- [`pi-integration.md`](pi-integration.md) — Pi extension and tools
- [`validation.md`](validation.md) — recorded acceptance evidence
- [`release-v0.7.0.md`](release-v0.7.0.md) — portable export/import
- NRG `INSTALLATION.md` and `TUTORIAL.md` — authoritative Fortran build options
