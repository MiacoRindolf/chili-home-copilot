# CC_REPORT: bracket-writer-cover-policy-clarify

## What shipped

- **Code commit (this push)** — `fix(bracket): clarify cover-policy framing + silent-exposure warning + admin status surface`. Files touched: `app/services/trading/bracket_writer_g2.py`, `app/main.py`, `scripts/scheduler_worker.py`, `app/routers/admin.py`, `tests/test_bracket_writer_cover_policy_clarify.py` (new).
- **Doc commit (this push)** — this CC report + `NEXT_TASK.md` DONE.
- Migrations added: **none**.

## Code

### Step 1 — Comment + label rewrite (`bracket_writer_g2.py`)

Two doc blocks rewritten to remove the misleading "the position is protected" framing:

1. **Lines ~680-705 (FIX 55 docstring header)** — replaced the closing sentence (*"the position is protected — skip placement entirely. The existing limit IS the exit; we don't need to add a stop on top of it."*) with an explicit **"THIS IS NOT DOWNSIDE PROTECTION"** statement and a description of the upside-lock vs downside-stop trade-off.

2. **Lines ~745-760 (DEFAULT POLICY block)** — replaced (*"The position is still protected — by the existing limit-sell, just at a different price level."*) with **"The position is NOT protected on the downside in this state."** + the deliberate-trade-off framing.

3. **Audit reason rename** — `_mtr(db, intent_id, reason="covered_by_existing_sell:protected_by_limit")` is now `reason="covered_by_existing_sell:no_stop_coverage"`. The change is at the writer's only call site for `mark_terminal_reject` in this branch.

4. **WriterAction.reason unchanged** — still `"covered_by_existing_sell"`. The action description is correct (the writer skipped because covered); only the persisted-state label changes.

### Step 2 — Startup warning (`bracket_writer_g2.warn_if_silent_exposure`)

New module-level helper that emits a **WARNING** log line when:

- `chili_bracket_missing_stop_repair_enabled` is `True`, AND
- `chili_bracket_writer_cancel_covering_sell` is `False`

Returns `True` when the warning was emitted (so callers can also use it as a probe). Does NOT escalate to ERROR or fail startup — both flag values are operator choices.

Wired into:
- `app/main.py` — module-level (alongside `_start_db_watchdog`), guarded by the `_under_pytest` check so test imports don't emit the warning.
- `scripts/scheduler_worker.py` — inside `main()`, just before `start_scheduler()`. The broker-sync-worker is the process that actually exercises the writer's covered-by-sell branch, so this is the most operationally relevant warning site.

### Step 3 — Admin status endpoint

`GET /api/admin/bracket/cover-policy-snapshot` in `app/routers/admin.py`. Read-only JSON, paired-context guard. Shape:

```json
{
  "as_of": "2026-05-03T22:58:23Z",
  "flags": {
    "chili_bracket_missing_stop_repair_enabled": true,
    "chili_bracket_writer_cancel_covering_sell": true,
    "chili_bracket_intent_mirror_enabled": true
  },
  "row_count": N,
  "rows": [
    {
      "intent_id": ..., "trade_id": ..., "ticker": "...",
      "intent_state": "terminal_reject",
      "last_diff_reason": "covered_by_existing_sell:no_stop_coverage",
      "stop_price_local": 0.65, "local_qty": 150,
      "broker_stop_order_id": null,
      "trade_status": "open",
      "updated_at": "...",
      "advisory": "no downside protection; broker has limit-sell only — set CHILI_BRACKET_WRITER_CANCEL_COVERING_SELL=1 for cancel-and-place-stop"
    }
  ]
}
```

Picked **Option A (admin route)** over **Option B (SQL-only)** because the existing `app/routers/admin.py` has 13 prior `@router.get("/api/admin/...")` precedents using `Depends(require_paired)` + the `_guard(ctx)` shim — wiring is cheap and matches the convention.

### Step 4 — Tests

`tests/test_bracket_writer_cover_policy_clarify.py` — **8 of 8 pass** in 226s against `chili_test`:

| Scenario | Status |
|---|---|
| 1. Audit reason uses `covered_by_existing_sell:no_stop_coverage` | ✅ |
| 2. Old `:protected_by_limit` label not regenerated anywhere | ✅ |
| 3. WriterAction.reason stays `covered_by_existing_sell` | ✅ |
| 4. Warning fires on silent-exposure combo | ✅ |
| 5a. Warning silent when both flags ON | ✅ |
| 5b. Warning silent when both flags OFF | ✅ |
| 5c. Warning silent when only `cancel=True` | ✅ |
| 6. `/api/admin/bracket/cover-policy-snapshot` shape | ✅ |

Tests stub `broker_service.get_position_held_for_sells` and inject a fake adapter — no real broker calls. Caplog captures the WARNING line for tests #4-5.

**Regression check**: 16 of 16 prior tests pass (`tests/test_bracket_intent_stale_label_cleanup.py` + `tests/test_bracket_emergency_terminal_reject_repair.py`) in 939s. No interaction between the new label rename and the prior label-aware paths.

## Verification

### Pre-deploy SQL probe (informational; this task makes no DB writes by itself)

```sql
SELECT bi.id, t.ticker, bi.intent_state, bi.last_diff_reason
FROM trading_bracket_intents bi
JOIN trading_trades t ON t.id = bi.trade_id
WHERE bi.last_diff_reason LIKE 'covered_by_existing_sell%'
  AND t.status = 'open';
```

