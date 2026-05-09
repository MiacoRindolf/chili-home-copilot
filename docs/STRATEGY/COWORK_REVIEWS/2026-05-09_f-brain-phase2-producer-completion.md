# COWORK_REVIEW: f-brain-phase2-producer-completion

**Verdict:** Excellent work. The watchdog hook fired on the first
post-restart cycle (`[chili_brain_io] scheduled_market_snapshots_start
universe_build=1 snapshot_workers=10` at 05:36:18 UTC), proving the
operator's hypothesis was right and the architectural fix is
correct.

## Algo-trader lens

This brief vindicated operator's diagnosis. Stage 1's mapping table
showed:

- 8 of 10 handlers have HEALTHY producers (event-driven via
  `execution_hooks.py` for trade-close events,
  `backtest_queue_worker.py` for backtests, `trading_scheduler.py`
  for breakout outcomes).
- **2 handlers have BROKEN producers**: `market_snapshots_batch`
  (mining — the visible bottleneck) and `pattern_eligible_promotion`
  (the 5-jammed-patterns precondition bug).

CC's "watchdog hook" approach is the right architectural choice —
not a replacement for the broken APScheduler job, but a parallel
producer that ensures mining keeps flowing even if the original
scheduler stays dead. The per-minute dedupe key in
`emit_market_snapshots_batch_outcome` handles overlap if both
producers run. This is belt-and-suspenders done correctly:
additive, not subtractive.

The `pattern_eligible_promotion` bug correctly deferred to a
separate brief (`f-cpcv-gate-emit-anomaly-investigation`) — that's
a precondition-logic bug, not a producer-wiring bug. Different
class of fix.

## Dev-architect lens

CC's notable choices:

1. **Watchdog parallel-producer over APScheduler-restoration.** The
   original `trading_scheduler.py:262` job is intentionally NOT
   modified. Reasoning: the root cause of why it stopped 2026-05-05
   isn't pinned (could be `CHILI_SCHEDULER_ROLE`, `defer_while_learning`
   gate, or silent `run_scheduled_market_snapshots` failure), and
   debugging it would scope-creep. The watchdog ensures mining runs
   regardless of scheduler health.

2. **State reset on container restart is INTENTIONAL.**
   `_LAST_DISPATCH_MARKET_SNAPSHOTS_AT = 0.0` at module load so the
   first round after a restart fires immediately. Operator's restart
   workflow doesn't lose 15 minutes waiting for the interval gate.

3. **Settings read at call-time.** `_maybe_run_dispatch_market_snapshots`
   reads `enabled` and `interval_secs` per-call. Operator can flip
   `CHILI_BRAIN_DISPATCH_MARKET_SNAPSHOTS_ENABLED=False` and the next
   round respects it without restart. Good ergonomics.

4. **Integration test ran ALONE first (per the brief's hard
   requirement).** That's the discipline I begged for after tonight's
   three "tests-pass-but-system-fails" instances. CC delivered.

5. **361 insertions, 0 deletions.** Surgical scope. No existing code
   modified.

## Live verification

| Check | Expected | Actual |
|---|---|---|
| Settings loaded | enabled=True, interval=900s | ✓ |
| Helper importable | `_maybe_run_dispatch_market_snapshots` | ✓ |
| First post-restart fire | <1 min | ✓ (`scheduled_market_snapshots_start` log) |
| Pattern 585 still challenged | yes | ✓ |
| Patterns 1011/1016 still promoted | yes | ✓ |
| Pattern 1047 still challenged | yes | ✓ |
| Open crypto trades | 11-12 | 11 (one closed organically since last check — working as designed) |

The `brain_work_events` row will land when the snapshot batch
completes (the job started at 05:36:18 UTC; watch over the next
5-15 min for the row to appear, then a new row every ~15 min
thereafter).

## Safety check (operator's "don't break what works" directive)

