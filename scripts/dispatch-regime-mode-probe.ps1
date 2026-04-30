# Probe the actual runtime mode values for regime services.
$out = "scripts/dispatch-regime-mode-probe-output.txt"
"# regime mode probe $(Get-Date)" | Out-File $out -Encoding utf8
function S { param([string]$T,[scriptblock]$B); "" | Add-Content $out; "===== $T =====" | Add-Content $out; try { (& $B 2>&1) | Out-String | Add-Content $out } catch { "ERR $_" | Add-Content $out } }

S "BRAIN_*_MODE env in brain-worker" {
    docker compose exec -T brain-worker env 2>&1 | Select-String -Pattern "^BRAIN_(TICKER|BREADTH|CROSS_ASSET|VOL|INTRADAY|MACRO|PATTERN_REGIME)" | Sort-Object
}
S "BRAIN_*_MODE env in chili" {
    docker compose exec -T chili env 2>&1 | Select-String -Pattern "^BRAIN_(TICKER|BREADTH|CROSS_ASSET|VOL|INTRADAY|MACRO|PATTERN_REGIME)" | Sort-Object
}
S "BRAIN_*_MODE env in scheduler-worker" {
    docker compose exec -T scheduler-worker env 2>&1 | Select-String -Pattern "^BRAIN_(TICKER|BREADTH|CROSS_ASSET|VOL|INTRADAY|MACRO|PATTERN_REGIME)" | Sort-Object
}
S "settings via python introspection in chili" {
    docker compose exec -T chili python -c "from app.config import settings; print('ticker_regime=', repr(settings.brain_ticker_regime_mode)); print('breadth_relstr=', repr(settings.brain_breadth_relstr_mode)); print('cross_asset=', repr(settings.brain_cross_asset_mode)); print('vol_dispersion=', repr(settings.brain_vol_dispersion_mode)); print('intraday_session=', repr(settings.brain_intraday_session_mode)); print('macro_regime=', repr(settings.brain_macro_regime_mode)); print('pattern_regime_perf=', repr(settings.brain_pattern_regime_perf_mode))" 2>&1
}
Write-Host "done"
