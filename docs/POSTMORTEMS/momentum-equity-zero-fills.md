# Postmortem: momentum equity lane — zero fills across 168 sessions

**Status:** root cause identified and fixed (2026-06). Keep for institutional memory.

## Root cause

The equity momentum lane NEVER cleanly filled because entries used **MARKET
orders that cross the spread of Ross-style low-float explosive names**, whose
average spread was **~4.6%**. The cost gates correctly rejected those entries
— the gates were protective, not broken. Replay profits were optimistic
ceilings because replays did not pay the spread.

## What was repeatedly misdiagnosed

- "The gates are too strict" — false; the gates priced the spread correctly.
- "Eligibility leak" — disproven; certified-pattern SUPPLY was the constraint.
- Broker/API connectivity — never the cause.

## The fix

- **Marketable-LIMIT entries** (Ross Cameron's own method) instead of market
  orders, plus a **selection-time liquidity floor** (dollar-volume / spread
  caps in viability).
- Where to look first when fills regress: `momentum_automation_outcomes`
  (terminal_state / exit_reason distribution) and the viability
  execution-readiness features (`spread_bps`, book imbalance from the
  IQFeed L2 bridge) — NOT the broker adapter.

## Related

- Viability/microstructure: `app/services/trading/momentum_neural/`
- Decision-moment L2: `iqfeed_depth_snapshots` (scripts/iqfeed_depth_bridge.py)
