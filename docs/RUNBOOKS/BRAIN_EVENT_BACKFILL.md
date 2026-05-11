# Brain event backfill (Phase 1c)

Operator runbook for `scripts/brain-event-backfill.ps1` — the
controlled mechanism to bring ~4,000 historical `outcome`/`done`
orphans in `brain_work_events` back into the unified queue so the
Phase 1b handlers process them.

**Author:** 2026-05-11 (Phase 1c of f-adaptive-promotion-architecture).
**Brief:** `docs/STRATEGY/QUEUED/f-brain-event-kind-backfill.md`
**Parent runbook (Phase 1b):** `docs/runbooks/BRAIN_WORK_EVENT_KIND.md`
**Pre-flight memos:**
- `docs/AUDITS/2026-05-11_backfill_safety_backtest_completed.md`
- `docs/AUDITS/2026-05-11_backfill_safety_breakout_alert_resolved.md`

## Cross-link to Phase 1b

Phase 1b shipped the unified `brain_work_events` queue model (work
and outcome events transit the same `pending → processing → done`
lifecycle). The flag `chili_brain_outcome_claimable_enabled` gates
the unification. **Phase 1c assumes Phase 1b is on in prod and has
soaked for 24h.** Re-read `docs/runbooks/BRAIN_WORK_EVENT_KIND.md`
sections "How to flip the flag" and "Observability" before starting
a backfill.

## Prereq checklist

Verify ALL of the following before running the script in live mode:

- [ ] `SELECT chili_brain_outcome_claimable_enabled FROM trading_settings WHERE id=1;`
      returns `true` in the **production** `chili` database.
- [ ] The brain-worker has been restarted **after** the flag flip
      (BaseSettings is read once at import; SQL-only flip does NOT
      propagate).
- [ ] At least 24h of brain-worker logs show all 9 handler prefixes
      firing without retry pile-up. Use the grep from Phase 1b
      runbook section "Verify in logs".
- [ ] No active incident is open against `brain_work_events` (no
      stuck leases, no dead-letter spike).
- [ ] Operator has a fresh snapshot of `brain_work_events` for
      baseline:
      ```sql
      SELECT event_kind, event_type, status, COUNT(*) AS n
      FROM brain_work_events
      WHERE domain = 'trading'
      GROUP BY event_kind, event_type, status
      ORDER BY event_kind, event_type, status;
      ```
- [ ] Operator has snapshots of any aggregates the backfill will
      touch (e.g. for `breakout_alert_resolved`: `scan_patterns`
      win_rate/avg_return_pct/trade_count and `trading_insights`
      confidence — see D2b memo).

## Recommended order

Smallest blast radius first. Verify the pipeline end-to-end before
unleashing the larger sets.

| Step | event_type                  | rows  | gate                             |
|------|-----------------------------|-------|----------------------------------|
| 1    | `paper_trade_closed`        | 1     | runbook + memo D2a is sufficient |
| 2    | `live_trade_closed`         | 4     | runbook + memo D2a is sufficient |
| 3    | `broker_fill_closed`        | 131   | runbook + memo D2a is sufficient |
| 4    | `market_snapshots_batch`    | 179   | **GATED** — see below            |
| 5    | `backtest_completed`        | 1055  | memo D2a (REQUIRED)              |
| 6    | `breakout_alert_resolved`   | 2659  | memo D2b (REQUIRED)              |

Run each step in dry-run mode first; only proceed to live mode after
inspecting the candidate set.

## GATED event types

### `market_snapshots_batch`

The target handler is `mine_patterns` (via the `regime_ledger`
handler family). `mine_patterns` has **no event-level dedupe** —
this is documented in Phase 1b runbook and re-flagged here.

**Do not run** `brain-event-backfill.ps1 -EventType market_snapshots_batch -DryRun:$false`
until one of:

1. The `mine_patterns` inner contract is verified — i.e., a test
   demonstrates that running it twice on the same snapshot does not
   double-write pattern evidence, OR
2. A separate D2c memo is authored that explicitly authorizes the
   replay with a documented bounded-mutation argument (analogous to
   the EWMA-convergence argument in `breakout_outcomes` memo D2b).

The script will warn and pause 5s before any live run targeting
this event type. **Do not bypass the pause** — it exists to give
the operator a last chance to abort if this checklist was skipped.

## Script invocation

The script is `scripts/brain-event-backfill.ps1`. All invocations
run on the Windows host; the script shells into the running
`postgres` container via `docker compose exec -T postgres psql`.

### Dry-run (default, mandatory first step)

```powershell
.\scripts\brain-event-backfill.ps1 -EventType paper_trade_closed
```

This prints:
- the candidate row count,
- the first 20 candidate IDs,
- the wall-clock estimate based on the 30s inter-batch sleep,
- and exits without touching the DB.

### Live run (requires explicit DryRun:$false)

```powershell
.\scripts\brain-event-backfill.ps1 -EventType paper_trade_closed -DryRun:$false
```

Common flags:

| flag           | default          | meaning                                          |
|----------------|------------------|--------------------------------------------------|
| `-EventType`   | (required)       | Whitelist of 7 known Phase 1a orphan types.      |
| `-BatchSize`   | `8`              | Rows per UPDATE statement.                       |
| `-MaxRows`     | `0` (= all)      | Cap candidate selection. Use for staged waves.   |
| `-DryRun`      | `$true`          | Preview only. `-DryRun:$false` to apply.         |

The 30s inter-batch sleep is hardcoded; you cannot override it from
the command line.

### Re-running is safe

The script tags every flipped row with
`payload->>'backfill_source' = 'phase_1c_backfill_2026_05_11'`. The
candidate selector excludes any row already carrying that marker, so
a second run with the same `-EventType` only picks up rows that
weren't flipped the first time (e.g., new rows added since the last
run, or rows that the dispatcher returned to `done` and then a NEW
organic event re-orphaned — unlikely but defended against).

## Reading progress

### Live log

`scripts/brain-event-backfill-progress.log` is appended on every
live run. Entries are tab-separated:

```
<iso8601-utc>\tSTART\tevent_type=...\ttotal=...\tbatch_size=...\tmarker=...
<iso8601-utc>\tBATCH\tevent_type=...\tbatch=K/N\tapplied=...\tcumulative=K/N\tfirst_id=...\tlast_id=...
<iso8601-utc>\tDONE\tevent_type=...\tcumulative=...\telapsed_sec=...
```

If the kill switch fires, a `HALTED` row replaces the trailing
`DONE`.

### Queue snapshot

While a backfill is in flight, watch the queue with the Phase 1b
snapshot query:

```sql
SELECT event_kind, event_type, status, COUNT(*) AS n
FROM brain_work_events
WHERE domain = 'trading'
  AND status IN ('pending', 'processing', 'retry_wait', 'dead')
GROUP BY event_kind, event_type, status
ORDER BY event_kind, event_type, status;
```

The dispatcher should drain the `pending` count back toward zero
each cycle. If `dead` starts growing, kill the backfill and
investigate.

### Handler-specific verification

| event_type                | grep for                                    |
|---------------------------|---------------------------------------------|
| `backtest_completed`      | `[brain_work:cpcv_gate]`                    |
| `breakout_alert_resolved` | `[brain_work:breakout_outcomes]`            |
| `market_snapshots_batch`  | `[brain_work:regime_ledger]` (and `mine`)   |
| `broker_fill_closed`      | `[brain_work:execution_robustness]`         |
| `live_trade_closed`       | `[brain_work:live_drift]`                   |
| `paper_trade_closed`      | `[brain_work:live_drift]`                   |
| `pattern_eligible_promotion` | `[brain_work:promote]`                   |

```bash
docker compose logs --tail 500 brain-worker | grep "[brain_work:cpcv_gate]"
```

## Kill switch

To halt a running backfill after the current batch:

```powershell
New-Item scripts/brain-event-backfill-stop.flag -ItemType File -Force
```

The script polls for this file at the top of each batch iteration.
On detection it writes a `HALTED` log entry and exits 0. **The
dispatcher continues to drain rows already flipped** — the
kill switch only stops further `done → pending` flips, it does not
recall any rows.

To re-enable, remove the flag:

```powershell
Remove-Item scripts/brain-event-backfill-stop.flag
```

## Rollback

Per-event-type rollback SQL is given in each memo (D2a and D2b).
General template:

```sql
UPDATE brain_work_events
SET status = 'done',
    processed_at = CURRENT_TIMESTAMP,
    attempts = 0,
    lease_holder = NULL,
    lease_expires_at = NULL
WHERE domain = 'trading'
  AND event_kind = 'outcome'
  AND event_type = '<EVENT_TYPE>'
  AND payload->>'backfill_source' = 'phase_1c_backfill_2026_05_11'
  AND status IN ('pending', 'retry_wait', 'processing');
```

This drains in-flight backfill rows back to `done` so the dispatcher
ignores them. **It does NOT reverse side effects** that already
fired (cpcv_gate writes to `scan_patterns.cpcv_*`,
breakout_outcomes writes to `scan_patterns` aggregates +
`trading_insights`). Side-effect reversal is operator + DBA work and
is per-handler:

- **cpcv_gate side effects:** the `cpcv_shadow_eval` table preserves
  every gate decision; revert by restoring `scan_patterns.cpcv_*`
  columns to their pre-backfill values from a `pg_dump` snapshot or
  by accepting that the gate decision is now authoritative.
- **breakout_outcomes side effects:** as noted in memo D2b, the
  aggregate is convergent, so letting one organic event fire will
  land the same value the backfill produced. There is no
  pre-backfill snapshot to revert to unless one was captured.

If a rollback is in scope, also flip the Phase 1b flag off (see
parent runbook section "How to roll back") so no further outcome
rows claim while the situation is investigated.

## Open question (deferred)

Should the script auto-pause when the prod brain-worker dispatcher
is observed to be falling behind (e.g., growing `pending` count)?
Currently the operator drives all pacing via `-MaxRows` waves and
the kill-switch flag. An auto-throttle would require the script to
query `brain_work_events` between batches — added complexity for
limited gain when the operator is the gate by design. Re-open if
Phase 2+ load makes manual pacing infeasible.
