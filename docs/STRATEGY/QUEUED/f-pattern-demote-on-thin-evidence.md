# f-pattern-demote-on-thin-evidence

STATUS: QUEUED
SLUG: pattern-demote-on-thin-evidence
PROPOSED: 2026-05-08
SEVERITY: high (low-quality patterns keep firing because realized_avg_return_pct masks low win rate on tiny samples)

## TL;DR

Pattern 585 is the only pattern firing alerts right now (~157 alerts in last 24h). Its current state:
- `lifecycle_stage: 'promoted'`
- `trade_count: 4` (only 4 realized trades)
- `win_rate: 0.25` (1W / 3L)
- `realized_avg_return_pct: 6.71` ← masked by 1 outlier winner
- `oos_win_rate: NULL` (never out-of-sample validated)
- `promotion_gate_reasons: ['provisional_small_paths']` ← thin-evidence flag set at promotion time
- `evidence_count: 0`
- `demoted_at: NULL`

**The autotrader's `projected_profit_below_min` gate correctly rejects most trades for this pattern**, so the system isn't losing money on it — but the pattern keeps generating noise that chokes the alert pipeline. Add a demote handler: any pattern with thin evidence + low realized win rate + never-OOS-validated should auto-demote regardless of the `realized_avg_return_pct` headline number, which is too easy to manipulate with one outlier when N is tiny.

## Why now

