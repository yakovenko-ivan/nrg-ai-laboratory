# NRG AI Laboratory User Guide

**Status:** v0.7.0  
**Audience:** researchers using NRG AI Laboratory manually from the command line or agentically through Pi  
**Scope:** operational and scientific workflow for campaign design, execution, pause/resume, provenance, portable execution, and staged analysis

> This guide documents the NRG AI Laboratory as an operational research system. It is intentionally different from the earlier general tutorial *Building a CFD/Combustion Research Agent with the Pi Framework*, which explains agentic-AI concepts and the original architecture. Here the focus is: **how to use the current laboratory correctly and reproducibly**.

---

## 1. What NRG AI Laboratory is

NRG AI Laboratory is a controlled research layer around the external NRG CFD package.

The two projects have different responsibilities:

```text
NRG repository
    Fortran solvers
    physical models
    chemistry/thermophysical resources
    package interfaces
    computing_module
    restart/checkpoint implementation

nrg-ai-laboratory repository
    campaign definitions
    deterministic campaign generation
    trusted preparation/execution control
    provenance and identity management
    pause/resume and operator control
    portable execution bundles
    analysis studies
    Pi tools and agent policy
```

The laboratory does **not** replace NRG and does not own the CFD solver. It orchestrates NRG in a controlled way.

A useful mental model is:

```text
scientific intent
      |
      v
campaign definition
      |
      v
deterministic generated campaign
      |
      v
prepared NRG cases
      |
      v
trusted runner / portable executor
      |
      v
structured run state + raw CFD output
      |
      v
validated scientific study
```

---

## 2. Two operating modes

The same laboratory can be used in two ways.

### 2.1 Manual mode

A researcher directly invokes the installed command:

```bash
nrg-lab ...
```

The equivalent low-level bridge form is:

```bash
python -m agent_workspace.lab_bridge ...
```

Examples in this guide use `nrg-lab` where practical. If the console entry point is unavailable, prepend:

```bash
python -m agent_workspace.lab_bridge
```

and use the same subcommand/options.

Manual mode is appropriate for:

- infrastructure validation;
- testing a new campaign generator or runner feature;
- debugging;
- operator control;
- scripted laboratory workflows;
- remote execution without an agent.

### 2.2 Agentic mode

Pi loads:

```text
AGENTS.md
.pi/extensions/nrg-laboratory/
.pi/skills/
```

and the agent uses registered trusted tools such as:

```text
nrg_lab_info
nrg_list_campaigns
nrg_campaign_preview
nrg_campaign_generate
nrg_campaign_prepare_cases
nrg_campaign_status
nrg_campaign_start
nrg_campaign_job_status
nrg_campaign_pause
nrg_campaign_stop
nrg_case_stop
nrg_campaign_reset_cases
nrg_campaign_export_bundle
nrg_campaign_import_results
nrg_create_study
nrg_study_pilot_plan
nrg_run_study_pilot
nrg_run_study
nrg_read_study_summary
```

Agentic mode should be viewed as a **scientific control plane**, not as permission for an agent to modify arbitrary infrastructure.

---

## 3. Trusted infrastructure versus scientific workspace

The laboratory deliberately separates trusted infrastructure from researcher/agent-writable scientific content.

### Trusted infrastructure

Ordinary scientific work should not modify:

```text
nrg_analysis/
campaign_tools/campaign_runner.py
campaign_tools/campaign_generator_0d.py
.pi/extensions/
config/
external NRG source/build
```

Changes there are infrastructure development and should be reviewed and tested explicitly.

### Scientific workspace

Research-specific content belongs under:

```text
campaigns/definitions/
runs/
agent_workspace/studies/<study>/
```

Typical interpretation:

```text
campaigns/definitions/
    declarative experiment design

runs/
    experimental CFD records
    do not edit manually

agent_workspace/studies/
    hypothesis-specific analysis
    plots
    tables
    reports
```

Raw case data under `runs/` are experimental records. Do not manually delete or edit them merely to make a rerun easier.

---

## 4. Installation and bootstrap

### 4.1 Requirements

Python laboratory layer:

- Python 3.11+
- `venv`
- `pip`

Full Pi environment:

- Node.js 20+
- Pi installed separately
- Git strongly recommended

Actual CFD:

- independently built NRG
- compatible `computing_module`
- required package-interface executable(s)
- NRG `package_interface/task_setup` resources

### 4.2 Bootstrap

From the repository root:

```bash
python3 scripts/bootstrap.py --dev
```

Optionally initialize the local configuration template at the same time:

```bash
python3 scripts/bootstrap.py --dev --init-local-config
```

Activate:

```bash
source .venv/bin/activate
```

Run repository tests:

```bash
python -m pytest -q
```

### 4.3 Validate the source repository

```bash
nrg-lab-doctor
```

This checks the laboratory repository without necessarily requiring a ready NRG runtime.

### 4.4 Configure NRG

Create a machine-local overlay:

```bash
nrg-lab-config init-local
```

Edit:

```text
config/laboratory.local.toml
```

Typical configuration:

```toml
[paths]
task_setup_template = "/path/to/NRG/package_interface/task_setup"

[runtime]
computing_module = "/path/to/NRG/build/bin/computing_module"
package_interface_0d = "/path/to/NRG/build/bin/package_interface_0D_ignition_delay_campaign"
package_interface_1d = "/path/to/NRG/build/bin/package_interface_1D_wall_flame_campaign"

[execution]
default_threads = 8
```

The committed `config/laboratory.toml` stays portable. Machine-specific paths belong in the local overlay.

### 4.5 Validate execution readiness

```bash
nrg-lab-config validate
nrg-lab-doctor --require-nrg
nrg-lab lab-info
```

Do not begin a real campaign until these checks are clean.

---

## 5. Core concepts

Understanding four identifiers prevents many workflow errors.

### 5.1 Logical case

A logical case is defined by the campaign identity policy.

Examples of identity fields may include:

- temperature;
- pressure;
- mechanism;
- geometry;
- H2 concentration;
- mesh spacing;
- radiation flag;
- Soret flag.

Changing an identity field means **a different scientific case**.

### 5.2 Attempt