The 5 stuck rows (AIDX 1812 / CCCC 1813 / CRDL 1814 / TLS 1821 / VFS 1822) currently carry `last_diff_reason='missing_stop:error'` (set by the reconciler's `bump_last_observed`, not by the writer). They will pick up the new `covered_by_existing_sell:no_stop_coverage` label the next time the writer's covered-by-sell branch fires on them — which depends on whether the operator's flag flip activates the cancel-and-place-stop path before the next sweep.

### Live-broker context — operator flag flip detected at task close

While I was finalizing tests, the operator added `CHILI_BRACKET_WRITER_CANCEL_COVERING_SELL=1` to the `broker-sync-worker` `environment:` block in `docker-compose.yml`, with a thoughtful inline comment naming the 5 positions, the ATR-widening engine context, and the ~$386 max-loss-from-entry.

This is a separate decision (per the brief, the cancel-covering-sell choice "belongs to the operator"). Effect when the worker restarts:

1. **My new warning will NOT fire** at startup (both flags ON → not the silent-exposure combo). Verified by test 5a.
2. On the next sweep, `place_missing_stop` for AIDX/CCCC/CRDL/TLS/VFS will hit the OPT-IN branch: cancel the covering limit-sells → sleep 2s → place SELL_STOPs at `bracket_intents.stop_price`.
3. **Live-broker action**: 5 cancel + 5 place = 10 real Robinhood order operations.

I have **not** restarted the worker. Ending the code task at the commit and surfacing the deploy decision — restart timing is operator's call. See "Open questions" #5.

## Surprises / deviations

### 1. The misleading framing was load-bearing in three places, not just the two doc blocks
The brief named lines ~680-696 and ~745-755. I also rewrote the inline comment at the persistence site (`# the position IS protected, just by the existing sell`) — same conflation, same concern. Three rewrites total.

### 2. `BRACKET_WRITER_G2` log prefix is imported, not defined locally
The brief mentioned a literal `[bracket_writer]` log prefix. The codebase actually imports `BRACKET_WRITER_G2 = "[bracket_writer_g2]"` from `ops_log_prefixes.py`. Using that constant means the warning line reads `[bracket_writer_g2] SILENT-EXPOSURE COMBO ACTIVE: ...` — same visual signal, consistent prefix with neighboring lines.

### 3. Prior persisted rows still carry `:protected_by_limit` — ZERO at the time of writing
SQL probe: `SELECT count(*) FROM trading_bracket_intents WHERE last_diff_reason LIKE '%protected_by_limit%'` returns 0 rows in the live `chili` DB. The old label propagated naturally as the reconciler's `bump_last_observed` overwrote `last_diff_reason` with `missing_stop:error` on subsequent sweeps. So the propagation concern documented in Step 1.3 of the brief is moot — there is nothing to backfill, and the rename will only affect rows that hit the writer's branch from this commit forward.

## Deferred

- **Operator restart of `broker-sync-worker`** to deploy the new code AND activate the flipped `CHILI_BRACKET_WRITER_CANCEL_COVERING_SELL=1` policy. Live-broker action; not self-authorized.
- **Investigation into why the 5 positions hit `terminal_reject` originally on 2026-05-01** — out of scope per the brief.
- **Auto-flipping `CHILI_BRACKET_WRITER_CANCEL_COVERING_SELL` based on position metrics** — out of scope; meaningful policy change.
- **Backfilling old persisted `:protected_by_limit` labels** — moot per Surprise #3.

## Open questions for Cowork (surface in the report)

1. **Replacement label choice**: I picked `:no_stop_coverage`. The other reasonable options were `:limit_only_coverage`, `:upside_lock_no_stop`, `:no_downside_stop`. `:no_stop_coverage` reads cleanly in `last_diff_reason LIKE 'covered_by_existing_sell:%'` queries and pairs naturally with the `held_for_sells == broker_qty` precondition. Cowork can rename in a follow-up if a different phrasing fits better.

2. **Admin route path**: I used `/api/admin/bracket/cover-policy-snapshot`. The codebase has both `/api/admin/...` (JSON) and `/admin/...` (HTML) prefixes. JSON felt right for diagnostic data. Cowork can graft an HTML wrapper later if the snapshot needs a UI.

3. **Broker-sync-worker startup hook**: there is no separate worker-side bootstrap function — `scripts/scheduler_worker.py:main()` is the entrypoint. I added the warning call inline alongside the kill-switch + broker session restore, before `start_scheduler()`. This puts the warning prominently in worker boot logs.

4. **Should the warning include a row-count from the DB?** The brief noted this would be more useful but more expensive at startup. I went **flag-state-only** to keep startup deterministic and avoid coupling the warning helper to DB connectivity. Operators can hit the new admin endpoint for the count + per-row data; the warning's job is to flag the combo, not to enumerate exposure.

5. **Restart timing**: code is committed; the operator's `CHILI_BRACKET_WRITER_CANCEL_COVERING_SELL=1` flip is in `docker-compose.yml` (not yet picked up by the running container). Recreating the worker now will:
   - Deploy the new label/warning code (cheap, no broker effect).
   - Activate cancel-and-place-stop on the 5 positions on the next sweep (5 cancel + 5 place = 10 broker operations).
   The brief sets a deadline of "BEFORE Monday 2026-05-04 13:30 UTC market open." Recommend operator confirms restart timing before I run `docker compose up -d --force-recreate --no-deps broker-sync-worker`.

## Rollback plan

- **Code rollback**: `git revert <code commit>`. The label rename, comments, warning, and admin endpoint all revert cleanly. No DB schema changes.
- **Persisted-data rollback**: not needed. Old `:protected_by_limit` rows are zero in current state (Surprise #3); after revert the writer would write the old label, but no live consumer switches behavior on this opaque text.
- **Status endpoint rollback**: removing the route is harmless — read-only, no side effects.
- **Startup warning rollback**: removing it returns to silent default; no state side effect. The flag combination remains a real operator choice.

This task makes no broker calls.
