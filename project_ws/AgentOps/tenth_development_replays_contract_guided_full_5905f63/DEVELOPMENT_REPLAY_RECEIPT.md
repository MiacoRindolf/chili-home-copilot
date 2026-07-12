# Tenth-Suite Contract-Guided Development Replay Receipt

Run completed: 2026-07-12T17:24:49.847014+00:00

## Classification

This is a **disclosed development replay** of the previously scored tenth diagnosis-to-fix suite. The fixture
has already informed CHILI development. The generated report retains the fixture manifest's historical
`blinded_holdout` evaluation label, but this replay is not untouched, blinded, or eligible for a Fable 5 parity
claim. It does not replace the original untouched tenth-suite result of 68.75/100 and 2/8 sealed-final passes.

## Frozen Inputs

- Source commit: `5905f636ae6128f445fe88057188f40aa246fd32`
- Source tree: `9562d86be690c9b2f14efc1080ea8843a6ddbfcf`
- Fixture tree: `f3c327cacdfba373dce2b635ad0c2db576abd667`
- Author receipt SHA-256: `b5606c05da563c01c375d0a7e64c3739d81b1d9f795659d9b69fbef9ada8a840`
- Results schema: `chili.diagnosis-to-fix-results.v5`

## Frozen Policy

- Primary model: local `qwen2.5-coder:7b`
- Primary repair rounds: 2
- Final escalation model: local `qwen2.5-coder:14b`
- Escalation repair rounds: 1
- Per-call timeout: 240 seconds
- Premium calls allowed: 0
- Sealed-final weight: 45/100
- Maximum score without sealed-final pass: 55/100
- Runner/source changes during execution: none

## Result

- Overall: **58.12/100**, `needs_improvement`
- Sealed-final functional solves: **2/8 (25%)**
- Correct causal families: **6/8 (75%)**
- Exact changed-file sets: **5/8 (62.5%)**
- Public regressions preserved at final state: **8/8**
- Premium calls: **0**
- Local calls: **168** total, 129 primary and 39 escalation
- Wall time: **6,334.5 seconds (105.6 minutes)**
- Average case time: **791.8 seconds (13.2 minutes)**
- Successful cases: `ts_http_vary_isolation`, `sql_tenant_grant_intervals`

## Failure Audit

| Case | Development result | Mechanism gap |
|---|---|---|
| `py_relay_rotation_window` | 30; final failed | Correct dependency diagnosis was later overwritten by `code`; repeated-query ordering and conditional second/millisecond timestamp handling remained incompatible. |
| `py_reservation_retry_scope` | 55; feedback passed, final failed | Visible retry contracts passed, but sealed parallel-tenant retries produced under-counted independent effects. |
| `ts_retry_budget_clock` | 30; final failed | Correct clock family was overwritten by `code`; an invalid mixed edit format caused the coordinated scheduler group to roll back, leaving only a partial parser edit. |
| `dart_offline_tombstone_join` | 40; no retained patch | The causal family was correct, but generated Dart used undefined `max` calls and a dynamically typed map; bounded compiler correction failed and restored the group. |
| `dart_resumable_chunk_boundaries` | 55; final failed | Diagnosis and owners were correct, but adjacent inclusive ranges were rejected and sealed upload completion never became true. |
| `sql_telemetry_correction_rollup` | 55; final failed | Diagnosis and owners were correct, but stale metadata survived correction and one site was bucketed by the wrong timestamp. |

## Artifact Integrity

- `DEVELOPMENT_REPLAY.md` SHA-256: `89a0b42b38608e5e5d2135b22574de332e9d2c76457099789ae590882a87b93e`
- `development_replay.json` SHA-256: `8b089c3ee6b870574a8c944cb0736fbf97d65e1f047eee3abee658a1b821d4b1`

## Interpretation

The replay demonstrates stronger local causal-family recognition and two reproducible multi-file solves without
premium models. It also shows that repair synthesis, diagnosis-revision discipline, Dart compiler recovery,
sealed-contract generalization, and latency remain materially below the requested Fable 5 replacement standard.
A fresh independently authored suite is required after generic mechanism repair; this disclosed replay cannot be
used as that gate.
