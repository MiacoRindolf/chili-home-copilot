# `fast_alerts` microsecond-duplicate pattern

**Origin:** F-hygiene-3.2 audit (2026-05-03), traced back to F8a's deferred-emit catchup batch.
**Producer:** `app/services/trading/fast_path/scanner.py` (`_drain_pullback_due`).
**Affects:** any consumer that joins `fast_alerts` on the composite key `(ticker, alert_type, fired_at)`.

## The pattern

`fast_alerts` rows can have **identical `(ticker, alert_type, fired_at)` to the microsecond.** This is by design, not a bug.

When `fast-data-worker` restarts, the WS client replays N historical 1m candles on subscribe. Each replayed bar that crosses the `volume_breakout_long` threshold fires immediately, and the F8a deferred-emit scheduler queues a `volume_breakout_pullback_long` for `fired_at + DELAY_S`. All those deferred emits are scheduled with deadlines already in the past, so they ALL drain on the very next book emit — and `_drain_pullback_due` stamps each emitted alert with `fired_at = now()`. Several alerts therefore land at *the same wall-clock microsecond*, distinguishable only by their incrementing `id`.

There is no bug to fix on the producer side: the duplicates ARE distinct alert firings (each is a separately-evaluated trade hypothesis), and adding a `UNIQUE(ticker, alert_type, fired_at)` constraint would silently drop legitimate emits.

**Consumers must be duplicate-tolerant.**

## Canonical query patterns

Use the right pattern for the question you're asking.

### Pattern A — single-row alert lookup (recover scalar features by entry)

When you need to recover a single alert's metadata given an execution row that denormalises `(ticker, alert_type, alert_fired_at)`:

```sql
SELECT a.<columns>
FROM fast_executions e
JOIN fast_alerts a
  ON a.ticker = e.ticker
 AND a.alert_type = e.alert_type
 AND a.fired_at = e.alert_fired_at
WHERE e.id = :eid
ORDER BY a.id DESC
LIMIT 1
```

`ORDER BY a.id DESC LIMIT 1` makes the choice deterministic (most-recent alert wins). The duplicates are functionally identical for the purposes consumers care about (same `signal_score`, same `alert_type`, same source bar), so picking any one is correct — but be deterministic so the same entry always resolves to the same alert across re-runs.

**Use `.first()`** (SQLAlchemy) NOT `.one_or_none()` — `.one_or_none()` raises `MultipleResultsFound` if the JOIN returns >1 even with `LIMIT 1` because the `LIMIT` is applied at SQL level but the assertion is at Python level depending on driver behaviour. `.first()` is unambiguous.

**In-codebase examples:**
- `app/services/trading/fast_path/decay_miner.py:566` (validation lookup in `_handle_exit_inserted`)
- `app/services/trading/fast_path/exit_manager.py:389` (calibration lookup in `_fetch_source_alert_meta`)

### Pattern B — aggregate (COUNT, SUM, AVG, …)

When you need a count or aggregate over executions/exits that originated from a specific alert subset:

```sql
SELECT COUNT(*) FILTER (WHERE entry_execution_id IN (
  SELECT e.id FROM fast_executions e
  JOIN fast_alerts a
    ON a.ticker = e.ticker
   AND a.alert_type = e.alert_type
   AND a.fired_at = e.alert_fired_at
  WHERE a.alert_type = 'volume_breakout_pullback_long'
)) AS pullback_exits
FROM fast_exits;
```

The `IN (SELECT e.id ...)` clause is **automatically DISTINCT-tolerant** because `IN` semantics is "at least one match." Even if the inner JOIN multiplies rows due to dup `fast_alerts`, the outer `IN` reduces to a set membership test against unique execution ids.

**Anti-pattern to avoid:** doing a direct JOIN at the top level —

```sql
-- WRONG: returns 142 (inflated)
SELECT COUNT(*)
FROM fast_exits x
JOIN fast_executions e ON e.id = x.entry_execution_id
JOIN fast_alerts a ON a.ticker=e.ticker
                  AND a.alert_type=e.alert_type
                  AND a.fired_at=e.alert_fired_at
WHERE a.alert_type='volume_breakout_pullback_long';
```

Each `fast_exits` row gets multiplied by the number of dup alerts at its entry's `(ticker, alert_type, fired_at)`. F8a-evaluation-rerun's CC report had this bug (n=142 was actually n=37).

**In-codebase examples:**
- `scripts/dispatch-postgres-integrity.ps1` (probe 4c — pullback exit count)

### Pattern C — canonical-id-keyed (when you have it)

The cleanest case. If a consumer already has `fast_alerts.id` (e.g., the executor's `_fetch_new_alerts`), no JOIN on the composite key is needed:

```sql
SELECT id, ticker, alert_type, fired_at, signal_score, features
FROM fast_alerts
WHERE id > :last_id
ORDER BY id ASC
LIMIT 200
```

Each dup is a distinct id and gets processed individually as a distinct alert event — which IS the producer's intent.

**In-codebase examples:**
- `app/services/trading/fast_path/executor.py:554` (`SELECT MAX(id) FROM fast_alerts`)
- `app/services/trading/fast_path/executor.py:563` (`WHERE id > :last_id`)
- `app/services/trading/fast_path/decay_miner.py:448` (`WHERE id = :id`)

## When to apply which

| You have… | Use pattern |
|---|---|
| The canonical `fast_alerts.id` | C (id-keyed) |
| An execution / exit row with denormalised `(ticker, alert_type, alert_fired_at)` and you need ONE alert's scalar fields | A (`ORDER BY id DESC LIMIT 1` + `.first()`) |
| A count / sum / avg over executions or exits whose alerts match a predicate | B (`IN (SELECT e.id ... )` outer wrap) |

## Affected sites — audit log

As of F-hygiene-3.2 (2026-05-03):

| Site | Pattern | Status |
|---|---|---|
| `migrations.py:14724` | DDL trigger | not a query |
| `db_writer.py:314` | INSERT producer | dup-tolerant by design |
| `decay_miner.py:448` | C — `WHERE id = :id` | canonical |
| `decay_miner.py:566` | A — composite JOIN with `ORDER BY id DESC LIMIT 1` | F-hygiene-2.1 fixed |
| `decay_miner.py:846` (cold-start backfill) | observation-side aggregate | by design — each dup IS a distinct event for Welford |
| `executor.py:554` | C — `MAX(id)` | canonical |
| `executor.py:563` | C — `WHERE id > :last_id` | canonical |
| `exit_manager.py:389` | A — composite JOIN with `LIMIT 1` (now `ORDER BY id DESC LIMIT 1`) | F-hygiene-3.2 cleanup |
| `dispatch-postgres-integrity.ps1` (probe 4c) | B — `IN (SELECT e.id ...)` | correct |

## Future structural considerations

If a future need arises for nanosecond-resolution `fired_at` or a sub-microsecond serial column on `fast_alerts`, that's a structural decision (migration + schema change). Not in scope for hygiene work. Producer-side de-duplication via UNIQUE constraint is **explicitly rejected** because it would silently drop legitimate deferred emits.

## Related runbooks

- `docs/RUNBOOKS/wsl-memory-cap.md` — host protection for the f-leak class.
- `docs/STRATEGY/CC_REPORTS/2026-05-02_f-hygiene-2.md` — F-hygiene-2.1's same-pattern fix in `decay_miner._handle_exit_inserted`.
- `docs/STRATEGY/CC_REPORTS/2026-05-03_f-leak-1.md` — surfaced the JOIN-cardinality inflation in F8a-evaluation-rerun's report (n=142 → 37).
