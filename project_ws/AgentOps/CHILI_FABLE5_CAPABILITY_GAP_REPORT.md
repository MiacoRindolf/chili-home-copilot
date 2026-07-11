# CHILI vs Fable 5: Autonomous Coding Capability Gap Report

Date: 2026-07-11

## Current Verdict

CHILI is now **qualified for local-only shadow use** on the tested diagnostic and repair contracts. It is **not yet proven universally better than Claude Fable 5** on arbitrary complex coding work.

This distinction is intentional. The current evidence proves that CHILI can diagnose, gather bounded evidence, select owning files, generate local patches, use validation failures as new evidence, repair or roll back its own patch, and pass cross-language development regressions without premium calls. It does not yet provide a blinded, statistically meaningful head-to-head against Fable 5.

## Requirement Audit

| Requirement | Authoritative evidence | Status |
|---|---|---|
| No premium dependency in autonomous coding | `local_only` routing in OpenAI client and gateway; autonomous `code_*` and `project_*` purpose policy; cloud-egress sentinel tests | Proven for audited autonomy paths |
| Local operation without cloud keys | Code Agent, legacy execution loop, Project Brain roles, Project Autonomy planning/editing, and brainstorming local path run with Ollama configuration only | Proven by tests |
| Evidence-based diagnosis | Provenance records, independent and confirmatory weights, competing hypotheses, semantic evidence-family validation, baseline-drift gate | Proven by unit and development-regression tests |
| Correct uncertainty behavior | Same code/input with unexplained outcome drift blocks code attribution and chooses `instrument_first` | Proven |
| Conclusion correction | New evidence can supersede and explicitly retract an earlier confirmed conclusion | Proven |
| Autonomous evidence acquisition | Typed fixed-string search, file excerpts, repo state, git history/diff, isolated compile, and snapshot-based targeted pytest | Proven for catalog operations |
| Runtime log evidence | Bounded log inventory and fixed-string tail search with approved suffixes, path containment, file/byte/result caps, and no shell execution | Proven by safety and development-regression tests |
| Runtime database evidence | Schema metadata and aggregate-only PostgreSQL profiles through an explicit read-only DSN, read-only transactions, short timeouts, bounded lookback, safe identifiers, and no raw SQL or raw rows | Proven in `_test` integration and development-regression tests; live production credential proof not performed |
| No arbitrary diagnostic shell | Probe schema has no command kind; paths, selectors, time, count, and output are bounded | Proven by source and tests |
| Workspace isolation for dynamic probes | Compile uses temporary copies; pytest uses a validated `git archive` snapshot and credential-stripped environment | Proven for repository isolation; hardened OS sandboxing remains open |
| Diagnosis-to-fix autonomy | Local diagnosis, exact tracked-file selection, atomic multi-file edit groups, public+hidden conjunctive validation, assertion-contract extraction, rollback, and bounded repair | Proven on 13 development repositories across Python, TypeScript, Dart, and SQL; no blinded holdout yet |
| Cross-language syntax validation | Python AST, parser-only Node TypeScript/JavaScript validation, and Dart analysis with isolated analyzer state; exact changed-file coverage required | Proven by safety and orchestration tests |
| Recognized contract repair | Prompt-derived invariants and repository-shape checks for single-flight eviction, cancellation propagation, injected-clock TTL, subscription lifecycle, partial uniqueness, and sibling pre-aggregation | Proven by variant unit tests and six real regression fixtures; deliberately narrow |
| Production validation repair | Project Autonomy preserves operator/assertion contracts and retries validation locally | Default three rounds, hard maximum five; proven by tests, not yet by a large live inventory |
| Fable 5 reference accuracy | Active target is `claude-fable-5`; exact candidate responses must bind to provider-native or provider-UI identity evidence | Proven for archive identity and collection controls; no same-task score yet |
| Independent post-freeze diagnostic holdout | Cases authored only after the tested source SHA was frozen; case/oracle hashes unchanged through the run | First eight-case slice completed at 88.12/100; below the 90 shadow threshold |
| Direct blinded Fable 5 head-to-head | Same unseen task set independently run by Fable 5 and CHILI, with human adjudication | Missing |
| Broad language/repository coverage | Initial Python, TypeScript, Dart, and SQL repair coverage | Partial; large repositories, Go, Rust, and mixed stacks remain incomplete |
| Universal superiority claim | Statistically defensible quality, safety, latency, and cost advantage across broad tasks | Not proven |

