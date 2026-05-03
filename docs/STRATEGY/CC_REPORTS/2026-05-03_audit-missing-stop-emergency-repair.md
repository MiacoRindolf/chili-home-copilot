# CC_REPORT: audit-missing-stop-emergency-repair

## What shipped

- **Commit `ef50d3f`** — `fix(bracket): emergency-repair branch for terminal_reject + open trade` (code + migration + tests).
- **This commit** — `docs(strategy): audit-missing-stop-emergency-repair CC report + flag flip`. Adds `CHILI_BRACKET_MISSING_STOP_REPAIR_ENABLED=1` to the `broker-sync-worker` service in `docker-compose.yml`, marks `NEXT_TASK.md` DONE, ships this report.
- Files touched: `app/migrations.py`, `app/config.py`, `app/services/trading/bracket_reconciliation_service.py`, `tests/test_bracket_emergency_terminal_reject_repair.py` (new), `docker-compose.yml`.
- Migrations added: **222** — `_migration_222_bracket_intent_terminal_reject_repair_throttle()`. Adds nullable `terminal_reject_repair_last_attempt_at TIMESTAMP` to `trading_bracket_intents`. Idempotent.

## Step 1 — Operator triage (per-position decisions)

Operator chose **option (C) controlled writer repair** for all 7 positions. No manual broker actions taken; the new emergency-repair code path handled all 7 on the first sweep after flag flip.

| trade_id | ticker | qty | Triage decision | Realized outcome |
|---|---|---|---|---|
| 1812 | AIDX | 150 | C — controlled repair | Writer SKIPPED — `covered_by_existing_sell` (broker had working sell for 150/150) |
| 1813 | CCCC | 150 | C — controlled repair | Writer SKIPPED — `covered_by_existing_sell` (150/150) |
| 1814 | CRDL | 200 | C — controlled repair | Writer SKIPPED — `covered_by_existing_sell` (200/200) |
| 1816 | ELTX |  25 | C — controlled repair | **Writer OK — new broker stop placed** order=`69f7c5b8-7e15-4176-a31f-1544696055d5` qty=25 stop=$11.0584 verified=queued |
| 1818 | IMTX | 100 | C — controlled repair | New branch did NOT fire — classifier returned `kind=agree` (broker has working stop). Continues under `state_gated_skip` (no-op) |
| 1821 | TLS  | 100 | C — controlled repair | Writer SKIPPED — `covered_by_existing_sell` (100/100) |
| 1822 | VFS  |  50 | C — controlled repair | Writer SKIPPED — `covered_by_existing_sell` (50/50) |

## Step 2 — Code

### Migration 222
Adds `terminal_reject_repair_last_attempt_at TIMESTAMP NULL` to `trading_bracket_intents`. Idempotent (checks `information_schema.columns` before ALTER). Registered in the `MIGRATIONS` list. `verify-migration-ids.ps1` passes.

### Feature flag
`app/config.py`: `chili_bracket_missing_stop_repair_enabled: bool = Field(default=False, validation_alias=AliasChoices("CHILI_BRACKET_MISSING_STOP_REPAIR_ENABLED"))`. Defaults OFF.

### Emergency-repair branch
New helper `_try_emergency_repair_terminal_reject(db, *, local, broker, decision, sweep_id)` in `bracket_reconciliation_service.py`, wired ABOVE the existing `state_gated_skip` block (existing gate untouched). Three sub-branches:

1. **Broker unavailable / `broker_qty is None`** → return None, fall through to existing `state_gated_skip`. No throttle bump.
2. **`broker_qty == 0` (phantom)** → mark trade `status='closed', exit_reason='phantom_after_terminal_reject', exit_date=now()`, mark intent `state='closed'`, bump throttle, audit emit, CRITICAL log. Returns writer dict with `reason='phantom_closed'`.
3. **`broker_qty > 0` (real exposure)** → throttle check (return None if 6h not elapsed). Bump throttle BEFORE calling `place_missing_stop` (FIX-51 path) with `qty = min(local_qty, broker_qty)`. Audit emit + CRITICAL log on entry and outcome.

Throttle: `EMERGENCY_REPAIR_THROTTLE_SECONDS = int(os.getenv("CHILI_BRACKET_TERMINAL_REJECT_REPAIR_THROTTLE_SECONDS", "21600"))` — 6h default, env-overridable. Module-level constant, no inline magic number.

Schema reality: `trading_trades` has `exit_reason`/`exit_date`, not `closed_reason`/`updated_at`. Used the existing convention from `portfolio.py:148-160`.

