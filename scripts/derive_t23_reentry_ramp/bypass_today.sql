-- [23] re-entry ramp derivation (PR #1412). READ-ONLY against the live DB: point
-- DATABASE_URL at it privately; every connection sets default_transaction_read_only.
SET statement_timeout='60s';
WITH en AS (
  SELECT e.session_id, e.leg_seq, e.symbol, e.fill_ts, e.broker_fill_price AS px,
         x.realized_pnl_usd AS pnl, x.exit_reason,
         (e.fill_ts AT TIME ZONE 'UTC' AT TIME ZONE 'America/New_York')::date AS d
  FROM momentum_fill_outcomes e
  JOIN momentum_fill_outcomes x ON x.session_id=e.session_id AND x.leg_seq=e.leg_seq AND x.side='exit' AND x.mode='live'
  WHERE e.mode='live' AND e.side='entry' AND e.fill_ts >= now() - interval '1 days'
), re AS (
  SELECT en.*, EXISTS (SELECT 1 FROM momentum_fill_outcomes p WHERE p.mode='live' AND p.side='exit' AND p.symbol=en.symbol
        AND p.fill_ts < en.fill_ts AND (p.fill_ts AT TIME ZONE 'UTC' AT TIME ZONE 'America/New_York')::date = en.d) AS is_re
  FROM en
)
SELECT re.session_id, re.leg_seq, re.symbol, to_char(re.fill_ts,'MM-DD HH24:MI:SS') t, re.px, round(re.pnl::numeric,2) pnl, re.exit_reason, re.is_re,
  b.event_type, coalesce(b.payload_json->>'decision_reason', b.payload_json->>'reason') AS reason, b.payload_json->>'escalation_level' lvl,
  b.payload_json->>'required_reclaim' req, b.payload_json->>'is_day_leader' leader, b.payload_json->>'structural_trigger' st,
  round(extract(epoch from re.fill_ts - b.ts)::numeric,1) age_s
FROM re
LEFT JOIN LATERAL (
  SELECT ev.* FROM trading_automation_events ev
  WHERE ev.session_id=re.session_id AND ev.ts <= re.fill_ts AND ev.ts >= re.fill_ts - interval '10 minutes'
    AND ev.event_type IN ('g4_reentry_escalation_blocked','g4_reentry_pass_unproven','g4_reentry_reclaim_proven','g4_reentry_escalation_wait')
  ORDER BY ev.ts DESC LIMIT 1) b ON true
WHERE re.is_re
ORDER BY re.fill_ts;
