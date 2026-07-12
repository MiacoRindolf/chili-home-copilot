# CHILI vs Fable 5: Autonomous Coding Capability Gap Report

Date: 2026-07-12

## Current Verdict

CHILI is now **qualified for local-only development shadow use** on the tested diagnostic and repair contracts. All eight untouched independent diagnostic slices remain below the promotion threshold, so it is **not yet proven universally better than Claude Fable 5** on arbitrary complex coding work.

This distinction is intentional. The current evidence proves that CHILI can diagnose, gather bounded evidence, select owning files, generate local patches, use validation failures as new evidence, repair or roll back its own patch, and pass cross-language development regressions without premium calls. It does not yet provide a blinded, statistically meaningful head-to-head against Fable 5.

The historical diagnosis-to-fix reports used the label `hidden` for tests that were loaded after the initial patch but could then guide bounded repair. Those scores are feedback-guided development evidence, not sealed final-adjudication evidence. Runner schema v3 now separates repair-feedback tests from a final oracle that is first read after every model call for its case. The first eight-case v3 holdout scored 53.75/100 with 1/8 final passes. After generic repair-loop hardening, a new independently authored eight-case holdout improved to 68.75/100 and 2/8 final passes, still far below replacement readiness.

A later contract-guided replay of the now-disclosed tenth suite scored 58.12/100 with the same 2/8 sealed-final
passes, 6/8 correct causal families, and 5/8 exact owner sets. It used zero premium calls but required 105.6
minutes. This is development evidence only: diagnosis improved, while functional reliability and latency remained
well below replacement readiness.

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
| Diagnosis-to-fix autonomy | Local diagnosis, exact tracked-file selection, atomic multi-file edit groups, public+repair-feedback validation, assertion-contract extraction, rollback, bounded repair, and isolated final adjudication | Ninth sealed holdout: 53.75/100 and 1/8 final. Fresh post-hardening tenth holdout: 68.75/100, 2/8 final, 3/8 diagnosis families, and 7/8 exact file sets. Improved, but failed promotion |
| Cross-language syntax validation | Python AST, parser-only Node TypeScript/JavaScript validation, and Dart analysis with isolated analyzer state; exact changed-file coverage required | Proven by safety and orchestration tests |
| Recognized contract repair | Prompt-derived invariants and repository-shape checks for single-flight eviction, cancellation propagation, injected-clock TTL, subscription lifecycle, partial uniqueness, and sibling pre-aggregation | Proven by variant unit tests and six real regression fixtures; deliberately narrow |
| Production validation repair | Project Autonomy reads full targeted test contracts, pins the original test selectors, maps test imports to bounded source owners, snapshots every repair scope, retains only measurable same-scope validation progress, rolls back regressions/no-progress edits, and feeds rejected diffs and failure signatures into later attempts | Default three rounds, hard maximum five; final-round local 14B escalation is Ollama-only and fail-closed; 163 production-autonomy tests and 166 diagnostic/repair tests passed. Fresh transfer improved file ownership but only solved 2/8 final contracts |
| Fable 5 reference accuracy | Active target is `claude-fable-5`; exact candidate responses must bind to provider-native or provider-UI identity evidence | Proven for archive identity and collection controls; no same-task score yet |
| Independent post-freeze diagnostic holdout | Cases authored only after the tested source SHA was frozen; case/oracle hashes unchanged through the run | Eight eight-case slices completed at 88.12/100, 87.5/100, 76.25/100, 69.38/100, 74.4/100, 67.5/100, 63.8/100, and 83.12/100; all below the 90 shadow threshold. The eighth oracle was dimension-lenient; strict primary-family scoring is 70.62/100 |
| Durable local benchmark execution | Atomic per-case checkpoints bound to source, runner, public inputs, model, stages, and inference parameters; incompatible resumes fail closed | Proven by simulated interruption and compatible resume; real host-loss recovery remains untested |
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
- Full-autopilot source-scope expansion is limited to at most two files directly identified by failed read-only test imports, remains under the eight-file cap, and acquires a file lease. Approval-mode plans never expand automatically.
- Repair-feedback test files are removed from the mutable repair set and hash-checked before and after validation. Zero collected tests are non-evidence, and every repair round reruns the original pinned selectors so a different passing suite cannot masquerade as progress.
- Source and test reads reject symlinks and re-check resolved worktree containment at the final read boundary. Raw model diffs must touch exactly their assigned path.
- The final bounded repair round may use the configured local 14B specialist only when it is installed and not cooling down. Both primary and escalation paths set `local_only`, permit zero premium calls, and fail cleanly when unavailable.
- Automatic merge now invokes the previously disconnected scope, patch-size, validation-evidence, public-contract, and domain-invariant gates. Authoritative Git status is refreshed after validation; new or validation-created files are included in changed-file and diff evidence before staging, and rejected attempts restore unexpected tracked or untracked paths.

