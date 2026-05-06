# NEXT_TASK: f-overnight-jumbo-2026-05-06

STATUS: DONE

## Meta-goal

Operator going offline for 8-10 hours. **Run all phases sequentially.
One commit per phase. One combined CC report at end.** Each phase
self-contained; if a phase blocks (operator authorization needed,
schema surprise, frozen contract), commit progress so far + skip to
the next. Don't deadlock waiting for input.

**Floor goal: at least Phase 1 ships** (live-money concern). Aspiration:
all 9 phases ship.

**Phase ordering (live-money first, then architectural cleanup, then
small fixes):**

| # | Phase | Tier | Est | Reasoning |
|---|---|---|---|---|
| 1 | f-bracket-writer-stop-construction-fix (PED) | 🚨 live-money | 90 min | PED has no working stop. 45+ retries/hour. Every other ticker with a 4-decimal brain stop hits this. |
| 2 | f-leak-4-phase-2 (chili pydantic closures) | 🚨 production | 90 min | 63 MB/min slope = 3.7 GB/hr. Chili will OOM regularly until fixed. |
| 3 | f-handler-breakout-outcomes | 🟡 backlog top | 90 min | Top of PHASE2_HANDLER_BACKLOG. Secondary-evidence path; closes pattern-stats-coverage gap for patterns with no closed trades. |
| 4 | f-cron-stale-promoted | 🟡 demote gap | 60 min | Per-trade-close demote handler doesn't catch patterns whose trades stop firing. Real coverage gap. |
| 5 | f-handler-validate-evolve | 🟡 weight evolution | 75 min | Stale weights mean brain doesn't react to regime changes. |
| 6 | f-handler-live-drift + f-handler-execution-robustness (bundle) | 🟡 monitoring | 120 min | Both trade-close-driven; bundleable. |
| 7 | f-tighten-db-watchdog-brain-worker-exemption | 🟢 cleanup | 30 min | 1800s exemption no longer justified now that legacy cycle is gated off. |
| 8 | f-cleanup-cycle-report | 🟢 code deletion | 30 min | Cycle gated off; report generator is dead code. |
| 9 | f-add-pg-stat-snapshot-logger | 🟢 observability | 45 min | Forensic snapshots of idle-in-tx so next leak has a trail. |

Total estimated: ~9.5 hours.

**Stop-on-blocker policy:** If Phase N has a hard blocker, write
`Phase N: BLOCKED — <reason>` in the CC report's per-phase section
and **proceed to Phase N+1**. Don't deadlock.

**Cross-phase constraints (apply to ALL phases):**

- Default mode stays paper. No live placement enable.
- All 8 fast-path safety belts intact. PROTOCOL Hard Rule 1.
- Do not re-enable `run_learning_cycle`. Stays gated off.
- Do not modify the canonical evaluator (`exit_evaluator.py`).
- Do not modify the realized-EV gate or promotion gate.
- Do not modify any of the 6 brain_work handlers shipped this session.
- Tests use `_test`-suffixed DB. PROTOCOL Hard Rule 5.
- Migration IDs: claim next sequential at execution time. Verify with
  `verify-migration-ids.ps1`. Last shipped today is **229**; next is
  230.
- One commit per phase. Atomic recoverability.
- No `git push --force`. PROTOCOL Hard Rule 4.
- Any new behavior flag defaults to `False` / `off` / disabled.
- All Tier 4 items (paper-shadow dashboard, prefer-shadow-evidence,
  cleanup paper_book_json placeholder) are EXPLICITLY OUT of scope —
  they need data accumulation first.

**Combined CC report**: write ONE report at
`docs/STRATEGY/CC_REPORTS/<date>_f-overnight-jumbo-2026-05-06.md`
covering all 9 phases. Status per phase: SHIPPED / BLOCKED /
VERIFIED-NON-ISSUE. Cross-phase observations + cookbook updates +
combined commit summary at the end.

---

# Phase 1 — f-bracket-writer-stop-construction-fix (LIVE-MONEY)

## Goal

Fix the recurring SELL_STOP placement failure that's been silently retrying for **hours** in the bracket-reconciliation sweep. Root cause is in the order-construction code in `app/services/broker_service.py` — Robinhood is rejecting requests with `non_field_errors=['Limit order requested, but no price provided.']` even though the bracket writer believes it's submitting a stop-MARKET order.

After this task ships:

- The actual broker error is no longer hidden behind the generic `[bracket_writer_g2] place_missing_stop broker error intent=N: Robinhood returned no order_id` log line. The full request body and broker response get captured at WARNING level on every placement failure.
- Stop prices are rounded to the equity's broker tick size before submission (today's PED `13.6275` becomes `13.63` before the API call, matching what worked yesterday). No more 4-decimal stop prices reaching the wire.
- The order-type bug — whatever it is — gets surfaced and fixed. Either the request body is being constructed with `type=limit` somewhere it shouldn't be, OR the trigger price's invalid tick is causing RH's validation to fall through to a misleading error branch. Either way, after the fix, `place_stop_loss_sell_order` for any equity in the open-position list should produce `state=confirmed` at the broker.

This is the recurring-cancellation-cascade the operator has been seeing for days. The fix unblocks PED's downside protection AND any future ticker that hits a 4-decimal brain stop_price.

## Why now

This morning's PED investigation captured the actual broker error inside the generic "no order_id" log:

```
[broker] SELL_STOP rejected (no order_id):
PED x30.0 trigger=13.6275 response={'non_field_errors': ['Limit order requested, but no price provided.']}
```