An attempt is a recalculation/configuration lineage of the same logical case.

Attempt-tunable fields may include selected numerical or runtime parameters explicitly permitted by the campaign policy.

Changing an allowed attempt field does not create a new logical case.

### 5.3 Runner job

A runner job is one background invocation of campaign execution.

The same attempt may legitimately appear in more than one runner job, for example after a pause/restart.

### 5.4 Bundle execution

A portable execution bundle is a frozen execution artifact derived from a prepared campaign. It has its own bundle identity while preserving the original logical-case identity and fingerprints.

---

## 6. Campaign state model

Do not infer state from filenames or conversation history. Query the laboratory.

The important states are:

| State | Meaning | Normally runnable? | Successful scientific completion? |
|---|---|---:|---:|
| `not_started` | prepared but never executed | yes | no |
| `running` | active CFD process | no | no |
| `finished` | normal accepted completion | normally skipped | yes |
| `condition_met` | trusted scientific/monitor criterion met | normally skipped | yes |
| `restart_required` | clean checkpointed termination requiring continuation | yes | no |
| `paused` | operator pause with verified restart checkpoint | yes | no |
| `pause_failed` | pause requested but restart checkpoint not verified | no automatic assumption | no |
| `interrupted` | previous runner ceased unexpectedly; process no longer trusted alive | yes after recovery checks | no |
| `stopped` | deliberate terminal operator stop | rerunnable | no |
| `timeout` | runtime ceiling reached without verified restart | requires investigation | no |
| `failed` | unsuccessful process/validation | policy dependent | no |

A campaign may contain several of these states simultaneously.

---

## 7. Discovering laboratory state

### Manual

Laboratory information:

```bash
nrg-lab lab-info
```

Registered campaigns:

```bash
nrg-lab campaign-list
```

Registered studies:

```bash
nrg-lab study-list
```

Campaign state:

```bash
nrg-lab campaign-status \
    --cases campaigns/<generated_campaign>/cases.csv
```

Detailed output is available only when needed:

```bash
nrg-lab campaign-status \
    --cases campaigns/<generated_campaign>/cases.csv \
    --full
```

The default output is deliberately compact.

### Agentic

Ask the agent to use:

```text
nrg_lab_info
nrg_list_campaigns
nrg_campaign_status
```

An agent should not guess an existing campaign path when the discovery tool can determine it.

---

# PART I - CAMPAIGN DESIGN AND EXECUTION

## 8. Stage 0: define and preview a campaign

Campaign definitions belong in:

```text
campaigns/definitions/
```

A campaign definition should specify:

- scientific objective;
- identity axes;
- campaign constants;
- output requirements;
- run-control strategy;
- mechanism/model selections;
- geometry;
- numerical resolution;
- permitted attempt overrides.

Preview before generation.

### Manual

```bash
nrg-lab campaign-preview \
    --campaign campaign_1d_wall_flame_keromnes.toml
```

Optional shorter/longer preview sample:

```bash
nrg-lab campaign-preview \
    --campaign campaign_1d_wall_flame_keromnes.toml \
    --limit 20
```

### Agentic

Prompt example:

```text
Design a new NRG campaign for outwardly propagating ultra-lean H2-air flames.

Use the campaign-design skill and project policy.
Define the identity axes, campaign constants, required observables and
termination strategy. Preview the campaign only.

Do not prepare or start CFD.
```

The correct stage gate is:

```text
definition accepted
case count accepted
identity policy accepted
outputs sufficient
termination strategy accepted
```

---

## 9. Stage 1: generate the deterministic campaign

### Manual

```bash
nrg-lab campaign-generate \
    --campaign campaign_1d_wall_flame_keromnes.toml
```

Generation creates the deterministic campaign metadata and setup inputs, including:

```text
campaign.toml
cases.csv
campaign_manifest.json
_setups/
```

Do not hand-edit `cases.csv`.

### Agentic

Use:

```text
nrg_campaign_generate
```

Overwriting already generated material should require explicit confirmation.

---

## 10. Stage 1b: inspect identity before expensive execution

For an already generated campaign:

```bash
nrg-lab campaign-identity-inspect \
    --cases campaigns/<generated_campaign>/cases.csv
```

Use this before:

- changing numerical parameters;
- resetting cases;
- appending design points;
- deciding whether a new condition is the same logical case or a new one.

The identity policy answers:

```text
Does this parameter define the science?
    -> new logical case

Is this field explicitly attempt-tunable?
    -> same logical case may be recalculated
```

---

## 11. Stage 1c: prepare NRG case directories

First inspect status:

```bash
nrg-lab campaign-status \
    --cases campaigns/<generated_campaign>/cases.csv
```

Prepare:

```bash
nrg-lab campaign-prepare \
    --cases campaigns/<generated_campaign>/cases.csv
```

Preparation invokes the appropriate trusted NRG package interface.

For current supported campaign families the laboratory selects package interfaces by problem kind, e.g.:

```text
0D -> package_interface_0d
1D -> package_interface_1d
```

Preparation should:

- create only missing cases;
- skip existing matching cases;
- reject fingerprint conflicts;
- materialize effective attempt configuration.

Inspect again:

```bash
nrg-lab campaign-status \
    --cases campaigns/<generated_campaign>/cases.csv
```

Do not start CFD until all intended cases are prepared.

---

## 12. Stage 2: start a normal campaign

### Manual

```bash
nrg-lab campaign-start \
    --cases campaigns/<generated_campaign>/cases.csv
```

The command launches a background runner and returns a job ID.

Save it:

```bash
JOB=<returned-job-id>
```

### Agentic

Use:

```text
nrg_campaign_start
```

The agent must obtain explicit confirmation before starting CFD.

### Important

Do not manually partition an ordinary campaign into arbitrary batches unless the workflow specifically calls for selected-case execution.

Completed cases are normally protected by runner policy and skipped automatically.

---

## 13. Campaigns with trusted physical termination

Some campaigns should not terminate merely at a preselected simulation time. For example, a 0D reactor may be intended to run until a trusted quasistationary product-state condition is reached.

Use the physical execution path rather than generic start.

### Manual preflight

