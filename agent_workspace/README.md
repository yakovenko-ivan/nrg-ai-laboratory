# Agent scientific workspace

`agent_workspace/studies/` is the area in which the Laboratory Assistant may
create and revise problem-specific analysis code.

The location of that directory is authoritative only through
`config/laboratory.toml -> paths.studies_root`. The controlled template remains
shipped with this package at `agent_workspace/studies/_template`.

## Create

```bash
python -m agent_workspace.create_study \
  --slug mechanism_divergence \
  --request "Investigate the mechanism divergence around 1100 K." \
  --cases /absolute/path/to/cases.csv
```

`create_study.py` loads the laboratory configuration, writes the new study to
`studies_root`, and records `laboratory_config`, `cases_csv`, and `case_root` in
`study_manifest.json`.

## Run

```bash
python -m agent_workspace.run_study \
  /absolute/path/to/agent_workspace/studies/mechanism_divergence
```

The study must resolve inside the configured `studies_root`. `run_study.py`
executes only the study's `analyze.py`, directs derived products to `results/`,
and records provenance. Raw case output is treated as read-only.

Version-1 manifests containing `workspace_root` remain readable; version-2
manifests use `case_root`.

The JSON file `workspace_policy.json` expresses the intended agent permission
contract. It is not itself a sandbox: operating-system permissions and the Pi
tool surface should enforce the actual boundary.
