# Study: {{STUDY_ID}}

## Scientific request

{{SCIENTIFIC_REQUEST}}

## Inputs

- Laboratory configuration: `{{LABORATORY_CONFIG}}`
- Campaign manifest: `{{CASES_CSV}}`
- Case-root fallback for legacy relative `case_path` values: `{{CASE_ROOT}}`

New campaign manifests should normally contain absolute `case_path` values, so
`case_root` is only a compatibility fallback.

## Analysis development plan

Before full execution, define:

- primary / secondary / diagnostic / integrity metrics;
- the per-case structured output contract;
- numerical and boundary-quality checks;
- how per-case results will be aggregated;
- for a large campaign, the representative pilot strategy and cache/reuse policy.

For newly written or substantially modified analysis on a large campaign, use
`nrg_study_pilot_plan` and `nrg_run_study_pilot` before `nrg_run_study`.

## Agent instructions

This directory is the editable scientific layer. The agent may modify
`analysis_config.toml` and `analyze.py`, and may create derived files under
`results/` and trusted pilot products under `pilot/`.

Do not modify raw NRG case directories, `nrg_analysis`, the external NRG
source/build, the campaign generator, or the campaign runner during an ordinary
study.

For large studies, prefer:

`raw histories -> cached/structured per-case records -> aggregation -> plots/report`

rather than repeatedly recomputing the complete campaign while debugging
analysis or plotting logic.

Record new scientific metrics in the study output with an explicit status such
as `primary`, `literature_supported`, `secondary`, or `diagnostic`.
