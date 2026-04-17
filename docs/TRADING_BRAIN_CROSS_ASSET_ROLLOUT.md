# Phase L.19 — Cross-asset signals v1 rollout

Status: **shadow-only (L.19.1)**. Phase L.19.2 opens the authoritative
consumer path explicitly, gated on governance approval + operator
review of shadow lead/lag accuracy vs realised equity-regime inflection
points.

## What L.19.1 ships

Additive, read-only observability for cross-asset lead/lag between US
equities, Treasuries, credit, USD, crypto, and volatility:

1. New append-only table `trading_cross_asset_snapshots`
   (migration `140_cross_asset_snapshot`).
2. New ORM model `CrossAssetSnapshot` (`app/models/trading.py`).
3. Pure classifier `app/services/trading/cross_asset_model.py`:
   - Bond vs equity lead (TLT vs SPY, 5d / 20d)
   - Credit vs equity lead (Δ(HYG-LQD) vs SPY, 5d / 20d)
   - USD vs crypto lead (UUP vs BTC, 5d / 20d)
   - VIX shock vs breadth-advance-ratio divergence score
   - BTC-SPY rolling beta + correlation (window configurable,
     default 60d)
   - Composite label in
     `{risk_on_crosscheck, risk_off_crosscheck,
       divergence, neutral}`.
4. DB service `app/services/trading/cross_asset_service.py`:
   `compute_and_persist`, `gather_asset_legs`,
   `get_latest_snapshot`, `cross_asset_summary`.
5. APScheduler job `cross_asset_daily` (07:00 local,
   gated by `BRAIN_CROSS_ASSET_MODE`).
6. Diagnostics endpoint
   `GET /api/trading/brain/cross-asset/diagnostics`
   (frozen shape; keys listed below).
7. Structured ops log `[cross_asset_ops] event=...` with events
   `cross_asset_computed`, `cross_asset_persisted`,
   `cross_asset_skipped`, `cross_asset_refused_authoritative`.
8. Release-blocker script
   `scripts/check_cross_asset_release_blocker.ps1`.
9. Docker soak `scripts/phase_l19_soak.py`
   (run inside the `chili` container).

### Frozen `cross_asset_summary` shape

```
{
  "mode": "off" | "shadow" | "compare" | "authoritative",
  "lookback_days": int,
  "snapshots_total": int,
  "by_cross_asset_label": {
    "risk_on_crosscheck": int,
    "risk_off_crosscheck": int,
    "divergence": int,
    "neutral": int
  },
  "by_bond_equity_label": { "risk_on": int, "risk_off": int, "neutral": int },
  "by_credit_equity_label": { "risk_on": int, "risk_off": int, "neutral": int },
  "by_usd_crypto_label": { "risk_on": int, "risk_off": int, "neutral": int },
  "by_vix_breadth_label": { "<label>": int, ... },
  "mean_coverage_score": float,
  "latest_snapshot": { ... } | None
}
```

The diagnostics endpoint wraps this in
`{"ok": true, "cross_asset": {...}}`.

## Release-blocker pattern (mandatory)

A line is a blocker if it contains `[cross_asset_ops]` **and** either:

- `event=cross_asset_persisted` **and** `mode=authoritative`
- `event=cross_asset_refused_authoritative`

Phase L.19.1 is shadow-only; an authoritative event in deploy logs
means config drift has bypassed governance. The gate also fails on
`mean_coverage_score < MinCoverageScore` or
`snapshots_total < MinSnapshots` when a diagnostics dump is provided
via `-DiagnosticsJson`.

### Commands

```powershell
# Against live container logs
docker compose logs chili scheduler-worker brain-worker --since 30m 2>&1 |
  .\scripts\check_cross_asset_release_blocker.ps1

# Against a diagnostics dump
docker compose exec -T chili python -c "
import urllib.request, ssl, json
ctx = ssl.create_default_context(); ctx.check_hostname=False; ctx.verify_mode=ssl.CERT_NONE
r = urllib.request.urlopen('https://localhost:8000/api/trading/brain/cross-asset/diagnostics?lookback_days=14', context=ctx)
print(r.read().decode())
" > ca.json
.\scripts\check_cross_asset_release_blocker.ps1 -DiagnosticsJson .\ca.json -MinCoverageScore 0.5 -MinSnapshots 1
```

Exit 0 = pass; Exit 1 = blocker.

## Rollout order (explicit; do not improvise)

Matches the canonical shadow-rollout pattern used by L.17 and L.18:

