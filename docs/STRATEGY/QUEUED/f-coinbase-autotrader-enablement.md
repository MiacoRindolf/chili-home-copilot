# f-coinbase-autotrader-enablement

STATUS: QUEUED
SLUG: coinbase-autotrader-enablement
PROPOSED: 2026-05-09
SEVERITY: high (operator-funded $2.2k Coinbase account; wider crypto universe; native crypto stop-loss primitive — but cost economics + multi-venue routing require careful design)

## TL;DR

Operator funded a Coinbase account with $2.2k cash and wants the
autotrader to use it alongside Robinhood. Today, autotrader is
RH-implicit (entry decisions assume Robinhood; bracket writer
crashes on RH crypto via the equity primitive — see tonight's
`f-prefilter-bypass-and-cooldown-investigation`).

This is a **multi-phase initiative**. Phase 1 (this brief's
NEXT_TASK promotion) is read-only audit — no broker calls flipped
live. Subsequent phases ship in sequence with operator approval
between each.

**Phases:**
1. **Audit + capability gap** (read-only) — what does
   `coinbase_spot.py` already support, what does autotrader
   assume RH-only, what's the gap, what's the cost-economics
   reality.
2. **Auth + connection verification** — confirm Coinbase API
   credentials, verify cash balance accessible, paper-test
   `place_crypto_buy_order` / `get_crypto_positions` against
   Coinbase.
3. **Broker selection logic** — explicit venue routing in
   autotrader; default crypto entries to Coinbase post-cutover,
   equity stays RH.
4. **Bracket writer + exit monitor Coinbase paths** — wire
   crypto-native stop-loss primitive; remove the
   `crypto_ticker_unsupported_via_equity_primitive` backstop for
   Coinbase tickers.
5. **Cost-aware sizing for $2.2k account** — Coinbase fees ~120
   bps round-trip; existing cost-aware admission gate becomes
   load-bearing.
6. **Paper-trade soak (3-5 days)** — verify entry/exit pipeline
   end-to-end on Coinbase paper before any live trade.
7. **Live verification with small-size** — explicit operator
   approval per first-trade.

## Why now

