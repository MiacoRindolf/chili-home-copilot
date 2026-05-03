# NEXT_TASK: audit-missing-stop-emergency-repair

STATUS: DONE

## Goal

Eliminate the live position risk surfaced by the 2026-05-03 audit: seven open Robinhood equity trades have no broker stop order because their bracket intents are parked at `terminal_reject`, and the reconciler's `state_gated_skip` path (added 2026-05-01 as part of FIX 51-53) has no escape valve when the position is still open.

Success means:

1. **The seven existing positions are protected** — by operator triage (close, manual re-arm, or controlled writer repair). This is human action, not code.
2. **A new code path exists** for `open trade + missing_stop + intent_state=terminal_reject`, gated behind a feature flag defaulted OFF, that distinguishes phantom positions from real exposure and re-arms the latter through the existing FIX-51 broker-qty-aware writer.
3. **Regression test in place** that exercises the new path against all three branches (phantom, real exposure with sufficient broker qty, real exposure with capped broker qty).
4. **Alerting wired** so the next time this state appears the operator is paged before 24-48h elapse silently.
5. **Broker-sync reconciliation reports `missing_stop=0`** for open Robinhood equities after triage + flag flip.

This task ships **code + ops triage**, not analysis. Deliverable is `docs/STRATEGY/CC_REPORTS/<date>_audit-missing-stop-emergency-repair.md`.

## Why now

The 2026-05-03 audit (`docs/AUDITS/2026-05-03.md`) and the Cowork reevaluation (`docs/STRATEGY/COWORK_REVIEWS/2026-05-03_audit-reevaluation.md`) both flag this as the highest-priority outstanding risk.

Concrete state at audit time:

| trade_id | ticker | open since | broker_stop_order_id | intent_state |
|---|---|---|---|---|
| 1822 | VFS | 2026-05-01 17:50 | NULL | terminal_reject |
| 1821 | TLS | 2026-05-01 17:50 | NULL | terminal_reject |
| 1818 | IMTX | 2026-05-02 16:51 | NULL | terminal_reject |
| 1816 | ELTX | 2026-05-01 18:03 | NULL | terminal_reject |
| 1814 | CRDL | 2026-05-01 17:50 | NULL | terminal_reject |
| 1813 | CCCC | 2026-05-01 17:50 | NULL | terminal_reject |
| 1812 | AIDX | 2026-05-01 17:50 | NULL | terminal_reject |

Broker-sync logs show every 2-min sweep classifying these as `missing_stop` and skipping repair with `reason=state_terminal_reject`. The state gate at `app/services/trading/bracket_reconciliation_service.py:668-682` was added 2026-05-01 to stop the SELL_STOP rejection storm; it does that job but lacks an escape valve for "open position + missing stop" — the failure mode currently in production.

The previously queued `f8b-verification-soak-3` is preserved at `docs/STRATEGY/QUEUED/f8b-verification-soak-3.md` for re-promotion on/after 2026-05-04 16:30 UTC. It is a data-window-gated analysis task, so deferring it costs nothing.

## Step 1 — Operator triage (human action, BEFORE code deploy)

Claude Code begins by enumerating the current state from the database (read-only) and presenting it to the operator. The operator decides per-position:

- **Close** the position via the broker (manual sell), OR
- **Manually re-arm** the stop via the broker UI (operator clicks), OR
- **Mark for controlled writer repair** (the new code path will handle it after deploy).

For each decision, Claude Code records the chosen action in the CC_REPORT. Claude Code does NOT close positions or place broker orders during this step. Read-only DB probes only.

Recommended SQL (read-only):

```sql
SELECT t.id AS trade_id, t.ticker, t.status, t.broker_source,
       bi.id AS intent_id, bi.state AS intent_state,
       bi.broker_stop_order_id, bi.stop_price,
       bi.quantity AS local_qty, bi.updated_at
FROM trading_trades t
JOIN trading_bracket_intents bi ON bi.trade_id = t.id
WHERE t.status = 'open'
  AND t.broker_source = 'robinhood'
  AND bi.broker_stop_order_id IS NULL
  AND bi.state = 'terminal_reject'
ORDER BY bi.updated_at;
```

This list may have shifted since the audit (positions closed or new ones surfaced). Operate on the live list, not the audit's snapshot.

## Step 2 — Code: emergency-repair path

### Where the change lives

- `app/services/trading/bracket_reconciliation_service.py` — add an emergency-repair branch ABOVE the existing `state_gated_skip` at lines 668-682. Do NOT remove the existing gate; the new branch is *additive*.
- `app/services/trading/bracket_writer_g2.py` — reuse `place_missing_stop` and its FIX-51 broker-qty-aware logic. No new writer needed.
- `app/config.py` (or equivalent) — add the feature flag `CHILI_BRACKET_MISSING_STOP_REPAIR_ENABLED`, defaulting `False`.

### Decision logic for the new branch

The new branch fires when ALL of the following are true:

