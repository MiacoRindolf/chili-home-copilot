# CC_REPORT: cleanup-2-healthcheck-and-protocol-infra

## What shipped

Four commits, all pushed to `origin/main`:

1. **`5c8e5d7`** — `chore(strategy): cleanup-2 task brief + cowork review of F5 cleanup`
   - 2 files. Subtask 1 from the brief.
   - Most strategy infrastructure (CLAUDE.md pointer, PROTOCOL.md, CURRENT_PLAN.md, original NEXT_TASK.md in DONE state, F5 CC report and earlier review) had already landed in `d18d073` during the previous F5-cleanup run. This commit covers what was new since: the rotated NEXT_TASK.md (cleanup-2 brief, PENDING) and Cowork's review of the F5-cleanup CC report.

2. **`0fb6285`** — `fix(fast-path): split /healthz into ws_connected + candle_freshness probes`
   - 2 files (+217 / -41).
   - Subtask 2. `app/services/trading/fast_path/healthz.py` rewritten around two probes AND'd together. `app/services/trading/fast_path/order_book.py` got a 4-line addition: an aggregator-level `last_emit_at_wall` timestamp surfaced via `stats()` so healthz reads L2 freshness from the existing supervisor snapshot — no DB query.

3. **`45436cb`** — `docs(fast-path): document bracket-age classifier invariant in exit_manager`
   - 1 file (+11 / -0).
   - Subtask 3. Inline comment at the `brain_payload` assignment site in `exit_manager.py` calling out that `computed_at` is set ONCE at bracket-decision time and must never be refreshed; explains *why* (it's the load-bearing classifier behind migration 219's `fast_exits_native` view) so a future refactor doesn't silently break the native-vs-inherited filter.

`docs/STRATEGY/NEXT_TASK.md` will be marked `STATUS: DONE` and committed in the same commit as this report (per protocol; the report and the DONE flip are companions to the work, not the work itself).

