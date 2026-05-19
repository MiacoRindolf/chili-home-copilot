# NEXT_TASK: f-coinbase-maker-only-paper-soak

STATUS: PENDING

## Goal

Operator-driven paper-soak of the Coinbase maker-only routing flag (`CHILI_COINBASE_MAKER_ONLY_ENABLED`). The code shipped 2026-05-19 (commit `18fee1e`, default OFF). This brief is the soak/audit/promote sequence — no further code change.

## Why this is next

The 2026-05-18 TCA finding showed Coinbase entry slippage averaging +102 bps, consuming ~60% of pattern 585's 168 bps gross edge. Maker-only routing addresses both taker fees AND adverse fills. Code is shipped and tested (41/41 tests pass), but the operational flip needs operator validation in production.

This is the **highest-leverage** of the three queued bridge briefs because the per-trade dollar impact compounds across every Coinbase entry going forward.

## Procedure

### Step 1 — Pre-flip baseline (5 min)

```sql
-- Capture the pre-flip avg entry slippage on Coinbase crypto:
SELECT
  AVG(tca_entry_slippage_bps)::numeric(10,2) AS avg_entry_bps,
  STDDEV(tca_entry_slippage_bps)::numeric(10,2) AS sd_bps,
  COUNT(*) AS n
FROM trading_trades
WHERE broker_source = 'coinbase'
  AND status = 'closed'
  AND tca_entry_slippage_bps IS NOT NULL
  AND entry_date > NOW() - INTERVAL '14 days';
-- Save this number. Target post-flip: avg < 30 bps.
```

### Step 2 — Flip flag

In `.env` (use ASCII WriteAllBytes per `feedback_never_powershell_outfile_env`):

```
CHILI_COINBASE_MAKER_ONLY_ENABLED=true
```

Then:

```
docker compose up -d --force-recreate autotrader-worker
```

(Lowest blast radius — only the autotrader needs the new value.)

### Step 3 — Watch logs for the first maker-only attempt

```bash
docker compose logs -f autotrader-worker | grep -E "maker-only|place_limit_order_gtc|falling back to market"
```

Expect either:
- `[autotrader] maker-only posted limit_buy <ticker> qty=<n> limit=<bid> post_only=True` → success
- `[autotrader] maker-only: no best_bid for <ticker>; falling back to market order` → degraded (no BBO available)
- `[autotrader] maker-only routing failed for <ticker>; falling back to market order` → exception (look at the traceback)

### Step 4 — Audit after 24-48h

```sql
-- New maker-routed entries:
SELECT id, ticker, broker_source,
       payload_json->>'_chili_maker_only' AS maker,
       payload_json->>'_chili_maker_limit_price' AS limit_px,
       status, average_fill_price
FROM trading_execution_events
WHERE event_type IN ('order_submitted', 'status')
  AND broker_source = 'coinbase'
  AND created_at > '<flip_ts>'
  AND payload_json->>'_chili_maker_only' IS NOT NULL
ORDER BY id DESC LIMIT 20;

-- Avg entry slippage post-flip (compare to Step 1 baseline):
SELECT AVG(tca_entry_slippage_bps)::numeric(10,2), COUNT(*)
FROM trading_trades
WHERE broker_source = 'coinbase'
  AND status = 'closed'
  AND tca_entry_slippage_bps IS NOT NULL
  AND entry_date > '<flip_ts>';
```

### Step 5 — Promote or rollback

After ~1 week of trades:

- **Promote** (leave flag on): avg entry bps dropped materially toward <30 bps. Document the flip in `docs/STRATEGY/COWORK_DECISIONS_LOG.md`.
- **Rollback**: avg bps didn't improve OR missed-entry rate is unacceptable. Flip flag back to false + restart autotrader-worker.

## Anomaly thresholds

- **No maker-routed entries in 48h:** check (a) BBO availability, (b) whether autotrader is even attempting Coinbase entries (alert volume could be low). Probe `trading_alerts WHERE broker_source='coinbase' AND created_at > <flip_ts>`.
- **High missed-entry rate (>30% of attempts log fallback or reject):** the bid was moving too fast. Consider a small bump above bid (e.g., +0.5 bps) OR accept the fallback and re-evaluate weekly.
- **Avg bps doesn't drop:** something else is contributing to slippage (delayed fills, queue position). Out-of-scope for this brief; queue a separate diagnostic.

## Out of scope

- Code changes. This is a soak/decision brief.
- Maker-only on the SELL side (separate future brief).
- Adaptive maker-timeout-then-taker fallback (separate future brief).
- Phase 5 envelope-rename (gated on first `[phase4_*]` log line; orthogonal to this).

## Rollback plan

```
CHILI_COINBASE_MAKER_ONLY_ENABLED=false
```
then `docker compose up -d --force-recreate autotrader-worker`. Legacy market-order path resumes immediately.

## Reference

- Code commit: `18fee1e`
- CC report (code ship): `docs/STRATEGY/CC_REPORTS/2026-05-19_f-coinbase-maker-only-routing.md`
- TCA finding source: `docs/STRATEGY/CC_REPORTS/2026-05-18_f-position-identity-phase-3-and-tca-and-account-type.md`
- Memory: `project_2026_05_19_coinbase_maker_only_shipped` (this session)
