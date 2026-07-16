# CHILI Model Candidate Tournament Benchmark

- Schema: chili.model-candidate-tournament-benchmark.v1
- Artifact schema: chili.model-candidate-tournament-artifacts.v1
- Generated UTC: 2026-07-10T02:54:45.707992Z
- Status: failed
- Target score: 100
- Evidence mode: real_artifacts
- Cases: 6
- Average score: 0/100
- Required source kinds: codex, claude, local_model
- Required frontier model targets: codex=gpt-5.5, claude=opus-4.8
- Missing source kinds: claude
- Source kinds: codex, local_model
- Required comparison classes: strict_candidate_win, runtime_control_behavior_regression, startup_contract_behavior_regression, preflight_behavior_regression, evidence_regression, scope_regression
- Missing comparison classes: none
- Required behavior: multi-source model outputs must be judged on scoped behavior-tested outcomes, with unsafe or regressing candidates rejected before any winner is selected.
- Safety: temporary repo patch replay only; no model calls, git action in the real checkout, runtime restart, deployment, database migration, broker call, or live-trading action.

| Case | Comparison Class | Winner | Score | Evidence |
| --- | --- | --- | ---: | --- |
| real-chili-broker-timeout-partial-loses | preflight_behavior_regression | none | 0 | reason=missing_source_kind:claude; incumbent=passed/behavior_tests_passed; sources=codex,local_model; passed=2; rejected=0 |
| real-chili-preflight-candidate-wins | strict_candidate_win | none | 0 | reason=missing_source_kind:claude; incumbent=passed/behavior_tests_passed; sources=codex,local_model; passed=2; rejected=0 |
| real-chili-runtime-control-no-evidence-loses | evidence_regression | none | 0 | reason=missing_source_kind:claude; incumbent=passed/behavior_tests_passed; sources=codex,local_model; passed=1; rejected=1; rejected_examples=local_model/local_model-real-chili-runtime-control-no-evidence-loses:failed/apply_failed |
| real-chili-runtime-control-partial-loses | runtime_control_behavior_regression | none | 0 | reason=missing_source_kind:claude; incumbent=passed/behavior_tests_passed; sources=codex,local_model; passed=1; rejected=1; rejected_examples=local_model/local_model-real-chili-runtime-control-partial-loses:failed/apply_failed |
| real-chili-runtime-control-unscoped-loses | scope_regression | none | 0 | reason=missing_source_kind:claude; incumbent=passed/behavior_tests_passed; sources=codex,local_model; passed=1; rejected=1; rejected_examples=local_model/local_model-real-chili-runtime-control-unscoped-loses:failed/apply_failed |
| real-chili-startup-static-partial-loses | startup_contract_behavior_regression | none | 0 | reason=missing_source_kind:claude; incumbent=passed/behavior_tests_passed; sources=codex,local_model; passed=1; rejected=1; rejected_examples=local_model/local_model-real-chili-startup-static-partial-loses:failed/invalid_diff |
