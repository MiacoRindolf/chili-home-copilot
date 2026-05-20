# COWORK_REVIEW: position-identity-phase-1 soak verification

**Window:** 2026-05-04 03:29 UTC (mig 224 applied) → 2026-05-11 16:04 UTC (this audit).
**Run by:** scheduled task `phase1-soak-checkpoint-2026-05-11`.
**Verdict:** SOAK COMPLETE — mechanical parity holds, no blocking discrepancies. Architectural findings worth surfacing before Phase 2 promotion.

## Verdict in one line

Phase 1 soak SUCCEEDED on its exit criterion at the DB layer (14/15 open
positions parity-match the active trade row; 12/12 closed positions parity-match;
1/15 is the very trade-row-death survivor case Phase 1 was designed to
preserve). The broker-truth audit script (`scripts/audit_position_layer_parity.py`)
could not be run from outside the docker network and remains a manual-confirm
item for the operator before Phase 2 flip.

## Raw counts (host.docker.internal:5433 / chili)

| Metric                                  | Value |
|-----------------------------------------|------:|
| `trading_positions` rows                | 27    |
| `trading_positions` state=open          | 15    |
| `trading_positions` state=closed        | 12    |
| `trading_position_events` rows          | 1147  |
| event_type=opened                       | 27    |
| event_type=re_opened                    | 187   |
| event_type=closed                       | 199   |
| event_type=qty_change                   | 2     |
| event_type=sync_gap                     | 732   |

## Mechanical parity (DB-layer surrogate for the broker-truth audit)

Cross-check: each `trading_positions` row at `state='open'` should have a
single matching `trading_trades` row at `status='open'` with the same
(user_id, broker_source, ticker) and identical quantity + entry_price.

| Open positions    | 15 |
| ----------------- |---:|
| With matching open trade row, qty+entry identical | **14** |
| Without any open trade row                        |  **1** (pos=15 GRT-USD) |
| Quantity / entry-price mismatches on the 14 with-trade-row cases | **0** |

The single "no open trade row" case is GRT-USD (pos=15, qty=12376):

- Most recent `trading_trades` row for that ticker is id=1796, status=`closed`,
  exit_reason=`broker_reconcile_no_exit_price`.
- Position event history shows the broker has continued reporting the
  position the entire time — the position layer recorded **13 close→re_opened
  cycles** between 2026-05-06 and 2026-05-11, ending in `state='open'`.
- This is the exact failure mode the design doc § 1.1 enumerated: the close
  path closed the trade row even though the broker position never left.
  Pre-Phase-1, the only signal was the trade row's status. Now the position
  layer carries the broker-truth-aligned timeline.

Closed-position cross-check: all 12 closed positions in `trading_positions`
have ZERO open trade rows on the same (user, broker, ticker) — consistent.

## State-machine activity by position

The close→re_open cycle frequency reveals a sharp split between cohorts:

| Cohort | Count | Closes / re_opens per position | Interpretation |
|---|---|---|---|
| Crypto pairs (-USD, on `robinhood`) | 9 | 12–13 each | broker_reconcile_position_gone fires every ~12–16h per crypto position |
| Equity intermittent | 3 | 1 each (AIDX, CCCC, CRDL) | sporadic single flap |
| Equity stable | 3 | 0 (VFS, LOGI, OXSQ) | clean across full window |

Reading: Robinhood Crypto's `get_crypto_positions()` is dropping positions
from its response intermittently and the existing reconcile path is closing
the trade row each time. Phase 1's shadow-mode write path recorded the
re-appearance every cycle, preserving the timeline. This is exactly what the
position layer was supposed to do.

The 13-close-13-reopen symmetry on the crypto cohort (and equal counts
across 7 of the 9 positions) suggests a single shared root cause — likely
`get_crypto_positions()` returning an empty/incomplete list at the same
tick, then recovering. R32 already handles the empty-positions wipeout for
the autotrader path; the trade-row close path is a separate code path that
hasn't been hardened the same way.

## sync_gap event diagnostics

The brief's working assumption was "sync_gap events only from the
deploy-restart window 2026-05-04." That assumption did not hold:

- 732 sync_gap events distributed across 25 of 27 positions (only OXSQ and
  LOGI never fired one).
- Spread daily: 19 (2026-05-05) → 149 → 90 → 108 → 141 → 164 → 61 (today
  partial).
