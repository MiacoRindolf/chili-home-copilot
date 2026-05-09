# CC_REPORT: f-coinbase-autotrader-enablement (Phase 1: audit)

**Read-only audit. ZERO code changes shipped from this brief.**

## Executive summary

Coinbase enablement is feasible but the surface area is larger than
"add an if/else." Three findings, each load-bearing for Phases 2-7:

1. **Venue factory exists but is bypassed.** `venue/factory.py:44`
   already routes `get_adapter("robinhood"|"coinbase")`. The
   autotrader hardcodes `get_adapter("robinhood")` at
   `auto_trader.py:1087`; `bracket_writer_g2.py:367` declares
   `_SUPPORTED_VENUES = frozenset({"robinhood"})`; the bracket
   reconciler filters `broker_source == "robinhood"` at
   `bracket_reconciliation_service.py:212/266`. Three explicit
   places need broadening, not a from-scratch refactor.
2. **Coinbase adapter has NO native stop primitive yet.** The
   `CoinbaseSpotAdapter` exposes `place_market_order` and
   `place_limit_order_gtc` only. Coinbase Advanced Trade's API
   supports stop-limit/stop-market via `OrderConfiguration`, but
   the adapter doesn't surface them. Phase 4 must wire
   `place_stop_limit_order_gtc` (or equivalent) before bracket
   writer can manage Coinbase stops. **The brief's "native stop
   primitive" framing is aspirational, not a current capability.**
3. **Risk infra is venue-aware in `bracket_writer_g2` but
   venue-blind elsewhere.** `pdt_guard` already excludes crypto
   tickers (R35 bypass) so the PDT count won't double-count Coinbase
   trades. But `portfolio_risk.check_drawdown_breaker` reads a
   single `account_equity` cache; cross-venue equity aggregation is
   unwired. `governance.is_kill_switch_active()` is global (no
   per-venue scope). These need addressing in Phases 5-6.

The 11 currently-open RH crypto positions stay on the RH path
unchanged — the cutover plan in Section E ships Coinbase as a
parallel route, NEVER migrating the open RH positions.

## Section A — Capability inventory

### `app/services/trading/venue/coinbase_spot.py` (1024 lines)

**Public surface (`CoinbaseSpotAdapter`):**

| Method | Line | Purpose |
|---|---:|---|
| `__init__` | 193 | Construct; lazy SDK import |
| `list_usd_spot_universe_entries` | 199 | USD spot universe (Phase 4 universe expansion) |
| `is_enabled` | 259 | SDK + credentials configured? |
| `get_product` / `get_products` | 275 / 293 | Product metadata |
| `get_best_bid_ask` | 316 | Top of book |
| `get_ticker` | 332 | Last price |
| `get_recent_trades` | 376 | Trade tape |
| `list_open_orders` | 392 | Live orders for a product |
| `get_order` | 411 | Order by id |
| `get_fills` | 427 | Fills for a product |
| **`place_market_order`** | 451 | **Buy/sell market** |
| **`place_limit_order_gtc`** | 555 | **Buy/sell limit GTC** |
| `cancel_order` | 662 | Cancel by id |
| `preview_market_order` | 701 | Pre-flight preview |
| `get_account_snapshot` | 727 | Equity + holdings snapshot |

**Module helpers:**

* `CoinbaseWebSocketSeam` (line 742): WebSocket subscription for
  real-time ticks. Used by fast-path; not the autotrader path.

**MISSING capabilities (vs. RH adapter):**

* **No stop primitive.** No `place_stop_loss_sell_order`,
  `place_stop_limit_*`, or trigger-order method. Bracket writer
  cannot manage Coinbase stops until this is added (Phase 4).
* **No bracket order primitive.** Coinbase Advanced Trade supports
  bracket (OCO) via `OrderConfiguration.bracket_gtd`, also unwired.

### `app/services/coinbase_service.py` (904 lines)

