# 006 — Asset-class-segregated lane breakers

**Status:** Draft (2026-04-30) — pending operator review.
**Authors:** internal (response to user-driven research request on
intraday crypto cadence + R31 fix scope).
**Supersedes:** none.
**Related:** ADR-005 (canonical truth layer), R31 commit `7af3d49`,
R32 commit `539e1c2`, R33 commit `9acf769`.

## Context

CHILI today has **one global drawdown circuit breaker** in
`app/services/trading/portfolio_risk.py` — `check_drawdown_breaker`
evaluates four trip rules across the union of every CHILI-placed
trade regardless of asset class:

1. Mark-to-market open-position drawdown
2. 5-day realized drawdown vs `brain_risk_max_5d_dd_pct` (default 3%)
3. 30-day realized drawdown vs `brain_risk_max_30d_dd_pct` (default 8%)
4. Consecutive-losses counter vs `brain_risk_max_consec_losses` (default 5)

R31 (`7af3d49`) tightened rule 4 by excluding synthetic exit reasons
(`broker_reconcile_position_gone`, `phantom_no_broker_id`, etc.) and
adding a 1%-of-capital magnitude floor so micro-losses don't manufacture
false trips. R32 (`539e1c2`) closed the upstream root cause that was
generating those synthetic exits in the first place.

Even with R31 + R32 in place, the breaker is still **a single global
circuit**: a streak of 5 real consecutive equity losses pauses ALL
trading, including crypto, even though equity and crypto are
behaviorally and microstructurally distinct lanes. The user's research
request — "how do well-architected retail algo traders structure
breakers, and should crypto intraday have a different one?" — surfaces
the design tension directly.

### Why single-breaker is a poor fit for CHILI

Three concrete asymmetries:

1. **Cadence.** Equity trades typically last 1–5 days; crypto trades
   were *intended* to last minutes-to-hours (the user explicitly
   expected "minute-or-two holds" on crypto, and R33 just shortened
   the pattern-imminent cooldown from 3h to 0.5h for crypto). Sharing
   a 5-loss counter means crypto's higher trade frequency dominates
   the streak signal: a normal day of crypto could exhaust the budget
   while equity is still compounding.

2. **Volatility regime.** Crypto has materially higher idiosyncratic
   vol than equity — the same `brain_risk_max_5d_dd_pct = 3%` applied
   to crypto is roughly equivalent to a `1%` threshold on equity. Real
   "this strategy is broken" signal in crypto requires a wider band;
   sharing the equity-calibrated band creates false trips.

3. **Independence of edge.** A 5-streak loss in equity does not
   evidence that crypto edge is also broken (and vice versa). The
   pattern, regime, and execution-cost models are largely independent
   per lane. Halting both on one lane's failure throws away the
   half of the system that's still healthy.

### What "well-architected retail algo" looks like

From the user's research prompt and the literature (Lopez de Prado,
Carver, Chan), the shared design pattern is:

- **Per-strategy / per-lane budgets.** Each strategy or asset class
  carries its own loss budget that ratchets independently.
- **Global meta-budget as a safety floor.** A single firm-wide cap
  still exists — usually a hard MTM drawdown or daily loss limit — but
  it is *additional* to the lane budgets, not a replacement.
- **Regime-scaled thresholds.** Carver's Robust 4 and Chan's
  vol-targeting both scale per-strategy thresholds by realized vol so
  the breakers self-recalibrate when conditions shift.
- **Cool-down with rampup, not binary.** When a breaker trips, the
  system reduces size for a window rather than going to zero; full
  resume only after evidence of healthy execution.

CHILI partially has the regime-scaled thresholds (the
`_REGIME_DD_MULTIPLIERS` table in `portfolio_risk.py:782`) but
explicitly lacks the per-lane budgets.

## Decision

Introduce **per-lane drawdown breakers** keyed on `asset_class ∈
{equity, crypto_spot}` (extensible later to `crypto_perps` once that
lane lands). Keep the existing global breaker as a meta-safety floor.
A trade is gated only if **either** the lane breaker for its asset
class **or** the global meta-breaker is tripped. Both surfaces persist
into `trading_risk_state` so the operator dashboard can show which
lane is healthy.

The minimum viable shape:

```python
# Per-lane state, persisted in trading_risk_state via a
# (lane, snapshot_date) composite key.
@dataclass
class LaneBreakerState:
    lane: str  # 'equity' | 'crypto_spot' | 'crypto_perps'
    consecutive_losses: int
    consecutive_loss_streak_pct: float  # sum of streak loss / capital
    last_trip_at: datetime | None
    last_trip_reason: str | None

# Resolution flow on a trade-close event:
def on_trade_closed(trade) -> None:
    lane = lane_for(trade)               # equity vs crypto_spot vs crypto_perps
    update_lane_state(lane, trade)
    if breaker_should_trip(lane_state):
        trip_lane(lane, reason=...)
    update_global_meta_state(trade)
    if breaker_should_trip(global_state):
        trip_global(reason=...)

# Resolution flow on an entry attempt:
def can_open(trade_intent) -> tuple[bool, str | None]:
    lane = lane_for(trade_intent)
    if is_lane_tripped(lane):
        return False, f"lane_breaker:{lane}"
    if is_global_tripped():
        return False, "global_breaker"
    return True, None
```