DB evidence shows 45+ retries in 44 minutes, all PED, every minute on the bracket-reconciliation sweep. Started at 12:42 UTC today after PED's existing stop got cancelled at the broker (90-day expiry, manual cancel via UI, or auto-cleanup — the cancellation source is a separate concern). Yesterday's deploy successfully placed PED's stop at `$13.63` (rounded). Today's re-placement requests are sending `$13.6275` raw and getting rejected.

**Why it's PED-specific so far:** PED's brain-derived stop is `13.6275` (4 decimals). All other equity stops in the portfolio rounded cleanly to 2 decimals (TLS=$3.86, AIDX=$0.91, CCCC=$2.22, etc.) and don't trigger the bug. Any future ticker whose brain stop has >2 decimals on a >$1 stock will hit the same path.

The operator has been seeing recurring "order cancelled" notifications in Robinhood for days; some of those were today's covered-limit cancellations (intentional per `bracket-writer-respect-upside-targets`), but at least 45 of them in the last hour are this PED bug.

Live-money risk: until the fix ships, PED has no working stop at the broker. If price drops below $13.63 the system can't auto-sell because re-placement keeps failing.

## Brain integration / source material

- `app/services/broker_service.py::place_stop_loss_sell_order` (or wherever the stop-loss SELL_STOP request body is constructed before `rh.orders.order(...)` — discover via grep). The order-type and stop_price formatting bug lives here.
- `app/services/broker_service.py` — surfaces the `[broker] SELL_STOP rejected (no order_id): ... response=...` ERROR log already. Extend it to also dump the FULL request body (qty, type, time_in_force, trigger_type, stop_price, limit_price, instrument_url, account_url) at WARNING level when `response` indicates rejection.
- `app/services/trading/bracket_writer_g2.py::place_missing_stop` — caller. After this fix, `place_missing_stop` should NOT need any change; the wrap is at broker_service layer.
- Robinhood instrument metadata — for tick-size lookup. The instrument response (`rs.helper.request_get(instrument_url)`) carries `min_tick_size`. Use it to round stop_price.
- Yesterday's verified-confirmed stop placements (`AIDX 0.9073`, `CCCC 2.22`, etc. per the rh-orders-investigate output from this morning) — confirm the post-fix behavior matches yesterday's working pattern.

## Path

**Design principle: zero new magic numbers.** Tick size derives from broker-side instrument metadata, not from a hardcoded "2 decimals for >$1 stocks." If the lookup fails, defer to a documented broker-API-respecting fallback (NOT a guess).

### Step 1 — Add full request-body diagnostic on placement failure

In `broker_service.py`, find the `[broker] SELL_STOP rejected (no order_id): ...` log line. Around it, wrap the `rh.orders.order(...)` call so that:

- The full request payload is logged at INFO level **before** submission (so a successful placement leaves an audit trail too).
- On any failure (no order_id returned, or the response contains `non_field_errors` / `detail` / similar rejection markers), log the request body AND the response body at WARNING level.

```python
logger.info(
    "[broker] SELL_STOP submitting: ticker=%s qty=%.4f trigger=%s "
    "type=%s time_in_force=%s instrument=%s account=%s",
    ticker, qty, stop_price, order_type, tif, instrument_url, account_url,
)
resp = rh.orders.order(...)
if not (resp and resp.get("id")):
    logger.warning(
        "[broker] SELL_STOP rejected (full diagnostic): ticker=%s qty=%.4f "
        "trigger=%s type=%s tif=%s response=%s",
        ticker, qty, stop_price, order_type, tif, resp,
    )
```

