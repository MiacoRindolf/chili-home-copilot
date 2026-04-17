# Phase L.18 — Breadth + cross-sectional relative-strength rollout

Status: **shadow-only (L.18.1)**. Phase L.18.2 opens the authoritative
consumer path explicitly, gated on governance approval + operator
review of shadow accuracy vs real regime regimes.

## What L.18.1 ships

Additive, read-only observability for US equity breadth and
cross-sectional relative strength:

1. New append-only table `trading_breadth_relstr_snapshots`
   (migration `139_breadth_relstr_snapshot`).
2. New ORM model `BreadthRelstrSnapshot`
   (`app/models/trading.py`).
3. Pure classifier `app/services/trading/breadth_relstr_model.py`:
   - 11 GICS sector SPDRs (XLK / XLF / XLE / XLV / XLY / XLP /
     XLI / XLB / XLU / XLRE / XLC)
   - SPY / QQQ / IWM benchmarks
   - ETF-basket advance/decline proxy, per-sector RS vs SPY (20d),
     size tilt (IWM-SPY), style tilt (QQQ-SPY),
     leader/laggard sector pick, composite label in
     `{broad_risk_on, mixed, broad_risk_off}`.
4. DB service `app/services/trading/breadth_relstr_service.py`:
   `compute_and_persist`, `gather_universe_members`,
   `get_latest_snapshot`, `breadth_relstr_summary`.
5. APScheduler job `breadth_relstr_daily` (06:45 local,
   gated by `BRAIN_BREADTH_RELSTR_MODE`).
6. Diagnostics endpoint
   `GET /api/trading/brain/breadth-relstr/diagnostics`
   (frozen shape; keys listed below).
7. Structured ops log `[breadth_relstr_ops] event=...` with events
   `breadth_relstr_computed`, `breadth_relstr_persisted`,
   `breadth_relstr_skipped`,
   `breadth_relstr_refused_authoritative`.
8. Release-blocker script
   `scripts/check_breadth_relstr_release_blocker.ps1`.
9. Docker soak `scripts/phase_l18_soak.py`
   (run inside the `chili` container).

### Frozen `breadth_relstr_summary` shape

```
{
  "mode": "off" | "shadow" | "compare" | "authoritative",
  "lookback_days": int,
  "snapshots_total": int,
  "by_breadth_label": {
    "broad_risk_on": int,
    "mixed": int,
    "broad_risk_off": int
  },
  "by_leader_sector": { "<SYM>": int, ... },
  "by_laggard_sector": { "<SYM>": int, ... },
  "mean_advance_ratio": float,
  "mean_coverage_score": float,
  "latest_snapshot": { ... } | None
}
```

The diagnostics endpoint wraps this in `{"ok": true, "breadth_relstr": {...}}`.

## Release-blocker pattern (mandatory)

A line is a blocker if it contains `[breadth_relstr_ops]` **and** either:

- `event=breadth_relstr_persisted` **and** `mode=authoritative`
- `event=breadth_relstr_refused_authoritative`

Phase L.18.1 is shadow-only; an authoritative event in deploy logs
means config drift has bypassed governance. The gate also fails on
`mean_coverage_score < MinCoverageScore` or
`snapshots_total < MinSnapshots` when a diagnostics dump is provided
via `-DiagnosticsJson`.

### Commands

```powershell
# Against live container logs
docker compose logs chili scheduler-worker brain-worker --since 30m 2>&1 |
  .\scripts\check_breadth_relstr_release_blocker.ps1

# Against a diagnostics dump
docker compose exec -T chili python -c "
import urllib.request, ssl, json
ctx = ssl.create_default_context(); ctx.check_hostname=False; ctx.verify_mode=ssl.CERT_NONE
r = urllib.request.urlopen('https://localhost:8000/api/trading/brain/breadth-relstr/diagnostics?lookback_days=14', context=ctx)
print(r.read().decode())
" > br.json
.\scripts\check_breadth_relstr_release_blocker.ps1 -DiagnosticsJson .\br.json -MinCoverageScore 0.5 -MinSnapshots 1
```

Exit 0 = pass; Exit 1 = blocker.

## Rollout order (explicit; do not improvise)

Matches the canonical shadow-rollout pattern used by L.17 (macro regime):

