# CHILI Hosted PR Repair Evidence Collection

- Schema: chili.hosted-pr-repair-evidence-collector.v1
- Generated UTC: 2026-07-10T06:15:06.999554Z
- Candidate report: project_ws/AgentOps/PR_282_CI_REPAIR.md
- PR: https://github.com/MiacoRindolf/chili-home-copilot/pull/282
- Permission boundary: local evidence staging only; no git/PR mutation, runtime restart, deploy, database, broker, or live-trading action.

## Required Files

- review_thread_transcript.jsonl
- publication_transcript.jsonl
- post_repair_check_receipt.json
- source_manifest.json

## Next Commands

- Assemble: `python scripts/autopilot_hosted_pr_repair_artifact_assembler.py --evidence-dir project_ws/AgentOps/hosted_pr_repair_evidence/pr-282 --json`
- Validate: `python scripts/autopilot_hosted_pr_repair_artifact_benchmark.py --artifact-dir project_ws/AgentOps/hosted_pr_repair_evidence/pr-282/artifact --json`
