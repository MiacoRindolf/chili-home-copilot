-- Assign NULL user_id rows to the primary CHILI account (default users.id = 1).
-- Target user: rindolf.miaco@gmail.com — verify with:
--   SELECT id, email FROM users WHERE email ILIKE '%rindolf.miaco%';
--
-- Steps (run from repo root, PowerShell):
--   1) Playbooks + perf daily (unique date keys — dedupe first):
--      Get-Content scripts/sql/backfill_null_user_id_to_owner.sql | docker compose exec -T postgres psql -U chili -d chili -v ON_ERROR_STOP=1
--   2) All other nullable-user_id tables (one statement = one commit):
--      Get-Content scripts/sql/backfill_null_user_id_updates.sql | docker compose exec -T postgres psql -U chili -d chili -v ON_ERROR_STOP=1
--   3) trading_pattern_trades (~millions of rows) — batched procedure (commits every 100k):
--      Get-Content scripts/sql/backfill_pattern_trades_user_id_procedure.sql | docker compose exec -T postgres psql -U chili -d chili -v ON_ERROR_STOP=1
--      (Alternate: scripts/backfill_pattern_trades_user_id.py if DATABASE_URL works from the host.)
--   4) Catch new alerts inserted during the run:
--      docker compose exec -T postgres psql -U chili -d chili -c "UPDATE trading_breakout_alerts SET user_id = 1 WHERE user_id IS NULL;"
--
-- Note: postgres is exposed on host port 5433 (see docker-compose.yml).

\set owner_id 1

-- ---------------------------------------------------------------------------
-- trading_daily_playbooks: collapse duplicate NULL rows per playbook_date
-- ---------------------------------------------------------------------------
DELETE FROM trading_daily_playbooks d
USING (
  SELECT id
  FROM (
    SELECT id,
           ROW_NUMBER() OVER (PARTITION BY playbook_date ORDER BY id) AS rn
    FROM trading_daily_playbooks
    WHERE user_id IS NULL
  ) s
  WHERE s.rn > 1
) x
WHERE d.id = x.id;

DELETE FROM trading_daily_playbooks d
WHERE d.user_id IS NULL
  AND EXISTS (
    SELECT 1 FROM trading_daily_playbooks e
    WHERE e.user_id = :owner_id AND e.playbook_date = d.playbook_date
  );

UPDATE trading_daily_playbooks SET user_id = :owner_id WHERE user_id IS NULL;

-- ---------------------------------------------------------------------------
-- trading_brain_performance_daily
-- ---------------------------------------------------------------------------
DELETE FROM trading_brain_performance_daily d
USING (
  SELECT id
  FROM (
    SELECT id,
           ROW_NUMBER() OVER (PARTITION BY perf_date ORDER BY id) AS rn
    FROM trading_brain_performance_daily
    WHERE user_id IS NULL
  ) s
  WHERE s.rn > 1
) x
WHERE d.id = x.id;

DELETE FROM trading_brain_performance_daily d
WHERE d.user_id IS NULL
  AND EXISTS (
    SELECT 1 FROM trading_brain_performance_daily e
    WHERE e.user_id = :owner_id AND e.perf_date = d.perf_date
  );

UPDATE trading_brain_performance_daily SET user_id = :owner_id WHERE user_id IS NULL;
