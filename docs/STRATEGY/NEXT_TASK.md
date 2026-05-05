# NEXT_TASK: f-partial-profit-wire-up

STATUS: DONE

## Goal

Make partial-profit-taking at 1R actually work. Today the canonical
exit evaluator emits `EXIT_ACTION_PARTIAL` correctly when configured
for it, but **no consumer in the codebase acts on the action** — it
falls through to "hold" because `run_exit_engine` and the live broker
adapters only handle "exit_X" terminal actions.

Legacy live's `partial_profit_eligible` flag (`live_exit_engine.py:99-103`)
was always aspirational — set but never read by any consumer. So the
feature has been "implemented in canonical" but completely inert in
production.

This task wires the missing consumer: when the canonical evaluator
emits `action="partial"`, submit a partial broker close (default 50%
of position size), record the partial fill, mark `partial_taken=True`
on the trade so it doesn't re-fire, and emit a `[partial_profit_ops]`
audit log line.

After this ships, partial-profit-taking is a real operational primitive
that pattern-level config (`exit_config.partial_at_1r`) can opt into,
and the brain can learn from realized partial-vs-full exits.

This task does NOT enable `partial_at_1r=True` on any pattern by
default. The opt-in stays per-pattern, decided by the operator or by
a future brain-learner.

## Why now

You said "yes I want it" when I explained today that the partial-at-1R
feature is half-built — eligibility detection lives in legacy, action
emission lives in canonical, and the broker-side handler that would
turn the action into a real partial close is missing.

The operational case for partial-profit-taking:

- **Locks in some realized P/L when the position has earned 1R.** The
  remaining position keeps running for trail/target/BOS while the
  taken half is no longer at risk of giving back.
- **Reduces drawdown variance.** A trade that partials at 1R then
  trails out flat realizes +0.5R; a trade that doesn't partial and
  trails out flat realizes 0R. The partial reduces variance without
  much expected-value cost.
- **Matches what professional discretionary traders do.** "Take half
  off at 1R" is one of the most common position-management primitives
  in trading literature; the brain should be able to test it.

Without the consumer, `partial_at_1r=True` on a pattern would mean
"emit a partial action and have it silently ignored." That's worse
than not having the feature, because it gives the false impression
the brain is partialing when it isn't. **Wiring the consumer is what
turns the feature from aspirational to operational.**

## Brain integration / source material

- `app/services/trading/exit_evaluator.py:357-369` — canonical's
  `EXIT_ACTION_PARTIAL` emission. Already correct; do NOT modify.
- `app/services/trading/exit_evaluator.py:411` — `build_config_live`
  reads `partial_at_1r` from the exit_config dict. Already correct.
- `app/services/trading/live_exit_engine.py:99-103` — legacy's dead
  `partial_profit_eligible` flag. **Delete** as part of this task
  since the canonical action replaces it. (Confirmed via Grep:
  flag is set in 1 place, read in 0 places.)
- `app/services/trading/live_exit_engine.py:217-249` —
  `run_exit_engine` consumer loop. **The consumer wiring lives here.**
- `app/services/broker_service.py` — find the existing
  `place_partial_close` / `partial_close` function if any; if not,
  use the existing `place_sell` / `place_market_sell` and pass the
  partial quantity. Search first.
- `app/models/trading.py` — `Trade` and `PaperTrade` ORMs. Need to
  add `partial_taken` (boolean) + `partial_taken_at` (timestamp) +
  `partial_taken_qty` (float) + `partial_taken_price` (float). Also
  add a `partial_taken` field to the canonical `PositionState`
  dataclass at `exit_evaluator.py:99-117`. **Already exists** at line
  116 (`partial_taken: bool = False`). The brain just needs to
  carry it forward via `updated_state` (it already does).
