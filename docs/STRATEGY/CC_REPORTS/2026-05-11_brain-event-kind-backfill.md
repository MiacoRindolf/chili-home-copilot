# CC_REPORT: f-brain-event-kind-backfill (Phase 1c)

**Date:** 2026-05-11.
**Brief:** `docs/STRATEGY/QUEUED/f-brain-event-kind-backfill.md`.
**Parent initiative:** `docs/STRATEGY/QUEUED/f-adaptive-promotion-architecture-2026-05-11.md`.
**Phase 1b CC_REPORT:** `docs/STRATEGY/CC_REPORTS/2026-05-11_brain-event-kind-unify.md`.
**Phase 1b runbook:** `docs/runbooks/BRAIN_WORK_EVENT_KIND.md`.

## What shipped

Execution-only delivery — script + memos + runbook. **No `app/`
code, no migrations, no schema changes.** The operator drives all
UPDATE runs.

| Deliverable | Path |
|---|---|
| D1 — backfill script        | `scripts/brain-event-backfill.ps1`                                          |
| D2a — pre-flight memo       | `docs/AUDITS/2026-05-11_backfill_safety_backtest_completed.md`              |
| D2b — pre-flight memo       | `docs/AUDITS/2026-05-11_backfill_safety_breakout_alert_resolved.md`         |
| D3 — operator runbook       | `docs/runbooks/BRAIN_EVENT_BACKFILL.md`                                     |
| D4 — this report            | `docs/STRATEGY/CC_REPORTS/2026-05-11_brain-event-kind-backfill.md`          |

Files touched: 5 new files.
Migrations added: 0.

### D1 contract (anchored back to the hard constraints)

- `-EventType` is **required** (`[Parameter(Mandatory=$true)]`) and
  whitelist-validated against the 7 known Phase 1a orphan types.
  Unknown types exit non-zero before any SQL.
- `-DryRun` defaults to `$true`. Live mode requires
  `-DryRun:$false`.
- Inter-batch sleep is hardcoded (`$INTER_BATCH_SLEEP_SECONDS =
  30`). No command-line override.
- Backfill marker is `phase_1c_backfill_2026_05_11` written to
  `payload->>'backfill_source'` via `jsonb_set`. The candidate
  selector excludes any row already carrying that marker
  (`NOT LIKE 'phase_1c_backfill_2026_05_11%'`), making re-runs
  idempotent.
- Kill switch: `Test-Path scripts/brain-event-backfill-stop.flag` at
  the top of each batch iteration. Exits 0 with a `HALTED` log
  entry on detection.
- Progress log: `scripts/brain-event-backfill-progress.log`
  (tab-separated, `START` / `BATCH` / `DONE` or `HALTED`).
- Defense against the `uq_brain_work_events_open_dedupe` unique
  partial index: candidate SELECT carries a `NOT EXISTS` guard
  against any competing row in `pending/processing/retry_wait`
  sharing the same `dedupe_key`.
- `market_snapshots_batch` is **GATED**: the script warns and pauses
  5s before any run targeting that event type. The pause is
  intentional — see Surprises / deviations below.
- Transport: `docker compose exec -T postgres psql -U chili -d chili`
  (codebase convention). The whitelisted event_type string is the
  only operator-supplied value that reaches SQL; row IDs are
  produced by the script's own SELECT, never from user input.

## Verification

### Static (PowerShell AST)

```
[System.Management.Automation.Language.Parser]::ParseFile(
    'scripts/brain-event-backfill.ps1', [ref]$tokens, [ref]$errs)
```

Result: zero parse errors after fixing one PS variable-colon
escape (`"$batchCount:"` parsed as a scope qualifier — replaced
with an explicit `-f` format-string call).

### Argument-handling walkthrough

| invocation                                              | result                                                       |
|---------------------------------------------------------|--------------------------------------------------------------|
| `brain-event-backfill.ps1` (no args)                    | PowerShell rejects with "missing mandatory parameter".       |
| `brain-event-backfill.ps1 -EventType unknown_type`      | Script exits 2 with whitelist error message (no SQL issued). |