**Public surface:**

| Function | Line | Purpose |
|---|---:|---|
| `connect` / `connect_with_credentials` | 85 / 110 | SDK init + auth |
| `is_connected` | 135 | Session liveness |
| `get_connection_status` | 160 | Auth/connection diagnostics |
| `get_accounts_raw` | 173 | Raw accounts |
| `get_portfolio` | 195 | Equity / positions snapshot (used by `pdt_guard`'s `_fetch_account_equity_usd` indirectly) |
| `_get_cost_basis_from_fills` | 228 | Cost basis derivation |
| **`get_positions`** | 260 | **Position list** |
| `get_recent_orders` | 301 | Order history |
| **`place_buy_order`** | 342 | **Market or limit buy (with `post_only=False` per the f-fastpath-maker-only-executor splice)** |
| **`place_sell_order`** | 420 | **Sell counterpart** |
| `get_order_by_id` | 493 | Order lookup |
| **`cancel_order_by_id`** | 507 | **Single-order cancel (added 2026-05-08)** |
| `map_cb_status` / `is_cb_terminal` | 561 / 567 | Status normalization |
| `sync_orders_to_db` | 573 | Order reconciler |
| `sync_positions_to_db` | 675 | Position reconciler |

**Authority duality:** Both `coinbase_service.place_buy_order` AND
`coinbase_spot.CoinbaseSpotAdapter.place_market_order` exist. The
adapter is the canonical surface for the venue-protocol abstraction
(used by fast_path); the service-level functions are the lower-
level primitives. **Phase 3 must pick one canonical layer per the
operator's "don't band-aid" directive.** Recommendation: adapter
becomes the canonical surface; service functions become
internal primitives.

## Section B — Autotrader RH-implicit assumption inventory

### `app/services/trading/auto_trader.py` (1632 lines)

| Line | Code | Type |
|---:|---|---|
| 773-775 | `is_venue_degraded(db, venue="robinhood")` | hardcoded venue arg |
| 868 | `venue="robinhood"` | hardcoded venue label in event payload |
| 898 | `venue: str` parameter | already abstracted |
| 1005 | `from .venue.factory import get_adapter` | factory imported (good) |
| **1087** | **`ad = get_adapter("robinhood")`** | **HARDCODED — single point of broker selection** |
| 1119-1124 | `is_robinhood_supported_crypto`, `_is_crypto_supported_on_robinhood` | RH-specific crypto whitelist gate |
| 1153 | `res = ad.place_market_order(...)` | adapter-level (good — works for both venues) |
| 1493-1500 | `reason="broker:place_no_order_id"` | venue-neutral reason string |
| 1525, 1550 | `broker_source="robinhood"` | hardcoded in Trade row write |
| 1579 | `reason="live_robinhood"` | hardcoded reason |

### `app/services/trading/auto_trader_monitor.py` (537 lines)

| Line | Code | Type |
|---:|---|---|
| 229 | `from .venue.robinhood_spot import RobinhoodSpotAdapter` | direct import |
| 266-272 | `from .robinhood_exit_execution import ...` | RH-specific exit module |
| 312 | `(t.broker_source or "").strip().lower() == "coinbase"` | branch already exists for Coinbase skip |
| 322-326 | `if broker_source and broker_source != "robinhood": skip` | EXPLICIT skip of non-RH |
| 328-330 | `if broker_source == "robinhood" and ticker.endswith("-USD"): skip` | hands crypto-on-RH to a different path |
| 478 | `submit_robinhood_trade_exit(...)` | RH-specific exit submission |

**Refactor surface for Phase 3 (broker selection):**

1. **Decide broker per alert** at `auto_trader.py:1087`. The selector
   needs inputs: alert.asset_class, alert.ticker (`-USD` suffix),
   operator config, currently-open positions cap per venue.
2. **Stamp `broker_source` from the selector** at lines 1525, 1550.
3. **Mirror the venue label** in events (lines 773, 868, 1579).
4. **Extract a `submit_trade_exit(broker_source, ...)` dispatch** in
   `auto_trader_monitor.py` so the existing Coinbase skip at line
   322 turns into a route to `submit_coinbase_trade_exit`.

The selector itself is a small function (~30 lines). The downstream
plumbing is the meaty work.

## Section D — Risk infrastructure audit

### `pdt_guard.py`

* **Crypto bypass already wired** (R35, line 186): tickers ending
  in `-USD` short-circuit to `allowed=True` with reason
  `crypto_not_pdt_eligible`. Coinbase trades will pass through this
  bypass cleanly.
* The crypto-rec phantom-row exclusions
  (`_RECONCILE_ARTIFACT_EXIT_REASONS`) DO NOT include any
  Coinbase-specific reasons today (Phase E was reverted; the two
  crypto reasons came back out). If Phases 5-6 introduce a Coinbase
  reconciler that emits exit-reason artifacts, the frozenset must
  be re-extended.

### `portfolio_risk.py`

* `check_drawdown_breaker(db, user_id, capital)` reads a single
  `capital` value. The caller (`assess_pre_trade`) passes whatever
  `account_equity` source it has. Currently RH-only via
  `pdt_guard._fetch_account_equity_usd` → RH `get_portfolio`.
* **Multi-venue equity aggregation is unwired.** Phase 5 must
  introduce `get_total_account_equity()` that sums RH + Coinbase
  portfolios. Otherwise the breaker uses RH-only equity and
  drawdown ratios become wrong.
* `is_kill_switch_active()` (line 145-146) is **global, not
  per-venue**. A single trip blocks all entries on both venues.
  That's correct (kill is intentional global stop), but operator
  should confirm — the alternative is per-venue trip granularity.
* `max_portfolio_heat_pct=6.0` (line 42) is a single threshold; if
  Coinbase+RH portfolios are summed, the heat calculation
  automatically applies cross-venue. No threshold tuning needed.

### `bracket_writer_g2.py` + `bracket_reconciliation_service.py`

* `bracket_writer_g2.py:367`:
  `_SUPPORTED_VENUES = frozenset({"robinhood"})`. **Single point
  of expansion.** Adding `"coinbase"` enables the writer for
  Coinbase intents — but the `place_stop_loss_sell_order` adapter
  call (line 1256) needs the Coinbase counterpart that DOESN'T EXIST
  YET (Section A finding #2).
* Tonight's prefilter at `bracket_writer_g2.py:1037` refuses ALL
  `-USD` tickers from `place_missing_stop`. **For Coinbase the
  refusal must skip if `broker_source='coinbase'`** AND the
  Coinbase adapter has the stop primitive wired.
* `bracket_reconciliation_service.py:212` and `:266` filter
  `broker_source == "robinhood"`. Coinbase intents would be
  silently ignored by the reconciler today. Phase 4 fix:
  parameterize, OR add a parallel
  `bracket_reconciliation_service_coinbase.py` (cleaner per the
  "don't band-aid" directive).

### Cross-venue position-correlation risk

If RH holds 100 ADA and Coinbase holds 100 ADA, the operator has
**200 ADA exposure** but each `portfolio_risk.calculate_total_heat`
call sees only one venue's positions (depending on which DB query
shape). Two open mitigations:

1. **Per-venue position cap** at the autotrader entry gate. If
   `same_ticker_total_qty_across_venues > cap`, refuse. Easy to
   wire (Trade query group by ticker).
2. **Cross-venue heat aggregation** in `portfolio_risk`. Same fix
   path as the equity aggregation above.

**Recommendation: ship both in Phase 5.** Cap is the cheap defence;
heat aggregation is the durable one.

## Section C — Cost economics with real numbers

### Coinbase Advanced Trade fee tiers (per
`https://docs.cdp.coinbase.com/exchange/docs/fees`, cited at
`fast_path/settings.py:159-161`):

