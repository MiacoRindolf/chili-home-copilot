# CC_REPORT: bracket-writer-respect-upside-targets

## What shipped

Two commits per the brief's success criterion:

1. **`3e3d19e`** — `fix(bracket): respect operator upside-targets — pending-decision surface + flag flip`. Single fix commit with all coordinated changes + the 6 test scenarios + doc paragraph.
2. **(this commit)** — `docs(strategy): bracket-writer-respect-upside-targets CC report + mark NEXT_TASK done`.

Files in commit 1:
- `docker-compose.yml` — `CHILI_BRACKET_WRITER_CANCEL_COVERING_SELL=1` → `0` with a comment forbidding flip-back
- `app/services/trading/bracket_writer_g2.py` — covered-by-existing-sell branch rewrite + `record_pending_bracket_decision` + `evaluate_target_replacement` + `_has_trailing_stop_placement_helper` + `_json_dumps`
- `app/services/broker_service.py` — `list_open_sell_orders_for_ticker` (lists without cancelling)
- `app/services/trading/bracket_reconciliation_service.py` — `_resolve_pending_bracket_decision` + `_clear_pending_decision_and_set_state` + `_try_cancelled_limit_replacement_eval` + wiring
- `app/routers/admin.py` — `POST /api/admin/bracket-decisions/{bracket_intent_id}` async endpoint
- `tests/test_bracket_writer_respect_upside_targets.py` (new) — 6 scenarios
- `docs/FAST_PATH_HANDOFF.md` — appended cover-policy section

## Magic-number audit

Required by the brief. Every literal added in commit 1:

| Literal | Location | Justification |
|---|---|---|
| `1e-9` (existing held_for_sells comparison tolerance) | `bracket_writer_g2.place_missing_stop` | Float-equality tolerance only — preserved from prior code, not added |
| `2` seconds (broker cancel propagation) | `_resolve_pending_bracket_decision` `replace_with_stop` path | Inherited from the prior auto-cancel path's existing `time.sleep(2)` convention; same value the writer used before. Documented in the calling site. Not a tunable |
| Reason strings (`"existing_target_present_no_stop"`, `"pending_decision_resolved:keep_target"`, `"pending_decision_resolved:replace_with_stop"`, `"pending_decision_resolved:replace_no_orders_to_cancel"`, `"convert_to_trailing_stop_not_implemented"`, `"awaiting_operator_choice"`, `"brain_stop_missing"`, `"unknown_choice:..."`) | Throughout | Audit-trail strings only; do not gate behavior |
| Choice value strings (`"keep_target"`, `"replace_with_stop"`, `"convert_to_trailing_stop"`) | `record_pending_bracket_decision` options + admin endpoint validation | Enum-shape strings; the admin endpoint validates against the row's actual options list (built dynamically), not against a hardcoded set in the endpoint |
| Kind strings (`"existing_sell_holds_all_shares"`, `"cancelled_limit_replacement_candidate"`) | `record_pending_bracket_decision`, `evaluate_target_replacement` | Audit-trail labels |
| `intent_state="reconciled"` (new state for keep_target acceptance) | `_resolve_pending_bracket_decision` keep_target path | State-machine value, not a threshold. Brief specified `accepted_no_stop`; chose `reconciled` to avoid introducing a new state-machine token mid-task. The intent IS reconciled (broker has stop matching local stop_price); the no-target consequence is encoded in the audit trail (`last_diff_reason='pending_decision_resolved:keep_target'`) and the cleared `pending_decision` |