1. **off -> shadow** (L.18.1 - this phase):
   - Set `BRAIN_BREADTH_RELSTR_MODE=shadow` in `.env`.
   - `docker compose up -d --force-recreate chili brain-worker scheduler-worker`.
   - Verify `Breadth + RS daily (06:45; mode=shadow)` in `scheduler-worker` logs.
   - Verify `/api/trading/brain/breadth-relstr/diagnostics` returns
     `mode: "shadow"`.
   - Run release blocker on live logs - expect exit 0.
2. **shadow -> compare** (L.18.2 plan): add the parity writer that
   compares daily classification against a known-good external
   source (e.g. FINVIZ breadth / WSJ sector heat) for a minimum of
   N trading days before opening authoritative.
3. **compare -> authoritative** (L.18.2 hard step): only after
   governance sign-off; the service will start refusing the
   `RuntimeError` guard once the flag actually means something.

## Rollback

Reverse the flip:

1. Set `BRAIN_BREADTH_RELSTR_MODE=off` in `.env`.
2. `docker compose up -d --force-recreate chili brain-worker scheduler-worker`.
3. Verify `/api/trading/brain/breadth-relstr/diagnostics` now reports
   `mode: "off"` and the scheduler log no longer registers the
   `breadth_relstr_daily` job.
4. Re-run the release blocker against a fresh 30m log slice (expect
   exit 0 still; rollback is not a blocker).

The table `trading_breadth_relstr_snapshots` is **append-only and
safe to retain** on rollback. If an operator wants a hard reset, they
can `TRUNCATE trading_breadth_relstr_snapshots` without affecting
any other phase.

## Additive-only guarantees

- `app/services/trading/market_data.py::get_market_regime()` is
  **not modified** by L.18. Its pre-L.17 keys (`spy_direction`,
  `spy_momentum_5d`, `vix`, `vix_regime`, `regime`, `regime_numeric`)
  are untouched.
- Phase L.17's `trading_macro_regime_snapshots` rows are **not
  written, updated, or deleted** by L.18. The soak script asserts
  the row count is stable around an L.18 write.
- L.17's `MacroRegimeConfig` and `classify_trend` primitive are
  **re-used** (import only) so both phases agree on "momentum_20d
  > threshold -> trend up"; L.18 never mutates L.17 config.
- Existing scheduler jobs retain their cron slots
  (prescreen 02:00, scan 02:30, divergence 06:15, macro 06:30);
  L.18's new slot is 06:45 to avoid collisions.

## Verification bundle (Phase L.18.1 sign-off)

- Migration 139 applied: `schema_version` shows
  `139_breadth_relstr_snapshot`. ✓
- Pure unit tests: `tests/test_breadth_relstr_model.py` - 20/20
  green. ✓
- API smoke test: `tests/test_phase_l18_diagnostics.py` -
  diagnostics frozen shape + lookback clamp 422. ✓
- Release-blocker smoke tests (5/5): clean, auth-persist, refused,
  diag-ok, diag-below-coverage. ✓
- Docker soak: `scripts/phase_l18_soak.py` inside the `chili`
  container - 41/41 checks green (includes the L.17 additive-only
  guard). ✓
- Live scheduler registration confirmed:
  `Breadth + RS daily (06:45; mode=shadow)`. ✓
- Live diagnostics confirms `mode=shadow` after `.env` flip. ✓
- Release blocker on live logs after flip: zero
  `[breadth_relstr_ops]` blocker lines. ✓
- scan_status frozen contract: unchanged (live probe green). ✓

## L.18.2 pre-flight checklist (not yet opened)

Do **not** open L.18.2 without all of the following:

1. User supplies the explicit authoritative consumer path
   (who reads `breadth_label`, `leader_sector`, `size_tilt`? under
   which risk authority? what does the consumer do with a
   `broad_risk_off` signal - size down, block, alert?).
2. A parity comparison window with an external source (minimum
   20 trading days) in `compare` mode, with drift bounds agreed
   (e.g. composite label disagreement <= 15%).
3. A governance gate is wired so flipping
   `BRAIN_BREADTH_RELSTR_MODE=authoritative` triggers an audit
   log and optional approval requirement (matches the L.17.2
   checklist).
4. A backfill job (or explicit decision not to backfill) for the
   history window L.18.2's consumers need.
5. Re-run the full release-blocker + soak bundle after the
   authoritative flip.

Until those are in place, the service hard-refuses
`authoritative` with a `RuntimeError` and logs
`event=breadth_relstr_refused_authoritative` for visibility.
