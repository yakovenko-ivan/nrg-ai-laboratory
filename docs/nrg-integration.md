# Integrating the external NRG CFD package

NRG AI Laboratory Assistant is a supplement to the NRG CFD software, not a
redistribution of NRG.  The authoritative solver, physical models, Fortran
package interfaces, and runtime resources are maintained in:

https://github.com/yakovenko-ivan/NRG

The upstream repository separates the core `computing_module`, the
`package_library`, and problem-specific `package_interface` sources.  Its CMake
configuration writes runtime executables to the build `bin` directory for
single-config generators.  NRG's package-interface tree also owns
`package_interface/task_setup`, including chemistry and thermophysical data.

## Recommended local layout

The committed portable configuration supports the following convenient layout:

```text
nrg-ai-laboratory/
  .local/
    NRG/              # separate Git checkout, ignored here
      build/
        bin/
      package_interface/
        task_setup/
```

Clone/build NRG independently according to the upstream NRG documentation.  The
assistant repository does not invoke a hidden build step and does not vendor the
resulting binaries.

## Required laboratory resources

For the currently validated 0-D campaign family, the local laboratory must
resolve three NRG-owned resources:

```text
paths.task_setup_template
runtime.computing_module
runtime.package_interface_0d
```

Typical local configuration for an NRG checkout elsewhere on disk is:

```toml
[paths]
task_setup_template = "/path/to/NRG/package_interface/task_setup"

[runtime]
computing_module = "/path/to/NRG/build/bin/computing_module"
package_interface_0d = "/path/to/NRG/build/bin/package_interface_0D_ignition_delay_campaign"
```

On Windows or with a multi-config CMake generator the executable directory may
contain an additional `Release`/`Debug` component.  The package-interface
filename also depends on the interface source selected in the NRG CMake
configuration; point `package_interface_0d` to the validated 0-D interface used
for this laboratory workflow.

Environment-expanded paths are also supported, for example:

```toml
[paths]
task_setup_template = "${NRG_SOURCE_ROOT}/package_interface/task_setup"

[runtime]
computing_module = "${NRG_BUILD_ROOT}/bin/computing_module"
package_interface_0d = "${NRG_PACKAGE_INTERFACE_0D}"
```

These environment-variable names are conventions for the local configuration;
the TOML path resolver expands ordinary **process environment variables**
generically.  They must therefore be exported/set before starting the
laboratory, for example:

```bash
export NRG_SOURCE_ROOT=/path/to/NRG
export NRG_BUILD_ROOT=/path/to/NRG/build
export NRG_PACKAGE_INTERFACE_0D=/path/to/NRG/build/bin/package_interface_0D_ignition_delay_campaign
```

Do not place `NRG_SOURCE_ROOT = "..."` or similar helper assignments inside
`laboratory.local.toml` and expect later `${NRG_SOURCE_ROOT}` references to use
them; TOML keys are configuration values, not environment-variable definitions.

## Validation boundary

Use:

```bash
nrg-lab-doctor
```

to validate this repository itself.  Missing NRG resources are reported as
external integration warnings.

Before generating/preparing/running real NRG campaigns, use:

```bash
nrg-lab-doctor --require-nrg
nrg-lab-config validate
```

The trusted runner still hashes and records the actual executable paths used for
scientific provenance.  Removing executables from this Git repository does not
weaken execution provenance.
