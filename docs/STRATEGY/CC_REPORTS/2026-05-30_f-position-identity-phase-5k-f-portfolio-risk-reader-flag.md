# f-position-identity-phase-5k-f-portfolio-risk-reader-flag

Date: 2026-05-30

Status: PROMOTED. Live flag is ON.

## What Changed

Added a default-off Phase 5K reader flag to `app/services/trading/portfolio_risk.py`:

- `CHILI_PHASE5K_PORTFOLIO_RISK_USE_ENVELOPES=false` (default)
- OFF reads `trading_trades` compatibility view
- ON reads `trading_management_envelopes` physical base table

The concrete reader surface is the portfolio/drawdown breaker closed-PnL math:

- `_monthly_dd_threshold`
- `_monthly_attributed_pnl`
- `_portfolio_dd_threshold`
- `_monthly_total_pnl`

No formulas, caps, thresholds, filters, broker paths, order paths, stop paths,
or reconcile paths changed.

## Default-Off Verification

Focused local tests:

```text
python -m pytest tests\test_phase5k_portfolio_risk_reader_flag.py tests\test_phase5k_live_path_parity_probe.py -q
15 passed
```

Compile check:

```text
python -m py_compile app\services\trading\portfolio_risk.py scripts\d-phase5k-live-path-parity-probe.py
OK
```

Live Phase 5K-A parity:

```text
VERDICT_STATUS=COMPLETE_POSITIVE
PARITY_CHECKS=6
PARITY_MISMATCHES=0
```

Live Phase 5I post-rename soak:

```text
VERDICT_STATUS=COMPLETE_POSITIVE
FRESH_DECISIONS=20
FRESH_ENVELOPES=20
FRESH_CLOSES=10
HARD_LINKAGE_ISSUES=0
MISMATCHED_ROWS=0
```

Direct old/new portfolio-risk checks:

```text
USER=None
MONTHLY_DD_OLD=(None, 0)
MONTHLY_DD_NEW=(None, 0)
MONTHLY_ATTR_OLD=0.00000000
MONTHLY_ATTR_NEW=0.00000000
PORTFOLIO_DD_OLD=(-5595.035574047489, 42)
PORTFOLIO_DD_NEW=(-5595.035574047489, 42)
MONTHLY_TOTAL_OLD=380.35580000
MONTHLY_TOTAL_NEW=380.35580000
MATCH=True

USER=1
MONTHLY_DD_OLD=(-115.22872833529681, 34)
MONTHLY_DD_NEW=(-115.22872833529681, 34)
MONTHLY_ATTR_OLD=431.74580000
MONTHLY_ATTR_NEW=431.74580000
PORTFOLIO_DD_OLD=(-3297.255256725238, 35)
PORTFOLIO_DD_NEW=(-3297.255256725238, 35)
MONTHLY_TOTAL_OLD=380.35580000
MONTHLY_TOTAL_NEW=380.35580000
MATCH=True
```

## Architect Read

The brief called this the "portfolio-risk open-exposure" reader, but the
remaining raw SQL surface in `portfolio_risk.py` is the drawdown/closed-PnL
breaker math. Open-position exposure still flows through the `Trade` ORM and
is not a clean single SQL relation switch. This implementation intentionally
cuts over the actual direct reader surface available in the module.

## Live Soak

Flag flipped:

```text
CHILI_PHASE5K_PORTFOLIO_RISK_USE_ENVELOPES=true
```

Consumers recreated:

- `chili`
- `autotrader-worker`
- `scheduler-worker`
- `broker-sync-worker`

Runtime flag visibility:

```text
autotrader-worker=true
chili=true
scheduler-worker=true
broker-sync-worker=true
```

Post-flip verification:

```text
Phase 5K-A: COMPLETE_POSITIVE, PARITY_MISMATCHES=0
Phase 5I: COMPLETE_POSITIVE, HARD_LINKAGE_ISSUES=0, MISMATCHED_ROWS=0
PORTFOLIO_RISK_DRAWDOWN_MATCH=True
```

Post-flip log scan:

```text
portfolio-risk/relation/query errors: none
```

Known unrelated noise: Coinbase product 404s for delisted/unavailable product
IDs. No portfolio-risk query, relation, or drawdown breaker errors appeared.

## Rollback

Leave or set `CHILI_PHASE5K_PORTFOLIO_RISK_USE_ENVELOPES=false`, then recreate
the consumer worker(s). With the flag off, behavior remains the compatibility
view path.