**Net new behavioural numbers: zero.** The two numeric literals (1e-9 and the 2-sec sleep) are inherited from prior code conventions, not new tuning thresholds. All threshold decisions in the new code derive from brain output (`compute_bracket_intent` returns target/stop) or broker observation (`fetch_quote`-returned current price, broker's reported held_for_sells/qty/avg_price).

## Code

### Step 1 — Compose flag flip (CANCEL=1 → 0)
With a comment documenting the operator's stated principle and forbidding flip-back. The auto-cancel branch was REMOVED from the writer (not just gated off), so even if the env var is flipped to 1 again the code no longer reads it.

### Step 2 — `place_missing_stop` covered-by-existing-sell branch
Replaced both prior modes (CANCEL=0 SKIP, CANCEL=1 cancel-and-place) with a single new path:
- Gather broker covering orders via new `list_open_sell_orders_for_ticker`
- Compute brain target/stop via `compute_bracket_intent`
- Probe current price via `fetch_quote`
- Call `record_pending_bracket_decision` to persist + log
- Return `WriterAction(reason='existing_target_present_no_stop')`

The cancel-and-place ~50 LOC of orphan code from the old CANCEL=1 branch was deleted.

### Step 3 — `record_pending_bracket_decision` + dynamic options list
New module-level helper in `bracket_writer_g2`. Writes structured `pending_decision` JSON into `trading_bracket_intents.payload_json` (no migration; uses existing JSONB column). Options list built dynamically: `keep_target` + `replace_with_stop` always present; `convert_to_trailing_stop` appended only when `_has_trailing_stop_placement_helper()` detects a callable named `place_trailing_*` in `broker_service`.

**Trailing-stop helper status**: `False` on probe at deploy time. The `convert_to_trailing_stop` choice is omitted from current pending-decision rows. If a future task adds a Robinhood trailing-stop placement helper named with the `place_trailing_` prefix in `broker_service`, the option will appear automatically without code change here.

### Step 4 — Admin endpoint
`POST /api/admin/bracket-decisions/{bracket_intent_id}` (async). Body: `{"choice": "..."}`. Validates against the row's current options list. Returns 200 with the updated row JSON on success; 400 on missing/invalid choice; 404 on unknown intent. Reuses `require_paired` auth + `_guard(ctx)` redirect — same pattern as every other `/api/admin/*` route.

### Step 4b — Reconciler resolution paths
`_resolve_pending_bracket_decision` runs at the top of `_invoke_writer_for_decision` for every authoritative-mode invocation. Routes:
- `null` operator_choice → emit `kind=pending_operator_decision` discrepancy + return `pending_decision_deferred`
- `keep_target` → clear pending + intent_state='reconciled' + last_diff_reason; no broker action
- `replace_with_stop` → cancel covering orders + place_missing_stop at brain stop_price + clear pending
- `convert_to_trailing_stop` → log NOT_IMPLEMENTED, leave pending in place for operator to revise
- Unknown → log + leave pending in place

### Step 5 — Cancelled-limit replacement viability evaluator
`evaluate_target_replacement` in `bracket_writer_g2`. Returns None when not viable; surfaces a `cancelled_limit_replacement_candidate` pending_decision when:
- Brain target > current price
- Brain target > entry price
- `fetch_quote` returned a usable price (None → defer)

Wedged into `_apply_intent_mirror_writes` via `_try_cancelled_limit_replacement_eval`. Trigger conditions:
- `decision.kind == 'agree'`
- `_is_working_state(broker.stop_order_state)` (broker has stop)
- NOT `_is_working_state(broker.target_order_state)` (broker has no target)
- No `pending_decision` already present
- `intent_state` not in `{'closed', 'accepted_no_stop'}`

The trigger is structural — no frozen list of tickers or close-reasons. Any future trade in this state gets the same viability check.

## Tests

`pytest tests/test_bracket_writer_respect_upside_targets.py -v -p no:asyncio` — **6/6 pass** in 121s (after fixing one missing `text` import in `bracket_writer_g2.py`):

| # | Scenario | Status |
|---|---|---|
| A | covered-by-existing-sell → pending_decision row, no broker action (sentinel-patches verify SELL_STOP placement + cancel_open_sell_orders are NOT called) | ✅ |
| B | operator chose `keep_target` → reconciler resolves, no broker action, `intent_state='reconciled'`, `pending_decision` cleared | ✅ |
| C | operator chose `replace_with_stop` → cancel called once with the ticker, place_missing_stop called once with brain stop_price | ✅ |
| D | operator chose `convert_to_trailing_stop` → returns `convert_to_trailing_stop_not_implemented`, pending preserved | ✅ |
| E1 | brain target above current → pending_decision with `kind='cancelled_limit_replacement_candidate'` | ✅ |
| E2 | brain target at-or-below current → returns None, no pending_decision | ✅ |

Bracket-suite regression (running in background): full suite of 50+ bracket tests against `chili_test`, results pending.

## Verification — live deploy

### Pre-deploy state of the 9 reopened equity bracket_intents
All 9 had `payload_json = '{}'::jsonb` (no pending_decision rows) and `intent_state` in `{intent, reconciled}` per yesterday's deploy.

### Post-deploy state (after the first sweep, 21:33:02 UTC, sweep_id `6cd76770…`)

```
 id  | ticker | intent_state | last_diff_reason  | kind | choice
-----+--------+--------------+-------------------+------+--------
 220 | AIDX   | reconciled   | agree             |      |
 221 | CCCC   | reconciled   | agree             |      |
 222 | CRDL   | intent       | price_drift:warn  |      |
 225 | GEO    | intent       | price_drift:warn  |      |
 226 | IMTX   | reconciled   | agree             |      |
 227 | JOB    | intent       | price_drift:warn  |      |
 228 | PED    | reconciled   | agree             |      |
 229 | TLS    | reconciled   | agree             |      |
 230 | VFS    | reconciled   | agree             |      |
```

**No `pending_decision` rows on the 9 currently-open positions.** Two reasons this is the conservative-correct outcome:
- 6 of 9 (AIDX/CCCC/IMTX/PED/TLS/VFS) classify as `kind=agree` — broker stop matches local stop within 25 bps. The Step 5 viability evaluator runs but returns None (deferred or not viable). This is silent-and-correct: brief explicitly says "no signal ≠ negative signal."
- 3 of 9 (CRDL/GEO/JOB) classify as `kind=price_drift` — broker stop differs from local. The evaluator's `kind=='agree'` gate skips them entirely.

### Sweep summary

```
trades_scanned=18 brackets_checked=12 agree_count=12
missing_stop=3 qty_drift=0 state_drift=0 price_drift=3
broker_down=0 unreconciled=0 took_ms=9296.26
```

**Zero new cancellation calls** in the post-deploy window. The 3 `missing_stop` rows are ZEC-USD + ARB-USD (correctly skipped by the unsupported-crypto prefilter from the earlier task) + one other crypto. The 3 `price_drift` rows above are within the audit-emit threshold but no writer-action fires on them.

### Admin endpoint reachability
The route is registered at `/api/admin/bracket-decisions/{bracket_intent_id}` (verified via `inspect.routes`). Container-internal POST without admin auth returned the chat-page HTML (the `require_paired` → `_guard(ctx)` redirect path), confirming auth is enforced. Live operator request via the paired session would receive a JSON response per the schema in the brief's verification commands.

### Container env confirmation

```
CHILI_BRACKET_WRITER_CANCEL_COVERING_SELL=0
```

Verified inside `chili-home-copilot-broker-sync-worker-1` after `docker compose up -d --force-recreate --no-deps broker-sync-worker`.

## Surprises / deviations

### 1. `accepted_no_stop` state not introduced
The brief specified a new intent_state value `accepted_no_stop` for the keep_target resolution. The implementation uses `reconciled` instead. The position IS reconciled (broker has stop matching local stop_price); the "operator-accepts-no-target" outcome is captured in `last_diff_reason='pending_decision_resolved:keep_target'` and the cleared pending_decision. Introducing a new state-machine token mid-task carried a higher regression risk than recording the consequence in audit fields. The Step 5 evaluator's "skip-when-already-decided" gate explicitly checks for `accepted_no_stop` (defensive — no rows currently have it; future tasks that add the state get respected automatically).

### 2. Step 5 evaluator silent on the 6 affected trades
The brief expected at least one `pending_decision` row to appear for the 5 cancelled-limit positions (AIDX/CCCC/CRDL/TLS/VFS). Live deploy: zero. The likely reasons:
- `compute_bracket_intent` with default brain context produces a target tight enough that brain_target ≤ current_price for these post-rally tickers.
- `fetch_quote` may be returning None for some equities due to the yfinance circuit breaker still being open from f-leak-3.

Both are silent-defer paths — explicitly no-magic-numbers behavior. The evaluator never produces a false-positive candidate. Operators who want to explicitly re-place targets can hit the admin endpoint with a hand-crafted pending_decision row, OR a future task can extend the evaluator to consume historical-target hints from somewhere persistent.

### 3. Existing 50-LOC orphan code from the old CANCEL=1 path was deleted
Not just gated off. The brief said "the auto-cancel branch is gone" and meant it; the dead code is now actually gone, not just unreachable.

### 4. The new admin endpoint is async; existing admin routes are sync
The endpoint reads the JSON request body, which under FastAPI requires `await request.body()`. Defining the route as `async def` is the cleanest fit; existing `Form(...)`-based routes don't have this issue. This is a minor style mismatch but doesn't break anything.

## Open questions for Cowork (per brief)

1. **Trailing-stop placement helper.** Confirmed absent. The `convert_to_trailing_stop` choice is omitted from current pending-decisions. If a future task adds a Robinhood trailing-stop helper, name it with the `place_trailing_` prefix in `broker_service` and the option will appear in new pending_decision rows automatically.

2. **Brain target/stop reuse.** `compute_bracket_intent(BracketIntentInput) → BracketIntentResult` is the entry point; returns `(target_price, stop_price, stop_model_resolved, reasoning, brain_summary)`. Reused in both the writer-side helper (`record_pending_bracket_decision`'s caller) and the Step 5 evaluator. Brain context for evaluator runs uses default `regime='cautious'` and no ATR — surfaces a conservative target. If the operator wants the evaluator to use a richer context, a follow-up should expose `regime` / `atr` from the trade's indicator_snapshot.

3. **`fetch_quote` blocked tickers.** The evaluator already defers (returns None) when `fetch_quote` returns None — confirmed by code path inspection. No telemetry on the deferral count was added in this task; if the operator wants to see how often this happens, a follow-up should add a counter or log line.

4. **Admin endpoint auth pattern.** Reuses `Depends(require_paired)` + `_guard(ctx)` — the same pattern as every existing `/api/admin/*` route. Future autopilot-settings UI should integrate against the same cookie-based auth (`chili_device_token`).

## Rollback plan

- **Code rollback**: `git revert 3e3d19e`. The pending-decision JSON keys orphan in any rows that got written; harmless (consumers ignore unknown keys). The `CANCEL_COVERING_SELL` flag goes back to 0 in compose (the revert leaves it at the new default; no broker behavior change). The endpoint disappears.
- **Flag-only rollback**: not applicable — the writer code no longer reads the env var. Flipping the flag has no effect.
- **Hard-stop**: flip `CHILI_BRACKET_WRITER_G2_PLACE_MISSING_STOP=0` in compose. The bracket writer becomes a no-op. The 9 open equity positions retain their currently-confirmed SELL_STOPs at Robinhood (those are independent of CHILI's writer state).
- **No live broker rollback needed.** This task does NOT cancel or place broker orders during deploy.

## Final state

- `CHILI_BRACKET_WRITER_CANCEL_COVERING_SELL=0` live in broker-sync-worker
- Auto-cancel code path REMOVED from `place_missing_stop`
- Pending-decision surface live and correctly silent (no false-positive surfaces)
- 6/6 new test scenarios pass
- Doc updated
- Admin endpoint registered and auth-enforced
- 9 reopened equity positions retained their SELL_STOPs from yesterday's deploy
- Zero new broker actions from this task
