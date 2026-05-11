# audit-cpcv-gate-force-eval.ps1 - Phase 0 of f-adaptive-promotion-architecture
#
# Read-only dry-run of check_promotion_ready against ONE pattern. Proves
# the CPCV gate WOULD produce a verdict if it were reached. Combined with
# D1 (audit-cpcv-gate-coverage.ps1) this localizes the funnel break.
#
# Runs in the brain-worker container via `docker exec ... python -c`.
# Wraps the whole evaluation in try/finally with sess.rollback() so
# nothing persists. No DB writes. No handler edits.
#
# Usage:
#   .\scripts\audit-cpcv-gate-force-eval.ps1                 # defaults: pid=731
#   .\scripts\audit-cpcv-gate-force-eval.ps1 -PatternId 1047 -MinTrades 30

param(
    [int]$PatternId   = 731,
    [int]$MinTrades   = 30,
    [int]$NHypotheses = 1
)

$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot\..

$bw  = "chili-home-copilot-brain-worker-1"
$out = "$PSScriptRoot\audit-cpcv-gate-force-eval-$PatternId-out.txt"

"# audit-cpcv-gate-force-eval  pid=$PatternId  start=$((Get-Date).ToUniversalTime().ToString('o'))" |
    Out-File $out -Encoding utf8

# Heredoc-style python block to run inside the brain-worker container.
# Notes:
#  - No early returns before finally; rollback runs unconditionally.
#  - Imports mirror handlers/cpcv_gate.py:handle_backtest_completed.
#  - cpcv_eval_to_scan_pattern_fields gives us the dict that WOULD be
#    written to scan_patterns (we don't write it; we just print it).
$pyPath = [System.IO.Path]::GetTempFileName() + '.py'
$py = @"
import json, sys, traceback
PID = $PatternId
MIN_TRADES = $MinTrades
N_HYPO = $NHypotheses

result = {"pattern_id": PID, "ok": False, "stage": "init"}
try:
    from app.db import SessionLocal
    from app.models.trading import PatternTradeRow as PTR, ScanPattern
    from app.services.trading.mining_validation import check_promotion_ready
    from app.services.trading.promotion_gate import (
        normalize_ptr_row_features,
        cpcv_eval_to_scan_pattern_fields,
    )
except Exception as exc:
    result["stage"] = "import"
    result["error"] = repr(exc)
    print(json.dumps(result, indent=2, default=str))
    sys.exit(0)

sess = SessionLocal()
try:
    result["stage"] = "load"
    pat = sess.get(ScanPattern, PID)
    if pat is None:
        result["error"] = "scan_pattern_not_found"
        result["found"] = False
        print(json.dumps(result, indent=2, default=str))
    else:
        result["found"] = True
        result["pattern_name"] = pat.name
        result["lifecycle_stage"] = pat.lifecycle_stage
        result["promotion_status"] = pat.promotion_status
        result["pattern_evidence_kind"] = getattr(pat, "pattern_evidence_kind", None)
        result["timeframe"] = pat.timeframe

        rows = (
            sess.query(PTR)
            .filter(PTR.scan_pattern_id == PID,
                    PTR.outcome_return_pct.isnot(None))
            .order_by(PTR.as_of_ts.asc())
            .all()
        )
        result["ptr_rows_loaded"] = len(rows)
        result["stage"] = "normalize"

        ensemble = []
        for r in rows:
            fj = r.features_json if isinstance(r.features_json, dict) else {}
            d = normalize_ptr_row_features(
                outcome_return_pct=r.outcome_return_pct,
                as_of_ts=r.as_of_ts,
                ticker=r.ticker,
                timeframe=r.timeframe,
                features_json=fj,
            )
            d["ret_5d"] = float(r.outcome_return_pct or 0.0)
            ensemble.append(d)

        result["stage"] = "check_promotion_ready"
        ok, detail = check_promotion_ready(
            ensemble,
            min_trades=MIN_TRADES,
            n_hypotheses_tested=N_HYPO,
            scan_pattern=pat,
        )
        result["ok"] = bool(ok)
        result["detail_blocked"] = detail.get("blocked")
        result["detail_keys"] = sorted(list(detail.keys()))

        cpcv_payload = detail.get("cpcv_promotion_gate") or {}
        cpcv_dump = {}
        for k in (
            "skipped", "reason", "cpcv_n_paths", "cpcv_median_sharpe",
            "cpcv_median_sharpe_by_regime", "deflated_sharpe", "pbo",
            "promotion_gate_passed", "promotion_gate_reasons", "evaluator",
            "n_labeled_samples", "n_trades", "n_effective_trials",
        ):
            cpcv_dump[k] = cpcv_payload.get(k)
        result["cpcv_promotion_gate"] = cpcv_dump

        # What the handler WOULD have written to scan_patterns:
        try:
            patch = cpcv_eval_to_scan_pattern_fields(cpcv_payload)
            result["scan_pattern_patch"] = patch
        except AssertionError as ae:
            result["scan_pattern_patch_error"] = str(ae)

        # Realized-EV gate (the second blocker in finalize_promotion_with_cpcv)
        result["realized_ev_gate"] = detail.get("realized_ev_gate")

        result["stage"] = "done"
        print(json.dumps(result, indent=2, default=str))
except Exception as exc:
    result["error"] = repr(exc)
    result["traceback"] = traceback.format_exc()
    print(json.dumps(result, indent=2, default=str))
finally:
    # READ-ONLY guarantee: roll back any implicit read transaction and close.
    try:
        sess.rollback()
    except Exception:
        pass
    try:
        sess.close()
    except Exception:
        pass
"@

$py | Out-File $pyPath -Encoding ascii
& docker cp $pyPath "${bw}:/tmp/audit_force_eval.py" 2>&1 | Out-Null
Remove-Item $pyPath -ErrorAction SilentlyContinue

"## docker exec brain-worker python /tmp/audit_force_eval.py" | Add-Content $out
$res = & docker exec -e PYTHONPATH=/app -w /app $bw python /tmp/audit_force_eval.py 2>&1
($res | Out-String).TrimEnd() | Add-Content $out

"" | Add-Content $out
"# end  finish=$((Get-Date).ToUniversalTime().ToString('o'))" | Add-Content $out

Write-Host "force-eval complete: $out"
