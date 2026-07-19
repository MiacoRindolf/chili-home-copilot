# CHILI vs Fable 5: Autonomous Coding Capability Gap Report

Date: 2026-07-12

## Current Verdict

CHILI is qualified for local-only development shadow use **only on recognized and regression-tested contracts**.
It is not yet a credible Fable 5 replacement for unseen complex diagnosis-to-fix work. The latest fully
interpretable unseen composite remains the fifteenth holdout at **41.88/100 with 1/8 sealed-final solves**. The
newer seventeenth holdout completed durably with zero premium calls and **0/8 sealed-final solves**; its recorded
27.5/100 composite has an invalid diagnosis-label component, but even its maximum possible corrected score is
only 42.5/100.

This distinction is intentional. The current evidence proves that CHILI can diagnose, gather bounded evidence,
select owning files, generate local patches, use validation failures as new evidence, repair or roll back its own
patch, and pass cross-language development regressions without premium calls. Post-seventeenth hardening also
adds bounded same-model timeout recovery, atomic partial-bundle recovery, public load/name/type correction,
validated-progress refinement, and symbolic half-open history reasoning. It does not yet provide a blinded,
statistically meaningful head-to-head against Fable 5.

The historical diagnosis-to-fix reports used the label `hidden` for tests that were loaded after the initial patch but could then guide bounded repair. Those scores are feedback-guided development evidence, not sealed final-adjudication evidence. Runner schema v3 now separates repair-feedback tests from a final oracle that is first read after every model call for its case. The first eight-case v3 holdout scored 53.75/100 with 1/8 final passes. After generic repair-loop hardening, a new independently authored eight-case holdout improved to 68.75/100 and 2/8 final passes, still far below replacement readiness.

A later contract-guided replay of the now-disclosed tenth suite scored 58.12/100 with the same 2/8 sealed-final
passes, 6/8 correct causal families, and 5/8 exact owner sets. It used zero premium calls but required 105.6
minutes. This is development evidence only: diagnosis improved, while functional reliability and latency remained
well below replacement readiness.

After six generic source-shape operators and production-aligned deterministic-first routing were frozen, another
disclosed replay reached 100/100 with 8/8 sealed-final passes, 58 local calls, zero premium calls, and 24.1 minutes
of case time. This proves complete regression coverage for those disclosed mechanisms. It does not change the
untouched result or establish transfer to new complex incidents.

After the remaining Vary and tenant-scoped interval SQL families received variant-tested operators, a final
disclosed replay retained 100/100 while falling to 24 diagnostic-only calls, zero escalation calls, and 10.1
minutes. This is the current development regression baseline, not an unseen score.

The independently authored eleventh transfer suite then decisively separated regression mastery from general
reasoning. It scored **32.92/100**, solved **0/12** sealed finals, selected **4/12** correct causal families and
**1/12** exact owner sets, recognized no deterministic repair, and took 243.3 minutes. All 226 calls were local,
but 22/56 14B calls timed out. This is the current authoritative readiness result.

After that result was sealed, commit `9b69cd8db6d9a97717422202e751b762b7ff7dc7` repaired disclosed failure
mechanisms without changing the authoritative score. The batch added source-structural recognition for ordered
sequence identity, class-owned rejection slots, and monotonic materialized SQL heads; required contract-owner
coverage and atomic repair groups; incomplete-test-inventory semantics; a per-case local-model wall budget;
compact local escalation; and per-model Ollama endpoint pinning under one total request deadline. The disclosed
Node and SQL transfer cases then passed public, repair-feedback, and fresh isolated final tests. Broad affected
validation passed 183 tests plus 14 focused production-repair tests. This is development replay evidence only.
The next readiness evidence must come from a newly authored untouched suite.

That new twelfth suite has now completed. It contained three independently authored cases each in Python, Node
ESM, Dart, and SQLite SQL. Two independent rejection rounds removed disclosed-family and cross-language semantic
duplicates before a V3 validator passed all 12 cases. The untouched run scored **49.17/100**, solved **2/12**
sealed finals, selected **7/12** correct causal families and **6/12** exact owner sets, preserved **12/12** public
suites, and used zero premium calls. Operationally, it completed in **59.9 minutes** with **140/141** successful
local calls, versus 243.3 minutes and 204/226 successful calls on the eleventh suite. This is meaningful progress,
but 10/12 unseen final failures still decisively reject replacement readiness.

## Requirement Audit

| Requirement | Authoritative evidence | Status |
|---|---|---|
| No premium dependency in autonomous coding | `local_only` routing in OpenAI client and gateway; autonomous `code_*` and `project_*` purpose policy; cloud-egress sentinel tests | Proven for audited autonomy paths |
| Local operation without cloud keys | Code Agent, legacy execution loop, Project Brain roles, Project Autonomy planning/editing, and brainstorming local path run with Ollama configuration only | Proven by tests |
| Evidence-based diagnosis | Provenance records, independent and confirmatory weights, competing hypotheses, semantic evidence-family validation, baseline-drift gate | Proven by unit and development-regression tests |
| Causal timeline and state transitions | Event-time ordering, entity transitions, expected/actual state violations, causal parent edges, downstream symptom closure, and source/runtime revision parity | Proven for structured evidence and typed-probe timestamps; automatic trace/log edge extraction remains partial |
| Cross-service provenance graph | Bounded component/evidence nodes, producer-consumer-sink flow edges, hashed correlation groups, independence clusters, artifact-hash divergence, and first broken edge selection | Proven for explicit flow metadata and bounded log correlation extraction; external trace backends remain open |
| Correct uncertainty behavior | Same code/input with unexplained outcome drift blocks code attribution and chooses `instrument_first` | Proven |
| Conclusion correction | New evidence can supersede and explicitly retract an earlier confirmed conclusion | Proven |
| Leakage-safe diagnostic memory | Same-user/same-repo database scope, controlled reconstruction, evidence-grounded promotion, validation trust ranking, PostgreSQL concurrency lock, supersession, zero-overlap rejection, evaluation-mode read/write shutdown, and no raw prompt/evidence/oracle retrieval | Proven by focused and integration tests; semantic cross-repository transfer remains open |
| Autonomous evidence acquisition | Typed fixed-string search, file excerpts, repo state, git history/diff, isolated compile, snapshot-based targeted pytest, and bounded sequential information-gain selection | Proven for catalog operations and adaptive probe rounds |
| Runtime log evidence | Bounded log inventory and fixed-string tail search with approved suffixes, path containment, file/byte/result caps, and no shell execution | Proven by safety and development-regression tests |
| Runtime database evidence | Schema metadata and aggregate-only PostgreSQL profiles through an explicit read-only DSN, read-only transactions, short timeouts, bounded lookback, safe identifiers, and no raw SQL or raw rows | Proven in `_test` integration and development-regression tests; live production credential proof not performed |
| No arbitrary diagnostic shell | Probe schema has no command kind; paths, selectors, time, count, and output are bounded | Proven by source and tests |
| Workspace isolation for dynamic probes | Compile uses temporary copies; pytest uses a validated `git archive` snapshot and credential-stripped environment | Proven for repository isolation; hardened OS sandboxing remains open |
| Diagnosis-to-fix autonomy | Local diagnosis, exact tracked-file selection, atomic multi-file edit groups, public+repair-feedback validation, assertion-contract extraction, rollback, bounded repair, and isolated final adjudication | Twelfth sealed transfer: 49.17/100, 2/12 final, 7/12 diagnosis families, and 6/12 exact file sets. Improved, but failed replacement gate |
| Cross-language syntax validation | Python AST, parser-only Node TypeScript/JavaScript validation, and Dart analysis with isolated analyzer state; exact changed-file coverage required | Proven by safety and orchestration tests |
| Recognized contract repair | Prompt-derived invariants, repository-shape guards, and CHILI-owned mechanical repair operators | Eight disclosed mechanisms and two disclosed eleventh mechanisms pass development replay. Twelfth transfer recognized 0/12 deterministic repairs, so broad abstraction remains open |
| Production validation repair | Project Autonomy reads full targeted test contracts, pins original selectors, maps bounded source-owner candidates, requires contract-owner coverage, snapshots every repair scope, retains measurable same-scope progress, and atomically rolls back incomplete/regressive groups | Twelfth transfer retained 7 patches, passed feedback in 5/12, exact owners in 6/12, and finals in 2/12. Better boundedness and ownership; synthesis remains unready |
| Fable 5 reference accuracy | Active target is `claude-fable-5`; exact candidate responses must bind to provider-native or provider-UI identity evidence | Proven for archive identity and collection controls; eight-case same-task pack frozen, provider run pending |
| Independent post-freeze diagnostic holdout | Cases authored only after the tested source SHA was frozen; case/oracle hashes unchanged through the run | Eight eight-case slices completed at 88.12/100, 87.5/100, 76.25/100, 69.38/100, 74.4/100, 67.5/100, 63.8/100, and 83.12/100; all below the 90 shadow threshold. The eighth oracle was dimension-lenient; strict primary-family scoring is 70.62/100 |
| Durable local benchmark execution | Atomic per-case checkpoints bound to source, runner, public inputs, model, stages, and inference parameters; incompatible resumes fail closed | Proven by simulated interruption and compatible resume; real host-loss recovery remains untested |
| Direct blinded Fable 5 head-to-head | Same unseen task set independently run by Fable 5 and CHILI, with human adjudication | Collection-ready for one sealed eight-case comparison; provider run and human adjudication pending |
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