Neither invocation touches the database. Both behaviours are the
intended fail-fast gates from the brief's hard constraints (#3 and
#4 in `f-brain-event-kind-backfill.md`).

### Live-mode walkthrough

Not executed. Per the operator brief ("the actual backfill UPDATE
runs are operator-controlled via the script") and the explicit
instruction to leave UPDATE runs to the operator, no row was
flipped from this session.

## Surprises / deviations

### One judgment call (resolved during plan, not deviating now)

The brief D2 names exactly two pre-flight memos (the two large
event types). `market_snapshots_batch` is a third hard-constraint
case (mining inner-contract). Operator approved the plan's
disposition: encode the gate as a script-level warning + a runbook
section + an explicit "GATED event types" footer in **both** D2
memos, rather than spawn a third memo file. The script will warn
and pause 5s before any live run targeting that event type, giving
the operator a last-mile abort path even if the runbook was
skipped.

### Live-mode psql transport

The approved plan sketched `psql --csv -h ... -U ... -d ...` with
`PGPASSWORD`. The shipped script instead pipes SQL via stdin to
`docker compose exec -T postgres psql -U chili -d chili` — the
codebase convention used by every other ops script in
`scripts/dispatch-*.ps1`. This avoids a host-psql install
dependency on the operator's Windows workstation and keeps the
credential path inside the container. Same SQL, same parameter
hygiene (whitelisted event_type + script-generated integer ID
array; no operator string interpolation). Flagging it here because
it diverges from the plan's literal wording.

### `uq_brain_work_events_open_dedupe` collision guard

Not specified in the brief; surfaced while reading
`app/migrations.py:5842-5849`. The partial unique index on
`dedupe_key WHERE status IN ('pending', 'processing', 'retry_wait')`
means a naive `done → pending` flip can fail if an organic row with
the same `dedupe_key` is already non-terminal. The candidate query
carries a `NOT EXISTS` guard so the script silently skips any row
that would collide rather than aborting mid-batch. Documented in
the D1 file header.

### Backfill marker

The brief listed two suggestions; the approved plan picked
`phase_1c_backfill_2026_05_11` (date-anchored). Shipped as-is.

## Deferred

- **Live UPDATE runs.** Per brief, operator-controlled. No rows
  have been flipped in this session.
- **`mine_patterns` inner-contract verification.** Required before
  any `market_snapshots_batch` backfill. Not in this brief's scope.
  See open question below.
- **Auto-throttle on dispatcher backlog.** Considered and rejected
  in the runbook's "Open question (deferred)" section. Operator is
  the gate by design.

## Open questions for Cowork

1. **`mine_patterns` inner contract.** The Phase 1b runbook noted
   `mine_patterns` has no event-level dedupe, and the brief
   inherited that as a hard constraint. The 179
   `market_snapshots_batch` rows are blocked behind a gate but
   haven't been actively unblocked. Does Phase 2/3 want a separate
   brief for the mining contract memo (D2c equivalent), or is that
   work folded into the regime-ledger / mining refactor?

2. **`pattern_eligible_promotion` row count = 0 at brief time.**
   Phase 1c will likely generate the first organic
   `pattern_eligible_promotion` rows as cpcv_gate verdicts fire on
   the 1055 `backtest_completed` replays. The script's whitelist
   includes that event type for symmetry, but no rows match yet.
   Confirm Phase 2+ owns the path that consumes those rows
   (`f-adaptive-cpcv-gate` already shipped; presumably yes).

3. **Wave sizing for `breakout_alert_resolved`.** Memo D2b argues
   the EWMA blend converges after ~10 events per asset_type/tier
   bucket, so the tail of the 2659-row set is effectively no-op
   work. Does Cowork want to recommend an explicit wave cap (e.g.,
   `-MaxRows 200`) and skip the remainder, or run the full set as
   audit trail for the cpcv-relevant fields (`scan_patterns.win_rate`
   etc.)?