- Inter-arrival on the most-affected position (GRT-USD): min 262s, median
  ~117 min, max 21.5h. Median is well above the 240s threshold (so it's not
  a flapping threshold), but the persistent volume across a 6-day soak
  means broker_sync cycles are routinely missing on crypto positions and
  occasionally on equity ones.

These are diagnostic events, not parity failures — they record cycle gaps,
not value mismatches. But they're a real signal: broker_sync is unreliable
on the crypto surface and the position-layer is now making that visible.
Worth a separate brief once Phase 2 is shipped.

## Algo-trader lens

The crypto close→re_opened cycle is the more important finding than the
sync_gap noise. Each cycle is a real trade-row death-and-rebirth in
`trading_trades`. Pre-Phase-1 those events were invisible — Phase 1's job
was to make them visible, and it did. The 1 GRT-USD case where the position
ended up `state='open'` while the trade row stayed `closed` is the
clearest "Phase 1 demonstrably saved decision history" datapoint.

For Phase 2, this matters: when we backfill
`trading_execution_events.position_id`, we'll be linking fills across these
trade-row generations. Without that link, history walks via `trade_id`
miss any fill that happened against a now-closed prior trade_id. The
GRT-USD case alone has at least 1 prior trade_id (1796) whose fill history
is orphaned from the current position-of-record.

## Dev-architect lens

**What's good.**

- The shadow-mode contract held: zero `[phase1_position_event] write failed`
  lines, no tracebacks, no broker_sync regressions. Phase 1's promise of
  "shadow-mode never raises into the live path" is verified at 1147 events.
- Position-layer state-machine transitions are working correctly: opened →
  closed → re_opened cycles preserve qty/avg_price across the close.
- Backfill idempotency held across `ON CONFLICT DO NOTHING` — no
  duplicates appeared during the soak.
- `current_envelope_id` is NULL for every position (no Phase-1 wiring of
  envelope linkage; correct per § 8.1 scope).

**What's worth flagging.**

1. **Broker-truth audit script not run in this checkpoint.** The
   scheduled-task sandbox cannot reach Robinhood from outside docker. The
   DB-layer cross-check is a strong surrogate but does NOT replace the
   broker-API parity check. Operator should run
   `docker compose exec -T scheduler-worker python /app/scripts/audit_position_layer_parity.py`
   before any Phase 2 flip.

2. **sync_gap volume is not deploy-window noise.** 732 events across 6 days
   is persistent broker_sync flakiness, not initial settling. Whether to
   loosen the 240s threshold or harden the broker_sync surface is a
   separate decision — surfacing for operator awareness.

3. **The crypto trade-row death cohort needs Phase 2 to mean anything.**
   13 close-and-reopen cycles per crypto position is a lot of orphaned fill
   history. Phase 2's `position_id` backfill is the mechanism that gives
   those orphan fills back to the position. Until Phase 2 lands,
   `event_count == 0`-style inverse-reconcile workarounds remain
   conservative-by-necessity.

## Decisions for the operator

1. **Phase 1 soak: PASS at the DB-parity level.** The mechanical exit
   criterion is satisfied. Phase 2 can queue when the operator chooses.
2. **Run the broker-truth audit script in docker before flipping Phase 2.**
   It's the only check this scheduled-task could not perform.
3. **NEXT_TASK.md is NOT being overwritten.** The active brief is
   `f-brain-event-kind-unify` (Phase 1b of the adaptive-promotion
   initiative) — a different initiative the operator queued after Phase 1
   shipped. The position-identity Phase 2 brief is staged in QUEUED:
   `docs/STRATEGY/QUEUED/f-position-identity-phase-2-execution-events-position-id-backfill.md`.
   When the operator wants to promote it, copy it into NEXT_TASK.md.
4. **The crypto trade-row death-and-rebirth cycle (13×/position over 6
   days) is independently worth investigation.** Possibly a separate brief
   to harden `broker_reconcile_position_gone` against transient crypto-API
   list dropouts (R31/R32 cover the autotrader-side wipeout but not the
   trade-row close path). Surfacing only; not auto-queuing.

## Forward pointer

Phase 2 brief queued at:
`docs/STRATEGY/QUEUED/f-position-identity-phase-2-execution-events-position-id-backfill.md`

Operator action to start Phase 2: copy that file's contents into
`docs/STRATEGY/NEXT_TASK.md` with `STATUS: PENDING`, then run `claude`.
