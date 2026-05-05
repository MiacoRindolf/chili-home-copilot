
# NEXT_TASK: f-time-decay-unit-fix

STATUS: DONE

## Goal

Fix the latent unit-mismatch bug in the live exit engine's time-decay
rule. Currently `live_exit_engine.py:107-111` computes
`(datetime.utcnow() - trade.entry_date).days` and compares to
`exit_config.max_bars` — treating "bars" as wall-clock days regardless
of what timeframe the position actually trades on.

**At a daily timeframe (`1d`) this is correct.** At 1m / 1h / 4h
timeframes, bars-held is wrong by 1440x / 24x / 6x respectively, and
time-decay effectively never fires. The fast-path Coinbase scalper uses
1m bars and would be silently affected.

The canonical adapter inherits the bug because the live adapter at
`live_exit_engine.py:299` passes the wall-clock days value into
`PositionState.bars_held`. Canonical trusts the adapter; the bug isn't
in canonical, but cutover doesn't fix it.

This task wires `ScanPattern.timeframe` into a unit-aware bars-held
computation, hardens the schema with a CHECK constraint on allowed
timeframes, and adds a shared `timeframe_to_seconds` helper that
replaces every ad-hoc parsing of timeframe strings.

## Why now

The audit during the f-exit-parity-persist conversation surfaced this
as a 🚨 latent bug. The fast-path Coinbase scalper (F1-F4 shipped)
operates on 1m bars but has not yet had a position ride long enough to
hit `max_bars=20` in wall-clock days (which would be 28800 minutes =
20 days). When that first position does hit 20 bars (= 20 minutes of
real time) and time-decay should fire, it won't, because legacy will
compute 20-bars-equivalent as 20 days. The position will hold
indefinitely instead.

This is operationally invisible until it bites — and it'll bite the
first 1m position that should time-decay.

## Brain integration / source material

- `app/services/trading/live_exit_engine.py:99-111` — the buggy
  `.days` comparison.
- `app/services/trading/live_exit_engine.py:296-302` — the adapter that
  passes `bars_held=days` into canonical.
- `app/services/trading/exit_evaluator.py::evaluate_bar` — canonical
  reads `state.bars_held` (correct concept), trusts the adapter to
  pass it correctly.
- `app/models/trading.py:820` — `ScanPattern.timeframe`
  (`String(10)`, default `"1d"`). **Already exists, no schema gap.**
- `app/services/trading/exit_evaluator.py::build_config_live` — the
  config builder whose adapter needs the unit-aware fix.
- `app/services/backtest_service.py:1283-1320` — backtest legacy uses
  `self._bars_in_trade` (already a bar count, not days). **No bug
  there**, but the new helper should be reused if anything in backtest
  parses timeframe strings.

## Path

### Step 1 — Add `timeframe_to_seconds` helper

**Search first**: grep for existing timeframe-string parsing logic
(`market_data.py`, `coinbase_ohlcv.py`, `polygon_client.py`). If a
canonical helper already exists, USE IT. Don't duplicate. If only ad
hoc parsing exists, write the new helper at
`app/services/trading/timeframe_utils.py`:

```python
"""Canonical timeframe parsing. Single source of truth for converting
between timeframe strings ('1m', '5m', '1h', '1d', '1w', etc.) and
integer seconds. Used by exit-engine adapters, time-decay computations,
and any code that needs to convert between bar counts and wall-clock
durations.
"""
from __future__ import annotations

# Frozen mapping. Add new entries at the bottom of the existing list;
# downstream callers should NEVER hardcode timeframe strings, they should
# go through this module. The CHECK constraint in mig 226 enforces that
# the DB only stores values present here.
_TIMEFRAME_SECONDS: dict[str, int] = {
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "2h": 7200,
    "4h": 14400,
    "1d": 86400,
    "1w": 604800,
}


def timeframe_to_seconds(tf: str) -> int:
    """Convert a timeframe string to seconds. Raises ValueError on unknown."""
    if tf in _TIMEFRAME_SECONDS:
        return _TIMEFRAME_SECONDS[tf]
    raise ValueError(f"Unknown timeframe: {tf!r}. Allowed: {list(_TIMEFRAME_SECONDS)}")


def known_timeframes() -> list[str]:
    """Return the list of allowed timeframe strings, in ascending duration order."""
    return list(_TIMEFRAME_SECONDS.keys())
```

If the codebase already has a similar helper (likely in
`market_data.py` for OHLCV interval parsing), extend it instead of
creating a parallel one. Surface the choice in the CC report.

### Step 2 — Wire unit-aware bars-held into `live_exit_engine`

In `app/services/trading/live_exit_engine.py`:

**Replace** the legacy computation at lines 105-111:

```python
# OLD (wall-clock days, wrong at intraday)
max_bars = exit_cfg.get("max_bars")
if max_bars and trade.entry_date:
    days_held = (datetime.utcnow() - trade.entry_date).days
    if days_held >= max_bars and result["action"] == "hold":
        result["action"] = "exit_time_decay"
        result["exit_price"] = current_price
        result["days_held"] = days_held
```

**With:**

```python
# NEW — unit-aware bars-held, derived from the position's timeframe.
max_bars = exit_cfg.get("max_bars")
if max_bars and trade.entry_date:
    bars_held = _compute_bars_held(db, trade)
    if bars_held >= max_bars and result["action"] == "hold":
        result["action"] = "exit_time_decay"
        result["exit_price"] = current_price
        result["bars_held"] = bars_held
```

**Add the helper** in the same module:

```python
def _compute_bars_held(db: Session, trade: PaperTrade | Trade) -> int:
    """Return the number of bars elapsed since trade.entry_date, sized to
    the position's pattern timeframe. Falls back to '1d' if no pattern is
    associated (preserves legacy behavior for orphan positions)."""
    from .timeframe_utils import timeframe_to_seconds
    if not trade.entry_date:
        return 0
    tf = "1d"  # backward-compat default
    sp_id = getattr(trade, "scan_pattern_id", None)
    if sp_id:
        try:
            pat = db.query(ScanPattern).filter(ScanPattern.id == sp_id).first()
            if pat and pat.timeframe:
                tf = pat.timeframe
        except Exception:
            pass
    try:
        tf_seconds = timeframe_to_seconds(tf)
    except ValueError:
        tf_seconds = 86400  # unknown timeframe -> treat as daily, log + continue
        logger.warning(
            "[exit_engine] Unknown timeframe '%s' for trade %s; "
            "defaulting to 1d. Add to timeframe_utils._TIMEFRAME_SECONDS.",
            tf, trade.id,
        )
    elapsed_s = (datetime.utcnow() - trade.entry_date).total_seconds()
    return max(0, int(elapsed_s // tf_seconds))
```

### Step 3 — Wire unit-aware bars-held into the canonical adapter

In `app/services/trading/live_exit_engine.py:296-302` (the adapter that
builds `PositionState` for the canonical evaluator):

**Replace:**

```python
bars_held = 0
if trade.entry_date:
    try:
        bars_held = max(0, (datetime.utcnow() - trade.entry_date).days)
    except Exception:
        bars_held = 0
```

**With** (call the same helper):

```python
bars_held = _compute_bars_held(db, trade)
```

This keeps legacy and canonical in sync — both consume the same
timeframe-aware bars-held, so any existing parity row showing
`agree=true` for time-decay decisions stays consistent post-fix.

### Step 4 — Migration `_migration_226_scan_patterns_timeframe_check`

Defensive CHECK constraint on `ScanPattern.timeframe`:

```sql
-- Survey existing values first to ensure no patterns will fail the check.
-- (Operator: run this BEFORE the migration to surface bad rows; if any
-- exist, decide whether to clean them up or extend the allowed list.)
--
--   SELECT timeframe, COUNT(*) FROM scan_patterns GROUP BY timeframe ORDER BY 2 DESC;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'scan_patterns_timeframe_check'
    ) THEN
        ALTER TABLE scan_patterns
            ADD CONSTRAINT scan_patterns_timeframe_check
            CHECK (timeframe IN ('1m', '5m', '15m', '30m', '1h', '2h', '4h', '1d', '1w'));
    END IF;
END $$;
```

Idempotent. Migration ID 226 (verify last is 225 from f-exit-parity-persist).
Run `.\scripts\verify-migration-ids.ps1` ahead of merge.

If the survey turns up `timeframe` values not in the allowed list, the
fix is one of two paths (decide based on what the survey shows):
- **Cleanup migration**: `UPDATE scan_patterns SET timeframe='1d' WHERE timeframe NOT IN (...)` before the CHECK adds. Surface the count in CC report.
- **Extend the allowed list**: add the encountered values to
  `_TIMEFRAME_SECONDS` AND the CHECK list. Only if the encountered
  values are legitimate (e.g., `'2d'`, `'12h'`).

### Step 5 — Tests

Add `tests/test_time_decay_unit_fix.py`:

1. ✅ `timeframe_to_seconds("1d") == 86400`
2. ✅ `timeframe_to_seconds("1m") == 60`
3. ✅ `timeframe_to_seconds("invalid")` raises `ValueError`
4. ✅ `_compute_bars_held` for a 1d-timeframe position 5 days old returns 5
5. ✅ `_compute_bars_held` for a 1m-timeframe position 100 minutes old returns 100
6. ✅ `_compute_bars_held` for a 1h-timeframe position 7200s old returns 2
7. ✅ `_compute_bars_held` for a position with no scan_pattern_id falls back to 1d
8. ✅ `_compute_bars_held` for a position with unknown timeframe logs warning + falls back to 1d
9. ✅ Integration: `compute_live_exit_levels` fires `exit_time_decay` on a 1m-timeframe paper trade after 21 minutes (default max_bars=20)
10. ✅ Existing tests still pass — particularly any that check the
    `result["days_held"]` key (renamed to `bars_held`).

