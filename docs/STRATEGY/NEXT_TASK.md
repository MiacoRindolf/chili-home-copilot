# NEXT_TASK: f-add-paper-shadow-mode

STATUS: DONE

## Goal

Add an **opt-in paper-shadow mode** to `auto_trader.py` so every live
trade decision ALSO creates a parallel paper trade. After this ships:

- Each BreakoutAlert that produces a live `placed`, `blocked`, or
  `skipped` decision ALSO produces a paper-trade row tagged
  `autotrader_v1_paper_shadow`, attributed to the original alert.
- Paper-shadow trades open at the alert's `current_price` (no broker
  slippage, no partial fills, no rejections) — measures the
  strategy's "ideal execution" P/L in real time.
- Pattern_stats handler (today's f-handler-pattern-stats) sees the
  paper-shadow closes too, but aggregates them under a separate
  attribution so live-vs-shadow can be compared per pattern.
- New SQL probe surfaces "live-vs-shadow P/L delta per alert" — the
  canonical execution-alpha-drag metric.

This is **opt-in via flag** (`chili_autotrader_paper_shadow_enabled`,
default `False`). Operator can flip it on without code changes; live
trading behavior is identical when the flag is off. **Default off
ships safely.**

## Why now

Today's chain of fixes proved the brain's event-driven architecture
is structurally sound but **paper-mode-as-canary is not currently
available** because paper and live are mutually-exclusive modes
(`auto_trader.py:632`). The operator runs in live mode; paper never
fires.

Paper-shadow-alongside-live solves five real algo-trader use cases:

1. **Execution-quality measurement.** Compare live realized P/L
   (slippage-affected) vs paper-shadow P/L (idealized fill) per
   alert → quantify execution-alpha-drag in basis points. Today
   measured indirectly via `execution_audit.py`; this gives a
   per-alert delta.

2. **Pure-strategy pattern evidence.** Today's canonical-aware writer
   (`update_pattern_stats_from_closed_trades`, mig 228) aggregates
   from realized live closes — contaminated by slippage, broker
   rejections, scale-in/out timing. Paper-shadow evidence is
   pure-strategy. The realized-EV gate could read either; pure is
   strictly more informative for pattern judgment.

3. **Brain learning surface.** Today the brain learns only from
   realized live trades — at current placement rate of ~0.14% of
   AutoTraderRun decisions, that's near-zero data. Paper-shadow
   makes 100% of decisions produce evidence regardless of broker
   availability or position-size limits. **More than 700× more
   training data per day.**

4. **Backtest-realism validation in real time.** Backtests use
   idealized fills. Live uses real fills. Paper-shadow uses
   real-time prices but idealized fills — **same as backtest**.
   If backtest projections match paper-shadow P/L, the backtest is
   realistic. If they diverge, the backtest math is broken (and
   today's pattern promotion decisions are based on fiction).

5. **Failure-mode visibility / opportunity cost.** When live fails
   (broker rejection, no order_id), today nothing is recorded as
   "this alert WOULD have made $N if executed." Paper-shadow
   captures the unrealized opportunity. Critical when live
   placement rate is low (as it is right now).

Three of five (#1, #2, #4) are load-bearing for the trading system's
correctness, not nice-to-haves. #3 unblocks the brain learning that
otherwise stays starved at the current placement rate. #5 gives
operator visibility when troubleshooting why live isn't placing.

## Scope boundary

**In scope:**
- Migration `_migration_NNN_paper_shadow_attribution` — add
  `trading_paper_trades.paper_shadow_of_alert_id` (nullable FK to
  `breakout_alerts.id`) + index.
- `chili_autotrader_paper_shadow_enabled: bool = False` config flag.
- `auto_trader.py`: after each live decision (placed / blocked /
  skipped), ALSO call `open_paper_trade(..., paper_shadow_of_alert_id=...)`
  when flag is on. Always-on within the live branch — independent of
  live success.
- `paper_trading.open_paper_trade`: accept new kwarg
  `paper_shadow_of_alert_id`, persist on the row.
- Pattern_stats handler **must NOT double-count** when aggregating
  closed trades. Filter `paper_shadow_of_alert_id IS NOT NULL` out
  of evidence aggregation by default; surface as a deferred follow-up
  whether to prefer-shadow vs prefer-live for evidence purposes.
- New SQL probe at
  `scripts/dispatch-paper-shadow-execution-delta.ps1` that reports
  per-alert live-vs-shadow P/L delta (the "execution-alpha-drag" metric).
- Tests covering opt-in toggle, parallel-row creation, attribution
  column, double-count filter.

**Out of scope:**
- Auto-promote paper-shadow to authoritative for the realized-EV
  gate. That's a separate strategic decision; surface as
  `f-prefer-shadow-evidence` if/when the data shows shadow is
  cleaner.
- Live ↔ shadow exit-time syncing. Shadow runs its OWN exit logic
  via the existing paper exit-engine. Live closes when it closes;
  shadow closes when it closes. The comparison is realized P/L,
  not bar-for-bar replay.
- UI / dashboard for execution-alpha-drag. Defer to follow-up brief
  `f-paper-shadow-dashboard` once data accumulates.
- Modifying live trading behavior in any way. Live path is
  unchanged; shadow is purely additive.
- Modifying the canonical evaluator (`exit_evaluator.py`).
- Modifying the realized-EV gate or promotion gate.
- Live-mode partial closes — separate concern from paper-shadow.

## Brain integration / source material

- `app/services/trading/auto_trader.py:625-1565` — the entry-side
  decision logic. Live branch at 1370+; paper branch at 1517+. The
  shadow hook lands inside the live branch, after each terminal
  decision (placed / blocked / skipped).
- `app/services/trading/auto_trader.py:632` — `live = bool(runtime.get("live_orders_effective"))`.
  Shadow only fires when `live=True`. When `live=False`, paper is
  already opening directly; shadow would be redundant.
- `app/services/trading/paper_trading.py::open_paper_trade` — extend
  the signature.
- `app/models/trading.py::PaperTrade` — add the new column to ORM
  matching the migration.
- `app/services/trading/learning.py:4798` —
  `update_pattern_stats_from_closed_trades`. Add the
  `paper_shadow_of_alert_id IS NULL` filter to the closed-trade
  query so shadow rows don't double-count.
- `app/config.py` — add `chili_autotrader_paper_shadow_enabled: bool = False`.
- `docs/STRATEGY/PHASE2_HANDLER_BACKLOG.md` — note that this brief
  generates new paper-trade traffic that the existing pattern_stats
  / demote / regime_ledger handlers will consume.

## Path

### Step 0 — Pre-execution audit (DO BEFORE ANY CODE CHANGE)

The codebase contains a **dormant placeholder** for "paper shadow book"
that was added with intent but never wired:

- `ScanPattern.paper_book_json` (`app/models/trading.py:865-868`) —
  JSONB column, default `{}`, comment "paper shadow book". As of
  brief-write time, **zero readers, zero writers** in the codebase.
- `brain_paper_book_on_promotion: bool = False`
  (`app/config.py:2616-2617`) — flag with comment "When a pattern is
  promoted, initialize paper_book_json for optional shadow tracking".
  As of brief-write time, **zero readers** of this flag.

This brief implements paper-shadow with a **better-shaped per-trade
schema** (FK column on `trading_paper_trades`) instead of the
placeholder's per-pattern JSONB aggregate. The per-trade design fits
the event-driven handler chain we just built; the JSONB design
doesn't.

**Confirm the placeholder is still unused before proceeding.** Run:

```bash
# Both should return zero reader/writer matches
grep -rn "paper_book_json" app/ scripts/ \
    | grep -v "models/trading.py" \
    | grep -v "migrations.py" \
    | grep -v "schema definition only"

grep -rn "brain_paper_book_on_promotion" app/ scripts/ \
    | grep -v "config.py"
```

**Decision tree:**
- **Both grep returns zero matches**: placeholder is dormant.
  **Proceed to Step 1** with the per-trade design as specified.
- **Either grep returns matches**: someone wired the placeholder
  between brief-write and execution. **Surface in CC report and
  STOP** — re-evaluate whether to ship the per-trade design alongside
  the now-active per-pattern JSONB or reconcile the two designs.

Document the audit result in the CC report's "Pre-execution audit"
section. The placeholder schema column + dead flag are NOT removed
in this brief — that's a separate cleanup brief
(`f-cleanup-paper-book-json-placeholder`) once paper-shadow has
been operationally proven (≥1 week of clean shadow data with the
new design). Premature cleanup risks a flailing rollback if the
new design fails.

### Step 1 — Migration `_migration_NNN_paper_shadow_attribution`

```sql
ALTER TABLE trading_paper_trades
    ADD COLUMN IF NOT EXISTS paper_shadow_of_alert_id INTEGER NULL
        REFERENCES breakout_alerts(id) ON DELETE SET NULL;

-- Sparse partial index — most paper trades won't be shadow trades.
CREATE INDEX IF NOT EXISTS ix_trading_paper_trades_paper_shadow_alert
    ON trading_paper_trades (paper_shadow_of_alert_id)
    WHERE paper_shadow_of_alert_id IS NOT NULL;
```

Migration ID = next sequential at execution time. Verify with
`scripts/verify-migration-ids.ps1`.

### Step 2 — ORM update

In `app/models/trading.py::PaperTrade`, add:

```python
paper_shadow_of_alert_id: Optional[int] = Column(
    Integer, ForeignKey("breakout_alerts.id"), nullable=True
)
```

Place near other foreign keys; match column ordering to the migration.

### Step 3 — Extend `open_paper_trade`

In `app/services/trading/paper_trading.py`, extend the `open_paper_trade`
signature:

```python
def open_paper_trade(
    db: Session,
    user_id: int,
    ticker: str,
    entry_price: float,
    *,
    scan_pattern_id: int | None = None,
    stop_price: float | None = None,
    target_price: float | None = None,
    direction: str = "long",
    quantity: int = 1,
    signal_json: dict | None = None,
    paper_shadow_of_alert_id: int | None = None,  # NEW
) -> PaperTrade | None:
    ...
```

Persist `paper_shadow_of_alert_id` on the new PaperTrade row when
provided.

### Step 4 — Wire shadow into auto_trader live branch

In `app/services/trading/auto_trader.py`, after each terminal decision
in the live branch, add the shadow hook. Locate the three terminal
points in the live branch (lines ~1407, ~1428, plus the placed-success
branch ending at ~1515):

```python
# Helper: open the paper-shadow trade. Always-on within the live
# branch (regardless of live success/failure). Gated on
# chili_autotrader_paper_shadow_enabled. Failures swallowed at this
# boundary — shadow must never break the live decision flow.
def _maybe_open_paper_shadow(
    db: Session, *, uid: int, alert: Any, qty: int, px: float, snap: dict,
) -> None:
    if not getattr(settings, "chili_autotrader_paper_shadow_enabled", False):
        return
    try:
        from .paper_trading import open_paper_trade
        sig = {
            "auto_trader_v1": True,
            "breakout_alert_id": int(alert.id),
            "paper_shadow": True,
            "shadow_of_alert_id": int(alert.id),
            "projected": snap.get("projected_profit_pct"),
        }
        open_paper_trade(
            db, uid, alert.ticker, px,
            scan_pattern_id=alert.scan_pattern_id,
            stop_price=float(alert.stop_loss) if alert.stop_loss is not None else None,
            target_price=float(alert.target_price) if alert.target_price is not None else None,
            direction="long",
            quantity=max(1, int(qty)),
            signal_json=sig,
            paper_shadow_of_alert_id=int(alert.id),
        )
        logger.info(
            "[autotrader_paper_shadow] alert_id=%s pattern_id=%s ticker=%s qty=%s px=%s opened",
            alert.id, alert.scan_pattern_id, alert.ticker, qty, px,
        )
    except Exception:
        logger.debug(
            "[autotrader_paper_shadow] open failed for alert_id=%s",
            getattr(alert, "id", None), exc_info=True,
        )
```

Then call it AFTER:
1. Each `_audit(... decision="blocked", ...)` in the live branch
2. Each `_audit(... decision="skipped", ...)` in the live branch
3. The successful `_audit(... decision="placed", reason="ok", ...)`
   at line ~1503

The "after" placement matters: the live decision and its audit row
land first, the shadow opens second. If shadow fails, the live
decision is intact.

**Do NOT** call this from the paper branch (lines 1517+). When
`live=False`, the paper branch is already creating paper trades
directly — shadow would create duplicates.

### Step 5 — Filter shadow rows out of evidence aggregation

In `app/services/trading/learning.py::update_pattern_stats_from_closed_trades`,
the closed-trade query (around line 4830) currently aggregates ALL
closed trades. Add a filter to exclude shadow rows from default
evidence:

```python
closed_q = db.query(...).filter(
    Trade.status == "closed",
    Trade.scan_pattern_id.isnot(None),
    Trade.exit_date.isnot(None),
    Trade.entry_date.isnot(None),
    # NEW: exclude paper-shadow rows from evidence aggregation by
    # default. Shadow rows are kept for execution-alpha-drag
    # measurement (separate query); they're not the canonical
    # realized P/L for pattern evidence.
)
# When extending to PaperTrade in the union, add:
# PaperTrade.paper_shadow_of_alert_id.is_(None)
```

The current function reads from `Trade` (not PaperTrade) per the audit;
verify the filter actually applies to the union the function uses.

### Step 6 — Config setting

In `app/config.py`:

```python
chili_autotrader_paper_shadow_enabled: bool = False
```

Default `False`. Comment inline: "When True, every autotrader live
decision ALSO opens a paper-shadow trade tagged
`paper_shadow_of_alert_id`. Used to measure execution-alpha-drag,
provide pure-strategy pattern evidence, and unstarve brain learning
during low-live-placement-rate periods. Default off; opt-in only."

### Step 7 — Execution-alpha-drag SQL probe

`scripts/dispatch-paper-shadow-execution-delta.ps1`:

```sql
-- Per-alert live-vs-shadow P/L delta. Positive = shadow did better
-- (slippage hurt live execution). Negative = shadow did worse
-- (entry price drift in operator's favor). Magnitude in basis points.
WITH pairs AS (
    SELECT
        pt.paper_shadow_of_alert_id AS alert_id,
        pt.id AS shadow_id,
        pt.scan_pattern_id,
        pt.ticker,
        pt.entry_date AS shadow_entry,
        pt.exit_date AS shadow_exit,
        pt.exit_price AS shadow_exit_price,
        pt.entry_price AS shadow_entry_price,
        pt.pnl AS shadow_pnl,
        t.id AS live_id,
        t.exit_price AS live_exit_price,
        t.entry_price AS live_entry_price,
        t.pnl AS live_pnl,
        t.exit_reason AS live_exit_reason
    FROM trading_paper_trades pt
    LEFT JOIN trading_trades t
        ON t.related_alert_id = pt.paper_shadow_of_alert_id
       AND t.broker_source = 'robinhood'
       AND t.management_scope = 'auto_trader_v1'
    WHERE pt.paper_shadow_of_alert_id IS NOT NULL
      AND pt.status = 'closed'
      AND pt.exit_date >= NOW() - INTERVAL '7 days'
)
SELECT
    p.alert_id,
    p.scan_pattern_id,
    p.ticker,
    p.shadow_pnl,
    p.live_pnl,
    (COALESCE(p.shadow_pnl,0) - COALESCE(p.live_pnl,0))
        / NULLIF(p.shadow_entry_price, 0) * 10000.0 AS delta_bps,
    p.live_exit_reason
FROM pairs p
ORDER BY ABS(COALESCE(p.shadow_pnl,0) - COALESCE(p.live_pnl,0)) DESC
LIMIT 30;
```

Plus aggregate stats (count of paired closes, mean delta_bps, stddev,
t-statistic on bias) to give the operator a one-shot read on
whether execution drag is significant at 95% CI.

### Step 8 — Tests

`tests/test_paper_shadow_mode.py`:

1. ✅ Flag off (default): live decision creates Trade row; **no
   PaperTrade row created.**
2. ✅ Flag on, live placed: live decision creates Trade row AND
   parallel PaperTrade row tagged `paper_shadow_of_alert_id`.
3. ✅ Flag on, live blocked: no Trade row, but PaperTrade-shadow
   IS still created (captures opportunity-cost).
4. ✅ Flag on, live skipped (e.g., PDT gate): no Trade row, but
   PaperTrade-shadow IS still created.
5. ✅ Flag on, paper mode (`live=False`): NO shadow created. The
   paper branch already runs; shadow would duplicate.
6. ✅ Shadow row carries `paper_shadow_of_alert_id` matching the
   alert that triggered it.
7. ✅ `update_pattern_stats_from_closed_trades` filters shadow rows
   out of evidence aggregation. Synthetic pattern with 5 normal
   closes + 5 shadow closes computes win_rate from the 5 normal,
   not 10 mixed.
8. ✅ Shadow open failure does not break the live decision flow
   (mock `open_paper_trade` to raise; live Trade still committed).

### Step 9 — Smoke verification

After deploy with `chili_autotrader_paper_shadow_enabled=true`:

1. Wait for next AutoTraderRun (every 1 min via scheduler-worker).
2. Check parallel rows:
   ```sql
   SELECT
       (SELECT COUNT(*) FROM trading_trades
        WHERE management_scope = 'auto_trader_v1'
          AND entry_date >= NOW() - INTERVAL '15 minutes') AS live_n,
       (SELECT COUNT(*) FROM trading_paper_trades
        WHERE paper_shadow_of_alert_id IS NOT NULL
          AND entry_date >= NOW() - INTERVAL '15 minutes') AS shadow_n;
   ```
   Expected: `shadow_n >= live_n + (skipped/blocked count)`. Every
   live decision should have a shadow.
3. Run the execution-alpha-drag probe (Step 7). With <30 minutes of
   data, the t-statistic will be unreliable but the per-alert table
   should populate.
4. Watch `[autotrader_paper_shadow]` log lines for any errors.

## Constraints / do not touch

- **Default mode stays paper.** Top-level operator default unchanged.
  Only the SHADOW addition is opt-in.
- **All 8 fast-path safety belts intact.** PROTOCOL Hard Rule 1.
- **Do not modify live trading behavior.** Shadow is purely additive
  alongside live; live path is unchanged.
- **Do not modify the canonical evaluator** (`exit_evaluator.py`).
- **Do not modify the realized-EV gate or promotion gate.** Auto-
  demote falls out for free if shadow is later promoted to
  authoritative; that decision is a separate brief.
- **Do not modify any of the 6 existing brain_work handlers.** The
  pattern_stats / demote / regime_ledger handlers will see shadow
  paper-trade-closed events; they'll process them. The filter at
  Step 5 is in `update_pattern_stats_from_closed_trades`, not in
  the handler itself.
- **Tests use `_test`-suffixed DB.** PROTOCOL Hard Rule 5.
- **Migration ID** = next sequential. Verify with
  `verify-migration-ids.ps1`.
- **No `git push --force`.** PROTOCOL Hard Rule 4.
- **The flag default is `False`.** Shipping must not change current
  trading behavior. Operator opts in explicitly.

## Out of scope

- Auto-promoting shadow evidence to be the canonical pattern-evidence
  source. Separate strategic decision; queue
  `f-prefer-shadow-evidence` for follow-up if data shows shadow is
  cleaner.
- UI / dashboard for execution-alpha-drag. Follow-up brief
  `f-paper-shadow-dashboard`.
- Live ↔ shadow exit-time syncing. Shadow runs its own exit logic.
- Modifying which exit reasons close shadow trades. They follow the
  existing paper exit-engine.
- Position-size sync between live and shadow (e.g., if live partials
  out, shadow should partial out). Not in scope; shadow runs
  independent.
- Backtest-shadow comparison (a third axis). Not in scope.
- LLM-context (`position_plan_generator`) updates to surface
  paper_shadow attribution. Out of scope.

## Success criteria

1. **Migration lands cleanly.** `verify-migration-ids.ps1` passes.
   Schema check confirms the new column + sparse index.
2. **`chili_autotrader_paper_shadow_enabled: bool = False`** added
   to `app/config.py` with documented default.
3. **`open_paper_trade` extended** with the new kwarg, persists
   correctly.
4. **Auto_trader live branch hooks the shadow** at all three
   terminal decision points (placed / blocked / skipped). Calls are
   try/except wrapped.
5. **Pattern_stats filter excludes shadow rows** from default
   evidence aggregation.
6. **All 8 new tests pass + existing tests still pass** against
   `chili_test`.
7. **Execution-alpha-drag SQL probe** at
   `scripts/dispatch-paper-shadow-execution-delta.ps1` exists and
   produces correct output on a synthetic test dataset.
8. **CC report** at
   `docs/STRATEGY/CC_REPORTS/<date>_f-add-paper-shadow-mode.md` per
   PROTOCOL format. Include a per-test result + flag-off / flag-on
   smoke comparison inline.

## Rollback plan

- **Flag rollback (no code change)**: set
  `chili_autotrader_paper_shadow_enabled=false` in compose.yml or
  env. Shadow path becomes inert; live behavior unchanged.
- **Code rollback**: `git revert` the implementation commit. Shadow
  hooks disappear, ORM column stays (harmless), migration stays
  (idempotent).
- **Migration rollback**: drop column if needed:
  ```sql
  ALTER TABLE trading_paper_trades
      DROP COLUMN paper_shadow_of_alert_id;
  ```
  Existing shadow rows lose their attribution but stay queryable
  by `signal_json->>'paper_shadow' = 'true'`.
- **No live-broker rollback needed** — task adds NO broker calls.
- **Existing shadow rows are preserved across rollbacks** — the
  ROW data stays; the FK column may go but the rows don't.

## Open questions for Cowork (surface in CC report only if relevant)

1. **Pattern_stats filter behavior**: shadow rows excluded by default.
   If post-deploy analysis suggests shadow is cleaner evidence, the
   follow-up `f-prefer-shadow-evidence` brief flips the filter logic
   (prefer shadow over live for evidence aggregation; live becomes
   the secondary). Surface a recommendation after first 24h of
   shadow data accumulates.

2. **Shadow open failure handling** — current design: swallow + log
   at DEBUG. If the operator wants louder failure visibility (e.g.,
   ERROR level when shadow fails), surface the choice. My read: DEBUG
   is right because shadow is "additive observability"; a failed
   shadow is a missed measurement, not a trading error. But surface
   for explicit confirmation.

3. **Delta-bps computation in the SQL probe** uses `shadow_entry_price`
   as the denominator. Strictly correct would be position-size-weighted
   (delta_dollars / position_dollars). With single-quantity paper
   trades the simple form is fine; surface if real position sizing
   makes the simple form misleading.

4. **First-deploy backfill** — when the flag is first flipped on,
   shadow trades only start opening from that moment forward. There's
   no historical backfill (would require fictional price data per
   past alert). Acceptable per the use case (forward-looking
   measurement), but surface explicitly.

5. **Correlation of shadow exit to live exit** — by design, shadow
   runs its own exit logic. So shadow may close at a different time
   than the live trade does. The "live-vs-shadow P/L delta" is over
   different holding periods. Strictly correct measurement requires
   matching close times; today's design measures overall P/L with
   different holding periods. Surface as a watch item; if the data
   shows wide hold-period divergence, the dashboard brief should
   show both raw delta and time-aligned delta.

6. **Paper-shadow capacity** — paper has its own "open positions"
   accounting. If shadow opens N positions per minute, the paper
   open-positions count grows fast. The pattern_position_monitor
   that currently runs every 5 min already handles paper-mode
   exits; it'll handle shadow too. But if shadow opens more
   positions than live does (because shadow doesn't have broker
   rejections), surface if the paper exit-engine becomes a
   bottleneck.