Operator audit 2026-05-08 surfaced:
1. **Pattern 585 fires every 13-30 min** but every alert gets blocked downstream by `projected_profit_below_min` — wasting cycles on a known low-EV signal.
2. **The brain's promote-demote loop has a gap**: `realized_avg_return_pct = +6.71` triggers whatever demote-protection threshold exists, even though the underlying sample is 4 trades / 25% WR / no OOS validation.
3. The pattern was promoted via `provisional_small_paths` (the brain's own admission that the evidence was thin) and **never demoted despite 4 live trades that didn't validate the backtest**.

References:
- `app/services/trading/learning.py` (where realized_sync runs)
- `scan_patterns` schema (lifecycle_stage, promotion_status, demoted_at, ...)
- Memory: `reference_promotion_gates.md` (the EV + CPCV gates)
- Memory: `project_pattern_1047_history.md` (similar pattern that was twice forcibly restored despite poor live results)
- Yesterday's stock-drought diagnosis (logs reference 0% hit rate; actually 25% WR on 4 trades, but same conclusion)

## Goal

Add a demote-on-thin-evidence rule to the brain's pattern lifecycle handler. **Any pattern matching all of:**

1. `lifecycle_stage = 'promoted'`
2. `trade_count < 10` (small live sample)
3. `win_rate < 0.33` (below random for 50/50-bet patterns)
4. `oos_win_rate IS NULL` (never out-of-sample validated)
5. `'provisional_small_paths' IN promotion_gate_reasons` (gate flagged thin evidence at promotion)

→ Auto-demote to `lifecycle_stage = 'challenged'` with `promotion_demote_reason = 'thin_evidence_low_realized_wr'`.

The check ignores `realized_avg_return_pct` entirely because it's not robust against single-outlier inflation when sample is tiny.

## Acceptance criteria

1. New handler `_handle_thin_evidence_demote` (or extend existing demote handler) in the brain Phase 2 dispatcher.
2. Runs on the same trigger as other realized-sync handlers (e.g., on `live_trade_closed` event or per-cycle sweep).
3. Pattern 585 specifically gets demoted on next handler run.
4. Demotion writes `lifecycle_stage='challenged'`, `demoted_at=NOW()`, `promotion_demote_reason='thin_evidence_low_realized_wr'`.
5. Status endpoint shows the pattern as challenged; `pattern_imminent_alerts` no longer fires for challenged patterns.
6. New helper-level tests in `tests/test_pattern_demote_on_thin_evidence.py`:
   - 5+ tests covering the matrix (each criterion in isolation, all-criteria, edge cases).
7. Existing pattern lifecycle tests still pass.
8. CC report at `docs/STRATEGY/CC_REPORTS/YYYY-MM-DD_f-pattern-demote-on-thin-evidence.md`.

## Brain integration (reuse, don't rewrite)

- `scan_patterns` schema columns already exist (`lifecycle_stage`, `promotion_gate_reasons`, `trade_count`, `win_rate`, `oos_win_rate`, `demoted_at`, `promotion_demote_reason`).
- The existing `_handle_*` handlers in the brain Phase 2 dispatcher (per memory `reference_phase2_event_handlers.md`).
- `pattern_imminent_alerts.py` already filters out `lifecycle_stage != 'promoted'` patterns; demoting flips the filter automatically.
- The existing realized_sync loop runs every cycle (verified in earlier memory).

## Constraints / do not touch

- **Hard Rule 5: prediction-mirror authority frozen.** Don't touch authority contract.
- **No re-promotion logic in this brief.** A pattern demoted by this rule stays challenged until ANOTHER brief re-promotes it via OOS validation. (See `f-pattern-oos-revalidation` as a separate future brief if needed.)
- **No threshold tuning of `projected_profit_below_min`** in autotrader. That gate is correctly rejecting the noise; this brief reduces the noise at source.
- **No deletion of pattern rows.** Demote only. The history is load-bearing for forensics.
- **Edit-tool truncation discipline (HARD).** Splice pattern. `wc -l + ast.parse` post-edit.
- **Tests use `_test`-suffixed DB.**
- **No magic numbers**: the four threshold values (10, 0.33, NULL, 'provisional_small_paths') are constants in the handler module with a docstring linking back to this brief.

## Out of scope

- Re-promotion path (different brief).
- Pattern 1047 history (separate memory).
- Other lifecycle gaps (e.g., demote on too-many-consecutive-losses, demote on regime-mismatch). Surface follow-up briefs if they recur.
- UI/dashboard changes.
- Backfill: this brief only changes future behavior. The first run will demote pattern 585 (and any others matching the criteria); that's the expected one-shot effect.

## Sequencing

1. Truncation scan.
2. Read existing brain Phase 2 handlers to understand the dispatcher contract.
3. Add `_handle_thin_evidence_demote` handler (or extend existing).
4. Wire into the dispatch loop (registry pattern, same as other handlers).
5. Add tests.
6. Commit + push.

## Operator-side after CC ships

1. Pull + truncation scan.
2. Restart `brain-worker` and `scheduler-worker`.
3. **Verify pattern 585 gets demoted within 1-2 brain cycles** (≤ 10 min):
   ```sql
   SELECT id, name, lifecycle_stage, promotion_demote_reason, demoted_at
   FROM scan_patterns WHERE id = 585;
   ```
   Expected: `lifecycle_stage='challenged'`, `demoted_at` not NULL.
4. **Verify alert flow stops for pattern 585**:
   ```sql
   SELECT COUNT(*) FROM trading_alerts
   WHERE alert_type='pattern_breakout_imminent' AND scan_pattern_id=585
     AND created_at > NOW() - interval '15 minutes';
   ```
   Expected: 0 (alert generation halted).
5. Identify other promoted patterns matching the demote criteria; surface count in operator review.

## Rollback plan

`git revert` the commit. The handler is additive; revert simply stops auto-demoting. Pattern 585 stays challenged in the DB (its lifecycle_stage doesn't auto-revert), which is the desired state regardless. If the operator wants to manually re-promote, that's a SQL UPDATE.

## Open questions

1. **What's the cadence of the brain Phase 2 handler dispatcher?** Per memory it runs on `live_trade_closed` events. Verify the cadence in CC report; if it's too rare, add a per-cycle sweep too.
2. **Should `evidence_count = 0` also trigger demote?** Pattern 585 has `evidence_count: 0` — that itself looks like a stronger signal than the WR/sample-count criteria. Surface as a possible additional criterion; for now, the four criteria above are conservative and the operator can tighten in a follow-up.
3. **Pattern 1047 history.** Memory entry notes 1047 was twice forcibly restored despite poor live results. Does the new demote handler put 1047 back at risk if it currently has WR<33% on small N? Verify in CC report; if so, surface to operator before the demote runs (one-shot bypass with explicit operator approval).
4. **`projected_profit_below_min` threshold.** Once 585 is demoted and other promoted-but-unhealthy patterns get caught, does the autotrader's `projected_profit_below_min` blocker also need re-tuning? Probably not — the threshold is supposed to reflect economic reality, not pattern quality. But surface trend in the CC report.
