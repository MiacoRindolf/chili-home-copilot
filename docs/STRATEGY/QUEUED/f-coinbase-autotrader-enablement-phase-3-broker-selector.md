# f-coinbase-autotrader-enablement-phase-3-broker-selector

**Owner**: Cowork → Claude Code
**Status**: PENDING
**Risk**: MEDIUM (touches autotrader entry-routing)
**Time budget**: 2-3h CC scope (single session, design + implementation + tests)

## Goal

Build the **broker selector** that routes autotrader entries to
either Robinhood (equity) or Coinbase (crypto) based on the ticker
and operator-locked design constraints. This is the first phase that
actually ROUTES anything to Coinbase. Up to now everything has been
read-only audit + auth probe.

The change should keep the existing RH path 100% unchanged when no
ticker matches the Coinbase whitelist. The Coinbase routing path is
new and gated behind a **kill-switch flag** that defaults OFF.

## Why now

Phase 2 verified end-to-end:
- Auth works across all 4 worker containers
- `place_buy_order` + `cancel_order_by_id` round-trip in ~0.6s
- `-USD` ticker convention works (operator converted USDC → USD;
  cash=$2200.01)
- Zero residual orders, zero capital impact on the redux test

Phase 3 is the gate to actually trading on Coinbase. Without it,
the `$2.2k` sits idle.

## Operator-locked design constraints (binding from Phase 1)

These are NOT in scope to debate — they are decisions the operator
made at the end of Phase 1 audit. Phase 3 implements them.

1. **Cross-venue position cap: SEPARATE per-venue.** No cross-venue
   aggregation. Each venue has its own independent position cap.
2. **Kill switch: GLOBAL.** One operator-pulled lever stops both
   venues. Specifically: a single `CHILI_AUTOTRADER_KILL_SWITCH=1`
   env var disables ALL autotrader entries (both RH and Coinbase).
3. **Selector preference for tickers in BOTH whitelists: RH-first
   (cost-cheaper).** RH is fee-free; Coinbase Advanced Trade is
   60bps taker. So any ticker that's listed on BOTH (e.g.,
   `BTC-USD` is on RH-crypto AND Coinbase) routes to RH. Coinbase
   is for the **long tail** — tickers RH doesn't list.
4. **Fast-path overlap: skip-on-fast-path-active.** If the
   fast-path scanner is currently holding a position or has an
   active alert for a ticker, autotrader skips Coinbase routing
   for that ticker. Avoids double-entry.

## Quote-currency convention (Phase 2 result)

