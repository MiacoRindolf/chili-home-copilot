# f-portfolio-vs-pattern-breaker-separation — two-tier drawdown architecture

## Context

`f-monthly-dd-breaker-numerator-symmetrize` (prerequisite) closes the
immediate open-loop bug: the monthly-DD breaker now sees a numerator
and threshold drawn from the same CHILI-attributed distribution. That
fix is **necessary but not sufficient**.

The remaining gap: CHILI's account can still drain through channels
that the pattern-attributed breaker, by design, cannot see. As of
2026-05-16:

- 67-day cum realized PnL across **all** closed trades: **−$1,172**.
- Of that, the no_pattern bucket contributed **−$1,560** (~133% of the
  bleed). CHILI-attributed contributed **+$388**.
- The 2026-05-15 quant audit established the legacy-cleanup channel is
  draining the account while CHILI-attributed flow is mildly +EV.
- An account-level halt for "the account is shrinking, stop all new
  entries regardless of source" does not exist today.

A pattern-attributed breaker should not be load-bearing for account
preservation; it should gate **pattern decisions**, not the **account**.
These are different concerns with different inputs, different
distributions, and different appropriate K-sigma tunings. Conflating
them led to the original asymmetry.

## Goal

Introduce a **two-tier breaker** architecture:

1. **Portfolio breaker** — account-level. Numerator and threshold both
   computed from **all-closed** PnL history. Gates **every entry path**
   that places real-money orders (CHILI-attributed, no_pattern,
   manual, broker-reconcile-inferred recoveries — anything that touches
   buying power). Default-OFF.

2. **Pattern breaker** — strategy-level. Numerator and threshold both
   computed from **CHILI-attributed** PnL history (already shipped by
   `f-monthly-dd-breaker-numerator-symmetrize`). Gates only
   CHILI-attributed decisions. Default-OFF.

Each tier is statistically coherent within itself; each gates only what
its trip signal can act on. No more open loops.

## Non-goals

- Re-tuning K-sigma on either tier. Default 2.0σ for both initially;
  separate tuning brief later if needed.
- Eliminating the no_pattern bucket. That is handled by the legacy
  cleanup + composite-reweight track. The portfolio breaker is a safety
  net, not a strategy fix.
- Removing or replacing the existing pattern breaker. The two coexist.

## Deliverables

### D1. New `_portfolio_dd_threshold` function

**Where:** `app/services/trading/portfolio_risk.py`, alongside
`_monthly_dd_threshold`.

**What:** Same math as `_monthly_dd_threshold` but **without** the
`scan_pattern_id` filter. Returns `(threshold_usd, n_days_observed)`.
Same `n_days < 30` skip-with-warning behavior. Same K-sigma settings
key but a separate one so the two can be tuned independently:
`chili_portfolio_dd_breaker_lower_bound_sigmas` (default 2.0).

### D2. New `check_portfolio_drawdown_breaker` (or extend existing)

Architect's choice: either add a second function next to
`check_drawdown_breaker` or extend the existing one with a second tier
inside. Recommend **separate function** because:

- Different return value (different reason string, different breaker
  state key for `_persist_breaker_state`).
- Different lever — see D3.
- Easier to disable one tier without disturbing the other.

Acceptance: function returns `(tripped: bool, reason: str | None)` and
calls into `_portfolio_dd_threshold`. Same n<30 skip behavior.

### D3. Wire the portfolio breaker into the **entry path**, not the pattern path

This is the critical architectural distinction.

The existing pattern breaker is consulted by `auto_trader.py` before
placing a CHILI-attributed entry. The portfolio breaker must be
consulted **earlier in the chain**, by any code path that calls the
venue adapters (`venue/robinhood_spot.py`, `venue/coinbase_spot.py`)
to place a buy order — regardless of pattern attribution.

Candidate gate point: a single helper at the venue adapter boundary
(e.g. `_assert_portfolio_breaker_ok(db, user_id)` called from the top
of both `place_market_order` / `place_limit_order` / similar). Pattern
attribution is irrelevant here.

Acceptance test: with the portfolio breaker tripped, **no** path —
CHILI-attributed, no_pattern reconcile-driven, broker-sync rehydration,
nothing — can place a buy order. The breaker is a kill switch on the
entry boundary, not on the strategy boundary.

### D4. Two independent flags

- `chili_pattern_dd_breaker_enabled` (rename existing
  `chili_monthly_dd_breaker_enabled` for clarity; provide an alias for
  backwards-compat for one release).
- `chili_portfolio_dd_breaker_enabled` (new).

Both default-OFF. Each is independently flippable.

### D5. Two independent persisted breaker-state keys

Avoid stomping on each other in `_persist_breaker_state`. Suggested
keys: `pattern_dd_tripped` and `portfolio_dd_tripped`. UI / runbook
surfacing should show **both** states distinctly.

### D6. Tests

- Portfolio breaker trips correctly on all-closed PnL exceeding
  portfolio threshold, regardless of pattern attribution.
- Portfolio breaker does **not** trip on a heavy CHILI-attributed loss
  that the pattern breaker would catch, provided all-closed PnL is
  within portfolio bounds (i.e. CHILI loses big but no_pattern was a
  big winner — the rare case).
- Pattern breaker still works as in `f-monthly-dd-breaker-numerator-symmetrize`.
- With portfolio breaker tripped, a manual / no_pattern entry attempt
  through the venue adapter is rejected.
- With portfolio breaker *not* tripped but pattern breaker tripped,
  CHILI-attributed entries are blocked but a no_pattern reconcile entry
  is allowed. (This validates the lever-signal alignment.)

### D7. Runbook update

Update `docs/DRAWDOWN_BREAKER_RUNBOOK.md` to cover both tiers — how to
read each state, when each is expected to trip, how to reset each, and
the principle that the two tiers gate different decision boundaries.

## Acceptance

1. Both breakers exist and can trip independently.
2. Portfolio breaker is consulted at the venue-adapter entry boundary;
   pattern breaker stays at the autotrader decision boundary.
3. All D6 tests pass.
4. Runbook clearly explains the two-tier design.
5. Both flags default-OFF. Arming each is operator-controlled.

## Risk

Medium. The portfolio breaker gates *every* entry path, so a bug in
the wiring (e.g. accidentally consulting it from a code path that
should not be gated) could halt the system entirely. Default-OFF +
extensive tests + 7-day shadow-log mode before the operator flips it
on are the mitigations. Add a shadow-log path that records "would
have tripped" decisions for at least 7 days before live-arming.

## Timing

Not before `f-monthly-dd-breaker-numerator-symmetrize` ships. After
that, no specific deadline — the pattern breaker handles the
immediate risk; this brief is the proper-architecture follow-up. Aim
for the next strategy cycle.
