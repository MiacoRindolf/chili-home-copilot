-- Batched backfill for trading_pattern_trades.user_id (PG 11+ PROCEDURE with COMMIT).
-- Run: Get-Content scripts/sql/backfill_pattern_trades_user_id_procedure.sql | docker compose exec -T postgres psql -U chili -d chili -v ON_ERROR_STOP=1
--
-- Uses ~100k rows per transaction to avoid one multi-hour UPDATE.

CREATE OR REPLACE PROCEDURE backfill_pattern_trades_user_id(IN p_owner_id int, IN p_batch int)
LANGUAGE plpgsql
AS $$
DECLARE
  n int;
BEGIN
  IF p_owner_id IS NULL OR p_batch < 1 THEN
    RAISE EXCEPTION 'invalid args';
  END IF;
  LOOP
    UPDATE trading_pattern_trades AS t
    SET user_id = p_owner_id
    FROM (
      SELECT id FROM trading_pattern_trades
      WHERE user_id IS NULL
      LIMIT p_batch
    ) AS s
    WHERE t.id = s.id;
    GET DIAGNOSTICS n = ROW_COUNT;
    COMMIT;
    RAISE NOTICE 'updated % rows (batch=%)', n, p_batch;
    EXIT WHEN n = 0;
  END LOOP;
END;
$$;

CALL backfill_pattern_trades_user_id(1, 100000);

DROP PROCEDURE backfill_pattern_trades_user_id(int, int);