## Implementation phases

Six phases, deployed in order. Each phase is independently shippable
and reversible by feature flag.

### Phase A — schema (mig 215)

- Add `lane TEXT NOT NULL DEFAULT 'global'` to `trading_risk_state`
  with a CHECK constraint over the allowed values.
- Add a partial unique index on `(lane, snapshot_date)` so the
  operator can't accidentally double-write the same lane/day.
- Backfill existing rows with `lane='global'`.
- Index `(lane, breaker_tripped, created_at DESC)` for the
  read-most-recent-tripped-row queries.

### Phase B — lane attribution at trade close

- Add a `lane_for(trade) -> str` helper in `portfolio_risk.py` that
  reuses `correlation_budget.is_crypto_symbol` plus a placeholder for
  perps that returns 'crypto_perps' once that lane lands.
- Persist `lane` on the `Trade` row at entry time so close-time
  attribution is non-revisable.
- New `update_lane_breaker_state(db, lane, trade)` writes per-lane
  state alongside the existing global state. **Shadow only** —
  doesn't gate anything yet.

### Phase C — read-only operator dashboard

- Surface per-lane state in `routers/trading_sub/operator.py` so the
  dashboard renders three widgets: equity, crypto_spot, global.
- This phase is purely visualization; gives the operator a chance to
  watch lane state populate for several days before any gating change
  takes effect.

### Phase D — shadow compare

- New flag `chili_lane_breakers_mode` ∈ {'off', 'shadow', 'authoritative'}.
- In `shadow`, both global and lane breakers compute on every event,
  but only the global breaker gates entries. Disagreements log a
  WARNING (`would_have_blocked_lane=...`, `actually_blocked=...`).
- Run for at least one full crypto-cycle week before flipping.

### Phase E — authoritative cutover

- Flip `chili_lane_breakers_mode = 'authoritative'`.
- Entry gate now consults BOTH breakers; either tripping refuses entry.
- Existing global thresholds become the meta-safety floor (loosened
  by ~30% to give lane breakers room to do their job before the
  meta floor kicks in).
- Per-lane thresholds tightened relative to today's global values:
  - equity: keep 5 consecutive, 3% / 8% DD
  - crypto_spot: 7 consecutive (higher cadence tolerance), 5% / 12% DD
  - crypto_perps (when lane lands): 4 consecutive, 4% / 10% DD
    (more aggressive because of leverage)

### Phase F — vol-targeted self-recalibration (optional, post-cutover)

- Replace static per-lane thresholds with rolling-vol-targeted ones
  (Carver's design). Defer until phase E has stable evidence.

## Consequences

### Positive

- **Crypto cadence unblocked.** A bad equity streak no longer halts
  crypto, which is the user's specific complaint about "expected
  minute-or-two holds, seeing days." R33 fixed the cooldown layer;
  this fixes the breaker layer.
- **Lane-fair attribution.** Operator dashboard tells you which lane
  is in trouble, not just that "the brain is paused."
- **Independent recovery.** When one lane recovers (cooldown elapsed),
  it resumes immediately rather than waiting for the slower lane.
- **Cleaner postmortem story.** R31's incident is exactly the failure
  mode this design prevents: synthetic crypto exits would have
  tripped only the crypto lane, leaving equity untouched.

### Negative

- **More state to reason about.** Operator dashboard must show
  three states (equity, crypto, global) instead of one.
- **Gaming risk.** A pattern that loses a small amount in lane A
  then wins in lane B can stay live longer than under a single
  breaker. Mitigated by the global meta-safety floor.
- **Migration cost.** Mig 215 plus a multi-week shadow window
  before any behavior changes. Not free.

### Neutral

- **Three lanes is the cap, not the start.** Adding a fourth lane
  later (options? overnight crypto?) requires a new mig and an entry
  in `lane_for`, but no other code change.

## Open questions for operator

1. **Should the global meta-floor be loosened on cutover?** Phase E
   suggests +30% to give lane breakers room. Operator may prefer
   tighter — say flat — and accept that lane breakers will be
   irrelevant until lane-specific signals exceed the global cap.
2. **Lane attribution backfill.** Phase B persists `lane` on new
   trades but doesn't touch existing rows. If the operator wants
   lane-aware history, a one-time backfill query (in mig 215) infers
   `lane` from `is_crypto_symbol(ticker)` for closed rows.
3. **Crypto perps timing.** Phase B treats `crypto_perps` as a
   placeholder. The lane plumbing should be in place before perps
   ingestion lands so the operator doesn't need a second migration
   when perps come online.
4. **Should lane-tripped propagate to `is_breaker_tripped()`?** The
   in-process flag is currently global. Easiest is to keep it global
   and add a parallel `is_lane_tripped(lane)` API; harder is to make
   the entry gate query both and have callers pass `lane` context.
   Recommendation: parallel API to avoid touching all callers.

## Roll-out trigger

This ADR is **drafted, not active**. Phases A–F should not begin
until:

- Egress block is resolved (`project_universal_egress_block`) — no
  point shipping breaker changes while the brain has zero candidates.
- At least one full week of post-R31/R32/R33 trading data has
  accumulated so the operator can see whether the existing global
  breaker still trips falsely after the cascade fix.
- The operator explicitly authorizes Phase A. ADR-006 is a sketch,
  not a commitment.
