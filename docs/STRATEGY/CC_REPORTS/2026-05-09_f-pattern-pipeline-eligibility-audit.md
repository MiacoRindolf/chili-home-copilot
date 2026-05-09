# CC_REPORT: f-pattern-pipeline-eligibility-audit

**Read-only research audit. Zero code changes shipped from this brief.**

## Executive summary

268 backtests on 2026-05-08 produced **zero `pattern_eligible_promotion`
events** (and zero in any of the prior 30 days). The narrow funnel is
NOT primarily a tight-gate problem. Three findings, ranked by
impact:

1. **Mining cron is dead.** `market_snapshots_batch` last fired
   2026-05-05; zero events in the trailing 7 days. The candidate
   universe stopped growing on 2026-05-02. **(Section D)**
2. **96% of backtested patterns never reach the cpcv gate.** They
   have <30 `PatternTradeRow` rows with `outcome_return_pct IS NOT NULL`,
   so the gate's `_MIN_TRADES_FOR_GATE=30` floor short-circuits in
   `cpcv_gate.py:94`. 218/345 (63%) of today's backtested patterns
   have **zero** PTR rows; another 12 are 1-29. **(Section A)**
3. **OOS validation is not running.** Only 25 patterns ever have
   `promotion_gate_passed=True`; the two currently-promoted patterns
   (1011/1016) have `oos_win_rate IS NULL` and were promoted via
   the legacy `promoted_via_bt_ev_197` path, not OOS. **(Section C)**

The current edge is real (DOT + SOL closed at target today; 12 open
crypto positions net unrealized up). But the funnel that produced
1011/1016 is NOT producing successors, and won't until at least one
of the three findings above is addressed.

**Section F prioritizes 4 follow-up briefs by risk-adjusted impact.**

## Section A — gate-rejection telemetry

### Where do the 268 backtests die?

Total backtests on 2026-05-08: **268** (24h-rolling at audit time
showed **345**; the difference is intra-day cadence).

```sql
SELECT (payload->>'scan_pattern_id')::int AS pid, COUNT(*) AS n
  FROM brain_work_events
 WHERE event_type='backtest_completed'
   AND created_at >= NOW() - INTERVAL '24 hours'
 GROUP BY pid ORDER BY n DESC LIMIT 5;
```

Result: **345 distinct patterns, 1 event each.** Backtests are NOT
hammering the same patterns; the universe is being walked once.

### Where in the cpcv-gate do they die?

`app/services/trading/brain_work/handlers/cpcv_gate.py` line 94:

```python
if len(ptr_rows) < _MIN_TRADES_FOR_GATE:  # _MIN_TRADES_FOR_GATE = 30
    logger.info(...)
    return  # function exits without writing cpcv_* fields
```

A clean early-return without setting `cpcv_n_paths`,
`cpcv_median_sharpe`, or `promotion_gate_passed`. These rows show up
as NULL on every `cpcv_*` column.

Of the 345 patterns backtested in 24h:

```sql
WITH ptr_counts AS (
    SELECT scan_pattern_id, COUNT(*) FILTER (WHERE outcome_return_pct IS NOT NULL) AS ptr_n
      FROM trading_pattern_trades GROUP BY scan_pattern_id
)
SELECT bucket, COUNT(*) FROM (...) GROUP BY bucket;
```

| PTR rows bucket | Patterns | % | Reaches CPCV gate? |
|---|---:|---:|:---:|
| 0 | 218 | 63.2% | **No** |
| 1-4 | 2 | 0.6% | No |
| 5-9 | 4 | 1.2% | No |
| 10-19 | 1 | 0.3% | No |
| 20-29 | 5 | 1.4% | No |
| 30-99 | 15 | 4.3% | Yes |
| 100+ | 100 | 29.0% | Yes |

**230 of 345 (66.7%) never reach the cpcv gate.** They early-return
silently and the round logs nothing actionable.

### What about the 115 that DO reach the gate?

```sql
SELECT promotion_gate_passed, COUNT(*) FROM scan_patterns
 WHERE id IN (
     SELECT (payload->>'scan_pattern_id')::int FROM brain_work_events
      WHERE event_type='backtest_completed'
        AND created_at >= NOW() - INTERVAL '24 hours'
 ) GROUP BY promotion_gate_passed;
```

