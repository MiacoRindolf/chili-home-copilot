# Triple-Barrier Labeling (Phase C of f-evidence-fidelity-architecture)

> **Audience:** operator + future Claude Code sessions.
> **Modules:**
> - Labeler (pure math): `app/services/trading/triple_barrier.py`
> - Labeler (DB writer): `app/services/trading/triple_barrier_labeler.py`
> - Cron wrapper: `app/services/trading/cron_jobs/triple_barrier_label.py`
> - Scheduler registration: `app/services/trading_scheduler.py` (`triple_barrier_label_cycle`)
> **Mode flag:** `settings.brain_triple_barrier_mode` (`off` / `shadow` / `authoritative`)
> **Storage:** `trading_triple_barrier_labels` (constraint `uq_triple_barrier_labels`)
> **Backfill:** `scripts/triple-barrier-backfill.ps1`
> **Kill switch:** `scripts/triple-barrier-backfill-stop.flag`
> **Activation phase brief:** `docs/STRATEGY/QUEUED/f-triple-barrier-activation.md`
> **Parent arc:** `docs/STRATEGY/QUEUED/f-evidence-fidelity-architecture-2026-05-14.md`

## TL;DR

The brain previously labeled snapshots with **N-bar-forward returns** —
"what fraction did this ticker move over the next K bars?" That's
information-poor: it averages winners and losers regardless of trade
construction. Triple-barrier labeling (Lopez de Prado, *Advances in
Financial Machine Learning*, ch. 3) instead asks the precise question
trading cares about:

> *Given this entry, did price hit the +tp barrier before the −sl
> barrier (or neither, within max_bars)?*

Outcomes are categorical (`+1` TP / `−1` SL / `0` timeout). Once enough
rows accumulate, a per-pattern meta-classifier can be trained on
`(setup features) → P(TP before SL)` and gated into the autotrader as
a "take this signal vs skip" filter. Same alpha, fewer false positives.

Phase C **activates the labeler** by wiring a 4-hourly scheduler job
that walks recent `MarketSnapshot` rows. Mode stays `shadow` at merge —
labels write to the table but **no downstream gate consumes them yet**.
Operator flips to `authoritative` after soak.

## Lifecycle

```
  every 4h        _run_triple_barrier_label_job (trading_scheduler.py)
        |
        v
        run_triple_barrier_label_cycle  (cron_jobs/triple_barrier_label.py)
        |  limit=500, side='long', min_lookback_days=10
        v
        triple_barrier_labeler.label_snapshots
        |  picks newest snapshots with completed-bar anchor <= utcnow - 10d
        |  fetches forward bars via market_data.fetch_ohlcv
        |  computes triple_barrier.compute_label(entry_close, bars, cfg)
        |  upserts into trading_triple_barrier_labels (idempotent)
        v
        log line: [triple_barrier_ops] event=run_summary ...
```

Anchor contract: for bar-keyed snapshots, the completed-bar anchor is
`MarketSnapshot.bar_start_at.date()` when `bar_start_at` is present.
`snapshot_date.date()` is a legacy fallback only when `bar_start_at` is
null. Eligibility cutoff, query ordering, `label_date`, idempotency key,
and forward-bar lookup start must all use that same chosen anchor because
`snapshot_date` can be ingestion time. Rows without clear anchor
provenance remain shadow diagnostic evidence only; they are not valid for
model training, tuning, drift evaluation, promotion evidence, sizing, or
live gates.

The labeler stays in shadow mode until separate operator approval. This
phase does not make triple-barrier labels authoritative for live trading.

## Mode flag

`settings.brain_triple_barrier_mode` (default `shadow`):

| Mode | Effect |
|---|---|
| `off` | Labeler returns immediately. Zero DB writes. Use to halt the system in an incident. |
| `shadow` | (Default.) Labels are computed and written to `trading_triple_barrier_labels`. Downstream gates do NOT consume them. Safe to run in production. |
| `authoritative` | Reserved for the meta-classifier cutover. Operator flip only — see "When to flip mode" below. Phase C does NOT change semantics on this side. |

