# NRG Laboratory Agent Instructions

## Scientific-first development context (2026-10-16)

The [three modes](docs/scientific-modes.md), [capability roadmap](docs/scientific-roadmap.md), and [first equilibrium mission](docs/mode1-equilibrium-pilot.md) describe the intended scientific-first product. They are **not** permission expansions. All restrictions below remain binding on ordinary Pi sessions: no unauthorized NRG source modification or compilation, trusted laboratory source edits, or arbitrary shell execution. Mode 1 interface-building and Mode 2 computing-core development require a separately approved implementation and authorization. Any infrastructure change must name a blocked scientific action, smallest remedy, new operational capability, and runnable acceptance test. Do not claim an equilibrium/Cantera verification or clean-machine installation was performed without actual evidence.

This project is a controlled agentic computational-research layer for the external NRG CFD package. NRG source code, task-setup resources, and executables are built and maintained in the separate NRG project; this repository selects them through local configuration.

## Scientific role

Act as a Laboratory Assistant. Translate scientific objectives into declarative campaigns, inspect structured run state, create hypothesis-specific analysis studies, execute those studies, and propose targeted follow-up calculations.

## Trusted infrastructure

Do not modify `nrg_analysis/`, `campaign_tools/campaign_generator_0d.py`, `campaign_tools/campaign_runner.py`, `.pi/extensions/`, trusted files under `config/`, or raw case data in `runs/` during ordinary research work. These are trusted infrastructure or experimental records. The external NRG checkout/build is also outside the agent-writable scientific layer; do not rebuild or modify NRG through ordinary laboratory operations.

Use registered `nrg_*` tools for laboratory state changes, scientific execution, campaign control, and other operations for which a trusted laboratory tool exists. A restricted read-only Pi `bash` channel is available only for inspection as described below. User-entered `!` shell commands are separate and remain under user control.


## Guarded development transactions

A separate **development transaction** capability (`nrg_dev_*` tools) exists for explicitly approved infrastructure work on this repository only. It does **not** broaden ordinary `agent_write_roots`: trusted infrastructure remains read-only for ordinary research, and the `write`/`edit` path to `campaign_tools/`, `nrg_analysis/`, `.pi/extensions/`, and trusted `config/` stays blocked.

A development transaction creates an isolated Git worktree under `.local/dev_worktrees/<transaction_id>/` on an `agent-dev/<transaction_id>` branch, enforces an explicit per-transaction allowed-path set plus a hard denylist (`runs/`, `campaigns/`, `.git/`, `.venv/`, `.local/`, `agent_workspace/studies/`, `agent_workspace/portable_returns/`, `config/laboratory.local.toml`) plus a self-protected development-TCB list, and applies only reviewed patch artifacts through `plan -> confirm -> execute -> verify`. Every plan->execute transition is bound to the confirmed plan (expected patch SHA / head / candidate tree / branch). The authoritative candidate state is the worktree Git index (`candidate_tree_sha = git write-tree`), not HEAD alone. Test execution refuses if the working tree does not match the candidate index; a candidate mutation during a test yields `integrity_failed`. Only named trusted test profiles run; no arbitrary shell/CLI strings. Test execution is not an OS sandbox; candidate Python runs with the local user's permissions.

v1 stops at a committed development branch. The agent must **not** merge into `main`, push, create tags, or promote a runtime. Human review and promotion remain out-of-band. The development transaction's own trusted computing base (`agent_workspace/dev_transactions.py`, `config/dev_transaction_policy.json`, `agent_workspace/workspace_policy.json`, `agent_workspace/lab_bridge.py`, `.pi/extensions/nrg-laboratory/index.ts`, `.pi/extensions/nrg-laboratory/read_only_shell.ts`, `config/read_only_shell_policy.json`, `AGENTS.md`) is self-protected: ordinary `nrg_dev_*` transactions reject patches touching it. Changes to this security boundary require a separate out-of-band security bootstrap.

For ordinary UTF-8 source changes, prefer the semantic `nrg_dev_write` (create/replace) and `nrg_dev_edit` (exact bounded replacement) tools. The trusted transaction engine performs the worktree mutation, stages it, and generates/persists the canonical Git patch artifact; the agent should not hand-author or hash unified diffs for routine edits. `nrg_dev_apply_patch` remains available as the advanced path for an already reviewed external patch artifact under the configured studies workspace. There is no direct edit-to-production path.

During an active development transaction, transaction metadata returned by `nrg_dev_*` tools (baseline SHA, current HEAD, branch, worktree, candidate-tree SHA, staged paths) is authoritative. Do not rediscover the same state through shell Git unless a guarded tool explicitly lacks the information required for the task.
## Read-only shell inspection

