# Cross-Case Semantic Audit

**Verdict: REJECT**

The four permitted lane results contain 12 unique case IDs and the exact required dimension distribution: `code=1`, `clock=2`, `config=2`, `data=2`, `dependency=2`, `runtime=1`, `state=2`.

The recorded mechanisms, feedback boundaries, and final boundaries are semantically distinct across all 12. Every lane records `PASS`; its failed/incomplete required-gate collection is empty, and every recorded final-novelty gate passes. Review against the common 12-family prohibited set found no material overlap. In particular, the two clock cases use different calendar/offset semantics, the two state cases use attempt fencing versus guarded SQL transitions, the two config cases use media-type parsing versus delimiter-aware rendering, and the two data cases use missing-value rollup versus unit conversion.

## Exact Reject Findings

1. **Cross-case source-skeleton uniqueness is incomplete.** Source-skeleton SHA-256 values exist only for the three Python cases. Node and SQL provide textual skeleton descriptions without skeleton hashes. Dart provides neither per-case skeleton descriptions nor skeleton hashes. Consequently, exact and materially equivalent source-skeleton checks cannot be independently completed across all 12 using only the permitted files.
2. **Cross-case assertion-family uniqueness is incomplete.** Dart does not record per-case `assertion_family` values. Its failure messages and novelty summaries describe boundaries but do not supply the requested assertion-family records, so all-12 assertion-family uniqueness cannot be independently completed.

All other requested checks completed successfully. The detailed extraction and null evidence fields are in `semantic_result.json`.
