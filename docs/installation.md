# Installation and bootstrap

NRG AI Laboratory Assistant v0.6 is deployed from a **Git checkout**.  It is an
agentic research supplement to the external NRG CFD package; it does not ship or
build NRG itself.

## Prerequisites

Required for the Python laboratory layer:

- Python 3.11 or newer;
- Python `venv` and `pip` support.

Required for the full Pi-based Laboratory Assistant:

- Node.js 20 or newer;
- Pi installed separately;
- Git is strongly recommended for provenance.

Required for actual CFD execution:

- an independently cloned/built NRG installation;
- a compatible `computing_module` executable;
- the validated problem-specific package-interface executable;
- the NRG `package_interface/task_setup` resources used by that interface.

See `nrg-integration.md`.

## Recommended bootstrap

From any freshly cloned repository location:

```bash
python3 scripts/bootstrap.py --dev
```

The script:

1. locates this repository from its own file location rather than the current
   shell directory;
2. creates `.venv/` if needed;
3. installs this checkout with `pip install -e .`;
4. runs read-only repository/environment diagnostics.

It does **not** install Pi, Node.js, a Fortran compiler, or NRG.  Missing NRG is
therefore an external-integration warning during normal bootstrap rather than a
bootstrap failure.

To create the local configuration template during bootstrap:

```bash
python3 scripts/bootstrap.py --dev --init-local-config
```

For an environment that already contains a suitable setuptools build backend
and must not use PEP-517 build isolation:

```bash
python3 scripts/bootstrap.py --offline
```

The bootstrap is safe to rerun.  Existing `.venv/` and
`config/laboratory.local.toml` are preserved.

## Configure NRG

A convenient local arrangement is to clone NRG into the ignored directory:

```text
.local/NRG/
```

and build it there.  The committed configuration uses this layout as a portable
convention, but no such directory is included in Git.

If NRG lives elsewhere, create:

```bash
nrg-lab-config init-local
```

then set the NRG paths in `config/laboratory.local.toml`.

Before scientific execution verify:

```bash
nrg-lab-config validate
nrg-lab-doctor --require-nrg
```

## Manual Python installation

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
python -m pytest -q
```

## Installed commands

```text
nrg-lab
nrg-lab-config
nrg-lab-doctor
```

Examples:

```bash
nrg-lab-config show
nrg-lab-doctor
nrg-lab-doctor --require-nrg
nrg-lab lab-info
```

`nrg-lab` operations that depend on trusted NRG execution require the external
NRG paths to validate successfully.

## Pi

Start Pi using this cloned repository as the project root.  The repository
contains project-local `.pi/extensions/` and `.pi/skills/` resources.  The NRG
extension prefers `<repository>/.venv/bin/python` (or the Windows venv
equivalent) when bootstrap created it; `NRG_PYTHON` remains the explicit
override.