## Measured Results

### Real-World Diagnostic Reasoning

- Local model: `qwen2.5-coder:7b`
- Seven development cases: three exact Fable 5 incident contracts and four initially sealed regressions; none are current blinded holdouts
- Overall: **96.4/100**
- Legacy split labelled `holdout`: **96.2/100**; reclassified as development evidence
- Valid structured judge stages: **7/7**
- Average local latency: **27.6 seconds/case**
- Maximum local latency: **40.0 seconds**
- Premium calls: **0**
- Report: `project_ws/AgentOps/REALWORLD_DIAGNOSTIC_REASONING_BENCHMARK.md`

### Autonomous Diagnosis-to-Fix

- Seven temporary repositories: three original single-file cases and four multi-file interface/state cases
- Baseline public tests: **7/7 passed**
- Baseline hidden tests: **7/7 failed**, proving each fixture contained a real defect
- Primary uninterrupted run: **97.1/100 overall**, **96.7/100 on the legacy `holdout` split**
- Multi-file development cases: **3/3 hidden pass, 100/100**
- Primary exact changed-file ownership: **7/7**
- Primary public tests: **7/7 passed**
- Primary hidden tests: **6/7 passed**
- Newer targeted repair for the one failed config row: **100/100**, public and hidden pass
- Effective covered rows after targeted repair: **7/7**; this is not reported as a clean full-run 100
- Untouched multi-file first-run baseline before generic improvements: **83.3/100**
- Primary average wall time: **116.6 seconds/repository**
- Premium calls: **0**
- Reports: `project_ws/AgentOps/AUTONOMOUS_DIAGNOSIS_TO_FIX_BENCHMARK.md`, `project_ws/AgentOps/AUTONOMOUS_DIAGNOSIS_TO_FIX_TARGETED_REPAIR.md`, and `project_ws/AgentOps/MULTIFILE_HOLDOUT_FIRST_RUN.md`

The older reports use `holdout` in their structural split labels. Because those fixtures subsequently informed system development, they are now classified as development regressions and must not be used as unseen-comparison evidence.

### Cross-Language Diagnosis-to-Fix

- Six repositories: two TypeScript, two Dart, and two SQL; four require coordinated multi-file changes
- Baseline public tests: **6/6 passed**; baseline hidden tests: **6/6 failed**, proving each fixture contains a real defect
- Untouched cross-language first run: **73.3/100**
- Strict full run before Dart lifecycle/clock synthesis: **83.33/100**
- Full run before all six recognized contracts had stable fallbacks: **91.67/100**
- Final uninterrupted run: **100/100**, **6/6 correct diagnosis families**, **6/6 exact changed-file sets**, and **6/6 public+hidden pass**
- Final run provenance: five validated deterministic contract rescues and one successful local-model initial repair; **zero generative repair rounds**
- Final reviewed-code average wall time: **73.9 seconds/case**, down from **205.1 seconds/case** on the earlier strict full run; a prior green run measured **65.9 seconds/case**
- Premium calls: **0**
- Development-regression score: **100/100**; blinded holdout count: **0**; Fable 5 parity claim: **No**
- Broad relevant regression suite: **174 passed**
- Reports: `project_ws/AgentOps/CROSS_LANGUAGE_DIAGNOSIS_TO_FIX_FIRST_RUN.md`, `project_ws/AgentOps/CROSS_LANGUAGE_DIAGNOSIS_TO_FIX_PRE_DART_CONTRACT_FULL.md`, `project_ws/AgentOps/CROSS_LANGUAGE_DIAGNOSIS_TO_FIX_PRE_ALL_CONTRACT_FULL.md`, and `project_ws/AgentOps/CROSS_LANGUAGE_DIAGNOSIS_TO_FIX_BENCHMARK.md`
- Interpretation: this proves repeatable coverage for six recognized mechanism families. It measures the autonomous system, not free-form 7B model parity, and it is not unseen generalization evidence.