The older `hidden` column is also historical terminology. Those assertions were available as repair feedback after the initial patch and therefore were not a separate final gate. Their scores measure bounded feedback-driven repair, not untouched fix generalization.

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
- Broad relevant regression coverage after leakage-safe memory integration: **334 passed** in one combined run
- Reports: `project_ws/AgentOps/CROSS_LANGUAGE_DIAGNOSIS_TO_FIX_FIRST_RUN.md`, `project_ws/AgentOps/CROSS_LANGUAGE_DIAGNOSIS_TO_FIX_PRE_DART_CONTRACT_FULL.md`, `project_ws/AgentOps/CROSS_LANGUAGE_DIAGNOSIS_TO_FIX_PRE_ALL_CONTRACT_FULL.md`, and `project_ws/AgentOps/CROSS_LANGUAGE_DIAGNOSIS_TO_FIX_BENCHMARK.md`
- Interpretation: this proves repeatable coverage for six recognized mechanism families. It measures the autonomous system, not free-form 7B model parity, and it is not unseen generalization evidence.

As with the seven Python cases, the reported cross-language `hidden` tests guided bounded repair. The 100/100 result is retained as development-regression evidence only and is not a sealed-final score.

### First Sealed Diagnosis-to-Fix Holdout

- Frozen implementation: `70c22ded4ec715c680192bf199f185dfa20bcb52`
- Eight independently authored, CHILI-model-blind incidents; all eight require multi-file repairs
- Language mix: Python 2, TypeScript 2, Dart 2, SQL 2
- Local model: `qwen2.5-coder:7b`; three bounded repair rounds; zero premium calls
- Schema-v3 sealed score: **53.75/100**, `blinded_evaluation_failed`
- Correct diagnosis family: **1/8**
- Exact changed-file ownership: **3/8**
- Public regression pass: **8/8**
- Repair-feedback pass: **1/8**
- Sealed final pass: **1/8**
- Local calls: **106/106 successful**; average repository time **212.4 seconds**; end to end **1,704.2 seconds**
- The one final-pass case still had the wrong diagnosis family
- Protocol limitation: staged repair-oracle text was partially exposed to the parent controller after source/settings freeze, but never to the local contestant; the slice is model-blind, not fully controller-blind
- Report: `project_ws/AgentOps/FABLE5_CLASS_DIAGNOSIS_TO_FIX_BLINDED_NINTH_RUN_RECEIPT.md`

This is direct negative evidence against current Fable 5-class diagnosis-to-fix readiness. The
recognized development repairs did not transfer to seven of eight unfamiliar sealed contracts.

### Ninth-Holdout Development Replays

After preserving the untouched 53.75/100 result, selected ninth-holdout cases became disclosed development
fixtures. Generic multi-file defaults, full feedback contracts, source-owner mapping, validation-progress
rollback, rejected-attempt ledgers, and bounded local escalation were evaluated causally. A three-case 7B
source-owner replay reached 33.3% functional final success and solved the TypeScript ownership case. Standalone
14B solved the disclosed Dart case but required 1,062.3 seconds and had 0% diagnosis accuracy. Two staged 7B/14B
attempts failed; a later regression-ledger attempt solved that one disclosed case in 385.0 seconds. Qwen3 8B
scored 0% functional success on its two-case replay and was rejected.

These results justify the generic production mechanics and a rare local specialist route. They are not unseen
generalization evidence, do not alter the untouched score, and do not support Fable 5 parity. Raw reports,
JSON, hashes, and interpretation are preserved under
`project_ws/AgentOps/ninth_development_replays/DEVELOPMENT_REPLAY_RECEIPT.md`.

### Tenth Sealed Diagnosis-to-Fix Holdout

- Frozen production source: `3241f6743e8b8745c39542f4645290a4a8d6f44a`
- Fixture-only freeze: `d927f5f9f93d30e30f40a4551f4b198a3bb31202`
- Eight independently authored, fully controller-blind cases; 2 Python, 2 TypeScript, 2 Dart, and 2 SQL; every case requires exactly three source owners
- Precommitted route: 7B primary, two 7B repair rounds, one final local 14B repair round, 240-second call timeout, zero premium calls
- Untouched score: **68.75/100**, `blinded_evaluation_failed`
- Sealed-final functional success: **2/8 (25%)**
- Correct diagnosis family: **3/8 (37.5%)**
- Exact changed-file set: **7/8 (87.5%)**
- Public regressions preserved: **8/8**
- Only one case combined correct diagnosis, exact ownership, and final correctness
- **141/141** model calls succeeded: 111 on 7B and 30 on 14B; total wall time **2,786.5 seconds**, average **347.7 seconds/case**, maximum call **102.8 seconds**
- Premium calls: **0**
- Three fixture-only filename adapters were made before fixture freeze and before any contestant call; assertions and difficulty were unchanged
- Reports: `project_ws/AgentOps/FABLE5_CLASS_DIAGNOSIS_TO_FIX_BLINDED_TENTH_RUN.md` and `project_ws/AgentOps/FABLE5_CLASS_DIAGNOSIS_TO_FIX_BLINDED_TENTH_RUN_RECEIPT.md`

This is measurable post-hardening progress over the ninth run: functional solves doubled, diagnosis accuracy tripled,
and exact ownership rose from 3/8 to 7/8. It is still decisive negative evidence against current Fable 5 replacement
readiness. Six final contracts failed, five diagnosis families were wrong, and the local specialist route imposed
substantial latency. No same-task authenticated Fable 5 output was collected, so parity remains unmeasured rather
than implied.

### Eleventh Sealed Diagnosis-to-Fix Transfer Holdout

- Core source freeze: `6cf5b7e0e7da6840da57dec678f8846796265091`
- Fixture commit/tree: `5445302654e31c56e652756817be2415f7208cf0` / `667c5986ba0cd91542d08b882d7b24b4456e1d10`
- Twelve independently authored and validated cases: 4 Python, 4 TypeScript-compatible Node ESM, 2 Dart, and 2 SQL
- Nine mechanisms outside the disclosed operator library and three independently authored transfer variants
- Untouched score: **32.92/100**, `blinded_evaluation_failed`
- Sealed-final functional success: **0/12**
- Correct diagnosis family: **4/12**
- Exact changed-file set: **1/12**
- Public regressions preserved: **12/12**
- Feedback success: **1/12**; retained patch: **5/12**; deterministic repair: **0/12**
- Local calls: **226**; 7B **170/170** successful; 14B **34/56** successful with **22 timeouts**
- Average case time: **20.27 minutes**; process wall: **243.3 minutes**
- Premium calls and calls after final adjudication: **0**
- Receipt: `project_ws/AgentOps/FABLE5_CLASS_DIAGNOSIS_TO_FIX_BLINDED_ELEVENTH_RUN_RECEIPT.md`

This is decisive negative evidence against current replacement readiness. One transfer variant used a materially
shorter seven-call route, but the other two fell into the generic path; no case passed sealed final adjudication.
The next development phase must improve causal-family calibration, exact ownership, generic multi-file synthesis,
and hard-bounded specialist routing. A new untouched suite is required after those repairs.

### Tenth-Suite Contract-Guided Development Replay

- Frozen source commit: `5905f636ae6128f445fe88057188f40aa246fd32`
- Classification: disclosed development replay; not an untouched holdout despite the fixture's historical split label
- Local route: 7B primary, two 7B repairs, one bounded 14B escalation, zero premium calls
- Schema-v5 score: **58.12/100**, `needs_improvement`
- Sealed-final functional success: **2/8 (25%)**
- Correct diagnosis family: **6/8 (75%)**
- Exact changed-file set: **5/8 (62.5%)**
- Successful cases: TypeScript Vary/cache isolation and SQL tenant grant intervals
- Visible feedback passed but sealed final failed for the tenant-scoped reservation retry case
- Local calls: **168** total, including **39** 14B calls
- Total wall time: **6,334.5 seconds (105.6 minutes)**; average **791.8 seconds/case**
- Premium calls: **0**
- Receipt: `project_ws/AgentOps/tenth_development_replays_contract_guided_full_5905f63/DEVELOPMENT_REPLAY_RECEIPT.md`

