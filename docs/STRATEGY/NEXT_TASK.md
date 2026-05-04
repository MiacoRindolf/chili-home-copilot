# NEXT_TASK: bracket-writer-respect-upside-targets

STATUS: DONE

## Goal

Stop the bracket writer from silently swapping the operator's upside profit-targets for downside stop-losses. Today's deploy revealed: when `CHILI_BRACKET_WRITER_CANCEL_COVERING_SELL=1`, the writer cancelled 5 covering limit-sells (operator's profit targets at +17% to +200% above entry) to free shares for SELL_STOPs. The shares now have downside protection but no upside take-profit on the broker's books — a strategic shift the operator did not authorize.

This task makes three coordinated changes so the writer respects existing protection, surfaces conflicts to the operator instead of resolving them unilaterally, and offers brain-judged replacement when a re-bracket is structurally needed.

After deploy:
1. The writer never again cancels a covering limit-sell on its own. The flag that enabled that behavior is OFF and the code path that uses it is gated by an explicit operator decision.
2. When a position has a covering limit but no stop, the writer logs a structured conflict and parks the trade in a new pending-decision state. The bracket reconciler emits the conflict on every sweep until the operator chooses.
3. When the operator chooses to replace (via UI or admin endpoint, both stubs in this task), the writer evaluates each side of the bracket through the brain — current price vs entry, brain's target/stop output, regime context — and only replaces when the new orders are more protective than the existing ones. No forced replacement.

The 5 already-cancelled covering limit-sells from today's deploy are accepted as a one-time operator loss. The writer evaluates each impacted ticker on the next bracket-sweep: if the brain's current target is still above current price and the position is otherwise unprotected on the upside, it queues a replacement target as a pending decision; if the brain says the trade thesis has shifted (e.g., current price has already passed the original target), it skips. Operator confirms each via the pending-decision surface.

## Why now

The operator stated explicitly: *"I want them to be managed correctly automatically and properly without bugs by chili."* Today's writer behavior failed that bar — it cancelled their authored upside targets to install downside stops, on its own initiative, without notice. The technical hard truth (Robinhood retail allows only one sell order per share, so true bracket pairs can't co-reserve shares) is real, but the writer was treating it as license to make a strategy decision rather than a fact requiring operator input.

The structural one-sell-per-share constraint stays. The fix is at the policy layer above it: the writer surfaces, the operator decides.

## Brain integration (reuse, don't rewrite)