```bash
nrg-lab campaign-physical-plan \
    --cases campaigns/<generated_campaign>/cases.csv \
    --profile <profile_name>
```

Start:

```bash
nrg-lab campaign-physical-start \
    --cases campaigns/<generated_campaign>/cases.csv \
    --profile <profile_name>
```

Offline audit after execution:

```bash
nrg-lab campaign-physical-audit \
    --cases campaigns/<generated_campaign>/cases.csv \
    --profile <profile_name>
```

### Agentic

Use the trusted physical-start tool such as:

```text
nrg_campaign_start_to_quasistationary
```

Do not substitute generic `nrg_campaign_start` when a trusted physical termination profile is required.

---

## 14. Stage 2b: monitor an active job

### Manual

```bash
nrg-lab campaign-job-status \
    --job "$JOB"
```

Routine output should remain compact.

For debugging/provenance:

```bash
nrg-lab campaign-job-status \
    --job "$JOB" \
    --full
```

### Inspect one case

```bash
nrg-lab case-inspect \
    --cases campaigns/<generated_campaign>/cases.csv \
    --case-id R000001
```

This is useful when:

- a case appears stuck;
- a process PID needs verification;
- NRG termination metadata are unclear;
- pause/stop markers must be inspected;
- attempt/run identifiers must be distinguished.

### Agentic

Use:

```text
nrg_campaign_job_status
```

Do not infer active state from old logs.

---

# PART II - PAUSE, RESUME, STOP, AND RECOVERY

## 15. `pause now`: checkpoint and resume later

Use this when the active case should stop soon but continue later from a checkpoint.

### Preflight

```bash
nrg-lab campaign-pause-plan \
    --job "$JOB" \
    --mode now
```

### Execute

```bash
nrg-lab campaign-pause-execute \
    --job "$JOB" \
    --mode now \
    --reason "maintenance window" \
    --wait-seconds 45
```

The trusted contract requires:

```text
NRG termination reason = external_pause_request
restart checkpoint required = true
graceful completion = true
force kill = false
```

Only then should the case be classified:

```text
status = paused
```

### Resume

Use the normal campaign start command on the **same `cases.csv`**:

```bash
nrg-lab campaign-start \
    --cases campaigns/<generated_campaign>/cases.csv
```

Do not:

- regenerate the campaign;
- reset the paused case;
- use selective rerun merely to continue;
- delete restart data.

A resumed case remains the same logical case and same attempt lineage.

---

## 16. `pause after current`: finish this case, then stop the runner

Use this when the current case should finish naturally, but no later case should start.

### Preflight

```bash
nrg-lab campaign-pause-plan \
    --job "$JOB" \
    --mode after_current
```

### Execute

```bash
nrg-lab campaign-pause-execute \
    --job "$JOB" \
    --mode after_current \
    --reason "stop before next case"
```

Expected immediate response:

```text
accepted = true
completed = false
control_state = acknowledged
pending = true
```

That is **successfully queued**, not failure.

Important semantics:

```text
current case:
    keeps running
    no run_control.pause
    no run_control.stop

after current case:
    runner stops before next case
    job becomes paused
```

The agentic workflow should return after acknowledgement. It should not poll repeatedly unless the user explicitly requests status.

### Validated `after_current` lifecycle

The local integration smoke test has validated the complete deferred-pause workflow:

```text
R000001 = running
R000002 = not_started
        ↓
pause_after_current requested
        ↓
control_state = acknowledged
pending = true
runner remains active
        ↓
R000001 reaches normal completion
        ↓
R000001 = finished
R000002 = not_started
operator control = handled
outcome = current_case_completed_runner_paused
runner = inactive
job = paused
        ↓
ordinary campaign-start on the same cases.csv
        ↓
R000001 is skipped
R000002 is launched
        ↓
R000002 = finished
job = completed
```

This confirms that `pause_after_current` does not interrupt the active CFD case, suppresses launch of the next case, and can be resumed with the ordinary campaign-start operation without regenerating or resetting the campaign.

### Operator-control escalation

A queued `pause_after_current` is a weaker control request and may be superseded by a stronger campaign-level action:

```text
pause_after_current -> pause now
pause_after_current -> terminal campaign stop
```

The integration test has also validated the `pause_after_current -> stop_campaign` path. The stronger stop request replaces the queued deferred pause, NRG exits gracefully with `external_stop_request`, no force kill is used, and the runner ends in `stopped` state.

If an older installed version still reports an acknowledged after-current request as an absolute blocker for these escalations, do not edit the control JSON manually. Update to the reviewed operator-control supersession implementation instead.

---

## 17. Terminal campaign stop

Use this only when continuation from the current state is **not** the operator's intention.

### Preflight

```bash
nrg-lab campaign-stop-plan \
    --job "$JOB"
```

### Execute

```bash
nrg-lab campaign-stop-execute \
    --job "$JOB" \
    --reason "terminate experimental run" \
    --wait-seconds 45
```

Expected NRG behavior:

```text
termination reason = external_stop_request
restart checkpoint required = false
```

The case becomes:

```text
status = stopped
```

This is not a successful scientific result.

---

## 18. Stop only the active case, continue the campaign

This is narrower than campaign stop.

Preflight:

```bash
nrg-lab case-stop-plan \
    --cases campaigns/<generated_campaign>/cases.csv \
    --case-id R000001
```

Execute:

```bash
nrg-lab case-stop-execute \
    --cases campaigns/<generated_campaign>/cases.csv \
    --case-id R000001 \
    --reason "case-specific anomaly"
```

Use this only when later campaign cases should continue.

---

## 19. Runner runtime ceiling

The trusted runner can have its own:

```text
max_runtime_seconds
```

If the ceiling is reached, the modern restartable behavior is:

```text
runner requests run_control.pause
NRG writes checkpoint
case -> restart_required
runner stops
```

If restart verification fails:

```text
case -> timeout
campaign -> halt/investigate
```

This is deliberately different from terminal `run_control.stop`.

---

## 20. Interrupted runner/process recovery

After VM shutdown, shell termination, or runner crash:

1. inspect current campaign state;
2. verify whether the recorded `computing_module` PID is actually alive;
3. do not delete `run_status.json`;
4. let the trusted runner recover stale `running` state as `interrupted` when safe;
5. resume through the normal start operation.

