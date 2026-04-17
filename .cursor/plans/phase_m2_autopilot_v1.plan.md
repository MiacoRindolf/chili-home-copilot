---
status: completed_live
title: Phase M.2-autopilot - auto-advance engine for pattern x regime slices
parent_plan: phase_m2_pattern_regime_authoritative_v1
phase_id: phase_m2_autopilot
depends_on_phase: phase_m2 (completed_shadow_ready)
created: 2026-04-17
frozen_at: 2026-04-17
rollout_mode_default: off
target_rollout_mode: on
user_approved_scope: FULL (shadow -> compare -> authoritative end-to-end)
user_approved_reports: BOTH (ops log + diagnostics endpoint)
---

# Phase M.2-autopilot — Auto-advance engine

Hands-off orchestration for the three M.2 slices (`tilt`, `promotion`,
`killswitch`). Operator said "I will forget to flip these" and asked
for full automation including the governance approval row.

## Objective

Remove the manual per-slice flip step. After this phase ships,
progression `shadow -> compare -> authoritative` happens automatically
per slice when evidence gates pass, and rolls back automatically if
anomaly gates trip. Operator never has to remember to flip an env
flag.

## Non-negotiables

1. **Additive-only.** Does not alter any M.1 / M.2 decision contract.
   Only adds: a runtime-mode override table, an autopilot audit table,
   a shared "what mode is this slice in" accessor, and a scheduled
   evaluator.
2. **Order lock preserved.** Cutover order is frozen from M.2:
   `M.2.a (tilt) -> M.2.c (killswitch) -> M.2.b (promotion)`. A
   slice cannot advance past the stage of the prior slice until the
   prior slice is fully authoritative.
3. **Never skips a stage.** `off -> shadow -> compare -> authoritative`,
   one step per evaluation at most.
4. **Rate-limited.** At most **one advance per slice per day**. Prevents
   cascade flips if something is off.
5. **Auto-revert is one stage only.** If an anomaly trips, flip down
   one stage (authoritative -> compare, or compare -> shadow). Never
   jumps to off except via the master kill.
6. **Master kill.** `BRAIN_PATTERN_REGIME_AUTOPILOT=off` in env halts
   the scheduler entirely. Existing slice modes remain unchanged.
7. **Kill-switch irreversible action preserved.** Rolling M.2.c back
   to compare does not un-quarantine patterns already quarantined.
8. **scan_status frozen contract unchanged.** `release=={}`, key order
   intact.
9. **Release blocker.** Authoritative mode cannot be active without a
   live approval row; the release blocker catches violations.

## Ship list

1. **Migration 146** `146_m2_autopilot`:
   - `trading_brain_runtime_modes` (slice_name VARCHAR PK, mode, updated_at, updated_by, reason, payload_json)
   - `trading_pattern_regime_autopilot_log` (append-only audit: id, as_of_date, slice_name, event, from_mode, to_mode, gates_json, reason_code, approval_id, ops_log_excerpt)
2. **ORM models** `BrainRuntimeMode`, `PatternRegimeAutopilotLog`.
3. **Config flags** (default values chosen conservatively):
   - `brain_pattern_regime_autopilot_enabled: bool = False`
   - `brain_pattern_regime_autopilot_kill: bool = False`
   - `brain_pattern_regime_autopilot_cron_hour: int = 6`
   - `brain_pattern_regime_autopilot_cron_minute: int = 15`
   - `brain_pattern_regime_autopilot_weekly_cron_hour: int = 9`
   - `brain_pattern_regime_autopilot_weekly_cron_dow: str = "mon"`
   - `brain_pattern_regime_autopilot_shadow_days: int = 5`
   - `brain_pattern_regime_autopilot_compare_days: int = 10`
   - `brain_pattern_regime_autopilot_min_decisions: int = 100`
   - `brain_pattern_regime_autopilot_tilt_mult_min: float = 0.85`
   - `brain_pattern_regime_autopilot_tilt_mult_max: float = 1.25`
   - `brain_pattern_regime_autopilot_promo_block_max_ratio: float = 0.10`
   - `brain_pattern_regime_autopilot_ks_max_fires_per_day: float = 1.0`
   - `brain_pattern_regime_autopilot_approval_days: int = 30`
