# NEXT_TASK: f-autotrader-payoff-sizing-paper-soak

STATUS: PENDING

## Goal

Operator-driven paper-soak of the autotrader payoff-ratio-aware sizing scaler. Code shipped 2026-05-19 (commit `c07077c`, default OFF). This brief is the soak/audit/promote sequence — no further code change.

## Why this is next

The Tier A payoff-ratio gate (commit `23bde18`, 2026-05-18) protects skew-driven edges from demote. The autotrader sizing scaler (commit `c07077c`, this session) extends the same signal to position sizing — up-sizes pattern 585 (4.97:1, n=86) to 1.5x; down-sizes patterns with sub-1:1 historical payoff to 0.5x. Code is shipped and tested (57/57 tests); the operational flip needs operator validation.

This is the **third bridge brief** while waiting for Phase 5 envelope-rename's `[phase4_*]` gate.

## Procedure

### Step 1 — Pre-flip distribution of pattern tiers

```sql
-- What tiers will fire when the flag goes on?
SELECT
  CASE
    WHEN payoff_ratio_n IS NULL OR payoff_ratio_n < 5 THEN 'insufficient_n'
    WHEN payoff_ratio >= 5.0 THEN 'very_high'
    WHEN payoff_ratio >= 2.0 THEN 'high'
    WHEN payoff_ratio >= 1.0 THEN 'moderate'
    ELSE 'low'
  END AS tier,
  COUNT(*) AS n_patterns,
  ROUND(AVG(payoff_ratio)::numeric, 3) AS avg_payoff,
  STRING_AGG(name, ' | ' ORDER BY payoff_ratio DESC NULLS LAST) FILTER (WHERE payoff_ratio_n >= 5) AS top_patterns
FROM scan_patterns
WHERE active = TRUE
GROUP BY tier
ORDER BY CASE tier WHEN 'very_high' THEN 1 WHEN 'high' THEN 2 WHEN 'moderate' THEN 3 WHEN 'low' THEN 4 ELSE 5 END;
```

Expected (rough): a small number of `very_high` (pattern 585, maybe pid 537), some `high`, a long tail of `moderate` / `insufficient_n`, a few `low`.

### Step 2 — Flip flag

In `.env` (ASCII WriteAllBytes per memory):

```
CHILI_AUTOTRADER_PAYOFF_SIZING_ENABLED=true
```

Restart autotrader only (lowest blast radius):

```
docker compose up -d --force-recreate autotrader-worker
```

### Step 3 — Watch the first few entry attempts

Once an entry alert fires, the `trading_autotrader_runs` row's `rule_snapshot` JSONB will contain the new fields. Query:

```sql
SELECT created_at, ticker, decision, reason,
       rule_snapshot->>'payoff_sizing_tier' AS tier,
       rule_snapshot->>'payoff_sizing_multiplier' AS mult,
       rule_snapshot->>'payoff_ratio_observed' AS ratio,
       rule_snapshot->>'payoff_ratio_n_observed' AS n_obs,
       rule_snapshot->>'notional_before_payoff_sizing' AS pre_n,
       rule_snapshot->>'notional_effective' AS post_n
FROM trading_autotrader_runs
WHERE created_at > '<flip_ts>'
  AND rule_snapshot ? 'payoff_sizing_tier'
ORDER BY created_at DESC LIMIT 30;
```

Sanity-check the tier mapping (e.g., pid 585 → `very_high`, mult=1.5; pid 1066 with payoff 0.275 → `low`, mult=0.5).

### Step 4 — Promote or rollback after ~1 week

Compute realized PnL by tier:

```sql
SELECT
  ar.rule_snapshot->>'payoff_sizing_tier' AS tier,
  COUNT(t.id) AS n_trades,
  ROUND(SUM(t.pnl)::numeric, 2) AS total_pnl,
  ROUND(AVG(t.pnl)::numeric, 2) AS avg_pnl
FROM trading_autotrader_runs ar
LEFT JOIN trading_trades t ON t.related_alert_id = ar.alert_id
                          AND t.status = 'closed'
WHERE ar.created_at > '<flip_ts>'
  AND ar.rule_snapshot ? 'payoff_sizing_tier'
GROUP BY tier
ORDER BY tier;
```

- **Promote** if `very_high` and `high` tiers out-realize `moderate` (validating the sizing edge).
- **Rollback** if any tier clearly underperforms in a way the sizing made worse.

## Bridge briefs still queued

After this paper-soak resolves:

- **`f-coinbase-maker-only-paper-soak`** (in-flight; weekly Sunday probe will report)
- **`f-position-identity-phase-5-envelope-rename`** (gated on first `[phase4_*]` log line)
- **`f-pid-537-watcher-elevation-decision`** (gated on n=15)

## Rollback plan

```
CHILI_AUTOTRADER_PAYOFF_SIZING_ENABLED=false
docker compose up -d --force-recreate autotrader-worker
```

## Reference

- Code commit: `c07077c`
- CC report: `docs/STRATEGY/CC_REPORTS/2026-05-19_f-stop-engine-payoff-ratio-gate.md`
- Tier A demote gate (predecessor): commit `23bde18`, 2026-05-18
- Memory: `project_2026_05_19_payoff_sizing_shipped` (this session)
