
// NRG E0: Read-only LOW -> HIGH subagent orchestration.
// Uses the installed pi-subagents workflow API.
// No compilation, scientific execution, or source modifications.

const evidence = await runs.run("e0-evidence", {
  agent: "nrg-evidence",
  label: "Collect E0 evidence",
  context: "fresh",
  task: [
    "Perform a READ-ONLY E0 preflight.",
    "Read AGENTS.md, docs/scientific-modes.md,",
    "and docs/mode1-equilibrium-pilot.md.",
    "Inspect the current equilibrium interface where accessible.",
    "Report verified facts with source paths.",
    "Identify historical evidence, Stage B blockers, and unknowns.",
    "Do not modify files, compile, execute NRG/Cantera,",
    "install software, or bypass laboratory permissions.",
    "Return a concise, traceable evidence report."
  ].join("\n")
});

if (
  !evidence ||
  typeof evidence.output !== "string" ||
  evidence.output.trim().length < 100
) {
  return {
    verdict: "BLOCKED",
    stage: "evidence",
    reason: "The LOW agent did not produce sufficient evidence.",
    evidence: evidence?.output ?? null
  };
}

// Automatically escalate to the HIGH-reasoning scientific reviewer.
const scientific = await runs.run("e0-scientific-review", {
  agent: "nrg-scientist",
  label: "Review E0 science",
  context: "fresh",
  task: [
    "Review the following evidence for E0 TP/HP readiness.",
    "Distinguish historical validation from fresh results.",
    "Assess thermochemical consistency and physical checks.",
    "Identify missing evidence for a KEROMNES reproduction.",
    "Do not modify files, compile, or run calculations.",
    "Return READY_FOR_STAGE_B_DESIGN,",
    "CONDITIONALLY_READY, or BLOCKED with justification.",
    "",
    "--- EVIDENCE START ---",
    evidence.output,
    "--- EVIDENCE END ---"
  ].join("\n")
});

return {
  workflow: "NRG E0 LOW-to-HIGH smoke test",
  evidence_agent: "nrg-evidence",
  scientific_agent: "nrg-scientist",
  evidence: evidence.output,
  scientific_review: scientific?.output ?? "NO REVIEW OUTPUT",
  note: "Verify actual models in Pi execution metadata."
};