The replay confirms that contract guidance materially improves causal-family recognition, but it does not improve
sealed functional success beyond the untouched tenth result. Two initially correct families were overwritten during
repair, one visible-green repair failed an unseen scope interaction, Dart compiler recovery failed, and three
correctly diagnosed/exact-owner cases still missed boundary semantics. The 7B route also escalated on seven cases
and remained too slow for an interactive Fable 5 replacement.

### Tenth-Suite Mechanical-Contract Development Replay

- Frozen source commit: `2675d4ef43c27d2e50697514f76a3ca5e0ee5ab1`
- Classification: disclosed development replay, not an untouched holdout
- Score: **100/100**, `shadow_ready`
- Sealed-final success, diagnosis family, and exact ownership: **8/8 each**
- Premium calls: **0**
- Local calls: **58**, down from 168; 14B calls: **9**, down from 39
- Case-time sum: **1,445.4 seconds (24.1 minutes)**, down 77.2% from 6,334.5 seconds
- Six cases used recognized mechanical operators with no generative repair rounds
- Vary and tenant-grant SQL remained generative, used 40/58 calls, and both escalated to 14B
- Broad focused validation before replay: **292 passed**
- Receipt: `project_ws/AgentOps/tenth_development_replays_mechanical_contracts_full_2675d4e/DEVELOPMENT_REPLAY_RECEIPT.md`

This is the strongest evidence that CHILI is a system rather than a premium-model wrapper: most repair work in the
run was performed by CHILI-owned taxonomy, source-shape operators, guards, rollback, and validators. It remains
development evidence. A fresh untouched transfer suite is required to determine whether the abstractions generalize
or merely cover the mechanisms that produced them.

### Tenth-Suite All-Mechanical Development Replay

- Frozen source commit: `6cf5b7e0e7da6840da57dec678f8846796265091`
- Classification: disclosed development replay
- Score, sealed-final success, diagnosis, and exact ownership: **100/100 and 8/8 each**
- Local calls: **24**, all 7B diagnostic stages
- Mechanical repairs: **8/8**; generative repair rounds and 14B calls: **0**
- Case-time sum: **605.2 seconds (10.1 minutes)**; average **75.7 seconds/case**
- Relative to `5905f63`: 85.7% fewer calls and 90.4% less case time
- Premium calls: **0**
- Broad focused validation before replay: **295 passed**
- Receipt: `project_ws/AgentOps/tenth_development_replays_all_mechanical_full_6cf5b7e/DEVELOPMENT_REPLAY_RECEIPT.md`

This result demonstrates the intended architecture: local models assist evidence-based diagnosis, while CHILI owns
the recognized repair algorithms, source scoping, compiler/adapter recovery, rollback, and proof. The suite is
fully disclosed, so the result measures regression mastery rather than Fable 5-class generalization.

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

### Authenticated Head-to-Head Collection Bridge

- The independently authored eighth diagnostic slice now has a deterministic oracle-free Fable 5 prompt pack with eight cases and SHA-256 `c279126fb23319a30f9b440645062a54bff98acaf8b6e88252b83a476b307eec`.
- Prompt generation reads manifest and public case inputs only; a sealed-term scan found zero oracle-path, primary-dimension, expected-decision/status, or safety-oracle leakage.
- The evaluator requires an exact prompt-bound and response-bound provider-native `claude-fable-5` transcript. It rejects another Claude family, an unrelated Fable event, an altered case set, invented evidence grounding, or a changed prompt hash.
- Reasoning quality is scored separately from premium cost, then compared case-by-case with the already sealed untouched CHILI eighth-slice result. Parity remains disabled until blind human adjudication and broader replication.
- A guarded Windows collector is inert without explicit `-Execute`, uses no fallback model, disables tools, runs in safe mode, verifies first-party authentication, and performs a no-write provenance/scoring preflight before publication.
- Preparation used **zero premium calls**. The one authenticated Fable 5 call remains pending explicit approval because it consumes the user's premium allowance.
- Readiness artifact: `project_ws/AgentOps/fable5_diagnostic_headtohead/COLLECTION_READY.md`

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

### Second Independent Post-Freeze Diagnostic Slice

- Frozen implementation: `e1bc5538c1cfac65ec992bed6be1d296b603acc4`
- Eight newly authored, non-trading operational incidents with 17 immutable case/oracle/manifest inputs
- All 17 hashes matched before and after the run; public observations carried no oracle dimensions
- Local model: `qwen2.5-coder:7b`; investigator, skeptic, and judge for every case; **24/24 calls completed**
- Untouched score: **87.5/100**; verdict: **needs_improvement**
- Untouched failures: four root-cause dimension checks
- Critical output finding: **0/24 usable model packets**; every response reached the 900-token cap, so the untouched score was deterministic-fallback-only
- Safety violations: **0**; premium calls: **0**
- Average untouched call latency: **61.2 seconds**; maximum: **66.0 seconds**
- Reports: `project_ws/AgentOps/FABLE5_CLASS_DIAGNOSTIC_BLINDED_SECOND_RUN.md`, `project_ws/AgentOps/FABLE5_CLASS_DIAGNOSTIC_BLINDED_SECOND_RUN_RECEIPT.md`, and `project_ws/AgentOps/fable5_class_diagnostic_blinded_second_run.json`

After preserving the untouched result, generic token-boundary matching, evidence polarity, discriminating-proof ranking, compact JSON contracts, fail-closed packet repair, probe-evidence retention, and a model-output promotion gate were added. The full disclosed development replay reached **99.38/100**, with **24/24 successful calls**, **21/24 accepted packets**, at least one accepted packet in **8/8 cases**, and average latency reduced to **15.7 seconds/call**. A final targeted breadth repair scored **100/100** with the promotion gate passing. These are development results and do not rewrite the untouched 87.5/100 score.

### Third Independent Post-Freeze Diagnostic Slice

- Frozen implementation: `851f14119f17703f4c6f7f07430b023c612f4036`
- Eight newly authored non-trading incidents covering all eight diagnostic dimensions, including two deliberately unresolved instrument-first cases
- All 17 case/oracle/manifest hashes matched before and after the run; tracked source diff remained empty
- Local model: `qwen2.5-coder:7b`; investigator, skeptic, and judge for every case; **24/24 calls completed**
- Accepted model stages: **22/24**; every case had at least one accepted stage; promotion gate passed
- Untouched score: **76.25/100**; verdict: **needs_improvement**
- Untouched failures: three causal-family selections plus decision/status over-attribution on both unresolved cases
- Safety violations: **0**; premium calls: **0**
- Average local-call latency: **21.33 seconds**; maximum: **27.89 seconds**
- Reports: `project_ws/AgentOps/FABLE5_CLASS_DIAGNOSTIC_BLINDED_THIRD_RUN.md`, `project_ws/AgentOps/FABLE5_CLASS_DIAGNOSTIC_BLINDED_THIRD_RUN_RECEIPT.md`, and `project_ws/AgentOps/fable5_class_diagnostic_blinded_third_run.json`

This third result is stronger evidence against a current broad parity claim: unlike the second slice, model packets were structurally usable, but causal-family calibration and uncertainty behavior still failed. Any repair replay is development evidence and cannot replace the untouched 76.25/100 score.

After preserving the untouched result, commit `3a45c82af1e93dac1731ab67c2635d9138dc3f00` added generic causal-polarity, edge-break, dimension-origin, causal-ownership, unresolved-attribution, evidence-completion, and confidence-calibration repairs. The exact-commit full disclosed replay scored **95.0/100**, with **24/24 successful calls**, **24/24 accepted stages**, correct uncertainty behavior on both unresolved cases, and zero premium calls. Seven cases were correctly calibrated; the remaining dependency case was mislabeled as code, and its local judge requested two unsafe automatic experiments that CHILI demoted to non-executable plans. The benchmark intentionally retained that safety failure. Focused validation passed **60 tests** and the broad autonomy slice passed **204 tests**. Full details: `project_ws/AgentOps/FABLE5_CLASS_DIAGNOSTIC_CAUSAL_CALIBRATION_REPAIR.md`.

The disclosed repair replay is not unseen evidence. Its 95.0 score does not overwrite the untouched 76.25 score and does not establish Fable 5 parity.

### Fourth Independent Post-Freeze Diagnostic Slice

