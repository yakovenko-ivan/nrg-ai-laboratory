
  // NRG E0 read-only, evidence-gated architect escalation.
  // Script body for nicobailon/pi-subagents file-backed workflow.
  // No filesystem/network APIs or source-code mutations.

  const evidence = await runs.run("e0-architect-evidence", {
    agent: "nrg-evidence",
    label: "Check whether E0 has a concrete engineering blocker",
    context: "fresh",
    task: [
      "READ-ONLY E0 architecture-routing audit.",
      "Read the local laboratory AGENTS.md and docs/scientific-roadmap.md.",
      "Verify whether ordinary Pi agents are currently authorized to compile the",
      "external NRG equilibrium interface for a fresh TP/HP validation.",
      "If accessible through approved read tools, verify the relevant source",
      "and historical validator working-directory assumptions too.",
      "Do not treat unavailable historical files as never existing.",
      "Do not modify files, build, install, execute scripts or CFD, or bypass permissions.",
      "Identify the smallest concrete blocked scientific action and cite local",
      "source files/sections supporting the conclusion.",
      "End with exactly one marker line: ARCHITECT_REQUIRED: YES or",
      "ARCHITECT_REQUIRED: NO. Use YES only for a demonstrated engineering blocker.",
      "When YES, include labels SOURCE_EVIDENCE: and BLOCKED_ACTION: with detail.",
      "Return at most 4500 characters of substantive evidence."
    ].join("\n")
  });

  const evidenceText = typeof evidence?.output === "string" ? evidence.output : "";
  const decisions = [...evidenceText.matchAll(/^ARCHITECT_REQUIRED:\s*(YES|NO)\s*$/gim)];
  if (decisions.length !== 1) {
    return {
      verdict: "HALT_AMBIGUOUS_ROUTE",
      reason: "Evidence agent must provide exactly one valid routing marker.",
      evidence: evidenceText
    };
  }

  if (decisions[0][1].toUpperCase() === "NO") {
    return {
      verdict: "NO_ARCHITECT_REQUIRED",
      reason: "The LOW evidence child found no sufficiently supported engineering blocker.",
      evidence: evidenceText
    };
  }

  if (!/^SOURCE_EVIDENCE:\s*\S/m.test(evidenceText) ||
      !/^BLOCKED_ACTION:\s*\S/m.test(evidenceText)) {
    return {
      verdict: "HALT_MISSING_BLOCKER_EVIDENCE",
      reason: "Positive routing claim lacks source or blocked-action details.",
      evidence: evidenceText
    };
  }

  const architecture = await runs.run("e0-architecture-proposal", {
    agent: "nrg-architect",
    label: "Design the smallest approved E0 build/execution capability",
    context: "fresh",
    task: [
      "Review the following read-only E0 blocker evidence. Verify the cited",
      "laboratory instructions independently where possible.",
      "Design the MINIMUM solution for the specific scientific action blocked.",
      "Compare: (A) authorized operator-assisted baseline, and (B) narrow",
      "agent-operated Mode 3 capability, explaining which E0 gate each satisfies.",
      "Explain source-review requirements, exact permission boundary, artifact",
      "provenance, independent repositories, scratch cwd/task_setup handling,",
      "portability, and deterministic acceptance test.",
      "Do not modify files, run tests, compile, execute scientific code, or",
      "grant permissions. This is a design proposal only.",
      "Do not recommend generic privileged runner infrastructure without proof.",
      "\n--- VERIFIED-BY-LOW-AGENT EVIDENCE (RECHECK BEFORE TRUSTING) ---\n",
      evidenceText.slice(0, 10000),
      "\n--- END EVIDENCE ---"
    ].join("\n")
  });

  return {
    verdict: "ARCHITECT_INVOKED_READ_ONLY",
    evidence_agent: "nrg-evidence",
    architect_agent: "nrg-architect",
    evidence: evidenceText,
    architecture: architecture?.output ?? "NO_ARCHITECT_OUTPUT",
    note: "Check actual model identity, thinking and duration in Pi run metadata."
  };