Operator's framing:
- Coinbase has ~hundreds of crypto pairs vs RH's 15-20.
- Native crypto stop-loss primitive (Coinbase Advanced Trade has
  proper stop orders; RH crypto doesn't via the equity API).
- No PDT regulation on crypto — already handled, but Coinbase
  removes any conflation risk entirely.
- Tonight's mining producer fix (`f-brain-phase2-producer-completion`)
  means new crypto patterns will flow in within 24h — they need
  somewhere good to trade.
- Funded $2.2k cash; ready to deploy.

Pre-existing chili infrastructure (per memory + grep):
- `app/services/trading/venue/coinbase_spot.py` — broker adapter
  exists.
- Fast-path universe rotation already uses Coinbase REST for
  ticker discovery.
- Fast-path executor has maker-only mode for Coinbase
  (`f-fastpath-maker-only-executor`, shipped 2026-05-08).
- `coinbase_service.get_crypto_positions()` exists and is wired
  for fast-path.

The fast-path infrastructure is the foundation. The autotrader
just doesn't use it.

## Phase-by-phase scope

### Phase 1: Audit + capability gap (this brief's NEXT_TASK target)

**Read-only.** No broker calls executed. Outputs a report.

- Catalogue what `venue/coinbase_spot.py` exposes (entry, exit,
  position fetch, order detail, stop placement primitives).
- Catalogue what the autotrader call sites assume RH-only (e.g.,
  `auto_trader.py` direct calls to `broker_service.place_crypto_*`
  which goes to RH).
- Identify the exact change set per phase 2-7.
- Cost economics: with 60 bps taker / 40 bps maker, what's the
  minimum-edge a pattern needs to be Coinbase-economical? At a
  $2.2k account, what's the max-position-size that respects
  operator's risk tolerance?
- Gap list: which existing reconciler / breaker / risk infra
  is RH-implicit and needs venue-aware updates.

**Deliverable**: `docs/STRATEGY/CC_REPORTS/YYYY-MM-DD_f-coinbase-autotrader-enablement-phase-1-audit.md`.

### Phase 2: Auth + connection verification (next brief)

- Verify `CHILI_COINBASE_API_KEY` / `CHILI_COINBASE_API_SECRET`
  configured.
- Confirm `coinbase_service.get_account_info()` returns the funded
  $2.2k.
- Confirm `coinbase_service.get_crypto_positions()` returns
  Coinbase holdings (initially zero).
- Paper-call `place_crypto_buy_order` (e.g., $5 BTC) — verify
  the response shape, then immediately cancel.
- Paper-call native stop-loss primitive — verify Coinbase accepts
  it cleanly.
- Multi-process auth liveness: confirm autotrader-worker /
  scheduler-worker / chili containers all see the same Coinbase
  session (lesson from tonight's RH multi-process auth divergence).

### Phase 3: Broker selection logic

- Add `Trade.broker_source` routing: equity → robinhood, crypto
  → coinbase (default) with feature flag.
- Settings: `CHILI_AUTOTRADER_DEFAULT_CRYPTO_VENUE` (coinbase or
  robinhood, default coinbase post-cutover).
- Pattern alerts route to the right venue at entry-decision time.
- Unit tests: a crypto pattern alert with default settings →
  Coinbase entry; equity pattern alert → RH.

### Phase 4: Bracket writer + exit monitor Coinbase paths

- For Coinbase tickers, route bracket writer to crypto-native
  stop-loss primitive (Coinbase Advanced Trade `STOP_LIMIT` or
  similar).
- Remove the `crypto_ticker_unsupported_via_equity_primitive`
  backstop for Coinbase venues (the backstop stays for RH, since
  RH crypto via equity API is genuinely unsupported).
- Exit monitor: route Coinbase exits via
  `coinbase_service.place_crypto_sell_order` instead of
  `broker_service.place_crypto_sell_order` (which is RH).

### Phase 5: Cost-aware sizing + risk

- Position size formula at entry-decision time: max
  `(account_equity * risk_per_trade_pct) / (stop_distance_pct)`
  bounded by `min(coinbase_min_notional, account_equity * 0.10)`.
- Cost-aware admission gate already exists (fast-path uses
  `2 * (taker_fee_bps + spread_bps)` minimum-edge). Apply same
  to autotrader entries on Coinbase.
- Drawdown breaker: confirm it sees Coinbase positions (was
  RH-implicit; need venue-aware accounting).

### Phase 6: Paper-trade soak

- Enable Coinbase routing in paper mode for 3-5 days.
- Verify a full round-trip: entry placed → fills → bracket
  writer places stop + target → exit fires → `Trade.status='closed'`
  with correct exit reason and pnl.
- Reconciler runs cleanly; no `position_quantity=0` mismatches.
- Drawdown breaker accumulates correctly.

### Phase 7: Live verification, small-size, operator-approved

- First Coinbase live trade: explicit operator approval per-trade
  for the first 5 trades.
- Position size capped at 5% of $2.2k = $110 per trade for the
  first 5.
- Daily review of pnl + reconcile state for the first week.
- Once validated, lift the per-trade approval requirement and
  let the autotrader run.

## Acceptance criteria (Phase 1 only — full multi-phase has its own per-phase criteria)

Phase 1 is the **load-bearing audit** that prevents a band-aid
integration. The operator's directive is "don't band-aid; properly
integrate." Phase 1's report is what makes the integration
architecturally clean. Acceptance criteria are demanding:

### A. Capability inventory (concrete, with line refs)

1. Full table of `app/services/trading/venue/coinbase_spot.py`'s
   public surface: every callable, its current signature, what it
   does today (working / broken / stub), and what's missing for
   autotrader use. Cite line numbers.
2. Same for `app/services/coinbase_service.py` — there are
   parallel surfaces; document which is canonical and which is
   shimming.
3. Authentication state: is Coinbase auth wired? Is there a
   `is_connected()` parallel to `broker_service.is_connected`? If
   yes, what's the multi-process auth-cache behavior (lesson from
   tonight's RH silent-empty incident — Phase 1 MUST surface
   whether Coinbase has the same class of bug latent).

### B. Autotrader RH-implicit assumption inventory

4. Every call site in `app/services/trading/auto_trader.py` and
   `auto_trader_monitor.py` that calls `broker_service.*` directly
   instead of going through a venue abstraction. Cite line
   numbers + the implicit RH assumption. This is the refactor
   surface for Phase 3 — if it's >20 sites, Phase 3 has to split.
5. Same for `pdt_guard.py`, `portfolio_risk.py`,
   `bracket_writer_g2.py`, `crypto/exit_monitor.py`,
   `bracket_reconciliation_service.py`. Multi-venue support
   requires venue-aware updates to each; the audit catalogues
   them.
6. Database schema audit: does `Trade.broker_source` already
   exist (yes per memory); does it actually segregate
   broker-specific queries everywhere it should? Find queries
   that filter on `Trade.user_id == X` without
   `Trade.broker_source` and assess whether they conflate venues.

### C. Cost economics with REAL numbers

7. Coinbase fee schedule for the operator's tier (assume tier 1,
   $0-10k volume, 60 bps taker / 40 bps maker — but verify
   per the Coinbase docs the operator's account is on this tier).
8. Round-trip cost calculation: taker-buy + taker-sell = 120 bps;
   maker-buy + taker-sell = 100 bps; maker-both = 80 bps.
9. Minimum edge a pattern needs to be Coinbase-economical at each
   round-trip cost level. Compare to the realized edge of patterns
   1011 (63.2% WR, +1.96% avg return) and 1016 (70.7% WR, +0.84%
   avg return). Conclusion: are these patterns Coinbase-economical
   under taker round-trip, maker round-trip, or neither?
10. Position sizing: at $2.2k account with 1-2% risk-per-trade,
    typical position size is $22-44 notional. Coinbase minimum
    notional per pair (typically $1-10). Maximum reasonable
    position count given the cash constraint. Account-level vs
    per-pair caps.

### D. Risk infrastructure audit

11. Drawdown breaker: today aggregates closed PnL across
    `Trade.user_id`. Does it segregate by `broker_source` or pool?
    If pooled, a Coinbase loss could trip the RH breaker (and vice
    versa). Surface as Phase 5 subtask if pooled; document the
    intended behavior.
12. PDT guard: confirms it correctly skips crypto entirely (we
    fixed this today with `_RECONCILE_ARTIFACT_EXIT_REASONS` and
    R35 crypto bypass). Verify Coinbase trades won't trigger PDT
    even by accident.
13. Kill switch / circuit breaker: cross-venue or per-venue? If
    cross-venue (likely), confirm the kill-switch consumer at
    Coinbase entry-decision honors it (today only RH does).
14. Position-correlation risk: if an operator holds AVAX-USD on
    RH AND AVAX-USD on Coinbase, the position-risk calculator
    must aggregate (operator is double-exposed). Audit the
    risk calculator's awareness.

### E. Multi-venue venue-abstraction design

15. Today's autotrader is RH-implicit. Phase 3 needs an
    abstraction. Phase 1 PROPOSES the design (interface, where
    it lives, how migration happens) — does NOT implement.
    Options to evaluate in the report:
    - Adapter-pattern with `VenueAdapter.place_buy_order(...)`
      that each broker implements
    - Function-dispatch via
      `_venue_router.route(asset_kind, broker_source).place_buy(...)`
    - Subclass `Trade` with `Trade.execute_buy()` polymorphic
    Recommend ONE; document why.
16. Cutover strategy: how do we migrate from RH-implicit to
    explicit-venue without breaking the 11 currently-open RH
    crypto positions? Backfill `broker_source='robinhood'` on
    existing rows? Make the new path opt-in via a settings flag
    until soak-tested?

### F. Reconciler + lifecycle audit

17. Today's RH crypto reconciler (Phases A+B+C) handles
    `broker_reconcile_position_gone` etc. Coinbase reconciliation
    is event-driven via Coinbase webhooks (not polled the same
    way). Audit how Coinbase position-tracking works today
    (per fast-path) and propose the autotrader-side reconciler
    architecture for Coinbase. Is the existing fast-path
    Coinbase position-tracking shared with autotrader, or is
    duplication needed?

### G. Phase recommendations + scope estimates

18. Phase 2-7 each get explicit scope estimates (small / medium /
    large) with prerequisite chains. If Phase 3 (broker selection)
    requires a venue abstraction not yet present, that abstraction
    becomes Phase 2.5 or part of Phase 3 — surface the choice.
19. Risk-to-existing-system rating per phase. Phases 2 (auth) +
    3 (selection logic) are LOW because they're additive opt-ins.
    Phase 4 (bracket writer Coinbase paths) is MEDIUM — the
    backstop we shipped tonight must NOT be removed for RH while
    being routed-around for Coinbase. Phase 5 (cost-aware sizing)
    + Phase 6+7 (live verification) are HIGH because real money
    moves.
20. Suggested first-fix-after-Phase-1: usually Phase 2 (auth) but
    audit data may surface a different priority.

### H. Hard constraints

21. Phase 1 itself ships ZERO code. Read-only research, just like
    tonight's `f-pattern-pipeline-eligibility-audit`.
22. Phase 1's report length: <600 lines (target ~400). Operator
    readability over completeness — appendix file allowed for
    detailed query output / line-by-line code dumps.
23. Phase 1 surfaces gaps; does NOT propose to fix them in this
    brief.

## Brain integration (read-only for Phase 1)

- `app/services/trading/venue/coinbase_spot.py` — read.
- `app/services/trading/auto_trader.py` — read; identify RH
  assumptions.
- `app/services/trading/auto_trader_monitor.py` — read.
- `app/services/coinbase_service.py` — read.
- `app/services/trading/bracket_writer_g2.py` — read; identify
  the crypto-native primitive gap.
- Fast-path code (`fast_path/executor.py`, etc.) for the Coinbase
  patterns already in use.

## Constraints / do not touch

- **Hard Rule 1**: live-placement safety belts unchanged.
- **Hard Rule 5**: prediction-mirror authority untouched.
- **Operator's directive: don't break what works.** RH autotrader
  + RH crypto reconciler chain (Phases A+B+C) untouched. The 11
  open RH crypto positions and patterns 1011/1016 must continue
  to work exactly as today.
- **NO live Coinbase trades from Phase 1.** Audit only.
- **NO code changes from Phase 1.**
- **DO NOT remove tonight's `crypto_ticker_unsupported_via_equity_primitive`
  backstop** for RH crypto. That backstop is correct for RH; only
  Coinbase gets a different primitive (Phase 4).
- **Edit-tool truncation discipline (HARD).**

## Out of scope (this brief — covered by later phases or other briefs)

- Architectural rebuild Phase 1 (auth liveness — separate
  multi-week initiative; complementary).
- Multi-broker support beyond RH + Coinbase (e.g., adding
  Hyperliquid). Future brief.
- Universe expansion beyond what Coinbase already provides
  (separate brief if surfaced).

## Sequencing (within Phase 1)

1. Truncation scan on the read-target files.
2. Coinbase adapter capability inventory.
3. Autotrader RH-assumption inventory.
4. Cost economics calculation (real numbers from Coinbase fee
   schedule; operator's risk tolerance).
5. Phase 2-7 scope estimation with explicit prerequisite chain.
6. Recommendation: which Phase 2 brief to write first.
7. Commit + push the audit report.

## Operator-side after Phase 1 ships

1. Read the audit report.
2. Decide which gap is the load-bearing first fix:
   - Auth verification (Phase 2) — usually first
   - Broker selection logic (Phase 3) — also early
3. Approve next phase brief; CC ships it.

## Rollback plan

N/A — Phase 1 is read-only.

## What CC should do if it's unsure

1. **If `coinbase_spot.py` is incomplete relative to needs**,
   document the gaps in the audit report — those become Phase 4
   subtasks. Don't try to extend the adapter from Phase 1.
2. **If the autotrader's RH-assumptions are deeper than expected**
   (i.e., refactoring is significant), surface the cost in the
   report. Phase 3 may need to be split.
3. **If Coinbase API authentication is broken out of the box**
   (operator may have configured the keys but chili never validated
   them), surface the gap; Phase 2 becomes urgent before Phase 3.

## Other queued briefs (parked while this initiative runs)

* `f-cpcv-gate-emit-anomaly-investigation` (cheap discovery win,
  separable, can interleave between Coinbase phases)
* `f-pattern-oos-revalidation` (medium scope, conditional on
  mining producing fresh candidates)
* `f-crypto-pattern-discovery-expansion` (universe expansion at
  multiple timeframes — partly subsumed by Coinbase enablement
  if Coinbase pairs auto-flow into the miner)
* `f-crypto-reconcile-architectural-rebuild` Phase 1 (auth
  liveness — operator-decoupled; multi-week)