If a trusted NRG process is still alive after the runner has disappeared, do not launch another copy.

---

## 21. `pause_failed`

A pause request is not considered successful merely because the process exited.

If NRG does not provide the verified restart contract, the case becomes:

```text
pause_failed
```

Do not assume it is restartable.

Inspect:

```bash
nrg-lab case-inspect ...
```

and the NRG logs/status before further action.

---

# PART III - RECALCULATION AND CAMPAIGN GROWTH

## 22. Reset selected logical cases for a new attempt

Use reset when the **same scientific logical case** must be recalculated with permitted attempt-level changes.

First inspect identity:

```bash
nrg-lab campaign-identity-inspect \
    --cases campaigns/<generated_campaign>/cases.csv
```

Plan a reset:

```bash
nrg-lab campaign-reset-plan \
    --cases campaigns/<generated_campaign>/cases.csv \
    --case-id R000017 \
    --override '{"field":"run_control_config.final_time_s","value":0.02}' \
    --reason "extend simulation"
```

If the plan is valid, execute:

```bash
nrg-lab campaign-reset-execute \
    --cases campaigns/<generated_campaign>/cases.csv \
    --case-id R000017 \
    --override '{"field":"run_control_config.final_time_s","value":0.02}' \
    --reason "extend simulation"
```

Then prepare normally.

The trusted reset process records attempt-history metadata and changes only explicitly selected cases.

Never use reset to change a field defined as logical identity.

---

## 23. Add genuinely new design points

If a new parameter combination represents a new logical case, append rather than reset.

Preview:

```bash
nrg-lab campaign-append-preview \
    --cases campaigns/<generated_campaign>/cases.csv \
    --campaign <revised_definition.toml>
```

Execute:

```bash
nrg-lab campaign-append-execute \
    --cases campaigns/<generated_campaign>/cases.csv \
    --campaign <revised_definition.toml>
```

Existing case IDs must not be renumbered.

---

## 24. Selective rerun

Selective rerun is for explicit targeted re-execution, not ordinary pause/resume.

Plan:

```bash
nrg-lab campaign-rerun-plan \
    --cases campaigns/<generated_campaign>/cases.csv \
    --case-id R000017
```

Start:

```bash
nrg-lab campaign-rerun-start \
    --cases campaigns/<generated_campaign>/cases.csv \
    --case-id R000017
```

Use it when the previous run itself is intentionally being replaced/repeated under the selective-rerun semantics.

---

# PART IV - PORTABLE / REMOTE EXECUTION

## 25. When to use a portable execution bundle

Use portable execution when:

- the agent/scientific control plane is on one machine;
- the expensive CFD should run on another workstation or cluster login node;
- Pi should not be required on the compute machine;
- the campaign design should remain frozen during remote execution.

The division of responsibility is:

```text
control-plane machine:
    design
    preview
    generate
    prepare
    audit
    export
    import
    analyze

compute machine:
    configure machine linkage
    validate bundle
    run/resume
    pause/stop
    pack results
```

---

## 26. Export a prepared campaign

Plan:

```bash
nrg-lab campaign-export-bundle-plan \
    --cases campaigns/<generated_campaign>/cases.csv \
    --output /path/to/bundle
```

Execute:

```bash
nrg-lab campaign-export-bundle-execute \
    --cases campaigns/<generated_campaign>/cases.csv \
    --output /path/to/bundle
```

The bundle contains:

```text
bundle_manifest.json
cases.csv
STATIC_CHECKSUMS.sha256
EXPORT_SNAPSHOT_CHECKSUMS.sha256
portable_executor.py
execution.local.toml
runs/
...
```

Execution case paths inside the bundle are relative and portable.

### Default export location and storage contract

The default export destination is a **transient managed root**, not the
campaign tree:

```text
<research_root>/.local/portable/exports/<campaign>__<bundle_id>
```

An explicit `--output` path is always honored and is never rewritten into the
managed root. The default destination is deterministic; exporting to an existing
non-empty default destination fails conservatively and never creates a
suffixed/timestamped duplicate copy.

The durable storage contract is:

```text
runs/                                  sole durable heavy scientific record
.local/portable/exports/              transient managed export bundles
.local/portable/returns/              transient managed returned archives
campaigns/**/_imports/*.json           durable lightweight import receipts
```

Legacy laboratory-managed locations are recognized for cleanup only; no new
artifacts are written there after this change:

```text
<campaign_root>/exports/
<research_root>/agent_workspace/portable_returns/
```

`runs/` is never a cleanup candidate, and no intra-campaign file deduplication,
hardlinks, reflinks, compression, or content-addressed storage is performed.

---

## 27. Configure the compute host

Only edit:

```text
execution.local.toml
```

Example:

```toml
[runtime]
computing_module = "/path/on/compute/host/computing_module"
require_exact_binary = false

[execution]
backend = "local"
threads = 16
poll_seconds = 0.25
```

Do not edit:

```text
cases.csv
setup_input.nml
task_setup/
fingerprints
portable_executor.py
```

just to adapt the bundle to another machine.

---

## 28. Validate and run the portable bundle

From the bundle directory:

```bash
python portable_executor.py validate \
    --config execution.local.toml
```

Run:

```bash
python portable_executor.py run \
    --config execution.local.toml
```

Resume later:

```bash
python portable_executor.py resume \
    --config execution.local.toml
```

Status:

```bash
python portable_executor.py status
```

---

## 29. Portable pause and stop

Checkpoint now:

```bash
python portable_executor.py pause \
    --mode now
```

After current:

```bash
python portable_executor.py pause \
    --mode after-current
```

Terminal stop:

```bash
python portable_executor.py stop
```

Notice the spelling difference:

```text
laboratory bridge: after_current
portable executor: after-current
```

---

## 30. Pack remote results

After the executor is inactive:

```bash
python portable_executor.py pack-results \
    --output results.zip
```

Only cases actually executed/updated inside the bundle are returned as portable results.

Do not manually copy remote case directories back over the source laboratory.

---

## 31. Import portable results

Preflight:

