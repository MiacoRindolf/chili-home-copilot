# f-coinbase-autotrader-enablement-phase-5-cost-aware-sizing

**Owner**: Cowork → Claude Code
**Status**: PENDING
**Risk**: MEDIUM (touches sizing/gate chain; if broken, Coinbase
entries either size wrong or get blocked when they shouldn't)
**Time budget**: 3-4h CC scope (single session, design +
implementation + tests)

## Goal

Make the autotrader **fee-aware** when routing to Coinbase. The
Coinbase Advanced Trade Tier 1 fee is **60bps taker / 40bps
maker**, so a 120bps round-trip burns silently into edge if the
min-edge gate doesn't account for it. Phase 5 closes that gap and
codifies per-venue notional caps + the USD/USDC buying-power
contract surfaced in Phase 2 G1.

After Phase 5 ships, the operator can flip
`CHILI_COINBASE_AUTOTRADER_LIVE=1` for a paper-soak (Phase 6)
without silently underwater fills.

## Why now

Phases 1-4 shipped (commits `39e9807` audit → `74b907b` Phase 2
verify → `403027b` selector → `aca780d` bracket writer). Phase 4
explicitly documented Phase 5 as the **last hard prerequisite**
before LIVE flip:

> Operator should NOT flip `CHILI_COINBASE_AUTOTRADER_LIVE=1`
> until Phase 5 (cost-aware sizing) ships. Without this, the
> 60bps-taker fee burns silently into edge.

## Operator-locked design constraints (binding from Phase 1)

These stay binding (no scope expansion):

1. Cross-venue position cap: SEPARATE per-venue caps.
2. Kill switch: GLOBAL.
3. Selector preference for both-listed: RH-first.
4. Fast-path overlap: skip-on-fast-path-active.

## The change (4 components)

### Component A — Coinbase buying-power resolver

New helper in `app/services/trading/coinbase_buying_power.py`
(or sit in `coinbase_service.py` if simpler):

```python
def resolve_coinbase_buying_power() -> dict:
    """
    Returns {'usd': float, 'usdc': float, 'total': float,
             'last_updated': iso_str}.

    usd:   portfolio.cash from get_portfolio() (USD wallet)
    usdc:  USDC quantity from get_positions() (treats 1 USDC = $1)
    total: usd + usdc

    Per Phase 2 G1: Coinbase BUY orders for `-USD` pairs debit USD
    wallet only; USDC funds are inactive until converted. Phase 5's
    sizing logic uses `usd` for the actual buying-power check, but
    surfaces `usdc` so the operator can see stranded capital.
    """
```

Cache the result for 30s (avoid hammering `get_portfolio` on every
sizing decision).

### Component B — Cost-aware min-edge gate

New gate in the autotrader entry chain (probably best in
`auto_trader.py` near the existing min-edge check, OR in a
dedicated `app/services/trading/cost_aware_gate.py` if the chain
is large enough):

```python
def cost_aware_min_edge_gate(
    *,
    venue: str,
    expected_edge_bps: float,
    settings_=None,
) -> dict:
    """
    Returns {'pass': bool, 'reason': str, 'fee_bps_round_trip': float}.

    For RH equity:    fee_bps = 0 (RH is fee-free).
    For RH crypto:    fee_bps = settings.chili_rh_crypto_fee_bps_round_trip
                                 (default 0; RH crypto fee model TBD).
    For Coinbase:     fee_bps = settings.chili_coinbase_taker_fee_bps_round_trip
                                 (default 120 = 60+60 taker round-trip).

    Buffer: settings.chili_min_edge_safety_buffer_bps (default 30 bps)
            ensures expected edge clears fee + buffer.

    Pass condition: expected_edge_bps >= fee_bps_round_trip + buffer_bps.
    """
```

The gate **runs BEFORE** the broker selector emits a route
decision so a Coinbase route gets blocked at the gate when edge
is too thin. RH equity routes pass through this gate naturally
(fee=0).

### Component C — Per-venue notional caps

Two new settings (defaults conservative):

```python
chili_coinbase_max_notional_usd: float = 50.0
   # Max $ per Coinbase entry. Conservative default for paper-soak.
   # Operator raises after Phase 6 verifies routes are sane.

chili_coinbase_max_concurrent_positions: int = 3
   # Max Coinbase positions held at once. Independent from RH cap
   # per design constraint #1 (no cross-venue aggregation).
```

The cap reads at the routing-decision point in `auto_trader.py`:

```python
if _venue_decision.venue == "coinbase":
    open_cb = _count_open_coinbase_positions(db)
    if open_cb >= settings.chili_coinbase_max_concurrent_positions:
        # audit + skip
    if requested_notional_usd > settings.chili_coinbase_max_notional_usd:
        # clip to cap (or skip; operator-decided default = clip)
```

### Component D — Autotrader splice + tests

1. **Splice cost-aware gate** into autotrader's pre-routing
   chain. Order: kill switch → drawdown breaker → ensemble
   promotion → cost-aware gate → broker selector → place. The
   gate sits AHEAD of selector so a Coinbase block doesn't waste
   a selector call.
2. **Splice per-venue cap check** at the routing-decision point
   (after selector returns `venue=coinbase`).
3. **Tests** in `tests/test_cost_aware_gate.py` (gate decisions
   for RH equity / RH crypto / Coinbase across edge thresholds)
   + `tests/test_coinbase_notional_cap.py` (cap clip + cap block).

## Acceptance criteria (10-item list)

1. **`resolve_coinbase_buying_power` shipped** with the documented
   `{usd, usdc, total, last_updated}` shape; 30s cache; logs once
   at info on first call per process to surface USDC standing.
2. **Cost-aware min-edge gate** shipped; for RH equity: fee=0,
   identical pass behavior to pre-Phase-5 (no regressions).
3. **Coinbase fee defaults**: `CHILI_COINBASE_TAKER_FEE_BPS_ROUND_TRIP=120`
   and `CHILI_MIN_EDGE_SAFETY_BUFFER_BPS=30`. Operator can
   override.
4. **Per-venue notional cap settings**: `CHILI_COINBASE_MAX_NOTIONAL_USD=50`
   (conservative paper-soak default), `CHILI_COINBASE_MAX_CONCURRENT_POSITIONS=3`.
5. **Autotrader splice** places cost-aware gate ahead of broker
   selector; per-venue cap check at routing decision point. RH
   path BYTE-IDENTICAL (parity unit test).
6. **Tests**: `test_cost_aware_gate.py` covers ≥6 cases (RH
   equity pass / RH crypto pass / Coinbase pass / Coinbase block
   below fee+buffer / Coinbase pass at exactly fee+buffer / kill
   switch fall-through). `test_coinbase_notional_cap.py` covers
   clip + block.
7. **No regressions on RH stop path** (Phase 4 parity test still
   passes).
8. **Multi-process verification**: all 4 worker containers
   resolve the new settings to defaults. Buying-power resolver
   importable in autotrader-worker.
9. **Cost log preserved**: routing decisions write
   `cost_gate_pass:venue=...:fee=...` audit lines.
10. **CC report at**:
    `docs/STRATEGY/CC_REPORTS/<YYYY-MM-DD>_f-coinbase-autotrader-enablement-phase-5-cost-aware-sizing.md`.

## Brain integration (read + write)

**Read-only:**
- `app/services/trading/auto_trader.py` — find min-edge gate
  callsite + capture pre-Phase-5 RH equity behavior for parity
  test.
- `app/services/trading/broker_selector.py` — read for ordering
  guarantees (gate must fire BEFORE selector).
- `app/services/coinbase_service.py` — confirm
  `get_portfolio` / `get_positions` shapes (verified Phase 2/3).
- `app/services/trading/bracket_writer_g2.py` — Phase 4 work
  unchanged.

**Write:**
- `app/services/trading/coinbase_buying_power.py` — NEW (or in
  `coinbase_service.py` if smaller scope wins).
- `app/services/trading/cost_aware_gate.py` — NEW.
- `app/services/trading/auto_trader.py` — splice gate + cap
  check; preserve RH equity path verbatim.
- `app/config.py` — 4 new settings (fee, buffer, max-notional,
  max-concurrent).
- `tests/test_cost_aware_gate.py` — NEW.
- `tests/test_coinbase_notional_cap.py` — NEW.

## Constraints / do not touch

- **Hard Rule 1**: live-placement safety belts unchanged.
- **Hard Rule 5**: prediction-mirror authority untouched.
- **RH equity path BYTE-IDENTICAL** (cost gate is a no-op for
  fee=0; parity test gates this).
- **No new entry signal logic.** Phase 5 is sizing-side only.
  Don't touch alert ingestion, pattern-imminent triggering,
  ensemble promotion check.
- **No changes to `coinbase_spot.py` adapter** — Phase 4
  shipped that. Phase 5 reads from it.
- **No `CHILI_COINBASE_AUTOTRADER_LIVE=1` flip in Phase 5.**
  Stays operator-controlled. Phase 5's "live test" is gate
  decisions verified via mocked-portfolio unit tests; LIVE flip
  is Phase 7 territory.
- **No paper-soak.** Phase 6's job. Phase 5's gate decisions
  must be unit-test green before Phase 6 starts.
- **Edit-tool truncation discipline (HARD).** `auto_trader.py`
  is now 1743 lines. After every edit: `wc -l` + `git diff
  --stat` + AST-parse check.

## Out of scope (Phase 5 — later phases)

- Paper-trade soak (Phase 6).
- Live with capital ramp (Phase 7).
- Coinbase Pro / different fee tiers (operator may opt into
  later if volume increases).
- Maker-only routing for Coinbase (deferred — sits with the
  fast-path maker-only work; can be folded in if operator
  decides to pursue).
- USDC-quoted (`-USDC`) ticker support (deferred unless
  operator changes funding pattern).
- Dynamic universe rotation for Coinbase (Phase 6+ informed).

## Sequencing

1. Truncation scan on `auto_trader.py`, `broker_selector.py`,
   `coinbase_service.py`.
2. Read autotrader to find min-edge gate callsite + capture
   RH equity pre-state for parity test.
3. Write `resolve_coinbase_buying_power` (in
   `coinbase_buying_power.py` or `coinbase_service.py`).
4. Write `cost_aware_min_edge_gate`.
5. Write tests for both — fail first to confirm test infra
   correct, then make pass.
6. Add 4 new settings to `app/config.py`.
7. Splice gate + cap check into `auto_trader.py` (RH equity
   byte-identical; new logic gated on `venue=coinbase`).
8. Run full pytest — RH equity parity gate held; new tests
   green.
9. Force-recreate workers; verify multi-process import +
   settings pickup.
10. CC report.
11. Commit + push.

## Operator-side after Phase 5 ships

1. Read CC report.
2. (Optional) override `CHILI_COINBASE_TAKER_FEE_BPS_ROUND_TRIP`
   if the actual Coinbase fee tier has changed (e.g., higher
   volume tier, or maker-mostly routing pattern).
3. (Optional) raise `CHILI_COINBASE_MAX_NOTIONAL_USD` from the
   conservative $50 default once Phase 6 verifies routes are
   sane.
4. **Decide**: queue Phase 6 (paper soak) or hold for more
   review? Phase 6 is the soak that verifies routes match
   expectations BEFORE live flip.

## Rollback plan

- **Cost gate misbehaves**: `git revert` the autotrader splice;
  the gate module + tests stay. RH path returns to pre-Phase-5
  behavior (fee=0 was a no-op anyway).
- **Notional cap blocks legitimate entries**: raise the
  setting in `.env` + force-recreate workers.
- **Buying-power resolver hangs / crashes**: cache failure mode
  returns the last cached value; if stale > 5 min, the gate
  emits CRITICAL log + falls back to skip (conservative).
  Operator can also flip `CHILI_COINBASE_AUTOTRADER_LIVE=0`.

## What CC should do if it's unsure

1. **Existing min-edge gate location ambiguous**: pick the gate
   the autotrader currently uses; surface alternates in CC
   report; do NOT touch them.
2. **`get_portfolio` / `get_positions` response shape changed
   since Phase 2 redux**: STOP. Surface for operator. Don't
   guess.
3. **Coinbase fee tier higher than 60bps taker**: read the
   actual tier from operator (Coinbase UI shows this) and
   adjust default. Document in CC report.
4. **RH equity parity test fails**: STOP. RH equity path is
   byte-identical or nothing ships.
5. **Buying-power resolver shows USDC > $0 again** (operator
   re-deposited): document in CC report; do NOT change `-USD`
   convention. Phase 5 surfaces stranded USDC for operator
   awareness; convention change is operator decision in Phase
   5.5 if desired.