- Frozen implementation: `b8616f6273480ca892c229d74021a6ef0c3c411f`
- Evaluation/fixture commit: `d58e880806a1d62ff3df54b6baa162e78dc789b5`
- Eight independently authored non-trading incidents covering all eight diagnostic dimensions; six confirmed cases and two calibrated-uncertainty cases
- All 17 final case/oracle/manifest bytes matched the staged Git blobs before the first call and remained unchanged after the run; implementation source diff was zero
- Local model: `qwen2.5-coder:7b`; investigator, skeptic, and judge for every case; **24/24 calls successful** and **24/24 stages accepted**
- Untouched score: **69.38/100**; verdict: **needs_improvement**
- Untouched failures: three wrong causal families, three under-confident confirmed cases, one uncertainty-status miss, one baseline-drift miss, and one hypothesis-breadth miss
- Unsafe final automatic experiments: **0**; premium calls: **0**
- Average local-call latency: **22.65 seconds**; maximum: **31.68 seconds**
- Reports: `project_ws/AgentOps/FABLE5_CLASS_DIAGNOSTIC_BLINDED_FOURTH_RUN.md`, `project_ws/AgentOps/FABLE5_CLASS_DIAGNOSTIC_BLINDED_FOURTH_RUN_RECEIPT.md`, and `project_ws/AgentOps/fable5_class_diagnostic_blinded_fourth_run.json`

This fourth result is the strongest evidence against a current broad parity claim. The inventory now contains **32 independently authored diagnostic cases**, clearing the numeric case-count target, but every untouched slice remains below 90 and the newest slice fell to 69.38 despite fully usable local-model output.

After preserving the untouched result, commit `8628ddea503814dced347792aaf1c56c0d67c243` added generic proof-precedence, mechanism-vocabulary, ambiguous-experiment, decisive-attribution, baseline-comparability, and minimum-breadth repairs. The disclosed fourth-slice heuristic replay and full 24-call council replay both reached **100/100**; the council had **24/24 successful calls**, **24/24 accepted stages**, zero unsafe automatic experiments, and zero premium calls. Focused validation passed **64 tests** and the broad autonomy slice passed **208 tests**. Full details: `project_ws/AgentOps/FABLE5_CLASS_DIAGNOSTIC_FOURTH_SLICE_REPAIR.md`.

These are development results. They do not overwrite the untouched 69.38 score and cannot support a parity claim until a new post-freeze slice reproduces the improvement.

### Fifth Independent Post-Freeze Diagnostic Slice

- Frozen implementation: `8628ddea503814dced347792aaf1c56c0d67c243`
- Evaluation/fixture commit: `2d75f05a0c9e70538d249f99d07a4ef10dc9fb52`
- Eight independently authored non-trading incidents covering all eight diagnostic dimensions; six confirmed cases and two calibrated-uncertainty cases
- All 17 case/oracle/manifest files exactly matched their frozen Git blobs after the run; implementation source diff was zero
- Local model: `qwen2.5-coder:7b`; investigator, skeptic, and judge for every case; **24/24 calls successful** and **24/24 stages accepted**
- Untouched score: **74.4/100**; verdict: **needs_improvement**
- Untouched failures: five wrong causal families, baseline drift missed in all five applicable cases, and one confirmed clock cause under-calibrated as provisional
- Every grounding, safety, premium-independence, and hypothesis-breadth check passed
- Unsafe final automatic experiments: **0**; premium calls: **0**
- Average local-call latency: **52.8 seconds**; maximum: **64.8 seconds**; valid retry wall time: **1,274.8 seconds**
- Reports: `project_ws/AgentOps/FABLE5_CLASS_DIAGNOSTIC_BLINDED_FIFTH_RUN.md`, `project_ws/AgentOps/FABLE5_CLASS_DIAGNOSTIC_BLINDED_FIFTH_RUN_RECEIPT.md`, and `project_ws/AgentOps/fable5_class_diagnostic_blinded_fifth_run.json`

The initial invocation was interrupted by an outer process timeout and produced no result artifact. The exact
frozen run then completed under a longer wrapper allowance. This adds a separate operational gap: long local
council runs need atomic per-case checkpoints and resumability. The valid score remains negative evidence. The
fourth-slice repair generalized only partially to fresh incidents, so its disclosed 100/100 result cannot support
a broad parity claim.

After preserving the untouched result, commit `aa2821db9c67444bb6d3ce5cc63c71bdbfe1756c` added generic
changed-variable attribution, bounded structured evidence, semantic baseline comparison, ambiguous-control,
known-family recovery, endpoint disambiguation, and atomic per-case checkpoint repairs. The disclosed fifth-slice
heuristic replay and full 24-call council replay both reached **100/100**. The council completed **24/24 calls**,
accepted **24/24 packets**, retained five model-selected conclusions, used stronger deterministic evidence for
three conclusions, produced no unsafe final action, and made zero premium calls. The broad autonomy validation
slice passed **221 tests**. Full details: `project_ws/AgentOps/FABLE5_CLASS_DIAGNOSTIC_FIFTH_SLICE_REPAIR.md`.

These are disclosed development results. They do not overwrite the untouched 74.4 score. The independently
authored sixth slice below tests whether the repair generalizes.

### Sixth Independent Post-Freeze Diagnostic Slice

- Frozen implementation: `aa2821db9c67444bb6d3ce5cc63c71bdbfe1756c`
- Evaluation/fixture commit: `90e88dbd1eeca7612ba9e9db2949e19d5a18bae8`
- Eight independently authored non-trading incidents covering all eight diagnostic dimensions; six confirmed cases and two calibrated-uncertainty cases
- All 17 case/oracle/manifest files exactly matched the isolated author output and committed Git blobs before and after the run; implementation/runner source diff was zero
- Local model: `qwen2.5-coder:7b`; investigator, skeptic, and judge for every case; **24/24 calls successful** and **24/24 stages accepted**
- Untouched score: **67.5/100**; verdict: **needs_improvement**
- Untouched failures: four wrong causal families, all four expected baseline-drift findings missed, both uncertainty cases over-confirmed, and two confirmed causes under-confirmed
- Only one case scored 100; one scored 90; the remaining six scored 75 or below
- Unsafe final automatic experiments: **0**; premium calls: **0**
- Average local-call latency: **50.9 seconds**; maximum: **66.5 seconds**; wall time: **1,228.1 seconds**
- Durable checkpoint completed and cleaned up without altering case independence
- Reports: `project_ws/AgentOps/FABLE5_CLASS_DIAGNOSTIC_BLINDED_SIXTH_RUN.md`, `project_ws/AgentOps/FABLE5_CLASS_DIAGNOSTIC_BLINDED_SIXTH_RUN_RECEIPT.md`, and `project_ws/AgentOps/fable5_class_diagnostic_blinded_sixth_run.json`

This is the strongest current evidence against broad Fable 5-class generalization. The fifth repair reached
100/100 only after those cases were disclosed, then fell to 67.5 on the next untouched slice. The system remains
safe, local-only, and structurally reliable, but causal-family transfer, baseline semantics, and uncertainty
calibration are not yet robust across novel incident language.

After preserving that untouched result, commit `43f6d3e60725370eacfea968046325b1550bbca9` added generic
operational-fingerprint classification, retained-baseline semantics, event-level comparability and mechanism-gap
controls, coarse-reset confidence limits, and unresolved-drift harness calibration. The disclosed heuristic replay
reached **100/100**. The full 24-call local-council replay reached **92.5/100**, with **24/24 successful and
accepted stages**, five model-selected conclusions, three deterministic evidence-gate selections, no unsafe final
automatic experiment, and zero premium calls. Two causal-boundary errors remained: `config` was labeled `code`,
and `runtime` was labeled `state`. The confirmed `code` misattribution also failed the fixture's
`forbid_confirmed_code` safety contract, so only **7/8 safety checks** passed. Focused validation passed **76
tests** and the broad autonomy slice passed **226 tests**. Full details:
`project_ws/AgentOps/FABLE5_CLASS_DIAGNOSTIC_SIXTH_SLICE_REPAIR.md`.

This repaired replay is disclosed development evidence. It does not overwrite the untouched 67.5 score, does not
show post-repair generalization, and does not support a Fable 5 parity claim. The seventh independently authored
post-repair slice below is the next diagnostic gate.

### Seventh Independent Post-Freeze Diagnostic Slice

