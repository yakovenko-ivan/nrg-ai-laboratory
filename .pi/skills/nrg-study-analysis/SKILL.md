---
name: nrg-study-analysis
description: Design, pilot, execute, validate, aggregate, and report an NRG scientific analysis study. Use when the user asks to analyze an existing campaign, create or revise a study, compute scientific metrics, compare cases, generate study tables/figures, or produce conclusions from CFD histories.
---

# NRG Scientific Study Workflow

Use this skill for hypothesis-specific scientific analysis performed inside an NRG study workspace.

The governing data flow is:

`raw case histories -> validated per-case structured results -> aggregate scientific tables -> plotting exports -> figures -> interpretation -> report`

Do not reverse this order. In particular, do not derive scientific conclusions from a few plotted examples when complete structured results can be computed.

## 1. Ground the study before editing

Before implementing or revising a study:

1. Resolve the study with `nrg_list_studies` when its directory is not already known.
2. Resolve and inspect the campaign with laboratory tools rather than conversation memory or path guesses.
3. Read `agent_workspace/STUDY_PROTOCOL.md` and the study manifest.
4. Inspect the available `nrg_analysis` APIs before writing new numerical logic.
5. Inspect existing study outputs if revising a completed study.

Work only in the study-writable layer allowed by `AGENTS.md`.

## 2. Reuse trusted analysis primitives

Prefer trusted functions from `nrg_analysis` for reusable operations such as:

- campaign and reactor-history I/O;
- provenance and integrity checks;
- nonuniform time-series differentiation;
- threshold crossing;
- chemistry/species conversion;
- ignition metrics;
- quasistationarity evaluation;
- plotting-data serialization.

Do not copy trusted algorithms into `analyze.py` merely to make the study self-contained.

If a reusable operation is missing, implement the smallest hypothesis-specific version in the study first. Recommend promotion into `nrg_analysis` only after the operation has become stable and demonstrably reusable across studies.

## 3. Declare metric roles before calculating them

For each scientific question, classify metrics as appropriate:

- **primary** — the main quantity used for the study conclusion;
- **secondary** — an independent or alternative definition used for robustness checks;
- **diagnostic** — useful for interpretation but not automatically equivalent to the primary metric;
- **integrity** — numerical/data-quality checks rather than physical observables.

Record these roles in `STUDY.md` or `analysis_config.toml` when practical.

Do not silently promote an exploratory diagnostic into an established physical criterion.

## 4. Large-study development rule: pilot before production

Do **not** execute newly written or substantially modified analysis code against a large complete campaign as its first validation attempt.

For a large study, the mandatory development sequence is:

`design -> representative pilot -> inspect/validate -> freeze per-case contract -> full extraction -> aggregation -> interpretation`

Use `nrg_study_pilot_plan` to obtain a deterministic representative subset based on the campaign logical-identity axes. Prefer this over:

- the first N cases;
- an unrecorded random sample;
- an arbitrary contiguous block of case IDs.

The default pilot should normally contain roughly 12-24 cases and should span important extremes, interior conditions, categorical models/mechanisms, and expected difficult/long histories. Add explicit difficult cases if the automatic plan does not cover a known edge regime.

Use `nrg_run_study_pilot` to execute the pilot. Pilot output is development evidence only and must not be presented as a campaign-wide scientific result.

Before full execution, inspect the pilot and verify that the requested per-case output contract is actually produced. At minimum verify:

- every pilot case is represented;
- required primary metrics are present and finite when physically available;
- optional metrics have explicit availability flags rather than silent omissions;
- expected boundary/integrity diagnostics exist;
- representative quasistationary/product-state logic succeeds where required;
- output units and parameter coordinates are correct;
- representative plotting exports are structurally valid when requested;
- no raw CFD files changed.

A successful tool invocation is not sufficient scientific validation by itself.

The trusted production wrapper ties large-study pilot validation to the exact hashes of:

- `analyze.py`;
- `analysis_config.toml`;
- `cases.csv`.

If any of these changes after the pilot, rerun the pilot before full production analysis.

## 5. Separate per-case extraction from aggregation

For large studies, `analyze.py` should be architected as two conceptual layers.

### Layer A — per-case extraction

Input:

`one case history + one case's structured metadata`

Output:

`one structured per-case scientific record`

The per-case record should contain, as applicable:

- logical case ID;
- physical/design parameters from structured campaign metadata;
- requested scientific metrics;
- availability flags for optional metrics;
- quality/boundary flags;
- current execution status and termination reason;
- attempt/job provenance when relevant;
- numerical-integrity diagnostics;
- source-history extent and sample count.

Do not infer missing exact campaign parameters from filenames.

### Layer B — campaign aggregation

Input:

`validated per-case structured records`

Output:

- trends;
- rankings;
- spreads and ratios;
- monotonicity classifications;
- cross-model comparisons;
- plotting tables;
- figures;
- `study_summary.json`;
- report-supporting aggregate tables.

Debugging a plot or aggregate classification should not require re-reading hundreds of raw histories if valid per-case results already exist.

