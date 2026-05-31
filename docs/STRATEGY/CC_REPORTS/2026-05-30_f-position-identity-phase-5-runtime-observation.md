# Phase 5 Runtime Observation

**Date:** 2026-05-30 PT  
**Branch:** `codex/phase5-runtime-observation-report`  
**Base:** `origin/codex/brain-work-done-marker-recovery` at `63a6e9d`

## Verdict

Phase 5 runtime observation is healthy for the current weekend/crypto window, but this is **not** a green light for a broader rename.

The correct architect call remains:

- keep `trading_management_envelopes` as the physical base table
- keep `trading_trades` as the deliberate compatibility view
- keep `Trade` as the deliberate compatibility ORM mapper
- do not public-rename `/trades`, `trade_id`, schema names, or UI labels
- do not start another broad rename slice without a concrete production issue

The system should continue soaking through the next normal market window before this observation task is closed.

## Checks Run

### Rollup probe

`scripts/d-phase5-runtime-observation-probe.py --since-minutes 10`

Result:

- `VERDICT_STATUS=IN_FLIGHT`
- `VERDICT_REASON=mechanical checks green; wait for a normal market-window soak before closeout`
- Phase 5K: `COMPLETE_POSITIVE`
- Phase 5I: `COMPLETE_POSITIVE`
- reader canary: clean
- app Phase 5 schema errors: 0
- Postgres Phase 5 schema errors: 0
- Postgres `schema_version.version` noise in final 10-minute check: 0

This script was added so the next market-window closeout can be repeated mechanically instead of reconstructed from notes.

### Phase 5K live-path parity

`scripts/d-phase5k-live-path-parity-probe.py`

Result:

- `VERDICT_STATUS=COMPLETE_POSITIVE`
- `VERDICT_REASON=6 live-path aggregate checks matched`
- `trading_management_envelopes` is a physical relation
- `trading_trades` is a compatibility view

### Phase 5I post-rename soak probe

`scripts/d-phase5i-post-rename-soak-probe.py`

Result:

- `VERDICT_STATUS=COMPLETE_POSITIVE`
- fresh post-rename data clean
- `decisions=20`
- `envelopes=20`
- `closes=10`
- hard linkage issues: 0
- mismatched rows: 0
- mismatched PnL: 0.0000

### Raw-reader drift canary

`scripts/analyze_phase5_remaining_trade_refs.py --json --include app --fail-on-unexpected-runtime`

Result:

- `ok: true`
- unexpected runtime readers: none
- unexpected runtime mutations: none
- runtime raw `trading_trades` readers remain gone

### Focused tests

`python -m pytest tests/test_phase5_remaining_trade_refs.py tests/test_phase5l_reader_allowlist.py -q`

Result:

- `9 passed`
- one SQLAlchemy sorted-table warning only

## Runtime Log Review

App services checked:

- `chili`
- `scheduler-worker`
- `autotrader-worker`
- `broker-sync-worker`

Observed over the inspection window:

- no `NoReferencedTableError`
- no `UndefinedTable`
- no `UndefinedColumn`
- no `relation ... does not exist`
- no `trading_trades` / `trading_management_envelopes` relation errors
- no Phase 5 app-side schema tracebacks

Non-Phase-5 observations:

- Robinhood phoenix precheck SSL warning, already on the known broker-connectivity surface
- Groq invalid API key suppression log, unrelated to position identity

## Classified Postgres Noise

Postgres logs included test-fixture cleanup noise from local validation activity:

- `TRUNCATE ... RESTART IDENTITY CASCADE`
- deadlocks around test cleanup and concurrent runtime writes

These are not Phase 5 runtime relation-read failures.

One schema-inspection query errored twice:

```sql
SELECT
  COALESCE(MAX(CASE WHEN version ~ '^[0-9]+' THEN substring(version from '^[0-9]+')::int END), 0) AS max_schema_num,
  COUNT(*) AS schema_rows,
  STRING_AGG(version, ', ' ORDER BY version DESC) FILTER (WHERE version ~ '^(284|285|286|287|288|289|290|291|292|293|294|295)_') AS recent_schema_versions
FROM schema_version
```

The live schema column is `schema_version.version_id`, not `version`.

Classification:

- probe/dashboard noise, not a live trading query
- not found in `app`, `scripts`, `docs`, or `tests` on the current codebase
- did not recur in the follow-up log window
- app logs stayed clean while this appeared in Postgres logs

No code fix is required unless the external schema-dashboard/probe source is reintroduced.

## Architect / Data-Science Read

The Phase 5 compatibility boundary is doing its job. The high-risk part of the rename already happened safely: the physical table is `trading_management_envelopes`, while old consumers can still use `trading_trades` through the compatibility view.

What matters from here is not further naming purity. The alpha system benefits more from:

- execution-cost work
- maker-only fill quality
- payoff-aware sizing observation
- pattern lifecycle evidence quality
- runtime drift canaries

Broad renaming now would add operational risk without improving edge, slippage, capital allocation, or broker truth.

## Recommendation

Keep Phase 5 parked in runtime observation.

Close this task only after one normal market window stays clean with:

- Phase 5K positive
- Phase 5I positive
- reader canary clean
- no app-side relation/query errors

Recommended closeout command after the next normal U.S. equity market window:

```powershell
python scripts\d-phase5-runtime-observation-probe.py --since-minutes 390 --market-window-complete
```

Until then, the next best work is concrete trading improvement, not more rename pressure.
