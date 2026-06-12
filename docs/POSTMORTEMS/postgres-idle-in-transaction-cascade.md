# Postmortem: Postgres idle-in-transaction cascade

**Status:** hygiene fixes shipped across 2026-06 (PRs #488–#610 series).
Keep for institutional memory.

## Root cause

System-wide `OperationalError` / `PendingRollbackError` /
`DetachedInstanceError` storms traced to **sessions held open in
transaction while doing slow work** (LLM calls, network, long loops).
The dominant holder was the work-ledger dispatcher keeping
`brain_work_events` loaded >120s; others included backtest param sets,
pattern position monitoring, and a viability-vs-viability deadlock fixed
by deterministic `(symbol, variant_id)` upsert ordering.

## The rules that keep it fixed

- Never hold a DB transaction across an LLM or network call — detach or
  close first, reopen after.
- Per-item sessions in loops (per-trade, per-parent, per-event), not one
  session for the whole sweep.
- Best-effort writes get savepoints; phase boundaries get explicit
  rollbacks.
- The gateway must NEVER share the caller's session: its
  rollback-on-error discards the caller's uncommitted rows (this exact
  bug deleted autopilot chat messages — fixed by passing db=None).

## Where to look on recurrence

`pg_stat_activity` for `idle in transaction` with old `xact_start`; the
holder's query text identifies the module.
