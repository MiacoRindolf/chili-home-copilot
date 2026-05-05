# NEXT_TASK: f-evidence-canonical-writer

STATUS: DONE

## Goal

Fix `update_pattern_stats_from_closed_trades` (`app/services/trading/learning.py:4798-4895`)
in place so it becomes the **canonical-aware** writer of
`ScanPattern.{win_rate, avg_return_pct, trade_count}`. Today it
aggregates raw `Trade.exit_price - entry_price` from closed trades
without any time-decay-correctness check; positions held past their
intended `max_bars` (= 81% of patterns per the f-time-decay-unit-fix
survey) leak their too-late exit prices into the pattern's evidence.

After this ships, every learning-cycle invocation (every 5s in
brain-worker) re-derives evidence using counterfactual exit prices
where overheld, writes an audit row to a new
`pattern_evidence_corrections` table, and the existing realized-EV
gate (`app/services/trading/realized_ev_gate.py`) demotes any
pattern whose corrected stats fail the gate. **The 5s lean-cycle is
the cadence.** No separate reconciler service. No new scheduler job.
No writer conflict — this IS the writer that today contaminates
the field, fixed in place.

The first invocation post-deploy IS the historical backfill —
every pattern's evidence gets recomputed from canonical semantics
and the audit table records the before/after.

## Why now

Pre-execution audit (this conversation) confirmed:

- **`update_pattern_stats_from_closed_trades` writes
  `pattern.win_rate` and `pattern.avg_return_pct` directly from
  realized `Trade.exit_price`** at lines 4870-4877. No
  counterfactual correction. No time-decay awareness.
- It runs every learning cycle (called from `learning.py:9582`,
  inside the lean-cycle that brain-worker runs at `--interval 5`).
- The other 7 writer sites in `learning.py` either target
  `TradingInsight` (different table, not read by realized-EV gate)
  or fire on rare events (new pattern mining via
  `ensure_mined_scan_pattern`). The conflict matrix is:

| Site | Field | Run frequency | Conflicts? |
|---|---|---|---|
| 950 | `TradingInsight.evidence_count` | per close | wrong table |
| 2020 | `ScanPattern.evidence_count` | per new mined pattern | rare; additive |
| 2037-2039 | `ScanPattern.{win_rate, avg_return_pct, evidence_count}` | per existing mined pattern | rare; from external args |
| 3144 | `TradingInsight.evidence_count` | per active-seeking | wrong table |
| 4715-4717 | `ScanPattern.{win_rate, avg_return_pct, trade_count}` | per learning cycle | overwritten by 4870-4877 |
| 4772 | `TradingInsight.evidence_count` | per learning cycle | wrong table |
| **4870-4877** | **`ScanPattern.{win_rate, avg_return_pct, trade_count}`** | **per learning cycle (5s)** | **the load-bearing writer** |

- Site 4870-4877 overwrites site 4715-4717 within the same cycle
  (both run inside `run_learning_cycle`; the closed-trade path runs
  AFTER the breakout-alert path at line 9582). For patterns with
  any closed trades in the last 180 days, the breakout-outcome
  values are immediately overwritten by the closed-trade values.
  **Fixing 4870-4877 is therefore sufficient to fix the canonical
  evidence pipeline.** Sites 4715-4717 remain a write that lives for
  microseconds for patterns that have closed trades; for patterns
  with no closed trades (rare in the active set), the breakout
  values stand — those are honest realized alert outcomes that
  don't have a time-decay bug because alerts don't hold positions.

- The realized-EV gate (`evaluate_realized_ev`) reads `win_rate`
  and `avg_return_pct` post-write. After this fix, those reads see
  canonical-corrected values, and patterns whose corrected stats
  fail the gate (`avg_return_pct > 0`, `win_rate > 0`,
  `trade_count >= chili_realized_ev_min_trades`) are demoted on the
  next promotion-cycle pass — no new gate code, no new threshold.

## Scope boundary

**In scope:**
- Refactor `update_pattern_stats_from_closed_trades` to use canonical
  time-decay semantics + counterfactual exit prices.
- New audit table `pattern_evidence_corrections` (mig 228).
- New helper module `app/services/trading/evidence_correction.py`
  with the counterfactual computation (testable in isolation; the
  `learning.py` function calls into it).
- Tests covering the corrected aggregation, the audit-row
  invariants, and the writer-conflict idempotence.
