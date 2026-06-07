# CC_REPORT: db-exit-parity-retention-fix

> **Out-of-band task.** This was an operator-directed DB-health task handed
> directly in the Claude Code session, NOT a `NEXT_TASK.md` item. `NEXT_TASK.md`
> remains `f-position-identity-phase-5i-post-rename-soak` (STATUS: PENDING) and
> was left untouched.

## Context

A multi-lens audit (2026-06-07) found prod Postgres (DB `chili`, container
`chili-home-copilot-postgres-1`, host port 5433) dominated by one runaway
table, `trading_exit_parity_log` — the same table that regrew the DB after the
prior 65→29 GB cleanup. The brief was to fix the **retention mechanism**, not
just the symptom.

Verified live (read-only) before touching anything:

| Fact | Value |
|---|---|
| Table total / heap | 20 GB / 8 GB |
| Rows | 42.68 M (backtest 42.47 M = 99.5%, live 0.21 M) |
| Oldest row | 2026-05-06 (32 d) despite 7-d backtest retention |
| Zero-scan indexes (`idx_scan=0`, all-time) | 7, totaling ~10.4 GB |
| Used indexes | `created_retention` (10), `mode_created` (13), `pattern_created` (112) |
| `autovacuum_count` (whole cluster) | 0 — never completed a pass |
| `n_dead_tup` on parity table | 200 k+ (> default trigger, yet never ran) |
| `shared_buffers` | 128 MB; container shm 64 MB |
| Wraparound | 5.9% of `autovacuum_freeze_max_age` (not urgent) |

Root cause of the growth: `_prune_exit_parity_log` issued exactly **one**
`DELETE ... LIMIT 50000` per **daily** sweep, against ingestion far above one
batch/day — a permanent deficit the prune could never close.

PK-drop safety verified: **no inbound FK** references `id`, **no** insert path
uses `ON CONFLICT (id)`, the retention delete keys on `ctid`, and **no reader**
does a by-id lookup (readers only filter/group by `created_at`/source/pattern).

## What shipped

Branch `chili/exit-parity-retention` → PR (one logical change, 7 files).

1. **Drain-loop prune** (`app/services/trading/data_retention.py`).
   `_prune_exit_parity_log` now loops the batch delete within a single sweep,
   **committing after each batch**, until the eligible set is drained or a
   per-sweep cap is hit. This makes steady-state prune rate ≥ ingestion AND
   bounds transaction size / lock duration (the per-batch commit also closes
   the idle-in-transaction exposure, consistent with the #488/#492 hygiene
   series). Dry-run path reports eligible count without deleting.

2. **One documented setting** (`app/config.py`):
   `brain_retention_exit_parity_max_rows_per_sweep = 5_000_000` — per-sweep
   backstop so a one-time backlog can't turn one sweep into an hours-long
   WAL/dead-tuple spike. Steady-state volume is far below it.

3. **Migration 301** (`app/migrations.py`,
   `_migration_301_exit_parity_log_index_prune_and_autovacuum`, idempotent):
   - `DROP` the 7 verified zero-scan indexes (6 secondary + the pkey
     constraint) → ~10 GB reclaimed, returned to the OS immediately, and 7×
     per-insert write-amplification removed from the hottest-write table.
     Keeps `created_retention`, `mode_created`, `pattern_created`.
   - Pins per-table autovacuum: `vacuum_scale_factor=0`,
     `vacuum_threshold=50000`, `analyze_*` likewise, `vacuum_cost_delay=0` — so
     autovacuum triggers on an **absolute** dead-tuple count (not 20% of an
     ever-growing/stale `reltuples`) and runs to completion at full speed.

4. **One-time heap reclaim** (`scripts/reclaim_exit_parity_log.sql` + `.ps1`).
   DELETE backlog + `VACUUM (FULL, ANALYZE)` to reclaim the 8 GB heap (a heap
   rewrite cannot run inside a migration transaction). Maintenance-window
   runbook in the file header; writers paused.

5. **Starved-table VACUUM** (`scripts/vacuum_autovacuum_starved_tables.sql`).
   Plain `VACUUM (ANALYZE, PARALLEL 0)` for the autovac-starved small tables
   (`trading_snapshots`, `brain_batch_jobs`, `fast_executions_default`,
   `trading_scans`, `trading_alerts`, `trading_breakout_alerts`).

## Verification

- **Live integration** against `chili_test` (standalone harness, since
  removed): drain loop deletes ALL 150 eligible across 3 batches in one sweep;
  per-sweep cap bounds a sweep to 100; dry-run deletes nothing; migration 301
  drops exactly the 7 targets, keeps the 3, sets the 5 reloptions, is
  idempotent on re-run; prune still works after the migration. ALL PASS.
- **pytest** `tests/test_operational_storage_retention.py` — 7 passed
  (added: config default, env override, drain-loop source guard, migration-301
  guard).
- **Migration-ID gate** `scripts/verify-migration-ids.ps1` — PASS (291
  migrations, 0 collisions; 301 free on origin/main).
- **Ran on prod now (safe, non-blocking):** `VACUUM (ANALYZE)` on the 6
  starved small tables — fresh planner stats (fixes the stale-`reltuples`
  autotrader probe timeout: `trading_snapshots` had `reltuples`~362k vs ~20
  live rows). Used `PARALLEL 0` because the container's default 64 MB
  `/dev/shm` makes parallel vacuum workers fail.

## Deferred / operator-coordinated

- **Apply migration 301 + run the heap reclaim** in a low-activity window.
  Migration 301 auto-applies on the next worker restart; index drops take a
  brief ACCESS EXCLUSIVE lock and the first post-migration autovacuum (with
  `cost_delay=0`) will scan the still-large table once, so deploy + reclaim
  together when load is low. The `VACUUM (FULL)` in the reclaim script blocks
  the table for its duration.

## Surprises

- **Autovacuum never *completed*** cluster-wide (`autovacuum_count=0`) even
  though it's enabled and Postgres had been up 23 h with no stats reset. Most
  consistent explanation: a default-throttled pass over a 20 GB table under
  the write firehose never finishes (the counter only increments on
  completion), compounded by stale `reltuples`. The per-table fix + shrinking
  the table resolves it regardless of the exact cause.
- **64 MB `/dev/shm`** in the PG container breaks parallel vacuum (and parallel
  query) — flagged for the audit's point-4 infra follow-up.

## Open questions for Cowork

1. **Backtest parity sampling (audit 1b).** Sampling already runs (5% boring
   holds / 25% agreed closes / always-keep disagreements + drift ≥10 bps), so
   800 k/day is post-sample. Tightening it would cut volume further but changes
   the evidence the exit-engine cutover gate sees — a strategy call, not a
   retention call. Left unchanged; flag if you want it tuned.
2. **Infra (audit 4):** raise PG container mem-limit + `shared_buffers` (~25%
   RAM) and `shm_size` once retention cuts write pressure. E: NVMe is ~4% used.
3. **Schedule:** daily sweep + drain loop is sufficient (drains everything each
   day). Hourly is available as a further lever if needed — not changed.
