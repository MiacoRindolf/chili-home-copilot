# CHILI Model Shadow Evidence Benchmark

- Schema: chili.model-shadow-evidence-benchmark.v1
- Generated UTC: 2026-07-10T12:20:49.334010Z
- Status: failed
- Target score: 100
- Evidence mode: partial_real_manifest
- Checks: 7
- Average score: 86/100
- Required source kinds: codex, claude, local_model
- Required frontier model targets: codex=gpt-5.5, claude=opus-4.8
- Source kinds: codex, local_model
- Missing source kinds: claude
- Manifests: 2
- Cases: 12
- Required checks: valid_multi_source_shadow_accepts, self_test_manifest_rejected, synthetic_model_rejected, wrong_frontier_model_rejected, missing_source_rejected, unverified_provenance_rejected, sparse_transcript_rejected
- Missing checks: none
- Required behavior: synthetic, self-test, incomplete, or unverified model-run bundles must not count as real frontier shadow evidence.
- Safety: deterministic manifest/hash validation only; no model calls, git action, runtime restart, deployment, database migration, broker call, or live-trading action.

| Check | Expected | Actual | Score | Evidence |
| --- | --- | --- | ---: | --- |
| valid_multi_source_shadow_accepts | accepted | accepted | 0 | validated_shadow_evidence=False; sources=codex,local_model; manifests=2; cases=12 |
| self_test_manifest_rejected | rejected | rejected | 100 | manifest[1].run_id looks synthetic: collector-self-test |
| synthetic_model_rejected | rejected | rejected | 100 | manifest[1].model_name looks synthetic: synthetic-codex-fixture |
| wrong_frontier_model_rejected | rejected | rejected | 100 | manifest[2].model_name must identify required frontier target claude=opus-4.8; got claude-sonnet-shadow |
| missing_source_rejected | rejected | rejected | 100 | missing source kinds: local_model |
| unverified_provenance_rejected | rejected | rejected | 100 | manifest[1].validated_with_provenance must be true |
| sparse_transcript_rejected | rejected | rejected | 100 | manifest[1].transcript_file must contain at least 3 non-empty events |
