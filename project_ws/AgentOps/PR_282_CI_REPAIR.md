# PR 282 CI Repair Evidence

- Schema: chili.hosted-pr-local-repair-evidence.v1
- Generated UTC: 2026-07-03T10:45:00Z
- PR: https://github.com/MiacoRindolf/chili-home-copilot/pull/282
- Title: [codex] Gate weak stock momentum under queue pressure
- State: MERGED
- Branch: codex/stock-momentum-context-gate
- Base: main
- Current head SHA observed: 6160d0f82d749fc04d0f74ea7030d2fd482b3e6d
- Merged UTC: 2026-06-03T11:12:06Z
- Current hosted green run observed: 26879809423
- Hosted job observed: 79276630311
- Hosted workflow: CI
- Hosted check: test
- Hosted conclusion: success
- Hosted run URL: https://github.com/MiacoRindolf/chili-home-copilot/actions/runs/26879809423
- Hosted job URL: https://github.com/MiacoRindolf/chili-home-copilot/actions/runs/26879809423/job/79276630311
- Evidence status: local_repair_verified; current hosted check success observed; review/comment transcript missing
- Promotion status: not real_inventory; review-thread/current-head proof has not been replayed through the transcript-bound hosted PR repair artifact contract.

## Hosted Proof Observed

- `gh run view 26879809423 --repo MiacoRindolf/chili-home-copilot --json databaseId,headSha,headBranch,status,conclusion,createdAt,updatedAt,url,event,displayTitle,workflowName,jobs` returned `conclusion=success`, `status=completed`, `headSha=6160d0f82d749fc04d0f74ea7030d2fd482b3e6d`, and one successful `test` job.
- `gh pr view 282 --repo MiacoRindolf/chili-home-copilot --json number,url,title,state,headRefName,headRefOid,baseRefName,mergedAt,comments,reviews,latestReviews,statusCheckRollup,files` returned the merged PR, head SHA, changed-file list, and a single successful CI check run.
- `gh api repos/MiacoRindolf/chili-home-copilot/commits/6160d0f82d749fc04d0f74ea7030d2fd482b3e6d/check-runs` returned check run `79276630311` with `conclusion=success`.

## Missing Hosted Evidence

- REST review comments for PR 282 were empty: `gh api repos/MiacoRindolf/chili-home-copilot/pulls/282/comments --paginate` returned `[]`.
- REST issue comments for PR 282 were empty: `gh api repos/MiacoRindolf/chili-home-copilot/issues/282/comments --paginate` returned `[]`.
- GraphQL review threads for PR 282 were empty: `reviewThreads.totalCount=0`.
- Because no review-thread transcript or line-thread detail exists for this PR, this report is a candidate lead only. It must not satisfy `HOSTED_PR_REPAIR_ARTIFACT_BENCHMARK.md` until a real transcript-bound artifact is collected for a suitable PR.

## Next Action

- Use this report to build the hosted PR repair collection packet and evidence skeleton.
- Find or create a future hosted PR repair with an actual review thread, line comment, current-head publication proof, and green post-repair GitHub Actions run.
- Replay that real inventory through `python scripts/autopilot_hosted_pr_repair_artifact_benchmark.py --artifact-dir project_ws/AgentOps/hosted_pr_repair_evidence/<pr-slug>/artifact --json`.