The labeler treats anything outside this set (typo, garbage env value)
as `off` — fail-closed.

## How to read the labels

```sql
-- Recent label flow, last 24h
SELECT
  mode,
  side,
  COUNT(*) AS labels_total,
  COUNT(DISTINCT ticker) AS tickers_distinct,
  COUNT(*) FILTER (WHERE barrier_hit = 'tp') AS hit_tp,
  COUNT(*) FILTER (WHERE barrier_hit = 'sl') AS hit_sl,
  COUNT(*) FILTER (WHERE barrier_hit = 'timeout') AS hit_timeout,
  COUNT(*) FILTER (WHERE barrier_hit = 'missing_data') AS hit_missing
FROM trading_triple_barrier_labels
WHERE created_at >= NOW() - INTERVAL '24 hours'
GROUP BY mode, side;
```

The shape to watch:

- **`hit_tp + hit_sl + hit_timeout`** is the resolvable population.
  `missing_data` rows are mostly tickers we can't fetch forward bars
  for — Massive/Polygon/yfinance miss. Spike in `missing_data` usually
  means the upstream data feed is degraded, not the labeler.
- **`hit_tp / (hit_tp + hit_sl)`** is the in-sample "would have hit
  TP first" base rate for the active TP/SL pair. With the default
  `tp_pct=0.015 / sl_pct=0.010` (a 1.5:1 reward:risk) you should
  expect this to drift somewhere in the 0.40–0.55 range across the
  broad universe. Hugely skewed values mean either the TP/SL pair is
  miscalibrated or the snapshot population is biased.
- **`hit_timeout`** counts trades that didn't decide either way
  within `max_bars=5`. High timeout rate suggests the barriers are
  too wide for the bar interval.

To inspect a single pattern's labels:

```sql
SELECT ticker, label_date, barrier_hit, label,
       realized_return_pct, exit_bar_idx, entry_close, tp_price, sl_price
FROM trading_triple_barrier_labels l
JOIN trading_snapshots s ON s.id = l.snapshot_id
WHERE s.ticker = 'AAPL'
ORDER BY l.label_date DESC
LIMIT 50;
```

The labeler also writes one `[triple_barrier_ops] event=label_write`
log per insert and one `event=run_summary` per cycle — searchable in
`brain_worker.log` / `scheduler-worker` container logs.

## Backfill

The 4h cron only labels the **most recent 500** snapshots per cycle
(filtered to `min_lookback_days=10`). To seed the table from cold, run
the one-shot backfill:

```powershell
# Step 1 — dry-run (safe, computes but inserts nothing)
.\scripts\triple-barrier-backfill.ps1 -VerboseProgress

# Step 2 — live (writes shadow-mode rows)
.\scripts\triple-barrier-backfill.ps1 -DryRun:$false -VerboseProgress
```

Defaults: `-BatchSize 500`, `-MaxPasses 12`, `-LookbackDays 14,30,60,90,180,365`.
The script repeats `label_snapshots(limit=BatchSize, min_lookback_days=N)`
for each `N` in the lookback list until that lookback's queue drains
or `MaxPasses` is hit. Each pass goes deeper into history.

**Tuning knobs:**

- `-BatchSize N` — rows per pass. Default 500 = the cron's per-cycle
  cap, gives a like-for-like ramp-up. Smaller (50–100) for trial runs.
- `-LookbackDays @(14,30,60,90,180,365)` — pass schedule. Reduce or
  extend depending on how deep your `trading_snapshots` history goes.
- `-MaxPasses N` — global safety cap. Default 12 limits total work
  to BatchSize × 12 = 6000 candidate snapshots per invocation.

### Kill switch

While the backfill is running, the operator can stop it gracefully:

```powershell
New-Item -Path .\scripts\triple-barrier-backfill-stop.flag -ItemType File -Force
```

The Python loop checks for the flag between passes and exits cleanly
with `stopped_by_flag=true` in its result dict. Remove the flag before
the next live run:

