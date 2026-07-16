# CHILI Hosted PR Repair Collection Packet

- Schema: chili.hosted-pr-repair-collection-packet.v1
- Generated UTC: 2026-07-10T06:15:06.958048Z
- Candidate report: project_ws/AgentOps/PR_282_CI_REPAIR.md
- PR: https://github.com/MiacoRindolf/chili-home-copilot/pull/282
- Branch: codex/stock-momentum-context-gate
- Current head SHA observed: 6160d0f82d749fc04d0f74ea7030d2fd482b3e6d
- Current hosted green run observed: 26879809423
- Permission boundary: evidence collection and local validation only; no git/PR mutation, runtime restart, deploy, database, broker, or live-trading action.

## Required Evidence Files

| File | Purpose |
| --- | --- |
| review_thread_transcript.jsonl | review thread and line-comment transcript bound to PR URL and thread id |
| publication_transcript.jsonl | publication/current-head transcript bound to PR URL and repaired commit |
| post_repair_check_receipt.json | hosted check receipt bound to current head and green run id |
| source_manifest.json | operator-filled manifest that names the collected files and IDs |

## Commands

- Collect evidence checklist: `python scripts/autopilot_hosted_pr_repair_evidence_collector.py --candidate-report project_ws/AgentOps/PR_282_CI_REPAIR.md --output-dir project_ws/AgentOps/hosted_pr_repair_evidence/<pr-slug> --json`
- Assemble artifact inventory: `python scripts/autopilot_hosted_pr_repair_artifact_assembler.py --evidence-dir project_ws/AgentOps/hosted_pr_repair_evidence/<pr-slug> --json`
- Validate real inventory: `python scripts/autopilot_hosted_pr_repair_artifact_benchmark.py --artifact-dir project_ws/AgentOps/hosted_pr_repair_evidence/<pr-slug>/artifact --json`

## Operator Fill-Ins

- PR URL: https://github.com/MiacoRindolf/chili-home-copilot/pull/282
- Branch: codex/stock-momentum-context-gate
- Post-repair/current head SHA: 6160d0f82d749fc04d0f74ea7030d2fd482b3e6d
- Current hosted green run id: 26879809423
- Review thread id: <review-thread-id>
- Line-thread comment id: <line-thread-comment-id>
