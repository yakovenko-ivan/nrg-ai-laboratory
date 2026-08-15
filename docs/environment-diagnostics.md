# Environment diagnostics

`nrg-lab-doctor` is a read-only diagnostic command.  It does not create
campaigns, modify runtime files, build NRG, or start CFD.

## Repository readiness

Run:

```bash
nrg-lab-doctor
```

The default mode validates this repository and reports external NRG integration
separately.  Missing NRG resources are warnings because NRG is intentionally not
bundled.

Required repository checks include:

- Python 3.11+;
- imports of `nrg_analysis`, `agent_workspace`, and `campaign_tools`;
- portable/local laboratory configuration loading;
- trusted campaign-runner and physical-termination policy files;
- project-local Pi extension and skill directories;
- absence of a bundled `nrg_runtime/` tree.

External NRG checks include:

- `package_interface/task_setup` availability;
- `computing_module` availability/executable permission;
- validated 0-D package-interface availability/executable permission;
- complete laboratory runtime validation.

The summary reports both:

```text
repository_ready
nrg_runtime_ready
```

## Execution readiness

Before running a real campaign use:

```bash
nrg-lab-doctor --require-nrg
```

This promotes the NRG integration checks to required failures.

## Strict workstation validation

```bash
nrg-lab-doctor --strict
```

Strict mode returns nonzero for any optional warning, including missing Git,
Node.js/Pi, or external NRG integration.

The report shows supported `NRG_*` overrides when they are active but does not
enumerate unrelated environment variables.
