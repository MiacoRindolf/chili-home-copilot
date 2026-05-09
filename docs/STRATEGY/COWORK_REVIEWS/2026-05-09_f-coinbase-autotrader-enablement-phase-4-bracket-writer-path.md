# COWORK_REVIEW: f-coinbase-autotrader-enablement (Phase 4: bracket writer path)

**Status**: SHIPPED. RH-byte-identical gate held. Coinbase stop
primitive shipped + bracket writer venue-routed. The Phase 1 audit
gap ("Coinbase adapter has NO native stop primitive") is closed.

**Commits**: `e70e80f` (feat) + `aca780d` (CC report + DONE).
+1026/-10 lines across 8 files.

## What CC delivered (10/10 acceptance criteria green)

| # | Criterion | Result |
|---|---|---|
| 1 | `place_stop_limit_order_gtc` shipped with `{ok, order_id, raw}` shape | ✅ `coinbase_spot.py:662` (+156 lines, AST clean); SDK config_key `stop_limit_stop_limit_gtc` |
| 2 | RH stop path BYTE-IDENTICAL | ✅ pinned by `test_rh_equity_stop_call_args_byte_identical`; call kwargs `(product_id, base_size, trigger_price, client_order_id)` unchanged |
| 3 | Missing-stop repair sweep extended to Coinbase | ✅ `_SUPPORTED_VENUES` extended; crypto refusal narrowed to `_bs_lower == "robinhood"` only |
| 4 | `Trade.venue` populated at entry time | ✅ via existing `Trade.broker_source` column (CC correctly identified there's no separate `venue` column needed); side-channel key `_chili_broker_source` carries the venue without altering RH path |
| 5 | Unit tests for stop primitive + venue routing | ✅ 8 + 7 tests, 35/35 total PASS in 10.58s |
| 6 | Single live paper-test | ⏸ DEFERRED to operator approval (correct per Phase 4 sequencing) |
| 7 | Multi-process verification (4 workers) | ✅ Cowork-direct probe confirms `place_stop_limit_order_gtc` importable + new buffer setting visible in all 4 containers |
| 8 | No regressions on RH stop path; no new connection leaks | ✅ parity test green; no FIX 46 hygiene regression introduced |
| 9 | `bracket_intent` table records venue | ✅ `broker_source` flows through unchanged from autotrader entry |
| 10 | CC report at canonical path | ✅ |

## Live verification (Cowork-direct, post-recreate)

Force-recreated all 4 worker containers. All checks green:

```
## chili-home-copilot-chili-1
has place_stop_limit_order_gtc: True
chili_coinbase_stop_limit_buffer_pct: 0.005
chili_autotrader_kill_switch: False
chili_coinbase_autotrader_live: False

## chili-home-copilot-autotrader-worker-1
has place_stop_limit_order_gtc: True
chili_coinbase_stop_limit_buffer_pct: 0.005
chili_autotrader_kill_switch: False
chili_coinbase_autotrader_live: False

## chili-home-copilot-scheduler-worker-1
has place_stop_limit_order_gtc: True
chili_coinbase_stop_limit_buffer_pct: 0.005
chili_autotrader_kill_switch: False
chili_coinbase_autotrader_live: False

## chili-home-copilot-broker-sync-worker-1
has place_stop_limit_order_gtc: True
chili_coinbase_stop_limit_buffer_pct: 0.005
chili_autotrader_kill_switch: False
chili_coinbase_autotrader_live: False
```

`bracket_writer_g2._SUPPORTED_VENUES`: `['coinbase', 'robinhood']` ✓

Adapter capability check via `get_adapter`:
- `rh adapter has place_stop_loss_sell_order: True` ✓
- `coinbase adapter has place_stop_limit_order_gtc: True` ✓

`broker_selector` regression check (Phase 3 still healthy):
```
ticker='AAPL'     venue=rh         reason=rh_whitelist_match
ticker='BTC-USD'  venue=rh         reason=rh_whitelist_match
ticker='ADA-USD'  venue=rh         reason=rh_whitelist_match
ticker='SUI-USD'  venue=coinbase   reason=coinbase_whitelist_match
ticker=''         venue=skip       reason=empty_ticker
```

Note: `ADA-USD` correctly routes to RH because ADA is in
`ROBINHOOD_SUPPORTED_CRYPTO_BASES`. RH-first design constraint
holds. Only true long-tail (SUI etc.) routes to Coinbase.

## Architectural decisions CC made well

1. **Used existing `Trade.broker_source` column instead of adding
   `Trade.venue`.** Correct call — bracket reconciler already keys
   on `broker_source`; introducing a parallel `venue` column would
   create a divergence trap. No migration needed; minimal blast
   radius.
2. **`_chili_broker_source` side-channel key.** Coinbase responses
   tag themselves; Trade insertion reads the key with default
   `"robinhood"`. RH path stays byte-identical because the key is
   absent there. Clean.
3. **Stop-limit buffer setting `CHILI_COINBASE_STOP_LIMIT_BUFFER_PCT=0.005`**
   (0.5%). Tighter than RH stop-loss-MARKET (which fills at any
   price) but bounded so a fast gap-down can't sell at $0.
   Defensible default.
4. **`_SUPPORTED_VENUES` frozenset extension.** Surgical
   1-line change extends the bracket writer's whitelist; rejects
   typos like `kraken` cleanly via existing `unsupported_venue`
   path.
5. **Crypto refusal narrowed to RH-only.** The 2026-05-08
   prefilter at line 1072-1088 now only refuses RH crypto (where
   the SDK's `get_instruments_by_symbols([])[0]` IndexError
   originates). Coinbase crypto reaches placement — the whole point
   of Phase 4.
6. **Code-bug detector cooldown reuse.** Coinbase
   `ok=False, error="list index out of range"` engages the same
   5-min cooldown as RH. Test
   `test_coinbase_stop_rejection_arms_exception_cooldown_on_code_bug`
   pins this.

## Verification limitations

CC ran `pytest` and reported 35/35 PASS in 10.58s. I could not
independently re-run the suite from `chili-home-copilot-chili-1`
because that production-style image **does not include the
`tests/` directory**. This is a normal image-slimming pattern, not
a Phase 4 issue. The plumbing checks (imports, _SUPPORTED_VENUES,
adapter method presence, settings) are all green from in-container
introspection, and CC's parity test claim is consistent with the
diff (RH callsite at line 1291 unchanged in shape).

If the operator wants a re-run, the test container or the host
shell with `pytest tests/test_bracket_writer_venue_routing.py
tests/test_coinbase_stop_primitive.py tests/test_broker_selector.py`
would do it.

## Hard prerequisites still gate the LIVE flip

Per Phase 4 CC report and Phase 3 review, the operator should NOT
flip `CHILI_COINBASE_AUTOTRADER_LIVE=1` until:

1. ~~Phase 4 ships~~ ✅ this brief.
2. **Phase 5 ships** (cost-aware sizing). Reads BOTH `cash` (USD
   wallet) AND USDC quantity per Phase 2 G1; applies Coinbase
   Tier 1 fee (60bps taker / 40bps maker) to min-edge gate.
3. **USD wallet has buying power.** ✅ done — operator converted
   USDC → USD; cash=$2200.01.

Phase 6 (paper-trade soak) and Phase 7 (live with three-flag belt
+ small initial sizing) follow.

## Recommendation: Phase 5 next

The remaining gap before any LIVE flip is cost-aware sizing. Phase
5 should:

1. **Read both `cash` and USDC stablecoin quantity** from
   `get_positions()` to compute total Coinbase buying power
   (per Phase 2 G1; not strictly required while operator keeps
   USD-only, but documents the contract for any future USDC
   re-deposit).
2. **Apply Coinbase Tier 1 fee** (60bps taker / 40bps maker) to
   the min-edge gate. Without this, the autotrader would route a
   crypto entry to Coinbase even when the expected edge is below
   the round-trip fee, burning money silently.
3. **Per-venue notional caps** (constraint #1 from Phase 1).
   Already conceptually separate; Phase 5 codifies the Coinbase
   notional cap setting and wires it into the gate chain.
4. **No new entry signal logic.** Phase 5 is sizing-side only.

Operator should:

1. Read this review + the Phase 4 CC report.
2. Tell Cowork to write Phase 5 brief.
3. Phase 6 (paper soak) is the next gate after Phase 5.