- Frozen implementation: `43f6d3e60725370eacfea968046325b1550bbca9`
- Evaluation/fixture commit: `fb627cb36d806ca5195332c2cf8df547215ea513`
- Eight independently authored non-trading incidents covering all eight diagnostic dimensions; six confirmed cases and two calibrated-uncertainty cases
- A separate context-isolated validator passed structural, blinding, safety, and semantic-fairness checks before the first model call
- All 17 case/oracle/manifest files exactly matched the isolated author output, staged and committed blobs, and post-run worktree; implementation/runner source diff was zero
- Local model: `qwen2.5-coder:7b`; investigator, skeptic, and judge for every case; **24/24 calls successful** and **24/24 stages accepted**
- Untouched score: **63.8/100**; verdict: **needs_improvement**
- Untouched failures: six wrong causal families, all four expected baseline-drift findings missed, three wrong decisions, and four wrong statuses
- No case scored 100; four final conclusions were model-selected and four came from deterministic evidence-gate hypotheses
- Final safety checks: **8/8 passed**, but the contract gate had to demote two unsafe automatic experiments requested by the local judge
- Premium calls: **0**
- Average local-call latency: **71.3 seconds**; maximum: **119.0 seconds**; wall time: **1,717.3 seconds**
- Durable checkpoint completed and cleaned up without altering case independence
- Reports: `project_ws/AgentOps/FABLE5_CLASS_DIAGNOSTIC_BLINDED_SEVENTH_RUN.md`, `project_ws/AgentOps/FABLE5_CLASS_DIAGNOSTIC_BLINDED_SEVENTH_RUN_RECEIPT.md`, and `project_ws/AgentOps/fable5_class_diagnostic_blinded_seventh_run.json`

This is now the strongest evidence against broad Fable 5-class generalization. The sixth repair improved its
disclosed cases to 92.5, but the next untouched slice fell to 63.8 with fully usable model packets. The system's
safety shell remains valuable, yet causal-family transfer, baseline semantics, and confidence/decision
calibration are still materially below the requested replacement standard.

A local `qwen3:8b` Q4_K_M challenger was then evaluated on the disclosed seventh slice. Generic top-level Ollama
thinking control was required: model-default thinking caused two 300-second smoke timeouts, while explicit
`think=false` completed the valid smoke in 25-37 seconds per call. The full no-think challenger accepted **22/24
stages**, averaged **35.8 seconds/call**, and finished in **864.8 seconds**, but scored only **61.9/100**. A
heuristic-only replay also scored exactly **61.9/100**. The larger local model therefore improved throughput, not
causal quality; it was not promoted. Full details:
`project_ws/AgentOps/FABLE5_CLASS_DIAGNOSTIC_SEVENTH_MODEL_CHALLENGER.md`.

After preserving the untouched result, commits `a637bfbd8c412bc8c4ee495c408d8556e785b18d`, `ddbceba`, and
`6f64b4fea317c5ab41d739331e867d2a93ebc402` added generic causal-owner separation, evidence lifecycle and
intervention scope, retained-comparison semantics, evidence-derived status, qualified contradiction filtering,
cross-stage causal-support retention, grounded-family preservation across changed model IDs, and monotonic
evidence ranking. The disclosed heuristic replay reached **100/100**. Three complete local-council reruns scored
**98.75**, **95.62**, and finally **100/100**; all made 24/24 successful local calls, used zero premium calls, and
ended with zero unsafe automatic experiments. The final run accepted **24/24** stages, passed **8/8** safety
checks, averaged **34.4 seconds/call**, peaked at **50.1 seconds**, and completed in **831.4 seconds**. Broad
validation passed **234 tests**. Full details and all three raw artifacts are retained in
`project_ws/AgentOps/FABLE5_CLASS_DIAGNOSTIC_SEVENTH_SLICE_REPAIR.md`.

These disclosed repair results do not overwrite the untouched 63.8 score. The 98.75-to-95.62 variance also
shows that one perfect replay is insufficient for a replacement claim. A new post-repair untouched slice is the
next authoritative diagnostic gate.

### Eighth Independent Post-Freeze Diagnostic Slice

- Frozen implementation: `1bbcf216ee758fe78ce67a5c2050030860daa5e8`
- Fixture-only commit: `2df59bee2506b0cebb47147e1feb4206195691a6`
- Runner-manifest adapter commit: `5dad7cc59926c888042fe3bdcd4b0e48ee0df7e4`
- Eight context-isolated, independently authored non-trading incidents with 78 unique public observations; one primary family per diagnostic dimension, six confirmed cases, and two calibrated-uncertainty cases
- Author process used `fork_context=false`, a separate external directory, and explicit no-CHILI/no-history/no-prior-output restrictions; all public observation dimensions were `unknown`
- An independent fixture-only validator returned semantic PASS with nonfatal adjacent-family ambiguities. It separately noted that artifact contents cannot prove process isolation; the orchestration record supplies that evidence
- The initial runner preflight made zero model calls and failed on author-supplied manifest key names. A manifest-only adapter changed no case or oracle bytes; all 16 case/oracle hashes remained exact through scoring
- Local model: `qwen2.5-coder:7b`; **24/24 calls successful**, **24/24 stages accepted**, zero premium calls, and **8/8 final safety checks passed**
- Frozen-oracle untouched score: **83.12/100**; verdict: `needs_improvement`
- The oracle allowed five adjacent dimensions per case. A post-seal strict audit against `primary_causal_dimension` scored **70.62/100**, with only **3/8** primary families correct
- Author-intended failures: five primary families, all four true drift findings, two decisions, and three statuses
- Average local-call latency: **33.5 seconds**; maximum: **46.5 seconds**; wall time: **809.5 seconds**
- Reports: `project_ws/AgentOps/FABLE5_CLASS_DIAGNOSTIC_BLINDED_EIGHTH_RUN.md`, `project_ws/AgentOps/FABLE5_CLASS_DIAGNOSTIC_BLINDED_EIGHTH_RUN_RECEIPT.md`, and `project_ws/AgentOps/fable5_class_diagnostic_blinded_eighth_run.json`

This fresh result is negative evidence against current Fable 5-class generalization. The nominal score recovered
from the seventh untouched 63.8 but remained below 90, while strict primary-family scoring exposed transfer
failures hidden by a permissive oracle. The disclosed seventh 100 therefore did not generalize to independently
authored causal language.

### Twelfth Disclosed Transfer Repair

After preserving the untouched twelfth result, the first generic replay regressed to **32.92/100, 0/12 finals,
5/12 diagnosis families, and 0/12 exact owner sets**. That failure exposed over-strict fake prompt-contract IDs,
a brittle 7B JSON bundle, and remaining ownership, SQL-validation, causal-accounting, and edit-authority gaps.

The corrected disclosed development replay reached **100/100** with **12/12 sealed-final solves, 12/12 diagnosis
families, 12/12 causally accepted conclusions, 12/12 exact owners, zero premium calls, 24 local diagnostic calls,
and 7.7 minutes wall time**. Broad affected validation passed **277 tests**. The final result is explicitly labeled
`disclosed_replay_passed` with zero blinded cases. Full details:
`project_ws/AgentOps/FABLE5_CLASS_TWELFTH_DISCLOSED_TRANSFER_REPAIR.md`.

This does not overwrite the untouched 49.17 score or prove Fable 5 parity. It proves disclosed mechanism capture;
the next independently authored untouched suite is the transfer gate.

### Thirteenth Through Sixteenth Diagnosis-To-Fix Evidence

Three later frozen, independently authored suites remained decisively below the requested replacement bar:

- Thirteenth untouched: **40.83/100**, **2/12** sealed-final solves, **3/12** correct diagnosis families, **3/12** exact owner sets, and zero premium calls.
- Fourteenth untouched: **27.92/100**, **0/12** sealed-final solves, **1/12** correct diagnosis families, **1/12** exact owner sets, and zero premium calls.
- Fifteenth untouched: **41.88/100**, **1/8** sealed-final solves, **2/8** correct diagnosis families, **4/8** exact owner sets, **78/78** successful local calls, and zero premium calls.

The fifteenth failures disclosed seven new cross-language mechanism families plus obligation-polarity, owner-plan,
partial-patch, SQLite-validation, and local reasoning-exhaustion gaps. After those cases were disclosed, the full
development replay reached **100/100 with 8/8 final solves**, and a targeted two-case recovery replay reached
**100/100 with 4/4 successful local calls and fully qualified live reasoning**. Those are useful regression
results, not unseen transfer evidence, and they do not replace the untouched 41.88 score.

The independently authored sixteenth suite passed model-independent fixture validation, but its first full run
hit the one-hour outer process limit before producing a report, result, or checkpoint. It therefore has **no valid
score** and cannot be counted as a failed or successful evaluation. The incident exposed that the diagnosis-to-fix
runner's prior durability claim was not implemented. Commit `19de897466158027d65742f06d5e562e4597d2f1`
added atomic per-case checkpoints bound to the exact fixture inventory, ordered cases, models, budgets, run policy,
implementation commit, and audited source hashes. Resume rejects digest, binding, order, sealing, post-final-call,
premium-call, malformed-payload, and output-path collisions. A real two-case interruption/resume integration test
proved that the completed sealed case is not sent to the model again; the relevant suite passed **301 tests**.

The authoritative current unseen diagnosis-to-fix result remains the fifteenth **41.88/100**. A fresh post-fix
holdout is required before making any new readiness statement.

### Seventeenth Post-Checkpoint Holdout

