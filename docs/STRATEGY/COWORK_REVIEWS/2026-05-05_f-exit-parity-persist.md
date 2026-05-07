# COWORK_REVIEW: f-exit-parity-persist

## Verdict

Cleanest single-commit task in the project's recent history. Three plumbing fixes + methodology hardening + migration 225 land in one commit, 248 + 27 tests pass, end-to-end smoke proves rows persist with computed `pnl_diff_pct` and round-trip `agree_strict_bool` on both sources. The CC honestly corrects the brief in two places where the brief was wrong, picks the right judgment-call branch in Step 2 with sound reasoning, and surfaces five substantive cutover-prep findings the brief asked for in Audit A-E.

The implementation quality is the floor we should expect from every CC pass going forward.

## Algo-trader lens

**What's good.** The core insight that `_parity_sink` / `_ticker` / `_scan_pattern_id` were *never set anywhere* — not a GC issue as the brief claimed — is the kind of thing that only comes from actually grepping the code rather than accepting the brief at face value. The fix injects all three attributes via the `type()` call at `strat_cls` construction. That's correcting the brief's diagnosis, not just executing it. CC's discipline here is the right discipline.

The `agree_strict_bool` column is the methodologically-correct response to the dual-definition problem. Existing `agree_bool` preserved (so prior analysis stays valid), new column populated identically on both paths, verdict queries can filter on the new column to get apples-to-apples comparisons. Migration 225 is idempotent ADD COLUMN + CREATE INDEX, no data backfill needed.

The five audit points in § Audit summary are the highest-value piece of the report. Each names a real concern (deliberate parity choice, dead-flag learner, units mismatch, dead informational flag, dual agree definitions) with cutover implications spelled out. **A-D are pre-existing; none block today's work.** The cutover-time decisions get framed before they need to be made — exactly what a design-doc-driven flow should produce.

**What's narrow.** Post-deploy verification depends on the brain-worker actually running a FractionalBacktest cycle AND the scheduler evaluating at least one live position. Both are environment-dependent. CC honestly flagged this as "what still needs the operator post-deploy" rather than claiming success criteria #2/3/6 were satisfied. The plumbing IS correct (smoke is dispositive); production row counts are 0 right now and will populate naturally over the next 24-48h.

If `dispatch-exit-parity-verdict.ps1` returns empty after 48h despite live engine evaluating positions, the live path's fresh-SessionLocal write is the first thing to check (was the `SessionLocal` import scoped correctly? did the parity table get the right schema?). The smoke ran in `chili_test`; production `chili` schema needs the same migration applied.

**What's deferred.** Audit points A (trail_monotonic at cutover), B (`_resolve_trailing_atr_mult` wiring), C (time-decay unit mismatch — pre-existing legacy bug), D (`partial_profit_eligible` dead flag) all surface for later. None block. CC explicitly listed each in the Deferred section with one-line rationale. Right discipline.

## Dev-architect lens

**What's good.** Single commit covering 6 brief steps. Migration ID 225 verified via `verify-migration-ids.ps1` (`OK: 225 migrations, 0 retired; no ID collisions.`). Idempotent migration. Test pass count up from 248 prior baseline. End-to-end smoke script that synthesized parity rows AND cleaned them up — that's the right pattern for testing-against-shared-DB without polluting it.

The Step 2 judgment call is exactly right. The brief said "if caller has wider transaction, use fresh SessionLocal." CC traced `run_exit_engine(db)` → `_run_paper_trade_check_job` → `check_paper_exits(db)` and confirmed the wider transaction exists. Documenting that in the surprise section is the right level of disclosure. Future readers won't have to re-derive the reasoning.

The Step 3 backtest sink-drain location ended up at `_run_dynamic_pattern_slice` after `_bt_run_budget` returns, not at "end of FractionalBacktest.run" as the brief sketched. Reason: `FractionalBacktest` is `backtesting.lib`, third-party, no hook available. CC found the right call site that has both `strat_cls` (with populated sink) AND the budget-exception envelope. Smart. Brief's location guidance was wrong; CC didn't blindly follow it.