**Out of scope and intentionally untouched in the working tree** (per the brief's constraint):
- `app/models/trading.py` (legacy Trade phantom-close guard)
- `.env.example` (pattern-evidence env vars)

## Verification

### Probe split — observation passed ✅

After `docker compose build chili` + `docker compose up -d fast-data-worker` (the worker reuses `chili-app:local`), the container ran `(healthy)` continuously across an 18-tick / 9-min observation window starting at 00:21:49 UTC and ending at 00:30:55 UTC — every poll returned `(healthy)`, with the container's `Up` clock advancing from 2 min to 10 min over the loop. Combined with the ~1 min of healthy uptime before the loop started, that's >10 min of consistent `(healthy)` from a fresh restart.

Final `/healthz` JSON body (200 OK) at the end of the window:

```json
{
  "ok": true,
  "ws_connected": true,
  "candle_freshness": true,
  "reason": "ok",
  "details": {
    "ws_window_s": 60.0,
    "candle_window_s": 300.0,
    "newest_book_age_s": 0.07,
    "newest_bar_age_s": 296.03,
    "freshest_pair_for_bars": "BTC-USD"
  }
}
```

Note `newest_bar_age_s: 296.03` — the freshest bar across all five pairs was just under the 300s candle window. **This is exactly the failure mode the old single-probe healthcheck failed on**: the candle channel is sparse on quiet pairs, but L2 books were emitting 0.07s ago — clear evidence WS is alive. The split probe correctly returns `(healthy)` here while the old `last_bar_at < 90s` rule would have flapped.

### Strategy infra commit — clean ✅

```
$ git log --oneline -7
45436cb docs(fast-path): document bracket-age classifier invariant in exit_manager
0fb6285 fix(fast-path): split /healthz into ws_connected + candle_freshness probes
5c8e5d7 chore(strategy): cleanup-2 task brief + cowork review of F5 cleanup
d18d073 docs(strategy): add cowork-directed protocol + F5 cleanup CC report
6bab79c feat(fast-path): F5-cleanup - fast_exits_native view (migration 219)
cb137ea feat(fast-path): F5 - exit manager closes paper-trade loop
8f18be5 feat(fast-path): wire real Coinbase live placement (gated, paper-default)
```

`models/trading.py` and `.env.example` remain modified-uncommitted, as instructed.

### Constraints respected ✅

- No live-placement safety belts touched.
- No strategy thresholds (MIN_SIGNAL_SCORE, MAX_SPREAD_BPS, IMBALANCE_*, VOL_BREAKOUT_MULT, ALERT_RECENCY_MAX_AGE_S, etc.) changed.
- Stop/target/time-stop bracket policy unchanged.
- The 11 inherited bootstrap positions remain untouched (exit_manager still adopts them naturally; only the inline comment changed in `exit_manager.py`).
- 60s ws / 300s candle thresholds — kept at the brief's defaults; no deviation.

## Surprises / deviations

1. **Most strategy infra was already in git from the prior task's commit `d18d073`.** The new task brief listed CLAUDE.md, PROTOCOL.md, CURRENT_PLAN.md, NEXT_TASK.md, the .gitkeep, the F5 CC report, and one Cowork review as files that should land in subtask 1's commit. All but two of those (the rotated NEXT_TASK.md and the new Cowork review of the F5 cleanup) were already committed during F5 cleanup. I committed only what was actually uncommitted to avoid bundling unrelated history. Calling out so it doesn't read as scope-skip — the success criterion ("strategy infrastructure is in version control") was already met before this task ran for everything except the two new files.

2. **Kept the JSON body shape from the brief exactly, but added a `reason` field at the top level.** The brief's example didn't have one but the previous implementation did, and operators looking at probe failure logs benefit from a one-word top-level summary (`ok` / `ws_disconnected` / `no_candle_freshness` / `ws_disconnected+no_candle_freshness` / `queue_full:N/M` / etc). Marking as a deviation only because it's an addition, not a removal; happy to drop if Cowork prefers strict adherence to the example.

3. **Chose L2 book emit timestamp over `fast_orderbook.snapshot_at` DB query for ws_connected.** The brief listed both signals (status_tracker fields plus the most-recent `fast_orderbook.snapshot_at`). I added a tiny in-memory `last_emit_at_wall` to `OrderBookAggregator` and surfaced it via `stats()` — keeps healthz a pure read of the supervisor snapshot, avoids an extra DB round-trip on every healthcheck poll. Same authoritative info, lower overhead. Open to switching to the DB-query approach if Cowork prefers persistence-as-source-of-truth.

4. **The `chili-app:local` image had to be rebuilt before fast-data-worker picked up the new healthz.** `fast-data-worker` reuses the image rather than mounting code (in this compose env), so `docker compose build chili` was required before `up -d fast-data-worker`. Took ~71s. Calling out in case future tasks targeting fast-path code need to budget the rebuild.

## Deferred

- **WS-side timestamp on the candle channel.** I considered adding a `last_candle_event_at` to `CoinbaseWSClient` for a third freshness signal; not strictly needed given L2 book emits already prove WS liveness, but it would let an operator distinguish "WS alive but candles channel specifically has gone silent" from "L2 channel slowed". Leaving for if/when we want better failure-mode classification.
- **Status-tracker queue-depth signal in ws_connected.** The brief mentioned `db_writer` queue depth as an optional third signal. Not implemented — the existing top-level `queue_full` short-circuit at 90% capacity already fails fast, and L2 emit freshness is a more responsive WS-liveness proxy.
- **Tightening the 60s WS window.** Under live load L2 emits 4/s/ticker so the window has ~240x headroom against zero. Could plausibly tighten to 30s or even 15s, but the brief said don't deviate; would propose in a future tuning task once we've observed enough probe-fail incidents to know the actual outage timescales.

## Open questions for Cowork

1. **Should `/healthz` body's `reason` field stay or go?** Top-level summary is convenient for operators but isn't in the brief's example shape. Trivially removable if you want strict adherence.

2. **At what point do we tighten the 60s WS window?** Right now `newest_book_age_s: 0.07` is typical and the 60s ceiling is generous. Once we've seen a real WS outage caught by the new probe, we'll know the real-world detection-vs-false-positive curve and can argue for a tighter value. Flagging now so it's on a future task's radar; not in scope here.

3. **Bootstrap-rebootstrap risk for the classifier invariant.** The invariant comment I added in `exit_manager.py` covers the existing code path. There's a related concern: if a position exits and is later re-bootstrapped (e.g., due to a write-failed exit row), the `_open` cache rebuilds and `computed_at` would be re-stamped. That's not the current code path — exits dedupe via the `(entry_execution_id, exited_at)` unique index — but a future change that reopens a "closed" entry could violate the invariant without triggering a test. Worth a unit test? Or worth a defensive check (e.g., skip bootstrap if the entry has a fast_exits row with stop_hit/target_hit/time_stop)? Not adding either in this task per scope.

## Verbatim observation log (for review use)

```
Started obs at 00:21:49
00:21:49  tick=1   Up 2 minutes (healthy)
00:22:19  tick=2   Up 2 minutes (healthy)
00:22:49  tick=3   Up 3 minutes (healthy)
00:23:20  tick=4   Up 3 minutes (healthy)
00:23:50  tick=5   Up 4 minutes (healthy)
00:24:21  tick=6   Up 4 minutes (healthy)
00:24:51  tick=7   Up 5 minutes (healthy)
00:25:21  tick=8   Up 5 minutes (healthy)
00:25:52  tick=9   Up 6 minutes (healthy)
00:26:22  tick=10  Up 6 minutes (healthy)
00:26:52  tick=11  Up 7 minutes (healthy)
00:27:23  tick=12  Up 7 minutes (healthy)
00:27:53  tick=13  Up 8 minutes (healthy)
00:28:23  tick=14  Up 8 minutes (healthy)
00:28:54  tick=15  Up 9 minutes (healthy)
00:29:24  tick=16  Up 9 minutes (healthy)
00:29:54  tick=17  Up 10 minutes (healthy)
00:30:25  tick=18  Up 10 minutes (healthy)
Done at 00:30:55
```

Zero flaps. No 503s. Probe split is a strict improvement.