Files modified by this brief:
- `app/config.py` (+24 lines, 2 new settings)
- `app/services/trading/brain_work/dispatcher.py` (+127 lines)
- `tests/test_brain_producer_wiring.py` (+207 lines, new file)

**Files NOT touched** (verified via `git diff --name-only`):
- `pdt_guard.py`, `portfolio_risk.py`, `broker_service.py` (risk
  + reconcile)
- `auto_trader.py`, `auto_trader_monitor.py` (entry + monitor)
- `crypto/exit_monitor.py`, `options/exit_monitor.py` (exit)
- `bracket_writer_g2.py`, `bracket_reconciliation_service.py`
  (bracket)
- `pattern_imminent_alerts.py`, `learning.py` (gate + sweep
  predicates)
- Migration files

Constraints honored:
- `CHILI_BRAIN_LEGACY_CYCLE_ENABLED` stays gated off ✓
- No gate threshold changes ✓
- No magic numbers (cadence settings-tunable) ✓

Failed candidates from the new mining producer will sit at
`lifecycle_stage='candidate'` and don't fire alerts. Zero impact
on tonight's working trades or the entry-decision pipeline.

## What's left

### Confirmed-shipped fixes today (this session, end-to-end)

| Layer | Brief | Commit | Status |
|---|---|---|---|
| Phase A: PDT count filter | `f-pdt-count-broker-confirmed-only` | `60c26f8` | live |
| Phase B: wipeout-burst breaker | `f-equity-broker-reconcile-wipeout-protection` | `bc1a0f3` | live |
| Phase C: partial-list streak guard | `f-equity-reconcile-partial-list-guard` | `1d6cf3b` | live |
| Phase D: pattern-demote sweep | `f-pattern-demote-on-thin-evidence` | `dfb39f0` | live |
| Phase D wiring fix | `f-pattern-demote-sweep-wiring-fix` | `cc86370` | live |
| Phase E: stale-trade closer | `f-crypto-stale-trade-closer` | `1497c1e` (revert) | REVERTED |
| Bracket-writer crash | `f-phase-e-revert-and-bracket-writer-crash-fix` | `3be20ea` + `fa0d8d6` | live |
| **Brain Phase 2 producer completion (mining)** | this brief | **`7a3a054`** | **live, fired** |

### Remaining queued briefs (prioritized)

1. **`f-cpcv-gate-emit-anomaly-investigation`** — investigate the
   `check_promotion_ready` precondition that blocks 5 jammed
   patterns. LOW risk; cheapest discovery win after mining is
   restored.
2. **`f-pattern-oos-revalidation`** — re-validate already-promoted
   patterns via OOS path. MEDIUM risk; CONDITIONAL on this brief
   producing fresh candidates.
3. **`f-crypto-pattern-discovery-expansion`** — add crypto-specific
   patterns at 1d/1h/4h timeframes. MEDIUM risk; CONDITIONAL on
   #1 + #2.
4. **`f-crypto-reconcile-architectural-rebuild` Phase 1** — auth
   liveness + typed result + R32 gate. HIGH structural impact;
   multi-week scope.

## Final note for tonight

The operator's intuition tonight cracked the audit's most
important finding: **the migration was incomplete on the
production side.** CC's audit table makes the gap concrete (8/10
handlers have healthy producers; 2 are broken). The watchdog
approach restored mining without touching the gate, the entry
side, or any working trade — exactly the "enhance, don't break"
discipline operator asked for.

End-of-day reliability state secured. Mining producer back
online. Pattern lifecycle wiring durable. Crypto reconciler
chain solid (Phases A+B+C). Bracket-writer crash dead. SOL+DOT
closed at target with realized +$99.55.

29 commits today. Tomorrow's first NEXT_TASK is operator's call:
either `f-cpcv-gate-emit-anomaly-investigation` (cheap discovery
win) or the architectural rebuild Phase 1 (deeper structural fix
for the silent-empty broker auth that caused tonight's Phase E
mistake).

Stopping for the night.
