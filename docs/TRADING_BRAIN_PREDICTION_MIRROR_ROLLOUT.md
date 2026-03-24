# Trading Brain — prediction mirror flags (operational rollout)

Phase **8** artifact: **documentation and ops checks only**. Does **not** change authority code, routers, or `app/config.py` defaults.

**Authority contract** (Phases 5–7) is unchanged: **`tickers=None` stays non-authoritative**; candidate-authoritative mirror reads apply **only** to **non-empty explicit** ticker lists when `brain_prediction_read_authoritative_enabled` is on, with freshness and parity. See [`app/trading_brain/README.md`](../app/trading_brain/README.md).

## Environment variables (`app/config.py` → `.env`)

| Setting | Env variable |
|---------|----------------|
| `brain_prediction_ops_log_enabled` | `BRAIN_PREDICTION_OPS_LOG_ENABLED` |
| `brain_prediction_dual_write_enabled` | `BRAIN_PREDICTION_DUAL_WRITE_ENABLED` |
| `brain_prediction_read_compare_enabled` | `BRAIN_PREDICTION_READ_COMPARE_ENABLED` |
| `brain_prediction_read_authoritative_enabled` | `BRAIN_PREDICTION_READ_AUTHORITATIVE_ENABLED` |
| `brain_prediction_read_max_age_seconds` | `BRAIN_PREDICTION_READ_MAX_AGE_SECONDS` (default `900`) |

After editing `.env`, **recreate** services that load it (e.g. `docker compose up -d --force-recreate chili brain-worker`).

## Rollout order (per environment)

Apply **one step at a time**; **restart/recreate** the app container(s) after each change; run **minimal soak** + **release-blocking check** before the next step.

1. **`BRAIN_PREDICTION_OPS_LOG_ENABLED=true`** — observability only; no read authority.
2. **`BRAIN_PREDICTION_DUAL_WRITE_ENABLED=true`** — mirror writes; still not read-authoritative.
3. **`BRAIN_PREDICTION_READ_COMPARE_ENABLED=true`**, **`BRAIN_PREDICTION_READ_AUTHORITATIVE_ENABLED=false`** — compare-only validation.
4. **`BRAIN_PREDICTION_READ_AUTHORITATIVE_ENABLED=true`** — narrow authoritative reads (explicit tickers only, when Phase 5 rules pass).

## Release-blocking grep rule (hard)

**Do not promote flags** (especially step 4) if this pattern appears in logs.

A line is a **release blocker** if it contains **all** of:

1. **`[chili_prediction_ops]`**
2. **`read=auth_mirror`**
3. **`explicit_api_tickers=false`**

**Pass** = **zero** such lines in the checked window.

**PowerShell (from repo root, `chili` service):**

```powershell
(docker compose logs chili --since 30m 2>&1 | Select-String "chili_prediction_ops") |
  Where-Object { $_.Line -match "read=auth_mirror" -and $_.Line -match "explicit_api_tickers=false" }
```

Empty output → **pass** for this gate. Any line → **stop** and fix before shipping.

**Script (optional helper):** pipe logs or pass `-Path`:

```powershell
docker compose logs chili --since 30m 2>&1 | .\scripts\check_chili_prediction_ops_release_blocker.ps1
```

## Rollback trigger rule

- **Any** line matching the **release-blocking** pattern above, **or**
- **Severe** regression you attribute to flag changes (define your own threshold: e.g. prediction **5xx** rate, timeouts)—document in your runbook.

## Rollback flag order (fast descent)

1. `BRAIN_PREDICTION_READ_AUTHORITATIVE_ENABLED=false`
2. `BRAIN_PREDICTION_READ_COMPARE_ENABLED=false`
3. `BRAIN_PREDICTION_DUAL_WRITE_ENABLED=false`
4. `BRAIN_PREDICTION_OPS_LOG_ENABLED=false` (optional last; may leave **on** briefly to confirm `read=` behavior)

Then **recreate** `chili` / `brain-worker` (and `brain` if it shares the same image + env for predictions).

## Minimal soak requirement (per environment)

After reaching **step 4** (authoritative on), with **ops log on**:

- **≥ 8** requests: `GET https://localhost:8000/api/trading/brain/predictions?tickers=AAPL,MSFT` (adjust host/port; use `curl -k` for self-signed TLS in Docker).
- **≥ 4** requests: `GET https://localhost:8000/api/trading/brain/predictions` (no query — **`tickers=None`** path).

Immediately run the **release-blocking** check on **`chili`** logs for that window. **Worker:** if `brain-worker` runs predictions with the same flags, repeat grep on `docker compose logs brain-worker ...` when relevant.

## Evidence to record

- Date, environment, flag step reached.
- Soak counts (explicit vs none).
- Blocker check: **0 lines** (paste or note).
- Optional: attach sample **safe** `[chili_prediction_ops]` lines (`auth_mirror` with `explicit_api_tickers=true` is expected on explicit traffic when authoritative is on).

## See also

- [`app/trading_brain/README.md`](../app/trading_brain/README.md) — Phases 4–7 technical detail + Phase 7 grep reference.
- [`scripts/check_chili_prediction_ops_release_blocker.ps1`](../scripts/check_chili_prediction_ops_release_blocker.ps1) — scripted blocker scan.
