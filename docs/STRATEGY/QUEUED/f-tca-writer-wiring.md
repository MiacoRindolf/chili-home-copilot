# QUEUED: f-tca-writer-wiring

> **STATUS: SHIPPED 2026-05-18.** Investigation found writer infrastructure intact; root cause was `auto_trader.py:2233` Trade-constructor missing the `tca_reference_entry_price=px` kwarg + 638/638 trades had `strategy_proposal_id=NULL` (autotrader path bypasses the proposal-driven alerts.py write). Code fix in auto_trader.py + mig 251/252/253 backfill from `trading_breakout_alerts`. Result: 285/638 trades populated. **Average entry slippage: +102 bps** — see CC_REPORT for the architectural follow-ups (maker-only Coinbase, tighter entry-price gating, reference-price re-snap at place time).

**Origin:** 2026-05-18 architect audit. The trading_trades table has columns `tca_reference_entry_price`, `tca_entry_slippage_bps`, `tca_reference_exit_price`, `tca_exit_slippage_bps` — but **zero rows are populated in the last 90 days** (verified via dispatch-arch-pnl probe). Slippage cost is currently invisible. On a pattern with avg return 1.68%/trade (pattern 585), an unmeasured 50-100bps round-trip can be eating half the gross edge.

## Goal

End-to-end: every closed trade in `trading_trades` since this brief lands has `tca_entry_slippage_bps` AND `tca_exit_slippage_bps` populated. Operator can then run "what's our after-cost edge per pattern?" with a single SQL.

## Why now

Tier A shipped (payoff_ratio gate + 585 restore + composite floor). The next-highest-leverage observability gap is slippage. Without it we cannot answer:

- Is pattern 585's +1.68%/trade edge after-cost positive on Coinbase (120bps round-trip + spread)?
- Are the −$459 stop-out losses on small-caps (AIFF / AIXI / BNRG cluster) explained by entry slippage or post-entry market move?
- Should `auto_trader.py`'s rule gates penalize tickers with historically high slippage?

## Investigation

Phase 1 (read-only, 30 min): identify where `tca_entry_slippage_bps` is INTENDED to be written.

```
grep -rn "tca_entry_slippage_bps" app/ scripts/
grep -rn "tca_reference_entry_price" app/
```

Phase 2: explain why it isn't running.

Candidates: (a) the writer exists but the caller is gated by a flag that defaults off; (b) the writer assumes a reference-price API that's not connected; (c) the columns predate any writer (dead schema).

Phase 3: ship a working writer.

If (a): flip the flag default and document in the brief.
If (b): pick a reference-price source. Polygon.io NBBO snapshot at fill timestamp is the standard. CHILI already has Polygon plumbing in the market-data priority chain.
If (c): write a new TCA service module. Pattern:
  - At fill time, capture broker fill price + a reference NBBO snapshot for the same instant.
  - Compute bps slippage as `(fill_px - ref_px) / ref_px * 10_000 * direction_sign`.
  - Write into the column. Same at exit.
  - Backfill helper for historical trades using best-effort NBBO data.

## Brain integration (reuse, don't rewrite)

- `app/services/trading/execution_audit.py` — already logs broker fill events; tap into the same write path.
- `app/services/market_data.py` or whichever module owns the Polygon NBBO endpoint — for the reference quote.
- `trading_venue_truth_log` — the existing cost-drift table; if the writer already populates this, maybe TCA columns are just a derived projection.

## Constraints / do not touch

- **No live broker behavior change.** This is observability only — readers compute on data written by the existing fill path.
- **Backwards-compat with NULL.** The existing 800+ pre-fix trades stay NULL; readers must tolerate.
- **No magic defaults.** If reference price is unavailable for a fill, the slippage column stays NULL (NULL propagation per `feedback_no_hardcoded_fallbacks`).

## Out of scope

- Adding a slippage-based demote gate (Tier C item, post-TCA-population).
- Pre-trade slippage prediction (Phase 4+ — first measure, then predict).
- Per-pattern slippage rollups on `scan_patterns` (similar to payoff_ratio, but needs a populated TCA dataset first).

## Success criteria

- New TCA rows accumulate at the cadence of new fills (verifiable: `SELECT COUNT(*) FROM trading_trades WHERE tca_entry_slippage_bps IS NOT NULL AND created_at > NOW() - INTERVAL '24 hours'`).
- Distribution looks plausible (entry slippage typically 1-50bps for liquid names, higher for small-caps and crypto).
- A one-shot helper script populates the back-90d window (best-effort; allowed to be partial if NBBO history is gappy).

## Rollback plan

If the writer crashes or produces wild values:

1. `git revert` the writer commit.
2. `UPDATE trading_trades SET tca_entry_slippage_bps=NULL, tca_exit_slippage_bps=NULL WHERE tca_*_updated_at > '<deploy_ts>'` (or whichever sentinel timestamp).
3. Setting flag to off, leave the column populated for whatever did succeed.

## Estimated complexity

- Phase 1 investigation: 30 min.
- Phase 2 root cause: 30-60 min.
- Phase 3 writer impl: 2-4 hours depending on whether reference-price source needs new plumbing.
- Test + deploy: 1 hour.

Single CC session is plausible if Phase 2 finds an existing writer just needs unblocking.
