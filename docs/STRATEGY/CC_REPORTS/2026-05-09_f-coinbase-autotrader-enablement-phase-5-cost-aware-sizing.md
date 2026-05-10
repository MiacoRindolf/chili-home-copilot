# CC_REPORT: f-coinbase-autotrader-enablement (Phase 5: cost-aware sizing)

## Outcome

Cost-aware min-edge gate + per-venue notional/position caps +
Coinbase buying-power resolver shipped. RH equity path is
**byte-identical** post-Phase-5 (cost-gate is a no-op for
RH-eligible tickers; cap-check skips the RH path entirely).
Coinbase entries (when LIVE flips) get refused if their projected
edge doesn't clear `120bps + 30bps = 150bps` (Tier-1 round-trip +
buffer), and refused again if per-venue notional or
concurrent-position caps are exceeded.

This is the **last hard prerequisite** before Phase 6 paper soak.
Operator can flip `CHILI_COINBASE_AUTOTRADER_LIVE=1` once they've
converted USDC → USD in Coinbase UI (per Phase 2 G1) OR until the
Phase 7 brief teaches CHILI to read USDC quantity for buying power.

## Per-step status

### Step 1 — Survey + RH parity capture — COMPLETE

* `auto_trader.py` 1743 lines (post-Phase-4), AST clean.
* Existing min-edge floor lives in
  `auto_trader_rules.py:111` (`min_projected_profit_pct = 12.0`).
  `snap["projected_profit_pct"]` carries the per-alert value into
  `_execute_broker_buy`.
* Phase 3's selector splice at line 1087+ is the natural insertion
  point for the cost-gate (which the brief specifies must run
  BEFORE the selector).

### Step 2 — Settings + helpers shipped

`config.py` (+34 lines):

* `chili_coinbase_taker_fee_bps_round_trip` (default 120,
  `CHILI_COINBASE_TAKER_FEE_BPS_ROUND_TRIP`). Tier-1 60bps × 2.
* `chili_min_edge_safety_buffer_bps` (default 30,
  `CHILI_MIN_EDGE_SAFETY_BUFFER_BPS`). Cushion above raw fee.
* `chili_coinbase_max_notional_usd` (default 50.0,
  `CHILI_COINBASE_MAX_NOTIONAL_USD`). Conservative paper-soak floor.
* `chili_coinbase_max_concurrent_positions` (default 3,
  `CHILI_COINBASE_MAX_CONCURRENT_POSITIONS`).

`cost_aware_gate.py` (new module, ~270 lines):

* `resolve_coinbase_buying_power(*, force_refresh=False,
  portfolio_fn=None, positions_fn=None)` — returns
  `{usd, usdc, total, last_updated}`. 30s in-process cache. Reads
  `cash` (USD wallet) AND USDC quantity from `get_positions()` so
  the total reflects actual buying power per Phase 2 G1.
  Test-injection seams: `portfolio_fn` / `positions_fn`.
* `cost_aware_min_edge_gate(*, ticker, projected_profit_pct,
  settings_=None)` — returns `CostGateDecision(allowed, reason,
  fee_bps, threshold_bps, edge_bps)`. RH-eligible tickers always
  pass with fee=0 (no behavior change vs pre-Phase-5). Coinbase
  tickers must clear `fee_bps + buffer_bps`.
* `per_venue_cap_check(*, venue, proposed_notional_usd, db,
  user_id=None, settings_=None)` — returns `CapDecision(allowed,
  reason, current_positions, current_notional_usd)`. Independent
  per-venue per Phase 1 design constraint #1. `venue='robinhood'`
  is a no-op (RH has its own size/heat gates upstream).

### Step 3 — Per-venue notional cap helper — COMPLETE

Folded into `cost_aware_gate.py:per_venue_cap_check`. Reads open
trades from `trading_trades` filtered to
`LOWER(broker_source) = 'coinbase'`, sums notionals, counts
positions. Conservative on DB failure: returns `allowed=False`
(refusal) so an outage can't accidentally let through unchecked
entries.

### Step 4 — Tests shipped (16 tests, all green)

