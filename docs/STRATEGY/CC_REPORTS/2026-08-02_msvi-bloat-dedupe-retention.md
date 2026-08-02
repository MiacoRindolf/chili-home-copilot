# CC_REPORT: msvi-bloat-dedupe-retention

Operator-directed task (chat brief, not NEXT_TASK.md): `momentum_symbol_viability`
was 7.8 GB for ~122k live rows and was the IO-pressure source behind the
2026-08-02 momentum-desk endpoint hang (the hang itself was fixed by mig356 +
read-model rewrite on `claude/happy-solomon-b9179d`, still unmerged).

## What shipped

- **mig357** (`357_momentum_viability_index_dedupe_and_autovacuum`):
  - Drops `ix_momentum_symbol_viability_id` (full duplicate of the pkey).
  - Drops `ix_msvi_corr` / `ix_msvi_freshness` — exact duplicates of the
    model-named `ix_momentum_symbol_viability_{correlation_id,freshness_ts}` —
    but ONLY when the model-named twin exists, so a prod-shaped fresh install
    (table created by migration, never `create_all`) keeps its sole copy.
  - Pins absolute-threshold autovacuum on the table AND its TOAST relation
    (scale_factor 0 / threshold 10k / cost_delay 0) — mig301 precedent. 10k ≈
    one active day's row churn (measured 1-7d freshness cohort ~26k rows).
- **Model**: `MomentumSymbolViability.id` no longer declares `index=True`
  (that was what kept recreating the pkey duplicate via `create_all`).
- **Retention**: `_slim_stale_viability_snapshots` in `data_retention.py`,
  wired into `run_retention_policy` as `viability_snapshots`. Rows whose
  `freshness_ts` is older than `brain_retention_viability_snapshot_days`
  (new setting, default 30 — mirrors the viability-history TTL) get their four
  JSONB snapshot columns reset to `{}` in per-batch-committed loops
  (5k/batch, 200k/sweep cap). Rows are KEPT — scalars survive; the producer
  upsert (`persistence.py` `on_conflict_do_update`) overwrites all four
  columns the moment a symbol re-enters the universe, and `{}` is the columns'
  NOT NULL birth default, so every reader tolerates it by construction.
- **Script**: `scripts/reclaim_momentum_viability.sql` — one-time
  VACUUM FULL reclaim runbook (mirrors `reclaim_exit_parity_log.sql`), with a
  pg_repack note for a future no-lock variant.
- **Tests**: 4 new tests in `tests/test_operational_storage_retention.py`
  incl. a DB-backed functional test of the slim (stale slimmed / fresh + scalars
  untouched / convergent second sweep).

## Verification (prod, executed 2026-08-02 ~21:40–21:50 UTC, Sunday window)

- Baseline measured: 7,865 MB total = 1,681 MB heap + 5,810 MB TOAST + 374 MB
  indexes for 121,864 rows; live JSONB payload only ~477 MB → >90% of TOAST was
  dead-tuple churn bloat. 16 indexes, `n_tup_hot_upd=0` over 1.1M updates.
- Applied mig357 statements manually (mig356 precedent — the migration no-ops
  at next deploy; `schema_version` row intentionally NOT inserted): 16 → 13
  indexes, reloptions pinned on heap + TOAST (verified via `pg_class`).
- Ran the slim through the real code path: dry-run 79,705 eligible → live run
  slimmed 79,705 in 16 batches, 122s. Matches the >30d cohort exactly.
- `VACUUM (FULL, ANALYZE)`: 1m48s. **7,865 MB → 332 MB** (heap 72 MB, TOAST
  241 MB, indexes 20 MB). DB 120 → 112 GB. Row count preserved (121,864).
- Writers resumed immediately post-lock (580 rows updated in the next 10 min;
  latest tick 21:49). No stuck queries in `pg_stat_activity`.
- `/api/trading/brain/momentum/summary`: 200 in 5.9s (was hanging >45s).
  Remaining latency is the OLD read-model still deployed — the happy-solomon
  branch's single-pass rewrite is the other half.
- `pytest tests/test_operational_storage_retention.py`: 11 passed.
- `verify-migration-ids.ps1`: PASS (345 migrations, no collisions). Needs
  `DATABASE_URL` set in the shell or it dies on Settings import — pre-existing.

## Surprises / deviations

- The brief framed this as "~48KB/row TOAST × 122k rows"; measurement showed
  the live payload was only ~477 MB — the 5.8 GB TOAST was overwhelmingly
  dead-tuple bloat. So the reclaim (VACUUM FULL) + recurrence prevention
  (autovacuum pins, index dedupe, ongoing slim) mattered more than the
  retention window itself. All were done.
- REINDEX CONCURRENTLY was unnecessary — VACUUUM FULL rebuilds all indexes as
  part of the rewrite (374 → 20 MB).
- Migration ID 356 is claimed by the unmerged desk-hang branch; this work
  registers as 357 with a comment reserving 356. Expect a trivial MIGRATIONS
  list-tail merge conflict when happy-solomon merges.

## Deferred

- `ix_msvi_symbol_updated` (0 scans) and `ix_momentum_symbol_viability_symbol`
  (redundant prefix of the sym_var unique) look droppable too, but they are
  NOT exact duplicates — left for a scan-stat review after a week of normal
  operation rather than bundling risk into this change.
- pg_repack install (no-lock future reclaims) — not worth the image churn for
  a sub-minute rewrite; documented in the script instead.

## Open questions for Cowork

- Merge `claude/happy-solomon-b9179d` (mig356 + read-model rewrite) soon — the
  endpoint's remaining 5.9s is that branch's half of the fix, and its mig356
  reserves the ID this work skips.
