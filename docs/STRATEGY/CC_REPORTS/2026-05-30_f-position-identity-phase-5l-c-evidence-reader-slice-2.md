# Phase 5L-C: Evidence Reader Slice 2

**Date:** 2026-05-30
**Status:** SHIPPED
**Commits:** `8c9b210`, merge `6d587b7`, `fe9d845`

## Summary

Reduced the Phase 5L reader allowlist by moving two more clean evidence/model
readers from the legacy compatibility view name to the semantic management
envelope relation:

- `app/services/trading/crypto/pattern_miner.py`
- `app/services/trading/options/portfolio_budget.py`

The slice also kept the tightened reader canary from the remote branch and
fixed one small options contract bug surfaced by clean-branch verification:
boolean greeks now parse as invalid instead of `1.0` / `0.0`.

No broker/order/reconcile writer path changed.

## Code Changes

- `crypto.pattern_miner.discover_crypto_winners` now reads
  `MANAGEMENT_ENVELOPES_RELATION`.
- `options.portfolio_budget._sum_open_trade_greeks` now reads
  `MANAGEMENT_ENVELOPES_RELATION`.
- `tests/test_phase5l_reader_allowlist.py` no longer allowlists those two raw
  reader lines.
- `options.contracts._float_or_none` now rejects bool values.

## Verification

Current worktree:

```text
python -m pytest tests\test_phase5l_reader_allowlist.py tests\test_options_portfolio_budget.py -q
22 passed

python -m py_compile app\services\trading\crypto\pattern_miner.py app\services\trading\options\portfolio_budget.py tests\test_phase5l_reader_allowlist.py tests\test_options_portfolio_budget.py

python scripts\d-phase5k-live-path-parity-probe.py
VERDICT_STATUS=COMPLETE_POSITIVE
PARITY_MISMATCHES=0

python scripts\d-phase5i-post-rename-soak-probe.py
VERDICT_STATUS=COMPLETE_POSITIVE
HARD_LINKAGE_ISSUES=0
MISMATCHED_ROWS=0
```

Clean temporary checkout after the boolean fix:

```text
python -m pytest tests\test_phase5l_reader_allowlist.py tests\test_options_portfolio_budget.py -q
22 passed
```

Live smoke:

```text
discover_crypto_winners(..., lookback_days=30) -> 0 winners
_sum_open_trade_greeks(user_id=1) -> missing_greeks_count=0, net_delta=0.0
```

Runtime:

- Recreated `chili`, `autotrader-worker`, and `brain-work-dispatcher`.
- All three came back healthy/running.
- Fresh logs showed no relation/schema/runtime errors for the touched paths.

## Architect Note

This slice keeps shrinking the stale `trading_trades` reader footprint without
touching the state machines. The boolean-greek fix is not part of position
identity, but it is the right data-science behavior: booleans are categorical
flags, not option greeks.

## Next

The remaining direct evidence/model reader candidates are dirty local files:

- `app/services/trading/pattern_regime_ledger.py`
- `app/services/trading/pattern_survival/features.py`

They are still good candidates, but they need isolated staging so unrelated
local edits are not absorbed.