- First-run backfill via the existing learning-cycle invocation —
  no separate one-time script.

**Out of scope:**
- The other 6 writer sites in `learning.py`. Sites 4715-4717 are
  overwritten anyway; site 2020 / 2037-2039 / TradingInsight sites
  are out of scope. Touch only the load-bearing site.
- A separate `evidence_reconciler` service or scheduler-worker job.
  The 5s lean-cycle is the cadence.
- Re-running pattern-aware backtests under canonical exit
  semantics. Backtest-derived evidence is a separate surface that
  becomes correctable once f-exit-parity-metric-v2's cutover lands
  (queued).
- Modifying `realized_ev_gate.py` or `promotion_gate.py`. Both
  consume the corrected fields; the auto-demote falls out for free.
- Modifying the canonical evaluator (`exit_evaluator.py`).
- Modifying `Trade` / `PaperTrade` row data. Counterfactual is a
  derived value at evidence time, not a row mutation.
- Position-side timeframe metadata (Trade/PaperTrade.timeframe
  column). Helper reads via `scan_pattern_id → ScanPattern.timeframe`
  per the f-time-decay-unit-fix shape.
- LLM-context (`position_plan_generator`) pattern-evidence path.
  That uses ScanPattern fields too, so it benefits transparently
  once this fix lands.

## Brain integration / source material

- `app/services/trading/learning.py:4798-4895` —
  `update_pattern_stats_from_closed_trades`. The refactor target.
- `app/services/trading/learning.py:9582` — the call site inside
  `run_learning_cycle`. Stays unchanged.
- `app/services/trading/timeframe_utils.py::timeframe_to_seconds` —
  unit-conversion source of truth (mig 227, just shipped).
- `app/services/trading/realized_ev_gate.py:66` —
  `evaluate_realized_ev`. Stays as the canonical EV gate; this fix
  corrects its inputs.
- `app/services/trading/market_data.py::fetch_ohlcv_df` —
  multi-provider OHLCV. Use this for counterfactual price reads;
  respects yf circuit breaker.
- `app/models/trading.py` — `Trade`, `PaperTrade`, `ScanPattern`
  ORMs. Confirm field names: ScanPattern has `win_rate`,
  `avg_return_pct`, `trade_count` (NOT `evidence_count` for the
  realized-EV-gate path; verify by reading the ORM and matching
  what the gate actually reads).
- `app/migrations.py` — last migration ID is 227 (CHECK on
  scan_patterns.timeframe). This task adds 228.

## Path

### Step 1 — Migration `_migration_228_pattern_evidence_corrections`

```sql
CREATE TABLE IF NOT EXISTS pattern_evidence_corrections (
    id                                BIGSERIAL PRIMARY KEY,
    scan_pattern_id                   INTEGER NOT NULL REFERENCES scan_patterns(id) ON DELETE CASCADE,
    cycle_run_id                      UUID NOT NULL,
    before_win_rate                   DOUBLE PRECISION NULL,
    after_win_rate                    DOUBLE PRECISION NULL,
    before_avg_return_pct             DOUBLE PRECISION NULL,
    after_avg_return_pct              DOUBLE PRECISION NULL,
    before_trade_count                INTEGER NULL,
    after_trade_count                 INTEGER NULL,
    closed_trades_considered          INTEGER NOT NULL,
    overheld_trade_count              INTEGER NOT NULL,
    counterfactual_applied_count      INTEGER NOT NULL,
    counterfactual_unavailable_count  INTEGER NOT NULL,
    correction_reason                 VARCHAR(64) NOT NULL,
        -- 'first_run_backfill', 'periodic_recompute',
        -- 'no_change' (when before == after, audit completeness),
        -- 'coverage_too_thin' (when CF gap > threshold; skipped update)
    created_at                        TIMESTAMP NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_pattern_evidence_corrections_pattern_created
    ON pattern_evidence_corrections (scan_pattern_id, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_pattern_evidence_corrections_cycle
    ON pattern_evidence_corrections (cycle_run_id);
CREATE INDEX IF NOT EXISTS ix_pattern_evidence_corrections_reason_created
    ON pattern_evidence_corrections (correction_reason, created_at DESC);
```

Migration ID 228 (verify with `verify-migration-ids.ps1`). Idempotent.

### Step 2 — Helper module

New `app/services/trading/evidence_correction.py`:

```python
"""Canonical-aware evidence correction for ScanPattern win_rate /
avg_return_pct / trade_count, computed from closed Trade rows using
canonical time-decay semantics. Used by
``learning.update_pattern_stats_from_closed_trades`` (the load-bearing
writer) every learning cycle.

Pure functions; no DB writes, no scheduler dependency. The caller is
responsible for persisting outputs and writing audit rows.
"""
from __future__ import annotations
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TradeCorrection:
    """Per-trade correction outcome."""
    realized_return_pct: float
    corrected_return_pct: float
    overheld: bool
    counterfactual_available: bool
    realized_won: bool
    corrected_won: bool


@dataclass(frozen=True)
class PatternStats:
    """Aggregate stats over a pattern's closed trades."""
    n: int
    win_rate: float       # in [0, 1]
    avg_return_pct: float # mean of trade returns × 100
    overheld_n: int
    counterfactual_applied_n: int
    counterfactual_unavailable_n: int


def compute_trade_correction(
    *, entry_price: float, exit_price: float, entry_date: datetime,
    close_date: datetime, direction: str, ticker: str,
    pattern_timeframe: str, max_bars: int,
) -> TradeCorrection:
    """Per-trade canonical-aware correction. Pure; no DB."""
    from .timeframe_utils import timeframe_to_seconds

    sign = 1.0 if direction == "long" else -1.0
    realized_pct = sign * (float(exit_price) - float(entry_price)) / float(entry_price) * 100.0
    realized_won = realized_pct > 0

    tf_seconds = timeframe_to_seconds(pattern_timeframe)
    held_seconds = (close_date - entry_date).total_seconds()
    held_bars = held_seconds / tf_seconds

    if held_bars <= max_bars:
        return TradeCorrection(
            realized_return_pct=realized_pct,
            corrected_return_pct=realized_pct,
            overheld=False,
            counterfactual_available=True,  # No correction needed
            realized_won=realized_won,
            corrected_won=realized_won,
        )

    cf_price = _fetch_counterfactual_close(
        ticker, entry_date, pattern_timeframe, max_bars,
    )
    if cf_price is None:
        # Coverage gap — fall back to realized to avoid biasing
        # the sample by dropping these trades. Caller increments
        # counterfactual_unavailable_count.
        return TradeCorrection(
            realized_return_pct=realized_pct,
            corrected_return_pct=realized_pct,
            overheld=True,
            counterfactual_available=False,
            realized_won=realized_won,
            corrected_won=realized_won,
        )

    cf_pct = sign * (cf_price - float(entry_price)) / float(entry_price) * 100.0
    return TradeCorrection(
        realized_return_pct=realized_pct,
        corrected_return_pct=cf_pct,
        overheld=True,
        counterfactual_available=True,
        realized_won=realized_won,
        corrected_won=cf_pct > 0,
    )


def aggregate_pattern_stats(corrections: list[TradeCorrection]) -> PatternStats:
    """Aggregate per-trade corrections into the three ScanPattern fields."""
    n = len(corrections)
    if n == 0:
        return PatternStats(
            n=0, win_rate=0.0, avg_return_pct=0.0,
            overheld_n=0, counterfactual_applied_n=0,
            counterfactual_unavailable_n=0,
        )

    wins = sum(1 for c in corrections if c.corrected_won)
    win_rate = wins / n
    avg_return_pct = sum(c.corrected_return_pct for c in corrections) / n
    overheld_n = sum(1 for c in corrections if c.overheld)
    cf_applied = sum(
        1 for c in corrections if c.overheld and c.counterfactual_available
    )
    cf_unavail = sum(
        1 for c in corrections if c.overheld and not c.counterfactual_available
    )

    return PatternStats(
        n=n, win_rate=win_rate, avg_return_pct=avg_return_pct,
        overheld_n=overheld_n, counterfactual_applied_n=cf_applied,
        counterfactual_unavailable_n=cf_unavail,
    )


def _fetch_counterfactual_close(
    ticker: str, entry_date: datetime,
    pattern_timeframe: str, max_bars: int,
) -> float | None:
    """Fetch OHLCV at pattern_timeframe; return close price of bar at
    index max_bars from entry_date. Returns None if unavailable."""
    from .market_data import fetch_ohlcv_df
    from .timeframe_utils import timeframe_to_seconds

    tf_seconds = timeframe_to_seconds(pattern_timeframe)
    period = _period_for_timeframe(pattern_timeframe, max_bars)

    try:
        df = fetch_ohlcv_df(ticker, period=period, interval=pattern_timeframe)
    except Exception as e:
        logger.debug(
            "[evidence_correction] OHLCV fetch failed for %s @ %s: %s",
            ticker, pattern_timeframe, e,
        )
        return None

    if df is None or df.empty:
        return None

    from datetime import timedelta
    target_ts = entry_date + timedelta(seconds=max_bars * tf_seconds)
    try:
        idx = df.index.searchsorted(target_ts, side="right") - 1
    except Exception:
        return None
    if idx < 0 or idx >= len(df):
        return None
    try:
        return float(df.iloc[idx]["Close"])
    except Exception:
        return None


def _period_for_timeframe(tf: str, max_bars: int) -> str:
    """Compute a period string wide enough to include max_bars of bars
    from the entry date. Errs on the side of "wider" so the bar at
    index max_bars is included."""
    # Provider-friendly defaults; fetch_ohlcv_df handles fallback.
    if tf in ("1m", "5m", "15m", "30m"):
        return "30d"
    if tf in ("1h", "2h", "4h"):
        return "1y"
    return "max"
```

