# Cowork Review: F5 — Exit Manager

**Reviewing:** Claude Code's verbal F5 ship report (no formal CC_REPORT file yet — F5 was built before the protocol existed).
**Reviewer:** Cowork.
**Date:** 2026-05-01.

## What shipped (verified by Cowork via dispatch)

- Migration 218 `fast_exits` table — partitioned by `exited_at`, columns include `entry_execution_id`, `stop_at_entry`, `target_at_entry`, `realized_pnl_usd`, `realized_return_pct`, `holding_period_s`, `brain_json` JSONB.
- `app/services/trading/fast_path/exit_manager.py` (21.5 KB) — async polling exit manager.
- `stop_engine.compute_initial_bracket()` — new public wrapper, brain-aware.
- `executor.py`, `supervisor.py`, `db_writer.py`, `scanner.py`, `ws_client.py`, `gates.py`, `fast_path_api.py` all touched.
- **NOT YET COMMITTED.** All work sits in working tree as of review time.

## First paper soak result (15-min midpoint)

| Metric | Value |
|---|---|
| Polls | 1375 (1Hz, no skips) |
| Bootstrap (open positions adopted) | 11 |
| Exits fired | 3 (all DOGE-USD `stop_hit`) |
| Realized P/L | -$0.27 |
| Avg loss | -0.366% |
| Avg holding time | 43.7 min |
| Win rate | 0% |
| Open positions remaining | 8 |
| Target hits | 0 |
| Time stops | 0 |

## Algo trader read

**The bracket is correctly sized for swing, wrong for scalp.** F5 calls `stop_engine.compute_initial_bracket()` which uses ATR(14) — and ATR(14) on 1m bars gives a stop ~16-37 bps wide. Combined with `max_hold_s=14400` (4 hours), we're holding scalps like swings. Imbalance has a 1-5s predictive horizon; by minute 43 the signal has zero relevance.

The 0% win rate over 3 exits isn't a strategy problem yet (sample too small, market regime unknown), but the *distribution* (0 targets, 0 time stops, 100% stops, 43-min holds) is diagnostic: targets are far enough that price never gets there, time stops are loose enough they never trigger, stops do all the work. That's the signature of bracket geometry mismatched to signal horizon.

**Fix is data-driven:** mine `fast_alerts` history to find the empirical mean-reversion time for `imbalance_long` per pair. That's `max_hold_s` derived from chili's brain instead of from a magic number. F6 is the right next move.

## Dev architect read

**Plumbing: clean.** The LEFT JOIN fast_exits idempotency pattern is the correct way to model "open positions" without a status column. Bootstrap-from-DB resolved the in-memory state concern from F4 era. Brain integration via `compute_initial_bracket()` is exactly the right seam — clean wrapper, no copy-paste of swing logic into fast lane.

**Concerns:**
1. **Uncommitted.** Operator should `git add . && git commit` before next iteration. Risk of accidental loss.
2. **Container went unhealthy mid-soak** (4 of 5 pairs showed bars stale by 6+ min). Needs investigation but not urgent — alerts are still flowing, exit manager still polling.
3. **`max_hold_s=14400` is a hardcoded constant in exit_manager.py.** Same magic-number antipattern the user explicitly asked to avoid.
4. **1Hz polling for exits** is fine for paper but mismatched to the "fast lane" name; same LISTEN/NOTIFY upgrade as executor.
5. **Bootstrap=11, max_hold_s=14400** — the 11 positions inherited from F4 era will time-stop at 4 hours past their original `decided_at`, several already 90+ min old. Some will exit not on signal but on the time-stop floor. That'll skew the soak data.

## Decisions made / confirmed

- Schema choice: separate `fast_exits` table > extending `fast_executions`. Cleaner partition cadence (daily exits don't pollute monthly executions partition), CHECK constraint isolation, and the unique index `(entry_execution_id, exited_at)` gives idempotency for free.
- Brain integration via public wrapper > exposing private internals. `compute_initial_bracket()` is the right shape.
- Paper-only first; live exits deferred. Correct call.

## Next move

F6: signal half-life mining. Discussed below in `NEXT_TASK.md` once operator confirms.

## Things to lock before next task

1. Operator should commit the F5 work (Claude Code didn't, the working tree is dirty).
2. Investigate the unhealthy container state — quick `docker compose logs` to see why bars went stale on 4 of 5 pairs.
3. Decide whether to flush the 11 inherited bootstrap positions before F6 soaks (so post-F6 data is clean).
