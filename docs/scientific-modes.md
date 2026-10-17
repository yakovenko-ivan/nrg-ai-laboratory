# NRG AI Laboratory — scientific mission and three operating modes

**Project direction (2026-10-16).** The product is evaluated by scientific results, numerical improvements, reproducibility and researcher time saved. The three modes below describe the intended product, **not** a claim that all its functionality already exists. Consult `AGENTS.md` for current ordinary-agent permissions.

## Mode 1 — Scientific campaign assistant

**Workflow:** research prompt → inspect/create task-specific NRG package interface → researcher approves changed Fortran source once → build → generate namelists and campaign → execute/monitor or export to another computer → verify against scientific references → plots/report.

NRG computing-core code remains frozen. An approved interface version should support different parameter values without rebuilding. The agent handles routine preparation, compilation, execution, provenance and analysis after appropriate authorization.

Priority tasks: (1) TP/HP chemical equilibrium against Cantera; (2) homogeneous-reactor tests, Cantera comparisons and chemistry-solver benchmarking; (3) 1D inlet-stabilized flames, solvers, convergence, flamelets and Poisson/chemistry performance; (4) 1D wall flames with transport, radiation, geometry and R(t) dynamics; (5) multicomponent Sod/contact tests for CPM and CABARET with suitable references.

## Mode 2 — Numerical computing-core developer

**Workflow:** scientific or numerical objective → baseline/reference → candidate NRG source change in separate local worktree → researcher inspects **each source candidate before it is compiled/executed** → automated build and benchmark campaign → scientific analysis → next candidate/acceptance decision.

Both computing core and interface may be modified. Campaign outputs are evidence for each iteration; comparison must address physical invariants, stability, accuracy, grid convergence and efficiency. Do not demand bitwise equality to old simulations when the correct solver improves. No autonomous GitHub push or merge.

Priorities: 1D/2D inlet-stabilized and cellular flames, flame stabilization, low-Mach gas dynamics, QSS2 chemistry backend optimization and multigrid Poisson solution.

## Mode 3 — Laboratory capability development

Mode 3 changes the laboratory infrastructure needed by Modes 1 and 2. Primary environment is the Acer Windows 10 / Xubuntu VirtualBox VM, where isolation and restrictions can be supplied **externally** by the operator. Do not recreate experimental privileged accounts, NRG mirrors under `/opt` or `/var`, or sandbox frameworks without a demonstrated scientific need. Mode 3 must explicitly authorize work on trusted code; the existing normal Pi agent remains subject to `AGENTS.md`.

Mode 1 and 2 primarily target an ASUS Windows 11 / WSL2 laptop, and optionally Linux/HPC workers. Portability means clean independent clones of NRG and the laboratory, reproducible installation/build, no absolute Acer dependencies, appropriate local configuration and transferable documented scientific inputs/results.

## Mandatory justification for infrastructure work

Every proposed change should state: **blocked scientific action; observed failure; smallest code change; capability enabled; runnable acceptance test; position on the scientific roadmap.** Avoid implementing generic mechanisms when existing NRG code and a narrow script can already complete the experiment.

See [roadmap](scientific-roadmap.md), [first equilibrium pilot](mode1-equilibrium-pilot.md), [installation](installation.md) and [current agent policy](../AGENTS.md).