**What's concerning.**

1. **Stale uncommitted work in the working tree** (CC Open Q #3) is real and worth your attention before the next NEXT_TASK fires. CC names: in-progress `_trade_phantom_close_guard` event listener in `app/models/trading.py`, new `CHILI_PATTERN_EVIDENCE_AUTO_DEMOTE*` flags in `.env.example`, one-byte change to `data/ticker_cache/crypto_top.json`, sizeable backlog of `.commit_msg_*.txt` and `docs/AUDITS/*.md`. None of it is from this task; CC left it untouched correctly. **Two risks:** (a) the next CC run might inadvertently commit some of this if it touches the same files, (b) this work-in-progress represents undocumented intent that may conflict with future briefs. Recommend a single `git stash` or `git restore` pass before the next task to clean the slate, OR an explicit chat-level "what is this stuff and where does it belong?" pass.

2. **The pytest-asyncio 0.23 vs pytest 9 mismatch** (Surprise #4) is a real CI/dev-loop concern. Every test run in this task needed `-p no:asyncio` to bypass the `Package.obj` collection bug. If CI runs without that flag, builds fail spuriously. Worth a one-line `pytest.ini` or `pyproject.toml` addition that disables the plugin globally OR pins to a compatible combination. Cosmetic but annoying.

3. **Live path's `SessionLocal` write adds one extra connection-pool checkout per live evaluation.** CC flagged this as negligible; I agree at the live-engine cadence (a few evaluations per minute, not thousands). If verdict-query data ever shows the live path under-writing rows compared to the live-engine evaluation count, pool exhaustion would be the first thing to check. Not a concern today; tracking item.

## Decisions for the operator

1. **Wait 24-48h for verdict data, then run `dispatch-exit-parity-verdict.ps1`.** That's the success criterion the brief specified.

2. **If verdict query returns empty after 48h:** check that mig 225 applied to production `chili` (not just `chili_test`). The CC's smoke ran in `chili_test`; production may need a separate apply pass.

3. **Stale working-tree work** (CC Open Q #3): clean-slate it before the next NEXT_TASK or document what it is. Recommend the former.

4. **Cutover-time questions surface as the verdict data lands.** Audit A (trail_monotonic) recommended staged-not-flag-day. Audit B (`_resolve_trailing_atr_mult`) only relevant if cutover also enables live trail-close. Don't pre-decide; let the data inform.

## Outstanding from earlier today

The **PED bracket-writer bug** from this morning's session is still unaddressed:

```
[broker] SELL_STOP rejected (no order_id):
PED x30.0 trigger=13.6275 response={'non_field_errors': ['Limit order requested, but no price provided.']}
```

You said you'd manually monitor. Status as of when this review was written: 45 retries in 44 minutes, all PED, every minute on the bracket-reconciliation sweep. PED's stop is dead at the broker; if price drops below $13.63 the system won't auto-sell because re-placement keeps failing.

When you're ready to attack it, the fix is in `broker_service.place_stop_loss_sell_order` (or wherever the order body is constructed before `rh.orders.order(...)`). Round stop_price to the broker's tick size, AND verify the request type is being sent as stop-MARKET not stop-LIMIT. ~50 LOC + tests.

## Status of NEXT_TASK.md

CC marked DONE for `f-exit-parity-persist`. Awaiting your call on what queues next:

- PED bracket-writer fix (live-money exposure; small scope)
- Phase 2 of position-identity refactor (after the 1-week soak completes)
- Some other task from your queue

## Status of CURRENT_PLAN.md

Forward pointer to design doc § 8 still accurate. Open architectural questions section still historically inaccurate (operator answered all 5 + 4 doc-internal opens; the section reads as if questions are still open). Cosmetic; flagged earlier in today's reviews; non-blocking.
