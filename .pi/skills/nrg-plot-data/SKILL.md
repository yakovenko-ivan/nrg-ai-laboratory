---
name: nrg-plot-data
description: Generate dense Tecplot/gnuplot-ready ASCII .dat tables from NRG scientific study results using the canonical laboratory plotting contract.
---

# NRG Plot Data Convention

Use this skill whenever a study produces parameter-sweep curves, mechanism comparisons, ignition-delay curves, product-state curves, or whenever the user asks for Tecplot/gnuplot plotting data.

Scientific authority remains:

`raw histories -> structured per-case results -> aggregate scientific tables -> plotting export -> figures`

Plotting `.dat` files are derived products. Never use them as the authoritative source for scientific calculations when structured study results are available.

## Mandatory trusted exporter

All Tecplot `.dat` files produced by an NRG study must be written through `nrg_analysis.plot_data`.

Prefer:

```python
from nrg_analysis.plot_data import (
    write_grouped_metric_tecplot_tables,
    write_pivoted_tecplot,
    write_tecplot_point_table,
)
```

Do not manually assemble Tecplot headers or numerical rows in study code. Do not reproduce legacy sparse CSV layouts and then rename them to `.dat`.

## Fundamental row-layout rule

Within one Tecplot zone, one physical value of the independent variable corresponds to exactly one numerical row.

For example, a fixed-pressure temperature sweep with 6 temperatures and 4 mechanisms must contain exactly 6 numerical rows, not 24 rows.

A fixed-temperature pressure sweep with 5 pressures and 4 mechanisms must contain exactly 5 numerical rows, not 20 rows.

Wrong:

```text
1.0 1.63e-4 0        0        0
1.0 0        3.53e-4 0        0
1.0 0        0        1.27e-4 0
1.0 0        0        0        1.65e-4
```

Correct:

```text
1.0 1.63e-4 3.53e-4 1.27e-4 1.65e-4
```

Never use zero as a placeholder for another mechanism or another series. Zero is a physical numerical value. Use the dense pivoted representation; if a genuinely unavailable value must be represented, use `NaN`.

## Canonical Tecplot ASCII header

The laboratory contract requires exactly these header concepts:

```text
TITLE = "descriptive title"
VARIABLES = "x_variable" "series_1" "series_2" ...
ZONE T = "descriptive zone"
```

Then write the numerical rows.

Do not add any of the following unless the user explicitly requests them:

- `N=`
- `E=`
- `DATAPACKING=`
- `I=`
- `J=`
- `K=`
- `F=`

The default NRG exporter intentionally emits only `TITLE`, `VARIABLES`, and `ZONE` before the data block.

## In-file semantic completeness

A Tecplot file must be understandable from its contents without relying on the filename. The filename is useful navigation metadata, but it is not sufficient scientific metadata.

For every dependent dataset, the physical quantity and, when relevant, the measurement definition must be identifiable from at least one in-file source:

1. the dependent `VARIABLES` names (preferred for mechanism-comparison files), or
2. an explicit `TITLE`, or
3. an explicit `ZONE T` label.

For example, this is ambiguous even though the filename may contain `tau_dTdt`:

```text
TITLE = "Ignition delay vs inverse temperature, P0=1atm"
VARIABLES = "1000_over_T0" "KONNOV" "KEROMNES" "TEREZA" "ZHANG"
ZONE T = "P0=1atm"
```

The file says that the columns are ignition delays, but it does not identify that the ignition-delay definition is `tau_dTdt`. Do not require the user to infer that from the filename.

The preferred representation remains self-describing dependent variables:

```text
TITLE = "Ignition delay vs inverse temperature, P0=1atm"
VARIABLES = "1000_over_T0" "tau_dTdt_Konnov" "tau_dTdt_Keromnes" "tau_dTdt_Tereza" "tau_dTdt_Zhang"
ZONE T = "P0=1atm"
```

However, compact bare mechanism names are acceptable when the measurement definition is explicit in the in-file metadata, for example:

```text
TITLE = "Ignition delay tau_dTdt vs inverse temperature, P0=1atm"
VARIABLES = "1000_over_T0" "KONNOV" "KEROMNES" "TEREZA" "ZHANG"
ZONE T = "tau_dTdt, P0=1atm"
```