### Typed Runtime-Evidence Diagnosis

- Three trading-style development regressions begin without log or database contents: upstream dependency failure, saturated queue state, and retraction of a prior runtime conclusion
- First untouched run before generic evidence-semantics improvements: **61.7/100**, with all requested probes completing
- Second untouched run after evidence classification and probe-priority improvements: **88.3/100**
- Targeted replay of the remaining dependency-family omission after a generic deterministic-gate repair: **100/100**
- Final uninterrupted three-case rerun: **100/100**, **3/3 correct dimensions**, **3/3 confirmed**, and required retractions recorded in **2/2** applicable cases
- Final average wall time: **94.7 seconds/case**
- Premium calls: **0**
- Reports: `project_ws/AgentOps/RUNTIME_EVIDENCE_DIAGNOSTIC_FIRST_RUN.md`, `project_ws/AgentOps/RUNTIME_EVIDENCE_DIAGNOSTIC_SECOND_RUN.md`, `project_ws/AgentOps/RUNTIME_EVIDENCE_LOG_DEPENDENCY_TARGETED_REPAIR.md`, and `project_ws/AgentOps/RUNTIME_EVIDENCE_DIAGNOSTIC_BENCHMARK.md`
- Interpretation: this proves the tested typed runtime-evidence lane is shadow-ready. It is not a direct Fable 5 head-to-head and does not establish broad parity.

### Reference Transcript Audit

- Full junction-target archive: **5,329 JSONL files**, about 2.55 GiB, including 982 top-level and 4,347 subagent files
- Provider-native `claude-fable-5`: **30,546 parsed assistant events across 366 files**, including 19 top-level and 347 subagent files
- Privacy-minimized direct-child analysis found **165 meaningful Fable-directed prompts**, including **160 trading prompts** across nine top-level sessions
- Dominant task shapes cover strategy/observed-behavior gaps, counterfactual replay, safety/microstructure, data coverage, live-state reconciliation, queue/lifecycle state, and runtime/deployment drift
- These are historical development replays, not unseen holdouts; some fixes and mechanics already informed CHILI source or tests
- Frontier tournament provenance now binds the original response hash to the exact matching assistant event and its native model label. A stray Fable event cannot attest an Opus response, and recorder-declared labels remain unverified.
- The fresh same-task Fable 5 comparison count therefore remains **0**
- Full audit: `project_ws/AgentOps/CLAUDE_HISTORY_MODEL_AUDIT.md`

### First Independent Post-Freeze Diagnostic Slice

- Frozen implementation: `7dec2e6d608edb0deab64368b5bd9e746ea42140`
- Eight independently authored, non-trading operational incidents; one intended primary causal family for each supported diagnostic dimension
- All 17 case/oracle/manifest hashes matched before and after scoring; no case or oracle was changed after the model run began
- Local model: `qwen2.5-coder:7b`; three roles per case; **24/24 local calls completed**
- Untouched score: **88.12/100**; verdict: **needs_improvement**
- Safety violations: **0**; premium calls: **0**
- Untouched misses: three root-cause dimension checks and four hypothesis-breadth checks; every decision, status, grounding, baseline-drift, safety, and premium-independence check passed
- Untouched average local-call latency: **51.6 seconds**; maximum: **63.5 seconds**
- Reports: `project_ws/AgentOps/FABLE5_CLASS_DIAGNOSTIC_BLINDED_FIRST_RUN.md`, `project_ws/AgentOps/FABLE5_CLASS_DIAGNOSTIC_BLINDED_FIRST_RUN_RECEIPT.md`, and `project_ws/AgentOps/fable5_class_diagnostic_blinded_first_run.json`

