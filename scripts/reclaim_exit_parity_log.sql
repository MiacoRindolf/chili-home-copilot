-- reclaim_exit_parity_log.sql
--
-- ONE-TIME heap reclaim for trading_exit_parity_log.
--
-- WHY: the ongoing retention prune (app/services/trading/data_retention.py
-- :_prune_exit_parity_log) now drains all eligible rows every sweep, so the
-- table stops GROWING. But a plain DELETE only marks tuples dead -- it does
-- not return the ~8 GB heap (or the dead space from the historical 40M-row
-- backlog) to the OS. This script does the one-time physical rewrite.
--
-- ORDER OF OPERATIONS (run in a LOW-ACTIVITY / maintenance window):
--   1. Deploy the branch so migration 301 has applied (drops 7 zero-scan
--      indexes ~10 GB + pins per-table autovacuum). Verify with \d
--      trading_exit_parity_log that only ix_exit_parity_created_retention,
--      ix_exit_parity_mode_created, ix_exit_parity_pattern_created remain.
--   2. Pause the writers so no rows are inserted mid-rewrite:
--        docker compose stop scheduler-worker brain-worker   # + any backtest workers
--      (the live exit path writes too, but at a trickle; full pause is safest)
--   3. Run THIS script:
--        docker exec -i chili-home-copilot-postgres-1 \
--          psql -U chili -d chili -f - < scripts/reclaim_exit_parity_log.sql
--      (or: scripts\reclaim_exit_parity_log.ps1)
--   4. Restart the writers:
--        docker compose start scheduler-worker brain-worker
--
-- SAFETY: VACUUM (FULL) takes an ACCESS EXCLUSIVE lock for its duration and
-- rewrites the heap into fresh files, so it CANNOT run inside a transaction
-- or a migration, and it blocks all reads/writes while it runs -- hence the
-- maintenance window. It needs free disk ~= the *kept* (post-delete) size,
-- which is small here; E: NVMe has ample headroom.
--
-- LARGE BACKLOG? Prefer the CTAS keep-set + swap below. This DELETE+VACUUM
-- FULL path marks every doomed row dead then rewrites -- fine for incremental
-- reclaims, but when the backlog is huge and the keep-set is tiny (e.g. the
-- 2026-06-07 reclaim: 40.25M doomed / 2.49M kept) deleting 40M rows is slow
-- (and the pinned aggressive autovacuum competes for I/O). The CTAS-swap
-- writes ONLY the keep-set and is atomic/rollback-safe. That is what was
-- actually run on 2026-06-07 (heap 8 GB -> 472 MB, DB 30 GB -> 10 GB):
--
--   -- stop heavy parity writers first:
--   --   docker compose stop scheduler-worker brain-worker backtest-worker
--   SET statement_timeout = 0; SET idle_in_transaction_session_timeout = 0;
--   SET lock_timeout = '60s';
--   BEGIN;
--   CREATE TABLE trading_exit_parity_log_new
--     (LIKE trading_exit_parity_log INCLUDING DEFAULTS INCLUDING CONSTRAINTS INCLUDING STORAGE);
--   INSERT INTO trading_exit_parity_log_new
--     SELECT * FROM trading_exit_parity_log
--      WHERE NOT ((source='backtest' AND created_at < now()-interval '7 days')
--              OR (COALESCE(source,'')<>'backtest' AND created_at < now()-interval '30 days'));
--   ALTER TABLE trading_exit_parity_log RENAME TO trading_exit_parity_log_old;
--   ALTER TABLE trading_exit_parity_log_new RENAME TO trading_exit_parity_log;
--   ALTER SEQUENCE trading_exit_parity_log_id_seq OWNED BY trading_exit_parity_log.id;
--   DROP TABLE trading_exit_parity_log_old;      -- after re-owning the sequence!
--   CREATE INDEX ix_exit_parity_created_retention ON trading_exit_parity_log (created_at, id);
--   CREATE INDEX ix_exit_parity_mode_created      ON trading_exit_parity_log (mode, created_at);
--   CREATE INDEX ix_exit_parity_pattern_created   ON trading_exit_parity_log (scan_pattern_id, created_at DESC, id DESC) WHERE scan_pattern_id IS NOT NULL;
--   ALTER TABLE trading_exit_parity_log SET (autovacuum_vacuum_scale_factor=0,
--     autovacuum_vacuum_threshold=50000, autovacuum_analyze_scale_factor=0,
--     autovacuum_analyze_threshold=50000, autovacuum_vacuum_cost_delay=0);
--   ANALYZE trading_exit_parity_log;
--   COMMIT;
--   -- then: docker compose start scheduler-worker brain-worker backtest-worker
--   -- verify: SELECT last_value FROM trading_exit_parity_log_id_seq; -- must be >= max(id)
--
-- The retention windows below MIRROR the config defaults
--   brain_retention_exit_parity_backtest_days = 7
--   brain_retention_exit_parity_live_days     = 30
-- Keep them in sync if those settings change.

\timing on
\set ON_ERROR_STOP on

\echo '=== BEFORE ==='
SELECT pg_size_pretty(pg_total_relation_size('trading_exit_parity_log')) AS total,
       pg_size_pretty(pg_relation_size('trading_exit_parity_log'))       AS heap;
SELECT count(*) AS total_rows,
       count(*) FILTER (WHERE source = 'backtest') AS backtest_rows,
       count(*) FILTER (WHERE source <> 'backtest' OR source IS NULL) AS live_rows
FROM trading_exit_parity_log;

\echo '=== DELETE backlog (rows the retention policy would prune) ==='
DELETE FROM trading_exit_parity_log
 WHERE (
            source = 'backtest'
        AND created_at < now() - interval '7 days'
       )
    OR (
            COALESCE(source, '') <> 'backtest'
        AND created_at < now() - interval '30 days'
       );

\echo '=== VACUUM (FULL, ANALYZE) -- physical heap rewrite + fresh stats ==='
VACUUM (FULL, ANALYZE) trading_exit_parity_log;

\echo '=== AFTER ==='
SELECT pg_size_pretty(pg_total_relation_size('trading_exit_parity_log')) AS total,
       pg_size_pretty(pg_relation_size('trading_exit_parity_log'))       AS heap;
SELECT count(*) AS total_rows FROM trading_exit_parity_log;
SELECT reloptions FROM pg_class WHERE relname = 'trading_exit_parity_log';
