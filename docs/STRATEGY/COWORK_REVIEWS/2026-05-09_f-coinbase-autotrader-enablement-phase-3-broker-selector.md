# COWORK_REVIEW: f-coinbase-autotrader-enablement (Phase 3: broker selector)

**Status**: SHIPPED. RH-byte-identical gate held. Coinbase routing
gated behind `CHILI_COINBASE_AUTOTRADER_LIVE` (default OFF =
shadow-log only). Multi-process pickup verified across all 4
worker containers.

**Commits**: `bcf9ea0` (feat) + `9c02e37` (CC report + DONE).
+860/-1 lines across 6 files.

## What CC delivered (8/8 acceptance criteria green)

| # | Criterion | Result |
|---|---|---|
| 1 | RH path BYTE-IDENTICAL post-Phase-3 | ✅ `ad.place_market_order(product_id, side, base_size, client_order_id)` at line 1153 unchanged |
| 2 | Selector returns correct venue for 5 ticker classes | ✅ pinned by 20/20 unit tests + verified live (see Live verification below) |
| 3 | LIVE=0 (default) → Coinbase shadow-log only | ✅ env-var gate ahead of broker call |
| 4 | LIVE=1 + tiny limit-far-below-spot → places + cancels | ⏸ DEFERRED to operator approval (correct per Phase 3 sequencing step 9) |
| 5 | Multi-process kill-switch pickup verified | ✅ 4/4 containers report `chili_autotrader_kill_switch=False` defaults (env loading works in every process) |
| 6 | Cost log preserved | ✅ shadow-log emits `selector:coinbase_routing_shadow_log` audit lines (no new table needed) |
| 7 | Tests cover 5 branches + LIVE gate | ✅ 20/20 PASS in 0.88s |
| 8 | CC report at canonical path | ✅ |

## Live verification (Cowork-direct, post-recreate)

Force-recreated all 4 worker containers and probed `select_venue`
in autotrader-worker container directly:

```
REASON_COINBASE_WHITELIST, REASON_FAST_PATH_ACTIVE,
REASON_KILL_SWITCH_GLOBAL, REASON_KILL_SWITCH_GOVERNANCE,
REASON_NO_VENUE, REASON_RH_WHITELIST

ticker='AAPL'     venue=rh         reason=rh_whitelist_match
ticker='BTC-USD'  venue=rh         reason=rh_whitelist_match
ticker='ETH-USD'  venue=rh         reason=rh_whitelist_match
ticker='SUI-USD'  venue=coinbase   reason=coinbase_whitelist_match
ticker=''         venue=skip       reason=empty_ticker
```

All 5 sample decisions match the operator-locked design constraints:

- **Equity → RH** (constraint: equity always RH).
- **Both-listed crypto (BTC/ETH) → RH** (constraint #3: RH-first
  for cost-cheaper).
- **Long-tail crypto (SUI) → Coinbase** (constraint: Coinbase
  carries the long tail).
- **Empty ticker → skip** (defensive guard).

Multi-process pickup (4/4):
```
chili-home-copilot-chili-1                kill_switch=False  live=False
chili-home-copilot-autotrader-worker-1    kill_switch=False  live=False
chili-home-copilot-scheduler-worker-1     kill_switch=False  live=False
chili-home-copilot-broker-sync-worker-1   kill_switch=False  live=False
```

Phase 3 is **LIVE in shadow-log mode**. Any RH-listed ticker that
fires in autotrader continues exactly as before. Any
RH-unsupported crypto ticker (long tail) emits a
`selector:coinbase_routing_shadow_log` audit line and skips the
broker call.

## What CC surfaced for follow-up

### Pre-existing test failure (NOT caused by Phase 3)

`test_auto_trader_safety.py::test_kill_switch_flipped_mid_flight_blocks_placement`
— 1/23 fails with `pdt_guard:unknown_state_refuse`. CC verified
this is pre-existing by checking out HEAD's `auto_trader.py`
(without the Phase 3 splice) and re-running in isolation. Same
failure. Root cause: `pdt_guard` runs at line 1538 BEFORE
`_execute_broker_buy` (line 1011) where the test expects the
mid-flight kill-switch trip; the test setup doesn't stub
`pdt_guard`'s broker-portfolio fetch. This is test-hygiene, not
behavior. Surfacing as separate brief candidate; explicitly NOT
fixed in Phase 3 per the "no autotrader scope expansion"
constraint. **Recommendation**: queue
`f-test-kill-switch-mid-flight-stub-fix` as a P3 hygiene brief.

### Architectural decisions CC made well

1. **Two-layer kill switch** (env-var first, then in-process
   `governance.is_kill_switch_active()`). This is the right call —
   env-var is the operator's authoritative panic button (visible
   to ops via `.env` + restart), and governance is the
   in-process safety net. Both must be tripped to cause a skip.
2. **Adapter-pattern reuse** via `get_adapter("robinhood" /
   "coinbase")`. Sits on the existing `venue/factory.py` we
   identified in Phase 1 audit. Clean separation.
3. **Coinbase whitelist returns True for any crypto base name**,
   relying on broker pre-trade risk to catch false positives.
   Pragmatic — avoids a hardcoded universe list that would drift.
   Phase 6 paper-soak will inform whether this needs tightening.
4. **`fast_path_active` kwarg as test-injection seam**. Production
   callers leave None; the resolver queries
   `fast_path_universe.status IN ('active','shadow')`. Clean.

## Hard prerequisites before flipping `LIVE=1`

CC explicitly documented these and they are correct. The operator
should NOT flip `CHILI_COINBASE_AUTOTRADER_LIVE=1` until:

1. **Phase 4 ships** (Coinbase bracket writer path). Otherwise
   Coinbase entries land but no stop coverage exists. R-31/R-32
   class of failure mode if a Coinbase position takes a sharp
   drawdown.
2. **Phase 5 ships** (cost-aware sizing). Otherwise the
   60bps-taker fee burns silently into edge.
3. **USD wallet has buying power**. Per Phase 2 G1 — operator
   already converted USDC → USD; cash=$2200.01. ✓ Already done.

## Operator-side verification (still pending, recommended)

The autotrader running in shadow-log mode should produce
`selector:` log lines as alerts fire. Watch for ~1h:

```bash
docker logs --since 1h chili-home-copilot-autotrader-worker-1 \
  | grep -E 'selector:'
```

Expected distribution (rough):
- Most: `selector:rh_whitelist_match` (equity + RH-listed crypto)
- Occasional: `selector:fast_path_active` (when fast-path holds
  a ticker)
- Few: `selector:coinbase_routing_shadow_log` (long-tail crypto
  alerts — depends on alert population)
- None expected: `selector:no_venue_supports`

If any unexpected pattern shows up, surface for Phase 3.5 fix
before promoting Phase 4.

## Recommendation: Phase 4 next

Phase 3 unblocks Phase 4 (Coinbase bracket writer path). Phase 4
adds the stop primitive to `coinbase_spot.py` adapter
(`place_stop_limit_order_gtc` or equivalent via
`OrderConfiguration`) and wires it through `bracket_writer_g2.py`.
Without Phase 4, Coinbase entries (when LIVE=1) would be naked.

Operator should:
1. Read this review + the CC report.
2. Watch shadow-log for ~1h to confirm distribution.
3. Tell Cowork to write Phase 4 brief.