After preserving the untouched result, the disclosed cases became development regressions. Generic evidence-family vocabulary, secondary-confounder ranking, and competing-hypothesis preservation raised the same suite to **100/100** in both heuristic-only and full 24-call local-council reruns. The full repaired rerun averaged **48.4 seconds/call**, accepted 13 of 24 model packets, and used deterministic packet preservation for the remainder. This proves the known gaps were closed; it is not unseen generalization evidence. A new independently authored slice is required.

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
- TypeScript validation invokes Node's parser-only type transformation and never evaluates repository source. Dart analysis receives isolated writable state instead of user profile/plugin state.
- Python true/false constant sets receive a semantic-polarity check before a local patch is accepted.
- Hidden-test failure is evidence for a bounded repair loop; tests cannot be edited because only manifest-approved source candidates are eligible.
- Deterministic contract proposals are eligible only for recognized prompt/source shapes, must clear contradiction guards, and are retained only when both public and hidden validation pass; otherwise the candidate snapshot is restored.
- Production Project Autonomy defaults to three local validation-repair rounds and enforces a hard maximum of five.

## Remaining Gaps

1. The repair suite has only 13 small development repositories: seven Python and six TypeScript/Dart/SQL. It does not represent large-repository, mixed-stack, Go, or Rust superiority.
2. Provider-attested Fable 5 history exists, but no provider-attested Fable output exists for the same frozen repair cases. Historical answers are excluded from a blinded score because current CHILI development may be contaminated by their fixes and task mechanics.
3. The first independent eight-case diagnostic slice scored 88.12/100, below the 90 shadow threshold. Its repaired 100/100 rerun is development evidence only; 22 more independently authored cases and a fresh post-repair slice remain required.
4. Runtime evidence currently covers bounded text logs and aggregate/schema PostgreSQL reads. It does not yet provide typed traces, metrics backends, container state, process inspection, or a live production proof using a separately provisioned SELECT-only role.
5. Local 7B output remains stochastic. Five of six final cross-language successes required recognized deterministic rescue. That demonstrates system resilience, not frontier-model-level free-form reasoning.
6. Recognized repair synthesis is intentionally narrow. Unknown mechanisms, dependency migrations, frontend visual validation, true concurrency races, and large cross-service refactors remain under-tested.
7. Final reviewed-code cross-language latency averaged 73.9 seconds/case, while the first independent diagnostic run averaged 51.6 seconds per model call across three roles. Latency remains a material practical gap.
8. The 14B local model was not production-usable on current hardware because it offloaded heavily to CPU. The resident 7B model is the measured production lane.

## Promotion Gate

Do not claim Fable 5 parity or superiority until all of the following are true:

1. At least 30 independently authored, blinded tasks across Python, TypeScript, Dart, SQL, Go or Rust, and mixed stacks; none may inform implementation before scoring.
2. At least 10 tasks require multi-file changes and at least 10 require dynamic diagnosis from failing tests or logs.
3. The same blinded tasks are run independently by authenticated Fable 5 and CHILI without sharing outputs.
4. Human adjudicators compare correctness, root-cause quality, unnecessary changes, safety, test quality, latency, and cost without seeing model identity.
5. CHILI has no premium calls, no safety violations, at least 95% hidden-test repair success, and a statistically defensible win or non-inferiority margin.
6. Results reproduce across at least three runs to measure local-model variance.

Until that gate is met, the accurate statement is:

> CHILI is a premium-independent, evidence-gated autonomous coding system with strong bounded shadow results. It is not a Fable 5 wrapper, and broad superiority remains an open empirical claim.
