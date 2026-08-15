# Scientific study protocol

This protocol defines what an agent-authored analysis must make explicit.

## Required before execution

- scientific question;
- campaign/cases used as evidence;
- requested observables;
- analysis method and units;
- status of each metric: `primary`, `literature_supported`, `secondary`, or `diagnostic`;
- thresholds/tolerances and why they were chosen;
- exclusions or missing species/data.

## Required before full execution of a large study

New or substantially modified analysis code must first be exercised on a bounded representative pilot subset.

The pilot should span the important logical-identity axes and expected difficult regimes. Prefer a deterministic identity-space pilot produced by `nrg_study_pilot_plan` over the first N cases or an unrecorded random sample.

Before production execution, verify that the pilot provides the intended per-case output contract, including required metrics, units, availability flags, numerical-integrity/boundary diagnostics, and any requested representative plotting exports.

A successful pilot process is necessary but not by itself sufficient scientific validation.

For large studies, separate per-case extraction from campaign aggregation and persist reusable per-case structured results when practical. A change to `analyze.py`, `analysis_config.toml`, or the campaign `cases.csv` requires renewed pilot validation before trusted full execution.

## Required after execution

- case failures or unavailable observables;
- numerical-integrity diagnostics relevant to the conclusion;
- whether all required cases produced valid per-case structured results;
- whether the requested question was answered by existing data;
- uncertainty/limitations of the chosen analysis method;
- any proposed follow-up calculation and the reason it is necessary.

## Large-study failure behavior

An individual per-case analysis failure may be recorded and processing may continue so that successful case products are not discarded. However, failed required cases must not be silently omitted from aggregate conclusions. The study remains incomplete until those failures are repaired or an explicit scientifically justified exclusion is documented.

## Forbidden behavior in ordinary study mode

- modifying raw NRG result files;
- modifying campaign generator/runner code;
- modifying NRG solver/library source;
- changing the approved campaign definition and presenting old and new cases as one homogeneous campaign;
- silently changing a metric definition between study runs;
- claiming a diagnostic metric is literature-standard without a verified source;
- repeatedly executing unvalidated new analysis code over a large complete campaign while debugging it.
