# CHILI Model Shadow Evidence Benchmark

- Schema: chili.model-shadow-evidence-benchmark.v1
- Generated UTC: 2026-07-10T23:09:06.016044Z
- Status: passed
- Target score: 100
- Evidence mode: real_manifest
- Checks: 7
- Average score: 100/100
- Required source kinds: codex, claude, local_model
- Required frontier model targets: codex=gpt-5.6-sol, claude=fable-5
- Source kinds: claude, codex, local_model
- Missing source kinds: none
- Manifests: 3
- Cases: 18
- Required checks: valid_multi_source_shadow_accepts, self_test_manifest_rejected, synthetic_model_rejected, wrong_frontier_model_rejected, missing_source_rejected, unverified_provenance_rejected, sparse_transcript_rejected
- Missing checks: none
- Required behavior: synthetic, self-test, incomplete, or unverified model-run bundles must not count as real frontier shadow evidence.
- Safety: deterministic manifest/hash validation only; no model calls, git action, runtime restart, deployment, database migration, broker call, or live-trading action.

| Check | Expected | Actual | Score | Evidence |
| --- | --- | --- | ---: | --- |
| valid_multi_source_shadow_accepts | accepted | accepted | 100 | validated_shadow_evidence=True; sources=claude,codex,local_model; manifests=3; cases=18 |
| self_test_manifest_rejected | rejected | rejected | 100 | manifest[1].run_id looks synthetic: collector-self-test |
| synthetic_model_rejected | rejected | rejected | 100 | manifest[1].model_name looks synthetic: synthetic-codex-fixture |
| wrong_frontier_model_rejected | rejected | rejected | 100 | manifest[2].model_name must identify required frontier target claude=fable-5; got claude-sonnet-shadow |
| missing_source_rejected | rejected | rejected | 100 | missing source kinds: local_model |
| unverified_provenance_rejected | rejected | rejected | 100 | manifest[1].validated_with_provenance must be true |
| sparse_transcript_rejected | rejected | rejected | 100 | manifest[1].transcript_file must contain at least 3 non-empty events |
