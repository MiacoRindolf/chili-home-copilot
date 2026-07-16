# CHILI Frontier Gap Matrix

- Schema: chili.frontier-gap-matrix.v1
- Generated UTC: 2026-07-11T05:27:14.416229Z
- Status: passed
- Claim status: frontier_superiority_proven
- Readiness score: 100/100
- Readiness blockers: 0
- Core coding proven: True
- Frontier evidence proven: True
- Frontier superiority proven: True
- Candidate generation superiority proven: True
- Premium-independent local autonomy proven: True
- Micro candidate superiority proven: True
- Meso project workflow superiority proven: True
- Macro long-horizon superiority proven: True
- Deep-context reasoning superiority proven: True
- Codex head-to-head available-source proven: True
- Tournament winner counts: local_model=6, codex=0, claude=0, none=0
- Available-source leader counts: local_model=6, codex=0, claude=0, none=0
- Tournament runtime measurements: measured=18, unmeasured=0
- Tournament unmeasured runtime count: 0
- Superiority required winner source: local_model
- Missing sources: none
- Claude source auth mode: subscription
- Claude API-key probe status: api_key_missing
- Claude source runner command: python scripts/autopilot_frontier_source_runner.py --source-kind claude --source-auth-mode auto --json
- Next action: none
- Safety: read-only evidence synthesis only; no model calls, git action, runtime restart, deployment, database, broker, or live-trading action.

## Proof Matrix

| Domain | Proof status | Actual | Evidence | Next action |
| --- | --- | --- | --- | --- |
| core_coding_benchmark | proven | status=passed; score=100/100; pass_rate=70/70; source_stability=stable; source_freshness=current | D:\dev\chili-home-copilot\project_ws\AgentOps\CODING_BENCHMARK_SCORECARD.md | none |
| frontier_source_evidence | proven | ready_sources=3/3; missing_sources=none; claude_auth=subscription; api_key_probe=api_key_missing | D:\dev\chili-home-copilot\project_ws\AgentOps\FRONTIER_MODEL_EVIDENCE_INTAKE.md | none |
| model_shadow_evidence | proven | status=passed; mode=real_manifest; missing_sources=none | D:\dev\chili-home-copilot\project_ws\AgentOps\MODEL_SHADOW_EVIDENCE_BENCHMARK.md | none |
| model_candidate_tournament | proven | status=passed; mode=real_artifacts; missing_sources=none | D:\dev\chili-home-copilot\project_ws\AgentOps\MODEL_CANDIDATE_TOURNAMENT_BENCHMARK.md | none |
| candidate_generation_superiority | proven | winner_counts=local_model=6, codex=0, claude=0, none=0; required=local_model wins all >=6 real-artifact tournament cases; runtime_measurements=measured=18, unmeasured=0 | D:\dev\chili-home-copilot\project_ws\AgentOps\MODEL_CANDIDATE_TOURNAMENT_BENCHMARK.md | none |
| premium_independent_local_autonomy | proven | status=passed; score=100/100 | D:\dev\chili-home-copilot\project_ws\AgentOps\OFFLINE_PROJECT_AUTONOMY_BENCHMARK.md | none |
| meso_project_workflow_superiority | proven | status=passed; mode=real_artifacts; tasks=3; winner_counts=local_model=3, codex=0, claude=0, none=0; runtime_measurements=measured=9, unmeasured=0 | D:\dev\chili-home-copilot\project_ws\AgentOps\MESO_PROJECT_WORKFLOW_TOURNAMENT_BENCHMARK.md | none |
| macro_long_horizon_superiority | proven | status=passed; mode=real_artifacts; tasks=3; winner_counts=local_model=3, codex=0, claude=0, none=0; runtime_measurements=measured=9, unmeasured=0 | D:\dev\chili-home-copilot\project_ws\AgentOps\MACRO_LONG_HORIZON_TOURNAMENT_BENCHMARK.md | none |
| deep_context_reasoning_superiority | proven | status=passed; mode=real_artifacts; tasks=3; winner_counts=local_model=3, codex=0, claude=0, none=0; runtime_measurements=measured=9, unmeasured=0 | D:\dev\chili-home-copilot\project_ws\AgentOps\DEEP_CONTEXT_REASONING_TOURNAMENT_BENCHMARK.md | none |
| codex_head_to_head_available_sources | proven | available_source_leaders=local_model=6, codex=0, claude=0, none=0; sources=claude, codex, local_model; runtime_measurements=measured=18, unmeasured=0 | D:\dev\chili-home-copilot\project_ws\AgentOps\MODEL_CANDIDATE_TOURNAMENT_BENCHMARK.md | none |
| hosted_pr_repair_evidence | proven | status=passed; mode=real_inventory; promotion_eligible=true | D:\dev\chili-home-copilot\project_ws\AgentOps\HOSTED_PR_REPAIR_ARTIFACT_BENCHMARK.md | none |

## Blocking Gaps

| Gap | Status | Required | Actual | Evidence | Next action |
| --- | --- | --- | --- | --- | --- |
| none | passed | none | none | none | none |
