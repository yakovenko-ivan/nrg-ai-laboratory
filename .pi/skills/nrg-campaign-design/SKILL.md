---
name: nrg-campaign-design
description: Turn a CFD research hypothesis into a minimal, reproducible NRG campaign design. Use when the user asks to plan a new parameter sweep, add design points, compare models or mechanisms, design a convergence study, or decide which variables belong to logical case identity versus recalculation attempts.
---

# NRG Campaign Design

Use this skill to translate a scientific question into a declarative NRG campaign before generation or execution.

This skill guides scientific design. Trusted tools and `AGENTS.md` remain authoritative for validation, generation, preparation, reset, append, confirmation, and execution safety.

## 1. Start from the hypothesis

State the scientific question in a form that identifies:

- the quantity to compare or explain;
- the factor or model suspected to control it;
- the observables needed to discriminate alternatives;
- the smallest useful parameter space.

Prefer a targeted campaign that can falsify or distinguish hypotheses over a broad sweep with no analysis plan.

## 2. Classify every parameter semantically

For the proposed campaign, separate parameters into three classes.

### Logical identity axes

A parameter is an identity axis when varying it is part of the scientific design of this campaign.

Examples can include:

- initial temperature or pressure;
- mixture composition;
- chemical mechanism;
- geometry/resolution in a convergence campaign;
- physical-model selection when model comparison is the objective.

Changing an identity axis means a new logical case.

### Campaign constants

A parameter is a campaign constant when it defines the common experiment but is not swept and is not an ordinary recalculation knob.

Changing a protected campaign constant is not an ordinary same-case rerun. Treat it as a campaign revision/variant unless the campaign identity policy explicitly says otherwise.

### Attempt-tunable parameters

A parameter may be an attempt tunable when changing it does not alter the scientific identity of the logical case in this campaign.

Typical examples can include reviewed output cadence, run-control settings, or numerical controls used only to complete the same intended physical case.

The same parameter can belong to different classes in different campaigns. For example, CFL or time step may be an attempt tunable in a chemistry campaign but an identity axis in a numerical-convergence campaign.

Never classify identity solely from the current numerical value; classify it from the campaign's scientific design.

## 3. Design observables together with the sweep

Before generation, determine what output is required to answer the question.

Check that output cadence and requested fields can resolve:

- transient extrema;
- threshold crossings;
- conserved quantities;
- species or thermochemical diagnostics;
- spatial features or fronts;
- post-event/product-state criteria.

Do not request high-frequency or high-volume output without a scientific reason.

## 4. Separate scientific termination from safety ceilings

When a calculation requires a physical endpoint, identify the scientific stopping condition explicitly and keep it conceptually separate from wall-time or finite-horizon safety limits.

For workflows with an existing trusted physical termination profile, prefer that reviewed profile rather than inventing case-specific thresholds.

Do not describe `wall_time` run-control mode as proof that the wall-time limit was reached; execution provenance must come from the actual termination reason.

## 5. Numerical convergence campaigns

When numerical sensitivity is the scientific objective, numerical parameters being varied belong to logical case identity for that campaign.

Examples:

- grid spacing;
- CFL;
- initial time step;
- solver variant;
- discretization order.

Do not hide a convergence axis inside attempt overrides, because that would collapse scientifically distinct calculations into one logical case.

## 6. Prefer staged designs when appropriate

For expensive multidimensional CFD, consider a staged campaign:

1. pilot/sanity cases;
2. minimal discriminating sweep;
3. refinement or extension only where results justify it.

Use lower-dimensional or simplified calculations as screening tools only when their limitations are explicitly understood.

## 7. Existing campaign growth

When adding genuinely new design points to an existing campaign:

- preserve the existing identity schema;
- preview with `nrg_campaign_append_preview`;
- use `nrg_campaign_append` only for true new logical cases;
- preserve existing case IDs and assign new IDs monotonically;
- do not use extension/composite merely to recalculate an existing logical case.

For same-case recalculation with reviewed attempt-tunable changes, use the trusted reset/attempt workflow defined by `AGENTS.md` rather than creating a duplicate scientific case.

## 8. Campaign-design record

A well-specified campaign definition should make it possible to answer:

- What hypothesis is being tested?
- What are the identity axes?
- What are the campaign constants?
- What fields are attempt tunable?
- What outputs are required and why?
- What constitutes successful physical completion?
- What analysis study will consume the results?

Record these choices in the declarative campaign definition and accompanying comments/documentation when the format supports them.

## 9. Preview before execution

Use the normal trusted workflow from `AGENTS.md`:

- discover existing definitions rather than guessing names;
- preview before generation;
- inspect generated/status state before preparation;
- inspect status again before execution;
- require the user's explicit confirmation for CFD start.

This skill must never be used as justification to bypass trusted tool validation or confirmation requirements.
