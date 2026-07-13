# Fable 5-Class Eleventh Suite Disclosed Repair

Date: 2026-07-12

## Evidence Boundary

- Authoritative untouched result: **32.92/100**, **0/12** sealed-final solves, **4/12** diagnosis families, and **1/12** exact owner sets.
- Untouched source: `6cf5b7e0e7da6840da57dec678f8846796265091`.
- Preserved result commit: `be18e0b7a2bd9e404990ebdc96b73127fbfad5da`.
- Disclosed repair commit: `9b69cd8db6d9a97717422202e751b762b7ff7dc7`.
- Premium calls in the repaired autonomy paths: **0 allowed**.

This document records development work after the eleventh cases and final oracles were disclosed. It does not
rescore, overwrite, or weaken the untouched result.

## Causal Repairs

1. Contract recognition no longer requires pre-existing invariant warnings before structural operators may inspect source. Warnings now guard the projected rewrite.
2. Generic structural operators cover stable ordered-sequence identity, class/private-field async rejection-slot eviction, and cross-file monotonic SQL head/read repair. Ambiguous owner or ordering shapes abstain.
3. Repair plans must cover every failed contract with selected source owners. Required multi-file production repair groups fail atomically instead of returning partial success.
4. Incomplete fail-fast test inventories treat omitted contracts as unknown; explicit pass-to-fail and complete-inventory omissions still regress.
5. Diagnostic production plans expose contract-to-owner postconditions and can cover up to four coordinated source owners.
6. Empty/invalid plans lose edit authority immediately. Empty transport responses do not trigger adapter retries.
7. Escalation skips generative review and model recovery retries, caps production generation at 4,096 tokens, and shares a 690-second per-case local-model wall budget in the benchmark.
8. Ollama fallback uses one total deadline and remembers the working host per model. A reachable-host timeout stops fallback.
9. Benchmark reporting separates JSON-valid stages from causally accepted diagnoses.

## Disclosed Replay

- `node-cache-locale-order-retry-slot`: repaired `src/catalog-cache-key.mjs` and `src/async-slot-cache.mjs`; public, repair-feedback, and isolated final tests passed.
- `sql_out_of_order_document_heads`: repaired `sql/002_head_triggers.sql` and `sql/read_current.sql`; public, repair-feedback, and isolated final tests passed.
- SQL recognition requires a unique parent-scoped authoritative order in schema evidence and abstains when order or owner signals conflict.

## Validation

- Broad affected suites: **183 passed**.
- Focused production autonomy repair suite: **14 passed**, 109 deselected.
- Disclosed eleventh benchmark replay: **2/2 cases passed public, repair-feedback, and isolated final adjudication**.
- Python compilation: passed for all changed Python modules and tests.
- `git diff --check`: passed.
- Known warnings only: existing SQLAlchemy cycle warning and existing `datetime.utcnow()` deprecation warning.

## Current Verdict

CHILI is now more bounded, more honest about causal evidence, and less dependent on slow local specialist retries.
It has development proof for two eleventh transfer mechanisms. It is still **not** a proven Fable 5 replacement
for unseen real-world complex diagnosis. The next gate is a newly authored, independently validated, untouched
multi-repository suite followed by an authenticated same-task Fable 5 comparison.
