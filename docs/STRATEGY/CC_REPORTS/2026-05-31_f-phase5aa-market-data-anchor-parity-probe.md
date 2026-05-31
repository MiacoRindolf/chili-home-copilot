# Phase 5AA - Market-Data Anchor Parity Probe

Date: 2026-05-31

## Summary

Added a read-only parity probe for
`market_data._resolve_implausibility_anchor(...)`, specifically the database
fallback that uses the most-recent open trade entry price when the in-memory
known-good quote cache has no ticker entry.

This path sits inside `fetch_quote(...)`'s implausible-quote boundary guard, so
it can affect live market-data behavior. The probe compares:

- old source: `trading_trades` compatibility view
- new candidate source: physical `trading_management_envelopes` table

No runtime behavior changed.

## Live Result

Manual live run:

```text
VERDICT_STATUS=COMPLETE_POSITIVE
VERDICT_REASON=8 market-data anchor checks matched
RELATION_KINDS={'trading_management_envelopes': 'r', 'trading_trades': 'v'}
ANCHOR_TICKERS=8
ANCHOR_MISMATCHES=0
```

Matched tickers: `AAOX`, `ABT-USD`, `ALCX-USD`, `COOKIE-USD`, `QNT-USD`,
`SAFE-USD`, `SENT-USD`, `SUP-USD`.

## Verification

- `python -m py_compile scripts\d-phase5aa-market-data-anchor-parity-probe.py`
- `pytest tests\test_phase5aa_market_data_anchor_parity_probe.py -q`
- Live run with `PHASE5AA_ALLOW_LIVE_PROBE=true` and live `DATABASE_URL`

Result: 6 tests passed and live probe emitted `COMPLETE_POSITIVE`.

## Architect Verdict

The anchor source is safe to convert in a separate tiny slice. Keep the actual
runtime swap independent so rollback is obvious and the probe remains useful.

