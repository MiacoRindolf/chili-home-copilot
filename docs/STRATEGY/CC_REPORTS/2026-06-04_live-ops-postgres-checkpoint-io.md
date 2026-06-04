# Live-ops pass — Postgres checkpoint/I-O is the autotrader's real bottleneck

**Date:** 2026-06-04
**Type:** live-ops pass (continuation of the Codex live-ops loop)
**Branch state at time of pass:** local checkout on `os-deploy` (284 ahead / 5 behind
`origin/main`, 511 modified files). Live runs from `chili-app:main-clean-*` recovery
containers; the dirty bind-mounted compose stack is stopped.

## TL;DR

The live system is healthy and self-recovering. The alarming
`PendingRollbackError` autotrader tracebacks are **symptoms of Postgres I/O
starvation, not an application bug**. Applied a safe, reversible, no-downtime
Postgres palliative; wrote the durable fix as a runbook
(`docs/RUNBOOKS/POSTGRES_VOLUME_MIGRATION.md`). **No code change, no app restart**
— and notably, the Codex plan's "rebuild latest clean main + restart autotrader"
would have changed nothing (proven below).

## What was observed

Health (all green): `getchili.app/trading` 200; local `:8000`/`:8001` 200;
Postgres + Ollama healthy; autotrader writing a run row ~every 60 s (all legitimate
conservative `skipped`/`blocked` — regime-gate negative, non-positive edge,
drawdown breaker). Main CI green on the latest 3 commits.

The scary signal: a tick that ran **210 s** and failed, with
`PendingRollbackError` cascades. Pulled the contiguous trace:

```
[risk] Portfolio drawdown unavailable; blocking as precaution:
  (psycopg2.OperationalError) server closed the connection unexpectedly
  [SQL: SELECT ... FROM trading_paper_trades WHERE status='closed' ...]
```

## Root cause (evidence chain)

1. Postgres data dir is a **Windows bind mount** (`D:/CHILI-Docker/postgres`),
   DB size **75 GB** → `fsync()` is pathologically slow.
2. Checkpoints take **80–237 s** (sync phase 80–95 s; `longest=36.6s` for one file;
   `sync files=168`).
3. During the sync the autotrader's open transaction stalls → app's per-session
   idle/statement timeout fires → Postgres `FATAL: terminating connection due to
   idle-in-transaction timeout` (07:36:33, 07:43:34) → "server closed the
   connection unexpectedly" → poisoned SQLAlchemy session → the tick's per-alert
   `_audit()` writes fail; one tick ran 210 s (3 ticks skipped:
   "maximum running instances reached").
4. Also `QueryCanceled: canceling statement due to statement timeout` — the app's
   bounded timeouts firing because queries are I/O-starved.
5. Frequency in the log window: **1** hard "tick failed", **24** "tick slow"
   (20–42 s). Chronic slowness, rare hard failure, **zero trade impact** (pre-market,
   crypto-only, all skipped, breaker active). Connections 23/350 — not contention.

Per **Hard Rule 3 (data-first, don't paper over in code)**, patching the audit path
in `auto_trader.py` would mask the I/O problem. Correct fix is at the storage layer.

## Disproved assumption

The Codex progress panel's next steps were "build latest clean main image" +
"restart autotrader from clean image." Verified this would accomplish nothing:

- `auto_trader.py` is **byte-identical** between the deployed image `fc86c6d` and
  main head `a8c7a64` (`git diff` empty; the 3 newer commits are momentum/fast-path
  env tweaks). Deployed container code confirmed to match the git blob.
- So a rebuild+restart would only churn a live trader for no behavioral change.
  Skipped it.

## Action taken (palliative, live, reversible, no restart)

`ALTER SYSTEM` + `pg_reload_conf()` — all reloadable, `pending_restart=f`,
confirmed in the Postgres log:

| setting              | before | after  | effect                                  |
|----------------------|--------|--------|-----------------------------------------|
| `checkpoint_timeout` | 300 s  | 1800 s | 6× fewer time-triggered fsync storms    |
| `max_wal_size`       | 1 GB   | 4 GB   | avoids volume-triggered checkpoints      |
| `wal_compression`    | off    | on     | less WAL volume to write/sync           |

**Held deliberately:** `synchronous_commit` left `on` (turning it `off` is the
biggest per-commit fsync win but carries a sub-second crash-loss window — no
corruption; left as an explicit operator opt-in).

Post-change verification: autotrader still ticking (4 runs / last 5 min); **zero**
tick-failures / connection-drops / FATALs since the change; `/trading` 200.

## Durable fix (planned, needs a window)

`docs/RUNBOOKS/POSTGRES_VOLUME_MIGRATION.md` — move the 75 GB data dir off the
`D:` bind mount onto a Docker named volume (WSL2/VM ext4, ms-fsync). Requires
backup + kill-switch + downtime. Not executed in this pass.

## Secondary observations (not acted on)

- Drawdown breaker showing `-28.0% breached` — the breaker working (Hard Rule 2),
  but worth checking whether −28 % is real vs. partly the known lying-P/L scars
  (CURRENT_PLAN deferred item).
- `broker-sync-worker` is Exited — reconciliation off; may be intentional in the
  clean-recovery posture, but flagging it.
- A stale pending `max_connections` change sits in `postgresql.auto.conf` (logged on
  every reload) — clear it next time a restart is taken.

## Not done / blockers

- No commit/push: the change is runtime DB config (nothing to commit), and the
  checkout is on `os-deploy` with 511 unrelated dirty files (Codex task said "stay
  on main"; not disturbing that tree). These two docs are written but untracked —
  can be landed on `main` via a clean worktree on request.