1. **off -> shadow** (L.19.1 - this phase):
   - Set `BRAIN_CROSS_ASSET_MODE=shadow` in `.env`.
   - `docker compose up -d --force-recreate chili brain-worker scheduler-worker`.
   - Verify `Cross-asset daily (07:00; mode=shadow)` in
     `scheduler-worker` logs.
   - Verify `/api/trading/brain/cross-asset/diagnostics` returns
     `mode: "shadow"`.
   - Run release blocker on live logs - expect exit 0.
2. **shadow -> compare** (L.19.2 plan): add the parity writer that
   compares daily cross-asset label against a known-good external
   source (e.g. WSJ cross-asset heatmap / Bloomberg dashboard) for a
   minimum of N trading days before opening authoritative.
3. **compare -> authoritative** (L.19.2 hard step): only after
   governance sign-off; the service's `RuntimeError` guard only
   starts meaning something once the flag is authoritative.

## Rollback

Reverse the flip:

1. Set `BRAIN_CROSS_ASSET_MODE=off` in `.env`.
2. `docker compose up -d --force-recreate chili brain-worker scheduler-worker`.
3. Verify `/api/trading/brain/cross-asset/diagnostics` now reports
   `mode: "off"` and the scheduler log no longer registers the
   `cross_asset_daily` job.
4. Re-run the release blocker against a fresh 30m log slice (expect
   exit 0 still; rollback is not a blocker).

The table `trading_cross_asset_snapshots` is **append-only and safe
to retain** on rollback. Hard reset is
`TRUNCATE trading_cross_asset_snapshots` — no other phase depends
on these rows.

## Additive-only guarantees

- `app/services/trading/market_data.py::get_market_regime()` is
  **not modified** by L.19. Pre-L.17 keys remain untouched; L.19 only
  **reads** VIX/breadth context (no writes).
- Phase L.17's `trading_macro_regime_snapshots` rows are **not
  written, updated, or deleted** by L.19. The soak script asserts
  pre/post counts are identical around an L.19 write.
- Phase L.18's `trading_breadth_relstr_snapshots` rows are **not
  written, updated, or deleted** by L.19. Soak verifies this too.
- L.17/L.18 config structs and classifiers are not modified; L.19
  only **reads** their latest snapshots as context echo in
  `payload_json`.
- Existing scheduler jobs retain their cron slots (prescreen 02:00,
  scan 02:30, divergence 06:15, macro 06:30, breadth+RS 06:45);
  L.19's new slot is 07:00 to avoid collisions.

## Verification bundle (Phase L.19.1 sign-off)

- Migration 140 applied: `schema_version` shows
  `140_cross_asset_snapshot`. ✓
- Pure unit tests: `tests/test_cross_asset_model.py` — 22/22 green. ✓
- API smoke test: `tests/test_phase_l19_diagnostics.py` —
  diagnostics frozen shape + lookback clamp 422. ✓
- Release-blocker smoke tests (5/5): clean, auth-persist, refused,
  diag-ok, diag-below-coverage. ✓
- Docker soak: `scripts/phase_l19_soak.py` inside the `chili`
  container — 47/47 checks green (includes L.17 + L.18 additive-only
  guards). ✓
- Live scheduler registration confirmed:
  `Cross-asset daily (07:00; mode=shadow)`. ✓
- Live diagnostics confirms `mode=shadow` after `.env` flip. ✓
- Release blocker on live logs after flip: zero
  `[cross_asset_ops]` blocker lines. ✓
- scan_status frozen contract: unchanged (live probe green). ✓
- L.17 / L.18 pure tests still 17/20 respectively — no regression. ✓

## L.19.2 pre-flight checklist (not yet opened)

Do **not** open L.19.2 without all of the following:

1. User supplies the explicit authoritative consumer path
   (who reads `cross_asset_label`, `bond_equity_label`,
   `crypto_equity_beta`? under which risk authority? what does a
   `divergence` or `risk_off_crosscheck` label cause — size down,
   block entries, raise alerts?).
2. A parity comparison window with an external source (minimum
   20 trading days) in `compare` mode, with drift bounds agreed
   (e.g. composite label disagreement <= 15%).
3. A governance gate is wired so flipping
   `BRAIN_CROSS_ASSET_MODE=authoritative` triggers an audit
   log and optional approval requirement (matches L.17.2 / L.18.2
   pattern).
4. A backfill job (or explicit decision not to backfill) for the
   history window L.19.2's consumers need.
5. Re-run the full release-blocker + soak bundle after the
   authoritative flip.

Until those are in place, the service hard-refuses
`authoritative` with a `RuntimeError` and logs
`event=cross_asset_refused_authoritative` for visibility.