The fresh seventeenth run completed all eight independently authored sealed cases in **3,906 seconds** under the
frozen `qwen3:8b` reasoner and `qwen2.5-coder:7b` editor policy. It used zero premium calls, committed all eight
cases through the new atomic checkpoint path, made no post-final model calls, reverified the policy digest, and
cleaned up the checkpoint only after writing both outputs.

The recorded result was **27.5/100**, **0/8 final repairs**, **2/8 exact owner sets**, **6/8 live-reasoning-qualified
cases**, and **14 errors/timeouts across 57 local calls**. This is strong negative evidence against current Fable
5-class replacement readiness.

Post-seal review found a fixture schema defect: the repair oracles used plural mechanism labels in
`expected_dimensions`, while the v6 scorer requires one canonical singular `expected_dimension`. The reported
0/8 diagnosis matches are therefore not interpretable. Even granting the maximum 15 diagnosis points to every
case yields only a **42.5/100 upper bound**, so the negative functional verdict is unchanged. The fifteenth
41.88/100 remains the latest fully interpretable unseen composite score; the seventeenth contributes valid 0/8
functional evidence and a requirement for fail-closed oracle-schema validation. Full details:
`project_ws/AgentOps/FABLE5_CLASS_DIAGNOSIS_TO_FIX_BLINDED_SEVENTEENTH_RESULT_RECEIPT.md`.

### Seventeenth Disclosed SQL A/B

After the seventeenth result was sealed, `sqlite_effective_price_intervals` became a disclosed development case.
The first replay on the generic recovery engine completed all **8/8 local calls successfully** but still scored
**25/100** and retained no patch. It selected the correct two owners, then generated incorrect interval algebra:
an exclusive lower point-lookup bound, a reversed overlap predicate, and no UPDATE overlap guard.

Commit `99122e485505b89dd3c887082917a877ff8bd877` added a source-shape-gated half-open effective-history contract:
adjacency is legal, overlap uses both strict cross-bounds, point lookup is lower-inclusive/upper-exclusive, and
INSERT/UPDATE share the guard while UPDATE excludes itself. The operator fails closed on unrelated INSERT
triggers and transfers to alternate table/column names. The exact-source replay then scored **100/100** with the
correct `data` family, exact two-file ownership, public/feedback/sealed-final success, **2/2 local diagnostic
calls**, no planning/edit/repair model calls, **88.3 seconds**, and zero premium calls. Broad affected validation
passed **348 tests**.

This A/B proves disclosed mechanism capture and a useful premium-independent symbolic reasoning path. It does not
change the untouched seventeenth 0/8 functional result, does not establish unknown-mechanism transfer, and does
not support a Fable 5 parity claim. Full details:
`project_ws/AgentOps/FABLE5_CLASS_DIAGNOSIS_TO_FIX_DISCLOSED_SEVENTEENTH_SQL_AB_RECEIPT.md`.

### Seventeenth Disclosed Node Shared-State A/B

The disclosed `node_coalesced_abort_poison` case exposed two different gaps. The repair system had no generic
subscriber-lifetime/result-retention operator, and the local diagnosis packet ranked constructor and key-policy
snippets above the direct abort and unresolved-promise cache lines. A first post-repair replay solved the fixture
functionally but the raw local reasoner selected a key-normalization distractor.

Commits `56203b45`, `fa0cd8ff`, and `fe89a111` added a source-shape-gated shared-work repair, contract-aware
behavioral evidence ranking, and bounded static contract/source mismatch context. Commit `3c69c2c4` separately
tightened the honesty gate: successful model calls no longer qualify as Fable 5-class live reasoning unless the
model conclusion is confirmed, causally sufficient, and consistent with the retained family.

The final disclosed replay scored **100/100** with exact two-file ownership, public/feedback/fresh-final success,
**2/2 successful local calls**, no model errors, **110.9 seconds**, and zero premium calls. The raw reasoner moved
to the correct `state` family and surfaced both unresolved-promise retention and unisolated abort propagation, but
selected only the cache half as its inconclusive leader. Consequently live-reasoning qualification was **0/1**,
claim eligibility was false, and the honest verdict remained `needs_improvement`. Full affected validation passed
**314 tests**.

This is stronger system-level evidence than a model wrapper: deterministic evidence extraction, causal gates,
symbolic repair, rollback, and sealed validation all contributed without a premium call. It is still disclosed
mechanism capture, not unknown-mechanism transfer or Fable 5 parity. Full details:
`project_ws/AgentOps/FABLE5_CLASS_DIAGNOSIS_TO_FIX_DISCLOSED_SEVENTEENTH_NODE_AB_RECEIPT.md`.

### Historical Fable 5 Trading Pilot

To test the user's intended replacement workflow directly, a post-source-freeze fixture reconstructed the
direction-loss mechanism from historical commit `a9e5ea2b`, `Preserve short direction in paper auto entry`. The
user identifies the source conversation as Fable 5 work; the commit is therefore user-attested historical
reference evidence, not provider-authenticated same-task output. CHILI source was frozen at `ae5c0fb8` before the
self-contained fixture was authored and validated. The fixture author was the current agent, so this is weaker than
the independent-author promotion gate but still a legitimate first-run post-freeze transfer check.

The untouched protocol run scored **25/100**, solved **0/1** sealed finals, retained no patch, and used zero premium
calls. It planned the correct two owners but retained `code` instead of the expected `data` family, inverted short
protective-stop polarity, damaged already-correct validation, and globally reversed risk distance instead of making
it side-aware. Feedback led the next plan to the right missing `direction` parameter and formulas, but the editor
inserted undefined, out-of-function code. Compiler/public-regression recovery failed safely, rollback preserved the
public long behavior, and the final repair-plan call exhausted the case budget. The run made **11 local calls**,
had one case-budget timeout, and took **491.8 seconds**.

This result directly rejects the claim that current CHILI can already reproduce representative Fable 5-era trading
fixes. It also pinpoints the next capability work: directional-polarity contracts, normalized cross-owner value
propagation, AST-aware Python repair, and faster retained-progress refinement. Full details:
`project_ws/AgentOps/FABLE5_HISTORICAL_TRADING_PILOT_UNTOUCHED_RECEIPT.md`.

After disclosure, commit `fb96519b` added a generic direction contract and AST-positioned repair that transfers to
alternate names and the real historical `EmitterSignal`/`open_paper_trade` shape. The first recovery replay reached
**100/100**, but two of three local diagnosis calls timed out and wall time remained **242.5 seconds**. Commit
`3e0a8204` then added a conservative deterministic diagnosis fast path for source-structurally proven operators.
The final recovery retained **100/100** with exact owners and all public/feedback/fresh-final tests passing in
**5.1 seconds**, with **zero model calls and zero premium calls**. Full affected validation passed **319 tests**.

Both recovery results remain disclosed development evidence. The fast result is deliberately
`deterministic_only`, has no live-reasoning credit, and keeps the `needs_improvement` verdict. It proves a useful
non-wrapper system capability for a recognized family; it does not replace the untouched 25/100 or prove transfer
to the next Fable 5-era trading mechanism. Full details:
`project_ws/AgentOps/FABLE5_HISTORICAL_TRADING_PILOT_DISCLOSED_RECOVERY_RECEIPT.md`.

### Historical Fable 5 Mesh Pressure Pilot

A second post-source-freeze fixture reconstructed historical commit `d5ee0e92`, `fix: cap mesh teacher under
queue pressure`. This case is disjoint from short-direction propagation: it requires effective settings ownership,
live queue-pressure observation, optional teacher-work admission, bounded daily cost, zero-value overrides,
fail-open telemetry, and continued mechanical aggregation across four stages. CHILI source was frozen at
`b25c1058`, the current agent authored and validated the fixture, and fixture-only commit `ccd03ba9` sealed it
before scoring. The historical Fable 5 attribution is user-attested, not provider-authenticated same-task output.

The untouched protocol run scored **40/100**, solved **0/1** sealed finals, retained no patch, made **13 local
calls** with two timeouts, took **488.1 seconds**, and used zero premium calls. It selected the correct `config`
family but retained only an inconclusive statement about the zero daily cap. The first plan chose the queue
repository instead of settings as the second owner; the editor nested pressure handling under the wrong branch,
passed settings as the queue database object, omitted imports, and duplicated provider behavior. Feedback produced
the correct two-owner plan, but the next edit hallucinated `def Settings()` instead of the actual dataclass. Atomic
validation rejected both groups and preserved public behavior.

This second negative result shows that the recovered direction operator did not create broad Fable 5-class
diagnostic transfer. The next architecture work is a generic bounded-optional-work contract, behavior-level
settings/consumer/provider ownership, AST-grounded dataclass and consumer repair, and a fast source-proven path that
still preserves zero overrides and fail-open telemetry. Full details:
`project_ws/AgentOps/FABLE5_HISTORICAL_TRADING_MESH_PILOT_UNTOUCHED_RECEIPT.md`.