- A restricted Pi `bash` tool may be used for read-only filesystem, text, disk-usage, checksum, and repository inspection when the needed information is not already exposed more directly by an `nrg_*` tool or Pi's built-in read/search tools.
- The shell allowlist is enforced by trusted extension code and trusted configuration. Typical permitted commands include `pwd`, `ls`, `tree`, `cat`, `head`, `tail`, `wc`, `du`, `df`, `stat`, `file`, `realpath`, `readlink`, `grep`, `rg`, restricted `find`, `uniq`, `cut`, `sha256sum`, `cmp`, `diff`, basic system-information commands, and explicitly read-only `git` subcommands.
- Shell inspection is scoped to the configured research root. Do not intentionally inspect paths outside that root.
- Shell commands must not create, modify, rename, or delete files; modify repository state; install packages; start or stop processes; compile or execute project code; run interpreters or arbitrary scripts; launch CFD; or bypass a registered NRG laboratory operation.
- Shell composition is intentionally restricted. Do not use output/input redirection, pipelines, background execution, command substitution, arbitrary command chaining, or execution features such as `find -exec`. A single `cd <directory> && <allowlisted read-only command>` may be accepted for convenience. A standalone `cd` does not persist across later tool calls, so prefer explicit path arguments.
- Read-only `git` access is for inspection only, such as status, log, diff, show, branch listing, revision parsing, and tracked-file listing. Do not use shell Git commands that create commits, branches, tags, checkouts, resets, merges, rebases, stashes, pushes, pulls, or otherwise alter repository state.
- If a shell command is blocked, treat that as a policy boundary. Do not try alternate shell syntax or another executable to bypass the restriction. Use a built-in inspection tool, an `nrg_*` tool, or ask the user if a genuinely state-changing infrastructure action is required.
- A shell-derived observation is not a substitute for structured laboratory state. Campaign/run state, case status, termination provenance, trusted resets, execution, and study execution must still be grounded through the corresponding `nrg_*` tools.

## Agent-writable scientific layer

You may create and modify:

- `agent_workspace/studies/<study>/` for scientific analysis code, methods, configs, plots, tables, and reports.
- `campaigns/definitions/` for declarative campaign specifications. This is a local scientific workspace created/used during research; campaign files and generated campaign state are not part of the portable source repository.

Analysis code is part of the scientific investigation and may change substantially between studies. Reusable parsing, chemistry conversion, numerical differentiation, campaign I/O, and provenance belong in `nrg_analysis/` and are not changed autonomously.

## Laboratory state grounding

- Never infer that a campaign has been generated, prepared, run, completed, failed, or analyzed from conversation history alone.
- Determine the current campaign state using the NRG laboratory discovery and status tools.
- Treat these as distinct states:
  - **DEFINED** — a campaign TOML definition exists.
  - **GENERATED** — `cases.csv` and generated setup inputs exist.
  - **PREPARED** — NRG case directories and their `task_setup/` data exist.
  - **RUNNING** — the campaign runner reports active calculations.
  - **TERMINAL** — execution has stopped and structured run status identifies one of:
    - **FINISHED** — normal successful completion.
    - **CONDITION_MET** — a scientific/monitoring stop criterion was satisfied.
    - **RESTART_REQUIRED** — execution stopped cleanly and should be continued from a checkpoint.
    - **PAUSED** — execution was deliberately paused with a verified restart checkpoint. It is incomplete and resumes as the same logical case/attempt.
    - **PAUSE_FAILED** — a pause was requested but a valid restart checkpoint was not verified; do not assume the case is restartable.
    - **INTERRUPTED** — a previous execution ceased unexpectedly; after verifying that no trusted `computing_module` process remains active, the case is recoverable and may be rerun.
    - **STOPPED** — execution was deliberately terminated by a trusted operator stop request. This is a clean operational stop, not a successful scientific completion; the case is rerunnable.
    - **TIMEOUT** — the runner or process exceeded its allowed execution time.
    - **FAILED** — execution terminated unsuccessfully.
  - **ANALYZED** — a study has produced its structured analysis outputs.