```bash
nrg-lab campaign-import-results-plan \
    --cases campaigns/<generated_campaign>/cases.csv \
    --results results.zip
```

Import:

```bash
nrg-lab campaign-import-results-execute \
    --cases campaigns/<generated_campaign>/cases.csv \
    --results results.zip
```

Import verifies:

- bundle identity;
- campaign contract;
- case fingerprints;
- file hashes;
- returned case inventory;
- execution provenance.

### Automatic transport-artifact reclamation

After a successful verified import the durable lightweight receipt
(`campaigns/**/_imports/*.json`) is written **before** any cleanup. By default
(`keep_portable_artifacts = false`, the production default) the import then
reclaims the matching managed transport artifacts:

- a returned results archive located under `.local/portable/returns/` (or the
  legacy `agent_workspace/portable_returns/`) is removed only when the receipt
  proves exact `bundle_id` equality and exact current SHA-256 agreement;
- a matching expanded export under `.local/portable/exports/` (or legacy
  `campaigns/exports/`) is removed only when the receipt proves contract
  identity, parsed-UTC temporal proof (import at or after export creation),
  and there is no executor lock.

Deletion safety is provenance-based, never filename-based. A user-provided
archive outside the managed/legacy return roots is **never** auto-deleted. An
unproven artifact is retained. A failed import retains everything.

If cleanup itself fails after a scientifically successful import, the
imported scientific data in `runs/` is **not** rolled back; the durable receipt
is preserved and the cleanup failure is reported separately.

Use `--keep-portable-artifacts` to suppress automatic cleanup (for debugging or
forensics):

```bash
nrg-lab campaign-import-results-execute \
    --cases campaigns/<generated_campaign>/cases.csv \
    --results results.zip \
    --keep-portable-artifacts
```

### Manual cleanup (core CLI)

Manual reclamation is a plan-then-execute workflow. The planner is read-only;
the executor acts only on artifacts returned by a fresh plan and revalidates
containment and provenance before each deletion.

```bash
python campaign_tools/portable_bundle.py --laboratory config/laboratory.toml \
    cleanup-plan
python campaign_tools/portable_bundle.py --laboratory config/laboratory.toml \
    cleanup
```

The default plan classifies only `safe_imported` artifacts (proven by a
successful import receipt). The advanced `--include-unimported-exports` option
may additionally classify an abandoned unlocked expanded export with complete
identity as deletable, but it **never** allows unimported returned archives to
be deleted (a returned ZIP may contain the only copy of not-yet-imported
results). There is no force/arbitrary-path option.

```bash
python campaign_tools/portable_bundle.py --laboratory config/laboratory.toml \
    cleanup-plan --include-unimported-exports
```

> **Note:** Pi-agent and laboratory-bridge integration for cleanup (a
> `nrg_campaign_portable_cleanup` tool and `campaign-portable-cleanup-*`
> bridge commands) is **not yet available**; it is deferred to a separate
> security-boundary follow-up. Use the core CLI above for manual reclamation.

---

## 32. Restart portability caveat

NRG restart checkpoints are raw binary state.

Moving a checkpoint to a different machine is safe only when NRG restart state is binary compatible, including:

- restart schema;
- relevant type/kind sizes;
- byte ordering;
- compatible solver/runtime state.

Prefer the identical executable/build provenance for cross-machine restart.

Fresh execution of not-yet-started cases is less restrictive than cross-machine checkpoint continuation.

---

# PART V - SCIENTIFIC VALIDATION AND ANALYSIS

## 33. Separate execution provenance from physical classification

A successful executable exit and a valid physical scientific state are not the same question.

Use two evidence layers.

### Execution provenance

```bash
nrg-lab campaign-execution-summary \
    --cases campaigns/<generated_campaign>/cases.csv
```

This answers questions such as:

- which cases finished;
- which were paused/restarted/stopped;
- actual NRG termination reasons;
- which runner jobs executed them.

### Offline physical audit

For campaigns with a trusted physical profile:

```bash
nrg-lab campaign-physical-audit \
    --cases campaigns/<generated_campaign>/cases.csv \
    --profile <profile_name>
```

The audit independently reads stored histories.

Do not infer physical validity solely from `run_status.json`.

---

## 34. Create a scientific study

Manual:

```bash
nrg-lab create-study \
    --slug flame-speed-mechanism-comparison \
    --request "Compare flame propagation metrics across mechanisms and resolutions" \
    --cases campaigns/<generated_campaign>/cases.csv
```

The study belongs under:

```text
agent_workspace/studies/<study>/
```

Read its `STUDY_PROTOCOL.md` before implementing analysis.

The hypothesis-specific files normally include:

```text
STUDY.md
analysis_config.toml
analyze.py
results/
```

Reusable infrastructure should not be copied into the study.

---

## 35. Large-study pilot

For newly written or materially modified analysis, especially for campaigns larger than 50 cases, first obtain a deterministic representative pilot.

Plan:

```bash
nrg-lab study-pilot-plan \
    --study agent_workspace/studies/<study> \
    --max-cases 20
```

The selector covers the logical identity space rather than taking the first N cases.

Run the selected cases:

```bash
nrg-lab run-study-pilot \
    --study agent_workspace/studies/<study> \
    --case-id R000001 \
    --case-id R000017 \
    --case-id R000103
```

The pilot should establish a per-case result contract containing at least:

- identity/provenance;
- primary scientific metrics;
- diagnostics;
- numerical-integrity flags;
- explicit `ok`, `warning`, or `analysis_error`.

A successful Python exit alone is not adequate pilot validation.

---

## 36. Full production analysis

Once the pilot is valid:

```bash
nrg-lab run-study \
    --study agent_workspace/studies/<study>
```

Then inspect:

```bash
nrg-lab study-summary \
    --study agent_workspace/studies/<study>
```

Large-study execution is guarded by hashes of:

```text
analyze.py
analysis_config.toml
cases.csv
```

Changing them invalidates the pilot and requires pilot revalidation.

---

# PART VI - COMPLETE USE-CASE EXAMPLES

## 37. Use case A: 0D ignition-delay mechanisms

Typical workflow:

```text
1. Define mechanisms / temperatures / pressures / composition.
2. Preview factorial count.
3. Generate.
4. Inspect identity.
5. Prepare with package_interface_0d.
6. Use the scientifically correct termination strategy.
7. Start.
8. Monitor.
9. Validate execution provenance.
10. Audit physical/product-state validity if applicable.
11. Create study.
12. Representative pilot.
13. Full analysis.
14. Report mechanism-to-mechanism trends.
```

Agent prompt:

```text
Design a 0D NRG ignition-delay campaign comparing <mechanisms> over
<temperature range> and <pressure range>.

Use the trusted campaign workflow.
Preview only. Report identity axes, case count, observables and termination
strategy. Do not prepare or start CFD.
```

---

## 38. Use case B: 1D outward H2-air flame campaign

Typical campaign dimensions may include:

```text
geometry = cartesian / cylindrical / spherical
H2 mole fraction
cell size
chemical mechanism
thermal radiation on/off
Soret on/off
```

Preparation uses the configured 1D package interface.

Recommended operational sequence:

```text
preview
generate
identity inspect
prepare
status
start
job status
pause/resume as needed
execution validation
scientific outcome classification
study pilot
full analysis
```

For long flame cases, operational pause/checkpoint capability is particularly important.

---

## 39. Use case C: maintenance interruption during a long case

User intent:

```text
"I need to shut down the machine, but I want to continue this case later."
```

Correct operation:

```text
pause now
verify external_pause_request
verify restart_required=true
job becomes paused
shut down machine
ordinary campaign start later
```

Incorrect operations:

```text
kill computing_module
terminal campaign stop
delete run_status.json
selective rerun
regenerate campaign
```

---

## 40. Use case D: finish the current case but do not begin the next

User intent:

```text
"Let this calculation finish, then stop."
```

Correct:

```text
pause after current
```

Expected:

```text
current case finishes naturally
next case remains not_started
operator control becomes handled
runner becomes inactive
job becomes paused
```

Later:

```text
ordinary campaign start
```

must skip the completed case and start the next runnable case. This full sequence has been validated in the two-case 1D runner smoke test.

---

## 41. Use case E: extend one under-resolved attempt

Suppose one logical case needs a longer final time but that field is attempt-tunable.

Correct workflow:

```text
campaign-identity-inspect
campaign-reset-plan
campaign-reset-execute
campaign-prepare
campaign-start
```

Do not append a duplicate logical case if the scientific identity has not changed.

---

## 42. Use case F: add a new mixture or mechanism

If H2 concentration or mechanism is an identity axis, a new value is a new logical case.

Correct:

```text
revise campaign definition
campaign-append-preview
campaign-append-execute
prepare new cases
run
```

Do not reset an existing case into a different scientific identity.

---

## 43. Use case G: execute on a faster workstation

Control-plane machine:

```text
prepare campaign
export bundle
transfer bundle
```

Compute machine:

```text
edit execution.local.toml
validate
run/resume
status on demand
pause/stop if needed
pack-results
```

Control-plane machine:

```text
import-plan
import
validate
analyze
```

No Pi installation is required on the compute host.

---

## 44. Use case H: campaign runner disappeared

If the runner vanished unexpectedly:

```text
do not start a second runner immediately
inspect job/case
verify child PID
verify execution lock
```

If no trusted child process remains, normal runner recovery may classify stale `running` as `interrupted`.

If the old CFD process still exists, resolve the orphan safely before launching anything else.

---

# PART VII - MANUAL / AGENTIC COMMAND MAPPING

## 45. Command mapping

| Purpose | Manual `nrg-lab` | Agentic tool |
|---|---|---|
| laboratory info | `lab-info` | `nrg_lab_info` |
| list campaigns | `campaign-list` | `nrg_list_campaigns` |
| preview | `campaign-preview` | `nrg_campaign_preview` |
| generate | `campaign-generate` | `nrg_campaign_generate` |
| prepare | `campaign-prepare` | `nrg_campaign_prepare_cases` |
| status | `campaign-status` | `nrg_campaign_status` |
| normal start | `campaign-start` | `nrg_campaign_start` |
| job status | `campaign-job-status` | `nrg_campaign_job_status` |
| pause | `campaign-pause-plan/execute` | `nrg_campaign_pause` |
| campaign stop | `campaign-stop-plan/execute` | `nrg_campaign_stop` |
| case stop | `case-stop-plan/execute` | `nrg_case_stop` |
| identity | `campaign-identity-inspect` | trusted identity inspection workflow |
| reset attempt | `campaign-reset-plan/execute` | `nrg_campaign_reset_cases` |
| append | `campaign-append-preview/execute` | `nrg_campaign_append_preview` / `nrg_campaign_append` |
| physical start | `campaign-physical-plan/start` | `nrg_campaign_start_to_quasistationary` |
| physical audit | `campaign-physical-audit` | trusted audit tool |
| export bundle | `campaign-export-bundle-plan/execute` | `nrg_campaign_export_bundle` |
| import results | `campaign-import-results-plan/execute` | `nrg_campaign_import_results` |
| create study | `create-study` | `nrg_create_study` |
| pilot plan | `study-pilot-plan` | `nrg_study_pilot_plan` |
| pilot analysis | `run-study-pilot` | `nrg_run_study_pilot` |
| full analysis | `run-study` | `nrg_run_study` |
| summary | `study-summary` | `nrg_read_study_summary` |

The manual interface often separates **plan** and **execute** because state-changing operations should be reviewed before execution.

---

# PART VIII - AGENTIC RESEARCH PROMPT CHAIN

## 46. Design stage

```markdown
### NRG workflow stage: DESIGN

**Target:** `<campaign>`
**Objective:** <scientific objective>
**Authority now:** create/revise and preview the declarative campaign only
**Stage-specific requirements:** <identity axes, constants, outputs, termination>
**Stop gate:** preview is scientifically accepted

Follow AGENTS.md and the relevant NRG skills.
Do not prepare or start CFD.
```

---

## 47. Generate and prepare

```markdown
### NRG workflow stage: PREPARE

**Target:** `<campaign>`
**Objective:** materialize the approved campaign
**Authority now:** generate and prepare missing cases
**Stage-specific requirements:** verify fingerprints and case count
**Stop gate:** campaign is PREPARED and execution-ready

Do not start CFD.
```

