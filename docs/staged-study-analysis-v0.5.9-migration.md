# v0.5.9 Staged Study Analysis — Integrated Migration Notes

> **Integration status:** this patch is already incorporated into NRG AI Laboratory v0.6.0.
> Do not apply the original v0.5.9 overlay on top of v0.6.0; these notes document
> the analysis behavior and migration semantics that were carried forward.


v0.5.9 changes large-study analysis development so newly written or substantially modified `analyze.py` code is not first exercised over a complete large campaign.

## Installation

No separate installation is required in v0.6.0.  The staged-analysis tools,
controlled template, skill instructions, and production guard are part of the
repository.  After a Pi reload, `/nrg-tools` should show the study pilot tools
as active.

## New large-study workflow

For newly written or substantially changed analysis:

1. create/edit the study;
2. call `nrg_study_pilot_plan` (default 20 cases);
3. inspect the identity-axis coverage;
4. run those exact cases with `nrg_run_study_pilot`;
5. inspect pilot structured outputs and correct the analysis as necessary;
6. rerun the bounded pilot after every material change;
7. only then call `nrg_run_study` for full production analysis;
8. inspect `nrg_read_study_summary` before interpretation.

The planner uses deterministic logical-identity-space coverage. For modest discrete axes it attempts to cover every axis level before filling the remaining pilot slots by maximin distance. It is a development subset, not a statistical sample.

## Production guard

The trusted `agent_workspace.run_study` wrapper now treats campaigns with more than 50 cases as large studies.

A full run of a large study requires a successful current pilot marker tied to the exact hashes of:

- `analyze.py`;
- `analysis_config.toml`;
- campaign `cases.csv`.

If any of these changes, the full-run guard rejects production execution until the pilot is rerun.

Small studies (50 cases or fewer) may still use `nrg_run_study` directly.

## Pilot products

Pilot execution is isolated from canonical production outputs:

```text
<study>/pilot/selected_cases.csv
<study>/pilot/campaign.toml                 # copied when available
<study>/pilot/campaign_manifest.json        # copied when available
<study>/pilot/results/
<study>/pilot/provenance.json
<study>/pilot/study_stdout.log
<study>/pilot/study_stderr.log
<study>/pilot_validation.json
```

The copied campaign metadata is read-only interpretive metadata; raw CFD histories remain in the original campaign case directories.

`pilot_validation.json` is removed before every new pilot attempt. A failed current pilot therefore cannot accidentally leave an older validation marker active.

## Per-case cache support

The trusted runner now exports the following environment variables to `analyze.py`:

```text
NRG_STUDY_MODE
NRG_STUDY_CAMPAIGN_CASE_COUNT
NRG_STUDY_SELECTED_CASE_COUNT
NRG_STUDY_CASE_CACHE_DIR
NRG_STUDY_ANALYSIS_SHA256
NRG_STUDY_CONFIG_SHA256
NRG_STUDY_CASES_SHA256
```

For large studies, use these to persist/reuse derived per-case records and to separate raw-history extraction from campaign-level aggregation.

This release exposes the cache contract but does not force a particular scientific record schema: `analyze.py` remains hypothesis-specific.

## Existing studies

Existing study directories do not need to be recreated. The new trusted runner and tools work with their current `study_manifest.json`, `analyze.py`, and `analysis_config.toml`.

However, the updated controlled study template applies only to newly created studies. Existing large studies should be refactored manually toward the staged per-case/aggregation pattern when they are next revised.

A previously completed large study remains readable. If you invoke `nrg_run_study` again after installing v0.5.9, a current pilot validation will be required before a new full execution.

## Scope

This update does not change CFD campaign execution, physical termination, campaign identity, reset/append semantics, operator stop control, or raw-run protection.