Audit emission via `bracket_writer_g2._g2_event(writer="emergency_terminal_reject_repair", ...)` — reuses existing `trading_execution_events` plumbing.

### Reconciler entry-point gate
```python
if (
    intent_state_raw == "terminal_reject"
    and decision.kind == "missing_stop"
    and (local.trade_status or "").lower() == "open"
    and getattr(settings, "chili_bracket_missing_stop_repair_enabled", False)
):
    repair_result = _try_emergency_repair_terminal_reject(...)
    if repair_result is not None:
        return repair_result
    # else fall through to existing state_gated_skip
```

## Step 3 — Regression tests

`tests/test_bracket_emergency_terminal_reject_repair.py` (new, ~360 LOC). All **7 tests pass** against `chili_test` in 453s:

1. ✅ Phantom branch (broker_qty=0)
2. ✅ Real-exposure success (broker_qty == local_qty)
3. ✅ Real-exposure capped (local=20, broker=10 → places at 10)
4. ✅ Real-exposure rejection-relock (writer returns ok=False; throttle set; intent stays terminal_reject)
5. ✅ Throttle expiry (clock advance past 6h → new attempt fires)
6. ✅ Flag OFF → returns `state_gated_skip`, no broker call
7. ✅ Broker unavailable → silent skip, no audit, no throttle bump

Run command: `pytest tests/test_bracket_emergency_terminal_reject_repair.py -v -p no:asyncio` (workaround for pre-existing pytest-asyncio plugin AttributeError on collection — not introduced by this task).

## Step 4 — Deploy + verify

### Deploy timeline (2026-05-03 UTC)
1. Code committed at `ef50d3f`, pushed to `origin/main`.
2. Image rebuilt; `broker-sync-worker` recreated with flag OFF — behavior unchanged (verified: `state_gated_skip` continued firing for all 7).
3. Flag flipped: added `CHILI_BRACKET_MISSING_STOP_REPAIR_ENABLED=1` to `docker-compose.yml` `broker-sync-worker.environment`.
4. `docker compose up -d --force-recreate --no-deps broker-sync-worker`. Env var verified inside container.
5. **Sweep `a4006105-eded-4aa8-8c50-d04e8e3f8dd7`** at 22:01:23–32 UTC processed all 7 positions.

### Pre-flip SQL state
7 rows: AIDX 1812, CCCC 1813, CRDL 1814, ELTX 1816, IMTX 1818, TLS 1821, VFS 1822 — all `intent_state='terminal_reject'`, all `broker_stop_order_id IS NULL`, all `terminal_reject_repair_last_attempt_at IS NULL`.

### Post-flip SQL state
7 rows. `intent_state` unchanged (still `terminal_reject` — see "Surprises" below). 6 rows have `terminal_reject_repair_last_attempt_at` populated (throttle correctly bumped); IMTX correctly NOT bumped (new branch returned None on `kind=agree`).