- `decision.kind == "missing_stop"`
- `local.intent_state == "terminal_reject"`
- The associated trade is `status == 'open'`
- `CHILI_BRACKET_MISSING_STOP_REPAIR_ENABLED` is True (the operator-controlled flag)

Three sub-branches based on `BrokerView.position_quantity`:

1. **`broker.available == False` or `broker_qty is None`** → skip silently this sweep, retry next. Mirror FIX 51's `skipped_broker_qty_unknown`. Do NOT escalate; broker-down sweeps are not actionable.

2. **`broker_qty == 0` → phantom open trade.** Mark the trade `closed` with `closed_reason='phantom_after_terminal_reject'` and the bracket_intent `state='closed'` with `closed_reason='phantom'`. Emit a CRITICAL log: `[bracket_reconciliation] EMERGENCY-REPAIR phantom_close trade=<id> intent=<id> ticker=<>`. Do NOT place a broker stop — the position is gone.

3. **`broker_qty > 0`** → real exposure. Place the stop via the existing FIX-51 path (`place_missing_stop` with `placement_qty = min(local_qty, broker_qty)`). Wrap in a per-intent attempt counter so the new path executes at most once per intent per 6h: if the placement rejects again, the new path re-locks immediately and reverts to `state_gated_skip` until manual operator intervention. Emit a CRITICAL log on entry and another on success/rejection.

Per-intent attempt persistence: a new column `terminal_reject_repair_last_attempt_at TIMESTAMPTZ` on `trading_bracket_intents` (migration 222 — verify this is the next free ID via `scripts/verify-migration-ids.ps1`). The 6h throttle is checked against `now() - terminal_reject_repair_last_attempt_at`. Use `_dynamic_throttle_seconds()` if such a helper already exists in the brain; otherwise the constant goes in module-level config (NOT inline) and is named.

### Audit emission

Every emergency-repair attempt — phantom, success, rejection-relock — must write a `trading_bracket_writer_actions` row (or whatever audit table the FIX-51 path already uses) with the new writer name `emergency_terminal_reject_repair` so the funnel-accounting and operator postmortem queries pick it up.

### Operator alert wiring

When the new branch fires, also write to the alerting channel the brain already uses for CRITICAL operational events. Reuse the existing alerting plumbing — do not invent a new one. If the brain currently has no alerting channel for bracket-reconciliation events, flag this back in Open Questions and proceed without it for now (the CRITICAL log line is the minimum bar).

## Step 3 — Regression tests

Add `tests/test_bracket_emergency_terminal_reject_repair.py` covering:

1. **Phantom branch.** Seed: open trade, terminal_reject intent, broker_qty=0. Assert: trade closed, intent closed, no broker order placed, audit row written, throttle column set.
2. **Real-exposure success.** Seed: open trade, terminal_reject intent, broker_qty=local_qty=10, FIX-51 writer mock succeeds. Assert: stop placed at min(local, broker), trade stays open, intent state transitions to whatever `place_missing_stop` sets on success, audit row written, throttle set, CRITICAL log line emitted.
3. **Real-exposure capped.** Seed: open trade, terminal_reject intent, local_qty=20, broker_qty=10. Assert: stop placed at qty=10 with the warning log line.
4. **Real-exposure rejection-relock.** Seed: open trade, terminal_reject intent, broker_qty>0, FIX-51 writer mock returns `ok=False`. Assert: throttle set, intent state stays `terminal_reject` (no progression), next call within 6h returns `state_gated_skip` and does NOT retry.
5. **Throttle expiry.** Same seed as (4) but advance the clock past 6h. Assert: new attempt fires.
6. **Flag OFF.** Seed: open trade, terminal_reject intent, broker_qty>0, flag False. Assert: returns `state_gated_skip`, no broker call, no audit row.
7. **Broker unavailable.** Seed: as above but `broker.available == False`. Assert: skipped silently, no audit row, no throttle bump.

All tests use `chili_test` per the conftest guard. No mocks-of-mocks — use the real `_invoke_writer_for_decision` entry point with a stubbed `place_missing_stop`.

## Step 4 — Deploy + verify