### Step 3 — Refactor `update_pattern_stats_from_closed_trades`

Replace lines 4798-4895 in `learning.py`. Keep the function name and
signature so the call site at line 9582 is unchanged. Inside:

```python
def update_pattern_stats_from_closed_trades(
    db: Session, user_id: int | None,
) -> dict[str, Any]:
    """Aggregate canonical-corrected win/loss/return from closed trades
    and update their linked ScanPattern. Replaces the legacy
    realized-only aggregation with a counterfactual-aware computation
    for trades that held past their pattern's intended max_bars.

    Each invocation also writes one ``pattern_evidence_corrections``
    audit row per pattern processed, capturing before/after values and
    the counterfactual-coverage gap.

    Only considers trades closed in the last 180 days that have a
    ``scan_pattern_id``. The 180-day window is the legacy semantic;
    preserve to avoid behavior drift in this fix.
    """
    import uuid
    from ...models.trading import ScanPattern, Trade
    from .evidence_correction import (
        compute_trade_correction, aggregate_pattern_stats, PatternStats,
    )

    cycle_run_id = uuid.uuid4()
    backfill_mode = _is_first_run(db)

    cutoff = datetime.utcnow() - timedelta(days=180)
    closed_q = db.query(
        Trade.scan_pattern_id, Trade.id,
        Trade.entry_price, Trade.exit_price,
        Trade.entry_date, Trade.close_date,
        Trade.direction, Trade.ticker, Trade.pnl,
    ).filter(
        Trade.status == "closed",
        Trade.scan_pattern_id.isnot(None),
        Trade.close_date.isnot(None),
        Trade.entry_date.isnot(None),
        Trade.close_date >= cutoff,
    )
    if user_id is not None:
        closed_q = closed_q.filter(Trade.user_id == user_id)

    try:
        closed = closed_q.all()
    except Exception as e:
        logger.warning("[evidence_correction] closed-trade query failed: %s", e)
        return {"patterns_updated": 0, "error": str(e)}

    # Bucket by scan_pattern_id.
    from collections import defaultdict
    buckets: dict[int, list] = defaultdict(list)
    for row in closed:
        buckets[row.scan_pattern_id].append(row)

    updated = 0
    for pattern_id, trade_rows in buckets.items():
        try:
            pattern = db.get(ScanPattern, pattern_id)
            if pattern is None:
                continue
            if (
                user_id is not None
                and pattern.user_id is not None
                and pattern.user_id != user_id
            ):
                continue

            tf = pattern.timeframe or "1d"
            max_bars = _resolve_max_bars(pattern)

            corrections = []
            for trade_row in trade_rows:
                try:
                    c = compute_trade_correction(
                        entry_price=float(trade_row.entry_price),
                        exit_price=float(trade_row.exit_price),
                        entry_date=trade_row.entry_date,
                        close_date=trade_row.close_date,
                        direction=trade_row.direction or "long",
                        ticker=trade_row.ticker,
                        pattern_timeframe=tf,
                        max_bars=max_bars,
                    )
                    corrections.append(c)
                except Exception as e:
                    logger.debug(
                        "[evidence_correction] trade %s correction failed: %s",
                        trade_row.id, e,
                    )

            stats = aggregate_pattern_stats(corrections)
            _persist_pattern_correction(
                db, pattern, stats, cycle_run_id, backfill_mode,
            )
            updated += 1

        except Exception as e:
            logger.warning(
                "[evidence_correction] pattern %s correction failed: %s",
                pattern_id, e,
            )
            try:
                db.rollback()
            except Exception:
                pass

    return {"patterns_updated": updated, "cycle_run_id": str(cycle_run_id)}


def _is_first_run(db: Session) -> bool:
    """Return True iff pattern_evidence_corrections is empty."""
    from sqlalchemy import text
    try:
        row = db.execute(
            text("SELECT 1 FROM pattern_evidence_corrections LIMIT 1")
        ).first()
        return row is None
    except Exception:
        return False


def _resolve_max_bars(pattern: Any) -> int:
    """Read max_bars from pattern.exit_config; fall back to engine default."""
    try:
        cfg = pattern.exit_config or {}
        if isinstance(cfg, str):
            import json
            cfg = json.loads(cfg)
        v = cfg.get("max_bars")
        if v is not None:
            return int(v)
    except Exception:
        pass
    return 20  # legacy default; matches _load_exit_config defaults


def _persist_pattern_correction(
    db: Session, pattern: Any, stats: "PatternStats",
    cycle_run_id: Any, backfill_mode: bool,
) -> None:
    """Atomic write: ScanPattern field update (if changed) + one audit row."""
    from ...models.trading import PatternEvidenceCorrection  # new ORM
    from sqlalchemy import func
    from .evidence_correction import PatternStats as _PS  # type only

    before_wr = pattern.win_rate
    before_avg = pattern.avg_return_pct
    before_n = pattern.trade_count

    after_wr = round(stats.win_rate, 4)
    after_avg = round(stats.avg_return_pct, 2)
    actual_trade_count = (
        db.query(func.count(Trade.id))
        .filter(Trade.scan_pattern_id == pattern.id)
        .scalar() or 0
    )
    after_n = int(actual_trade_count)

    # Coverage-gate: if more than half of overheld trades had no
    # counterfactual data, the corrected stats are biased. Don't apply
    # the update; write an audit row with reason coverage_too_thin.
    if stats.overheld_n > 0:
        cf_unavail_share = stats.counterfactual_unavailable_n / stats.overheld_n
        if cf_unavail_share > 0.5:
            audit_reason = "coverage_too_thin"
            after_wr = before_wr
            after_avg = before_avg
            after_n = before_n
        else:
            audit_reason = (
                "first_run_backfill" if backfill_mode else "periodic_recompute"
            )
    else:
        audit_reason = (
            "first_run_backfill" if backfill_mode else "periodic_recompute"
        )

    changed = (
        before_wr != after_wr
        or before_avg != after_avg
        or before_n != after_n
    )
    if not changed and audit_reason != "coverage_too_thin":
        audit_reason = "no_change"
    elif changed and audit_reason not in ("coverage_too_thin", "first_run_backfill"):
        audit_reason = "periodic_recompute"

    if changed and audit_reason != "coverage_too_thin":
        # Only mutate ScanPattern when stats actually shifted AND the
        # coverage gate passed. math.isfinite + range guards as in
        # the original function (defense against NaN sweeps).
        import math
        if math.isfinite(stats.win_rate) and 0.0 <= stats.win_rate <= 1.0:
            pattern.win_rate = after_wr
        if math.isfinite(stats.avg_return_pct):
            pattern.avg_return_pct = after_avg
        pattern.trade_count = after_n
        pattern.updated_at = datetime.utcnow()

    audit_row = PatternEvidenceCorrection(
        scan_pattern_id=pattern.id,
        cycle_run_id=cycle_run_id,
        before_win_rate=before_wr,
        after_win_rate=after_wr,
        before_avg_return_pct=before_avg,
        after_avg_return_pct=after_avg,
        before_trade_count=before_n,
        after_trade_count=after_n,
        closed_trades_considered=stats.n,
        overheld_trade_count=stats.overheld_n,
        counterfactual_applied_count=stats.counterfactual_applied_n,
        counterfactual_unavailable_count=stats.counterfactual_unavailable_n,
        correction_reason=audit_reason,
    )
    db.add(audit_row)
    db.commit()
```

