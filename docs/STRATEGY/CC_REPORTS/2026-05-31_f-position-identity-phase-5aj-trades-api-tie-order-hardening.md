# Phase 5AJ - Trades API Tie-Order Hardening

Date: 2026-05-31

## Summary

Phase 5AJ removed the last soft acceptance path from the Phase 5AH
`/api/trading/trades` cutover probe.

The legacy `Trade` ORM reader now orders by `entry_date DESC, id DESC`, matching
the management-envelope runtime-object reader's deterministic secondary
tie-breaker for rows that share the exact same `entry_date`. The probe no
longer accepts `tie_order_only=true`; all `/trades` parity checks must match
exactly.

## Changes

- Added deterministic `id DESC` tie-breaking to
  `app.services.trading.portfolio.get_trades(...)`.
- Removed the Phase 5AH probe's mixed/all `tie_order_only` allowance.
- Updated the probe test so reordered payloads fail instead of being accepted.

No broker, order, close, reconcile, PDT, capital, cash, portfolio, promotion,
or stop-execution behavior was changed.

## Verification

Focused checks:

```text
python -m py_compile app/services/trading/portfolio.py scripts/d-phase5ah-trades-api-cutover-probe.py tests/test_phase5ah_trades_api_cutover_probe.py
pytest -q tests/test_phase5ah_trades_api_cutover_probe.py tests/test_trades_api_shadow_compare.py tests/test_phase5ag_trades_open_runtime_adapter_probe.py tests/test_trades_api_broker_truth.py
19 passed
```

Live probes:

```text
Phase 5AH cutover probe: COMPLETE_POSITIVE
  all exact_match=true, accepted=true, old_rows=50, new_rows=50
  open exact_match=true, accepted=true, old_rows=5, new_rows=5
  closed exact_match=true, accepted=true, old_rows=50, new_rows=50

Phase 5AG open runtime adapter probe: COMPLETE_POSITIVE
Phase 5AE trades API parity probe: COMPLETE_POSITIVE
Phase 5K live-path parity probe: COMPLETE_POSITIVE
Phase 5I post-rename soak probe: COMPLETE_POSITIVE
```

Phase 5K-C runtime recovery also completed before this slice: the Coinbase-cap
envelope flag was reset to false, Postgres health was verified, both Phase 5K
and Phase 5I probes passed, then the flag was retried with an autotrader-only
restart. The retry remained stable and both probes stayed
`COMPLETE_POSITIVE`.

## Architect verdict

This closes the Phase 5AH/5AI `/trades` route parity caveat. The API cutover is
now exact for all, open, and closed views under the flag.

The remaining operational issue is source-of-truth hygiene: the live `chili`
web container is intentionally running from a clean worktree because the live
root is dirty and behind the merged route code. The next slice should either
make the route flag's posture permanent from a reconciled source of truth or
explicitly document the runtime override as a temporary deployment shape before
continuing deeper relation-symbol cleanup.