- `app/services/trading/auto_trader.py` — if it reads
  `partial_profit_eligible` anywhere (it shouldn't, but verify),
  remove the dead read.
- `docs/PHASE_ROLLBACK_RUNBOOK.md` — Phase rollback shape.

## Path

### Step 1 — Migration `_migration_NNN_partial_taken_columns`

Add the persistence columns to both Trade and PaperTrade tables. Use
the next sequential migration ID at execution time (likely 226 if
f-exit-parity-persist's 225 just shipped).

```sql
ALTER TABLE trading_trades
    ADD COLUMN IF NOT EXISTS partial_taken         BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS partial_taken_at      TIMESTAMP NULL,
    ADD COLUMN IF NOT EXISTS partial_taken_qty     DOUBLE PRECISION NULL,
    ADD COLUMN IF NOT EXISTS partial_taken_price   DOUBLE PRECISION NULL;

ALTER TABLE paper_trades
    ADD COLUMN IF NOT EXISTS partial_taken         BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS partial_taken_at      TIMESTAMP NULL,
    ADD COLUMN IF NOT EXISTS partial_taken_qty     DOUBLE PRECISION NULL,
    ADD COLUMN IF NOT EXISTS partial_taken_price   DOUBLE PRECISION NULL;

CREATE INDEX IF NOT EXISTS ix_trading_trades_partial_taken
    ON trading_trades (partial_taken)
    WHERE partial_taken = TRUE;
CREATE INDEX IF NOT EXISTS ix_paper_trades_partial_taken
    ON paper_trades (partial_taken)
    WHERE partial_taken = TRUE;
```

Idempotent (`IF NOT EXISTS`). The partial indexes are sparse — most
positions never partial, so a partial index on `WHERE partial_taken
= TRUE` keeps the index small.

Verify migration ID with `.\scripts\verify-migration-ids.ps1`.

### Step 2 — Update ORMs

In `app/models/trading.py`, add the four new fields to both `Trade`
(near line 39-188) and `PaperTrade` (near line 1022-1048):

```python
partial_taken: bool = Column(Boolean, nullable=False, default=False)
partial_taken_at: Optional[datetime] = Column(DateTime, nullable=True)
partial_taken_qty: Optional[float] = Column(Float, nullable=True)
partial_taken_price: Optional[float] = Column(Float, nullable=True)
```

Match column ordering to the migration. The default value at the ORM
layer is required because new code may construct `Trade(...)` without
explicitly passing it.

### Step 3 — Determine partial-close fraction

The partial fraction (e.g., 50%, 33%, 25%) should be configurable per
pattern, not hardcoded. Decide where it lives:

- **Option A: New pattern-level field** `partial_close_fraction` on
  `ScanPattern` — most flexible, requires another migration.
- **Option B: Read from `exit_config.partial_close_fraction`** — uses
  the existing JSONB exit_config, no new column.
- **Option C: Hardcode 0.5 (50%) at first**, surface as a follow-up if
  the brain shows the fraction matters.

**Recommendation: Option B.** The exit_config dict already houses
related parameters (`partial_at_1r`, `bos_buffer_pct`, etc.). Add
`partial_close_fraction` as another key with default 0.5:

```python
# In _load_exit_config in live_exit_engine.py, add to defaults:
"partial_close_fraction": 0.5,
```

Caller reads it from the config alongside `partial_at_1r`.

### Step 4 — Wire the consumer in `run_exit_engine`

In `app/services/trading/live_exit_engine.py:217-249`, the consumer
loop currently filters `result["action"] != "hold"` and treats every
non-hold action as a terminal close. **This is the bug for partial:**
"partial" is non-hold but non-terminal.

Refactor:

```python
def run_exit_engine(db: Session, user_id: int | None = None) -> dict[str, Any]:
    """Evaluate all open positions through the exit engine. Returns action recommendations."""
    from .market_data import fetch_quote

    open_paper = db.query(PaperTrade).filter(PaperTrade.status == "open")
    if user_id is not None:
        open_paper = open_paper.filter(PaperTrade.user_id == user_id)
    positions = open_paper.all()

    results = []
    partial_actions = []   # NEW
    terminal_actions = []  # NEW
    for pos in positions:
        try:
            q = fetch_quote(pos.ticker)
            if not q or not q.get("price"):
                continue
            price = float(q["price"])
            exit_rec = compute_live_exit_levels(db, pos, price)
            exit_rec["ticker"] = pos.ticker
            exit_rec["position_id"] = pos.id
            exit_rec["current_price"] = price
            results.append(exit_rec)
            action = exit_rec.get("action", "hold")
            if action == "partial":
                partial_actions.append(exit_rec)  # NEW: separate bucket
            elif action != "hold":
                terminal_actions.append(exit_rec)
        except Exception as e:
            logger.debug("[exit_engine] Error evaluating %s: %s", pos.ticker, e)

    logger.info(
        "[exit_engine] Evaluated %d positions: %d terminal + %d partial actions recommended",
        len(results), len(terminal_actions), len(partial_actions),
    )

    return {
        "ok": True,
        "evaluated": len(results),
        "actions": terminal_actions,                # backward-compat: "actions" still means terminal
        "partial_actions": partial_actions,         # NEW: separate key for partial
        "all": results,
    }
```

The downstream consumer of `run_exit_engine`'s return dict (whoever
calls it for paper-mode auto-management) needs to handle
`partial_actions` separately from `actions`. Search and update those
call sites.

### Step 5 — Add the canonical partial path to `compute_live_exit_levels`

Today, `compute_live_exit_levels` only emits `result["action"]` for
hard stop / hard target / BOS / time decay. The canonical evaluator
inside `_phase_b_shadow_parity` correctly emits `partial`, but in
shadow mode the canonical decision doesn't influence `result`.

For the consumer wiring to work, **`compute_live_exit_levels` must
emit `result["action"] = "partial"`** when partial-at-1R fires AND
the position hasn't already partialed. The cleanest path:

```python
# Replace the dead partial_profit_eligible block at lines 99-103:
#   if risk > 0 and exit_cfg.get("partial_at_1r", False):
#       r_move = ...
#       if r_move >= 1.0:
#           result["partial_profit_eligible"] = True
#           result["r_multiple"] = round(r_move, 2)
# With:

if (
    risk > 0
    and exit_cfg.get("partial_at_1r", False)
    and not getattr(trade, "partial_taken", False)
    and result["action"] == "hold"  # don't override a real terminal exit
):
    r_move = (current_price - entry) / risk if is_long else (entry - current_price) / risk
    if r_move >= 1.0:
        result["action"] = "partial"
        result["exit_price"] = current_price
        result["r_multiple"] = round(r_move, 2)
        result["partial_close_fraction"] = float(exit_cfg.get("partial_close_fraction", 0.5))
```

Priority discipline: partial fires only when the position would
otherwise hold. If a hard stop / hard target / BOS / time decay would
fire on the same bar, those terminal closes take precedence — partial
is suppressed because the whole position is closing anyway.

### Step 6 — Wire the broker-side partial close

Search broker_service.py for an existing partial-close primitive.
Likely candidates:

- `broker_service.place_market_sell(ticker, quantity)` —
  generic market sell; pass the partial quantity.
- `broker_service.place_crypto_sell(ticker, quantity)` — crypto
  variant.
- `broker_service.place_partial_close(...)` — if it exists, use it.

If only a "close entire position" primitive exists, create
`place_partial_close(trade, fraction)`:

```python
def place_partial_close(trade: Trade | PaperTrade, fraction: float) -> dict[str, Any]:
    """Submit a partial sell for `fraction` of the position's quantity.

    Updates partial_taken bookkeeping on success. Failure modes log + return
    {"error": "..."} dict; never raises into the caller (consistent with
    other broker_service helpers).
    """
    if trade.partial_taken:
        return {"error": "already_partialed"}
    if not (0.0 < fraction < 1.0):
        return {"error": f"invalid_fraction:{fraction}"}

    qty = trade.quantity * fraction
    # ... route to the right broker per trade.broker_source ...
    # ... record partial_taken_qty, partial_taken_price, partial_taken_at on success ...
```

For paper-mode positions, the partial close updates the PaperTrade's
quantity and credits the partial proceeds to the paper-balance ledger.

### Step 7 — Auto-trader integration

Find the consumer of `run_exit_engine` in the autotrader / paper-runner
path. Add handling of `partial_actions`:

```python
exit_result = run_exit_engine(db)
for partial_rec in exit_result.get("partial_actions", []):
    pos = db.query(PaperTrade).get(partial_rec["position_id"])
    if pos is None or pos.partial_taken:
        continue
    fraction = float(partial_rec.get("partial_close_fraction", 0.5))
    outcome = broker_service.place_partial_close(pos, fraction)
    if outcome.get("ok"):
        logger.info(
            "[partial_profit_ops] position_id=%s ticker=%s "
            "fraction=%.2f r_multiple=%s qty=%.4f price=%.4f",
            pos.id, pos.ticker, fraction,
            partial_rec.get("r_multiple"),
            outcome.get("quantity"), outcome.get("price"),
        )
    else:
        logger.warning(
            "[partial_profit_ops] FAILED position_id=%s reason=%s",
            pos.id, outcome.get("error"),
        )

for terminal_rec in exit_result.get("actions", []):
    # existing terminal-close handling stays unchanged
    ...
```

The `[partial_profit_ops]` log prefix matches the project's existing
conventions for ops log lines (`[exit_engine_ops]`,
`[bracket_writer_g2]`, etc.).

### Step 8 — Tests

Add `tests/test_partial_profit_wire_up.py`:

1. ✅ `compute_live_exit_levels` returns `action="partial"` when:
   `partial_at_1r=True`, position has reached 1R, `partial_taken=False`,
   and no terminal exit would fire.
2. ✅ `compute_live_exit_levels` returns `action="hold"` (not partial)
   when `partial_at_1r=False`.
3. ✅ `compute_live_exit_levels` returns the terminal action (not
   partial) when both partial AND a terminal rule would fire on the
   same bar.
4. ✅ `compute_live_exit_levels` returns `action="hold"` (not partial
   re-fire) when `partial_taken=True`.
5. ✅ `run_exit_engine` separates partial actions from terminal actions
   in the return dict.
6. ✅ `place_partial_close` updates `partial_taken=True`, populates
   the four bookkeeping fields, and reduces position quantity by the
   fraction.
7. ✅ `place_partial_close` returns `{"error": "already_partialed"}`
   when called on a trade with `partial_taken=True`.
8. ✅ `place_partial_close` returns
   `{"error": "invalid_fraction:..."}` for fraction outside (0, 1).
9. ✅ Auto-trader integration: synthetic 1R-hit paper trade flows
   through the full path and ends up with `partial_taken=True` and a
   reduced quantity.
10. ✅ The existing `partial_profit_eligible` removal does not break
    any existing test (search for tests that asserted that key).

### Step 9 — Smoke verification

After deploy:

1. Find or create a paper trade where `partial_at_1r=True` (set on
   one of the patterns explicitly for the smoke):
   ```sql
   UPDATE scan_patterns
       SET exit_config = jsonb_set(
           coalesce(exit_config, '{}'::jsonb),
           '{partial_at_1r}', 'true'::jsonb
       )
       WHERE id = <chosen_pattern_id>;
   ```
2. Wait for a position on that pattern to reach 1R.
3. Verify in logs: `[partial_profit_ops] position_id=... fraction=0.50 ...`
4. Confirm via SQL:
   ```sql
   SELECT id, ticker, quantity, partial_taken, partial_taken_qty,
          partial_taken_price, partial_taken_at
   FROM paper_trades WHERE partial_taken = TRUE LIMIT 5;
   ```
   Expect: at least one row with all four fields populated.
5. Confirm the position keeps running — `status='open'`, partial_taken
   trade should NOT re-emit `action="partial"` on subsequent bars.

## Constraints / do not touch

- **Default mode stays paper.** No live placement. Partial-close in
  paper mode = paper-ledger update. Live-mode partial close lives
  behind the existing 8 fast-path safety belts (PROTOCOL Hard Rule 1).
- **Do not enable `partial_at_1r=True` on any pattern by default.**
  Opt-in stays per-pattern. The smoke verification step above sets
  it on ONE pattern manually for testing; revert before close.
- **Do not modify the canonical evaluator.** `exit_evaluator.py` is
  source of truth and already does the right thing. This task wires
  the missing consumer, not the producer.
- **Do not change the priority order.** Canonical's
  stop > target > BOS > time_decay > trail > partial priority is
  correct. Partial is non-terminal and only fires when nothing
  terminal would.
- **Tests use `_test`-suffixed DB.** PROTOCOL Hard Rule 5.
- **Migration ID** = next sequential at execution time. Verify with
  `.\scripts\verify-migration-ids.ps1`.

## Out of scope

- Brain-learner that decides which patterns benefit from
  `partial_at_1r=True`. Once partial fires for real and the brain
  has realized partial-vs-full data, a separate brief can wire
  pattern-level adaptive selection.
- Multiple partials per trade (e.g., 33% at 1R then 33% at 2R).
  Single partial is enough surface area for the first version. Add
  if and when the data shows it's worth it.
- The time-decay unit-mismatch fix (queued separately at
  `docs/STRATEGY/QUEUED/f-time-decay-unit-fix.md`).
- The sophisticated parity metric (queued at
  `docs/STRATEGY/QUEUED/f-exit-parity-metric-v2.md`).
- Live-mode (real broker) partial closes. The wiring should work
  for live too, but the smoke and tests should stay in paper. Live
  activation needs the existing fast-path safety-belt review.

## Success criteria

1. **Migration lands cleanly.** `verify-migration-ids.ps1` passes.
   Schema check confirms the four new columns on both tables.
2. **`compute_live_exit_levels` emits `action="partial"`** under the
   four conditions (partial_at_1r=True, 1R reached, not yet
   partialed, no terminal exit pending) — verified by tests.
3. **`run_exit_engine`** returns separate `actions` (terminal) and
   `partial_actions` (non-terminal) buckets.
4. **`place_partial_close`** updates `partial_taken` and the three
   bookkeeping fields atomically.
5. **Auto-trader handles `partial_actions`** and emits
   `[partial_profit_ops]` log lines.
6. **All 10 new tests pass + existing exit-engine tests still pass**
   against `chili_test`.
7. **Smoke verification** shows at least one real partial fire in
   paper mode with all bookkeeping populated.
8. **CC report** at
   `docs/STRATEGY/CC_REPORTS/<date>_f-partial-profit-wire-up.md` per
   PROTOCOL format. Include the smoke output inline.

## Rollback plan

- **Code rollback**: `git revert` the consumer-wiring commits.
  `compute_live_exit_levels` reverts to never emitting `partial`,
  `run_exit_engine` reverts to one-bucket return, the dead
  `partial_profit_eligible` flag stays gone but harmless. No
  partial-close attempts are made.
- **Data rollback**: existing positions with `partial_taken=TRUE`
  stay correctly marked — they really did partial. The four
  bookkeeping fields keep their values for audit. If the broker
  rolls back the partial fill on its side (unlikely), reconcile
  via existing broker-sync; not a code-rollback concern.
- **Migration rollback**:
  ```sql
  ALTER TABLE trading_trades
      DROP COLUMN partial_taken,
      DROP COLUMN partial_taken_at,
      DROP COLUMN partial_taken_qty,
      DROP COLUMN partial_taken_price;
  ALTER TABLE paper_trades
      DROP COLUMN partial_taken,
      DROP COLUMN partial_taken_at,
      DROP COLUMN partial_taken_qty,
      DROP COLUMN partial_taken_price;
  ```
  Per PHASE_ROLLBACK_RUNBOOK.

## Open questions for Cowork (surface in CC report only if relevant)

1. **`partial_close_fraction` location** — Option B (in
   `exit_config` JSONB) is recommended, but if the codebase has
   established a different config-knob pattern for trade-level
   parameters, surface the alternative. Goal is single source of
   truth — don't create a parallel config surface.
2. **Existing `place_partial_close` or equivalent** — surface
   whether a pre-existing broker primitive for partial closes
   exists. If yes, use it; if no, document the new helper.
3. **Auto-trader call site** — surface where `run_exit_engine`'s
   output is currently consumed, and confirm the partial_actions
   bucket is handled in the right place (likely the paper-runner
   loop in scheduler-worker).
4. **Live-mode safety review** — does enabling
   `partial_at_1r` on a live-mode pattern require a separate
   safety-belt review? My read: no — partial close is a SELL action
   on an open position, which is allowed under existing belts.
   Surface for explicit confirmation.
5. **Single-fire enforcement** — currently `partial_taken` is a
   bool, not a counter. If a pattern ever wanted multiple partials
   (33% at 1R + 33% at 2R), the schema needs a counter or a
   separate `trading_partial_fills` table. Out of scope here, but
   surface if the data suggests it's worth doing.