4. **Ops-log module** `pattern_regime_autopilot_ops_log.py` with
   prefix `[pattern_regime_autopilot_ops]` and events
   `autopilot_advance`, `autopilot_hold`, `autopilot_revert`,
   `autopilot_weekly_summary`, `autopilot_killswitch_disabled`,
   `autopilot_gate_fail`.
5. **Shared runtime-mode override**
   `app/services/trading/runtime_mode_override.py`:
   - `get_runtime_mode_override(slice_name) -> Optional[str]` (reads
     from `trading_brain_runtime_modes`; falls back to None if no row).
   - `set_runtime_mode_override(db, slice_name, mode, updated_by, reason, payload)`
     (UPSERT).
   - `clear_runtime_mode_override(db, slice_name)` (for master kill
     rollback).
   - Uses a process-local 30-second TTL cache to keep `_raw_mode`
     cheap on the hot path.
6. **Modify the 3 slice `_raw_mode()` helpers** to consult the DB
   override first; fallback to `settings.brain_pattern_regime_*_mode`.
   No other slice code changes.
7. **Pure model** `pattern_regime_autopilot_model.py`:
   - Dataclasses: `SliceEvidence`, `GateEvaluation`, `AutopilotDecision`.
   - `evaluate_slice_gates(slice, evidence, config, order_lock_state)` pure function.
   - `compute_order_lock_state(all_slice_states)` pure helper.
   - Returns one of: advance / hold / revert / blocked_by_order_lock.
8. **Service** `pattern_regime_autopilot_service.py`:
   - `gather_slice_evidence(db, slice_name)` — reads decision-log
     tables + ops log-derived counters + current mode from
     runtime_mode_override.
   - `evaluate_all_slices(db)` — loops 3 slices, applies pure model,
     writes audit rows, writes mode overrides for advances/reverts,
     inserts governance approval rows for shadow/compare->authoritative.
   - `weekly_summary(db)` — writes a single ops line with per-slice
     stage / days-in-stage / last-advance-at.
   - `diagnostics_summary(db)` — frozen-shape dict for the endpoint.
9. **Scheduler jobs** in `trading_scheduler.py`:
   - `pattern_regime_autopilot_daily` cron (default 06:15, gated by
     `BRAIN_PATTERN_REGIME_AUTOPILOT_ENABLED`).
   - `pattern_regime_autopilot_weekly_summary` cron (default Mon 09:00).
10. **Diagnostics endpoint** `GET /api/trading/brain/m2-autopilot/status`
    (frozen shape: enabled, kill, next_eval_at, slices.{tilt,promotion,killswitch}.{stage, days_in_stage, last_advance_at, last_gate_eval, would_advance_at}).
11. **Unit tests** `tests/test_pattern_regime_autopilot_pure.py`
    (~25 tests: order-lock, per-slice advance gates, revert, ratelimit,
    stage math, safety envelopes per slice).
12. **Docker soak** `scripts/phase_m2_autopilot_soak.py` (~20 checks:
    migration, tables, flags, ops-log, runtime-mode override helper,
    pure-model happy path + each fail, service off-mode short-circuit,
    diagnostics shape, M.2 additive-only invariants).
13. **Release-blocker** `scripts/check_pattern_regime_autopilot_release_blocker.ps1`
    (gates: authoritative without approval = fail; revert without
    anomaly event = fail; ops log clean; diagnostics healthy).
14. **Docs** `docs/TRADING_BRAIN_PATTERN_REGIME_M2_AUTOPILOT_ROLLOUT.md`
    (scope, frozen contract, rollback, master-kill, evidence gates,
    per-slice envelopes, FAQ).