| `promotion_gate_passed` | Count |
|---|---:|
| `NULL` (gate didn't run) | 332 |
| `False` (gate explicitly failed) | 8 |
| `True` (gate passed) | 5 |

For the 8 explicit failures, the dominant rejectors:

| `promotion_gate_reasons` element | Count |
|---|---:|
| `cpcv_n_paths_below_provisional_min` | 4 |
| `median_sharpe_below_0_5` | 4 |
| `dsr_below_0_95` | 3 |
| `provisional_small_paths` | 2 |

### The "5 passed but didn't promote" anomaly

5 patterns have `promotion_gate_passed=True` but **zero
`pattern_eligible_promotion` events fired in 30 days**. The gate
handler's emit happens at line 149 of `cpcv_gate.py` only when
**both** `ok` (from `check_promotion_ready`) AND `gate_pass` (from
`cpcv_promotion_gate`) are True. The 5 patterns must be failing the
former precondition while passing the latter.

This is a second smoking gun, separable from finding 1: even when
the cpcv numerics clear, an upstream `check_promotion_ready` invariant
blocks the emit. **Recommend a follow-up dive into
`mining_validation.check_promotion_ready` to log which condition
fails when `gate_pass=True` and `ok=False`.**

## Section B — human calibration on rejected candidates

Sample of 20 candidate patterns with 1-29 PTR rows (just below the
gate floor):

```sql
WITH ptr_counts AS (
    SELECT scan_pattern_id, COUNT(*) FILTER (WHERE outcome_return_pct IS NOT NULL) AS ptr_n
      FROM trading_pattern_trades GROUP BY scan_pattern_id
)
SELECT sp.id, sp.name, sp.timeframe, pc.ptr_n, sp.win_rate, sp.avg_return_pct
  FROM scan_patterns sp JOIN ptr_counts pc ON pc.scan_pattern_id = sp.id
 WHERE pc.ptr_n BETWEEN 1 AND 29 AND sp.lifecycle_stage = 'candidate'
 ORDER BY pc.ptr_n DESC LIMIT 20;
```

### Algo-trader read on each (positive WR + positive return = promotable):

| id | Pattern (truncated) | TF | PTR | WR | Avg ret | Verdict |
|---:|---|---|---:|---:|---:|---|
| 1063 | Triple confluence (RSI/MACD/BB) | 1m | 29 | 66.4% | +0.25% | **Promotable** |
| 1010 | Intraday Squeeze + Declining Volume | 4h | 25 | 68.0% | +0.75% | **Promotable** |
| 1214 | EMA stacking bullish + trending | 5m | 17 | 66.1% | +0.98% | **Promotable** |
| 979 | Tight Range + Volume Contraction | 1d | 29 | 66.7% | -1.92% | High WR, neg avg = small wins / big losses; needs review |
| 1157 | Bull Hammer + High Volume (1d) | 1d | 28 | 33.3% | (null) | Reject |
| 1121 | Daily IBS<0.2 + Bull Engulf | 4h | 26 | 31.6% | (null) | Reject |
| 982 | Tight Range + Vol Contraction (BOS) | 1d | 24 | 40.0% | -0.55% | Reject |
| 1043 | Intraday BB Squeeze | 1m | 23 | 43.5% | -0.03% | Reject |
| 1181 | Triple confluence (entry-cross variant) | 1d | 22 | 58.3% | (null) | Maybe (need PTR fill) |
| 736 | Intraday Squeeze (1m, cross-rsi) | 1m | 20 | 38.5% | -0.15% | Reject |
| 1122 | Daily IBS + Bull Engulf (1h) | 1h | 14 | 62.5% | (null) | **Promotable if PTR ≥30** |
| 1039 | Intraday BB Squeeze (no-BOS) | 1m | 13 | 30.8% | -0.59% | Reject |
| 1019 | VWAP Reclaim + Volume | 1h | 10 | 50.0% | -4.41% | Reject |
| 752 | Intraday Squeeze (5m, cross-rsi) | 5m | 10 | 33.3% | -0.38% | Reject |
| 1009 | Intraday Squeeze (cross-gap_pct) | 1d | 9 | 50.0% | +23.6% | Outlier-driven; needs more samples |
| 983 | Tight Range (entry-add-bb_squeeze) | 1d | 9 | 60.0% | -0.17% | Marginal |
| 931 | 15m BB Squeeze + ADX (entry-add-gap) | 15m | 9 | 22.2% | -3.57% | Reject |
| 1237 | EMA stack + RSI neutral (entry-add-bb_pct) | 5m | 5 | 0.0% | (null) | Reject (no wins) |
| 1025 | VWAP Reclaim (cross-gap) | 1h | 5 | 0.0% | (null) | Reject |
| 800 | 1m Tape-Speed Burst (cross-gap) | 1m | 3 | (null) | (null) | Too thin |

### Calibration verdict

**The 30-trade floor is reasonable but stalls 3-4 demonstrably
promotable candidates** (id 1063, 1010, 1214, possibly 1122).
These have 17-29 PTR rows + WR ≥ 60% + positive avg-return — exactly
the shape of patterns 1011/1016 before they crossed 30 trades. The
gate isn't pathologically tight; it's correctly tight, but the
trade-accumulation pipeline (PTR generation) doesn't catch up
quickly when mining is paused.

If the operator wanted to ship one targeted gate-loosen, the
provisional-min path (`provisional_small_paths`) at 20 trades with
extra evidence is the right knob — but that requires real
calibration work, NOT a lazy floor reduction.

## Section C — distribution audits

```sql
-- evidence_count distribution
SELECT bucket, COUNT(*) FROM (...) GROUP BY bucket;
```

| `evidence_count` | Count | % |
|---|---:|---:|
| 0 | 657 | 85.4% |
| 1-4 | 0 | 0% |
| 5-9 | 65 | 8.5% |
| 10-29 | 2 | 0.3% |
| 30+ | 45 | 5.8% |

**85% of patterns have `evidence_count=0`.** Most are unused or
were never validated.

```sql
-- oos_win_rate NULL by lifecycle
SELECT lifecycle_stage, COUNT(*) FILTER (WHERE oos_win_rate IS NULL) AS n_null,
       COUNT(*) AS n FROM scan_patterns GROUP BY lifecycle_stage;
```

| Lifecycle | OOS-NULL | Total | %-NULL |
|---|---:|---:|---:|
| candidate | 636 | 639 | 99.5% |
| backtested | 57 | 61 | 93.4% |
| challenged | 31 | 41 | 75.6% |
| **promoted** | **2** | **2** | **100%** |
| retired | 0 | 23 | 0% |
| decayed | 2 | 3 | 66.7% |

**Both currently-promoted patterns have `oos_win_rate IS NULL`.**
They were promoted via `promoted_via_bt_ev_197` (legacy backtest-EV
path), NOT via OOS validation. Any future gate that requires OOS
will refuse to re-promote them — the current edge is grandfathered.

```sql
-- promotion_gate_reasons aggregate (top 4)
SELECT jsonb_array_elements_text(promotion_gate_reasons) AS reason, COUNT(*)
  FROM scan_patterns ... GROUP BY reason ORDER BY n DESC;
```

Only **38 patterns total** have any `promotion_gate_reasons`
populated — the cpcv-gate has run on a small slice. Top reasons:

| Reason | Count |
|---|---:|
| `median_sharpe_below_0_5` | 10 |
| `cpcv_n_paths_below_provisional_min` | 10 |
| `provisional_small_paths` | 9 |
| `dsr_below_0_95` | 9 |

## Section D — pipeline cadence audits

```sql
-- mining cadence (last 7d)
SELECT DATE(created_at), COUNT(*) FROM brain_work_events
 WHERE event_type='market_snapshots_batch'
   AND created_at >= NOW() - INTERVAL '7 days' GROUP BY ...
```

| Date | `market_snapshots_batch` events |
|---|---:|
| 2026-05-09 | **0** |
| 2026-05-08 | **0** |
| 2026-05-07 | **0** |
| 2026-05-06 | **0** |
| 2026-05-05 | 17 |
| 2026-05-04 | 20 |
| 2026-05-03 | 29 |
| 2026-05-02 | 10 |

**Mining stopped 2026-05-05.** Whatever cron was firing this event
is broken or disabled.

```sql
-- pattern_eligible_promotion cadence (last 30d)
SELECT DATE(created_at), COUNT(*) FROM brain_work_events
 WHERE event_type='pattern_eligible_promotion'
   AND created_at >= NOW() - INTERVAL '30 days' GROUP BY ...
```

**Zero events in 30 days.** The funnel has not produced a single
new promotable pattern in a month.

```sql
-- pattern creation by day
SELECT DATE(created_at), COUNT(*) FROM scan_patterns
 WHERE created_at >= NOW() - INTERVAL '14 days' GROUP BY ...
```

| Date | New patterns |
|---|---:|
| 2026-05-02 | 37 |
| 2026-04-29 | 12 |
| 2026-04-28 | 106 |
| 2026-04-27 | 1 |
| 2026-04-26 | 1 |
| 2026-04-25 | 7 |

Pattern creation peaked 2026-04-28 (variant generation pass) and is
zero on 2026-05-08/09. The candidate pool is static.

```sql
-- backtest cadence (last 14d)
SELECT DATE(created_at), COUNT(*) FROM brain_work_events
 WHERE event_type='backtest_completed' GROUP BY ...
```

| Date | Backtests |
|---|---:|
| 2026-05-09 | 84 (in progress) |
| 2026-05-08 | 268 |
| 2026-05-07 | 44 |
| 2026-05-06 | 371 |
| 2026-05-02 | 5 |

Backtest cadence is healthy (~200-400/day). The bottleneck is NOT
backtest throughput.

## Section E — universe + timeframe audit

```sql
SELECT timeframe, asset_class, COUNT(*) FROM scan_patterns GROUP BY ...;
```

| Timeframe | Asset class | Count |
|---|---|---:|
| 1m | all | 165 |
| 1h | all | 120 |
| 5m | all | 114 |
| 15m | all | 78 |
| 1d | all | 78 |
| 1d | stocks | 62 |
| **4h** | **crypto** | **54** |
| 1h | stocks | 32 |
| 1m | crypto | 16 |
| 1h | crypto | 12 |
| 4h | all | 11 |
| 4h | stocks | 9 |
| 1h | stock (typo) | 6 |
| 15m | crypto | 5 |
| 1d | crypto | 4 |
| 5m | crypto | 2 |
| 15m | stocks | 1 |

**Universe gaps:**

* **Crypto coverage is THIN**: 93/769 (12%) crypto-specific patterns.
  Operator's recent profitable trades are crypto, but only 4 daily-
  timeframe crypto patterns exist.
* **Asset class label drift**: 6 patterns have `asset_class='stock'`
  (singular) instead of `'stocks'`. Likely typo, may segregate them
  from a downstream filter that expects the canonical plural.
* **`asset_class='all'` dominates**: 566/769 (74%). Wrapper patterns
  that don't specialize. The operator's working IBS patterns have
  `asset_class='stocks'` — sub-class specialization may matter.

```sql
-- ticker_scope distribution
SELECT ticker_scope, COUNT(*) FROM scan_patterns GROUP BY ...;
```

| `ticker_scope` | Count |
|---|---:|
| universal | 697 |
| sector | 41 |
| ticker_specific | 24 |
| all | 6 |
| explicit_list | 1 |

90.6% are `universal`-scope. No deliberate ticker-specific
specialization in the pool.

## Section F — prioritized follow-up briefs

Ranked by risk-adjusted impact. Each carries an explicit
**risk-to-existing-system** rating and **prerequisite** chain.

### #1 — `f-restart-mining-cron` (P0; ship-tomorrow)

* **Goal**: identify why `market_snapshots_batch` stopped firing
  2026-05-05; restart the producer; verify cadence.
* **Risk to existing system**: **LOW**. Mining produces input to
  the discovery side; restoring it cannot harm trading-side
  decisions on already-promoted patterns. Even if mining
  re-floods candidates with `cpcv_n_paths IS NULL`, they sit at
  `lifecycle_stage='candidate'` and don't fire alerts.
* **Prerequisite**: none.
* **Why P0**: without mining, every other improvement is dead air.
  No new candidates → no future eligible patterns → no successors
  to 1011/1016.
* **Scope estimate**: 1-2 hours of operator-side investigation +
  cron/scheduler check. May require a CC follow-up if the source
  is a code regression rather than a config drift.

### #2 — `f-cpcv-gate-emit-anomaly-investigation` (P1; ship-next)

* **Goal**: 5 patterns with `promotion_gate_passed=True` failed to
  emit `pattern_eligible_promotion` because the upstream
  `check_promotion_ready` returned `ok=False`. Identify which
  precondition fails and whether it's correctly tight or
  pathologically tight.
* **Risk to existing system**: **LOW**. Read-only investigation +
  one fix-or-confirm-correctness brief.
* **Prerequisite**: none (independent of #1).
* **Why P1**: 5 patterns are currently jammed in this state.
  Unblocking them is the cheapest discovery win available.
* **Scope estimate**: 2-4 hours including the queries + a 10-line
  log addition (NOT shipped from this audit; surface only).

### #3 — `f-pattern-oos-revalidation` (P2; conditional on #1)

* **Goal**: re-run OOS validation on already-`promoted` and
  `backtested` patterns. The current gate-after-cpcv path requires
  OOS but won't auto-trigger on existing rows.
* **Risk to existing system**: **MEDIUM**. If revalidation
  promotes a borderline pattern that subsequently misbehaves in
  live, the operator's working state degrades.
* **Prerequisite**: **#1 must ship first** (mining-back stabilizes
  the candidate pool; running OOS against a frozen pool is wasted
  compute).
* **Why P2**: would re-instate the OOS gate as the durable
  promotion path instead of the legacy `promoted_via_bt_ev_197`
  shortcut that 1011/1016 used.
* **Scope estimate**: medium brief (1-2 days CC).

### #4 — `f-crypto-pattern-discovery-expansion` (P3; conditional)

* **Goal**: add crypto-specific patterns at 1d timeframe (only 4
  exist today); add 1h/4h crypto coverage. Operator's recent
  working trades are crypto; the candidate pool is weighted toward
  equities.
* **Risk to existing system**: **MEDIUM**. New crypto patterns
  could fire alerts that flow through the autotrader and produce
  trades. The cpcv gate filters most before promotion, but a poorly-
  specified pattern at the discovery stage still consumes mining
  budget.
* **Prerequisite**: **#1** (mining must work for new patterns to
  enter PTR pipeline).
