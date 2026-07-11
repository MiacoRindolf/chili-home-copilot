# CHILI claude Frontier Source Collection Packet

- Schema: chili.frontier-source-collection-packet.v1
- Generated UTC: 2026-06-03T15:02:15.491818Z
- Source kind: claude
- Model name: claude-fable-5
- Current source status: partial
- Prompt pack: D:\dev\chili-home-copilot\project_ws\AgentOps\frontier_model_prompt_packs\claude\prompt_pack.md
- Prompt pack SHA-256: 1160abd9a2d081007b1eb1404a963562b616003bddc701578a91a0d02ebb0cb3
- Raw source dir: D:\dev\chili-home-copilot\project_ws\AgentOps\frontier_model_evidence_intake\raw_sources\claude
- Response staging file: project_ws/AgentOps/frontier_model_evidence_intake/collection_packets/claude_all_cases_response.txt
- Recommended recorder command: python scripts/autopilot_frontier_source_evidence_recorder.py --source-kind claude --all-cases --response project_ws/AgentOps/frontier_model_evidence_intake/collection_packets/claude_all_cases_response.txt --run-id <real-claude-run-id> --source-command <exact-claude-command-or-session-export> --json --no-write
- Write/import recorder command: python scripts/autopilot_frontier_source_evidence_recorder.py --source-kind claude --all-cases --response project_ws/AgentOps/frontier_model_evidence_intake/collection_packets/claude_all_cases_response.txt --run-id <real-claude-run-id> --source-command <exact-claude-command-or-session-export> --json
- Single-case fallback command: python scripts/autopilot_frontier_source_evidence_recorder.py --source-kind claude --case-id <case-id> --response <claude-response.txt> --run-id <real-claude-run-id> --source-command <exact-claude-command-or-session-export> --json
- Intake validation command: python scripts/autopilot_frontier_model_evidence_intake.py --input-root project_ws/AgentOps/frontier_model_evidence_intake/raw_sources --allow-partial --json --no-write
- Publish scorecards command: python scripts/autopilot_frontier_model_evidence_intake.py --input-root project_ws/AgentOps/frontier_model_evidence_intake/raw_sources --publish-scorecards --json
- Success criteria: metadata.json, transcript.jsonl, prompt_pack.md, and raw candidate artifacts validate through the frontier source recorder.
- Permission boundary: evidence collection only; do not mutate source/tests, git, PR state, runtime, database, broker/API, deployment, release posture, or live trading.

## Operator Steps

1. Send the prompt pack listed above to the named model/source and ask it to answer every case in the pack.
2. Save the complete model response at `project_ws/AgentOps/frontier_model_evidence_intake/collection_packets/claude_all_cases_response.txt` outside the raw_sources folder.
3. Run the recommended recorder command first; it includes `--no-write` so parser and provenance failures surface before evidence is changed.
4. If the dry run passes, run the write/import recorder command. If the model only produced one case, use the single-case fallback command with `--case-id <case-id>`. Add `--drop-dir <drop-dir>` only when importing prebuilt raw drop files.
5. Run the intake validation command and confirm no-write readiness before promotion.
6. Run the publish scorecards command only after every required source is ready.
7. Use `--overwrite` only after reviewing existing evidence for that source; ready sources should not be replaced casually.

## All-Cases Response Contract

- Return exactly one JSON object per case, either as JSONL or objects inside a JSON array.
- Every object must include `source_kind: claude`, `model_name: claude-fable-5`, `case_id`, `candidate_id`, and `patch`.
- Include `planned_file`, `expected_changed_files`, and `declared_commands` exactly as listed in the case matrix when possible; CHILI verifies them when present.
- The `patch` must be a unified diff scoped to the planned file for that case.
- Empty or incomplete cases are allowed to be rejected by CHILI; do not invent validation results.
- Do not wrap the response in Markdown fences, PR summaries, readiness claims, or placeholder template values.

## Enforced Case Matrix

| Case | Planned file | Required command |
| --- | --- | --- |
| real-chili-preflight-candidate-wins | preflight.py | C:\Users\rindo\miniconda3\python.exe -m pytest test_preflight.py -q |
| real-chili-runtime-control-partial-loses | autopilot_prompt.py | C:\Users\rindo\miniconda3\python.exe -m pytest test_autopilot_prompt.py -q |
| real-chili-startup-static-partial-loses | startup_contracts.py | C:\Users\rindo\miniconda3\python.exe -m pytest test_startup_contracts.py -q |
| real-chili-broker-timeout-partial-loses | preflight.py | C:\Users\rindo\miniconda3\python.exe -m pytest test_preflight.py -q |
| real-chili-runtime-control-no-evidence-loses | autopilot_prompt.py | C:\Users\rindo\miniconda3\python.exe -m pytest test_autopilot_prompt.py -q |
| real-chili-runtime-control-unscoped-loses | autopilot_prompt.py | C:\Users\rindo\miniconda3\python.exe -m pytest test_autopilot_prompt.py -q |

## Post-Import Validation Loop

1. Dry-run parse and provenance recording:
   `python scripts/autopilot_frontier_source_evidence_recorder.py --source-kind claude --all-cases --response project_ws/AgentOps/frontier_model_evidence_intake/collection_packets/claude_all_cases_response.txt --run-id <real-claude-run-id> --source-command <exact-claude-command-or-session-export> --json --no-write`
2. Write/import only after the dry run passes:
   `python scripts/autopilot_frontier_source_evidence_recorder.py --source-kind claude --all-cases --response project_ws/AgentOps/frontier_model_evidence_intake/collection_packets/claude_all_cases_response.txt --run-id <real-claude-run-id> --source-command <exact-claude-command-or-session-export> --json`
3. Validate source readiness without writing:
   `python scripts/autopilot_frontier_model_evidence_intake.py --input-root project_ws/AgentOps/frontier_model_evidence_intake/raw_sources --allow-partial --json --no-write`
4. Publish scorecards only when all required sources are ready:
   `python scripts/autopilot_frontier_model_evidence_intake.py --input-root project_ws/AgentOps/frontier_model_evidence_intake/raw_sources --publish-scorecards --json`

## Required Transcript Evidence

- At least 3 non-empty JSONL events.
- Include source kind `claude` and model name `claude-fable-5`.
- Include the prompt-pack SHA-256, run id, case id, and final patch/drop decision.
- Claims about PR state, readiness, or current-head status are not promotion evidence.

## Missing Artifacts

- D:\dev\chili-home-copilot\project_ws\AgentOps\frontier_model_evidence_intake\raw_sources\claude\metadata.json
- D:\dev\chili-home-copilot\project_ws\AgentOps\frontier_model_evidence_intake\raw_sources\claude\transcript.jsonl
- D:\dev\chili-home-copilot\project_ws\AgentOps\frontier_model_evidence_intake\raw_sources\claude\raw\*