---

## 48. Start/resume

```markdown
### NRG workflow stage: EXECUTE

**Target:** `<campaign>`
**Objective:** start or resume all currently runnable cases
**Authority now:** trusted campaign-wide execution
**Stage-specific requirements:** use <normal/physical> completion mode
**Stop gate:** runner has started and job ID is reported

Do not change the campaign definition or trusted runner policy.
```

---

## 49. Monitor

```markdown
### NRG workflow stage: MONITOR

**Target:** `<campaign/job>`
**Objective:** inspect current execution state
**Authority now:** read-only status inspection
**Stage-specific requirements:** report runner state, active case and status counts
**Stop gate:** intervention required or requested status has been reported

Do not infer state from old conversation context.
```

---

## 50. Pilot analysis

```markdown
### NRG workflow stage: ANALYSIS PILOT

**Target:** `<study>`
**Objective:** <analysis objective>
**Authority now:** create/modify study-local analysis and run representative pilot
**Stage-specific requirements:** define per-case result contract and validate coverage
**Stop gate:** READY FOR FULL PRODUCTION ANALYSIS or NOT READY

Do not run the full campaign analysis.
```

---

## 51. Production analysis

```markdown
### NRG workflow stage: FULL ANALYSIS

**Target:** `<study>`
**Objective:** <scientific questions>
**Authority now:** run trusted full analysis
**Stage-specific requirements:** verify pilot hashes and completeness
**Stop gate:** campaign-wide structured analysis is complete or exact blockers reported
```

---

# PART IX - OUTPUT, PROVENANCE, AND TROUBLESHOOTING

## 52. Compact output versus `--full`

Operational commands return compact JSON by default.

Example intent:

```json
{
  "job_id": "...",
  "state": "running",
  "runner_active": true,
  "active_case": {
    "case_id": "R000001",
    "status": "running"
  }
}
```

Use:

```bash
--full
```

only when detailed provenance or debugging is needed.

The guiding principle is:

```text
persist richly
return minimally
```

Rich forensic state remains in persistent job/run-status records.

---

## 53. Important files

### Generated campaign

```text
cases.csv
campaign.toml
campaign_manifest.json
_setups/
```

### Prepared/running case

```text
setup_input.nml
task_setup/
run_status.json
computing_module.stdout.log
computing_module.stderr.log
run_control_status.inf
data_output/
data_save/
```

### Control markers

```text
run_control.pause
run_control.stop
```

Never create these manually in normal laboratory use; trusted control operations own them.

### Completion markers may include

```text
run_finished.done
run_condition_met.done
run_paused.done
run_restart_required.done
run_stopped.done
run_pause_failed.done
run_failed.done
```

The authoritative interpretation remains `run_status.json` plus verified NRG control status.

---

## 54. Common failure modes

### Fingerprint conflict

Symptom:

```text
existing case directory does not contain expected fingerprint
```

Meaning:

A directory exists but does not represent the generated case contract.

Action:

Do not overwrite it manually. Inspect provenance and decide whether this is old data, wrong campaign linkage, or a reset/cleanup requiring a trusted operation.

### Stale `running`

Symptom:

```text
run_status = running
runner not active
```

Action:

Verify the recorded process. Trusted recovery may convert it to `interrupted`.

### Pending operator control

Symptom:

```text
requested
acknowledged
```

Meaning:

The request has not yet reached final `handled` state.

For `pause_after_current`, `acknowledged` is expected while the current case runs.

### NRG pause request exits but no checkpoint

Symptom:

```text
external_pause_request missing
restart_required != true
```

Action:

Classify as `pause_failed`; do not resume blindly.

### Portable binary mismatch

Symptom:

```text
computing_module SHA differs
```

Action:

Fresh unstarted cases may still be executable if policy permits. Cross-machine restart should use binary-compatible runtime, preferably identical provenance.

### Agent wants to use shell instead of trusted tool

Action:

Use the registered `nrg_*` operation for any state change. The agent's shell channel is inspection-only.

---

# PART X - RECOMMENDED RESEARCH DISCIPLINE

## 55. Scientific gates

Treat the campaign as a chain of explicit acceptance gates:

```text
DESIGN
  ↓ accept identity/output/termination
GENERATE
  ↓ accept deterministic manifest
PREPARE
  ↓ accept fingerprints/runtime linkage
EXECUTE
  ↓ accept termination provenance
VALIDATE
  ↓ accept physical data quality
PILOT ANALYSIS
  ↓ accept output schema/coverage
FULL ANALYSIS
  ↓ accept completeness
REPORT
```

Do not allow an agent to silently cross several gates because the next action appears obvious.

---

## 56. Minimal authority prompts

A good user prompt gives only:

1. workflow stage;
2. target campaign/study;
3. scientific objective;
4. authority granted now;
5. stop/acceptance gate.

Example:

```markdown
### NRG workflow stage: PREPARE

**Target:** `H2_1D_campaign`
**Objective:** materialize the approved 1D H2-air campaign
**Authority now:** generate and prepare only
**Stage-specific requirements:** preserve campaign identity and verify fingerprints
**Stop gate:** all intended cases are prepared

Do not start CFD.
```

This works better than repeating the whole laboratory manual in every prompt.

---

## 57. When manual operation is preferable

Use manual mode for:

- infrastructure feature testing;
- first validation of a new control path;
- checking a patch;
- diagnosing a stuck process;
- local/remote bundle execution;
- explicit operator intervention;
- CI/regression work.

Use agentic mode for:

- scientific campaign design;
- structured campaign progression;
- monitoring and handoff;
- systematic validation;
- study creation;
- hypothesis-driven analysis;
- targeted follow-up recommendations.

---

## 58. Recommended validation sequence for infrastructure changes

Whenever pause/resume, runner, bridge, portable execution, or identity code is modified, first run the narrow regression test that directly exercises the changed subsystem.

For the current operator-control and pause/resume implementation:

```bash
python -m pytest tests/test_operator_control_supersession.py -v
python -m pytest tests/test_campaign_pause_resume.py -v
python -m pytest -v
git diff --check
```

