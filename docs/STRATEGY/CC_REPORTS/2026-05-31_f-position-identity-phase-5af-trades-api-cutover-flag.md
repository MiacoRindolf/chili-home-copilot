# Phase 5AF - Trades API Cutover Flag

Date: 2026-05-31

## Verdict

SHIPPED as a default-off, reversible route flag.

This is not a public rename. `/api/trading/trades`, response key `trades`,
`trade_id` vocabulary, schema names, UI labels, broker/order/reconcile paths,
PDT/capital gates, and sell/close behavior are unchanged by default.

## What changed

- Added typed settings flag
  `CHILI_PHASE5AF_TRADES_API_USE_ENVELOPES`, default `false`.
- Added a management-envelope response renderer for `/api/trading/trades`.
- When the flag is enabled, the route can serve rows from
  `trading_management_envelopes`.
- The cutover path refuses open-row responses and falls back to the
  compatibility route whenever open trades are present or requested.
- Broker-truth display overlays and stale-open suppression therefore remain on
  the proven compatibility path until a separate parity slice owns that
  contract.
- While waiting for CI, hardened the Coinbase OHLCV focused test reset helper
  so it clears the provider circuit-breaker state in addition to product and
  rate-limit caches. Full-suite order had exposed that leaked breaker state.

## Architect call

This is the right Phase 5A-style move for `/trades`: get a live-switchable
closed-row read-route path without pretending the open-trade display contract is
simple. Open rows are not passive database rows; they carry broker truth,
stale-open filtering, and current-position overlays. The flag is useful now,
but full `/trades` cutover should only happen after an open-row runtime adapter
parity probe proves those overlays match.

## Verification

```text
python -m py_compile app/routers/trading_sub/trades.py app/services/trading/management_envelopes.py app/config.py
PASS

python -m py_compile app/services/trading/coinbase_ohlcv.py
PASS

TEST_DATABASE_URL=postgresql://chili:chili@localhost:5433/chili_test \
  pytest -q \
    tests/test_trades_api_shadow_compare.py \
    tests/test_phase5t_audit_export_helper.py \
    tests/test_management_envelopes.py \
    tests/test_phase5_remaining_trade_refs.py \
    tests/test_phase5l_reader_allowlist.py
38 passed

TEST_DATABASE_URL=postgresql://chili:chili@localhost:5433/chili_test \
  pytest -q tests/test_coinbase_ohlcv_missing_product_cache.py tests/test_trades_api_shadow_compare.py
14 passed

python scripts/analyze_phase5_remaining_trade_refs.py --bucket orm_trade_symbol_compat --fail-on-unexpected-runtime
raw reader bucket: 0
orm_trade_symbol_compat: 94

python scripts/d-phase5ae-trades-api-parity-probe.py
VERDICT_STATUS=COMPLETE_POSITIVE
CHECKS=3
MISMATCHES=0

DATABASE_URL=postgresql://chili:chili@localhost:5433/chili \
  python scripts/d-phase5k-live-path-parity-probe.py
VERDICT_STATUS=COMPLETE_POSITIVE
PARITY_CHECKS=6
PARITY_MISMATCHES=0

python scripts/d-phase5i-post-rename-soak-probe.py
VERDICT_STATUS=COMPLETE_POSITIVE
FRESH_DECISIONS=20
FRESH_ENVELOPES=20
FRESH_CLOSES=10
FRESH_CLOSE_MISMATCHES=0
```

## Next

Phase 5AF soak:

1. Leave `CHILI_PHASE5AF_TRADES_API_USE_ENVELOPES=false` in live runtime.
2. Exercise `/api/trading/trades`, `/api/trading/trades?status=open`, and
   `/api/trading/trades?status=closed`.
3. Watch for `[phase5v] /trades envelope shadow mismatch` and `[phase5af]`
   fallback lines.
4. If the operator wants a live cutover trial, first enable only a short
   `status=closed` route soak and confirm API responses stay clean.
5. Build a separate open-row runtime adapter probe before allowing open/all
   route responses through the envelope renderer.