### Live observations
- 6 `EMERGENCY-REPAIR` CRITICAL log lines fired in sweep `a4006105...`.
- 1 `EMERGENCY-REPAIR result … ok=True … new_stop_order_id=69f7c5b8-…` (ELTX live broker stop).
- 5 `EMERGENCY-REPAIR result … ok=False … reason=covered_by_existing_sell` (writer's safety guard — broker already had working sells covering full position).
- 6 `[bracket_reconciliation_ops] event=writer_action writer=emergency_terminal_reject_repair` audit lines.
- IMTX: `event=writer_action writer=state_gated_skip` (correctly skipped).

### Live blast radius
**1 new broker order**: ELTX 25 shares, stop @ $11.0584 (~$276 notional). All other 6 had no broker-side change.

## Surprises / deviations

### 1. False alarm on 5 of 7 — covered_by_existing_sell
The audit's "$2,107 unprotected exposure" was largely a **labeling artifact**. Robinhood reports `held_for_sells == broker_qty` for AIDX/CCCC/CRDL/TLS/VFS — i.e., working sells covering the full position quantity already exist at the broker. The `bracket_writer_g2` writer's `covered_by_existing_sell` safety guard correctly refused to place a duplicate.

Why our DB row says `broker_stop_order_id IS NULL` while the broker has a working sell: **the codebase never UPDATEs `trading_bracket_intents.broker_stop_order_id`** (verified — no writer in the tree references it for assignment). The classifier looks at broker truth on each sweep, but the local row remains a stale label.

This means the actual real exposure was 1 ticker (ELTX), ~$276 notional — not 7 tickers / $2,107.

### 2. ELTX placed but local row not auto-linked
ELTX got a real broker stop (verified queued at Robinhood), yet `bracket_intents.broker_stop_order_id` for trade 1816 is still NULL after the sweep. Same root cause as #1: no writer in the codebase assigns this column. The next sweep should classify ELTX as `kind=agree` (broker has working stop matching local stop_price) — the new branch's `decision.kind == "missing_stop"` guard then naturally stops firing.

### 3. IMTX `kind=agree` on classifier, `terminal_reject` on local
Same stale-label pattern as #1. Broker reports a working stop; local row never updated. The new branch's `decision.kind` guard correctly leaves it alone.

### 4. Throttle bumped on SKIPPED outcomes
The throttle is bumped BEFORE `place_missing_stop` is called (per the brief, to prevent retry storms on transient broker rejection). When the writer returns `ok=False, reason=covered_by_existing_sell`, the throttle still locks the row for 6h. This is intentional — the writer's refusal is real (the existing sell genuinely covers the position) and re-attempting at every 2-min sweep would be noise.

## Deferred

- **Cleanup of stale `intent_state='terminal_reject'` labels** for the 6 positions whose broker is already protected. The local rows will continue to fire `state_gated_skip` (after the 6h throttle expires) every sweep. Two options surfaced to operator; neither is urgent. Recommendation: (A) one-shot SQL `UPDATE trading_bracket_intents SET state='reconciled' WHERE id IN (220,221,222,224,226,229,230)` after operator visually confirms broker positions at their next session.
- **Why `broker_stop_order_id` is never UPDATEd** — pre-existing structural gap. The reconciler relies entirely on broker truth per sweep rather than mirroring the order ID locally. This works but means the local column is dead. Belongs in a separate hygiene task.
- **Root cause of `terminal_reject` on these 7** — out of scope per the brief. The original SELL_STOP submissions were rejected by Robinhood on 2026-05-01 17:50 (5 of them in one batch) and 2026-05-02 16:51 (IMTX). Likely tied to the post-fast-path-restart submission burst that prompted FIX-51. Separate investigation.

## Open questions for Cowork

1. **Should we add a writer that mirrors `broker_stop_order_id` into `bracket_intents` from the broker truth feed?** This would close the "stale label" gap surfaced by surprises #1, #2, #3. The classifier already has the broker order ID via `BrokerView.stop_order_id`; persisting it would let the local row reflect reality. Risk: making the local column authoritative invites consumers to read it instead of broker truth, eroding the "broker is authoritative" contract. Recommend: keep mirror cosmetic (read-only display in admin UI) rather than authoritative.

2. **Should `intent_state='terminal_reject'` auto-transition to `reconciled` when classifier returns `kind=agree` on a subsequent sweep?** Current behavior leaves stale `terminal_reject` labels indefinitely. A small reconciler addition (`if intent_state == 'terminal_reject' and decision.kind == 'agree': intent.state = 'reconciled'`) would cleanly close the loop. Cowork should weigh whether this belongs in a follow-up task.

3. **`covered_by_existing_sell` semantics — are these the original SELL_STOP orders that "rejected"?** The writer reports working sells matching `broker_qty` exactly, on positions whose intent rows say `terminal_reject`. Either (a) the rejections were transient and the orders did land, or (b) different sell orders (manual? from a separate code path?) are covering. Worth a quick `gh api` / Robinhood sweep on order metadata to confirm provenance.

4. **6h throttle duration validation.** All 6 SKIPPED rows are now locked for 6h. If `covered_by_existing_sell` is the *correct* steady state (option #2 above), the throttle is never the limiting factor — the next sweep's `kind=agree` would naturally skip. But if these are *real* terminal-reject loops, 6h might be too long. The brief specified 6h as default; flag if soak shows it's mistuned.

## Rollback plan (if needed in next 24h)

1. Set `CHILI_BRACKET_MISSING_STOP_REPAIR_ENABLED=0` in `docker-compose.yml` `broker-sync-worker.environment` (or remove the line).
2. `docker compose up -d --force-recreate --no-deps broker-sync-worker`.
3. New branch becomes a no-op; behavior reverts to `state_gated_skip` for all 7. Throttle column remains populated (harmless; flag-OFF code never reads it).
4. ELTX's live broker stop remains in place (broker is authoritative; no rollback needed at the broker layer).
5. Migration 222 column is nullable + unused — leave in place.

Code revert: `git revert ef50d3f && git revert <this commit>`.