Keep the existing ERROR-level log line (don't break log-grep patterns operators rely on); add the WARNING with full diagnostic alongside.

### Step 2 — Round stop_price to broker tick size before submission

Add a helper in `broker_service.py`:

```python
def _round_stop_price_to_tick(ticker: str, instrument_url: str | None, stop_price: float) -> float:
    """Round stop_price to the equity's broker-reported tick size.

    Defers to instrument.min_tick_size from Robinhood. If the lookup
    fails or the instrument doesn't carry a tick size (rare), surface
    via WARNING and return the original stop_price (let the broker reject
    it with a clearer error rather than guessing).
    """
    try:
        if not instrument_url:
            instrument_url = _resolve_instrument_url(ticker)
        instr = rh.helper.request_get(instrument_url)
        tick_str = (instr or {}).get("min_tick_size")
        if tick_str:
            tick = Decimal(str(tick_str))
            quantized = (Decimal(str(stop_price))
                         / tick).quantize(Decimal("1"), rounding=ROUND_DOWN) * tick
            return float(quantized)
    except Exception as e:
        logger.warning("[broker] tick-size lookup failed ticker=%s err=%s", ticker, e)
    return stop_price
```

Call it before submitting:

```python
stop_price_rounded = _round_stop_price_to_tick(ticker, instrument_url, stop_price)
if stop_price_rounded != stop_price:
    logger.info(
        "[broker] stop_price rounded to broker tick: ticker=%s %s -> %s",
        ticker, stop_price, stop_price_rounded,
    )
stop_price = stop_price_rounded
```

`ROUND_DOWN` for long-stop is intentional: a stop just above the brain's preferred level (rounded UP) would trigger SOONER than the brain wanted; rounding DOWN preserves brain's intent. Document this choice in the helper's docstring.

### Step 3 — Audit the order-type / payload construction

Run the placement once with diagnostic logging from Step 1 in place; capture the full request body. Compare to the rh-orders-investigate output from this morning where successful placements show `type=market trigger=stop`.

Verify the request body submitted has:
- `type=market` (NOT `limit`)
- `trigger=stop` (NOT `immediate`)
- `stop_price=<rounded>`
- `price=null` (no limit price for stop-MARKET)

If the bracket-writer / broker_service code is constructing the body with any of those fields wrong (e.g., `type=limit` because of a misnamed parameter, or `price=<some value>` causing RH to interpret as stop-LIMIT), fix in place.

If the rounding from Step 2 alone fixes the issue (i.e., RH was just giving a misleading error for the invalid tick), Step 3 reduces to "verify and document." That's still a successful outcome.

### Step 4 — Tests

`tests/test_broker_stop_construction.py`:

- **Scenario A: 4-decimal stop_price gets rounded down to 2 decimals.** Mock instrument with `min_tick_size=0.01`. Call `_round_stop_price_to_tick("PED", url, 13.6275)`. Assert returns `13.62` (or `13.63` depending on `ROUND_DOWN`/`ROUND_FLOOR` semantics — verify and lock the choice in the test). No actual broker call.
- **Scenario B: 8-decimal crypto stop_price respects 4-decimal tick.** Mock crypto-style tick `0.0001`. Call with `0.91234567`. Assert returns `0.9123`.
- **Scenario C: tick-lookup failure surfaces WARNING and returns original.** Mock `request_get` to raise. Call with `13.6275`. Assert returns `13.6275` AND a WARNING log was emitted.
- **Scenario D: round-down preserves brain intent on long stops.** Brain wants stop at `13.6275`. After rounding, stop is at OR BELOW that — never above. Assert.
- **Scenario E: full request body diagnostic fires on rejection.** Mock `rh.orders.order` returning a no-id response with `non_field_errors`. Call `place_stop_loss_sell_order`. Assert WARNING log line containing the full diagnostic with `type=`, `trigger=`, `stop_price=`, `response=` substrings.

All scenarios use `chili_test`. No live network.

### Step 5 — Live verification

Operator-side, post-deploy, watch for the next bracket-reconciliation sweep that hits PED:

```powershell
docker compose logs broker-sync-worker --since 3m -f | Select-String -Pattern "PED|stop_price rounded|SELL_STOP submitting|SELL_STOP rejected"
```

Expected sequence on success:
1. `[broker] stop_price rounded to broker tick: ticker=PED 13.6275 -> 13.63`
2. `[broker] SELL_STOP submitting: ticker=PED qty=30.0000 trigger=13.63 type=market ...`
3. NO `[broker] SELL_STOP rejected` line
4. New `g2_place_missing_stop_submitted` row in `trading_execution_events` with a non-null `order_id`
5. PED's `trading_bracket_intents.broker_stop_order_id` populated; `last_diff_reason` flips from `missing_stop:error` to `agree`

If the placement still fails after rounding, the WARNING-level full-body diagnostic log will reveal the actual order-type bug. That's a follow-up commit in the same task: read the diagnostic, fix the body construction, redeploy.

## Constraints / do not touch

- **No magic numbers.** Tick size comes from broker-side `min_tick_size`. The `ROUND_DOWN` direction is documented as deliberate (brain-intent-preserving for long stops); not tunable.
- **No live-broker behavior change beyond the fix.** The stop placement either succeeds or fails per today's rules; the fix removes a class of failures, doesn't add new behavior.
- **Don't modify `bracket_writer_g2.place_missing_stop`.** The wrap is at the broker_service layer. Writer continues to send the brain's `stop_price` value; broker_service rounds before submission.
- **Don't change the existing ERROR-level rejection log message.** Operators / log-grep patterns reference it. Add the WARNING-level full-body log alongside.
- **Tests use `_test`-suffixed DB.** PROTOCOL Hard Rule.
- **No `git push --force` to main.** PROTOCOL Hard Rule.
- **No new env-overridable defaults.** The rounding behavior is structural, not tunable.

## Out of scope

- **Why PED's existing stop got cancelled at the broker yesterday or this morning.** That's a separate question (90-day RH expiry, manual UI cancel, auto-cleanup, restricted-symbol action). The fix here makes RE-PLACEMENT work correctly; the original-cancellation cause can be investigated separately if it recurs.
- **Updating the brain to emit 2-decimal stops directly.** The brain's calibrated stop is `13.6275` because that's what the math says. Rounding belongs at the broker-API-adapter layer, not in the brain.
- **OCO / multi-leg / bracket-pair orders.** Out of scope; structural concern for a future Phase 6 of the position-identity refactor (or earlier if operator decides).
- **Crypto stop placement.** Robinhood crypto stops use a different broker call. This task scopes equity stops only. Crypto rounding can land in a follow-up if/when crypto stops fail similarly.
- **Investigating other tickers' silent rejection history.** The 24-hour history shows PED is the only ticker with the no-order-id rejection in scope. If other tickers start exhibiting it post-fix, separate task.

## Success criteria

1. **Two commits, both pushed:**
   - `fix(broker): round stop_price to instrument tick size + full-body diagnostic on rejection`
   - `docs(strategy): f-bracket-writer-stop-construction-fix CC report + mark NEXT_TASK done`
2. **Test scenarios A-E pass** against `chili_test`.
3. **Live verification:** within 3 minutes of deploy, the bracket-reconciliation sweep places PED's stop successfully.
4. **DB evidence:** `trading_execution_events` shows a fresh `g2_place_missing_stop_submitted` row for PED with a non-null `order_id`.
5. **Broker evidence:** `rs.orders.get_all_open_stock_orders()` includes a SELL_STOP order for PED at the rounded stop_price (verify with the existing `_rh_probe_stops_now.py`).
6. **No new "Limit order requested, but no price provided" rejections** in the post-deploy 30-minute window.
7. **Magic-number audit clean** in the CC report. Expected count of new behavioral literals: zero. Tick size is broker-derived, rounding direction is structural.
8. **CC_REPORT** at `docs/STRATEGY/CC_REPORTS/<date>_f-bracket-writer-stop-construction-fix.md` per PROTOCOL format. Include:
   - Magic-number audit
   - The diagnostic log line from a live failed placement (if any captured pre-fix), with the actual request body
   - Live verification of PED's successful post-fix placement
   - Whether Step 3's order-type investigation revealed an additional bug beyond tick-size rounding, and what was fixed if so

## Rollback plan

- **Code rollback:** `git revert <fix-commit>`. Stop placements revert to today's behavior — PED-style 4-decimal rejection cascades resume. Other tickers continue working.
- **No migration to roll back.**
- **No live broker rollback needed.** This task adds rounding + diagnostic logging; it doesn't cancel or place broker orders during the deploy itself. The post-deploy first-PED-sweep will be the first placement attempt under the new code.

## Verification commands (for the executor + the operator)

```powershell
# Pre-deploy: confirm the bug is still firing
docker compose logs --since 5m broker-sync-worker | Select-String "PED" | Select-String "no order_id"

# Post-deploy: watch the next sweep place PED's stop successfully
docker compose logs broker-sync-worker --since 3m -f | Select-String -Pattern "PED|stop_price rounded|SELL_STOP submitting|SELL_STOP rejected"

# Confirm new SELL_STOP at Robinhood
docker compose exec -T broker-sync-worker python /app/scripts/_rh_probe_stops_now.py | Select-String "PED"

# Confirm DB intent updated
docker compose exec -T postgres psql -U chili -d chili -c "SELECT id, ticker, intent_state, broker_stop_order_id, last_diff_reason, updated_at FROM trading_bracket_intents WHERE ticker='PED' ORDER BY updated_at DESC LIMIT 3;"

# Tests
$env:TEST_DATABASE_URL='postgresql://chili:chili@localhost:5433/chili_test'
pytest tests/test_broker_stop_construction.py -v
```

## Open questions for Cowork (surface in the CC report)

1. **Did Step 3 reveal an additional order-type bug beyond tick-size rounding?** If the rounding alone fixed PED's placement, that's the simplest explanation (RH gave a misleading error for an invalid-tick stop_price). If the body construction also had a `type=limit` bug, surface and fix in the same commit.
2. **Crypto rounding deferral.** Equity-only scope per "Out of scope" above. Surface if any crypto stop is currently failing with similar errors; if not, deferral is fine.
3. **Tick-size cache.** `request_get(instrument_url)` is one HTTP call per stop placement. At low placement cadence (hours between attempts) the latency is negligible. If a future task wants to pre-cache instrument metadata, that's a small follow-up; not blocking.

## Phase 1 forward pointer

After Phase 1 ships, proceed to Phase 2. **Do not stop after Phase 1**;
the operator wants forward progress on all 9 phases.

---

# Phase 2 — f-leak-4-phase-2: chili pydantic closure leak

## Goal

Fix the chili main-app +63 MB/min memory slope (= 3.7 GB/hr if
sustained). Per f-leak-4 Phase 2's static analysis, the leak is in
pydantic v2's `set_model_mocks` deferred-validation rebuild path —
1488 closures retained per request burst. Fix is to eagerly rebuild
the offending models at module import time so the per-request
rebuild path doesn't fire.

## Source material

- `docs/AUDITS/2026-05-06_chili-app-closure-leak.md` — the runtime
  diagnostic written by f-leak-4 Phase 2.
- `app/main.py` — FastAPI app + middleware setup.
- `app/routers/` — every route module's pydantic models.
- `app/schemas/` (if exists) — shared pydantic models.
- pydantic v2 docs: `Model.model_rebuild()` for eager forward-ref
  resolution.

## Path

### Step 2.1 — Run the closure-count diagnostic from the audit doc

The audit doc has a 60s diagnostic script. Run it via dispatch
to capture closure counts before + after a request burst. Surface
the top 10 qualnames + their delta. **The qualname with the largest
delta is the offender.**

If diagnostic fails (no growth observed in the window), surface as
"reproduction failed" and skip to Step 2.4 (preventive fix).

### Step 2.2 — Identify the model with deferred forward-refs

Grep the codebase for pydantic models that use:
- `from __future__ import annotations` (forward-refs everywhere)
- Type hints referencing strings (`"OtherModel"`)
- Self-referential models (`field: list["Self"]`)
- Nested models defined in different modules

Each such model triggers `set_model_mocks` on first validation. If
the validation runs per request (versus once at import), each request
creates new closures.

### Step 2.3 — Add eager `model_rebuild()` calls

For each suspect model, add at module import time (or in `main.py`):

```python
from app.schemas.X import Foo, Bar
Foo.model_rebuild()
Bar.model_rebuild()
```

This forces pydantic to resolve forward-refs once at import. Subsequent
validations don't trigger the rebuild path.

If no specific model is identifiable from Step 2.2, the safe blanket
fix is to call `model_rebuild()` on every model defined in the
schemas / models packages at app startup. Has minor startup-time
cost but eliminates the per-request rebuild leak entirely.

### Step 2.4 — Tests

`tests/test_chili_pydantic_no_per_request_rebuild.py`:

1. ✅ Synthetic 100-request burst against a TestClient → closure count
   for `set_model_mocks.<locals>.attempt_rebuild_fn.<locals>.handler`
   doesn't grow proportional to request count.
2. ✅ All response shapes still validate correctly post-fix
   (regression guard against accidentally breaking validation).

### Step 2.5 — Smoke verification (post-deploy, operator-side)

```powershell
docker compose logs chili --since 30m | Select-String "mem_watcher" |
    Select-String "set_model_mocks|request_response|get_request_handler"
```

Expected: closure counts plateau within first few requests, not
monotonic growth. Pre-fix slope was +63 MB/min; post-fix should be
< 10 MB/min.

## Constraints / do not touch

- **Do not modify route handlers themselves.** The fix is at the
  pydantic-model layer, not the FastAPI-router layer.
- **Do not modify pydantic Settings classes** (`app/config.py`).
  Those load once at startup; not the leak source.

## Success criteria

- Closure-count diagnostic fires; top offender identified.
- Eager `model_rebuild()` calls added for the offender(s).
- Tests pin the per-request closure-count invariant.
- Smoke verification queued for operator post-deploy.

## Commit message

`fix(pydantic-leak): eager model_rebuild() to stop per-request closure growth (f-leak-4 phase 2)`

OR if no specific model is identifiable:

`docs(audit): chili pydantic closure leak — runtime profile inconclusive (f-leak-4 phase 2)`

---

# Phase 3 — f-handler-breakout-outcomes

## Goal

Ship the next handler from `PHASE2_HANDLER_BACKLOG.md`'s top of list:
`learn_from_breakout_outcomes`. This is the secondary-evidence path
for patterns that don't have closed trades yet — uses BreakoutAlert
outcomes (winner / fakeout / loser) instead of realized P/L.

## Source material

- `app/services/trading/learning.py:4635` (or similar — `learn_from_breakout_outcomes`).
- `app/services/trading/brain_work/handlers/pattern_stats.py` — model
  for the new handler shape.
- `app/services/trading/brain_work/dispatcher.py` — wire the new
  handler into the dispatch chain.
- `PHASE2_HANDLER_BACKLOG.md` row: `learn_from_breakout_outcomes`.

## Path

### Step 3.1 — Identify the trigger event

`learn_from_breakout_outcomes` aggregates `BreakoutAlert` rows by
`outcome` (winner / fakeout / loser). The natural trigger is when
a BreakoutAlert resolves (its `outcome` field changes from NULL to
a final value). Check if there's an existing emitter for
`breakout_alert_resolved`. If not, add one in
`app/services/trading/brain_work/emitters.py` mirroring
`emit_paper_trade_closed_outcome`.

### Step 3.2 — Wire the emitter

Find the code path where BreakoutAlert.outcome is set to its final
value (likely in `auto_trader_monitor.py` or similar). Add the emit
call in-transaction.

### Step 3.3 — New handler module

Create `app/services/trading/brain_work/handlers/breakout_outcomes.py`:

```python
"""Phase 2 handler: pattern-evidence update from breakout-alert outcomes.

Subscribes to ``breakout_alert_resolved``. Calls
``learn_from_breakout_outcomes`` for the resolved alert's pattern.
"""
import logging
from typing import Any
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def handle_breakout_alert_resolved(
    db: Session, ev: Any, user_id: int | None
) -> None:
    from app.db import SessionLocal
    sess = SessionLocal()
    try:
        from app.services.trading.learning import learn_from_breakout_outcomes
        result = learn_from_breakout_outcomes(sess, user_id)
        logger.info(
            "[handler:breakout_outcomes] event_id=%s user_id=%s "
            "patterns_learned=%d",
            getattr(ev, "id", None), user_id,
            int(result.get("patterns_learned", 0)),
        )
    except Exception as e:
        logger.exception(
            "[handler:breakout_outcomes] event_id=%s failed: %s",
            getattr(ev, "id", None), e,
        )
    finally:
        sess.close()
```

**Use absolute import `from app.db import SessionLocal`** —
remember the f-handler-pattern-stats finding: 4-dot relative
imports break in this package.

### Step 3.4 — Wire into dispatcher

`app/services/trading/brain_work/dispatcher.py` — add the dispatch
branch for `breakout_alert_resolved`. Mirror the
`paper_trade_closed` branch that dispatches to pattern_stats /
demote / regime_ledger.

### Step 3.5 — Config setting

`app/config.py`:
```python
brain_work_breakout_outcomes_batch_size: int = 4
```

### Step 3.6 — Tests

`tests/test_handler_breakout_outcomes.py`:

1. ✅ `handle_breakout_alert_resolved` calls `learn_from_breakout_outcomes`.
2. ✅ Handler swallows exceptions (mock function to raise).
3. ✅ Handler uses absolute import (regression guard against the
   import-bug class).
4. ✅ Dispatcher source contains `handle_breakout_alert_resolved`
   reference (wiring guard).

## Success criteria

- New handler module + emitter + dispatcher wiring + config.
- Tests pass.
- PHASE2_HANDLER_BACKLOG.md updated.

## Commit message

`feat(brain-work): handler for breakout-alert-resolved events (f-handler-breakout-outcomes)`

---

# Phase 4 — f-cron-stale-promoted (sweep-mode demote gap)

## Goal

`handlers/demote.py` re-evaluates the realized-EV gate on every
trade-close event. **Patterns whose trades have stopped firing
entirely never get re-checked** — they stay
`lifecycle_stage='promoted'` indefinitely. Add a weekly cron-sweep
that catches these.

## Source material

- `app/services/trading/realized_ev_gate.py` — the gate this sweep
  re-runs.
- `app/services/trading_scheduler.py` — APScheduler job registration
  pattern. Find existing weekly cron jobs as a template.
- `learning.py::run_live_pattern_depromotion` (now-dead in the cycle)
  — read as reference for the sweep logic, but DO NOT call from the
  cycle (cycle is gated off).

## Path

### Step 4.1 — New cron-sweep function

Create `app/services/trading/cron_jobs/stale_promoted_sweep.py`:

```python
"""Weekly sweep: re-evaluate the realized-EV gate on patterns whose
trades have stopped firing.

Per-trade-close demote handler covers the active case. This sweep
catches patterns where trades stopped firing entirely — would
otherwise sit at promoted indefinitely.
"""
import logging
from datetime import datetime, timedelta
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def run_stale_promoted_sweep(db: Session) -> dict:
    """Iterate promoted patterns; demote any that fail the EV gate."""
    from app.models.trading import ScanPattern, Trade
    from app.services.trading.realized_ev_gate import evaluate_realized_ev

    stale_cutoff = datetime.utcnow() - timedelta(days=7)
    patterns = db.query(ScanPattern).filter(
        ScanPattern.lifecycle_stage == 'promoted',
        ScanPattern.active.is_(True),
    ).all()

    demoted = 0
    checked = 0
    skipped_recent = 0
    for p in patterns:
        # Skip patterns with recent trade activity — handler covers them.
        last_trade_q = db.query(Trade).filter(
            Trade.scan_pattern_id == p.id
        ).order_by(Trade.exit_date.desc().nulls_last()).first()
        if last_trade_q and last_trade_q.exit_date and last_trade_q.exit_date >= stale_cutoff:
            skipped_recent += 1
            continue
        checked += 1

        result = evaluate_realized_ev(p)
        if not result.passed:
            p.lifecycle_stage = 'challenged'
            p.updated_at = datetime.utcnow()
            demoted += 1
            logger.info(
                "[stale_promoted_sweep] demoted pattern_id=%s name=%s "
                "reason=%s",
                p.id, p.name, result.reason,
            )

    db.commit()
    return {
        "patterns_checked": checked,
        "patterns_skipped_recent": skipped_recent,
        "patterns_demoted": demoted,
    }
```

### Step 4.2 — Register the cron job

In `app/services/trading_scheduler.py`, add the APScheduler job:

```python
# Weekly: demote-sweep for stale promoted patterns
scheduler.add_job(
    _run_stale_promoted_sweep_job,
    trigger="cron",
    day_of_week="sun",
    hour=2,  # 2 AM UTC Sundays — quiet window
    id="stale_promoted_sweep",
    replace_existing=True,
)
```

Plus the wrapper:
```python
def _run_stale_promoted_sweep_job():
    from app.db import SessionLocal
    from app.services.trading.cron_jobs.stale_promoted_sweep import run_stale_promoted_sweep
    with SessionLocal() as db:
        try:
            result = run_stale_promoted_sweep(db)
            logger.info("[cron:stale_promoted_sweep] %s", result)
        except Exception as e:
            logger.exception("[cron:stale_promoted_sweep] failed: %s", e)
```

### Step 4.3 — Tests

`tests/test_cron_stale_promoted.py`:

1. ✅ Pattern with no trades + failing EV → demoted to 'challenged'.
2. ✅ Pattern with trade in last 7 days → skipped (handler covers).
3. ✅ Pattern with passing EV → stays 'promoted'.
4. ✅ Function uses `with SessionLocal() as` (lifecycle guard).

## Success criteria

- New module + cron registration.
- Tests pass.
- Cron job appears in scheduler-worker startup logs.

## Commit message

`feat(cron): weekly stale-promoted-pattern sweep (f-cron-stale-promoted)`

---

# Phase 5 — f-handler-validate-evolve

## Goal

Wire `validate_and_evolve` (the hypothesis-weight-evolution step
from the legacy cycle) into an event-driven handler. Stale weights
mean the brain doesn't react to regime changes in feature
predictiveness.

## Source material

- `app/services/trading/learning.py` — `validate_and_evolve` function
  (around the cycle's Step 10, line ~9700 per saved memory).
- `app/services/trading/brain_work/handlers/pattern_stats.py` — model
  for new handler.

## Path

### Step 5.1 — Identify the trigger event

`validate_and_evolve` evolves hypothesis weights based on accumulated
realized data. Natural trigger: pattern_stats handler runs (which
fires on every trade close). Could be a `pattern_evidence_recomputed`
event emitted from pattern_stats handler post-recompute.

OR: timer-based — every 6h. Per cookbook, timer-based work goes in
scheduler-worker as APScheduler cron, not in `brain_work/handlers/`.

**Decision rule**: if `validate_and_evolve` reads pattern evidence
to evolve weights, event-trigger via `pattern_evidence_recomputed`
makes sense. If it reads broader market state, cron is better.

Read the function. Pick based on what it actually consumes.

### Step 5.2 — Implement either an event handler OR a cron job

Choose based on Step 5.1's read. Either:
- New `app/services/trading/brain_work/handlers/validate_evolve.py`
  + new event emit in pattern_stats handler
- New `app/services/trading/cron_jobs/validate_evolve.py` + APScheduler
  job (every 6h)

### Step 5.3 — Tests

`tests/test_<handler_or_cron>_validate_evolve.py`:

1. ✅ Function is invoked by the trigger.
2. ✅ Failures swallowed (mock function to raise).
3. ✅ Absolute import (handler) OR APScheduler job registered (cron).

## Success criteria

- Function wrapped in handler OR cron.
- Tests pass.
- PHASE2_HANDLER_BACKLOG.md updated.

## Commit message

`feat(brain-work): wire validate_and_evolve into <event|cron> path (f-handler-validate-evolve)`

---

# Phase 6 — f-handler-live-drift + f-handler-execution-robustness (BUNDLE)

## Goal

Two trade-close-driven handlers, bundled because both subscribe to
the same events:

- `f-handler-live-drift` — wraps `run_live_drift_refresh` from
  `live_drift.py`. Detects when promoted patterns' live behavior
  drifts from backtest expectations.
- `f-handler-execution-robustness` — wraps
  `run_execution_robustness_refresh` from `execution_robustness.py`.
  Tracks whether live executions meet expected slippage/cost profiles.

## Source material

- `app/services/trading/live_drift.py::run_live_drift_refresh`
- `app/services/trading/execution_robustness.py::run_execution_robustness_refresh`
- Both called from cycle's depromote step today.
- `app/services/trading/brain_work/handlers/demote.py` — model for
  trade-close-driven handler shape.

## Path

### Step 6.1 — Two new handler modules

`app/services/trading/brain_work/handlers/live_drift.py`:

```python
def handle_live_trade_closed(db, ev, user_id):
    from app.db import SessionLocal
    sess = SessionLocal()
    try:
        from app.services.trading.live_drift import run_live_drift_refresh
        run_live_drift_refresh(sess, user_id=user_id)
    except Exception as e:
        logger.exception("[handler:live_drift] failed: %s", e)
    finally:
        sess.close()

# Same shape for handle_paper_trade_closed and handle_broker_fill_closed.
```

`app/services/trading/brain_work/handlers/execution_robustness.py`:
mirror shape, calls `run_execution_robustness_refresh`.

### Step 6.2 — Wire into dispatcher

Both handlers added as fanout subscribers on the trade-close events,
alongside pattern_stats / demote / regime_ledger. Dispatch order
doesn't matter for these two (they don't update inputs each other reads).

### Step 6.3 — Config settings

```python
brain_work_live_drift_batch_size: int = 2
brain_work_execution_robustness_batch_size: int = 2
```

Lower batch sizes because these functions may do non-trivial work.

### Step 6.4 — Tests

Per-handler test file. Same shape as Phase 3's tests.

## Success criteria

- Two new handler modules.
- Two new dispatcher branches added to existing trade-close fanout.
- Tests pass.
- PHASE2_HANDLER_BACKLOG.md updated.

## Commit message

`feat(brain-work): live-drift + execution-robustness handlers (f-handler-live-drift + f-handler-execution-robustness)`

---

# Phase 7 — f-tighten-db-watchdog-brain-worker-exemption

## Goal

`db_watchdog` has a 1800s exemption for `chili-brain-worker` and
`chili-backtest-child` (per FIX 32). That exemption was justified
when the legacy `run_learning_cycle` held sessions during 60-140
minute cycles. Cycle is gated off now (f-kill-legacy-learning-cycle).
Tighten the brain-worker exemption back to the standard 600s.

## Source material

- `app/services/db_watchdog.py` — find the per-app threshold lookup.
- FIX 32 history (saved memory `reference_2026_04_28_deep_audit_fixes.md`)
  for context.

## Path

### Step 7.1 — Find and lower the threshold

In `db_watchdog.py`, find the per-app threshold map. Change the
brain-worker entry from `1800` to `600`. Same for
`chili-backtest-child`.

Add a comment explaining the change:
```python
# 600s threshold restored 2026-05-06 (f-tighten-db-watchdog-brain-worker-exemption).
# Was 1800s under FIX 32 to allow legacy cycle's long-held sessions; cycle is
# gated off via CHILI_BRAIN_LEGACY_CYCLE_ENABLED=0 (f-kill-legacy-learning-cycle).
# If a future code path needs the exemption back, add a per-query carve-out
# rather than a per-app one.
```

### Step 7.2 — Tests

`tests/test_db_watchdog_brain_worker_threshold.py`:

1. ✅ Brain-worker idle-in-tx held 700s → triggers KILL (was previously
   exempted under 1800s).
2. ✅ Other apps still respect their existing threshold.

## Success criteria

- Threshold change committed.
- Tests pass.

## Commit message

`fix(db-watchdog): tighten brain-worker exemption 1800s -> 600s (f-tighten-db-watchdog-brain-worker-exemption)`

---

# Phase 8 — f-cleanup-cycle-report

## Goal

`generate_and_store_cycle_report` was a step inside the legacy
`run_learning_cycle`. With the cycle gated off (f-kill-legacy-learning-cycle)
this function is dead code. **Delete the source.**

## Source material

- `app/services/trading/learning_cycle_report.py::generate_and_store_cycle_report`
  (or wherever — find via grep).
- `app/services/trading/learning.py:run_learning_cycle` — call site
  (now unreachable).

## Path

### Step 8.1 — Verify zero callers

Grep for `generate_and_store_cycle_report`:
- Should be 1 caller: inside `run_learning_cycle` (gated off).
- Anything else: surface and STOP.

### Step 8.2 — Delete

Remove the function, the call site (if it's the only thing in its
block), and any imports that referenced it. Verify the existing tests
still pass.

If the function is used by a future re-enabled cycle path, the cycle
re-enable would need to be a separate explicit decision; this cleanup
is irreversible without a `git revert`.

### Step 8.3 — Tests

If there's a test that imports the function, update / delete it.
Run the full test suite to confirm no regression.

## Success criteria

- Function source deleted.
- Test suite passes.
- PHASE2_HANDLER_BACKLOG.md row updated to ✅ DROPPED.

## Commit message

`chore(cleanup): drop generate_and_store_cycle_report (cycle gated off; f-cleanup-cycle-report)`

---

# Phase 9 — f-add-pg-stat-snapshot-logger

## Goal

Periodic `pg_stat_activity` snapshots so the next leak has a forensic
trail. Per Phase 2 of f-overnight-cleanup's deferred Open Q.

## Source material

- `scripts/_stats_log/` — existing stats-logger pattern (per
  `dispatch-stats-logger.ps1`). Mirror its shape for the SQL probe.
- `app/services/db_watchdog.py` — already queries `pg_stat_activity`;
  could be extended OR the new probe stays separate.

## Path

### Step 9.1 — New scheduler-worker job

Add an APScheduler cron in `app/services/trading_scheduler.py`:

```python
# Every 5 min: snapshot pg_stat_activity for forensics
scheduler.add_job(
    _run_pg_stat_snapshot_job,
    trigger="interval",
    minutes=5,
    id="pg_stat_snapshot",
    replace_existing=True,
)
```

Wrapper writes to a rotating log:
```python
def _run_pg_stat_snapshot_job():
    from app.db import SessionLocal
    from sqlalchemy import text
    with SessionLocal() as db:
        rows = db.execute(text("""
            SELECT pid, application_name, state, wait_event_type, wait_event,
                   EXTRACT(EPOCH FROM (NOW() - state_change))::int AS held_s,
                   LEFT(query, 100) AS q
            FROM pg_stat_activity
            WHERE application_name LIKE 'chili%'
              AND state IS NOT NULL
            ORDER BY held_s DESC NULLS LAST LIMIT 30
        """)).fetchall()
        # Write to scripts/_pg_stat_log/<UTC iso>.txt
        ...
```

### Step 9.2 — Tests

`tests/test_pg_stat_snapshot.py`:

1. ✅ Function writes a non-empty snapshot.
2. ✅ Failure swallowed (mock cursor to raise; function logs but
   doesn't crash the scheduler).

## Success criteria

- Cron job registered.
- Snapshot files appear in `scripts/_pg_stat_log/` post-deploy.
- Tests pass.

## Commit message

`feat(observability): periodic pg_stat_activity snapshot logger (f-add-pg-stat-snapshot-logger)`

---

# Combined CC report (after all phases)

Write ONE report at
`docs/STRATEGY/CC_REPORTS/<date>_f-overnight-jumbo-2026-05-06.md`
with:

```markdown
# CC_REPORT: f-overnight-jumbo-2026-05-06

## Outcome: <N> SHIPPED / <M> BLOCKED / <K> VERIFIED-NON-ISSUE

## Per-phase status

### Phase 1 — f-bracket-writer-stop-construction-fix
- Status: SHIPPED / BLOCKED / VERIFIED-NON-ISSUE
- Commit: <hash>
- Files: <count>
- Tests: <pass/total>
- Key finding: ...

### Phase 2 — f-leak-4-phase-2 (chili pydantic closures)
- ... same shape ...

(repeat for all 9 phases)

## Cross-phase observations

(things that turned up in multiple phases)

## Surprises / deviations

(per-phase or cross-cutting)

## What needs operator action

(things only operator can do post-deploy)

## Cookbook updates from this run

(any new patterns CC encountered worth adding to the running list)

## Combined commit summary

```
<hash> Phase 1 commit message
<hash> Phase 2 commit message
...
```
```

After report, mark NEXT_TASK.md `STATUS: DONE`. Single commit
covering the report + status update.

## Out of scope (jumbo-wide)

- **f-fix-autotrader-paper-fallback** — operator decision (flip flag
  vs code change); needs operator authorization, not CC time.
- **f-prefer-shadow-evidence** — needs ≥24h of shadow data
  accumulated.
- **f-paper-shadow-dashboard** — needs shadow data first.
- **f-cleanup-paper-book-json-placeholder** — needs ≥1 week of clean
  paper-shadow.
- **f-fix-pytest-bootstrap-kernel-pool** — Windows kernel pool issue
  reproduces inconsistently; CC can't reliably reproduce.
- **PED bracket-writer details beyond Phase 1** — Phase 1 is the fix;
  if it doesn't fully resolve, follow-up brief.
- **Memory saves of today's session findings** — Cowork-side task,
  not CC time.

## Final success criteria (jumbo-wide)

1. **At least Phase 1 SHIPPED.** Floor.
2. **All 9 phases attempted, status documented per phase.**
3. **Combined CC report covers all 9 phases.**
4. **No live-broker behavior change beyond Phase 1's fix.**
5. **No frozen-contract violations.**
6. **Brain-worker still functional post-each-commit.**
7. **All migrations sequential (next is 230).**
8. **No `git push --force`.**

When operator wakes, the CC report tells them exactly what shipped,
what blocked, and what needs operator action. Operator deploys +
verifies via mem_watcher trend, then queues the next round based on
post-deploy data.

**Sleep well.** Tomorrow's Cowork session reads the CC report and
queues the next round of follow-ups.
