# Repository creation checklist

The v0.6.0 source tree is technically prepared for repository creation.  The
steps below are intentionally separate from the portability implementation so
that repository-hosting choices remain explicit owner decisions.

## Before the first commit

- choose repository visibility (public/private);
- choose and add a software license if the repository will be distributed;
- choose the default branch name;
- confirm that no active campaign, run, local study, NRG checkout/build, or
  `config/laboratory.local.toml` is present in the source tree;
- run the full regression suite and portability scan;
- inspect `git status --ignored` after initializing Git to confirm local research
  paths are ignored as intended.

The project does not select a license automatically.  Licensing is a legal and
project-governance decision rather than a portability implementation detail.

## Candidate verification

```bash
python -m pytest -q
nrg-lab-doctor
```

On a workstation with an external NRG build configured:

```bash
nrg-lab-config validate
nrg-lab-doctor --require-nrg
```

A normal fresh source checkout should remain repository-ready even before NRG
is attached.

## Git boundary to verify

These paths must remain untracked/local:

```text
config/laboratory.local.toml
.local/
campaigns/
runs/
agent_workspace/studies/<study>/
.venv/
```

These project-local Pi resources must remain tracked:

```text
.pi/extensions/
.pi/skills/
```

## Initial repository creation

After the owner decisions above are made, initialize/create the repository using
the selected default branch, inspect the staged file list, and make the initial
commit.  Before pushing, verify that the staged tree contains no ignored local
research/runtime artifacts.

After the remote repository exists, add its canonical URL to `pyproject.toml`
and/or README metadata if desired.  Create the `v0.6.0` tag only after one final
fresh-clone smoke test from the remote repository succeeds.
