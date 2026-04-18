-- Merge every account into users.id = 1 (rindolf.miaco@gmail.com).
-- Part 1: reassign all FKs except trading_pattern_trades (huge table).
-- Part 2: batched remap for trading_pattern_trades.
-- Part 3: delete other user rows.
--
--   Get-Content scripts/sql/merge_all_users_into_primary.sql | docker compose exec -T postgres psql -U chili -d chili -v ON_ERROR_STOP=1
--
-- Destructive. Backup first.

-- ===========================================================================
-- Part 1
-- ===========================================================================
BEGIN;

-- One row per broker — prefer primary's row, else lowest id
DELETE FROM broker_credentials bc
WHERE bc.id NOT IN (
  SELECT id FROM (
    SELECT DISTINCT ON (broker) id
    FROM broker_credentials
    ORDER BY broker, (user_id = 1) DESC, id
  ) keepers
);

DELETE FROM housemate_profiles WHERE user_id <> 1 AND EXISTS (SELECT 1 FROM housemate_profiles h WHERE h.user_id = 1);
UPDATE housemate_profiles SET user_id = 1
WHERE id = (SELECT MIN(id) FROM housemate_profiles WHERE user_id <> 1)
  AND NOT EXISTS (SELECT 1 FROM housemate_profiles WHERE user_id = 1);
DELETE FROM housemate_profiles WHERE user_id <> 1;

DELETE FROM intercom_consents WHERE user_id <> 1 AND EXISTS (SELECT 1 FROM intercom_consents i WHERE i.user_id = 1);
UPDATE intercom_consents SET user_id = 1
WHERE id = (SELECT MIN(id) FROM intercom_consents WHERE user_id <> 1)
  AND NOT EXISTS (SELECT 1 FROM intercom_consents WHERE user_id = 1);
DELETE FROM intercom_consents WHERE user_id <> 1;

DELETE FROM user_statuses WHERE user_id <> 1 AND EXISTS (SELECT 1 FROM user_statuses u WHERE u.user_id = 1);
UPDATE user_statuses SET user_id = 1
WHERE id = (SELECT MIN(id) FROM user_statuses WHERE user_id <> 1)
  AND NOT EXISTS (SELECT 1 FROM user_statuses WHERE user_id = 1);
DELETE FROM user_statuses WHERE user_id <> 1;

DELETE FROM trading_daily_playbooks d
WHERE d.user_id IS NOT NULL AND d.user_id <> 1
  AND EXISTS (
    SELECT 1 FROM trading_daily_playbooks o
    WHERE o.user_id = 1 AND o.playbook_date = d.playbook_date
  );

DELETE FROM trading_brain_performance_daily d
WHERE d.user_id IS NOT NULL AND d.user_id <> 1
  AND EXISTS (
    SELECT 1 FROM trading_brain_performance_daily o
    WHERE o.user_id = 1 AND o.perf_date = d.perf_date
  );

DELETE FROM project_members pm
WHERE pm.user_id <> 1
  AND EXISTS (
    SELECT 1 FROM project_members o
    WHERE o.project_id = pm.project_id AND o.user_id = 1
  );

UPDATE chores SET assigned_to = 1 WHERE assigned_to IS NOT NULL AND assigned_to <> 1;
UPDATE plan_tasks SET assigned_to = 1 WHERE assigned_to IS NOT NULL AND assigned_to <> 1;
UPDATE plan_tasks SET reporter_id = 1 WHERE reporter_id IS NOT NULL AND reporter_id <> 1;
UPDATE intercom_messages SET from_user_id = 1 WHERE from_user_id IS NOT NULL AND from_user_id <> 1;
UPDATE intercom_messages SET to_user_id = 1 WHERE to_user_id IS NOT NULL AND to_user_id <> 1;
UPDATE coding_task_brief SET created_by = 1 WHERE created_by IS NOT NULL AND created_by <> 1;

DO $$
DECLARE
  r RECORD;
BEGIN
  FOR r IN
    SELECT table_name
    FROM information_schema.columns
    WHERE table_schema = 'public'
      AND column_name = 'user_id'
      AND table_name NOT IN ('users', 'trading_pattern_trades')
    ORDER BY table_name
  LOOP
    EXECUTE format(
      'UPDATE %I SET user_id = 1 WHERE user_id IS NOT NULL AND user_id <> 1',
      r.table_name
    );
  END LOOP;
END $$;

COMMIT;

-- ===========================================================================
-- Part 2: trading_pattern_trades (batched commits inside procedure)
-- ===========================================================================
CREATE OR REPLACE PROCEDURE merge_pattern_trades_user_ids_to_primary(IN p_primary int, IN p_batch int)
LANGUAGE plpgsql
AS $$
DECLARE
  n int;
BEGIN
  IF p_primary IS NULL OR p_batch < 1 THEN
    RAISE EXCEPTION 'invalid args';
  END IF;
  LOOP
    UPDATE trading_pattern_trades AS t
    SET user_id = p_primary
    FROM (
      SELECT id FROM trading_pattern_trades
      WHERE user_id IS NOT NULL AND user_id <> p_primary
      LIMIT p_batch
    ) AS s
    WHERE t.id = s.id;
    GET DIAGNOSTICS n = ROW_COUNT;
    COMMIT;
    RAISE NOTICE 'trading_pattern_trades: remapped % rows', n;
    EXIT WHEN n = 0;
  END LOOP;
END;
$$;

CALL merge_pattern_trades_user_ids_to_primary(1, 100000);

DROP PROCEDURE merge_pattern_trades_user_ids_to_primary(int, int);

-- ===========================================================================
-- Part 3 (manual): after CALL finishes, verify no FKs point at id<>1, then:
--   Get-Content scripts/sql/merge_finish_delete_spare_users.sql | docker compose exec -T postgres psql -U chili -d chili -v ON_ERROR_STOP=1
-- ===========================================================================
