# v0.6.1 — Large-campaign audit reliability

This patch addresses a production-scale failure mode exposed by a 720-case
0-D ignition campaign.

## Problem

The Pi bridge previously used a 120 s default timeout and parsed absent stdout
as `{}`. A long synchronous `nrg_campaign_quasistationary_audit` could therefore
surface an empty object. An LLM could then incorrectly interpret `{}` as
"zero anomalies" or "all cases passed", even though the Python audit contract
always returns structured counts on success.

## Changes

- Empty bridge stdout is now an explicit error; it is never converted to `{}`.
- Bridge execution exceptions/timeouts are returned as explicit structured errors.
- Full-history quasistationarity audit receives a 30 minute read-only timeout.
- New `nrg_campaign_execution_summary` reports current campaign execution
  provenance from `run_status.json` without reading reactor histories.
- Execution summary now counts status, NRG termination reason, termination
  condition, online physical-condition status, online `physical_condition_met`,
  termination profile, attempt IDs, and runner job IDs.
- AGENTS and the 0-D ignition-analysis skill explicitly state that `{}` or a
  missing audit contract is invalid and cannot establish offline
  quasistationarity.
- Regression tests cover the bridge contract, timeout, new tool registration,
  policy wording, and provenance counters.

## Scientific semantics

`nrg_campaign_execution_summary` answers:

> How did the current runs terminate, and what online trusted physical-condition
> metadata was recorded?

`nrg_campaign_quasistationary_audit` answers:

> Do the stored `reactor_history.dat` histories independently satisfy the
> reviewed quasistationarity profile?

These are deliberately separate claims.