### Step 6 — Smoke verification

After deploy:

1. Survey current ScanPattern timeframe distribution:
   ```sql
   SELECT timeframe, COUNT(*) FROM scan_patterns GROUP BY timeframe ORDER BY 2 DESC;
   ```
   Expect: `1d` dominant; possibly some `1h` / `1m` / etc. Surface in CC report.

2. Find any open paper trade on a non-1d pattern, evaluate it:
   ```sql
   SELECT t.id, sp.timeframe, t.entry_date,
          NOW() - t.entry_date AS elapsed
   FROM paper_trades t
   JOIN scan_patterns sp ON sp.id = t.scan_pattern_id
   WHERE t.status='open' AND sp.timeframe <> '1d'
   LIMIT 5;
   ```
   For one of those, verify `_compute_bars_held` returns a value
   consistent with the timeframe.

3. Confirm logs show `[exit_engine] Unknown timeframe ...` warnings
   ONLY if the survey turned up unexpected values; otherwise zero.

## Constraints / do not touch

- **Default mode stays paper.** No live placement enable.
- **All 8 fast-path safety belts intact.** PROTOCOL Hard Rule 1.
- **No threshold tuning, no strategy-code changes.** The default
  `max_bars=20` stays. We're only fixing what unit "20" is measured in.
- **Do not change the canonical evaluator.** `exit_evaluator.py` reads
  `state.bars_held` and that interface stays.
- **Do not change the backtest path.** `backtest_service.py` already
  uses `self._bars_in_trade` (a real bar count, not wall-clock days)
  — no bug there.
- **Tests use `_test`-suffixed DB.** PROTOCOL Hard Rule 5.
- **Migration ID 226.** Verify last is 225 (from f-exit-parity-persist).
- **No `git push --force`.** PROTOCOL Hard Rule 4.

## Out of scope

- Adding new timeframe values beyond what `_TIMEFRAME_SECONDS`
  enumerates today. Only extend if the survey turns up patterns with
  values like `'12h'` that need to keep functioning.
- Backfilling historical positions' time-decay decisions to "what
  would have been". The fix is forward-only; existing positions get
  evaluated correctly from the next cycle onward.
- Changing the default `max_bars=20` per pattern. That's a strategy
  decision separate from the unit fix.
- Any changes to the parity logger. f-exit-parity-persist already
  shipped that side.

## Success criteria

1. **Migration 226 lands cleanly.** `verify-migration-ids.ps1` passes.
   The CHECK constraint is enforced; survey shows no rejected rows.
2. **`timeframe_to_seconds` helper exists** and is the single source
   of truth for timeframe parsing.
3. **`_compute_bars_held` produces correct results** at 1m, 1h, 1d
   timeframes per the test suite.
4. **`live_exit_engine.compute_live_exit_levels`** uses the unit-aware
   computation in BOTH the legacy time-decay branch AND the canonical
   adapter — so legacy and canonical see the same bars_held.
5. **All 10 new tests pass + existing tests still pass** against
   `chili_test`.
6. **Smoke survey** shows no unknown-timeframe warnings post-deploy
   (or surfaces what was found).
7. **CC report** at `docs/STRATEGY/CC_REPORTS/<date>_f-time-decay-unit-fix.md`
   per PROTOCOL format. Include the timeframe-distribution survey
   results inline.

## Rollback plan

- **Code rollback**: `git revert` the fix commit. Legacy `.days`
  computation returns. The unit-mismatch bug returns. No state-side
  rollback needed.
- **Migration rollback**: drop the CHECK constraint:
  ```sql
  ALTER TABLE scan_patterns DROP CONSTRAINT scan_patterns_timeframe_check;
  ```
  Per PHASE_ROLLBACK_RUNBOOK.
- **No live-broker rollback** — task makes no broker calls.

## Open questions for Cowork (surface in CC report only if relevant)

1. **Existing helper reuse** — if the codebase already has a
   timeframe-string parser (likely in market_data.py or similar),
   surface the location and confirm the new helper extended it
   instead of creating a parallel.
2. **Survey results** — how many distinct `timeframe` values exist in
   `scan_patterns` today? Any unexpected ones? If `'12h'` or similar
   appear, surface the count and the decision (extend the list vs
   migrate them to a known value).
3. **Position-side timeframe metadata** — `Trade` and `PaperTrade` ORMs
   don't have their own `timeframe` field; the fix derives it via
   `scan_pattern_id → ScanPattern.timeframe`. If a position ever has
   `scan_pattern_id IS NULL`, the fallback is `1d`. Is 