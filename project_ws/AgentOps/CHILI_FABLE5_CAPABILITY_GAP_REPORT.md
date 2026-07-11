# CHILI vs Fable 5: Autonomous Coding Capability Gap Report

Date: 2026-07-11

## Current Verdict

CHILI is now **qualified for local-only shadow use** on the tested diagnostic and repair contracts. It is **not yet proven universally better than Claude Fable 5** on arbitrary complex coding work.

This distinction is intentional. The current evidence proves that CHILI can diagnose, gather bounded evidence, select an owning file, generate a local patch, use validation failures as new evidence, repair its own patch, and pass sealed tests without premium calls. It does not yet provide a blinded, statistically meaningful, multi-language head-to-head against Fable 5.

## Requirement Audit

| Requirement | Authoritative evidence | Status |
|---|---|---|
| No premium dependency in autonomous coding | `local_only` routing in OpenAI client and gateway; autonomous `code_*` and `project_*` purpose policy; cloud-egress sentinel tests | Proven for audited autonomy paths |
| Local operation without cloud keys | Code Agent, legacy execution loop, Project Brain roles, Project Autonomy planning/editing, and brainstorming local path run with Ollama configuration only | Proven by tests |
| Evidence-based diagnosis | Provenance records, independent and confirmatory weights, competing hypotheses, semantic evidence-family validation, baseline-drift gate | Proven by unit and holdout tests |
| Correct uncertainty behavior | Same code/input with unexplained outcome drift blocks code attribution and chooses `instrument_first` | Proven |
| Conclusion correction | New evidence can supersede and explicitly retract an earlier confirmed conclusion | Proven |
| Autonomous evidence acquisition | Typed fixed-string search, file excerpts, repo state, git history/diff, isolated compile, and snapshot-based targeted pytest | Proven for catalog operations |
| No arbitrary diagnostic shell | Probe schema has no command kind; paths, selectors, time, count, and output are bounded | Proven by source and tests |
| Workspace isolation for dynamic probes | Compile uses temporary copies; pytest uses a validated `git archive` snapshot and credential-stripped environment | Proven for repository isolation; hardened OS sandboxing remains open |
| Diagnosis-to-fix autonomy | Local diagnosis, planning, file selection, edit adaptation, public tests, hidden tests, and at most two repair rounds | Proven on three sealed Python repositories |
| Fable 5 reference accuracy | Active reference is `claude-fable-5`; calibration incidents come from an authenticated Fable 5 task ledger and merged fixes | Proven for three incident contracts |
| Direct blinded Fable 5 head-to-head | Same unseen task set independently run by Fable 5 and CHILI, with human adjudication | Missing |
| Broad language/repository coverage | Large Python, TypeScript, Dart, SQL, Go, Rust, and mixed-repository repairs | Incomplete |
| Universal superiority claim | Statistically defensible quality, safety, latency, and cost advantage across broad tasks | Not proven |

## Measured Results

### Real-World Diagnostic Reasoning

- Local model: `qwen2.5-coder:7b`
- Seven cases: three exact Fable 5 incident contracts and four sealed holdouts
- Overall: **96.4/100**
- Holdout: **96.2/100**
- Valid structured judge stages: **7/7**
- Average local latency: **27.6 seconds/case**
- Maximum local latency: **40.0 seconds**
- Premium calls: **0**
- Report: `project_ws/AgentOps/REALWORLD_DIAGNOSTIC_REASONING_BENCHMARK.md`

### Autonomous Diagnosis-to-Fix

- Three sealed temporary repositories: clock, config, and data source/sink failures
- Baseline public tests: **3/3 passed**
- Baseline hidden tests: **3/3 failed**, proving each fixture contained a real defect
- Final diagnosis dimension: **3/3 correct**
- Final owning file: **3/3 correct**
- Final public tests: **3/3 passed**
- Final hidden tests: **3/3 passed**
- Overall: **100/100**
- Average wall time: **56.2 seconds/repository**
- Premium calls: **0**
- The data case required two repair rounds, demonstrating validation-driven correction rather than first-answer acceptance
- Report: `project_ws/AgentOps/AUTONOMOUS_DIAGNOSIS_TO_FIX_BENCHMARK.md`

## Safety Boundaries

- Automatic probes cannot represent Docker, broker, deployment, process restart, database mutation, network mutation, or arbitrary shell execution.
- Runtime and live experiments remain non-automatic.
- Targeted tests must use a selector under `tests/` and run from a temporary committed snapshot.
- Snapshot execution protects the source workspace but is not a hardened operating-system sandbox; only already-committed, selector-bounded tests are eligible.
- Subprocess environments omit API keys, broker credentials, database credentials other than an explicitly `_test`-suffixed test URL, and user-provided environment maps.
- Model edits are accepted only as exact SEARCH/REPLACE blocks, validated diffs, or one guarded full-file fence with syntax, similarity, and size checks.
- Hidden-test failure is evidence for a bounded repair loop; tests cannot be edited because only manifest-approved source candidates are eligible.

## Remaining Gaps

1. The repair holdout has only three small Python repositories. It proves the control loop, not broad coding superiority.
2. No same-task Fable 5 output exists for the sealed repair cases. Current Fable evidence is incident-derived calibration, not a direct tournament.
3. The generic probe catalog cannot yet query application logs, schemas, or databases through typed read-only adapters. Those were important in the real NBBO investigation.
4. Local 7B output remains stochastic. Deterministic evidence gates and repair loops contain errors, but difficult tasks can require more calls and higher latency.
5. Multi-file architectural changes, dependency migrations, frontend visual validation, concurrency bugs, and large cross-service refactors remain under-tested.
6. The 14B local model was not production-usable on current hardware because it offloaded heavily to CPU. The resident 7B model is the measured production lane.

## Promotion Gate

Do not claim Fable 5 parity or superiority until all of the following are true:

1. At least 30 sealed tasks across Python, TypeScript, Dart, SQL, Go or Rust, and mixed stacks.
2. At least 10 tasks require multi-file changes and at least 10 require dynamic diagnosis from failing tests or logs.
3. The same sealed tasks are run independently by authenticated Fable 5 and CHILI without sharing outputs.
4. Human adjudicators compare correctness, root-cause quality, unnecessary changes, safety, test quality, latency, and cost without seeing model identity.
5. CHILI has no premium calls, no safety violations, at least 95% hidden-test repair success, and a statistically defensible win or non-inferiority margin.
6. Results reproduce across at least three runs to measure local-model variance.

Until that gate is met, the accurate statement is:

> CHILI is a premium-independent, evidence-gated autonomous coding system with strong bounded shadow results. It is not a Fable 5 wrapper, and broad superiority remains an open empirical claim.