Here **targeted test** means the small regression test for the exact behavior that was changed. For example, `test_operator_control_supersession.py` specifically checks that an acknowledged `pause_after_current` request can be replaced by a stronger campaign stop without leaving stale in-memory pause state. It is not a substitute for the full test suite; it is the first, faster diagnostic layer.

Then perform a bounded real integration smoke test before trusting production campaigns.

The local pause/resume integration sequence currently validated is:

```text
pause now
verified checkpoint
ordinary resume of the paused case
terminal campaign stop
pause after current
after-current acknowledgement is non-blocking
current case finishes normally
next case is not launched
job becomes paused
ordinary campaign-start resumes the campaign
completed case is skipped
remaining case is launched
campaign reaches completed
pause-after-current can be superseded by terminal campaign stop
```

Portable execution should additionally validate:

```text
export
same-machine bundle validate
run/resume
pack-results
import-plan
import
```

before first remote production use.

---

# PART XI - QUICK CHECKLISTS

## 59. Before launching CFD

```text
[ ] repository tests pass
[ ] nrg-lab-doctor --require-nrg passes
[ ] laboratory configuration resolves correct NRG build
[ ] campaign preview accepted
[ ] cases generated
[ ] identity inspected
[ ] cases prepared
[ ] no fingerprint conflicts
[ ] intended termination mode verified
[ ] output cadence/observables verified
[ ] status shows expected runnable count
```

## 60. Before pausing

```text
[ ] correct runner job selected
[ ] active process verified
[ ] choose now vs after_current intentionally
[ ] use trusted pause command
[ ] for now: verify NRG restart checkpoint contract
```

## 61. Before full analysis

```text
[ ] execution state validated
[ ] required physical/offline audit complete
[ ] analysis study exists
[ ] representative pilot completed
[ ] per-case schema accepted
[ ] pilot hashes current
[ ] missing/NaN policy defined
```

## 62. Before remote execution

```text
[ ] campaign PREPARED
[ ] bundle export plan accepted
[ ] bundle checksums valid
[ ] only execution.local.toml edited remotely
[ ] remote computing_module provenance known
[ ] restart binary compatibility understood
[ ] result import will use trusted import path
```

---

# PART XII - MAINTENANCE OF THIS GUIDE

This document evolves with the public command/tool contract.

When infrastructure changes, update this guide only after:

1. regression tests pass;
2. manual integration semantics are known;
3. the public CLI/tool names are stable;
4. `AGENTS.md` agrees with the behavior;
5. the corresponding repository version/release note is updated.

Recommended repository location:

```text
docs/user-guide.md
```

Recommended README link:

```markdown
## User guide

For end-to-end manual and Pi/agentic workflows, see
[`docs/user-guide.md`](docs/user-guide.md).
```

---

## Appendix A - Manual command inventory

Current major `nrg-lab` subcommands include:

```text
lab-info
campaign-list
study-list
campaign-status
case-inspect
campaign-identity-inspect

campaign-preview
campaign-generate
campaign-prepare
campaign-start
campaign-job-status

campaign-execution-summary
campaign-physical-audit
campaign-physical-plan
campaign-physical-start
campaign-physical-cases-plan
campaign-physical-cases-start

campaign-rerun-plan
campaign-rerun-start

campaign-pause-plan
campaign-pause-execute
campaign-stop-plan
campaign-stop-execute
case-stop-plan
case-stop-execute

campaign-reset-plan
campaign-reset-execute
campaign-append-preview
campaign-append-execute

campaign-export-bundle-plan
campaign-export-bundle-execute
campaign-import-results-plan
campaign-import-results-execute

create-study
study-pilot-plan
run-study-pilot
run-study
study-summary

extension-preview
extension-generate
composite-create
```

The extension/composite workflow is mainly for genuinely independent, legacy, or externally produced datasets. It is not a substitute for reset/append of an ordinary current campaign.

---

## Appendix B - Portable executor command inventory

From an exported bundle:

```bash
python portable_executor.py validate --config execution.local.toml
python portable_executor.py run --config execution.local.toml
python portable_executor.py resume --config execution.local.toml
python portable_executor.py status
python portable_executor.py pause --mode now
python portable_executor.py pause --mode after-current
python portable_executor.py stop
python portable_executor.py pack-results --output results.zip
```

## Appendix B2 - Portable bundle command inventory (control plane)

On the control-plane machine, `campaign_tools/portable_bundle.py` manages
export, import, and transport-artifact cleanup:

```bash
python campaign_tools/portable_bundle.py --laboratory config/laboratory.toml \
    export-plan --cases campaigns/<campaign>/cases.csv
python campaign_tools/portable_bundle.py --laboratory config/laboratory.toml \
    export --cases campaigns/<campaign>/cases.csv
python campaign_tools/portable_bundle.py --laboratory config/laboratory.toml \
    import-plan --cases campaigns/<campaign>/cases.csv --results results.zip
python campaign_tools/portable_bundle.py --laboratory config/laboratory.toml \
    import --cases campaigns/<campaign>/cases.csv --results results.zip \
    --keep-portable-artifacts
python campaign_tools/portable_bundle.py --laboratory config/laboratory.toml \
    cleanup-plan
python campaign_tools/portable_bundle.py --laboratory config/laboratory.toml \
    cleanup-plan --include-unimported-exports
python campaign_tools/portable_bundle.py --laboratory config/laboratory.toml \
    cleanup
```

The corresponding `nrg-lab` bridge commands (`campaign-export-bundle-*`,
`campaign-import-results-*`) wrap export/import. Bridge/Pi cleanup commands are
deferred to a separate security-boundary follow-up; use the core CLI above for
manual reclamation.

---

## Appendix C - Key invariants

The laboratory should preserve these invariants:

```text
cases.csv is immutable execution design metadata
logical identity is not rewritten by rerun attempts
completed cases are not deleted merely to resume
pause is not terminal stop
after-current pause does not interrupt the active case
raw runs are not edited by the agent
full forensic state is persisted even when API output is compact
remote execution may change machine linkage, not scientific case definition
analysis pilot validation precedes large full-study execution
```

These invariants are more important than any single command syntax.
