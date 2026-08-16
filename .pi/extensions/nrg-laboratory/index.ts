import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import { existsSync, readFileSync } from "node:fs";
import { dirname, isAbsolute, join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { evaluateReadOnlyShell, type ReadOnlyShellPolicy } from "./read_only_shell.ts";

// Resolve project-internal resources from this extension's installed location,
// not from the shell working directory.  This keeps a cloned repository
// relocatable while preserving environment-variable overrides.
const EXTENSION_DIR = dirname(fileURLToPath(import.meta.url));
const PROJECT_ROOT = resolve(EXTENSION_DIR, "../../..");
const PROJECT_VENV_PYTHON = process.platform === "win32"
  ? join(PROJECT_ROOT, ".venv", "Scripts", "python.exe")
  : join(PROJECT_ROOT, ".venv", "bin", "python");
const PYTHON = process.env.NRG_PYTHON
  || (existsSync(PROJECT_VENV_PYTHON) ? PROJECT_VENV_PYTHON : "python");
const LAB_CONFIG = process.env.NRG_LABORATORY_CONFIG || join(PROJECT_ROOT, "config", "laboratory.toml");
const READ_ONLY_SHELL_POLICY = process.env.NRG_READ_ONLY_SHELL_POLICY || join(PROJECT_ROOT, "config", "read_only_shell_policy.json");

const LONG_READ_ONLY_AUDIT_TIMEOUT_MS = 30 * 60_000;

type JsonObject = Record<string, any>;

type LabState = {
  researchRoot: string;
  campaignRoot: string;
  studiesRoot: string;
} | null;

function parseJson(text: string): JsonObject {
  try {
    return JSON.parse(text);
  } catch {
    return { error: "bridge returned non-JSON output", raw: text };
  }
}

function inside(root: string, target: string): boolean {
  const rel = relative(resolve(root), resolve(target));
  return rel === "" || (!rel.startsWith("..") && !isAbsolute(rel));
}

export default function (pi: ExtensionAPI) {
  let labState: LabState = null;
  let shellPolicy: ReadOnlyShellPolicy | null = null;

  function loadShellPolicy(): ReadOnlyShellPolicy | null {
    if (shellPolicy) return shellPolicy;
    try {
      const parsed = JSON.parse(readFileSync(READ_ONLY_SHELL_POLICY, "utf8")) as ReadOnlyShellPolicy;
      if (parsed.schema_version !== 1 || !Array.isArray(parsed.allowed_commands) || !Array.isArray(parsed.git_subcommands)) {
        return null;
      }
      shellPolicy = parsed;
      return shellPolicy;
    } catch {
      return null;
    }
  }

  async function bridge(args: string[], signal?: AbortSignal, timeout = 120_000): Promise<JsonObject> {
    const command = ["-m", "agent_workspace.lab_bridge", "--laboratory", LAB_CONFIG, ...args];
    try {
      const result = await pi.exec(PYTHON, command, { signal, timeout });
      const stdout = (result.stdout ?? "").trim();
      const stderr = (result.stderr ?? "").trim();

      if (!stdout) {
        return {
          error: "laboratory bridge returned empty stdout",
          exit_code: result.code ?? null,
          stderr: stderr || null,
          command: [PYTHON, ...command],
          timeout_ms: timeout,
        };
      }

      const payload = parseJson(stdout);
      if (result.code !== 0 && !payload.error) {
        payload.error = `bridge exited with code ${result.code}`;
      }
      if (result.code !== 0 && stderr) {
        payload.stderr = stderr;
      }
      return payload;
    } catch (error) {
      return {
        error: "laboratory bridge execution failed",
        detail: error instanceof Error ? error.message : String(error),
        command: [PYTHON, ...command],
        timeout_ms: timeout,
      };
    }
  }

  async function ensureLab(signal?: AbortSignal): Promise<LabState> {
    if (labState) return labState;
    const info = await bridge(["lab-info"], signal);
    if (info.error || !info.laboratory) return null;
    labState = {
      researchRoot: info.laboratory.research_root,
      campaignRoot: info.laboratory.campaign_root,
      studiesRoot: info.laboratory.studies_root,
    };
    return labState;
  }

  function toolResult(payload: JsonObject) {
    return {
      content: [{ type: "text" as const, text: JSON.stringify(payload, null, 2) }],
      details: payload,
    };
  }

  pi.registerTool({
    name: "nrg_lab_info",
    label: "NRG Lab Info",
    description: "Inspect the configured NRG laboratory roots and trusted runtime hashes.",
    promptSnippet: "Inspect NRG laboratory configuration and runtime provenance",
    promptGuidelines: ["Use nrg_lab_info before making assumptions about NRG laboratory paths or trusted executables."],
    parameters: Type.Object({}),
    async execute(_id, _params, signal) {
      return toolResult(await bridge(["lab-info"], signal));
    },
  });

  pi.registerTool({
    name: "nrg_list_campaigns",
    label: "NRG List Campaigns",
    description: "List declarative campaign definitions available in the configured campaigns/definitions workspace.",
    promptSnippet: "Discover available NRG campaign definitions without guessing filenames",
    promptGuidelines: ["Use nrg_list_campaigns when the user refers to an existing campaign but its definition filename is not already known."],
    parameters: Type.Object({}),
    async execute(_id, _params, signal) {
      return toolResult(await bridge(["campaign-list"], signal));
    },
  });

  pi.registerTool({
    name: "nrg_list_studies",
    label: "NRG List Studies",
    description: "List scientific analysis studies available under the configured studies_root.",
    promptSnippet: "Discover existing NRG analysis studies",
    promptGuidelines: ["Use nrg_list_studies when an existing study is referenced but its directory name is not already known."],
    parameters: Type.Object({}),
    async execute(_id, _params, signal) {
      return toolResult(await bridge(["study-list"], signal));
    },
  });

  pi.registerTool({
    name: "nrg_campaign_identity_inspect",
    label: "NRG Campaign Identity",
    description: "Inspect the campaign-specific logical case identity axes and reviewed attempt-tunable fields. For legacy campaigns, identity is inferred from sweep/numerical-variant design without changing existing case IDs or runtime fingerprints.",
    promptSnippet: "Inspect what defines a logical case versus a recalculation attempt",
    promptGuidelines: [
      "Use this tool before changing numerical/output/run-control settings for an existing logical case.",
      "A field in identity_fields cannot be changed by reset/recalculation; changing it creates a new case.",
      "For legacy campaigns, logical_identity_fingerprint is authoritative for design identity while existing case_fingerprint values are retained for runtime compatibility.",
    ],
    parameters: Type.Object({
      cases: Type.String({ description: "Path to generated campaign cases.csv" }),
    }),
    async execute(_id, params, signal) {
      return toolResult(await bridge(["campaign-identity-inspect", "--cases", params.cases], signal));
    },
  });

  pi.registerTool({
    name: "nrg_campaign_reset_cases",
    label: "NRG Reset Cases",
    description: "Reset exact logical cases to their post-generation state inside the same campaign, optionally applying reviewed attempt-only overrides such as initial time step, CFL settings, output frequency, or run-control policy. Previous raw execution products are deleted after compact metadata-only provenance is recorded. Requires confirmation.",
    promptSnippet: "Clean selected unresolved NRG cases and recalculate them in the same campaign",
    promptGuidelines: [
      "Use exact case IDs grounded in campaign status/audit output; no wildcards or ranges.",
      "The logical case fingerprint/identity is never changed by this operation.",
      "Attempt overrides are allowed only for the campaign's reviewed attempt-tunable fields and are blocked if the field is a campaign identity axis.",
      "The reset deletes current execution products for selected cases after writing compact metadata/inventory under _attempt_history; generated _setups and all unselected cases are untouched.",
      "After reset, prepare the same campaign normally. Existing completed cases will be skipped; reset cases will be recreated from effective attempt setups.",
      "Always require explicit user confirmation because this operation deletes selected case runtime data.",
    ],
    parameters: Type.Object({
      cases: Type.String({ description: "Path to generated campaign cases.csv" }),
      case_ids: Type.Array(Type.String({ description: "Exact logical case_id" }), { minItems: 1, maxItems: 50 }),
      overrides: Type.Optional(Type.Array(Type.Object({
        field: Type.String({ description: "Exact group.key attempt-tunable field path" }),
        value: Type.Union([Type.String(), Type.Number(), Type.Boolean()]),
      }), { maxItems: 20 })),
      reason: Type.Optional(Type.String({ description: "Short provenance reason for recalculation" })),
    }),
    async execute(_id, params, signal, _onUpdate, ctx) {
      const args = ["campaign-reset-plan", "--cases", params.cases];
      for (const caseId of params.case_ids) args.push("--case-id", caseId);
      for (const override of params.overrides ?? []) {
        args.push("--override", JSON.stringify(override));
      }
      if (params.reason) args.push("--reason", params.reason);
      const plan = await bridge(args, signal);
      if (plan.error || !plan.can_reset) return toolResult(plan);
      if (!ctx.hasUI) return toolResult({ error: "case reset requires interactive confirmation", plan });

      const selectedSummary = plan.selected_cases
        .map((x: any) => `${x.case_id}:${x.current_status}`)
        .join(", ");
      const overrideSummary = Object.entries(plan.requested_attempt_overrides ?? {})
        .map(([k, v]) => `${k}=${String(v)}`)
        .join(", ") || "none";
      const ok = await ctx.ui.confirm(
        "Reset selected NRG cases?",
        `Reset ${plan.selected_count} logical case(s): ${selectedSummary}. ` +
        `${plan.other_cases_untouched} other case(s) remain untouched. ` +
        `Delete ${plan.cleanup.files_to_remove} runtime file(s), ${plan.cleanup.bytes_to_remove} byte(s), ` +
        `keeping metadata-only provenance. Attempt overrides: ${overrideSummary}.`,
      );
      if (!ok) return toolResult({ cancelled: true, plan });

      const execArgs = ["campaign-reset-execute", "--cases", params.cases];
      for (const caseId of params.case_ids) execArgs.push("--case-id", caseId);
      for (const override of params.overrides ?? []) {
        execArgs.push("--override", JSON.stringify(override));
      }
      if (params.reason) execArgs.push("--reason", params.reason);
      return toolResult(await bridge(execArgs, signal));
    },
  });

  pi.registerTool({
    name: "nrg_campaign_append_preview",
    label: "NRG Campaign Append Preview",
    description: "Compare a revised campaign definition with an existing generated campaign using logical identity axes. Reports already-present, genuinely new, and conflicting design points without modifying the campaign.",
    promptSnippet: "Preview adding mechanisms or thermodynamic states to an existing NRG campaign",
    promptGuidelines: [
      "Use this when expanding the design space of an existing campaign.",
      "Existing logical case IDs are never renumbered.",
      "The identity schema must match the existing campaign; protected campaign-constant changes are reported as conflicts rather than silently appended.",
    ],
    parameters: Type.Object({
      cases: Type.String({ description: "Existing generated campaign cases.csv" }),
      campaign: Type.String({ description: "Revised campaign TOML path or definition filename" }),
    }),
    async execute(_id, params, signal) {
      return toolResult(await bridge([
        "campaign-append-preview",
        "--cases", params.cases,
        "--campaign", params.campaign,
      ], signal));
    },
  });

  pi.registerTool({
    name: "nrg_campaign_append",
    label: "NRG Campaign Append",
    description: "Append only genuinely new logical design points from a revised definition to an existing generated campaign, preserving all existing case IDs and raw results. Requires confirmation.",
    promptSnippet: "Append new mechanisms or thermodynamic states to an existing NRG campaign",
    promptGuidelines: [
      "Always preview first and report the number/identity of new and conflicting cases.",
      "Append only when conflict_count is zero.",
      "Existing case directories, run statuses, and case IDs are untouched; only new generated cases and a revision record are added.",
      "After append, prepare/start normally; trusted policy skips already completed existing cases.",
    ],
    parameters: Type.Object({
      cases: Type.String({ description: "Existing generated campaign cases.csv" }),
      campaign: Type.String({ description: "Revised campaign TOML path or definition filename" }),
    }),
    async execute(_id, params, signal, _onUpdate, ctx) {
      const preview = await bridge([
        "campaign-append-preview",
        "--cases", params.cases,
        "--campaign", params.campaign,
      ], signal);
      if (preview.error || preview.exit_code !== 0) return toolResult(preview);
      const plan = preview.plan ?? {};
      if ((plan.conflict_count ?? 0) > 0) return toolResult(preview);
      if ((plan.new_case_count ?? 0) === 0) return toolResult(preview);
      if (!ctx.hasUI) return toolResult({ error: "campaign append requires interactive confirmation", preview });
      const ok = await ctx.ui.confirm(
        "Append new NRG campaign cases?",
        `Append ${plan.new_case_count} new logical case(s) to ${plan.existing_case_count} existing case(s). ` +
        `${plan.already_present_count} requested design point(s) already exist. Existing IDs/results are unchanged.`,
      );
      if (!ok) return toolResult({ cancelled: true, preview });
      return toolResult(await bridge([
        "campaign-append-execute",
        "--cases", params.cases,
        "--campaign", params.campaign,
      ], signal));
    },
  });

  pi.registerTool({
    name: "nrg_extension_preview",
    label: "NRG Extension Preview",
    description: "Preview a separate provenance-linked extension campaign. This is an exceptional interoperability/legacy path; for recalculating the same logical case in the same campaign, prefer nrg_campaign_reset_cases.",
    promptSnippet: "Preview a small NRG extension campaign",
    promptGuidelines: [
      "Use extensions when only a small subset of an expensive base campaign requires recomputation.",
      "Use exact parent case IDs. Never silently broaden the extension set.",
      "The base campaign remains immutable.",
    ],
    parameters: Type.Object({
      extension: Type.String({ description: "Extension TOML path or filename under campaigns/definitions" }),
    }),
    async execute(_id, params, signal) {
      return toolResult(await bridge(["extension-preview", "--extension", params.extension], signal));
    },
  });

  pi.registerTool({
    name: "nrg_extension_generate",
    label: "NRG Extension Generate",
    description: "Generate a provenance-linked extension campaign without preparing or running CFD.",
    promptSnippet: "Generate an approved NRG extension campaign",
    promptGuidelines: [
      "Preview first. Extension generation never modifies the base campaign.",
      "Overwriting existing extension generator output requires confirmation.",
    ],
    parameters: Type.Object({
      extension: Type.String({ description: "Extension TOML path or filename under campaigns/definitions" }),
      overwrite: Type.Optional(Type.Boolean()),
    }),
    async execute(_id, params, signal, _onUpdate, ctx) {
      const preview = await bridge(["extension-preview", "--extension", params.extension], signal);
      if (preview.error || preview.exit_code !== 0) return toolResult(preview);
      const overwrite = params.overwrite ?? false;
      if (overwrite) {
        if (!ctx.hasUI) return toolResult({ error: "overwrite requires interactive confirmation", preview });
        const ok = await ctx.ui.confirm(
          "Overwrite generated extension?",
          "Existing extension generator output will be replaced; the base campaign is not modified.",
        );
        if (!ok) return toolResult({ cancelled: true, preview });
      }
      const args = ["extension-generate", "--extension", params.extension];
      if (overwrite) args.push("--overwrite");
      return toolResult(await bridge(args, signal));
    },
  });

  pi.registerTool({
    name: "nrg_campaign_compose_extension",
    label: "NRG Composite Dataset",
    description: "Create a single analysis-only logical campaign by replacing explicitly linked base cases with successfully physically terminated extension cases. Raw data are not copied.",
    promptSnippet: "Compose a base campaign and completed extension into one analysis dataset",
    promptGuidelines: [
      "Use only after every extension case has condition_met and physical_condition_met=true.",
      "Parent fingerprints and reactor/mixture/physics/output identity are verified deterministically.",
      "The composite is analysis-only and must never be prepared or executed.",
      "Downstream studies may read the composite cases.csv as one logical campaign while source provenance remains explicit.",
    ],
    parameters: Type.Object({
      base_cases: Type.String({ description: "Base campaign cases.csv" }),
      extension_cases: Type.String({ description: "Completed extension cases.csv" }),
      output_name: Type.String({ description: "Composite name under campaigns/_composites/" }),
      overwrite: Type.Optional(Type.Boolean()),
    }),
    async execute(_id, params, signal, _onUpdate, ctx) {
      const overwrite = params.overwrite ?? false;
      if (overwrite) {
        if (!ctx.hasUI) return toolResult({ error: "overwrite requires interactive confirmation" });
        const ok = await ctx.ui.confirm(
          "Overwrite composite dataset?",
          "Only the composite manifest will be regenerated; raw base and extension results remain untouched.",
        );
        if (!ok) return toolResult({ cancelled: true });
      }
      const args = [
        "composite-create",
        "--base-cases", params.base_cases,
        "--extension-cases", params.extension_cases,
        "--output-name", params.output_name,
      ];
      if (overwrite) args.push("--overwrite");
      return toolResult(await bridge(args, signal));
    },
  });

  pi.registerTool({
    name: "nrg_campaign_preview",
    label: "NRG Campaign Preview",
    description: "Validate and preview a declarative NRG campaign without creating cases or running CFD.",
    promptSnippet: "Preview a declarative NRG campaign safely",
    promptGuidelines: ["Use nrg_campaign_preview before generating or running a new NRG campaign."],
    parameters: Type.Object({
      campaign: Type.String({ description: "Campaign TOML path, or filename/relative path under campaigns/definitions" }),
      limit: Type.Optional(Type.Number({ minimum: 1, maximum: 50 })),
    }),
    async execute(_id, params, signal) {
      const args = ["campaign-preview", "--campaign", params.campaign, "--limit", String(params.limit ?? 12)];
      return toolResult(await bridge(args, signal));
    },
  });

  pi.registerTool({
    name: "nrg_campaign_generate",
    label: "NRG Campaign Generate",
    description: "Generate deterministic campaign manifests and setup-input files without launching package interfaces or CFD.",
    promptSnippet: "Generate deterministic NRG campaign manifest and setup-input files",
    promptGuidelines: ["Use nrg_campaign_generate only after nrg_campaign_preview; overwriting existing generated campaign files requires user confirmation."],
    parameters: Type.Object({
      campaign: Type.String({ description: "Campaign TOML path, or filename/relative path under campaigns/definitions" }),
      overwrite: Type.Optional(Type.Boolean({ description: "Replace existing generator output" })),
    }),
    async execute(_id, params, signal, _onUpdate, ctx) {
      const preview = await bridge(["campaign-preview", "--campaign", params.campaign, "--limit", "5"], signal);
      if (preview.error) return toolResult(preview);
      const overwrite = params.overwrite ?? false;
      if (overwrite) {
        if (!ctx.hasUI) return toolResult({ error: "overwrite requires interactive confirmation" });
        const ok = await ctx.ui.confirm(
          "Overwrite generated campaign files?",
          `Campaign contains ${preview.final_cases ?? "?"} cases. Existing generator output may be replaced. Continue?`,
        );
        if (!ok) return toolResult({ cancelled: true, reason: "user declined campaign overwrite" });
      }
      const args = ["campaign-generate", "--campaign", params.campaign];
      if (overwrite) args.push("--overwrite");
      return toolResult(await bridge(args, signal, 3_600_000));
    },
  });

  pi.registerTool({
    name: "nrg_campaign_prepare_cases",
    label: "NRG Prepare Cases",
    description: "Invoke the trusted package interface only for missing case directories, using any reviewed attempt overrides created by nrg_campaign_reset_cases while preserving logical case fingerprints.",
    promptSnippet: "Prepare missing NRG case directories from generated setup-input files",
    promptGuidelines: ["Use nrg_campaign_prepare_cases after generation and status inspection; it skips existing cases only when their fingerprints match and always asks the user before preparing cases."],
    parameters: Type.Object({
      cases: Type.String({ description: "Path to generated cases.csv" }),
    }),
    async execute(_id, params, signal, _onUpdate, ctx) {
      const status = await bridge(["campaign-status", "--cases", params.cases], signal);
      if (status.error) return toolResult(status);
      if ((status.missing_cases ?? 0) === 0) return toolResult({ message: "all case directories already exist", status });
      if (!ctx.hasUI) return toolResult({ error: "case preparation requires interactive confirmation", status });
      const ok = await ctx.ui.confirm(
        "Prepare NRG cases?",
        `Invoke the trusted package interface for ${status.missing_cases} missing case(s) out of ${status.total_cases}? Existing matching cases will be skipped.`,
      );
      if (!ok) return toolResult({ cancelled: true, reason: "user declined case preparation", status });
      return toolResult(await bridge(["campaign-prepare", "--cases", params.cases], signal, 3_600_000));
    },
  });

  pi.registerTool({
    name: "nrg_campaign_status",
    label: "NRG Campaign Status",
    description: "Read structured execution status for all cases in a cases.csv manifest.",
    promptSnippet: "Inspect NRG campaign completion, failure, restart, and missing-case counts",
    promptGuidelines: ["Use nrg_campaign_status to inspect a campaign instead of inferring progress from logs."],
    parameters: Type.Object({
      cases: Type.String({ description: "Path to cases.csv" }),
    }),
    async execute(_id, params, signal) {
      return toolResult(await bridge(["campaign-status", "--cases", params.cases], signal));
    },
  });

  pi.registerTool({
    name: "nrg_case_inspect",
    label: "NRG Case Inspect",
    description: "Inspect one NRG case deterministically: current run_status.json, logical-attempt metadata, runner-job metadata, process state, marker files, and laboratory runner lock.",
    promptSnippet: "Inspect one NRG case without guessing from empty reads or searching for lock files",
    promptGuidelines: [
      "Use nrg_case_inspect when diagnosing an individual case status, especially invalid_status, interrupted, or stale running cases.",
      "Do not infer whether run_status.json is empty from read output and do not search for laboratory lock files manually when nrg_case_inspect can report both directly.",
      "Keep attempt_id and runner_job_id distinct: attempt_id identifies recalculation/configuration lineage and may span multiple runner jobs; runner_job_id identifies the runner invocation that produced the current run_status. Legacy v0.5 selective reruns may expose the latter as selective_rerun_job_id.",
      "When describing an external physical stop, say that the laboratory controller wrote run_control.stop and NRG detected it and terminated with external_stop_request; do not say NRG originated the stop signal.",
    ],
    parameters: Type.Object({
      cases: Type.String({ description: "Path to the generated campaign cases.csv" }),
      case_id: Type.String({ description: "Exact case id, for example R000012" }),
    }),
    async execute(_id, params, signal) {
      return toolResult(await bridge(["case-inspect", "--cases", params.cases, "--case-id", params.case_id], signal));
    },
  });

  pi.registerTool({
    name: "nrg_campaign_start",
    label: "NRG Campaign Start",
    description: "Start or resume an ordinary NRG campaign using the trusted laboratory execution policy. This generic start path does not activate a physical termination profile. Completed statuses configured in the policy are skipped automatically; no run_config argument is needed or permitted. Launch occurs as a detached background job after explicit user confirmation.",
    promptSnippet: "Start or resume an ordinary NRG CFD campaign using the trusted skip/recovery/concurrency policy",
    promptGuidelines: [
      "Use nrg_campaign_start only for ordinary execution after checking nrg_campaign_status; this tool always asks the user before launching CFD.",
      "Never use nrg_campaign_start as a fallback when the requested execution requires a trusted physical termination profile. Use nrg_campaign_start_to_quasistationary or nrg_campaign_run_cases_to_quasistationary instead.",
      "If the required physical-start tool is unavailable, report the missing capability and do not launch CFD through the generic start path.",
      "The campaign runner configuration is trusted laboratory policy. Never create, edit, search for, or invent a run_config.json to start or resume a campaign.",
      "When the user asks to preserve or skip finished cases, inspect nrg_campaign_status.execution_policy and skipped_by_policy, then call nrg_campaign_start with only cases.csv. Do not read campaign_runner.py to rediscover this behavior.",
      "nrg_campaign_start automatically applies the configured skip statuses, rerun-failed policy, interrupted-run recovery, thread limit, and concurrency limit.",
    ],
    parameters: Type.Object({
      cases: Type.String({ description: "Path to cases.csv" }),
    }),
    async execute(_id, params, signal, _onUpdate, ctx) {
      const status = await bridge(["campaign-status", "--cases", params.cases], signal);
      if (status.error) return toolResult(status);
      if ((status.missing_cases ?? 0) > 0) {
        return toolResult({ error: "case directories are missing; prepare the campaign before running", status });
      }
      if (status.laboratory_runner?.active || (status.running_cases ?? 0) > 0) {
        return toolResult({ error: "another campaign runner is already active", status });
      }
      if ((status.runnable_cases ?? 0) === 0) {
        return toolResult({ message: "no runnable cases remain", status });
      }
      if (!ctx.hasUI) return toolResult({ error: "campaign execution requires interactive confirmation", status });
      const skipped = Object.entries(status.skipped_by_policy ?? {})
        .map(([name, count]) => `${count} ${name}`)
        .join(", ") || "none";
      const runnable = Object.entries(status.runnable_by_status ?? {})
        .map(([name, count]) => `${count} ${name}`)
        .join(", ") || "none";
      const policy = status.execution_policy ?? {};
      const ok = await ctx.ui.confirm(
        "Start NRG campaign?",
        `Trusted policy will skip: ${skipped}. ` +
          `Runnable now: ${status.runnable_cases}/${status.total_cases} (${runnable}). ` +
          `${status.stale_running_cases ?? 0} stale running case(s) will be recovered as interrupted. ` +
          `Concurrency=${policy.max_concurrent_cases ?? 1}, threads/case=${policy.threads ?? 1}. ` +
          `The runner will execute in the background.`,
      );
      if (!ok) return toolResult({ cancelled: true, reason: "user declined campaign execution", status });
      return toolResult(await bridge(["campaign-start", "--cases", params.cases], signal));
    },
  });

  pi.registerTool({
    name: "nrg_campaign_execution_summary",
    label: "NRG Campaign Execution Summary",
    description: "Fast read-only campaign summary from current per-case run_status.json files. Reports execution status, NRG termination reasons, online physical-condition metadata, termination profiles, and runner provenance without reading reactor histories.",
    promptSnippet: "Summarize how an NRG campaign executed without performing an expensive offline history audit",
    promptGuidelines: [
      "Use this tool for rapid campaign-wide execution/provenance checks, especially on large campaigns.",
      "This tool reads current run_status.json only. It does not independently prove that stored reactor histories satisfy a quasistationarity profile.",
      "Keep online physical-condition metadata separate from the offline history classification returned by nrg_campaign_quasistationary_audit.",
      "Use nrg_campaign_quasistationary_audit when an independent reactor_history.dat audit is scientifically required.",
    ],
    parameters: Type.Object({
      cases: Type.String({ description: "Path to the generated campaign cases.csv" }),
    }),
    async execute(_id, params, signal) {
      return toolResult(await bridge([
        "campaign-execution-summary",
        "--cases", params.cases,
      ], signal));
    },
  });

  pi.registerTool({
    name: "nrg_campaign_quasistationary_audit",
    label: "NRG Quasistationary Audit",
    description: "Read-only audit of reactor histories against a trusted named post-ignition quasistationarity profile. History classification and current execution provenance are reported as separate dimensions.",
    promptSnippet: "Audit an NRG reactor campaign for post-ignition quasistationary product states",
    promptGuidelines: [
      "Use this tool before deciding which completed ignition cases require longer product-state calculations.",
      "Do not infer quasistationarity from ignition delay alone; use the deterministic audit result.",
      "The audit is read-only and does not rerun or modify cases.",
      "Quasistationarity classification is based on reactor_history.dat only. Never infer termination reason, run-control mode outcome, attempt identity, or runner job identity from quasistationarity status.",
      "If reporting how cases terminated, use execution_provenance.status_counts and execution_provenance.nrg_termination_reason_counts from current run_status.json. Keep these separate from history-based audit counts.",
      "Do not say that wall_time mode means the wall-time limit was reached. Report the actual nrg_termination_reason.",
      "Keep attempt_id and runner_job_id distinct. One recalculation attempt may be executed across multiple runner jobs.",
      "A successful audit response must contain case_count, counts, quasistationary_count, and needs_recalculation_count. An empty object {} is an invalid tool response and must never be interpreted as zero anomalies or universal quasistationarity.",
      "Large full-history audits can take several minutes. If the tool reports an execution/timeout/empty-output error, do not immediately repeat the same expensive audit; report the tool failure and use nrg_campaign_execution_summary only for the separate execution/provenance question.",
    ],
    parameters: Type.Object({
      cases: Type.String({ description: "Path to the generated campaign cases.csv" }),
      profile: Type.Optional(Type.String({
        description: "Exact trusted profile name",
        default: "0d_cv_post_ignition_quasistationary_v1",
      })),
    }),
    async execute(_id, params, signal) {
      return toolResult(await bridge([
        "campaign-physical-audit",
        "--cases", params.cases,
        "--profile", params.profile ?? "0d_cv_post_ignition_quasistationary_v1",
      ], signal, LONG_READ_ONLY_AUDIT_TIMEOUT_MS));
    },
  });

  pi.registerTool({
    name: "nrg_campaign_start_to_quasistationary",
    label: "NRG Physical Campaign Start/Resume",
    description: "Start or resume all currently runnable cases in a prepared campaign under a trusted physical termination profile, regardless of campaign size. Already protected finished/condition_met cases are skipped by trusted execution policy. Requires confirmation.",
    promptSnippet: "Start or resume every runnable case in an NRG campaign until a trusted physical product-state condition is met",
    promptGuidelines: [
      "Use only with campaigns generated for a compatible run-control mode. The v1 0D quasistationary profile requires wall_time mode so a finite simulation-time ceiling cannot preempt the physical stop.",
      "Do not modify the trusted profile thresholds or runner configuration. If preflight reports an incompatible run-control mode, create/generate a compatible campaign definition instead.",
      "Successful cases terminate as condition_met and include an averaged product_state in run_status.json.",
      "This is the campaign-wide start/resume tool: it automatically selects every currently runnable case and skips statuses protected by trusted execution policy, regardless of campaign size. Do not partition a full campaign into selected-case batches.",
      "The laboratory physical-condition controller creates run_control.stop after detecting the condition; NRG then reports external_stop_request. Do not reverse this direction in reports.",
      "Always ask for explicit user confirmation before launch.",
    ],
    parameters: Type.Object({
      cases: Type.String({ description: "Path to the prepared campaign cases.csv" }),
      profile: Type.Optional(Type.String({
        description: "Exact trusted profile name",
        default: "0d_cv_post_ignition_quasistationary_v1",
      })),
    }),
    async execute(_id, params, signal, _onUpdate, ctx) {
      const profile = params.profile ?? "0d_cv_post_ignition_quasistationary_v1";
      const plan = await bridge([
        "campaign-physical-plan", "--cases", params.cases, "--profile", profile,
      ], signal);
      if (plan.error || !plan.can_start) return toolResult(plan);
      if (!ctx.hasUI) return toolResult({ error: "physical campaign launch requires interactive confirmation", plan });
      const ok = await ctx.ui.confirm(
        "Run NRG campaign to quasistationary products?",
        `Run ${plan.selected_count} runnable case(s) with trusted profile ${profile}. ` +
        `Success requires the physical condition, not merely process exit. ` +
        `Concurrency=${plan.execution_policy?.max_concurrent_cases ?? 1}, ` +
        `threads/case=${plan.execution_policy?.threads ?? 1}.`,
      );
      if (!ok) return toolResult({ cancelled: true, plan });
      return toolResult(await bridge([
        "campaign-physical-start", "--cases", params.cases, "--profile", profile,
      ], signal));
    },
  });

  pi.registerTool({
    name: "nrg_campaign_run_cases_to_quasistationary",
    label: "NRG Selected Physical-Termination Cases",
    description: "Run only explicitly named cases under a trusted post-ignition quasistationarity profile. Named finished cases may be rerun with prior compact artifacts archived; all other cases are outside the execution subset. Requires confirmation.",
    promptSnippet: "Run exact NRG cases until the trusted quasistationary product-state condition is met",
    promptGuidelines: [
      "Ground exact case IDs in a quasistationary audit or case inspection; no wildcards or ranges.",
      "The v1 profile requires wall_time run-control mode. Do not patch prepared case inputs or weaken fingerprints to bypass this requirement.",
      "The trusted global runner policy and physical profile are not edited.",
      "The laboratory physical-condition controller creates run_control.stop after detecting the condition; NRG then reports external_stop_request. Do not describe NRG as issuing the stop request.",
      "Keep any attempt_id in run_status separate from runner_job_id/selective_rerun_job_id when reporting provenance.",
      "This tool is limited to at most 50 exact case IDs and is intended only for deliberately selected subsets. Never use repeated calls to partition a full campaign; use nrg_campaign_start_to_quasistationary to start or resume all runnable cases.",
      "Always ask for explicit user confirmation.",
    ],
    parameters: Type.Object({
      cases: Type.String({ description: "Path to the prepared campaign cases.csv" }),
      case_ids: Type.Array(Type.String({ description: "Exact case_id" }), { minItems: 1, maxItems: 50 }),
      profile: Type.Optional(Type.String({
        description: "Exact trusted profile name",
        default: "0d_cv_post_ignition_quasistationary_v1",
      })),
    }),
    async execute(_id, params, signal, _onUpdate, ctx) {
      const profile = params.profile ?? "0d_cv_post_ignition_quasistationary_v1";
      const args = [
        "campaign-physical-cases-plan", "--cases", params.cases, "--profile", profile,
      ];
      for (const caseId of params.case_ids) args.push("--case-id", caseId);
      const plan = await bridge(args, signal);
      if (plan.error || !plan.can_start) return toolResult(plan);
      if (!ctx.hasUI) return toolResult({ error: "physical selected-case launch requires interactive confirmation", plan });
      const ok = await ctx.ui.confirm(
        "Run selected NRG cases to quasistationarity?",
        `Run exactly ${plan.selected_count} case(s): ${plan.selected_case_ids.join(", ")}. ` +
        `${plan.other_cases_not_scheduled} other case(s) are outside this job. ` +
        `Profile=${profile}; concurrency=${plan.execution_policy?.max_concurrent_cases ?? 1}; ` +
        `threads/case=${plan.execution_policy?.threads ?? 1}.`,
      );
      if (!ok) return toolResult({ cancelled: true, plan });
      const startArgs = [
        "campaign-physical-cases-start", "--cases", params.cases, "--profile", profile,
      ];
      for (const caseId of params.case_ids) startArgs.push("--case-id", caseId);
      return toolResult(await bridge(startArgs, signal));
    },
  });

  pi.registerTool({
    name: "nrg_campaign_rerun_cases",
    label: "NRG Selective Rerun",
    description: "Explicitly rerun only named NRG cases, even when their current terminal status would normally be skipped. The trusted global runner policy is not changed. Previous compact run artifacts are archived per selected case before rerun. Requires explicit user confirmation.",
    promptSnippet: "Selectively rerun explicitly identified damaged or scientifically invalid NRG cases",
    promptGuidelines: [
      "Use nrg_campaign_rerun_cases only for a small, explicitly identified set of exact case IDs that require recomputation; use nrg_campaign_start for ordinary campaign resume.",
      "Never modify skip_statuses or create a temporary run_config to force selected cases.",
      "Ground the exact case IDs in laboratory status, case inspection, or analysis output before calling this tool; do not invent IDs, wildcards, or ranges.",
      "The tool archives prior compact run metadata/history files, bypasses ordinary skip status only for the named cases, preserves all other cases, and retains the laboratory-wide concurrency/thread limits.",
      "Selective rerun always asks for explicit user confirmation.",
    ],
    parameters: Type.Object({
      cases: Type.String({ description: "Path to the generated campaign cases.csv" }),
      case_ids: Type.Array(
        Type.String({ description: "Exact case_id, for example R000010" }),
        { minItems: 1, maxItems: 50 },
      ),
    }),
    async execute(_id, params, signal, _onUpdate, ctx) {
      const args = ["campaign-rerun-plan", "--cases", params.cases];
      for (const caseId of params.case_ids) args.push("--case-id", caseId);
      const plan = await bridge(args, signal);
      if (plan.error || !plan.can_start) return toolResult(plan);
      if (!ctx.hasUI) return toolResult({ error: "selective rerun requires interactive confirmation", plan });

      const casesText = (plan.selected_cases ?? [])
        .map((item: any) => {
          const historySize = item.reactor_history?.size_bytes;
          const historyText = historySize === null || historySize === undefined
            ? "history missing"
            : `reactor_history=${historySize} B`;
          return `${item.case_id} [${item.current_status}; ${historyText}]`;
        })
        .join(", ");
      const policy = plan.execution_policy ?? {};
      const ok = await ctx.ui.confirm(
        "Selectively rerun NRG cases?",
        `Rerun exactly ${plan.selected_count} case(s): ${casesText}. ` +
          `${plan.other_cases_untouched} other case(s) will not be executed. ` +
          `Prior compact run artifacts will be archived under each case's _rerun_archive/<job_id>/. ` +
          `The trusted skip policy itself is unchanged. ` +
          `Concurrency=${policy.max_concurrent_cases ?? 1}, threads/case=${policy.threads ?? 1}.`,
      );
      if (!ok) return toolResult({ cancelled: true, reason: "user declined selective rerun", plan });

      const startArgs = ["campaign-rerun-start", "--cases", params.cases];
      for (const caseId of params.case_ids) startArgs.push("--case-id", caseId);
      return toolResult(await bridge(startArgs, signal));
    },
  });

  pi.registerTool({
    name: "nrg_campaign_job_status",
    label: "NRG Campaign Job Status",
    description: "Inspect a background campaign-runner job, current per-case status counts, and the authoritative live laboratory-runner lock state.",
    promptSnippet: "Check progress of a background NRG campaign job and live runner state",
    promptGuidelines: [
      "Use nrg_campaign_job_status for ongoing campaign monitoring; do not poll more frequently than needed.",
      "For current lock state, treat the top-level laboratory_runner field as authoritative. job.runner_lock is persisted job metadata and, for older jobs, may be only an acquisition-time snapshot.",
      "Do not report that a completed job still holds the runner lock when laboratory_runner.active is false.",
      "When a stop was requested, inspect operator_control.state: handled means the trusted runner applied it; requested/acknowledged means it is still pending; rejected means it was not applied.",
    ],
    parameters: Type.Object({
      job: Type.String({ description: "Job id returned by nrg_campaign_start, or job JSON path" }),
    }),
    async execute(_id, params, signal) {
      return toolResult(await bridge(["campaign-job-status", "--job", params.job], signal));
    },
  });

  pi.registerTool({
    name: "nrg_campaign_stop",
    label: "NRG Stop Campaign",
    description: "Gracefully stop the active trusted campaign runner. If a case is currently running, the runner requests NRG finalization through run_control.stop, records that case as stopped, and exits without launching another case. Requires confirmation.",
    promptSnippet: "Stop an active NRG campaign runner cleanly without starting another case",
    promptGuidelines: [
      "Use this tool instead of killing the campaign-runner Python process or computing_module manually.",
      "A campaign stop is operational, not scientific success: the active case becomes stopped unless it independently satisfies a trusted condition first.",
      "After the request, verify the returned control state or use nrg_campaign_job_status. Do not report completion while the stop request is merely pending.",
      "The stopped case is rerunnable under ordinary trusted policy; completed cases remain untouched and no additional case is launched by the stopped job.",
    ],
    parameters: Type.Object({
      job: Type.Optional(Type.String({ description: "Active runner job id; omit only when there is exactly one live runner" })),
      reason: Type.Optional(Type.String({ description: "Short operator reason recorded in stop provenance" })),
    }),
    async execute(_id, params, signal, _onUpdate, ctx) {
      const planArgs = ["campaign-stop-plan"];
      if (params.job) planArgs.push("--job", params.job);
      const plan = await bridge(planArgs, signal);
      if (plan.error || !plan.can_stop) return toolResult(plan);
      if (!ctx.hasUI) return toolResult({ error: "campaign stop requires interactive confirmation", plan });
      const activeCase = plan.active_case?.case_id
        ? ` Current case ${plan.active_case.case_id} will receive a graceful external stop request.`
        : " No CFD case is currently active; the runner will stop before the next case.";
      const ok = await ctx.ui.confirm(
        "Stop active NRG campaign?",
        `Stop runner job ${plan.job_id}.${activeCase} No further cases will be launched by this job.`,
      );
      if (!ok) return toolResult({ cancelled: true, plan });
      const execArgs = ["campaign-stop-execute"];
      if (params.job) execArgs.push("--job", params.job);
      if (params.reason) execArgs.push("--reason", params.reason);
      return toolResult(await bridge(execArgs, signal, 60_000));
    },
  });

  pi.registerTool({
    name: "nrg_case_stop",
    label: "NRG Stop Active Case",
    description: "Gracefully stop one explicitly named case only when it is the case currently executing under the trusted runner. The runner remains active and may continue with later cases. Requires confirmation.",
    promptSnippet: "Stop the currently running NRG case while allowing the campaign runner to continue",
    promptGuidelines: [
      "Use exact case IDs grounded in nrg_case_inspect or campaign/job status; the tool refuses to stop a case that is not currently active.",
      "Use nrg_campaign_stop instead when the user wants the entire campaign runner to stop and no later cases to launch.",
      "The case stop uses run_control.stop through the trusted runner and records status=stopped; stopped is incomplete and must not be reported as scientific success.",
      "After the request, inspect the returned control state or job status before claiming that the case stopped.",
    ],
    parameters: Type.Object({
      cases: Type.String({ description: "Path to the active campaign cases.csv" }),
      case_id: Type.String({ description: "Exact currently running case_id" }),
      reason: Type.Optional(Type.String({ description: "Short operator reason recorded in stop provenance" })),
    }),
    async execute(_id, params, signal, _onUpdate, ctx) {
      const plan = await bridge([
        "case-stop-plan", "--cases", params.cases, "--case-id", params.case_id,
      ], signal);
      if (plan.error || !plan.can_stop) return toolResult(plan);
      if (!ctx.hasUI) return toolResult({ error: "case stop requires interactive confirmation", plan });
      const ok = await ctx.ui.confirm(
        "Stop active NRG case?",
        `Stop ${params.case_id} in runner job ${plan.job_id}. The runner may continue with later cases after this case is finalized.`,
      );
      if (!ok) return toolResult({ cancelled: true, plan });
      const args = [
        "case-stop-execute", "--cases", params.cases, "--case-id", params.case_id,
      ];
      if (params.reason) args.push("--reason", params.reason);
      return toolResult(await bridge(args, signal, 60_000));
    },
  });

  pi.registerTool({
    name: "nrg_create_study",
    label: "NRG Create Study",
    description: "Create an agent-editable scientific analysis study from the controlled template.",
    promptSnippet: "Create a hypothesis-specific analysis workspace for an NRG campaign",
    promptGuidelines: ["Use nrg_create_study when the scientific question requires new or modified analysis logic; then edit only files inside that study."],
    parameters: Type.Object({
      slug: Type.String(),
      request: Type.String({ description: "Scientific analysis objective" }),
      cases: Type.String({ description: "Path to cases.csv" }),
      force: Type.Optional(Type.Boolean()),
    }),
    async execute(_id, params, signal) {
      const args = ["create-study", "--slug", params.slug, "--request", params.request, "--cases", params.cases];
      if (params.force) args.push("--force");
      return toolResult(await bridge(args, signal));
    },
  });

  pi.registerTool({
    name: "nrg_study_pilot_plan",
    label: "NRG Study Pilot Plan",
    description: "Choose a deterministic representative subset for validating new or substantially modified study analysis code before full-campaign execution. The planner uses the campaign logical-identity axes and maximin coverage; it is not a statistical sample.",
    promptSnippet: "Plan a representative pilot subset before running a large NRG analysis study",
    promptGuidelines: [
      "For a new or substantially modified analysis on a large campaign, call this before nrg_run_study_pilot.",
      "Prefer the deterministic identity-space subset over the first N cases or an unrecorded random sample.",
      "Inspect the returned identity-axis coverage. Add deliberately difficult exact cases if the proposed subset misses an important regime.",
      "A pilot validates analysis implementation and output contract; it is not evidence for global scientific conclusions.",
    ],
    parameters: Type.Object({
      study: Type.String({ description: "Study directory path or name under studies_root" }),
      max_cases: Type.Optional(Type.Integer({ minimum: 5, maximum: 50, default: 20, description: "Maximum representative pilot cases; 12-24 is normally appropriate" })),
    }),
    async execute(_id, params, signal) {
      const args = ["study-pilot-plan", "--study", params.study];
      if (params.max_cases !== undefined) args.push("--max-cases", String(params.max_cases));
      return toolResult(await bridge(args, signal));
    },
  });

  pi.registerTool({
    name: "nrg_run_study_pilot",
    label: "NRG Run Study Pilot",
    description: "Execute the current study analysis on 5-50 exact representative cases through the raw-integrity wrapper. Successful pilot validation is tied to the current analyze.py, analysis_config.toml, and cases.csv hashes and is invalidated by later changes.",
    promptSnippet: "Validate analysis code on a bounded representative subset before a large production study",
    promptGuidelines: [
      "Use this for newly written or substantially modified analysis before nrg_run_study on a large campaign.",
      "Normally obtain case IDs from nrg_study_pilot_plan rather than using the first N cases or an arbitrary random subset.",
      "Inspect pilot outputs and the structured pilot summary before declaring the analysis implementation ready for production.",
      "If analyze.py or analysis_config.toml changes after a successful pilot, rerun the pilot; the trusted full-study wrapper will reject stale validation.",
      "Do not draw campaign-wide scientific conclusions from pilot outputs.",
    ],
    parameters: Type.Object({
      study: Type.String({ description: "Study directory path or name under studies_root" }),
      case_ids: Type.Array(Type.String({ description: "Exact representative logical case_id" }), { minItems: 5, maxItems: 50 }),
    }),
    async execute(_id, params, signal) {
      const args = ["run-study-pilot", "--study", params.study];
      for (const caseId of params.case_ids) args.push("--case-id", caseId);
      return toolResult(await bridge(args, signal, 1_800_000));
    },
  });

  pi.registerTool({
    name: "nrg_run_study",
    label: "NRG Run Study Production",
    description: "Execute the full agent-authored analysis study through the provenance/raw-integrity wrapper. For campaigns larger than 50 cases, a successful current pilot validation is mandatory before production execution.",
    promptSnippet: "Run a validated scientific NRG analysis study over its full campaign",
    promptGuidelines: [
      "Use nrg_run_study only after the analysis implementation has been validated on representative pilot cases when the campaign is large.",
      "Do not repeatedly debug newly written analysis by launching the entire campaign. Use nrg_study_pilot_plan and nrg_run_study_pilot first.",
      "For large studies, design analyze.py to persist/reuse per-case structured results and separate per-case extraction from campaign aggregation.",
      "Treat raw_data_integrity_ok as mandatory and use nrg_read_study_summary before scientific interpretation.",
    ],
    parameters: Type.Object({
      study: Type.String({ description: "Study directory path or name under studies_root" }),
    }),
    async execute(_id, params, signal) {
      return toolResult(await bridge(["run-study", "--study", params.study], signal, 1_800_000));
    },
  });

  pi.registerTool({
    name: "nrg_read_study_summary",
    label: "NRG Study Summary",
    description: "Read the structured manifest, provenance, summary, and output file list from a completed analysis study.",
    promptSnippet: "Read structured results from an NRG scientific analysis study",
    promptGuidelines: ["Use nrg_read_study_summary before drawing conclusions from an analysis study."],
    parameters: Type.Object({
      study: Type.String({ description: "Study directory path or name under studies_root" }),
    }),
    async execute(_id, params, signal) {
      return toolResult(await bridge(["study-summary", "--study", params.study], signal));
    },
  });

  pi.registerCommand("nrg-tools", {
    description: "Show registered and active NRG laboratory tools",
    handler: async (_args, ctx) => {
      const registered = pi.getAllTools().map((tool) => tool.name).filter((name) => name.startsWith("nrg_")).sort();
      const active = new Set(pi.getActiveTools());
      const missing = registered.filter((name) => !active.has(name));
      const keyTools = [
        "nrg_campaign_start",
        "nrg_campaign_start_to_quasistationary",
        "nrg_campaign_run_cases_to_quasistationary",
        "nrg_campaign_stop",
        "nrg_case_stop",
        "nrg_study_pilot_plan",
        "nrg_run_study_pilot",
        "nrg_run_study",
      ];
      const lines = [
        `NRG laboratory tools: registered=${registered.length}, active=${registered.length - missing.length}`,
        "",
        "Execution / control:",
        ...keyTools.map((name) => `  ${name}: ${registered.includes(name) ? (active.has(name) ? "ACTIVE" : "INACTIVE") : "NOT REGISTERED"}`),
      ];
      if (missing.length > 0) {
        lines.push("", `Inactive registered NRG tools (${missing.length}):`, ...missing.map((name) => `  ${name}`));
      } else {
        lines.push("", "All registered NRG laboratory tools are active.");
      }
      ctx.ui.notify(lines.join("\n"), missing.length > 0 ? "warning" : "info");
    },
  });

  pi.on("session_start", async (_event, ctx) => {
    const state = await ensureLab(ctx.signal);
    if (!state) {
      ctx.ui.notify(`NRG laboratory extension could not load ${LAB_CONFIG}`, "error");
      return;
    }

    // Project policy: every tool registered by this laboratory extension is a
    // first-class trusted capability. Preserve already-active tools, but make
    // all registered nrg_* tools explicitly active so specialized execution
    // paths cannot disappear from the model-visible tool set.
    const allTools = pi.getAllTools();
    const available = new Set(allTools.map((tool) => tool.name));
    const registeredNrgTools = allTools.map((tool) => tool.name).filter((name) => name.startsWith("nrg_"));
    const active = new Set(pi.getActiveTools());
    for (const name of registeredNrgTools) active.add(name);
    for (const name of ["bash", "ls", "find", "grep"]) {
      if (available.has(name)) active.add(name);
    }
    pi.setActiveTools([...active]);

    const activeAfter = new Set(pi.getActiveTools());
    const missingNrgTools = registeredNrgTools.filter((name) => !activeAfter.has(name));
    if (missingNrgTools.length > 0) {
      ctx.ui.notify(
        `NRG laboratory tool activation incomplete; ${missingNrgTools.length} registered tool(s) remain inactive: ${missingNrgTools.join(", ")}`,
        "warning",
      );
    }

    if (!loadShellPolicy()) {
      ctx.ui.notify(`NRG read-only shell policy could not load ${READ_ONLY_SHELL_POLICY}; bash will be blocked`, "warning");
      ctx.ui.setStatus("nrg-lab", missingNrgTools.length > 0 ? "NRG lab · tools incomplete · shell blocked" : "NRG lab · shell blocked");
    } else {
      ctx.ui.setStatus("nrg-lab", missingNrgTools.length > 0 ? "NRG lab · tools incomplete · RO shell" : "NRG lab · RO shell");
    }
  });

  pi.on("tool_call", async (event, ctx) => {
    if (event.toolName === "bash") {
      const state = await ensureLab(ctx.signal);
      if (!state) return { block: true, reason: "NRG laboratory configuration is unavailable" };
      const policy = loadShellPolicy();
      if (!policy) {
        return { block: true, reason: "NRG read-only shell policy is unavailable; bash is default-deny" };
      }
      const input = event.input as { command?: string; timeout?: number };
      if (typeof input.command !== "string") {
        return { block: true, reason: "Cannot validate bash command" };
      }
      const decision = evaluateReadOnlyShell(input.command, state.researchRoot, ctx.cwd, policy, input.timeout);
      if (!decision.allowed) {
        return {
          block: true,
          reason: `NRG read-only shell blocked this command: ${decision.reason}. ` +
            "Use built-in read/search or registered nrg_* tools when possible; do not try shell variants to bypass the policy.",
        };
      }
      if (decision.command) input.command = decision.command;
      if (decision.timeout_ms !== undefined) input.timeout = decision.timeout_ms;
      return;
    }

    if (event.toolName !== "write" && event.toolName !== "edit") return;
    const state = await ensureLab(ctx.signal);
    if (!state) return { block: true, reason: "NRG laboratory configuration is unavailable" };

    const input = event.input as any;
    const rawPath = input?.path;
    if (typeof rawPath !== "string") {
      return { block: true, reason: "Cannot validate write/edit path" };
    }
    const target = resolve(ctx.cwd, rawPath);
    const definitionsRoot = join(state.campaignRoot, "definitions");
    if (inside(state.studiesRoot, target) || inside(definitionsRoot, target)) return;

    return {
      block: true,
      reason:
        `NRG laboratory policy permits agent write/edit only inside ${state.studiesRoot} ` +
        `or ${definitionsRoot}. Target was ${target}.`,
    };
  });
}