Coinbase tickers route as `-USD` pairs (e.g., `BTC-USD`,
`ETH-USD`). This matches CHILI's existing `coinbase_service.py`
calls. If operator funds future deposits as USDC, they must
convert to USD in the Coinbase UI before autotrader BUYs will
succeed (Phase 2 G1 — operator runbook responsibility, not CHILI's).

## The change (3 components)

### Component A — Broker selector module

New file: `app/services/trading/broker_selector.py`

API surface:
```python
def select_venue(
    ticker: str,
    *,
    db: Session,
    rh_whitelist: set[str],
    coinbase_whitelist: set[str],
    fast_path_active_tickers: set[str],
    kill_switch_engaged: bool,
) -> dict:
    """
    Returns {'venue': 'rh' | 'coinbase' | 'skip', 'reason': str}.

    Decision tree:
    1. kill_switch_engaged -> {'venue': 'skip', 'reason': 'kill_switch'}
    2. ticker in fast_path_active_tickers -> {'venue': 'skip',
       'reason': 'fast_path_active'}
    3. ticker in rh_whitelist -> {'venue': 'rh', 'reason': 'rh_listed'}
       (RH-first wins for both-listed tickers per design constraint 3)
    4. ticker in coinbase_whitelist -> {'venue': 'coinbase',
       'reason': 'coinbase_only'}
    5. otherwise -> {'venue': 'skip', 'reason': 'no_venue_match'}
    """
```

Key properties:
- **Pure function** (no side effects, no broker calls).
- Reads kill switch from `settings.autotrader_kill_switch` (new
  env var, default `False`).
- Whitelists computed once per autotrader cycle and passed in
  (caller's job).
- Returns dict, not enum, so `reason` can be logged for audit.

### Component B — Whitelist resolution

Two new helpers in the selector module:

```python
def resolve_rh_whitelist(db: Session) -> set[str]:
    """
    Returns the set of tickers RH supports for autotrader entries.
    Reads from existing whitelist source (likely
    settings.rh_universe_tickers or pattern_universe table).
    """

def resolve_coinbase_whitelist(db: Session) -> set[str]:
    """
    Returns the set of Coinbase-listed tickers WITH non-zero
    quoted prices. Filters out the 31 dust positions from the
    operator's wallet (positions where current_price=0 and
    equity=0). Caller passes this to select_venue.
    """
```

Sourcing strategy for `coinbase_whitelist`:
- Primary: query `cb.list_products()` filtered to active
  `product_type='SPOT'` with `quote_currency_id='USD'` (NOT USDC).
- Cache result for 1h (Coinbase product list rarely changes).
- Out-of-scope for Phase 3: dynamic universe rotation. Phase 6
  paper-soak will inform that.

### Component C — Wire into autotrader

Modify `app/services/trading/auto_trader.py`:

Find the existing entry-placement call (looks roughly like
`broker_service.place_buy_order(ticker=...)` for stocks; or
`crypto_broker.place_crypto_order(...)` for crypto). Replace the
direct call with:

```python
decision = broker_selector.select_venue(
    ticker=ticker,
    db=db,
    rh_whitelist=rh_whitelist,
    coinbase_whitelist=coinbase_whitelist,
    fast_path_active_tickers=fast_path_active_tickers,
    kill_switch_engaged=kill_switch_engaged,
)

if decision['venue'] == 'skip':
    logger.info(
        '[autotrader] skip ticker=%s reason=%s',
        ticker, decision['reason']
    )
    continue

if decision['venue'] == 'rh':
    # existing RH path -- unchanged
    res = broker_service.place_buy_order(...)
elif decision['venue'] == 'coinbase':
    # NEW path -- gated on CHILI_COINBASE_AUTOTRADER_LIVE flag
    if not settings.coinbase_autotrader_live:
        logger.info(
            '[autotrader] coinbase route gated (LIVE flag off); '
            'shadow-log only'
        )
        # log to a new shadow table for paper-soak observability
        _log_coinbase_shadow_entry(ticker, ...)
        continue
    res = coinbase_service.place_buy_order(
        ticker=ticker,                 # already -USD form
        quantity=qty,
        order_type='market',           # or 'limit' per existing logic
        ...
    )
```

Two new env-var flags, both default OFF:
- `CHILI_AUTOTRADER_KILL_SWITCH=1` — global kill (constraint 2).
- `CHILI_COINBASE_AUTOTRADER_LIVE=1` — Coinbase routing live (off
  = shadow-log only). This is what protects the operator capital
  during the first 2-3 days of selector operation.

## Acceptance criteria

1. **No code changes to RH path.** Ticker `AAPL` (RH-listed)
   continues to flow through `broker_service.place_buy_order` with
   identical kwargs and identical response handling. Test with a
   parity unit test that captures RH call args before+after.
2. **Selector returns correct venue for 5 ticker classes**:
   - RH-only (e.g., `AAPL`) → `venue=rh`
   - Coinbase-only (e.g., a long-tail crypto RH doesn't list) →
     `venue=coinbase` (but gated behind LIVE flag)
   - Both-listed (e.g., `BTC-USD`) → `venue=rh` (RH-first)
   - Fast-path-active (e.g., `ETH-USD` if fast-path holds it) →
     `venue=skip` reason=`fast_path_active`
   - Kill switch engaged → `venue=skip` reason=`kill_switch` (any
     ticker)
3. **`CHILI_COINBASE_AUTOTRADER_LIVE=0`** (default): Coinbase
   routes log to shadow table; no broker call made; no money
   moves.
4. **`CHILI_COINBASE_AUTOTRADER_LIVE=1`** + Phase 3 paper-test
   redux: a single tiny limit-buy at far-below-market routes to
   Coinbase, places, cancels via existing autotrader-side
   bracket cancellation logic.
5. **Multi-process consistency**: kill-switch flag picked up by
   ALL 4 worker containers after one `docker compose up -d
   --force-recreate`. Verified via per-container log line.
6. **Cost log preserved**: any Coinbase entry (live or shadow)
   writes a row to the existing cost-audit table
   (`trading_venue_truth_log` or equivalent) so Phase 5
   cost-aware sizing has data to learn from.
7. **Test parity**: new tests in
   `tests/test_broker_selector.py` cover all 5 decision branches
   + the LIVE flag gate.
8. **CC report at**:
   `docs/STRATEGY/CC_REPORTS/<YYYY-MM-DD>_f-coinbase-autotrader-enablement-phase-3-broker-selector.md`.

## Brain integration (read + write)

**Read-only:**
- `app/services/trading/auto_trader.py` — find the entry-placement
  callsite to splice into.
- `app/services/coinbase_service.py` — confirm `place_buy_order`
  signature + response shape (already verified in Phase 2 redux).
- `app/services/trading/venue/coinbase_spot.py` — read for the
  optional parallel adapter check; do NOT modify in this phase.

**Write:**
- `app/services/trading/broker_selector.py` — NEW file (the
  selector + whitelist resolvers).
- `app/services/trading/auto_trader.py` — splice in selector call;
  preserve RH path verbatim.
- `app/config.py` — two new env vars: `autotrader_kill_switch`
  (bool, default False), `coinbase_autotrader_live` (bool,
  default False).
- `tests/test_broker_selector.py` — NEW test file.
- (optional) `app/migrations.py` — if a new shadow-log table is
  needed; check for existing `trading_venue_truth_log` first.

## Constraints / do not touch

- **Hard Rule 1**: live-placement safety belts unchanged. Kill
  switch, drawdown breaker, ensemble promotion check — all
  PRECEDE selector in the entry path.
- **Hard Rule 5**: prediction-mirror authority untouched.
- **Operator's directive: don't break what works.** RH path
  byte-identical post-Phase-3.
- **No paper-trade-soak in Phase 3.** That's Phase 6's job. Phase
  3's "live test" is a single tiny limit-far-below-spot that goes
  through `cb.place_buy_order` then is canceled via the existing
  autotrader-side bracket cancellation logic. Same safety belt
  pattern as Phase 2 redux.
- **No cost-aware sizing in Phase 3.** Phase 5's job. Phase 3
  uses whatever sizing logic the existing autotrader uses for
  RH (probably notional from settings).
- **No bracket writer Coinbase path in Phase 3.** Phase 4's job.
  Phase 3 places entry only; exits remain via existing
  `crypto_broker` paths or `coinbase_service.place_sell_order`
  if it exists. If exit paths don't exist for Coinbase, document
  in CC report and surface for Phase 4.
- **No autotrader scope expansion**. Phase 3 only adds a routing
  decision; it does NOT relax any existing entry-eligibility
  rules.
- **Edit-tool truncation discipline (HARD).** `auto_trader.py` is
  >2000 lines per recent memory. After every edit, do
  `wc -l` + `git diff --stat` and confirm no silent truncation.

## Out of scope (Phase 3 — covered by later phases)

- Bracket writer Coinbase paths (Phase 4).
- Cost-aware sizing (Phase 5).
- Paper-trade soak (Phase 6).
- Live verification + capital ramp (Phase 7).
- Coinbase WebSocket order updates (deferred).
- Coinbase Pro / Coinbase Advanced Trade fee-tier optimization
  (deferred — sits with Phase 5 cost-aware sizing).
- Dynamic universe rotation for Coinbase whitelist (deferred —
  Phase 6 will inform).
- USDC-quoted (`-USDC`) tickers (deferred unless operator
  changes funding pattern).

## Sequencing

1. Truncation scan on `auto_trader.py` + `coinbase_service.py`.
2. Read `auto_trader.py` to find the entry-placement callsite +
   capture the existing RH-path call signature for parity test.
3. Write `broker_selector.py` (selector + whitelist resolvers).
4. Write `tests/test_broker_selector.py` (5 decision branches +
   LIVE flag gate).
5. Splice selector call into `auto_trader.py` (RH path unchanged;
   Coinbase path gated on LIVE flag).
6. Add 2 env vars to `app/config.py`.
7. Run pytest — all new tests pass; existing
   `test_entry_feature_parity.py` (or whichever) still green.
8. Force-recreate workers, verify multi-process kill-switch
   pickup.
9. **Single live test** with `CHILI_COINBASE_AUTOTRADER_LIVE=1`
   and a tiny far-below-spot limit-buy + immediate cancel
   (operator may scope-decide whether to use a $1 or $5
   notional). Operator approval required before this step.
10. CC report.
11. Commit + push.
12. Operator decides whether to leave `LIVE=1` for soak (Phase
    6 territory) or revert to `LIVE=0` until Phase 4 lands.

## Operator-side after Phase 3 ships

1. Read CC report.
2. Decide: keep `CHILI_COINBASE_AUTOTRADER_LIVE=1` for shadow
   soak (Phase 6 prep) or flip to `0` until Phase 4 (bracket
   writer Coinbase paths) is in place.
3. If LIVE: monitor `trading_venue_truth_log` daily for the
   first week — any unexpected fills or rejections surface
   here.
4. Promote Phase 4 (bracket writer Coinbase paths) as next
   NEXT_TASK if Phase 3 verifies cleanly.

## Rollback plan

- **Selector behaves badly**: set
  `CHILI_AUTOTRADER_KILL_SWITCH=1`. All entries blocked
  (both RH and Coinbase). 30-second mitigation.
- **Coinbase routing works but resting orders unsafe**: set
  `CHILI_COINBASE_AUTOTRADER_LIVE=0`. RH path unaffected.
  30-second mitigation.
- **Selector returns wrong venue for some ticker**: revert
  the auto_trader.py splice via git revert. Selector
  module + tests stay; just bypass the call.

## What CC should do if it's unsure

1. **`auto_trader.py` callsite ambiguous** (multiple entry
   paths): pick the ONE that handles autotrader pattern-imminent
   entries; do NOT modify other entry paths (e.g., manual
   trades, fast-path scanner). Surface the others in the CC
   report.
2. **`coinbase_service.list_products()` not present**: build the
   whitelist from a hard-coded list in settings as a Phase 3
   placeholder; surface "Phase 3.5 follow-up: dynamic
   product-list resolution" in the CC report.
3. **Existing `trading_venue_truth_log` schema mismatch**: write
   to a NEW table `trading_venue_routing_log` (Phase 3 scope).
   Don't break the existing audit log.
4. **Test parity violation** (RH path call args change for any
   reason): STOP. Surface for operator. RH path is byte-
   identical or nothing ships.
5. **LIVE-flag-on accidental fill**: same as Phase 2 — document
   resulting position in CC report; operator manual close
   required.