- These states describe laboratory artifacts and execution state rather than a strictly linear workflow. For example, analysis may legitimately be performed on a selected subset of completed cases while other cases remain unfinished.
- Files or runs may be created, deleted, regenerated, or changed outside Pi between turns. Re-check the current laboratory state whenever the requested action depends on it.
- Do not use directory-name guesses or conversation history as substitutes for laboratory discovery.
- If the filename of an existing campaign is unknown, use `nrg_list_campaigns`.
- If the current execution state is needed, use `nrg_campaign_status` or the appropriate job-status tool.
- Treat `campaigns/definitions/` as the canonical registry of agent-discoverable campaign definitions.
- If `nrg_list_campaigns` finds no matching campaign, do not search other directories by guessing paths or filenames. Report that no registered definition was found, and either ask whether a new campaign definition should be created or create one only when the user's request clearly authorizes it.
- Do not treat `condition_met` as a failure, and do not treat `restart_required`, `interrupted`, `stopped`, `timeout`, or `failed` as successful completion.

## Campaign workflow

1. Inspect the laboratory with `nrg_lab_info` when paths/runtime provenance matter.
2. If an existing campaign is referenced but its filename is unknown, use `nrg_list_campaigns`; do not guess filenames.
3. Preview a new campaign with `nrg_campaign_preview`.
4. Generate deterministic campaign files with `nrg_campaign_generate`; overwriting generated files requires user confirmation.
5. Inspect `nrg_campaign_status`, then prepare missing case directories with `nrg_campaign_prepare_cases`; the tool skips matching existing cases and asks for confirmation.
6. Inspect `nrg_campaign_status` again before execution.
7. Start CFD only with the trusted start tool appropriate to the campaign. Use `nrg_campaign_start` for ordinary execution only. When a trusted physical termination profile is intended to control scientific completion, use `nrg_campaign_start_to_quasistationary` or the corresponding selected-case physical-run tool rather than the generic start path. Never substitute `nrg_campaign_start` when a required physical-start tool is unavailable; report the missing capability and do not launch CFD. Start tools require explicit user confirmation and return a background job id. `nrg_campaign_start_to_quasistationary` is the campaign-wide start/resume operation and must automatically select all currently runnable cases regardless of campaign size. The selected-case physical-run tool is limited to small explicit subsets and must not be called repeatedly to partition a full campaign into batches.
- Campaign execution policy is trusted infrastructure. Do not create, edit, search for, or infer a `run_config.json` during ordinary campaign execution.
- `nrg_campaign_status` and trusted start tools automatically use the laboratory runner policy.
- When asked to preserve or skip already completed cases, inspect `execution_policy`, `skipped_by_policy`, and `runnable_by_status`; do not modify runner configuration unless the user explicitly requests an infrastructure-policy change.
8. Monitor with `nrg_campaign_job_status` rather than guessing from logs.
9. Prefer `nrg_campaign_pause` when the user intends to continue the campaign later. `mode=after_current` lets the active case finish normally and stops the runner before the next case. `mode=now` requests `run_control.pause`; NRG must write/register a restart checkpoint before the case is trusted as `paused`. Use `nrg_campaign_stop` only for deliberate terminal operational stopping, and `nrg_case_stop` only when the current case should be terminally stopped while later cases may continue. All operations require confirmation. Do not kill the runner or `computing_module` manually when a trusted control tool is available.
10. After a stop request, inspect the returned control state or `nrg_campaign_job_status`. `handled` means the runner applied the request; `requested` or `acknowledged` is still pending; `rejected` means it was not applied. Never claim a stop completed from the request alone.
11. After a VM or process interruption, re-check campaign status. A stale `running` record may be recovered by the trusted runner as `interrupted`; never delete successfully completed case data merely to resume a campaign.
12. Treat `finished`, `condition_met`, `restart_required`, `interrupted`, `stopped`, `timeout`, and `failed` as distinct scientific/operational states.

## Trusted operator stop control

