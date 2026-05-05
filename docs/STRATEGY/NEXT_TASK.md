# NEXT_TASK: f-exit-parity-persist

STATUS: DONE

## Goal

Make the exit-engine parity logger actually produce evidence. Today it logs
to stdout in shadow mode but persists nothing useful to
`trading_exit_parity_log` — the table has **0 rows total across all sources,
all time** despite millions of decisions evaluated. That blocks the eventual
cutover to `brain_exit_engine_mode=authoritative` because we have no
persisted data to base the decision on.

This task ships **three plumbing fixes plus a methodology hardening** so
that 24-48h post-deploy we can run the verdict query
(`scripts/dispatch-exit-parity-verdict.ps1`) and get a real answer to "are
canonical and legacy producing equivalent close decisions and equivalent
realized P/L?"

This task does NOT flip the authoritative mode. That stays a separate,
explicit operator decision once data exists.

## Why now

Today's parity audit (this conversation) revealed:

1. **Backtest path drops parity rows on the floor.**
   `app/services/backtest_service.py:1446-1462` appends every parity record
   to `strategy._parity_sink`, a Python list attribute on the strategy
   instance. **Nothing else in the codebase reads it** (one reference,
   zero consumers per `Grep`). When the FractionalBacktest run finishes,
   the strategy instance is garbage-collected and the list with it.
2. **Live path uses `db.flush()` not `db.commit()`.**
   `app/services/trading/live_exit_engine.py:354-355` does
   `db.add(row); db.flush()` — flush sends to server-side state but
   doesn't commit. If the calling session ends without `commit()` (e.g.,
   the parent caller's exception path doesn't commit), the row rolls back.
3. **`agree_bool` is computed differently on backtest vs live rows.**
   - Backtest (`backtest_service.py:1439-1442`):
     ```python
     agree = (legacy_action == canonical_action) or (
         legacy_action != "hold" and canonical_action != "hold"
     )
     ```
     "Both engines decided to close" passes regardless of label.
   - Live (`live_exit_engine.py:329`): strict label equality.

   Mixing both definitions in the same column means any aggregate over
   `agree_bool` is methodologically unsound.
4. **Two real concerns from the rule-by-rule audit** (separate doc:
   add as `docs/STRATEGY/CC_REPORTS/<date>_f-exit-parity-persist.md`
   "Audit summary" section):
   - `trail_monotonic=False` in `build_config_backtest` is a deliberate
     parity choice; cutover decision needs to call out whether the
     monotonicity guard turns on at flip time or later.
   - `_resolve_trailing_atr_mult` brain learner is currently writing to
     a StrategyParameter that feeds nothing because canonical's
     `build_config_live` sets `trail_atr_mult=None`. Cutover prep should
     decide whether to wire it into `build_config_live`.

Without persistence, we can't quantify any of the above. Fix the plumbing
first, decide on the substantive concerns once data exists.

## Brain integration / source material

- `app/services/trading/exit_evaluator.py` — canonical evaluator. Pure;
  do not import adapters into it.
- `app/services/trading/live_exit_engine.py:252-380` — live shadow hook,
  parity row write at line 354-355, ops-log emission at 357-380.
- `app/services/backtest_service.py:1370-1495` — backtest shadow hook,
  sink append at 1446-1462, ops-log emission at 1464-1495.
- `app/models/trading.py:1715-1752` — `ExitParityLog` ORM. Note all
  required fields and indexes already exist; no migration needed.
