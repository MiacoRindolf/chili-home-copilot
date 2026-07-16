# PR 895 Hosted PR Repair Evidence

- Schema: chili.hosted-pr-local-repair-evidence.v1
- Generated UTC: 2026-07-10T07:02:00Z
- PR: https://github.com/MiacoRindolf/chili-home-copilot/pull/895
- Title: Add hosted PR repair evidence harness
- Branch: codex/hosted-pr-receipt-proof-20260710
- Initial review head SHA: d2f7d0dbfbe2cba4695ef76af8f3a4a71a74bcba
- Repaired head SHA: 7737c7e08585dcdcab75cde391c8cd9c091eefd4
- Current head SHA observed: 7737c7e08585dcdcab75cde391c8cd9c091eefd4
- Current hosted green run observed: 86305663655
- Hosted workflow run observed: 29075349301
- Hosted job observed: 86305663655
- Hosted workflow: CI
- Hosted check: hosted-pr-repair-proof
- Hosted conclusion: success
- Hosted run URL: https://github.com/MiacoRindolf/chili-home-copilot/actions/runs/29075349301
- Hosted job URL: https://github.com/MiacoRindolf/chili-home-copilot/actions/runs/29075349301/job/86305663655
- Review thread id: PRRT_kwDORbf5rs6PyiYn
- Line-thread comment id: PRRC_kwDORbf5rs7UADSb
- Evidence status: real_review_thread_repair_verified; focused hosted check success observed
- Promotion status: candidate real_inventory artifact assembled for hosted PR repair benchmark.

## Repair Sequence

- Initial PR commit added the hosted PR repair evidence harness.
- Hosted line review comment requested replacing a manifest-embedded check receipt with a collected post_repair_check_receipt.json file.
- Repair commits bound the collector template to post_repair_check_receipt_file, added regressions for file-backed receipt collection, accepted BOM-marked evidence JSON/JSONL, and surfaced real artifact provenance in the scorecard.
- Focused GitHub Actions job hosted-pr-repair-proof completed successfully for the current head.
