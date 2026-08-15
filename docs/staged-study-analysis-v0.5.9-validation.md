# Validation Record — v0.5.9 Staged Study Analysis

> Historical focused validation for the v0.5.9 patch.  The functionality is now
> integrated into v0.6.0 and is also covered by the complete v0.6 regression suite
> and merge-validation checks documented in `validation.md`.

Validated in the artifact build environment:

- Python syntax compilation succeeds for:
  - `agent_workspace/lab_bridge.py`
  - `agent_workspace/run_study.py`
  - controlled template `analyze.py`
- focused unit tests: **4 passed**;
- TypeScript syntax/type-shape check of the updated extension passed against lightweight declaration stubs plus the existing `read_only_shell.ts`;
- pilot CSV filtering preserves source campaign order and rejects stale selection assumptions;
- pilot validation is invalidated when `analyze.py` changes;
- deterministic pilot planning is not equivalent to selecting the first N cases;
- a synthetic 720-case design with axes 6 H2 x 6 T x 5 P x 4 mechanisms obtains full individual-axis level coverage with a 20-case pilot;
- the same planner was exercised against the earlier 120-case H2 mechanism campaign and a 20-case pilot covered all 6 temperatures, all 5 pressures, and all 4 mechanisms.

The focused test command used was equivalent to:

```bash
python tests/test_staged_study_analysis_v059.py
```

within a project environment containing the existing trusted NRG analysis modules.

Full end-to-end Pi execution should be checked after installation with `/reload`, `/nrg-tools`, one `nrg_study_pilot_plan`, and one bounded `nrg_run_study_pilot` before using the production full-study path.
