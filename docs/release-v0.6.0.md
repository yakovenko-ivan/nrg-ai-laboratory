# NRG AI Laboratory Assistant v0.6.0

## Purpose

v0.6.0 converts the previously machine-specific NRG AI Laboratory installation
into a portable source repository that can be cloned independently and attached
to a separately built NRG CFD installation.

## Main changes

- repository-internal resources resolve relative to the repository/configuration
  location rather than a developer machine path;
- committed portable `config/laboratory.toml` is separated from ignored
  `config/laboratory.local.toml` machine overrides;
- environment-variable path expansion and documented configuration selectors
  are supported;
- Python packaging covers `nrg_analysis`, `agent_workspace`, and
  `campaign_tools` with a single project version source;
- bootstrap, configuration, and environment-diagnostic commands are provided;
- project-local Pi extension and skills remain clone-local resources;
- NRG source, executables, package interfaces, and `task_setup` are external
  dependencies and are never vendored here;
- campaigns, runs, and user-specific studies are local scientific artifacts and
  are ignored by Git;
- project documentation is organized under `docs/` rather than Pi skill
  discovery directories;
- Git hygiene and portability regression tests protect the repository boundary.
- the v0.5.9 staged-study analysis patch is integrated: large studies use
  deterministic representative pilot planning, pilot execution with hash-bound
  validation, a production guard, and a shared per-case cache contract.

## Compatibility principle

The release intentionally preserves the trusted v0.5.x CFD execution model:
logical campaign identity, attempts, single-runner/single-case execution,
physical termination, operator stop behavior, reset/append semantics,
provenance separation, restricted shell inspection, and plotting/Tecplot
conventions.  Study execution retains the same raw-data/provenance protection
but incorporates the v0.5.9 staged large-study workflow before production
analysis.

## Acceptance status

The release candidate passed a real clean-install test connected to an external
NRG build.  A Pi-driven six-case 0-D KEROMNES ignition campaign was designed and
executed using campaign-wide physical quasistationary termination.  All six
cases completed as `condition_met` with NRG reporting
`external_stop_request`, and an independent quasistationarity audit agreed for
6/6 cases.

See `validation.md` for the acceptance protocol and results.

## Repository boundary

This repository is a supplement to NRG, not an alternative distribution of
NRG.  NRG remains the authoritative project for CFD algorithms, physical
models, Fortran interfaces, task-setup data, and compiled executables.

## Before public repository creation

Technical release preparation is complete.  Repository visibility, licensing,
and any GitHub-specific metadata (repository URL, topics, CI policy) are owner
choices and should be finalized when the repository is created.
