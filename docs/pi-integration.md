# Pi integration contract — v0.6

The NRG AI Laboratory Assistant uses a project-local Pi extension and project
skills while delegating laboratory state changes to trusted `nrg_*` tools.

## Project-local resources

```text
AGENTS.md
.pi/extensions/nrg-laboratory/
.pi/skills/
```

These are repository-relative.  Start Pi with this clone as the project so Pi
applies `AGENTS.md`, the local extension, and local skills.

## Python bridge

The extension invokes:

```bash
python -m agent_workspace.lab_bridge ...
```

and editable installation exposes the equivalent human-facing command:

```bash
nrg-lab ...
```

Interpreter precedence is:

1. `NRG_PYTHON`;
2. repository `.venv` created by `scripts/bootstrap.py`;
3. `python` from PATH as a compatibility fallback.

## NRG boundary

The Pi project does not own or build the CFD solver.  NRG source,
`package_interface/task_setup`, `computing_module`, and problem-specific package
interfaces come from the external NRG project and are selected through local
laboratory configuration.

The trusted tool boundary is unchanged:

- campaign execution/reset/append/stop and provenance changes use registered
  `nrg_*` operations;
- raw data under `runs/` are experimental records;
- study-specific analysis is local under the configured studies root;
- the restricted shell is inspection-only;
- physical-completion campaigns use trusted physical-condition start tools,
  not the generic start path.

## Staged study-analysis tools

The integrated v0.5.9 analysis workflow adds three trusted study operations:

- `nrg_study_pilot_plan` — deterministic representative identity-space pilot
  selection;
- `nrg_run_study_pilot` — bounded pilot execution with raw-input integrity and
  hash-bound validation;
- `nrg_run_study` — full production execution, guarded for campaigns larger
  than 50 cases.

A current pilot marker becomes stale when `analyze.py`, `analysis_config.toml`,
or the source campaign `cases.csv` changes.  Pilot outputs are development
products and are not campaign-wide scientific evidence.

## First validation after cloning

```bash
python3 scripts/bootstrap.py --dev --init-local-config
source .venv/bin/activate
python -m pytest -q
nrg-lab-doctor
```

After building/configuring NRG:

```bash
nrg-lab-config validate
nrg-lab-doctor --require-nrg
nrg-lab lab-info
```

Then start Pi and use `/nrg-tools` to verify registered/active NRG laboratory
tools.

No campaign is expected in a clean source checkout.