## Remaining Gaps

1. The repair evidence now includes 13 small feedback-guided development repositories, two independently authored eight-case sealed suites, and a full disclosed tenth-suite replay. The ninth scored 53.75/100 with 1/8 final passes; the untouched tenth improved to 68.75/100 with 2/8 final passes. The disclosed contract-guided replay scored 58.12/100 and still solved only 2/8 final contracts. Large-repository, mixed-stack, Go, and Rust superiority remain untested.
2. Provider-attested Fable 5 history exists, but no provider-attested Fable output exists for the same frozen repair cases. Historical answers are excluded from a blinded score because current CHILI development may be contaminated by their fixes and task mechanics.
3. Eight independent eight-case diagnostic slices now total 64 cases and scored 88.12/100, 87.5/100, 76.25/100, 69.38/100, 74.4/100, 67.5/100, 63.8/100, and 83.12/100. Every untouched slice is below the 90 shadow threshold. The eighth oracle's five-family tolerance inflated its nominal result; strict primary-family scoring was 70.62/100. The set still lacks the required repository/language mix and direct Fable 5 comparison.
4. Runtime evidence currently covers bounded text logs, aggregate/schema PostgreSQL reads, typed-probe timestamps, structured causal timelines, hashed log correlation identities, and explicit cross-service flow edges. It does not yet provide external trace-backend ingestion, metrics backends, container state, process inspection, automatic producer/consumer role inference for arbitrary systems, or a live production proof using a separately provisioned SELECT-only role.
5. Local 7B output remains stochastic. The second untouched diagnostic slice produced 0/24 usable packets before compact contracts. The fourth through eighth untouched slices accepted 24/24 packets yet scored only 69.38, 74.4, 67.5, 63.8, and 83.12, proving that structural usability is not diagnostic quality. The untouched tenth suite completed 141/141 local calls but chose the wrong diagnosis family in 5/8 cases and failed 6/8 final contracts. Its disclosed contract-guided replay improved diagnosis to 6/8 but still failed the same 6/8 final contracts. Strong structural reliability, causal labels, exact ownership, and frontier-level repair synthesis remain distinct capabilities.
6. Recognized repair synthesis is intentionally narrow. Unknown mechanisms, dependency migrations, frontend visual validation, true concurrency races, and large cross-service refactors remain under-tested.
7. Final reviewed-code cross-language latency averaged 73.9 seconds/case. The disclosed contract-guided tenth replay averaged 791.8 seconds/case and required 6,334.5 seconds end to end. Atomic per-case checkpointing now prevents total progress loss and passed a simulated interruption/resume test, but adaptive stage routing, cancellation of redundant generative review, and a real host-loss recovery proof remain open.
8. The 14B local model is too slow and unreliable for default routing on current hardware. The untouched tenth suite used 30 escalation calls in 2,786.5 seconds; the disclosed replay used 39 escalation calls and expanded to 6,334.5 seconds while still solving only 2/8 final suites. It remains limited to an optional final local repair round; evidence-gated routing must reduce unnecessary specialist calls before the workflow is practical.
9. Diagnostic memory is same-repository and lexical. It cannot yet transfer validated mechanisms across unrelated repositories, and unattended full-autopilot runs cannot self-promote their conclusions.

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
