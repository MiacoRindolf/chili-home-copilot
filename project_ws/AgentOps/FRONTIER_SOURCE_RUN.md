# CHILI Frontier Source Runner

- Schema: chili.frontier-source-runner.v1
- Generated UTC: 2026-07-10T23:06:02.871974Z
- Status: passed
- Write mode: True
- Source kind: claude
- Model: claude-fable-5
- Source auth mode: subscription
- Run id: claude-fable5-20260710T230100Z
- Cases: 6
- Measured run duration seconds: 324.3374107000018
- Duration attribution: measured_source_wall_clock_evenly_attributed_across_cases
- Promotion ready: False
- Failure stage: none
- Failure reason: none
- Next action: Validate frontier source readiness with python scripts/autopilot_frontier_model_evidence_intake.py --input-root project_ws/AgentOps/frontier_model_evidence_intake/raw_sources_56_chili_default_comparison --allow-partial --json --no-write. Publish scorecards only after all required sources are ready: python scripts/autopilot_frontier_model_evidence_intake.py --input-root project_ws/AgentOps/frontier_model_evidence_intake/raw_sources_56_chili_default_comparison --publish-scorecards --json.
- Permission boundary: frontier source response collection only; no source/test edits, git/PR action, runtime restart, deployment, database migration, broker call, or live trading

| Artifact | Path |
| --- | --- |
| run_dir | D:\dev\chili-home-copilot\project_ws\AgentOps\frontier_model_evidence_intake\source_runs\claude-fable5-20260710t230100z |
| response | project_ws\AgentOps\frontier_model_evidence_intake\collection_packets\fable5_all_cases_response.txt |
| metadata | D:\dev\chili-home-copilot\project_ws\AgentOps\frontier_model_evidence_intake\raw_sources_56_chili_default_comparison\claude\metadata.json |
| transcript | D:\dev\chili-home-copilot\project_ws\AgentOps\frontier_model_evidence_intake\raw_sources_56_chili_default_comparison\claude\transcript.jsonl |
| raw_dir | D:\dev\chili-home-copilot\project_ws\AgentOps\frontier_model_evidence_intake\raw_sources_56_chili_default_comparison\claude\raw |
