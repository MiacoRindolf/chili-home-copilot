# CHILI Hosted PR Repair Artifact Benchmark

- Schema: chili.hosted-pr-repair-artifact-benchmark.v1
- Generated UTC: 2026-07-10T07:02:16.418930Z
- Status: passed
- Target score: 100
- Evidence mode: real_inventory
- Checks: 18
- Average score: 100/100
- Artifacts: 1
- Artifact PRs: https://github.com/MiacoRindolf/chili-home-copilot/pull/895
- Artifact source runs: gh-pr895-hosted-repair-20260710T0702Z
- Promotion eligible: true
- Required checks: valid_hosted_pr_repair_accepts, self_test_artifact_rejected, missing_review_thread_transcript_rejected, sparse_review_transcript_rejected, review_transcript_pr_mismatch_rejected, review_transcript_thread_detail_mismatch_rejected, missing_line_thread_rejected, missing_remote_publication_rejected, post_repair_head_mismatch_rejected, missing_post_repair_check_receipt_rejected, transcript_hash_mismatch_rejected, sparse_publication_transcript_rejected, publication_transcript_pr_mismatch_rejected, publication_transcript_commit_mismatch_rejected, valid_artifact_inventory_accepts, empty_artifact_inventory_rejected, duplicate_pr_artifact_rejected, duplicate_source_run_rejected
- Missing checks: none
- Required behavior: hosted PR repair promotion must be backed by transcript-bound review, publication, current-head, and hosted green-check receipts.
- Safety: local artifact/hash validation only; no git action, PR mutation, runtime restart, deployment, database migration, broker call, or live-trading action.

| Check | Expected | Actual | Score | Evidence |
| --- | --- | --- | ---: | --- |
| valid_hosted_pr_repair_accepts | accepted | accepted | 100 | validated_inventory=True; artifacts=1; prs=https://github.com/MiacoRindolf/chili-home-copilot/pull/282 |
| self_test_artifact_rejected | rejected | rejected | 100 | inventory.evidence_mode must be real_inventory |
| missing_review_thread_transcript_rejected | rejected | rejected | 100 | inventory.artifacts[1].review_thread_transcript must be an object |
| sparse_review_transcript_rejected | rejected | rejected | 100 | inventory.artifacts[1].review_thread_transcript must contain at least 3 transcript events |
| review_transcript_pr_mismatch_rejected | rejected | rejected | 100 | inventory.artifacts[1].review_thread_transcript.pr_url mismatch |
| review_transcript_thread_detail_mismatch_rejected | rejected | rejected | 100 | inventory.artifacts[1].review_thread_transcript.thread_id mismatch |
| missing_line_thread_rejected | rejected | rejected | 100 | inventory.artifacts[1].line_thread must be an object |
| missing_remote_publication_rejected | rejected | rejected | 100 | inventory.artifacts[1].remote_publication must be an object |
| post_repair_head_mismatch_rejected | rejected | rejected | 100 | inventory.artifacts[1].post_repair_head_sha must match current_head_sha_observed |
| missing_post_repair_check_receipt_rejected | rejected | rejected | 100 | inventory.artifacts[1].post_repair_check_receipt must be an object |
| transcript_hash_mismatch_rejected | rejected | rejected | 100 | inventory.artifacts[1].review_thread_transcript.sha256 mismatch |
| sparse_publication_transcript_rejected | rejected | rejected | 100 | inventory.artifacts[1].publication_transcript must contain at least 3 transcript events |
| publication_transcript_pr_mismatch_rejected | rejected | rejected | 100 | inventory.artifacts[1].publication_transcript.pr_url mismatch |
| publication_transcript_commit_mismatch_rejected | rejected | rejected | 100 | inventory.artifacts[1].publication_transcript.commit_sha mismatch |
| valid_artifact_inventory_accepts | accepted | accepted | 100 | validated_inventory=True; artifacts=1; prs=https://github.com/MiacoRindolf/chili-home-copilot/pull/282 |
| empty_artifact_inventory_rejected | rejected | rejected | 100 | inventory.artifacts must be a non-empty list |
| duplicate_pr_artifact_rejected | rejected | rejected | 100 | duplicate PR artifact: https://github.com/MiacoRindolf/chili-home-copilot/pull/282 |
| duplicate_source_run_rejected | rejected | rejected | 100 | duplicate source_run_id: gh-pr282-repair-20260603T1112Z |
