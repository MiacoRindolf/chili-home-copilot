# CHILI Model Candidate Tournament Benchmark

- Schema: chili.model-candidate-tournament-benchmark.v1
- Artifact schema: chili.model-candidate-tournament-artifacts.v1
- Generated UTC: 2026-07-10T20:52:05.284851Z
- Status: failed
- Target score: 100
- Evidence mode: real_artifacts
- Cases: 6
- Average score: 0/100
- Required source kinds: codex, claude, local_model
- Required frontier model targets: codex=gpt-5.6-sol, claude=opus-4.8
- Missing source kinds: claude, local_model
- Source kinds: codex
- Runtime measurements: measured=0, unmeasured=6
- Available-source leader counts: local_model=0, codex=6, claude=0, none=0
- Required comparison classes: strict_candidate_win, runtime_control_behavior_regression, startup_contract_behavior_regression, preflight_behavior_regression, evidence_regression, scope_regression
- Missing comparison classes: none
- Required behavior: multi-source model outputs must be judged on scoped behavior-tested outcomes, with unsafe or regressing candidates rejected before any winner is selected.
- Safety: temporary repo patch replay only; no model calls, git action in the real checkout, runtime restart, deployment, database migration, broker call, or live-trading action.

| Case | Comparison Class | Winner | Score | Evidence |
| --- | --- | --- | ---: | --- |
| real-chili-broker-timeout-partial-loses | preflight_behavior_regression | none | 0 | reason=missing_source_kind:claude,local_model; incumbent=passed/behavior_tests_passed; sources=codex; passed=1; rejected=0; available_source_leader=codex/codex-real-chili-broker-timeout-partial-loses; available_source_leader_duration=0.00s; available_source_leader_cost=0.00; passed_examples=codex/codex-real-chili-broker-timeout-partial-loses:passed/behavior_tests_passed; unmeasured_runtime=codex/codex-real-chili-broker-timeout-partial-loses |
| real-chili-preflight-candidate-wins | strict_candidate_win | none | 0 | reason=missing_source_kind:claude,local_model; incumbent=passed/behavior_tests_passed; sources=codex; passed=1; rejected=0; available_source_leader=codex/codex-real-chili-preflight-candidate-wins; available_source_leader_duration=0.00s; available_source_leader_cost=0.00; passed_examples=codex/codex-real-chili-preflight-candidate-wins:passed/behavior_tests_passed; unmeasured_runtime=codex/codex-real-chili-preflight-candidate-wins |
| real-chili-runtime-control-no-evidence-loses | evidence_regression | none | 0 | reason=missing_source_kind:claude,local_model; incumbent=passed/behavior_tests_passed; sources=codex; passed=1; rejected=0; available_source_leader=codex/codex-real-chili-runtime-control-no-evidence-loses; available_source_leader_duration=0.00s; available_source_leader_cost=0.00; passed_examples=codex/codex-real-chili-runtime-control-no-evidence-loses:passed/behavior_tests_passed; unmeasured_runtime=codex/codex-real-chili-runtime-control-no-evidence-loses |
| real-chili-runtime-control-partial-loses | runtime_control_behavior_regression | none | 0 | reason=missing_source_kind:claude,local_model; incumbent=passed/behavior_tests_passed; sources=codex; passed=1; rejected=0; available_source_leader=codex/codex-real-chili-runtime-control-partial-loses; available_source_leader_duration=0.00s; available_source_leader_cost=0.00; passed_examples=codex/codex-real-chili-runtime-control-partial-loses:passed/behavior_tests_passed; unmeasured_runtime=codex/codex-real-chili-runtime-control-partial-loses |
| real-chili-runtime-control-unscoped-loses | scope_regression | none | 0 | reason=missing_source_kind:claude,local_model; incumbent=passed/behavior_tests_passed; sources=codex; passed=1; rejected=0; available_source_leader=codex/codex-real-chili-runtime-control-unscoped-loses; available_source_leader_duration=0.00s; available_source_leader_cost=0.00; passed_examples=codex/codex-real-chili-runtime-control-unscoped-loses:passed/behavior_tests_passed; unmeasured_runtime=codex/codex-real-chili-runtime-control-unscoped-loses |
| real-chili-startup-static-partial-loses | startup_contract_behavior_regression | none | 0 | reason=missing_source_kind:claude,local_model; incumbent=passed/behavior_tests_passed; sources=codex; passed=1; rejected=0; available_source_leader=codex/codex-real-chili-startup-static-partial-loses; available_source_leader_duration=0.00s; available_source_leader_cost=0.00; passed_examples=codex/codex-real-chili-startup-static-partial-loses:passed/behavior_tests_passed; unmeasured_runtime=codex/codex-real-chili-startup-static-partial-loses |
