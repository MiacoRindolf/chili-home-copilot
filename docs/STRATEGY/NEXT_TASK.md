# NEXT_TASK: f5-cleanup-and-baseline

STATUS: DONE

## Goal

Establish a clean, observable baseline for the fast-path subsystem before any new feature work. Three things must be true at the end:

1. **F5 committed.** All of Claude Code's F5 work (exit_manager.py, migration 218, executor/supervisor/db_writer/scanner/ws_client/gates/fast_path_api edits) is in a clean git commit pushed to main.
2. **Container health is confirmed.** The transient `(unhealthy)` state observed at 23:04 UTC was already gone by the 30-min final report (`Container status: healthy throughout`). We just need to confirm the cause was transient and not a recurring class of bug.
3. **Bootstrap-inherited positions are identifiable.** The 11 positions F5 adopted on first boot (entries from the F4-only era, pre-stop_engine) are tagged or queryable separately from native F5-era entries, so signal-quality analysis isn't polluted by them.

## Why now

F5's 30-min soak produced real data: 3 realized exits (all DOGE stop_hit, -$0.27), 5 BTC/ETH/SOL positions still open and floating green at +0.012% to +0.109%. Container was healthy throughout. Pipeline plumbing is solid; the strategy/calibration question (DOGE's 16-bp ATR/price ratio is structurally too tight; targets too far for a 30-min window) is what F6 will mine answers for.

Before F6 mines, three things must be true: F5 is committed and not at risk of being lost, the unhealthy episode is understood (or confirmed transient), and the 11 inherited positions are filterable so they don't pollute F6's training set. F4-era entries had their stop/target *backfilled by F5 at boot* using current ATR — not derived at entry time — so treating them as F5-native data is textbook training/test contamination.

## Scope — three subtasks, ordered

### 1. Commit F5

- `git status` will show all the F5 changes Claude Code made today (exit_manager.py is new; executor/supervisor/db_writer/scanner/ws_client/gates/fast_path_api are modified).
- One commit. Message should reference: F5 phase, migration 218, brain integration via `stop_engine.compute_initial_bracket()`, fast_exits schema, and the 30-min soak result (3 exits, all DOGE stop_hit, 5 still-open green).
- Push to main.

### 2. Confirm the unhealthy state was transient (5-min check, not a deep dive)

At ~22:00–23:04 UTC the container was `(unhealthy)` with `last_bar_at` 6+ min stale on 4 of 5 pairs (BTC/SOL/AVAX/DOGE) while ETH was fine and L2 books continued updating. By the 30-min final report the container was healthy throughout. So the working hypothesis is: it was transient and self-resolved.

**Quick verification only:**
- Skim `docker compose logs fast-data-worker --since 1h` for the affected window. Look for any reconnect attempts, candle-channel resubscribes, or unusual gaps.
- Check `fast_path_status` history if it kept any record (it may not — that table is single-row-per-ticker overwriting).
- If the cause is obvious from logs, document it in the CC_REPORT.
- **If the cause is NOT obvious or you find evidence it could recur, STOP and flag in Open Questions.** Do not pull on the thread mid-cleanup; deep diagnosis becomes its own next task.

### 3. Tag bootstrap-inherited positions

The 11 positions F5 adopted on first boot have `stop_at_entry` and `target_at_entry` backfilled at bootstrap, not derived at entry time. We need a way to filter them out of any "F5-native" P/L analysis.

Two acceptable approaches — pick whichever is cheaper:

**A. Schema column** — extend `fast_exits` (or add to `fast_executions` so it survives across both entry and exit rows) with a nullable `inherited_bootstrap` boolean. Set TRUE for the 11 affected entry IDs and any future exits referencing them.

**B. Convention via brain_json** — write a marker into the `brain_json` JSONB at exit time for these specific entries (`{"inherited_bootstrap": true, ...}`). Add a SQL view `fast_exits_native` that filters them out so reviews can use it cleanly.

**B is probably faster and just as good** — your call.

Either way, after this step a single SQL query should produce "realized P/L on F5-native trades only" without manual cherry-picking. Include that query verbatim in the CC_REPORT so I can copy-paste it into reviews.

## Brain integration (reuse, don't rewrite)

- Healthcheck logic already exists in `app/services/trading/fast_path/healthz.py` — read it for context, don't write a new probe.
- Status tracking in `app/services/trading/fast_path/status_tracker.py` — examine `fast_path_status` rows for the affected pairs around 22:00-23:04 UTC.

## Constraints / do not touch

- **Live-placement safety belts.** All 8 layers in `_place_coinbase_order_live`, `is_live_authorized()`, and the mode_interlock gate.
- **Stop/target/time-stop policy.** Even if you observe the calibration is bad. Don't tune in this task. F6 will derive these from data; tuning by hand here would invalidate F6's training set.
- **`ALERT_RECENCY_MAX_AGE_S` (currently 60s).** Tempting to tighten to 5–10s now that F5 closes the loop, but tightening before F6 would shrink F6's training data. Tighten only after F6 produces calibrated values per signal type.
- **The 11 bootstrap positions themselves.** Don't manually exit them or force them through cleanup. Let exit_manager handle them naturally; we just want a way to tag and filter them.

## Out of scope

- F6 signal half-life mining (next task — I'll write it after this one finishes)
- Any new gates, scanner signals, or strategy logic
- Switching to LISTEN/NOTIFY (deferred)
- Watchdog task (deferred)
- UI changes
- Any tuning of recency / score / spread / capacity / budget thresholds

## Success criteria

1. `git log --oneline -5` shows a new commit including `exit_manager.py` and migration 218 entry, pushed to origin
2. `docker compose ps fast-data-worker` was/is `(healthy)` at the time of report; transient cause documented or explicitly flagged as Open Question
3. A SQL query exists that returns "F5-native realized P/L only" excluding the 11 inherited positions — verbatim in the CC_REPORT
4. `docs/STRATEGY/CC_REPORTS/2026-05-01_f5-cleanup-and-baseline.md` written following the format in PROTOCOL.md, with: what shipped, what the unhealthy investigation found, which tagging approach was chosen and why, deferrals, Open Questions

## Open questions for Cowork (don't try to answer; just surface in your report if relevant)

- The 5 still-open BTC/ETH/SOL positions are floating green at +0.025% to +0.109% after 1h41m. Should we extend the soak to capture more time_stop / target_hit data before F6 starts mining, or is the existing alert-history + book-trajectory data sufficient for F6 to begin? (My current vote: F6 can start now from books-and-alerts; realized exits will accumulate in parallel.)
- If the unhealthy investigation finds the cause is the 90s healthcheck threshold being too tight relative to candle channel cadence on quiet pairs, should the threshold be raised, or should we instead add a "candle-channel-staleness" probe distinct from "WS-connection-alive"? (Don't fix; just flag if you see it.)

## Rollback plan

- F5 commit: any of Claude Code's F5 changes that broke something not yet noticed can be reverted without data loss; `fast_exits` is a new table and reverting the code doesn't drop it.
- Healthcheck/diag: read-only — no rollback needed.
- Bootstrap tag option B: no schema delta, leaves no trace if reverted.
- Bootstrap tag option A: nullable column, forward-safe; worst case stays NULL on future rows.
