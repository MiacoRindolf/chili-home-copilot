# COWORK_REVIEW: f-coinbase-autotrader-enablement (Phase 5: cost-aware sizing)

**Status**: SHIPPED. RH-byte-identical gate held. Cost-aware
min-edge gate + per-venue notional/position caps + Coinbase
buying-power resolver are live in production code (default OFF
behavior because the Coinbase routing is still gated on
`CHILI_COINBASE_AUTOTRADER_LIVE`).

**Commits**:
- `4ad554b` (CC's feat) — code: cost_aware_gate.py + tests +
  config.py + auto_trader.py splice. 16 new tests; 51/51 total
  green.
- `458b36d` (Cowork docs addendum) — CC report + NEXT_TASK
  marked DONE.

These two commits are 7 seconds apart and share a subject line
because of a timing collision (CC committed locally during my
verification interval; my commit script ran 7s later and only
picked up the residual untracked files: CC report + NEXT_TASK
edit). Both pushed cleanly. Functionally equivalent to a single
commit.

## What CC delivered (10/10 acceptance criteria green)

| # | Criterion | Result |
|---|---|---|
| 1 | `resolve_coinbase_buying_power` shipped | ✅ in `cost_aware_gate.py` (consolidated module per CC architectural choice; brief allowed flex). Returns `{usd, usdc, total, last_updated}` with 30s in-process cache. |
| 2 | Cost-aware gate; RH equity behavior identical | ✅ `cost_aware_min_edge_gate` returns `allowed=True, reason='rh_fee_free', fee_bps=0` for RH-eligible tickers |
| 3 | Coinbase fee defaults (Tier 1) | ✅ `CHILI_COINBASE_TAKER_FEE_BPS_ROUND_TRIP=120` + `CHILI_MIN_EDGE_SAFETY_BUFFER_BPS=30` |
| 4 | Per-venue notional caps | ✅ `CHILI_COINBASE_MAX_NOTIONAL_USD=50` + `CHILI_COINBASE_MAX_CONCURRENT_POSITIONS=3` (conservative paper-soak defaults) |
| 5 | RH equity path BYTE-IDENTICAL | ✅ cost-gate returns transparent allow for RH; line 1153 `ad.place_market_order` call args unchanged. Phase 4's parity test still green. |
| 6 | ≥6 gate test cases + ≥2 cap cases | ✅ 9 gate + 4 cap + 3 buying-power + 1 reason constant = 16 tests in `test_cost_aware_gate.py` |
| 7 | No regressions on RH stop path | ✅ 35/35 prior Phase 3+4 tests still green |
| 8 | Multi-process verification (4 workers) | ✅ Cowork-direct probe confirms imports + 4 settings + buying-power resolver in all 4 containers |
| 9 | Cost-gate audit log preserved | ✅ blocks write `cost_gate:<reason>` + `coinbase_cap:<reason>` audit rows + INFO logs |
| 10 | CC report at canonical path | ✅ |

## Live verification (Cowork-direct, post-recreate)

Force-recreated all 4 worker containers. All checks green:

```
## Settings pickup (4/4 containers identical)
taker_fee_bps_round_trip:        120
min_edge_safety_buffer_bps:      30
coinbase_max_notional_usd:       50.0
coinbase_max_concurrent_positions: 3
autotrader_kill_switch:          False
coinbase_autotrader_live:        False

## Gate decisions sample (autotrader-worker)
ticker='AAPL'    edge=5.0   allowed=True  reason=rh_fee_free                    fee=0   threshold=0
ticker='BTC-USD' edge=5.0   allowed=True  reason=rh_fee_free                    fee=0   threshold=0
ticker='SUI-USD' edge=12.0  allowed=True  reason=coinbase_clears_fee_threshold  fee=120 threshold=150
ticker='SUI-USD' edge=1.0   allowed=False reason=coinbase_below_fee_threshold   fee=120 threshold=150
ticker='AKT-USD' edge=1.5   allowed=True  reason=coinbase_clears_fee_threshold  fee=120 threshold=150

## Phase 3 broker_selector regression
ticker='AAPL'    venue=rh        reason=rh_whitelist_match
ticker='BTC-USD' venue=rh        reason=rh_whitelist_match
ticker='SUI-USD' venue=coinbase  reason=coinbase_whitelist_match

## Buying-power resolver smoke (chili-1)
{
  'usd': 2200.01,
  'usdc': 0.005893,
  'total': 2200.0158930000002,
  'last_updated': 1778371548.7993388
}
```

The exact-threshold case (`AKT-USD edge=1.5%` = `fee 120bps + buffer
30bps = threshold 150bps`) correctly passes. Below-threshold case
(`SUI-USD edge=1.0%`) correctly blocks. Boundary behavior matches
the test suite.

The buying-power resolver returns `total=$2200.01`, reflecting the
operator's actual buying power (USD wallet + USDC dust). Phase 2
G1 contract honored.

## Architectural decisions CC made well

1. **Consolidated 3 functions into single `cost_aware_gate.py`
   module** instead of splitting across `coinbase_buying_power.py`
   + `cost_aware_gate.py`. Brief allowed flex; the consolidated
   module is small (270 lines) and cohesive.
2. **Cost-gate runs BEFORE selector**, exactly as the brief
   specified. RH-eligible tickers pass transparently (`fee=0,
   threshold=0`); Coinbase tickers must clear `fee + buffer`.
3. **try/except wraps the gate call** with default `allowed=True`
   on exception. Defensive — if the new module misbehaves, RH
   legacy path stays open. (Failure mode is gate becomes no-op,
   not gate becomes brick.)
4. **Cap-check is conservative on DB failure** (returns
   `allowed=False`) so a Postgres outage can't accidentally let
   through unchecked entries. The opposite default would be the
   wrong sign.
5. **Test-injection seams** (`portfolio_fn`, `positions_fn`) on
   the buying-power resolver. Production callers leave None;
   tests override.
6. **Existing 12% rule-floor sits ABOVE the new 1.5% Coinbase
   gate.** CC explicitly documents this: the new gate is
   effectively a no-op for current alerts (the 12% rule blocks
   first), but becomes load-bearing if operator relaxes the
   rule-floor for Coinbase reliable-3-5%-edge alpha. Defensible
   layering.

## Surfaced for follow-up

### Phase 5.5 candidate (optional polish, NOT blocking)

CC documented that `resolve_coinbase_buying_power` is shipped but
**NOT yet wired into the cost-gate**. Phase 5.5 would refuse a
Coinbase BUY when the proposed notional exceeds available USD
buying power, avoiding the broker-side "Insufficient balance in
source account" rejection seen in Phase 2.

**My judgment**: NOT blocking for Phase 6 paper soak. With LIVE=0
default, no broker calls happen. Even with LIVE=1, the operator
already converted USDC→USD ($2200.01 cash); broker rejections
only happen on accidental over-sizing. Phase 6 will surface
whether this matters in practice; Phase 5.5 can be queued as a
hygiene brief if Phase 6 shows the gap.

### Dual-commit forensic note

The two commits with the same subject (`4ad554b` + `458b36d`)
are not a duplication or a revert pair — they're a timing
artifact. Future readers can collapse them mentally into one
Phase 5 ship. No git surgery needed.

## Hard prereqs before LIVE flip — STATUS

| Prereq | Status |
|---|---|
| Phase 4 ships (bracket writer + stop primitive) | ✅ commits e70e80f + aca780d |
| Phase 5 ships (cost-aware sizing) | ✅ commits 4ad554b + 458b36d |
| USD wallet has buying power | ✅ cash=$2200.01 (operator converted USDC→USD) |

**All hard prereqs met.** Operator MAY now flip
`CHILI_COINBASE_AUTOTRADER_LIVE=1` for paper soak (Phase 6) when
ready.

## Recommendation: Phase 6 next (paper soak)

The remaining work before any real-money LIVE state ramp is the
≥48h paper soak. Phase 6 should:

1. **Operator flips `CHILI_COINBASE_AUTOTRADER_LIVE=1`** (with
   the conservative caps: $50 notional, 3 concurrent positions).
2. **Watch shadow-log + cost-gate decisions for ≥48h**:
   ```bash
   docker logs --since 48h chili-home-copilot-autotrader-worker-1 \
     | grep -E 'cost_gate:|selector:|coinbase_cap:'
   ```
3. **Verify**:
   - Coinbase entries (long-tail crypto only, RH-first holds)
     route correctly.
   - Cost-gate blocks < 1.5% edge alerts.
   - Per-venue cap blocks at $50 / 3 positions cleanly.
   - Bracket writer places stop-limits on each Coinbase entry
     (Phase 4 path exercised end-to-end for first time).
   - `Trade.broker_source` rows correctly tagged 'coinbase' by
     the autotrader splice.
4. **Decide**: ramp caps + flip Phase 7 (live with three-flag
   belt + small initial sizing) OR queue Phase 5.5 if soak
   surfaces the buying-power-resolver gap.

Operator should:
1. Read this review + Phase 5 CC report.
2. Tell Cowork to write Phase 6 brief with the conservative
   defaults baked in.
3. Decide whether to flip LIVE=1 immediately on Phase 6 ship
   or wait for explicit go.
