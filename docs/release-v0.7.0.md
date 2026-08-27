# Release v0.7.0

NRG AI Laboratory v0.7.0 extends the laboratory from a locally controlled
campaign system into a portable execution and restart-aware research workflow.

## Highlights

- Added restartable campaign pause/resume with two operator modes:
  - immediate checkpointed pause (`pause now`);
  - deferred pause after the current case (`pause after current`).
- Preserved terminal campaign/case stop semantics separately from restartable pause.
- Added operator-control supersession so an acknowledged deferred pause may be
  escalated to immediate pause or terminal campaign stop.
- Added verified NRG checkpoint handshake for trusted paused/restart-required states.
- Added portable campaign export/import and a standalone Python 3.11+ execution client.
- Added portable relative case locators and machine-local execution configuration.
- Generalized runtime package-interface selection for 0D and 1D campaigns.
- Added the deterministic 1D H2-air wall/radial flame campaign workflow.
- Added compact operational JSON by default, with `--full` retained for forensic output.
- Added and expanded regression coverage for pause/resume, operator control,
  portable execution, campaign identity, and live-runner state.
- Added an end-to-end user guide for manual and Pi/agentic research workflows.

## Validated operator-control workflow

The local 1D smoke campaign validated:

```text
pause now
  -> NRG external_pause_request
  -> verified restart checkpoint
  -> ordinary campaign resume

pause after current
  -> acknowledged while current case continues
  -> current case finishes normally
  -> next case is not launched
  -> job becomes paused
  -> ordinary campaign start skips the finished case
  -> remaining case runs
  -> campaign completes

pause after current
  -> terminal campaign stop supersession
  -> graceful NRG external_stop_request
  -> no force kill
```

## Portable execution

A prepared campaign may now be exported as a self-contained execution bundle.
The execution host needs Python 3.11+ and a compatible NRG `computing_module`,
but does not require Pi or the campaign package-interface executable.

The portable executor supports:

```text
validate
run
resume
status
pause --mode now
pause --mode after-current
stop
pack-results
```

Returned results are imported through the trusted laboratory import path rather
than copied directly into `runs/`.

## Compatibility and provenance

Logical case identity remains separate from attempt configuration and runner-job
identity. Pause/resume continues the same logical case/attempt rather than
creating a new calculation. Completed cases remain protected by execution policy.

Restart checkpoints are raw NRG binary state and should be moved only between
binary-compatible runtimes; identical build provenance is preferred.

## Documentation

See:

- `docs/user-guide.md`
- `docs/configuration.md`
- `docs/nrg-integration.md`
- `docs/pi-integration.md`
- `docs/validation.md`

## Validation before release

Recommended release checks:

```bash
python -m pytest tests/test_operator_control_supersession.py -v
python -m pytest tests/test_campaign_pause_resume.py -v
python -m pytest tests/test_portable_execution_bundle.py -v
python -m pytest -v
git diff --check
nrg-lab-doctor
nrg-lab-config validate
```

The portable execution round-trip should continue to be integration-tested
before using a new remote compute environment for production campaigns.