## 6. Persist and reuse per-case results for large studies

Large-study analyzers should persist per-case structured outputs under the study workspace and reuse them when they are still valid.

The trusted study runner exposes:

- `NRG_STUDY_MODE` (`pilot` or `full`);
- `NRG_STUDY_CASE_CACHE_DIR`;
- `NRG_STUDY_ANALYSIS_SHA256`;
- `NRG_STUDY_CONFIG_SHA256`;
- `NRG_STUDY_CASES_SHA256`;
- campaign and selected case counts.

Use these to make cache validity explicit. A cached result should not be reused when the relevant analysis definition/configuration or raw input identity has changed.

Do not treat the cache as authoritative raw data. It is a derived, reproducible intermediate product.

If a single case exposes an analysis bug late in a large production run, prefer reprocessing only invalid/missing cases and then rebuilding aggregates rather than recomputing every successful case.

## 7. Fail soft at the per-case layer, fail closed at the scientific conclusion layer

A large analysis should normally continue past an individual per-case analysis exception while recording that failure structurally, for example with:

- case ID;
- exception/error category;
- missing output fields;
- source file/provenance context.

After extraction, report counts such as:

- successful per-case analyses;
- failed per-case analyses;
- unavailable optional observables.

Do **not** silently drop failed cases and proceed to a global conclusion. If required cases are missing, the study must remain incomplete until the failures are repaired or an explicit scientifically justified exclusion is documented.

## 8. Aggregate algorithmically

Only after the required per-case results exist, calculate requested cross-case summaries such as:

- parameter trends;
- monotonicity classifications;
- rankings;
- ratios and spreads;
- error/difference statistics;
- percentile summaries;
- outlier identification;
- convergence comparisons.

Global prose statements must be supported by these aggregate results. If a trend has exceptions, report the exceptions rather than averaging them away.

## 9. Numerical integrity and boundary checks

Every study should include diagnostics appropriate to its numerical formulation and scientific metric. Examples include:

- mass-fraction closure;
- density or conserved-quantity consistency;
- history length and final physical time;
- missing-species/metric availability;
- derivative extrema near history boundaries;
- grid/time-step sensitivity when the campaign is designed for convergence;
- stored-versus-recomputed derived quantities where an independent check exists.

Name each diagnostic precisely. For example, `abs(sum(Y_k)-1)` is mass-fraction closure, not elemental conservation.

## 10. Separate physical classification from execution provenance

Do not infer why a run terminated from a physical-history classification.

Keep separate:

- physical/history classification;
- execution `status`;
- `nrg_termination_reason`;
- `attempt_id`;
- `runner_job_id`.

Follow the provenance semantics in `AGENTS.md`.

## 11. Plotting and export

Figures are derived products, not the scientific source of truth.

When the study produces parameter-sweep curves or the user requests Tecplot/gnuplot data, follow the `nrg-plot-data` skill and use `nrg_analysis.plot_data` rather than hand-building sparse pseudo-wide tables.

Keep:

- structured CSV/JSON for machine-readable analysis;
- dense `.dat` exports for direct plotting;
- a manageable main-figure set;
- systematic supplementary figures when useful.

## 12. Execute through the trusted study runner

For a new or substantially changed **large** study:

1. edit study files;
2. call `nrg_study_pilot_plan`;
3. run the chosen subset with `nrg_run_study_pilot`;
4. inspect the pilot structured outputs;
5. correct analysis logic as necessary, rerunning only the bounded pilot;
6. once the pilot output contract is correct, call `nrg_run_study` for production;
7. inspect execution outcome and completeness;
8. call `nrg_read_study_summary` before writing final conclusions.

For a small study, `nrg_run_study` may be used directly when the cost of full execution is itself a reasonable validation run.

Do not modify raw CFD results to make an analysis pass.

Do not repeatedly launch full-campaign analysis while `analyze.py` is still being debugged.

## 13. Interpretation discipline

Write scientific interpretation only after inspecting the completed structured outputs.

Distinguish explicitly among:

- numerical effects;
- physical-model effects;
- chemistry-model effects;
- analysis-definition effects;
- execution/provenance facts.

A correlation or trend in the campaign does not by itself identify a reaction pathway or causal physical mechanism. Mechanistic claims require appropriate evidence such as sensitivity, rate-of-production, flux, or targeted follow-up analysis.

Do not call a computational result experimentally validated or experimentally grounded unless experimental data are actually included and compared.

## 14. Final study products

A mature completed study should normally contain:

- `STUDY.md` with objective and metric definitions;
- `analysis_config.toml` when configurable analysis settings are useful;
- hypothesis-specific `analyze.py`;
- per-case structured results;
- aggregate tables;
- plotting-ready exports when relevant;
- figures and a figure manifest when the figure set is large;
- substantive `results/study_summary.json` with `status = "completed"`;
- a report whose numerical claims can be traced to structured outputs.

For large studies, the study should additionally retain enough pilot provenance to show which analysis/config/campaign version was validated before production execution.