### Step 4 — Add `PatternEvidenceCorrection` ORM

In `app/models/trading.py`, add the ORM class matching mig 228's
schema. Place near other audit-table ORMs (e.g.,
`ExitParityLog`).

### Step 5 — Tests

`tests/test_evidence_canonical_writer.py`:

1. ✅ `compute_trade_correction` for a 1d trade held 5 days,
   `max_bars=20`, returns `overheld=False, corrected_pct ==
   realized_pct`.
2. ✅ `compute_trade_correction` for a 1m trade held 60 minutes,
   `max_bars=20`, returns `overheld=True` and corrected_pct based
   on counterfactual close (mock `fetch_ohlcv_df` for determinism).
3. ✅ `compute_trade_correction` short-position sign convention: long
   exit_price < entry_price loses; short exit_price < entry_price
   wins.
4. ✅ `compute_trade_correction` counterfactual unavailable returns
   `counterfactual_available=False, corrected_pct == realized_pct`.
5. ✅ `aggregate_pattern_stats` over 10 trades (3 overheld with
   counterfactual, 1 overheld without, 6 non-overheld) produces
   correct `overheld_n=4, counterfactual_applied_n=3,
   counterfactual_unavailable_n=1`.
6. ✅ `aggregate_pattern_stats` over zero trades returns `n=0,
   win_rate=0, avg_return_pct=0` (edge case, no NaN).