15. **Flip `.env`** `BRAIN_PATTERN_REGIME_AUTOPILOT_ENABLED=true`
    (after all tests + soak + blocker pass), recreate scheduler-worker,
    verify scheduler registered both jobs + diag reports enabled=true.

## Evidence gates (frozen)

### shadow -> compare (per slice)

- `days_in_stage >= brain_pattern_regime_autopilot_shadow_days` (5)
- Release blocker exit 0 within last 24h (derived from
  absent `*_refused_authoritative` events + absent
  `*_applied mode=authoritative approval_live=false` events)
- `total_decision_rows >= brain_pattern_regime_autopilot_min_decisions`
  (100) across the window, proving evidence is flowing
- Diagnostics endpoint responded within last 24h with `ok=true` and
  `mode=shadow`
- `scan_status` frozen contract intact (lightweight check: GET +
  assert keys)

### compare -> authoritative (per slice)

All the above, plus slice-specific safety envelope:

| Slice | Envelope |
|-------|----------|
| tilt | mean `would_apply_multiplier` in [0.85, 1.25] over the compare window |
| promotion | `consumer_block_ratio` <= 0.10 of baseline-allows |
| killswitch | mean `would_quarantine_per_day` <= 1.0 across live patterns |

If the envelope passes: **auto-insert** a governance approval row with
`action_type` = slice action type, `status='approved'`,
`decision='allow'`, `decided_at=NOW()`,
`expires_at=NOW() + interval '30 days'`,
`notes='auto-advance-policy: compare window clean, envelope met'`.
Record the approval id in the autopilot audit row.

### Auto-revert (any slice, any stage)

Evaluated first on every tick, before advance logic:

- If current mode == authoritative but `has_live_approval` is False:
  revert to compare (same as M.2 fail-closed; belt-and-suspenders).
- If any `*_refused_authoritative` event appeared in last 24h: revert
  to compare.
- If diagnostics endpoint unreachable for > 1h or release blocker
  exit != 0: revert one stage.
- Reverts do NOT un-quarantine kill-switched patterns.

### Order lock

A slice may only advance **beyond shadow** if the prior slice in the
cutover sequence has reached **authoritative**:

- M.2.a: no lock (first slice).
- M.2.c: may advance beyond shadow only if M.2.a is authoritative.
- M.2.b: may advance beyond shadow only if M.2.c is authoritative.

Slices may enter shadow (from off) in any order; the autopilot does
not downgrade from shadow if a following slice advances.

### Rate limit

At most one advance per slice per UTC day. If multiple gates pass on
the same day, only the first advance applies.

## Calendar (all-green case)

| Day | Action |
|-----|--------|
| 0 (today) | autopilot_enabled=true; all 3 slices currently in `shadow`. |
| +5 BD | M.2.a shadow->compare. |
| +15 BD | M.2.a compare->authoritative + approval row inserted. |
| +20 BD | M.2.c shadow->compare (order lock: waits for M.2.a authoritative). |
| +30 BD | M.2.c compare->authoritative + approval row. |
| +35 BD | M.2.b shadow->compare. |
| +45 BD | M.2.b compare->authoritative + approval row. M.2 fully cut over. |

## Rollback

- **Master kill**: set `BRAIN_PATTERN_REGIME_AUTOPILOT_ENABLED=false`
  (or set `BRAIN_PATTERN_REGIME_AUTOPILOT_KILL=true`). Recreate
  scheduler-worker. No more advances. Existing slice modes stay put.
- **Per-slice hard revert**: write a row to `trading_brain_runtime_modes`
  with the desired mode and `updated_by='operator-manual-revert'`.
  Autopilot will see it on next tick and continue from there.
- **Revoke approval**: `UPDATE trading_governance_approvals SET
  status='revoked', expires_at=NOW() WHERE id=...`. Slice auto-reverts
  to compare on next tick via `has_live_approval=False` gate.

## Done-when

- All unit tests green.
- Docker soak all checks pass.
- Release blocker exit 0.
- Scheduler reports `pattern_regime_autopilot_daily` + weekly_summary
  jobs registered with mode=enabled.