- `nrg_campaign_pause` is the authoritative pause/resume operation. `after_current` never writes an NRG stop/pause marker; it only prevents the next case launch after the active case finishes. `now` writes `run_control.pause`, requires `external_pause_request` plus `restart_checkpoint_required = true`, and records `status = paused` only after that checkpoint contract is verified.
- Resume a paused campaign with the ordinary trusted campaign start operation on the same immutable `cases.csv`. Do not regenerate the campaign and do not use selective rerun for ordinary continuation; completed cases remain skipped while `paused`/`restart_required` cases remain runnable.
- After queueing `pause_after_current`, do not wait for case completion or repeatedly poll unless the user explicitly asks for status.
- The NRG laboratory extension explicitly activates every registered `nrg_*` tool at session start. If a required trusted tool still appears unavailable, use `/nrg-tools` to inspect registered/active tool state, reload the project extension if appropriate, and do not substitute a semantically different tool.
- `nrg_campaign_stop` is the authoritative operation for stopping the active campaign runner. The runner must stop launching new cases; if a CFD case is active, it first requests graceful NRG finalization through `run_control.stop` and uses the configured force-kill fallback only if NRG does not respond within the trusted graceful-stop timeout.
- `nrg_case_stop` is narrower: it may stop only the exact case that is currently running, and the campaign runner may continue with later cases afterward. Do not use it when the user's intent is to stop or pause the entire campaign.
- A user/operator stop is recorded as `status = stopped` unless an independent trusted scientific condition was already satisfied first. `stopped` is an incomplete operational result and must not be interpreted as `finished` or `condition_met`.
- For a graceful operator stop, the laboratory runner originates the stop request, writes `run_control.stop`, and NRG normally reports `nrg_termination_reason = external_stop_request`. Keep the operator reason (`operator_campaign_stop` or `operator_case_stop`) separate from the NRG low-level termination reason.
- A campaign-stop request is job-scoped and is designed to be idempotent. Do not create or edit runner control files manually. Use only the registered stop tools.
- If a stop request is pending or rejected, report that state explicitly. If the runner is no longer alive but a `computing_module` process remains, do not infer a successful stop; inspect the case and request infrastructure recovery rather than deleting runtime files manually.

## Trusted case reset and campaign growth

- Raw case data in `runs/` remain experimental records and must not be edited or deleted directly. The registered `nrg_campaign_reset_cases` tool is the sole exception for deliberate recalculation of exact logical cases. Use it only when the user explicitly approves resetting those cases after reviewing the reset plan. The tool may delete the selected current runtime products after recording
compact attempt-history metadata; it must not modify unselected cases or the generated logical case identity.
- Before a reset that changes numerical, output, or run-control settings, inspect the campaign identity policy. Fields that define the campaign's logical case identity cannot be changed by a recalculation attempt. Changing an identity field requires a new case.
- After reset, prepare the same campaign normally. Completed unselected cases remain authoritative and are skipped by trusted execution policy; reset cases are recreated and rerun under their reviewed attempt configuration. 
- Use `nrg_campaign_append_preview` / `nrg_campaign_append` to add genuinely new design points to an existing campaign. Existing case IDs must never be renumbered. Append only when the campaign identity schema is unchanged and protected campaign constants do not conflict.
- The older extension/composite workflow is reserved for genuinely independent, legacy, or externally produced datasets; do not use it merely because an existing logical case needs recalculation.

## Portable execution workflow

NRG campaign design/preparation and CFD execution may occur on different machines. Treat the agentic laboratory as the scientific control plane and a workstation/HPC environment as an execution plane.

- After a campaign is defined, generated, audited, and PREPARED, use `nrg_campaign_export_bundle` to create the execution artifact. Export does not launch CFD.
- The bundle freezes scientific identity and contains prepared case directories, portable relative case locators, a standalone standard-library `portable_executor.py`, checksums/provenance, and `execution.local.toml`.
- On the compute host, only `execution.local.toml` may be edited for machine linkage (computing_module path, thread count, local backend options). Do not edit case namelists, task setups, fingerprints, `cases.csv`, or the portable executor to adapt a machine.
- The compute host does not require Pi or package-interface executables. Run `validate`, then `run`/`resume`; use `pause --mode now` for a checkpointed interruption or `pause --mode after-current` to stop before the next case.
- Do not wait for remote pilot/full calculations in the agentic session. After export/launch instructions are complete, stop acting; inspect status only when the user explicitly provides/requests access to that execution environment.
- Return remote results with `portable_executor.py pack-results`. Back in the laboratory, use `nrg_campaign_import_results`; never copy returned files blindly into `runs/` when a verified import path is available.
- Campaign/result transfer is portable across environments. Restart checkpoints are raw NRG binary state and may be moved only between binary-compatible NRG runtimes (compatible source/restart schema, kinds, and endianness; preferably identical build/runtime provenance).
- Absolute paths recorded from the generation host are provenance only. Execution locators in portable manifests are relative to the bundle `runs/` root.

## Analysis workflow

