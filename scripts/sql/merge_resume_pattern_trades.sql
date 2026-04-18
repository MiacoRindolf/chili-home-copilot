-- Resume or re-run trading_pattern_trades → user 1 remap (safe if already done).
--   Get-Content scripts/sql/merge_resume_pattern_trades.sql | docker compose exec -T postgres psql -U chili -d chili -v ON_ERROR_STOP=1

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
