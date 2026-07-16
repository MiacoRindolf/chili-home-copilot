# CHILI Local Model Evidence Recording

- Schema: chili.local-model-evidence-recorder.v1
- Generated UTC: 2026-07-10T14:21:29.324201Z
- Status: passed
- Write mode: True
- Source kind: local_model
- Model: qwen2.5-coder:7b
- Run id: local-qwen25-allcases-synth-probe-20260710-070150
- Cases: 6
- Validated with provenance: True
- Promotion ready: False
- Source dir: project_ws\AgentOps\frontier_model_evidence_intake\measured_synth_sources\local_model
- Next action: Record matching Codex and Claude drops, then run scripts/autopilot_frontier_model_evidence_intake.py --publish-scorecards.
- Permission boundary: records and validates local-model evidence only; it does not run models, edit source/tests, restart runtime, use git/PR tools, deploy, or touch live trading

| Artifact | Path |
| --- | --- |
| prompt_pack | project_ws\AgentOps\frontier_model_evidence_intake\measured_synth_sources\local_model\prompt_pack.md |
| metadata | project_ws\AgentOps\frontier_model_evidence_intake\measured_synth_sources\local_model\metadata.json |
| transcript | project_ws\AgentOps\frontier_model_evidence_intake\measured_synth_sources\local_model\transcript.jsonl |
| raw_dir | project_ws\AgentOps\frontier_model_evidence_intake\measured_synth_sources\local_model\raw |

| Raw file |
| --- |
| real-chili-broker-timeout-partial-loses.json |
| real-chili-broker-timeout-partial-loses.patch |
| real-chili-preflight-candidate-wins.json |
| real-chili-preflight-candidate-wins.patch |
| real-chili-runtime-control-no-evidence-loses.json |
| real-chili-runtime-control-no-evidence-loses.patch |
| real-chili-runtime-control-partial-loses.json |
| real-chili-runtime-control-partial-loses.patch |
| real-chili-runtime-control-unscoped-loses.json |
| real-chili-runtime-control-unscoped-loses.patch |
| real-chili-startup-static-partial-loses.json |
| real-chili-startup-static-partial-loses.patch |
