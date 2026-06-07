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