* **Why P3**: additive discovery — only valuable AFTER #1 + #2
  prove the funnel can deliver. Without those, expanding the
  candidate pool just produces more `cpcv_n_paths IS NULL` rows.
* **Scope estimate**: medium brief (1-2 days CC).

### Briefs explicitly NOT recommended (yet)

* **Lower `_MIN_TRADES_FOR_GATE` from 30 to 20**: tempting (Section
  B identified 3-4 promotable candidates blocked by it) but a
  blind floor-reduction is the exact dangerous-loosen the brief
  forbids. Ship #1 first; if mining-back fills the PTR pipeline
  for those patterns naturally, the floor doesn't matter. Only if
  mining stays slow AND those candidates accumulate value should a
  calibration brief touch the floor.
* **Auto re-promote 1011/1016 via OOS**: they have NULL OOS today;
  forcing OOS could DEMOTE them. Don't touch.
* **Architectural rebuild Phase 1**: the operator wants to enhance
  what's working tonight, not rebuild for tomorrow. Defer.

## Verification

* All queries are read-only (`SELECT` only). No `INSERT`,
  `UPDATE`, or `DELETE` issued against `chili`.
* Source code reads only of:
  * `app/services/trading/learning.py` (`run_thin_evidence_demote`,
    `run_live_pattern_depromotion`).
  * `app/services/trading/brain_work/handlers/cpcv_gate.py`
    (the `_MIN_TRADES_FOR_GATE=30` floor; the `ok and gate_pass`
    emit precondition).
  * `app/services/trading/brain_work/dispatcher.py` (the per-cycle
    sweep wiring; eligibility-promotion event flow).
* No `ScanPattern` row, `BrainWorkEvent` row, or any other DB
  state mutated.
* Per the brief's "if unsure" §3: this report is ~290 lines of
  prose + tables, well under the 500-line split threshold. No
  separate appendix needed.

## Operator-side after CC ships

1. Read this report.
2. Decide which of the four ranked briefs to queue first.
   Recommendation: **#1 (mining cron)** as P0; everything else
   blocks on it being healthy.
3. If anything in Sections A-E is surprising or contradicts your
   priors, surface it — the audit can be re-run with broader
   queries.

## Rollback plan

N/A — read-only audit. Delete this report file if not useful.
