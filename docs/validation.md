# Validation — v0.6.0 release candidate

The v0.6 portability work was validated in two layers: automated repository
regression tests and a real clean-install/real-NRG acceptance campaign driven
through the Pi agent.

## Automated regression suite

From the repository root:

```bash
python -m pytest -q
```

Current release-candidate result:

```text
123 passed
```

The release-candidate suite verifies, among other things, that:

- the repository does not vendor `nrg_runtime/`, campaigns, or raw runs;
- portable defaults reference only the ignored local/external NRG integration
  boundary;
- `Laboratory.validate(require_runtime=False)` works without an installed NRG
  tree;
- the environment doctor distinguishes repository readiness from external NRG
  execution readiness;
- `--require-nrg` promotes missing NRG resources/executables to required
  failures;
- the root `.gitignore` protects external NRG, campaigns, runs, local
  configuration, and user study instances while preserving the controlled study
  template;
- all three Python layers (`nrg_analysis`, `agent_workspace`, `campaign_tools`)
  are installable and importable outside the checkout working directory;
- project-local Pi extension/skill resources remain present;
- committed source contains no known developer-machine path prefixes.
- staged-study pilot CSV filtering preserves source order;
- pilot markers are invalidated when the analysis implementation changes;
- deterministic pilot planning covers logical-identity axes rather than using
  the first N cases;
- a synthetic 720-case, four-axis campaign obtains complete individual-axis
  level coverage with a 20-case pilot.

## v0.5.9 staged-analysis merge validation

The staged-study patch was merged selectively onto the v0.6 portability tree so
that v0.6 behavior was not regressed.  In particular, the merge preserves:

- project-root resolution from the Pi extension location;
- `.venv` interpreter preference;
- the v0.6 project version reported by the bridge;
- external-NRG repository boundaries;
- base and local laboratory configuration provenance.

A synthetic 51-case study was then exercised through the merged trusted runner.
A full run before pilot validation was rejected with exit code 42.  A five-case
pilot completed, produced `pilot_validation.json`, and a subsequent full run
completed successfully.  Full-run provenance retained the local laboratory
configuration fields.

## Git hygiene test

A disposable Git repository was initialized from the candidate tree and
committed.  Representative local artifacts were then created under:

```text
config/laboratory.local.toml
.local/NRG/...
campaigns/...
runs/...
agent_workspace/studies/<local-study>/...
```

`git status --porcelain` remained clean.  The controlled
`agent_workspace/studies/_template/` remained tracked.

## Arbitrary-location test

A fresh copy was placed at an unrelated filesystem location containing spaces
and invoked from outside the repository working directory.  The Python package
and configuration layer resolved the repository from installed/module
locations rather than from the shell cwd.

Without NRG attached, the expected diagnostic state was observed:

```text
repository_ready = true
nrg_runtime_ready = false
required_failures = 0
```

After attaching an external NRG-shaped test tree at local-configured paths, the
path/interface contract validated successfully.  This synthetic check was then
superseded by the real-NRG acceptance test below.

## Real clean-install acceptance test

The candidate was installed in a clean project location using the documented
bootstrap workflow.  `nrg-lab-doctor` reported the source repository ready with
zero required failures before NRG was configured.

A separately maintained/built NRG checkout was then attached through
`config/laboratory.local.toml`.  Full configuration validation succeeded with
real external resources for:

```text
package_interface/task_setup
computing_module
validated 0-D package-interface executable
```

No NRG executable, source tree, or task-setup copy was placed in the assistant
repository.

### Agent-driven physical campaign acceptance test

Pi was given a scientific objective rather than a prepared campaign file.  The
agent designed, previewed, generated, prepared, inspected, and executed a small
homogeneous constant-volume H2-air ignition campaign with:

- KEROMNES kinetics;
- initial pressure 1 atm;
- 20 mol.% H2 in air using the laboratory's standard O2/N2 convention;
- initial temperatures 1000, 1100, 1200, 1300, 1400, and 1500 K;
- temperature as the sole logical identity axis;
- campaign-wide trusted physical termination profile
  `0d_cv_post_ignition_quasistationary_v1`.

The preview contained exactly six logical cases.  Preparation succeeded for all
six using the external NRG package interface and external NRG task-setup data.
Execution used the campaign-wide physical start/resume operation rather than
the generic campaign start path.

All six cases terminated with the required provenance signature:

```text
status = condition_met
nrg_termination_reason = external_stop_request
physical_condition_met = true
physical_condition_status = quasistationary
```

Aggregate completion was:

```text
condition_met = 6
running = 0
stopped = 0
interrupted = 0
timeout = 0
failed = 0
restart_required = 0
runner_exit_code = 0
```

The runner progressed sequentially with one active CFD case at a time.  The
physical controller detected the accepted post-ignition quasistationary window,
wrote `run_control.stop`, NRG terminated with `external_stop_request`, and the
runner recorded `condition_met`.  An independent reactor-history
quasistationarity audit classified 6/6 cases as quasistationary with no cases
requiring recalculation.

No portability or NRG-integration failure was encountered in this real campaign.
The campaign definition, generated state, raw runs, and analysis products remain
local research artifacts and are not part of this repository.

## Stage-D usability correction

The clean-install test exposed one documentation ambiguity: `${NAME}` expansion
in TOML paths uses the process environment, not sibling TOML helper keys.  The
release candidate therefore makes the example explicit and documents both
supported forms:

1. direct absolute/local paths in `laboratory.local.toml`; or
2. `${NAME}` paths only when `NAME` has already been exported/set in the process
   environment.

No TOML-local variable-substitution semantics were added.

## Upstream NRG boundary

NRG remains the authoritative CFD software project.  The assistant repository
contains orchestration, policy, analysis, and agent integration only.  Local
configuration points to external NRG-owned runtime resources and the trusted
execution provenance records the actual executable paths/hashes used.

## Behavior preserved

v0.6.0 does not redesign campaign identity, attempt semantics, runner locking,
physical-condition control, operator stop behavior, trusted reset/append,
provenance separation, restricted shell policy, or Tecplot conventions.  The
CFD execution architecture remains unchanged.  Study analysis additionally
incorporates the v0.5.9 staged pilot/production workflow while preserving the
trusted raw-data and provenance wrapper.
