# f-canonical-outcome-layer (Phase A of evidence-fidelity-architecture)

> **Type:** Schema migration + writer/reader contract update
> **Parent:** `docs/STRATEGY/QUEUED/f-evidence-fidelity-architecture-2026-05-14.md`
> **Foundational:** Phases B/C/D/E all read these columns. Ship first.

## Goal

Stop the silent race between `update_pattern_stats_from_closed_trades`
(corrected, time-decay-aware) and `sync_realized_stats` (raw realized).
Currently both write to the SAME `scan_patterns.{trade_count, win_rate,
avg_return_pct}` columns; whichever runs last wins. Pattern 585 currently
shows the dumber raw-realized numbers (83/34.9%) instead of the corrected
ones (87/39.8%).

## Design

### New columns on scan_patterns (migration N+1)

```sql
ALTER TABLE scan_patterns
  ADD COLUMN corrected_trade_count INTEGER,
  ADD COLUMN corrected_win_rate DOUBLE PRECISION,
  ADD COLUMN corrected_avg_return_pct DOUBLE PRECISION,
  ADD COLUMN raw_realized_trade_count INTEGER,
  ADD COLUMN raw_realized_win_rate DOUBLE PRECISION,
  ADD COLUMN raw_realized_avg_return_pct DOUBLE PRECISION,
  ADD COLUMN corrected_stats_updated_at TIMESTAMP,
  ADD COLUMN raw_realized_stats_updated_at TIMESTAMP;

-- Keep legacy columns as canonical (= corrected) for backward compat
-- via a one-time backfill + ongoing dual-write from the corrected
-- writer only.
```

### Writer contract

- **`update_pattern_stats_from_closed_trades` (learning.py:5147)**
  writes BOTH `corrected_*` columns AND the legacy `{trade_count,
  win_rate, avg_return_pct}` columns. It is the canonical writer.
- **`sync_realized_stats` (realized_stats_sync.py:40)** writes ONLY
  the `raw_realized_*` columns. **Never overwrites the legacy columns.**

### Reader contract

Update these callers to explicitly read `corrected_*`:
- `promotion_gate.py` (every threshold check)
- `realized_ev_gate.py`
- `cpcv_adaptive_gate.py` (Phase 2 from prior arc)
- `auto_trader.py` (sizing + filter gates)
- `pattern_quality_score.compute` (Phase 3 from prior arc)

UI consumers (`brain_runtime_gates.html` from Phase 4) read BOTH and
display side-by-side with a delta indicator.

## Deliverables

1. **`app/migrations.py`** — migration N+1: add 8 columns + indexes
2. **`app/services/trading/learning.py`** — `update_pattern_stats_from_closed_trades`
   writes both corrected_* and legacy columns
3. **`app/services/trading/realized_stats_sync.py`** — writes only
   raw_realized_*, NEVER legacy
4. **Reader updates** in 5 files (promotion_gate, realized_ev_gate,
   cpcv_adaptive_gate, auto_trader, pattern_quality_score)
5. **One-shot backfill** `scripts/canonical-outcome-backfill.ps1` —
   populates corrected_* and raw_realized_* for all active patterns
   from historical data
6. **Test:** `tests/test_canonical_outcome_layer.py` — race test (both
   writers fire, verify legacy column unchanged; verify corrected_* +
   raw_realized_* both populated with their own definitions)
7. **CC_REPORT**: `docs/STRATEGY/CC_REPORTS/2026-05-14_canonical-outcome-layer.md`

## Hard constraints

- Legacy `{trade_count, win_rate, avg_return_pct}` columns stay populated
  (mapped to corrected_*) — no downstream consumer breaks at merge.
- Migration is additive: add columns, no drops.
- Backfill is idempotent; can be re-run.
- No autotrader live behavior change at merge (autotrader still reads
  legacy columns until reader update lands; legacy = corrected after
  this PR, so the change is from "sometimes corrected, sometimes raw"
  to "always corrected" — strict improvement).

## Consult gate

When corrected and raw values disagree by >20%, do we emit an audit
alert or just shadow-log? Brief default: shadow-log only; alert when
delta >50%. CC should surface in plan.