- `app/services/trading/bracket_writer_g2.py::place_missing_stop` — the writer's primary entry point. The covered-by-existing-sell branch is the one that today silently cancels. After this task, that branch surfaces a pending-decision row instead of cancelling.
- `app/services/trading/bracket_reconciliation_service.py` — the sweep loop. Emits the new pending-decision discrepancy as part of `event=writer_action ... reason=existing_target_present_no_stop` so the existing audit funnel picks it up.
- `app/services/trading/stop_engine.compute_initial_bracket` (or whatever the brain's bracket-derivation function is — discover in code) — already returns the brain's stop_price + target_price for a position. The writer reuses this output to evaluate "is the operator's existing limit at or above the brain's target" (preserve) vs "below" (the brain wants tighter; surface as candidate replacement).
- `trading_bracket_intents.payload_json` — already a JSONB column. Carries the conflict payload (existing limit price/qty, brain's preferred target, current market price, regime) without a schema change. No new columns until Phase 1 of the position-identity refactor.
- `governance.py` — kill-switch primitive. The writer DOES NOT auto-resolve conflicts; conflicts route to the pending-decision surface. The operator's decision-write API uses the existing admin auth pattern; this task adds the endpoint stub but does not build the UI (that's Phase 7 of the broader initiative).

## Path

**Design principle: zero new magic numbers, zero new env-overridable hardcoded defaults, zero new auto-close paths, zero new auto-cancel paths.** Every decision threshold derives from brain output (already computed) or broker state (already observed). If you find yourself typing a literal price-comparison tolerance or a hardcoded list of "viable" conditions, stop and call it back to Cowork.

### Step 1 — Flip `CHILI_BRACKET_WRITER_CANCEL_COVERING_SELL=1` → `0` in compose

`docker-compose.yml` for the broker-sync-worker service. Single-line change. Comment above the line documenting why: "preserves operator's authored upside profit-targets; bracket writer surfaces conflicts via pending-decision instead of unilaterally cancelling." Operator restarts broker-sync-worker post-deploy to pick up.

### Step 2 — Modify `place_missing_stop` covered-by-existing-sell branch

In `bracket_writer_g2.py`, find the branch that today reads (paraphrased):

```python
if held_for_sells == broker_qty:
    if CANCEL_COVERING_SELL:
        cancel_covering_sells(...)
        proceed_to_place_stop(...)
    else:
        log_silent_exposure_warning(...)
        return skip_outcome
```

Replace with:

```python
if held_for_sells == broker_qty:
    # Position is fully reserved by an existing sell order (limit-sell or
    # similar). One-sell-per-share constraint at Robinhood retail means we
    # can't co-place a stop. This is operator-decision territory.
    #
    # Surface the conflict via the pending-decision surface (Step 3) and
    # park the intent. The bracket reconciler emits this on every sweep
    # until the operator resolves; the writer never auto-resolves.
    record_pending_bracket_decision(
        db, intent=intent, broker=broker, brain=brain_view,
        conflict='existing_sell_holds_all_shares',
    )
    return outcome(
        writer='place_missing_stop',
        ok=False,
        reason='existing_target_present_no_stop',
        decision_status='pending_operator',
    )
```

The `record_pending_bracket_decision` helper writes a structured row (Step 3). The outcome's `decision_status='pending_operator'` is a new field on the writer-action audit emit so the funnel knows this is awaiting operator input rather than a bug.

### Step 3 — Pending-decision surface (data only; UI is Phase 7)

Use `trading_bracket_intents.payload_json` to carry the pending decision. New JSON shape (no schema migration):

```json
{
  "pending_decision": {
    "kind": "existing_sell_holds_all_shares",
    "observed_at": "2026-05-04T20:14:00Z",
    "broker_state": {
      "qty": 150.0, "avg_price": 2.815, "held_for_sells": 150.0,
      "covering_orders": [
        {"order_id": "...", "type": "limit", "side": "sell",
         "qty": 150.0, "price": 3.30}
      ]
    },
    "brain_state": {
      "target_price": 3.2372,
      "stop_price": 2.2225,
      "current_price": 2.85,
      "regime": "..."
    },
    "options": [
      {"choice": "keep_target",
       "consequence": "no_downside_stop"},
      {"choice": "replace_with_stop",
       "consequence": "cancels_existing_limit_sell_and_places_stop_at_brain_price"},
      {"choice": "convert_to_trailing_stop",
       "consequence": "cancels_existing_limit_sell_and_places_trailing_stop_per_brain_atr"}
    ],
    "operator_choice": null
  }
}
```

The reconciler reads `pending_decision.operator_choice` on each sweep. While `null`, it skips the writer call for this intent and emits a `kind=pending_operator_decision` discrepancy (visible in audit + log).

### Step 4 — Admin endpoint stub for operator decision

Add `POST /api/admin/bracket-decisions/<bracket_intent_id>` that accepts a JSON body with `choice` (one of the values surfaced in `options`). The endpoint:

1. Validates the choice against the current `pending_decision.options` list (rejects unknown choices).
2. Sets `payload_json.pending_decision.operator_choice` and bumps `updated_at`.
3. Returns the updated row.

The reconciler picks up the choice on next sweep and routes to the appropriate writer action:
- `keep_target`: clears `pending_decision`, marks intent as `state=accepted_no_stop`, no broker action.
- `replace_with_stop`: cancels the listed covering orders, places stop at `brain_state.stop_price`, clears `pending_decision`.
- `convert_to_trailing_stop`: cancels the listed covering orders, places trailing-stop per the brain's ATR/regime output, clears `pending_decision`.

The endpoint stub goes in `app/routers/admin.py`. UI surface is **out of scope** — that's the autopilot settings page, Phase 7 of the broader initiative.

### Step 5 — Today's 5 cancelled limits — viability evaluation, not forced replacement

Per operator: *"Replace them if they're still viable, else don't force it."*

Add a one-shot helper in `bracket_writer_g2.py`: `evaluate_target_replacement(intent, brain_view, broker_view) -> dict | None`. Returns:

- `None` if the brain's target is at-or-below the current market price (target already realized or no longer ahead — not viable).
- A pending-decision row (Step 3 shape) with `kind="cancelled_limit_replacement_candidate"` if the brain's target is above current price AND above entry price AND the position is unprotected on the upside.

The reconciler runs this once per intent, on the next sweep after this commit deploys, for the 5 trades whose covering limit was cancelled today (1812 AIDX, 1813 CCCC, 1814 CRDL, 1821 TLS, 1822 VFS — identifiable by their 19:14:18-19:14:57 timestamp on intent.last_diff_reason). If the brain says viable, a pending-decision row appears; operator decides via Step 4 endpoint.

No forced replacement, no automatic re-cancel, no surprise broker calls.

### Step 6 — Tests

Add `tests/test_bracket_writer_respect_upside_targets.py`:

- **scenario A: existing limit + missing stop → pending decision, no broker action.** Mock writer with `held_for_sells == broker_qty`. Assert: no `place_stop` call, no `cancel` call, `pending_decision` row written with the three options, audit row emitted with `decision_status='pending_operator'`.
- **scenario B: operator chooses `keep_target` → intent transitions to accepted_no_stop, no broker action.** Mock the admin endpoint, write the choice. Run reconciler. Assert: no broker call, intent state updated, `pending_decision` cleared.
- **scenario C: operator chooses `replace_with_stop` → cancel + place sequence runs.** Assert: cancel called once with the listed covering order_id, place_stop called with brain's stop_price, both broker calls verified, `pending_decision` cleared.
- **scenario D: operator chooses `convert_to_trailing_stop`.** Same shape as C but routes to the trailing-stop placement path.
- **scenario E: cancelled-limit replacement viability evaluator.** Mock a Trade row with `intent.last_diff_reason='inverse_reconcile_reopen'` and a recently-cancelled limit, brain target ABOVE current price → pending-decision row appears with `kind="cancelled_limit_replacement_candidate"`. Same scenario but brain target BELOW current price → returns None, no pending-decision.

All scenarios use `chili_test`. No live network.

### Step 7 — Documentation

Add a short paragraph to `docs/FAST_PATH_HANDOFF.md` (or wherever the bracket flag inventory lives) documenting:

- The one-sell-per-share constraint at Robinhood retail (architectural fact).
- Why `CHILI_BRACKET_WRITER_CANCEL_COVERING_SELL` defaults to `0` going forward.
- The pending-decision surface as the operator-input mechanism.
- Forward pointer to the autopilot-settings UI (Phase 7).

## Constraints / do not touch

- **No magic numbers.** Brain output (target_price, stop_price, ATR, regime) is the source of every threshold. Current price comes from the existing `fetch_quote` path. No literal tolerance, no hardcoded "viable" thresholds.
- **No new auto-cancel paths.** The writer NEVER cancels broker orders without an operator-recorded choice. The cancel call appears only in the `replace_with_stop` and `convert_to_trailing_stop` resolution paths, both gated by `operator_choice != null`.
- **No new auto-place paths.** Same logic for placement — the writer only places when the operator has chosen a resolution that requires placement.
- **No new env-overridable defaults.** The `CHILI_BRACKET_WRITER_CANCEL_COVERING_SELL=0` flip in compose is the LAST env-flag change for this surface. Going forward, decisions live in `pending_decision.operator_choice`, not env vars.
- **Do NOT remove the `place_missing_stop` function** — it's reused by the resolution paths in Step 4. Only the covered-by-existing-sell branch's behavior changes.
- **Do NOT modify the inverse-reconcile path** from `broker-truth-self-heal`. That ships independently and the new pending-decision logic runs after inverse-reconcile reopens a Trade row.
- **Do NOT auto-resolve any of today's 5 cancelled limits.** Step 5 is evaluator-only; it surfaces candidates as pending-decision rows. Operator decides each.
- **Tests use `_test`-suffixed DB.** PROTOCOL Hard Rule 5.
- **No `git push --force` to main.** PROTOCOL Hard Rule 4.
- **No new schema migrations.** Pending-decision data lives in existing `payload_json`. The new column work is Phase 1 of the position-identity refactor (separate initiative).

## Out of scope

- **Autopilot settings UI** — the page the operator described (per-broker enable/disable, per-strategy toggles, broker-routing rules). That's Phase 7 of the broader initiative; this task only ships the data model + admin endpoint that the UI will eventually consume.
- **Position-identity refactor** — the three-layer split (decision → envelope → position) is the next initiative-shaped task. This task uses today's Trade-row model as-is.
- **Replacing existing API admin auth** — the new endpoint reuses existing admin auth patterns. Don't redesign auth here.
- **Trailing-stop primitive itself** — `convert_to_trailing_stop` resolution path assumes a trailing-stop placement helper exists in the broker layer. If it doesn't yet, surface it as an Open Question in the CC report and stub the resolution path with a clear NOT_IMPLEMENTED return that surfaces to the operator. Don't build trailing-stop placement in this task.
- **Manual replacement of today's 5 cancelled limits via Cowork-staged script.** Operator declined. The Step 5 evaluator handles "still viable" judgment; no script.
- **Schema-removal of mig 223's orphan column.** Bundled with Phase 1 of the position-identity refactor per the agreed plan.

## Success criteria

1. **Two commits, both pushed:**
   - `fix(bracket): respect operator upside-targets — pending-decision surface + flag flip`
   - `docs(strategy): bracket-writer-respect-upside-targets CC report + mark NEXT_TASK done`
2. **Magic-number audit clean.** CC report enumerates any literal numeric/string-list values added; expected count is zero net new behavioural literals (audit-trail strings and JSON keys excepted).
3. **All existing tests still pass.** `pytest tests/test_bracket_writer_g2.py tests/test_bracket_reconciliation_service.py tests/test_alerts.py -v`. The covered-by-existing-sell behavior change requires updating any prior scenario that asserted the cancel-and-place sequence — that's expected, not a regression.
4. **6 new test scenarios (A-E + cancelled-limit evaluator) pass.**
5. **Live verification (post-deploy):**
   - `docker compose ps broker-sync-worker` shows the env var `CHILI_BRACKET_WRITER_CANCEL_COVERING_SELL=0` after `docker compose up -d broker-sync-worker`.
   - At least one `pending_decision` row appears in `trading_bracket_intents.payload_json` for any of the 9 currently-open equity positions whose covering limit got cancelled today (this would indicate the Step 5 evaluator is correctly identifying replacement candidates).
   - Zero new `cancelled` covering-limit-sell orders at Robinhood within 30 minutes of deploy.
6. **Admin endpoint reachable.** `curl -X POST /api/admin/bracket-decisions/<id> -d '{"choice":"keep_target"}'` returns 200 with the updated row JSON. Verify against an actual bracket_intent_id in the running DB.
7. **CC_REPORT** at `docs/STRATEGY/CC_REPORTS/2026-05-04_bracket-writer-respect-upside-targets.md`. Include:
   - Magic-number audit
   - Pre-deploy and post-deploy state of the 9 reopened equity bracket_intents (`payload_json` contents)
   - The trailing-stop placement helper status (does it exist? if not, what's the current stub path?)
   - Whether any CONTRADICTION or pending-decision rows appeared in the 30-min post-deploy window

## Rollback plan

- **Code rollback:** `git revert <fix-commit>`. The `pending_decision` JSON keys orphan in any rows that got written; harmless (consumers ignore unknown keys). The flag goes back to whatever was committed before. The endpoint disappears.
- **Flag-only rollback:** if the new behavior shows a regression but the rest is fine, the operator can flip `CHILI_BRACKET_WRITER_CANCEL_COVERING_SELL=1` back without reverting code — the pending-decision surface still works alongside the old auto-cancel. (Not recommended; defeats the purpose.)
- **Hard-stop:** flip the writer's whole feature flag (`CHILI_BRACKET_WRITER_G2_PLACE_MISSING_STOP=0`) — the bracket writer becomes a no-op. The 9 open equity positions retain their currently-confirmed SELL_STOPs at Robinhood (those are independent of CHILI's writer state); operator manually manages going forward.
- **No live broker rollback needed.** This task does NOT cancel or place broker orders during deploy.

## Verification commands (for the executor + the operator)

```powershell
# Pre-deploy: confirm the 9 SELL_STOPs are still confirmed
docker compose exec -T broker-sync-worker python /app/scripts/_rh_probe_stops_now.py

# Post-deploy: env var flip confirmed
docker compose exec -T broker-sync-worker sh -c 'env | grep CHILI_BRACKET_WRITER_CANCEL_COVERING_SELL'
# Expect: CHILI_BRACKET_WRITER_CANCEL_COVERING_SELL=0

# Watch the pending-decision rows appear on next sweep
docker compose exec -T postgres psql -U chili -d chili -c "
  SELECT id, ticker, intent_state, last_diff_reason,
         payload_json->'pending_decision'->>'kind' AS pending_kind,
         payload_json->'pending_decision'->>'observed_at' AS observed_at,
         payload_json->'pending_decision'->>'operator_choice' AS choice
  FROM trading_bracket_intents
  WHERE trade_id IN (1812,1813,1814,1817,1818,1819,1820,1821,1822)
  ORDER BY trade_id;
"

# Watch the writer NOT cancel anything new
docker compose logs broker-sync-worker --since 30m -f | Select-String -Pattern "cancel|pending_decision|existing_target_present_no_stop|kind=pending_operator_decision"

# Tests
$env:TEST_DATABASE_URL='postgresql://chili:chili@localhost:5433/chili_test'
pytest tests/test_bracket_writer_respect_upside_targets.py -v
```

## Open questions for Cowork (surface in your CC_REPORT)

1. **Trailing-stop placement helper presence.** Does a function exist today that places a Robinhood trailing-stop sell? If yes, document the entry point. If no, the `convert_to_trailing_stop` resolution path stubs to NOT_IMPLEMENTED and the option doesn't appear in the pending-decision options list (only `keep_target` and `replace_with_stop`). Surface either way.
2. **Brain's target/stop reuse.** The viability evaluator depends on brain output for current target_price/stop_price for a given (ticker, regime). Confirm the existing brain function (likely `compute_initial_bracket` or similar) returns this in a stable shape; if it doesn't, surface what it does return and how the evaluator should adapt.
3. **`fetch_quote` blocked tickers.** Some tickers may be in the yf-breaker OPEN state or the Massive/Polygon block list. The viability evaluator needs current price; if `fetch_quote` returns None, defer the evaluation rather than treating it as "not viable" (no signal ≠ negative signal). Confirm this is what your implementation does and surface the count of deferrals if any.
4. **Admin endpoint auth.** Reusing existing admin auth pattern is the brief's intent, but the precise pattern (cookie? bearer? IP allow?) lives in `app/routers/admin.py`. Document the choice in the CC report so the future autopilot-settings UI knows what to integrate against.

## Forward pointer

After this task ships and the operator confirms the writer is no longer eating their profit targets, the next initiative-shaped step is the position-identity refactor design doc (per `docs/STRATEGY/CURRENT_PLAN.md`). The pending-decision surface in this task becomes part of that refactor's UI scope (Phase 7 of the broader plan).
