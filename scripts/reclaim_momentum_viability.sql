-- reclaim_momentum_viability.sql
--
-- ONE-TIME heap/TOAST reclaim for momentum_symbol_viability.
--
-- WHY: the table held 7.8 GB for ~122k live rows (measured 2026-08-02:
-- 1.68 GB heap + 5.8 GB TOAST + 374 MB indexes) against only ~477 MB of
-- LIVE JSONB payload — >90% accumulated dead-tuple bloat from viability-tick
-- UPSERT churn (zero HOT updates: freshness_ts is indexed and changes every
-- tick). Migration 357 stops the index write-amplification (3 duplicate
-- indexes dropped) and pins absolute-threshold autovacuum incl. TOAST so the
-- bloat stops RE-ACCUMULATING; the ongoing JSONB slim of stale rows lives in
-- data_retention._slim_stale_viability_snapshots. But none of that returns
-- the already-lost space to the OS — this script does the one-time rewrite.
--
-- ORDER OF OPERATIONS (run in a LOW-ACTIVITY / maintenance window; weekend
-- with the equity lane closed is ideal — the momentum tick writers block for
-- the duration of the rewrite and then proceed):
--   1. Ensure migration 357 has applied (or its statements were applied
--      manually): \d momentum_symbol_viability must NOT list
--      ix_momentum_symbol_viability_id, ix_msvi_corr, ix_msvi_freshness.
--   2. Run one retention sweep first (or the batched slim below) so the
--      stale-row JSONB is already '{}' — the rewrite then only carries the
--      fresh cohort's payload.
--   3. Run THIS script:
--        docker exec -i chili-home-copilot-postgres-1 \
--          psql -U chili -d chili -f - < scripts/reclaim_momentum_viability.sql
--   4. Verify sizes (query at the bottom) and that the momentum desk/summary
--      endpoints respond.
--
-- SAFETY: VACUUM (FULL) takes an ACCESS EXCLUSIVE lock for its duration and
-- cannot run inside a transaction. It rewrites heap + TOAST + ALL indexes
-- into fresh files (so no separate REINDEX is needed afterwards). Disk needed
-- ~= the post-slim live size (hundreds of MB here — trivial). The table is
-- ~122k rows, so the rewrite is expected to take well under a minute; writers
-- (viability tick upserts) and readers queue behind the lock and resume.
-- lock_timeout guards the acquisition: if something long-running holds the
-- table, we abort instead of wedging a lock queue behind us.
--
-- pg_repack would avoid the exclusive lock entirely, but it is not installed
-- in the postgres:16-alpine image and the extension + OS package churn is not
-- worth it for a sub-minute rewrite of a 122k-row table. If a NO-LOCK reclaim
-- ever becomes necessary (e.g. table must stay hot 24/7), install pg_repack
-- 1.5+ (apk add pg_repack && CREATE EXTENSION pg_repack) and run:
--   pg_repack --table momentum_symbol_viability --no-order -d chili

SET lock_timeout = '30s';
SET statement_timeout = 0;

-- Optional pre-slim (idempotent; the retention sweep does the same in
-- batches — running it here just front-loads the win before the rewrite):
UPDATE momentum_symbol_viability
SET regime_snapshot_json = '{}'::jsonb,
    execution_readiness_json = '{}'::jsonb,
    explain_json = '{}'::jsonb,
    evidence_window_json = '{}'::jsonb
WHERE freshness_ts < now() - interval '30 days'
  AND (regime_snapshot_json <> '{}'::jsonb
       OR execution_readiness_json <> '{}'::jsonb
       OR explain_json <> '{}'::jsonb
       OR evidence_window_json <> '{}'::jsonb);

VACUUM (FULL, ANALYZE, VERBOSE) momentum_symbol_viability;

-- Verify:
SELECT pg_size_pretty(pg_total_relation_size('momentum_symbol_viability')) AS total,
       pg_size_pretty(pg_relation_size('momentum_symbol_viability')) AS heap,
       pg_size_pretty(pg_indexes_size('momentum_symbol_viability')) AS indexes,
       (SELECT pg_size_pretty(pg_total_relation_size(reltoastrelid))
          FROM pg_class WHERE relname = 'momentum_symbol_viability') AS toast,
       (SELECT count(*) FROM momentum_symbol_viability) AS rows;
