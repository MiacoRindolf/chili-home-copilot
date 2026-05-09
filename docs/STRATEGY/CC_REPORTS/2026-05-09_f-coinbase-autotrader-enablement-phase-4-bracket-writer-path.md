# CC_REPORT: f-coinbase-autotrader-enablement (Phase 4: bracket writer path)

## Outcome

Coinbase stop primitive shipped + bracket writer venue-routed.
RH stop path is **byte-identical** post-Phase-4 (call args to
`place_stop_loss_sell_order` are unchanged; pinned by parity test).
Coinbase entries (when LIVE flips) now get bracket coverage via
the new `place_stop_limit_order_gtc` primitive — no naked downside
risk.

The Phase 1 audit's identified gap ("Coinbase adapter has NO
native stop primitive yet") is closed. Operator can now safely flip
`CHILI_COINBASE_AUTOTRADER_LIVE=1` once Phase 5 (cost-aware sizing)
ships and the USDC↔USD conversion is handled per Phase 2 G1.

35/35 tests PASS in 10.58s (8 stop primitive + 7 venue routing +
20 prior selector regression).

## Per-step status

### Step 1 — Truncation scan + RH parity capture — COMPLETE

* `bracket_writer_g2.py` 1572 lines, AST clean.
* `coinbase_spot.py` 1024 lines, AST clean.
* RH stop callsite at line 1291: `adapter.place_stop_loss_sell_order(
  product_id=ticker, base_size=str(float(local_quantity)),
  trigger_price=str(float(stop_price)), client_order_id=client_oid)`
  — captured for byte-identical parity test.

### Step 2 — `place_stop_limit_order_gtc` shipped — COMPLETE

`coinbase_spot.py` (+156 lines, AST clean):

* Method signature mirrors `place_limit_order_gtc` for caller
  uniformity: `(product_id, side, base_size, stop_price,
  limit_price, client_order_id=None, stop_direction=None)`.
* SDK call: `c.stop_limit_order_gtc_buy` /
  `c.stop_limit_order_gtc_sell` (per Coinbase Advanced Trade docs,
  config_key `stop_limit_stop_limit_gtc`).
* Default `stop_direction`: `STOP_DIRECTION_STOP_DOWN` for sell;
  `STOP_DIRECTION_STOP_UP` for buy.
* Envelope shape matches existing primitives: `{ok: True,
  order_id, client_order_id, raw}` on success;
  `{ok: False, error, client_order_id}` on failure.
* Idempotency-store dedupe + rate-limiter gate + state-machine
  transition recording — all parallel to `place_limit_order_gtc`.
* SDK exceptions caught and packaged as `ok=False` with the
  exception message (the bracket writer's `_is_code_bug_error`
  detector arms its 5-min cooldown on these).

### Step 3 — `tests/test_coinbase_stop_primitive.py` (8 tests, all green in 1.20s)

* Sell stop-loss success envelope shape + SDK call args.
* Buy stop default direction (STOP_UP).
* Failure response packaged as ok=False.
* SDK exception caught + packaged as ok=False.
* Invalid side rejected without SDK call.
* Duplicate client_order_id short-circuits.
* Disabled adapter short-circuits.
* Rate-limited returns canonical response.

### Step 4 — bracket_writer_g2.py venue routing splice — COMPLETE

3 surgical changes (+31 lines, AST clean):

1. **`_SUPPORTED_VENUES` extended** to
   `frozenset({"robinhood", "coinbase"})`.
2. **Crypto refusal narrowed to RH-only**: the 2026-05-08
   prefilter at line 1072-1088 now checks
   `if _t_upper.endswith("-USD") and _bs_lower == "robinhood":`.
   RH crypto still SKIPPED with `venue_unsupported_crypto_path`
   (the SDK's `get_instruments_by_symbols([])[0]` failure is
   unchanged); Coinbase crypto reaches placement.
3. **Place-call dispatch**: at the existing
   `adapter.place_stop_loss_sell_order` site (line 1291), branch
   on broker_source:
   * **`coinbase`**: compute
     `limit_price = stop_price * (1 - chili_coinbase_stop_limit_buffer_pct)`
     and call `adapter.place_stop_limit_order_gtc(product_id,
     side='sell', base_size, stop_price, limit_price,
     client_order_id)`.
   * **else (RH default)**: existing
     `adapter.place_stop_loss_sell_order(product_id, base_size,
     trigger_price, client_order_id)` — args unchanged.

The RH path's call kwargs are byte-identical post-splice. Pinned by
`test_rh_equity_stop_call_args_byte_identical`.

### Step 5 — `tests/test_bracket_writer_venue_routing.py` (7 tests, all green)

* `test_rh_equity_stop_call_args_byte_identical` — captures
  `(product_id, base_size, trigger_price, client_order_id)` and
  asserts unchanged shape.
* `test_coinbase_crypto_routes_to_stop_limit_primitive` — ADA-USD
  → `place_stop_limit_order_gtc` with `limit_price = 0.45 * 0.995`.
* `test_rh_crypto_still_refused_via_prefilter` — RH ADA-USD still
  SKIPPED; neither RH nor Coinbase primitive reached.
* `test_coinbase_stop_rejection_arms_exception_cooldown_on_code_bug`
  — Coinbase ok=False with "list index out of range" → cooldown
  engaged.