```powershell
Remove-Item .\scripts\triple-barrier-backfill-stop.flag -Force
```

Dry-run mode forces `mode_override='off'` on the labeler — even if the
flag is missing, no rows are written. Live runs refuse to start if the
flag exists at launch.

## When to flip mode to `authoritative`

**Not in Phase C.** The activation phase only writes the rows. The
`authoritative` flip belongs to a separate operator decision once:

1. `trading_triple_barrier_labels` has ≥ 1000 rows spanning ≥ 3
   distinct tickers and ≥ 4 weeks of label_date history (rough rule
   of thumb for a meta-classifier to be trainable per family).
2. A meta-classifier has been trained and offline-validated on the
   accumulated labels (Phase F or later in the arc).
3. The classifier is wired into the autotrader gate chain behind its
   own flag (separate brief).
4. A 30-min paper soak has shown the classifier doesn't regress
   imminent-alert volume into the floor.

Only after all four does the operator flip
`brain_triple_barrier_mode = 'authoritative'`. This is **not** a
self-service change; it requires the meta-classifier to exist on the
other side. Until then, leave the flag at `shadow`.

## Schedule-side contract

The 4-hourly scheduler job:

- **id:** `triple_barrier_label_cycle`
- **trigger:** `IntervalTrigger(hours=4)`
- **max_instances:** `1` (mandatory — prevents overlap if a cycle
  runs long, which can happen when `fetch_ohlcv` rate-limits)
- **coalesce:** `True` (mandatory — drop missed runs instead of
  back-firing them and burning rate limit)
- **role gate:** `include_web_light` (so it runs in `scheduler-worker`
  via `CHILI_SCHEDULER_ROLE=cron_only` and in `chili` web tier under
  legacy `all`)

Verify it's registered in a running container:

```powershell
docker compose logs scheduler-worker | Select-String triple_barrier_label
# Expect periodic "[scheduler] Starting triple_barrier_label cycle" lines.
```

Or query the job directly via Python:

```powershell
docker compose exec chili python -c "from app.services.trading_scheduler import _scheduler; print(_scheduler.get_job('triple_barrier_label_cycle'))"
```

## Incident playbook

**Symptom:** label table growing way faster than expected (>>500 rows
per 4h cycle).

The labeler inserts at most ~500 rows per cycle — the limit is
hard-coded in the cron wrapper. If you see more, check whether the
backfill script is also running. Stop it with the kill flag.

**Symptom:** `missing_data` dominates new labels.

The market_data layer (Massive / Polygon / yfinance) is failing for
those tickers. Triage in this order:

1. Check Massive credit balance — exhaustion turns into "no bars"
   silently.
2. `docker compose logs chili | Select-String market_data` for fetch
   errors.
3. If the upstream is healthy, the labeler's
   `_fetch_forward_bars` may be hitting the 10-day buffer ceiling for
   highly illiquid tickers; that's expected, not an incident.

**Symptom:** the cron is registered but never fires.

The scheduler-worker container has `CHILI_SCHEDULER_ROLE=cron_only`
which maps to `include_web_light=True`. If the role got changed to
`broker_sync_only` or `autotrader_only`, this job won't be wired in
that container. Check `docker compose config scheduler-worker | grep CHILI_SCHEDULER_ROLE`.

**Emergency stop the labeler entirely:** flip the mode flag.

```powershell
# Append to .env (do NOT use Out-File on .env — see advisor brief §2.2).
# Use a here-string + AppendAllText to preserve bytes.
# Or: edit the .env in your editor of choice, set
#   BRAIN_TRIPLE_BARRIER_MODE=off
# then restart scheduler-worker.
docker compose restart scheduler-worker
```

The labeler returns immediately when mode is `off`. Existing rows are
preserved.

## Related

- Phase A (canonical outcome split): `ca1705f`
- Phase B (execution-truth wiring): `51da8cc`
- Phase D (NetEdge live wiring): queued, not yet started.
- Phase E (multiple-testing discipline): queued, not yet started.
