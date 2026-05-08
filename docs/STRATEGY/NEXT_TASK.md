# NEXT_TASK: f-pattern-demote-on-thin-evidence

STATUS: DONE

## Goal

Add an auto-demote handler so promoted patterns with thin evidence
+ poor live results + no OOS validation get moved back to
`lifecycle_stage='challenged'`. Specifically: pattern 585 (the
sole alert source today, 158/158 alerts in 24h, 4 trades / 25% WR
/ no OOS / `provisional_small_paths` gate) gets demoted on first
handler run. Cuts 100% of current alert noise without harming
healthy patterns.

The full brief is at
`docs/STRATEGY/QUEUED/f-pattern-demote-on-thin-evidence.md`
— read it first.

## Why now (algo-trader-architect framing)

Pre-deploy audit of `scan_patterns` for promoted patterns
(2026-05-08 14:07 PDT):

| id | name | trades | WR | OOS | gates | verdict |
|---|---|---|---|---|---|---|
| 585 | Intraday Squeeze + Declining Volume | 4 | 25% | NULL | `provisional_small_paths` | **DEMOTE** |
| 1011 | Reddit IBS mean reversion | **409** | **63.2%** | NULL | (clean) | KEEP |
| 1016 | Reddit IBS mean reversion | **565** | **70.7%** | NULL | (clean) | KEEP |
| 1047 | rsi_bullish_divergence | 4 | 25% | 50% | `provisional_small_paths` | already `challenged` ✓ |

**The healthy promoted patterns (1011, 1016) are real signals**
with statistically meaningful sample sizes. They just aren't
firing right now because their setup conditions aren't being
met — which is correct behaviour. Pattern 585 is the only thing
in the alert pipeline today, and its `avg_return_pct=+6.72` is
masked by a single outlier winner (1W / 3L on 4 trades).

After this brief ships, alert volume will drop sharply (likely
to 0 until 1011/1016 IBS conditions trigger). **That is the
correct algo-trader outcome**: prefer no signal to a bad signal.
A pattern with N=4, WR=25%, no OOS validation, and a
"provisional_small_paths" promotion flag is not a tradeable
edge.

The reconciler chain shipped earlier today (Phase A + B + C)
closed the wipeout-cascade loop. With trade-side risk infra
solid, the next-largest source of operational noise is the
promotion gate's leniency on thin-evidence patterns.

## Why this is the right next move (vs. the alternatives)

* Not `f-pdt-crypto-bypass-cleanup`: hygiene only; doesn't change
  observable behaviour. Smaller leverage.
* Not `f-autotrader-pdt-aware-exit-deferral`: premise was flawed
  (no real autotrader same-day round-trips occurring). Needs
  rewriting before it can ship.
* Not Phase D of the reconciler chain: 7-day soak first; let
  the post-R32 phantom count drop to 0 before piling on more
  reconciler protection.
* Not new algo work (e.g., `f-fastpath-microstructure-features-v2`,
  Hyperliquid perps): the pattern-demote brief unblocks
  observability of the equity book; better alpha work is more
  productive on a pipeline that isn't drowning in noise.

## The change (per the brief)

Add `_handle_thin_evidence_demote` to the brain's Phase 2
dispatcher. Pattern matches all of:

1. `lifecycle_stage = 'promoted'`
2. `trade_count < 10`
3. `win_rate < 0.33`
4. `oos_win_rate IS NULL`
5. `'provisional_small_paths' IN promotion_gate_reasons`

→ Demote to `lifecycle_stage='challenged'` with
`promotion_demote_reason='thin_evidence_low_realized_wr'`.

The check **ignores `avg_return_pct`** entirely — it's not robust
against single-outlier inflation when N is tiny. Today's pattern
585 has `avg_return_pct=+6.72` and is hard-blocked at every
entry attempt by `projected_profit_below_min`, but the brain's
existing demote logic looks at `avg_return_pct` and refuses to
demote.

## Brain integration (reuse, don't rewrite)

- `app/services/trading/learning.py` — existing realized-sync
  loop runs every cycle; the new handler hooks into the Phase 2
  dispatcher (per memory `reference_phase2_event_handlers.md`).
- `pattern_imminent_alerts.py` already filters
  `lifecycle_stage != 'promoted'`; demoting flips the alert
  filter automatically. No new gating logic needed.
- `scan_patterns` schema columns already exist — no migration.

## Acceptance criteria (per brief)

1. New handler `_handle_thin_evidence_demote` (or extend an
   existing demote handler) wired into the Phase 2 dispatcher.
