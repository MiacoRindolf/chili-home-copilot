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
| Runtime log evidence | Bounded log inventory and fixed-string tail search with approved suffixes, path containment, file/byte/result caps, and no shell execution | Proven by safety tests and sealed holdouts |
| Runtime database evidence | Schema metadata and aggregate-only PostgreSQL profiles through an explicit read-only DSN, read-only transactions, short timeouts, bounded lookback, safe identifiers, and no raw SQL or raw rows | Proven in `_test` integration tests and sealed holdouts; live production credential proof not performed |
| No arbitrary diagnostic shell | Probe schema has no command kind; paths, selectors, time, count, and output are bounded | Proven by source and tests |
| Workspace isolation for dynamic probes | Compile uses temporary copies; pytest uses a validated `git archive` snapshot and credential-stripped environment | Proven for repository isolation; hardened OS sandboxing remains open |
| Diagnosis-to-fix autonomy | Local diagnosis, exact tracked-file selection, atomic multi-file edit groups, public+hidden conjunctive validation, assertion-contract extraction, and bounded repair | Proven on seven Python repositories, including four multi-file cases |
| Production validation repair | Project Autonomy preserves operator/assertion contracts and retries validation locally | Default three rounds, hard maximum five; proven by tests, not yet by a large live inventory |
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

- Seven temporary repositories: three original single-file cases and four multi-file interface/state cases
- Baseline public tests: **7/7 passed**
- Baseline hidden tests: **7/7 failed**, proving each fixture contained a real defect
- Primary uninterrupted run: **97.1/100 overall**, **96.7/100 holdout**
- Multi-file holdout-derived cases: **3/3 hidden pass, 100/100**
- Primary exact changed-file ownership: **7/7**
- Primary public tests: **7/7 passed**
- Primary hidden tests: **6/7 passed**
- Newer targeted repair for the one failed config row: **100/100**, public and hidden pass
- Effective covered rows after targeted repair: **7/7**; this is not reported as a clean full-run 100
- Untouched multi-file first-run baseline before generic improvements: **83.3/100**
- Primary average wall time: **116.6 seconds/repository**
- Premium calls: **0**
- Reports: `project_ws/AgentOps/AUTONOMOUS_DIAGNOSIS_TO_FIX_BENCHMARK.md`, `project_ws/AgentOps/AUTONOMOUS_DIAGNOSIS_TO_FIX_TARGETED_REPAIR.md`, and `project_ws/AgentOps/MULTIFILE_HOLDOUT_FIRST_RUN.md`

### Typed Runtime-Evidence Diagnosis

- Three sealed trading-style cases begin without log or database contents: upstream dependency failure, saturated queue state, and retraction of a prior runtime conclusion
- First untouched run before generic evidence-semantics improvements: **61.7/100**, with all requested probes completing
- Second untouched run after evidence classification and probe-priority improvements: **88.3/100**
- Targeted replay of the remaining dependency-family omission after a generic deterministic-gate repair: **100/100**
- Final uninterrupted three-case rerun: **100/100**, **3/3 correct dimensions**, **3/3 confirmed**, and required retractions recorded in **2/2** applicable cases
- Final average wall time: **94.7 seconds/case**
- Premium calls: **0**
- Reports: `project_ws/AgentOps/RUNTIME_EVIDENCE_DIAGNOSTIC_FIRST_RUN.md`, `project_ws/AgentOps/RUNTIME_EVIDENCE_DIAGNOSTIC_SECOND_RUN.md`, `project_ws/AgentOps/RUNTIME_EVIDENCE_LOG_DEPENDENCY_TARGETED_REPAIR.md`, and `project_ws/AgentOps/RUNTIME_EVIDENCE_DIAGNOSTIC_BENCHMARK.md`
- Interpretation: this proves the tested typed runtime-evidence lane is shadow-ready. It is not a direct Fable 5 head-to-head and does not establish broad parity.

## Safety Boundaries

- Automatic probes cannot represent Docker, broker, deployment, process restart, database mutation, network mutation, or arbitrary shell execution.
- Log probes read only approved text-log suffixes under the authorized repository root and enforce file, tail, byte, match, and output limits.
- Database probes expose schema metadata or bounded aggregates only. They have no raw-SQL field, return no raw rows, use `NullPool`, assert PostgreSQL transaction read-only mode, and require a separate production read-only DSN that differs from `DATABASE_URL`.
- Production aggregate profiles require an explicit timestamp column and bounded lookback; `_test` databases remain eligible for fixture-driven integration tests.
- Runtime and live experiments remain non-automatic.
- Targeted tests must use a selector under `tests/` and run from a temporary committed snapshot.
- Snapshot execution protects the source workspace but is not a hardened operating-system sandbox; only already-committed, selector-bounded tests are eligible.
- Subprocess environments omit API keys, broker credentials, database credentials other than an explicitly `_test`-suffixed test URL, and user-provided environment maps.
- Model edits are accepted only as exact SEARCH/REPLACE blocks, validated diffs, or one guarded full-file fence with syntax, similarity, and size checks.
- Python true/false constant sets receive a semantic-polarity check before a local patch is accepted.
- Hidden-test failure is evidence for a bounded repair loop; tests cannot be edited because only manifest-approved source candidates are eligible.
- Production Project Autonomy defaults to three local validation-repair rounds and enforces a hard maximum of five.

## Remaining Gaps

1. The repair suite has only seven small Python repositories. Four require multi-file changes, but this still does not represent large-repository or multi-language superiority.
2. No same-task Fable 5 output exists for the sealed repair cases. Current Fable evidence is incident-derived calibration, not a direct tournament.
3. Runtime evidence currently covers bounded text logs and aggregate/schema PostgreSQL reads. It does not yet provide typed traces, metrics backends, container state, process inspection, or a live production proof using a separately provisioned SELECT-only role.
4. Local 7B output remains stochastic. Deterministic evidence gates recovered an omitted causal family and raised the runtime holdout from 61.7 to 100, but the final lane still averaged 94.7 seconds per case and needs unchanged-code repeat runs.
5. Multi-file interface and state repairs now have initial evidence; dependency migrations, frontend visual validation, true concurrency races, and large cross-service refactors remain under-tested.
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
