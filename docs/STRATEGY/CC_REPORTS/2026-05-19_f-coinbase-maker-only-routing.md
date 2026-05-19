# CC_REPORT: f-coinbase-maker-only-routing

**Session type:** Cowork-direct execution via daemon (operator: "USE DAEMON" after surfacing the bridge briefs).

## What shipped

**Commit `18fee1e`** on `main` (auto-pushed to `origin` by the daemon), 4 files / ~250 LOC:

- `app/config.py` — new flag `chili_coinbase_maker_only_enabled` (default `False`)
- `app/services/trading/venue/coinbase_spot.py` — `CoinbaseSpotAdapter.place_limit_order_gtc` gains `post_only: bool = False` kwarg with SDK-variant dispatch
- `app/services/trading/auto_trader.py` — `_execute_broker_buy` Coinbase branch gains a maker-only path BEFORE the existing `place_market_order` call
- `tests/test_coinbase_maker_only_routing.py` — 5 new pinned tests

## Why this matters

2026-05-18 TCA finding (in CC_REPORT `2026-05-18_f-position-identity-phase-3-and-tca-and-account-type.md`):

| Trade type | Avg entry slippage | Pattern 585 gross edge | Slippage as % of edge |
|---|---|---|---|
| All crypto | **+102 bps** | 168 bps | **~60%** |

Coinbase taker fees: 60 bps per side (120 bps round-trip). Maker fees: 40 bps or less depending on volume tier. Maker-only routing addresses BOTH the fee delta AND the adverse-fill component of the 102 bps slippage.

**Trade-off the design accepts:** when maker-only is ON and price moves up while the order is in flight, the broker REJECTS the post_only limit and the entry is MISSED. Pattern 585 has plenty of opportunities; one missed alert is recoverable. A 102 bps haircut on every entry is not.

## How it works

When `CHILI_COINBASE_MAKER_ONLY_ENABLED=true`:

1. Autotrader reaches the Coinbase entry path (`auto_trader.py:_execute_broker_buy`).
2. Adapter's `get_best_bid_ask(ticker)` fetches the current BBO.
3. If `bid > 0`: calls `place_limit_order_gtc(side='buy', limit_price=str(bid), post_only=True)`.
4. Adapter routes through SDK's `limit_order_gtc_buy_post_only` (preferred) or falls back to `limit_order_gtc_buy(post_only=True, ...)`.
5. Response tagged with `_chili_maker_only=True` and `_chili_maker_limit_price=<bid>` for downstream audit.

When `CHILI_COINBASE_MAKER_ONLY_ENABLED=false` (default): byte-identical to today's `place_market_order` path. Zero behavior change on deploy.

**Fallback paths (preserve today's behavior on any glitch):**
- No best_bid available: logs `falling back to market order`, calls `place_market_order`.
- Maker call raises any exception: logs `falling back to market order`, calls `place_market_order`.
- Flag off: skip the entire maker branch.

## Verification

**Tests.** 41/41 PASS:
- 5 new maker-only tests (flag default, adapter signature, SDK-variant dispatch, autotrader branch existence, fallback log, observability tag)
- 4 bracket-fired-stop tests
- 5 coinbase-exit-recording tests
- 27 existing position-identity Phase 2/3/4 tests

**Compile.** All 4 modified files compile clean.

**Deploy.** `docker compose up -d --force-recreate chili scheduler-worker brain-worker autotrader-worker broker-sync-worker` clean. All 5 services recreated healthy.

**Push.** `git push origin main` — `a30c054..18fee1e main -> main`. Commit live on `origin`.

**Live state confirmation:**
- `.env` does NOT contain `CHILI_COINBASE_MAKER_ONLY_ENABLED` (flag defaults to False, as designed for paper-soak)
- Operator opt-in required to actually enable

## Operator promotion path (next steps after this commit)

When ready to paper-soak:

1. Add `CHILI_COINBASE_MAKER_ONLY_ENABLED=true` to `.env` (use ASCII-safe write per `feedback_never_powershell_outfile_env`).
2. `docker compose up -d --force-recreate autotrader-worker` (lowest blast radius).
3. Watch for Coinbase entry attempts:
   ```bash
   docker compose logs -f autotrader-worker | grep -E "maker-only|place_limit_order_gtc"
   ```
4. Probe new entries in `trading_execution_events`:
   ```sql
   SELECT id, ticker, payload_json->>'_chili_maker_only' AS maker,
          payload_json->>'_chili_maker_limit_price' AS limit_px,
          status, average_fill_price
   FROM trading_execution_events
   WHERE event_type IN ('order_submitted', 'status')
     AND broker_source = 'coinbase'
     AND created_at > '<flip_ts>'
   ORDER BY id DESC LIMIT 20;
   ```
5. Re-compute avg entry slippage bps after ~1 week of trades. Target: avg drops from +102 bps to <30 bps.
6. If achieved: promote (leave flag on). If not: investigate "no best_bid" rate + missed-entry rate.

## Surprises / deviations

1. **The Coinbase fast-path already had maker-only support** in `coinbase_service.place_buy_order` (added 2026-05-08 in `f-fastpath-maker-only-executor`). But that path is for the fast-path executor (F4+), not the main autotrader. The autotrader has always gone through `CoinbaseSpotAdapter.place_market_order`. This brief plumbs the same capability into the autotrader path by adding `post_only` to the adapter's limit-order method.

2. **`NormalizedTicker` uses `.bid` not `.best_bid`.** Caught during implementation; the auto_trader.py code uses `getattr(bbo, "bid", None)` accordingly.

3. **Push worked through the daemon.** The PROTOCOL Hard Rule on no-force-push to main is preserved; regular pushes via daemon-run scripts are fine because the daemon's rejection regex (`'\bgit push\b.*--force(?!-with-lease)'`) targets force pushes, not regular pushes.

## Deferred

- **Maker-only on the exit side (SELL post_only at best-ask).** Same principle, separate brief. Less urgent because most exits already go through `pending_exit_order_id` which has its own price logic.
- **Adaptive maker timeout** — if the limit doesn't fill within N seconds, cancel and re-route as taker. More complex; current design accepts missed entries.
- **TCA dashboard for maker-vs-market comparison.** Operator will need a query that segments avg slippage by `_chili_maker_only` flag. Not in this brief.
- **`f-stop-engine-payoff-ratio-gate`** — bridge brief still queued. Operator picks next.

## Rollback plan

If maker-only ever produces bad behavior:

1. `CHILI_COINBASE_MAKER_ONLY_ENABLED=false` in `.env`.
2. `docker compose up -d --force-recreate autotrader-worker`.
3. Legacy market-order path resumes immediately.

If a code regression is suspected:
- `git revert 18fee1e` — removes all 3 code changes; tests + flag stay.
- The flag default is `False`, so even without revert the behavior is OFF by default.

## Status

Code shipped. Push complete. Flag stays OFF until operator opts in.

NEXT_TASK can become:
- The operator promotion (paper-soak the flag), OR
- `f-stop-engine-payoff-ratio-gate` (the other bridge brief), OR
- Wait for first `[phase4_*]` log line and unlock Phase 5.

Operator picks.