| Tier (30d volume USD) | Taker fee | Maker fee |
|---|---:|---:|
| **Tier 1 (<$10k)** | **60 bps** | **40 bps** |
| Tier 2 (≥$10k) | 40 bps | 25 bps |
| Tier 3 (≥$50k) | 25 bps | 15 bps |
| Tier 4 (≥$100k) | 15 bps | 8 bps |
| Tier 5 (≥$1M) | 10 bps | 6 bps |

Operator's funded $2.2k cash places the account at **Tier 1** for
the foreseeable future (would need ~$50k+ of monthly volume to
move tiers, which $2.2k of capital churning at moderate frequency
won't approach).

### Round-trip cost (per share):

| Execution mode | Round-trip fee | Plus typical spread (5-15bps) |
|---|---:|---:|
| Taker × 2 | **120 bps** | 130-150 bps |
| Taker → Maker | 100 bps | 105-115 bps |
| Maker × 2 | **80 bps** | 80-95 bps |

### Minimum-edge calc

For a strategy to break even at Tier 1 taker round-trip, the per-
trade expected return must clear **120 bps** before any net P&L.
Patterns 1011/1016 realized edge:

* Pattern 1011: WR 63.16%, avg_return_pct +unknown (RH historical;
  not directly comparable).
* Pattern 1016: WR 70.69%, avg_return_pct +unknown.

The fast-path `cost_aware_admission` gate (per
`fast_path/gates.py`) already handles this calculus for fast-path
crypto. **The autotrader does NOT have a cost-aware admission
gate.** Phase 5 must port the fast-path gate's cost math into the
autotrader's `gate_cost_aware_admission` path so Coinbase entries
are filtered against the Tier 1 cost floor.

**Recommendation: default Coinbase to maker-only or
maker-first-then-taker** (per the
`f-fastpath-maker-only-executor` settings in
`fast_path/settings.py:172-217`). Saves 40 bps per round-trip.

### Comparison to RH crypto

RH crypto has near-zero spread + no per-trade fee for retail. **RH
is cost-cheaper than Coinbase for any pair RH lists.** Coinbase's
edge is **universe coverage** (RH lists ~17 crypto bases per
`broker_service.py:3153-3162`; Coinbase lists hundreds), NOT cost.
The selector should prefer RH for any base in the RH whitelist;
route to Coinbase only for the long tail.

## Section F — Reconciler + lifecycle audit for Coinbase

### Existing surfaces

* `coinbase_service.sync_orders_to_db` (line 573) — order reconciler.
* `coinbase_service.sync_positions_to_db` (line 675) — position
  reconciler.
* `crypto/exit_monitor.py` — supports Coinbase already (line 145
  docstring: "Also accepts `broker_source='coinbase'` so the same
  monitor works for Coinbase-routed crypto if/when that adapter is
  wired in"). Line 312: `(t.broker_source or "").strip().lower() == "coinbase"`
  branch exists.

### Missing surfaces

* **No bracket reconciler for Coinbase.** The
  `bracket_reconciliation_service.py` is RH-only (Section D).
  Coinbase doesn't have a parallel module.
* **No webhook integration.** Coinbase Advanced Trade supports
  WebSocket order updates (already wired in
  `CoinbaseWebSocketSeam`, line 742). The fast-path subscribes;
  the autotrader doesn't. Phase 6 should integrate Coinbase
  WebSocket fills into the autotrader's reconcile cycle so
  position state is real-time, not poll-based.
* **No fast-path overlap handling.** The fast-path executor
  (`fast_path/executor.py:120 _place_coinbase_order_live`)
  ALREADY places Coinbase orders for fast-path patterns. If the
  autotrader also routes Coinbase, the two pipelines could place
  conflicting orders for the same ticker. Phase 3 must pick:
  (a) autotrader does NOT route Coinbase if fast-path is active for
  that ticker (cheap), or (b) cross-pipeline coordination via a
  per-ticker lock (durable).

### Lifecycle question

Currently RH crypto trades flow through `crypto/exit_monitor.py`
which calls `broker_service.place_crypto_sell_order`. **The exit
monitor must call the Coinbase exit primitive** (`place_sell_order`
in `coinbase_service.py`) when `broker_source='coinbase'`. The
branch at line 145/312 exists but the body is unwired:

```python
src = (trade.broker_source or "").strip().lower()
if src == "coinbase":
    # ??? no implementation
```

Phase 4 fix: branch routes to
`coinbase_service.place_sell_order(ticker, qty, "market")` when
src=='coinbase'.

## Section E — Venue abstraction design recommendation

### Picked: **adapter-pattern with broker selector**

The `venue/factory.py` already implements this; the codebase needs
to USE it consistently.

### Components

1. **`venue/factory.get_adapter(broker_source)`** — already exists.
   Picks `RobinhoodSpotAdapter` or `CoinbaseSpotAdapter`.
2. **NEW: `broker_selector.select_for_alert(alert, db, settings)`**
   — module to write in Phase 3. Takes alert + system state;
   returns broker_source string. Logic:
   * If asset_class != crypto → 'robinhood' (Coinbase doesn't trade
     equities).
   * If ticker is in RH crypto whitelist (per
     `broker_service.ROBINHOOD_SUPPORTED_CRYPTO_BASES`) AND RH is
     not degraded → 'robinhood' (cost-preferred).
   * Else if ticker is in Coinbase USD universe → 'coinbase'.
   * Else → reject (no venue can trade this).
3. **Bracket writer's `_SUPPORTED_VENUES`** — extend to
   `frozenset({"robinhood", "coinbase"})` ONCE the Coinbase adapter
   exposes the stop primitive (Phase 4).
4. **Bracket reconciler** — parameterize the
   `broker_source == "robinhood"` filters at lines 212/266 to a
   `broker_sources` set parameter; default to RH-only; flip to
   `{"robinhood", "coinbase"}` when Phase 4 is ready.
5. **Exit monitor branch** — wire the `src == 'coinbase'` body in
   `crypto/exit_monitor.py:151` to call the Coinbase sell
   primitive.

### Cutover strategy (must NOT break the 11 open RH crypto positions)

* **Phase 3 ships the selector + selector-driven autotrader entry
  routing.** The selector defaults to `'robinhood'` for all current
  cases (the RH whitelist captures all currently-traded bases). New
  Coinbase entries fire ONLY for tickers in Coinbase universe AND
  not in RH whitelist.
* **Phase 4 ships the Coinbase bracket writer + adapter stop
  primitive.** New Coinbase entries get bracket coverage.
* **Open RH crypto positions never migrate.** The 11 currently-open
  RH positions stay on the RH path; their `broker_source='robinhood'`
  in the Trade row routes them to the existing RH exit monitor +
  bracket writer until they close.
* **No Coinbase trading in Phases 1-3.** Phases 4-6 introduce paper
  Coinbase trading; Phase 7 introduces live with the same
  three-flag belt the RH live path uses
  (CHILI_FAST_PATH_EXEC_LIVE_AUTHORIZED + CHILI_FAST_PATH_LIVE_NOTIONAL_OK
  pattern).

### Why adapter pattern vs alternatives

* **vs function dispatch** (e.g., `submit_trade_exit(broker_source,
  ...)` that internally dispatches): more verbose at the call site,
  but the type contract (`VenueAdapter` protocol) catches missing
  methods at construction time. Already partially adopted; finish it.
* **vs polymorphic Trade**: would require a Trade-level method like
  `trade.exit()` that dispatches on `self.broker_source`. Tighter
  coupling between domain model and IO; harder to test the IO in
  isolation. Rejected.

## Section G — Phase 2-7 scope + risk-to-existing-system

| Phase | Goal | Scope (CC time) | Risk to existing | Prerequisite |
|---|---|---|---|---|
| **2** | Coinbase auth verification | <1h | LOW (read-only) | none |
| **3** | Broker selector + autotrader entry routing | 2-3h | MEDIUM (new code path; default-routes-RH) | 2 |
| **4** | Coinbase adapter stop primitive + bracket writer integration | 4-6h | MEDIUM (new bracket writer arm; default-disabled flag) | 3 |
| **5** | Cross-venue equity aggregation + per-ticker position cap | 2-3h | MEDIUM (drawdown breaker math change) | 3 |
| **6** | Paper Coinbase soak (≥48h, no live trades) | operator-side wait + 1h CC for monitoring helpers | LOW | 4, 5 |
| **7** | Live Coinbase enable behind three-flag belt | 1-2h CC + operator approval | HIGH (real money) | 6 |

### Risk-to-existing-system per phase

* **Phase 2**: read-only auth probe; cannot impact RH path.
* **Phase 3**: selector defaults to RH for every existing case;
  Coinbase routing ONLY fires for tickers RH doesn't list. Risk =
  bug in selector that misroutes a current RH ticker. **Mitigation**:
  selector's default branch must be `'robinhood'`; Coinbase return
  requires explicit Coinbase-universe membership AND non-RH-whitelist.
* **Phase 4**: bracket writer arm gated behind a settings flag
  defaulted OFF. Existing RH bracket writer untouched.
* **Phase 5**: drawdown breaker change is the highest sub-risk —
  Cross-venue equity sum changes the breaker's denominator.
  Operator must validate the new equity total matches their bank
  reality before Phase 5 ships.
* **Phase 6**: paper-only; no live.
* **Phase 7**: real money + operator-side three-flag belt.

## Section H — Hard constraints honored

* ✅ **Read-only audit.** Zero `INSERT` / `UPDATE` / `DELETE`
  against any DB.
* ✅ **No code changes.** No commits other than this report.
* ✅ **RH autotrader untouched.** No reads modified the RH code
  path.
* ✅ **Patterns 1011/1016 untouched.** Their `lifecycle_stage` and
  `oos_win_rate` rows are unchanged.
* ✅ **Tonight's `crypto_ticker_unsupported_via_equity_primitive`
  backstop preserved.** The audit confirms it stays in place;
  Phase 4 introduces the Coinbase stop primitive that bypasses it
  cleanly via the venue selector, not by removing the backstop.
* ✅ **No live Coinbase trades.** Phase 7 territory.
* ✅ **Edit-tool truncation discipline.** Only file added is this
  report; no source files edited.

## Operator-side after Phase 1 ships

1. Read this report.
2. Verify the venue-abstraction design (Section E) and the per-
   phase risk ratings (Section G) match how you want chili to
   evolve.
3. If approved, queue **Phase 2 (auth verification)** as the next
   brief.
4. If something in Sections A-D is surprising or contradicts your
   priors, surface it before Phase 2.

## Open questions for the operator

1. **Cross-venue position cap**: same-ticker on both venues — do
   you want a hard cap (refuse second venue's open) or a soft
   warn? Section D's recommendation defaults to hard cap; flag if
   you want soft.
2. **Kill switch granularity**: global vs per-venue. Section D's
   recommendation is to keep global (one operator-pulled lever);
   confirm.
3. **Selector preference**: RH-first (cost-cheaper) or
   Coinbase-first (broader universe / native-stop) for tickers in
   BOTH whitelists? Section E recommends RH-first (cost); confirm.
4. **Fast-path overlap**: should the autotrader skip Coinbase
   entries when fast-path is active for that ticker, OR should
   they coordinate via a per-ticker lock? Section F recommends
   skip (cheap); flag if you want lock.

## Rollback plan

N/A — Phase 1 is read-only.
