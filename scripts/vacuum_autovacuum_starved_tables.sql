-- vacuum_autovacuum_starved_tables.sql
--
-- Manual VACUUM (ANALYZE) for high-churn tables whose autovacuum had never
-- completed (autovacuum_count=0), leaving stale planner stats. The marquee
-- symptom: trading_snapshots had reltuples ~362k vs ~20 live rows, which made
-- an autotrader probe pick a bad plan and time out.
--
-- Plain VACUUM (ANALYZE) takes only a SHARE UPDATE EXCLUSIVE lock -- it does
-- NOT block reads or writes -- so this is safe to run anytime (no maintenance
-- window needed). These tables are small, so it completes in seconds.
--
-- PARALLEL 0 disables parallel index-vacuum workers. The container ships with
-- the Docker default /dev/shm = 64 MB, and parallel workers fail with
-- "could not resize shared memory segment ... No space left on device".
-- (Consider raising shm_size in docker-compose -- see audit point 4.)
--
-- NOTE: trading_exit_parity_log is intentionally NOT here -- it gets a full
-- heap rewrite via scripts/reclaim_exit_parity_log.sql in a maintenance
-- window; a plain VACUUM on the 20 GB table would not return space to the OS.
--
-- Usage:
--   docker exec -i chili-home-copilot-postgres-1 \
--     psql -U chili -d chili -f - < scripts/vacuum_autovacuum_starved_tables.sql

\timing on
\set ON_ERROR_STOP on

VACUUM (ANALYZE, PARALLEL 0) trading_snapshots;
VACUUM (ANALYZE, PARALLEL 0) brain_batch_jobs;
VACUUM (ANALYZE, PARALLEL 0) fast_executions_default;
VACUUM (ANALYZE, PARALLEL 0) trading_scans;
VACUUM (ANALYZE, PARALLEL 0) trading_alerts;
VACUUM (ANALYZE, PARALLEL 0) trading_breakout_alerts;

\echo '=== post-vacuum stats ==='
SELECT relname, n_live_tup, n_dead_tup, last_vacuum, last_analyze
FROM pg_stat_user_tables
WHERE relname IN ('trading_snapshots','brain_batch_jobs','fast_executions_default',
                  'trading_scans','trading_alerts','trading_breakout_alerts')
ORDER BY relname;
