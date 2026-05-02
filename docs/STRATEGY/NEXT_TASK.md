# NEXT_TASK: f8a-fix-per-ticker-heaps

STATUS: DONE

## Goal

Fix the cross-ticker drain bug in F8a's deferred-emit scanner heap so that every `volume_breakout_pullback_long` alert lands in `fast_signal_decay` with usable price-capture, not just the lucky ones whose ticker matched the triggering book emit. After this task:

1. **The pullback experiment's data pipeline produces no spurious drops.** Each scheduled deferred emit drains on its own ticker's next book emit (not on whichever ticker happens to fire first). Every drained alert has current `best_bid`/`best_ask`/`close` in `features` by construction.
2. **The architectural change is invisible to the decay miner and to the rest of the scanner.** Same alert shape, same NOTIFY trigger, same gate stack. Pure internal refactor of `MomentumScanner._pullback_heap`.
3. **Organic data starts accumulating cleanly** after the fix lands and the system soaks naturally.

This is a focused defect repair, not a feature add. One commit.

## Why now

F8a's CC report (`docs/STRATEGY/CC_REPORTS/2026-05-02_f8a-volume-breakout-pullback-fade.md`) verified the architectural skeleton works but surfaced a bug: the global `_pullback_heap: list[_DeferredEmit]` drains on every book emit regardless of ticker. When `on_book_emit(ticker=BTC, ...)` fires, it pops EVERY entry past deadline — including AVAX, ETH, SOL entries — and tries to enrich them with BTC's book, which doesn't apply. The non-matching pops emit alerts with empty `best_bid`/`best_ask`/`close`, and the decay miner drops them as malformed.

Of 19 catchup pullback alerts, only 2 produced `fast_signal_decay` rows. **90% data loss.** Until this is fixed, the F8a experiment can't accumulate enough samples to reach the decay miner's MIN_SAMPLES floor, no matter how long the soak runs.

We picked **per-ticker heaps** over DB-lookup-at-drain in the F8a review because it's cleaner (one-line invariant), zero new I/O, same memory bound, and avoids three downstream failure modes (stale book / missing book / DB latency).

## Architectural commitments

- **Same event-driven shape as F8a.** No `while True: sleep(N)`. Per-ticker heap drains on that ticker's own next book emit.
- **No new magic numbers.** `MAX_PENDING_DEFERRED = 1000` cap stays as a *global* cap (sum across all per-ticker heaps), so steady-state memory is unchanged. Optional: split to a per-ticker cap of 200 (= 1000 / 5 pairs) — explicit choice in the brief below.
- **No miner changes.** The miner gets the same alert payload structure; it just gets it on every drain instead of 1-in-N.
- **No gate changes.** No new gates, no constants modified, no migrations.
- **Single commit.** This is one logical defect repair.

## Scope — single commit

In `app/services/trading/fast_path/scanner.py`:

### 1. Replace `_pullback_heap` with `_pullback_heaps` keyed by ticker

```python
# Before
self._pullback_heap: list[_DeferredEmit] = []

# After
self._pullback_heaps: dict[str, list[_DeferredEmit]] = {}
```

`heapq` works on plain lists, so each value remains a list-as-heap — same semantics, just keyed by ticker.

### 2. Update the schedule path (`_schedule_pullback_deferred`)

When scheduling a deferred emit for ticker T:
- `heap = self._pullback_heaps.setdefault(T, [])`
- `heapq.heappush(heap, _DeferredEmit(...))`

The cap check applies to the global sum, not the per-ticker length:
```python
total_pending = sum(len(h) for h in self._pullback_heaps.values())
if total_pending >= MAX_PENDING_DEFERRED:
    self.deferred_dropped_overcap += 1
    logger.warning(
        "[fast_path] scanner deferred-emit cap reached "
        "(total_pending=%d, cap=%d); dropping new arrival ticker=%s",
        total_pending, MAX_PENDING_DEFERRED, T,
    )
    return
```

Sum-across-dict is O(N_tickers) where N_tickers ≤ 5. Negligible cost.

### 3. Update the drain path (`_drain_pullback_due` or wherever it lives)

The drain is now scoped to the triggering ticker:

```python
def _drain_pullback_due(self, triggering_ticker: str, current_book: dict, now_unix: float) -> list[dict]:
    heap = self._pullback_heaps.get(triggering_ticker)
    if not heap:
        return []
    drained: list[dict] = []
    while heap and heap[0].deadline_unix <= now_unix:
        obs = heapq.heappop(heap)
        # By construction obs.ticker == triggering_ticker, so the
        # current_book always applies. Inline-assert this invariant
        # so a future refactor that breaks per-ticker keying explodes
        # loudly instead of silently shipping malformed observations.
        assert obs.ticker == triggering_ticker, (
            f"_drain_pullback_due invariant violated: heap key "
            f"{triggering_ticker} contained entry for {obs.ticker}"
        )
        drained.append(self._build_pullback_alert(obs, current_book, now_unix))
    return drained
```

The `assert` is intentional — this method MUST only see entries for its own key. If a future change breaks per-ticker semantics, the assert ensures we hear about it immediately rather than silently corrupting decay data again.

### 4. Update `stats()` to report total heap depth across all per-ticker heaps

```python
total_pending = sum(len(h) for h in self._pullback_heaps.values())
return {
    ...
    "pullback_pending_heap": total_pending,
    "pullback_per_ticker_pending": {t: len(h) for t, h in self._pullback_heaps.items() if h},
    ...
}
```