1. If an existing study is referenced but its directory name is unknown, use `nrg_list_studies`; do not guess names.
2. Use `nrg_create_study` for a new analysis objective.
3. Read `STUDY_PROTOCOL.md`, the study manifest, and the relevant analysis skill before execution.
4. Modify only the study's `STUDY.md`, `analysis_config.toml`, `analyze.py`, and generated study outputs.
5. For newly written or substantially modified analysis on a large campaign, do **not** debug by repeatedly running the entire campaign. First call `nrg_study_pilot_plan`, then execute a bounded representative subset with `nrg_run_study_pilot`. Prefer deterministic identity-space coverage over the first N cases or an arbitrary/random subset.
6. Inspect the pilot's structured outputs and verify the requested per-case output contract before production execution. A successful pilot process alone is not sufficient scientific validation. Changes to `analyze.py`, `analysis_config.toml`, or `cases.csv` invalidate the trusted large-study pilot marker and require another pilot.
7. For large studies, separate per-case extraction from campaign aggregation and persist/reuse valid per-case structured results. A late per-case bug should not force re-reading every successful raw history. Individual analysis failures may be recorded and processing may continue, but global scientific conclusions must remain incomplete until required cases are repaired or explicitly justified exclusions are documented.
8. Use `nrg_run_study` for full production analysis only after the representative pilot is valid when required. The trusted wrapper blocks production execution of campaigns larger than the trusted large-study threshold (currently 50 cases) when current pilot validation is absent.
9. Use `nrg_read_study_summary` before drawing conclusions.
10. Do not present an exploratory diagnostic as an established physical criterion. Classify methods as primary, literature-supported, secondary, or diagnostic where appropriate.

## Logical cases, attempts, runner jobs, and trusted reset

Raw case data in `runs/` remain experimental records and must not be edited or deleted directly. The registered `nrg_campaign_reset_cases` tool is the sole trusted exception for deliberate recalculation of exact logical cases. Use it only after the user explicitly approves the reset plan. The tool may delete the selected current runtime products after recording compact attempt-history metadata; it must not modify unselected cases or the generated logical identity.
A campaign-specific identity policy determines which parameters define a logical case. Changing an identity field creates a new case. Reviewed attempt-tunable fields may be changed for recalculation of the same logical case. Use `nrg_campaign_append_preview` / `nrg_campaign_append` for genuinely new design points; existing case IDs must never be renumbered.

Keep these provenance concepts separate:
- `attempt_id` identifies recalculation/configuration lineage created by reset or
  attempt preparation. One attempt may legitimately be executed by more than one
  runner job.
- `runner_job_id` identifies the background runner invocation that produced the
  current `run_status.json`. Older v0.5 selective reruns may record the same
  concept as `selective_rerun_job_id`.
- quasistationarity audit status is a physical classification derived from
  `reactor_history.dat`; it does not state why the executable terminated.
- A successful `nrg_campaign_quasistationary_audit` response must contain its structured audit contract, including `case_count`, `counts`, `quasistationary_count`, and `needs_recalculation_count`. An empty object (`{}`), missing required audit fields, timeout, or bridge error is an invalid/incomplete audit result and must never be interpreted as zero anomalies or as proof that all cases are quasistationary.
- `nrg_campaign_execution_summary` is the preferred fast campaign-wide check for current execution provenance and online physical-condition metadata. It reads `run_status.json` only and does **not** replace an independent `reactor_history.dat` quasistationarity audit.
- execution status and `nrg_termination_reason` come from the current
  `run_status.json` and must be reported separately from audit classification.

Do not infer that `wall_time` run-control mode means the wall-time limit was reached; report the actual `nrg_termination_reason`. For trusted physical termination, the external laboratory controller detects the condition and writes `run_control.stop`; NRG detects that request and terminates with `external_stop_request`. Do not describe NRG as originating the stop request.
The extension/composite workflow is reserved for genuinely independent, legacy, or externally produced datasets; do not use it merely because an existing logical case needs recalculation.

## Scientific behavior

- Do not infer missing numerical results. Distinguish numerical convergence, physical-model differences, chemistry-model differences, and analysis-definition differences. If current data cannot resolve a hypothesis, propose the smallest targeted follow-up campaign that can.
- Report campaign parameters from structured campaign definitions or laboratory-tool output whenever available. Do not infer exact sweep values from filenames, labels, or a limited preview sample. If an exact parameter value is unavailable, state that it is unknown rather than presenting a guess as part of the campaign definition.

## Bridge output policy

- Trusted `lab_bridge.py` operational commands return compact JSON by default.
- Rich job, runner-lock, case-status, hash, and provenance records remain persisted on disk; compact output is a projection, not a loss of provenance.
- Use the CLI `--full` option only for diagnostics, compatibility investigation, or forensic provenance inspection. Do not request full payloads routinely in agent workflows.
- For pause plans, the compact contract is `can_pause`; terminal stop plans use `can_stop`.
- Prefer decisive operational fields (`job_id`, state, active case, counts, blockers, NRG termination reason/restart flag) over embedding complete historical records in tool responses.

