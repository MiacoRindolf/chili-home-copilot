-- Final step after merge_all_users_into_primary.sql:
-- Run only when no row references users.id other than 1, e.g.:
--   SELECT COUNT(*) FROM trading_pattern_trades WHERE user_id IS NOT NULL AND user_id <> 1;
--   → 0
--
--   Get-Content scripts/sql/merge_finish_delete_spare_users.sql | docker compose exec -T postgres psql -U chili -d chili -v ON_ERROR_STOP=1

UPDATE trading_breakout_alerts SET user_id = 1 WHERE user_id IS NULL;

DELETE FROM users WHERE id <> 1;