1. Land the migration and code on the dirty worktree's existing branch (or `git checkout -B fix/audit-missing-stop-emergency-repair` if the worktree is too dirty to commit cleanly — use judgment).
2. **Run the regression tests against `chili_test` and report results in the CC_REPORT.**
3. Restart the affected workers (`broker-sync-worker` is the one that drives the reconciler). `docker compose restart broker-sync-worker`.
4. **Operator triage of the seven existing positions** must be complete before flipping the flag. The CC_REPORT should show the operator-decided action per position, with timestamps if available.
5. Flip `CHILI_BRACKET_MISSING_STOP_REPAIR_ENABLED=1` (operator action — Claude Code requests, doesn't self-authorize). Restart broker-sync-worker.
6. Watch one full sweep cycle (~2 minutes) and capture `[bracket_reconciliation] EMERGENCY-REPAIR ...` lines in the CC_REPORT.
7. Verify `missing_stop=0` for open Robinhood equities via the same SQL as Step 1.

## Brain integration (reuse, don't rewrite)

- `app/services/trading/bracket_writer_g2.place_missing_stop` — the FIX-51 broker-qty-aware writer. Reuse, do not duplicate.
- `app/services/trading/bracket_reconciliation_service._invoke_writer_for_decision` — the existing entry point. Add the new branch inside it, above the existing `state_gated_skip` at lines 668-682.
- The `BrokerView` struct already exposes `available`, `position_quantity` — use them; do not query the broker again.
- The `trading_bracket_writer_actions` audit table (or whatever FIX 51 writes to) — reuse for the new writer name.
- Migration framework in `app/migrations.py` — add `_migration_222_terminal_reject_repair_throttle()` per the existing pattern. Idempotent. Check with `scripts/verify-migration-ids.ps1` before commit.

## Constraints / do not touch

- **Do not modify the live-fast-path safety belts** (8 belts, three flags). PROTOCOL Hard Rule 1.
- **Do not remove the `state_gated_skip` branch** at lines 668-682. The new branch is additive.
- **Do not flip `CHILI_BRACKET_MISSING_STOP_REPAIR_ENABLED` to default True.** Default OFF, operator flips manually after triage.
- **Do not place broker orders during Step 1 (operator triage).**
- **Do not bypass the `chili_test` guard in conftest.py.** PROTOCOL Hard Rule 5.
- **Migration ID must not collide.** Run `scripts/verify-migration-ids.ps1` before commit.
- **No magic numbers.** The 6h throttle goes in named module-level config or a brain function — not inline.

## Out of scope

- Fixing the `terminal_reject` *root cause* — i.e., why Robinhood was rejecting the original SELL_STOP submissions. That's a separate investigation; the emergency-repair path handles the symptom.
- Unsupported-crypto pre-filter (audit HIGH #4). Next task after this.
- Venue-truth shadow-log wiring (audit HIGH #2). Queued behind the soak-3.
- Pullback-exit signal-specific cold-start hold (audit HIGH #3). Queued.
- CHECK-constraint migrations (audit MEDIUM #7-8). Bundle later.
- `fetch_ohlcv_batch` crypto-skip parity (audit MEDIUM #5). Queued.
- Any change to F8b allowlist or fast-path calibration tables. The fast-path is paper and orthogonal.

## Success criteria

1. Migration 222 (or next-free ID) added, registered in `MIGRATIONS`, idempotent, applies cleanly on app startup. `verify-migration-ids.ps1` passes.
2. New emergency-repair branch lives in `bracket_reconciliation_service.py` above the existing `state_gated_skip`. Existing gate untouched.
3. `tests/test_bracket_emergency_terminal_reject_repair.py` exists and all seven scenarios pass.
4. Operator triage completed for the seven positions (or however many remain at task start). Per-position action recorded in the CC_REPORT.
5. Feature flag `CHILI_BRACKET_MISSING_STOP_REPAIR_ENABLED` exists, defaults False, documented in the CC_REPORT.
6. After flag flip + worker restart, the SQL probe in Step 1 returns 0 rows. CC_REPORT shows the verification.
7. CC_REPORT written at `docs/STRATEGY/CC_REPORTS/<date>_audit-missing-stop-emergency-repair.md` per PROTOCOL format. One commit (or tight series), pushed.

## Open questions for Cowork (surface in your report only if relevant)

1. **Should the 6h throttle be configurable per-broker?** Robinhood-specific failure modes might differ from Coinbase if/when the same path is extended. Default uniform; flag if the implementation surfaces a reason for differentiation.
2. **Is there an existing alerting channel for bracket-reconciliation CRITICAL events?** If yes, name it; if no, the CRITICAL log line is the minimum bar and a follow-up alerting wiring task should be queued.
3. **If broker_qty > local_qty (broker has MORE shares than the bracket_intent recorded)** — the FIX-51 path covers this case (`min(local_qty, broker_qty) = local_qty`), but it leaves residual unprotected exposure. Surface this if observed; it's a separate bug class (likely manual buy or reconciliation drift) that should not be silently absorbed.
4. **Phantom-detection vs orphan-intent cleanup overlap.** FIX-51's comments mention an "orphan-intent cleanup path" responsible for clearing bracket_intent rows when the position is gone. If the new phantom-close branch overlaps that path's responsibilities, surface the boundary.

## Rollback plan

- **Code rollback:** Revert the commit. Migration 222 is additive (new column with default NULL); leaving the column in place is safe.
- **Flag rollback:** Set `CHILI_BRACKET_MISSING_STOP_REPAIR_ENABLED=0` and restart `broker-sync-worker`. The new branch becomes a no-op; behavior reverts to pre-task `state_gated_skip` for terminal-reject intents. Open positions remain protected by whatever Step 1 triage decided.
- **Migration rollback:** None required — the column is nullable and unused once the flag is off. If aggressive cleanup is desired later, write a follow-up migration to drop the column.