Thus a bare mechanism name is not automatically invalid. It is invalid only when the combination of `VARIABLES`, `TITLE`, and `ZONE` leaves the dependent quantity or measurement technique ambiguous.

When several definitions of the same physical quantity exist, include the definition token explicitly, e.g. `tau_dTdt`, `tau_dpdt`, or `tau_Tplus400`. Generic text such as `Ignition delay` is not sufficient to distinguish those metrics.

The trusted helper `validate_semantic_context()` can enforce this contract, and `write_pivoted_tecplot(..., semantic_label=...)` performs the check during export.

### Preferred self-describing variable names

For mechanism-comparison files, names that combine the quantity, measurement definition, mechanism and, where useful, unit remain preferred:

```text
VARIABLES = "1000_over_T0" "tau_dTdt_Konnov" "tau_dTdt_Keromnes" "tau_dTdt_Tereza" "tau_dTdt_Zhang"
```

Similarly, product-state curves should use names such as:

```text
"Tproduct_Konnov"
"Tproduct_Keromnes"
"Pproduct_Tereza"
```

The trusted helper `compose_series_name()` can be used to build these names, and `write_pivoted_tecplot(..., series_label_map=...)` can relabel categorical mechanism series after pivoting.

A filename should likewise describe the numerical quantity actually stored. If a file contains ignition-delay curves used to infer mechanism ranking, prefer a filename such as:

`tau_dTdt_vs_pressure_T1500K.dat`

over an interpretive filename such as:

`mechanism_ranking_T1500K.dat`

Ranking itself belongs in structured tables or in a dedicated ranking table with explicit rank variables.

## Mechanism comparison

At fixed secondary parameters, use one dependent metric per file and place mechanisms in columns.

Ignition delay versus pressure at fixed temperature:

```text
TITLE = "Ignition delay vs pressure, T0=1500 K"
VARIABLES = "P0_atm" "tau_dTdt_Konnov" "tau_dTdt_Keromnes" "tau_dTdt_Tereza" "tau_dTdt_Zhang"
ZONE T = "T0=1500 K"
1.0 ... ... ... ...
2.0 ... ... ... ...
3.0 ... ... ... ...
4.0 ... ... ... ...
5.0 ... ... ... ...
```

Ignition delay versus inverse temperature at fixed pressure:

```text
TITLE = "Ignition delay vs inverse temperature, P0=2 atm"
VARIABLES = "1000_over_T0" "tau_dTdt_Konnov" "tau_dTdt_Keromnes" "tau_dTdt_Tereza" "tau_dTdt_Zhang"
ZONE T = "P0=2 atm"
1.000000 ... ... ... ...
0.909091 ... ... ... ...
...
```

Separate files should normally be generated for `tau_dTdt`, `tau_dpdt`, and `tau_Tplus400` rather than putting all metrics into one extremely wide mechanism-comparison table.

## Ignition-metric comparison

Ignition-definition comparison must never hide a categorical dimension in repeated numerical rows. If the source records contain several mechanisms, split the output by mechanism before writing the Tecplot table.

Wrong for a fixed pressure:

```text
T0_K tau_dTdt_s tau_dpdt_s tau_Tplus400_s
1000 ... ... ...   # KONNOV, but mechanism is not represented
1000 ... ... ...   # KEROMNES, but mechanism is not represented
1000 ... ... ...   # TEREZA, but mechanism is not represented
1000 ... ... ...   # ZHANG, but mechanism is not represented
```

This violates the one-row-per-independent-variable contract and makes the rows semantically undecodable.

Correct: generate one file per mechanism. For example:

`ignition_metrics_vs_T0_Konnov_P5atm.dat`

```text
TITLE = "Ignition metrics vs temperature, Konnov, P0=5 atm"
VARIABLES = "T0_K" "tau_dTdt_s" "tau_dpdt_s" "tau_Tplus400_s"
ZONE T = "Konnov, P0=5 atm"
1000 ... ... ...
1100 ... ... ...
1200 ... ... ...
1300 ... ... ...
1400 ... ... ...
1500 ... ... ...
```

Generate analogous files for Keromnes, Tereza and Zhang. The same rule applies to pressure sweeps: one mechanism per metric-comparison file unless the mechanism itself is represented explicitly by dedicated dependent-variable columns.

Prefer the trusted `write_grouped_metric_tecplot_tables()` helper for this case. It partitions records by the categorical field and then applies the same unique-x dense-table validation to every output file.

