# Trading brain — PIT hygiene + historical universe snapshot (Phase C)

Frozen rollout doc for the Phase-C shadow rollout of the PIT hygiene audit
(`trading_pit_audit_log`) and historical universe snapshot
(`trading_universe_snapshots`). Follows the same rollout ladder as the
prediction mirror, NetEdgeRanker, ExitEngine, and Economic-truth ledger.

## What Phase C gives you

1. An explicit **point-in-time contract** for mining-DSL condition fields.
   Every field used in `ScanPattern.rules_json[conditions][*].indicator`
   (or `ref`) is classified as `pit`, `non_pit`, or `unknown` against a
   canonical allow/deny list.
2. A **per-pattern audit log** that records the classification on every
   learning cycle when the feature is enabled.
3. A **historical universe snapshot** table so later phases can answer
   "was ticker `T` in our tradable universe on date `D`?" without
   re-fetching from live sources.
4. A **diagnostics endpoint** (`GET /api/trading/brain/pit/diagnostics`)
   that rolls the most-recent audit per pattern up into a shadow
   dashboard.
5. A **release blocker** (`scripts/check_pit_release_blocker.ps1`) that
   fails when `[pit_ops]` lines carry `mode=authoritative` before the
   explicit cutover, or (optionally, via `-PatternsJson`) when any
   pattern is violating in shadow.

## Rollout ladder

| Stage            | `BRAIN_PIT_AUDIT_MODE` | Ops log emitted? | Diagnostics populated? | Release blocker must pass? |
|------------------|------------------------|------------------|------------------------|----------------------------|
| Off              | `off`                  | No               | No (empty)             | Trivially yes              |
| Shadow (Phase C) | `shadow`               | Yes              | Yes                    | Yes — zero `mode=authoritative` lines |
| Compare          | `compare`              | Yes              | Yes                    | Yes                        |
| Authoritative    | `authoritative`        | Yes              | Yes                    | **No** — cutover has landed |

Phase C only ships stages `off -> shadow`. Advancing to `compare` or
`authoritative` is a separate, named phase that requires its own plan,
soak, and blocker re-run.

## Forward path

1. Add to container `.env`:
   ```
   BRAIN_PIT_AUDIT_MODE=shadow
   BRAIN_PIT_AUDIT_OPS_LOG_ENABLED=true
   ```
2. Recreate the `chili` + `brain-worker` services so the new env is in
   the container process:
   ```
   docker compose up -d --force-recreate chili brain-worker
   ```
3. Trigger (or wait for) a learning cycle; each run writes one
   `trading_pit_audit_log` row per audited pattern and one `[pit_ops]`
   line per audit.
4. Inspect the shadow dashboard:
   ```
   curl -sk https://localhost:8000/api/trading/brain/pit/diagnostics
   ```
5. Grep the release blocker against real logs:
   ```
   docker compose logs chili --since 30m 2>&1 |
       .\scripts\check_pit_release_blocker.ps1
   ```

## Rollback

1. Flip the env back to `off` and recreate services:
   ```
   BRAIN_PIT_AUDIT_MODE=off
   docker compose up -d --force-recreate chili brain-worker
   ```
2. The shadow hook becomes a no-op; the tables stay in place (cheap to
   keep; safe to truncate manually if desired).
3. Re-run the release blocker against fresh logs to confirm no stray
   `[pit_ops]` lines.

## Ops log format

One INFO line per audited pattern, stable field order:

```
[pit_ops] mode=<off|shadow|compare|authoritative> pattern_id=<int> name="..." lifecycle=<stage> pit=<int> non_pit=<int> unknown=<int> agree=<true|false>
```

* `mode` — effective audit mode at write time.
* `pattern_id` — `ScanPattern.id` being audited.
* `name` — sanitized to ≤60 chars; quotes and newlines stripped.
* `lifecycle` — `candidate | backtested | validated | challenged | promoted | live | decayed | retired`.
* `pit`, `non_pit`, `unknown` — per-classification field counts.
* `agree` — `true` iff `non_pit + unknown == 0`.

## Diagnostics endpoint

`GET /api/trading/brain/pit/diagnostics?lookback_hours=N`

Response:

```json
{
  "ok": true,
  "pit": {
    "ok": true,
    "mode": "shadow",
    "lookback_hours": 24,
    "audits_total": 42,
    "patterns_audited": 18,
    "patterns_clean": 16,
    "patterns_violating": 2,
    "top_violators": [
      {"pattern_id": 123, "name": "...", "lifecycle": "validated",
       "non_pit_fields": ["future_return_5d"], "unknown_fields": []}
    ],
    "forbidden_hits_by_field": {"future_return_5d": 1},
    "unknown_hits_by_field": {"some_secret_feature": 1}
  }
}
```

Only the **most-recent** audit per `pattern_id` within the lookback is
counted toward `patterns_*`; total audit rows are exposed separately as
`audits_total`.

## Mandatory release blocker

`scripts/check_pit_release_blocker.ps1`

1. Pipe `docker compose logs chili --since 30m` through the script.
2. Exit `0` = no `[pit_ops] mode=authoritative` lines (required for
   Phase C to stay valid).
3. Exit `1` = deploy leak. Rollback immediately.
4. Optional `-PatternsJson <path>` reads a saved diagnostics payload and
   additionally fails when `patterns_violating > 0`, giving CI a pre-
   cutover gate.

## PIT contract — allowlist extension policy

`app/services/trading/pit_contract.py` is the **only** source of truth
for PIT classification. Any new indicator field added to
`trading_snapshots.indicator_data` **must** be declared in
`ALLOWED_INDICATORS` before it can appear in a mined rule. Otherwise the
auditor flags it as `unknown`, and the `-PatternsJson` gate will fail CI.

Conversely, any field that encodes future information (labels, forecasts,
triple-barrier outcomes) must live in `FORBIDDEN_INDICATORS` so it is
actively rejected.

Cross-timeframe prefixes (`1d:rsi_14` etc.) are recognised — the prefix
is stripped before classification. Unknown timeframe prefixes stay
`unknown` on purpose.

## Known limitations (explicit)

* **Advisory only** — Phase C does not quarantine, deactivate, or
  otherwise mutate any pattern. Phase J owns CUSUM/Brier-driven
  quarantine.
* **Candidates excluded by default** — the audit skips
  `lifecycle_stage == "candidate"` because mining emits and discards
  many candidate patterns per cycle. Pass
  `lifecycle_stages=("candidate",)` to `audit_active_patterns` to audit
  them manually.
* **No automatic universe backfill** — `trading_universe_snapshots` is
  empty on day one; callers populate it opportunistically. Phase D / F
  will backfill meaningfully.
* **User-submitted patterns** — audited on the same terms as mined
  patterns (same allow/deny list). If user pipelines become richer than
  the miner, extend the allowlist explicitly in code review.
* **Classification is field-name-based, not provenance-based** —
  renaming a field does not change its PIT safety; only the contract
  does.
