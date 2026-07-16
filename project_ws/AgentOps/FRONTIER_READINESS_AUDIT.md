# CHILI Frontier Readiness Audit

- Schema: chili.frontier-readiness-audit.v1
- Status: passed
- Readiness score: 100/100
- Requirements: 29
- Blockers: 0
- Required behavior: Codex 5.6 Sol / Claude Fable 5-class promotion must be backed by stable all-up coding evidence plus real model-shadow, real tournament, and real hosted PR repair artifacts, while CHILI's operational coding path remains premium-independent and locally executable.

| Requirement | Status | Required | Actual | Evidence | Next action |
| --- | --- | --- | --- | --- | --- |
| runner_pytest_supported_version | passed | pytest>=8.2,<9 | 8.4.2 | D:\dev\chili-home-copilot\.pytest_venv\Scripts\python.exe (.pytest_venv) | none |
| runner_pytest_runtime_isolation | passed | isolated repo-local pytest runtime | isolated | D:\dev\chili-home-copilot\.pytest_venv\Scripts\python.exe (.pytest_venv) | none |
| runner_pytest_required_imports | passed | all required pytest runtime imports available | none missing | D:\dev\chili-home-copilot\.pytest_venv\Scripts\python.exe (.pytest_venv) | none |
| coding_benchmark_harness_references | passed | all scenario command files exist | none missing | scripts/autopilot_coding_benchmark.py scenario commands | none |
| coding_scorecard_status | passed | passed | passed | project_ws/AgentOps/CODING_BENCHMARK_SCORECARD.md | none |
| coding_score | passed | >=90 | 100 | project_ws/AgentOps/CODING_BENCHMARK_SCORECARD.md Overall score | none |
| coding_scenario_count | passed | >=6 | 70 | project_ws/AgentOps/CODING_BENCHMARK_SCORECARD.md Scenarios | none |
| coding_pass_rate | passed | all scenarios passed | 70/70 | project_ws/AgentOps/CODING_BENCHMARK_SCORECARD.md Pass rate | none |
| source_stability | passed | stable with 0 source changes | stable; changes=0 | project_ws/AgentOps/CODING_BENCHMARK_SCORECARD.md Source stability | none |
| coding_scorecard_current_source_freshness | passed | no source/test files newer than Generated UTC | current; changes=0 | project_ws/AgentOps/CODING_BENCHMARK_SCORECARD.md Generated UTC; project_ws/AgentOps/SOURCE_CHURN_DIAGNOSTICS.md | none |
| required_capability_coverage | passed | all required capabilities covered | none missing | 127 required capabilities | none |
| offline_project_autonomy_status | passed | passed | passed | project_ws/AgentOps/OFFLINE_PROJECT_AUTONOMY_BENCHMARK.md | none |
| offline_project_autonomy_score | passed | 100/100 | 100/100 | project_ws/AgentOps/OFFLINE_PROJECT_AUTONOMY_BENCHMARK.md Average score | none |
| offline_project_autonomy_zero_premium_calls | passed | premium_models_required=false and premium_calls=0 | premium dependency absent; premium calls=0 | project_ws/AgentOps/OFFLINE_PROJECT_AUTONOMY_BENCHMARK.md | none |
| synthetic_repo_repair_scorecard_status | passed | passed | passed | project_ws/AgentOps/SYNTHETIC_REPO_REPAIR_BENCHMARK.md | none |
| model_promotion_scorecard_status | passed | passed | passed | project_ws/AgentOps/MODEL_PROMOTION_REPLAY_BENCHMARK.md | none |
| local_model_candidate_run_status | passed | local_model source drop imported or candidate run promotion ready | covered by real frontier artifacts | project_ws/AgentOps/LOCAL_MODEL_CANDIDATE_RUN.md | none |
| model_shadow_scorecard_status | passed | passed | passed | project_ws/AgentOps/MODEL_SHADOW_EVIDENCE_BENCHMARK.md | none |
| model_shadow_check_count | passed | checks>=7 | 7 | project_ws/AgentOps/MODEL_SHADOW_EVIDENCE_BENCHMARK.md | none |
| model_shadow_real_manifest_mode | passed | real_manifest | real_manifest | project_ws/AgentOps/MODEL_SHADOW_EVIDENCE_BENCHMARK.md | none |
| model_tournament_scorecard_status | passed | passed | passed | project_ws/AgentOps/MODEL_CANDIDATE_TOURNAMENT_BENCHMARK.md | none |
| model_tournament_case_count | passed | cases>=6 | 6 | project_ws/AgentOps/MODEL_CANDIDATE_TOURNAMENT_BENCHMARK.md | none |
| model_tournament_real_artifacts_mode | passed | real_artifacts | real_artifacts | project_ws/AgentOps/MODEL_CANDIDATE_TOURNAMENT_BENCHMARK.md | none |
| codex_tournament_case_pass_count | passed | codex passes 6/6 tournament cases | present=6/6; passed=6/6; rejected=0 | project_ws/AgentOps/MODEL_CANDIDATE_TOURNAMENT_BENCHMARK.md | none |
| local_model_tournament_case_pass_count | passed | local_model passes 6/6 tournament cases | present=6/6; passed=6/6; rejected=0 | project_ws/AgentOps/MODEL_CANDIDATE_TOURNAMENT_BENCHMARK.md | none |
| hosted_pr_repair_scorecard_status | passed | passed | passed | project_ws/AgentOps/HOSTED_PR_REPAIR_ARTIFACT_BENCHMARK.md | none |
| hosted_pr_repair_check_count | passed | checks>=18 | 18 | project_ws/AgentOps/HOSTED_PR_REPAIR_ARTIFACT_BENCHMARK.md | none |
| hosted_pr_repair_real_inventory_mode | passed | real_inventory | real_inventory | project_ws/AgentOps/HOSTED_PR_REPAIR_ARTIFACT_BENCHMARK.md | none |
| hosted_pr_repair_promotion_eligible | passed | true | true | project_ws/AgentOps/HOSTED_PR_REPAIR_ARTIFACT_BENCHMARK.md promotion eligible | none |
