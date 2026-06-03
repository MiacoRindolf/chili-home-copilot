# CHILI Frontier Model Evidence Intake

- Schema: chili.frontier-model-evidence-intake.v1
- Generated UTC: 2026-06-03T15:21:36.584700Z
- Status: warning
- Input root: D:\dev\chili-home-copilot\project_ws\AgentOps\frontier_model_evidence_intake\raw_sources
- Generated artifacts root: D:\dev\chili-home-copilot\project_ws\AgentOps\frontier_model_evidence_intake
- Preflight report: D:\dev\chili-home-copilot\project_ws\AgentOps\FRONTIER_EVIDENCE_PREFLIGHT_LIVE.md
- Preflight recovery routes: 1
- Source kinds: codex, local_model
- Ready sources: 2/3
- Missing/incomplete sources: claude
- Shadow evidence mode: partial_real_manifest
- Shadow status: failed
- Tournament evidence mode: real_artifacts
- Tournament status: failed
- Published scorecards: False
- Required behavior: one run ingests Codex, Claude, and local-model raw drops, stamps provenance, validates real shadow evidence, and runs the real-artifact tournament.

| Artifact | Path |
| --- | --- |
| manifest | D:\dev\chili-home-copilot\project_ws\AgentOps\frontier_model_evidence_intake\manifests\codex.manifest.json |
| manifest | D:\dev\chili-home-copilot\project_ws\AgentOps\frontier_model_evidence_intake\manifests\local_model.manifest.json |
| model shadow scorecard | D:\dev\chili-home-copilot\project_ws\AgentOps\frontier_model_evidence_intake\scorecards\MODEL_SHADOW_EVIDENCE_BENCHMARK.md |
| model tournament scorecard | D:\dev\chili-home-copilot\project_ws\AgentOps\frontier_model_evidence_intake\scorecards\MODEL_CANDIDATE_TOURNAMENT_BENCHMARK.md |

## Source Readiness

| Source | Path | Status | Raw drops | Missing files | Next action |
| --- | --- | --- | ---: | --- | --- |
| codex | codex | ready | 1 | none | none |
| claude | claude | partial | 0 | claude/metadata.json, claude/transcript.jsonl, claude/raw/*.json | Preflight recovery: Import saved claude response. Save all-cases response to: D:\dev\chili-home-copilot\project_ws\AgentOps\frontier_model_evidence_intake\collection_packets\claude_all_cases_response.txt. Dry-run import first: python scripts/autopilot_frontier_source_evidence_recorder.py --source-kind claude --all-cases --response project_ws/AgentOps/frontier_model_evidence_intake/collection_packets/claude_all_cases_response.txt --run-id <real-claude-run-id> --source-command <exact-claude-command-or-session-export> --json --no-write. All-cases import: python scripts/autopilot_frontier_source_evidence_recorder.py --source-kind claude --all-cases --response project_ws/AgentOps/frontier_model_evidence_intake/collection_packets/claude_all_cases_response.txt --run-id <real-claude-run-id> --source-command <exact-claude-command-or-session-export> --json. Single-case fallback: python scripts/autopilot_frontier_source_evidence_recorder.py --source-kind claude --case-id real-chili-preflight-candidate-wins --response project_ws/AgentOps/frontier_model_evidence_intake/collection_packets/claude_single_case_response.txt --run-id <real-claude-run-id> --source-command <exact-claude-command-or-session-export> --json. After import validation: python scripts/autopilot_frontier_model_evidence_intake.py --input-root project_ws/AgentOps/frontier_model_evidence_intake/raw_sources --allow-partial --json --no-write. Publish only when all sources are ready: python scripts/autopilot_frontier_model_evidence_intake.py --input-root project_ws/AgentOps/frontier_model_evidence_intake/raw_sources --publish-scorecards --json. Boundary: collection and evidence import only; does not run models, edit source/tests, use git/PR tools, restart runtime, deploy, or touch live trading. |
| local_model | local_model | ready | 1 | none | none |

## Preflight Recovery Routes

| Source | Action | Staging file | Dry-run import | Write/import | Single-case fallback | Validate | Publish | Boundary |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| claude | Import saved claude response | D:\dev\chili-home-copilot\project_ws\AgentOps\frontier_model_evidence_intake\collection_packets\claude_all_cases_response.txt | python scripts/autopilot_frontier_source_evidence_recorder.py --source-kind claude --all-cases --response project_ws/AgentOps/frontier_model_evidence_intake/collection_packets/claude_all_cases_response.txt --run-id <real-claude-run-id> --source-command <exact-claude-command-or-session-export> --json --no-write | python scripts/autopilot_frontier_source_evidence_recorder.py --source-kind claude --all-cases --response project_ws/AgentOps/frontier_model_evidence_intake/collection_packets/claude_all_cases_response.txt --run-id <real-claude-run-id> --source-command <exact-claude-command-or-session-export> --json | python scripts/autopilot_frontier_source_evidence_recorder.py --source-kind claude --case-id real-chili-preflight-candidate-wins --response project_ws/AgentOps/frontier_model_evidence_intake/collection_packets/claude_single_case_response.txt --run-id <real-claude-run-id> --source-command <exact-claude-command-or-session-export> --json | python scripts/autopilot_frontier_model_evidence_intake.py --input-root project_ws/AgentOps/frontier_model_evidence_intake/raw_sources --allow-partial --json --no-write | python scripts/autopilot_frontier_model_evidence_intake.py --input-root project_ws/AgentOps/frontier_model_evidence_intake/raw_sources --publish-scorecards --json | collection and evidence import only; does not run models, edit source/tests, use git/PR tools, restart runtime, deploy, or touch live trading |