7. ✅ `update_pattern_stats_from_closed_trades` against fixtures with
   one pattern and 5 closed trades writes ONE audit row, updates
   the pattern's three fields, and returns `patterns_updated=1`.
8. ✅ `update_pattern_stats_from_closed_trades` first-run detection:
   empty `pattern_evidence_corrections` → `correction_reason =
   'first_run_backfill'`. Second invocation → `correction_reason
   in ('periodic_recompute', 'no_change')`.
9. ✅ Coverage-gate: when `counterfactual_unavailable_n /
   overheld_n > 0.5`, `correction_reason = 'coverage_too_thin'`
   and the ScanPattern fields are NOT updated.
10. ✅ Idempotence (steady-state): with no new closed trades,
    running `update_pattern_stats_from_closed_trades` twice
    produces identical ScanPattern field values on the second
    call. The second audit row has `correction_reason='no_change'`.
11. ✅ Realized-EV-gate integration: a fixture pattern that
    crosses from positive avg_return_pct to negative under
    counterfactual recomputation, when passed through
    `evaluate_realized_ev`, returns `passed=False`.
12. ✅ NaN guard: a malformed trade (NaN exit_price) is skipped at
    the per-trade `try/except` boundary; the rest of the pattern's
    aggregation completes normally.
13. ✅ Sign-convention sanity: a pattern of long winners all
    overheld with counterfactual closes lower than realized closes
    produces a corrected `avg_return_pct` LOWER than the realized
    `avg_return_pct`.
14. ✅ The 180-day cutoff is preserved — a closed trade older than
    180 days is excluded from the aggregation.

### Step 6 — Smoke verification

After deploy:

1. Wait one full learning cycle (≤ 5 seconds for brain-worker).
2. Audit-table population:
   ```sql
   SELECT correction_reason, COUNT(*)
     FROM pattern_evidence_corrections
     GROUP BY correction_reason ORDER BY 2 DESC;
   ```
   First cycle expects `first_run_backfill` to dominate; subsequent
   cycles expect `periodic_recompute` and `no_change`.

3. Per-pattern impact summary:
   ```sql
   SELECT scan_pattern_id,
          before_win_rate, after_win_rate,
          before_avg_return_pct, after_avg_return_pct,
          before_trade_count, after_trade_count,
          closed_trades_considered, overheld_trade_count,
          counterfactual_applied_count, counterfactual_unavailable_count
     FROM pattern_evidence_corrections
     WHERE correction_reason = 'first_run_backfill'
       AND (before_win_rate IS DISTINCT FROM after_win_rate
         OR before_avg_return_pct IS DISTINCT FROM after_avg_return_pct
         OR before_trade_count IS DISTINCT FROM after_trade_count)
     ORDER BY ABS(after_avg_return_pct - before_avg_return_pct) DESC NULLS LAST
     LIMIT 30;
   ```
   Surface the top 30 movers in the CC report.

