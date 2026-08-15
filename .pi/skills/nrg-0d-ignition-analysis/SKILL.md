---
name: nrg-0d-ignition-analysis
description: Analyze homogeneous zero-dimensional or uniform-surrogate ignition campaigns in NRG. Use for ignition-delay definitions, hydrogen/air mechanism comparisons, temperature-pressure sweeps, radical diagnostics, and post-ignition quasistationary product-state analysis.
---

# NRG 0D Ignition Analysis

Use this skill together with `nrg-study-analysis` for homogeneous-reactor ignition studies, including NRG's uniform small-domain surrogate for a 0D reactor.

Unless the study explicitly states otherwise, the conventions below are for an adiabatic closed constant-volume reactor. Do not silently reuse constant-volume diagnostics or termination assumptions for constant-pressure or open-reactor studies.

## 1. Metric hierarchy

For the current constant-volume ignition workflow, use the following default roles unless the scientific objective requires another reviewed definition.

### Primary

`tau_dTdt_s` — physical time of maximum `dT/dt`.

### Secondary

- `tau_dpdt_s` — physical time of maximum `dp/dt`;
- `tau_Tplus400_s` — first upward crossing of `T0 + 400 K`, interpolated between the bracketing time samples.

### Radical diagnostics

Where the required species exist:

- time of maximum OH growth rate;
- time of peak OH;
- time of maximum H growth rate;
- time of peak H.

Do not automatically treat radical peaks as equivalent ignition-delay definitions. They characterize radical-pool dynamics and may occur before or after the principal thermal event.

## 2. Use trusted numerical implementations

Use the existing `nrg_analysis.ignition` and `nrg_analysis.timeseries` routines for nonuniform-time differentiation and ignition metrics.

Do not implement derivative metrics as ad hoc `gradient(value) / gradient(time)` if a trusted implementation is available.

For threshold metrics, identify the first upward crossing and interpolate in time between the samples that bracket the threshold. Do not assume temperature is globally monotonic merely to use value-axis interpolation.

## 3. Boundary/truncation diagnostics

For every derivative-based ignition metric, record enough information to detect an unresolved endpoint extremum:

- extremum sample index;
- total sample count;
- normalized position in history;
- distance from start and end;
- near-start flag;
- near-end flag.

A derivative maximum at or near the final sample is a truncation warning, not evidence that the true ignition delay equals the simulation horizon.

Report aggregate boundary-flag counts in the study summary.

## 4. Post-ignition quasistationary product state

When the study requires a stable post-ignition state, use the trusted quasistationarity implementation and the reviewed termination profile applicable to the campaign.

For the current H2 constant-volume workflow, the established profile is:

`0d_cv_post_ignition_quasistationary_v1`

Use the accepted physical-time window to calculate averages of:

- temperature;
- pressure;
- density;
- available species mass fractions.

Call this a **post-ignition quasistationary product state**, not thermodynamic equilibrium unless an independent equilibrium calculation establishes that equivalence.

The window average is the representative product state; do not substitute the final instantaneous history sample.

Where the runtime stored a `product_state`, independently recompute the same window average and compare both values as a consistency check.

## 5. Constant-volume integrity diagnostics

For closed constant-volume cases, calculate appropriate checks such as:

- maximum `abs(sum(Y_k)-1)` over history — describe as **mass-fraction closure** only;
- relative bulk-density variation over history and/or the product window — interpret as consistency with the closed constant-volume formulation;
- history duration and number of samples.

Do not claim that mass-fraction closure proves elemental conservation.

Do not interpret near-constant density across mechanisms as an independent kinetic prediction: in a closed constant-volume reactor bulk density is strongly constrained by total mass and volume.

## 6. Temperature dependence

At each fixed pressure and for each mechanism, analyze ignition delay over the full temperature series.

Report:

- actual values;
- monotonicity;
- transformed coordinates only when clearly defined;
- exceptions to any global trend.

If using logarithms, ensure terminology exactly matches implementation:

- `np.log` -> `ln`;
- `np.log10` -> `log10`.

Do not state an activation energy or Arrhenius law from visual curvature alone unless it is actually fitted and its range/assumptions are reported.

## 7. Pressure dependence

At each fixed temperature and mechanism, analyze ignition delay over the full pressure series.

For every adjacent pressure pair calculate signed and relative changes and classify the complete series algorithmically.

Repeat the classification for the principal secondary ignition definitions when possible. If `tau_dTdt`, `tau_dpdt`, and `tau_Tplus400` produce different trend classifications, report the disagreement explicitly.

Pressure-dependent behavior in hydrogen ignition can motivate mechanistic hypotheses, but this campaign alone does not establish which elementary reactions cause a trend. Claims about HO2/H2O2, third-body, branching, or termination pathways require rate-of-production, sensitivity, or other targeted evidence.

## 8. Mechanism comparison

At every fixed `(T0, P0)` condition:

- rank mechanisms by the primary ignition metric;
- record fastest and slowest mechanisms;
- calculate `max/min` ratio;
- absolute spread;
- relative spread with a documented denominator.

Summarize how often rankings change with temperature or pressure. Do not claim one global ranking unless it is invariant over the complete grid.

Large mechanism ratios are scientifically important even when product-state bulk properties remain similar.

## 9. Ignition-definition agreement

Compare primary and secondary ignition metrics using both absolute and relative differences.

For relative comparisons, use a documented reference such as:

`abs(tau_secondary - tau_dTdt) / tau_dTdt`

Report at least median, 95th percentile, and maximum, and identify the cases producing the largest discrepancies.

Do not call metrics interchangeable solely because their absolute difference is small; the significance of an absolute offset depends on the ignition timescale.

## 10. Product-state mechanism spread

At fixed `(T0, P0)`, compare mechanism-dependent product-state quantities using exact full-grid extrema and relative spreads.

Treat bulk thermodynamic quantities separately from radicals/minor species:

- small temperature/pressure spreads can indicate similar post-ignition thermodynamic states;
- radical/minor-species relative spreads can be much larger because their denominators are small and their concentrations remain kinetically sensitive.

State the relative-spread definition explicitly.

## 11. Plotting products

Follow the `nrg-plot-data` skill.

For mechanism comparisons, prefer one independent variable per file and mechanism columns, for example:

```text
VARIABLES="P0_atm" "KONNOV" "KEROMNES" "TEREZA" "ZHANG"
```

or:

```text
VARIABLES="T0_K" "KONNOV" "KEROMNES" "TEREZA" "ZHANG"
```

Generate separate files for `tau_dTdt`, `tau_dpdt`, and `tau_Tplus400` when comparing pressure or temperature trends.

Keep radical diagnostics in supplementary outputs unless they are central to the hypothesis.

## 12. Scientific wording

Use terminology supported by the calculation:

- `post-ignition quasistationary product state`, not automatically `equilibrium`;
- `computational characterization`, not `experimentally grounded` unless experiments are included;
- `consistent with` for plausible kinetic interpretation unless causal analysis has been performed.

When a surprising mechanism or pressure trend appears, verify the underlying per-case table before explaining it chemically.