- `app/trading_brain/infrastructure/exit_engine_ops_log.py` —
  `format_exit_engine_ops_line` (used; correct as-is, don't touch).
- `scripts/dispatch-exit-parity-verdict.ps1` — verdict query that will
  run against the data this task produces.
- `app/config.py` — `brain_exit_engine_mode`, `brain_exit_engine_parity_sample_pct`,
  `brain_exit_engine_ops_log_enabled` settings already defined.

## Path

### Step 1 — Drain `_parity_sink` to DB at backtest-run completion

`backtest_service.py` already attaches `_parity_sink` (a list) to the
strategy instance. Add a single drain point at the end of
`FractionalBacktest.run` (or wherever the strategy lifecycle ends — find
the most appropriate hook by reading the file):

```python
# Phase B fix: persist accumulated parity rows. NEVER raises; failures
# log + continue. The legacy decision has already happened; this is
# bookkeeping only.
sink = getattr(strategy, "_parity_sink", None) or []
if sink:
    try:
        from ..models.trading import ExitParityLog
        rows = [
            ExitParityLog(
                source=r["source"],
                position_id=r.get("position_id"),
                scan_pattern_id=r.get("scan_pattern_id"),
                ticker=r.get("ticker") or "",
                bar_ts=None,
                legacy_action=r["legacy_action"],
                legacy_exit_price=r.get("legacy_exit_price"),
                canonical_action=r["canonical_action"],
                canonical_exit_price=r.get("canonical_exit_price"),
                pnl_diff_pct=None,  # computed in Step 4 if both prices present
                agree_bool=bool(r["agree_bool"]),
                mode=r["mode"],
                config_hash=r["config_hash"],
                provenance_json={
                    "reason_code": r.get("reason_code"),
                    "bar_idx": r.get("bar_idx"),
                },
            )
            for r in sink
        ]
        db.bulk_save_objects(rows)
        db.commit()
        logger.info(
            "[exit_parity] persisted %d backtest parity rows", len(rows)
        )
    except Exception as e:
        logger.warning("[exit_parity] sink drain failed: %s", e)
        try:
            db.rollback()
        except Exception:
            pass
```

`bulk_save_objects` is fine here — these rows are append-only audit data,
no relationships to update. **Find the actual `db` Session in scope** —
it likely comes from the brain-worker's batch context. If the strategy
doesn't already see the session, the sink drain should happen at the
backtest call site, not inside the strategy.

### Step 2 — Replace `db.flush()` with explicit commit in the live path

`app/services/trading/live_exit_engine.py:354-355`:

```python
db.add(row)
db.flush()
```

Change to:

```python
db.add(row)
db.commit()
```

**Caveat**: if the caller has a wider transaction in flight,
`commit()` here will close their transaction too. Read the call site
(`compute_live_exit_levels` consumers — `run_exit_engine` etc.) to
verify this is safe. If not, the alternative is a fresh nested session:

```python
from ...db import SessionLocal  # adjust import to project's pattern
with SessionLocal() as parity_db:
    parity_db.add(row)
    parity_db.commit()
```

Pick whichever doesn't disturb the caller's transaction. Surface the
choice in the CC report.

### Step 3 — Add a strict-label parity column

Don't fix the existing `agree_bool` semantic — that's already in the DB
and changing it retroactively would invalidate prior analysis. Instead,
add a **new** column `agree_strict_bool` that's always strict label
equality, and populate it in BOTH paths:

```python
# In both backtest_service.py and live_exit_engine.py:
agree_strict = (legacy_action == canonical_action)
# ... add to row construction:
agree_strict_bool=bool(agree_strict),
```

Migration `_migration_225_exit_parity_strict_agree`:

```sql
ALTER TABLE trading_exit_parity_log
    ADD COLUMN IF NOT EXISTS agree_strict_bool BOOLEAN NULL;
CREATE INDEX IF NOT EXISTS ix_exit_parity_strict_agree_created
    ON trading_exit_parity_log (agree_strict_bool, created_at);
```

NULL on existing rows is fine — they pre-date the strict definition.
Future verdict queries can filter `WHERE agree_strict_bool IS NOT NULL`
to restrict to consistently-defined rows.

### Step 4 — Fill `pnl_diff_pct` when both engines emitted an exit_price

Both `live_exit_engine.py` and `backtest_service.py` row construction
currently leave `pnl_diff_pct=None`. Compute it at row creation when both
prices are present:

```python
if (
    row.canonical_exit_price is not None
    and row.legacy_exit_price is not None
    and row.legacy_exit_price > 0
):
    row.pnl_diff_pct = float(
        (row.canonical_exit_price - row.legacy_exit_price)
        / row.legacy_exit_price
        * 100.0
    )
```

Direction-aware sign: if shorts ever appear, negate for short positions.
For now, long-only is the only case both legacies handle — comment that
in the code.

### Step 5 — Smoke verification

After deploy:

1. Trigger a brain-worker FractionalBacktest cycle (it runs every 5s
   anyway, but `docker compose restart brain-worker` to pick up the new
   code).
2. Wait one full backtest pass (~minutes for 25 tickers).
3. SQL probe:
   ```sql
   SELECT source, COUNT(*) FROM trading_exit_parity_log GROUP BY source;
   ```
   Expect: backtest rows >> 0. If still 0, the sink drain didn't fire
   — debug.
4. Re-run `.\scripts\dispatch-exit-parity-verdict.ps1`. All 6 sections
   should now have meaningful rows.
5. Trigger a live exit decision (open paper position, wait for one bar,
   manually trigger `run_exit_engine`). Verify a `source=live` row
   appears.

### Step 6 — Audit summary in the CC report

The CC report at `docs/STRATEGY/CC_REPORTS/<date>_f-exit-parity-persist.md`
should include the rule-by-rule audit findings from this conversation,
specifically:

- The trail_monotonicity cutover question (legacy backtest's
  `trail_monotonic=False` is intentional parity; cutover decision needs
  to specify whether to flip monotonic at the same time).
- The `_resolve_trailing_atr_mult` brain-learner status (currently
  feeding a parameter that affects nothing because legacy live's trail
  doesn't close; cutover prep must decide whether to wire it into
  `build_config_live` if trail-close gets enabled live).
- The time-decay unit mismatch (legacy live uses days; canonical reads
  bars_held; adapter passes days as bars_held; both legacies share the
  bug at intraday timeframes).
- `partial_profit_eligible` informational flag is dead (no consumers).
  No migration risk.
- The dual `agree_bool` definitions explanation, and how
  `agree_strict_bool` from Step 3 fixes it.

## Constraints / do not touch

- **Do not flip `brain_exit_engine_mode`.** Stays at `shadow`. Cutover is
  an explicit operator decision once 24-48h of data exists.
- **Do not change canonical evaluator semantics.** No edits to
  `exit_evaluator.py` — that file is the source of truth.
- **Do not modify the legacy decision paths.** This task is purely
  additive — better persistence, new strict column. The legacy
  trades-decide flow stays untouched.
- **Do not touch the live-fast-path safety belts.** PROTOCOL Hard Rule 1.
- **No threshold tuning, no strategy-code changes.** This is plumbing.
- **Tests use `_test`-suffixed DB.** PROTOCOL Hard Rule 5.
- Migration ID 225 (verify last is 224). Run
  `.\scripts\verify-migration-ids.ps1` ahead of merge.

## Out of scope

- Flipping `brain_exit_engine_mode=authoritative`. Separate task once
  this task's data accumulates and the verdict query says it's safe.
- Wiring the StrategyParameter resolver into `build_config_live`.
  That's only relevant if cutover also enables trail-close in live, and
  that's a follow-up phase.
- Fixing the time-decay unit mismatch. Real bug but pre-existing in
  legacy — not a parity concern. Separate brief if it becomes
  operationally painful.
- The `trail_monotonic` cutover decision. Surface in the CC report;
  decide at cutover time, not in this task.

## Success criteria

1. **Migration 225 lands cleanly.** `verify-migration-ids.ps1` passes.
   Schema check confirms the new column.
2. **Backtest sink drain works.** After one brain-worker FractionalBacktest
   cycle post-deploy, `SELECT COUNT(*) FROM trading_exit_parity_log
   WHERE source='backtest'` returns > 1000.
3. **Live commit works.** After one live exit-engine evaluation
   post-deploy, `SELECT COUNT(*) FROM trading_exit_parity_log
   WHERE source='live'` returns >= 1.
4. **`agree_strict_bool` populated** on every new row from both paths.
   `SELECT COUNT(*) FROM trading_exit_parity_log
   WHERE agree_strict_bool IS NOT NULL` returns > 0.
5. **`pnl_diff_pct` populated** on rows where both prices are present.
   No NULL `pnl_diff_pct` when both exit_prices are non-null and
   legacy_exit_price > 0.
6. **Verdict query produces meaningful output**
   (`scripts/dispatch-exit-parity-verdict.ps1` shows non-empty result
   sets in all 6 sections).
7. **Existing tests pass.** Run the bracket-related test suite + any
   exit-engine-specific tests against `chili_test`.
8. **CC report at `docs/STRATEGY/CC_REPORTS/<date>_f-exit-parity-persist.md`**
   per PROTOCOL format, including the rule-by-rule audit summary in
   Step 6.

## Rollback plan

- **Code rollback**: `git revert` of the fix commits returns the parity
  logger to its current "logging into the void" state. No data loss
  beyond what wasn't being captured anyway.
- **Migration rollback**: `agree_strict_bool` is NULL-able — dropping
  the column is a one-line ALTER if needed:
  ```sql
  ALTER TABLE trading_exit_parity_log DROP COLUMN agree_strict_bool;
  ```
  Per PHASE_ROLLBACK_RUNBOOK.
- **No live-broker rollback** — task makes no broker calls.

## Open questions for Cowork (surface in CC report only if relevant)

1. **Live commit semantics** (Step 2 caveat). If `db.commit()` in
   `_phase_b_shadow_parity` would prematurely close the caller's
   transaction, surface and use the nested-session approach. Either
   choice is fine; just pick one explicitly.
2. **Backtest sink drain location.** If the strategy instance doesn't
   have a session in scope, the drain might need to live at the
   `FractionalBacktest.run` caller. Surface the chosen call site.
3. **Time-decay unit bug** — surface as a watch item if any 1m/1h/intraday
   exits are observed in the post-fix data. Should not happen given
   current usage patterns but worth flagging if it does.
4. **Verdict thresholds for cutover** — once data exists, what's the
   right "OK to flip" criterion? Suggested defaults:
   - `agree_strict_pct >= 99.0%` over 24h on both `source=live` AND
     `source=backtest`
   - `total_pnl_diff_pct` mean within ±0.5% (essentially zero)
   - `pnl_diff_pct` stddev < 1.0 (tight distribution around zero)
   - At least 1000 live-source rows AND 100,000 backtest-source rows

   Actual thresholds belong in the cutover task brief, not this one.
   But surface if the post-fix data already meets them — that
   accelerates the cutover decision.
