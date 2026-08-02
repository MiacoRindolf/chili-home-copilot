# CC report — momentum desk/summary endpoint hang fix (2026-08-02)

Operator-directed task (not the queued NEXT_TASK): `GET /api/trading/brain/momentum/summary`
and `/momentum/desk` hung (no response in 45s, HTTP 000) while `/health` and `/paper`
answered — the operator was blind in the Live UI to the running captured-paper PAPER lane.

## Root cause (measured on prod `chili`)

Both endpoints (and `/api/trading/brain/graph` via `projection.py`) funnel into
`build_momentum_neural_graph_context` → `_viability_durable_stats`, which ran **5 statements
per request** against `momentum_symbol_viability` — a churn-bloated table (121,864 live rows;
**1.6GB heap + 5.8GB TOAST + 370MB indexes**, ~48KB/row of JSONB snapshots):

| statement | before | after |
|---|---|---|
| top-5 `ORDER BY viability_score DESC` (no index → seq scan + sort) | **48,762ms** | 3.1ms |
| `paper_eligible AND NOT live_eligible` count (no index → seq scan) | 7,119ms | — |
| plain count + `live_eligible` count | ~2,200ms | 685ms (one FILTER agg) |
| `freshness_ts >= 24h` count (indexed) | 10ms | 10ms |

≈57s+ per request, with UI pile-on worsening IO contention → the observed hang.
Secondary cost: `_outcome_windows` + `evolution_credit_diagnostics` loaded thousands of
full ORM rows, detoasting ~40MB of JSONB per request that the read-model never reads.

## Fix (PR: momentum desk read-model)

1. **mig356** — `ix_msvi_score_desc (viability_score DESC)` + `ix_msvi_eligibility
   (live_eligible, paper_eligible)`. Applied to prod ahead of deploy via
   `CREATE INDEX CONCURRENTLY` (no write-block on the live lane); the idempotent
   migration no-ops at next startup.
2. **`_viability_durable_stats`** — 3 counts → one index-only FILTER aggregate; top-5
   selects scalar columns only (no JSONB detoast). Statements kept separate per index
   on purpose (a combined aggregate would force a seq scan).
3. **Timeout correctness** — `SET LOCAL statement_timeout = 5000` armed at the top of
   the read-model (documented base setting); on any statement failure the section
   degrades in-payload and `_recover_session` rolls back the aborted txn + re-arms,
   so one slow/broken section can never 500 or wedge the endpoint again.
4. **Column pruning** — `_outcome_windows` paper/live slices select only
   `(return_bps, evidence_weight)`; `evolution_credit_diagnostics` uses `load_only`
   (keeps `extracted_summary_json`, skips the 7 other JSONB snapshot columns).

## Verification

- `tests/test_momentum_brain_desk_read_model.py` (4 new): single-pass counts, payload
  shape, **degrade-on-poisoned-txn with session recovery**, txn-scoped timeout arming.
  All pass vs `chili_test`; 19 existing tests over the touched modules also pass.
- Live endpoints recovered immediately after the concurrent index build (old deployed
  code, new indexes): HTTP 200 in ~11-13s (was: timeout at 45s).
- Fixed code timed read-only against prod `chili`: **cold 1.54s, warm 0.12-0.18s** —
  inside the ~2s target. Container picks this up at next web redeploy.

## Follow-up worth queuing (not in this change)

`momentum_symbol_viability` is 7.8GB for 122k rows: JSONB snapshot columns average
~48KB/row and update churn bloats heap + indexes (the pkey index alone is ~170MB for a
121k-row table; `ix_momentum_symbol_viability_id`, `ix_msvi_corr`, `ix_msvi_freshness`
duplicate other indexes and amplify every write). A retention/slim-down pass like the
exit-parity one (#494/mig301) would shrink IO pressure across every consumer.