Do not emit repeated x values whose distinction depends on row order, comments, or external knowledge. Every categorical distinction needed to interpret the numeric rows must appear either in the filename/zone because the file has been split by that category, or explicitly in the variables themselves.

## Product-state comparison

Use the same dense convention and self-describing quantity-plus-mechanism names. Example:

```text
VARIABLES = "P0_atm" "Tproduct_Konnov" "Tproduct_Keromnes" "Tproduct_Tereza" "Tproduct_Zhang"
```

for product temperature versus pressure at one fixed initial temperature.

Density should normally remain a numerical-consistency diagnostic when the physical formulation constrains it and a mechanism-comparison curve adds little scientific information.

## Human-readable filenames

File names must identify the quantity and fixed condition without encoded decimal-padding conventions.

Preferred examples:

```text
tau_dTdt_vs_invT_P1atm.dat
tau_dTdt_vs_invT_P2atm.dat
tau_dTdt_vs_pressure_T1500K.dat
Tproduct_vs_T0_P5atm.dat
Tproduct_vs_pressure_T1200K.dat
```

Do not generate names such as:

```text
P2p000atm
T1500p000K
```

For integer values, omit unnecessary decimal places entirely: use `P2atm`, not `P2.000atm` or `P2p000atm`.

For genuinely fractional values, use a compact human-readable decimal form such as `P2.5atm`.

The helper `format_parameter_label()` from `nrg_analysis.plot_data` may be used to construct compact labels.

## Directory structure

For studies with curve data, prefer:

```text
results/plot_data/
  vs_pressure/
  vs_temperature/
  metric_comparison/
  product_state/
  supplementary/
```

Keep the independent variable, metric, and fixed condition obvious from the file name.

## Study-code pattern

If `case_results` contains one record per logical case:

```python
from nrg_analysis.plot_data import write_pivoted_tecplot

subset = [r for r in case_results if r["T0_K"] == 1500.0]

write_pivoted_tecplot(
    output_dir / "plot_data/vs_pressure/tau_dTdt_vs_pressure_T1500K.dat",
    subset,
    x_field="P0_atm",
    series_field="mechanism",
    value_field="tau_dTdt_s",
    series_order=["KONNOV", "KEROMNES", "TEREZA", "ZHANG"],
    series_label_map={
        "KONNOV": "tau_dTdt_Konnov",
        "KEROMNES": "tau_dTdt_Keromnes",
        "TEREZA": "tau_dTdt_Tereza",
        "ZHANG": "tau_dTdt_Zhang",
    },
    title="Ignition delay vs pressure, T0=1500 K",
    zone="T0=1500 K",
    semantic_label="tau_dTdt",
)
```

The trusted pivot performs the mechanism merge. Do not loop over mechanisms and append one sparse row for each mechanism.

## Acceptance checks

Before accepting a study's plotting outputs, inspect at least one fixed-pressure and one fixed-temperature `.dat` file and verify all of the following:

- the file begins with `TITLE`, `VARIABLES`, and `ZONE`;
- no `N=`, `E=`, `DATAPACKING=`, `I=`, or `F=` metadata is present;
- the number of numerical rows equals the number of unique independent-variable values;
- each independent-variable value appears exactly once;
- every expected mechanism/metric has its own column;
- the dependent quantity and measurement definition are identifiable from in-file metadata; self-describing variables such as `tau_dTdt_Konnov` are preferred, while bare mechanism names require an explicit metric such as `tau_dTdt` in `TITLE` or `ZONE`;
- no categorical dimension is hidden in repeated rows; metric-comparison files containing multiple mechanisms are split by mechanism or encode the mechanism explicitly;
- mechanisms are not represented by separate sparse rows;
- zeros are not used as missing-series placeholders;
- x values are sorted;
- file names use compact, immediately understandable parameter labels;
- values originate from structured study outputs, not from previously generated plot files.

For the standard H2 ignition campaign this means:

- fixed-pressure temperature/inverse-temperature files: 6 numerical rows;
- fixed-temperature pressure files: 5 numerical rows.

If any of these checks fail, correct the study export logic before reporting plotting data as complete.

## Legacy data

`campaign_tools/plot_data_export.py` may be used to repair older sparse tables for human use, but new studies should regenerate plotting products directly through `nrg_analysis.plot_data`.