4. Coverage gap quantification:
   ```sql
   SELECT
       SUM(closed_trades_considered) AS total_trades_considered,
       SUM(overheld_trade_count) AS total_overheld,
       SUM(counterfactual_applied_count) AS total_cf_computed,
       SUM(counterfactual_unavailable_count) AS total_cf_gap,
       COUNT(*) FILTER (WHERE correction_reason = 'coverage_too_thin') AS patterns_skipped_coverage
     FROM pattern_evidence_corrections
     WHERE correction_reason IN (
       'first_run_backfill', 'periodic_recompute', 'coverage_too_thin'
     );
   ```
   The total_cf_gap as a fraction of total_overheld is the headline
   honesty metric. The patterns_skipped_coverage count is the
   number of patterns where the bug bit hardest AND we couldn't
   correct.

5. Demotion chain — patterns whose lifecycle_stage changed
   post-correction:
   ```sql
   SELECT sp.id, sp.name, sp.timeframe, sp.lifecycle_stage,
          pec.after_win_rate, pec.after_avg_return_pct, pec.after_trade_count
     FROM scan_patterns sp
     JOIN pattern_evidence_corrections pec
       ON pec.scan_pattern_id = sp.id
    WHERE pec.created_at >= NOW() - INTERVAL '24 hours'
      AND pec.before_avg_return_pct IS DISTINCT FROM pec.after_avg_return_pct
      AND sp.lifecycle_stage <> 'promoted'
    ORDER BY pec.created_at DESC LIMIT 30;
   ```
   (Demotions show up after the next `finalize_promotion_with_cpcv`
   pass; surface honestly if none triggered yet.)

### Step 7 — Audit summary in CC report

The CC report at `docs/STRATEGY/CC_REPORTS/<date>_f-evidence-canonical-writer.md`
should include, in addition to the standard sections:

