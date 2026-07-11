# Leakage-Safe Diagnostic Memory

Date: 2026-07-11

## Purpose

CHILI can now reuse validated diagnostic mechanisms without replaying a prior incident, hidden answer, or premium-model response. The memory is a system capability around the local model, not a larger-model wrapper.

## Promotion Boundary

- Normal Project Autonomy runs may promote a mechanism only when the diagnosis is confirmed, validation passes, and the run followed the explicit operator plan-approval path.
- That automatic classification is `operator_validated`, never `production_validated`.
- `production_validated` is accepted only when a trusted caller explicitly supplies it; ordinary unit-test success is not relabeled as production proof.
- Full-autopilot local validation cannot promote its own diagnosis.
- Skipped-only, timed-out, malformed, or missing-exit-code validation cannot promote a diagnosis.
- Lower-trust operator validation cannot supersede an existing production-validated mechanism.
- `blinded_holdout`, `development_replay`, `historical_fable_answer`, `sealed_oracle`, and `synthetic_evaluation` are forbidden retrieval classes.
- `CHILI_PROJECT_AUTOPILOT_EVALUATION_MODE=true` hard-disables retrieval before any memory query.

## Stored Abstraction

The durable row contains a controlled diagnostic dimension, controlled event family, bounded flow classification, allowlisted diagnostic lenses, generic contract topics, an abstract lesson, safe retrieval terms, validation status, outcome, mechanism key, source revision, and supersession metadata.

It does not store the source prompt, free-form conclusion claim, raw evidence, raw contract sentence, oracle, Fable response, file path, correlation ID, or incident secret. Event and flow labels are mapped to controlled families before storage. Retrieval reconstructs its public lesson from allowlisted fields rather than trusting stored free-form text. Older lessons remain auditable but are demoted when an equal- or higher-trust validated lesson with the same mechanism supersedes them; a repeated final run updates the existing row's outcome without duplicating the memory.

## Retrieval Boundary

- The SQL query itself requires the same non-null user and repository on both the memory row and source run.
- Payload scope is rechecked after the database filter to catch tampering.
- PostgreSQL uses a transaction-scoped advisory lock keyed by user, repository, and mechanism so concurrent workers cannot create two active heads. Predecessor lookup filters by the exact mechanism key rather than scanning only a recent row window.
- Missing user or repository scope disables memory rather than creating a shared null-tenant pool.
- Only promoted, validation-passed, non-superseded abstractions with approved evidence classification are eligible.
- A positive lexical or dimension overlap is required; unrelated memories are excluded.
- At most four public abstractions enter the diagnostic case. Source prompts and source run IDs are not in that public object.
- Retrieval and recording decisions are written as audit artifacts.

## Validation

- Focused memory and planning integration: **15 passed**.
- Broad autonomy, routing, identity, safety, repair, API, and coding-workflow regression coverage: **334 passed** in one combined run.
- PostgreSQL test-database advisory-lock smoke: acquired successfully and rolled back without mutation.
- Disclosed second-slice heuristic replay remained **100/100**. This is development evidence, not a new holdout.
- Premium calls: **0**.
- Runtime, broker, trading, and container state changed: **none**.

## Remaining Boundary

Retrieval is deliberately same-repository and lexical; it is not yet a semantic case library or cross-repository transfer learner. Production validation still requires an explicit trusted caller. This feature reduces repeated diagnostic work and contamination risk, but it does not establish broad Fable 5 parity. A new sealed post-freeze holdout remains required.