2. Runs on the same trigger as other realized-sync handlers
   (per-cycle sweep or `live_trade_closed` event).
3. **Pattern 585 specifically gets demoted on next handler run**
   (verifiable via SQL post-deploy).
4. Demotion writes `lifecycle_stage='challenged'`,
   `demoted_at=NOW()`,
   `promotion_demote_reason='thin_evidence_low_realized_wr'`.
5. Status endpoint shows pattern 585 as challenged;
   `pattern_imminent_alerts` no longer fires for it.
6. Patterns 1011 and 1016 stay promoted (verifiable via SQL —
   they don't match the criteria; trade_count is well above 10
   and win_rate is well above 0.33).
7. Pattern 1047 stays challenged (already in that state, gate
   wouldn't re-touch it).
8. New helper-level tests in
   `tests/test_pattern_demote_on_thin_evidence.py`:
   - 5+ tests covering the matrix (each criterion in isolation,
     all-criteria, edge cases).
9. Existing pattern lifecycle tests still pass.
10. CC report at
    `docs/STRATEGY/CC_REPORTS/2026-05-08_f-pattern-demote-on-thin-evidence.md`.

## Constraints / do not touch

- **Hard Rule 1**: live-placement safety belts unchanged.
- **Hard Rule 5**: prediction-mirror authority untouched.
- **No re-promotion logic in this brief.** A pattern demoted by
  this rule stays challenged until ANOTHER brief re-promotes via
  OOS validation.
- **No threshold tuning of `projected_profit_below_min`** in the
  autotrader. That gate is correctly rejecting noise from pattern
  585; this brief reduces noise at source.
- **No deletion of pattern rows.** Demote only. History is
  load-bearing for forensics.
- **No magic numbers**: the four threshold values (10, 0.33, NULL,
  `'provisional_small_paths'`) are constants in the handler module
  with a docstring linking back to this brief.
- **Edit-tool truncation discipline (HARD).** Splice pattern.
  `wc -l + ast.parse` post-edit verification mandatory. See memory
  `reference_2026_05_07_widespread_truncation.md`.
- **Tests use `_test`-suffixed DB.**

## Out of scope

- Re-promotion path (separate future brief if needed).
- Other lifecycle gaps (e.g., demote on too-many-consecutive-losses,
  demote on regime-mismatch). Surface follow-up briefs if they
  recur.
- UI/dashboard changes.
- Backfill: this brief only changes future behaviour. The first
  handler run will demote pattern 585; that's the expected one-shot
  effect.
- The other queued briefs (`f-pdt-crypto-bypass-cleanup`,
  `f-autotrader-pdt-aware-exit-deferral`) remain parked.

## Sequencing

1. Truncation scan.
2. Read existing brain Phase 2 handlers to understand the
   dispatcher contract (per memory
   `reference_phase2_event_handlers.md`).
3. Add `_handle_thin_evidence_demote` (or extend existing
   demote handler).
4. Wire into the dispatch loop (registry pattern, same as other
   handlers).
5. Add tests.
6. Commit + push + CC report + mark NEXT_TASK DONE.

## Operator-side after CC ships

1. Pull + truncation scan.
2. `docker compose up -d --force-recreate brain-worker
   scheduler-worker`.
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
   Expected: 0.
5. **Verify patterns 1011 and 1016 stay promoted:**
   ```sql
   SELECT id, lifecycle_stage FROM scan_patterns WHERE id IN (1011, 1016);
   ```
   Expected: both `promoted`.

## Rollback plan

`git revert` the commit. The handler is additive; revert simply
stops auto-demoting. Pattern 585 stays challenged in the DB (its
`lifecycle_stage` doesn't auto-revert), which is the desired state
regardless. If the operator wants to manually re-promote 585,
that's a SQL UPDATE.

## What CC should do if it's unsure

1. **If the audit shows additional patterns matching the criteria
   beyond pattern 585** (current count: 1), surface in the CC
   report. The handler should still demote them — that's the
   correct behaviour — but the operator should know the blast
   radius before deploy.
2. **If the brain Phase 2 dispatcher cadence is `live_trade_closed`-
   triggered only and there are no recent live trades**, also wire
   into the per-cycle sweep so demotion happens on the next brain
   cycle (≤ 5 min) regardless of trade activity.
3. **If a follow-up architectural concern emerges** (e.g., the
   demote handler needs to be parameterized for future criteria,
   or the brain's promote-then-never-demote loop has a deeper
   gap), surface as a separate follow-up brief — don't expand
   scope of this one.
