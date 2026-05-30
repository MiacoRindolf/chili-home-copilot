# f-position-identity-phase-5k-h-alpha-portfolio-gate-reader-flag

Date: 2026-05-30

Status: SHIPPED default-off. Live flip pending.

## What Changed

Added a default-off Phase 5K reader flag to
`app/services/trading/alpha_portfolio_gate.py`:

- `CHILI_PHASE5K_ALPHA_PORTFOLIO_GATE_USE_ENVELOPES=false` (default)
- OFF reads `trading_trades` compatibility view
- ON reads `trading_management_envelopes` physical base table

The switched reader surface is the realized-trade aggregate inside
`_load_pattern_rows`, used by `scan_alpha_portfolio` and the maintenance path.

No candidate scoring math, portfolio crowding penalty, recert rules, lifecycle
staging behavior, or write paths changed.

## Verification

Focused tests:

```text
python -m pytest tests\test_phase5k_alpha_portfolio_gate_reader_flag.py tests\test_alpha_portfolio_gate.py::test_scan_alpha_portfolio_marks_recert_and_selects_sleeve_candidates -q
5 passed
```

Compile check:

```text
python -m py_compile app\services\trading\alpha_portfolio_gate.py
OK
```

Live Phase 5K-A parity:

```text
VERDICT_STATUS=COMPLETE_POSITIVE
PARITY_CHECKS=6
PARITY_MISMATCHES=0
```

Direct old/new gate-reader check:

```text
ALPHA_ROWS_OLD 446
ALPHA_ROWS_NEW 446
ALPHA_MATCH True
```

## Rollback

Leave or set `CHILI_PHASE5K_ALPHA_PORTFOLIO_GATE_USE_ENVELOPES=false`, then
recreate the consumer worker(s).