- The audit-table population per query above.
- The top 30 per-pattern impact table (Step 6 #3).
- The coverage gap headline (Step 6 #4).
- Demotion chain output (Step 6 #5).
- Confirmation that the 5s lean-cycle convergence is stable —
  show two consecutive learning-cycle audit rows for at least one
  pattern with `correction_reason='no_change'` proving steady-state.

## Constraints / do not touch

- **Default mode stays paper.** No live placement enable.
- **All 8 fast-path safety belts intact.** PROTOCOL Hard Rule 1.
- **No threshold tuning.** The realized-EV gate's existing
  thresholds stay; this fix corrects only the inputs.
- **Do not modify `realized_ev_gate.py` or `promotion_gate.py`.**
  They consume corrected data; auto-demote falls out for free.
- **Do not modify `exit_evaluator.py`.** Canonical evaluator stays
  source of truth.
- **Do not modify `Trade` / `PaperTrade` row data.** Counterfactual
  is derived; no schema mutation on the trade tables.
- **Do not touch the other 6 writer sites in `learning.py`.** Sites
  4715-4717 are overwritten anyway; `TradingInsight` writers
  aren't in the EV-gate read path; `ensure_mined_scan_pattern`
  writers fire on rare events.
- **Tests use `_test`-suffixed DB.** PROTOCOL Hard Rule 5.
- **Migration ID 228.** Verify with `verify-migration-ids.ps1`.
- **No `git push --force`.** PROTOCOL Hard Rule 4.
- **The function signature stays unchanged.** `update_pattern_stats_from_closed_trades(db, user_id)`
  remains callable from `learning.py:9582` without touching the call
  site.
- **The 180-day cutoff stays.** Test #14 enforces.

## Out of scope

- Backtest-derived evidence. Different surface; gated on
  f-exit-parity-metric-v2 cutover (queued).
- The other 6 writer sites in `learning.py`. Not load-bearing.
- A separate scheduler-worker job. The 5s lean-cycle is the
  cadence.
- Per-trade audit granularity (which specific trades got
  counterfactual). Surface as a follow-up if the operator wants it
  persisted.
- Notifying the operator on per-pattern demotion. Audit table is
  the alert surface.
- Position-side timeframe column on Trade/PaperTrade. Helper
  derives via `scan_pattern_id → ScanPattern.timeframe` for
  consistency with the time-decay-fix shape.
- LLM-context (`position_plan_generator`) pattern-evidence path.
  Reads `ScanPattern` fields directly; benefits transparently
  once corrected.

## Success criteria

1. **Migration 228 lands cleanly.** `verify-migration-ids.ps1`
   passes. Schema check confirms the audit table + 3 indexes.
2. **`evidence_correction.py` exists** with the contract in Step 2.
   Pure functions (no DB writes); testable in isolation.
3. **`update_pattern_stats_from_closed_trades` refactored** in
   place. Same signature, same call site. Calls the new helpers.
4. **Audit row written every cycle, every pattern processed**,
   even when the value didn't change (`correction_reason='no_change'`).
   Audit completeness preserved.
5. **Coverage gate works.** When `counterfactual_unavailable_share
   > 0.5` for a pattern, ScanPattern fields are NOT updated;
   audit row records `correction_reason='coverage_too_thin'`.
6. **All 14 new tests pass + existing exit-evaluator + parity
   tests still pass** against `chili_test`.
7. **First-run backfill** populates `pattern_evidence_corrections`
   with `correction_reason='first_run_backfill'` for every active
   pattern post-deploy. Subsequent cycles use `periodic_recompute`
   or `no_change`.
8. **Steady-state convergence demonstrated** in the CC report:
   two consecutive cycles' audit rows for the same pattern with
   `correction_reason='no_change'`.
9. **Coverage gap headline** in the CC report: total CF gap as a
   percentage of total overheld trades; count of patterns
   skipped due to `coverage_too_thin`.
10. **Top 30 per-pattern movers** table in the CC report.
11. **CC report** at
    `docs/STRATEGY/CC_REPORTS/<date>_f-evidence-canonical-writer.md`
    per PROTOCOL format.

## Rollback plan

- **Code rollback**: `git revert` the implementation commit.
  `update_pattern_stats_from_closed_trades` reverts to the legacy
  realized-only aggregation; existing audit rows stay intact for
  forensic purposes. **No data loss** — corrections already
  persisted to ScanPattern stay; the audit table preserves the
  before-state for reverse migration if ever needed.
- **Audit-table rollback**: drop the table:
  ```sql
  DROP TABLE IF EXISTS pattern_evidence_corrections;
  ```
  ScanPattern fields stay at their corrected values. Audit history
  is gone but production state is preserved.
- **Restore historical evidence per pattern**: if specific
  patterns' corrections are judged wrong-direction, the audit
  table's `before_*` columns enable a reverse migration:
  ```sql
  UPDATE scan_patterns sp
     SET win_rate = pec.before_win_rate,
         avg_return_pct = pec.before_avg_return_pct,
         trade_count = pec.before_trade_count
    FROM (SELECT DISTINCT ON (scan_pattern_id) *
          FROM pattern_evidence_corrections
          WHERE correction_reason = 'first_run_backfill'
          ORDER BY scan_pattern_id, created_at ASC) pec
   WHERE sp.id = pec.scan_pattern_id;
  ```
- **No live-broker rollback** — task is read-only on trades, no
  broker calls.

## Open questions for Cowork (surface in CC report only if relevant)

1. **OHLCV coverage at 1m timeframe.** What fraction of overheld
   1m trades have no counterfactual data? Surface prominently —
   this is the limit of what's correctable.
2. **First demotion-cycle observation.** Does the realized-EV
   gate fire post-correction on any historical patterns? List
   demoted patterns with their before/after stats.
3. **`coverage_too_thin` patterns.** Surface count + which
   timeframes dominate. Almost certainly weighted toward 1m where
   provider retention is shortest.
4. **The 180-day cutoff.** Inherited from legacy; preserves
   behavior in this fix. If the operator wants a different
   window (e.g., all-time), surface as a follow-up brief — that's
   a strategy decision not a correctness fix.
5. **Sites 4715-4717 (`learn_from_breakout_outcomes`)** still write
   `pattern.win_rate` and `pattern.avg_return_pct` from breakout-
   alert outcomes (different signal, not closed trades). For
   patterns with NO closed trades in the 180-day window, those
   values stand. They don't have the time-decay bug because
   alerts don't have positions, but they're a different signal
   entirely. Surface if the audit shows any meaningful number of
   patterns where the breakout-outcome value materially differs
   from what the closed-trade aggregation would say.
6. **Per-trade audit granularity.** Current design audits at
   pattern-level. If the operator wants per-trade audit (which
   trades got counterfactual treatment, with the realized vs
   counterfactual prices), it's a separate table — surface as a
   follow-up brief if useful.