The supervisor's per-minute metrics line that currently shows `pullback_heap=0` continues to read the same key and sees the global total — unchanged operator UX. The new `pullback_per_ticker_pending` field is for debugging when a specific ticker's book channel goes quiet.

### 5. Verify behavior

After deploy, restart fast-data-worker so the snapshot-replay catchup re-runs:

```sql
-- Should produce N rows (= count of replayed volume_breakout_long bars), all with non-empty
-- best_bid, best_ask, close in features
SELECT
  COUNT(*) AS total,
  COUNT(*) FILTER (WHERE features ? 'best_bid' AND features->>'best_bid' != '') AS with_best_bid,
  COUNT(*) FILTER (WHERE features ? 'close' AND features->>'close' != '') AS with_close
FROM fast_alerts
WHERE alert_type = 'volume_breakout_pullback_long'
  AND fired_at > NOW() - INTERVAL '15 minutes';
```

Expected: `total = with_best_bid = with_close`. Pre-fix that was `~total / 5` because only 1 in 5 ticker pops matched.

Then verify decay miner observation rate:

```sql
SELECT alert_type,
       COUNT(*) AS rows,
       SUM(sample_count) AS total_obs
FROM fast_signal_decay
WHERE alert_type = 'volume_breakout_pullback_long'
GROUP BY alert_type;
```

`total_obs` should track `total` from the alerts query × number-of-horizons-crossed-since-fire. Pre-fix ratio was wildly under what alert count would predict; post-fix it should match.

## Brain integration (reuse, don't rewrite)

- `MomentumScanner` class — extend in place. Don't subclass, don't refactor unrelated methods.
- `_DeferredEmit` dataclass — unchanged.
- `heapq` stdlib — same usage pattern as before, just one heap-per-ticker instead of one heap-per-scanner.
- F6 decay miner — unchanged. It receives correctly-shaped alerts and Welford-updates as normal.

## Constraints / do not touch

- **All 8 live-placement safety belts.** Same as always.
- **Default mode stays paper.**
- **`VOL_BREAKOUT_PULLBACK_DELAY_S = 30.0`.** Don't tune. Calibration from data is a follow-up after the experiment runs.
- **`MAX_PENDING_DEFERRED = 1000` cap.** Stays as global cap. Don't split per-ticker.
- **Decay miner code.** Don't touch. The fix is upstream.
- **Calibration helpers, gates, exit_manager.** All untouched.
- **`models/trading.py` and `.env.example`.** Continue to leave them alone.
- **The unsolicited observability commit (`c5f9746`)'s metric line.** Keep it; it's genuinely useful. Just update the field name if needed to reflect the new aggregation.

## Out of scope

- F8b (calibrating `DELAY_S` from data once organic firings accumulate).
- F9 (new signal types — order-book momentum, trade-tape, etc.).
- Unit-test harness for the deferred-emit pipeline.
- Watchdog task on decay_miner.
- Any tuning of any threshold.
- Lowering `VOL_BREAKOUT_MULT` to seed firings faster. Don't.

## Success criteria

1. `git log --oneline -3` shows one new commit including `app/services/trading/fast_path/scanner.py`, pushed to origin.
2. `docker compose ps fast-data-worker` healthy after deploy. (No new behavior at WS / book layer; risk confined to scanner internals.)
3. Post-deploy `total = with_best_bid = with_close` in the verification SQL above.
4. `fast_signal_decay.sample_count` for `volume_breakout_pullback_long` grows proportionally to `fast_alerts` row count after restart, not at the prior ~10% rate.
5. The supervisor's per-minute metrics line shows `pullback_heap=N` (global total across per-ticker dicts), with `N=0` after each natural drain wave (entries don't accumulate between book emits).
6. `docs/STRATEGY/CC_REPORTS/<date>_f8a-fix-per-ticker-heaps.md` written following PROTOCOL.md format. Include verbatim verification SQL outputs (the `total/with_best_bid/with_close` counts pre and post — even if "pre" is from F8a's report).

## Open questions for Cowork (surface in your report only if relevant)

1. **Should the assert in `_drain_pullback_due` log + skip rather than raise** in production? My instinct: keep it as `assert`. If the per-ticker invariant breaks, that's a structural bug we want loud, not a silent data corruption. Asserts are cheap and the failure mode is "scanner crashes; supervisor restarts; we see the trace in logs." That's better than silently dropping observations again.

2. **Per-ticker pending dict in `stats()` could grow if many distinct tickers fire.** Currently 5 pairs configured; pollute-with-stale-empty-keys is bounded. If we ever scale to 100+ pairs we'd want to clean up empty keys; for now the dict has ≤ 5 entries always.

3. **No explicit eviction of expired-but-undrained entries** if a ticker's book channel goes silent for hours. Heap entries for that ticker just sit there until the next book emit. The candle_freshness healthcheck (F6.5 cleanup-2) would already detect prolonged silence on a ticker; that's a different alarm. Worth flagging only if you want a lazy-eviction pass on a low-frequency timer (e.g. every 5 min, drop expired entries with `pullback_dropped_expired += 1`). Not in scope for this task.

## Rollback plan

- Single-commit revert restores the pre-F8a-fix global heap. Existing `fast_alerts` rows of type `volume_breakout_pullback_long` stay; they have correct lineage; only post-revert observations would resume the prior 10% effective rate. Harmless; just a regression in data quality.
- No migrations.
- No data migrations or backfills.
- No live-placement risk: scanner-internal change with the same alert payload shape and the same gate stack.
