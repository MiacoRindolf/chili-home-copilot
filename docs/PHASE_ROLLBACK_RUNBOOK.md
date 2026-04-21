# Prediction-mirror phase-rollback runbook

**Hard Rule 5 (CLAUDE.md):** the prediction mirror authority contract (phases 3-8 under `app/trading_brain/`) is **frozen**. Changes to the authority surface require a new phase with design + tests + soak + rollout doc. This runbook is for **rollback only** — unwinding a rollout that is actively misbehaving.

Use this runbook when:

- A rolled-out phase flag is producing `[brain_prediction_read_auth] parity_fail` / `mirror_miss` / `stale_or_missing_asof` lines at a rate that exceeds the soak baseline
- The release-blocker grep finds `read=auth_mirror` with `explicit_api_tickers=false` (per CLAUDE.md hard rule on prediction mirror authority)
- You need to step back from an authoritative-read flag to compare-mode while investigating

The migration forward (phase n → phase n+1) is out of scope — use the specific phase rollout doc under `docs/`.

## Release-blocker gate (pre-flight check)

Before rollback or any related change, confirm the release-blocker grep is clean:

```powershell
.\scripts\check_chili_prediction_ops_release_blocker.ps1
```

Expected output: `PASS`. If it finds a `read=auth_mirror` line with `explicit_api_tickers=false`, **that is the blocker** — the authority mirror is being consulted for non-explicit ticker intent, which is the one case the frozen contract forbids. Do not proceed to any rollback action until you have isolated that source; the rollback may itself be the fix, but you must be able to name what you are rolling back first.

## Flag inventory

All live under `app.config.settings`. Defaults are set to the safest non-enabled state:

| Flag | Phase | Meaning when True |
|---|---|---|
| `brain_prediction_dual_write_enabled` | 2-3 | Every legacy prediction write is mirrored into `brain_prediction_*` tables |
| `brain_prediction_read_compare_enabled` | 5 (compare) | Read path fetches both legacy and mirror, logs parity diff, **returns legacy** |
| `brain_prediction_read_authoritative_enabled` | 5+ (auth) | Read path returns mirror data as source of truth (still falls back to legacy on miss / stale / parity failure) |
| `brain_prediction_read_max_age_seconds` | 5+ | Staleness budget on mirror asof; over this → fallback to legacy with `stale_or_missing_asof` log |
| `brain_prediction_mirror_write_dedicated` | 3+ | Dual-write runs on a dedicated SQLAlchemy session; isolates mirror write failures |

Authoritative definitions: `app/trading_brain/infrastructure/prediction_read_phase5.py` and `prediction_mirror_session.py`.

## Rollback options, in increasing blast radius

### 1. Step authoritative → compare

Least invasive. The mirror is still populated and checked; the legacy path is authoritative again.

```bash
# Stop the app containers
docker compose stop chili brain-worker

# Edit .env:
#   BRAIN_PREDICTION_READ_AUTHORITATIVE_ENABLED=false
#   BRAIN_PREDICTION_READ_COMPARE_ENABLED=true
# (leave BRAIN_PREDICTION_DUAL_WRITE_ENABLED=true)

docker compose up -d chili brain-worker

# Verify
.\scripts\check_chili_prediction_ops_release_blocker.ps1
```

Expected log shift: `[brain_prediction_read_auth]` lines disappear; `[brain_prediction_read_compare]` lines remain; legacy reads are authoritative.

### 2. Step compare → shadow (dual-write only)

Disables every read-path consult of the mirror. Mirror is still written but never consulted. Safe default if the compare logs are noisy.

```
BRAIN_PREDICTION_READ_AUTHORITATIVE_ENABLED=false
BRAIN_PREDICTION_READ_COMPARE_ENABLED=false
BRAIN_PREDICTION_DUAL_WRITE_ENABLED=true
```

### 3. Full rollback (automated)

Use the bundled script:

```powershell
.\scripts\rollback-prediction-mirror.ps1
```

It:

- Backs up `.env` to `.env.bak.<timestamp>`
- Flips all `BRAIN_PREDICTION_*` flags to `false`
- Recreates `chili` and `brain-worker` containers
- Runs `.\scripts\check_chili_prediction_ops_release_blocker.ps1` and exits non-zero if the grep still finds the blocker

The script is idempotent: running it twice in a row on an already-rolled-back system leaves `.env` unchanged after the first flip (comparing values before writing).

### 4. Emergency — disable dual-write

Only do this if dual-write itself is the failure (mirror writes are blocking learning cycles, corrupting state, or throwing at a rate that keeps the brain worker from finishing a cycle):

```
BRAIN_PREDICTION_DUAL_WRITE_ENABLED=false
```

This freezes the mirror. Reads are automatically irrelevant since nothing new is written. The mirror data stays in-place and can be resumed later; it does not reset or truncate the tables.

**Never delete the `brain_prediction_*` tables** to recover. That destroys the backfill and invalidates every future phase gate. If you truly need to reset, snapshot first and open an incident.

## Migrations

Rolling back a phase flag does **not** roll back any migration. Migration IDs are sequential and never reused (see `app/migrations.py` header for the contract). If a migration applied during the rollout introduced a column the mirror depends on, leave the column in place — it is harmless with the flags off.

## Verification after any rollback

1. Release-blocker grep clean:
   ```powershell
   .\scripts\check_chili_prediction_ops_release_blocker.ps1
   ```
2. Application log shows the legacy path is handling reads (no `[brain_prediction_read_auth]` at the authoritative step after the flip took effect).
3. `scripts/verify-migration-ids.ps1` passes (should be unaffected by a rollback, but this confirms the schema is coherent).
4. Brain worker completes at least one full learning cycle (≈ 13 steps per `learning.py`) without `[brain_prediction_dual_write]` WARNING-level errors. Grep the log for `[brain_io]` step markers.
5. Open a test session via `/chat` → trading → run a forecast; the returned prediction matches what the legacy path produces (same fingerprint, same scores).

## What NOT to do

- Do not hot-flip flags without restarting the chili + brain-worker containers. Startup is where the flag is read into the per-request code path. A live-flip can leave one worker reading the mirror while another reads legacy, producing drift.
- Do not edit `prediction_read_phase5.py` or the mirror-write path to "manually short-circuit" the issue. That is an authority contract change and is frozen.
- Do not delete `.env.bak.*` files until the incident is closed.
- Do not reset the kill switch or drawdown breaker as part of a phase rollback. They are orthogonal concerns; if both are active at the same time, see the corresponding runbooks.

## Audit trail

Every rollback script run logs to the container logs and to the `.env.bak.<timestamp>` filename. Keep the backup files until the postmortem is filed.