- Diagnostics endpoint returns frozen shape with
  `enabled=true, kill=false`, three slices listed at `stage=shadow`.
- scan_status frozen contract byte-equal.

## Non-goals

- No UI.
- No per-user settings.
- No auto-advance for phases outside M.2 (A/C/D/F/G/H/I/J/K/L/M.1 stay
  as-is).
- No changes to M.1 or M.2 decision contracts (pure models unchanged).
- No auto-promotion of patterns (M.2.b only blocks; unchanged).
- No auto-un-quarantine (M.2.c stays irreversible; unchanged).

## Self-critique (ahead of execution)

1. **The autopilot is effectively approving its own gates.** Default
   envelope values are deliberately conservative; any relaxation
   requires a new plan. The weekly summary gives the operator visibility
   to notice bad trends.
2. **DB-override read on hot path.** Mitigated by 30s TTL cache.
   Every slice call is still O(1) lookup cached.
3. **Clock skew.** `days_in_stage` uses UTC date diff so timezone
   flips don't double-advance. Rate limit uses UTC day as well.
4. **Simultaneous manual + auto.** If an operator flips `.env`
   manually while autopilot is on, the DB override wins. Operator can
   clear the override to restore env-driven mode. Logged in audit.
5. **Approval row auto-insert is new surface.** Release blocker
   specifically checks for `auto-advance-policy` approvals missing
   the `decision='allow'` gate. Every auto-inserted approval is
   auditable with the `approved_by='auto-advance-policy'` tag.

## Closeout (2026-04-17, completed_live)

### Executed

1. All pure model + service + scheduler + diagnostics + release-blocker
   + soak + doc artifacts shipped per the frozen plan.
2. `BRAIN_PATTERN_REGIME_AUTOPILOT_ENABLED=true` flipped in `.env`;
   `chili` + `brain-worker` + `scheduler-worker` all recreated.
3. Scheduler-worker startup log shows both jobs registered:
   `Pattern x regime autopilot tick (06:15)` and
   `Pattern x regime autopilot weekly (mon 09:00)`.
4. `/api/trading/brain/m2-autopilot/status` returns the frozen shape
   with `enabled=true, kill=false` and all three slices at
   `stage=shadow`, `days_in_stage=0`, `approval_live=false`,
   `override_present=false`.
5. Manual `run_autopilot_tick()` ran cleanly end-to-end:
   - `tilt` -> `hold` (gates_not_ready: days_in_stage, total_decisions,
     scan_status_frozen)
   - `killswitch` -> `blocked_by_order_lock` (prior slice not
     authoritative)
   - `promotion` -> `blocked_by_order_lock` (prior slice not
     authoritative)
6. Release blocker PS1 -> exit 0 against live logs AND live diagnostics.
7. Order lock (tilt -> killswitch -> promotion) and rate limit enforced
   exactly as specified by the pure model.

### Observed calendar (all-green projection)

First real advance (tilt shadow -> compare) earliest ~5 business days
out, pending `brain_runtime.release == {}` scan_status gate and 100+
M.2.a shadow decisions accumulated. Operator does not need to do
anything for the remainder of the rollout unless the release blocker
reports a violation or the weekly Monday 09:00 summary flags a
regression.

### Known limitations

- Weekly summary emits via ops log only; no email/SMS. Operator must
  check logs on Monday morning or query diagnostics.
- Autopilot disables itself if `brain_pattern_regime_autopilot_kill`
  is flipped to `true`; recreate scheduler-worker afterwards to
  guarantee the job no-ops.
- 30s TTL cache on runtime overrides means a manual DB override takes
  up to 30 seconds to take effect at a running worker (acceptable
  for daily cadence).

### Deferred (out of scope for this phase)

- UI surface for operator visibility (scope: dashboard-level).
- Multi-phase autopilot (M.3+ slices will repeat the pattern).
- Auto-un-quarantine (explicitly out of scope; stays irreversible).