After disclosure, commit `42ed0615` added that generic capability. The implementation recognizes settings,
queue-state provider, and optional-work consumer as distinct structural roles; modifies only settings and the
consumer; supports dataclass and Pydantic policy shapes plus single-call and multi-stage teacher gates; and fails
closed on ambiguous providers. It preserves the daily cap, exact pressure boundary, zero-value overrides,
absent/failing telemetry behavior, and mechanical state continuity. The operator also produced valid exact-owner
repairs against the complete historical parent sources in a read-only probe.

The disclosed recovery scored **100/100** in **5.2 seconds**, selected the exact two owners, passed all public,
feedback, and fresh-final checks, and used **zero model calls and zero premium calls**. Full affected validation
passed **325 tests**. This is strong non-wrapper system evidence for a recognized family, but it is deliberately
`deterministic_only`, earns no live-reasoning credit, and does not replace the untouched 40/100. Full details:
`project_ws/AgentOps/FABLE5_HISTORICAL_TRADING_MESH_PILOT_DISCLOSED_RECOVERY_RECEIPT.md`.

### Historical Fable 5 Queue-Priority Pilot

A third post-source-freeze fixture reconstructed historical commit `b04ade06`, `fix: protect mesh refresh under
queue pressure`. This mechanism is disjoint from direction propagation and teacher admission. It requires one
auditable state replacement only at exact fixed capacity, with protected incoming and sheddable existing cause
allowlists, a 30-minute age boundary, oldest/id ordering, locked-row exclusion, correlation rejection before
mutation, over-cap rejection, and timezone-safe lifecycle metadata. CHILI source was frozen at `9d466377`; the
fixture-only commit was `0cd0d05b`.

The untouched protocol run scored **40/100**, solved **0/1** sealed finals, retained no patch, made **9 local calls**
with three timeouts, took **491.2 seconds**, and used zero premium calls. It chose the correct `state` family and
repository owner, but never established a causal conclusion. More seriously, `brain_market_snapshots` plus audit
language falsely activated an unrelated immutable-request-snapshot invariant. The first edit shed any old pending
cause for any incoming cause, used incompatible datetime forms, wrote an undefined audit field, and mutated before
the correlation gate. Feedback repairs introduced different control-flow regressions, and rollback preserved the
public contract.

This result adds two concrete architecture gaps: semantic contract activation must reject identifier-substring
collisions, and queue-priority repair needs a source-guarded state-transition operator that treats all eligibility
and ordering clauses as one indivisible boundary. Full details:
`project_ws/AgentOps/FABLE5_HISTORICAL_TRADING_QUEUE_PRIORITY_PILOT_UNTOUCHED_RECEIPT.md`.

After disclosure, commit `54043ffd` corrected both gaps. Natural request-policy reload semantics remain recognized,
while underscore-delimited `brain_market_snapshots` no longer activates the unrelated snapshot family. The new
state operator extracts causes and age from the incident, recognizes one queue owner, handles both in-memory and
SQLAlchemy `SKIP LOCKED` sources, enforces correlation-first and exact-cap ordering, and fails closed on ambiguous
or partial shapes. A read-only full historical-source probe selected only the repository owner and produced valid
Python with all warnings closed.

The disclosed recovery scored **100/100** in **4.9 seconds**, selected the exact owner, passed all public, feedback,
and fresh-final checks, and used **zero model calls and zero premium calls**. Full affected validation passed
**331 tests**. As with the other recoveries, it is `deterministic_only`, earns no live-reasoning credit, and does not
replace the untouched 40/100. Full details:
`project_ws/AgentOps/FABLE5_HISTORICAL_TRADING_QUEUE_PRIORITY_PILOT_DISCLOSED_RECOVERY_RECEIPT.md`.

### Historical Fable 5 Candidate-Scope Pilot

A fourth post-source-freeze fixture reconstructed historical commit `b7afb8f3`, `fix: split autotrader candidate
scope lanes`. Runtime evidence behind that change showed seven explicit-user alerts versus roughly 98,000 system
NULL alerts; one mixed OR selector made the narrow explicit-user path traverse the broad system query shape. The
case requires two locally bounded scope-pure reads, one global merge/order/limit, identity deduplication, and no
downstream gate changes. CHILI source was frozen at `33a4038c`; fixture-only commit `1317c7b0` sealed the case.

The untouched protocol run scored **40/100**, solved **0/1** sealed finals, retained no patch, made **10 local calls**
with two timeouts, took **489.4 seconds**, and used zero premium calls. The reasoner named most of the mechanism and
kept the correct `data` family, but its first plan assigned orchestration to the query provider. Feedback moved to
the correct owner, yet the edit removed the zero-limit guard, concatenated without dedupe, ignored id-first mode,
and negated a datetime. A timestamp-only correction remained incomplete, public validation stayed red, and rollback
preserved the original selector.

This adds a distinct capability gap: cross-boundary ownership must separate query execution from selection policy,
and split-lane repairs must synthesize local limits plus a mode-aware global merge as one contract. Full details:
`project_ws/AgentOps/FABLE5_HISTORICAL_TRADING_SCOPE_LANE_PILOT_UNTOUCHED_RECEIPT.md`.

Generic remediation added a scope-asymmetric query invariant, selector-versus-provider ownership analysis, guarded
repairs for both small provider-call selectors and the historical SQLAlchemy shape, per-lane capacity, identity
deduplication, timezone-safe mode-aware ordering, zero-limit preservation, and fail-closed ambiguity checks. A
read-only probe against the complete historical parent selected only `app/services/trading/auto_trader.py`, left the
provider unchanged, compiled the result, and closed all warnings.

The disclosed recovery scored **100/100** in **5.7 seconds**, selected the exact owner, passed all public, feedback,
and fresh-final checks, and used **zero model calls and zero premium calls**. Full affected validation passed
**336 tests**. It is `deterministic_only`, earns no live-reasoning credit, and does not replace the untouched 40/100.
Full details:
`project_ws/AgentOps/FABLE5_HISTORICAL_TRADING_SCOPE_LANE_PILOT_DISCLOSED_RECOVERY_RECEIPT.md`.

A post-remediation live-reasoning ablation then disabled every recognized-contract diagnosis and repair path. It
still scored **40/100**, solved **0/1** sealed finals, selected `trading/query_store.py` instead of the caller-owned
`trading/auto_trader.py`, retained no patch, used **8 local calls** with one timeout, and took **486.7 seconds**.
Compared with the untouched run, call count fell from 10 to 8 and errors from two to one, but correctness and latency
did not materially improve. The explicit ownership packet made the error visible; it did not resolve caller-versus-
callee policy ownership. Full details:
`project_ws/AgentOps/FABLE5_HISTORICAL_TRADING_SCOPE_LANE_PILOT_LIVE_REASONING_ABLATION_RECEIPT.md`.

The first caller/callee graph remediation correctly distinguished policy callers from execution primitives in its
bounded unit contracts, but exposing the full graph to each model stage regressed the disclosed replay to
**25/100**. Five of seven model calls timed out, the fallback selected `clock`, and no patch survived. This negative
result requires a compact deterministic owner hint rather than repeated verbose graph serialization. Full details:
`project_ws/AgentOps/FABLE5_HISTORICAL_TRADING_SCOPE_LANE_PILOT_CALLER_CALLEE_ATTEMPT_RECEIPT.md`.

The compact-owner attempt still scored **25/100** and solved **0/1**, but it corrected the substantive ownership
error: fallback, planner, and editor all chose `trading/auto_trader.py` and kept `trading/query_store.py` as context.
The first patch issued separate locally bounded `user` and `system` calls, then failed to synthesize identity dedupe,
global mode-aware ordering, and the final cap. Four diagnosis timeouts consumed 300 seconds, so feedback repair
could not complete. Full details:
`project_ws/AgentOps/FABLE5_HISTORICAL_TRADING_SCOPE_LANE_PILOT_COMPACT_OWNER_ATTEMPT_RECEIPT.md`.

The budget-reserved attempt returned to **40/100**, retained the correct `auto_trader.py` owner, and reduced errors
to one, but still solved **0/1**. Its initial patch split capacity between lanes and omitted global ordering. A later
unanimous repair plan was rejected without editing because generic failed-test binding only accepted drafts with
exactly one contract entry. This identified a harness convergence restriction rather than a new ownership failure.
Full details:
`project_ws/AgentOps/FABLE5_HISTORICAL_TRADING_SCOPE_LANE_PILOT_BUDGET_RESERVED_ATTEMPT_RECEIPT.md`.

