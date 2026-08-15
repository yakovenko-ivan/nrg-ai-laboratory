# Laboratory configuration

NRG AI Laboratory Assistant v0.6 separates portable repository configuration
from machine-local NRG/runtime configuration.

## Configuration layers

The committed base file is:

```text
config/laboratory.toml
```

It contains portable defaults.  For convenience those defaults assume an
**ignored** NRG checkout at `.local/NRG`, but no NRG files are included in this
repository.

For a real workstation copy:

```text
config/laboratory.local.toml.example
```

to:

```text
config/laboratory.local.toml
```

and edit only values that differ locally.  The local file is an overlay:
omitted keys inherit from the base configuration.

The effective precedence is:

1. explicit base configuration selected by `--laboratory`;
2. otherwise `NRG_LABORATORY_CONFIG`;
3. otherwise repository `config/laboratory.toml`;
4. explicit trusted-code `local_path`, when supplied;
5. otherwise `NRG_LABORATORY_LOCAL_CONFIG`;
6. otherwise sibling `config/laboratory.local.toml`, if present.

The local layer overrides corresponding base keys.

## Path resolution

`research_root = ".."` is resolved relative to `config/laboratory.toml`, so it
points to this repository regardless of clone location.  Other relative paths
are resolved under the effective `research_root`.

Environment variables and `~` are expanded before path resolution.

## External NRG paths

The current 0-D laboratory family needs:

```text
paths.task_setup_template
runtime.computing_module
runtime.package_interface_0d
```

These point to resources owned by the external NRG project.  The simplest
local override uses explicit machine-local paths:

```toml
[paths]
task_setup_template = "/absolute/path/to/NRG/package_interface/task_setup"

[runtime]
computing_module = "/absolute/path/to/NRG/build/bin/computing_module"
package_interface_0d = "/absolute/path/to/NRG/build/bin/package_interface_0D_ignition_delay_campaign"
```

Environment-expanded paths are also supported:

```toml
[paths]
task_setup_template = "${NRG_SOURCE_ROOT}/package_interface/task_setup"

[runtime]
computing_module = "${NRG_BUILD_ROOT}/bin/computing_module"
package_interface_0d = "${NRG_PACKAGE_INTERFACE_0D}"
```

In that form, `NRG_SOURCE_ROOT`, `NRG_BUILD_ROOT`, and
`NRG_PACKAGE_INTERFACE_0D` must already exist in the **process environment**
(for example via `export` in the shell) before the laboratory is started.  A
TOML key named `NRG_SOURCE_ROOT` does not define an environment variable and is
not available for `${NRG_SOURCE_ROOT}` substitution.  Expansion is generic;
the loader does not require these particular environment-variable names.

See `nrg-integration.md` for NRG build-layout details.

## Local research workspaces

These committed defaults remain relative to this repository:

```toml
campaign_root = "campaigns"
runs_root = "runs"
studies_root = "agent_workspace/studies"
```

They are **local scientific workspaces**, not source-repository content.  They
may be absent in a fresh clone.

Large-data locations can be overridden locally, for example:

```toml
[paths]
runs_root = "${NRG_RUNS_ROOT}"
```

## Environment selectors

Special environment selectors are:

- `NRG_LABORATORY_CONFIG` — alternate base TOML;
- `NRG_LABORATORY_LOCAL_CONFIG` — alternate local overlay;
- `NRG_PYTHON` — Python executable used by the Pi extension;
- `NRG_READ_ONLY_SHELL_POLICY` — alternate trusted read-only shell policy.

Any TOML path may additionally use ordinary environment variables.

## Provenance

When a local configuration layer is active, trusted generation/execution
provenance records both base and local configuration paths and hashes.  Trusted
campaign generation and execution also hash the actual NRG executables used.
Thus externalizing NRG from this source repository does not make runtime choice
implicit.

## Configuration command

```bash
nrg-lab-config init-local
nrg-lab-config show
nrg-lab-config validate --no-runtime
```

`--no-runtime` validates the repository/configuration layer without requiring an
installed NRG runtime.  Before actual campaign generation/execution use the full:

```bash
nrg-lab-config validate
```
