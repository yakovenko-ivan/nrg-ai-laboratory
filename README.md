# NRG AI Laboratory Assistant

NRG AI Laboratory Assistant is a portable, controlled **agentic research layer
for the NRG CFD package**.  It does not contain the NRG solver itself.  NRG
source code, physical models, package interfaces, task-setup resources, and
compiled executables remain owned and built in the upstream
[yakovenko-ivan/NRG](https://github.com/yakovenko-ivan/NRG) project.

This repository supplies the Pi-facing laboratory tools, deterministic campaign
orchestration, trusted execution/provenance logic, reusable Python analysis, and
scientific-study workflow needed to conduct CFD/combustion research with NRG.

Current portable-repository release candidate: **v0.6.0**.

## Architecture

```text
NRG repository (external)
  -> Fortran sources, models, task_setup, package interfaces, executables

NRG AI Laboratory Assistant (this repository)
  AGENTS.md
    -> laboratory policy and permissions

  .pi/skills/
    -> reusable scientific/procedural knowledge

  .pi/extensions/nrg-laboratory/
    -> trusted Pi-facing NRG tools

  campaign_tools/
    -> deterministic campaign generation/execution/provenance mechanics

  nrg_analysis/
    -> reusable analysis and laboratory configuration primitives

  agent_workspace/studies/_template/
    -> controlled template for hypothesis-specific local studies
```

The trusted campaign, CFD execution, physical-termination, operator-stop,
provenance, restricted-shell, and Tecplot behavior from the validated v0.5.x
laboratory is preserved.  v0.6 changes deployment and repository boundaries;
it also incorporates the v0.5.9 staged large-study analysis workflow without
changing the CFD execution model.

## External NRG dependency

A fresh clone intentionally contains **no NRG executable and no copied NRG
`task_setup` tree**.  Build NRG independently, then point the local laboratory
configuration to the resulting resources.

For convenience, the committed defaults assume an ignored local checkout at:

```text
<assistant-repository>/.local/NRG
```

but NRG may live anywhere.  Machine-specific paths belong in:

```text
config/laboratory.local.toml
```

See `docs/nrg-integration.md` and `docs/configuration.md`.

## Bootstrap

From a fresh clone:

```bash
python3 scripts/bootstrap.py --dev
```

This creates `.venv/`, installs the Python packages in editable mode, and runs
read-only repository diagnostics.  It does **not** install or build NRG, Pi,
Node.js, or a Fortran compiler.

Then configure the external NRG installation:

```bash
nrg-lab-config init-local
# edit config/laboratory.local.toml
nrg-lab-config validate
nrg-lab-doctor --require-nrg
```

See `docs/installation.md`.

## Installed Python commands

```bash
nrg-lab ...
nrg-lab-config show
nrg-lab-config validate
nrg-lab-doctor
```

`nrg-lab-doctor` distinguishes source-repository readiness from external NRG
runtime readiness.  This allows bootstrap and repository tests to succeed on a
machine where NRG has not yet been built, while `--require-nrg` performs the
execution-readiness check.

## Scientific workspace

Active research artifacts are deliberately local and ignored by Git:

```text
campaigns/
runs/
agent_workspace/studies/<study>/
```

The repository therefore contains no current campaign and no user-specific
study.  Only the controlled study template remains committed.

See `docs/repository-policy.md` for the exact Git/research-artifact boundary.

## Staged analysis for large studies

The v0.6.0 release incorporates the v0.5.9 staged-study analysis patch.  For
campaigns larger than 50 cases, newly written or materially changed study
analysis must be validated on a bounded representative pilot before trusted
full-campaign execution.  Pi exposes:

```text
nrg_study_pilot_plan
nrg_run_study_pilot
nrg_run_study
```

Pilot selection is deterministic and uses logical-identity-space coverage rather
than simply taking the first N cases.  Pilot validation is tied to the exact
`analyze.py`, `analysis_config.toml`, and campaign `cases.csv` hashes.  See the
`nrg-study-analysis` skill and `docs/staged-study-analysis-v0.5.9-migration.md`.

## Validation

```bash
python -m pytest -q
nrg-lab-doctor
```

After configuring an NRG build:

```bash
nrg-lab-doctor --require-nrg
nrg-lab-config validate
```

See `docs/validation.md`, `docs/release-v0.6.0.md`, and `docs/repository-creation-checklist.md`.