`tests/test_cost_aware_gate.py`:

**Gate cases (9 tests, brief required ≥6):**
1. RH equity → fee=0, allowed.
2. RH whitelisted crypto (BTC/ETH/ADA/DOGE) → fee=0, allowed.
3. Coinbase high edge (12% >> 150bps) → allowed.
4. Coinbase right at threshold (1.5% = 150bps) → allowed.
5. Coinbase below threshold (1.0%) → BLOCKED.
6. Coinbase with `projected_profit_pct=None` → BLOCKED (treated
   as 0bps).
7. Empty ticker → no-venue.
8. Buffer setting changes threshold.
9. Higher tier (e.g. 30bps round-trip) lowers floor.

**Cap cases (4 tests, brief required ≥2):**
1. No open positions → allowed.
2. Notional cap exceeded → BLOCKED.
3. Position-count cap exceeded → BLOCKED.
4. RH venue → no-op (cap module doesn't enforce on RH).

**Buying-power resolver (3 tests):**
1. Aggregates USD + USDC into total.
2. Phase 2 G1 fingerprint (cash=$0, USDC=$2200) → total=$2200.
3. Resilient to fetch failure (returns zero, doesn't raise).

**Reason constants pinned (1 test).**

### Step 5 — auto_trader.py splice (+73 lines, AST clean)

Two surgical insertions:

1. **Cost-gate BEFORE selector** (line 1087+ pre-Phase-5; now
   ~1130): cost gate runs first. RH-eligible tickers pass
   transparently; Coinbase-only tickers must clear the threshold.
   Block writes structured `cost_gate:<reason>` audit row + INFO
   log with edge/threshold/fee bps. Wrapped in try/except so a
   gate failure defaults to allowed=True (defensive — RH legacy
   path stays open even if the new module misbehaves).

2. **Cap-check INSIDE Coinbase routing branch** (after the
   shadow-log gate, before adapter instantiation): per-venue cap
   check fires only when LIVE=1 routing has already cleared the
   shadow-log gate. Block writes `coinbase_cap:<reason>` audit
   row + INFO log with current positions + notional. Wrapped in
   try/except.

RH path call args at line 1153 (`ad.place_market_order(product_id,
side, base_size, client_order_id)`) UNCHANGED. The cost-gate
inserts BEFORE the existing selector + line 1153 chain; for RH
tickers it returns allowed=True with reason='rh_fee_free' and
flow proceeds identically.

### Step 6 — Pytest + verification — IN PROGRESS

* 16/16 cost_aware_gate tests PASS (helper-level, ~quick;
  cap-tests use the chili_test `db` fixture).
* 35/35 prior Phase-3+4 tests still green (broker_selector,
  bracket_writer venue routing, coinbase stop primitive).

Multi-process verification + live single-paper-test deferred to
operator-side per the brief.

## Surprises / deviations

1. **Cost-gate is conservatively additive, not replacing the
   existing 12% min-projected gate.** The existing `auto_trader_rules`
   floor (12%) sits ABOVE the new cost-gate threshold (1.5% for
   Tier-1 Coinbase). For current Coinbase entries the new gate is
   effectively a no-op because the existing floor blocks first.
   The new gate becomes load-bearing IF/WHEN operator relaxes
   the rule-floor for Coinbase (e.g., for alpha that reliably
   yields 3-5%); the cost-gate independently enforces the fee
   floor as a separable safety belt.

2. **Per-venue cap is conservatively low for paper soak.** Default
   $50 notional + 3 concurrent positions. With Coinbase Tier-1
   60bps taker, a $50 notional position costs $0.30 to open + $0.30
   to close. Three concurrent positions at $50 each = $150 total
   exposure. Operator raises after Phase 6 paper soak validates
   the chain.

3. **Buying-power resolver NOT yet wired into the autotrader.**
   The function is shipped + tested but the Phase-3 splice doesn't
   yet read it. The brief asks for the resolver as a deliverable;
   wiring it into the cost-aware gate (e.g., refusing if buying
   power < proposed notional) is a Phase 5.5 hygiene addition or
   Phase 6 paper-soak observability tweak. Surfaced for operator
   judgment.

4. **`Trade.broker_source = 'coinbase'` defaults via the side-
   channel from Phase 4.** Cap-check correctly counts these rows
   via `LOWER(broker_source) = 'coinbase'`. Verified by test
   `test_cap_position_count_exceeded_blocks` which seeds 3 such
   rows.

## Constraints honored

* ✅ **RH equity path BYTE-IDENTICAL.** Cost-gate returns
  `allowed=True, fee=0` for RH-eligible tickers; selector +
  adapter call args unchanged.
* ✅ **No new entry signal logic.** Phase 5 is sizing-side only
  (gate + cap).
* ✅ **No changes to `coinbase_spot.py` adapter.**
* ✅ **No `CHILI_COINBASE_AUTOTRADER_LIVE=1` flip.** Operator-
  controlled.
* ✅ **No paper-soak.** Phase 6.
* ✅ Splice + edit pattern with `wc -l` + `ast.parse` after every
  edit; no truncation regressions.
* ✅ Operator-locked Phase 1 design constraints unchanged.

## Verification

* `cost_aware_gate.py`: 270 lines; AST clean.
* `auto_trader.py`: 1743 → 1816 (+73); AST clean.
* `config.py`: +34 lines (4 new settings).
* All 51 tests PASS (16 new cost-gate + 35 prior Phase 3+4).
* RH parity gate held (Phase 4's
  `test_rh_equity_stop_call_args_byte_identical` continues green).

## Operator-side after Phase 5 ships

1. `git pull` + truncation scan.
2. `docker compose up -d --force-recreate chili autotrader-worker
   scheduler-worker broker-sync-worker`.
3. Quick import sanity:
   ```bash
   docker exec chili-home-copilot-chili-1 python -c \
     "from app.services.trading.cost_aware_gate import \
       cost_aware_min_edge_gate, per_venue_cap_check, \
       resolve_coinbase_buying_power; \
      r=cost_aware_min_edge_gate(ticker='AKT-USD', \
       projected_profit_pct=1.0); \
      print('low-edge AKT block:', r)"
   ```
   Expected: `allowed=False, reason='coinbase_below_fee_threshold'`.
4. **Decide whether to flip `CHILI_COINBASE_AUTOTRADER_LIVE=1`**
   for paper soak. Prerequisites:
   - USDC → USD conversion in Coinbase UI (Phase 2 G1) — OR — wait
     for Phase 5.5 USDC-aware buying-power wiring.
   - Verify the conservative caps ($50 notional, 3 positions) are
     acceptable for the paper-soak window.
5. Watch shadow-log + cost-gate decisions:
   ```bash
   docker logs --since 1h chili-home-copilot-autotrader-worker-1 \
     | grep -E 'cost_gate:|selector:|coinbase_cap:'
   ```

## Rollback plan

* **Cost-gate misbehaves**: `git revert` only the auto_trader.py
  splice commit; `cost_aware_gate.py` module + tests stay (unused).
  RH path returns to pre-Phase-5 (no-op).
* **Notional cap blocks legit entries**: raise
  `CHILI_COINBASE_MAX_NOTIONAL_USD` in `.env` + force-recreate.
* **Buying-power resolver hangs**: 30s cache fallback; on persistent
  failure, gate's try/except returns allowed=True so RH legacy
  doesn't break.
* **Catastrophic**: `CHILI_COINBASE_AUTOTRADER_LIVE=0` halts all
  Coinbase routing without code revert; RH unaffected.

## What's NEXT

* **Phase 6**: paper-trade soak (≥48h). Operator verifies cost-
  gate decisions match expectations + per-venue caps stay
  comfortable.
* **Phase 7**: live with three-flag belt + small initial sizing
  (≤$10/order). Ramp after operator-side post-soak review.
* **Phase 5.5 (optional)**: wire `resolve_coinbase_buying_power`
  into the cost-gate so entries refuse on insufficient buying
  power (avoids the Phase 2 G1 "Insufficient balance in source
  account" rejections at the broker).