* `test_supported_venues_includes_coinbase`.
* `test_unsupported_venue_rejected_pre_routing` — kraken →
  unsupported_venue.
* `test_coinbase_buffer_pct_setting_applied` — custom 1% buffer
  applied correctly.

### Step 6 — `Trade.venue` (effectively `Trade.broker_source`) populated — COMPLETE

Two-line change in `auto_trader.py`:

* Coinbase branch tags response: `cb_res["_chili_broker_source"] = "coinbase"`.
* Trade insertion reads:
  `_broker_source_for_trade = res.get("_chili_broker_source") or "robinhood"`
  → passed as `broker_source=_broker_source_for_trade` (was hardcoded
  `"robinhood"`). RH path is byte-identical (the side-channel key
  is absent → defaults to `"robinhood"`).

`Trade` ORM uses the existing `broker_source` column (line 59).
There is no separate `venue` column on Trade; the brief's
"Trade.venue" is conceptually `broker_source` (which the bracket
reconciler already keys on).

### Step 7 — Operator-side verification (deferred)

* Multi-process verification of new env vars + import sanity:
  operator runs after `docker compose up -d --force-recreate`.
* Single live paper-test (place + cancel via Coinbase): explicit
  operator approval required per brief sequencing step 12. Not
  attempted here.

## Settings added

* `chili_coinbase_stop_limit_buffer_pct: float = 0.005`
  (`CHILI_COINBASE_STOP_LIMIT_BUFFER_PCT`). 0.5% below stop_price
  for SELL stop-limits. Tighter than RH's stop-loss-MARKET (which
  fills at any price) but bounded so a fast gap-down can't sell at
  $0.

## Constraints honored

* ✅ **RH stop path BYTE-IDENTICAL.** Pinned by parity test.
* ✅ **Operator-locked design constraints (Phase 1) untouched.**
  Per-venue caps, global kill switch, RH-first preference,
  skip-on-fast-path-active all flow through unchanged.
* ✅ **No `CHILI_COINBASE_AUTOTRADER_LIVE=1` flip in this brief.**
  Stop primitive paper-tested via mocked SDK only; LIVE flip
  remains operator-controlled.
* ✅ **No autotrader entry-side changes** beyond the
  `_chili_broker_source` side-channel for Trade row tagging.
* ✅ **No new bracket strategies.** Only the stop-limit primitive
  was added; no trailing/OCO/etc.
* ✅ **Splice pattern via Edit tool justified** — both files
  edited are large but the changes are small (3 surgical anchors
  in `bracket_writer_g2.py`, one method addition in
  `coinbase_spot.py`). Each post-edit:
  `wc -l` + `ast.parse` clean.

## Verification

* `coinbase_spot.py`: 1024 → 1180 (+156); AST clean.
* `bracket_writer_g2.py`: 1572 → 1603 (+31); AST clean.
* `auto_trader.py`: 1727 → 1743 (+16); AST clean.
* `config.py`: +14 lines (one new setting).
* All 35 Phase-3 + Phase-4 tests PASS in 10.58s.
* RH parity gate held (test_rh_equity_stop_call_args_byte_identical).

## Operator-side after Phase 4 ships

1. `git pull` + truncation scan.
2. `docker compose up -d --force-recreate chili autotrader-worker
   scheduler-worker broker-sync-worker`.
3. Quick import sanity:
   ```bash
   docker exec chili-home-copilot-chili-1 python -c \
     "from app.services.trading.venue.coinbase_spot import CoinbaseSpotAdapter; \
      print('has stop primitive:', hasattr(CoinbaseSpotAdapter, 'place_stop_limit_order_gtc'))"
   ```
   Expected: `True`.
4. **DO NOT** flip `CHILI_COINBASE_AUTOTRADER_LIVE=1` yet.
   Phase 5 (cost-aware sizing) needs to ship first to honor Phase 2
   G1's USDC-wallet handling.
5. (Optional) Single live paper-test via `python -c` calling
   `CoinbaseSpotAdapter().place_stop_limit_order_gtc(...)` with
   stop and limit prices far below current spot, then cancel
   immediately. Operator-controlled.

## Rollback plan

* **Stop primitive misbehaves**: `git revert` only the
  bracket_writer venue-splice commit; the new
  `place_stop_limit_order_gtc` method stays (unused) and the
  Phase 3 selector continues routing in shadow-log mode.
* **Adapter import error**: `git revert` the coinbase_spot.py
  splice; force-recreate workers.
* **Coinbase stop placements rejected at venue**: emergency
  `CHILI_COINBASE_AUTOTRADER_LIVE=0`; manual-cancel any open
  Coinbase stops via the Coinbase web UI.

## What's NEXT

* **Phase 5**: cost-aware sizing. Reads BOTH `cash` (USD wallet)
  AND USDC quantity from `get_positions()` (per Phase 2 G1).
  Applies Coinbase Tier 1 fee (60bps taker / 40bps maker) to
  min-edge gate.
* **Phase 6**: paper-trade soak (≥48h). Operator verifies
  shadow-log routes match expectations + bracket coverage lands
  for any Coinbase entries.
* **Phase 7**: live with three-flag belt + small initial sizing.
