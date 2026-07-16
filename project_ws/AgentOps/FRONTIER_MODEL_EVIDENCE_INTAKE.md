# CHILI Frontier Model Evidence Intake

- Schema: chili.frontier-model-evidence-intake.v1
- Generated UTC: 2026-07-10T23:09:55.018863Z
- Status: passed
- Input root: D:\dev\chili-home-copilot\project_ws\AgentOps\frontier_model_evidence_intake\raw_sources_56_chili_default_comparison
- Generated artifacts root: D:\dev\chili-home-copilot\project_ws\AgentOps\frontier_model_evidence_intake\intake_56_chili_fable5_comparison
- Preflight report: none
- Preflight recovery routes: 0
- Availability report: D:\dev\chili-home-copilot\project_ws\AgentOps\FRONTIER_SOURCE_AVAILABILITY_DIAGNOSTICS.md
- Availability recovery routes: 0
- Collection packet summary: D:\dev\chili-home-copilot\project_ws\AgentOps\FRONTIER_SOURCE_COLLECTION_PACKETS.md
- Source runner routes: 3
- Source kinds: codex, claude, local_model
- Ready sources: 3/3
- Missing/incomplete sources: none
- Shadow evidence mode: real_manifest
- Shadow status: passed
- Tournament evidence mode: real_artifacts
- Tournament status: passed
- Published scorecards: True
- Required behavior: one run ingests Codex, Claude, and local-model raw drops, stamps provenance, validates real shadow evidence, and runs the real-artifact tournament.

| Artifact | Path |
| --- | --- |
| manifest | D:\dev\chili-home-copilot\project_ws\AgentOps\frontier_model_evidence_intake\intake_56_chili_fable5_comparison\manifests\codex.manifest.json |
| manifest | D:\dev\chili-home-copilot\project_ws\AgentOps\frontier_model_evidence_intake\intake_56_chili_fable5_comparison\manifests\claude.manifest.json |
| manifest | D:\dev\chili-home-copilot\project_ws\AgentOps\frontier_model_evidence_intake\intake_56_chili_fable5_comparison\manifests\local_model.manifest.json |
| model shadow scorecard | D:\dev\chili-home-copilot\project_ws\AgentOps\MODEL_SHADOW_EVIDENCE_BENCHMARK.md |
| model tournament scorecard | D:\dev\chili-home-copilot\project_ws\AgentOps\MODEL_CANDIDATE_TOURNAMENT_BENCHMARK.md |

## Source Readiness

| Source | Path | Status | Raw drops | Missing files | Next action |
| --- | --- | --- | ---: | --- | --- |
| codex | codex | ready | 6 | none | none |
| claude | claude | ready | 6 | none | none |
| local_model | local_model | ready | 6 | none | none |

## Source Runner Routes

| Source | Runner command | Packet summary |
| --- | --- | --- |
| codex | python scripts/autopilot_frontier_source_runner.py --source-kind codex --source-auth-mode account --json | D:\dev\chili-home-copilot\project_ws\AgentOps\FRONTIER_SOURCE_COLLECTION_PACKETS.md |
| claude | python scripts/autopilot_frontier_source_runner.py --source-kind claude --source-auth-mode auto --json | D:\dev\chili-home-copilot\project_ws\AgentOps\FRONTIER_SOURCE_COLLECTION_PACKETS.md |
| local_model | python scripts/autopilot_frontier_source_runner.py --source-kind local_model --source-auth-mode auto --json | D:\dev\chili-home-copilot\project_ws\AgentOps\FRONTIER_SOURCE_COLLECTION_PACKETS.md |