After that generic restriction was removed, the contract-bound attempt still scored **40/100** and solved **0/1**.
The new canonicalizer correctly rejected a split-owner draft, then accepted a second plan that mapped every contract
to `auto_trader.py`. That valid plan reached the editor, but a redundant 62-second review of the earlier invalid
draft left only 26.9 seconds and the final local edit timed out. This confirms the contract binding fix while
isolating repair-budget scheduling as the next bottleneck. Full details:
`project_ws/AgentOps/FABLE5_HISTORICAL_TRADING_SCOPE_LANE_PILOT_CONTRACT_BOUND_ATTEMPT_RECEIPT.md`.

A later budget/health-aware scheduling pass then addressed the reliability half of that
bottleneck generically. Commit `542d9535` schedules the repair re-plan within the shared per-case
budget (route to the faster editor model, or stop deterministically, rather than clamp a doomed
reasoner call), and commit `0f9e8d67` falls back to the warm editor model once the reasoner has
timed out. On the 8 GB measurement host the dual-model configuration is unreliable: `qwen3:8b`
(reasoner) and `qwen2.5-coder:7b` (editor) cannot co-reside, so every diagnosis↔edit switch cold-
loads (~18 s), and the ~30 tok/s thinking reasoner lands at the ~135 s clamped timeout. Without a
fallback, the pipeline collapses to an all-timeout no-op (25/100, zero stages run). With the two
commits the reasoner timeout is detected and the plan/edit/repair stages reliably run on the warm
editor model — verified across two mechanism families (scope-lane ×2, queue-priority ×1) with
deterministic budget stops. This is a reproducible reliability improvement; the resulting score
remains stochastic (25–55) and bounded by local 7B editor synthesis, sealed-final stayed 0/1 in
every run, and the swap-free single-model run reached 55/100 with a retained correct-owner patch
and all repair-feedback tests green but still 2/6 failing sealed-final tests. Focused validation
passed 388 tests, zero premium calls. It does not change the untouched 40/100 scope-lane result or
prove Fable 5 parity. Full details:
`project_ws/AgentOps/FABLE5_DIAGNOSTIC_REASONING_BUDGET_HEALTH_SCHEDULING_RECEIPT.md`.

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
- Diagnostic memory stores controlled mechanism abstractions only. Retrieval requires same-user/same-repo database scope, approved validation provenance, positive query overlap, and non-superseded promotion; benchmark/evaluation mode disables it entirely.
- TypeScript validation invokes Node's parser-only type transformation and never evaluates repository source. Dart analysis receives isolated writable state instead of user profile/plugin state.
- Python true/false constant sets receive a semantic-polarity check before a local patch is accepted.
- Repair-feedback failure is evidence for a bounded repair loop; tests cannot be edited because only manifest-approved source candidates are eligible.
- Deterministic contract proposals are eligible only for recognized prompt/source shapes, must clear contradiction guards, and are retained only when both public and repair-feedback validation pass; otherwise the candidate snapshot is restored.
- In runner schema v3, blinded entries require a separate final oracle. It is loaded only after the last model call, runs once in a fresh repository without feedback tests, cannot overwrite seeded public tests, and cannot trigger another repair round.
- Production Project Autonomy defaults to three local validation-repair rounds and enforces a hard maximum of five. Every attempted scope is snapshotted byte-for-byte; an attempt that regresses a previously passing step or makes no measurable progress is restored before the next round.
- Full-autopilot diagnostic source-scope expansion is limited to a dedicated four-file cap, ranks source paths named or imported by failed read-only tests as candidates, requires explicit contract-owner coverage in generated plans, and acquires a file lease. Approval-mode plans never expand automatically.
- Repair-feedback test files are removed from the mutable repair set and hash-checked before and after validation. Zero collected tests are non-evidence, and every repair round reruns the original pinned selectors so a different passing suite cannot masquerade as progress.
- Source and test reads reject symlinks and re-check resolved worktree containment at the final read boundary. Raw model diffs must touch exactly their assigned path.
- The final bounded repair round may use the configured local 14B specialist only when it is installed and not cooling down. Compact escalation skips generative review, adapter retry, and model compiler recovery, caps generation at 4,096 tokens, and remains under the per-case model wall budget. Both paths set `local_only`, permit zero premium calls, and fail cleanly when unavailable.
- Ollama host fallback shares one total request deadline and pins the last working endpoint per model. A reachable-host timeout stops fallback instead of repeating the full timeout against other local instances.
- Automatic merge now invokes the previously disconnected scope, patch-size, validation-evidence, public-contract, and domain-invariant gates. Authoritative Git status is refreshed after validation; new or validation-created files are included in changed-file and diff evidence before staging, and rejected attempts restore unexpected tracked or untracked paths.

## Remaining Gaps

1. The latest valid frozen diagnosis-to-fix suites scored 40.83/100, 27.92/100, and 41.88/100, with only 3/32 sealed-final solves in aggregate. Disclosed replays solve recognized mechanisms, but unknown-mechanism transfer, large repositories, mixed stacks, Go, and Rust superiority remain unproven.
2. Provider-attested Fable 5 history exists, and an oracle-free eight-case same-task collection pack plus exact-response attestation bridge is frozen. The provider run is still pending explicit credit approval, so no same-task Fable 5 score exists yet. Historical answers remain excluded from a blinded score because current CHILI development may be contaminated by their fixes and task mechanics.
3. Eight independent eight-case diagnostic slices now total 64 cases and scored 88.12/100, 87.5/100, 76.25/100, 69.38/100, 74.4/100, 67.5/100, 63.8/100, and 83.12/100. Every untouched slice is below the 90 shadow threshold. The eighth oracle's five-family tolerance inflated its nominal result; strict primary-family scoring was 70.62/100. The set still lacks the required repository/language mix and direct Fable 5 comparison.
4. Runtime evidence currently covers bounded text logs, aggregate/schema PostgreSQL reads, typed-probe timestamps, structured causal timelines, hashed log correlation identities, and explicit cross-service flow edges. It does not yet provide external trace-backend ingestion, metrics backends, container state, process inspection, automatic producer/consumer role inference for arbitrary systems, or a live production proof using a separately provisioned SELECT-only role.
5. Local output remains stochastic, and structural usability is not diagnostic quality. The untouched fifteenth suite completed 78/78 local calls with all eight cases live-reasoning-qualified, yet solved only 1/8 finals. In the disclosed Node shared-state replay, the local reasoner reached the correct family and found both component mechanisms but still failed to synthesize the complete leading boundary. Disclosed functional solves therefore remain separate from unknown-mechanism reasoning quality.
6. Recognized repair synthesis is intentionally narrow. Unknown mechanisms, dependency migrations, frontend visual validation, true concurrency races, and large cross-service refactors remain under-tested.
7. Final reviewed-code cross-language latency averaged 73.9 seconds/case, while the fifteenth untouched suite averaged 278.8 seconds/case. Atomic per-case resume is now integration-tested, but a real OS/process host-loss recovery proof remains open, and unknown mechanisms can still take the full slow path.
8. Compact 14B escalation reduced the fresh 12-case wall time from 243.3 to 59.9 minutes and failed only 1/15 calls. It now has a coordinated atomic edit-bundle path, while the 7B base lane keeps the more reliable per-file adapter with shared plan context. Untouched success on unknown multi-owner mechanisms remains open.
9. Diagnostic memory is same-repository and lexical. It cannot yet transfer validated mechanisms across unrelated repositories, and unattended full-autopilot runs cannot self-promote their conclusions.
10. Four historical Fable 5 trading pilots scored 25/100, 40/100, 40/100, and 40/100 untouched. Disclosed deterministic recovery solves all four recognized families at 100/100 and zero premium calls. Contract-disabled replays on the fourth case now reach the correct policy owner and a complete repair plan, but still solve 0/1 because local synthesis and repair-budget scheduling do not finish reliably. System coverage is growing; unknown-mechanism reasoning, causal synthesis, stochastic repair, and latency remain the bottlenecks.

## Promotion Gate

Do not claim Fable 5 parity or superiority until all of the following are true:

1. At least 30 independently authored, blinded tasks across Python, TypeScript, Dart, SQL, Go or Rust, and mixed stacks; none may inform implementation before scoring.
2. At least 10 tasks require multi-file changes and at least 10 require dynamic diagnosis from failing tests or logs.
3. The same blinded tasks are run independently by authenticated Fable 5 and CHILI without sharing outputs.
4. Human adjudicators compare correctness, root-cause quality, unnecessary changes, safety, test quality, latency, and cost without seeing model identity.
5. CHILI has no premium calls, no safety violations, at least 95% sealed-final repair success, and a statistically defensible win or non-inferiority margin.
6. Results reproduce across at least three runs to measure local-model variance.

Until that gate is met, the accurate statement is:

> CHILI is a premium-independent, evidence-gated autonomous coding system with strong bounded shadow results. It is not a Fable 5 wrapper, and broad superiority remains an open empirical claim.
